"""
Web 控制面板. Flask 线程嵌在主进程里, 直接读写 LiveRunner.
安全: X-Auth-Token 鉴权; 默认只绑 127.0.0.1, 远程访问走 nginx 反代或 SSH 隧道.
"""
import logging
import sqlite3
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

log = logging.getLogger("web")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg, controller):
    app = Flask(__name__, static_folder=None)
    token = cfg.web.token

    def db():
        # web线程独立只读连接
        conn = sqlite3.connect(cfg.storage.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def auth_ok():
        return request.headers.get("X-Auth-Token") == token

    @app.before_request
    def gate():
        if request.path.startswith("/api/") and not auth_ok():
            return jsonify({"error": "认证失败"}), 401

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(controller.status_dict())

    @app.get("/api/grid")
    def grid():
        e = controller.engine
        price = controller.last_price
        return jsonify({
            "price": price,
            "anchor": e.anchor,
            "upper": e.anchor * (1 + cfg.grid.range_pct),
            "lower": e.anchor * (1 - cfg.grid.range_pct),
            "paused": {s.value: True for s in e.paused},
            "levels": [{
                "side": l.side.value,
                "entry": l.entry_price,
                "exit": l.exit_price,
                "state": l.state.value,
                "qty": l.qty,
                "has_order": bool(l.entry_order_id or l.exit_order_id),
            } for l in sorted(e.levels, key=lambda x: -x.entry_price)],
        })

    @app.get("/api/equity")
    def equity():
        hours = int(request.args.get("hours", 168))
        rows = db().execute(
            "SELECT ts, price, anchor, realized_pnl, xmr_bought, net_pos_usdt, funding "
            "FROM snapshots WHERE ts > strftime('%s','now') - ? ORDER BY ts",
            (hours * 3600,)).fetchall()
        # 抽稀到最多500点
        step = max(len(rows) // 500, 1)
        return jsonify([dict(r) for r in rows[::step]])

    @app.get("/api/trades")
    def trades():
        limit = int(request.args.get("limit", 30))
        rows = db().execute(
            "SELECT ts, side, entry_price, exit_price, qty, pnl_usdt "
            "FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/recycles")
    def recycles():
        rows = db().execute(
            "SELECT ts, usdt, xmr, price FROM recycles ORDER BY ts DESC LIMIT 30"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    # 只读面板: 不提供任何控制端点. 所有写操作 (暂停/恢复/重锚定/KILL)
    # 只能通过 Telegram bot 或服务器命令行执行, 网页无法操作 → 暴露公网也无风险.

    return app


def start_web(cfg, controller):
    if not cfg.web.enabled:
        return
    app = create_app(cfg, controller)

    def run():
        app.run(host=cfg.web.host, port=cfg.web.port,
                debug=False, use_reloader=False, threaded=True)

    threading.Thread(target=run, daemon=True).start()
    log.info(f"Web 面板: http://{cfg.web.host}:{cfg.web.port}")
