# 币安 BTC/USDT U本位网格机器人 · Docker 部署

主流币 BTC 永续网格，目标赚 USDT（不做利润回购）。只读监控面板 + Telegram 菜单操作。

## 为什么从 Gate/XMR 换到 币安/BTC

- XMR 在币安已下架，无法交易
- 币安 API 规范，没有 Gate 的统一账户/经典账户端点分裂问题（之前 RISK_CHECK_MARKET_FORBIDDEN 的根源）
- BTC 流动性远好于 XMR，网格滑点小

## 部署三步

```bash
cd /opt/btc-grid-bot
cp config.yaml config.local.yaml
nano config.local.yaml      # 填币安 key、web.token、TG
docker compose up -d --build   # 老docker用 docker-compose（带横杠）
```

## 币安 API key 设置

币安期货 API：https://www.binance.com/zh-CN/my/settings/api-management

- 创建 API key，勾 **"允许合约"**（Enable Futures）
- **关闭"允许提现"**
- 限制 IP：填服务器 IP（强烈建议）
- 把 API Key 和 Secret 填进 config.local.yaml

## ⚠️ 资金门槛（重要）

币安 BTC 永续**最小下单量 0.001 BTC ≈ 60U**。配置里 `usdt_per_grid: 50` 会被自动抬到一个最小单（约60U）。网格双向满仓名义敞口可能到 1500U+，3倍杠杆占用保证金约 500U。**确保合约账户有足够 USDT**，否则开几格就触顶。

资金少的话：调大 `step_pct`（格距）减少格子数，或降低 `range_pct`（范围）。

## Telegram 菜单

发 `/menu`：状态/盈亏/持仓/余额、暂停/恢复/重新锚定、调参数（格距/单格金额/杠杆）、切模式、KILL。参数改动写回配置持久化。

## 面板

`http://服务器IP:8787`，输 web.token。只读，纯展示。

## 切实盘

dry-run 跑几天确认无误 → 币安合约账户充 USDT → Telegram 切换模式 → 小仓位开始。

## 自动调参（待开发）

下一步计划：根据波动率自动调格距、每日重新锚定。当前先手动跑通验证。
