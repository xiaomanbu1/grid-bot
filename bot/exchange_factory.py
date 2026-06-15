"""
交易所工厂: 根据 config.exchange.id 返回对应适配器.
- binance: 币安 U本位永续 (主流币 + 币安上的中小币)
- gate: Gate 永续 (隐私币如 XMR/ZEC, 币安没有的小币)

两个适配器接口一致, 上层代码 (live.py) 无需关心用哪个所.
"""
import logging

log = logging.getLogger("exchange")


def make_exchange(cfg):
    ex_id = cfg.exchange.id.lower()
    if ex_id == "binance":
        from .ex_binance import BinanceAdapter
        return BinanceAdapter(cfg)
    elif ex_id == "gate":
        from .ex_gate import GateAdapter
        return GateAdapter(cfg)
    else:
        raise ValueError(f"不支持的交易所: {ex_id} (支持 binance / gate)")
