#!/usr/bin/env python3
"""一次性回填：给 data/store.json 里的存量条目补 summary_full（完整摘要，截 1500 字）。

从 sources.json 的 12 个 RSS 信源现拉一遍，按规范化链接匹配库里的条目，
匹配上的填 summary_full。单源失败容忍（Unreal 常 403，hnrss 常 502）。
只改 data/store.json，不动其他文件；跑完可留着以后复用。

用法：cd <仓库根目录> && python3 tools/backfill_full.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import generate  # noqa: E402  (复用 fetch / parse_feed / norm_link / save_json)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    sources = generate.load_json(os.path.join(ROOT, "sources.json"))
    if isinstance(sources, dict):
        sources = sources.get("sources", [])
    store_path = os.path.join(ROOT, "data", "store.json")
    store = generate.load_json(store_path)
    items = store["items"]
    by_link = {generate.norm_link(it.get("link", "")): it for it in items}

    fetched, failed = 0, []
    filled = 0
    for src in sources:
        try:
            body = generate.fetch(src["url"])
            new_items = generate.parse_feed(body, src)
        except Exception as e:  # noqa: BLE001 - 单源失败不中断整轮
            failed.append((src["id"], str(e)[:80]))
            continue
        fetched += len(new_items)
        for it in new_items:
            full = it.get("summary_full")
            if not full:
                continue
            hit = by_link.get(generate.norm_link(it.get("link", "")))
            if hit is not None and not hit.get("summary_full"):
                hit["summary_full"] = full
                filled += 1
    if filled:
        store["items"] = items
        generate.save_json(store_path, store)
    total = len(items)
    have = sum(1 for it in items if it.get("summary_full"))
    print(f"store: {total} items, {have} have summary_full "
          f"({filled} newly filled from {fetched} feed items)")
    if failed:
        print("failed sources: " + ", ".join(f"{s}: {e}" for s, e in failed))

    # 精选 10（data/daily.json 的 items）与热点 20（data/hot.json 的 hot）
    by_id = {it["id"]: it for it in items}
    for label, path, key in (("selected", "data/daily.json", "items"),
                             ("hot", "data/hot.json", "hot")):
        try:
            data = generate.load_json(os.path.join(ROOT, path))
        except (OSError, json.JSONDecodeError) as e:
            print(f"{label}: unreadable ({e})")
            continue
        lst = data.get(key, [])
        got, miss = [], []
        for ref in lst:
            it = by_id.get(ref.get("id"))
            title = (it.get("zh_title") or it.get("title", "")) if it else \
                (ref.get("zh_title") or ref.get("title", ""))
            (got if (it and it.get("summary_full")) else miss).append(title)
        print(f"{label} ({len(lst)}): {len(got)} have full, {len(miss)} missing")
        for t in got:
            print(f"  HAVE    {t}")
        for t in miss:
            print(f"  MISSING {t}")


if __name__ == "__main__":
    main()
