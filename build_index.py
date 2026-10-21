
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EPG_URL = "https://myeth-epg.github.io/public/epg.a.xml"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
OUTPUT = Path("logos-map.json")

HEADERS = {
    "User-Agent": "APTV-Logo-Index/1.0",
    "Accept": "*/*",
}
TIMEOUT = 20


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def normalize(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\.(png|jpe?g|webp|svg)$", "", value)
    value = re.sub(r"[^a-z0-9\u0080-\uffff]+", "", value)
    return value.strip()


def valid_http_url(url):
    try:
        parsed = urllib.parse.urlparse(url or "")
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def is_placeholder(url):
    if not url:
        return True
    value = url.strip().casefold()
    return any(term in value for term in (
        "placeholder", "no-logo", "no_logo", "default-logo",
        "default_logo", "blank.png", "empty.png"
    ))


def image_url_responds(url):
    """Best-effort check; some image hosts block automated requests."""
    if not valid_http_url(url) or is_placeholder(url):
        return False

    req = urllib.request.Request(
        url,
        headers={**HEADERS, "Range": "bytes=0-1023"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            first_bytes = response.read(1024)
            if response.status < 200 or response.status >= 300:
                return False
            if content_type.startswith("image/"):
                return bool(first_bytes)
            # Some hosts do not report an image content type.
            return first_bytes.startswith(
                (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")
            )
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def add_candidate(index, key, url, source):
    key = normalize(key)
    if not key or not valid_http_url(url) or is_placeholder(url):
        return
    index.setdefault(key, [])
    candidate = {"url": url, "source": source}
    if candidate not in index[key]:
        index[key].append(candidate)


def main():
    print("Downloading EPG...")
    epg_data = fetch(EPG_URL)
    root = ET.fromstring(epg_data)

    candidates = {}
    epg_channels = 0

    # EPG is the first-choice source.
    for channel in root.findall(".//channel"):
        epg_channels += 1
        channel_id = channel.get("id", "")
        names = [node.text.strip() for node in channel.findall("display-name")
                 if node.text and node.text.strip()]
        icons = [node.get("src", "") for node in channel.findall("icon")
                 if node.get("src")]

        for icon_url in icons:
            if not valid_http_url(icon_url) or is_placeholder(icon_url):
                continue
            add_candidate(candidates, channel_id, icon_url, "epg")
            for name in names:
                add_candidate(candidates, name, icon_url, "epg")

    print(f"EPG channels read: {epg_channels}")
    print("Downloading iptv-org channel and logo databases...")

    channels = json.loads(fetch(CHANNELS_URL))
    logos = json.loads(fetch(LOGOS_URL))

    channel_by_id = {
        str(channel.get("id", "")): channel
        for channel in channels
        if channel.get("id")
    }

    # Build the public logo database, associating logos with channel IDs.
    for logo in logos:
        channel_id = str(logo.get("channel", "") or "")
        url = str(logo.get("url", "") or "")
        if not valid_http_url(url) or is_placeholder(url):
            continue

        channel = channel_by_id.get(channel_id, {})
        add_candidate(candidates, channel_id, url, "iptv-org")
        for field in ("name", "alt_names"):
            value = channel.get(field)
            if isinstance(value, str):
                add_candidate(candidates, value, url, "iptv-org")
            elif isinstance(value, list):
                for name in value:
                    if isinstance(name, str):
                        add_candidate(candidates, name, url, "iptv-org")

    # Choose one candidate per normalized key. Prefer EPG candidates.
    # Validate selected URLs, caching results to avoid duplicate checks.
    result = {"byId": {}, "byName": {}, "meta": {}}
    checked = {}
    stats = {"epg": 0, "iptv-org": 0, "fanmingming": 0}

    def choose(key, target):
        options = candidates.get(key, [])
        options.sort(key=lambda item: (
            0 if item["source"] == "epg" else 1,
            item["url"]
        ))

        for item in options:
            url = item["url"]
            if url not in checked:
                checked[url] = image_url_responds(url)
                time.sleep(0.05)
            if checked[url]:
                target[key] = url
                stats[item["source"]] += 1
                return

        # Last resort: record a conventional Fanmingming URL.
        # It will be checked again by the runtime consumer if needed.
        raw_key = urllib.parse.quote(key, safe="")
        fallback = f"https://live.fanmingming.cn/tv/{raw_key}.png"
        if key and image_url_responds(fallback):
            target[key] = fallback
            stats["fanmingming"] += 1

    all_keys = sorted(candidates)
    for key in all_keys:
        # Channel IDs are also included in byId for exact ID matching.
        if any(channel.get("id") and normalize(str(channel["id"])) == key
               for channel in channels):
            choose(key, result["byId"])
        else:
            choose(key, result["byName"])

    result["meta"] = {
        "epg_url": EPG_URL,
        "epg_channels_read": epg_channels,
        "candidate_keys": len(candidates),
        "checked_image_urls": len(checked),
        "matched_by_source": stats,
        "note": "Generated logo index only; does not edit or remove any M3U channel or stream URL."
    }

    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    main()
