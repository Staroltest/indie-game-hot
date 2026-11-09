#!/usr/bin/env python3
"""IndieHOT 生成器：拉取 RSS -> 去重 -> 规则打分 -> 生成静态站。

只用 Python 标准库（urllib + xml.etree），不依赖任何外部包，也不依赖
大模型 API 或数据库；数据层就是 data/ 下的 JSON 文件。

流水线（参考 AIHOT 的信息架构：采集 -> 判重 -> 评分 -> 精选 -> 日报，
但全部落成静态文件，供 GitHub Pages + Actions 定时跑）：
  1. 拉取 sources.json 里的公开 RSS/Atom（只读标题+摘要+链接，不转载全文）
  2. 按规范化链接去重，与 data/items.json 里的历史合并（增量更新）
  3. 规则打分：时间新鲜度 + 信源分级 + 开发者关键词（config.json 可调）
  4. 产出 data/hot.json（精选/热点榜/时间线）与 index.html
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


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
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
        pub = parse_date(fields.get("pubDate") or fields.get("published")
                         or fields.get("updated"))
        items.append({
            "title": title,
            "link": link,
            "source": source["name"],
            "source_id": source["id"],
            "published": pub,  # datetime 或 None（调用方补抓取时间）
            "summary": shorten(summary),
        })
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
:root{--ink:#1c2430;--muted:#667085;--line:#e6e9ef;--bg:#fafbfc;--card:#fff;
--accent:#e8590c;--accent2:#f7b733;--tag:#eef2f7}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
color:var(--ink);background:var(--bg);line-height:1.6}
a{color:#0b5fff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:960px;margin:0 auto;padding:0 16px 48px}
header{padding:28px 0 8px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:26px;margin:0;line-height:1.2}
header .sub{color:var(--muted);font-size:14px;margin-top:4px}
nav{margin:14px 0 4px;display:flex;gap:16px;font-size:15px}
nav a{color:var(--ink)}nav a.cur{color:var(--accent);font-weight:700}
h2{font-size:19px;margin:28px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:12px}
.card h3{margin:0 0 4px;font-size:17px;line-height:1.45}
.meta{font-size:13px;color:var(--muted);margin-bottom:6px}
.tag{display:inline-block;background:var(--tag);border-radius:4px;
padding:1px 7px;font-size:12px;margin-right:6px;color:#344054}
.score{float:right;background:#fff4e5;color:var(--accent);border-radius:12px;
padding:1px 9px;font-size:13px;font-weight:700}
.rank{font-size:22px;font-weight:800;color:var(--accent);margin-right:8px}
.filters{margin:10px 0 16px;display:flex;gap:8px;flex-wrap:wrap}
.filters button{border:1px solid var(--line);background:#fff;border-radius:16px;
padding:4px 13px;font-size:14px;cursor:pointer}
.filters button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.tl{list-style:none;margin:0;padding:0}
.tl li{padding:10px 0;border-bottom:1px solid var(--line)}
.tl .t{font-size:15px}
footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);
font-size:13px;color:var(--muted)}
@media(max-width:600px){header h1{font-size:22px}.card h3{font-size:16px}}
"""


def esc(s):
    return html.escape(s or "", quote=True)


def item_card(it, rank=None, show_score=True):
    rank_html = f'<span class="rank">{rank}</span>' if rank else ""
    score_html = f'<span class="score">{it["score"]} 分</span>' if show_score else ""
    pub = it.get("_pub")
    if pub is None:  # daily.json 里的条目只有 ISO 字符串，没有 _pub
        pub = datetime.fromisoformat(it["published"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    return (f'<article class="card" data-category="{esc(it["category"])}">'
            f'{score_html}{rank_html}'
            f'<h3><a href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["title"])}</a></h3>'
            f'<div class="meta"><span class="tag">{esc(it["category_label"])}</span>'
            f'{esc(it["source"])} · {esc(fmt_cst(pub))}</div>'
            + (f'<div>{esc(it["summary"])}</div>' if it.get("summary") else "")
            + "</article>")


def render_index(site, cats, selected, hot, timeline, generated_at, counts):
    cat_opts = "".join(
        f'<button data-filter="{c["key"]}">{esc(c["label"])}</button>' for c in cats)
    sel_html = "".join(item_card(it, rank=i + 1) for i, it in enumerate(selected)) \
        or "<p>今日暂无达到精选门槛的条目。</p>"
    hot_html = "".join(
        f'<li><span class="rank">{i + 1}</span>'
        f'<a href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
        f' <span class="meta">（{esc(it["source"])} · {it["hot_score"]} 分）</span></li>'
        for i, it in enumerate(hot)) or "<li>暂无热点。</li>"
    tl_html = "".join(
        f'<li data-category="{esc(it["category"])}">'
        f'<span class="tag">{esc(it["category_label"])}</span>'
        f'<a class="t" href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
        f'<div class="meta">{esc(it["source"])} · {esc(fmt_cst(it["_pub"]))} · {it["score"]} 分</div>'
        f'<div>{esc(it["summary"])}</div></li>'
        for it in timeline) or "<li>暂无动态。</li>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site["title"])} · 今日精选与热点榜</title>
<meta name="description" content="{esc(site["description"])}">
<style>{CSS}</style></head>
<body><div class="wrap">
<header>{LOGO_SVG}<div><h1>{esc(site["name"])} <span style="color:var(--accent)">独立游戏热点</span></h1>
<div class="sub">{esc(site["tagline"])}</div></div></header>
<nav><a class="cur" href="index.html">首页</a><a href="daily.html">日报</a>
<a href="data/hot.json">数据 JSON</a></nav>
<p class="meta">更新于 {esc(generated_at)}（北京时间）· 最近 7 天共 {counts["total_7d"]} 条 ·
精选 {counts["selected"]} 条 · 只显示标题与摘要，点击标题阅读原文。</p>
<h2>🔥 今日精选</h2>
{sel_html}
<h2>📈 热点榜（近 7 天，按热度衰减排序）</h2>
<ol>{hot_html}</ol>
<h2>📰 全部动态</h2>
<div class="filters"><button data-filter="all" class="on">全部</button>{cat_opts}</div>
<ul class="tl" id="timeline">{tl_html}</ul>
<footer>IndieHOT 是聚合摘要与阅读索引，标题与摘要版权归各来源所有。
内容只作开发者资讯参考；本站不转载全文。信息架构参考 AIHOT 的
采集—评分—精选—日报流水线，代码为本站原创，MIT 协议开源。</footer>
</div>
<script>
document.querySelectorAll('.filters button').forEach(function(b){{
  b.addEventListener('click',function(){{
    document.querySelectorAll('.filters button').forEach(function(x){{x.classList.remove('on')}});
    b.classList.add('on');
    var f=b.getAttribute('data-filter');
    document.querySelectorAll('#timeline li').forEach(function(li){{
      li.style.display=(f==='all'||li.getAttribute('data-category')===f)?'':'none';
    }});
  }});
}});
</script>
</body></html>
"""


def render_daily(site, daily):
    items_html = "".join(item_card(it, rank=i + 1) for i, it in enumerate(daily["items"])) \
        or "<p>过去 24 小时没有达到精选门槛的条目。</p>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site["name"])}日报 · {esc(daily["date"])}</title>
<meta name="description" content="{esc(site["description"])}">
<style>{CSS}</style></head>
<body><div class="wrap">
<header>{LOGO_SVG}<div><h1>{esc(site["name"])}日报</h1>
<div class="sub">{esc(daily["date"])} · 每天 {esc(site["edition_time_cst"])} 出刊</div></div></header>
<nav><a href="index.html">首页</a><a class="cur" href="daily.html">日报</a>
<a href="data/daily.json">数据 JSON</a></nav>
<p>{esc(daily["summary"])}</p>
<h2>今日精选 {len(daily["items"])} 条</h2>
{items_html}
<footer>IndieHOT 是聚合摘要与阅读索引，标题与摘要版权归各来源所有。
内容只作开发者资讯参考；本站不转载全文。</footer>
</div></body></html>
"""


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
    hist_path = os.path.join(data_dir, "items.json")
    history = {}
    if os.path.exists(hist_path):
        try:
            for it in load_json(hist_path).get("items", []):
                history[it["id"]] = it
        except (json.JSONDecodeError, KeyError):
            print("items.json damaged, starting fresh", file=sys.stderr)

    src_by_id = {s["id"]: s for s in sources}
    fetched, failed = 0, []
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
            history[it["id"]] = {
                "id": it["id"], "title": it["title"], "link": it["link"],
                "source": it["source"], "source_id": it["source_id"],
                "published": it["published"], "summary": it["summary"],
                "fetched_at": now.isoformat(),
            }
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
        item["_tier"] = src_by_id.get(it["source_id"], {}).get("tier", "T2")
        item["category"] = classify(item, cats,
                                    src_by_id.get(it["source_id"], {}).get("category_hint", "industry"))
        item["category_label"] = cat_label.get(item["category"], item["category"])
        item["score"] = score(item, cfg, now)
        live.append(item)

    # 可选 LLM 重排（无 Key 时直接跳过）
    live.sort(key=lambda x: x["score"], reverse=True)
    live = maybe_llm_rerank(live)

    recent_48h = [it for it in live if (now - it["_pub"]) <= timedelta(hours=sc["selected_hours"])]
    selected = [it for it in recent_48h if it["score"] >= sc["selected_min"]]
    selected.sort(key=lambda x: x["score"], reverse=True)
    selected = selected[:sc["daily_top"]]

    last_7d = [it for it in live if (now - it["_pub"]) <= timedelta(days=sc["hot_days"])]
    for it in last_7d:
        age_days = (now - it["_pub"]).total_seconds() / 86400.0
        it["hot_score"] = round(it["score"] * (sc["decay_base"] ** age_days), 1)
    hot = sorted(last_7d, key=lambda x: x["hot_score"], reverse=True)[:sc["hot_top"]]
    timeline = sorted(last_7d, key=lambda x: x["_pub"], reverse=True)[:sc["timeline_max"]]

    def pub_item(it):
        return {"id": it["id"], "title": it["title"], "link": it["link"],
                "source": it["source"], "source_id": it["source_id"],
                "published": it["published"], "summary": it["summary"],
                "category": it["category"], "category_label": it["category_label"],
                "score": it["score"],
                "hot_score": it.get("hot_score", it["score"])}

    gen_cst = now_cst.strftime("%Y年%m月%d日 %H:%M")
    hot_data = {
        "generated_at": now.isoformat(),
        "generated_at_cst": gen_cst,
        "counts": {"total_7d": len(last_7d), "selected": len(selected),
                   "hot": len(hot), "timeline": len(timeline)},
        "selected": [pub_item(it) for it in selected],
        "hot": [pub_item(it) for it in hot],
        "timeline": [pub_item(it) for it in timeline],
    }
    save_json(os.path.join(data_dir, "hot.json"), hot_data)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(site, cats, selected, hot, timeline, gen_cst,
                             hot_data["counts"]))
    print(f"fetch: {fetched} items parsed from {len(sources) - len(failed)}/{len(sources)} sources"
          + (f"; failed: {failed}" if failed else ""))
    print(f"history: {len(history)} items; 7d: {len(last_7d)}; "
          f"selected: {len(selected)}; hot: {len(hot)}; timeline: {len(timeline)}")

    if args.daily:
        last_24h = [it for it in live if (now - it["_pub"]) <= timedelta(hours=sc["daily_hours"])]
        last_24h.sort(key=lambda x: x["score"], reverse=True)
        daily_items = last_24h[:sc["daily_top"]]
        per_cat = {}
        for it in daily_items:
            per_cat[it["category_label"]] = per_cat.get(it["category_label"], 0) + 1
        cat_part = "、".join(f"{k} {v} 条" for k, v in per_cat.items()) or "无"
        head = f"头条：{daily_items[0]['title']}" if daily_items else "今日暂无精选"
        date_str = now_cst.strftime("%Y年%m月%d日")
        daily = {
            "date": date_str,
            "generated_at": now.isoformat(),
            "summary": (f"过去 24 小时共收录 {len(last_24h)} 条动态，精选 "
                        f"{len(daily_items)} 条（{cat_part}）。{head}。"),
            "items": [pub_item(it) for it in daily_items],
        }
        save_json(os.path.join(data_dir, "daily.json"), daily)
        with open(os.path.join(root, "daily.html"), "w", encoding="utf-8") as f:
            f.write(render_daily(site, daily))
        print(f"daily: {date_str}, {len(daily_items)} items")


if __name__ == "__main__":
    main()
