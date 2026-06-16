"""
多币种模拟对比引擎.
在一个进程里并行跑 N 个币的网格策略 (纯模拟, 不下任何真实单, 不需要API写权限),
记录每个币的虚拟成交/盈亏/波动率, 供横向对比, 帮你判断换哪个币。

设计: 每个币一个独立 GridEngine 实例 + 自己的模拟挂单簿, 共用一个币安行情客户端 (只读).
"""
import time
import logging
import threading

import ccxt

from .strategy import GridEngine, Side, LevelState, ema

log = logging.getLogger("multisim")

FEE_MAKER = 0.0002


class CoinSim:
    """单个币的模拟实例."""
    def __init__(self, symbol: str, cfg):
        self.symbol = symbol
        self.cfg = cfg
        self.engine = GridEngine(cfg)
        self.sim_orders = {}   # oid -> {side, price, qty, kind, level}
        self.last_price = 0.0
        self.vol = 0.0
        self.started = time.time()
        self._oid = 0

    def new_oid(self):
        self._oid += 1
        return f"{self.symbol}-{self._oid}"


class MultiSim:
    def __init__(self, cfg, symbols: list[str]):
        self.cfg = cfg
        self.ex = ccxt.binance({
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
            "enableRateLimit": True, "timeout": 15000,
        })
        self.coins: dict[str, CoinSim] = {}
        self._init_symbols(symbols)

    def _norm(self, s: str) -> str:
        s = s.strip().upper()
        if "/" not in s:
            s = f"{s}/USDT:USDT"
        elif ":" not in s:
            s = f"{s}:USDT"
        return s

    def _init_symbols(self, symbols):
        try:
            self.ex.load_markets()
        except Exception as e:
            log.warning(f"加载市场失败: {e}")
        for raw in symbols:
            sym = self._norm(raw)
            if sym not in self.ex.markets:
                log.warning(f"跳过 {sym}: 币安无此永续")
                continue
            self.coins[sym] = CoinSim(sym, self.cfg)
            log.info(f"模拟币种已加入: {sym}")

    # ---------- 波动率 + 锚点 ----------

    def _vol_and_anchor(self, sym: str):
        candles = self.ex.fetch_ohlcv(sym, "1d", limit=30)
        closes = [c[4] for c in candles]
        rets = [(closes[i]/closes[i-1]-1) for i in range(1, len(closes))]
        mean = sum(rets)/len(rets)
        vol = (sum((r-mean)**2 for r in rets)/len(rets))**0.5
        anchor = ema(closes, self.cfg.grid.anchor_ema_period)
        return vol, anchor

    def _auto_params(self, vol):
        step = min(max(vol*0.4, 0.005), 0.03)
        rng = min(max(vol*8, 0.08), 0.30)
        return step, rng

    def rebuild(self, cs: CoinSim):
        """按该币波动率自动设参数, 重建网格."""
        vol, anchor = self._vol_and_anchor(cs.symbol)
        cs.vol = vol
        step, rng = self._auto_params(vol)
        # 临时改 engine 看到的参数 (每个币独立)
        cs.engine.cfg = self._coin_cfg(step, rng)
        cs.engine.rebuild_grid(anchor, time.time())
        cs.sim_orders.clear()

    def _coin_cfg(self, step, rng):
        """给单币生成一份参数副本 (只改 grid 部分)."""
        import copy
        c = copy.deepcopy(dict(self.cfg))
        c["grid"]["step_pct"] = step
        c["grid"]["range_pct"] = rng
        from .config import Cfg
        return Cfg(c)

    # ---------- 模拟成交 ----------

    def _maintain_and_fill(self, cs: CoinSim, price: float):
        eng = cs.engine
        # 挂入场单 (模拟): 给最近N格挂上
        for lev in eng.active_entry_levels(price):
            if lev.state == LevelState.EMPTY and not lev.entry_order_id:
                qty = eng.grid_qty(lev.side, lev.entry_price)
                lev.entry_order_id = cs.new_oid()
                lev.qty = qty
        # 判定成交: 价格穿过挂单价 (要求穿过, 比触碰更接近实盘)
        for lev in eng.levels:
            if lev.state == LevelState.EMPTY and lev.entry_order_id:
                hit = (lev.side == Side.SHORT and price >= lev.entry_price) or \
                      (lev.side == Side.LONG and price <= lev.entry_price)
                if hit:
                    eng.on_entry_filled(lev, lev.qty)
                    lev.exit_order_id = cs.new_oid()
            elif lev.state == LevelState.HOLDING and lev.exit_order_id:
                hit = (lev.side == Side.SHORT and price <= lev.exit_price) or \
                      (lev.side == Side.LONG and price >= lev.exit_price)
                if hit:
                    eng.on_exit_filled(lev, fee_rate=FEE_MAKER)

    def tick(self):
        """拉所有币最新价, 跑一轮模拟."""
        for sym, cs in self.coins.items():
            try:
                if cs.engine.anchor == 0:
                    self.rebuild(cs)
                price = float(self.ex.fetch_ticker(sym)["last"])
                cs.last_price = price
                self._maintain_and_fill(cs, price)
            except Exception as e:
                log.warning(f"{sym} 模拟tick出错: {e}")

    # ---------- 对比报表 ----------

    def leaderboard(self) -> list[dict]:
        """各币表现排名 (按虚拟实现盈亏)."""
        rows = []
        for sym, cs in self.coins.items():
            s = cs.engine.stats
            hours = max((time.time() - cs.started) / 3600, 0.01)
            rows.append({
                "symbol": sym.split("/")[0],
                "price": round(cs.last_price, 4),
                "vol_pct": round(cs.vol * 100, 1),
                "round_trips": s.grid_round_trips,
                "realized_pnl": round(s.realized_pnl_usdt, 2),
                "pnl_per_day": round(s.realized_pnl_usdt / hours * 24, 2),
                "anchor": round(cs.engine.anchor, 4),
            })
        rows.sort(key=lambda r: -r["realized_pnl"])
        return rows
