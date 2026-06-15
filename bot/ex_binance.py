"""
交易所适配层: ccxt(binance) 封装币安 U本位永续 (BTC/USDT).
dry_run 模式用真实行情判断模拟限价单成交, 不发真实订单.

币安 vs Gate 的区别 (为什么换币安更省心):
- 下单数量直接用 BTC 数量 (按 amount precision), 没有"张数"换算
- 没有统一账户/经典账户的端点分裂问题
- API 规范, 错误信息清晰
- 注意: 主流币目标赚 USDT, 不做利润回购现货
"""
import time
import uuid
import logging
import ccxt

log = logging.getLogger("exchange")


class BinanceAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dry = cfg.mode.dry_run
        self.perp = cfg.exchange.perp_symbol      # BTC/USDT:USDT
        opts = {
            "apiKey": cfg.exchange.api_key,
            "secret": cfg.exchange.api_secret,
            "options": {"defaultType": "future"},  # 币安 U本位永续
            "enableRateLimit": True,
        }
        self.ex = ccxt.binance(opts)
        self.sim_orders = {}
        self._amount_precision = None
        self._min_amount = None

    def _load_market(self):
        if self._amount_precision is not None:
            return
        try:
            self.ex.load_markets()
            m = self.ex.market(self.perp)
            self._min_amount = float(m["limits"]["amount"]["min"] or 0.001)
            self._amount_precision = m["precision"]["amount"]
            log.info(f"市场: {self.perp} 最小下单量={self._min_amount} 精度={self._amount_precision}")
        except Exception as e:
            log.warning(f"加载市场失败, 用默认值: {e}")
            self._min_amount = 0.001
            self._amount_precision = 0.001

    def fmt_amount(self, btc_qty):
        self._load_market()
        q = float(self.ex.amount_to_precision(self.perp, btc_qty))
        return max(q, self._min_amount)

    def price(self):
        return float(self.ex.fetch_ticker(self.perp)["last"])

    def ohlcv(self, timeframe, limit=200, symbol=None):
        return self.ex.fetch_ohlcv(symbol or self.perp, timeframe, limit=limit)

    def funding_rate(self):
        try:
            return float(self.ex.fetch_funding_rate(self.perp).get("fundingRate") or 0)
        except Exception as e:
            log.warning(f"获取资金费失败: {e}")
            return 0.0

    def set_leverage(self):
        if self.dry:
            return
        try:
            self.ex.set_leverage(self.cfg.grid.leverage, self.perp)
        except Exception as e:
            log.warning(f"设置杠杆失败: {e}")

    def place_limit(self, side, price, qty, reduce_only=False):
        if self.dry:
            oid = f"sim-{uuid.uuid4().hex[:8]}"
            self.sim_orders[oid] = {"side": side, "price": price, "qty": qty,
                                    "filled": False, "ts": time.time()}
            log.info(f"[DRY] 挂单 {side} {qty}BTC@{price:.1f} reduce_only={reduce_only} id={oid}")
            return oid
        amt = self.fmt_amount(qty)
        params = {"timeInForce": "GTX"}  # GTX = post-only, 保证 maker
        if reduce_only:
            params["reduceOnly"] = True
        o = self.ex.create_order(self.perp, "limit", side, amt, price, params)
        log.info(f"挂单 {side} {amt}BTC@{price:.1f} id={o['id']}")
        return o["id"]

    def cancel(self, order_id):
        if self.dry:
            self.sim_orders.pop(order_id, None)
            return
        try:
            self.ex.cancel_order(order_id, self.perp)
        except Exception as e:
            log.warning(f"撤单失败 {order_id}: {e}")

    def is_filled(self, order_id, last_price):
        if self.dry:
            o = self.sim_orders.get(order_id)
            if not o or o["filled"]:
                return bool(o and o["filled"])
            if (o["side"] == "buy" and last_price <= o["price"]) or \
               (o["side"] == "sell" and last_price >= o["price"]):
                o["filled"] = True
                log.info(f"[DRY] 模拟成交 {o['side']} {o['qty']}@{o['price']:.1f}")
                return True
            return False
        try:
            return self.ex.fetch_order(order_id, self.perp)["status"] == "closed"
        except Exception as e:
            log.warning(f"查询订单失败 {order_id}: {e}")
            return False

    def close_position_side(self, side, qty):
        if qty <= 0:
            return
        order_side = "sell" if side == "long" else "buy"
        if self.dry:
            log.info(f"[DRY] 市价平仓 {side} qty={qty}")
            return
        amt = self.fmt_amount(qty)
        self.ex.create_order(self.perp, "market", order_side, amt,
                             params={"reduceOnly": True})

    def position_info(self):
        if self.dry:
            return {"qty": 0.0, "liq_price": 0.0, "entry_price": 0.0}
        try:
            for p in self.ex.fetch_positions([self.perp]):
                if p["symbol"] == self.perp and p.get("contracts"):
                    return {"qty": float(p["contracts"]),
                            "liq_price": float(p.get("liquidationPrice") or 0),
                            "entry_price": float(p.get("entryPrice") or 0)}
        except Exception as e:
            log.warning(f"查询持仓失败: {e}")
        return {"qty": 0.0, "liq_price": 0.0, "entry_price": 0.0}

    def balances(self):
        if self.dry:
            return {"dry": True}
        out = {"dry": False}
        try:
            b = self.ex.fetch_balance()
            u = b.get("USDT", {})
            out["fut_usdt_total"] = float(u.get("total") or 0)
            out["fut_usdt_free"] = float(u.get("free") or 0)
            out["fut_usdt_used"] = float(u.get("used") or 0)
        except Exception as e:
            log.warning(f"查余额失败: {e}")
            out["err"] = str(e)[:60]
        return out
