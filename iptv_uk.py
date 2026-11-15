#!/usr/bin/env python3
"""
Build a verified UK IPTV playlist from multiple sources.

Pipeline:
  1. Gather candidate streams from every source (Free-TV, iptv-org, the BBC
     mediaselector API, Pluto TV UK, plus any extra --source URLs/files).
  2. Test every unique URL: fetch the HLS playlist, follow master -> variant,
     and download the start of a real segment (a manifest alone can be stale).
  3. Group candidates into channels and pick the best working stream for each
     (source priority first, then response time).
  4. Write:
       --master   every working channel found (the "known good" master list)
       --output   only the channels in your desired list, in that order

Desired list format (one channel per line, '#' comments, '|' separates aliases,
aliases are tried in order; optional "Display Name = alias | alias"):
    ITV 3
    QVC UK | QVC
    BBC One = BBC One Yorkshire | BBC One

Matching ignores case, spacing, punctuation, HD/SD, Ⓖ/Ⓢ markers, "(720p)",
"[Geo-blocked]" and a trailing "TV"/"UK", and also matches on tvg-id.

Stdlib only. Examples:
    python3 iptv_uk.py                                   # master list only
    python3 iptv_uk.py -d desired_channels.txt           # + filtered playlist
    python3 iptv_uk.py -d desired_channels.txt --fast    # only test desired channels
    python3 iptv_uk.py --source my_extra.m3u8 --source https://example.com/x.m3u
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Sources in priority order: earlier sources win when several work for the same
# channel, and their metadata (logo, tvg-id, chno) is preferred.
DEFAULT_SOURCES = [
    ("bbc",      "bbc",   None),
    ("free-tv",  "m3u",   "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_uk.m3u8"),
    ("iptv-org", "m3u",   "https://iptv-org.github.io/iptv/countries/uk.m3u"),
    ("pluto",    "pluto", "https://i.mjh.nz/PlutoTV/.channels.json"),
]
EXTRA_EPG = {"pluto": "https://i.mjh.nz/PlutoTV/gb.xml.gz"}
# EPG used to fix up tvg-ids so the guide actually matches (and written to x-tvg-url).
DEFAULT_EPG = "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"
# Names the automatic matcher can't work out (abbreviated EPG ids). name -> EPG id.
EPG_ALIASES = {
    "BBC Radio 1": "BBC.R1.uk", "BBC Radio 1Xtra": "BBC.R1X.uk", "BBC Radio 2": "BBC.R2.uk",
    "BBC Radio 3": "BBC.R3.uk", "BBC Radio 4": "BBC.R4.FM.uk", "BBC Radio 4 FM": "BBC.R4.FM.uk",
    "BBC Radio 4 Extra": "BBC.R4.Ex.uk", "BBC Radio 5 Live": "BBC.R5L.uk",
    "BBC Radio 5 Sports Extra": "BBC.R5SX.uk", "BBC Radio 6 Music": "BBC.6.Music.uk",
    "BBC Asian Network": "BBC.Asian.uk", "BBC Radio Asian Network": "BBC.Asian.uk",
    "BBC World Service": "BBC.WS.uk", "TalkTV": "TalkRadio.(TalkTV).uk",
}

# (display name, epgshare tvg-id, BBC PIPs service id) for the BBC mediaselector API.
BBC_SERVICES = [
    ("BBC One", "BBC.One.Lon.HD.uk", "bbc_one_hd"),
    ("BBC Two", "BBC.Two.HD.uk", "bbc_two_hd"),
    ("BBC Three", "BBC.Three.HD.uk", "bbc_three_hd"),
    ("BBC Four", "BBC.Four.HD.uk", "bbc_four_hd"),
    ("CBBC", "CBBC.HD.uk", "cbbc_hd"),
    ("CBeebies", "CBeebies.HD.uk", "cbeebies_hd"),
    ("BBC News", "BBC.NEWS.HD.uk", "bbc_news_channel_hd"),
    ("BBC Parliament", "BBC.Parliament.HD.uk", "bbc_parliament"),
    ("BBC Alba", "BBC.ALBA.HD.uk", "bbc_alba"),
    ("BBC Scotland", "BBCScotlandHD.uk", "bbc_scotland_hd"),
    ("BBC One Scotland", "BBC.One.ScotHD.uk", "bbc_one_scotland_hd"),
    ("BBC One Wales", "BBC.One.Wal.HD.uk", "bbc_one_wales_hd"),
    ("BBC One Northern Ireland", "BBC.One.NI.HD.uk", "bbc_one_northern_ireland_hd"),
    ("BBC Two Northern Ireland", "BBC.Two.NI.HD.uk", "bbc_two_northern_ireland_hd"),
    ("BBC Two Wales", "BBC.Two.Wal.HD.uk", "bbc_two_wales_digital"),
    ("BBC One London", "BBC.One.Lon.HD.uk", "bbc_one_london"),
    ("BBC One Yorkshire", "BBC.One.Yorks.HD.uk", "bbc_one_yorks"),
    ("BBC One East Yorkshire", "BBC.One.Y.and.L.HD.uk", "bbc_one_east_yorkshire"),
    ("BBC One North West", "BBC.One.N.West.HD.uk", "bbc_one_north_west"),
    ("BBC One North East", "BBC.One.NE.and.C.HD.uk", "bbc_one_north_east"),
    ("BBC One East Midlands", "BBC.One.E.Mid.HD.uk", "bbc_one_east_midlands"),
    ("BBC One West Midlands", "BBC.One.WM.HD.uk", "bbc_one_west_midlands"),
    ("BBC One East", "BBC.One.EastHD.uk", "bbc_one_east"),
    ("BBC One South", "BBC.One.Sth.HD.uk", "bbc_one_south"),
    ("BBC One South East", "BBC.One.S.East.HD.uk", "bbc_one_south_east"),
    ("BBC One South West", "BBC.One.S.West.HD.uk", "bbc_one_south_west"),
    ("BBC One West", "BBC.One.West.HD.uk", "bbc_one_west"),
    ("BBC One Oxford", "BBC.One.Sth.HD.uk", "bbc_one_oxford"),
    ("BBC One Cambridge", "BBC.One.EastHD.uk", "bbc_one_cambridge"),
    ("BBC One Channel Islands", "BBC.One.CI.HD.uk", "bbc_one_channel_islands"),
    ("BBC World Service", "BBC.WS.uk", "bbc_world_service"),
]
MEDIASELECTOR = "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/iptv-all/vpid/{}/format/json"

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
OUT_ATTRS = ["tvg-id", "tvg-name", "tvg-logo", "tvg-chno", "tvg-country", "group-title"]
_ssl_ctx = ssl.create_default_context()


# ======================================================================= model

@dataclass
class Candidate:
    source: str
    priority: int
    name: str
    url: str
    attrs: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)   # http-user-agent / http-referrer
    status: str = "UNTESTED"                       # OK / DEAD / SKIP / UNTESTED
    detail: str = ""
    elapsed: float = 0.0

    @property
    def name_key(self) -> str:
        return norm(self.name)

    @property
    def id_key(self) -> str:
        tid = self.attrs.get("tvg-id", "")
        return norm(tid.split("@")[0].rsplit(".", 1)[0]) if tid else ""

    @property
    def keys(self) -> set[str]:
        return {k for k in (self.name_key, self.id_key) if k}


@dataclass
class Desired:
    display: str
    aliases: list[str]          # normalised, in preference order
    epg_id: str = ""            # explicit tvg-id override from "{...}"

    @property
    def keys(self) -> set[str]:
        return set(self.aliases)

    def preference(self, c: Candidate) -> int:
        """Lower is better: exact name match on an earlier alias wins; tvg-id-only matches last."""
        return self.aliases.index(c.name_key) if c.name_key in self.aliases else len(self.aliases)


# =================================================================== matching

_CIRCLED = re.compile(r"[Ⓐ-ⓩ]")            # Ⓖ Ⓢ Ⓨ ...
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_DROP_TOKENS = {"hd", "sd", "fhd", "uhd", "4k"}


def norm(s: str) -> str:
    s = _CIRCLED.sub("", s.lower())
    s = _BRACKETS.sub(" ", s)
    s = s.replace("+1", " plus1 ").replace("&", " and ")
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _DROP_TOKENS]
    key = "".join(tokens)
    changed = True
    while changed:  # strip trailing "tv"/"uk" but never leave a stub ("itv" stays "itv")
        changed = False
        for suffix in ("tv", "uk"):
            if key.endswith(suffix) and len(key) - len(suffix) >= 3:
                key, changed = key[: -len(suffix)], True
    return key


def clean_name(s: str) -> str:
    s = _BRACKETS.sub("", _CIRCLED.sub("", s))
    return re.sub(r"\s+", " ", s).strip()


def load_desired(path: str) -> list[Desired]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            epg_id = ""
            m = re.search(r"\{([^}]*)\}\s*$", line)
            if m:
                epg_id, line = m.group(1).strip(), line[: m.start()].strip()
            display, eq, rest = line.partition("=")
            names = [n.strip() for n in (rest if eq else line).split("|") if n.strip()]
            display = display.strip() if eq else names[0]
            if eq and norm(display) not in map(norm, names):
                names.append(display)  # the display name is always a (last-resort) alias
            out.append(Desired(display, list(dict.fromkeys(norm(n) for n in names)), epg_id))
    return out


# ========================================================================= EPG

@dataclass
class EpgIndex:
    ids: set[str] = field(default_factory=set)
    by_key: dict[str, str] = field(default_factory=dict)

    def resolve(self, current_id: str, names: list[str]) -> str:
        """Keep current_id if the EPG has it, else find an EPG id by name; '' if none."""
        if current_id in self.ids:
            return current_id
        for n in names:
            k = norm(n)
            if k and k in self.by_key:
                return self.by_key[k]
        return ""


def load_epg_index(url: str, timeout: float) -> EpgIndex:
    import gzip
    import io
    import xml.etree.ElementTree as ET

    raw = fetch(url, timeout * 6)[0]
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    idx = EpgIndex()
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "programme":
            break  # channels come first in XMLTV
        if el.tag != "channel":
            continue
        cid = el.get("id", "")
        idx.ids.add(cid)
        base = re.sub(r"\.[a-z]{2}$", "", cid)            # drop ".uk"
        names = [d.text or "" for d in el.findall("display-name")]
        names += [base.replace(".", " ").replace("+", " +")]
        for n in names:
            for variant in (n, re.sub(r"(?i)^u\s*(and|&)\s*", "", n)):   # "U&Dave" -> "Dave"
                k = norm(variant)
                if k and k not in idx.by_key:   # first channel to claim a key keeps it
                    idx.by_key[k] = cid
        el.clear()
    for name, cid in EPG_ALIASES.items():
        if cid in idx.ids:
            idx.by_key[norm(name)] = cid
    return idx


# ==================================================================== fetching

def fetch(url: str, timeout: float, headers: dict | None = None,
          max_bytes: int | None = None) -> tuple[bytes, str, str]:
    """Return (body, final_url, content_type). Raises on HTTP/network error."""
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        if headers.get("http-user-agent"):
            h["User-Agent"] = headers["http-user-agent"]
        if headers.get("http-referrer"):
            h["Referer"] = headers["http-referrer"]
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as resp:
        body = resp.read(max_bytes) if max_bytes else resp.read()
        return body, resp.geturl(), resp.headers.get("Content-Type", "")


def read_text(src: str, timeout: float = 30) -> str:
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            return f.read()
    return fetch(src, timeout)[0].decode("utf-8", errors="replace")


# ===================================================================== sources

def parse_m3u(text: str, source: str, priority: int) -> tuple[list[str], list[Candidate]]:
    """Return (epg urls from header, candidates)."""
    epgs: list[str] = []
    cands: list[Candidate] = []
    extinf: str | None = None
    vlc: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            hdr = dict(ATTR_RE.findall(line))
            for k in ("x-tvg-url", "url-tvg"):
                epgs += [u.strip() for u in hdr.get(k, "").split(",") if u.strip()]
        elif line.startswith("#EXTINF"):
            extinf, vlc = line, {}
        elif line.startswith("#EXTVLCOPT:"):
            k, _, v = line[len("#EXTVLCOPT:"):].partition("=")
            vlc[k.strip()] = v.strip()
        elif line.startswith("#"):
            continue
        elif extinf:
            attrs = dict(ATTR_RE.findall(extinf))
            rest = ATTR_RE.sub("", extinf)
            name = rest.split(",", 1)[1].strip() if "," in rest else attrs.get("tvg-name", "")
            headers = {k: attrs.pop(k, None) or vlc.get(k) for k in ("http-user-agent", "http-referrer")}
            cands.append(Candidate(source, priority, name, line, attrs,
                                   {k: v for k, v in headers.items() if v}))
            extinf = None
    return epgs, cands


def source_bbc(source: str, priority: int, timeout: float) -> list[Candidate]:
    def one(svc):
        name, tid, vpid = svc
        try:
            data = json.loads(fetch(MEDIASELECTOR.format(vpid), timeout)[0])
        except Exception:
            return []
        hrefs = []
        for m in data.get("media", []):
            for c in m.get("connection", []):
                if c.get("transferFormat") == "hls" and c["href"] not in hrefs:
                    hrefs.append(c["href"])
        attrs = {"tvg-id": tid, "tvg-country": "GB",
                 "group-title": "UK Radio" if vpid == "bbc_world_service" else "UK"}
        return [Candidate(source, priority, name, h, dict(attrs)) for h in hrefs[:2]]

    with cf.ThreadPoolExecutor(8) as pool:
        return [c for lst in pool.map(one, BBC_SERVICES) for c in lst]


def source_pluto(source: str, priority: int, url: str, timeout: float) -> list[Candidate]:
    data = json.loads(read_text(url, timeout))
    chans = data.get("regions", {}).get("gb", {}).get("channels", {})
    out = []
    for cid, ch in chans.items():
        attrs = {"tvg-id": cid, "tvg-logo": ch.get("logo", ""), "tvg-chno": str(ch.get("chno", "")),
                 "tvg-country": "GB", "group-title": f"Pluto - {ch.get('group', '')}".rstrip(" -")}
        out.append(Candidate(source, priority, ch["name"], f"https://jmp2.uk/plu-{cid}.m3u8", attrs))
    return out


def gather(sources: list[tuple[str, str, str | None]], timeout: float) -> tuple[list[str], list[Candidate]]:
    epgs: list[str] = []
    cands: list[Candidate] = []
    for prio, (name, kind, url) in enumerate(sources):
        try:
            if kind == "bbc":
                got = source_bbc(name, prio, timeout)
            elif kind == "pluto":
                got = source_pluto(name, prio, url, timeout)
            else:
                e, got = parse_m3u(read_text(url, timeout * 3), name, prio)
                epgs += e
            if name in EXTRA_EPG:
                epgs.append(EXTRA_EPG[name])
            log(f"  {name:<10} {len(got):>4} candidates")
            cands += got
        except Exception as ex:
            log(f"  {name:<10} FAILED: {type(ex).__name__}: {ex}")
    return list(dict.fromkeys(epgs)), cands


# ===================================================================== testing

def playlist_uris(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


def check_stream(url: str, timeout: float, headers: dict, depth: int = 0) -> str:
    body, final_url, ctype = fetch(url, timeout, headers, max_bytes=2_000_000)
    head = body.lstrip()[:512]

    if b"<MPD" in head:
        return "DASH manifest (segments not tested)"
    if not head.startswith(b"#EXTM3U"):
        if ctype.startswith(("audio/", "video/")) or body[:1] == b"\x47":  # 0x47 = MPEG-TS sync
            return f"direct media ({ctype or 'ts'})"
        raise ValueError(f"not a stream (content-type {ctype or '?'})")

    text = body.decode("utf-8", errors="replace")
    uris = playlist_uris(text)
    if not uris:
        raise ValueError("empty playlist")
    if "#EXT-X-STREAM-INF" in text:
        if depth >= 3:
            raise ValueError("too many nested playlists")
        n = text.count("#EXT-X-STREAM-INF")
        return f"{n} variant(s); " + check_stream(urljoin(final_url, uris[0]), timeout, headers, depth + 1)

    seg, _, _ = fetch(urljoin(final_url, uris[-1]), timeout, headers, max_bytes=4096)
    if not seg:
        raise ValueError("segment empty")
    return f"{len(uris)} segment(s), segment fetched"


def test_url(url: str, headers: dict, timeout: float, retries: int) -> tuple[str, str, float]:
    t0 = time.monotonic()
    if re.search(r"(youtube\.com|youtu\.be|dailymotion\.com|twitch\.tv)", url):
        return "SKIP", "web page, needs yt-dlp/streamlink", 0.0
    for attempt in range(retries + 1):
        try:
            return "OK", check_stream(url, timeout, headers), round(time.monotonic() - t0, 2)
        except urllib.error.HTTPError as ex:
            status, detail, retry = "DEAD", f"HTTP {ex.code} {ex.reason}".strip(), ex.code >= 500
        except urllib.error.URLError as ex:
            status, detail, retry = "DEAD", f"URL error: {ex.reason}", True
        except Exception as ex:
            status, detail, retry = "DEAD", f"{type(ex).__name__}: {ex}", True
        if not retry:
            break
        time.sleep(1)
    return status, detail, round(time.monotonic() - t0, 2)


def test_all(cands: list[Candidate], timeout: float, workers: int, retries: int) -> None:
    unique: dict[tuple, list[Candidate]] = {}
    for c in cands:
        unique.setdefault((c.url, tuple(sorted(c.headers.items()))), []).append(c)
    log(f"\nTesting {len(unique)} unique URLs ({len(cands)} candidates) with {workers} workers ...")

    done = 0
    with cf.ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(test_url, url, dict(h), timeout, retries): group
                for (url, h), group in unique.items()}
        for fut in cf.as_completed(futs):
            status, detail, elapsed = fut.result()
            for c in futs[fut]:
                c.status, c.detail, c.elapsed = status, detail, elapsed
            done += 1
            if sys.stderr.isatty():
                print(f"\r  {done}/{len(unique)}", end="", file=sys.stderr, flush=True)
    if sys.stderr.isatty():
        print(file=sys.stderr)


# ===================================================================== output

def rank(c: Candidate) -> tuple:
    return ({"OK": 0, "SKIP": 1}.get(c.status, 2), c.priority, c.elapsed)


def merged_attrs(group: list[Candidate]) -> dict:
    """First non-empty value per attribute, in source-priority order."""
    attrs: dict = {}
    for c in sorted(group, key=lambda c: c.priority):
        for k, v in c.attrs.items():
            if v and not attrs.get(k):
                attrs[k] = v
    return attrs


def entry_text(name: str, best: Candidate, attrs: dict) -> str:
    attrs = dict(attrs, **{"tvg-name": name})
    parts = [f'{k}="{attrs[k]}"' for k in OUT_ATTRS if attrs.get(k)]
    lines = [f"#EXTINF:-1 {' '.join(parts)},{name}"]
    lines += [f"#EXTVLCOPT:{k}={v}" for k, v in best.headers.items()]
    lines.append(best.url)
    return "\n".join(lines)


def write_playlist(path: str, epgs: list[str], entries: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U x-tvg-url="{", ".join(epgs)}"\n' if epgs else "#EXTM3U\n")
        for e in entries:
            f.write(e + "\n")


def fix_epg_id(attrs: dict, names: list[str], epg: EpgIndex | None, override: str = "") -> dict:
    if override:
        return dict(attrs, **{"tvg-id": override})
    if epg:
        new = epg.resolve(attrs.get("tvg-id", ""), names)
        if new:
            return dict(attrs, **{"tvg-id": new})
    return attrs


def build_master(cands: list[Candidate], include_unverified: bool,
                 epg: EpgIndex | None) -> list[tuple[str, Candidate, dict]]:
    groups: dict[str, list[Candidate]] = {}
    for c in cands:  # cands are already in source-priority order
        if c.name_key:
            groups.setdefault(c.name_key, []).append(c)
    out = []
    for group in groups.values():
        best = min(group, key=rank)
        if best.status == "OK" or (include_unverified and best.status == "SKIP"):
            name = clean_name(min(group, key=lambda c: c.priority).name)
            attrs = fix_epg_id(merged_attrs(group), [name] + [c.name for c in group], epg)
            out.append((name, best, attrs))
    return out


def build_desired(cands: list[Candidate], desired: list[Desired], include_unverified: bool,
                  epg: EpgIndex | None):
    rows, entries = [], []
    for d in desired:
        matches = [c for c in cands if c.keys & d.keys]
        # Working first, then earliest alias matched by name (tvg-id-only matches last),
        # then source priority, then speed.
        matches.sort(key=lambda c: (rank(c)[0], d.preference(c), c.priority, c.elapsed))
        best = matches[0] if matches else None
        ok = best and (best.status == "OK" or (include_unverified and best.status == "SKIP"))
        if ok:
            # metadata: the chosen stream's own attrs first, gaps filled from other matches
            attrs = dict(merged_attrs(matches), **{k: v for k, v in best.attrs.items() if v})
            attrs = fix_epg_id(attrs, [d.display, best.name] + [c.name for c in matches], epg, d.epg_id)
            entries.append(entry_text(d.display, best, attrs))
        rows.append((d, best if ok else None, matches))
    return rows, entries


# ======================================================================= main

def log(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--desired", help="desired channel list (one per line, '|' for aliases)")
    ap.add_argument("-o", "--output", default="playlist_desired.m3u8", help="filtered playlist (needs -d)")
    ap.add_argument("-m", "--master", default="playlist_master.m3u8", help="all working channels")
    ap.add_argument("--report", help="write full per-candidate results as JSON")
    ap.add_argument("--source", action="append", default=[], metavar="URL_OR_FILE",
                    help="extra M3U source (repeatable); lowest priority")
    ap.add_argument("--no-default-sources", action="store_true", help="use only --source entries")
    ap.add_argument("--exclude-source", action="append", default=[], metavar="NAME",
                    help=f"skip a built-in source ({', '.join(s[0] for s in DEFAULT_SOURCES)})")
    ap.add_argument("--fast", action="store_true", help="only test candidates matching the desired list")
    ap.add_argument("--include-unverified", action="store_true",
                    help="include YouTube/web-page entries that can't be tested")
    ap.add_argument("--epg", help="override the x-tvg-url EPG header (comma-separated)")
    ap.add_argument("--epg-map", default=DEFAULT_EPG, metavar="URL",
                    help="XMLTV file used to correct tvg-ids so the guide matches (default: epgshare UK1)")
    ap.add_argument("--no-epg-map", action="store_true", help="keep source tvg-ids as-is")
    ap.add_argument("--epg-from-sources", action="store_true",
                    help="use the (large) x-tvg-url lists from the source playlists in the header")
    ap.add_argument("-t", "--timeout", type=float, default=10.0)
    ap.add_argument("-w", "--workers", type=int, default=32)
    ap.add_argument("--retries", type=int, default=1, help="retries for timeouts / 5xx")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every candidate tried per desired channel")
    args = ap.parse_args()

    sources = [] if args.no_default_sources else [s for s in DEFAULT_SOURCES if s[0] not in args.exclude_source]
    for i, s in enumerate(args.source):
        label = os.path.basename(s) if os.path.exists(s) else f"extra{i + 1}"
        sources.append((label, "m3u", s))

    log("Gathering sources:")
    source_epgs, cands = gather(sources, args.timeout)

    epg = None
    if not args.no_epg_map:
        try:
            epg = load_epg_index(args.epg_map, args.timeout)
            log(f"  EPG map    {len(epg.ids):>4} channels from {args.epg_map}")
        except Exception as ex:
            log(f"  EPG map    FAILED ({type(ex).__name__}: {ex}); keeping source tvg-ids")

    def header_epgs(chosen: list[Candidate]) -> list[str]:
        if args.epg:
            return [u.strip() for u in args.epg.split(",") if u.strip()]
        if args.epg_from_sources:
            return source_epgs
        urls = [args.epg_map] if epg else []
        urls += [EXTRA_EPG[s] for s in dict.fromkeys(c.source for c in chosen) if s in EXTRA_EPG]
        return urls

    desired = load_desired(args.desired) if args.desired else None
    to_test = cands
    if desired and args.fast:
        wanted = set().union(*(d.keys for d in desired)) if desired else set()
        to_test = [c for c in cands if c.keys & wanted]
    test_all(to_test, args.timeout, args.workers, args.retries)

    # ---- per-source stats
    log("\nPer source:")
    for name, *_ in sources:
        sc = [c for c in to_test if c.source == name]
        if sc:
            log(f"  {name:<10} {sum(c.status == 'OK' for c in sc):>4} / {len(sc):<4} working")

    # ---- master list
    if not args.fast:
        master = build_master(cands, args.include_unverified, epg)
        write_playlist(args.master, header_epgs([b for _, b, _ in master]),
                       [entry_text(n, b, a) for n, b, a in master])
        log(f"\nMaster: {len(master)} working channels -> {args.master}")

    # ---- desired list
    if desired:
        rows, entries = build_desired(to_test, desired, args.include_unverified, epg)
        tty = sys.stdout.isatty()
        g, r, y, z = ("\033[32m", "\033[31m", "\033[33m", "\033[0m") if tty else ("",) * 4
        width = max(len(d.display) for d in desired)
        print()
        for d, best, matches in rows:
            if best:
                print(f"{g}OK  {z}  {d.display:<{width}}  [{best.source}] {best.url}")
            elif matches:
                print(f"{r}DEAD{z}  {d.display:<{width}}  all {len(matches)} candidate(s) failed")
            else:
                print(f"{y}MISS{z}  {d.display:<{width}}  not found in any source")
            if args.verbose:
                for c in matches:
                    print(f"        {c.status:<4} [{c.source}] {c.name!r}: {c.detail}\n"
                          f"             {c.url}")
        n_ok = sum(1 for _, b, _ in rows if b)
        write_playlist(args.output, header_epgs([b for _, b, _ in rows if b]), entries)
        print(f"\nDesired: {n_ok}/{len(desired)} channels working -> {args.output}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump([{k: getattr(c, k) for k in ("source", "name", "url", "status", "detail",
                                                   "elapsed", "attrs", "headers")}
                       for c in cands], f, indent=2)
        log(f"Report -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
