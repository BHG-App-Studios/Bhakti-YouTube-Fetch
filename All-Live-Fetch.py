#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
import sys
import time
import re

from youtube_ytdlp import (
    list_channel_entries,
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
    "UCsCY7yimnS3FCIo-SCXD-Zg",
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


# ---------------- HELPER ----------------
def find_active_live_ids(video_ids):
    """Extract details for the given ids and return {id: info} for those live now.

    Returns None on total extraction failure so callers can abort safely.
    """
    if not video_ids:
        return {}
    details = extract_videos(video_ids)
    if not details:
        return None
    active = {}
    for vid, info in details.items():
        if live_status(info) == "is_live":
            active[vid] = info
            print(f"🔴 Detected Active LIVE stream: {vid}")
    return active


# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
raw_ids = doc.to_dict().get("video_id", []) if doc.exists else []
existing_ids = set(raw_ids) if isinstance(raw_ids, (list, tuple, set)) else set()

print(f"📦 Existing in Bhakti App: {len(existing_ids)}")

# ---------------- CLEANUP STALE LIVE STREAMS ----------------
total_deleted = 0

if existing_ids:
    print(f"\n🔄 Checking {len(existing_ids)} previously saved live streams...")
    details = extract_videos(list(existing_ids))
    if not details:
        print("❌ yt-dlp unavailable; aborting stale-stream cleanup and this run.")
        sys.exit(1)

    # Only delete a stream when yt-dlp DEFINITIVELY confirms it is no longer live
    # (extracted OK with a non-live status, e.g. was_live/post_live). If a video
    # could not be extracted at all (transient error / private / removed), leave it
    # for the next run instead of risking deletion of a stream that is still live.
    stale_ids = set()
    for vid in existing_ids:
        info = details.get(vid)
        if info is None:
            print(f"⚠️ Could not verify {vid}; leaving in place for next run.")
            continue
        if live_status(info) != "is_live":
            stale_ids.add(vid)

    if stale_ids:
        print(f"🗑️ Found {len(stale_ids)} streams no longer live. Cleaning up...")
        for vid in stale_ids:
            target_url = f"https://www.youtube.com/watch?v={vid}"

            # App Cleanup
            existing_ids.remove(vid)
            docs = db.collection(COLLECTION_NAME).where(filter=FieldFilter("url", "==", target_url)).stream()
            for doc_item in docs:
                doc_id = doc_item.id
                doc_item.reference.delete()
                # Remove from Search_Collection
                db.collection("Search_Collection").document("streams").set({
                    doc_id: firestore.DELETE_FIELD
                }, merge=True)
            total_deleted += 1

        # Update ALL_IDS_DOC index
        if total_deleted > 0:
            db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
                "video_id": list(existing_ids), "total_count": len(existing_ids)
            }, merge=True)
    else:
        print("✅ All previously saved streams are still actively live.")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_no_live_word = 0
total_skipped_existing = 0
total_skipped_keywords = 0
total_skipped_not_live = 0
total_skipped_duplicate_titles = 0
total_inserted = 0

new_ids = []

# ---------------- MAIN LOGIC PIPELINE ----------------

# STEP 1: Gather recent streams per channel via yt-dlp (the /streams tab).
print("\n---------------- STARTING yt-dlp STREAMS SCAN ----------------")
scanned_videos = []
for channel_id in CHANNEL_IDS:
    print(f"🔍 Scanning channel: {channel_id}")
    videos = list_channel_entries(channel_id, tab="streams", limit=LIVE_SCAN_LIMIT)
    total_fetched += len(videos)
    scanned_videos.extend(videos)

# STEP 2: The "Live" Word Title Hack & Exclusions (NO extraction cost yet)
print("\n🧹 Filtering out obvious non-live videos, existing DB videos, and bad keywords...")
candidates_for_extract = []
seen_ids = set()

for v in scanned_videos:
    vid = v["video_id"]
    title = v["title"]

    # Filter A: The "Live" Word Hack
    if "live" not in title.lower():
        total_skipped_no_live_word += 1
        continue

    # Filter B: Existing in DB Check
    if vid in existing_ids:
        total_skipped_existing += 1
        continue

    if vid in seen_ids:
        continue

    # Filter C: Excluded Bad Keywords check
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

    candidates_for_extract.append(v)
    seen_ids.add(vid)

print(f"\n📝 Candidates surviving local filters needing extraction: {len(candidates_for_extract)}")

if not candidates_for_extract:
    print("✅ No new valid candidates found to verify with yt-dlp.")
    sys.exit(0)

# STEP 3: Real Live Check via yt-dlp
print("\n📡 Checking Real Live status & fetching details via yt-dlp...")
candidate_ids = [v["video_id"] for v in candidates_for_extract]
active_live_details = find_active_live_ids(candidate_ids)

if active_live_details is None:
    print("❌ yt-dlp unavailable; no database changes will be made.")
    sys.exit(1)

# Keep ONLY the candidates confirmed currently LIVE
live_candidates = [v for v in candidates_for_extract if v["video_id"] in active_live_details]
total_skipped_not_live = len(candidates_for_extract) - len(live_candidates)

if not live_candidates:
    print("✅ No confirmed active live streams found right now.")
    sys.exit(0)

# STEP 4: Title Deduplication
print("\n👯 Checking for Duplicate Titles among confirmed Live streams...")
unique_live_candidates = []
seen_titles = set()

for v in live_candidates:
    title = active_live_details[v["video_id"]].get("title") or v["title"]
    v["title"] = title
    if title in seen_titles:
        print(f"👯 Skipped Duplicate Title: {title[:40]}...")
        total_skipped_duplicate_titles += 1
    else:
        seen_titles.add(title)
        unique_live_candidates.append(v)

live_candidates = unique_live_candidates

if not live_candidates:
    print("✅ No unique active live streams found after deduplication.")
    sys.exit(0)

# STEP 5: Firebase Push
print("\n🚀 Starting Firebase Insertion for Final Confirmed Streams...")

for v in live_candidates:
    vid = v["video_id"]
    info = active_live_details[vid]
    title = info.get("title") or v["title"]

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
        "viewCount": view_count(info, live=True),
        "timestamp": str(int(time.time() * 1000)),
    }

    # Insert into Bhakti App DB
    if vid not in existing_ids:
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
        time.sleep(0.03)

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
print(f"📥 Total Scanned (yt-dlp)   : {total_fetched}")
print(f"✂️  Skipped (No 'Live' word): {total_skipped_no_live_word}")
print(f"⏭️  Skipped (Already in DB) : {total_skipped_existing}")
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"🗑️  Skipped (Not Live)      : {total_skipped_not_live}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti App   : {total_inserted} (Total Live: {len(existing_ids)})")
print("========================================")
