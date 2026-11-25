#!/usr/bin/env python3
"""
Listen Up Campaign Revenue - daily sync.

1. Reads the Listen Up Priorities playlist on Spotify. Every track on it is a paid campaign. A track joins the
   dashboard the first time it is seen, with the date it was added to the playlist as its campaign start.
   Taking it off the playlist later does NOT remove it: revenue keeps being tracked after the campaign.
2. For every record ever seen, pulls from Songstats:
   - Spotify streams, daily running totals since release (for lifetime and any date window)
   - YouTube views on the "<Artist> - Topic" upload only (the version YouTube Music plays). Songstats only
     gives today's view count per video, so a daily log is kept here from the first run onwards.

Writes data/dashboard.json. Secrets: SONGSTATS_API_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET.
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
SP_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

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
def sp_token():
    r = requests.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                      auth=(SP_ID, SP_SECRET), timeout=30)
    calls["spotify"] += 1
    if not r.ok:
        raise SyncError(f"Spotify login failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def sp_get(url, tok, params=None):
    for attempt in range(4):
        r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        calls["spotify"] += 1
        if r.status_code == 429 and attempt < 3:
            wait = min(int(r.headers.get("Retry-After", "5") or 5), 60)
            print(f"  [spotify] 429, waiting {wait}s")
            time.sleep(wait)
            continue
        return r
    return r


def playlist_tracks():
    tok = sp_token()
    fields = "next,items(added_at,track(id,name,artists(name),external_ids,album(id,name,album_type,release_date,images)))"
    out = []
    # Spotify has renamed this endpoint before, so try the newer name if the usual one is refused
    for path in ("tracks", "items"):
        url = f"https://api.spotify.com/v1/playlists/{PLAYLIST_ID}/{path}"
        params = {"fields": fields, "limit": 100}
        out = []
        ok = True
        while url:
            r = sp_get(url, tok, params)
            if not r.ok:
                print(f"  [spotify] playlists/{path} -> {r.status_code} {r.text[:200]}")
                ok = False
                break
            body = r.json()
            for it in body.get("items") or []:
                t = it.get("track") or it.get("item")
                if t and t.get("id"):
                    out.append((it.get("added_at"), t))
            url, params = body.get("next"), None
        if ok:
            return out
    raise SyncError("Spotify would not return the playlist. Check the app credentials, and that the playlist is public.")


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
    if not (SS_KEY and SP_ID and SP_SECRET):
        print("Missing a secret: SONGSTATS_API_KEY, SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are all needed.")
        sys.exit(1)
    doc = load()
    recs = doc.setdefault("records", {})

    print("Reading the Listen Up Priorities playlist")
    tracks = playlist_tracks()
    print(f"  {len(tracks)} tracks on the playlist")
    on_now = set()
    for added_at, t in tracks:
        isrc = ((t.get("external_ids") or {}).get("isrc") or "").upper()
        if not isrc:
            print(f"  no ISRC for {t.get('name')}, skipped")
            continue
        on_now.add(isrc)
        al = t.get("album") or {}
        imgs = al.get("images") or []
        r = recs.get(isrc)
        if not r:
            print(f"  NEW: {', '.join(a.get('name') for a in t.get('artists') or [])} - {t.get('name')}")
            r = recs[isrc] = {"isrc": isrc, "first_seen": date.today().isoformat()}
        r.update({
            "spotify_id": t.get("id"), "title": t.get("name"),
            "artists": [a.get("name") for a in t.get("artists") or []],
            "album_id": al.get("id"), "album_name": al.get("name"), "album_type": al.get("album_type"),
            "release_date": al.get("release_date"),
            "image": (imgs[1] if len(imgs) > 1 else imgs[0])["url"] if imgs else None,
        })
        added = (added_at or "")[:10]
        if added and (not r.get("added_at") or added < r["added_at"]):
            r["added_at"] = added   # campaign start = when it went on the playlist (earliest if re-added)
    for isrc, r in recs.items():
        r["on_playlist"] = isrc in on_now

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
