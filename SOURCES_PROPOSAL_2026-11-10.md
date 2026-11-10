# IndieHOT 增加信源 — 待选清单（2026-11-10 12:15 实测）

用户原话（2026-11-10 12:04，main chat）：“能不能多增加一些源，对于教程、开发日志、新游发行、观点洞察、拆解分析、八卦、资源分享比较感兴趣，特别是增加中国的源”。

现有 12 个源（sources.json），全是英文/西方站，0 个中文源。本清单逐个实测，只收录当场 HTTP 200 且解析出条目的。

## 一、推荐加入（6 个，12 → 18 个源）

| # | 源 | RSS | 实测（2026-11-10 约 12:10-12:15，Asia/Shanghai） | 覆盖用户要的类 | 建议配置 |
|---|---|---|---|---|---|
| 1 | 触乐 ChuApp | https://www.chuapp.com/feed | 200，30 条，最新 2026-11-09 23:17 | 观点洞察、拆解分析、行业数据（例：《影之刃零》72小时销量破400万、在AI战争中幸存的策划） | id `chuapp`，name `触乐`，tier T2，category_hint `industry` |
| 2 | 机核 GCORES | https://www.gcores.com/rss | 200，20 条，最新 2026-11-10 09:20 | 八卦、新游发行、观点（中文） | id `gcores`，name `机核`，tier T2，category_hint `industry` |
| 3 | Warp Door | https://warpdoor.com/feed | 200，15 条，最新 2026-11-09 | 新游发行、开发日志（英文小游戏/ jam 新作） | id `warpdoor`，name `Warp Door`，tier T2，category_hint `devlog` |
| 4 | GameFromScratch | https://gamefromscratch.com/feed | 200，10 条，最新 2026-11-09 16:20 | 教程、引擎与工具、资源分享（例：Godot Terrain3D 1.1.0、免费资源月报） | id `gamefromscratch`，name `GameFromScratch`，tier T2，category_hint `engine` |
| 5 | Kenney | https://kenney.nl/feed | 200，25 条，最新 2026-11-07 | 资源分享（免费游戏美术资源） | id `kenney`，name `Kenney`，tier T2，category_hint `engine` |
| 6 | OpenGameArt | https://opengameart.org/rss.xml | 200，10 条，最新 2026-10-29 | 资源分享、Game Jam（更新偏慢，约月更） | id `opengameart`，name `OpenGameArt`，tier T2，category_hint `engine` |

可直接追加的 JSON（照 sources.json 现有格式）：

```json
{"category_hint":"industry","id":"chuapp","name":"触乐","tier":"T2","url":"https://www.chuapp.com/feed"},
{"category_hint":"industry","id":"gcores","name":"机核","tier":"T2","url":"https://www.gcores.com/rss"},
{"category_hint":"devlog","id":"warpdoor","name":"Warp Door","tier":"T2","url":"https://warpdoor.com/feed"},
{"category_hint":"engine","id":"gamefromscratch","name":"GameFromScratch","tier":"T2","url":"https://gamefromscratch.com/feed"},
{"category_hint":"engine","id":"kenney","name":"Kenney","tier":"T2","url":"https://kenney.nl/feed"},
{"category_hint":"engine","id":"opengameart","name":"OpenGameArt","tier":"T2","url":"https://opengameart.org/rss.xml"}
```

注意：
- 机核 RSS 是全站流，最新标题里 GTA6、质量效应 5 这类玩家新闻不少，开发者内容会被分类器按关键词分流，但仍会带进一些噪音；它是目前唯一实测可用的中文“八卦/观点”源，取舍由她定。
- 触乐、机核本身就是中文，入库后不需要翻译，精选/热点翻中文的工作量不增加；新增的 4 个英文源按现有流程走（精选/热点才翻）。
- 生成器已支持 RSS 2.0 与 Atom（tools/generate.py parse_feed），以上 6 个都能直接解析；半小时任务每轮多拉 6 个源，无需改流程。

## 二、备选（1 个，可加可不加）

- Reddit r/devblogs — https://www.reddit.com/r/devblogs/.rss ，200，25 条，最新 2026-11-09/10，纯开发日志。但社区投稿质量参差（有“求反馈”帖），且 r/gamedev 的 RSS 同场实测 403。建议先不加，6 个跑顺了再说。

## 三、实测后排除的（别再试同一批）

- indienova（最想要的中文独立游戏站）：/feed、/rss、/atom.xml 全是 404，首页 HTML 里也没有 RSS 订阅链接——没有公开 RSS，加不进去。公开 RSSHub 实例 https://rsshub.app/indienova/article 实测 403。想加它得自建 RSSHub 或等它出 RSS，不建议现在折腾。
- 游戏葡萄 youxiputao：/feed、/rss 全 404，首页无 RSS 链接。
- VGTime 游戏时光：/feed 404。
- 游戏大观 GameLook：两次请求都超时（20–25 秒无响应）。
- IndieGames.com：https://indiegames.com/feed 能拉通（200，10 条），但频道标题就是 "Indie Games Plus"，与现有源 indiegamesplus（https://indiegamesplus.com/feed）疑似同一份 feed，加了会重复，别加。
- TIGSource：403 Cloudflare 拦截，与 Unreal 博客同一类风险。
- itch.io 博客：429 被限流；Alpha Beta Gamer：两次超时（环境内不稳定）；IndieDB：/rss 跳回 HTML 页，0 条；GDQuest、Catlike Coding、GameDev.tv：/feed 均 404；GameDiscoverCo、How to Market a Game 的 Substack：/feed 404（slug/路径未核实）；Game Maker's Toolkit 的 YouTube RSS：用查到的频道 ID 拉仍 404，未核实，不加。

## 四、坦白说的缺口

- 中文教程类没有找到带公开 RSS 的源：中文教程基本在微信公众号、B站、知乎专栏，都没有公开 RSS，只能靠 RSSHub 类工具转接，可靠性差。这一块目前只能靠英文源（GameFromScratch、GameDev.net、80 Level）顶着。
- “八卦”没有干净的独立源，机核/触乐只能部分覆盖。
