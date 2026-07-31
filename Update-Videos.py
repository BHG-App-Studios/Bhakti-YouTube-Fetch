#!/usr/bin/env python3
"""Update existing videos via the official YouTube Data API.

Two jobs only:
1. Remove videos that are deleted/private (absent from the API response).
2. Update view counts for videos that still exist.

The Data API's videos.list (part=statistics) checks up to 50 IDs per call at a
cost of 1 quota unit each — fast for thousands of videos and with no bot-detection
risk, unlike yt-dlp. A deleted/private video is simply omitted from the response,
which is the definitive signal that it is gone.
"""

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
import sys
import time

import requests

# ---------------- CONFIG ----------------
COLLECTION_NAME = "Listen_Kirtans_Videos_New"
ALL_IDS_DOC = "-All_Videos_Id"
API_CHUNK_SIZE = 50  # YouTube Data API allows up to 50 IDs per videos.list call

SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

# ---------------- FIREBASE INIT ----------------
print("🔌 Initializing Firebase Connection...")

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT))
app = firebase_admin.initialize_app(cred, name='update_app')
db = firestore.client(app=app)


def chunk_list(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


# ---------------- READ ALL VIDEO IDS ----------------
print(f"\n📖 Reading all video IDs from {COLLECTION_NAME}/{ALL_IDS_DOC}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
if not doc.exists:
    print("⚠️ No video index found. Nothing to update.")
    sys.exit(0)

video_ids = doc.to_dict().get("video_id", [])
if not video_ids:
    print("⚠️ Video ID list is empty. Nothing to update.")
    sys.exit(0)

print(f"📦 Found {len(video_ids)} videos to check")

# ---------------- QUERY YOUTUBE DATA API ----------------
print("\n📡 Checking video status & view counts via YouTube Data API...")

API_URL = "https://www.googleapis.com/youtube/v3/videos"
present_ids = set()      # IDs the API confirmed still exist
fresh_views = {}         # id -> current viewCount (only where available)
verified_ids = set()     # IDs that were part of a SUCCESSFUL API call

for chunk in chunk_list(video_ids, API_CHUNK_SIZE):
    params = {
        "part": "statistics",
        "id": ",".join(chunk),
        "key": YOUTUBE_API_KEY,
        "maxResults": API_CHUNK_SIZE,
    }
    try:
        r = requests.get(API_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "items" not in data:
            raise ValueError("YouTube API returned an invalid payload")
    except Exception as e:
        # Batch failed (network/quota/etc.) — DO NOT touch these IDs this run.
        print(f"⚠️ API batch failed (kept untouched): {str(e)[:150]}")
        continue

    verified_ids.update(chunk)  # every ID in this chunk was actually checked
    for item in data.get("items", []):
        vid = item["id"]
        present_ids.add(vid)
        stats = item.get("statistics", {})
        if "viewCount" in stats:
            fresh_views[vid] = int(stats["viewCount"])

if not verified_ids:
    print("❌ No API batch succeeded (bad key/quota?); no database changes made.")
    sys.exit(1)

# A video is GONE only if it was verified in a successful batch but not returned.
gone_ids = verified_ids - present_ids

print(f"✅ Confirmed existing : {len(present_ids)}")
print(f"🗑️  Deleted/private    : {len(gone_ids)}")
unverified = len(video_ids) - len(verified_ids)
if unverified:
    print(f"⏭️  Unverified (skipped): {unverified}")

# ---------------- DELETE GONE VIDEOS ----------------
total_deleted = 0

if gone_ids:
    print(f"\n🗑️ Removing {len(gone_ids)} deleted/private videos from Firebase...")
    for vid in gone_ids:
        target_url = f"https://www.youtube.com/watch?v={vid}"
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
            print(f"   🗑️ Deleted: {vid}")

    # Rewrite the -All_Videos_Id index (keeps unverified IDs, drops only gone ones)
    remaining_ids = [vid for vid in video_ids if vid not in gone_ids]
    db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": remaining_ids,
        "total_count": len(remaining_ids)
    }, merge=True)
    print(f"💾 Updated {ALL_IDS_DOC} index: {len(remaining_ids)} videos remain")

# ---------------- UPDATE VIEW COUNTS ----------------
total_updated = 0

if fresh_views:
    print(f"\n🔄 Updating view counts for {len(fresh_views)} videos...")
    for vid, new_view_count in fresh_views.items():
        target_url = f"https://www.youtube.com/watch?v={vid}"
        docs = db.collection(COLLECTION_NAME).where(
            filter=FieldFilter("url", "==", target_url)
        ).stream()
        for doc_item in docs:
            doc_item.reference.update({"viewCount": new_view_count})
            total_updated += 1
        time.sleep(0.01)  # tiny throttle for Firebase writes
    print(f"✅ Updated view counts for {total_updated} videos")

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📦 Total videos checked     : {len(video_ids)}")
print(f"🗑️  Deleted/private removed  : {total_deleted}")
print(f"🔄 View counts updated      : {total_updated}")
print(f"📊 Videos remaining in DB   : {len(video_ids) - total_deleted}")
print("========================================")
