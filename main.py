#!/usr/bin/env python3
"""
XMR 币本位网格机器人
  python main.py live                     # 实盘/dry-run (由 config.yaml 控制)
  python main.py backtest --days 180      # 在线拉K线回测
  python main.py backtest --csv data.csv  # 本地CSV回测
"""
import argparse
import logging
import sys
from pathlib import Path

from bot.config import load_config


def setup_logging(cfg):
    Path(cfg.storage.log_path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cfg.storage.log_path, encoding="utf-8")])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["live", "backtest", "multisim"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    if args.cmd == "live":
        from bot.live import LiveRunner
        LiveRunner(cfg, config_path=args.config).run()
    elif args.cmd == "multisim":
        from bot.multisim_run import MultiSimRunner
        MultiSimRunner(cfg).run()
    else:
        from bot import backtest as bt
        candles = bt.load_csv(args.csv) if args.csv else bt.fetch_klines(cfg, args.days)
        if not candles:
            print("没有K线数据")
            return
        engine, eq = bt.run_backtest(cfg, candles)
        bt.report(engine, eq, candles)


if __name__ == "__main__":
    main()
