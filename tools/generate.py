#!/usr/bin/env python3
"""IndieHOT 生成器：拉取 RSS -> 去重 -> 规则打分 -> 生成静态站。

只用 Python 标准库（urllib + xml.etree），不依赖任何外部包，也不依赖
大模型 API 或数据库；数据层就是 data/ 下的 JSON 文件。

流水线（参考 AIHOT 的信息架构：采集 -> 判重 -> 评分 -> 精选 -> 日报，
但全部落成静态文件，供 GitHub Pages + Actions 定时跑）：
  1. 拉取 sources.json 里的公开 RSS/Atom（只读标题+摘要+链接，不转载全文）
  2. 按规范化链接去重，与 data/store.json 里的本地历史合并（增量更新；store.json 不推送）
  3. 规则打分：时间新鲜度 + 信源分级 + 开发者关键词（config.json 可调）
  4. 产出 data/hot.json（热点榜 Top 20）、data/days/（按天分块）与 index.html
  5. 带 --daily 时额外产出 data/daily.json 与 daily.html（当日日报）

可选 LLM 打分：只有同时设置 INDIEHOT_LLM_API_KEY、INDIEHOT_LLM_BASE_URL、
INDIEHOT_LLM_MODEL 三个环境变量时才启用；任何失败都静默回退到规则分，
且绝不把 Key 写进代码、配置或输出文件。
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "IndieHOTBot/1.0 (+https://github.com/Staroltest/indie-game-hot)"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 工具函数

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data, compact=False):
    # 写 JSON（原子替换）。compact=True 时写紧凑单行，省体积（分块数据与 hot.json 用）。
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def shorten(text, limit=200):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


SENT_END = "。！？.!?"
CLAUSE_END = "，、；：,;:；"
CLOSE_CHARS = "）】」』\"'”’"


def complete_para(text, limit=1500):
    """把一段摘要收成完整的一段话：不超过 limit，以句末标点收尾，不加省略号。
    详情页导读/正文摘要用：不允许截断到半句、末尾挂 …。
    已在限制内且以句末标点结尾的原样返回；否则退到 limit 内最后一个完整句；
    无完整句时退到最后一个分句标点；再无则硬截（仍不加 …）。"""
    text = (text or "").strip()
    if not text:
        return ""
    if text.endswith("…"):
        text = text[:-1].rstrip()
    if not text:
        return ""
    if len(text) <= limit and text[-1] in SENT_END:
        return text
    cut = text[:limit] if len(text) > limit else text
    best = -1
    for i, ch in enumerate(cut):
        if ch not in SENT_END:
            continue
        # ASCII 句点夹在数字之间是版本号/小数（如 4.8），不是句末
        if ch == "." and i > 0 and i + 1 < len(cut) \
                and cut[i - 1].isdigit() and cut[i + 1].isdigit():
            continue
        j = i + 1
        while j < len(cut) and cut[j] in CLOSE_CHARS:
            j += 1
        best = j
    if best > 0:
        return cut[:best].rstrip()
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] in CLAUSE_END:
            return cut[:i + 1].rstrip()
    return cut.rstrip()


def norm_link(link):
    """去 fragment 与 utm_* 追踪参数，供去重用。"""
    link = (link or "").strip().split("#")[0]
    if "?" in link:
        base, qs = link.split("?", 1)
        kept = [p for p in qs.split("&") if p and not p.lower().startswith("utm_")]
        link = base + ("?" + "&".join(kept) if kept else "")
    return link.rstrip("/")


def parse_date(text):
    """RSS 的 RFC 2822 与 Atom 的 ISO 8601 都接受；失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_cst(dt):
    return dt.astimezone(CST).strftime("%m月%d日 %H:%M")


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- 抓取与解析

def fetch(url, timeout=25, attempts=2):
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(5)
    raise last_err


def parse_feed(body, source):
    """RSS 2.0 与 Atom 1.0 都解析，返回条目 dict 列表。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        name = local_name(el.tag)
        if name not in ("item", "entry"):
            continue
        fields = {}
        for child in el:
            cn = local_name(child.tag)
            if cn in ("title", "link", "pubDate", "published", "updated",
                      "description", "summary", "content", "encoded"):
                if cn == "link":
                    href = child.get("href")
                    # Atom: <link href="..."/>；RSS: <link>url</link>
                    fields.setdefault("link", href or (child.text or "").strip())
                else:
                    fields.setdefault(cn, (child.text or "").strip())
        title = fields.get("title", "").strip()
        link = norm_link(fields.get("link", ""))
        if not title or not link:
            continue
        summary = strip_html(fields.get("description") or fields.get("summary") or "")
        if not summary:
            summary = strip_html(fields.get("content") or fields.get("encoded") or "")
        # 完整摘要：详情页用（卡片仍用 200 字短摘要）。只有比短摘要明显更长时
        # 才存，短摘要本身已完整的条目不存，省体积。收成完整的一段话，
        # 不截断到半句、不挂省略号（导读/正文摘要不允许截断）。
        summary_full = complete_para(summary, 1500) if len(summary) > 200 else ""
        pub = parse_date(fields.get("pubDate") or fields.get("published")
                         or fields.get("updated"))
        item = {
            "title": title,
            "link": link,
            "source": source["name"],
            "source_id": source["id"],
            "published": pub,  # datetime 或 None（调用方补抓取时间）
            "summary": shorten(summary),
        }
        if summary_full:
            item["summary_full"] = summary_full
        items.append(item)
    return items


# ---------------------------------------------------------------- 分类与打分

def classify(item, categories, fallback):
    text = (item["title"] + " " + item["summary"]).lower()
    for cat in categories:
        for kw in cat["keywords"]:
            if kw.lower() in text:
                return cat["key"]
    return fallback


def score(item, cfg, now):
    sc = cfg["scoring"]
    total = sc["base"]
    age_h = (now - item["_pub"]).total_seconds() / 3600.0
    for rule in sc["recency"]:
        if age_h <= rule["max_hours"]:
            total += rule["bonus"]
            break
    total += sc["tier_bonus"].get(item["_tier"], 0)
    text = (item["title"] + " " + item["summary"]).lower()
    dev_hits = sum(1 for kw in sc["dev_keywords"] if kw.lower() in text)
    total += min(dev_hits * sc["dev_keyword_bonus"], sc["dev_keyword_cap"])
    noise_hits = sum(1 for kw in sc["noise_keywords"] if kw.lower() in text)
    total += max(noise_hits * sc["noise_keyword_penalty"], sc["noise_keyword_floor"])
    return max(0, min(100, total))


# 开发者关键词 -> 中文标签（推荐理由里只出现中文，不漏英文关键词）
REASON_LABELS = {
    "教程": "教程", "tutorial": "教程",
    "工具": "工具", "tool": "工具",
    "数据": "数据", "data": "数据",
    "案例": "案例", "case study": "案例",
    "开源": "开源", "open source": "开源",
    "指南": "指南", "guide": "指南",
    "复盘": "复盘", "postmortem": "复盘",
    "引擎": "引擎", "engine": "引擎",
    "优化": "优化", "optimization": "优化",
    "性能": "性能", "performance": "性能",
    "收入": "营收", "revenue": "营收",
    "销量": "销量", "sales": "销量",
    "经验": "经验教训", "教训": "经验教训", "lesson": "经验教训",
    "源码": "源码", "source code": "源码",
    "工作流": "工作流", "workflow": "工作流",
}


# 中文标签 -> 主题页 slug（B11/B16；与 REASON_LABELS 的值一一对应）
TAG_SLUGS = {
    "教程": "tutorial", "工具": "tools", "数据": "data", "案例": "case-study",
    "开源": "open-source", "指南": "guide", "复盘": "postmortem",
    "引擎": "engine", "优化": "optimization", "性能": "performance",
    "营收": "revenue", "销量": "sales", "经验教训": "lessons",
    "源码": "source-code", "工作流": "workflow",
}


def labels_for(item, cfg):
    """标题+摘要命中开发者关键词 -> 去重后的中文标签（B16/C1 共用）。"""
    text = (item["title"] + " " + (item.get("summary") or "")).lower()
    labels = []
    for kw in cfg["scoring"]["dev_keywords"]:
        if kw.lower() in text:
            lab = REASON_LABELS.get(kw, kw)
            if lab not in labels:
                labels.append(lab)
    return labels


def reason_for(item, cfg, now):
    """从打分信号拼一句推荐理由（只用真实派生出的事实，不编造）：
    T1 信源 -> 官方来源；命中开发者关键词 -> 中文标签（去重，最多两个）；
    6/24 小时内发布 -> 新鲜度；只有一条理由时补一条默认理由，保证每卡
    至少有两条可读信息。"""
    parts = []
    if item.get("_tier") == "T1":
        parts.append("官方来源")
    labels = labels_for(item, cfg)
    if labels:
        parts.append("涉及" + "与".join(labels[:2]))
    age_h = (now - item["_pub"]).total_seconds() / 3600.0
    if age_h <= 6:
        parts.append("6 小时内发布")
    elif age_h <= 24:
        parts.append("24 小时内发布")
    if len(parts) < 2:
        parts.append("独立游戏开发相关")
    return " · ".join(parts)


def maybe_llm_rerank(items):
    """可选 LLM 打分：三个环境变量缺一即跳过；任何异常都回退规则分。"""
    key = os.environ.get("INDIEHOT_LLM_API_KEY", "")
    base = os.environ.get("INDIEHOT_LLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("INDIEHOT_LLM_MODEL", "")
    if not (key and base and model):
        return items
    cands = items[:30]
    if not cands:
        return items
    try:
        import urllib.request as u
        listing = "\n".join(
            f'{i}. [{c["id"][:8]}] {c["title"]}（{c["source"]}）' for i, c in enumerate(cands))
        prompt = ("你是独立游戏开发者社区的编辑。给下面每条动态按“对独立游戏开发者"
                  "的实用价值”打 0-100 分（教程、工具、数据、案例、引擎进展高分；"
                  "纯玩家向新游资讯、促销低分）。只输出 JSON 数组，形如 "
                  '[{"id": "前8位id", "score": 82}]，不要其他文字。\n' + listing)
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode("utf-8")
        req = u.Request(base + "/chat/completions", data=payload,
                        headers={"Authorization": "Bearer " + key,
                                 "Content-Type": "application/json"})
        with u.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return items
        for entry in json.loads(m.group(0)):
            for c in cands:
                if c["id"].startswith(entry.get("id", "x")) and \
                        isinstance(entry.get("score"), (int, float)):
                    c["score"] = max(0, min(100, int(entry["score"])))
                    c["scored_by"] = "llm"
        print("LLM rerank applied to %d items" % len(cands))
    except Exception as e:  # noqa: BLE001 - 任何 LLM 失败都回退规则分
        print("LLM rerank skipped (%s)" % e, file=sys.stderr)
    return items


# ---------------------------------------------------------------- 渲染

LOGO_SVG = """<svg width="44" height="44" viewBox="0 0 48 48" aria-hidden="true">
<defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#ff6b35"/><stop offset="1" stop-color="#f7b733"/>
</linearGradient></defs>
<rect x="2" y="2" width="44" height="44" rx="11" fill="url(#lg)"/>
<rect x="14" y="14" width="20" height="20" rx="3" transform="rotate(45 24 24)" fill="#fff"/>
<rect x="20" y="20" width="8" height="8" rx="1.5" transform="rotate(45 24 24)" fill="#ff6b35"/>
</svg>"""

CSS = """
/* 设计语言：暖纸底色、青色点缀、细线边框、无渐变；
   侧栏导航、热点条、时间线时间轨、评分徽章均为其组件的静态化移植。品牌与 logo 仍是 IndieHOT 自己的。 */
:root{
  --bg:#faf9f6; --bg-sunk:#f3f3ef; --bg-muted:#e9ede9; --well:var(--bg-sunk);
  --surface:#fff; --surface-2:#fdfdfb; --sidebar:#fdfdfb; --daybar:#f0f2ee;
  --ink:#202a30; --ink-2:#303c42; --ink-3:#59656b; --ink-4:#657176;
  --line:#dfe4e1; --line-strong:#d1d9d5; --line-soft:#e9ede9;
  --accent:#176b75; --accent-strong:#0f565e; --accent-ink:#145f68; --accent-soft:rgba(23,107,117,.08); --accent-softer:rgba(23,107,117,.05);
  --note:#526f72; --hot:#b3402a; --hot-soft:rgba(179,64,42,.08);
  --amber:#b8873a; --amber-ink:#7c5a24; --amber-soft:rgba(184,135,58,.1); --ok:#2f7d5c;
  --rank-1:#b3402a; --rank-2:#a3642f; --rank-3:#96702e; --rank-rest:#6b7684;
  --shadow-card:0 1px 2px rgba(32,42,48,.025);
  --shadow-card-hover:0 3px 12px rgba(32,42,48,.055);
  --shadow-thumb:0 1px 2px rgba(28,39,51,.1),0 1px 1px rgba(28,39,51,.04);
  --raised:#ffffff; --selection:rgba(23,107,117,.16); --shadow-soft:0 4px 12px rgba(28,39,51,.06);
  --accent-contrast:#ffffff; --ok-soft:rgba(47,125,92,.1); --ok-ink:#2a6e51;
  --shadow-pop:0 12px 32px rgba(28,39,51,.12); --prose-ink:#26323a;
  --code-bg:#f6f7f4; --code-keyword:#006d77; --code-string:#2f6f44; --code-number:#9a6700;
  --code-title:#6f42c1; --code-variable:#a13d10; --code-meta:#b42348;
  --page-max-wide:max(1200px,calc(270dvh - 236px));
  --page-max-reading:max(1100px,calc(200dvh - 236px));
  --bar-h:48px; color-scheme:light;
}
@media(min-width:700px){:root{--bar-h:0px}}
[data-theme="dark"]{
  color-scheme:dark;
  --bg:#13191c; --bg-sunk:#182024; --bg-muted:#202a2f; --well:color-mix(in oklab, var(--bg-muted) 60%, transparent);
  --surface:#1a2226; --surface-2:#1d272b; --sidebar:#161e21; --daybar:#182024;
  --ink:#e7eceb; --ink-2:#d2dada; --ink-3:#a5b0b3; --ink-4:#89979d;
  --line:rgba(255,255,255,.08); --line-strong:rgba(255,255,255,.12); --line-soft:rgba(255,255,255,.06);
  --accent:#12ccd8; --accent-strong:#0db2bd; --accent-ink:#2ce2e8; --accent-soft:rgba(18,204,216,.12); --accent-softer:rgba(18,204,216,.06);
  --note:#8fb8a8; --hot:#d86a52; --hot-soft:rgba(216,106,82,.12);
  --amber:#d3b26a; --amber-ink:#d3b26a; --amber-soft:rgba(211,178,106,.12); --ok:#5fc79a;
  --rank-1:#d86a52; --rank-2:#d18a5e; --rank-3:#d3b26a; --rank-rest:#7b869a;
  --shadow-card:none; --shadow-card-hover:none;
  --raised:#202a2f; --selection:rgba(18,204,216,.22); --shadow-soft:0 6px 18px rgba(0,0,0,.35);
  --shadow-thumb:0 1px 2px rgba(0,0,0,.45);
  --accent-contrast:#000000; --ok-soft:rgba(95,199,154,.12); --ok-ink:#7fd1ad;
  --shadow-pop:0 16px 40px rgba(0,0,0,.45); --prose-ink:#d7dfdf;
  --code-bg:#10151c; --code-keyword:#78dce8; --code-string:#a9dc76; --code-number:#ffd866;
  --code-title:#ab9df2; --code-variable:#fc9867; --code-meta:#ff6188;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;text-autospace:normal}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","HarmonyOS Sans SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.5}
:root:not([data-theme="dark"]) body:has(.report-shell){background:color-mix(in srgb, var(--bg) 60%, var(--surface))}
::selection{background:var(--selection)}
a{color:inherit;text-decoration:none}
button{cursor:pointer;font:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:5px}
.num{font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}

/* ---- 壳：侧栏 / 窄轨 / 主列 / 手机顶栏与底栏 ---- */
.shell{display:flex;min-height:100dvh}
.sidebar,aside.rail{display:none}
.main{flex:1;min-width:0}
/* 列表宽度：桌面撑满主列，上限 max(1200px, 270dvh-236px)，超宽屏再居中 */
.page{max-width:640px;width:100%;margin:0 auto;padding:0 16px 96px}
@media(min-width:700px){
  .page{max-width:var(--page-max-wide);padding:0 20px 48px}
}
@media(min-width:700px){
  aside.rail{display:flex;position:sticky;top:0;height:100dvh;width:64px;flex-shrink:0;
    flex-direction:column;align-items:center;border-right:1px solid var(--line);
    background:var(--sidebar);padding:20px 6px 14px}
}
@media(min-width:941px){
  .page{padding:0 28px 48px}
  aside.rail{display:none}
  .sidebar{display:flex;position:sticky;top:0;height:100dvh;width:180px;flex-shrink:0;
    flex-direction:column;border-right:1px solid var(--line);background:var(--sidebar);
    padding:24px 12px 14px}
}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink)}
.brand b{font-size:20px;font-weight:700;letter-spacing:.01em}
.brand small{display:block;font-size:11px;color:var(--ink-4);font-weight:400}
.nav-sec{margin-top:14px}
.nav-sec h3{margin:0;padding:14px 10px 4px;font-size:11px;font-weight:400;color:var(--ink-4)}
.side-link{display:flex;height:40px;align-items:center;gap:10px;padding:0 10px;border-radius:8px;
  font-size:14px;font-weight:500;color:var(--ink-3)}
.side-link svg{flex-shrink:0}
.side-link:hover{background:var(--bg-sunk);color:var(--ink)}
.side-link.on{background:rgba(23,107,117,.10);font-weight:600;color:var(--ink)}
.side-link.on svg{color:var(--accent)}
.side-foot{margin-top:auto;padding-top:12px}
.side-foot .gh{display:flex;height:34px;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--line);border-radius:9999px;font-size:12.5px;color:var(--ink-3)}
.side-foot .gh:hover{background:var(--bg-sunk);color:var(--ink)}
.side-foot .theme-btn{width:100%;height:34px;margin-top:8px;border:1px solid var(--line);
  border-radius:9999px;background:transparent;color:var(--ink-3);font-size:12.5px}
.side-foot .theme-btn:hover{background:var(--bg-sunk);color:var(--ink)}
.side-foot .note{margin:10px 4px 0;font-size:11.5px;color:var(--ink-4);text-align:center}
.rail-link{display:flex;width:100%;height:56px;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;border-radius:8px;font-size:11px;font-weight:500;color:var(--ink-3)}
.rail-link:hover{background:var(--bg-sunk);color:var(--ink)}
.rail-link.on{background:rgba(23,107,117,.10);font-weight:600;color:var(--ink)}
[data-theme="dark"] .side-link.on,[data-theme="dark"] .rail-link.on{background:var(--accent-soft)}
.rail-link.on svg{color:var(--accent)}
aside.rail .brand{margin-bottom:12px}

/* 手机顶栏：品牌 + 精选|全部切换 + 外观按钮 */
.topbar{position:sticky;top:0;z-index:30;display:flex;height:var(--bar-h);align-items:center;gap:8px;
  padding:0 8px;background:var(--bg);border-bottom:1px solid var(--line)}
.topbar .brand b{font-size:17px}
.switch{display:flex;margin:0 auto;background:var(--well);border-radius:9999px;padding:2px;box-shadow:inset 0 0 0 1px var(--line-soft)}
.switch a{padding:3px 14px;border-radius:9999px;font-size:13px;font-weight:500;color:var(--ink-3)}
.switch a.on{background:var(--surface);color:var(--ink);font-weight:600;box-shadow:var(--shadow-card)}
.topbar .title{margin:0 auto;font-size:15px;font-weight:600}
.icon-btn{display:inline-flex;width:36px;height:36px;align-items:center;justify-content:center;
  border:0;border-radius:8px;background:transparent;color:var(--ink-3)}
.icon-btn:hover{background:var(--bg-sunk);color:var(--ink)}
.ic-sun,.ic-monitor{display:none}

/* 手机底栏 */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;background:var(--surface);
  border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.tabbar a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;padding:7px 0 6px;
  font-size:11px;font-weight:500;color:var(--ink-3)}
.tabbar a.on{color:var(--accent);font-weight:600}
/* 桌面（≥700px）只留侧栏/窄轨，手机顶栏与底栏隐藏（须放在基础规则之后，否则同优先级后规则覆盖） */
@media(min-width:700px){.topbar,.tabbar{display:none}}

/* ---- 通用组件 ---- */
h1{font-size:24px;font-weight:600;line-height:1.3;margin:22px 0 0}
h2.sec{font-size:19px;font-weight:600;margin:30px 0 12px}
.lede{font-size:13px;color:var(--ink-4);margin:8px 0 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-card)}
.chip{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:9999px;
  border:1px solid var(--line-soft);background:var(--bg-sunk);font-size:12px;color:var(--ink-4)}
button.chip:hover{color:var(--accent);border-color:var(--line-strong)}
.chip.on{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink);font-weight:600}
.tabs{position:relative;display:inline-flex;gap:2px;overflow-x:auto;scrollbar-width:none;
  margin:0;padding:3px;border-radius:9999px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line-soft)}
.tabs::-webkit-scrollbar{display:none}
.tabs .chip{position:relative;z-index:1;flex-shrink:0;height:36px;padding:0 16px;border:0;
  background:transparent;font-size:14px;font-weight:500;color:var(--ink-3);border-radius:9999px}
.tabs .chip:hover{color:var(--ink)}
.tabs .chip.on{color:var(--ink);font-weight:600}
.tabs-thumb{position:absolute;top:3px;bottom:3px;left:3px;width:0;border-radius:9999px;
  background:var(--surface);box-shadow:var(--shadow-thumb),inset 0 0 0 1px var(--line);
  transition:left 300ms cubic-bezier(.25,1,.5,1),width 300ms cubic-bezier(.25,1,.5,1)}
[data-theme="dark"] .tabs-thumb{background:var(--raised)}
[data-theme="dark"] .switch a.on{background:var(--raised)}
/* 筛选行：左侧分类分段控件，右侧搜索框 */
.filter-row{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:16px 0 4px}
.filter-row .tabs{max-width:100%}
.search{position:relative;width:100%}
@media(min-width:700px){.search{width:240px;flex-shrink:0}}
.search svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--ink-4);pointer-events:none}
.search:focus-within svg{color:var(--ink-3)}
.search input{width:100%;height:42px;border:0;border-radius:9999px;background:var(--well);
  padding:0 40px 0 40px;font:inherit;font-size:16px;color:var(--ink);outline:none;
  box-shadow:inset 0 0 0 1px var(--line-soft)}
@media(min-width:640px){.search input{font-size:14px}}
.search input::placeholder{color:var(--ink-4)}
.search input:hover{box-shadow:inset 0 0 0 1px var(--line-strong)}
.search input:focus{background:var(--surface);box-shadow:0 0 0 3px var(--accent-soft),inset 0 0 0 1px var(--accent)}
.search .clear{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  display:none;width:24px;height:24px;align-items:center;justify-content:center;
  border:0;border-radius:9999px;background:transparent;color:var(--ink-4)}
.search .clear:hover{background:var(--bg-sunk);color:var(--ink)}
.search.has-value .clear{display:inline-flex}
.search kbd{position:absolute;right:16px;top:50%;transform:translateY(-50%);display:none;
  font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10.5px;line-height:16px;
  padding:0 6px;border:1px solid var(--line-strong);border-radius:5px;background:var(--surface);color:var(--ink-4)}
@media(min-width:700px){.search:not(.has-value) kbd{display:block}}
/* 只看未读：独立小开关（首个日期行右侧 / 手机单独一行） */
.toggle-chip{display:inline-flex;min-height:28px;align-items:center;gap:6px;padding:0 12px;
  border-radius:9999px;border:1px solid var(--line-soft);background:var(--bg-sunk);
  font-size:12.5px;font-weight:500;color:var(--ink-3)}
.toggle-chip:hover{color:var(--ink);border-color:var(--line-strong)}
.toggle-chip.on{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink);font-weight:600}
.unread-row{display:flex;justify-content:flex-end;margin:10px 0 2px}
@media(min-width:700px){.unread-row{display:none}}
.well{background:var(--well);box-shadow:inset 0 0 0 1px var(--line-soft);border-radius:14px;padding:14px 16px}
.well .kicker{font-size:12px;font-weight:600;color:var(--accent-ink)}
.well p{margin:6px 0 0;font-size:14.5px;line-height:1.75;color:var(--ink-2)}

/* 评分徽章（按档位着色的小药丸，85+ 红、70+ 青、其余安静） */
.score-pill{display:inline-flex;height:20px;align-items:center;gap:6px;white-space:nowrap;
  border-radius:9999px;padding:0 8px;box-shadow:inset 0 0 0 1px var(--ring,rgba(0,0,0,0))}
.score-pill .lbl{font-size:11px;font-weight:500;opacity:.8}
.score-pill .sep{width:1px;height:10px;background:currentColor;opacity:.25}
.score-pill .num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px;font-weight:700;font-variant-numeric:tabular-nums}
.score-pill.t-hot{background:color-mix(in srgb, var(--hot) 10%, transparent);color:var(--hot);--ring:color-mix(in srgb, var(--hot) 25%, transparent)}
.score-pill.t-acc{background:var(--accent-soft);color:var(--accent);--ring:color-mix(in srgb, var(--accent) 20%, transparent)}
.score-pill.t-quiet{color:var(--ink-4);--ring:var(--line-soft)}
@media(max-width:640px){.score-pill .lbl,.score-pill .sep{display:none}}
/* 精选徽章：琥珀底 + 圆点（不是青色） */
.pick{display:inline-flex;height:18px;align-items:center;gap:4px;padding:0 8px;border-radius:9999px;
  background:var(--amber-soft);color:var(--amber-ink);font-size:11px;font-weight:500}
.pick::before{content:"";width:5px;height:5px;border-radius:9999px;background:currentColor}
/* 当日新到徽章：趋势列的“新”样式 */
.new-pill{display:inline-flex;height:18px;align-items:center;padding:0 6px;border-radius:9999px;
  background:var(--accent-soft);color:var(--accent);font-size:10.5px;font-weight:600;line-height:16px}
.rank{font-weight:800;color:var(--rank-rest)}
.rank.r1{color:var(--rank-1)}.rank.r2{color:var(--rank-2)}.rank.r3{color:var(--rank-3)}

/* ---- 动态卡片（桌面白卡、手机行；来源行 + 标题 + 摘要 + 分类） ---- */
.feed-item{padding:14px 0;border-bottom:1px solid var(--line-soft)}
.feed-item:last-child{border-bottom:0}
@media(min-width:700px){
  .feed-item{padding:15px 18px 14px;background:var(--surface);border:1px solid var(--line);
    border-radius:12px;box-shadow:var(--shadow-card);margin-bottom:12px;
    transition:box-shadow 200ms,border-color 200ms,transform 200ms}
  .feed-item:hover{box-shadow:var(--shadow-card-hover);border-color:var(--line-strong);transform:translateY(-1px)}
}
.fi-head{display:flex;min-height:20px;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-4)}
.fi-head .src{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fi-head .pt{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
@media(min-width:700px){.fi-head .pt{display:none}}
.fi-head .sp{margin-left:auto;display:flex;align-items:center;gap:6px;flex-shrink:0}
.star-btn{display:inline-flex;width:44px;height:44px;align-items:center;justify-content:center;
  margin:-11px -12px -11px 0;border:0;border-radius:8px;background:transparent;color:var(--ink-4)}
.star-btn:hover{background:var(--bg-sunk);color:var(--ink-2)}
.star-btn.on{color:var(--accent)}
.star-btn.on svg{fill:currentColor}
@media(min-width:700px){.star-btn{width:26px;height:26px;margin:-1px 0}}
.feed-item h3{margin:6px 0 0;font-size:17px;font-weight:650;line-height:1.5}
@media(min-width:700px){.feed-item h3{line-height:1.55}}
.feed-item.is-read h3 a{color:var(--ink-4)}
.feed-item h3 a:hover{color:var(--accent)}
.feed-item h3 .rank{margin-right:8px;font-size:17px}
.feed-item .sum{margin:4px 0 0;font-size:14.5px;line-height:1.7;color:var(--ink-3);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(min-width:700px){.feed-item .sum{-webkit-line-clamp:3;font-size:15px;line-height:1.75}}
.fi-foot{margin-top:8px;display:flex;gap:8px;align-items:center}
.fi-foot .when{font-size:12.5px;color:var(--ink-4)}
/* 推荐理由：13px、注色，桌面加顶线与间距 */
.fi-reason{margin-top:4px;font-size:13px;line-height:1.65;color:var(--note);
  display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}
@media(min-width:700px){
  .fi-reason{margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft);
    -webkit-line-clamp:unset;line-height:1.75}
}

/* ---- 热点条（右上热色晕染 + 跳动 live 点 + 排名行） ---- */
.hot-card{position:relative;overflow:hidden;margin:16px 0 0;padding:10px 14px 4px;
  background:radial-gradient(120% 90% at 100% 0%,var(--hot-soft),transparent 55%),var(--surface)}
@media(min-width:700px){.hot-card{margin:18px 0 24px;padding:14px 20px 8px}}
.hot-head{display:flex;align-items:center;justify-content:space-between}
.hot-head h2{display:flex;align-items:center;gap:8px;margin:0;font-size:14px;font-weight:600}
.live{position:relative;display:inline-flex;width:8px;height:8px}
.live .ping{position:absolute;width:100%;height:100%;border-radius:9999px;background:var(--hot);opacity:.4;animation:ping 1.6s ease-out infinite}
.live .dot{position:relative;width:8px;height:8px;border-radius:9999px;background:var(--hot)}
@keyframes ping{0%{transform:scale(1);opacity:.4}80%,100%{transform:scale(2.4);opacity:0}}
.hot-more{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;
  font-size:12.5px;color:var(--ink-3);padding:6px 0}
.hot-more:hover{color:var(--accent)}
.hot-list{list-style:none;margin:2px 0 0;padding:0}
.hot-row{display:grid;grid-template-columns:20px minmax(0,1fr) 64px 20px;align-items:center;gap:12px;
  padding:7px 8px;margin:0 -8px;border-radius:10px}
.hot-row:hover{background:rgba(243,243,239,.7)}
[data-theme="dark"] .hot-row:hover{background:rgba(32,42,47,.4)}
.hot-row .rank{font-size:14px;font-weight:700;text-align:center}
.hot-row .rank.r1,.hot-row .rank.r2,.hot-row .rank.r3{font-size:15px;font-weight:900}
.hot-row .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14.5px;font-weight:600}
@media(min-width:700px){.hot-row .t{font-size:14px}}
.hot-row:hover .t{color:var(--accent)}
.hot-heat{white-space:nowrap;font-size:12.5px;color:var(--ink-4);text-align:right}
.hot-heat b{font-size:13.5px;font-weight:600;color:var(--ink-2)}
.hot-trend{display:flex;width:20px;justify-content:center;color:var(--ink-4)}
.hot-trend .up{color:var(--hot)}
.hot-trend .flat{color:var(--ink-4)}
/* 手机只留前 3 名进榜，让动态流尽快出现在首屏 */
@media(max-width:639px){.hot-top5 li:nth-child(n+4){display:none}}

/* ---- 时间线（按天分组，桌面时间轨 + 青点，手机灰色日栏） ---- */
.day-head{position:sticky;top:var(--bar-h);z-index:20;display:flex;height:44px;align-items:center;gap:8px;
  margin:0 -16px;padding:0 16px;background:var(--daybar)}
.day-head .dh-name{font-size:14px;font-weight:700}
.day-head .dh-date{font-size:12.5px;color:var(--ink-4)}
.day-head .dh-wd{font-size:12.5px;color:var(--ink-4)}
.day-head .dh-count{margin-left:auto;font-size:12.5px;color:var(--ink-4)}
.day-head .dh-wd-full{display:none}
.day-head .dh-aside{display:none}
.day-head .dh-toggle{display:inline-flex;width:24px;height:24px;align-items:center;justify-content:center;
  flex-shrink:0;border:0;border-radius:9999px;background:transparent;color:var(--ink-4)}
.day-head .dh-toggle:hover{background:var(--bg-sunk);color:var(--ink)}
.day-head .dh-toggle svg{transition:transform 200ms}
.day-sec.collapsed .day-list{display:none}
.day-sec.collapsed .dh-toggle svg{transform:rotate(-90deg)}
@media(min-width:700px){
  .day-head{position:sticky;top:0;height:44px;margin:0;padding:0;background:var(--bg)}
  .day-head .dh-name{display:none}
  .day-head .dh-date{font-size:18px;font-weight:600;color:var(--ink)}
  .day-head .dh-wd{font-size:13px}
  .day-head .dh-wd-short{display:none}
  .day-head .dh-wd-full{display:inline}
  .day-head .dh-count{margin-left:0}
  .day-head .dh-aside{display:block;margin-left:auto}
}
.day-head:not(.is-today) .dh-date{display:none}
@media(min-width:700px){.day-head:not(.is-today) .dh-date{display:inline;font-size:18px;font-weight:600}}
.day-sec{margin-top:8px}
.day-list{list-style:none;margin:0;padding:0}
.slot{padding:14px 0;border-bottom:1px solid var(--line-soft)}
.slot:last-child{border-bottom:0}
.slot .slot-time,.slot .rail{display:none}
@media(min-width:700px){
  .slot{display:grid;grid-template-columns:64px 22px minmax(0,1fr);padding:0 0 12px;border-bottom:0}
  .slot .slot-time{display:block;padding-top:17px;font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12.5px;font-weight:600;color:var(--ink-3)}
  .slot .rail{display:block;position:relative;width:auto;height:auto;background:transparent;padding:0;border:0}
  .slot .rail::before{content:"";position:absolute;left:10.5px;top:29px;bottom:-41px;width:1px;background:var(--line-strong)}
  .slot:last-child .rail::before{display:none}
  .slot .rail::after{content:"";position:absolute;left:7.5px;top:25.5px;width:7px;height:7px;
    border-radius:9999px;background:var(--accent);box-shadow:0 0 0 4px var(--bg)}
  .slot .feed-item{margin-bottom:0}
}

.foot{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--ink-4)}
.foot p{margin:0 0 8px}
@media(max-width:699px){.foot{padding-bottom:8px}}
.tip{margin-top:20px;padding-top:16px;border-top:1px dashed var(--line);text-align:center}
.tip-title{font-size:13px;color:var(--ink-3)}
.tip-qr{width:150px;height:150px;border-radius:8px;background:#fff}
.tip-label{font-size:13px;color:var(--ink-3)}
.tip-addr{font-size:12px;color:var(--ink-4);word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
/* 回到顶部：圆按钮，滚过一屏多再出现（仅桌面） */
.to-top{position:fixed;right:max(24px,env(safe-area-inset-right));bottom:max(24px,env(safe-area-inset-bottom));
  z-index:30;display:none;width:44px;height:44px;align-items:center;justify-content:center;
  border-radius:9999px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
  box-shadow:var(--shadow-soft);transition:opacity 200ms,transform 200ms;
  opacity:0;transform:translateY(8px);pointer-events:none}
.to-top.show{opacity:1;transform:none;pointer-events:auto}
.to-top:hover{color:var(--ink)}
@media(min-width:700px){.to-top{display:flex}}
/* 列表到底 */
.feed-end{padding:24px 0;text-align:center;font-size:12px;color:var(--ink-4)}
/* 日报名头：大字名 + 双线，照报纸的版头 */
.masthead{margin-top:24px;padding-bottom:16px;border-bottom:3px double var(--line-strong)}
.masthead .mp-brand{display:flex;align-items:center;gap:14px}
.masthead .mp-meta{margin:10px 0 0;font-size:13px;color:var(--ink-4)}
.masthead .mp-issue{display:inline-flex;height:20px;align-items:center;padding:0 8px;border-radius:9999px;
  background:var(--accent-soft);color:var(--accent-ink);font-size:12px;font-weight:600;margin-left:10px}

/* ---- v9：视觉打磨与新页面（颜色 token 不动） ---- */
/* A2：标题手机 2 行截断；摘要桌面上边距 8px；头部行手机 22px/6px */
@media(max-width:699px){.feed-item h3{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
@media(min-width:700px){.feed-item .sum{margin-top:8px}}
@media(max-width:699px){.fi-head{min-height:22px;gap:6px}}
/* A3：手机隐藏 h1，lede 只留“更新于 …” */
@media(max-width:699px){h1{display:none}.lede-rest{display:none}}
/* A4：手机大标题（与 A3 联动） */
.m-title{display:none}
@media(max-width:699px){.m-title{display:block;font-size:30px;font-weight:800;line-height:1.25;margin:18px 0 0}}
/* A4：顶栏/底栏毛玻璃；顶栏滚动阴影 */
.topbar{background:color-mix(in srgb, var(--bg) 85%, transparent);
  backdrop-filter:blur(20px) saturate(1.5);-webkit-backdrop-filter:blur(20px) saturate(1.5)}
.topbar.scrolled{box-shadow:0 1px 0 var(--line),0 6px 16px rgba(28,39,51,.08)}
.tabbar{background:color-mix(in srgb, var(--surface) 90%, transparent);
  backdrop-filter:blur(20px) saturate(1.5);-webkit-backdrop-filter:blur(20px) saturate(1.5)}
.tabbar-in{display:flex;width:100%;max-width:640px;margin:0 auto}
.tabbar-in a{flex:1}
/* A6：圆点悬停放大；卡片按压态 */
.slot .rail::after{transition:transform 300ms}
.slot:hover .rail::after{transform:scale(1.15)}
.feed-item:active{transform:scale(0.995)}
/* A7：手机 chip 44px；热点条“完整榜单”整行 40px */
@media(max-width:699px){.tabs .chip{height:44px}}
.hot-more{display:flex;min-height:40px;align-items:center;justify-content:center;gap:6px;
  border-top:1px solid var(--line-soft);margin-top:6px;padding:0;font-size:13px;color:var(--ink-3)}
/* A8：减弱动效时关掉动画 */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;
    transition-duration:.01ms !important;scroll-behavior:auto !important}
}
/* A10：日报页用更窄的阅读宽度 */
@media(min-width:700px){.page.page-reading{max-width:var(--page-max-reading)}}
/* B1：热点榜头条大卡 / 次卡 / Delta / 方法说明 */
.hot-hero{padding:18px 20px 16px;margin:16px 0 0}
.hot-hero-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.no-tag{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;font-weight:800;
  letter-spacing:.06em;color:var(--rank-1)}
.hot-badge{display:inline-flex;height:18px;align-items:center;padding:0 8px;border-radius:9999px;
  font-size:11px;font-weight:600}
.hot-badge.b-new{background:var(--accent-soft);color:var(--accent)}
.hot-badge.b-hot{background:var(--hot-soft);color:var(--hot)}
.hot-badge.b-up{background:var(--amber-soft);color:var(--amber-ink)}
.hot-hero h2{margin:10px 0 0;font-size:20px;font-weight:700;line-height:1.45}
.hot-hero h2 a:hover{color:var(--accent)}
.hot-hero-meta{margin:6px 0 0;font-size:12.5px;color:var(--ink-4)}
.hot-hero-latest{margin:10px 0 0;font-size:14px;line-height:1.7;color:var(--ink-2)}
.hot-hero-latest .k{font-size:12px;font-weight:600;color:var(--accent-ink);margin-right:6px}
.hot-hero-foot{display:flex;align-items:flex-end;gap:14px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line-soft)}
.hot-big{font-size:13px;color:var(--ink-4)}
.hot-big b{font-size:34px;font-weight:800;line-height:1;color:var(--ink);margin-right:4px;
  font-variant-numeric:tabular-nums}
.hot-hero-foot .score-pill{margin-left:auto}
.hot-sub-grid{display:grid;gap:12px;margin-top:12px}
@media(min-width:700px){.hot-sub-grid{grid-template-columns:1fr 1fr}}
.hot-sub{padding:14px 16px}
.hot-sub-top{display:flex;align-items:center;gap:8px}
.hot-sub h3{margin:8px 0 0;font-size:16px;font-weight:650;line-height:1.5}
.hot-sub h3 a:hover{color:var(--accent)}
.hot-sub-meta{margin:4px 0 0;font-size:12.5px;color:var(--ink-4)}
.hot-sub-foot{display:flex;align-items:center;gap:10px;margin-top:10px}
.hot-num{font-size:12.5px;color:var(--ink-4)}
.hot-num b{font-size:16px;font-weight:700;color:var(--ink-2);font-variant-numeric:tabular-nums}
.hot-delta{font-size:12.5px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.hot-delta.up{color:var(--hot)}
.hot-delta.down{color:var(--ink-4)}
.hot-delta.new{color:var(--accent)}
.hot-delta.flat{color:var(--ink-4)}
.hot-row{grid-template-columns:20px minmax(0,1fr) 64px 56px 20px}
.hot-row .hot-delta{text-align:right}
.hot-method{margin-top:20px}
.hot-method p{margin:6px 0 0;font-size:13.5px;line-height:1.75;color:var(--ink-3)}
/* B19：卡片式空状态 */
.empty-state{margin:24px 0;padding:32px 20px;text-align:center;
  background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-card)}
.empty-icon{display:flex;justify-content:center;color:var(--ink-4);margin-bottom:12px}
.empty-title{margin:0;font-size:16px;font-weight:650}
.empty-text{margin:8px 0 0;font-size:13.5px;color:var(--ink-4)}
.empty-actions{display:flex;gap:10px;justify-content:center;margin-top:18px;flex-wrap:wrap}
.empty-btn{display:inline-flex;min-height:36px;align-items:center;padding:0 18px;border-radius:9999px;
  border:1px solid var(--line-strong);background:var(--surface);font-size:13.5px;font-weight:500;color:var(--ink-2)}
.empty-btn:hover{border-color:var(--accent);color:var(--accent)}
.empty-btn.primary{background:var(--accent);border-color:transparent;color:var(--accent-contrast);font-weight:600}
.empty-btn.primary:hover{color:var(--accent-contrast);opacity:.9}
/* B14：手机搜索浮层（触发按钮只在手机显示） */
.search-trigger{display:flex;width:100%;height:42px;align-items:center;gap:10px;padding:0 16px;
  border:0;border-radius:9999px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line-soft);
  font-size:16px;color:var(--ink-4);text-align:left}
@media(min-width:700px){.search-trigger{display:none}}
@media(max-width:699px){.filter-row .search{display:none}}
.search-overlay{position:fixed;inset:0;z-index:60;overflow-y:auto;background:var(--bg);padding:0 16px 48px}
.search-overlay .overlay-bar{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:8px;
  padding:12px 0;background:color-mix(in srgb, var(--bg) 90%, transparent);
  backdrop-filter:blur(20px) saturate(1.5);-webkit-backdrop-filter:blur(20px) saturate(1.5)}
.overlay-search{flex:1}
.overlay-sec{margin-top:20px}
.overlay-sec h3{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--ink-4)}
.overlay-sec .recent-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.overlay-sec .recent-row button{flex:1;text-align:left}
.overlay-sec .recent-clear{font-size:12.5px;color:var(--ink-4);border:0;background:transparent;padding:4px 0}
.overlay-sec .recent-clear:hover{color:var(--accent)}
.overlay-cats{display:flex;flex-wrap:wrap;gap:8px}
.overlay-hot{list-style:none;margin:0;padding:0}
.overlay-hot li{border-bottom:1px solid var(--line-soft)}
.overlay-hot li:last-child{border-bottom:0}
.overlay-hot a{display:flex;gap:10px;align-items:baseline;padding:10px 0;font-size:14.5px;font-weight:600}
.overlay-hot a:hover{color:var(--accent)}
.overlay-hot .rank{flex-shrink:0}
/* B15：三向主题切换（带滑动 thumb） */
.theme-seg{position:relative;display:inline-flex;gap:2px;padding:3px;border-radius:9999px;
  background:var(--well);box-shadow:inset 0 0 0 1px var(--line-soft)}
.theme-seg button{position:relative;z-index:1;display:inline-flex;height:36px;align-items:center;gap:6px;
  padding:0 14px;border:0;border-radius:9999px;background:transparent;font-size:13.5px;font-weight:500;color:var(--ink-3)}
.theme-seg button.on{color:var(--ink);font-weight:600}
.theme-seg-thumb{position:absolute;top:3px;bottom:3px;left:3px;width:0;border-radius:9999px;
  background:var(--surface);box-shadow:var(--shadow-thumb),inset 0 0 0 1px var(--line);
  transition:left 300ms cubic-bezier(.25,1,.5,1),width 300ms cubic-bezier(.25,1,.5,1)}
[data-theme="dark"] .theme-seg-thumb{background:var(--raised)}
/* B4/B6：我的页行 + 红点 */
.more-sec{margin-top:24px}
.more-sec h2{margin:0 0 8px;font-size:15px;font-weight:600}
.more-row{display:flex;align-items:center;gap:12px;width:100%;min-height:52px;padding:10px 14px;
  border:0;border-bottom:1px solid var(--line-soft);background:var(--surface);text-align:left;
  font-size:14.5px;font-weight:500;color:var(--ink)}
.more-row:first-of-type{border-top:1px solid var(--line-soft)}
.more-row:hover{background:var(--bg-sunk)}
.more-row svg{color:var(--ink-4);flex-shrink:0}
.more-row .m-label{flex:1}
.more-row .m-val{font-size:13px;color:var(--ink-4);font-weight:400}
.more-row .m-arrow{color:var(--ink-4)}
.dot-red{position:relative;display:inline-flex;width:8px;height:8px}
.dot-red::after{content:"";width:8px;height:8px;border-radius:9999px;background:var(--hot)}
.more-note{margin:10px 0 0;font-size:13px;line-height:1.7;color:var(--ink-4)}
.more-note a{color:var(--accent);text-decoration:underline}
/* B6/B8：更新日志与存档 */
.cl-item{padding:16px 0;border-bottom:1px solid var(--line-soft)}
.cl-item:last-child{border-bottom:0}
.cl-item h3{margin:0;font-size:16px;font-weight:650}
.cl-item .cl-date{font-size:12.5px;color:var(--ink-4);margin:4px 0 0}
.cl-item ul{margin:8px 0 0;padding-left:18px;font-size:14px;line-height:1.75;color:var(--ink-2)}
.arch-month{margin-top:20px}
.arch-month h3{margin:0 0 8px;font-size:15px;font-weight:600}
.arch-list{list-style:none;margin:0;padding:0}
.arch-list li{border-bottom:1px solid var(--line-soft)}
.arch-list li:last-child{border-bottom:0}
.arch-list a{display:flex;align-items:baseline;gap:10px;padding:10px 0;font-size:14.5px}
.arch-list a:hover{color:var(--accent)}
.arch-list .a-date{font-weight:600}
.arch-list .a-issue{font-size:12.5px;color:var(--ink-4)}
.arch-list .a-count{margin-left:auto;font-size:12.5px;color:var(--ink-4)}
/* 日报上下期导航 */
.daily-nav{display:flex;justify-content:space-between;gap:12px;margin-top:28px;padding-top:16px;border-top:1px solid var(--line)}
.daily-nav a{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:500;color:var(--ink-2)}
.daily-nav a:hover{color:var(--accent)}
.daily-nav .dn-next{margin-left:auto}
/* B3：收藏页下架条目置灰 */
.starred-item.is-gone{opacity:.55}
.starred-item .gone-tag{display:inline-flex;height:18px;align-items:center;padding:0 8px;border-radius:9999px;
  background:var(--bg-muted);color:var(--ink-4);font-size:11px;font-weight:500}
.starred-item .starred-at{font-size:12.5px;color:var(--ink-4)}
/* B13：同事件归组展开 */
.slot .group-toggle{grid-column:3;justify-self:start;margin:2px 0 0;padding:4px 10px;border:1px solid var(--line-soft);
  border-radius:9999px;background:var(--bg-sunk);font-size:12.5px;color:var(--ink-3)}
.slot .group-toggle:hover{color:var(--accent);border-color:var(--line-strong)}
.slot .group-extra{grid-column:3;margin-top:8px}
.group-extra-list{list-style:none;margin:0;padding:0}
.group-extra-list li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-top:1px solid var(--line-soft)}
.group-extra-list li:first-child{border-top:0}
.group-extra-list a{font-size:13.5px;font-weight:500}
.group-extra-list a:hover{color:var(--accent)}
.group-extra-list .ge-time{margin-left:auto;font-size:12px;color:var(--ink-4);white-space:nowrap}
/* B16：卡片标签 */
.fi-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.fi-tags .tag{font-size:12px;color:var(--accent-ink);background:var(--accent-softer);
  border-radius:9999px;padding:2px 8px}
.fi-tags .tag:hover{background:var(--accent-soft)}
/* 详情页（items/:id）：阅读宽度 + 大标题 + 导读 + 操作行 */
.item-back{display:inline-flex;align-items:center;gap:6px;margin:4px 0 14px;padding:6px 12px 6px 8px;
  border:1px solid var(--line-soft);border-radius:9999px;background:var(--surface);
  font-size:13.5px;color:var(--ink-3)}
.item-back:hover{color:var(--accent);border-color:var(--line-strong)}
.item-meta{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px;color:var(--ink-4);margin-bottom:12px}
.item-meta .src{font-weight:600;color:var(--ink-2)}
.item-h1{font-size:32px;line-height:1.35;margin:0 0 10px}
.item-en-title{font-size:15px;line-height:1.6;color:var(--ink-4);margin:0 0 16px}
.item-guide{margin:16px 0}
.item-guide p{font-size:18px;line-height:1.7;margin:0}
.item-guide .guide-note{font-size:13px;color:var(--ink-4);margin-bottom:6px}
.item-actions{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.item-open-btn{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 20px;
  border-radius:9999px;background:var(--accent);color:#fff;font-size:15px;font-weight:600}
.item-open-btn:hover{background:var(--accent-strong)}
.item-open-btn:visited,.item-open-btn:active{color:#fff}
.item-ghost-btn{display:inline-flex;align-items:center;gap:6px;min-height:44px;padding:0 16px;
  border-radius:9999px;border:1px solid var(--line-strong);background:var(--surface);
  font-size:14px;color:var(--ink-2)}
.item-ghost-btn:hover{border-color:var(--accent);color:var(--accent)}
.item-ghost-btn.on{color:var(--accent);border-color:var(--accent)}
.item-source-line{font-size:13.5px;color:var(--ink-4);margin:6px 0 16px}
.item-source-line b{color:var(--ink-2)}
.item-sec{margin-top:28px}
.item-sec h2{font-size:17px;margin:0 0 10px}
.item-related-list{display:flex;flex-direction:column;gap:10px}
.item-related-list a{font-size:14.5px;font-weight:500;line-height:1.5}
.item-related-list a:hover{color:var(--accent)}
.item-related-list .rel-meta{font-size:12.5px;color:var(--ink-4)}
@media(max-width:699px){.item-h1{font-size:26px}}
/* B2：加载更多 */
.load-more-wrap{text-align:center;padding:16px 0 4px}
.load-more-btn{display:inline-flex;min-height:40px;align-items:center;gap:8px;padding:0 22px;
  border-radius:9999px;border:1px solid var(--line-strong);background:var(--surface);
  font-size:14px;font-weight:500;color:var(--ink-2)}
.load-more-btn:hover{border-color:var(--accent);color:var(--accent)}
.load-more-btn:disabled{opacity:.6;cursor:default}
.load-more-btn .spin{width:14px;height:14px;border:2px solid var(--line-strong);border-top-color:var(--accent);
  border-radius:9999px;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.load-more-state{margin:10px 0 0;font-size:12.5px;color:var(--ink-4)}
/* B2/B17：搜索结果区 */
.search-results-head{display:flex;align-items:baseline;gap:10px;margin:16px 0 4px}
.search-results-head h2{margin:0;font-size:17px;font-weight:600}
.search-results-head .sr-count{font-size:13px;color:var(--ink-4)}
/* C3：页脚链接行 */
.foot-links{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:4px;font-size:13px}
.foot-links a{color:var(--ink-3)}
.foot-links a:hover{color:var(--accent)}
/* 官方标记：T1 信源卡片头部的小标签 */
.off-tag{display:inline-flex;height:18px;align-items:center;padding:0 6px;border-radius:9999px;
  background:var(--accent-soft);color:var(--accent);font-size:11px;font-weight:600}
"""


def esc(s):
    return html.escape(s or "", quote=True)


WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
WEEKDAYS_SHORT = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 线性描边小图标（feather 风格；不引入任何外部资源）
ICONS = {
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>'
            '<line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>'
            '<line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "trend": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
           '<polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>'
           '<line x1="16" y1="17" x2="8" y2="17"/>',
    "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "bookmark": '<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "trend-up": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "trend-down": '<polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/>',
    "minus": '<line x1="5" y1="12" x2="19" y2="12"/>',
    "arrow-up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/>'
               '<line x1="12" y1="17" x2="12" y2="21"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>'
            '<circle cx="12" cy="7" r="4"/>',
    "rss": '<path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/>'
           '<circle cx="5" cy="19" r="1"/>',
    "message": '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/>'
                '<line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "sun": '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/>'
           '<line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>'
           '<line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/>'
           '<line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>'
           '<line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
}


def icon(name, size=17):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def score_pill(score):
    """AIHOT Score 的分档药丸：85+ 红、70+ 青、其余安静；手机只留数字。"""
    tier = "t-hot" if score >= 85 else ("t-acc" if score >= 70 else "t-quiet")
    return (f'<span class="score-pill {tier}" title="评分 {score}/100">'
            f'<span class="lbl">评分</span><span class="sep"></span>'
            f'<span class="num">{score}</span></span>')


def _pub_dt(it):
    pub = it.get("_pub")
    if pub is None:  # daily.json 里的条目只有 ISO 字符串，没有 _pub
        pub = datetime.fromisoformat(it["published"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    return pub


def feed_item(it, rank=None, badge=False, base=""):
    """AIHOT FeedItem 的静态版：来源行 + 标题 + 一句话简介 + 分类 + 推荐理由 + 星标。
    标题有中文翻译时优先显示中文；简介是本站自己写的一句话（zh_oneliner），
    不展示源站摘要原文。base 是到站根的目录前缀（'' 或 '../'，日报存档页用）。"""
    pub = _pub_dt(it)
    rank_html = ""
    if rank:
        cls = "r1" if rank == 1 else ("r2" if rank == 2 else ("r3" if rank == 3 else ""))
        rank_html = f'<span class="rank {cls}">{rank}</span>'
    badge_html = '<span class="pick">精选</span>' if badge else ""
    new_html = '<span class="new-pill">新</span>' if it.get("is_new") else ""
    off_html = '<span class="off-tag">官方</span>' if it.get("_tier") == "T1" else ""
    reason_html = (f'<p class="fi-reason">推荐理由：{esc(it["reason"])}</p>'
                   if it.get("reason") else "")
    tags_html = ""
    tags = it.get("tags") or []
    if tags:
        tags_html = ('<div class="fi-tags">' + "".join(
            f'<a class="tag" href="{base}topics/{TAG_SLUGS.get(t, t)}.html">#{esc(t)}</a>'
            for t in tags[:3]) + '</div>')
    title = it.get("zh_title") or it["title"]
    summary = it.get("zh_oneliner") or ""
    en_text = it["title"]
    return (f'<article class="feed-item" data-id="{esc(it["id"])}" data-category="{esc(it["category"])}"'
            f' data-official="{"1" if it.get("_tier") == "T1" else "0"}"'
            f' data-en="{esc(en_text)}">'
            f'<header class="fi-head"><span class="src">{esc(it["source"])}</span>{off_html}'
            f'<time class="pt">· {esc(pub.astimezone(CST).strftime("%H:%M"))}</time>'
            f'{badge_html}{new_html}'
            f'<span class="sp">{score_pill(it["score"])}'
            f'<button type="button" class="star-btn" aria-label="收藏" title="收藏">'
            f'{icon("bookmark", 16)}</button></span></header>'
            f'<h3>{rank_html}<a href="{base}items/{esc(it["id"])}.html">{esc(title)}</a></h3>'
            + (f'<p class="sum">{esc(summary)}</p>' if summary else "")
            + f'<div class="fi-foot"><button type="button" class="chip cat" data-filter="{esc(it["category"])}">'
            f'{esc(it["category_label"])}</button>'
            f'<span class="when">{esc(fmt_cst(pub))}</span></div>'
            + tags_html
            + reason_html
            + "</article>")


def hot_delta_html(it):
    """Delta：对照上一轮热度（prev_hot_score），带 %；首次上榜标“新”。"""
    prev = it.get("prev_hot_score")
    cur = it.get("hot_score")
    if prev is None or cur is None:
        return '<span class="hot-delta new">新</span>'
    if prev <= 0:
        return '<span class="hot-delta flat">–</span>'
    d = (cur - prev) / prev * 100.0
    if abs(d) < 0.5:
        return '<span class="hot-delta flat">–</span>'
    cls = "up" if d > 0 else "down"
    arrow = "▲" if d > 0 else "▼"
    return f'<span class="hot-delta {cls}">{arrow} {abs(d):.0f}%</span>'


def hot_badges(it):
    """头条/次卡徽章：热度 ≥90 为“爆”，首次上榜为“新”，名次上升为“发酵中”。"""
    out = []
    if it.get("trend") == "new":
        out.append('<span class="hot-badge b-new">新</span>')
    if (it.get("hot_score") or 0) >= 90:
        out.append('<span class="hot-badge b-hot">爆</span>')
    if it.get("trend") == "up":
        out.append('<span class="hot-badge b-up">发酵中</span>')
    return "".join(out[:2])


def hot_row(it, rank, pad=False):
    """AIHOT HotTopics 的排名行：彩色名次 + 标题 + 热度分 + Delta + 趋势（升/降/新/持平）。"""
    cls = "r1" if rank == 1 else ("r2" if rank == 2 else ("r3" if rank == 3 else ""))
    num = f"{rank:02d}" if pad else str(rank)
    trend = it.get("trend", "same")
    if trend == "up":
        trend_html = f'<span class="hot-trend"><span class="up" title="热度上升">{icon("trend-up", 14)}</span></span>'
    elif trend == "down":
        trend_html = f'<span class="hot-trend"><span title="热度回落">{icon("trend-down", 14)}</span></span>'
    elif trend == "new":
        trend_html = '<span class="hot-trend"><span class="new-pill">新</span></span>'
    else:
        trend_html = f'<span class="hot-trend"><span class="flat" title="热度持平">{icon("minus", 14)}</span></span>'
    return (f'<li><a class="hot-row" href="items/{esc(it["id"])}.html">'
            f'<span class="rank {cls}">{num}</span>'
            f'<span class="t">{esc(it.get("zh_title") or it["title"])}</span>'
            f'<span class="hot-heat"><b class="num">{round(it["hot_score"])}</b> 热度</span>'
            f'{hot_delta_html(it)}'
            f'{trend_html}'
            f'</a></li>')

THEME_BOOT = """<script>(function(){try{var k='indiehot-theme';function sys(){return window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}function apply(t){document.documentElement.setAttribute('data-theme',t==='dark'?'dark':'light');}var saved=localStorage.getItem(k);if(saved==='dark'||saved==='light'){apply(saved);}else{apply(sys());var mq=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)');if(mq&&mq.addEventListener){mq.addEventListener('change',function(){try{if(!localStorage.getItem(k))apply(sys());}catch(e){}});}}}catch(e){}})();</script>"""

SCRIPT = """
<script>
(function(){
var LS_READ='indiehot-read',LS_STAR='indiehot-starred',LS_STAR_META='indiehot-starred-meta',
    LS_THEME='indiehot-theme',LS_RECENT='indiehot-recent-search',LS_CL_READ='indiehot-changelog-read';
function loadSet(k){try{return new Set(JSON.parse(localStorage.getItem(k)||'[]'));}catch(e){return new Set();}}
function saveSet(k,s){try{localStorage.setItem(k,JSON.stringify(Array.from(s)));}catch(e){}}
function loadObj(k){try{return JSON.parse(localStorage.getItem(k)||'{}')||{};}catch(e){return {};}}
function saveObj(k,o){try{localStorage.setItem(k,JSON.stringify(o));}catch(e){}}
var readSet=loadSet(LS_READ),starSet=loadSet(LS_STAR),starMeta=loadObj(LS_STAR_META);
function sysTheme(){return window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
function currentTheme(){var s=null;try{s=localStorage.getItem(LS_THEME);}catch(e){}return (s==='dark'||s==='light')?s:'system';}
function applyTheme(mode){
  var t=mode==='system'?sysTheme():mode;
  var done=function(){
    document.documentElement.setAttribute('data-theme',t);
    try{
      if(mode==='system'){localStorage.removeItem(LS_THEME);}
      else{localStorage.setItem(LS_THEME,mode);}
    }catch(e){}
    syncThemeUI();
  };
  if(document.startViewTransition){try{document.startViewTransition(done);return;}catch(e){}}
  done();
}
function syncThemeUI(){
  var mode=currentTheme();
  document.querySelectorAll('.theme-cycle').forEach(function(b){
    b.setAttribute('data-mode',mode);
    var ic=b.querySelector('.ic-moon'),is=b.querySelector('.ic-sun'),im=b.querySelector('.ic-monitor');
    if(ic)ic.style.display=mode==='dark'?'inline-flex':'none';
    if(is)is.style.display=mode==='light'?'inline-flex':'none';
    if(im)im.style.display=mode==='system'?'inline-flex':'none';
    b.setAttribute('aria-label','外观：'+(mode==='dark'?'深色':mode==='light'?'浅色':'跟随系统')+'（点击切换）');
  });
  document.querySelectorAll('.theme-seg button').forEach(function(b){
    b.classList.toggle('on',b.getAttribute('data-theme-opt')===mode);
  });
  moveThemeThumb();
}
document.querySelectorAll('.theme-btn').forEach(function(b){b.addEventListener('click',function(){
  var order=['system','dark','light'],cur=currentTheme();
  applyTheme(order[(order.indexOf(cur)+1)%order.length]);
});});
document.querySelectorAll('.theme-seg button').forEach(function(b){b.addEventListener('click',function(){
  applyTheme(b.getAttribute('data-theme-opt'));
});});
/* 三向切换的滑动 thumb：量一下当前选中项的位置 */
var themeSegEl=document.querySelector('.theme-seg');
var themeThumb=document.getElementById('themeSegThumb');
function moveThemeThumb(){
  if(!themeSegEl||!themeThumb)return;
  var on=themeSegEl.querySelector('button.on');
  if(!on){themeThumb.style.width='0px';return;}
  themeThumb.style.left=(on.offsetLeft)+'px';
  themeThumb.style.width=(on.offsetWidth)+'px';
}
window.addEventListener('resize',moveThemeThumb);
window.addEventListener('load',moveThemeThumb);
syncThemeUI();
/* 已读：点开标题即记已读，标题变灰；星标：本地收藏，可再筛出来。
   bindFeedItem 供动态加载的卡片复用（B2 分页/搜索结果）。 */
function bindFeedItem(el){
  if(el.getAttribute('data-bound'))return;
  el.setAttribute('data-bound','1');
  var id=el.getAttribute('data-id');
  if(!id)return;
  if(readSet.has(id))el.classList.add('is-read');
  var star=el.querySelector('.star-btn');
  if(star){
    if(starSet.has(id)){star.classList.add('on');star.setAttribute('aria-label','取消收藏');star.title='取消收藏';}
    star.addEventListener('click',function(){
      if(starSet.has(id)){
        starSet.delete(id);delete starMeta[id];
        star.classList.remove('on');star.setAttribute('aria-label','收藏');star.title='收藏';
      }else{
        starSet.add(id);
        /* 收藏快照：标题/链接/来源/时间，供收藏页离线展示与下架置灰用 */
        var a=el.querySelector('h3 a'),src=el.querySelector('.fi-head .src');
        starMeta[id]={title:a?a.textContent.trim():'',link:a?a.getAttribute('href'):'',
          source:src?src.textContent.trim():'',at:new Date().toISOString()};
        star.classList.add('on');star.setAttribute('aria-label','取消收藏');star.title='取消收藏';
      }
      saveSet(LS_STAR,starSet);saveObj(LS_STAR_META,starMeta);applyFilters();
    });
  }
  var a=el.querySelector('h3 a');
  if(a)a.addEventListener('click',function(){
    if(!readSet.has(id)){readSet.add(id);saveSet(LS_READ,readSet);el.classList.add('is-read');applyFilters();}
  });
}
document.querySelectorAll('.feed-item').forEach(bindFeedItem);
/* 筛选状态：分类 + 只看收藏 + 只看未读 + 搜索词，四者叠加 */
var state={cat:'all',star:false,unread:false,q:''};
function itemVisible(el){
  var id=el.getAttribute('data-id')||'';
  if(state.cat==='official'&&el.getAttribute('data-official')!=='1')return false;
  if(state.cat!=='all'&&state.cat!=='starred'&&state.cat!=='official'&&el.getAttribute('data-category')!==state.cat)return false;
  if(state.cat==='starred'&&!starSet.has(id))return false;
  if(state.star&&!starSet.has(id))return false;
  if(state.unread&&readSet.has(id))return false;
  if(state.q){
    var t=((el.textContent||'')+' '+(el.getAttribute('data-en')||'')).toLowerCase();
    if(t.indexOf(state.q)===-1)return false;
  }
  return true;
}
function applyFilters(){
  /* 有搜索词时进入全量搜索模式（B2/B17）：对 data/days/ 30 天分块检索并排序，
     隐藏正常的精选/热点/时间线，显示搜索结果区。 */
  if(state.q){enterSearchMode();return;}
  exitSearchMode();
  var hot=document.getElementById('hot');
  if(hot)hot.style.display=(state.cat==='all')?'':'none';
  var shown=0;
  document.querySelectorAll('#featured .feed-item').forEach(function(el){
    var ok=itemVisible(el);el.style.display=ok?'':'none';if(ok)shown++;
  });
  document.querySelectorAll('#timeline .slot').forEach(function(li){
    var card=li.querySelector('.feed-item');
    var ok=card?itemVisible(card):true;
    li.style.display=ok?'':'none';if(ok)shown++;
  });
  document.querySelectorAll('#timeline .day-sec').forEach(function(sec){
    var vis=sec.querySelectorAll('.slot:not([style*="none"])').length;
    sec.style.display=vis?'':'none';
  });
  var nr=document.getElementById('noResult');
  if(nr){
    nr.hidden=!!shown;nr.style.display='';
    var t=document.getElementById('noResultTitle'),x=document.getElementById('noResultText');
    if(state.cat==='starred'||state.star){
      if(t)t.textContent='还没有收藏';
      if(x)x.textContent='去点卡片右上角的书签，喜欢的动态就会出现在这里。';
    }else if(state.unread){
      if(t)t.textContent='这批都读过了';
      if(x)x.textContent='关掉「只看未读」可以看到全部动态。';
    }else if(state.q){
      if(t)t.textContent='没有匹配「'+state.q+'」的动态。';
      if(x)x.textContent='换个关键词试试，或者清除搜索看看全部。';
    }else{
      if(t)t.textContent='这个分类下暂无动态。';
      if(x)x.textContent='换个分类看看，或者回到全部动态。';
    }
  }
  var sc=document.getElementById('starCount');
  if(sc)sc.textContent=starSet.size?('（'+starSet.size+'）'):'';
  if(typeof updateStarCounts==='function')updateStarCounts();
}
var filterTimer=null;
function scheduleFilter(){
  if(filterTimer)clearTimeout(filterTimer);
  filterTimer=setTimeout(applyFilters,150);
}
function setCat(f){
  state.cat=f;state.star=(f==='starred');
  document.querySelectorAll('.tabs .chip').forEach(function(x){
    x.classList.toggle('on',x.getAttribute('data-filter')===f);
  });
  document.querySelectorAll('[data-filter="starred"]').forEach(function(x){
    x.classList.toggle('on',f==='starred');
  });
  moveThumb();applyFilters();
}
/* 分类 chips：用事件委托，动态加载的卡片里的分类 chip 也能点（B2） */
document.addEventListener('click',function(e){
  var b=e.target.closest('[data-filter]');
  if(!b)return;
  if(b.closest('#searchOverlay'))return; /* 浮层里的 chips 有自己的处理 */
  setCat(b.getAttribute('data-filter'));
});
/* 分段控件的白色滑块：量一下当前选中项的位置，滑动过去 */
var tabsEl=document.querySelector('.tabs');
var thumb=document.getElementById('tabsThumb');
function moveThumb(){
  if(!tabsEl||!thumb)return;
  var on=tabsEl.querySelector('.chip.on');
  if(!on){thumb.style.width='0px';return;}
  thumb.style.left=(on.offsetLeft)+'px';
  thumb.style.width=(on.offsetWidth)+'px';
}
window.addEventListener('resize',moveThumb);
window.addEventListener('load',moveThumb);
moveThumb();
/* 搜索：标题+摘要+来源即时过滤；“/” 聚焦；× 清空 */
var searchInput=document.getElementById('siteSearch');
if(searchInput){
  var box=searchInput.closest('.search');
  searchInput.addEventListener('input',function(){
    state.q=searchInput.value.trim().toLowerCase();
    if(box)box.classList.toggle('has-value',!!searchInput.value);
    scheduleFilter();
  });
  var clr=document.getElementById('searchClear');
  if(clr)clr.addEventListener('click',function(){searchInput.value='';state.q='';if(box)box.classList.remove('has-value');searchInput.focus();applyFilters();});
  document.addEventListener('keydown',function(e){
    if(e.key==='/'&&!(e.target instanceof HTMLInputElement||e.target instanceof HTMLTextAreaElement)){
      e.preventDefault();searchInput.focus();
    }
  });
}
/* 只看未读（桌面在首个日期行右侧，手机在单独一行） */
document.querySelectorAll('.unread-toggle').forEach(function(b){
  b.addEventListener('click',function(){
    state.unread=!state.unread;
    document.querySelectorAll('.unread-toggle').forEach(function(x){x.classList.toggle('on',state.unread);});
    applyFilters();
  });
});
/* 日期折叠：事件委托，动态加载的日期区也能折（B2） */
document.addEventListener('click',function(e){
  var b=e.target.closest('.dh-toggle');
  if(!b)return;
  var sec=b.closest('.day-sec');
  if(sec)sec.classList.toggle('collapsed');
});
/* 回到顶部 */
var toTop=document.getElementById('toTop');
if(toTop){
  toTop.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
  window.addEventListener('scroll',function(){
    toTop.classList.toggle('show',window.scrollY>window.innerHeight*1.2);
  },{passive:true});
}
/* #starred 直达收藏视图（加载时与 hash 变化时都生效） */
if(location.hash==='#starred'){setCat('starred');}
window.addEventListener('hashchange',function(){
  if(location.hash==='#starred'){setCat('starred');}
});
/* 顶栏滚动阴影（A4）：滚出一点就加阴影 */
var topbar=document.querySelector('.topbar');
function syncTopbar(){if(topbar)topbar.classList.toggle('scrolled',window.scrollY>8);}
window.addEventListener('scroll',syncTopbar,{passive:true});syncTopbar();
/* 底栏点当前 tab：回顶；已在顶部则刷新（A5） */
document.querySelectorAll('.tabbar a').forEach(function(a){
  if(!a.classList.contains('on'))return;
  a.addEventListener('click',function(e){
    e.preventDefault();
    if(window.scrollY<10){location.reload();}
    else{window.scrollTo({top:0,behavior:'smooth'});}
  });
});
/* 收藏数量：侧栏/我的页/底栏都显示（B3/B4） */
function updateStarCounts(){
  document.querySelectorAll('[data-star-count]').forEach(function(el){el.textContent=starSet.size?('（'+starSet.size+'）'):(el.getAttribute('data-star-count-empty')||'');});
}
updateStarCounts();
/* 空状态操作按钮（B19）：清除筛选 / 去看精选（事件委托，搜索结果里的空状态也生效） */
document.addEventListener('click',function(e){
  var b=e.target.closest('[data-empty-action]');
  if(!b)return;
  var act=b.getAttribute('data-empty-action');
  state.q='';state.unread=false;
  var si=document.getElementById('siteSearch');
  if(si){si.value='';var bx=si.closest('.search');if(bx)bx.classList.remove('has-value');}
  var oi=document.getElementById('overlaySearch');
  if(oi)oi.value='';
  document.querySelectorAll('.unread-toggle').forEach(function(x){x.classList.remove('on');});
  setCat('all');
  if(act==='featured'){
    var f=document.getElementById('featured');
    if(f){f.scrollIntoView();}
    else{var br=document.querySelector('.topbar .brand')||document.querySelector('.sidebar .brand');if(br)location.href=br.getAttribute('href');}
  }
});
/* 手机搜索浮层（B14）：全屏层 + 最近搜索 + 分类 chips + 热议 */
var overlay=document.getElementById('searchOverlay');
var overlayInput=document.getElementById('overlaySearch');
function isMobile(){return window.matchMedia&&window.matchMedia('(max-width: 699px)').matches;}
function openOverlay(){
  if(!overlay)return;
  if(!isMobile()){var si=document.getElementById('siteSearch');if(si)si.focus();return;}
  overlay.hidden=false;document.body.style.overflow='hidden';
  renderRecent();
  if(overlayInput)setTimeout(function(){overlayInput.focus();},50);
}
function closeOverlay(){
  if(!overlay)return;
  overlay.hidden=true;document.body.style.overflow='';
}
document.querySelectorAll('.search-trigger').forEach(function(b){b.addEventListener('click',openOverlay);});
var ovClose=document.getElementById('overlayClose');
if(ovClose)ovClose.addEventListener('click',closeOverlay);
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&overlay&&!overlay.hidden)closeOverlay();});
if(overlayInput){
  overlayInput.addEventListener('input',function(){
    state.q=overlayInput.value.trim().toLowerCase();
    var si=document.getElementById('siteSearch');if(si)si.value=overlayInput.value;
    scheduleFilter();
  });
  overlayInput.addEventListener('keydown',function(e){
    if(e.key==='Enter'){
      var q=overlayInput.value.trim();
      if(q)saveRecent(q);
      closeOverlay();
      var tl=document.getElementById('timeline');if(tl)tl.scrollIntoView();
    }
  });
}
function loadRecent(){try{return JSON.parse(localStorage.getItem(LS_RECENT)||'[]')||[];}catch(e){return [];}}
function saveRecent(q){
  var list=loadRecent().filter(function(x){return x!==q;});
  list.unshift(q);list=list.slice(0,8);
  try{localStorage.setItem(LS_RECENT,JSON.stringify(list));}catch(e){}
}
function renderRecent(){
  var box=document.getElementById('recentList');if(!box)return;
  var list=loadRecent();
  box.innerHTML=list.length?list.map(function(q){
    return '<div class="recent-row"><button type="button" class="chip" data-recent="'+q.replace(/"/g,'&quot;')+'">'+q.replace(/</g,'&lt;')+'</button></div>';
  }).join(''):'<p class="more-note">还没有搜索过。</p>';
  box.querySelectorAll('[data-recent]').forEach(function(b){
    b.addEventListener('click',function(){
      var q=b.getAttribute('data-recent');
      if(overlayInput)overlayInput.value=q;
      state.q=q.toLowerCase();
      var si=document.getElementById('siteSearch');if(si)si.value=q;
      applyFilters();closeOverlay();
      var tl=document.getElementById('timeline');if(tl)tl.scrollIntoView();
    });
  });
}
var clrRecent=document.getElementById('clearRecent');
if(clrRecent)clrRecent.addEventListener('click',function(){
  try{localStorage.removeItem(LS_RECENT);}catch(e){}renderRecent();
});
document.querySelectorAll('#searchOverlay [data-filter]').forEach(function(b){
  b.addEventListener('click',function(){setCat(b.getAttribute('data-filter'));closeOverlay();});
});
/* 我的页：收藏数量 + 更新日志红点（B4/B6） */
(function(){
  var dot=document.getElementById('changelogDot');
  if(dot){
    var latest=dot.getAttribute('data-changelog-version')||'';
    var read='';
    try{read=localStorage.getItem(LS_CL_READ)||'';}catch(e){}
    dot.hidden=!!(read&&read===latest);
  }
  var mc=document.getElementById('moreStarCount');
  if(mc)mc.textContent=starSet.size?String(starSet.size):'0';
})();
/* 更新日志页：打开即记为已读（B6） */
(function(){
  var pg=document.getElementById('changelogPage');
  if(pg){
    var v=pg.getAttribute('data-changelog-version')||'';
    if(v){try{localStorage.setItem(LS_CL_READ,v);}catch(e){}}
  }
})();
/* ============ 客户端数据层：按天分块（data/days/） ============ */
/* 首页首屏由服务端渲染；浏览器只在需要时才按天取分块：分页一次取一天，
   搜索与收藏页在用户操作时才并行取全部天（仍按天分块，不再拉 30 天单文件）。 */
var dayIndexPromise=null;
var dayCache={};
function getDayIndex(){
  if(dayIndexPromise)return dayIndexPromise;
  dayIndexPromise=fetch('data/days/index.json').then(function(r){
    if(!r.ok)throw new Error('HTTP '+r.status);return r.json();
  }).then(function(d){
    return (d.days||[]).map(function(x){return x.date;});
  }).catch(function(e){dayIndexPromise=null;throw e;});
  return dayIndexPromise;
}
function getDay(date){
  if(dayCache[date])return dayCache[date];
  dayCache[date]=fetch('data/days/'+date+'.json').then(function(r){
    if(!r.ok)throw new Error('HTTP '+r.status);return r.json();
  }).then(function(d){
    return (d.items||[]).sort(function(a,b){return new Date(b.published)-new Date(a.published);});
  }).catch(function(e){delete dayCache[date];throw e;});
  return dayCache[date];
}
/* 全量：只在搜索 / 收藏页按需调用（并行取各天分块并缓存），首页加载时不调用 */
function getStore(){
  return getDayIndex().then(function(days){
    return Promise.all(days.map(getDay));
  }).then(function(lists){
    var all=[];
    lists.forEach(function(l){all=all.concat(l);});
    return all.sort(function(a,b){return new Date(b.published)-new Date(a.published);});
  });
}
/* 收藏页：从本地快照渲染，查 data/days/ 分块判断是否下架（B3） */
(function(){
  var list=document.getElementById('starList');
  if(!list)return;
  var empty=document.getElementById('starEmpty');
  var ids=Array.from(starSet);
  if(!ids.length){if(empty)empty.hidden=false;return;}
  getStore().then(function(items){
    var live={};
    items.forEach(function(it){live[it.id]=it;});
    var html=ids.map(function(id){
      var m=starMeta[id]||{};
      var it=live[id];
      var gone=!it;
      var title=m.title||(it?(it.zh_title||it.title):'（标题未知）');
      var link=m.link||(it?it.link:'#');
      var source=m.source||(it?it.source:'');
      var at=m.at?new Date(m.at):null;
      var atStr=at?('收藏于 '+at.getFullYear()+'年'+(at.getMonth()+1)+'月'+at.getDate()+'日'):'';
      return '<article class="feed-item starred-item'+(gone?' is-gone':'')+'">'
        +'<header class="fi-head"><span class="src">'+source.replace(/</g,'&lt;')+'</span>'
        +(gone?'<span class="gone-tag">已下架</span>':'')
        +'<span class="sp"><button type="button" class="star-btn on" data-unstar="'+id+'" aria-label="取消收藏" title="取消收藏">'
        +'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>'
        +'</button></span></header>'
        +'<h3><a href="items/'+id+'.html">'+title.replace(/</g,'&lt;')+'</a></h3>'
        +(gone?'<p class="sum">这条动态已不在本站 30 天数据里（可能已下架或超出保存期），只保留了你收藏时的标题与链接。</p>':'')
        +(atStr?'<p class="starred-at">'+atStr+'</p>':'')
        +'</article>';
    }).join('');
    list.innerHTML=html;
    list.querySelectorAll('[data-unstar]').forEach(function(b){
      b.addEventListener('click',function(){
        var id=b.getAttribute('data-unstar');
        starSet.delete(id);delete starMeta[id];
        saveSet(LS_STAR,starSet);saveObj(LS_STAR_META,starMeta);
        updateStarCounts();
        b.closest('.starred-item').remove();
        if(!list.querySelector('.starred-item')&&empty)empty.hidden=false;
      });
    });
  }).catch(function(){
    if(empty)empty.hidden=false;
  });
})();
/* ============ B2 分页 + B17 全量搜索 + B18 滚动恢复 ============ */
function escHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
var SEARCH_ICON_SVG='<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
var BOOKMARK_SVG='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>';
var CHEV_SVG='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
var IH_CFG=null;
try{var cfgEl=document.getElementById('ihConfig');if(cfgEl)IH_CFG=JSON.parse(cfgEl.textContent);}catch(e){IH_CFG=null;}
/* getStore() 见上方的客户端数据层（按天分块取 data/days/）。 */
/* 北京时间的年月日时分（用 UTC+8 换算，不依赖浏览器时区） */
function cstParts(date){
  var c=new Date(date.getTime()+8*3600000);
  var p=function(n){return String(n).padStart(2,'0');};
  return {y:c.getUTCFullYear(),m:c.getUTCMonth()+1,d:c.getUTCDate(),
    hh:c.getUTCHours(),mm:c.getUTCMinutes(),wd:c.getUTCDay(),
    iso:c.getUTCFullYear()+'-'+p(c.getUTCMonth()+1)+'-'+p(c.getUTCDate()),
    hm:p(c.getUTCHours())+':'+p(c.getUTCMinutes())};
}
function todayCST(){return cstParts(new Date()).iso;}
function fmtCST(iso){
  var p=cstParts(new Date(iso));
  return p.m+'月'+p.d+'日 '+p.hm;
}
/* 与 Python score()/reason_for() 同规则的客户端版（动态卡片用） */
function computeMeta(it){
  var tier=it.tier||(IH_CFG?IH_CFG.tiers[it.source_id]:null)||'T2';
  var labels=it.tags||[];
  var score=0,reason='';
  if(IH_CFG){
    var pub=new Date(it.published);
    var ageH=(Date.now()-pub.getTime())/3600000;
    score=IH_CFG.base;
    for(var i=0;i<IH_CFG.recency.length;i++){
      var r=IH_CFG.recency[i];
      if(ageH<=r.max_hours){score+=r.bonus;break;}
    }
    score+=((IH_CFG.tierBonus||{})[tier]||0);
    var text=((it.title||'')+' '+(it.zh_oneliner||'')).toLowerCase();
    var devHits=0;
    Object.keys(IH_CFG.devLabels||{}).forEach(function(kw){
      if(text.indexOf(kw.toLowerCase())>-1)devHits++;
    });
    score+=Math.min(devHits*IH_CFG.devBonus,IH_CFG.devCap);
    var noiseHits=0;
    (IH_CFG.noiseKw||[]).forEach(function(kw){
      if(text.indexOf(kw.toLowerCase())>-1)noiseHits++;
    });
    score+=Math.max(noiseHits*IH_CFG.noisePenalty,IH_CFG.noiseFloor);
    score=Math.max(0,Math.min(100,score));
    if(!labels.length){
      var seen={};
      Object.keys(IH_CFG.devLabels||{}).forEach(function(kw){
        if(text.indexOf(kw.toLowerCase())>-1){
          var lab=IH_CFG.devLabels[kw];
          if(!seen[lab]){seen[lab]=1;labels.push(lab);}
        }
      });
      labels=labels.slice(0,3);
    }
    var parts=[];
    if(tier==='T1')parts.push('官方来源');
    if(labels.length)parts.push('涉及'+labels.slice(0,2).join('与'));
    if(ageH<=6)parts.push('6 小时内发布');else if(ageH<=24)parts.push('24 小时内发布');
    if(parts.length<2)parts.push('独立游戏开发相关');
    reason=parts.join(' · ');
  }else{
    score=it.score||0;reason=it.reason||'';
  }
  return {score:score,reason:reason,labels:labels,tier:tier};
}
function cardHTML(it){
  var m=computeMeta(it);
  var title=it.zh_title||it.title;
  var summary=it.zh_oneliner||'';
  var en=it.title;
  var pub=cstParts(new Date(it.published));
  var off=m.tier==='T1'?'<span class="off-tag">官方</span>':'';
  var isNew=false;
  if(it.fetched_at){
    isNew=cstParts(new Date(it.fetched_at)).iso===todayCST()
      &&(Date.now()-new Date(it.published).getTime())<=86400000;
  }
  var newHtml=isNew?'<span class="new-pill">新</span>':'';
  var tagsHtml=m.labels.length?'<div class="fi-tags">'+m.labels.map(function(t){
    return '<a class="tag" href="topics/'+((IH_CFG&&IH_CFG.tagSlugs[t])||t)+'.html">#'+escHtml(t)+'</a>';
  }).join('')+'</div>':'';
  var scoreCls=m.score>=85?'t-hot':(m.score>=70?'t-acc':'t-quiet');
  return '<article class="feed-item" data-id="'+escHtml(it.id)+'" data-category="'+escHtml(it.category||'')+'"'
    +' data-official="'+(m.tier==='T1'?'1':'0')+'" data-en="'+escHtml(en)+'">'
    +'<header class="fi-head"><span class="src">'+escHtml(it.source)+'</span>'+off
    +'<time class="pt">· '+pub.hm+'</time>'+newHtml
    +'<span class="sp"><span class="score-pill '+scoreCls+'" title="评分 '+m.score+'/100">'
    +'<span class="lbl">评分</span><span class="sep"></span><span class="num">'+m.score+'</span></span>'
    +'<button type="button" class="star-btn" aria-label="收藏" title="收藏">'+BOOKMARK_SVG+'</button></span></header>'
    +'<h3><a href="items/'+escHtml(it.id)+'.html">'+escHtml(title)+'</a></h3>'
    +(summary?'<p class="sum">'+escHtml(summary)+'</p>':'')
    +'<div class="fi-foot"><button type="button" class="chip cat" data-filter="'+escHtml(it.category||'')+'">'
    +escHtml(it.category_label||it.category||'')+'</button>'
    +'<span class="when">'+escHtml(fmtCST(it.published))+'</span></div>'
    +tagsHtml
    +'<p class="fi-reason">推荐理由：'+escHtml(m.reason)+'</p>'
    +'</article>';
}
function slotHTML(it){
  var pub=cstParts(new Date(it.published));
  return '<li class="slot" data-category="'+escHtml(it.category||'')+'">'
    +'<time class="slot-time" datetime="'+escHtml(it.published)+'">'+pub.hm+'</time>'
    +'<span class="rail" aria-hidden="true"></span>'
    +cardHTML(it)+'</li>';
}
function normTitle(t){
  return (t||'').toLowerCase().replace(/[\\s\\-_·•|/\\:;,.!?？！。、，；：（）()\\[\\]{}“”"'‘’<>《》]+/g,'');
}
function groupSimilarJS(items){
  var groups=[];
  items.forEach(function(it){
    var norm=normTitle(it.zh_title||it.title);
    var date=cstParts(new Date(it.published)).iso;
    var merged=false;
    if(norm.length>=10){
      for(var i=0;i<groups.length;i++){
        var g=groups[i],m=g.main;
        if(m.source_id!==it.source_id||cstParts(new Date(m.published)).iso!==date)continue;
        var mn=normTitle(m.zh_title||m.title);
        if(mn.length<10)continue;
        if(norm===mn||((norm.indexOf(mn)===0||mn.indexOf(norm)===0)
            &&Math.min(norm.length,mn.length)/Math.max(norm.length,mn.length)>=0.8)){
          g.extra.push(it);merged=true;break;
        }
      }
    }
    if(!merged)groups.push({main:it,extra:[]});
  });
  return groups;
}
function groupSlotHTML(g){
  var slot=slotHTML(g.main);
  if(!g.extra.length)return slot;
  var rows=g.extra.map(function(e){
    var p=cstParts(new Date(e.published));
    return '<li><a href="items/'+escHtml(e.id)+'.html">'+escHtml(e.zh_title||e.title)+'</a>'
      +'<span class="ge-time">'+p.hm+'</span></li>';
  }).join('');
  var toggle='<button type="button" class="group-toggle" aria-expanded="false">另有 '+g.extra.length+' 篇报道 <span aria-hidden="true">▾</span></button>';
  var extra='<div class="group-extra" hidden><ul class="group-extra-list">'+rows+'</ul></div>';
  return slot.slice(0,-5)+toggle+extra+'</li>';
}
function dayHeaderHTML(dateISO,count){
  var parts=dateISO.split('-');
  var wd=new Date(+parts[0],+parts[1]-1,+parts[2]).getDay();
  var isToday=dateISO===todayCST();
  var name=isToday?'今天':(+parts[1]+'月'+(+parts[2])+'日');
  var wdShort=['周日','周一','周二','周三','周四','周五','周六'][wd];
  var wdFull=['星期日','星期一','星期二','星期三','星期四','星期五','星期六'][wd];
  return '<header class="day-head'+(isToday?' is-today':'')+'">'
    +'<span class="dh-name">'+name+'</span>'
    +'<span class="dh-date">'+(+parts[1])+'月'+(+parts[2])+'日</span>'
    +'<button type="button" class="dh-toggle" aria-label="收起/展开这一天">'+CHEV_SVG+'</button>'
    +'<span class="dh-wd dh-wd-short">'+wdShort+'</span>'
    +'<span class="dh-wd dh-wd-full">'+wdFull+'</span>'
    +'<span class="dh-count"><b class="num">'+count+'</b> 条</span></header>';
}
function renderBatch(items){
  var groups=groupSimilarJS(items);
  var days={},order=[];
  groups.forEach(function(g){
    var d=cstParts(new Date(g.main.published)).iso;
    if(!days[d]){days[d]=[];order.push(d);}
    days[d].push(g);
  });
  var tl=document.getElementById('timeline');
  if(!tl)return;
  order.forEach(function(d){
    var html=days[d].map(groupSlotHTML).join('');
    var count=days[d].reduce(function(n,g){return n+1+g.extra.length;},0);
    var sec=tl.querySelector('.day-sec[data-date="'+d+'"]');
    if(sec){
      var ol=sec.querySelector('.day-list');
      if(ol)ol.insertAdjacentHTML('beforeend',html);
      var cnt=sec.querySelector('.dh-count .num');
      if(cnt)cnt.textContent=parseInt(cnt.textContent||'0',10)+count;
    }else{
      tl.insertAdjacentHTML('beforeend',
        '<section class="day-sec" data-date="'+d+'">'+dayHeaderHTML(d,count)
        +'<ol class="day-list">'+html+'</ol></section>');
    }
  });
  tl.querySelectorAll('.feed-item').forEach(bindFeedItem);
  applyFilters();
}
/* 分页状态机：加载态 / 失败重试 / 到底 */
var lmState={loading:false,done:false};
function setLoadMoreState(s){
  var btn=document.getElementById('loadMoreBtn'),st=document.getElementById('loadMoreState'),
      wrap=document.getElementById('loadMoreWrap'),end=document.getElementById('feedEnd');
  if(!btn)return;
  if(s==='loading'){btn.disabled=true;btn.innerHTML='<span class="spin"></span>正在加载';if(st)st.textContent='';}
  else if(s==='error'){btn.disabled=false;btn.textContent='加载失败，点击重试';if(st)st.textContent='网络或数据有问题，点按钮再试一次。';}
  else if(s==='done'){if(wrap)wrap.hidden=true;if(end)end.hidden=false;}
  else{btn.disabled=false;btn.textContent='加载更多';if(st)st.textContent='';}
}
/* 分页：按天取分块，一天不够再取下一天，直到凑满 30 条未见过的卡片 */
var lmDayIdx=0,lmBuffer=[];
function loadMore(){
  if(lmState.loading||lmState.done||state.q)return Promise.resolve(false);
  var tl=document.getElementById('timeline');
  if(!tl)return Promise.resolve(false);
  lmState.loading=true;setLoadMoreState('loading');
  var seen={};
  tl.querySelectorAll('.feed-item').forEach(function(el){seen[el.getAttribute('data-id')]=1;});
  tl.querySelectorAll('.group-extra-list a').forEach(function(a){
    var m=/items\\/([0-9a-f]+)\\.html/.exec(a.getAttribute('href')||'');
    if(m)seen[m[1]]=1;
  });
  return getDayIndex().then(function(days){
    var next=[];
    function fill(){
      while(next.length<30&&lmBuffer.length){
        var it=lmBuffer.shift();
        if(!seen[it.id])next.push(it);
      }
      if(next.length>=30||lmDayIdx>=days.length)return Promise.resolve(next);
      var d=days[lmDayIdx++];
      return getDay(d).then(function(items){lmBuffer=items.slice();return fill();});
    }
    return fill();
  }).then(function(next){
    if(!next.length){lmState.done=true;lmState.loading=false;setLoadMoreState('done');saveScrollState();return false;}
    renderBatch(next);
    lmState.loading=false;
    if(next.length<30){lmState.done=true;setLoadMoreState('done');}
    else setLoadMoreState('idle');
    saveScrollState();
    return true;
  }).catch(function(){
    lmState.loading=false;setLoadMoreState('error');
    return false;
  });
}
var loadBtn=document.getElementById('loadMoreBtn');
if(loadBtn)loadBtn.addEventListener('click',function(){loadMore();});
var sentinel=document.getElementById('loadMoreSentinel');
if(sentinel&&'IntersectionObserver' in window){
  var io=new IntersectionObserver(function(entries){
    entries.forEach(function(en){if(en.isIntersecting)loadMore();});
  },{rootMargin:'900px 0px'});
  io.observe(sentinel);
}
/* 全量搜索模式（B2/B17）：标题 > 摘要 > 来源，同级按时间倒序 */
var searchMode=false;
function enterSearchMode(){
  var sr=document.getElementById('searchResults');
  if(!sr)return;
  searchMode=true;
  ['featured','hot','timeline','loadMoreWrap','loadMoreSentinel','feedEnd','noResult'].forEach(function(id){
    var el=document.getElementById(id);if(el)el.hidden=true;
  });
  sr.hidden=false;
  renderSearchResults();
}
function exitSearchMode(){
  if(!searchMode)return;
  searchMode=false;
  ['featured','hot','timeline','loadMoreSentinel'].forEach(function(id){
    var el=document.getElementById(id);if(el)el.hidden=false;
  });
  var wrap=document.getElementById('loadMoreWrap');if(wrap)wrap.hidden=lmState.done;
  var end=document.getElementById('feedEnd');if(end)end.hidden=!lmState.done;
  var sr=document.getElementById('searchResults');if(sr)sr.hidden=true;
}
function matchFiltersData(it){
  if(state.cat==='official'&&(it.tier||(IH_CFG?IH_CFG.tiers[it.source_id]:'T2'))!=='T1')return false;
  if(state.cat==='starred'&&!starSet.has(it.id))return false;
  if(state.cat!=='all'&&state.cat!=='starred'&&state.cat!=='official'&&it.category!==state.cat)return false;
  if(state.unread&&readSet.has(it.id))return false;
  return true;
}
function renderSearchResults(){
  var srList=document.getElementById('srList'),srCount=document.getElementById('srCount');
  if(!srList)return;
  var q=state.q;
  srList.innerHTML='<p class="more-note">正在搜索…</p>';
  getStore().then(function(items){
    if(state.q!==q)return;
    var scored=[];
    items.forEach(function(it){
      if(!matchFiltersData(it))return;
      var title=((it.zh_title||it.title||'')+'').toLowerCase();
      var sum=((it.zh_oneliner||'')+'').toLowerCase();
      var src=(it.source||'').toLowerCase();
      var tier=0;
      if(title.indexOf(q)>-1)tier=3;
      else if(sum.indexOf(q)>-1)tier=2;
      else if(src.indexOf(q)>-1)tier=1;
      if(tier)scored.push({it:it,tier:tier});
    });
    scored.sort(function(a,b){
      return b.tier-a.tier||(new Date(b.it.published)-new Date(a.it.published));
    });
    if(srCount)srCount.textContent='找到 '+scored.length+' 条';
    if(!scored.length){
      srList.innerHTML='<div class="empty-state"><div class="empty-icon">'+SEARCH_ICON_SVG+'</div>'
        +'<h3 class="empty-title">没有匹配「'+escHtml(state.q)+'」的动态。</h3>'
        +'<p class="empty-text">换个关键词试试，或者清除搜索看看全部。</p>'
        +'<div class="empty-actions"><button type="button" class="empty-btn" data-empty-action="clear">清除筛选</button>'
        +'<button type="button" class="empty-btn primary" data-empty-action="featured">去看精选</button></div></div>';
      return;
    }
    var show=scored.slice(0,50);
    srList.innerHTML=show.map(function(s){return cardHTML(s.it);}).join('')
      +(scored.length>50?'<p class="more-note">只显示前 50 条，共 '+scored.length+' 条。</p>':'');
    srList.querySelectorAll('.feed-item').forEach(bindFeedItem);
  }).catch(function(){
    srList.innerHTML='<div class="empty-state"><h3 class="empty-title">搜索暂时不可用</h3>'
      +'<p class="empty-text">数据没加载出来，刷新页面再试。</p></div>';
  });
}
/* 滚动恢复（B18）：从详情页/热点页返回首页时恢复滚动位置与已加载批次数 */
var SCROLL_KEY='indiehot-scroll';
function saveScrollState(){
  var tl=document.getElementById('timeline');
  if(!tl)return;
  try{sessionStorage.setItem(SCROLL_KEY,JSON.stringify({
    y:window.scrollY,n:tl.querySelectorAll('.feed-item').length,t:Date.now()}));}catch(e){}
}
window.addEventListener('pagehide',saveScrollState);
var scrollSaveTimer=null;
window.addEventListener('scroll',function(){
  if(scrollSaveTimer)return;
  scrollSaveTimer=setTimeout(function(){scrollSaveTimer=null;saveScrollState();},500);
},{passive:true});
function maybeRestoreScroll(){
  var tl=document.getElementById('timeline');
  if(!tl)return;
  var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];
  var isBack=nav&&nav.type==='back_forward';
  var sameRef=document.referrer&&document.referrer.indexOf(location.host)>-1;
  if(!isBack&&!sameRef)return;
  var raw=null;try{raw=sessionStorage.getItem(SCROLL_KEY);}catch(e){}
  if(!raw)return;
  var st=null;try{st=JSON.parse(raw);}catch(e){}
  if(!st||!st.n||st.n<=100||Date.now()-st.t>1800000)return;
  var target=st.n;
  (function again(ok){
    var cur=tl.querySelectorAll('.feed-item').length;
    if(ok&&cur<target){loadMore().then(again);return;}
    window.scrollTo(0,st.y);
  })(true);
}
maybeRestoreScroll();
/* 同事件归组展开（B13）：事件委托，动态加载的也生效 */
document.addEventListener('click',function(e){
  var t=e.target.closest('.group-toggle');
  if(!t)return;
  var li=t.closest('.slot');
  var extra=li?li.querySelector('.group-extra'):null;
  if(!extra)return;
  var open=extra.hidden;
  extra.hidden=!open;
  t.setAttribute('aria-expanded',open?'true':'false');
  var n=extra.querySelectorAll('li').length;
  t.innerHTML=open?'收起 <span aria-hidden="true">▴</span>':'另有 '+n+' 篇报道 <span aria-hidden="true">▾</span>';
});
/* 浮层搜索框的清空按钮（B14） */
var ovClear=document.getElementById('overlaySearchClear');
if(ovClear)ovClear.addEventListener('click',function(){
  if(overlayInput){overlayInput.value='';overlayInput.dispatchEvent(new Event('input'));overlayInput.focus();}
});
/* 详情页（B20）：打开即记已读；收藏；返回；复制链接；分享 */
(function(){
  var pg=document.querySelector('[data-item-id]');
  if(!pg)return;
  var id=pg.getAttribute('data-item-id');
  if(id&&!readSet.has(id)){readSet.add(id);saveSet(LS_READ,readSet);}
  var star=document.getElementById('itemStarBtn');
  if(star&&id){
    var syncStar=function(){
      var on=starSet.has(id);
      star.classList.toggle('on',on);
      var sp=star.querySelector('span');if(sp)sp.textContent=on?'已收藏':'收藏';
      star.setAttribute('aria-label',on?'取消收藏':'收藏');star.title=on?'取消收藏':'收藏';
    };
    syncStar();
    star.addEventListener('click',function(){
      if(starSet.has(id)){starSet.delete(id);delete starMeta[id];}
      else{
        starSet.add(id);
        starMeta[id]={title:star.getAttribute('data-title')||'',
          link:star.getAttribute('data-link')||'',
          source:star.getAttribute('data-source')||'',at:new Date().toISOString()};
      }
      saveSet(LS_STAR,starSet);saveObj(LS_STAR_META,starMeta);
      syncStar();updateStarCounts();
    });
  }
  var back=document.getElementById('backBtn');
  if(back)back.addEventListener('click',function(){
    if(document.referrer&&document.referrer.indexOf(location.host)>-1){history.back();}
    else{location.href=back.getAttribute('data-home')||'../index.html';}
  });
  var copy=document.getElementById('copyLinkBtn');
  function fallbackCopy(text){
    var ta=document.createElement('textarea');ta.value=text;
    ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);
  }
  if(copy)copy.addEventListener('click',function(){
    var url=location.href;
    var done=function(){copy.textContent='已复制';setTimeout(function(){copy.textContent='复制链接';},1500);};
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(url).then(done).catch(function(){fallbackCopy(url);done();});
    }else{fallbackCopy(url);done();}
  });
  var share=document.getElementById('shareBtn');
  if(share&&navigator.share){
    share.hidden=false;
    share.addEventListener('click',function(){
      navigator.share({title:document.title,url:location.href}).catch(function(){});
    });
  }
})();
})();
</script>
"""

# 外置 JS 文件内容：去掉 SCRIPT 首尾的 <script> 标签
# （行内主题启动脚本 THEME_BOOT 不受影响，继续内联在 <head> 里）
_SCRIPT_JS = SCRIPT.strip()
assert _SCRIPT_JS.startswith("<script>") and _SCRIPT_JS.endswith("</script>")
SCRIPT_JS = _SCRIPT_JS[len("<script>"):-len("</script>")].strip() + "\n"


def _logo(size):
    return LOGO_SVG.replace('width="44" height="44"',
                            f'width="{size}" height="{size}"')


def _nav_items(prefix, base=""):
    """prefix：首页为 ''，其它根页面为 'index.html'，
    子目录页为 '../index.html'（base 拼在前面）。
    精选与全部动态是两个页面（对标 AIHOT / 与 /all），不再是站内锚点。"""
    root = prefix or "index.html"
    all_page = root.replace("index.html", "all.html")
    return [
        ("featured", "精选", "zap", f"{base}{root}"),
        ("all", "全部动态", "list", f"{base}{all_page}"),
        ("hot", "热点榜", "trend", f"{base}hot.html"),
        ("daily", "日报", "doc", f"{base}daily.html"),
        ("starred", "收藏", "bookmark", f"{base}starred.html"),
    ]


def sidebar_html(site, active, prefix="", base=""):
    items = _nav_items(prefix, base)
    links = "".join(
        f'<a class="side-link{" on" if key == active else ""}" href="{href}">'
        f'{icon(ic)}<span>{label}</span>'
        f'{"<span class=\"num\" id=\"starCount\"></span>" if key == "starred" else ""}</a>'
        for key, label, ic, href in items)
    more_links = (
        f'<a class="side-link{" on" if active == "more" else ""}" href="{base}more.html">{icon("user")}<span>我的</span></a>'
        f'<a class="side-link" href="{base}weekly.html">{icon("calendar")}<span>周报</span></a>'
        f'<a class="side-link" href="{base}monthly.html">{icon("calendar")}<span>月报</span></a>'
        f'<a class="side-link{" on" if active == "changelog" else ""}" href="{base}changelog.html">{icon("doc")}<span>更新日志</span></a>'
        f'<a class="side-link{" on" if active == "about" else ""}" href="{base}about.html">{icon("rss")}<span>关于</span></a>'
        f'<a class="side-link" href="https://github.com/Staroltest/indie-game-hot/issues" target="_blank" rel="noopener">{icon("message")}<span>反馈</span></a>'
        f'<a class="side-link" href="{base}data/hot.json">{icon("code")}<span>数据 JSON</span></a>')
    return (f'<aside class="sidebar"><a class="brand" href="{base}index.html">{_logo(32)}'
            f'<span><b>IndieHOT</b><small>独立游戏开发者热点</small></span></a>'
            f'<nav aria-label="主导航"><div class="nav-sec"><h3>内容</h3>{links}</div>'
            f'<div class="nav-sec"><h3>更多</h3>{more_links}'
            f'</div></nav>'
            f'<div class="side-foot">'
            f'<a class="gh" href="https://github.com/Staroltest/indie-game-hot" target="_blank" rel="noopener">GitHub 仓库</a>'
            f'{_theme_btn()}'
            f'<p class="note">每天 {esc(site["edition_time_cst"])} 出日报</p>'
            f'</div></aside>')


def rail_html(active, prefix="", base=""):
    items = _nav_items(prefix, base)
    links = "".join(
        f'<a class="rail-link{" on" if key == active else ""}" href="{href}">'
        f'{icon(ic, 20)}<span>{label}</span></a>'
        for key, label, ic, href in items)
    links += (f'<a class="rail-link{" on" if active == "more" else ""}" href="{base}more.html">'
              f'{icon("user", 20)}<span>我的</span></a>')
    return (f'<aside class="rail"><a class="brand" href="{base}index.html" aria-label="IndieHOT 首页">'
            f'{_logo(28)}</a><nav aria-label="主导航">{links}</nav></aside>')


def _theme_btn():
    return ('<button type="button" class="icon-btn theme-btn theme-cycle" aria-label="外观：跟随系统（点击切换）">'
            f'<span class="ic-moon">{icon("moon", 20)}</span>'
            f'<span class="ic-sun">{icon("sun", 20)}</span>'
            f'<span class="ic-monitor">{icon("monitor", 20)}</span></button>')


def topbar_html(active, prefix="", base=""):
    if active in ("daily", "weekly", "monthly"):
        center = ('<span class="title">'
                  + {"daily": "日报", "weekly": "周报", "monthly": "月报"}[active] + '</span>')
    else:
        root = prefix or "index.html"
        all_page = root.replace("index.html", "all.html")
        center = (f'<span class="switch">'
                  f'<a class="{"on" if active == "featured" else ""}" href="{base}{root}">精选</a>'
                  f'<a class="{"on" if active == "all" else ""}" href="{base}{all_page}">全部</a></span>')
    return (f'<header class="topbar"><a class="brand" href="{base}index.html">{_logo(26)}<b>IndieHOT</b></a>'
            f'{center}{_theme_btn()}</header>')


def tabbar_html(active, prefix="", base=""):
    root = prefix or "index.html"
    all_page = root.replace("index.html", "all.html")
    tabs = [
        ("featured", "精选", "zap", f"{base}{root}"),
        ("hot", "热点", "trend", f"{base}hot.html"),
        ("daily", "日报", "doc", f"{base}daily.html"),
        ("all", "全部", "list", f"{base}{all_page}"),
        ("starred", "收藏", "bookmark", f"{base}starred.html"),
    ]
    links = "".join(
        f'<a class="{"on" if key == active else ""}" href="{href}">'
        f'{icon(ic, 20)}<span>{label}</span></a>'
        for key, label, ic, href in tabs)
    return f'<nav class="tabbar" aria-label="主导航"><div class="tabbar-in">{links}</div></nav>'


def footer_html(base=""):
    return ('<footer class="foot"><p>IndieHOT 是聚合摘要与阅读索引，标题与摘要版权归各来源所有。'
            '内容只作开发者资讯参考；本站不转载全文。</p>'
            '<p>信息架构与界面风格参考 AIHOT（采集—评分—精选—日报流水线、暖纸底色与侧栏导航），'
            '本站代码按 MIT 协议开源。</p>'
            f'<nav class="foot-links" aria-label="页脚链接">'
            f'<a href="{base}feed.xml">RSS 订阅</a>'
            f'<a href="{base}data/days/index.json">数据 JSON</a>'
            f'<a href="https://github.com/Staroltest/indie-game-hot">GitHub 仓库</a>'
            f'<a href="https://github.com/Staroltest/indie-game-hot/issues">反馈</a>'
            f'<a href="{base}more.html">我的</a>'
            f'<a href="{base}about.html">关于</a>'
            f'<a href="{base}terms.html">条款</a>'
            f'<a href="{base}privacy.html">隐私</a></nav>'
            f'<div class="tip"><p class="tip-title">赞赏支持</p>'
            f'<img class="tip-qr" src="{base}assets/tip-usdt.png" alt="赞赏码：USDT TRC20" width="150" height="150">'
            f'<p class="tip-label">USDT · TRC20</p>'
            f'<p class="tip-addr">TBH8fAi6Vbr9G4VW3PDm3DugZU6fknr6na</p></div></footer>')


def page_open(site, active, title, description, prefix="", base="", canonical="",
              ld_items=None, page_cls="", ld_blocks=None):
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")
    canon = canonical or site_url + "/"
    og = (f'<meta property="og:title" content="{esc(title)}">\n'
          f'<meta property="og:description" content="{esc(description)}">\n'
          f'<meta property="og:type" content="website">\n'
          f'<meta property="og:url" content="{esc(canon)}">\n'
          f'<meta property="og:image" content="{site_url}/favicon.svg">\n'
          f'<meta name="twitter:card" content="summary">\n'
          f'<meta name="twitter:title" content="{esc(title)}">\n'
          f'<meta name="twitter:description" content="{esc(description)}">\n'
          f'<meta name="twitter:image" content="{site_url}/favicon.svg">\n')
    ld = {"@context": "https://schema.org", "@type": "WebSite",
          "name": "IndieHOT", "url": site_url,
          "description": site.get("description", "")}
    ld_parts = [json.dumps(ld, ensure_ascii=False)]
    if ld_items:
        items_ld = {"@context": "https://schema.org", "@type": "ItemList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": i + 1,
                         "name": (it.get("zh_title") or it["title"]),
                         "url": site_url + "/items/" + it["id"] + ".html"}
                        for i, it in enumerate(ld_items[:10])]}
        ld_parts.append(json.dumps(items_ld, ensure_ascii=False))
    for block in (ld_blocks or []):
        ld_parts.append(json.dumps(block, ensure_ascii=False))
    ld_html = "".join(
        f'<script type="application/ld+json">{part}</script>\n' for part in ld_parts)
    return (f"<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{esc(title)}</title>\n"
            f"<meta name=\"description\" content=\"{esc(description)}\">\n"
            f'<link rel="icon" type="image/svg+xml" href="{base}favicon.svg">\n'
            f"{og}"
            f'{ld_html}'
            f"{THEME_BOOT}<link rel=\"stylesheet\" href=\"{base}assets/site.css\"></head>\n<body>"
            f'<div class="shell">{sidebar_html(site, active, prefix, base)}{rail_html(active, prefix, base)}'
            f'<div class="main">{topbar_html(active, prefix, base)}<div class="page{page_cls}">')


def page_close(site, active, prefix="", base=""):
    to_top = (f'<button type="button" class="to-top" id="toTop" aria-label="回到顶部">'
              f'{icon("arrow-up", 18)}</button>')
    return (f"{footer_html(base)}</div></div></div>{to_top}{tabbar_html(active, prefix, base)}<script src='{base}assets/site.js'></script></body></html>\n")


def day_header(d, items, today, aside=False):
    is_today = d == today
    name = "今天" if is_today else f"{d.month}月{d.day}日"
    cls = "day-head is-today" if is_today else "day-head"
    toggle = (f'<button type="button" class="dh-toggle" aria-label="收起/展开这一天">'
              f'{icon("chevron-down", 14)}</button>')
    aside_html = ('<span class="dh-aside"><button type="button" class="toggle-chip unread-toggle">'
                  '只看未读</button></span>') if aside else ""
    return (f'<header class="{cls}">'
            f'<span class="dh-name">{name}</span>'
            f'<span class="dh-date">{d.month}月{d.day}日</span>'
            f'{toggle}'
            f'<span class="dh-wd dh-wd-short">{WEEKDAYS_SHORT[d.weekday()]}</span>'
            f'<span class="dh-wd dh-wd-full">{WEEKDAYS[d.weekday()]}</span>'
            f'<span class="dh-count"><b class="num">{len(items)}</b> 条</span>{aside_html}</header>')


def _norm_title(title):
    """标题归一化：去标点/空白/大小写，供同事件归组比较用。"""
    t = (title or "").lower()
    t = re.sub(r"[\s\-_·•|/\\:;,.!?？！。、，；：（）()\[\]{}“”\"'‘’<>《》]+", "", t)
    return t


def group_similar(items):
    """保守版同事件归组（B13）：只合并同一来源、同一天、标题归一化后相同
    或仅差后缀（短的是长的的前缀，且长度比 ≥0.8；归一化后至少 10 字）。
    返回 [{main, extra:[...]}]，顺序不变；跨来源不合并。"""
    groups = []
    for it in items:
        pub = it["_pub"].astimezone(CST).date()
        key = (it.get("source_id", ""), pub)
        norm = _norm_title(it.get("zh_title") or it["title"])
        merged = False
        if len(norm) >= 10:
            for g in groups:
                m = g["main"]
                if (m.get("source_id", ""), m["_pub"].astimezone(CST).date()) != key:
                    continue
                mnorm = _norm_title(m.get("zh_title") or m["title"])
                if len(mnorm) < 10:
                    continue
                if norm == mnorm or (
                        (norm.startswith(mnorm) or mnorm.startswith(norm))
                        and min(len(norm), len(mnorm)) / max(len(norm), len(mnorm)) >= 0.8):
                    g["extra"].append(it)
                    merged = True
                    break
        if not merged:
            groups.append({"main": it, "extra": []})
    return groups


def _same_topic(a, b):
    """跨来源同事件判定（2026-11-11 把关新增）：归一化标题一短一长、
    短者是长者的前缀且至少 10 字 -> 同一事件（如两家媒体同报 UE 5.9 预览）。
    只用于精选/热点/日报的取前 N 去重，时间线不受影响。"""
    na = _norm_title(a.get("zh_title") or a["title"])
    nb = _norm_title(b.get("zh_title") or b["title"])
    if len(na) < 10 or len(nb) < 10:
        return False
    return na.startswith(nb) or nb.startswith(na)


def dedup_topics(items):
    """按顺序去重同事件条目，保留先出现的（调用前已按分数排好序）。"""
    kept = []
    for it in items:
        if any(_same_topic(it, k) for k in kept):
            continue
        kept.append(it)
    return kept


def timeline_slot(it, sel_ids, base=""):
    pub = it["_pub"].astimezone(CST)
    badge = it["id"] in sel_ids
    return (f'<li class="slot" data-category="{esc(it["category"])}">'
            f'<time class="slot-time" datetime="{pub.isoformat()}">{pub.strftime("%H:%M")}</time>'
            f'<span class="rail" aria-hidden="true"></span>'
            f'{feed_item(it, badge=badge, base=base)}</li>')


def timeline_group_slot(group, sel_ids, base=""):
    """归组后的时间线格：主卡 + “另有 N 篇报道”展开（B13）。"""
    main = group["main"]
    extra = group["extra"]
    slot = timeline_slot(main, sel_ids, base)
    if not extra:
        return slot
    n = len(extra)
    rows = "".join(
        f'<li><a href="{base}items/{esc(e["id"])}.html">'
        f'{esc(e.get("zh_title") or e["title"])}</a>'
        f'<span class="ge-time">{esc(e["_pub"].astimezone(CST).strftime("%H:%M"))}</span></li>'
        for e in extra)
    toggle = (f'<button type="button" class="group-toggle" aria-expanded="false">'
              f'另有 {n} 篇报道 <span aria-hidden="true">▾</span></button>')
    extra_html = (f'<div class="group-extra" hidden><ul class="group-extra-list">{rows}</ul></div>')
    # 把展开区插进主卡的 article 末尾之前：直接拼在 slot 的 </li> 之前
    return slot[:-5] + toggle + extra_html + "</li>"


# 首页首屏服务端渲染的时间线条数上限（见 render_index 里的说明）
INDEX_SSR_SLOTS = 20


def _tl_sections(items, sel_ids):
    """时间线按天分组渲染（首页精选 feed 与全部动态页共用）。
    首屏只渲染前 INDEX_SSR_SLOTS 条（全渲染会让单文件超过推送上限）；
    其余由 JS 分页按需从 data/days/ 取。日期头上的条数按已渲染的算，
    否则 JS 追加时再累加会把数算重。"""
    rendered = items[:INDEX_SSR_SLOTS]
    groups = group_similar(rendered)
    days = {}
    for g in groups:
        days.setdefault(g["main"]["_pub"].astimezone(CST).date(), []).append(g)
    day_counts = {}
    for it in rendered:
        d = it["_pub"].astimezone(CST).date()
        day_counts[d] = day_counts.get(d, 0) + 1
    today = datetime.now(CST).date()
    tl_sections = ""
    for n, (d, gs) in enumerate(days.items()):
        slots = "".join(timeline_group_slot(g, sel_ids) for g in gs)
        fake_items = [None] * day_counts[d]
        tl_sections += (f'<section class="day-sec" data-date="{d.isoformat()}">'
                        f'{day_header(d, fake_items, today, aside=(n == 0))}'
                        f'<ol class="day-list">{slots}</ol></section>')
    return tl_sections


def render_index(site, cats, cfg, src_by_id, selected, hot, timeline, generated_at, counts):
    sel_ids = {it["id"] for it in selected}
    cat_opts = "".join(
        f'<button type="button" class="chip" data-filter="{c["key"]}">{esc(c["label"])}</button>'
        for c in cats)
    cat_opts += '<button type="button" class="chip" data-filter="official">官方</button>'
    search_html = (f'<div class="search" role="search">{icon("search", 16)}'
                   f'<input id="siteSearch" type="text" placeholder="搜索标题、摘要…" '
                   f'maxlength="200" autocomplete="off" aria-label="搜索标题和摘要">'
                   f'<button type="button" class="clear" id="searchClear" aria-label="清空">✕</button>'
                   f'<kbd>/</kbd></div>')
    search_trigger = (f'<button type="button" class="search-trigger" aria-label="打开搜索">'
                      f'{icon("search", 16)}<span>搜索标题、摘要…</span></button>')
    overlay_cats = "".join(
        f'<button type="button" class="chip" data-filter="{c["key"]}">{esc(c["label"])}</button>'
        for c in cats)
    overlay_cats += '<button type="button" class="chip" data-filter="official">官方</button>'
    overlay_hot = "".join(
        f'<li><span class="rank{" r1" if i == 0 else (" r2" if i == 1 else (" r3" if i == 2 else ""))}">{i + 1}</span>'
        f'<a href="items/{esc(it["id"])}.html">{esc(it.get("zh_title") or it["title"])}</a></li>'
        for i, it in enumerate(hot[:5]))
    overlay_html = (
        f'<div class="search-overlay" id="searchOverlay" hidden>'
        f'<div class="overlay-bar"><button type="button" class="icon-btn" id="overlayClose" aria-label="关闭搜索">{icon("x", 20)}</button>'
        f'<div class="search overlay-search">{icon("search", 16)}'
        f'<input id="overlaySearch" type="text" placeholder="搜索标题、摘要…" maxlength="200" autocomplete="off" aria-label="搜索标题和摘要">'
        f'<button type="button" class="clear" id="overlaySearchClear" aria-label="清空">✕</button></div></div>'
        f'<div class="overlay-sec"><h3>最近搜索</h3><div id="recentList"></div>'
        f'<button type="button" class="recent-clear" id="clearRecent">清除最近搜索</button></div>'
        f'<div class="overlay-sec"><h3>按分类找</h3><div class="overlay-cats">{overlay_cats}</div></div>'
        f'<div class="overlay-sec"><h3>正在热议</h3><ol class="overlay-hot">{overlay_hot}</ol></div>'
        f'</div>')
    sel_html = "".join(feed_item(it, badge=True) for it in selected) \
        or "<p>今日暂无达到精选门槛的条目。</p>"
    top5 = hot[:5]
    hot_html = "".join(hot_row(it, i + 1) for i, it in enumerate(top5)) \
        or "<li>暂无热点。</li>"
    hot_more = ('<a class="hot-more" href="hot.html">'
                '完整榜单 <span aria-hidden="true">→</span></a>') if len(hot) > 5 else ""
    tl_sections = _tl_sections(timeline, sel_ids)
    unread_row = ('<div class="unread-row"><button type="button" class="toggle-chip unread-toggle">'
                  '只看未读</button></div>')
    # 给 JS 分页/搜索用的配置：打分参数 + 关键词标签映射 + 信源分级（供动态渲染卡片时算分/理由/标签）
    sc = cfg["scoring"]
    ih_cfg = {
        "base": sc["base"],
        "recency": sc["recency"],
        "tierBonus": sc["tier_bonus"],
        "devBonus": sc["dev_keyword_bonus"],
        "devCap": sc["dev_keyword_cap"],
        "devLabels": REASON_LABELS,
        "noiseKw": sc["noise_keywords"],
        "noisePenalty": sc["noise_keyword_penalty"],
        "noiseFloor": sc["noise_keyword_floor"],
        "tagSlugs": TAG_SLUGS,
        "tiers": {sid: s.get("tier", "T2") for sid, s in src_by_id.items()},
    }
    cfg_json = json.dumps(ih_cfg, ensure_ascii=False)
    empty_state = (
        '<div id="noResult" class="empty-state" hidden>'
        f'<div class="empty-icon" id="noResultIcon">{icon("search", 40)}</div>'
        '<h3 class="empty-title" id="noResultTitle">这个分类下暂无动态。</h3>'
        '<p class="empty-text" id="noResultText"></p>'
        '<div class="empty-actions">'
        '<button type="button" class="empty-btn" data-empty-action="clear">清除筛选</button>'
        '<button type="button" class="empty-btn primary" data-empty-action="featured">去看精选</button>'
        '</div></div>')
    return (page_open(site, "featured", f'{site["title"]} · 今日精选与热点榜',
                      site["description"], ld_items=selected)
            + f'<div class="m-title">精选</div>'
            + f'<h1>精选</h1>'
            + f'<p class="lede">更新于 {esc(generated_at)}（北京时间）<span class="lede-rest"> · 最近 7 天共 {counts["total_7d"]} 条 · '
            f'精选 {counts["selected"]} 条 · 只显示标题与摘要，点击标题查看详情，详情页里有原文链接。</span></p>'
            + f'<div class="filter-row"><div class="tabs" role="tablist" aria-label="分类筛选">'
            f'<span class="tabs-thumb" id="tabsThumb"></span>'
            f'<button type="button" class="chip on" data-filter="all">全部</button>{cat_opts}</div>'
            + search_trigger + search_html + '</div>'
            + overlay_html
            + f'<section class="card hot-card" id="hot" aria-labelledby="hot-topics">'
            f'<div class="hot-head"><h2 id="hot-topics"><span class="live" aria-hidden="true">'
            f'<span class="ping"></span><span class="dot"></span></span>当前热点</h2></div>'
            f'<ol class="hot-list hot-top5">{hot_html}</ol>'
            + hot_more
            + '</section>'
            + f'<div id="featured">{sel_html}</div>'
            + f'<div id="timeline">'
            + unread_row
            + tl_sections
            + '</div>'
            + f'<a class="more-row" href="all.html">{icon("list", 18)}'
            + '<span class="m-label">查看全部动态</span><span class="m-arrow">›</span></a>'
            + '<div id="searchResults" hidden>'
            + '<div class="search-results-head"><h2>搜索结果</h2><span class="sr-count" id="srCount"></span></div>'
            + '<div id="srList"></div></div>'
            + empty_state
            + '<div id="loadMoreSentinel"></div>'
            + '<div class="load-more-wrap" id="loadMoreWrap">'
            + '<button type="button" class="load-more-btn" id="loadMoreBtn">加载更多</button>'
            + '<p class="load-more-state" id="loadMoreState"></p></div>'
            + '<p class="feed-end" id="feedEnd" hidden>已经到底了</p>'
            + f'<script id="ihConfig" type="application/json">{cfg_json}</script>'
            + page_close(site, "featured"))


def render_all(site, cats, cfg, src_by_id, selected, hot, pool, generated_at):
    """全部动态页（对标 AIHOT /all）：30 天全量按时间倒序、按天分组；
    首屏只渲染前 INDEX_SSR_SLOTS 条，其余由 JS 分页从 data/days/ 取。
    无精选区、无热点框；分类筛选与搜索与首页共用同一套 JS。"""
    sel_ids = {it["id"] for it in selected}
    cat_opts = "".join(
        f'<button type="button" class="chip" data-filter="{c["key"]}">{esc(c["label"])}</button>'
        for c in cats)
    cat_opts += '<button type="button" class="chip" data-filter="official">官方</button>'
    search_html = (f'<div class="search" role="search">{icon("search", 16)}'
                   f'<input id="siteSearch" type="text" placeholder="搜索标题、摘要…" '
                   f'maxlength="200" autocomplete="off" aria-label="搜索标题和摘要">'
                   f'<button type="button" class="clear" id="searchClear" aria-label="清空">✕</button>'
                   f'<kbd>/</kbd></div>')
    search_trigger = (f'<button type="button" class="search-trigger" aria-label="打开搜索">'
                      f'{icon("search", 16)}<span>搜索标题、摘要…</span></button>')
    overlay_cats = "".join(
        f'<button type="button" class="chip" data-filter="{c["key"]}">{esc(c["label"])}</button>'
        for c in cats)
    overlay_cats += '<button type="button" class="chip" data-filter="official">官方</button>'
    overlay_hot = "".join(
        f'<li><span class="rank{" r1" if i == 0 else (" r2" if i == 1 else (" r3" if i == 2 else ""))}">{i + 1}</span>'
        f'<a href="items/{esc(it["id"])}.html">{esc(it.get("zh_title") or it["title"])}</a></li>'
        for i, it in enumerate(hot[:5]))
    overlay_html = (
        f'<div class="search-overlay" id="searchOverlay" hidden>'
        f'<div class="overlay-bar"><button type="button" class="icon-btn" id="overlayClose" aria-label="关闭搜索">{icon("x", 20)}</button>'
        f'<div class="search overlay-search">{icon("search", 16)}'
        f'<input id="overlaySearch" type="text" placeholder="搜索标题、摘要…" maxlength="200" autocomplete="off" aria-label="搜索标题和摘要">'
        f'<button type="button" class="clear" id="overlaySearchClear" aria-label="清空">✕</button></div></div>'
        f'<div class="overlay-sec"><h3>最近搜索</h3><div id="recentList"></div>'
        f'<button type="button" class="recent-clear" id="clearRecent">清除最近搜索</button></div>'
        f'<div class="overlay-sec"><h3>按分类找</h3><div class="overlay-cats">{overlay_cats}</div></div>'
        f'<div class="overlay-sec"><h3>正在热议</h3><ol class="overlay-hot">{overlay_hot}</ol></div>'
        f'</div>')
    tl_sections = _tl_sections(pool, sel_ids)
    unread_row = ('<div class="unread-row"><button type="button" class="toggle-chip unread-toggle">'
                  '只看未读</button></div>')
    sc = cfg["scoring"]
    ih_cfg = {
        "base": sc["base"],
        "recency": sc["recency"],
        "tierBonus": sc["tier_bonus"],
        "devBonus": sc["dev_keyword_bonus"],
        "devCap": sc["dev_keyword_cap"],
        "devLabels": REASON_LABELS,
        "noiseKw": sc["noise_keywords"],
        "noisePenalty": sc["noise_keyword_penalty"],
        "noiseFloor": sc["noise_keyword_floor"],
        "tagSlugs": TAG_SLUGS,
        "tiers": {sid: s.get("tier", "T2") for sid, s in src_by_id.items()},
    }
    cfg_json = json.dumps(ih_cfg, ensure_ascii=False)
    empty_state = (
        '<div id="noResult" class="empty-state" hidden>'
        f'<div class="empty-icon" id="noResultIcon">{icon("search", 40)}</div>'
        '<h3 class="empty-title" id="noResultTitle">这个分类下暂无动态。</h3>'
        '<p class="empty-text" id="noResultText"></p>'
        '<div class="empty-actions">'
        '<button type="button" class="empty-btn" data-empty-action="clear">清除筛选</button>'
        '<button type="button" class="empty-btn primary" data-empty-action="featured">去看精选</button>'
        '</div></div>')
    return (page_open(site, "all", f'{site["title"]} · 全部动态',
                      site["description"])
            + f'<div class="m-title">全部动态</div>'
            + f'<h1>全部动态</h1>'
            + f'<p class="lede">更新于 {esc(generated_at)}（北京时间）<span class="lede-rest"> · 最近 30 天共 {len(pool)} 条 · '
            f'只显示标题与摘要，点击标题查看详情，详情页里有原文链接。</span></p>'
            + f'<div class="filter-row"><div class="tabs" role="tablist" aria-label="分类筛选">'
            f'<span class="tabs-thumb" id="tabsThumb"></span>'
            f'<button type="button" class="chip on" data-filter="all">全部</button>{cat_opts}</div>'
            + search_trigger + search_html + '</div>'
            + overlay_html
            + f'<div id="timeline">'
            + unread_row
            + tl_sections
            + '</div>'
            + '<div id="searchResults" hidden>'
            + '<div class="search-results-head"><h2>搜索结果</h2><span class="sr-count" id="srCount"></span></div>'
            + '<div id="srList"></div></div>'
            + empty_state
            + '<div id="loadMoreSentinel"></div>'
            + '<div class="load-more-wrap" id="loadMoreWrap">'
            + '<button type="button" class="load-more-btn" id="loadMoreBtn">加载更多</button>'
            + '<p class="load-more-state" id="loadMoreState"></p></div>'
            + '<p class="feed-end" id="feedEnd" hidden>已经到底了</p>'
            + f'<script id="ihConfig" type="application/json">{cfg_json}</script>'
            + page_close(site, "all"))


LAUNCH_DATE = datetime(2026, 11, 9, tzinfo=CST).date()


def issue_no(kind, date_iso):
    """期号从上线日 2026-11-09 起算：日报 = 上线天数；周报 = 上线周数；
    月报 = 上线月数。"""
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    days = (d - LAUNCH_DATE).days + 1
    if kind == "daily":
        return days
    if kind == "weekly":
        return days // 7 + 1
    return (d.year - 2026) * 12 + (d.month - 11) + 1


def render_period(site, kind, report, base="", prev=None, next_=None, canonical=""):
    """日报/周报/月报共用同一模板（B9）：kind 为 daily/weekly/monthly。
    report: {kind, date, date_iso, issue, summary, items}。
    prev/next: (label, href) 或 None。base 是到站根的目录前缀。"""
    labels = {"daily": ("日报", "过去 24 小时", "今日精选", "每天"),
              "weekly": ("周报", "过去 7 天", "本周精选", "每周"),
              "monthly": ("月报", "过去 30 天", "本月精选", "每月")}
    name, window, picked, _every = labels[kind]
    if kind == "daily":
        sched_txt = f"每天 {esc(site['edition_time_cst'])} 出刊"
    elif kind == "weekly":
        sched_txt = "过去 7 天滚动精选，每半小时更新"
    else:
        sched_txt = "过去 30 天滚动精选，每半小时更新"
    items_html = "".join(feed_item(it, rank=i + 1, base=base)
                         for i, it in enumerate(report["items"])) \
        or f"<p>{window}没有达到精选门槛的条目。</p>"
    try:
        wd = WEEKDAYS[datetime.strptime(report["date"], "%Y年%m月%d日").weekday()]
    except ValueError:
        wd = ""
    nav = ""
    if prev or next_:
        pl = (f'<a href="{esc(prev[1])}">← 上一期 · {esc(prev[0])}</a>') if prev else "<span></span>"
        nl = (f'<a class="dn-next" href="{esc(next_[1])}">下一期 · {esc(next_[0])} →</a>') if next_ else ""
        nav = f'<nav class="daily-nav">{pl}{nl}</nav>'
    period_nav = (
        f'<p class="more-note">'
        f'<a href="{base}daily.html">日报</a> · <a href="{base}weekly.html">周报</a> · '
        f'<a href="{base}monthly.html">月报</a> · <a href="{base}daily/archive.html">往期存档</a></p>')
    return (page_open(site, kind, f'{site["name"]}{name} · {report["date"]}',
                      site["description"], prefix="index.html", base=base,
                      canonical=canonical, ld_items=report["items"], page_cls=" page-reading")
            + f'<div class="m-title">{name}</div>'
            + f'<div class="report-shell"><div class="masthead"><div class="mp-brand">'
            f'{_logo(44)}<b>{esc(site["name"])}{name}</b><span class="mp-issue">第 {report["issue"]} 期</span></div>'
            + f'<p class="mp-meta">{esc(report["date"])} {wd} · {sched_txt} · '
            f'{window}精选 {len(report["items"])} 条</p>{period_nav}</div>'
            + f'<div class="well" style="margin-top:16px"><span class="kicker">速览</span>'
            f'<p>{esc(report["summary"])}</p></div>'
            + f'<h2 class="sec">{picked} {len(report["items"])} 条</h2>'
            + items_html + nav + "</div>"
            + page_close(site, kind, prefix="index.html", base=base))


def render_daily(site, daily, base="", prev=None, next_=None, canonical=""):
    report = dict(daily)
    report.setdefault("kind", "daily")
    report.setdefault("issue", issue_no("daily", daily.get("date_iso", "2026-11-09")))
    return render_period(site, "daily", report, base=base, prev=prev, next_=next_,
                         canonical=canonical)


def render_starred(site):
    """收藏页（B3）：内容由 JS 从 localStorage 快照 + data/days/ 分块渲染，
    下架条目置灰并提示；导入/导出先不做。"""
    empty_state = (
        '<div id="starEmpty" class="empty-state" hidden>'
        f'<div class="empty-icon">{icon("bookmark", 40)}</div>'
        '<h3 class="empty-title">还没有收藏</h3>'
        '<p class="empty-text">去点卡片右上角的书签，喜欢的动态就会出现在这里。</p>'
        '<div class="empty-actions">'
        '<a class="empty-btn primary" href="index.html">去看精选</a>'
        '</div></div>')
    return (page_open(site, "starred", f'{site["title"]} · 收藏',
                      site["description"], prefix="index.html",
                      canonical=site.get("url", "") + "/starred.html")
            + f'<div class="m-title">收藏</div>'
            + f'<h1>收藏</h1>'
            + f'<p class="lede">收藏只保存在你的浏览器里（localStorage），换设备不会同步。<span class="lede-rest">'
            f'点开标题即记已读；下架的条目会置灰保留。</span></p>'
            + '<div id="starList"></div>'
            + empty_state
            + page_close(site, "starred", prefix="index.html"))


def render_more(site, changelog_version):
    """“我的”页（B4）：收藏、外观、数据、更新日志、反馈、RSS、关于等入口收在一起。"""
    star_row = (
        f'<a class="more-row" href="starred.html">{icon("bookmark", 18)}'
        f'<span class="m-label">收藏</span>'
        f'<span class="m-val"><span id="moreStarCount">0</span> 条</span>'
        f'<span class="m-arrow">›</span></a>')
    theme_seg = (
        f'<div class="more-row" style="cursor:default">{icon("sun", 18)}'
        f'<span class="m-label">外观</span>'
        f'<span class="theme-seg" role="group" aria-label="外观">'
        f'<span class="theme-seg-thumb" id="themeSegThumb"></span>'
        f'<button type="button" data-theme-opt="dark">深色</button>'
        f'<button type="button" data-theme-opt="system">跟随系统</button>'
        f'<button type="button" data-theme-opt="light">浅色</button>'
        f'</span></div>')
    data_row = (
        f'<a class="more-row" href="feed.xml">{icon("rss", 18)}'
        f'<span class="m-label">RSS 订阅</span><span class="m-val">feed.xml</span>'
        f'<span class="m-arrow">›</span></a>'
        f'<a class="more-row" href="data/days/index.json">{icon("code", 18)}'
        f'<span class="m-label">数据 JSON</span><span class="m-val">30 天全量</span>'
        f'<span class="m-arrow">›</span></a>')
    about_rows = (
        f'<a class="more-row" href="about.html">{icon("rss", 18)}'
        f'<span class="m-label">关于 IndieHOT</span><span class="m-arrow">›</span></a>'
        f'<a class="more-row" href="changelog.html">{icon("doc", 18)}'
        f'<span class="m-label">更新日志</span>'
        f'<span class="dot-red" id="changelogDot" data-changelog-version="{esc(changelog_version)}" hidden></span>'
        f'<span class="m-arrow">›</span></a>'
        f'<a class="more-row" href="https://github.com/Staroltest/indie-game-hot/issues" target="_blank" rel="noopener">{icon("message", 18)}'
        f'<span class="m-label">反馈</span><span class="m-val">GitHub Issues</span>'
        f'<span class="m-arrow">›</span></a>'
        f'<a class="more-row" href="terms.html">{icon("doc", 18)}'
        f'<span class="m-label">条款</span><span class="m-arrow">›</span></a>'
        f'<a class="more-row" href="privacy.html">{icon("doc", 18)}'
        f'<span class="m-label">隐私</span><span class="m-arrow">›</span></a>')
    return (page_open(site, "more", f'{site["title"]} · 我的',
                      site["description"], prefix="index.html",
                      canonical=site.get("url", "") + "/more.html")
            + f'<div class="m-title">我的</div>'
            + f'<h1>我的</h1>'
            + f'<p class="lede">收藏、外观、数据与关于，都在这里。</p>'
            + f'<section class="more-sec"><h2>内容</h2>{star_row}'
            f'<a class="more-row" href="daily/archive.html">{icon("calendar", 18)}'
            f'<span class="m-label">往期日报</span><span class="m-arrow">›</span></a>'
            f'<a class="more-row" href="weekly.html">{icon("calendar", 18)}'
            f'<span class="m-label">周报</span><span class="m-arrow">›</span></a>'
            f'<a class="more-row" href="monthly.html">{icon("calendar", 18)}'
            f'<span class="m-label">月报</span><span class="m-arrow">›</span></a></section>'
            + f'<section class="more-sec"><h2>外观</h2>{theme_seg}</section>'
            + f'<section class="more-sec"><h2>数据与订阅</h2>{data_row}'
            f'<p class="more-note">RSS 用 feed.xml 订阅（精选 20 条，每半小时更新）。'
            f'想自己拉全量数据：<a href="data/days/index.json">data/days/</a> 按天分块存放最近 30 天的条目，'
            f'<a href="data/hot.json">data/hot.json</a> 是今日热点榜 Top 20。</p></section>'
            + f'<section class="more-sec"><h2>关于本站</h2>{about_rows}</section>'
            + page_close(site, "more", prefix="index.html"))


def render_changelog(site, entries):
    """更新日志页（B6）：由 data/changelog.json 驱动；打开即记为已读（JS）。"""
    latest = entries[0]["version"] if entries else ""
    items_html = ""
    for e in entries:
        lis = "".join(f"<li>{esc(x)}</li>" for x in e.get("items", []))
        items_html += (
            f'<article class="cl-item"><h3>{esc(e.get("title", ""))}</h3>'
            f'<p class="cl-date">{esc(e.get("date", ""))}</p>'
            f"<ul>{lis}</ul></article>")
    return (page_open(site, "changelog", f'{site["title"]} · 更新日志',
                      site["description"], prefix="index.html",
                      canonical=site.get("url", "") + "/changelog.html")
            + f'<div class="m-title">更新日志</div>'
            + f'<h1>更新日志</h1>'
            + f'<p class="lede">IndieHOT 每次改版都记在这里。</p>'
            + f'<div id="changelogPage" data-changelog-version="{esc(latest)}">{items_html}</div>'
            + page_close(site, "changelog", prefix="index.html"))


def render_archive(site, daily_entries, weekly_entries, monthly_entries):
    """往期存档（B8/B9）：日报按月分组；周报、月报各列一组。"""
    def month_groups(entries):
        groups = {}
        for e in entries:
            ym = e["date_iso"][:7]
            groups.setdefault(ym, []).append(e)
        return groups

    def daily_section():
        if not daily_entries:
            return "<p>还没有往期日报——从今天开始攒。</p>"
        out = ""
        for ym in sorted(month_groups(daily_entries), reverse=True):
            y, m = ym.split("-")
            rows = "".join(
                f'<li><a href="{esc(e["file"])}"><span class="a-date">{e["date_short"]}</span>'
                f'<span class="a-issue">第 {e["issue"]} 期</span>'
                f'<span class="a-count">{e["count"]} 条</span></a></li>'
                for e in month_groups(daily_entries)[ym])
            out += (f'<div class="arch-month"><h3>{y}年{int(m)}月</h3>'
                    f'<ul class="arch-list">{rows}</ul></div>')
        return out

    def period_section(title, entries, label):
        if not entries:
            return ""
        rows = "".join(
            f'<li><a href="{esc(e["file"])}"><span class="a-date">{e["date_short"]}</span>'
            f'<span class="a-issue">第 {e["issue"]} 期</span>'
            f'<span class="a-count">{e["count"]} 条</span></a></li>'
            for e in entries)
        return (f'<div class="arch-month"><h3>{title}</h3>'
                f'<ul class="arch-list">{rows}</ul></div>')

    return (page_open(site, "archive", f'{site["title"]} · 往期存档',
                      site["description"], prefix="index.html", base="../",
                      canonical=site.get("url", "") + "/daily/archive.html")
            + f'<div class="m-title">往期存档</div>'
            + f'<h1>往期存档</h1>'
            + f'<p class="lede">日报、周报、月报的合订本。期号从 2026-11-09 上线日起算。</p>'
            + f'<h2 class="sec">日报</h2>' + daily_section()
            + period_section("周报", weekly_entries, "周报")
            + period_section("月报", monthly_entries, "月报")
            + page_close(site, "archive", prefix="index.html", base="../"))


def render_topic(site, label, slug, items):
    """主题页（B11）：某标签下的条目列表。"""
    items_html = "".join(feed_item(it, base="../") for it in items) or "<p>这个标签下暂时没有条目。</p>"
    return (page_open(site, "topics", f'{site["title"]} · #{label}',
                      f"关于「{label}」的独立游戏开发者动态。", prefix="index.html", base="../",
                      canonical=site.get("url", "") + "/topics/" + slug + ".html",
                      ld_items=items)
            + f'<div class="m-title">#{esc(label)}</div>'
            + f'<h1>#{esc(label)}</h1>'
            + f'<p class="lede">共 {len(items)} 条 · <a href="../topics.html">全部主题 →</a></p>'
            + items_html
            + page_close(site, "topics", prefix="index.html", base="../"))


def render_topics_index(site, tag_counts):
    """主题索引（B11）：全部标签 + 条数。"""
    rows = "".join(
        f'<a class="more-row" href="topics/{slug}.html">{icon("rss", 18)}'
        f'<span class="m-label">#{esc(label)}</span>'
        f'<span class="m-val">{n} 条</span><span class="m-arrow">›</span></a>'
        for label, slug, n in tag_counts)
    return (page_open(site, "topics", f'{site["title"]} · 主题',
                      "按主题浏览独立游戏开发者动态。", prefix="index.html",
                      canonical=site.get("url", "") + "/topics.html")
            + f'<div class="m-title">主题</div>'
            + f'<h1>主题</h1>'
            + f'<p class="lede">按标签浏览：点一个标签，看它下面最近 30 天的动态。</p>'
            + (f'<section class="more-sec">{rows}</section>' if rows else "<p>暂无标签。</p>")
            + page_close(site, "topics", prefix="index.html"))


def render_simple_page(site, active, title, lede, blocks, canonical_path):
    """静态文案页（B5 关于 / C4 条款与隐私）共用骨架。"""
    body = ""
    for h, paras in blocks:
        ps = "".join(f"<p>{p}</p>" for p in paras)
        body += (f'<h2 class="sec">{esc(h)}</h2>' if h else "") + ps
    return (page_open(site, active, f'{site["title"]} · {title}',
                      site["description"], prefix="index.html",
                      canonical=site.get("url", "") + "/" + canonical_path)
            + f'<div class="m-title">{esc(title)}</div>'
            + f'<h1>{esc(title)}</h1>'
            + f'<p class="lede">{esc(lede)}</p>'
            + body
            + page_close(site, active, prefix="index.html"))


def render_about(site):
    return render_simple_page(site, "about", "关于", "IndieHOT 是什么、怎么做、谁做的。", [
        ("定位", ["IndieHOT 是给独立游戏开发者看的热点站——只收对做游戏有用的东西："
                  "引擎与工具、发行与宣发、开发日志、行业与融资、展会与活动。"]),
        ("怎么做", ["采集——公开 RSS 信源，每半小时拉一次。",
                    "打分——规则打分（时效、开发相关度、来源），不是 AI 打分。",
                    "精选——每天 06:00（北京时间）出日报，每半小时更新。",
                    "出报——只展示标题、摘要与原文链接，不转载全文。"]),
        ("数据说明", ["内容来自各家公开 RSS，版权归原作者；翻译是我们自己做的中文标题与摘要，原文以链接为准。"]),
        ("制作人", ["Camille，独立游戏开发者（正在做太空贸易游戏《星穹商旅》）。"
                    "本站界面设计参考了卡兹克的开源项目 AIHOT（MIT 协议），品牌、内容与实现均为原创。"]),
        ("联系", ["有问题或想推荐信源，去 GitHub Issues 留言。"]),
    ], "about.html")


def render_terms(site):
    return render_simple_page(site, "terms", "条款", "使用本站前请先了解这些。", [
        ("", ["本站为信息聚合，内容来自各家公开 RSS；站内只展示标题、摘要与原文链接，不转载全文。",
              "内容版权归原作者所有。",
              "如有内容不妥或希望下架，请通过 GitHub Issues 联系，我们会尽快处理。"]),
    ], "terms.html")


def render_privacy(site):
    return render_simple_page(site, "privacy", "隐私", "本站如何对待你的数据。", [
        ("", ["本站不收集、不上传任何个人信息。",
              "收藏与已读记录只保存在你自己的浏览器里（localStorage），清除浏览器数据即清除。"]),
    ], "privacy.html")


def render_item(site, it, related, is_selected, daily_issue):
    """文章详情页（items/<id>.html）：只有标题、一句话简介、来源、评分、理由、标签、
    相关条目——不编造正文，不转载全文，不展示源站摘要或完整摘要原文。
    导读就是本站自己写的一句话简介（zh_oneliner）；没有简介时只显示标题与链接，
    原文只通过“打开原文”按钮出去。"""
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")
    title = it.get("zh_title") or it["title"]
    en_title = it["title"] if (it.get("zh_title") and it["zh_title"] != it["title"]) else ""
    oneliner = it.get("zh_oneliner") or ""
    desc = oneliner if oneliner else title
    pub = _pub_dt(it)
    domain = urllib.parse.urlparse(it["link"]).netloc
    canonical = f"{site_url}/items/{it['id']}.html"
    mid_name = "精选" if is_selected else "全部动态"
    mid_href = "/" if is_selected else "/all.html"
    ld_blocks = [
        {"@context": "https://schema.org", "@type": "Article",
         "headline": title, "description": desc,
         "datePublished": pub.astimezone(timezone.utc).isoformat(),
         "author": {"@type": "Organization", "name": it["source"]},
         "publisher": {"@type": "Organization", "name": "IndieHOT"},
         "mainEntityOfPage": canonical},
        {"@context": "https://schema.org", "@type": "BreadcrumbList",
         "itemListElement": [
             {"@type": "ListItem", "position": 1, "name": "首页",
              "item": site_url + "/"},
             {"@type": "ListItem", "position": 2, "name": mid_name,
              "item": site_url + mid_href},
             {"@type": "ListItem", "position": 3, "name": title,
              "item": canonical}]},
    ]
    meta = (f'<span class="src">{esc(it["source"])}</span>'
            + ('<span class="off-tag">官方</span>' if it.get("tier") == "T1" else "")
            + f'<time>· {esc(fmt_cst(pub))}</time>'
            + ('<span class="pick">精选</span>' if is_selected else "")
            + f'<span class="sp">{score_pill(it["score"])}</span>')
    en_html = (f'<p class="item-en-title">原文标题：{esc(en_title)}</p>' if en_title else "")
    # 导读：只显示本站自己写的一句话简介；没有简介时不展示任何源站摘要文字。
    guide_text = oneliner
    reason_html = (f'<section class="item-sec"><h2>推荐理由</h2>'
                   f'<p>{esc(it.get("reason", ""))}</p></section>' if it.get("reason") else "")
    tags_html = ""
    tags = it.get("tags") or []
    if tags:
        tags_html = ('<section class="item-sec"><h2>主题</h2><div class="fi-tags">' + "".join(
            f'<a class="tag" href="../topics/{TAG_SLUGS.get(t, t)}.html">#{esc(t)}</a>'
            for t in tags[:3]) + '</div></section>')
    issue_html = ""
    if daily_issue:
        issue_html = (f'<p class="item-source-line">收录于'
                      f'<a href="../daily/{daily_issue["key"]}.html">日报第 {daily_issue["issue"]} 期'
                      f'（{esc(daily_issue["date"])}）</a></p>')
    related_html = ""
    if related:
        cards = "".join(feed_item(r, base="../") for r in related)
        related_html = (f'<section class="item-sec"><h2>相关条目</h2>'
                        f'<div class="item-related-list">{cards}</div></section>')
    return (page_open(site, "all", f"{title} · IndieHOT", desc,
                      prefix="index.html", base="../", canonical=canonical,
                      page_cls=" page-reading", ld_blocks=ld_blocks)
            + f'<article class="item-page" data-item-id="{esc(it["id"])}">'
            + f'<button type="button" class="item-back" id="backBtn" data-home="../index.html">← 返回</button>'
            + f'<div class="item-meta">{meta}</div>'
            + f'<h1 class="item-h1">{esc(title)}</h1>'
            + en_html
            + f'<div class="well item-guide"><span class="kicker">导读</span>'
            + (f'<p>{esc(guide_text)}</p>' if guide_text else '<p>这条动态暂无简介，点“打开原文”查看。</p>')
            + '</div>'
            + reason_html
            + f'<div class="item-actions">'
            f'<a class="item-open-btn" href="{esc(it["link"])}" target="_blank" rel="noopener">打开原文 ↗</a>'
            f'<button type="button" class="item-ghost-btn" id="itemStarBtn" data-id="{esc(it["id"])}" '
            f'data-title="{esc(title)}" data-link="{esc(it["link"])}" data-source="{esc(it["source"])}">'
            f'{icon("bookmark", 16)}<span>收藏</span></button>'
            f'<button type="button" class="item-ghost-btn" id="copyLinkBtn">复制链接</button>'
            f'<button type="button" class="item-ghost-btn" id="shareBtn" hidden>分享</button>'
            f'</div>'
            + f'<p class="item-source-line">来源：<b>{esc(it["source"])}</b> · {esc(domain)}</p>'
            + issue_html
            + tags_html
            + related_html
            + '</article>'
            + page_close(site, "all", prefix="index.html", base="../"))


def render_404(site):
    """GitHub Pages 缺页处理：简单独立页（链接用绝对地址，任何缺页路径下都可用）。"""
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")
    return (f"<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>找不到这篇内容 · IndieHOT</title>\n"
            f"<meta name=\"description\" content=\"找不到这篇内容。\">\n"
            f'<link rel="icon" type="image/svg+xml" href="{site_url}/favicon.svg">\n'
            f"{THEME_BOOT}<link rel=\"stylesheet\" href=\"{site_url}/assets/site.css\"></head>\n<body>"
            f'<div class="shell"><div class="main"><div class="page page-reading">'
            f'<div class="empty-state" style="margin-top:20vh">'
            f'<div class="empty-icon">{icon("search", 40)}</div>'
            f'<h3 class="empty-title">找不到这篇内容</h3>'
            f'<p class="empty-text">链接可能有误，或者这篇内容已经下架。</p>'
            f'<div class="empty-actions">'
            f'<a class="empty-btn primary" href="{site_url}/index.html">回首页</a>'
            f'<a class="empty-btn" href="{site_url}/hot.html">去看热点榜</a>'
            f'</div></div></div></div></div></body></html>\n')


def render_hot(site, hot, generated_at, counts, cfg):
    """完整热点榜：No.1 头条大卡 + No.2–3 次卡 + No.4–20 带 Delta 的行 + 方法说明（B1）。"""
    sc = cfg["scoring"]
    hero = ""
    subs = ""
    rest = ""
    if hot:
        h0 = hot[0]
        latest = h0.get("zh_oneliner") or ""
        hero = (
            f'<article class="card hot-hero"><div class="hot-hero-top">'
            f'<span class="no-tag">NO.01</span>{hot_badges(h0)}</div>'
            f'<h2><a href="items/{esc(h0["id"])}.html">'
            f'{esc(h0.get("zh_title") or h0["title"])}</a></h2>'
            f'<p class="hot-hero-meta">{esc(h0["source"])} · {esc(fmt_cst(_pub_dt(h0)))} · '
            f'{esc(h0["category_label"])}</p>'
            + (f'<p class="hot-hero-latest"><span class="k">最新进展</span>{esc(shorten(latest, 110))}</p>'
               if latest else "")
            + f'<div class="hot-hero-foot"><span class="hot-big"><b class="num">{round(h0["hot_score"])}</b>热度</span>'
            f'{hot_delta_html(h0)}{score_pill(h0["score"])}</div></article>')
    if len(hot) > 1:
        sub_cards = ""
        for i, it in enumerate(hot[1:3], start=2):
            sub_cards += (
                f'<article class="card hot-sub"><div class="hot-sub-top">'
                f'<span class="no-tag">NO.{i:02d}</span>{hot_badges(it)}</div>'
                f'<h3><a href="items/{esc(it["id"])}.html">'
                f'{esc(it.get("zh_title") or it["title"])}</a></h3>'
                f'<p class="hot-sub-meta">{esc(it["source"])} · {esc(fmt_cst(_pub_dt(it)))}</p>'
                f'<div class="hot-sub-foot"><span class="hot-num"><b class="num">{round(it["hot_score"])}</b> 热度</span>'
                f'{hot_delta_html(it)}</div></article>')
        subs = f'<div class="hot-sub-grid">{sub_cards}</div>'
    if len(hot) > 3:
        rows = "".join(hot_row(it, i + 1, pad=True) for i, it in enumerate(hot[3:], start=3))
        rest = (f'<section class="card hot-card" aria-labelledby="hot-rest" style="margin-top:12px">'
                f'<ol class="hot-list">{rows}</ol></section>')
    elif not hot:
        rest = "<p>暂无热点。</p>"
    rec = sc["recency"]
    rec_txt = "、".join(f'{r["max_hours"]} 小时内 +{r["bonus"]}' for r in rec)
    tier_txt = f'官方信源 +{sc["tier_bonus"].get("T1", 0)}'
    if "T3" in sc["tier_bonus"]:
        tier_txt += f'；综合游戏媒体 {sc["tier_bonus"]["T3"]}'
    method = (
        f'<section class="well hot-method"><span class="kicker">热度是怎么算的？</span>'
        f'<p>每条动态先按规则打分：基础 {sc["base"]} 分；新鲜度（{rec_txt}）；'
        f'{tier_txt}；命中开发者关键词（教程、工具、数据、开源、指南、复盘、引擎、优化、性能、营收、销量、经验、源码、工作流）'
        f'每个 +{sc["dev_keyword_bonus"]}、上限 +{sc["dev_keyword_cap"]}；噪音关键词每个 {sc["noise_keyword_penalty"]}、'
        f'下限 {sc["noise_keyword_floor"]}；总分封顶 100 分。热度 = 评分 × {sc["decay_base"]}'
        f'^天数（每天衰减），只看最近 {sc["hot_days"]} 天。趋势对照上一轮榜单：名次上升 2 名以上为上升，'
        f'下降 2 名以上为回落，只挪 1 名算持平；首次上榜标“新”。</p></section>')
    return (page_open(site, "hot", f'{site["title"]} · 热点榜',
                      site["description"], prefix="index.html",
                      canonical=site.get("url", "") + "/hot.html", ld_items=hot)
            + f'<div class="m-title">热点榜</div>'
            + f'<h1>热点榜</h1>'
            + f'<p class="lede">更新于 {esc(generated_at)}（北京时间）<span class="lede-rest"> · 最近 7 天共 {counts["total_7d"]} 条 · '
            f'热度按评分随时间衰减，趋势对照上一轮榜单。</span></p>'
            + hero + subs + rest + method
            + page_close(site, "hot", prefix="index.html"))


def rss_date(dt):
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def build_feed_xml(site, items):
    """RSS 2.0（B12）：精选 20 条，每轮都写。"""
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")
    now_rfc = rss_date(datetime.now(timezone.utc))
    chan_title = site.get("title", "IndieHOT")
    chan_desc = site.get("description", "")
    items_xml = ""
    for it in items[:20]:
        title = it.get("zh_title") or it["title"]
        desc = it.get("zh_oneliner") or ""
        pub = _pub_dt(it)
        items_xml += ("<item><title>" + esc(title) + "</title>"
                      "<link>" + esc(it["link"]) + "</link>"
                      "<description>" + esc(desc) + "</description>"
                      "<pubDate>" + rss_date(pub) + "</pubDate>"
                      "<guid isPermaLink=\"false\">" + esc(it["id"]) + "</guid>"
                      "<source url=\"" + esc(it["link"]) + "\">" + esc(it["source"]) + "</source>"
                      "</item>")
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<rss version=\"2.0\"><channel>"
            "<title>" + esc(chan_title) + "</title>"
            "<link>" + esc(site_url) + "/</link>"
            "<description>" + esc(chan_desc) + "</description>"
            "<language>zh-CN</language>"
            "<lastBuildDate>" + now_rfc + "</lastBuildDate>"
            + items_xml +
            "</channel></rss>\n")


def load_period_archive(dirpath):
    """读回某类（daily/weekly/monthly）已存档的报告，按 date_iso 倒序。"""
    reports = []
    if not os.path.isdir(dirpath):
        return reports
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith(".json"):
            continue
        try:
            r = load_json(os.path.join(dirpath, fn))
            if r.get("date_iso"):
                reports.append(r)
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    reports.sort(key=lambda r: r["date_iso"], reverse=True)
    return reports


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="同时产出当日日报")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = args.root
    cfg = load_json(os.path.join(root, "config.json"))
    sources = load_json(os.path.join(root, "sources.json"))["sources"]
    site = cfg["site"]
    cats = cfg["categories"]
    cat_label = {c["key"]: c["label"] for c in cats}
    sc = cfg["scoring"]
    now = datetime.now(timezone.utc)
    now_cst = now.astimezone(CST)

    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 历史（增量更新：新条目合并进来，旧条目按时间淘汰，不全量覆盖丢历史）
    # 本地库 data/store.json（不推送；推送清单里没有它）。老版 data/items.json
    # 只在首次迁移时读一次，之后不再读写。
    hist_path = os.path.join(data_dir, "store.json")
    history = {}
    if os.path.exists(hist_path):
        try:
            for it in load_json(hist_path).get("items", []):
                history[it["id"]] = it
        except (json.JSONDecodeError, KeyError):
            print("store.json damaged, starting fresh", file=sys.stderr)
    elif os.path.exists(os.path.join(data_dir, "items.json")):
        try:
            for it in load_json(os.path.join(data_dir, "items.json")).get("items", []):
                history[it["id"]] = it
            print("migrated history from data/items.json to data/store.json")
        except (json.JSONDecodeError, KeyError):
            print("items.json damaged, starting fresh", file=sys.stderr)

    src_by_id = {s["id"]: s for s in sources}

    def enrich_store_item(it):
        """给历史条目补上与时间无关的派生字段（B16/B2 供 JS 动态渲染用）：
        信源分级、分类、中文标签。"""
        it["tier"] = src_by_id.get(it.get("source_id", ""), {}).get("tier", "T2")
        it["category"] = classify(it, cats,
                                  src_by_id.get(it.get("source_id", ""), {}).get(
                                      "category_hint", "industry"))
        it["category_label"] = cat_label.get(it["category"], it["category"])
        it["tags"] = labels_for(it, cfg)[:3]

    for _it in history.values():  # 老条目回填（以前的 items.json 没有这些字段）
        if "tags" not in _it or "tier" not in _it or "category" not in _it:
            enrich_store_item(_it)
        # 分级跟随 sources.json 实时刷新：改了某源的 tier，历史条目下一轮立即生效，
        # 否则旧条目会一直沿用首次收录时的分级（2026-11-11 把关发现 T3 改完旧条目仍按 T2 打分）
        _it["tier"] = src_by_id.get(_it.get("source_id", ""), {}).get("tier", "T2")

    fetched, failed = 0, []
    # 中文翻译（data/zh_draft.json，按规范化链接匹配；每日精选把关时更新草稿，
    # 下一轮生成自动合入并写回 store.json，Actions 重生成也不会丢）
    zh_path = os.path.join(data_dir, "zh_draft.json")
    if os.path.exists(zh_path):
        try:
            zh = load_json(zh_path)
            applied = 0
            for it in history.values():
                tr = zh.get(norm_link(it.get("link", "")))
                if not tr:
                    continue
                if tr.get("zh_title"):
                    it["zh_title"] = tr["zh_title"]
                if tr.get("zh_oneliner"):
                    it["zh_oneliner"] = tr["zh_oneliner"]
                applied += 1
            if applied:
                print(f"zh: {applied} items matched from zh_draft.json")
        except (json.JSONDecodeError, AttributeError) as e:
            print("zh_draft.json skipped (%s)" % e, file=sys.stderr)
    for src in sources:
        try:
            body = fetch(src["url"])
            new_items = parse_feed(body, src)
        except Exception as e:  # noqa: BLE001 - 单个信源失败不中断整轮
            failed.append((src["id"], str(e)[:80]))
            continue
        fetched += len(new_items)
        for it in new_items:
            pub = it["published"] or now
            it["published"] = pub.astimezone(timezone.utc).isoformat()
            it["id"] = hashlib.sha1(norm_link(it["link"]).encode()).hexdigest()
            if it["id"] in history:
                continue  # 已收录：去重
            entry = {
                "id": it["id"], "title": it["title"], "link": it["link"],
                "source": it["source"], "source_id": it["source_id"],
                "published": it["published"], "summary": it["summary"],
                "fetched_at": now.isoformat(),
            }
            if it.get("summary_full"):
                entry["summary_full"] = it["summary_full"]
            enrich_store_item(entry)
            history[it["id"]] = entry
    # 淘汰超过 history_days 的旧条目
    cutoff_hist = now - timedelta(days=sc["history_days"])
    history = {k: v for k, v in history.items()
               if datetime.fromisoformat(v["published"]) >= cutoff_hist}
    save_json(hist_path, {"updated_at": now.isoformat(), "items": list(history.values())})

    # 分类与打分（在内存里算，不写回历史文件，保持历史干净）
    live = []
    for it in history.values():
        pub = datetime.fromisoformat(it["published"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        item = dict(it)
        item["_pub"] = pub
        item["_tier"] = it.get("tier") or src_by_id.get(it["source_id"], {}).get("tier", "T2")
        item["category"] = it.get("category") or classify(
            item, cats, src_by_id.get(it["source_id"], {}).get("category_hint", "industry"))
        item["category_label"] = cat_label.get(item["category"], item["category"])
        item["tags"] = it.get("tags") or labels_for(item, cfg)[:3]
        item["score"] = score(item, cfg, now)
        item["reason"] = reason_for(item, cfg, now)
        # 当日新到：今天首次被本站收录，且本身也是 24 小时内发布的
        # （只按收录日期会把建站日批量回填的旧文也标上“新”）
        fa = it.get("fetched_at")
        is_new = False
        if fa:
            try:
                fdt = datetime.fromisoformat(fa)
                if fdt.tzinfo is None:
                    fdt = fdt.replace(tzinfo=timezone.utc)
                is_new = (fdt.astimezone(CST).date() == now_cst.date()
                          and (now - item["_pub"]) <= timedelta(hours=24))
            except ValueError:
                pass
        item["is_new"] = is_new
        live.append(item)

    # 可选 LLM 重排（无 Key 时直接跳过）
    live.sort(key=lambda x: x["score"], reverse=True)
    live = maybe_llm_rerank(live)

    recent_48h = [it for it in live if (now - it["_pub"]) <= timedelta(hours=sc["selected_hours"])]
    selected = [it for it in recent_48h if it["score"] >= sc["selected_min"]]
    selected.sort(key=lambda x: x["score"], reverse=True)
    selected = dedup_topics(selected)[:sc["daily_top"]]

    last_7d = [it for it in live if (now - it["_pub"]) <= timedelta(days=sc["hot_days"])]
    for it in last_7d:
        age_days = (now - it["_pub"]).total_seconds() / 86400.0
        it["hot_score"] = round(it["score"] * (sc["decay_base"] ** age_days), 1)
    hot = dedup_topics(sorted(last_7d, key=lambda x: x["hot_score"], reverse=True))[:sc["hot_top"]]
    timeline = sorted(last_7d, key=lambda x: x["_pub"], reverse=True)[:sc["timeline_max"]]

    # 趋势：对照上一轮已发布的榜单名次与热度（读旧 data/hot.json，写新文件之前）
    prev_ranks = {}
    prev_scores = {}
    old_hot_path = os.path.join(data_dir, "hot.json")
    if os.path.exists(old_hot_path):
        try:
            for i, h in enumerate(load_json(old_hot_path).get("hot", [])):
                prev_ranks[h["id"]] = i + 1
                prev_scores[h["id"]] = h.get("hot_score")
        except (json.JSONDecodeError, KeyError):
            pass
    for i, it in enumerate(hot):
        r = i + 1
        prev = prev_ranks.get(it["id"])
        it["prev_rank"] = prev
        it["prev_hot_score"] = prev_scores.get(it["id"])
        if prev is None:
            it["trend"] = "new"
        elif prev - r >= 2:
            it["trend"] = "up"
        elif r - prev >= 2:
            it["trend"] = "down"
        else:
            it["trend"] = "same"  # 只挪 1 名算持平，免得 4 小时一轮的抖动全画成箭头

    def pub_item(it):
        return {"id": it["id"], "title": it["title"], "link": it["link"],
                "source": it["source"], "source_id": it["source_id"],
                "published": it["published"],
                "zh_title": it.get("zh_title", ""),
                "zh_oneliner": it.get("zh_oneliner", ""),
                "category": it["category"], "category_label": it["category_label"],
                "tier": it.get("_tier", "T2"), "tags": it.get("tags", []),
                "score": it["score"],
                "reason": it.get("reason", ""), "is_new": bool(it.get("is_new", False)),
                "trend": it.get("trend", "same"),
                "hot_score": it.get("hot_score", it["score"]),
                "prev_rank": it.get("prev_rank"),
                "prev_hot_score": it.get("prev_hot_score")}

    gen_cst = now_cst.strftime("%Y年%m月%d日 %H:%M")
    # hot.json 只写最终发布的热点榜 Top 20（7 天候选只在内存里，不落盘）
    hot_data = {
        "generated_at": now.isoformat(),
        "generated_at_cst": gen_cst,
        "counts": {"total_7d": len(last_7d), "selected": len(selected),
                   "hot": len(hot), "timeline": len(timeline)},
        "hot": [pub_item(it) for it in hot],
    }
    save_json(os.path.join(data_dir, "hot.json"), hot_data, compact=True)

    # 客户端数据按天分块（data/days/）：首页分页与搜索按需取，不再发 30 天全量文件
    def chunk_item(it):
        return {"id": it["id"], "title": it["title"],
                "zh_title": it.get("zh_title", ""),
                "zh_oneliner": it.get("zh_oneliner", ""),
                "link": it["link"], "source": it["source"],
                "source_id": it.get("source_id", ""),
                "published": it["published"],
                "score": it["score"], "tags": it.get("tags", []),
                "tier": it.get("_tier", "T2"), "category": it["category"],
                "category_label": it["category_label"]}
    days_dir = os.path.join(data_dir, "days")
    os.makedirs(days_dir, exist_ok=True)
    by_day = {}
    for it in live:
        d = it["_pub"].astimezone(CST).date().isoformat()
        by_day.setdefault(d, []).append(it)
    day_index = []
    for d in sorted(by_day, reverse=True):
        items = sorted(by_day[d], key=lambda x: x["_pub"], reverse=True)
        save_json(os.path.join(days_dir, d + ".json"),
                  {"date": d, "items": [chunk_item(it) for it in items]},
                  compact=True)
        day_index.append({"date": d, "count": len(items)})
    save_json(os.path.join(days_dir, "index.json"),
              {"updated_at": now.isoformat(), "days": day_index}, compact=True)
    # 清掉已出 30 天窗口的旧分块（index.json 不再列出它们）
    for fn in os.listdir(days_dir):
        if fn.endswith(".json") and fn != "index.json" and fn[:-5] not in by_day:
            os.remove(os.path.join(days_dir, fn))
    print(f"days: {len(day_index)} day files, "
          f"{sum(x['count'] for x in day_index)} items")

    # 共享 CSS/JS 外置（assets/）：每页改引相对路径，不再内嵌整份
    assets_dir = os.path.join(root, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    with open(os.path.join(assets_dir, "site.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    with open(os.path.join(assets_dir, "site.js"), "w", encoding="utf-8") as f:
        f.write(SCRIPT_JS)
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")

    # 主页与热点榜
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(site, cats, cfg, src_by_id, selected, hot, timeline,
                             gen_cst, hot_data["counts"]))
    # 全部动态页（对标 AIHOT /all）：30 天全量按时间倒序
    with open(os.path.join(root, "all.html"), "w", encoding="utf-8") as f:
        f.write(render_all(site, cats, cfg, src_by_id, selected, hot,
                           sorted(live, key=lambda x: x["_pub"], reverse=True),
                           gen_cst))
    with open(os.path.join(root, "hot.html"), "w", encoding="utf-8") as f:
        f.write(render_hot(site, hot, gen_cst, hot_data["counts"], cfg))

    # 静态页：收藏 / 我的 / 更新日志 / 关于 / 条款 / 隐私（每轮都重写，内容不依赖抓取）
    changelog = []
    changelog_path = os.path.join(data_dir, "changelog.json")
    if os.path.exists(changelog_path):
        try:
            changelog = load_json(changelog_path).get("entries", [])
        except (json.JSONDecodeError, AttributeError):
            changelog = []
    changelog_version = changelog[0]["version"] if changelog else ""
    with open(os.path.join(root, "starred.html"), "w", encoding="utf-8") as f:
        f.write(render_starred(site))
    with open(os.path.join(root, "more.html"), "w", encoding="utf-8") as f:
        f.write(render_more(site, changelog_version))
    with open(os.path.join(root, "changelog.html"), "w", encoding="utf-8") as f:
        f.write(render_changelog(site, changelog))
    with open(os.path.join(root, "about.html"), "w", encoding="utf-8") as f:
        f.write(render_about(site))
    with open(os.path.join(root, "terms.html"), "w", encoding="utf-8") as f:
        f.write(render_terms(site))
    with open(os.path.join(root, "privacy.html"), "w", encoding="utf-8") as f:
        f.write(render_privacy(site))
    with open(os.path.join(root, "404.html"), "w", encoding="utf-8") as f:
        f.write(render_404(site))

    # 主题页（B11）：按标签从 30 天全量里取条目，每标签一页 + 索引页
    tag_items = {}
    for it in live:
        for t in it.get("tags", []):
            tag_items.setdefault(t, []).append(it)
    tag_counts = []
    topics_dir = os.path.join(root, "topics")
    os.makedirs(topics_dir, exist_ok=True)
    for label in sorted(tag_items, key=lambda l: -len(tag_items[l])):
        items = sorted(tag_items[label], key=lambda x: x["_pub"], reverse=True)[:50]
        slug = TAG_SLUGS.get(label, label)
        tag_counts.append((label, slug, len(tag_items[label])))
        with open(os.path.join(topics_dir, slug + ".html"), "w", encoding="utf-8") as f:
            f.write(render_topic(site, label, slug, [pub_item(it) for it in items]))
    with open(os.path.join(root, "topics.html"), "w", encoding="utf-8") as f:
        f.write(render_topics_index(site, tag_counts))

    # RSS / robots / sitemap / favicon（C5；每轮都写）
    feed_items = sorted(recent_48h, key=lambda x: x["score"], reverse=True)[:20]
    with open(os.path.join(root, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(build_feed_xml(site, [pub_item(it) for it in feed_items]))
    with open(os.path.join(root, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nSitemap: {site_url}/sitemap.xml\n")
    with open(os.path.join(root, "favicon.svg"), "w", encoding="utf-8") as f:
        f.write(LOGO_SVG)
    sitemap_urls = ["", "all.html", "hot.html", "daily.html", "weekly.html", "monthly.html",
                    "starred.html", "more.html", "changelog.html", "about.html",
                    "terms.html", "privacy.html", "topics.html",
                    "daily/archive.html", "feed.xml"]
    sitemap_urls += ["topics/" + slug + ".html" for _l, slug, _n in tag_counts]
    sitemap_urls += ["items/" + it_id + ".html" for it_id in history]
    today_iso = now_cst.strftime("%Y-%m-%d")
    sm = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
          "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"]
    for p in sitemap_urls:
        sm.append(f"<url><loc>{site_url}/{p}</loc><lastmod>{today_iso}</lastmod></url>")
    sm.append("</urlset>")
    with open(os.path.join(root, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sm) + "\n")
    print(f"fetch: {fetched} items parsed from {len(sources) - len(failed)}/{len(sources)} sources"
          + (f"; failed: {failed}" if failed else ""))
    print(f"history: {len(history)} items; 7d: {len(last_7d)}; "
          f"selected: {len(selected)}; hot: {len(hot)}; timeline: {len(timeline)}; "
          f"topics: {len(tag_counts)}")

    if args.daily:
        def build_period(kind, pool, window_label):
            pool_sorted = sorted(pool, key=lambda x: x["score"], reverse=True)
            items = dedup_topics(pool_sorted)[:sc["daily_top"]]
            per_cat = {}
            for it in items:
                per_cat[it["category_label"]] = per_cat.get(it["category_label"], 0) + 1
            cat_part = "、".join(f"{k} {v} 条" for k, v in per_cat.items()) or "无"
            head = (f"头条：{items[0].get('zh_title') or items[0]['title']}"
                    if items else "暂无精选")
            date_str = now_cst.strftime("%Y年%m月%d日")
            date_iso = now_cst.strftime("%Y-%m-%d")
            if kind == "daily":
                key = date_iso
                label = date_str
            elif kind == "weekly":
                iso_y, iso_w, _ = now_cst.isocalendar()
                key = f"{iso_y}-W{iso_w:02d}"
                label = f"{iso_y}年第{iso_w}周"
            else:
                key = now_cst.strftime("%Y-%m")
                label = now_cst.strftime("%Y年%m月")
            return {
                "kind": kind, "key": key, "label": label,
                "date": date_str, "date_iso": date_iso,
                "issue": issue_no(kind, date_iso),
                "generated_at": now.isoformat(),
                "summary": (f"{window_label}共收录 {len(pool)} 条动态，精选 "
                            f"{len(items)} 条（{cat_part}）。{head}。"),
                "items": [pub_item(it) for it in items],
            }

        def render_archived(kind, reports, out_dir):
            """渲染某类全部存档页（带上一期/下一期）；out_dir 为 daily/weekly/monthly。"""
            os.makedirs(os.path.join(root, out_dir), exist_ok=True)
            for i, rep in enumerate(reports):
                prev = None
                nxt = None
                if i + 1 < len(reports):
                    prev = (reports[i + 1]["label"], reports[i + 1]["key"] + ".html")
                if i > 0:
                    nxt = (reports[i - 1]["label"], reports[i - 1]["key"] + ".html")
                with open(os.path.join(root, out_dir, rep["key"] + ".html"),
                          "w", encoding="utf-8") as f:
                    f.write(render_period(site, kind, rep, base="../", prev=prev,
                                          next_=nxt,
                                          canonical=f"{site_url}/{out_dir}/{rep['key']}.html"))

        def current_page(kind, cur, reports, out_dir):
            """当前页（daily.html/weekly.html/monthly.html，站根）：上一期指向上一个存档。"""
            prev = None
            older = [r for r in reports if r["key"] != cur["key"]]
            if older:
                prev = (older[0]["label"], f"{out_dir}/{older[0]['key']}.html")
            with open(os.path.join(root, kind + ".html"), "w", encoding="utf-8") as f:
                f.write(render_period(site, kind, cur, base="", prev=prev, next_=None,
                                      canonical=f"{site_url}/{kind}.html"))

        # --- 日报（每天一份存档）
        last_24h = [it for it in live if (now - it["_pub"]) <= timedelta(hours=sc["daily_hours"])]
        daily = build_period("daily", last_24h, "过去 24 小时")
        save_json(os.path.join(data_dir, "daily.json"), daily)
        daily_data_dir = os.path.join(data_dir, "daily")
        os.makedirs(daily_data_dir, exist_ok=True)
        save_json(os.path.join(daily_data_dir, daily["key"] + ".json"), daily)
        daily_reports = load_period_archive(daily_data_dir)
        render_archived("daily", daily_reports, "daily")
        current_page("daily", daily, daily_reports, "daily")
        print(f"daily: {daily['date']}, {len(daily['items'])} items, 第 {daily['issue']} 期")

        # --- 周报（滚动 7 天精选；每周只存一份快照）
        last_7d_sorted = sorted(last_7d, key=lambda x: x["score"], reverse=True)
        weekly = build_period("weekly", last_7d_sorted, "过去 7 天")
        save_json(os.path.join(data_dir, "weekly.json"), weekly)
        weekly_data_dir = os.path.join(data_dir, "weekly")
        os.makedirs(weekly_data_dir, exist_ok=True)
        if not os.path.exists(os.path.join(weekly_data_dir, weekly["key"] + ".json")):
            save_json(os.path.join(weekly_data_dir, weekly["key"] + ".json"), weekly)
        weekly_reports = load_period_archive(weekly_data_dir)
        render_archived("weekly", weekly_reports, "weekly")
        current_page("weekly", weekly, weekly_reports, "weekly")
        print(f"weekly: {weekly['label']}, {len(weekly['items'])} items, 第 {weekly['issue']} 期")

        # --- 月报（滚动 30 天精选；每月只存一份快照）
        last_30d = [it for it in live if (now - it["_pub"]) <= timedelta(days=30)]
        monthly = build_period("monthly", last_30d, "过去 30 天")
        save_json(os.path.join(data_dir, "monthly.json"), monthly)
        monthly_data_dir = os.path.join(data_dir, "monthly")
        os.makedirs(monthly_data_dir, exist_ok=True)
        if not os.path.exists(os.path.join(monthly_data_dir, monthly["key"] + ".json")):
            save_json(os.path.join(monthly_data_dir, monthly["key"] + ".json"), monthly)
        monthly_reports = load_period_archive(monthly_data_dir)
        render_archived("monthly", monthly_reports, "monthly")
        current_page("monthly", monthly, monthly_reports, "monthly")
        print(f"monthly: {monthly['label']}, {len(monthly['items'])} items, 第 {monthly['issue']} 期")

        # --- 往期存档页（B8/B9）：日报按月分组；周报、月报各列一组
        def arch_entry(rep, file_href, date_short):
            return {"date_iso": rep["date_iso"], "date_short": date_short,
                    "file": file_href, "issue": rep["issue"],
                    "count": len(rep["items"])}
        d_entries = [arch_entry(r, r["key"] + ".html",
                                datetime.strptime(r["date_iso"], "%Y-%m-%d").strftime("%m月%d日"))
                     for r in daily_reports]
        w_entries = [arch_entry(r, "../weekly/" + r["key"] + ".html", r["label"])
                     for r in weekly_reports]
        m_entries = [arch_entry(r, "../monthly/" + r["key"] + ".html", r["label"])
                     for r in monthly_reports]
        os.makedirs(os.path.join(root, "daily"), exist_ok=True)
        with open(os.path.join(root, "daily", "archive.html"), "w", encoding="utf-8") as f:
            f.write(render_archive(site, d_entries, w_entries, m_entries))
        # sitemap 补上存档页（日报页在 daily/ 下）
        sm_extra = [f"<url><loc>{site_url}/daily/{r['key']}.html</loc>"
                    f"<lastmod>{today_iso}</lastmod></url>" for r in daily_reports]
        sm_extra += [f"<url><loc>{site_url}/weekly/{r['key']}.html</loc>"
                     f"<lastmod>{today_iso}</lastmod></url>" for r in weekly_reports]
        sm_extra += [f"<url><loc>{site_url}/monthly/{r['key']}.html</loc>"
                     f"<lastmod>{today_iso}</lastmod></url>" for r in monthly_reports]
        sm_all = sm[:-1] + sm_extra + ["</urlset>"]
        with open(os.path.join(root, "sitemap.xml"), "w", encoding="utf-8") as f:
            f.write("\n".join(sm_all) + "\n")
        print(f"archive: daily {len(d_entries)}, weekly {len(w_entries)}, monthly {len(m_entries)}")

    # --- 文章详情页（items/<id>.html）：30 天库里的每条内容各一页；
    # 旧条目过期后页面留着当永久链接，不删除。
    daily_issue = {}
    for rep in load_period_archive(os.path.join(data_dir, "daily")):
        for it in rep.get("items", []):
            if it["id"] not in daily_issue:
                daily_issue[it["id"]] = {"issue": rep["issue"],
                                         "date": rep["date"],
                                         "key": rep["key"]}
    sel_ids = {it["id"] for it in selected}
    items_dir = os.path.join(root, "items")
    os.makedirs(items_dir, exist_ok=True)
    by_cat = {}
    by_src = {}
    for it in live:
        by_cat.setdefault(it["category"], []).append(it)
        by_src.setdefault(it["source_id"], []).append(it)
    for lst in list(by_cat.values()) + list(by_src.values()):
        lst.sort(key=lambda x: x["score"], reverse=True)
    n_items = 0
    for it in live:
        picked = []
        for cand in by_cat.get(it["category"], []):
            if cand["id"] != it["id"] and cand not in picked:
                picked.append(cand)
            if len(picked) >= 5:
                break
        if len(picked) < 5:
            for cand in by_src.get(it["source_id"], []):
                if cand["id"] != it["id"] and cand not in picked:
                    picked.append(cand)
                if len(picked) >= 5:
                    break
        # 详情页只用 pub_item 的发布字段（标题 + 一句话简介），不并完整摘要。
        detail = dict(pub_item(it))
        with open(os.path.join(items_dir, it["id"] + ".html"), "w", encoding="utf-8") as f:
            f.write(render_item(site, detail, [pub_item(r) for r in picked],
                                it["id"] in sel_ids,
                                daily_issue.get(it["id"])))
        n_items += 1
    print(f"items: {n_items} detail pages")


if __name__ == "__main__":
    main()
