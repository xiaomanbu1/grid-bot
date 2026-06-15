"""
实盘/dry-run 运行器.
主循环: 行情 -> 锚点维护 -> 挂单梯子维护 -> 成交检测 -> 熔断 -> 风控 -> 快照.
"""
import time
import logging
import datetime as dt

from .strategy import GridEngine, Side, LevelState, ema
from .exchange_factory import make_exchange
from .storage import Store
from .telegram_bot import TgBot
from .webserver import start_web

log = logging.getLogger("live")


class LiveRunner:
    def __init__(self, cfg, config_path="config.local.yaml"):
        self.cfg = cfg
        self._config_path = config_path
        self.ex = make_exchange(cfg)
        self.engine = GridEngine(cfg)
        self.store = Store(cfg.storage.db_path)
        self.tg = TgBot(cfg, self)
        self.manual_pause = False
        self.recenter_flag = False
        self.last_cb_candle_ts = 0
        self.last_report = time.time()
        self.last_day = dt.date.today()
        self.last_price = 0.0

    # ---------- TG controller 接口 ----------

    def status_dict(self) -> dict:
        from .strategy import LevelState
        s = self.engine.stats
        pos = self.ex.position_info()
        price = self.last_price
        liq_dist = (abs(price - pos["liq_price"]) / price
                    if price > 0 and pos["liq_price"] > 0 else None)
        return {
            "mode": "dry_run" if self.cfg.mode.dry_run else "live",
            "manual_pause": self.manual_pause,
            "killed": self.engine.killed,
            "price": price,
            "anchor": self.engine.anchor,
            "funding": self.engine.funding_rate,
            "holding_levels": sum(1 for l in self.engine.levels
                                  if l.state == LevelState.HOLDING),
            "net_pos_usdt": self.engine.net_position_usdt(price),
            "liq_price": pos["liq_price"],
            "liq_dist_pct": liq_dist,
            "round_trips": s.grid_round_trips,
            "realized_pnl": s.realized_pnl_usdt,
            "realized_today": s.realized_today_usdt,
            "pending_recycle": s.pending_recycle_usdt,
            "xmr_bought": s.xmr_bought_back,
            "paused_sides": [k.value for k in self.engine.paused],
        }

    def status_text(self) -> str:
        try:
            price = self.ex.price()
        except Exception:
            price = 0.0
        s = self.engine.stats
        holding = sum(1 for l in self.engine.levels if l.state == LevelState.HOLDING)
        pos = self.ex.position_info()
        return (f"📊 BTC 网格状态\n"
                f"模式: {'DRY-RUN' if self.cfg.mode.dry_run else '实盘'}"
                f"{' | ⏸暂停' if self.manual_pause else ''}"
                f"{' | 🛑KILLED' if self.engine.killed else ''}\n"
                f"现价: {price:.2f} | 锚点: {self.engine.anchor:.2f}\n"
                f"持仓格子: {holding} | 净敞口: {self.engine.net_position_usdt(price):.1f}U\n"
                f"强平价: {pos['liq_price']:.2f}\n"
                f"资金费: {self.engine.funding_rate*100:.4f}%\n"
                f"完成轮次: {s.grid_round_trips} | 实现盈亏: {s.realized_pnl_usdt:.2f}U")

    def pnl_text(self) -> str:
        d = self.store.summary()
        return (f"💰 累计: {d['round_trips']} 轮 | 实现盈亏 {d['realized_pnl']:.2f}U")

    def balance_text(self) -> str:
        b = self.ex.balances()
        if b.get("dry"):
            return ("💵 当前 DRY-RUN 模拟模式, 无真实余额。\n"
                    "切到实盘后这里显示 Gate 合约保证金和现货持仓。")
        lines = ["💵 Gate 账户余额"]
        if "fut_err" in b:
            lines.append(f"  合约: 查询失败 ({b['fut_err']})")
        else:
            lines.append(f"  合约保证金 USDT: {b.get('fut_usdt_total',0):.2f} "
                         f"(可用 {b.get('fut_usdt_free',0):.2f} / "
                         f"占用 {b.get('fut_usdt_used',0):.2f})")
        return "\n".join(lines)

    def set_paused(self, v: bool):
        self.manual_pause = v

    def request_recenter(self):
        self.recenter_flag = True

    def kill(self):
        self.engine.killed = True
        self._cancel_all_entries()

    # ---------- 运行时参数调整 (写回 config.local.yaml 持久化) ----------

    # 可调参数白名单: key -> (配置路径, 类型, 最小值, 最大值, 显示名)
    ADJUSTABLE = {
        "step_pct":        ("grid.step_pct",        float, 0.005, 0.05,  "格距"),
        "usdt_per_grid":   ("grid.usdt_per_grid",   float, 5,     500,   "单格金额"),
        "leverage":        ("grid.leverage",        int,   1,     5,     "杠杆"),
    }

    def get_param(self, key: str):
        path, _, _, _, _ = self.ADJUSTABLE[key]
        node = self.cfg
        for p in path.split("."):
            node = node[p]
        return node

    def set_param(self, key: str, value) -> str:
        """改参数 + 写回配置文件. 返回结果文本."""
        if key not in self.ADJUSTABLE:
            return f"未知参数: {key}"
        path, typ, lo, hi, name = self.ADJUSTABLE[key]
        try:
            value = typ(value)
        except (ValueError, TypeError):
            return f"{name} 取值无效"
        if not (lo <= value <= hi):
            return f"{name} 超出范围 [{lo}, {hi}]"
        # 改内存 cfg
        node = self.cfg
        parts = path.split(".")
        for p in parts[:-1]:
            node = node[p]
        old = node[parts[-1]]
        node[parts[-1]] = value
        # 写回文件
        self._persist_config()
        # 杠杆改了通知交易所; 网格类参数下次重新锚定生效
        if key == "leverage":
            self.ex.set_leverage()
        log.warning(f"参数调整: {name} {old} -> {value}")
        note = " (下次重新锚定生效, 可点重新锚定立即生效)" if key in ("step_pct",) else ""
        return f"✅ {name}: {old} → {value}{note}"

    def set_dry_run(self, dry: bool) -> str:
        from .strategy import LevelState
        if self.cfg["mode"]["dry_run"] == dry:
            return f"已经是{'DRY-RUN' if dry else '实盘'}模式, 无变化"
        # 切模式前: 清空所有旧挂单状态. 不同模式的订单 ID 体系不通用
        # (dry 是 sim-xxx 假ID, 实盘是 Gate 真ID), 不清会导致拿错ID去查询/撤单报错.
        for l in self.engine.levels:
            for attr in ("entry_order_id", "exit_order_id"):
                oid = getattr(l, attr)
                if oid:
                    try:
                        self.ex.cancel(oid)
                    except Exception:
                        pass
                    setattr(l, attr, None)
            l.state = LevelState.EMPTY
            l.qty = 0.0
        self.cfg["mode"]["dry_run"] = dry
        self.ex.dry = dry
        self.ex.sim_orders = {}        # 清空模拟挂单簿
        self.engine.killed = False     # 解除可能的 kill 状态
        self.recenter_flag = True      # 触发重新锚定, 用新模式重新挂单
        self._persist_config()
        log.warning(f"模式切换: dry_run={dry}, 已清空旧挂单状态并重新锚定")
        return ("🟡 已切回 DRY-RUN 模拟模式\n已清空旧挂单, 重新锚定中" if dry
                else "🔴 已切到实盘! 已清空旧挂单, 重新锚定中\n"
                     "⚠️ 确认合约账户有 USDT 保证金, 否则下单会失败\n"
                     "⚠️ 确认 API key 已关提现并绑 IP")

    # ---------- 切换币种 + 波动率自适应参数 ----------

    def recommend_params(self, symbol: str) -> dict:
        """根据该币近30天日线波动率, 推荐格距/范围. 高波动币用宽格距."""
        try:
            candles = self.ex.ohlcv("1d", limit=30, symbol=symbol)
            closes = [c[4] for c in candles]
            if len(closes) < 10:
                raise ValueError("数据不足")
            # 日收益率标准差 = 波动率
            rets = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
            mean = sum(rets) / len(rets)
            vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        except Exception as e:
            log.warning(f"算波动率失败, 用默认参数: {e}")
            vol = 0.02
        # 格距 ≈ 日波动率的 40%, 限制在 0.5%~3%; 范围 ≈ 日波动率 × 8, 限制 8%~30%
        step = min(max(vol * 0.4, 0.005), 0.03)
        rng = min(max(vol * 8, 0.08), 0.30)
        return {"vol": vol, "step_pct": round(step, 4), "range_pct": round(rng, 3)}

    def switch_symbol(self, symbol: str, auto_param: bool = True) -> str:
        """切换交易对. 先撤所有挂单, 改配置, 可选自动调参, 重新锚定."""
        from .strategy import LevelState
        symbol = symbol.strip().upper()
        # 规整成 ccxt 永续格式: BTC -> BTC/USDT:USDT
        if "/" not in symbol:
            symbol = f"{symbol}/USDT:USDT"
        elif ":" not in symbol:
            symbol = f"{symbol}:USDT"
        # 验证该交易对在所里存在
        try:
            self.ex.ex.load_markets()
            if symbol not in self.ex.ex.markets:
                return f"❌ {symbol} 在 {self.cfg.exchange.id} 不存在或不可交易"
        except Exception as e:
            return f"❌ 验证交易对失败: {e}"
        # 撤所有挂单 + 清状态
        for l in self.engine.levels:
            for attr in ("entry_order_id", "exit_order_id"):
                oid = getattr(l, attr)
                if oid:
                    try: self.ex.cancel(oid)
                    except Exception: pass
            l.state = LevelState.EMPTY
            l.qty = 0.0
        old = self.cfg["exchange"]["perp_symbol"]
        self.cfg["exchange"]["perp_symbol"] = symbol
        self.ex.perp = symbol
        self.ex._amount_precision = None  # 强制重新加载新币种精度
        self.ex._min_amount = None
        msg = [f"🔀 交易对: {old} → {symbol}"]
        if auto_param:
            rec = self.recommend_params(symbol)
            self.cfg["grid"]["step_pct"] = rec["step_pct"]
            self.cfg["grid"]["range_pct"] = rec["range_pct"]
            msg.append(f"日波动率 {rec['vol']*100:.1f}% → 自动设格距 "
                       f"{rec['step_pct']*100:.2f}% 范围 ±{rec['range_pct']*100:.0f}%")
        self.ex.sim_orders = {}
        self.recenter_flag = True
        self._persist_config()
        log.warning(f"切换交易对: {old} -> {symbol}, auto_param={auto_param}")
        msg.append("已重新锚定")
        return "\n".join(msg)

    def _persist_config(self):
        """把当前 cfg 写回 config.local.yaml, 重启后保留."""
        import yaml
        def plain(o):
            if isinstance(o, dict):
                return {k: plain(v) for k, v in o.items()}
            if isinstance(o, list):
                return [plain(x) for x in o]
            return o
        path = self._config_path
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(plain(self.cfg), f, allow_unicode=True,
                               default_flow_style=False, sort_keys=False)
        except Exception as e:
            log.error(f"写回配置失败: {e}")

    # ---------- 内部 ----------

    def _compute_anchor(self) -> float:
        g = self.cfg.grid
        candles = self.ex.ohlcv(g.anchor_timeframe, limit=g.anchor_ema_period * 3)
        closes = [c[4] for c in candles]
        return ema(closes, g.anchor_ema_period)

    def _cancel_all_entries(self):
        for l in self.engine.levels:
            if l.state == LevelState.EMPTY and l.entry_order_id:
                self.ex.cancel(l.entry_order_id)
                l.entry_order_id = None

    def _maintain_ladder(self, price: float):
        """保证最近 N 格挂着入场单, 其余撤掉."""
        if self.manual_pause or self.engine.killed:
            return
        if abs(self.engine.net_position_usdt(price)) >= self.cfg.risk.max_net_position_usdt:
            log.warning("净敞口达上限, 暂停新挂单")
            return
        wanted = set(id(l) for l in self.engine.active_entry_levels(price))
        for l in self.engine.levels:
            if l.state != LevelState.EMPTY:
                continue
            if id(l) in wanted and not l.entry_order_id:
                side = "sell" if l.side == Side.SHORT else "buy"
                qty = self.engine.grid_qty(l.side, l.entry_price)
                l.entry_order_id = self.ex.place_limit(side, l.entry_price, qty)
                l.qty = qty
            elif id(l) not in wanted and l.entry_order_id:
                self.ex.cancel(l.entry_order_id)
                l.entry_order_id = None

    def _check_fills(self, price: float):
        for l in self.engine.levels:
            if l.state == LevelState.EMPTY and l.entry_order_id:
                if self.ex.is_filled(l.entry_order_id, price):
                    self.engine.on_entry_filled(l, l.qty)
                    side = "buy" if l.side == Side.SHORT else "sell"
                    l.exit_order_id = self.ex.place_limit(
                        side, l.exit_price, l.qty, reduce_only=True)
            elif l.state == LevelState.HOLDING and l.exit_order_id:
                if self.ex.is_filled(l.exit_order_id, price):
                    pnl = self.engine.on_exit_filled(l)
                    self.store.trade(l.side, l.entry_price, l.exit_price, l.qty, pnl)
                    if self.engine.check_daily_loss():
                        self.kill()
                        self.tg.send("🛑 当日亏损超限, KILL SWITCH 已触发")

    def _check_breaker(self):
        cb = self.cfg.circuit_breaker
        candles = self.ex.ohlcv(cb.timeframe, limit=3)
        if not candles:
            return
        last_closed = candles[-2]  # 倒数第二根是已收盘的
        if last_closed[0] == self.last_cb_candle_ts:
            return
        self.last_cb_candle_ts = last_closed[0]
        intent = self.engine.check_circuit_breaker(last_closed[4], time.time())
        if intent and intent.action == "close_side":
            qty = sum(l.qty for l in self.engine.levels
                      if l.state == LevelState.HOLDING and l.side == intent.side)
            # 撤掉该方向所有挂单
            for l in self.engine.levels:
                if l.side == intent.side:
                    for oid_attr in ("entry_order_id", "exit_order_id"):
                        oid = getattr(l, oid_attr)
                        if oid:
                            self.ex.cancel(oid)
                            setattr(l, oid_attr, None)
                    if l.state == LevelState.HOLDING:
                        l.state = LevelState.EMPTY
                        l.qty = 0.0
            self.ex.close_position_side(intent.side.value, qty)
            self.recenter_flag = True
            self.tg.send(f"⚡ 熔断: {intent.reason}\n已平 {intent.side} 方向, 暂停后将重新锚定")

    def _check_risk(self, price: float):
        pos = self.ex.position_info()
        if pos["liq_price"] <= 0 or price <= 0:
            return
        dist = abs(price - pos["liq_price"]) / price
        r = self.cfg.risk
        if dist < r.liq_distance_reduce_pct:
            qty = abs(pos["qty"]) * 0.5
            side = "long" if pos["qty"] > 0 else "short"
            self.ex.close_position_side(side, qty)
            self.tg.send(f"🚨 强平距离 {dist*100:.1f}% < {r.liq_distance_reduce_pct*100}%, 已自动减仓50%")
        elif dist < r.liq_distance_warn_pct:
            self.tg.send(f"⚠️ 强平距离仅 {dist*100:.1f}% (强平价 {pos['liq_price']:.2f})")

    def _recycle(self, price: float):
        usdt = self.engine.should_recycle(price)
        if usdt > 0:
            xmr = self.ex.spot_buy_xmr(usdt, price)
            self.engine.on_recycled(usdt, xmr)
            self.store.recycle(usdt, xmr, price)

    def _daily_housekeeping(self):
        today = dt.date.today()
        if today != self.last_day:
            self.last_day = today
            self.engine.stats.realized_today_usdt = 0.0
            # 每日自动调参: 重算波动率, 变化明显则调整格距/范围
            if self.cfg.get("auto_tune", {}).get("enabled", False) if hasattr(self.cfg, "get") else False:
                self._auto_tune()
        if time.time() - self.last_report > self.cfg.telegram.report_interval_hours * 3600:
            self.last_report = time.time()
            self.tg.send("📅 每日报告\n" + self.status_text())

    def _auto_tune(self):
        """每日重算当前币种波动率, 若格距建议值与当前值偏差超阈值则自动调整."""
        try:
            symbol = self.cfg.exchange.perp_symbol
            rec = self.recommend_params(symbol)
            cur_step = self.cfg.grid.step_pct
            new_step = rec["step_pct"]
            # 偏差超过 30% 才调, 避免小波动频繁折腾
            change = abs(new_step - cur_step) / cur_step if cur_step else 1
            if change < 0.30:
                log.info(f"每日调参: 波动率 {rec['vol']*100:.1f}%, 格距变化 {change*100:.0f}% < 30%, 不调")
                return
            self.cfg["grid"]["step_pct"] = new_step
            self.cfg["grid"]["range_pct"] = rec["range_pct"]
            self._persist_config()
            self.recenter_flag = True
            msg = (f"🔧 每日自动调参\n{symbol} 日波动率 {rec['vol']*100:.1f}%\n"
                   f"格距 {cur_step*100:.2f}% → {new_step*100:.2f}%\n"
                   f"范围 → ±{rec['range_pct']*100:.0f}%\n已重新锚定")
            log.warning(msg.replace("\n", " "))
            self.tg.send(msg)
        except Exception as e:
            log.warning(f"每日调参失败: {e}")


    # ---------- 主循环 ----------

    def run(self):
        log.info("启动 LiveRunner, dry_run=%s", self.cfg.mode.dry_run)
        self.ex.set_leverage()
        self.engine.funding_rate = self.ex.funding_rate()
        self.engine.rebuild_grid(self._compute_anchor(), time.time())
        self.tg.start()
        start_web(self.cfg, self)
        self.tg.send("🤖 BTC 网格机器人已启动\n" + self.status_text())

        funding_ts = 0
        while True:
            try:
                now = time.time()
                self.engine.tick_pause(now)

                # 定期锚点维护
                if (self.recenter_flag or
                        now - self.engine.last_recenter_ts >
                        self.cfg.grid.recenter_interval_hours * 3600):
                    self.recenter_flag = False
                    self._cancel_all_entries()
                    self.engine.rebuild_grid(self._compute_anchor(), now)

                # 资金费每10分钟刷新
                if now - funding_ts > 600:
                    funding_ts = now
                    self.engine.funding_rate = self.ex.funding_rate()

                price = self.ex.price()
                self.last_price = price
                if not self.engine.killed:
                    self._check_fills(price)
                    self._maintain_ladder(price)
                    self._check_breaker()
                    self._check_risk(price)
                    self._recycle(price)
                self._daily_housekeeping()
                self.store.snapshot(price, self.engine.anchor,
                                    self.engine.stats.realized_pnl_usdt,
                                    self.engine.stats.xmr_bought_back,
                                    self.engine.net_position_usdt(price),
                                    self.engine.funding_rate)
            except KeyboardInterrupt:
                log.info("手动退出")
                break
            except Exception as e:
                log.exception(f"主循环异常: {e}")
                # 错误节流: 同类错误最多每5分钟通知一次, 避免刷屏卡住TG
                now2 = time.time()
                if now2 - getattr(self, "_last_err_notify", 0) > 300:
                    self._last_err_notify = now2
                    self.tg.send(f"❌ 主循环异常 (5分钟内不再重复提醒): {e}")
            time.sleep(self.cfg.mode.poll_interval_sec)
