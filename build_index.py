
import json
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EPG_URL = "https://myeth-epg.github.io/public/epg.a.xml"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
FANMINGMING_BASE = "https://live.fanmingming.cn/tv/"
OUTPUT = Path("logos-map.json")
TIMEOUT = 30

HEADERS = {"User-Agent": "APTV-Logo-Index/3.0", "Accept": "*/*"}


def log(message):
    print(message, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def normalize(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\.(png|jpe?g|webp|svg)$", "", value)
    return re.sub(r"[^a-z0-9\u0080-\uffff]+", "", value)


def safe_url(url):
    url = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        path = urllib.parse.quote(
            parts.path, safe="/%:@!$&'()*+,;=-._~"
        )
        query = urllib.parse.quote(
            parts.query, safe="=&;%:@!$'()*+,/?-._~"
        )
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, query, parts.fragment)
        )
    except (ValueError, UnicodeError):
        return ""


def usable_url(url):
    url = safe_url(url)
    if not url:
        return ""
    low = url.casefold()
    if any(term in low for term in (
        "placeholder", "no-logo", "no_logo",
        "default-logo", "default_logo", "blank.png", "empty.png"
    )):
        return ""
    return url


def add(index, key, url):
    key = normalize(key)
    url = usable_url(url)
    if key and url:
        index.setdefault(key, url)


def fanmingming_url(name):
    name = (name or "").strip()
    if not name:
        return ""
    return FANMINGMING_BASE + urllib.parse.quote(name, safe="") + ".png"


def main():
    log("[1/4] Downloading EPG...")
    root = ET.fromstring(fetch(EPG_URL))
    epg_channels = root.findall(".//channel")
    log(f"EPG channels read: {len(epg_channels)}")

    # Build all EPG name/ID indexes in one pass.
    epg_by_id = {}
    epg_by_name = {}
    all_epg_names = {}

    for channel in epg_channels:
        cid = (channel.get("id") or "").strip()
        names = [
            node.text.strip()
            for node in channel.findall("display-name")
            if node.text and node.text.strip()
        ]
        icons = [
            usable_url(node.get("src", ""))
            for node in channel.findall("icon")
            if node.get("src")
        ]
        logo = next((url for url in icons if url), "")

        if cid:
            key = normalize(cid)
            epg_by_id.setdefault(key, logo)
            all_epg_names.setdefault(key, names)

        for name in names:
            key = normalize(name)
            if key:
                all_epg_names.setdefault(key, names)
                if logo:
                    epg_by_name.setdefault(key, logo)

    log("[2/4] Downloading iptv-org databases...")
    channels = json.loads(fetch(CHANNELS_URL))
    logos = json.loads(fetch(LOGOS_URL))

    channel_by_id = {
        str(item["id"]): item
        for item in channels
        if item.get("id")
    }

    org_by_id = {}
    org_by_name = {}

    for logo in logos:
        cid = str(logo.get("channel", "") or "")
        url = usable_url(str(logo.get("url", "") or ""))
        if not url:
            continue

        if cid:
            org_by_id.setdefault(normalize(cid), url)

        channel = channel_by_id.get(cid, {})
        names = []
        for field in ("name", "alt_names"):
            value = channel.get(field)
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, list):
                names.extend(
                    item for item in value if isinstance(item, str)
                )

        for name in names:
            key = normalize(name)
            if key:
                org_by_name.setdefault(key, url)

    log("[3/4] Building logo index (without slow image probes)...")
    result = {"byId": {}, "byName": {}, "meta": {}}
    stats = {"epg": 0, "iptv-org": 0, "fanmingming": 0}

    def choose(key, names, epg_logo=""):
        # Prefer an EPG-provided logo.
        if epg_logo:
            stats["epg"] += 1
            return epg_logo

        # Prefer an existing indexed public logo to avoid guessing
        # a Fanmingming filename that may not exist.
        url = org_by_id.get(key, "")
        if not url:
            for name in names:
                url = org_by_name.get(normalize(name), "")
                if url:
                    break
        if url:
            stats["iptv-org"] += 1
            return url

        # Last-resort candidate. Existence is not verified at build time.
        for name in names:
            url = fanmingming_url(name)
            if url:
                stats["fanmingming"] += 1
                return url
        return ""

    for channel in epg_channels:
        cid = (channel.get("id") or "").strip()
        names = [
            node.text.strip()
            for node in channel.findall("display-name")
            if node.text and node.text.strip()
        ]
        icons = [
            usable_url(node.get("src", ""))
            for node in channel.findall("icon")
            if node.get("src")
        ]
        epg_logo = next((url for url in icons if url), "")
        key = normalize(cid) if cid else ""

        if key:
            url = choose(key, names, epg_logo)
            if url:
                result["byId"][key] = url

        for name in names:
            name_key = normalize(name)
            if not name_key:
                continue
            # If duplicate display names exist, keep the first mapping.
            if name_key not in result["byName"]:
                url = choose(
                    name_key, names,
                    epg_by_name.get(name_key, "")
                )
                if url:
                    result["byName"][name_key] = url

    result["meta"] = {
        "epg_url": EPG_URL,
        "epg_channels_read": len(epg_channels),
        "byId_count": len(result["byId"]),
        "byName_count": len(result["byName"]),
        "matched_by_source": stats,
        "image_urls_verified": False,
        "note": (
            "Logo index only. Does not modify M3U channels, stream URLs, "
            "or EPG settings. Image availability is not checked at build time."
        ),
    }

    log("[4/4] Writing logos-map.json...")
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(json.dumps(result["meta"], ensure_ascii=False, indent=2))
    log("SUCCESS: Created logos-map.json")


if __name__ == "__main__":
    main()
