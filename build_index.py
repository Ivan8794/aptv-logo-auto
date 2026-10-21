
import concurrent.futures
import contextlib
import json
import os
import re
import time
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

EPG_URL = "https://myeth-epg.github.io/public/epg.a.xml"
M3U_URL = os.environ.get("M3U_URL", "").strip()
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
FAN_TREE_URL = "https://api.github.com/repos/fanmingming/live/git/trees/main?recursive=1"
FAN_BASE = "https://live.fanmingming.cn/"
OUTPUT = Path("logos-map.json")
REPORT = Path("logo-check-report.json")
CUSTOM_LOGOS = Path("custom-logos.json")

TIMEOUT = 15
RETRIES = 2
WORKERS = 12
WIKIMEDIA_REQUESTS = threading.Semaphore(3)
HEADERS = {
    "User-Agent": "APTV-Logo-Index/4.0",
    "Accept": "image/*,*/*;q=0.8",
}

PLACEHOLDERS = (
    "placeholder", "no-logo", "no_logo", "nologo",
    "default-logo", "default_logo", "blank.png",
    "empty.png", "unknown.png", "missing-logo",
)


def log(message):
    print(message, flush=True)


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def normalize(value):
    value = unicodedata.normalize("NFKC", value or "").casefold().strip()
    value = re.sub(r"\.(png|jpe?g|webp|svg|gif|avif|apng)$", "", value)
    return re.sub(r"[^a-z0-9\u0080-\uffff]+", "", value)

def name_keys(value, min_length=4):
    """Return conservative aliases after removing provider labels and channel numbers."""
    value = unicodedata.normalize("NFKC", value or "").strip()
    candidates = [value]
    candidates.extend(part.strip() for part in value.split("|") if part.strip())

    without_provider = re.sub(r"^\s*\[[^\]]+\]\s*", "", value)
    candidates.append(without_provider)
    without_number = re.sub(r"^\s*\d{1,5}\s+", "", without_provider)
    candidates.append(without_number)

    # EPG feeds often include the alternate name in parentheses.
    candidates.extend(re.findall(r"\(([^()]*)\)", without_provider))
    candidates.append(re.sub(r"\s*\([^()]*\)", "", without_provider))
    candidates.append(re.sub(r"\s*\([^()]*\)", "", without_number))
    # Also recover the channel name before nested or malformed parentheses.
    candidates.append(without_provider.split("(", 1)[0].strip())
    candidates.append(without_number.split("(", 1)[0].strip())

    keys = []
    for candidate in candidates:
        key = normalize(candidate)
        if key and (key == normalize(value) or len(key) >= min_length) and key not in keys:
            keys.append(key)
    return keys


def clean_url(url):
    url = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme.lower() not in ("http", "https") or not p.netloc:
            return ""
        path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(p.query, safe="=&;%:@!$'()*+,/?-._~")
        url = urllib.parse.urlunsplit(
            (p.scheme.lower(), p.netloc, path, query, p.fragment)
        )
        low = urllib.parse.unquote(url).casefold()
        filename = urllib.parse.unquote(p.path.rsplit("/", 1)[-1])
        if any(x in low for x in PLACEHOLDERS):
            return ""
        if filename.casefold() in (
            "", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"
        ):
            return ""
        return url
    except (ValueError, UnicodeError):
        return ""


def names_of(channel):
    return list(dict.fromkeys(
        n for node in channel.findall("display-name")
        if (n := (node.text or "").strip())
    ))


def parse_m3u(text):
    """Read M3U metadata only; stream URLs are never edited or written to the logo index."""
    lines = text.lstrip("\ufeff").splitlines()
    records = []
    pending = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs = {
                key.casefold(): value.strip()
                for key, value in re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', line)
            }
            title = line.rsplit(",", 1)[1].strip() if "," in line else ""
            names = list(dict.fromkeys(
                x for x in (attrs.get("tvg-name", ""), title) if x
            ))
            logo = attrs.get("tvg-logo", "")
            candidates = []
            add_candidate(candidates, "m3u", logo)
            pending = {
                "id": attrs.get("tvg-id", "").strip(),
                "names": names,
                "epg_candidates": [],
                "m3u_candidates": candidates,
            }
        elif not line.startswith("#") and pending is not None:
            # Keep only metadata for matching; do not modify the stream URL.
            records.append(pending)
            pending = None

    return records


def image_type(data):
    data = data[:8192].lstrip(b"\xef\xbb\xbf \t\r\n")
    low = data.lower()

    if not data or low.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and (
        b"avif" in data[8:32] or b"avis" in data[8:32]
    ):
        return "avif"
    if b"<svg" in low and b"<html" not in low:
        return "svg"
    return ""


def check_image(url):
    """404/410 or non-image content is invalid; transient errors remain pending."""
    reason = "unknown"
    host = urllib.parse.urlsplit(url).hostname or ""
    wikimedia = host.casefold() == "upload.wikimedia.org"
    for attempt in range(RETRIES + 1):
        retry_after = None
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            gate = WIKIMEDIA_REQUESTS if wikimedia else contextlib.nullcontext()
            with gate:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read(128 * 1024)
            kind = image_type(data)
            if kind:
                return {"status": "valid", "reason": kind}
            return {"status": "invalid", "reason": "not-an-image"}
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return {"status": "invalid", "reason": f"http-{e.code}"}
            reason = f"http-{e.code}"
            if e.code == 429:
                try:
                    retry_after = min(30.0, max(0.0, float(e.headers.get("Retry-After", ""))))
                except (TypeError, ValueError):
                    retry_after = 2.0 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = type(e).__name__
        except Exception as e:
            reason = type(e).__name__

        if attempt < RETRIES:
            delay = 0.5 * (attempt + 1)
            if retry_after is not None:
                delay = max(delay, retry_after)
            time.sleep(delay)
    return {"status": "pending", "reason": reason}


def add_candidate(target, source, url):
    url = clean_url(url)
    if url and all(x["url"] != url for x in target):
        target.append({"source": source, "url": url})


def load_custom_logos():
    """Load persistent public logo overrides; invalid entries are skipped safely."""
    result = {"byId": {}, "byName": {}}
    invalid = 0
    if not CUSTOM_LOGOS.exists():
        return result, invalid

    try:
        data = json.loads(CUSTOM_LOGOS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result, 1

    if not isinstance(data, dict):
        return result, 1

    for section in ("byId", "byName"):
        entries = data.get(section, {})
        if not isinstance(entries, dict):
            invalid += 1
            continue
        for key, value in entries.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                invalid += 1
                continue
            url = clean_url(value)
            if not url:
                invalid += 1
                continue
            parsed = urllib.parse.urlsplit(url)
            if parsed.username or parsed.password:
                invalid += 1
                continue
            result[section][key.strip()] = url

    return result, invalid


def main():
    custom_logos, invalid_custom_logos = load_custom_logos()
    custom_logo_count = len(custom_logos["byId"]) + len(custom_logos["byName"])
    if custom_logo_count:
        log(f"Custom logo overrides configured: {custom_logo_count}")
    if invalid_custom_logos:
        log(f"Skipped invalid custom logo entries: {invalid_custom_logos}")

    log("[1/6] 读取 EPG，并先清除明显占位符和空地址...")
    root = ET.fromstring(fetch(EPG_URL))
    channels = root.findall(".//channel")

    # Each EPG channel remains a separate record; no channel is deleted.
    records = []
    for ch in channels:
        icons = []
        for icon in ch.findall("icon"):
            add_candidate(icons, "epg", icon.get("src", ""))
        records.append({
            "id": (ch.get("id") or "").strip(),
            "names": names_of(ch),
            "epg_candidates": icons,
            "m3u_candidates": [],
        })

    epg_channel_count = len(records)
    log(f"EPG channels: {epg_channel_count}")
    log("[2/6] 读取可选的 M3U 频道元数据（不改动节目源地址）...")
    m3u_records = []
    m3u_loaded = False
    if not M3U_URL:
        log("M3U_URL 未配置；继续使用 EPG 生成台标索引。")
    else:
        try:
            m3u_req = urllib.request.Request(
                M3U_URL,
                headers={"User-Agent": "APTV-Logo-Index/4.0", "Accept": "*/*"},
            )
            with urllib.request.urlopen(m3u_req, timeout=TIMEOUT) as response:
                m3u_text = response.read().decode("utf-8-sig", errors="replace")
            m3u_records = parse_m3u(m3u_text)
            m3u_loaded = True
            log(f"M3U entries: {len(m3u_records)}")
        except urllib.error.HTTPError as e:
            log(f"M3U unavailable (HTTP {e.code}); continuing with EPG only.")
        except Exception as e:
            log(f"M3U unavailable ({type(e).__name__}); continuing with EPG only.")

    records.extend(m3u_records)
    log(f"Combined records: {len(records)}")
    log("[3/6] 下载 iptv-org 与 Fanmingming 文件清单...")
    org_channels = json.loads(fetch(CHANNELS_URL))
    org_logos = json.loads(fetch(LOGOS_URL))
    org_channel_by_id = {
        str(x.get("id")): x for x in org_channels if x.get("id")
    }

    org_by_id = {}
    org_by_name = {}

    # Prefer logos marked in_use; retain alternatives for fallback.
    org_logos.sort(
        key=lambda x: (
            not bool(x.get("in_use")),
            -(int(x.get("width") or 0) * int(x.get("height") or 0)),
        )
    )
    for logo in org_logos:
        cid = str(logo.get("channel") or "")
        url = clean_url(str(logo.get("url") or ""))
        if not cid or not url:
            continue
        org_by_id.setdefault(normalize(cid), [])
        if url not in org_by_id[normalize(cid)]:
            org_by_id[normalize(cid)].append(url)

        info = org_channel_by_id.get(cid, {})
        aliases = []
        for field in ("name", "alt_names"):
            v = info.get(field)
            if isinstance(v, str):
                aliases.append(v)
            elif isinstance(v, list):
                aliases.extend(x for x in v if isinstance(x, str))
        for alias in aliases:
            for key in name_keys(alias):
                org_by_name.setdefault(key, [])
                if url not in org_by_name[key]:
                    org_by_name[key].append(url)

    fan_by_name = {}
    tree = json.loads(fetch(FAN_TREE_URL, {
        **HEADERS, "Accept": "application/vnd.github+json"
    }))
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if not item.get("type") == "blob":
            continue
        if not path.startswith("tv/"):
            continue
        if not path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")):
            continue
        filename = path.rsplit("/", 1)[-1]
        key = normalize(filename)
        if key:
            fan_by_name.setdefault(key, [])
            fan_by_name[key].append(urllib.parse.urljoin(FAN_BASE, path))

    log("[4/6] 完成 EPG 与可用 M3U 的候选匹配，之后才统一检测图片...")
    matched = []
    all_urls = set()

    custom_by_id = {
        normalize(channel_id): url
        for channel_id, url in custom_logos["byId"].items()
    }
    custom_by_name = {
        normalize(name): url
        for name, url in custom_logos["byName"].items()
    }

    for rec in records:
        candidates = []
        id_key = normalize(rec["id"])
        if id_key in custom_by_id:
            add_candidate(candidates, "custom", custom_by_id[id_key])
        for name in rec["names"]:
            for key in name_keys(name, min_length=3):
                if key in custom_by_name:
                    add_candidate(candidates, "custom", custom_by_name[key])

        for x in rec["epg_candidates"]:
            add_candidate(candidates, "epg", x["url"])
        for x in rec.get("m3u_candidates", []):
            add_candidate(candidates, "m3u", x["url"])

        for url in org_by_id.get(id_key, []):
            add_candidate(candidates, "iptv-org", url)

        for name in rec["names"]:
            for key in name_keys(name):
                for url in org_by_name.get(key, []):
                    add_candidate(candidates, "iptv-org", url)

        # Only use Fanmingming filenames that actually exist in its repository.
        for name in rec["names"]:
            for key in name_keys(name):
                for url in fan_by_name.get(key, []):
                    add_candidate(candidates, "fanmingming", url)

        for c in candidates:
            all_urls.add(c["url"])
        matched.append({**rec, "candidates": candidates})

    all_urls.update(
        url
        for section in custom_logos.values()
        for url in section.values()
    )
    log(f"Candidate references prepared; unique URLs: {len(all_urls)}")
    log("[5/6] 统一检测候选图片（404/非图片会淘汰，超时标记待复查）...")
    checks = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_image, u): u for u in all_urls}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            url = futures[future]
            try:
                checks[url] = future.result()
            except Exception as e:
                checks[url] = {"status": "pending", "reason": type(e).__name__}
            if i % 100 == 0 or i == len(futures):
                log(f"Checked {i}/{len(futures)}")

    result = {"byId": {}, "byName": {}, "meta": {}}
    selected = Counter()
    invalid_urls, pending_urls, unmatched = {}, {}, []
    invalid_epg_channels = {}

    # Priority: EPG logo, M3U tvg-logo, iptv-org, then Fanmingming.
    for rec in matched:
        chosen = None
        for c in rec["candidates"]:
            check = checks.get(c["url"], {"status": "pending", "reason": "not-checked"})
            if check["status"] == "valid":
                chosen = c
                break
            item = {
                "url": c["url"],
                "source": c["source"],
                "reason": check["reason"],
            }
            if check["status"] == "invalid":
                invalid_urls[c["url"]] = item
                if c["source"] == "epg":
                    refs = invalid_epg_channels.setdefault(c["url"], [])
                    ref = {"id": rec["id"], "names": rec["names"]}
                    if ref not in refs:
                        refs.append(ref)
            else:
                pending_urls[c["url"]] = item

        if not chosen:
            unmatched.append({
                "id": rec["id"], "names": rec["names"],
                "reason": "no-verified-image",
            })
            continue

        selected[chosen["source"]] += 1
        if rec["id"]:
            result["byId"].setdefault(normalize(rec["id"]), chosen["url"])
        for name in rec["names"]:
            key = normalize(name)
            if key:
                result["byName"].setdefault(key, chosen["url"])

    for section, target in (("byId", "byId"), ("byName", "byName")):
        for raw_key, url in custom_logos[section].items():
            key = normalize(raw_key)
            if not key:
                continue
            check = checks.get(url, {"status": "pending", "reason": "not-checked"})
            if check["status"] == "valid":
                result[target][key] = url
                selected["custom"] += 1
                continue
            item = {"url": url, "source": "custom", "reason": check["reason"]}
            if check["status"] == "invalid":
                invalid_urls[url] = item
            else:
                pending_urls[url] = item

    result["meta"] = {
        "epg_url": EPG_URL,
        "epg_channels_read": epg_channel_count,
        "m3u_entries_read": len(m3u_records),
        "m3u_loaded": m3u_loaded,
        "custom_logo_overrides_configured": custom_logo_count,
        "custom_logo_overrides_applied": selected.get("custom", 0),
        "combined_records_read": len(records),
        "byId_count": len(result["byId"]),
        "byName_count": len(result["byName"]),
        "candidate_urls_unique": len(all_urls),
        "selected_channels_by_source": dict(selected),
        "invalid_unique_urls": len(invalid_urls),
        "pending_unique_urls": len(pending_urls),
        "unmatched_channels": len(unmatched),
        "image_urls_verified": True,
        "policy": "Custom overrides first, then EPG logo, M3U tvg-logo, iptv-org, and existing Fanmingming filenames; verified images only",
        "note": "Always uses EPG, adds M3U metadata when available, and applies verified entries from custom-logos.json; does not edit stream URLs or EPG settings.",
    }

    report = {
        "meta": result["meta"],
        "invalid_urls": [
            {**item, **({"channels": invalid_epg_channels[item["url"]]} if item["url"] in invalid_epg_channels else {})}
            for item in invalid_urls.values()
        ],
        "pending_urls": list(pending_urls.values()),
        "unmatched_channels": unmatched,
    }

    log("[6/6] 写入索引与检测报告...")
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    log("SUCCESS: logos-map.json and logo-check-report.json created")


if __name__ == "__main__":
    main()
