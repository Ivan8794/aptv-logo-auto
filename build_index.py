
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
FANMINGMING_BASE = "https://live.fanmingming.cn/tv/"
OUTPUT = Path("logos-map.json")
TIMEOUT = 20

HEADERS = {
    "User-Agent": "APTV-Logo-Index/2.0",
    "Accept": "*/*",
}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def normalize(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\.(png|jpe?g|webp|svg)$", "", value)
    value = re.sub(r"[^a-z0-9\u0080-\uffff]+", "", value)
    return value.strip()


def safe_url(url):
    """Percent-encode non-ASCII characters in URL path and query."""
    url = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        path = urllib.parse.quote(
            urllib.parse.unquote(parts.path), safe="/%:@!$&'()*+,;=-._~"
        )
        query = urllib.parse.quote(
            urllib.parse.unquote(parts.query), safe="=&;%:@!$'()*+,/?-._~"
        )
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, query, parts.fragment)
        )
    except (ValueError, UnicodeError):
        return ""


def is_placeholder(url):
    if not url:
        return True
    value = url.strip().casefold()
    return any(term in value for term in (
        "placeholder", "no-logo", "no_logo", "default-logo",
        "default_logo", "blank.png", "empty.png"
    ))


def image_url_responds(url):
    url = safe_url(url)
    if not url or is_placeholder(url):
        return False

    req = urllib.request.Request(
        url,
        headers={**HEADERS, "Range": "bytes=0-1023"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            data = response.read(1024)
            if not 200 <= response.status < 300:
                return False
            if content_type.startswith("image/"):
                return bool(data)
            return data.startswith(
                (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")
            )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # Some image hosts reject automated checks; don't crash the whole build.
        return False


def add_candidate(index, key, url, source):
    key = normalize(key)
    url = safe_url(url)
    if not key or not url or is_placeholder(url):
        return
    options = index.setdefault(key, [])
    candidate = {"url": url, "source": source}
    if candidate not in options:
        options.append(candidate)


def fanmingming_url(name):
    name = (name or "").strip()
    if not name:
        return ""
    # Keep the original display name; encode it as a URL path segment.
    return FANMINGMING_BASE + urllib.parse.quote(name, safe="") + ".png"


def main():
    print("Downloading EPG...")
    epg_data = fetch(EPG_URL)
    root = ET.fromstring(epg_data)

    candidates = {}
    epg_names = {}
    epg_ids = set()
    epg_channels = 0

    for channel in root.findall(".//channel"):
        epg_channels += 1
        channel_id = (channel.get("id") or "").strip()
        names = [
            node.text.strip()
            for node in channel.findall("display-name")
            if node.text and node.text.strip()
        ]
        icons = [
            node.get("src", "").strip()
            for node in channel.findall("icon")
            if node.get("src")
        ]

        if channel_id:
            epg_ids.add(normalize(channel_id))
            epg_names.setdefault(normalize(channel_id), names)

        for name in names:
            epg_names.setdefault(normalize(name), names)

        for icon_url in icons:
            if not safe_url(icon_url) or is_placeholder(icon_url):
                continue
            if channel_id:
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

    for logo in logos:
        channel_id = str(logo.get("channel", "") or "")
        url = str(logo.get("url", "") or "")
        if not safe_url(url) or is_placeholder(url):
            continue

        channel = channel_by_id.get(channel_id, {})
        if channel_id:
            add_candidate(candidates, channel_id, url, "iptv-org")

        for field in ("name", "alt_names"):
            value = channel.get(field)
            if isinstance(value, str):
                add_candidate(candidates, value, url, "iptv-org")
            elif isinstance(value, list):
                for name in value:
                    if isinstance(name, str):
                        add_candidate(candidates, name, url, "iptv-org")

    result = {"byId": {}, "byName": {}, "meta": {}}
    checked = {}
    stats = {"epg": 0, "iptv-org": 0, "fanmingming": 0}
    fanmingming_checked = {}

    def choose(key, target, original_names=None):
        options = list(candidates.get(key, []))
        options.sort(key=lambda item: (
            0 if item["source"] == "epg" else 1,
            item["url"]
        ))

        for item in options:
            url = item["url"]
            if url not in checked:
                checked[url] = image_url_responds(url)
                time.sleep(0.02)
            if checked[url]:
                target[key] = url
                stats[item["source"]] += 1
                return

        # No usable EPG/iptv-org logo: try Fanmingming with original names.
        for name in original_names or []:
            fallback = safe_url(fanmingming_url(name))
            if not fallback:
                continue
            if fallback not in fanmingming_checked:
                fanmingming_checked[fallback] = image_url_responds(fallback)
                time.sleep(0.02)
            if fanmingming_checked[fallback]:
                target[key] = fallback
                stats["fanmingming"] += 1
                return

    # Exact channel-ID index.
    for channel in root.findall(".//channel"):
        channel_id = (channel.get("id") or "").strip()
        if not channel_id:
            continue
        key = normalize(channel_id)
        names = [
            node.text.strip()
            for node in channel.findall("display-name")
            if node.text and node.text.strip()
        ]
        choose(key, result["byId"], names)

    # Name index: includes EPG names even if the EPG had no icon.
    all_name_keys = set(candidates)
    for names in epg_names.values():
        for name in names:
            key = normalize(name)
            if key:
                all_name_keys.add(key)

    for key in sorted(all_name_keys):
        names = epg_names.get(key, [])
        if not names:
            # Recover names from EPG channels when their normalized name is the key.
            for channel in root.findall(".//channel"):
                display_names = [
                    node.text.strip()
                    for node in channel.findall("display-name")
                    if node.text and node.text.strip()
                ]
                if any(normalize(name) == key for name in display_names):
                    names = display_names
                    break
        choose(key, result["byName"], names)

    result["meta"] = {
        "epg_url": EPG_URL,
        "epg_channels_read": epg_channels,
        "candidate_keys": len(candidates),
        "checked_image_urls": len(checked),
        "fanmingming_urls_checked": len(fanmingming_checked),
        "matched_by_source": stats,
        "note": (
            "Logo index only. Does not edit or remove M3U channels, "
            "stream URLs, or EPG settings."
        ),
    }

    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    main()
