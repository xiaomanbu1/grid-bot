"""
回测器: 用 1h K线模拟网格成交.
数据来源: ccxt 在线拉取 (在你的 VPS 上跑), 或本地 CSV (ts,open,high,low,close,volume).
成交模拟: 每根K线内, 价格触及格子入场/止盈价即视为成交 (保守: 不假设同一根K线内双向成交多轮).
资金费: 用历史资金费(若可取)或常数近似, 按持仓净敞口每8h结算.

用法:
  python main.py backtest --days 180
  python main.py backtest --csv data/xmr_1h.csv
"""
import logging
import time
import csv as csvmod

from .strategy import GridEngine, Side, LevelState, ema

log = logging.getLogger("backtest")

FEE_MAKER = 0.0002   # Gate 合约 maker 费率近似
FUNDING_CONST = 0.0001  # 取不到历史资金费时的常数近似 (+0.01%/8h)


def fetch_klines(cfg, days: int):
    import ccxt
    ex = ccxt.gate({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    symbol = cfg.exchange.perp_symbol
    since = int((time.time() - days * 86400) * 1000)
    out = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        if not batch:
            break
        out.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    log.info(f"拉取K线 {len(out)} 根")
    return out


def load_csv(path: str):
    out = []
    with open(path) as f:
        for row in csvmod.reader(f):
            if row[0].lower().startswith("ts"):
                continue
            out.append([float(x) for x in row[:6]])
    return out


def run_backtest(cfg, candles: list):
    """candles: [[ts_ms, o, h, l, c, v], ...] 1h."""
    g = cfg.grid
    engine = GridEngine(cfg)
    daily_closes = []
    cb_tf_hours = {"1h": 1, "4h": 4, "1d": 24}[cfg.circuit_breaker.timeframe]

    equity_xmr = []   # 币本位净值曲线: 回购到的BTC + 浮动盈亏折币
    warmup = g.anchor_ema_period * 24  # 需要的预热小时数

    for i, (ts_ms, o, h, l, c, v) in enumerate(candles):
        ts = ts_ms / 1000
        if i % 24 == 0:
            daily_closes.append(c)

        if len(daily_closes) < g.anchor_ema_period:
            continue

        # 锚点维护
        if (engine.anchor == 0 or
                ts - engine.last_recenter_ts > g.recenter_interval_hours * 3600):
            anchor = ema(daily_closes, g.anchor_ema_period)
            engine.rebuild_grid(anchor, ts)

        engine.tick_pause(ts)
        engine.funding_rate = FUNDING_CONST

        # --- 成交模拟 ---
        # 入场: 该格子在"激活梯子"里且本K线触及入场价
        active = set(id(x) for x in engine.active_entry_levels(o))
        for lev in engine.levels:
            if lev.state == LevelState.EMPTY and id(lev) in active:
                touched = (lev.side == Side.SHORT and h >= lev.entry_price) or \
                          (lev.side == Side.LONG and l <= lev.entry_price)
                if touched:
                    qty = engine.grid_qty(lev.side, lev.entry_price)
                    engine.on_entry_filled(lev, qty)
        # 止盈
        for lev in engine.levels:
            if lev.state == LevelState.HOLDING:
                touched = (lev.side == Side.SHORT and l <= lev.exit_price) or \
                          (lev.side == Side.LONG and h >= lev.exit_price)
                if touched:
                    engine.on_exit_filled(lev, fee_rate=FEE_MAKER)

        # 资金费结算 (每8h): 净空头在正资金费下收钱
        if i % 8 == 0:
            net = sum(lv.qty if lv.side == Side.LONG else -lv.qty
                      for lv in engine.levels if lv.state == LevelState.HOLDING)
            engine.stats.realized_pnl_usdt += -net * c * engine.funding_rate

        # 熔断 (按收盘对齐熔断周期)
        if i % cb_tf_hours == 0:
            intent = engine.check_circuit_breaker(c, ts)
            if intent:
                # 市价平掉该方向, 按当前收盘价结算
                for lev in engine.levels:
                    if lev.side == intent.side and lev.state == LevelState.HOLDING:
                        if lev.side == Side.SHORT:
                            pnl = lev.qty * (lev.entry_price - c)
                        else:
                            pnl = lev.qty * (c - lev.entry_price)
                        pnl -= lev.qty * (lev.entry_price + c) * FEE_MAKER * 2
                        engine.stats.realized_pnl_usdt += pnl
                        engine.stats.realized_today_usdt += pnl
                        lev.state = LevelState.EMPTY
                        lev.qty = 0.0

        # 利润回购
        usdt = engine.should_recycle(c)
        if usdt > 0:
            engine.on_recycled(usdt, usdt / c)

        # 净值快照 (币本位)
        float_pnl = 0.0
        for lev in engine.levels:
            if lev.state == LevelState.HOLDING:
                float_pnl += (lev.entry_price - c) * lev.qty if lev.side == Side.SHORT \
                    else (c - lev.entry_price) * lev.qty
        # 币本位净值 = 已回购BTC + (未回购的实现利润 + 浮动盈亏) 按现价折币
        unspent = engine.stats.pending_recycle_usdt
        eq = engine.stats.xmr_bought_back + (unspent + float_pnl) / c
        equity_xmr.append((ts, eq, c))

    return engine, equity_xmr


def report(engine: GridEngine, equity_xmr, candles):
    s = engine.stats
    start_p, end_p = candles[0][4], candles[-1][4]
    days = (candles[-1][0] - candles[0][0]) / 86400000
    print("=" * 52)
    print(f"回测区间: {days:.0f} 天 | 价格 {start_p:.2f} -> {end_p:.2f} "
          f"({(end_p/start_p-1)*100:+.1f}%)")
    print(f"完成网格轮次: {s.grid_round_trips}")
    print(f"实现盈亏(USDT): {s.realized_pnl_usdt:.2f}")
    print(f"累计回购(已禁用): {s.xmr_bought_back:.4f} "
          f"(按期末价折合 {s.xmr_bought_back*end_p:.2f}U)")
    print(f"待回购利润: {s.pending_recycle_usdt:.2f}U")
    if equity_xmr:
        peak, max_dd = -1e9, 0.0
        for _, eq, _ in equity_xmr:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        print(f"币本位净值最大回撤: {max_dd:.4f} BTC (按期末价 ~{max_dd*end_p:.1f}U)")
    invested = engine.cfg.grid.usdt_per_grid * \
        int(engine.cfg.grid.range_pct / engine.cfg.grid.step_pct)
    print(f"单侧满格名义资金: ~{invested:.0f}U (杠杆{engine.cfg.grid.leverage}x "
          f"占用保证金 ~{invested/engine.cfg.grid.leverage:.0f}U)")
    print("=" * 52)
