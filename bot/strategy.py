"""
策略核心: 锚定均线中性网格引擎.
被回测和实盘共用 —— 引擎只产生"意图"(开仓/平仓信号), 不直接碰交易所.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math
import logging

log = logging.getLogger("strategy")


class Side(str, Enum):
    LONG = "long"    # 锚点下方: 低买高卖
    SHORT = "short"  # 锚点上方: 高卖低买


class LevelState(str, Enum):
    EMPTY = "empty"      # 等待入场
    HOLDING = "holding"  # 已入场, 等待止盈


@dataclass
class GridLevel:
    side: Side
    entry_price: float       # 入场价 (空=卖出价, 多=买入价)
    exit_price: float        # 止盈价 (空=买回价, 多=卖出价)
    qty: float = 0.0         # 持仓数量 (XMR)
    state: LevelState = LevelState.EMPTY
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None


@dataclass
class Intent:
    """引擎输出的交易意图, 由执行层翻译成订单."""
    action: str              # place_entry / place_exit / cancel / close_side / alarm
    level: Optional[GridLevel] = None
    side: Optional[Side] = None
    reason: str = ""


@dataclass
class EngineStats:
    realized_pnl_usdt: float = 0.0
    realized_today_usdt: float = 0.0
    grid_round_trips: int = 0
    xmr_bought_back: float = 0.0
    recycle_hwm_usdt: float = 0.0   # 已回购利润的高水位线

    @property
    def pending_recycle_usdt(self) -> float:
        """可回购金额 = 实现盈亏超出高水位线的部分. 亏损后必须先赚回来才继续回购."""
        return max(self.realized_pnl_usdt - self.recycle_hwm_usdt, 0.0)


class GridEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.anchor: float = 0.0
        self.levels: list[GridLevel] = []
        self.paused: dict[Side, float] = {}        # side -> 恢复时间戳
        self.breaker_count: dict[Side, int] = {Side.LONG: 0, Side.SHORT: 0}
        self.funding_rate: float = 0.0
        self.last_recenter_ts: float = 0.0
        self.stats = EngineStats()
        self.killed = False

    # ---------- 网格构建 ----------

    def rebuild_grid(self, anchor: float, ts: float):
        """以 anchor 为中心重建网格. 已持仓的格子保留, 等它自己止盈."""
        g = self.cfg.grid
        holding = [l for l in self.levels if l.state == LevelState.HOLDING]
        self.anchor = anchor
        self.last_recenter_ts = ts
        self.levels = list(holding)
        n = int(g.range_pct / g.step_pct)
        for i in range(1, n + 1):
            up = anchor * (1 + i * g.step_pct)
            dn = anchor * (1 - i * g.step_pct)
            # 上方挂空: entry=up, 止盈=低一格买回
            self.levels.append(GridLevel(
                side=Side.SHORT, entry_price=up,
                exit_price=anchor * (1 + (i - 1) * g.step_pct)))
            # 下方挂多: entry=dn, 止盈=高一格卖出
            self.levels.append(GridLevel(
                side=Side.LONG, entry_price=dn,
                exit_price=anchor * (1 - (i - 1) * g.step_pct)))
        log.info(f"网格重建: 锚点={anchor:.2f} 每侧{n}格 步距{g.step_pct*100:.2f}%")

    def grid_qty(self, side: Side, price: float) -> float:
        """单格数量, 含资金费偏置."""
        base = self.cfg.grid.usdt_per_grid / price
        fb = self.cfg.funding_bias
        if fb.enabled:
            if self.funding_rate > fb.threshold:        # 多头狂热 → 偏空
                base *= fb.bias_multiplier if side == Side.SHORT else (1 / fb.bias_multiplier)
            elif self.funding_rate < -fb.threshold:     # 空头狂热 → 偏多
                base *= fb.bias_multiplier if side == Side.LONG else (1 / fb.bias_multiplier)
        return round(base, 4)

    # ---------- 行情驱动 ----------

    def active_entry_levels(self, price: float) -> list[GridLevel]:
        """返回当前应挂入场单的格子: 每侧距离现价最近的 N 个空格子."""
        k = self.cfg.grid.active_levels_per_side
        out = []
        for side in (Side.SHORT, Side.LONG):
            if self.is_paused(side):
                continue
            empties = [l for l in self.levels
                       if l.side == side and l.state == LevelState.EMPTY]
            if side == Side.SHORT:
                empties = sorted([l for l in empties if l.entry_price > price],
                                 key=lambda l: l.entry_price)[:k]
            else:
                empties = sorted([l for l in empties if l.entry_price < price],
                                 key=lambda l: -l.entry_price)[:k]
            out.extend(empties)
        return out

    def on_entry_filled(self, level: GridLevel, qty: float) -> Intent:
        level.state = LevelState.HOLDING
        level.qty = qty
        log.info(f"入场成交 {level.side} @{level.entry_price:.2f} qty={qty}")
        return Intent(action="place_exit", level=level)

    def on_exit_filled(self, level: GridLevel, fee_rate: float = 0.0005) -> float:
        """止盈成交, 返回本格实现利润(USDT, 已扣双边手续费)."""
        if level.side == Side.SHORT:
            gross = level.qty * (level.entry_price - level.exit_price)
        else:
            gross = level.qty * (level.exit_price - level.entry_price)
        fees = level.qty * (level.entry_price + level.exit_price) * fee_rate
        pnl = gross - fees
        self.stats.realized_pnl_usdt += pnl
        self.stats.realized_today_usdt += pnl
        self.stats.grid_round_trips += 1
        log.info(f"止盈成交 {level.side} {level.entry_price:.2f}->{level.exit_price:.2f} "
                 f"pnl={pnl:.3f}U 累计={self.stats.realized_pnl_usdt:.2f}U")
        level.state = LevelState.EMPTY
        level.qty = 0.0
        level.entry_order_id = level.exit_order_id = None
        return pnl

    # ---------- 熔断 ----------

    def check_circuit_breaker(self, candle_close: float, ts: float) -> Optional[Intent]:
        """每根熔断周期K线收盘时调用一次."""
        cb = self.cfg.circuit_breaker
        upper = self.anchor * (1 + self.cfg.grid.range_pct)
        lower = self.anchor * (1 - self.cfg.grid.range_pct)
        if candle_close > upper:
            self.breaker_count[Side.SHORT] += 1
            self.breaker_count[Side.LONG] = 0
        elif candle_close < lower:
            self.breaker_count[Side.LONG] += 1
            self.breaker_count[Side.SHORT] = 0
        else:
            self.breaker_count = {Side.LONG: 0, Side.SHORT: 0}
            return None

        for side, cnt in self.breaker_count.items():
            if cnt >= cb.consecutive_closes and not self.is_paused(side):
                self.paused[side] = ts + cb.action_pause_hours * 3600
                self.breaker_count[side] = 0
                log.warning(f"熔断触发: 暂停 {side} 方向 {cb.action_pause_hours}h, "
                            f"收盘价 {candle_close:.2f} 超出网格边界")
                return Intent(action="close_side", side=side,
                              reason=f"熔断: {cb.consecutive_closes}根K线收在边界外")
        return None

    def is_paused(self, side: Side) -> bool:
        return self.paused.get(side, 0) > 0

    def tick_pause(self, ts: float):
        for side, until in list(self.paused.items()):
            if ts >= until:
                del self.paused[side]
                log.info(f"熔断解除: {side} 恢复, 触发重新锚定")

    # ---------- 利润回购 ----------

    def should_recycle(self, price: float) -> float:
        """返回应回购的USDT金额, 0表示不回购."""
        pr = self.cfg.profit_recycle
        if not pr.enabled or self.stats.pending_recycle_usdt < pr.min_usdt_to_buy:
            return 0.0
        if pr.buy_on_dip_only and price >= self.anchor:
            return 0.0
        return self.stats.pending_recycle_usdt * pr.get("recycle_fraction", 1.0)

    def on_recycled(self, usdt: float, xmr: float):
        # HWM 按全额 pending 提升: 未回购的那部分利润永久留作缓冲, 不再重复回购
        frac = self.cfg.profit_recycle.get("recycle_fraction", 1.0)
        self.stats.recycle_hwm_usdt += usdt / max(frac, 1e-9)
        self.stats.xmr_bought_back += xmr
        log.info(f"利润回购: {usdt:.2f}U -> {xmr:.4f} XMR, 累计回购 {self.stats.xmr_bought_back:.4f} XMR")

    # ---------- 风控 ----------

    def net_position_usdt(self, price: float) -> float:
        net_qty = sum(l.qty if l.side == Side.LONG else -l.qty
                      for l in self.levels if l.state == LevelState.HOLDING)
        return net_qty * price

    def check_daily_loss(self) -> bool:
        if self.stats.realized_today_usdt < -self.cfg.risk.daily_loss_limit_usdt:
            self.killed = True
            log.error(f"当日亏损 {self.stats.realized_today_usdt:.2f}U 超限, KILL SWITCH 触发")
            return True
        return False


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e
