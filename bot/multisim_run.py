"""
多币模拟对比运行器. 独立于实盘容器运行.
- 主循环: 每轮拉各币行情跑模拟
- Web 面板: 横向对比排名表 (端口默认 8788)
- 每日 TG 报告: 各币表现排名, 帮你决定换哪个币
"""
import time
import logging
import threading
import datetime as dt

import requests
from flask import Flask, jsonify, send_from_directory
from pathlib import Path

from .multisim import MultiSim

log = logging.getLogger("multisim_run")
STATIC = Path(__file__).parent / "static"


class MultiSimRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        syms = cfg.get("multisim", {}).get("symbols", ["BTC", "ETH", "SOL", "DOGE", "BNB"]) \
            if hasattr(cfg, "get") else ["BTC", "ETH", "SOL", "DOGE", "BNB"]
        self.sim = MultiSim(cfg, syms)
        self.last_report = time.time()

    # ---------- TG ----------

    def tg_send(self, text):
        if not self.cfg.telegram.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram.bot_token}/sendMessage",
                json={"chat_id": str(self.cfg.telegram.chat_id), "text": text},
                timeout=15)
        except Exception as e:
            log.warning(f"TG发送失败: {e}")

    def _report_text(self):
        rows = self.sim.leaderboard()
        lines = ["📊 多币模拟对比 (按虚拟盈亏排名)\n"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}. {r['symbol']}: {r['realized_pnl']:+.2f}U "
                f"| {r['round_trips']}轮 | 日均{r['pnl_per_day']:+.2f}U "
                f"| 波动{r['vol_pct']}%")
        lines.append("\n💡 日均盈亏高且轮次多 = 当下更适合网格")
        return "\n".join(lines)

    # ---------- Web 对比面板 ----------

    def _start_web(self):
        app = Flask(__name__, static_folder=None)
        token = self.cfg.web.token

        @app.get("/")
        def idx():
            return send_from_directory(STATIC, "compare.html")

        @app.get("/api/compare")
        def compare():
            from flask import request
            if request.headers.get("X-Auth-Token") != token:
                return jsonify({"error": "认证失败"}), 401
            return jsonify(self.sim.leaderboard())

        port = self.cfg.get("multisim", {}).get("web_port", 8788) \
            if hasattr(self.cfg, "get") else 8788
        threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=port,
                                   debug=False, use_reloader=False, threaded=True),
            daemon=True).start()
        log.info(f"多币对比面板: http://0.0.0.0:{port}")

    # ---------- 主循环 ----------

    def run(self):
        log.info("启动多币模拟对比, 币种: %s", list(self.sim.coins.keys()))
        self._start_web()
        self.tg_send("🔬 多币模拟对比已启动\n币种: " +
                     ", ".join(s.split("/")[0] for s in self.sim.coins))
        while True:
            try:
                self.sim.tick()
                # 每日报告
                interval = self.cfg.telegram.report_interval_hours * 3600
                if time.time() - self.last_report > interval:
                    self.last_report = time.time()
                    self.tg_send(self._report_text())
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.exception(f"多币模拟主循环异常: {e}")
            time.sleep(self.cfg.mode.poll_interval_sec)
