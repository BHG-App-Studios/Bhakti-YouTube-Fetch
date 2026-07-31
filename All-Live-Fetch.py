#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
import sys
import time
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from youtube_ytdlp import (
    base_args,
    extract_videos,
    channel_name,
    live_status,
    view_count,
    published_ms,
    get_working_image_url,
    fetch_channel_logo,
)

# ---------------- CONFIG ----------------
CHANNEL_IDS = [
     "UCiMASbpDUjNvy5CJAmfekOw",
    "UCLIryeFjYeiEtpqNETz_Ydg",
    "UCAJcxMaiGu-cjzklR-63ojw",
    "UCuFjc50BSjqeW7AOVmSR7dQ",
    "UCL0cLclH8j_qGjQhnn_5skg",
    "UC31Y8qVbsrRMUt1hbIfvCaw",
    "UC5zCR2OSUvo1g49rkAL8PoQ",

    #mandirs
    "UCBAvMHZO3BIfMMhOK9LMOYQ",
    "UC82-0zBQho_hyV10fFAAeQA",
    "UCpSTRmTFY7pCzdeHJwAiAEg",
    "UC1OSbPhj52oW6VM6Odq4uzA",
    "UCT1egsvA08YcdMLiEu1DTRg",
    "UC1qqv4R3RhT5OVMy-E_PciQ",
    "UCJKGP1t3yZMrh1Yc4Afs5rQ",
    "UC7Uo3euG3IA0yBlQyIXDcUA",
    "UCmX4QOJHAu2vni7nuGmNT5A",
    "UCxghhy9WjHpiO2jixD3t6WQ",
    "UCT3k8uyu8K8r6155o-9shdg",
]

# 🚫 Keywords to exclude (Case Insensitive, Whole Words Only)
# Kept exactly as your previous script per your instructions
EXCLUDED_KEYWORDS = [
     "antim ardaas", "bhog", "bhogg",
]

# Database Configurations (Updated to target live streams)
COLLECTION_NAME = "liveStreams"
ALL_IDS_DOC = "-All_Live_Videos_Id"
LIVE_SCAN_LIMIT = 15  # 🔢 latest streams to scan per channel (yt-dlp --playlist-end)

# Parallel scan tuning (overridable via env)
SCAN_WORKERS = int(os.environ.get("LIVE_SCAN_WORKERS", "8"))
SCAN_TIMEOUT = int(os.environ.get("LIVE_SCAN_TIMEOUT", "180"))  # seconds per channel

# One yt-dlp pass per channel returns ONLY currently-live streams, already carrying
# every field the Firebase doc needs — no second full-extraction step is required.
# %(...j) prints a compact JSON object we can parse line-by-line.
LIVE_PRINT_TEMPLATE = (
    "%(.{id,title,channel,channel_id,uploader,uploader_id,"
    "concurrent_view_count,view_count,release_timestamp,timestamp,"
    "upload_date,live_status})j"
)

# Env variable for single service account (no YouTube API key needed anymore)
SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

if not SERVICE_ACCOUNT:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

# ---------------- FIREBASE SINGLE INIT ----------------
print("🔌 Initializing Firebase Connection for Bhakti App...")

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT))
app_bhakti = firebase_admin.initialize_app(cred, name='bhakti_app')
db = firestore.client(app=app_bhakti)


# ---------------- FAST LIVE SCAN (single-pass per channel) ----------------
def scan_channel_live(channel_id, limit):
    """Return a list of info dicts for streams that are LIVE RIGHT NOW on a channel.

    Uses `--match-filter "live_status=is_live"` so yt-dlp only emits currently-live
    videos, and `--print <json>` so each emitted line already contains the details
    we need. Never raises: on any failure it logs and returns an empty list (that
    channel simply contributes nothing this run — nothing is deleted as a result).
    """
    url = f"https://www.youtube.com/channel/{channel_id}/streams"
    cmd = base_args() + [
        "--playlist-end", str(limit),
        "--match-filter", "live_status=is_live",
        "--ignore-no-formats-error",   # ended/interrupted streams have no formats: don't abort the channel
        "--no-download",
        "--print", LIVE_PRINT_TEMPLATE,
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"⚠️ Live scan timed out for {channel_id}; skipping this channel this run.")
        return []
    except Exception as e:
        print(f"⚠️ Live scan failed for {channel_id}: {e}")
        return []

    infos = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Belt-and-suspenders: only trust rows yt-dlp itself marked is_live.
        if info.get("id") and info.get("live_status") == "is_live":
            infos.append(info)
    return infos


def scan_all_live(channel_ids, limit):
    """Scan every channel in parallel. Returns {video_id: info} for all live-now streams."""
    live = {}
    workers = max(1, min(SCAN_WORKERS, len(channel_ids)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(scan_channel_live, cid, limit): cid
            for cid in channel_ids
        }
        for future in as_completed(future_map):
            cid = future_map[future]
            try:
                for info in future.result():
                    # First occurrence wins (a given id only appears once anyway).
                    live.setdefault(info["id"], info)
            except Exception as e:
                print(f"⚠️ Live scan failed for {cid}: {e}")
    return live


# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
raw_ids = doc.to_dict().get("video_id", []) if doc.exists else []
existing_ids = set(raw_ids) if isinstance(raw_ids, (list, tuple, set)) else set()

print(f"📦 Existing in Bhakti App: {len(existing_ids)}")

# ---------------- ONE FAST PARALLEL SCAN (discovery + live-check + details) ----------------
print("\n---------------- STARTING FAST LIVE SCAN ----------------")
print(f"🔍 Scanning {len(CHANNEL_IDS)} channels in parallel (match-filter live_status=is_live)...")
scan_start = time.time()
live_now = scan_all_live(CHANNEL_IDS, LIVE_SCAN_LIMIT)
print(f"⚡ Scan complete in {time.time() - scan_start:.1f}s — {len(live_now)} live stream(s) found across channels.")

# ---------------- COUNTERS ----------------
total_deleted = 0
total_fetched = len(live_now)
total_skipped_existing = 0
total_skipped_keywords = 0
total_skipped_duplicate_titles = 0
total_inserted = 0

new_ids = []

# ---------------- CLEANUP STALE LIVE STREAMS (SAFE / DEFINITIVE) ----------------
# A stored stream is a "suspect" only if it did NOT appear in the fresh live scan.
# We NEVER delete on the scan alone: if the scan window missed it or a channel scan
# failed, we do an explicit, definitive re-extract and delete ONLY when yt-dlp
# confirms a non-live status. Unverifiable streams are left in place for next run.
if existing_ids:
    suspects = [vid for vid in existing_ids if vid not in live_now]
    still_live_from_scan = len(existing_ids) - len(suspects)
    print(f"\n🔄 {still_live_from_scan} stored stream(s) confirmed still live by scan; "
          f"re-checking {len(suspects)} suspect(s) definitively...")

    stale_ids = set()
    if suspects:
        details = extract_videos(suspects)
        if not details:
            # Total extraction failure: do NOT delete anything (safety first).
            print("⚠️ Could not verify any suspect (yt-dlp returned nothing); "
                  "skipping stale cleanup this run to protect the database.")
        else:
            for vid in suspects:
                info = details.get(vid)
                if info is None:
                    print(f"⚠️ Could not verify {vid}; leaving in place for next run.")
                    continue
                if live_status(info) != "is_live":
                    stale_ids.add(vid)

    if stale_ids:
        print(f"🗑️ {len(stale_ids)} stream(s) definitively no longer live. Cleaning up...")
        for vid in stale_ids:
            target_url = f"https://www.youtube.com/watch?v={vid}"

            existing_ids.discard(vid)
            docs = db.collection(COLLECTION_NAME).where(
                filter=FieldFilter("url", "==", target_url)
            ).stream()
            for doc_item in docs:
                doc_id = doc_item.id
                doc_item.reference.delete()
                # Remove from Search_Collection
                db.collection("Search_Collection").document("streams").set({
                    doc_id: firestore.DELETE_FIELD
                }, merge=True)
            total_deleted += 1

        # Update ALL_IDS_DOC index after deletions
        db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
            "video_id": list(existing_ids), "total_count": len(existing_ids)
        }, merge=True)
    else:
        print("✅ No stale streams to remove.")

# ---------------- SELECT NEW LIVE STREAMS TO INSERT ----------------
# Everything in live_now is already confirmed is_live and already carries full
# details, so there is NO second extraction step. We just filter + dedup + insert.
print("\n🧹 Selecting new live streams (existing/keyword filters)...")
candidates = []
for vid, info in live_now.items():
    title = (info.get("title") or "").strip()
    if not title:
        continue

    # Filter A: already stored
    if vid in existing_ids:
        total_skipped_existing += 1
        continue

    # Filter B: excluded keywords (whole word, case-insensitive)
    found_keyword = False
    for keyword in EXCLUDED_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            found_keyword = True
            print(f"🛑 Bad Keyword '{keyword}': {title[:40]}...")
            break
    if found_keyword:
        total_skipped_keywords += 1
        continue

    info["title"] = title
    candidates.append((vid, info))

# Title deduplication among the new candidates
print("👯 De-duplicating titles among new candidates...")
unique_candidates = []
seen_titles = set()
for vid, info in candidates:
    title = info["title"]
    if title in seen_titles:
        print(f"👯 Skipped Duplicate Title: {title[:40]}...")
        total_skipped_duplicate_titles += 1
        continue
    seen_titles.add(title)
    unique_candidates.append((vid, info))

if not unique_candidates:
    print("✅ No new live streams to insert.")
else:
    # ---------------- FIREBASE PUSH ----------------
    print(f"\n🚀 Inserting {len(unique_candidates)} confirmed live stream(s) into Bhakti App...")
    for vid, info in unique_candidates:
        title = info["title"]

        ch_id = info.get("channel_id") or info.get("uploader_id") or ""
        logo_url = fetch_channel_logo(ch_id) if ch_id else ""
        final_image_url = get_working_image_url(vid)

        base_doc_data = {
            "channelLogoUrl": logo_url,
            "channelName": channel_name(info),
            "imageUrl": final_image_url,
            "isLive": True,
            "timeAgo": published_ms(info),
            "title": title,
            "titleLowercase": title.lower(),
            "url": f"https://www.youtube.com/watch?v={vid}",
            # Store TOTAL views (not concurrent viewers) so this value stays
            # consistent with Update-Live-Videos.py, which refreshes it from the
            # Data API's statistics.viewCount for still-live streams.
            "viewCount": view_count(info),
            "timestamp": str(int(time.time() * 1000)),
        }

        doc_ref = db.collection(COLLECTION_NAME).document()
        doc_ref.set(base_doc_data)

        # Safely save to Search_Collection
        db.collection("Search_Collection").document("streams").set({
            doc_ref.id: base_doc_data["titleLowercase"]
        }, merge=True)

        existing_ids.add(vid)
        new_ids.append(vid)
        total_inserted += 1

        print(f"➕ Inserted LIVE STREAM: {vid} - {title[:30]}...")

# ---------------- UPDATE ID INDEXES ----------------
if new_ids:
    print(f"\n💾 Updating {ALL_IDS_DOC} index for Bhakti App...")
    db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids),
        "total_count": len(existing_ids)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"🗑️  Stale Streams Deleted   : {total_deleted}")
print(f"📥 Live Found (yt-dlp scan) : {total_fetched}")
print(f"⏭️  Skipped (Already in DB) : {total_skipped_existing}")
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti App   : {total_inserted} (Total Live: {len(existing_ids)})")
print("========================================")
