# IndieHOT 独立游戏热点

每天盯着独立游戏圈，值得开发者看的，只有几条。

IndieHOT 是一个给独立游戏开发者看的热点站：每天从一批公开信源里收动态，
按规则打分精选——引擎与工具、发行与宣发、开发日志、行业与融资、展会与活动、
评测与复盘，每天早上 08:00（北京时间）出一份日报。只显示标题、摘要与原文链接，
不转载全文；标题与摘要的版权归各来源所有。

信息架构参考卡兹克开源的 [AIHOT](https://github.com/KKKKhazix/AIHOT) 的
采集—评分—精选—日报流水线；本站代码（Python 标准库，无构建步骤）
按 MIT 协议开源。本站不用 AIHOT 的名字与 logo。

## 在线看

在线地址：[https://staroltest.github.io/indie-game-hot/](https://staroltest.github.io/indie-game-hot/)

GitHub Pages：仓库 Settings → Pages → Source 选 `Deploy from a branch`、
分支 `main`、目录 `/ (root)`，保存后访问上面的地址。

## 本地预览

```bash
python3 tools/generate.py --daily   # 拉取一次，生成 index.html / daily.html / data/*.json
python3 -m http.server 8000          # 浏览器打开 http://localhost:8000
```

生成器只用 Python 标准库（`urllib` + `xml.etree`），不需要装任何依赖，
也不需要大模型 API Key 或数据库。

## 更新循环（一轮一轮持续跑）

- **GitHub Actions 是主力**：`.github/workflows/daily.yml` 每 4 小时自动跑一轮
  （cron `0 */4 * * *`），拉取 → 评分 → 生成 → 直接提交回仓库；
  每天早上 06:00（北京时间，UTC 22:00）那轮额外产出当日日报
  （`data/daily.json` + `daily.html`）。公开库的 Actions 分钟数不限，
  跑在 GitHub 的服务器上，不需要自己的电脑开着，也没有常驻服务器或数据库。
  也可以在 Actions 页面手动触发（可勾选同时产出日报）。
- **Muse 每天做一次精选把关**：看精选质量、调权重、修信源，批量提交；
  是补充把关，不是 24 小时在线——主力更新靠 Actions。

## 数据层（静态 JSON，没有数据库）

- `data/items.json`：历史库（最近 30 天），增量更新——新条目按规范化链接去重后合并，
  旧条目按时间淘汰，不会每轮全量覆盖丢历史。
- `data/hot.json`：当前视图——今日精选（48 小时内、规则分 ≥ 65 的前 10 条）、
  热点榜（近 7 天、按热度衰减排序的前 20 条）、全部动态时间线（近 7 天、按时间倒序）。
- `data/daily.json`：当日日报（过去 24 小时、按规则分排序的前 10 条 + 一句话摘要）。

## 配置（改配置不动代码）

- `sources.json`：信源列表（id、名字、RSS 地址、级别 T1 官方一手 / T2 媒体社区、
  默认分类）。只收 2026-11-09 逐个实测能拉通的公开 RSS；不通的不要硬编。
- `config.json`：分类与关键词、打分权重（时间新鲜度 + 信源分级 + 开发者关键词）、
  精选门槛、热点衰减、站点文案。
- 分类：引擎与工具 / 发行与宣发 / 开发日志 / 行业与融资 / 展会与活动 / 评测与复盘。
  精选口径偏向对开发者有用的内容（教程、工具、数据、案例、引擎进展），
  纯玩家向的新游资讯、促销会被降权，不进精选。

## 信源（2026-11-09 实测拉通）

| 信源 | 级别 | 地址 |
|---|---|---|
| Godot 引擎博客 | T1 | godotengine.org/rss.xml |
| Unity 博客 | T1 | blog.unity.com/feed |
| Unreal Engine 博客 | T1 | unrealengine.com/en-US/blog/rss |
| GameMaker 博客 | T1 | gamemaker.io/en/blog/rss |
| Game Developer | T2 | gamedeveloper.com/rss.xml |
| GamesIndustry.biz | T2 | gamesindustry.biz/feed |
| GameDaily | T2 | gamedaily.biz/feed |
| Rock Paper Shotgun | T2 | rockpapershotgun.com/feed |
| Indie Games Plus | T2 | indiegamesplus.com/feed |
| 80 Level | T2 | 80.lv/feed |
| GameDev.net | T2 | gamedev.net/feed/ |
| Hacker News：独立游戏 | T2 | hnrss.org/newest?q=indie+game |
| Indie Game Reviewer | T2 | indiegamereviewer.com/feed |

实测不通、暂未收录：itch.io 博客（429 后 404，不稳定）、Reddit（403 拦截）、
IndieDB（Cloudflare 拦截）、TIGSource（403）、dev.to（超时）、
Defold / Bevy / IGF / Wholesome Games（404）、Steamworks（200 但无条目）、
GDC（404）。以后环境或地址变了可再测再加。

## 可选 LLM 打分

默认只用规则打分，不依赖任何大模型。想让模型参与精选重排时，
在 Actions 的 Secrets 或本地环境里同时设置
`INDIEHOT_LLM_API_KEY`、`INDIEHOT_LLM_BASE_URL`、`INDIEHOT_LLM_MODEL`
（OpenAI 兼容的 `/chat/completions` 接口）；
缺任何一个就跳过，调用失败也自动回退规则分。Key 绝不写进代码或配置文件。

## MCP 接入

本站提供 MCP（Model Context Protocol）接入点，AI 客户端可以直接查热点、
日报、搜索最近动态，返回的同样只有标题、一句简介与原文链接：

- 端点：`https://indie-game-hot.pages.dev/mcp`（Streamable HTTP，无需密钥）
- 工具：`get_hot`（热点榜）、`get_daily`（最新日报）、`search_items`（关键词/分类搜索）
- 实现：`_worker.js`（Cloudflare Pages Advanced Mode，单文件无依赖），
  `_routes.json` 只把 `/mcp` 交给它，其余路径仍走静态托管。

Claude Desktop 配置示例（`mcpServers` 里加一项）：

```json
"indiehot": { "type": "http", "url": "https://indie-game-hot.pages.dev/mcp" }
```

## 许可

MIT License，Copyright (c) 2026 Camille (IndieHOT)。见 [LICENSE](LICENSE)。

## 赞赏支持

如果 IndieHOT 对你有帮助，可以扫码赞赏：

<img src="assets/tip-usdt.png" alt="赞赏码：USDT TRC20" width="200">

USDT · TRC20：`TBH8fAi6Vbr9G4VW3PDm3DugZU6fknr6na`

<img src="assets/tip-usdt-sol.png" alt="赞赏码：USDT Solana" width="200">

USDT · Solana：`7pitxN5xECR2NH2qdhFiSRC3fDdMsmVczqFSY4Zz4v5H`
