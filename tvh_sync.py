#!/usr/bin/env python3
"""
Keep a managed set of verified IPTV streams in Tvheadend as failover for
your Freeview/Freesat channels (and as extra IPTV-only channels).

It uses iptv_uk.py to find working streams for the channels in your desired
list, then, through Tvheadend's HTTP API:

  * keeps ONE IPTV network that it owns (default name "UKIPTV (tvh_sync)"),
    with one mux per stream (the best N working streams per channel);
  * adds those streams as extra, LOWER-priority services on your existing
    DVB channels with the same name, so Tvheadend falls back to IPTV when
    the aerial/dish signal fails;
  * creates channels for IPTV-only entries, tagged with a marker tag;
  * removes or disables what it created when streams die or channels leave
    the list.

It never edits, deletes or renames anything it did not create. The only change
it makes to your own channels is adding/removing its own services in their
service list. Every --apply run saves a backup and logs each change.

Nothing is changed unless you pass --apply.

    python3 tvh_sync.py                    # dry run: show what would change
    python3 tvh_sync.py --apply            # make the changes
    python3 tvh_sync.py --status           # show what it manages now
    python3 tvh_sync.py --disable --apply  # switch all its IPTV streams off
    python3 tvh_sync.py --enable --apply   # switch them back on
    python3 tvh_sync.py --uninstall --apply  # remove everything it created

Settings (Tvheadend address, login, options) live in tvh_sync.conf. See
tvh_sync.conf.example.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iptv_uk as iu  # noqa: E402

MANAGED_COMMENT = "Managed by tvh_sync.py (ukiptv). Removed by: tvh_sync.py --uninstall"
MM_DISABLE, MM_ENABLE = 0, 1
SCAN_IDLE, SCAN_OK, SCAN_FAIL, SCAN_PARTIAL = 0, 1, 2, 3


def say(msg: str = "") -> None:
    print(msg, flush=True)


class ConfigError(Exception):
    pass


class TvhError(Exception):
    pass


# ====================================================================== config

@dataclass
class Config:
    path: str
    url: str
    username: str
    password: str
    http_timeout: float = 20.0
    # sync
    desired: str = "desired_channels.txt"
    network_name: str = "UKIPTV (tvh_sync)"
    marker_tag: str = "ukiptv-managed"
    extra_tag: str = "IPTV"
    streams_per_channel: int = 2
    service_priority: int = -5
    network_priority: int = 1
    prefer_iptv: list = field(default_factory=list)
    attach_to_existing: bool = True
    create_channels: bool = True
    channel_number_start: int = 1000
    stream_mode: str = "auto"
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    scan_wait: int = 300
    failed_retry_hours: float = 24.0
    state_dir: str = ".tvh_sync"
    keep_backups: int = 30
    # stream finding (iptv_uk)
    test_timeout: float = 10.0
    workers: int = 32
    retries: int = 1
    exclude_sources: list = field(default_factory=list)
    extra_sources: list = field(default_factory=list)
    epg_map: str = iu.DEFAULT_EPG
    # [channel_map]: desired display name -> list of existing TVH channel names, or ["new"] / ["none"]
    channel_map: dict = field(default_factory=dict)


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser(delimiters=("=",), interpolation=None,
                                   comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    cp.optionxform = str  # keep case (channel names)
    if os.path.exists(path):
        cp.read(path, encoding="utf-8")
    elif not os.environ.get("TVH_URL"):
        raise ConfigError(f"config file {path!r} not found "
                          "(copy tvh_sync.conf.example to tvh_sync.conf and edit it)")
    base = os.path.dirname(os.path.abspath(path))

    def get(sec, key, default=""):
        return cp.get(sec, key, fallback=default).strip() if cp.has_section(sec) else default

    def getbool(sec, key, default):
        v = get(sec, key, "")
        if not v:
            return default
        if v.lower() in ("1", "yes", "true", "on"):
            return True
        if v.lower() in ("0", "no", "false", "off"):
            return False
        raise ConfigError(f"[{sec}] {key}: expected yes/no, got {v!r}")

    def getnum(sec, key, default, kind=int):
        v = get(sec, key, "")
        try:
            return kind(v) if v else default
        except ValueError:
            raise ConfigError(f"[{sec}] {key}: expected a number, got {v!r}")

    def rel(p):
        return p if not p or os.path.isabs(p) or re.match(r"^\w+://", p) else os.path.join(base, p)

    password = os.environ.get("TVH_PASSWORD") or get("tvheadend", "password")
    pw_file = os.environ.get("TVH_PASSWORD_FILE") or get("tvheadend", "password_file")
    if pw_file:
        with open(rel(pw_file), encoding="utf-8") as f:
            password = f.read().strip()

    c = Config(
        path=path,
        url=(os.environ.get("TVH_URL") or get("tvheadend", "url")).rstrip("/"),
        username=os.environ.get("TVH_USER") or get("tvheadend", "username"),
        password=password,
        http_timeout=getnum("tvheadend", "timeout", 20.0, float),
    )
    if not c.url:
        raise ConfigError("[tvheadend] url is not set")
    if not re.match(r"^https?://", c.url):
        raise ConfigError(f"[tvheadend] url must start with http:// or https:// (got {c.url!r})")

    s = "sync"
    c.desired = rel(get(s, "desired", c.desired))
    c.network_name = get(s, "network_name", c.network_name)
    c.marker_tag = get(s, "marker_tag", c.marker_tag)
    c.extra_tag = get(s, "extra_tag", c.extra_tag) if cp.has_option(s, "extra_tag") else c.extra_tag
    c.streams_per_channel = max(1, getnum(s, "streams_per_channel", c.streams_per_channel))
    c.service_priority = getnum(s, "service_priority", c.service_priority)
    c.network_priority = getnum(s, "network_priority", c.network_priority)
    c.prefer_iptv = _split(get(s, "prefer_iptv"))
    c.attach_to_existing = getbool(s, "attach_to_existing", c.attach_to_existing)
    c.create_channels = getbool(s, "create_channels", c.create_channels)
    c.channel_number_start = getnum(s, "channel_number_start", c.channel_number_start)
    c.stream_mode = get(s, "stream_mode", c.stream_mode).lower()
    c.ffmpeg_path = get(s, "ffmpeg_path", c.ffmpeg_path)
    c.scan_wait = getnum(s, "scan_wait", c.scan_wait)
    c.failed_retry_hours = getnum(s, "failed_retry_hours", c.failed_retry_hours, float)
    c.state_dir = rel(get(s, "state_dir", c.state_dir))
    c.keep_backups = getnum(s, "keep_backups", c.keep_backups)
    if c.stream_mode not in ("auto", "direct", "ffmpeg"):
        raise ConfigError("[sync] stream_mode must be auto, direct or ffmpeg")
    if not c.network_name or not c.marker_tag:
        raise ConfigError("[sync] network_name and marker_tag must not be empty")

    s = "streams"
    c.test_timeout = getnum(s, "timeout", c.test_timeout, float)
    c.workers = getnum(s, "workers", c.workers)
    c.retries = getnum(s, "retries", c.retries)
    c.exclude_sources = _split(get(s, "exclude_sources"))
    c.extra_sources = [rel(x) for x in _split(get(s, "extra_sources"))]
    c.epg_map = get(s, "epg_map", c.epg_map) or c.epg_map

    if cp.has_section("channel_map"):
        for k, v in cp.items("channel_map"):
            c.channel_map[iu.norm(k)] = _split(v)
    return c


# ================================================================ Tvheadend API

class Tvh:
    """Minimal Tvheadend HTTP API client with Digest (MD5/SHA-256) or Basic auth."""

    def __init__(self, url: str, user: str, password: str, timeout: float):
        self.base, self.user, self.password, self.timeout = url, user, password, timeout
        self._challenge: tuple[str, dict] | None = None
        self._nc = 0

    # ---- auth
    @staticmethod
    def _parse_challenges(headers: list[str]) -> list[tuple[str, dict]]:
        out = []
        for h in headers:
            scheme, _, rest = h.strip().partition(" ")
            params = {}
            for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest):
                params[key.lower()] = quoted or bare
            out.append((scheme.lower(), params))
        return out

    def _pick(self, challenges):
        order = {"SHA-256": 0, "MD5": 1}
        digests = [c for c in challenges if c[0] == "digest"
                   and c[1].get("algorithm", "MD5").upper() in order]
        if digests:
            return min(digests, key=lambda c: order[c[1].get("algorithm", "MD5").upper()])
        for c in challenges:
            if c[0] == "basic":
                return c
        raise TvhError("Tvheadend asked for an authentication method this script doesn't support: "
                       + ", ".join(c[0] for c in challenges))

    def _auth_header(self, method: str, uri: str) -> str | None:
        if not self._challenge or not self.user:
            return None
        scheme, p = self._challenge
        if scheme == "basic":
            return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        algo = p.get("algorithm", "MD5").upper()
        H = (lambda s: hashlib.sha256(s.encode()).hexdigest()) if algo == "SHA-256" else \
            (lambda s: hashlib.md5(s.encode()).hexdigest())
        ha1 = H(f"{self.user}:{p.get('realm', '')}:{self.password}")
        ha2 = H(f"{method}:{uri}")
        self._nc += 1
        nc, cnonce = f"{self._nc:08x}", hashlib.md5(os.urandom(8)).hexdigest()[:16]
        qop = "auth" if "auth" in [q.strip() for q in p.get("qop", "").split(",")] else ""
        resp = H(f"{ha1}:{p['nonce']}:{nc}:{cnonce}:{qop}:{ha2}") if qop else H(f"{ha1}:{p['nonce']}:{ha2}")
        parts = [f'username="{self.user}"', f'realm="{p.get("realm", "")}"', f'nonce="{p["nonce"]}"',
                 f'uri="{uri}"', f'response="{resp}"', f"algorithm={algo}"]
        if qop:
            parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        if "opaque" in p:
            parts.append(f'opaque="{p["opaque"]}"')
        return "Digest " + ", ".join(parts)

    # ---- calls
    def call(self, path: str, **args) -> dict:
        uri = "/api/" + path
        data = urllib.parse.urlencode({
            k: json.dumps(v) if isinstance(v, (dict, list)) else str(int(v) if isinstance(v, bool) else v)
            for k, v in args.items() if v is not None}).encode()
        for attempt in range(3):
            req = urllib.request.Request(self.base + uri, data=data, method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "tvh_sync"})
            auth = self._auth_header("POST", uri)
            if auth:
                req.add_header("Authorization", auth)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as ex:
                if ex.code == 401 and self.user and attempt < 2:
                    challenges = self._parse_challenges(ex.headers.get_all("WWW-Authenticate") or [])
                    if not challenges:
                        break
                    new = self._pick(challenges)
                    stale = new[1].get("stale", "").lower() == "true"
                    if attempt > 0 and not stale:
                        raise TvhError("login failed: check [tvheadend] username/password")
                    self._challenge, self._nc = new, 0
                    continue
                if ex.code == 401:
                    raise TvhError("login failed: check [tvheadend] username/password")
                if ex.code == 403:
                    raise TvhError(f"access denied for /api/{path}: the Tvheadend user needs "
                                   "admin rights (Configuration > Users > Access entries)")
                if ex.code == 404:
                    raise TvhError(f"/api/{path} not found (object already gone, or unsupported version)")
                raise TvhError(f"/api/{path} failed: HTTP {ex.code} (Tvheadend rejected the request)")
            except urllib.error.URLError as ex:
                raise TvhError(f"cannot reach Tvheadend at {self.base}: {ex.reason}")
        raise TvhError("login failed: check [tvheadend] username/password")

    def grid(self, path: str, **args) -> list[dict]:
        return self.call(path, start=0, limit=1_000_000, **args).get("entries", [])

    def save(self, node: dict) -> None:
        self.call("idnode/save", node=node)

    def delete(self, uuids: list[str]) -> None:
        if uuids:
            self.call("idnode/delete", uuid=uuids)

    def node_class(self, uuid: str) -> str:
        ents = self.call("idnode/load", uuid=uuid, meta=0).get("entries", [])
        return ents[0].get("class", "") if ents else ""


# ===================================================================== state

class State:
    """Local memory between runs: URLs Tvheadend failed to play, plus backups and a change log."""

    def __init__(self, cfg: Config):
        self.dir = cfg.state_dir
        self.path = os.path.join(self.dir, "state.json")
        self.retry_secs = cfg.failed_retry_hours * 3600
        self.failed: dict[str, dict] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.failed = json.load(f).get("failed", {})
        except (OSError, ValueError):
            pass
        now = time.time()
        self.failed = {u: v for u, v in self.failed.items() if now - v.get("time", 0) < self.retry_secs}

    def is_failed(self, url: str) -> bool:
        return url in self.failed

    def mark_failed(self, url: str, reason: str) -> None:
        self.failed[url] = {"time": time.time(), "reason": reason}

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"failed": self.failed}, f, indent=1)
        os.replace(tmp, self.path)

    def journal(self, action: str, **info) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "changes.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"),
                                "action": action, **info}) + "\n")

    def backup(self, snap: "Snapshot", keep: int) -> str:
        d = os.path.join(self.dir, "backups")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"network": snap.network, "muxes": snap.muxes, "services": snap.services,
                       "channels": snap.channels, "tags": snap.tags}, f, indent=1)
        old = sorted(os.listdir(d))[:-keep] if keep > 0 else []
        for name in old:
            os.remove(os.path.join(d, name))
        return path


# ================================================================== snapshot

@dataclass
class Snapshot:
    network: dict | None
    muxes: list[dict]
    services: list[dict]
    channels: list[dict]
    tags: list[dict]

    @classmethod
    def fetch(cls, tvh: Tvh, cfg: Config) -> "Snapshot":
        nets = [n for n in tvh.grid("mpegts/network/grid") if n.get("networkname") == cfg.network_name]
        if len(nets) > 1:
            raise TvhError(f"{len(nets)} networks are called {cfg.network_name!r}; "
                           "rename the extra ones or change [sync] network_name")
        net = nets[0] if nets else None
        if net:
            cls_name = tvh.node_class(net["uuid"])
            if cls_name != "iptv_network":
                raise TvhError(f"network {cfg.network_name!r} exists but is a {cls_name or 'unknown'} "
                               "network, not one this script created. Change [sync] network_name.")
        muxes, services = [], []
        if net:
            for m in tvh.grid("mpegts/mux/grid", hidemode="none"):
                if "network_uuid" in m:
                    if m["network_uuid"] == net["uuid"]:
                        muxes.append(m)
                elif m.get("network") == cfg.network_name:
                    muxes.append(m)
            mux_ids = {m["uuid"] for m in muxes}
            for s in tvh.grid("mpegts/service/grid", hidemode="none"):
                if "multiplex_uuid" not in s:
                    raise TvhError("this Tvheadend version doesn't report service mux ids; unsupported")
                if s["multiplex_uuid"] in mux_ids:
                    services.append(s)
        channels = tvh.grid("channel/grid", all=1)
        tags = tvh.grid("channeltag/grid", all=1)
        return cls(net, muxes, services, channels, tags)

    def tag_uuid(self, name: str) -> str | None:
        for t in self.tags:
            if t.get("name") == name:
                return t["uuid"]
        return None

    def managed_channels(self, marker: str) -> list[dict]:
        tid = self.tag_uuid(marker)
        return [c for c in self.channels if tid and tid in (c.get("tags") or [])]

    def services_of(self, mux_uuid: str) -> list[dict]:
        return [s for s in self.services if s.get("multiplex_uuid") == mux_uuid]


# ============================================================ finding streams

@dataclass
class Stream:
    tvh_url: str          # what goes in the mux's URL field (http(s):// or pipe://)
    hdr: str              # custom HTTP headers (direct mode only)
    cand: iu.Candidate


@dataclass
class Wanted:
    index: int
    d: iu.Desired
    streams: list[Stream]
    attrs: dict

    @property
    def display(self) -> str:
        return self.d.display

    @property
    def keys(self) -> set[str]:
        return self.d.keys | {iu.norm(self.d.display)}


def _q(s: str) -> str:
    """Quote one argument for Tvheadend's pipe:// parser."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pipe_url(cfg: Config, c: iu.Candidate) -> str:
    args = [cfg.ffmpeg_path, "-loglevel", "fatal", "-nostdin"]
    if c.headers.get("http-user-agent"):
        args += ["-user_agent", _q(c.headers["http-user-agent"])]
    if c.headers.get("http-referrer"):
        args += ["-headers", _q("Referer: " + c.headers["http-referrer"]) + "\\r\\n"]
    args += ["-i", _q(c.url), "-c", "copy", "-map", "0", "-f", "mpegts", "pipe:1"]
    return "pipe://" + " ".join(args)


def direct_hdr(c: iu.Candidate) -> str:
    lines = []
    if c.headers.get("http-user-agent"):
        lines.append("User-Agent: " + c.headers["http-user-agent"])
    if c.headers.get("http-referrer"):
        lines.append("Referer: " + c.headers["http-referrer"])
    return "\n".join(lines)


def stream_for(cfg: Config, c: iu.Candidate, state: State) -> Stream | None:
    """Pick how Tvheadend should open this candidate, skipping ways that failed recently."""
    needs_ffmpeg = c.detail.startswith("DASH")
    options = []
    if cfg.stream_mode in ("auto", "direct") and not needs_ffmpeg:
        options.append(Stream(c.url, direct_hdr(c), c))
    if cfg.stream_mode in ("auto", "ffmpeg") and cfg.ffmpeg_path:
        options.append(Stream(pipe_url(cfg, c), "", c))
    for s in options:
        if not state.is_failed(s.tvh_url):
            return s
    return None


def find_streams(cfg: Config, desired: list[iu.Desired], state: State,
                 existing: set[tuple[str, str]]) -> list[Wanted]:
    sources = [s for s in iu.DEFAULT_SOURCES if s[0] not in cfg.exclude_sources]
    for i, s in enumerate(cfg.extra_sources):
        sources.append((os.path.basename(s) if os.path.exists(s) else f"extra{i + 1}", "m3u", s))
    iu.log("Finding streams:")
    _, cands = iu.gather(sources, cfg.test_timeout)
    epg = None
    if cfg.epg_map.lower() != "none":
        try:
            epg = iu.load_epg_index(cfg.epg_map, cfg.test_timeout)
        except Exception as ex:
            iu.log(f"  EPG map    FAILED ({type(ex).__name__}: {ex}); keeping source tvg-ids")
    wanted_keys = set().union(*(d.keys | {iu.norm(d.display)} for d in desired)) if desired else set()
    to_test = [c for c in cands if c.keys & wanted_keys]
    iu.test_all(to_test, cfg.test_timeout, cfg.workers, cfg.retries)

    out = []
    for i, d in enumerate(desired):
        keys = d.keys | {iu.norm(d.display)}
        matches = [c for c in to_test if c.keys & keys]
        options = []
        for c in matches:
            if c.status != "OK":
                continue
            s = stream_for(cfg, c, state)
            if s:
                options.append(s)
        # Preferred alias, then source priority, then keep what's already in Tvheadend
        # (avoids churn), then speed.
        options.sort(key=lambda s: (d.preference(s.cand), s.cand.priority,
                                    (d.display, s.tvh_url) not in existing, s.cand.elapsed))
        # One stream per real source: the same URL over http and https (the BBC API
        # returns both) would give no failover at all.
        streams, seen = [], set()
        for s in options:
            same = re.sub(r"^https?://", "", s.cand.url)
            if same not in seen:
                seen.add(same)
                streams.append(s)
        streams = streams[:cfg.streams_per_channel]
        attrs = iu.merged_attrs(matches)
        if streams:
            attrs.update({k: v for k, v in streams[0].cand.attrs.items() if v})
        attrs = iu.fix_epg_id(attrs, [d.display] + [c.name for c in matches], epg, d.epg_id)
        out.append(Wanted(i, d, streams, attrs))
    return out


# ======================================================================== sync

class Changer:
    """Prints each change; performs and journals it only with --apply."""

    def __init__(self, tvh: Tvh, state: State, apply: bool):
        self.tvh, self.state, self.apply, self.count = tvh, state, apply, 0

    def __call__(self, symbol: str, text: str, action: str, fn=None, **info):
        self.count += 1
        say(f"  {symbol} {text}")
        if not self.apply or fn is None:
            return None
        result = fn()
        self.state.journal(action, **info)
        return result


def priority_for(cfg: Config, w: Wanted, rank: int) -> int:
    base = abs(cfg.service_priority) if iu.norm(w.display) in {iu.norm(n) for n in cfg.prefer_iptv} \
        else cfg.service_priority
    return max(-10, min(10, base - rank))


def ensure_tag(tvh: Tvh, snap: Snapshot, ch: Changer, name: str, internal: bool) -> str | None:
    uuid = snap.tag_uuid(name)
    if uuid:
        return uuid
    conf = {"name": name, "enabled": True, "internal": internal, "comment": MANAGED_COMMENT}
    res = ch("+", f"channel tag {name!r}" + (" (internal marker)" if internal else ""),
             "create_tag", lambda: tvh.call("channeltag/create", conf=conf), conf=conf)
    if res and res.get("uuid"):
        snap.tags.append({"uuid": res["uuid"], "name": name})
        return res["uuid"]
    return None


def channel_keys(name: str) -> set[str]:
    """Match keys for one of your channel names. 'U&Dave' also matches 'Dave' (UKTV's branding)."""
    return {k for k in (iu.norm(name), iu.norm(re.sub(r"(?i)^u\s*(and|&)\s*", "", name))) if k}


def existing_targets(cfg: Config, snap: Snapshot, w: Wanted, managed_ids: set[str]) -> list[dict] | None:
    """Your own channels this entry should back up. None means 'use a channel of our own'."""
    mapped = cfg.channel_map.get(iu.norm(w.display))
    others = [c for c in snap.channels if c["uuid"] not in managed_ids]
    if mapped is not None:
        if [m.lower() for m in mapped] in (["new"], ["none"]):
            return None if mapped[0].lower() == "new" else []
        want = {iu.norm(m) for m in mapped}
        hits = [c for c in others if iu.norm(c.get("name", "")) in want]
        if not hits:
            say(f"  ! [channel_map] {w.display!r}: no channel called {', '.join(mapped)}")
        return hits
    if not cfg.attach_to_existing:
        return None
    hits = [c for c in others if c.get("enabled", True) and channel_keys(c.get("name", "")) & w.keys]
    return hits or None


def similar_channels(snap: Snapshot, w: Wanted, managed_ids: set[str], n: int = 4) -> list[str]:
    """Your channels whose names look like this entry (for hints when nothing matched exactly)."""
    chans = [c for c in snap.channels if c["uuid"] not in managed_ids and c.get("name")]
    keys = {k for k in w.keys if k}
    scored = []
    for c in chans:
        cks = channel_keys(c["name"])
        if not cks:
            continue
        exact = bool(cks & keys)
        prefix = any(k.startswith(x) or x.startswith(k) for k in cks for x in keys if min(len(k), len(x)) >= 4)
        ratio = max(difflib.SequenceMatcher(None, k, x).ratio() for k in cks for x in keys)
        if exact or prefix or ratio >= 0.8:
            scored.append((not exact, not prefix, -ratio, c))
    scored.sort(key=lambda t: t[:3])
    return [repr(c["name"]) + ("" if c.get("enabled", True) else " (disabled)") for *_, c in scored[:n]]


def wait_for_scan(tvh: Tvh, cfg: Config, mux_ids: set[str], wait: int) -> None:
    if not mux_ids or wait <= 0:
        return
    say(f"\nWaiting up to {wait}s for Tvheadend to scan {len(mux_ids)} new mux(es) ...")
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        snap = Snapshot.fetch(tvh, cfg)
        pending = 0
        for m in snap.muxes:
            if m["uuid"] not in mux_ids:
                continue
            done = snap.services_of(m["uuid"]) or (
                m.get("scan_state", 0) == SCAN_IDLE and m.get("scan_result", 0) != 0)
            pending += not done
        if not pending:
            say(f"  scan finished after {int(time.monotonic() - t0)}s")
            return
        time.sleep(5)
    say("  still scanning; the rest will be mapped on the next run")


def run_sync(cfg: Config, tvh: Tvh, state: State, apply: bool) -> int:
    snap = Snapshot.fetch(tvh, cfg)
    if snap.network and not snap.network.get("enabled", True):
        say(f"Network {cfg.network_name!r} is disabled (tvh_sync.py --disable). Nothing to do.\n"
            "Run 'tvh_sync.py --enable --apply' to switch it back on.")
        return 0

    desired = iu.load_desired(cfg.desired)
    existing = {(m.get("iptv_muxname", ""), m.get("iptv_url", "")) for m in snap.muxes}
    # "[channel_map] Name = none" means: leave this channel out of Tvheadend entirely.
    desired = [d for d in desired
               if [x.lower() for x in cfg.channel_map.get(iu.norm(d.display), [])] != ["none"]]
    wanted = find_streams(cfg, desired, state, existing)
    if apply:
        say(f"\nBackup of current Tvheadend state: {state.backup(snap, cfg.keep_backups)}")

    say("\nPlan:" if not apply else "\nChanges:")
    ch = Changer(tvh, state, apply)

    # ---- 1. network
    net_uuid = snap.network["uuid"] if snap.network else None
    if not net_uuid:
        conf = {"networkname": cfg.network_name, "priority": cfg.network_priority,
                "spriority": cfg.network_priority, "max_streams": 0, "max_timeout": 15,
                "scan_create": True, "skipinitscan": True, "idlescan": False}
        res = ch("+", f"IPTV network {cfg.network_name!r}", "create_network",
                 lambda: tvh.call("mpegts/network/create", **{"class": "iptv_network"}, conf=conf),
                 conf=conf)
        net_uuid = res.get("uuid") if res else None
        if apply and not net_uuid:
            raise TvhError("Tvheadend did not return a uuid for the new network")

    # ---- 2. muxes
    by_key = {(m.get("iptv_muxname", ""), m.get("iptv_url", "")): m for m in snap.muxes}
    wanted_keys: set[tuple[str, str]] = set()
    keep_disabled: set[str] = set()
    new_mux_ids: set[str] = set()
    displays = {w.display for w in wanted}
    for w in wanted:
        if not w.streams:
            # No working stream right now: keep any existing muxes, but switched off.
            for m in snap.muxes:
                if m.get("iptv_muxname") == w.display:
                    keep_disabled.add(m["uuid"])
                    if m.get("enabled", 1) != MM_DISABLE:
                        ch("~", f"{w.display}: disable mux (no working stream now) {m.get('iptv_url', '')[:70]}",
                           "disable_mux", lambda m=m: tvh.save({"uuid": m["uuid"], "enabled": MM_DISABLE}),
                           uuid=m["uuid"])
            continue
        for rank, s in enumerate(w.streams):
            key = (w.display, s.tvh_url)
            wanted_keys.add(key)
            props = {"iptv_sname": w.display, "iptv_hdr": s.hdr, "enabled": MM_ENABLE,
                     "iptv_epgid": w.attrs.get("tvg-id", ""), "iptv_icon": w.attrs.get("tvg-logo", "")}
            label = f"{w.display}: [{s.cand.source}] {s.tvh_url[:90]}"
            m = by_key.get(key)
            if m:
                diff = {k: v for k, v in props.items()
                        if (m.get("enabled", 1) if k == "enabled" else (m.get(k) or "")) != v}
                if diff:
                    ch("~", f"update mux {label} ({', '.join(diff)})", "update_mux",
                       lambda m=m, diff=diff: tvh.save({"uuid": m["uuid"], **diff}),
                       uuid=m["uuid"], before={k: m.get(k) for k in diff}, after=diff)
            else:
                conf = {"iptv_muxname": w.display, "iptv_url": s.tvh_url, **props}
                res = ch("+", f"mux {label}", "create_mux",
                         lambda conf=conf: tvh.call("mpegts/network/mux_create", uuid=net_uuid, conf=conf),
                         conf=conf)
                if res and res.get("uuid"):
                    new_mux_ids.add(res["uuid"])

    stale = [m for m in snap.muxes
             if (m.get("iptv_muxname", ""), m.get("iptv_url", "")) not in wanted_keys
             and m["uuid"] not in keep_disabled]

    # ---- 3. wait for Tvheadend to scan new muxes, then drop ones it can't play
    if apply and new_mux_ids:
        wait_for_scan(tvh, cfg, new_mux_ids, cfg.scan_wait)
    if apply:
        snap = Snapshot.fetch(tvh, cfg)
    for m in snap.muxes:
        if (m.get("enabled", 1) == MM_ENABLE and not snap.services_of(m["uuid"])
                and m.get("scan_state", 0) == SCAN_IDLE and m.get("scan_result", 0) == SCAN_FAIL):
            url = m.get("iptv_url", "")
            state.mark_failed(url, "Tvheadend scan failed")
            if m not in stale:
                stale.append(m)
            say(f"  ! {m.get('iptv_muxname')}: Tvheadend could not play {url[:80]}; "
                f"will skip it for {cfg.failed_retry_hours:g}h")

    # ---- 4. channels and service mapping
    managed = snap.managed_channels(cfg.marker_tag)
    managed_ids = {c["uuid"] for c in managed}
    our_svc = {s["uuid"] for s in snap.services}
    mux_by_key = {(m.get("iptv_muxname", ""), m.get("iptv_url", "")): m for m in snap.muxes}
    stale_ids = {m["uuid"] for m in stale}
    mapping: dict[str, list[str]] = {}       # channel uuid -> our service uuids
    want_managed: dict[str, Wanted] = {}     # display -> entry needing a channel of its own
    svc_prio: dict[str, int] = {}

    svcs_of: dict[str, list[str]] = {}      # display -> our service uuids, best first
    for w in wanted:
        svcs = []
        for rank, s in enumerate(w.streams):
            m = mux_by_key.get((w.display, s.tvh_url))
            if m and m["uuid"] not in stale_ids:
                for sv in snap.services_of(m["uuid"]):
                    svcs.append(sv["uuid"])
                    svc_prio[sv["uuid"]] = priority_for(cfg, w, rank)
        if not w.streams:  # keep the (disabled) mapping so it comes back by itself
            svcs = [sv["uuid"] for m in snap.muxes if m["uuid"] in keep_disabled
                    and m.get("iptv_muxname") == w.display for sv in snap.services_of(m["uuid"])]
        svcs_of[w.display] = svcs
        targets = existing_targets(cfg, snap, w, managed_ids)
        if targets is None and w.streams and iu.norm(w.display) not in cfg.channel_map:
            near = similar_channels(snap, w, managed_ids)
            if near:
                say(f"  ? {w.display}: no channel of yours has this name, so it gets its own channel."
                    f"\n      Similar: {', '.join(near)}"
                    f"\n      To use one as failover instead, add to [channel_map]:  "
                    f"{w.display} = {near[0].split(' (disabled)')[0].strip(chr(39))}")
        if targets is None:
            if cfg.create_channels and (w.streams or any(c.get("name") == w.display for c in managed)):
                want_managed[w.display] = w
            continue
        if targets and w.streams:
            names = ", ".join(repr(t.get("name")) for t in targets)
            say(f"  = {w.display}: failover for your channel(s) {names}")
        for t in targets:
            lst = mapping.setdefault(t["uuid"], [])
            lst += [x for x in svcs if x not in lst]

    # IPTV-only channels of our own: create / enable / disable / delete
    marker_uuid = snap.tag_uuid(cfg.marker_tag)
    extra_uuid = snap.tag_uuid(cfg.extra_tag) if cfg.extra_tag else None
    if want_managed:
        marker_uuid = ensure_tag(tvh, snap, ch, cfg.marker_tag, internal=True) or marker_uuid
        if cfg.extra_tag:
            extra_uuid = ensure_tag(tvh, snap, ch, cfg.extra_tag, internal=False) or extra_uuid
    for c in managed:
        w = want_managed.get(c.get("name", ""))
        if not w:
            ch("-", f"channel {c.get('name')!r} (no longer wanted as an IPTV-only channel)",
               "delete_channel", lambda c=c: tvh.delete([c["uuid"]]), uuid=c["uuid"], before=c)
            continue
        mapping[c["uuid"]] = svcs_of[w.display]
        enable = bool(w.streams)
        if c.get("enabled", True) != enable:
            ch("~", f"channel {c.get('name')!r}: {'enable' if enable else 'disable (no working stream)'}",
               "toggle_channel", lambda c=c, e=enable: tvh.save({"uuid": c["uuid"], "enabled": e}),
               uuid=c["uuid"], enabled=enable)
    have = {c.get("name") for c in managed}
    for display, w in want_managed.items():
        if display in have or not w.streams:
            continue
        svcs = svcs_of[display]
        if apply and not svcs:
            say(f"  … {display}: waiting for Tvheadend to scan its stream before creating the channel")
            continue
        conf = {"name": display, "enabled": True, "services": svcs, "epgauto": True,
                "tags": [t for t in (marker_uuid, extra_uuid) if t],
                "icon": w.attrs.get("tvg-logo", "")}
        if cfg.channel_number_start > 0:
            conf["number"] = str(cfg.channel_number_start + w.index)
        ch("+", f"channel {display!r}" + (f" #{conf['number']}" if "number" in conf else "") + " (IPTV only)",
           "create_channel", lambda conf=conf: tvh.call("channel/create", conf=conf), conf=conf)

    # service priorities
    for sv in snap.services:
        p = svc_prio.get(sv["uuid"])
        if p is not None and sv.get("priority", 0) != p:
            ch("~", f"service {sv.get('svcname', sv['uuid'])!r} priority {sv.get('priority', 0)} -> {p}",
               "service_priority", lambda sv=sv, p=p: tvh.save({"uuid": sv["uuid"], "priority": p}),
               uuid=sv["uuid"], before=sv.get("priority", 0), after=p)

    # add / remove our services on every channel (your channels keep all their own services)
    for c in snap.channels:
        if c["uuid"] in managed_ids and c.get("name") not in want_managed:
            continue  # being deleted
        current = list(c.get("services") or [])
        cur_ours = [s for s in current if s in our_svc]
        new_ours = [s for s in mapping.get(c["uuid"], []) if s not in stale_ids]
        if set(cur_ours) == set(new_ours):
            continue
        new = [s for s in current if s not in our_svc] + new_ours
        added, removed = len(set(new_ours) - set(cur_ours)), len(set(cur_ours) - set(new_ours))
        ch("~", f"channel {c.get('name')!r}: " + ", ".join(
            x for x in (f"+{added} IPTV service(s)" if added else "",
                        f"-{removed} IPTV service(s)" if removed else "") if x),
           "channel_services", lambda c=c, new=new: tvh.save({"uuid": c["uuid"], "services": new}),
           uuid=c["uuid"], name=c.get("name"), before=current, after=new)

    # ---- 5. remove muxes nobody wants any more
    for m in stale:
        ch("-", f"mux {m.get('iptv_muxname')}: {m.get('iptv_url', '')[:90]}", "delete_mux",
           lambda m=m: tvh.delete([m["uuid"]]), uuid=m["uuid"], before=m)

    # ---- summary
    ok = sum(1 for w in wanted if w.streams)
    say(f"\n{ok}/{len(wanted)} channels have a working IPTV stream.")
    missing = [w.display for w in wanted if not w.streams]
    if missing:
        say("No stream: " + ", ".join(missing))
    if not ch.count:
        say("Tvheadend is already up to date.")
    elif not apply:
        say(f"\nDry run: {ch.count} change(s) listed, nothing was changed. Add --apply to make them.")
    else:
        say(f"\nDone: {ch.count} change(s). Log: {os.path.join(state.dir, 'changes.log')}")
    state.save()
    return 0


# ============================================================ disable / enable

def run_toggle(cfg: Config, tvh: Tvh, state: State, apply: bool, enable: bool) -> int:
    snap = Snapshot.fetch(tvh, cfg)
    if not snap.network:
        say(f"No network called {cfg.network_name!r}: nothing to {'enable' if enable else 'disable'}.")
        return 0
    if apply:
        say(f"Backup: {state.backup(snap, cfg.keep_backups)}")
    ch = Changer(tvh, state, apply)
    verb = "enable" if enable else "disable"
    say(f"\n{'Enabling' if enable else 'Disabling'} everything tvh_sync manages:")
    net = snap.network
    if net.get("enabled", True) != enable:
        ch("~", f"network {cfg.network_name!r}: {verb}", f"{verb}_network",
           lambda: tvh.save({"uuid": net["uuid"], "enabled": enable}), uuid=net["uuid"])
    target = MM_ENABLE if enable else MM_DISABLE
    muxes = [m for m in snap.muxes if m.get("enabled", 1) != target]
    if muxes:
        ch("~", f"{len(muxes)} mux(es): {verb}", f"{verb}_muxes",
           lambda: tvh.save({"uuid": [m["uuid"] for m in muxes], "enabled": target}),
           uuids=[m["uuid"] for m in muxes])
    for c in snap.managed_channels(cfg.marker_tag):
        if c.get("enabled", True) != enable:
            ch("~", f"channel {c.get('name')!r}: {verb}", f"{verb}_channel",
               lambda c=c: tvh.save({"uuid": c["uuid"], "enabled": enable}), uuid=c["uuid"])
    if not ch.count:
        say(f"  already {verb}d")
    elif not apply:
        say("\nDry run: nothing changed. Add --apply.")
    elif not enable:
        say("\nIPTV streams are off. Your DVB channels are untouched. Scheduled runs will do "
            "nothing until you run --enable --apply.")
    else:
        say("\nIPTV streams are back on. Run tvh_sync.py --apply to refresh them.")
    return 0


# ================================================================== uninstall

def run_uninstall(cfg: Config, tvh: Tvh, state: State, apply: bool, yes: bool) -> int:
    snap = Snapshot.fetch(tvh, cfg)
    managed = snap.managed_channels(cfg.marker_tag)
    our_svc = {s["uuid"] for s in snap.services}
    managed_ids = {c["uuid"] for c in managed}
    our_tags = [t for t in snap.tags if t.get("comment") == MANAGED_COMMENT]

    say("Remove everything tvh_sync.py created:")
    ch = Changer(tvh, state, apply)
    if apply:
        if sys.stdin.isatty() and not yes:
            ans = input(f"This removes the network {cfg.network_name!r}, its {len(snap.muxes)} mux(es), "
                        f"{len(managed)} IPTV-only channel(s) and the IPTV services on your channels. "
                        "Type 'yes' to continue: ")
            if ans.strip().lower() != "yes":
                say("Cancelled.")
                return 1
        say(f"Backup: {state.backup(snap, cfg.keep_backups)}")

    for c in snap.channels:
        if c["uuid"] in managed_ids:
            continue
        current = list(c.get("services") or [])
        new = [s for s in current if s not in our_svc]
        if new != current:
            ch("~", f"channel {c.get('name')!r}: remove {len(current) - len(new)} IPTV service(s)",
               "channel_services", lambda c=c, new=new: tvh.save({"uuid": c["uuid"], "services": new}),
               uuid=c["uuid"], name=c.get("name"), before=current, after=new)
    for c in managed:
        ch("-", f"channel {c.get('name')!r}", "delete_channel",
           lambda c=c: tvh.delete([c["uuid"]]), uuid=c["uuid"], before=c)
    if snap.network:
        ch("-", f"IPTV network {cfg.network_name!r} with {len(snap.muxes)} mux(es) and "
           f"{len(snap.services)} service(s)", "delete_network",
           lambda: tvh.delete([snap.network["uuid"]]), uuid=snap.network["uuid"])
    for t in our_tags:
        ch("-", f"channel tag {t.get('name')!r}", "delete_tag",
           lambda t=t: tvh.delete([t["uuid"]]), uuid=t["uuid"], name=t.get("name"))

    if not ch.count:
        say("  nothing to remove; Tvheadend has nothing from tvh_sync.py")
    elif not apply:
        say("\nDry run: nothing changed. Add --apply to remove these.")
    else:
        left = Snapshot.fetch(tvh, cfg)
        if left.network or left.managed_channels(cfg.marker_tag):
            say("\n! Some items are still there; check the Tvheadend web UI.")
        else:
            say(f"\nUninstalled. Local state and backups are still in {state.dir}/ (safe to delete).")
    return 0


# ===================================================================== status

def run_list_channels(cfg: Config, tvh: Tvh) -> int:
    snap = Snapshot.fetch(tvh, cfg)
    managed_ids = {c["uuid"] for c in snap.managed_channels(cfg.marker_tag)}
    our_svc = {s["uuid"] for s in snap.services}
    say(f"{'Number':>7}  {'On':<3} {'Name':<32} {'Match key':<24} Notes")
    def num(c):
        try:
            return float(c.get("number") or 0)
        except (TypeError, ValueError):
            return 0.0
    for c in sorted(snap.channels, key=lambda c: (num(c), c.get("name", ""))):
        notes = []
        if c["uuid"] in managed_ids:
            notes.append("created by tvh_sync")
        ours = sum(1 for s in c.get("services") or [] if s in our_svc)
        if ours:
            notes.append(f"{ours} IPTV service(s)")
        say(f"{str(c.get('number') or ''):>7}  {'yes' if c.get('enabled', True) else 'no':<3} "
            f"{c.get('name', ''):<32} {' / '.join(sorted(channel_keys(c.get('name', '')))):<24} "
            f"{', '.join(notes)}")
    say(f"\n{len(snap.channels)} channels. A desired entry matches a channel when one of its names "
        "has the same match key.")
    return 0


def run_status(cfg: Config, tvh: Tvh) -> int:
    info = tvh.call("serverinfo")
    say(f"Tvheadend {info.get('sw_version', '?')} at {cfg.url} (API v{info.get('api_version', '?')})")
    snap = Snapshot.fetch(tvh, cfg)
    if not snap.network:
        say(f"Network {cfg.network_name!r}: not created yet. Run tvh_sync.py to see the plan.")
        return 0
    say(f"Network {cfg.network_name!r}: {'enabled' if snap.network.get('enabled', True) else 'DISABLED'}, "
        f"{len(snap.muxes)} mux(es), {len(snap.services)} service(s)")
    our_svc = {s["uuid"]: s for s in snap.services}
    managed_ids = {c["uuid"] for c in snap.managed_channels(cfg.marker_tag)}
    result = {0: "not scanned", SCAN_OK: "ok", SCAN_FAIL: "FAILED", SCAN_PARTIAL: "partial"}
    say("\nMuxes:")
    for m in sorted(snap.muxes, key=lambda m: m.get("iptv_muxname", "")):
        onoff = "on " if m.get("enabled", 1) == MM_ENABLE else "off"
        say(f"  {onoff} {m.get('iptv_muxname', '?'):<24} scan {result.get(m.get('scan_result', 0), '?'):<11} "
            f"{m.get('iptv_url', '')[:70]}")
    say("\nChannels using its streams:")
    for c in sorted(snap.channels, key=lambda c: c.get("name", "")):
        ours = [s for s in c.get("services") or [] if s in our_svc]
        if ours or c["uuid"] in managed_ids:
            kind = "IPTV-only, " if c["uuid"] in managed_ids else f"{len(c.get('services') or []) - len(ours)} own + "
            prios = ", ".join(str(our_svc[s].get("priority", 0)) for s in ours)
            say(f"  {c.get('name', '?'):<28} {kind}{len(ours)} IPTV (priority {prios or '-'})")
    return 0


# ======================================================================= main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.environ.get("TVH_SYNC_CONFIG", "tvh_sync.conf"),
                    help="settings file (default: tvh_sync.conf, or $TVH_SYNC_CONFIG)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show what tvh_sync manages in Tvheadend")
    mode.add_argument("--list-channels", action="store_true",
                      help="list all your Tvheadend channels with the key used for name matching")
    mode.add_argument("--disable", action="store_true", help="switch off every IPTV stream it created")
    mode.add_argument("--enable", action="store_true", help="switch them back on after --disable")
    mode.add_argument("--uninstall", action="store_true", help="remove everything it created")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before --uninstall --apply")
    ap.add_argument("--interval", type=float, metavar="MINUTES",
                    help="keep running, syncing every MINUTES (for Docker); implies a normal sync")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as ex:
        print(f"Config error: {ex}", file=sys.stderr)
        return 2
    tvh = Tvh(cfg.url, cfg.username, cfg.password, cfg.http_timeout)
    state = State(cfg)

    def once() -> int:
        if args.list_channels:
            return run_list_channels(cfg, tvh)
        if args.status:
            return run_status(cfg, tvh)
        if args.disable or args.enable:
            return run_toggle(cfg, tvh, state, args.apply, enable=args.enable)
        if args.uninstall:
            return run_uninstall(cfg, tvh, state, args.apply, args.yes)
        return run_sync(cfg, tvh, state, args.apply)

    if not args.interval:
        try:
            return once()
        except TvhError as ex:
            print(f"Tvheadend error: {ex}", file=sys.stderr)
            return 1
    if args.status or args.list_channels or args.disable or args.enable or args.uninstall:
        print("--interval only works with a normal sync", file=sys.stderr)
        return 2
    while True:
        say(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")
        try:
            once()
        except TvhError as ex:
            print(f"Tvheadend error: {ex}", file=sys.stderr)
        except Exception as ex:  # keep the loop alive
            print(f"Error: {type(ex).__name__}: {ex}", file=sys.stderr)
        time.sleep(max(1.0, args.interval) * 60)


if __name__ == "__main__":
    sys.exit(main())
