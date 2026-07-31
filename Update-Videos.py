#!/usr/bin/env python3
"""Update existing videos: refresh view counts and remove deleted/private videos.

Reads all saved video IDs from the -All_Videos_Id index, then uses yt-dlp to:
1. Detect deleted/private videos (extraction fails or returns unavailable status)
2. Update view counts for videos that still exist

Deleted/private videos are removed from:
- Listen_Kirtans_Videos_New collection (the document)
- Search_Collection/streams (the search entry)
- -All_Videos_Id index (the ID list)
"""

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import os
import sys
import time

from youtube_ytdlp import extract_videos, view_count, confirm_gone

# ---------------- CONFIG ----------------
COLLECTION_NAME = "Listen_Kirtans_Videos_New"
ALL_IDS_DOC = "-All_Videos_Id"

# Env variable for Firebase service account
SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

if not SERVICE_ACCOUNT:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

# ---------------- FIREBASE INIT ----------------
print("🔌 Initializing Firebase Connection...")

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT))
app = firebase_admin.initialize_app(cred, name='update_app')
db = firestore.client(app=app)

# ---------------- READ ALL VIDEO IDS ----------------
print(f"\n📖 Reading all video IDs from {COLLECTION_NAME}/{ALL_IDS_DOC}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
if not doc.exists:
    print("⚠️ No video index found. Nothing to update.")
    sys.exit(0)

data = doc.to_dict()
video_ids = data.get("video_id", [])
if not video_ids:
    print("⚠️ Video ID list is empty. Nothing to update.")
    sys.exit(0)

print(f"📦 Found {len(video_ids)} videos to check")

# ---------------- EXTRACT ALL VIDEOS VIA YT-DLP ----------------
print("\n⏱️ Extracting video details via yt-dlp (parallel batches)...")
print("   This checks availability and fetches current view counts...")

details_map = extract_videos(video_ids)

if not details_map:
    print("❌ yt-dlp returned no details; aborting update run.")
    sys.exit(1)

print(f"✅ yt-dlp returned details for {len(details_map)}/{len(video_ids)} videos")

# ---------------- CLASSIFY: GONE vs AVAILABLE ----------------
# Videos that failed extraction might be gone, but could also be transient network
# errors. Re-verify suspected-gone videos with a clean lightweight check that
# distinguishes "Video unavailable" errors (permanent) from timeouts (transient).

suspected_gone = set()
available_ids = set()

for vid in video_ids:
    info = details_map.get(vid)
    if info is None:
        # yt-dlp could not extract this video → suspect gone, verify below
        suspected_gone.add(vid)
    else:
        # Video returned metadata; check availability field
        availability = info.get("availability")
        # yt-dlp availability: public, unlisted, private, needs_auth, premium_only, etc.
        if availability in ("private", "subscriber_only", "premium_only", "needs_auth"):
            suspected_gone.add(vid)
        else:
            available_ids.add(vid)

print(f"\n🔍 Videos suspect gone (re-verifying): {len(suspected_gone)}")
print(f"✅ Videos confirmed available: {len(available_ids)}")

# Re-verify suspected-gone videos with clean error-message checks
gone_ids = set()
if suspected_gone:
    print(f"   Re-checking {len(suspected_gone)} suspected videos for definitive errors...")
    gone_ids = confirm_gone(suspected_gone)
    print(f"🗑️  Confirmed deleted/private: {len(gone_ids)}")
    kept_after_verify = suspected_gone - gone_ids
    if kept_after_verify:
        # Transient failures: keep them in the DB untouched. We have no fresh
        # metadata for them this run, so their view count simply isn't updated.
        print(f"✅ Re-verified as OK (transient issue, left untouched): {len(kept_after_verify)}")

# ---------------- DELETE GONE VIDEOS ----------------
total_deleted = 0

if gone_ids:
    print(f"\n🗑️ Removing {len(gone_ids)} deleted/private videos from Firebase...")

    for vid in gone_ids:
        target_url = f"https://www.youtube.com/watch?v={vid}"

        # Find and delete the document(s) with this URL
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

    # Update the -All_Videos_Id index (remove gone IDs)
    remaining_ids = [vid for vid in video_ids if vid not in gone_ids]
    db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": remaining_ids,
        "total_count": len(remaining_ids)
    }, merge=True)

    print(f"💾 Updated {ALL_IDS_DOC} index: {len(remaining_ids)} videos remain")

# ---------------- UPDATE VIEW COUNTS ----------------
total_updated = 0

if available_ids:
    print(f"\n🔄 Updating view counts for {len(available_ids)} available videos...")

    for vid in available_ids:
        info = details_map.get(vid)
        if info is None:
            continue  # no fresh metadata (transient failure) → skip update
        new_view_count = view_count(info)
        target_url = f"https://www.youtube.com/watch?v={vid}"

        # Find the document with this URL and update its viewCount
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
