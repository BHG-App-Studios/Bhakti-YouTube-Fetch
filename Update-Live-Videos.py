#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys

import requests

# ---------------- CONFIG ----------------
COLLECTION_NAME = "liveStreams"
ALL_IDS_DOC = "-All_Live_Videos_Id"
API_CHUNK_SIZE = 50    # YouTube Data API allows up to 50 IDs per videos.list call
WRITE_BATCH_SIZE = 450  # Firestore hard limit is 500 writes per batch

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
app = firebase_admin.initialize_app(cred, name='update_live_app')
db = firestore.client(app=app)


def chunk_list(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


# ---------------- READ ALL LIVE VIDEO IDS ----------------
print(f"\n📖 Reading live video IDs from {COLLECTION_NAME}/{ALL_IDS_DOC}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
if not doc.exists:
    print("⚠️ No live video index found. Nothing to update.")
    sys.exit(0)

video_ids = doc.to_dict().get("video_id", [])
if not video_ids:
    print("⚠️ Live video ID list is empty. Nothing to update.")
    sys.exit(0)

print(f"📦 Found {len(video_ids)} live streams to check")

# ---------------- QUERY YOUTUBE DATA API ----------------
print("\n📡 Fetching current concurrent viewers via YouTube Data API...")

API_URL = "https://www.googleapis.com/youtube/v3/videos"
fresh_views = {}     # id -> current concurrent viewers (only for streams STILL live)
verified_ids = set()  # IDs that were part of a SUCCESSFUL API call

for chunk in chunk_list(video_ids, API_CHUNK_SIZE):
    params = {
        "part": "liveStreamingDetails",
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

    verified_ids.update(chunk)
    for item in data.get("items", []):
        vid = item["id"]
        details = item.get("liveStreamingDetails") or {}
        cv = details.get("concurrentViewers")
        # concurrentViewers is present ONLY while the stream is live. Missing =>
        # ended / upcoming / not live => skip (leave it for All-Live-Fetch.py).
        if cv is not None:
            try:
                fresh_views[vid] = int(cv)
            except (TypeError, ValueError):
                pass

if not verified_ids:
    print("❌ No API batch succeeded (bad key/quota?); no database changes made.")
    sys.exit(1)

print(f"🔴 Still live (updatable)   : {len(fresh_views)}")
skipped_not_live = len(verified_ids) - len(fresh_views)
print(f"⏭️  Not live now (skipped)   : {skipped_not_live}")

if not fresh_views:
    print("✅ No currently-live streams to update.")
    sys.exit(0)

# ---------------- LOAD COLLECTION ONCE (url -> [doc snapshots]) ----------------
# One streamed read of the collection instead of a Firestore query per video.
print("\n📚 Loading live stream documents (single pass)...")
url_to_docs = {}
for snap in db.collection(COLLECTION_NAME).stream():
    if snap.id == ALL_IDS_DOC:
        continue  # skip the index doc (it has no `url`)
    d = snap.to_dict() or {}
    url = d.get("url")
    if url:
        url_to_docs.setdefault(url, []).append(snap)
print(f"📚 Indexed {sum(len(v) for v in url_to_docs.values())} documents by url")

# ---------------- APPLY CHANGES (batched writes) ----------------
batch = db.batch()
ops_in_batch = 0
total_updated = 0


def flush_batch(force=False):
    global batch, ops_in_batch
    if ops_in_batch and (force or ops_in_batch >= WRITE_BATCH_SIZE):
        batch.commit()
        batch = db.batch()
        ops_in_batch = 0


print(f"\n🔄 Updating view counts for still-live streams...")
for vid, new_view_count in fresh_views.items():
    target_url = f"https://www.youtube.com/watch?v={vid}"
    for snap in url_to_docs.get(target_url, []):
        d = snap.to_dict() or {}
        old_vc = d.get("viewCount")
        try:
            old_vc_int = int(old_vc) if old_vc is not None else None
        except (TypeError, ValueError):
            old_vc_int = None
        if old_vc_int == new_view_count:
            continue  # unchanged; skip the write
        batch.update(snap.reference, {"viewCount": new_view_count})
        ops_in_batch += 1
        total_updated += 1
        flush_batch()

flush_batch(force=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"📦 Live streams checked     : {len(video_ids)}")
print(f"🔴 Still live               : {len(fresh_views)}")
print(f"🔄 View counts updated      : {total_updated}")
print("========================================")
