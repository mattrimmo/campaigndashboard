# Listen Up: Campaign Revenue

Estimated gross revenue for every record Listen Up works, so you can see what a campaign made, during the
campaign month and afterwards.

## How it works
- **Records come from Spotify.** The daily sync reads the *Listen Up Priorities* playlist. Every track on it is a paid
  campaign. The date it was added is the campaign start, and the campaign month is that day plus 29 days.
- **Nothing drops off.** Taking a track off the playlist doesn't remove it here, so revenue after the campaign keeps showing.
  Tracks from the same release are grouped into one campaign, filed under the label Songstats lists.
- **Revenue** (gross, before distributor or label cuts):
  - Spotify: streams from Songstats × £3.28 per 1,000 (Ditto's £327.75 per 100,000).
  - YouTube Music: views on the record's "Artist - Topic" upload, the version YouTube Music plays, × £1.24 per 1,000
    (published official-channel rate of $0.00164 a view). Fan uploads aren't counted.
  - Rates can be changed with the **Rates** button (saved in your browser).
  - Apple Music, Amazon, Deezer and Tidal aren't included: Songstats has no stream counts for them.
- **Windows.** Spotify has a full daily history, so any date range works. Songstats only gives today's YouTube view counts,
  so YouTube views are logged daily from the first sync; window figures for YouTube start from then.

## Setup
Secrets (Settings → Secrets and variables → Actions): `SONGSTATS_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`.
The sync runs daily at 06:10 UTC, or from Actions → "Daily revenue sync" → Run workflow.
To use a different playlist, add a `PLAYLIST_ID` env line to the Sync step in `.github/workflows/sync.yml`.

## Songstats usage
About 2 calls per record per day (Spotify history and YouTube videos), plus 2 the first time a record is seen.
