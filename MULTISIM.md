# 实盘 + 多币模拟对比

两个容器一起跑:
- **grid-bot** (实盘): 跑一个币, 真实下单, 面板 8787
- **grid-multisim** (模拟): 并行跑多个币(默认8个), 不下单, 对比面板 8788

模拟盘帮你看"当下哪个币最适合网格", 作为实盘换币的参考。

## 启动

```bash
cd /opt/grid-bot
docker-compose up -d --build      # 一次起两个容器
docker-compose ps                 # 看两个都 Up
```

## 看对比

**网页**: `http://服务器IP:8788` (输 web.token) — 横向排名表, 哪个币虚拟盈亏高/轮次多一目了然

**Telegram**: 每日自动发对比报告 (各币排名)

## 怎么解读

- **日均盈亏高 + 完成轮次多** → 该币当下震荡活跃、适合网格 → 可考虑实盘换过去
- **轮次很少** → 价格平淡没成交, 网格赚不到钱
- **盈亏为负** → 可能在走单边趋势, 网格在吃亏, 别碰

## 配置模拟哪些币

改 config.local.yaml 的 multisim.symbols:

```yaml
multisim:
  symbols: ["BTC", "ETH", "SOL", "DOGE", "BNB", "XRP", "AVAX", "LINK"]
  web_port: 8788
```

改完 `docker-compose restart grid-multisim`。

## 换币操作

看模拟对比发现比如 AVAX 表现最好, 想把实盘换过去:
1. 实盘容器的 Telegram 发 `/symbol AVAX` (会自动按波动率调参)
2. 或改 config.local.yaml 的 perp_symbol 重启

## 重要提醒

- 模拟盘成交是"价格穿过挂单价"判定, 比实盘略乐观(实盘post-only可能排不上队), 实盘表现会打折
- 模拟盘表现好 ≠ 实盘一定好, 它只是个相对参考, 帮你排除明显不适合的币
- 模拟盘不需要API写权限, 纯拉行情, 所以即使实盘那个key有问题, 模拟照样跑
- 两个容器共用一份 config.local.yaml, 但模拟盘只读行情、不碰你的实盘持仓
