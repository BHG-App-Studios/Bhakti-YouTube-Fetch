#!/usr/bin/env python3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os
import sys
import time
import re

# ---------------- CONFIG ----------------
# Add your Bhakti channel IDs here
CHANNEL_IDS = [
    "UCiMASbpDUjNvy5CJAmfekOw",
]

# 🚫 Keywords to exclude (Case Insensitive, Whole Words Only)
EXCLUDED_KEYWORDS = [
    "antim ardaas", "bhog", "bhogg",
]

# Database Configurations
COLLECTION_NAME = "liveStreams"
ALL_IDS_DOC = "-All_Live_Videos_Id"  

# Env variables for Bhakti App
SERVICE_ACCOUNT_BHAKTI = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BHAKTI")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_BHAKTI:
    print("❌ FIREBASE_SERVICE_ACCOUNT_BHAKTI env var missing")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE INIT ----------------
print("🔌 Initializing Firebase Connection for Bhakti App...")

cred_bhakti = credentials.Certificate(json.loads(SERVICE_ACCOUNT_BHAKTI))
app_bhakti = firebase_admin.initialize_app(cred_bhakti, name='bhakti_app')
db_bhakti = firestore.client(app=app_bhakti)

# ---------------- HELPER METHODS ----------------
def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def get_ONLY_live_streams_batch(video_ids):
    active_live_ids = set()
    CHUNK_SIZE = 50 
    
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY,
            "maxResults": 50
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            for item in data.get("items", []):
                vid = item["id"]
                broadcast_content = item["snippet"].get("liveBroadcastContent", "none")
                
                # ONLY grab videos that are actively "live"
                if broadcast_content == "live":
                    active_live_ids.add(vid)
                    print(f"🔴 Detected Active LIVE stream: {vid}")
        except Exception as e:
            print(f"⚠️ Error checking live status: {e}")
    return active_live_ids

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

# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc_bhakti = db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
existing_ids_bhakti = set(doc_bhakti.to_dict().get("video_id", [])) if doc_bhakti.exists else set()

print(f"📦 Existing in Bhakti App: {len(existing_ids_bhakti)}")

# ---------------- CLEANUP STALE LIVE STREAMS ----------------
total_deleted_bhakti = 0

if existing_ids_bhakti:
    print(f"\n🔄 Checking {len(existing_ids_bhakti)} previously saved live streams...")
    still_live_ids = get_ONLY_live_streams_batch(list(existing_ids_bhakti))
    stale_ids = existing_ids_bhakti - still_live_ids

    if stale_ids:
        print(f"🗑️ Found {len(stale_ids)} streams no longer live. Cleaning up...")
        
        for vid in stale_ids:
            target_url = f"https://www.youtube.com/watch?v={vid}"
            
            existing_ids_bhakti.remove(vid)
            # Find and delete document by matching the url
            docs = db_bhakti.collection(COLLECTION_NAME).where("url", "==", target_url).stream()
            for doc in docs:
                doc.reference.delete()
            total_deleted_bhakti += 1

        # Update ALL_IDS_DOC array and count after deletions
        if total_deleted_bhakti > 0:
            db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
                "video_id": list(existing_ids_bhakti),
                "total_count": len(existing_ids_bhakti)
            }, merge=True)
            print(f"✅ Updated Bhakti {ALL_IDS_DOC} (Removed {total_deleted_bhakti} stale streams)")
    else:
        print("✅ All previously saved streams are still actively live.")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_existing = 0
total_skipped_not_live = 0
total_skipped_keywords = 0
total_skipped_duplicate_titles = 0
total_inserted_bhakti = 0
new_ids_bhakti = []

# ---------------- RSS FETCH ----------------
def fetch_videos_from_channel(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"⚠️ Error fetching channel {channel_id}: {e}")
        return []

    root = ET.fromstring(response.text)
    videos = []
    entries = root.findall("atom:entry", NS)
    
    for entry in entries:
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        
        if title_el is None or video_id_el is None:
            continue

        video_id = video_id_el.text.strip()

        videos.append({
            "video_id": video_id,
            "title": title_el.text.strip(),
            "url": f"https://www.youtube.com/watch?v={video_id}"
        })
    return videos

# ---------------- MAIN LOGIC ----------------
rss_videos = []

# 1. Gather all videos from RSS
print("\n---------------- STARTING RSS FETCH ----------------")
for channel_id in CHANNEL_IDS:
    print(f"🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    total_fetched += len(videos)
    rss_videos.extend(videos)

# 2. Filter out Existing IDs 
candidates = []
for v in rss_videos:
    vid = v["video_id"]
    if vid in existing_ids_bhakti:
        total_skipped_existing += 1
        continue
    if any(c["video_id"] == vid for c in candidates):
        continue
    candidates.append(v)

print(f"\n📝 Candidates needing processing (missing in DB): {len(candidates)}")

if not candidates:
    print("✅ No new videos to process for the database.")
    sys.exit(0)

candidate_ids = [v["video_id"] for v in candidates]

# 3. Check Live Status (API Call)
print("\n📡 Checking Live status (Filtering OUT normal videos)...")
active_live_ids = get_ONLY_live_streams_batch(candidate_ids)

# Keep ONLY the candidates that are currently LIVE
live_candidates = [v for v in candidates if v["video_id"] in active_live_ids]
total_skipped_not_live = len(candidates) - len(live_candidates)

if not live_candidates:
    print("✅ No new active live streams found right now.")
    sys.exit(0)

# 3.5 Deduplicate by EXACT title match before inserting
unique_live_candidates = []
seen_titles = set()

for v in live_candidates:
    if v["title"] in seen_titles:
        print(f"👯 Skipped Duplicate Title: {v['title'][:40]}...")
        total_skipped_duplicate_titles += 1
    else:
        seen_titles.add(v["title"])
        unique_live_candidates.append(v)

live_candidates = unique_live_candidates

if not live_candidates:
    print("✅ No unique active live streams found right now.")
    sys.exit(0)

# 4. Insert Final Live Streams into DB
print("\n🚀 Starting Final Filtering & Firebase Insertion...")
for v in live_candidates:
    vid = v["video_id"]
    title = v["title"]
    
    # --- FILTER 1: Title Keywords ---
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

    # --- PREPARE DATABASE DOCUMENT (Strictly only requested fields) ---
    final_image_url = get_working_image_url(vid)
    doc_data = {
        "title": v["title"],
        "url": v["url"],
        "imageUrl": final_image_url
    }

    # Insert into Bhakti App DB
    db_bhakti.collection(COLLECTION_NAME).document().set(doc_data)
    existing_ids_bhakti.add(vid)
    new_ids_bhakti.append(vid)
    total_inserted_bhakti += 1
    
    print(f"➕ Inserted LIVE STREAM: {vid} - {title[:30]}...")
    time.sleep(0.03)

# ---------------- UPDATE ID INDEX ----------------
if new_ids_bhakti:
    print(f"\n💾 Updating {ALL_IDS_DOC} index for Bhakti App...")
    db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": list(existing_ids_bhakti),
        "total_count": len(existing_ids_bhakti)
    }, merge=True)

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"🗑️  Stale Streams Deleted   : {total_deleted_bhakti}")
print(f"📥 Total RSS Fetched        : {total_fetched}")
print(f"⏭️  Skipped (Already in DB) : {total_skipped_existing}")
print(f"🗑️  Skipped (Normal Videos) : {total_skipped_not_live}")
print(f"🛑 Skipped (Keywords)       : {total_skipped_keywords}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti      : {total_inserted_bhakti} (Total Live: {len(existing_ids_bhakti)})")
print("========================================")
