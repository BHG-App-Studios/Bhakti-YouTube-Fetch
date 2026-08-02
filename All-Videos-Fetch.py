#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
import time
import re
import random

from youtube_ytdlp import (
    list_channels,
    extract_videos,
    channel_name,
    channel_id_of,
    live_status,
    view_count,
    duration_seconds,
    format_duration,
    published_ms,
    get_working_image_url,
    fetch_channel_logo,
)

# ---------------- CONFIG ----------------
CHANNEL_IDS = [


    "UCcGjF-pB4bV5vgaqahJ1qpg",
    "UCaayLD9i5x4MmIoVZxXSv_g",
    "UC6vQRTCxutg6fJLUGkDKynQ",
    "UCaF3MVnBYNnjAKF16k3mUjw",
    "UCBAvMHZO3BIfMMhOK9LMOYQ",
    "UCpSTRmTFY7pCzdeHJwAiAEg",
    "UC7Uo3euG3IA0yBlQyIXDcUA",
    "UCmX4QOJHAu2vni7nuGmNT5A",
    "UCL0cLclH8j_qGjQhnn_5skg",
    "UCLIryeFjYeiEtpqNETz_Ydg",
    "UCMGxP9tdDh7yOcd-H8pbfqQ",
    "UC5fbdgYVnVwnEcM2KnaeZ0g",


]

# 🚫 Keywords to exclude (Case Insensitive, Whole Words Only)
EXCLUDED_KEYWORDS = [
    "mahapuran",
    "concert",
    "live",
    "lyrical",
    "song",
    "extended",
    "punjabi",
    "punjab",
    "singh",
]

# Database Configurations
COLLECTION_NAME = "Listen_Kirtans_Videos_New"  # Renamed for Bhakti app context
ALL_IDS_DOC = "-All_Videos_Id"
MIN_DURATION_SECONDS = 180  # ⏱️ 3 minutes
SCAN_LIMIT = 50             # 🔢 latest videos to scan per channel (yt-dlp --playlist-end)

# Env variable for Single service account (no YouTube API key needed anymore)
SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

if not SERVICE_ACCOUNT:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

# ---------------- FIREBASE SINGLE INIT ----------------
print("🔌 Initializing Firebase Connection for Bhakti App...")

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT))
app_bhakti = firebase_admin.initialize_app(cred, name='bhakti_app')
db = firestore.client(app=app_bhakti)

# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
raw_ids = doc.to_dict().get("video_id", []) if doc.exists else []
existing_ids = set(raw_ids) if isinstance(raw_ids, (list, tuple, set)) else set()

print(f"📦 Existing in Bhakti App DB: {len(existing_ids)}")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_existing = 0
total_skipped_live = 0
total_skipped_short = 0
total_skipped_keywords = 0
total_skipped_duplicate_titles = 0
total_inserted = 0
new_ids = []

# ---------------- MAIN LOGIC PIPELINE ----------------

# 1. Gather latest videos per channel via yt-dlp (the /videos tab excludes Shorts).
print("\n---------------- STARTING yt-dlp CHANNEL SCAN ----------------")
print(f"🔍 Scanning {len(CHANNEL_IDS)} channels in parallel...")
scanned_videos = list_channels(CHANNEL_IDS, tab="videos", limit=SCAN_LIMIT)
total_fetched = len(scanned_videos)

# 2. Local Filters (ID & Keyword Exclusions - NO extraction cost)
print("\n🧹 Filtering out existing DB videos and bad keywords locally...")
candidates_for_extract = []
seen_ids = set()

for v in scanned_videos:
    vid = v["video_id"]
    title = v["title"]

    # Filter A: Existing in DB Check
    if vid in existing_ids:
        total_skipped_existing += 1
        continue

    if vid in seen_ids:
        continue

    # Filter B: Bad Keywords Check
    found_keyword = False
    for keyword in EXCLUDED_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, title, re.IGNORECASE):
            found_keyword = True
            print(f"🛑 Skipped (Keyword '{keyword}'): {title[:40]}...")
            break

    if found_keyword:
        total_skipped_keywords += 1
        continue

    # Filter C: Fast Shorts Hack (Drops obvious shorts before extraction)
    if "#shorts" in title.lower():
        print(f"✂️ Skipped (Obvious Short in Title): {title[:40]}...")
        total_skipped_short += 1
        continue

    candidates_for_extract.append(v)
    seen_ids.add(vid)

print(f"\n📝 Candidates surviving local filters needing extraction: {len(candidates_for_extract)}")

if not candidates_for_extract:
    print("✅ No new valid videos to process.")
    sys.exit(0)

# 3. Fetch Complete Video Details via yt-dlp (single process, many videos)
print("\n⏱️ Fetching Full Video Details & Live Status (via yt-dlp)...")
candidate_ids = [v["video_id"] for v in candidates_for_extract]
details_map = extract_videos(candidate_ids)

if not details_map:
    print("❌ yt-dlp returned no video details; no database changes will be made.")
    sys.exit(1)

# 4. Final Filters & Firebase Insertion
print("\n🚀 Starting Final Filtering & Firebase Insertion...")
current_timestamp_ms = str(int(time.time() * 1000))
seen_final_titles = set()

for v in candidates_for_extract:
    vid = v["video_id"]
    info = details_map.get(vid)

    if not info:
        print(f"⚠️ Skipping {vid} because yt-dlp returned no details.")
        continue

    title = info.get("title") or v["title"]

    # --- FINAL FILTER 1: Live Status ---
    if live_status(info) in ("is_live", "is_upcoming"):
        print(f"🚫 Skipped (Live/Upcoming stream): {vid}")
        total_skipped_live += 1
        continue

    # --- FINAL FILTER 2: Duration Check ---
    duration_sec = duration_seconds(info)
    if duration_sec < MIN_DURATION_SECONDS:
        print(f"⏭️ Skipped short ({duration_sec}s): {vid}")
        total_skipped_short += 1
        continue

    # --- FINAL FILTER 3: Title Deduplication ---
    if title in seen_final_titles:
        print(f"👯 Skipped Duplicate Title: {title[:40]}...")
        total_skipped_duplicate_titles += 1
        continue
    seen_final_titles.add(title)

    # --- PREPARE DATA BASE ---
    ch_id = channel_id_of(info)
    duration_formatted = format_duration(duration_sec)
    final_image_url = get_working_image_url(vid)
    logo_url = fetch_channel_logo(ch_id) if ch_id else ""

    base_doc_data = {
        "channelLogoUrl": logo_url,
        "channelName": channel_name(info),
        "channel_id": ch_id,
        "duration": duration_formatted,
        "imageUrl": final_image_url,
        "isLive": False,
        "timeAgo": published_ms(info),
        "timestamp": current_timestamp_ms,
        "title": title,
        "titleLowercase": title.lower(),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "viewCount": view_count(info)
    }

    # Insert into Bhakti App DB
    if vid not in existing_ids:
        # 1. Create a reference to get the auto-generated Document ID
        doc_ref = db.collection(COLLECTION_NAME).document()

        # 2. Set the data into the video collection
        doc_ref.set(base_doc_data)

        # 3. Safely update Search_Collection -> streams with the new ID and lowercase title
        db.collection("Search_Collection").document("streams").set({
            doc_ref.id: base_doc_data["titleLowercase"]
        }, merge=True)

        existing_ids.add(vid)
        new_ids.append(vid)
        total_inserted += 1

        print(f"➕ Inserted ({duration_formatted}): {vid} - {title[:30]}...")
        time.sleep(0.03)

# ---------------- UPDATE ID INDEXES & APP-SETUP ----------------
if new_ids:
    print(f"\n💾 Updating {ALL_IDS_DOC} index for Bhakti App...")
    db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids),
        "total_count": len(existing_ids)
    }, merge=True)

    # Update App-Setup trigger safely
    random_trigger = random.randint(100000000, 999999999)  # Generates random 9-digit number
    print(f"🔄 Updating kirtan_videos_fetch in Bhakti App-Setup to: {random_trigger}")

    # merge=True ensures we don't overwrite other fields in this document
    db.collection("App-Setup").document("App-Setup").set({
        "kirtan_videos_fetch": random_trigger
    }, merge=True)


# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📥 Total Scanned (yt-dlp)   : {total_fetched}")
print(f"⏭️  Skipped (Already in DB)  : {total_skipped_existing}")
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"✂️  Skipped (Shorts)         : {total_skipped_short}")
print(f"🚫 Skipped (Live/Upc)       : {total_skipped_live}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti App   : {total_inserted} (Total: {len(existing_ids)})")
print("========================================")
