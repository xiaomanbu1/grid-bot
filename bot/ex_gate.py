"""
交易所适配层: ccxt(gate) 封装, 支持 dry_run 模拟.
dry_run 模式下用真实行情判断模拟限价单是否成交, 但不发任何真实订单.
"""
import time
import uuid
import logging
import ccxt

log = logging.getLogger("exchange")


class GateAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dry = cfg.mode.dry_run
        self.perp = cfg.exchange.perp_symbol
        self.spot = cfg.exchange.spot_symbol
        # 统一账户模式 (组合保证金/跨币种保证金). 你的账户下单单位是USDT而非张数, 即为此模式.
        self.unified = bool(cfg.exchange.get("unified_account", False)) \
            if hasattr(cfg.exchange, "get") else False
        opts = {
            "apiKey": cfg.exchange.api_key,
            "secret": cfg.exchange.api_secret,
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
            "timeout": 15000,  # 15秒请求超时, 防止单个请求挂死拖垮主循环
        }
        self.ex = ccxt.gate(opts)
        self.ex_spot = ccxt.gate({**opts, "options": {"defaultType": "spot"}})
        # dry_run 模拟挂单簿: order_id -> dict
        self.sim_orders: dict[str, dict] = {}
        # 合约面值 (1张 = 多少 XMR), 实盘下单要把 XMR 数量换算成整数张数
        self._contract_size = None
        self._amount_precision = None

    def _order_params(self, base: dict) -> dict:
        """统一账户模式下, 给请求加 unified 标记, 走统一账户端点."""
        if self.unified:
            base = {**base, "unified": True}
        return base

    def _load_market(self):
        """加载合约市场信息, 取每张面值. 失败时回退到常见默认值."""
        if self._contract_size is not None:
            return
        try:
            self.ex.load_markets()
            m = self.ex.market(self.perp)
            self._contract_size = float(m.get("contractSize") or 1)
            log.info(f"合约面值: 1张 = {self._contract_size} XMR")
        except Exception as e:
            log.warning(f"加载合约市场信息失败, 用默认面值1: {e}")
            self._contract_size = 1.0

    def xmr_to_contracts(self, xmr_qty: float) -> int:
        """把 XMR 数量换算成 Gate 合约张数 (整数, 至少1张)."""
        self._load_market()
        contracts = round(xmr_qty / self._contract_size)
        return max(contracts, 1)

    # ---------- 行情 ----------

    def price(self) -> float:
        t = self.ex.fetch_ticker(self.perp)
        return float(t["last"])

    def ohlcv(self, timeframe: str, limit: int = 200, symbol: str | None = None):
        return self.ex.fetch_ohlcv(symbol or self.perp, timeframe, limit=limit)

    def funding_rate(self) -> float:
        try:
            fr = self.ex.fetch_funding_rate(self.perp)
            return float(fr.get("fundingRate") or 0.0)
        except Exception as e:
            log.warning(f"获取资金费失败: {e}")
            return 0.0

    # ---------- 订单 ----------

    def set_leverage(self):
        if self.dry:
            return
        try:
            self.ex.set_leverage(self.cfg.grid.leverage, self.perp)
        except Exception as e:
            log.warning(f"设置杠杆失败(可能已设置): {e}")

    def place_limit(self, side: str, price: float, qty: float, reduce_only=False) -> str:
        """side: buy/sell. qty 是 XMR 数量, 实盘会换算成合约张数. 返回 order_id."""
        if self.dry:
            oid = f"sim-{uuid.uuid4().hex[:8]}"
            self.sim_orders[oid] = {"side": side, "price": price, "qty": qty,
                                    "filled": False, "ts": time.time()}
            log.info(f"[DRY] 挂单 {side} {qty}@{price:.2f} reduce_only={reduce_only} id={oid}")
            return oid
        contracts = self.xmr_to_contracts(qty)   # XMR -> 整数张数
        params = self._order_params({"reduceOnly": reduce_only, "timeInForce": "PO"})
        o = self.ex.create_order(self.perp, "limit", side, contracts, price, params)
        log.info(f"挂单 {side} {qty}XMR={contracts}张 @{price:.2f} id={o['id']}")
        return o["id"]

    def cancel(self, order_id: str):
        if self.dry:
            self.sim_orders.pop(order_id, None)
            return
        try:
            self.ex.cancel_order(order_id, self.perp)
        except Exception as e:
            log.warning(f"撤单失败 {order_id}: {e}")

    def cancel_all(self) -> int:
        """撤掉该交易对所有挂单. 紧急停止用. 返回撤单数."""
        if self.dry:
            n = len(self.sim_orders)
            self.sim_orders.clear()
            return n
        try:
            open_orders = self.ex.fetch_open_orders(self.perp)
            n = len(open_orders)
            self.ex.cancel_all_orders(self.perp)
            log.info(f"已撤掉 {self.perp} 全部 {n} 个挂单")
            return n
        except Exception as e:
            log.warning(f"批量撤单失败, 逐个撤: {e}")
            n = 0
            try:
                for o in self.ex.fetch_open_orders(self.perp):
                    try:
                        self.ex.cancel_order(o["id"], self.perp); n += 1
                    except Exception:
                        pass
            except Exception:
                pass
            return n

    def is_filled(self, order_id: str, last_price: float) -> bool:
        if self.dry:
            o = self.sim_orders.get(order_id)
            if not o or o["filled"]:
                return bool(o and o["filled"])
            # 简化模拟: 现价穿过限价即视为成交
            if (o["side"] == "buy" and last_price <= o["price"]) or \
               (o["side"] == "sell" and last_price >= o["price"]):
                o["filled"] = True
                log.info(f"[DRY] 模拟成交 {o['side']} {o['qty']}@{o['price']:.2f}")
                return True
            return False
        try:
            o = self.ex.fetch_order(order_id, self.perp)
            return o["status"] == "closed"
        except Exception as e:
            log.warning(f"查询订单失败 {order_id}: {e}")
            return False

    def close_position_side(self, side: str, qty: float):
        """市价平掉某方向 qty (XMR数量). side=long → 卖出平多."""
        if qty <= 0:
            return
        order_side = "sell" if side == "long" else "buy"
        if self.dry:
            log.info(f"[DRY] 市价平仓 {side} qty={qty}")
            return
        contracts = self.xmr_to_contracts(qty)
        params = self._order_params({"reduceOnly": True})
        self.ex.create_order(self.perp, "market", order_side, contracts, params=params)

    def position_info(self) -> dict:
        """返回 {qty, liq_price, entry_price} (净持仓)."""
        if self.dry:
            return {"qty": 0.0, "liq_price": 0.0, "entry_price": 0.0}
        try:
            ps = self.ex.fetch_positions([self.perp])
            for p in ps:
                if p["symbol"] == self.perp and p.get("contracts"):
                    return {"qty": float(p["contracts"]),
                            "liq_price": float(p.get("liquidationPrice") or 0),
                            "entry_price": float(p.get("entryPrice") or 0)}
        except Exception as e:
            log.warning(f"查询持仓失败: {e}")
        return {"qty": 0.0, "liq_price": 0.0, "entry_price": 0.0}

    # ---------- 现货回购 ----------

    def spot_buy_xmr(self, usdt: float, price: float) -> float:
        """用 usdt 市价买入现货 XMR, 返回买到的数量."""
        qty = round(usdt / price, 4)
        if self.dry:
            log.info(f"[DRY] 现货回购 {usdt:.2f}U -> {qty} XMR")
            return qty
        o = self.ex_spot.create_order(self.spot, "market", "buy", qty)
        return float(o.get("filled") or qty)

    def balances(self) -> dict:
        """拉账户余额: 合约 USDT 保证金 + 现货 USDT/XMR. dry_run 返回占位."""
        if self.dry:
            return {"dry": True}
        out = {"dry": False}
        try:
            fut = self.ex.fetch_balance()  # swap 账户
            u = fut.get("USDT", {})
            out["fut_usdt_total"] = float(u.get("total") or 0)
            out["fut_usdt_free"] = float(u.get("free") or 0)
            out["fut_usdt_used"] = float(u.get("used") or 0)
        except Exception as e:
            log.warning(f"查合约余额失败: {e}")
            out["fut_err"] = str(e)[:60]
        try:
            spot = self.ex_spot.fetch_balance()
            out["spot_usdt"] = float(spot.get("USDT", {}).get("total") or 0)
            out["spot_xmr"] = float(spot.get("XMR", {}).get("total") or 0)
        except Exception as e:
            log.warning(f"查现货余额失败: {e}")
            out["spot_err"] = str(e)[:60]
        return out

