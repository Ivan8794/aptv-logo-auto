#!/usr/bin/env python3
"""Generate EPG-name-based logo files for APTV GitHub Pages."""
import os
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

EPG_URL = "https://myeth-epg.github.io/public/epg.a.xml"
OUT = Path("site")
LOGOS = OUT / "logos"
UA = "Mozilla/5.0 (compatible; APTV-EPG-Logo-Library/1.0)"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()

def safe_name(name):
    # Keep EPG display-name as the logical filename; remove only path separators/control chars.
    name = name.strip().replace("/", "／").replace("\\", "＼")
    name = re.sub(r"[\\x00-\\x1f\\x7f]", "", name)
    return name

def main():
    LOGOS.mkdir(parents=True, exist_ok=True)
    xml = get(EPG_URL)
    root = ET.fromstring(xml)
    success = 0
    no_icon = 0
    failed = 0
    seen = set()
    for channel in root.findall(".//channel"):
        names = [safe_name((n.text or "").strip()) for n in channel.findall("display-name")]
        names = [n for n in names if n]
        icon = channel.find("icon")
        src = (icon.get("src", "").strip() if icon is not None else "")
        if not names or not src:
            no_icon += 1
            continue
        try:
            data = get(src)
            # Store as PNG extension only if source bytes are PNG; preserve other formats.
            ext = ".png"
            if data.startswith(b"\\xff\\xd8\\xff"): ext = ".jpg"
            elif data.startswith((b"GIF87a", b"GIF89a")): ext = ".gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP": ext = ".webp"
            elif b"<svg" in data[:4096].lower(): ext = ".svg"
            for name in names:
                filename = name + ext
                path = LOGOS / filename
                if filename in seen:
                    continue
                path.write_bytes(data)
                seen.add(filename)
                success += 1
        except Exception as e:
            print(f"Failed icon for {names[0]}: {e}")
            failed += 1
    # APTV can query a plain URL template; this index is useful for auditing.
    (OUT / "index.txt").write_text("\\n".join(sorted(seen)) + "\\n", encoding="utf-8")
    # GitHub Pages needs an index.html at the artifact root (site/).
    import html
    items = sorted(seen)
    rows = "\\n".join(
        f'<li><a href="logos/{urllib.parse.quote(name)}">{html.escape(name)}</a></li>'
        for name in items
    )
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APTV EPG 台标库</title>
<style>body{{font:16px system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px;color:#222}}code{{word-break:break-all}}li{{margin:6px 0}}</style>
</head><body><h1>APTV EPG 台标库</h1>
<p>自动从 EPG 获取台标。当前收录 <strong>{len(items)}</strong> 个文件。</p>
<p>EPG：<a href="{EPG_URL}">{EPG_URL}</a></p>
<p>台标文件目录：<a href="./logos/">./logos/</a></p>
<h2>台标文件清单</h2><ul>{rows}</ul></body></html>"""
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"EPG logo files: {success}; channels without icon/name: {no_icon}; failed downloads: {failed}")
    if not success:
        raise SystemExit("No logos downloaded; refusing to publish empty library.")

if __name__ == "__main__":
    main()
