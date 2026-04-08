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
EXCLUDED_KEYWORDS = [
    "antim ardaas", "bhog", "bhogg",
]

# Database Configurations
COLLECTION_NAME = "liveStreams"
ALL_IDS_DOC = "-All_Live_Videos_Id"  

# Env variables for Bhakti App
SERVICE_ACCOUNT_BHAKTI = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT_BHAKTI:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
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

def normalize_title(title):
    """Removes extra spaces and makes lowercase for strict matching"""
    return " ".join(title.split()).lower()

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
existing_video_map = {}
is_migration_needed = False
doc_exists = doc_bhakti.exists

if doc_exists:
    doc_data = doc_bhakti.to_dict()
    vid_field = doc_data.get("video_id", {})
    
    # Backward compatibility: if the existing field is an array, migrate it to a map
    if isinstance(vid_field, list):
        print("🔄 Detected old Array format. Migrating to Map format...")
        existing_video_map = {vid: "Unknown Title" for vid in vid_field}
        is_migration_needed = True
    elif isinstance(vid_field, dict):
        existing_video_map = vid_field

existing_ids_bhakti = set(existing_video_map.keys())

# Create a set of normalized titles to prevent whitespace/case issues
existing_titles_bhakti = {normalize_title(t) for t in existing_video_map.values()}

# Auto-migrate immediately so subsequent updates don't fail
if is_migration_needed:
    db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC).set({
        "video_id": existing_video_map,
        "total_count": len(existing_video_map)
    })
    print("✅ Migration to Map format complete.")

print(f"📦 Existing in Bhakti App: {len(existing_ids_bhakti)}")

# ---------------- CLEANUP STALE & DUPLICATE LIVE STREAMS ----------------
total_deleted_bhakti = 0
total_deleted_duplicates = 0

if existing_ids_bhakti:
    print(f"\n🔄 Checking {len(existing_ids_bhakti)} previously saved live streams...")
    still_live_ids = get_ONLY_live_streams_batch(list(existing_ids_bhakti))
    
    # 1. Find streams that are no longer live
    stale_ids = existing_ids_bhakti - still_live_ids
    
    # 2. Find streams that are ALREADY duplicates in the database map
    seen_existing_titles = set()
    duplicate_ids_in_db = set()
    
    for vid, title in existing_video_map.items():
        if vid in stale_ids:
            continue # Skip if it's already marked as stale
            
        norm_t = normalize_title(title)
        if norm_t in seen_existing_titles and title != "Unknown Title":
            duplicate_ids_in_db.add(vid)
            total_deleted_duplicates += 1
            print(f"🧹 Found DUPLICATE title already in DB to clean up: {title[:40]}...")
        else:
            seen_existing_titles.add(norm_t)

    # Combine both stale streams and duplicate streams for deletion
    ids_to_remove = stale_ids.union(duplicate_ids_in_db)

    if ids_to_remove:
        print(f"🗑️ Removing {len(stale_ids)} stale streams and {len(duplicate_ids_in_db)} database duplicates...")
        
        # Use DELETE_FIELD and Increment to update safely
        updates = {
            "total_count": firestore.Increment(-len(ids_to_remove))
        }
        
        for vid in ids_to_remove:
            target_url = f"https://www.youtube.com/watch?v={vid}"
            
            existing_ids_bhakti.remove(vid)
            if vid in existing_video_map:
                title = existing_video_map[vid]
                norm_t = normalize_title(title)
                if norm_t in existing_titles_bhakti:
                    # Only remove from the title set if we are deleting the last instance of it
                    existing_titles_bhakti.remove(norm_t) 
                del existing_video_map[vid]
                
            # Add to map field deletion updates using dot notation
            updates[f"video_id.{vid}"] = firestore.DELETE_FIELD
            
            # Find and delete document by matching the url in the collection
            docs = db_bhakti.collection(COLLECTION_NAME).where("url", "==", target_url).stream()
            for doc in docs:
                doc.reference.delete()
            total_deleted_bhakti += 1

        # Update ALL_IDS_DOC after deletions
        if total_deleted_bhakti > 0:
            db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC).update(updates)
            print(f"✅ Updated Bhakti {ALL_IDS_DOC} (Safely Removed {total_deleted_bhakti} streams from map)")
            
            # Rebuild existing titles set after deletion to ensure accuracy
            existing_titles_bhakti = {normalize_title(t) for t in existing_video_map.values()}
    else:
        print("✅ All previously saved streams are actively live and unique.")

# ---------------- COUNTERS ----------------
total_fetched = 0
total_skipped_existing_id = 0
total_skipped_existing_title = 0
total_skipped_not_live = 0
total_skipped_keywords = 0
total_skipped_duplicate_titles = 0
total_inserted_bhakti = 0
new_ids_bhakti = []
new_video_updates = {} # To hold additions for map updates

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

# 2. Filter out Existing IDs AND Titles
candidates = []
for v in rss_videos:
    vid = v["video_id"]
    title = v["title"]
    norm_title = normalize_title(title)
    
    # Check ID match
    if vid in existing_ids_bhakti:
        total_skipped_existing_id += 1
        continue
        
    # Check Title match against the database
    if norm_title in existing_titles_bhakti:
        total_skipped_existing_title += 1
        print(f"👯 Skipped (Title already in DB): {title[:40]}...")
        continue
        
    # Check Title match against other videos already added to candidates in this same run
    if any(normalize_title(c["title"]) == norm_title for c in candidates):
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

# 3.5 Deduplicate by EXACT title match before inserting (Extra safety net)
unique_live_candidates = []
seen_titles = set()

for v in live_candidates:
    norm_title = normalize_title(v["title"])
    if norm_title in seen_titles:
        print(f"👯 Skipped Duplicate Title in Fetch: {v['title'][:40]}...")
        total_skipped_duplicate_titles += 1
    else:
        seen_titles.add(norm_title)
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
    existing_titles_bhakti.add(normalize_title(title))
    existing_video_map[vid] = title
    new_ids_bhakti.append(vid)
    
    # Add new entry into the map update dictionary
    new_video_updates[f"video_id.{vid}"] = title
    
    total_inserted_bhakti += 1
    
    print(f"➕ Inserted LIVE STREAM: {vid} - {title[:30]}...")
    time.sleep(0.03)

# ---------------- UPDATE ID INDEX ----------------
if new_video_updates:
    print(f"\n💾 Safely Updating {ALL_IDS_DOC} Map index for Bhakti App...")
    
    # Safely increase total count by amount inserted
    new_video_updates["total_count"] = firestore.Increment(total_inserted_bhakti)
    
    doc_ref = db_bhakti.collection(COLLECTION_NAME).document(ALL_IDS_DOC)
    
    if doc_exists:
        # Securely append map fields
        doc_ref.update(new_video_updates)
    else:
        # If document didn't exist at all yet, initialize it
        doc_ref.set({
            "video_id": {vid: existing_video_map[vid] for vid in new_ids_bhakti},
            "total_count": total_inserted_bhakti
        })

# ---------------- SUMMARY ----------------
print("\n================ SUMMARY ================")
print(f"🗑️  Total Streams Deleted      : {total_deleted_bhakti} (Stale: {total_deleted_bhakti - total_deleted_duplicates}, Duplicates Cleaned: {total_deleted_duplicates})")
print(f"📥 Total RSS Fetched           : {total_fetched}")
print(f"⏭️  Skipped (ID Already in DB) : {total_skipped_existing_id}")
print(f"⏭️  Skipped (Title already in DB): {total_skipped_existing_title}")
print(f"🗑️  Skipped (Normal Videos)    : {total_skipped_not_live}")
print(f"🛑 Skipped (Keywords)          : {total_skipped_keywords}")
print(f"👯 Skipped (Duplicate Title)   : {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti         : {total_inserted_bhakti} (Total Live: {len(existing_ids_bhakti)})")
print("========================================")
