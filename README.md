# UK IPTV Playlist Builder

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

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
- **Tvheadend failover** (optional): `tvh_sync.py` adds the working streams to
  Tvheadend as backups for your Freeview/Freesat channels. See
  [Tvheadend failover](#tvheadend-failover-tvh_syncpy).

## Quick start

```bash
git clone https://github.com/andyb2000/ukiptv.git
cd ukiptv

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

## Tvheadend failover (`tvh_sync.py`)

If you watch TV through [Tvheadend](https://tvheadend.org/) with an aerial or
dish, a poor signal makes channels break up or drop out. `tvh_sync.py` adds
verified IPTV streams to Tvheadend as **backup services** on your existing
channels. When the tuner loses the signal, Tvheadend switches to the IPTV
stream. Channels that you can't receive over the air are added as new
IPTV-only channels.

It talks to Tvheadend's HTTP API (port 9981), so it can run on another machine
or in its own Docker container. Written for Tvheadend 4.3.

### What it does in Tvheadend

- **One IPTV network that it owns**, called `UKIPTV (tvh_sync)` by default.
  Each stream it uses becomes one mux in that network. By default it keeps the
  best 2 working streams per channel.
- **Backup services on your channels.** For each channel in your list it finds
  your Tvheadend channel with the same name (for example `BBC One HD`) and adds
  its IPTV services to that channel's service list, with a **lower priority**
  than your tuners (`-5`, then `-6`). Tvheadend always prefers the aerial/dish
  and only falls back to IPTV when that fails.
- **IPTV-only channels** for entries with no matching channel of yours, such
  as BBC Four on Freeview-only setups or the BBC radio stations. These are
  numbered from 1000 and tagged `IPTV`, plus a hidden marker tag
  `ukiptv-managed`.
- **Housekeeping on every run.** Dead streams are replaced. Muxes for streams
  that are gone are deleted. Channels you remove from your list are cleaned up.
  If Tvheadend can't play a stream, it's skipped for 24 hours. In `auto` mode,
  it's retried once through ffmpeg first.

### What it never does

- It never edits, renames or deletes anything it didn't create. The only
  change it makes to your own channels is adding or removing **its own**
  services in their service list. Their name, number, EPG, tags and other
  services stay as they are.
- It makes no changes unless you pass `--apply`. Without it, it prints the plan.
- Before every `--apply` it saves a backup of the relevant Tvheadend state in
  `.tvh_sync/backups/`. It logs each change to `.tvh_sync/changes.log`.

### Setup

1. **In Tvheadend**, create a user for the script (Configuration > Users).
   Give it an access entry with **admin** rights, because Tvheadend's network
   and channel APIs are admin-only.
2. **Copy the settings file** and fill in the address and login:

   ```bash
   cp tvh_sync.conf.example tvh_sync.conf
   nano tvh_sync.conf     # url, username, password
   ```

   `tvh_sync.conf` is git-ignored. You can also pass the login in the
   environment variables `TVH_URL`, `TVH_USER`, `TVH_PASSWORD` or
   `TVH_PASSWORD_FILE`.
3. **Look at the plan** before changing anything:

   ```bash
   python3 tvh_sync.py
   ```

   It lists what it would create and which of your channels each stream would
   back up:

   ```text
   + IPTV network 'UKIPTV (tvh_sync)'
   + mux BBC One: [bbc] http://vs-hls-pushb-uk-live.akamaized.net/...
   = BBC One: failover for your channel(s) 'BBC One HD'
   + channel 'BBC Four' #1004 (IPTV only)
   ```

   If a channel matched the wrong thing, or didn't match because your
   channel's name is different, fix it in the `[channel_map]` section:

   ```ini
   [channel_map]
   # back up these channels of yours
   BBC One = BBC One Yorks HD, BBC ONE Yorks
   # give it an IPTV-only channel instead
   BBC News = new
   # leave it out of Tvheadend
   QVC = none
   ```
4. **Apply it**:

   ```bash
   python3 tvh_sync.py --apply
   ```

   New muxes need a few seconds for Tvheadend to scan them. Anything not
   scanned within `scan_wait` is finished on the next run.
5. **Run it regularly** to keep the streams fresh: with cron, or with the
   Docker container below.

### Commands

```text
python3 tvh_sync.py                       # dry run: show the plan
python3 tvh_sync.py --apply               # sync Tvheadend
python3 tvh_sync.py --status              # what it manages now
python3 tvh_sync.py --disable --apply     # switch off all its streams and channels
python3 tvh_sync.py --enable --apply      # switch them back on
python3 tvh_sync.py --uninstall --apply   # remove everything it created
python3 tvh_sync.py --apply --interval 360   # keep running, sync every 6 hours

  -c, --config FILE   settings file (default: tvh_sync.conf, or $TVH_SYNC_CONFIG)
  -y, --yes           don't ask for confirmation before --uninstall --apply
```

- **`--disable`** switches off the network, its muxes and the IPTV-only
  channels, so Tvheadend only uses your tuners. Nothing is deleted.
  Scheduled syncs do nothing until you run `--enable --apply`.
- **`--uninstall`** removes its services from your channels, then deletes its
  IPTV-only channels, its network (with all muxes and services) and the tags
  it created. Your Tvheadend is left as it was before the first run.

### Settings

All settings are in `tvh_sync.conf`. Each one is explained in
[`tvh_sync.conf.example`](tvh_sync.conf.example). The main ones are:

| Setting | Default | Meaning |
|---|---|---|
| `streams_per_channel` | `2` | Backup streams per channel |
| `service_priority` | `-5` | Priority of IPTV services. Below `0` means DVB first. |
| `prefer_iptv` | (empty) | Channels where IPTV should win over DVB |
| `attach_to_existing` | `yes` | Add streams to your channels with the same name |
| `create_channels` | `yes` | Create IPTV-only channels for the rest |
| `channel_number_start` | `1000` | Numbering for IPTV-only channels |
| `stream_mode` | `auto` | `direct`, `ffmpeg` (`pipe://`), or `auto` (ffmpeg only when needed) |
| `ffmpeg_path` | `/usr/bin/ffmpeg` | ffmpeg path **inside the Tvheadend container** |

### Docker

```bash
docker build -t ukiptv-sync .
docker run -d --name ukiptv-sync --restart unless-stopped \
  -v /path/to/config:/config ukiptv-sync
```

Put `tvh_sync.conf` and your channel list in `/path/to/config`. The container
syncs every 6 hours and writes its backups and change log to
`/path/to/config/.tvh_sync`. Do a dry run first:

```bash
docker run --rm -v /path/to/config:/config ukiptv-sync          # dry run
docker run --rm -v /path/to/config:/config ukiptv-sync --status
```

### How the failover behaves

Tvheadend picks the service with the highest priority, which is your tuner.
It moves to the next service when the current one fails to tune, or when its
data stops (the tuner's data timeout). So a complete signal loss switches to
IPTV within a few seconds. A signal that is still delivering data but with
glitches won't trigger a switch. For channels that are always bad, add them to
`prefer_iptv`.

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
- **Tvheadend can't play every stream directly.** Audio-only HLS (radio) and
  DASH streams need ffmpeg in the Tvheadend container (`stream_mode = auto`
  handles this).

## Disclaimer

This repository contains **no video content and hosts no streams**. It only
checks links that are already published in public playlists and on
broadcasters' own services. You are responsible for making sure your use
complies with the laws where you live and with each broadcaster's terms. For
example, watching live BBC TV in the UK requires a TV Licence.

## Contributing

Found a working stream that's missing, a dead source, or a channel that
matches the wrong thing? Please
[open an issue](https://github.com/andyb2000/ukiptv/issues) or a pull request.
Run `python3 iptv_uk.py -d desired_channels.txt --fast` before submitting to
check nothing broke.

The generated playlists (`playlist_*.m3u8`, `report.json`) aren't committed,
because they go stale within hours. Build your own with the command above.

## License

Apache License 2.0. See [LICENSE](LICENSE).
