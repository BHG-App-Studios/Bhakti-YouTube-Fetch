#!/usr/bin/env python3
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from bs4 import BeautifulSoup
import json
import os
import sys
import time
import re

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
COLLECTION_NAME = "liveStreams_More"
ALL_IDS_DOC = "-All_Live_Videos_Id"  

# Env variables for single service account
SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

if not SERVICE_ACCOUNT:
    print("❌ FIREBASE_SERVICE_ACCOUNT env var missing")
    sys.exit(1)

if not YOUTUBE_API_KEY:
    print("❌ YOUTUBE_API_KEY env var missing")
    sys.exit(1)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015"
}

# ---------------- FIREBASE SINGLE INIT ----------------
print("🔌 Initializing Firebase Connection for Bhakti App...")

cred = credentials.Certificate(json.loads(SERVICE_ACCOUNT))
app_bhakti = firebase_admin.initialize_app(cred, name='bhakti_app')
db = firestore.client(app=app_bhakti)

# ---------------- HELPER METHODS ----------------
def fetch_channel_logo(channel_id):
    """Scrapes the channel HTML for the logo (Cost: 0 Units)"""
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    print(f"🖼️ Scraping Logo from: {channel_url}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    try:
        response = requests.get(channel_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        meta_image = soup.find('meta', property='og:image')
        if meta_image and meta_image.get('content'):
            print(f"✅ Logo found: {meta_image['content']}")
            return meta_image['content']
    except Exception as e:
        print(f"❌ Error scraping logo: {e}")
    return ""

def chunk_list(data, chunk_size):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]

def get_live_streams_details_batch(video_ids):
    """Checks live status and grabs statistics & channel info (Cost: 1 Unit per 50 videos)"""
    active_live_details = {}
    CHUNK_SIZE = 50 
    
    for chunk in chunk_list(video_ids, CHUNK_SIZE):
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,statistics",
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
                    active_live_details[vid] = {
                        "channelName": item["snippet"].get("channelTitle", ""),
                        "channelId": item["snippet"].get("channelId", ""),
                        "viewCount": int(item.get("statistics", {}).get("viewCount", 0))
                    }
                    print(f"🔴 Detected Active LIVE stream: {vid}")
        except Exception as e:
            print(f"⚠️ Error checking live status: {e}")
    return active_live_details

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
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        published_dt = datetime.fromisoformat(
            published_el.text.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        video_id = video_id_el.text.strip()

        videos.append({
            "video_id": video_id,
            "title": title_el.text.strip(),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published": published_dt
        })
    return videos

# ---------------- READ EXISTING IDS ----------------
print(f"\n📖 Fetching existing Video IDs from {COLLECTION_NAME}...")

doc = db.collection(COLLECTION_NAME).document(ALL_IDS_DOC).get()
existing_ids = set(doc.to_dict().get("video_id", [])) if doc.exists else set()

print(f"📦 Existing in Bhakti App: {len(existing_ids)}")

# ---------------- CLEANUP STALE LIVE STREAMS ----------------
total_deleted = 0

if existing_ids:
    print(f"\n🔄 Checking {len(existing_ids)} previously saved live streams...")
    still_live_ids = set(get_live_streams_details_batch(list(existing_ids)).keys())
    stale_ids = existing_ids - still_live_ids

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

# STEP 1: Gather all videos from RSS
print("\n---------------- STARTING RSS FETCH ----------------")
rss_videos = []
for channel_id in CHANNEL_IDS:
    print(f"🔍 Fetching channel: {channel_id}")
    videos = fetch_videos_from_channel(channel_id)
    total_fetched += len(videos)
    rss_videos.extend(videos)

# STEP 2: The "Live" Word Title Hack & Exclusions (NO API COST YET)
print("\n🧹 Filtering out obvious non-live videos, existing DB videos, and bad keywords...")
candidates_for_api = []
seen_rss_ids = set()

for v in rss_videos:
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

    if vid in seen_rss_ids:
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

    candidates_for_api.append(v)
    seen_rss_ids.add(vid)

print(f"\n📝 Candidates surviving local filters needing API checking: {len(candidates_for_api)}")

if not candidates_for_api:
    print("✅ No new valid candidates found to check against YouTube API.")
    sys.exit(0)

# STEP 3: API Call (REAL Live Check)
print("\n📡 Checking Real Live status & fetching details via YouTube API...")
candidate_ids = [v["video_id"] for v in candidates_for_api]
active_live_details = get_live_streams_details_batch(candidate_ids)

# Keep ONLY the candidates that the API confirms are currently LIVE
live_candidates = [v for v in candidates_for_api if v["video_id"] in active_live_details]
total_skipped_not_live = len(candidates_for_api) - len(live_candidates)

if not live_candidates:
    print("✅ No API-confirmed active live streams found right now.")
    sys.exit(0)

# STEP 4: Title Deduplication
print("\n👯 Checking for Duplicate Titles among confirmed Live streams...")
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
    print("✅ No unique active live streams found after deduplication.")
    sys.exit(0)

# STEP 5: Firebase Push
print("\n🚀 Starting Firebase Insertion for Final Confirmed Streams...")
channel_logos = {}

for v in live_candidates:
    vid = v["video_id"]
    title = v["title"]
    
    details = active_live_details[vid]
    channel_id = details["channelId"]
    
    if channel_id not in channel_logos:
        channel_logos[channel_id] = fetch_channel_logo(channel_id)
        
    logo_url = channel_logos[channel_id]
    final_image_url = get_working_image_url(vid)
    published_ms = str(int(v["published"].timestamp() * 1000))

    base_doc_data = {
        "channelLogoUrl": logo_url,
        "channelName": details["channelName"],
        "imageUrl": final_image_url,
        "isLive": True,
        "timeAgo": published_ms,
        "title": v["title"],
        "titleLowercase": v["title"].lower(),
        "url": v["url"],
        "viewCount": details["viewCount"],
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
print(f"📥 Total RSS Fetched        : {total_fetched}")
print(f"✂️  Skipped (No 'Live' word): {total_skipped_no_live_word}")
print(f"⏭️  Skipped (Already in DB) : {total_skipped_existing}")
print(f"🛑 Skipped (Bad Keywords)   : {total_skipped_keywords}")
print(f"🗑️  Skipped (API: Not Live) : {total_skipped_not_live}")
print(f"👯 Skipped (Duplicate Title): {total_skipped_duplicate_titles}")
print(f"➕ Inserted to Bhakti App   : {total_inserted} (Total Live: {len(existing_ids)})")
print("========================================")
