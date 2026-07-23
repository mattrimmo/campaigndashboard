# Campaign Dashboard

Internal-only view of active and archived Listen Up campaigns — playlist
reach, stream growth, and an estimated GBP revenue figure — per track and
rolled up by client. Standalone tool, same architecture pattern as Long
Term Target Tracking (public static site + GitHub Actions doing the real
API calls, so keys never sit in the public-facing page), but tracking a
different population of records: campaigns you're actively working, not
prospects you're monitoring.

## One-time setup

1. Create a new **public** GitHub repo called `campaigndashboard` (public
   is required for the free GitHub Pages tier — same as targettracking).
2. Push everything in this folder to that repo's `main` branch.
3. In **Settings → Secrets and variables → Actions**, add:
   - `SOT_API_KEY` — Spot On Track bearer token
   - `SPOTIFY_CLIENT_ID`
   - `SPOTIFY_CLIENT_SECRET`
   (Same values as the targettracking repo's secrets if you want to reuse
   the same app credentials.)
4. In **Settings → Pages**, source = "Deploy from a branch", branch
   `main`, folder `/ (root)`. Live at
   `https://<your-username>.github.io/campaigndashboard/`.
5. Check the `REPO` constant near the top of `index.html`'s `<script>`
   block matches your actual GitHub username/repo path.
6. Trigger the workflow once manually (Actions tab → "Daily campaign
   sync" → Run workflow) to confirm the secrets work.

## Adding / archiving campaigns

Open the live page, expand **"Add / edit campaigns"**, paste a GitHub
fine-grained PAT with **contents: read and write** access to this repo
(the targettracking one works if its scope covers this repo too), and
fill in client / artist / track / ISRC. The PAT lives only in your
browser's local storage.

To stop tracking a campaign, open its detail page and hit **"Archive /
stop tracking"** — this flips its status so the next sync skips it
entirely (no more API calls), while keeping everything already collected
in `history.json`. Reactivate the same way if you pick it back up later.

## What each metric actually is

- **Total streams / daily streams** — pulled directly from Spot On
  Track's `/tracks/{isrc}/spotify/streams` endpoint (the same one the
  report generator uses).
- **Playlist reach** — summed follower count of playlists the track is
  *currently* placed on (editorial + independent), not artist followers.
  Artist followers climb regardless of any one campaign, so they're a
  weak signal; reach only grows when you land a new placement.
- **Estimated revenue (GBP)** — `total_streams × PER_STREAM_RATE_GBP`
  (default £0.003/stream, set at the top of `sync.py`). This is a
  **directional estimate only**, not a real royalty figure — actual
  payouts vary 4-8x by listener geography, subscription tier, and your
  distributor's specific deal, none of which any API exposes. It's
  useful for "is this campaign roughly worth what we're charging for
  it," not for anything that goes in front of a client as a real number.

## How the data flows

- `data/campaigns.json` — tracked list: client, artist, track, ISRC,
  start date, status (`active` / `archived`).
- `scripts/sync.py` — runs daily via GitHub Actions, skips anything not
  `active`, pulls streams (Spot On Track) and current playlist
  placements (Spot On Track + Spotify for owner classification) for
  everything else, appends a dated snapshot to `data/history.json`.
- `index.html` — reads both JSON files client-side, renders the client
  rollup → per-campaign → full trend chart drill-down.

## Security note

Unlike the report generator (a local tool), this repo is public. Never
hardcode a real API key anywhere in `index.html` or commit one to this
repo — secrets only ever live in GitHub Actions repo secrets, called from
`sync.py` server-side. The admin panel's PAT is the one exception, and
it's designed to never leave the user's own browser.
