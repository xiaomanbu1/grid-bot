"""
Telegram 控制 + 菜单键. 轻量实现 (requests 长轮询), 不依赖第三方 TG 库.

发 /menu 或 /start 弹出内联键盘菜单, 点按钮操作:
  📊 状态 / 💰 盈亏 / 📋 持仓
  ⏸ 暂停 / ▶️ 恢复 / 🎯 重新锚定
  ⚙️ 调参数 (格距/单格金额/杠杆/回购比例, 点 +/- 调整, 写回配置持久化)
  🔀 切换模式 (dry-run ↔ 实盘, 二次确认)
  🛑 KILL (二次确认)

只响应 config 里配置的 chat_id, 别人发命令无效.
"""
import threading
import logging
import requests

log = logging.getLogger("tg")


class TgBot:
    def __init__(self, cfg, controller):
        self.token = cfg.telegram.bot_token
        self.chat_id = str(cfg.telegram.chat_id)
        self.enabled = cfg.telegram.enabled
        self.ctrl = controller
        self.offset = 0
        self._stop = False

    # ---------- 底层 API ----------

    def api(self, method, **kw):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                json=kw, timeout=35)
            return r.json()
        except Exception as e:
            log.warning(f"TG API 失败: {e}")
            return {}

    def send(self, text, keyboard=None):
        if not self.enabled:
            return
        kw = {"chat_id": self.chat_id, "text": text}
        if keyboard:
            kw["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("sendMessage", **kw)

    def _edit(self, msg_id, text, keyboard=None):
        kw = {"chat_id": self.chat_id, "message_id": msg_id, "text": text}
        if keyboard:
            kw["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("editMessageText", **kw)

    def _answer(self, cb_id, text=""):
        self.api("answerCallbackQuery", callback_query_id=cb_id, text=text)

    def start(self):
        if not self.enabled:
            return
        self.api("setMyCommands", commands=[
            {"command": "menu", "description": "打开控制菜单"},
            {"command": "status", "description": "当前状态"},
            {"command": "pnl", "description": "盈亏汇总"},
        ])
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self):
        self._stop = True

    # ---------- 菜单定义 ----------

    def _main_menu(self):
        return [
            [{"text": "📊 状态", "callback_data": "status"},
             {"text": "💰 盈亏", "callback_data": "pnl"},
             {"text": "📋 持仓", "callback_data": "positions"}],
            [{"text": "💵 余额", "callback_data": "balance"},
             {"text": "⏸ 暂停", "callback_data": "pause"},
             {"text": "▶️ 恢复", "callback_data": "resume"}],
            [{"text": "🎯 重新锚定", "callback_data": "recenter"},
             {"text": "⚙️ 调参数", "callback_data": "params"}],
            [{"text": "🪙 切换币种", "callback_data": "symbol"},
             {"text": "🔀 切换模式", "callback_data": "mode"}],
            [{"text": "🛑 KILL", "callback_data": "kill_confirm"}],
        ]

    # 常用币种快捷按钮 (币安主流币). 也可发文字 "/symbol DOGE" 切任意币
    QUICK_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]

    def _symbol_menu(self):
        cur = self.ctrl.cfg["exchange"]["perp_symbol"].split("/")[0]
        rows, row = [], []
        for sym in self.QUICK_SYMBOLS:
            mark = "✅" if sym == cur else ""
            row.append({"text": f"{mark}{sym}", "callback_data": f"sym:{sym}"})
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([{"text": "« 返回", "callback_data": "menu"}])
        return rows

    def _params_menu(self):
        c = self.ctrl
        rows = []
        for key, (_, typ, _, _, name) in c.ADJUSTABLE.items():
            val = c.get_param(key)
            disp = f"{val:.4f}" if typ is float else str(val)
            if key == "step_pct":
                disp = f"{val*100:.2f}%"
            elif key == "recycle_fraction":
                disp = f"{val*100:.0f}%"
            rows.append([
                {"text": "➖", "callback_data": f"dec:{key}"},
                {"text": f"{name}: {disp}", "callback_data": "noop"},
                {"text": "➕", "callback_data": f"inc:{key}"},
            ])
        rows.append([{"text": "« 返回", "callback_data": "menu"}])
        return rows

    STEP = {"step_pct": 0.0025, "usdt_per_grid": 5,
            "leverage": 1, "recycle_fraction": 0.1}

    # ---------- 轮询 ----------

    def _poll_loop(self):
        while not self._stop:
            try:
                resp = self.api("getUpdates", offset=self.offset, timeout=30)
                for u in resp.get("result", []):
                    self.offset = u["update_id"] + 1
                    try:
                        if "message" in u:
                            msg = u["message"]
                            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                                continue
                            self._handle_text(msg.get("text", "").strip())
                        elif "callback_query" in u:
                            cq = u["callback_query"]
                            if str(cq.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                                continue
                            self._handle_callback(cq)
                    except Exception as e:
                        # 单条消息处理出错不能让整个TG线程崩溃
                        log.warning(f"TG 消息处理出错: {e}")
            except Exception as e:
                log.warning(f"TG 轮询出错, 继续: {e}")
                import time as _t
                _t.sleep(3)

    def _handle_text(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        if cmd in ("/menu", "/start"):
            self.send("🤖 网格控制台", self._main_menu())
        elif cmd == "/symbol":
            if len(parts) < 2:
                self.send("用法: /symbol DOGE  (切换到任意币种, 自动按波动率调参)")
            else:
                self.send(self.ctrl.switch_symbol(parts[1], auto_param=True),
                          self._main_menu())
        elif cmd == "/status":
            self.send(self.ctrl.status_text())
        elif cmd == "/pnl":
            self.send(self.ctrl.pnl_text())
        elif cmd.startswith("/"):
            self.send("发 /menu 打开控制菜单")

    def _handle_callback(self, cq):
        data = cq.get("data", "")
        cb_id = cq["id"]
        msg_id = cq["message"]["message_id"]
        c = self.ctrl

        if data == "status":
            self._answer(cb_id); self.send(c.status_text())
        elif data == "pnl":
            self._answer(cb_id); self.send(c.pnl_text())
        elif data == "balance":
            self._answer(cb_id, "查询中..."); self.send(c.balance_text())
        elif data == "positions":
            self._answer(cb_id); self.send(self._positions_text())

        elif data == "pause":
            c.set_paused(True); self._answer(cb_id, "已暂停")
            self.send("⏸ 已暂停开新仓 (已有持仓继续止盈)")
        elif data == "resume":
            c.set_paused(False); self._answer(cb_id, "已恢复")
            self.send("▶️ 已恢复开仓")
        elif data == "recenter":
            c.request_recenter(); self._answer(cb_id, "已请求")
            self.send("🎯 下个循环重新锚定")

        elif data == "menu":
            self._answer(cb_id)
            self._edit(msg_id, "🤖 网格控制台", self._main_menu())
        elif data == "params":
            self._answer(cb_id)
            self._edit(msg_id, "⚙️ 点 ➕/➖ 调整, 自动写回配置", self._params_menu())
        elif data == "noop":
            self._answer(cb_id)

        elif data.startswith(("inc:", "dec:")):
            op, key = data.split(":")
            cur = c.get_param(key)
            step = self.STEP.get(key, 1)
            newv = cur + step if op == "inc" else cur - step
            result = c.set_param(key, round(newv, 6))
            self._answer(cb_id, result[:180])
            self._edit(msg_id, "⚙️ 点 ➕/➖ 调整, 自动写回配置", self._params_menu())

        elif data == "mode":
            self._answer(cb_id)
            dry = c.cfg["mode"]["dry_run"]
            if dry:
                kb = [[{"text": "⚠️ 确认切到实盘", "callback_data": "mode_live"}],
                      [{"text": "« 取消", "callback_data": "menu"}]]
                self._edit(msg_id, "当前: 🟡 DRY-RUN 模拟\n切到实盘后会下真实单, 确认?", kb)
            else:
                kb = [[{"text": "切回 DRY-RUN", "callback_data": "mode_dry"}],
                      [{"text": "« 取消", "callback_data": "menu"}]]
                self._edit(msg_id, "当前: 🔴 实盘\n切回模拟?", kb)
        elif data == "mode_live":
            self._answer(cb_id, "已切实盘")
            self._edit(msg_id, c.set_dry_run(False), self._main_menu())
        elif data == "mode_dry":
            self._answer(cb_id, "已切模拟")
            self._edit(msg_id, c.set_dry_run(True), self._main_menu())

        elif data == "symbol":
            self._answer(cb_id)
            cur = c.cfg["exchange"]["perp_symbol"]
            self._edit(msg_id, f"🪙 当前: {cur}\n选快捷币种, 或发文字 /symbol DOGE 切任意币\n(切换会自动按波动率调格距)", self._symbol_menu())
        elif data.startswith("sym:"):
            sym = data.split(":")[1]
            self._answer(cb_id, f"切换到 {sym}...")
            result = c.switch_symbol(sym, auto_param=True)
            self._edit(msg_id, result, self._main_menu())

        elif data == "kill_confirm":
            self._answer(cb_id)
            kb = [[{"text": "🛑 确认 KILL (撤单停机)", "callback_data": "kill_do"}],
                  [{"text": "« 取消", "callback_data": "menu"}]]
            self._edit(msg_id, "确认紧急停止? 会撤所有挂单并停止策略.\n持仓不动, 需你手动处理.", kb)
        elif data == "kill_do":
            c.kill(); self._answer(cb_id, "已KILL")
            self._edit(msg_id, "🛑 已撤单停机. 持仓请手动处理.", self._main_menu())
        else:
            self._answer(cb_id)

    def _positions_text(self):
        from .strategy import LevelState
        e = self.ctrl.engine
        holding = [l for l in e.levels if l.state == LevelState.HOLDING]
        if not holding:
            return "📋 当前无持仓格子"
        lines = ["📋 持仓格子:"]
        for l in sorted(holding, key=lambda x: -x.entry_price):
            side = "空" if l.side.value == "short" else "多"
            lines.append(f"  {side} {l.entry_price:.2f} -> 止盈 {l.exit_price:.2f} | {l.qty}")
        return "\n".join(lines)
