#!/usr/bin/env python3
"""
Listen Up Campaign Revenue - daily sync.

1. Reads the Listen Up Priorities playlist (public Spotify embed page, no API needed). Every track on it is a paid
   campaign. A track joins the dashboard the first day it is seen, which is its campaign start (release day for anything
   added before release, and for everything already on the playlist the first time this runs).
   Taking it off the playlist later does NOT remove it: revenue keeps being tracked after the campaign.
2. For every record ever seen, pulls from Songstats:
   - Spotify streams, daily running totals since release (for lifetime and any date window)
   - YouTube views on the "<Artist> - Topic" upload only (the version YouTube Music plays). Songstats only
     gives today's view count per video, so a daily log is kept here from the first run onwards.

Writes data/dashboard.json. Secret: SONGSTATS_API_KEY.
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

PLAYLIST_ID = os.environ.get("PLAYLIST_ID", "3ev7Cq1opAjobbXqeOXrQG")
SS_KEY = os.environ.get("SONGSTATS_API_KEY", "")
SS_BASE = "https://api.songstats.com/enterprise/v1"
SS_DELAY = float(os.environ.get("SS_DELAY", "0.3"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FP = os.path.join(ROOT, "data", "dashboard.json")
calls = {"songstats": 0, "spotify": 0}


class SyncError(Exception):
    pass


def load():
    if os.path.exists(DATA_FP):
        with open(DATA_FP, encoding="utf-8") as f:
            txt = f.read().strip()
            if txt:
                return json.loads(txt)
    return {"records": {}}


def save(doc):
    with open(DATA_FP, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ Spotify playlist
# Spotify's Web API now refuses to list playlist tracks for apps like ours (403 / "user authentication required").
# The public embed player for a public playlist still lists every track, so the track IDs are read from that.
def playlist_track_ids():
    url = f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
                                   "Accept-Language": "en-GB,en;q=0.9"}, timeout=30)
    calls["spotify"] += 1
    if not r.ok:
        raise SyncError(f"Spotify embed page returned {r.status_code}. Is the playlist still public?")
    ids = []
    for m in re.finditer(r"spotify:track:([A-Za-z0-9]{22})", r.text):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        raise SyncError("Could not find any tracks on the playlist's embed page. Spotify may have changed the page.")
    return ids


# ------------------------------------------------------------------ Songstats
def ss_get(path, params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    for attempt in range(5):
        r = requests.get(f"{SS_BASE}/{path}", params=params, headers={"apikey": SS_KEY, "accept": "application/json"}, timeout=60)
        calls["songstats"] += 1
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            ra = r.headers.get("Retry-After")
            wait = min(int(float(ra)), 60) + 1 if ra and ra.replace(".", "", 1).isdigit() else 3 * (attempt + 1)
            print(f"  [songstats] {r.status_code} on {path}, waiting {wait}s")
            time.sleep(wait)
            continue
        if not r.ok:
            raise SyncError(f"Songstats {path}: HTTP {r.status_code} {r.text[:160]}")
        time.sleep(SS_DELAY)
        body = r.json()
        body = body.get("response", body)
        if isinstance(body, dict) and body.get("result") == "error":
            raise SyncError(f"Songstats {path}: {body.get('message')}")
        return body
    raise SyncError(f"Songstats {path}: kept failing")


def source_data(body, source):
    for s in (body or {}).get("stats") or []:
        if s.get("source") == source:
            return s.get("data") or {}
    return {}


def find_ids(isrc, hint):
    """Songstats track + artist IDs (artist-scoped keys need both on every call). Looked up once, then kept."""
    for q in [isrc, hint]:
        if not q:
            continue
        for res in (ss_get("tracks/search", {"q": q, "limit": 5}) or {}).get("results") or []:
            artists = [a.get("songstats_artist_id") for a in res.get("artists") or [] if a.get("songstats_artist_id")]
            for aid in artists or [None]:
                ident = {k: v for k, v in {"songstats_track_id": res.get("songstats_track_id"), "songstats_artist_id": aid}.items() if v}
                try:
                    body = ss_get("tracks/info", dict(ident, source="all"))
                except SyncError:
                    continue
                info = (body or {}).get("track_info") or {}
                isrcs = {l.get("isrc") for l in info.get("links") or [] if l.get("isrc")}
                if q != isrc or isrc in isrcs or not isrcs:
                    return ident, info
    raise SyncError(f"could not find {isrc} on Songstats")


def lookup_spotify(sid):
    """Songstats IDs and track info from a Spotify track ID."""
    try:
        body = ss_get("tracks/info", {"spotify_track_id": sid, "source": "all"})
        info = (body or {}).get("track_info") or {}
        if info.get("songstats_track_id"):
            ident = {"songstats_track_id": info["songstats_track_id"]}
            aid = next((a.get("songstats_artist_id") for a in info.get("artists") or [] if a.get("songstats_artist_id")), None)
            if aid:
                ident["songstats_artist_id"] = aid
            return ident, info
    except SyncError as e:
        print(f"  direct lookup failed ({e}), trying search")
    for res in (ss_get("tracks/search", {"q": f"https://open.spotify.com/track/{sid}", "limit": 1}) or {}).get("results") or []:
        aid = next((a.get("songstats_artist_id") for a in res.get("artists") or [] if a.get("songstats_artist_id")), None)
        ident = {k: v for k, v in {"songstats_track_id": res.get("songstats_track_id"), "songstats_artist_id": aid}.items() if v}
        info = (ss_get("tracks/info", dict(ident, source="all")) or {}).get("track_info") or {}
        return ident, info
    raise SyncError("not found")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def youtube_topic_views(ident, artists):
    """Views on the auto-generated '<Artist> - Topic' upload(s): the version YouTube Music plays.
    Fan uploads and other channels are ignored."""
    body = ss_get("tracks/stats", dict(ident, source="youtube", with_videos="true", limit=100))
    vids = source_data(body, "youtube").get("videos") or []
    names = {norm(a) for a in artists}
    keep = []
    for v in vids:
        ch = v.get("youtube_channel_name") or ""
        if not ch.endswith(" - Topic"):
            continue
        # the Topic channel of one of the record's artists (a collab can sit on either artist's Topic channel)
        if names and norm(ch[:-8]) not in names and not any(n and n in norm(ch) for n in names):
            continue
        keep.append({"id": v.get("external_id"), "title": v.get("title"), "channel": ch, "views": v.get("view_count") or 0,
                     "uploaded": v.get("upload_date")})
    return keep


# ------------------------------------------------------------------ one record
def sync_record(rec):
    errors = []
    if not rec.get("songstats"):
        ident, info = find_ids(rec["isrc"], f"{', '.join(rec.get('artists') or [])} {rec.get('title') or ''}".strip())
        rec["songstats"] = ident
        labels = [l.get("name") for l in info.get("labels") or [] if l.get("name")]
        if labels and not rec.get("label"):
            rec["label"] = labels[0]
    ident = rec["songstats"]

    if not rec.get("label"):
        try:
            info = (ss_get("tracks/info", dict(ident, source="spotify")) or {}).get("track_info") or {}
            labels = [l.get("name") for l in info.get("labels") or [] if l.get("name")]
            if labels:
                rec["label"] = labels[0]
        except SyncError as e:
            errors.append(f"label: {e}")

    # Spotify streams: first run pulls everything since release, later runs only the last few weeks
    today = date.today()
    have = rec.get("streams") or []
    rel = d(rec.get("release_date")) or (today - timedelta(days=365))
    start = rel - timedelta(days=1)
    if have:
        start = max(start, d(have[-1][0]) - timedelta(days=21))
    try:
        body = ss_get("tracks/historic_stats", dict(ident, source="spotify", start_date=start.isoformat(), end_date=today.isoformat()))
        hist = source_data(body, "spotify").get("history") or []
        merged = {dt: v for dt, v in have}
        for h in hist:
            if h.get("date") and h.get("streams_total") is not None:
                merged[h["date"][:10]] = h["streams_total"]
        rec["streams"] = sorted([[k, v] for k, v in merged.items()])
    except SyncError as e:
        errors.append(f"spotify streams: {e}")

    try:
        vids = youtube_topic_views(ident, rec.get("artists") or [])
        rec["youtube"] = vids
        total = sum(v["views"] for v in vids)
        log = [x for x in rec.get("youtube_log") or [] if x[0] != today.isoformat()]
        if vids:
            log.append([today.isoformat(), total])
        rec["youtube_log"] = log
    except SyncError as e:
        errors.append(f"youtube: {e}")

    rec["errors"] = errors
    rec["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return rec


# ------------------------------------------------------------------ main
def main():
    if not SS_KEY:
        print("Missing the SONGSTATS_API_KEY secret.")
        sys.exit(1)
    doc = load()
    recs = doc.setdefault("records", {})

    print("Reading the Listen Up Priorities playlist")
    ids = playlist_track_ids()
    print(f"  {len(ids)} tracks on the playlist")
    first_run = not recs
    by_sp = {r.get("spotify_id"): k for k, r in recs.items()}
    on_now = set()
    for sid in ids:
        key = by_sp.get(sid)
        if not key:
            try:
                ident, info = lookup_spotify(sid)
            except SyncError as e:
                print(f"  could not find Spotify track {sid} on Songstats yet: {e}")
                continue
            isrc = next((l.get("isrc") for l in info.get("links") or [] if l.get("source") == "spotify" and l.get("isrc")), None) \
                or next((l.get("isrc") for l in info.get("links") or [] if l.get("isrc")), None) or sid
            key = isrc.upper()
            labels = [l.get("name") for l in info.get("labels") or [] if l.get("name")]
            rel = (info.get("release_date") or "")[:10]
            seen = date.today().isoformat()
            # campaign start: release day (from Songstats) for anything already on the playlist the first time this runs,
            # anything added before release, and anything that appears within a week of release. Records you come
            # on board with later start the day they first show up on the playlist (the sync runs daily).
            # within a week of release counts as "from release" (it was most likely added on release day)
            near_release = rel and d(seen) - d(rel) <= timedelta(days=7)
            start = rel if rel and (first_run or near_release) else seen
            recs[key] = {"isrc": key, "spotify_id": sid, "songstats": ident, "first_seen": seen, "added_at": start,
                         "title": info.get("title"), "artists": [a.get("name") for a in info.get("artists") or []],
                         "release_date": rel or None, "label": labels[0] if labels else None, "image": info.get("avatar")}
            r = recs[key]
            print(f"  NEW: {', '.join(r['artists'])} - {r['title']} (campaign from {start})")
        on_now.add(key)
    for k, r in recs.items():
        r["on_playlist"] = k in on_now
        # tracks from the same release go together: same label and release day
        r["album_id"] = r.get("album_id") or f"{norm(r.get('label') or '')}|{r.get('release_date') or ''}|{norm((r.get('artists') or [''])[0]) if not r.get('label') else ''}"

    ok = failed = 0
    for isrc, r in recs.items():
        print(f"Syncing {', '.join(r.get('artists') or [])} - {r.get('title')} ({isrc})")
        try:
            sync_record(r)
            ok += 1
            if r["errors"]:
                print("  partial:", "; ".join(r["errors"]))
        except Exception as e:  # keep going, keep the old data for this one
            r.setdefault("errors", []).append(str(e))
            print(f"  FAILED: {e}")
            failed += 1
    doc["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc["playlist_id"] = PLAYLIST_ID
    save(doc)
    print(f"\nSUMMARY ok={ok} failed={failed} | calls: Songstats {calls['songstats']}, Spotify {calls['spotify']}")
    if ok == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
