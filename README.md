# UK IPTV Playlist Builder

This tool builds an IPTV playlist of free UK TV and radio channels that it has
**checked are working**.

Public IPTV lists go stale quickly. Streams move, servers vanish, and tokens
expire. This tool collects candidate streams from several public sources and
tests each one by downloading part of the live video. For each channel it keeps
the best working stream. It then writes:

- **`playlist_master.m3u8`**: every working channel it found.
- **`playlist_desired.m3u8`**: only the channels you asked for, in your order.

It needs only Python 3.9 or later, with no extra packages.

## Features

- **Several sources**: Free-TV, iptv-org, the BBC's own stream API and Pluto TV UK. You can add your own.
- **Real stream checks**: it follows master playlists to a quality variant and fetches an actual video segment, not just the playlist file.
- **Your own channel list**: list the channels you want, in plain text, in your order.
- **Flexible name matching**: `ITV 3`, `ITV3 HD` and `ITV3 (360p)` all match the same channel.
- **Official BBC streams**: current URLs for every BBC channel, including each regional BBC One.
- **Programme guide (EPG) fix-up**: it rewrites each channel's `tvg-id` so the guide in your player shows listings.
- **Fast**: about 550 streams tested in parallel in about 30 seconds.

## Quick start

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>

# Build both playlists
python3 iptv_uk.py -d desired_channels.txt
```

Then load `playlist_desired.m3u8` into any IPTV player, such as VLC, TiviMate,
Kodi (PVR IPTV Simple Client), Jellyfin or Plex.

## Choosing your channels

Edit `desired_channels.txt` with one channel per line. The playlist keeps the
same order.

```text
# Comments start with #
BBC One = BBC One Yorkshire | BBC One   # name shown = preferred | fallback
BBC Two
ITV 3
QVC UK | QVC                            # alternative names, tried in order
Some Channel {Some.Channel.uk}          # force a specific EPG tvg-id
```

- `|` separates alternative names to match. They're tried in order.
- `Display Name = ...` sets the name your player shows.
- `{id}` at the end forces the EPG `tvg-id`.

Matching ignores case, spacing, punctuation, `HD`/`SD`, quality tags like
`(720p)`, `[Geo-blocked]`, and a trailing `TV`/`UK`.

## Usage

```text
python3 iptv_uk.py [options]

  -d, --desired FILE        your channel list
  -o, --output FILE         filtered playlist     (default: playlist_desired.m3u8)
  -m, --master FILE         all working channels  (default: playlist_master.m3u8)
  --fast                    only test channels in your list (quicker, no master list)
  -v, --verbose             show every URL tried for each channel
  --report FILE             write full per-stream results as JSON
  --source URL_OR_FILE      add another M3U source (repeatable)
  --exclude-source NAME     skip a built-in source: bbc, free-tv, iptv-org, pluto
  --no-default-sources      use only --source entries
  --include-unverified      keep YouTube/web-page links that can't be tested
  --epg URLS                override the x-tvg-url guide header
  --epg-map URL             XMLTV file used to fix tvg-ids (default: epgshare UK)
  --no-epg-map              keep the sources' tvg-ids unchanged
  -t, --timeout SECONDS     per-request timeout (default: 10)
  -w, --workers N           parallel tests (default: 32)
  --retries N               retries for timeouts / server errors (default: 1)
```

Example output:

```text
OK    BBC One        [bbc] http://vs-hls-pushb-uk-live.akamaized.net/.../bbc_one_yorks/...
OK    QVC UK         [iptv-org] https://qvcuk-live.akamaized.net/hls/live/.../qvc/master.m3u8
DEAD  ITV 3          all 2 candidate(s) failed
MISS  Film4          not found in any source

Desired: 46/68 channels working -> playlist_desired.m3u8
```

- `OK`: a working stream was found and added to the playlist.
- `DEAD`: candidates were found, but none of them worked.
- `MISS`: no source has this channel.

## Keeping it fresh

Streams change often, so rebuild regularly. For example, with cron every 6 hours:

```cron
0 */6 * * * cd /path/to/repo && python3 iptv_uk.py -d desired_channels.txt >/dev/null 2>&1
```

## Sources

| Source | What it provides |
|---|---|
| [Free-TV/IPTV](https://github.com/Free-TV/IPTV) | Curated free-to-air UK list |
| [iptv-org/iptv](https://github.com/iptv-org/iptv) | Large community-maintained UK list |
| BBC mediaselector API | Official BBC TV and World Service streams |
| [i.mjh.nz](https://i.mjh.nz/) | Pluto TV UK channel list |
| [epgshare01](https://epgshare01.online/) | Programme guide (XMLTV) |

## Limitations

- **Some channels have no working stream.** ITV, Channel 4, Channel 5 and UKTV
  (Dave and others) don't publish open streams. Public lists only contain
  unofficial rebroadcasts of them, which are rarely up. Watch these through
  their own apps (ITVX, Channel 4, My5, U).
- **Some streams only work in the UK.** BBC and some others are geo-blocked.
- **YouTube links can't be tested.** Use `--include-unverified` to keep them.
- A stream that passes the check can still fail later. Rebuilding often helps.

## Disclaimer

This repository contains **no video content and hosts no streams**. It only
checks links that are already published in public playlists and on
broadcasters' own services. You are responsible for making sure your use
complies with the laws where you live and with each broadcaster's terms. For
example, watching live BBC TV in the UK requires a TV Licence.

## License

MIT. See `LICENSE` if one is included.
