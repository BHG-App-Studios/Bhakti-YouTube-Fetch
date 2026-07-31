#!/usr/bin/env python3
"""Perceptual-hash thumbnail de-duplication for live streams.

WHY THIS EXISTS
    Some channels broadcast the SAME video under DIFFERENT titles (the app
    screenshot shows two identical "Mahakal" cards from one channel titled
    "Bhasma Aarti Live" and "Sandhya Aarti Live"). The title-based de-dup in
    All-Live-Fetch.py cannot catch those — the titles differ — so both slip
    through and the app shows visually identical live cards. This module catches
    them by comparing the THUMBNAIL IMAGES with a perceptual hash (pHash).

WHY pHash AND NOT AN AI MODEL (e.g. Gemini)
    * pHash is pure CPU work: each image is reduced to a 32x32 grayscale DCT and
      turned into a 64-bit fingerprint. It needs NO GPU and runs in milliseconds,
      so it fits a 2-core / 7 GB GitHub runner comfortably.
    * It is free, offline and deterministic — no API key, no per-call cost, no
      rate limits, no network dependency for the comparison, and no risk of a
      model returning malformed JSON.
    * "Same broadcast, same custom thumbnail" means the images are byte-identical
      or near-identical, which is precisely what pHash is strongest at.

SAFETY RULES
    * Comparison happens ONLY within the same channel (channel_id), never across
      channels, because different channels legitimately reuse generic deity art.
    * FAIL-OPEN: any thumbnail we cannot download or hash is always KEPT. We only
      ever drop a stream when we have confidently hashed BOTH images and they are
      within the distance threshold. Losing a legit stream is worse than letting
      a rare duplicate through.

RETURN CONTRACT
    deduplicate_by_thumbnail(candidates, reserved) -> (kept, dropped)
        kept    : list of (vid, info) — the candidates to actually insert
        dropped : list of (vid, info, matched_vid) — candidates removed as dupes,
                  each paired with the vid of the stream it duplicated
"""

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Pillow + imagehash are optional at import time so the rest of the pipeline
# still runs (fail-open) if they are somehow missing on the runner.
try:
    from io import BytesIO

    import imagehash
    import requests
    from PIL import Image

    _DEPS_OK = True
    _IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - defensive
    _DEPS_OK = False
    _IMPORT_ERROR = str(e)


# ---------------- CONFIG (overridable via env) ----------------
# Max Hamming distance (out of 64 bits) for two thumbnails to count as the SAME
# image. Identical custom thumbnails score ~0; slight recompression drifts a few
# bits. 8 balances catching real duplicates against never dropping genuinely
# different per-day designs (which differ far more than 8 bits).
HAMMING_THRESHOLD = int(os.environ.get("THUMB_DEDUP_HAMMING", "8"))

# Parallel thumbnail downloads (network-bound, so a small pool is plenty).
DOWNLOAD_WORKERS = int(os.environ.get("THUMB_DEDUP_WORKERS", "8"))
DOWNLOAD_TIMEOUT = int(os.environ.get("THUMB_DEDUP_TIMEOUT", "10"))

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _channel_of(info):
    return info.get("channel_id") or info.get("uploader_id") or ""


def _start_ts(info):
    """Stream start time as an int epoch; 0 when unknown (sorts oldest)."""
    ts = info.get("release_timestamp") or info.get("timestamp") or 0
    try:
        return int(ts)
    except (TypeError, ValueError):
        return 0


def _thumbnail_hash(vid):
    """Download a video's custom thumbnail and return its pHash, or None.

    Uses hqdefault.jpg — the 480x360 custom thumbnail that exists for every
    video. pHash resizes internally, so resolution does not affect the result.
    Never raises: any failure returns None so the caller fails open (keeps it).
    """
    url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code != 200 or not resp.content:
            return None
        with Image.open(BytesIO(resp.content)) as img:
            return imagehash.phash(img.convert("RGB"))
    except Exception:
        return None


def _hash_many(vids):
    """Hash many thumbnails in parallel. Returns {vid: phash_or_None}."""
    hashes = {}
    unique = [v for v in dict.fromkeys(vids) if v]
    if not unique:
        return hashes
    workers = max(1, min(DOWNLOAD_WORKERS, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(_thumbnail_hash, v): v for v in unique}
        for fut in as_completed(future_map):
            v = future_map[fut]
            try:
                hashes[v] = fut.result()
            except Exception:
                hashes[v] = None
    return hashes


def deduplicate_by_thumbnail(candidates, reserved=None, distance_threshold=None):
    """Drop per-channel thumbnail duplicates from `candidates`.

    Args:
        candidates : list of (vid, info) — NEW streams eligible for insert.
        reserved   : list of (vid, info) — already-stored, still-live streams for
                     the same channels. Used ONLY as duplicate reference points:
                     a new candidate matching one of these is dropped in favour of
                     the stored stream (no DB churn). Reserved streams are never
                     dropped and never returned. Defaults to [].
        distance_threshold : override for HAMMING_THRESHOLD.

    Returns:
        (kept, dropped)
            kept    : list of (vid, info) to insert.
            dropped : list of (vid, info, matched_vid) removed as dupes.

    Rule: within a channel, among a cluster of matching thumbnails we KEEP the
    stream with the LATEST start time (newest broadcast) and drop the older ones.
    """
    reserved = reserved or []
    threshold = HAMMING_THRESHOLD if distance_threshold is None else distance_threshold

    if not _DEPS_OK:
        print(f"⚠️ Thumbnail dedup skipped (Pillow/imagehash unavailable: {_IMPORT_ERROR}). "
              f"Keeping all {len(candidates)} candidate(s).")
        return list(candidates), []

    if not candidates:
        return [], []

    # Group NEW candidates by channel; only channels with a candidate matter.
    cands_by_channel = defaultdict(list)
    for vid, info in candidates:
        cands_by_channel[_channel_of(info)].append((vid, info))

    # Reserved (already-stored still-live) streams, grouped by channel, but only
    # for channels that actually have a new candidate to compare against.
    reserved_by_channel = defaultdict(list)
    for vid, info in reserved:
        ch = _channel_of(info)
        if ch in cands_by_channel:
            reserved_by_channel[ch].append((vid, info))

    # Only bother hashing when a channel has something to compare:
    #   >=2 candidates, OR >=1 candidate AND >=1 reserved stream.
    vids_to_hash = []
    for ch, group in cands_by_channel.items():
        res = reserved_by_channel.get(ch, [])
        if len(group) >= 2 or (len(group) >= 1 and len(res) >= 1):
            vids_to_hash += [v for v, _ in group]
            vids_to_hash += [v for v, _ in res]

    if not vids_to_hash:
        return list(candidates), []

    print(f"🖼️  Hashing {len(set(vids_to_hash))} thumbnail(s) for per-channel duplicate check...")
    hashes = _hash_many(vids_to_hash)

    kept = []
    dropped = []

    for ch, group in cands_by_channel.items():
        res = reserved_by_channel.get(ch, [])

        # Nothing to compare in this channel → keep every candidate untouched.
        if len(group) < 2 and not res:
            kept.extend(group)
            continue

        # Representatives already "occupying" a thumbnail. Seed with the reserved
        # (stored) streams so a new dupe of a stored stream is dropped, no churn.
        # Each rep: (vid, phash). Reserved reps with no hash are skipped as refs.
        reps = []
        for vid, _info in res:
            h = hashes.get(vid)
            if h is not None:
                reps.append((vid, h))

        # Process candidates LATEST-STARTED FIRST so, within a matching cluster,
        # the newest broadcast becomes the rep and the older ones get dropped.
        ordered = sorted(group, key=lambda vi: _start_ts(vi[1]), reverse=True)

        for vid, info in ordered:
            h = hashes.get(vid)

            # Fail-open: could not hash this thumbnail → keep it, don't use as ref.
            if h is None:
                kept.append((vid, info))
                continue

            matched_vid = None
            for rep_vid, rep_hash in reps:
                if (h - rep_hash) <= threshold:
                    matched_vid = rep_vid
                    break

            if matched_vid is not None:
                dropped.append((vid, info, matched_vid))
                title = (info.get("title") or "").strip()
                print(f"🖼️  Skipped Duplicate Thumbnail: {title[:40]}... "
                      f"(matches {matched_vid})")
            else:
                kept.append((vid, info))
                reps.append((vid, h))

    return kept, dropped
