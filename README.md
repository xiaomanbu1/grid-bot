# XMR 币本位网格机器人 (Gate.io)

**策略**: 锚定日线 EMA30 的中性合约网格 + 资金费偏置 + 趋势熔断 + 利润回购现货 XMR。
目标是 **XMR 数量增长**（币赚币），不是 USDT 收益。

## 架构

```
main.py                 入口: live / backtest
bot/strategy.py         策略引擎 (纯逻辑, 回测和实盘共用)
bot/exchange.py         Gate 适配层 (ccxt), 支持 dry_run 模拟
bot/live.py             实盘主循环
bot/backtest.py         回测器 (在线拉K线或本地CSV)
bot/telegram_bot.py     TG 控制: /status /pnl /pause /resume /recenter /kill
bot/storage.py          SQLite 记账
bot/webserver.py        Web 面板后端 (Flask, 嵌在主进程)
bot/static/index.html   Web 面板前端 (单页, 零构建)
config.yaml             全部参数
```

## 部署 (VPS)

```bash
git clone <your-repo> && cd xmr-grid-bot
pip install -r requirements.txt --break-system-packages
cp config.yaml config.local.yaml   # 填 API key / TG token
# Gate API key: 只开 [合约交易+现货交易], 关提现, 绑VPS IP白名单

# 1. 先回测最近6个月真实行情
python main.py backtest --days 180 --config config.local.yaml

# 2. dry_run: true 跑至少一周, 看TG日报和日志
python main.py live --config config.local.yaml

# 3. 确认行为符合预期后 dry_run: false, 小仓位实盘
```

### systemd

```ini
# /etc/systemd/system/xmr-grid.service
[Unit]
Description=XMR Grid Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/xmr-grid-bot
ExecStart=/usr/bin/python3 main.py live --config config.local.yaml
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

## Web 控制面板

`python main.py live` 启动后自动监听 `http://127.0.0.1:8787`（config 里 `web` 段）。

- **英雄数字**: 累计回购 XMR —— 币赚币的核心指标
- **网格梯子**: 每格实时状态。橙色实线=持仓中, 虚线=挂单中, 红点=空格/绿点=多格,
  白色横条=现价, 橙色虚线=锚点, 熔断暂停的半边会黄色高亮
- **图表**: 回购XMR/实现盈亏曲线 + 价格vs锚点 (来自快照表, 5秒刷新)
- **操作**: 暂停/恢复/重锚定一键执行; KILL 需要4秒内点两次确认

**安全**:
- 首次打开输入 `web.token`（`openssl rand -hex 16` 生成）
- 默认只绑 127.0.0.1。远程访问两种方式:
  `ssh -L 8787:127.0.0.1:8787 vps` 隧道（推荐）, 或 nginx 反代 + TLS + basic auth
- 千万别裸改 `host: 0.0.0.0` 暴露公网 —— 这个面板能下 KILL 指令

## 关键参数怎么调

| 参数 | 默认 | 说明 |
|---|---|---|
| `grid.step_pct` | 1.75% | 格距。太密被手续费吃光, 太疏成交少 |
| `grid.range_pct` | ±25% | 网格半径。XMR 月波动 ~8%, 给足空间 |
| `grid.usdt_per_grid` | 20U | 单格名义。满格名义 ≈ 这个 × 14格/侧 |
| `grid.leverage` | 3x | 别加。XMR 出现过单月 -52% |
| `funding_bias.threshold` | 0.05%/8h | 资金费偏置触发线 |
| `circuit_breaker.consecutive_closes` | 3根4h | 熔断灵敏度。调小=止损快但易被假突破洗 |
| `profit_recycle.recycle_fraction` | 60% | 利润中拿去买币的比例, 剩余留保证金缓冲 |
| `risk.daily_loss_limit_usdt` | 100U | 当日实现亏损 kill switch |

## 回测结论 (合成数据验证, 务必用真实K线重跑)

- **宽幅震荡市**: 网格轮次密集, 利润稳定回购成 XMR —— 设计场景
- **单边趋势市**: 熔断止损会吃掉网格利润, 实现盈亏可能为负;
  回购的 XMR 是高水位线锁定的"已落袋"利润, 趋势市里就是少回购甚至不回购
- **结论**: 这个策略赌的是"XMR 维持 280-420 区间整理"。如果你判断要走单边, 关掉它

## 诚实的风险清单

1. **下架风险最大**: Gate 是隐私币下架潮后少数还有 XMR 合约的所。一旦公告下架,
   先 `/kill` 平仓提币, 策略停摆。别放超过策略所需的资金在所里
2. 熔断保护的是仓位, 保不了缺口: 极端新闻一根 -30% 大阴线, 熔断也是在亏损后触发
3. dry_run 的成交模拟偏乐观 (价格触线即成交, 实际 post-only 可能排队不成交)
4. 回测用 1h K线模拟格内成交, 真实成交频率会略低于回测
5. 资金费偏置依赖你的现货底仓做对冲, 现货不在 Gate 时它只是裸空敞口, 注意

## 安全

- API key 严禁开提现权限, 绑 IP 白名单
- config.local.yaml 加进 .gitignore, 别把 key 推到 GitHub
- TG bot 只响应 config 里的 chat_id
