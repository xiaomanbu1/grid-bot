"""SQLite 记账: 每格成交 + 净值快照. 线程安全 (主循环写, TG线程读)."""
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False 允许跨线程访问 (TG线程要读, 主线程要写)
        # 配合 _lock 串行化所有操作, 避免并发损坏
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                ts REAL, side TEXT, entry_price REAL, exit_price REAL,
                qty REAL, pnl_usdt REAL
            );
            CREATE TABLE IF NOT EXISTS recycles (
                ts REAL, usdt REAL, xmr REAL, price REAL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                ts REAL, price REAL, anchor REAL, realized_pnl REAL,
                xmr_bought REAL, net_pos_usdt REAL, funding REAL
            );
            """)
            self.db.commit()

    def trade(self, side, entry, exit_, qty, pnl):
        with self._lock:
            self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?)",
                            (time.time(), side, entry, exit_, qty, pnl))
            self.db.commit()

    def recycle(self, usdt, xmr, price):
        with self._lock:
            self.db.execute("INSERT INTO recycles VALUES (?,?,?,?)",
                            (time.time(), usdt, xmr, price))
            self.db.commit()

    def snapshot(self, price, anchor, realized, xmr_bought, net_pos, funding):
        with self._lock:
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                            (time.time(), price, anchor, realized, xmr_bought,
                             net_pos, funding))
            self.db.commit()

    def summary(self) -> dict:
        with self._lock:
            c = self.db.cursor()
            trips, pnl = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(pnl_usdt),0) FROM trades").fetchone()
            xmr, = c.execute(
                "SELECT COALESCE(SUM(xmr),0) FROM recycles").fetchone()
        return {"round_trips": trips, "realized_pnl": pnl, "xmr_bought": xmr}

    def clear_trades(self):
        """清空成交和回购记录 (盈亏统计归零). 快照保留(画图用)."""
        with self._lock:
            self.db.execute("DELETE FROM trades")
            self.db.execute("DELETE FROM recycles")
            self.db.commit()
