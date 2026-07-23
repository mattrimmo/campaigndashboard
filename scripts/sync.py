#!/usr/bin/env python3
"""
Campaign Dashboard — sync script.

Runs daily via GitHub Actions (see .github/workflows/sync.yml). Reads
data/campaigns.json (the tracked list — client, artist, track, ISRC,
status), pulls current metrics from Spot On Track and Spotify for every
"active" campaign, and appends a dated snapshot to data/history.json.

Campaigns with status != "active" (i.e. archived / stopped) are skipped
entirely — no API calls are made for them, and their existing history is
left untouched. This is the same discipline as the targettracking repo's
sync.py: once you're done working a record, archive it and it stops
costing API credits forever.

Secrets (set as GitHub Actions repo secrets):
  SOT_API_KEY            — Spot On Track bearer token
  SPOTIFY_CLIENT_ID       — Spotify app client id
  SPOTIFY_CLIENT_SECRET   — Spotify app client secret

Metric choices, deliberately:
  - "reach" is the summed follower count of playlists the track is
    CURRENTLY placed on (independent + editorial), not artist followers.
    Artist followers climb regardless of any one campaign's performance
    (Release Radar, general artist growth), so they're a weak signal for
    "did this campaign work." Playlist reach only grows when you land a
    new placement — same logic as targettracking's independent_followers_total.
  - "estimated_revenue_gbp" is exactly that — an ESTIMATE, built from Spot
    On Track's total stream count for the ISRC multiplied by a configurable
    blended per-stream rate (PER_STREAM_RATE_GBP below). Actual payouts
    depend on listener geography, subscription tier, and your distributor
    deal, none of which any API exposes — treat this as a directional
    figure, never a real royalty statement number.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMPAIGNS_FP  = os.path.join(REPO_ROOT, "data", "campaigns.json")
HISTORY_FP    = os.path.join(REPO_ROOT, "data", "history.json")

SOT_KEY   = os.environ.get("SOT_API_KEY", "")
SP_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

MIN_FOLLOWERS = 100  # same floor as the report generator / LTT

# Blended average as of mid-2026 is roughly $0.003-0.005/stream (~£0.0022-0.0037
# at current USD/GBP), independent artists commonly land around $0.004 (~£0.003).
# This is a rough midpoint, not a real rate — adjust if you have better data on
# your clients' actual distributor deals and audience geography.
PER_STREAM_RATE_GBP = 0.003


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def sot_get(path):
    r = requests.get(
        "https://www.spotontrack.com/api/v1" + path,
        headers={"Authorization": "Bearer " + SOT_KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


_sp_token = None
_sp_exp = 0


def sp_token():
    global _sp_token, _sp_exp
    if _sp_token and time.time() < _sp_exp:
        return _sp_token
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _sp_token = data["access_token"]
    _sp_exp = time.time() + data["expires_in"] - 30
    return _sp_token


def sp_owner(spotify_id):
    """Returns playlist owner display name ('' means editorial/Spotify-owned)."""
    for attempt in range(2):
        tok = sp_token()
        r = requests.get(
            f"https://api.spotify.com/v1/playlists/{spotify_id}",
            headers={"Authorization": "Bearer " + tok},
            params={"fields": "owner.display_name"},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 0.5)
            continue
        if not r.ok:
            print(f"  [debug] Spotify playlist lookup failed for {spotify_id}: {r.status_code}")
            return ""
        return (r.json().get("owner") or {}).get("display_name") or ""
    return ""


def process_campaign(entry):
    isrc = entry["isrc"]

    streams = sot_get(f"/tracks/{isrc}/spotify/streams")
    current = sot_get(f"/tracks/{isrc}/spotify/playlists/current")
    current = [p for p in current if (p.get("playlist", {}).get("followers") or 0) >= MIN_FOLLOWERS]

    total_streams = streams[0]["total"] if streams else None
    daily_streams = streams[0]["daily"] if streams else None

    editorial_reach = 0
    independent_reach = 0
    editorial_count = 0
    independent_count = 0

    for p in current:
        pl = p["playlist"]
        followers = pl.get("followers") or 0
        owner = sp_owner(pl["spotify_id"])
        if owner.strip() == "":
            editorial_reach += followers
            editorial_count += 1
        else:
            independent_reach += followers
            independent_count += 1

    estimated_revenue_gbp = (
        round(total_streams * PER_STREAM_RATE_GBP, 2) if total_streams is not None else None
    )

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_streams": total_streams,
        "daily_streams": daily_streams,
        "editorial_count": editorial_count,
        "independent_count": independent_count,
        "editorial_reach": editorial_reach,
        "independent_reach": independent_reach,
        "estimated_revenue_gbp": estimated_revenue_gbp,
        "estimated_revenue_rate_gbp": PER_STREAM_RATE_GBP,
    }


def main():
    if not (SOT_KEY and SP_ID and SP_SECRET):
        print("Missing one or more required secrets (SOT_API_KEY / SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
        sys.exit(1)

    campaigns = load_json(CAMPAIGNS_FP, [])
    history = load_json(HISTORY_FP, {})

    for entry in campaigns:
        if entry.get("status") != "active":
            continue  # archived — no calls, history left as-is

        cid = entry["id"]
        print(f"Syncing {entry.get('client','?')} / {entry.get('artist','?')} - {entry.get('track','?')} ({entry.get('isrc')})")
        try:
            snap = process_campaign(entry)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        history.setdefault(cid, [])
        # Avoid duplicate snapshots if the workflow runs twice in a day
        history[cid] = [h for h in history[cid] if h["date"] != snap["date"]]
        history[cid].append(snap)
        print(f"  -> {snap}")

    save_json(HISTORY_FP, history)
    print("Done.")


if __name__ == "__main__":
    main()
