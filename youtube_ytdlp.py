#!/usr/bin/env python3
"""Shared yt-dlp helpers for the Bhakti fetch scripts.

Replaces the old RSS feed (channel listing) and the YouTube Data API
(video details / live status) with yt-dlp, using the same Firefox-cookie
anti-bot technique proven in Palki-Sahib-Video-Creator/Scripts/01_download_stream.py:

    yt-dlp --ignore-config --cookies-from-browser firefox \
           --js-runtimes node --remote-components ejs:github ... <URL>

Node.js + the ejs:github remote component solve YouTube's JS (nsig) challenge,
and the restored Firefox profile supplies a signed-in session so requests are
not blocked with "Sign in to confirm you're not a bot".
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------- CONFIG (overridable via env) ----------------
YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
# Empty string disables cookies (e.g. for local testing without Firefox).
COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "firefox")

# Channel logo cache shared across a single run.
_CHANNEL_LOGO_CACHE = {}


def base_args():
    """Anti-bot base command mirroring the Palki-Sahib download script."""
    args = [YTDLP_BIN, "--ignore-config", "--no-warnings"]
    if COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    args += ["--js-runtimes", "node", "--remote-components", "ejs:github"]
    return args


def _run(cmd):
    """Run a yt-dlp command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout or "", result.stderr or ""


# ---------------- LISTING (replaces RSS) ----------------
def list_channel_entries(channel_id, tab="videos", limit=50):
    """List the latest videos on a channel tab via a flat (fast) yt-dlp playlist read.

    tab is one of "videos", "streams", "shorts". Returns a list of
    {"video_id", "title"} for the newest `limit` entries. Never raises; on
    failure it logs and returns an empty list.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
    cmd = base_args() + [
        "--flat-playlist",
        "--playlist-end", str(limit),
        "--dump-single-json",
        "--no-download",
        url,
    ]
    code, out, err = _run(cmd)
    if code != 0:
        print(f"⚠️ yt-dlp listing failed for {channel_id}/{tab}: {err.strip()[:200]}")
        return []

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        print(f"⚠️ Could not parse listing JSON for {channel_id}/{tab}: {e}")
        return []

    videos = []
    for entry in data.get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        title = entry.get("title")
        if not vid or not title:
            continue
        videos.append({"video_id": vid, "title": title.strip()})
    return videos


# ---------------- DETAILS (replaces YouTube Data API) ----------------
def extract_videos(video_ids):
    """Full-extract many videos in one yt-dlp process (newline-delimited JSON).

    Returns {video_id: info_json}. Missing/failed videos are simply absent.
    """
    ids = [v for v in dict.fromkeys(video_ids) if v]  # de-dupe, keep order
    if not ids:
        return {}

    urls = [f"https://www.youtube.com/watch?v={v}" for v in ids]
    cmd = base_args() + [
        "-j",                       # one JSON object per line
        "--no-download",
        "--ignore-errors",          # skip a bad video, keep going
        "--sleep-requests", "1",
        "--extractor-retries", "3",
    ] + urls

    code, out, err = _run(cmd)
    # returncode may be non-zero if some videos failed; parse whatever we got.
    if not out.strip() and err.strip():
        print(f"⚠️ yt-dlp extraction produced no output: {err.strip()[:300]}")

    details = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = info.get("id")
        if vid:
            details[vid] = info
    return details


# ---------------- FIELD HELPERS ----------------
def channel_name(info):
    return info.get("channel") or info.get("uploader") or ""


def channel_id_of(info):
    return info.get("channel_id") or info.get("uploader_id") or ""


def live_status(info):
    """yt-dlp live_status: is_live | is_upcoming | was_live | post_live | not_live."""
    return info.get("live_status")


def view_count(info, live=False):
    if live:
        vc = info.get("concurrent_view_count")
        if vc is not None:
            return int(vc)
    vc = info.get("view_count")
    return int(vc) if vc is not None else 0


def duration_seconds(info):
    d = info.get("duration")
    return int(d) if d else 0


def format_duration(seconds):
    """Seconds -> M:SS or H:MM:SS (matches the old ISO-8601 formatter)."""
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def published_ms(info):
    """Publish/stream-start time as a millisecond epoch string."""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return str(int(ts) * 1000)
    upload_date = info.get("upload_date")  # 'YYYYMMDD'
    if upload_date:
        try:
            dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            return str(int(dt.timestamp() * 1000))
        except ValueError:
            pass
    return str(int(datetime.now(timezone.utc).timestamp() * 1000))


# ---------------- IMAGE / LOGO (no API, unchanged behavior) ----------------
def get_working_image_url(video_id):
    maxres_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    fallback_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg"
    try:
        response = requests.head(maxres_url, timeout=5)
        if response.status_code == 200:
            return maxres_url
    except Exception:
        pass
    return fallback_url


def fetch_channel_logo(channel_id):
    """Scrape the channel's og:image logo (cached per run)."""
    if channel_id in _CHANNEL_LOGO_CACHE:
        return _CHANNEL_LOGO_CACHE[channel_id]

    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    logo = ""
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        meta_image = soup.find("meta", property="og:image")
        if meta_image and meta_image.get("content"):
            logo = meta_image["content"]
    except Exception as e:
        print(f"❌ Error scraping logo for {channel_id}: {e}")

    _CHANNEL_LOGO_CACHE[channel_id] = logo
    return logo
