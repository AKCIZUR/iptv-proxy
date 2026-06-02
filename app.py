from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from urllib.parse import urljoin
import gzip
import io
import os
import threading
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EPG_URL = os.getenv("EPG_URL", "").strip()
EPG_REFRESH_SECONDS = int(os.getenv("EPG_REFRESH_SECONDS", "21600"))

epg_lock = threading.Lock()
epg_cache = {}
epg_last_updated = None


CHANNEL_MAP = {
    "ČT1": "ct1",
    "ČT2": "ct2",
    "ČT24": "ct24",
    "ČT Sport": "ctsport",
    "TV Nova": "nova",
    "Nova Cinema": "novacinema",
    "Nova Fun": "novafun",
    "Nova Action": "novaaction",
    "Nova Gold": "novagold",
    "Nova Lady": "novalady",
    "Prima": "prima",
    "Prima COOL": "primacool",
    "Prima KRIMI": "primakrimi",
    "Prima LOVE": "primalove",
    "Prima ZOOM": "primazoom",
    "Prima Show": "primashow",
    "CNN Prima News": "cnnprimanews",
    "AMC": "amc",
    "AXN": "axn",
    "Cinemax": "cinemax",
    "Cinemax 2": "cinemax2",
    "Film+": "filmplus",
    "Filmbox": "filmbox",
    "HBO": "hbo",
    "HBO 2": "hbo2",
    "HBO 3": "hbo3",
}


def _parse_xmltv_dt(value: str):
    if not value:
        return None
    raw = value.strip()
    parts = raw.split()
    stamp = parts[0]
    tz = parts[1] if len(parts) > 1 else "+0000"

    dt = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    if tz == "Z":
        return dt.replace(tzinfo=timezone.utc)

    sign = 1 if tz[0] == "+" else -1
    hours = int(tz[1:3])
    mins = int(tz[3:5])
    offset = timezone(sign * timedelta(hours=hours, minutes=mins))
    return dt.replace(tzinfo=offset)


def _download_epg_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def _load_epg():
    global epg_cache, epg_last_updated

    if not EPG_URL:
        with epg_lock:
            epg_cache = {}
            epg_last_updated = None
        return

    try:
        raw = _download_epg_bytes(EPG_URL)

        if EPG_URL.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        root = ET.fromstring(raw)

        parsed = {}

        for programme in root.findall("programme"):
            channel = programme.attrib.get("channel")
            start = _parse_xmltv_dt(programme.attrib.get("start", ""))
            stop = _parse_xmltv_dt(programme.attrib.get("stop", ""))

            title_node = programme.find("title")
            title = title_node.text.strip() if title_node is not None and title_node.text else ""

            desc_node = programme.find("desc")
            desc = desc_node.text.strip() if desc_node is not None and desc_node.text else ""

            if not channel or not start or not stop:
                continue

            parsed.setdefault(channel, []).append(
                {
                    "title": title,
                    "desc": desc,
                    "start": start.isoformat(),
                    "stop": stop.isoformat(),
                    "start_ts": start.timestamp(),
                    "stop_ts": stop.timestamp(),
                }
            )

        for channel in parsed:
            parsed[channel].sort(key=lambda x: x["start_ts"])

        with epg_lock:
            epg_cache = parsed
            epg_last_updated = datetime.now(timezone.utc).isoformat()

    except Exception:
        with epg_lock:
            epg_cache = {}
            epg_last_updated = None


def _epg_worker():
    while True:
        _load_epg()
        time.sleep(EPG_REFRESH_SECONDS)


@app.on_event("startup")
def startup():
    thread = threading.Thread(target=_epg_worker, daemon=True)
    thread.start()


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/proxy")
def proxy(url: str):
    r = requests.get(url, timeout=60)

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
    }

    content_type = r.headers.get("content-type", "")

    if ".m3u8" in url or "mpegurl" in content_type:
        lines = []

        for line in r.text.splitlines():
            if line.startswith("#") or not line.strip():
                lines.append(line)
                continue

            absolute = urljoin(url, line.strip())
            lines.append(f"/proxy?url={absolute}")

        return Response(
            "\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers=headers,
        )

    return StreamingResponse(
        iter([r.content]),
        media_type=content_type or "application/octet-stream",
        headers=headers,
    )


@app.get("/guide")
def guide():
    now = datetime.now(timezone.utc).timestamp()
    out = {}

    with epg_lock:
        cache = dict(epg_cache)
        updated = epg_last_updated

    for name, epg_id in CHANNEL_MAP.items():
        items = cache.get(epg_id, [])
        current = None
        next_item = None

        for i, item in enumerate(items):
            if item["start_ts"] <= now < item["stop_ts"]:
                current = item
                if i + 1 < len(items):
                    next_item = items[i + 1]
                break
            if item["start_ts"] > now:
                next_item = item
                break

        out[name] = {
            "epgId": epg_id,
            "now": None if not current else {
                "title": current["title"],
                "desc": current["desc"],
                "start": current["start"],
                "stop": current["stop"],
            },
            "next": None if not next_item else {
                "title": next_item["title"],
                "desc": next_item["desc"],
                "start": next_item["start"],
                "stop": next_item["stop"],
            },
        }

    return {
        "updatedAt": updated,
        "channels": out,
    }


@app.get("/now/{channel}")
def now(channel: str):
    guide_data = guide()
    return guide_data["channels"].get(channel, {
        "epgId": None,
        "now": None,
        "next": None,
    })
