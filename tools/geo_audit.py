#!/usr/bin/env python3
"""IndieHOT GEO 自检：独立运行，输出 geo-audit.html + data/geo-audit.json。

用法：
    python3 tools/geo_audit.py [--root 站根目录，默认仓库根目录]

只读磁盘上的生成物做检查，不拉 RSS、不重写其它文件。
复用 tools/generate.py 里的 build_geo_audit（同一份检查逻辑与页面模板）。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import build_geo_audit, load_json

CST = timezone(timedelta(hours=8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="仓库根目录（默认：本文件所在仓库的根目录）")
    args = ap.parse_args()
    root = args.root
    cfg = load_json(os.path.join(root, "config.json"))
    site = cfg["site"]
    site_url = site.get("url", "https://staroltest.github.io/indie-game-hot")
    today_iso = datetime.now(CST).strftime("%Y-%m-%d")
    html, data = build_geo_audit(site, root, site_url, today_iso)
    with open(os.path.join(root, "geo-audit.html"), "w", encoding="utf-8") as f:
        f.write(html)
    data_path = os.path.join(root, "data", "geo-audit.json")
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    s = data["summary"]
    print(f"geo-audit: {s['passed']} passed, {s['warn']} warn, "
          f"{s['failed']} failed -> geo-audit.html, data/geo-audit.json")


if __name__ == "__main__":
    main()
