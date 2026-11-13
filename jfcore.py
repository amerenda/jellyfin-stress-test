"""Shared, I/O-free pieces of jfstress: device profiles, HLS parsing, item selection and
the player buffer model. Used by both the asyncio CLI (jfstress.py) and the Locust
runner (locustfile.py)."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

VERSION = "0.2.0"
CLIENT = "jfstress"
TICKS = 10_000_000
MODES = ("direct", "remux", "transcode")
AUDIO_COPY = "aac,mp3,ac3,eac3,opus,flac,dts,truehd,alac,vorbis"
SOURCE_FILTERS = ("4k", "1080p", "hdr", "sdr", "h264", "hevc", "av1", "vp9", "mpeg2video", "vc1")


# --------------------------------------------------------------------------- auth

def auth_header(device: str, device_id: str, token: str | None = None) -> str:
    h = (f'MediaBrowser Client="{CLIENT}", Device="{device}", '
         f'DeviceId="{device_id}", Version="{VERSION}"')
    if token:
        h += f', Token="{token}"'
    return h


# --------------------------------------------------------------------------- profiles

def device_profile(mode: str, codec: str, max_bitrate: int, max_height: int | None) -> dict:
    """Device profile that steers Jellyfin's StreamBuilder to one playback method."""
    if mode == "remux":
        tp_video, tp_audio = "h264,hevc,av1,vp9", AUDIO_COPY
    else:
        tp_video, tp_audio = codec, "aac"
    profile: dict = {
        "Name": f"{CLIENT}-{mode}",
        "MaxStreamingBitrate": max_bitrate,
        "MaxStaticBitrate": max_bitrate,
        "MusicStreamingTranscodingBitrate": 192000,
        "DirectPlayProfiles": [{"Type": "Video"}, {"Type": "Audio"}] if mode == "direct" else [],
        "TranscodingProfiles": [{
            "Container": "ts",
            "Type": "Video",
            "VideoCodec": tp_video,
            "AudioCodec": tp_audio,
            "Protocol": "hls",
            "Context": "Streaming",
            "MaxAudioChannels": "2",
            "MinSegments": 1,
            "BreakOnNonKeyFrames": True,
        }],
        "ContainerProfiles": [],
        "CodecProfiles": [],
        "SubtitleProfiles": [
            {"Format": f, "Method": "External"} for f in ("vtt", "srt", "ass", "ssa")
        ] + [{"Format": f, "Method": "Encode"} for f in ("pgssub", "dvdsub", "dvbsub")],
    }
    if mode == "transcode" and max_height:
        profile["CodecProfiles"].append({
            "Type": "Video",
            "Conditions": [{"Condition": "LessThanEqual", "Property": "Height",
                            "Value": str(max_height), "IsRequired": False}],
        })
    return profile


def playback_info_body(mode: str, user_id: str, source: dict, start_s: float, *, codec: str,
                       bitrate: int, max_height: int | None, burn_subs: bool) -> dict:
    body = {
        "UserId": user_id,
        "DeviceProfile": device_profile(mode, codec, bitrate, max_height),
        "MaxStreamingBitrate": bitrate,
        "StartTimeTicks": int(start_s * TICKS),
        "EnableDirectPlay": mode == "direct",
        "EnableDirectStream": mode == "direct",
        "EnableTranscoding": mode != "direct",
        "AllowVideoStreamCopy": mode == "remux",
        "AllowAudioStreamCopy": mode == "remux",
        "AutoOpenLiveStream": True,
        "MediaSourceId": source.get("Id"),
    }
    if burn_subs and mode == "transcode":
        sub = next((m for m in source.get("MediaStreams", []) if m.get("Type") == "Subtitle"), None)
        if sub:
            body["SubtitleStreamIndex"] = sub["Index"]
            body["DeviceProfile"]["SubtitleProfiles"] = [
                {"Format": f, "Method": "Encode"} for f in
                ("*", "srt", "subrip", "ass", "ssa", "vtt", "pgssub", "dvdsub", "dvbsub")]
    return body


def method_for(mode: str, media_source: dict) -> str:
    """The playback method the server granted, as Jellyfin's PlayMethod names it."""
    if (mode == "direct" and media_source.get("SupportsDirectPlay")) or not media_source.get("TranscodingUrl"):
        return "DirectPlay"
    return {"remux": "DirectStream", "transcode": "Transcode"}.get(mode, "Transcode")


def transcoding_url(mode: str, media_source: dict, overrides: dict[str, str]) -> str:
    url = media_source["TranscodingUrl"]
    if mode == "transcode":
        url = set_params(url, {"AllowVideoStreamCopy": "false", "AllowAudioStreamCopy": "false"})
    return set_params(url, overrides)


def parse_mix(spec: str) -> list[str]:
    out: list[str] = []
    for part in spec.split(","):
        mode, _, n = part.partition("=")
        mode = mode.strip()
        if not mode:
            continue
        if mode not in MODES:
            raise ValueError(f"bad mode in mix: {mode!r} (use {', '.join(MODES)})")
        out += [mode] * int(n or 1)
    if not out:
        raise ValueError("empty mix")
    return out


# --------------------------------------------------------------------------- items

ITEM_FIELDS = "MediaSources,MediaStreams,RunTimeTicks"


def video_stream(item_or_source: dict) -> dict | None:
    for s in item_or_source.get("MediaStreams") or []:
        if s.get("Type") == "Video":
            return s
    return None


def first_source(item: dict) -> dict:
    return (item.get("MediaSources") or [{}])[0]


def describe_video(vs: dict | None) -> str:
    if not vs:
        return "?"
    h = vs.get("Height") or 0
    res = "4K" if h >= 1600 else f"{h}p"
    rng = vs.get("VideoRangeType") or vs.get("VideoRange") or ""
    rng = "" if rng in ("SDR", "") else f" {rng}"
    return f"{res} {vs.get('Codec', '?')}{rng}"


def item_label(item: dict) -> str:
    if item.get("SeriesName"):
        return f"{item['SeriesName']} {item.get('ParentIndexNumber', 0)}x{item.get('IndexNumber', 0)}"
    return item.get("Name", "?")


def parse_filter(spec: str | None) -> set[str]:
    filt = {f.strip().lower() for f in (spec or "").split(",") if f.strip() and f.strip().lower() != "any"}
    bad = filt - set(SOURCE_FILTERS)
    if bad:
        raise ValueError(f"unknown source filter(s) {sorted(bad)}; use {', '.join(SOURCE_FILTERS)}")
    return filt


def item_matches(item: dict, filt: set[str], min_runtime_s: float = 600) -> bool:
    if (item.get("RunTimeTicks") or 0) < min_runtime_s * TICKS:
        return False
    vs = video_stream(first_source(item))
    if vs is None:
        return False
    h = vs.get("Height") or 0
    codec = (vs.get("Codec") or "").lower()
    hdr = (vs.get("VideoRange") or "SDR") != "SDR"
    for f in filt:
        if f == "4k" and h < 1600: return False
        if f == "1080p" and not (900 <= h < 1600): return False
        if f == "hdr" and not hdr: return False
        if f == "sdr" and hdr: return False
        if f not in ("4k", "1080p", "hdr", "sdr") and codec != f: return False
    return True


def items_query(user_id: str, types: str, parent_id: str | None = None) -> dict:
    q = {"userId": user_id, "Recursive": "true", "IncludeItemTypes": types, "MediaTypes": "Video",
         "Fields": ITEM_FIELDS, "SortBy": "Random", "Limit": "400", "IsMissing": "false"}
    if parent_id:
        q["ParentId"] = parent_id
    return q


# --------------------------------------------------------------------------- HLS

def set_params(url: str, overrides: dict[str, str]) -> str:
    if not overrides:
        return url
    p = urlsplit(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    lower = {k.lower(): k for k in q}
    for k, v in overrides.items():
        q[lower.get(k.lower(), k)] = v
    return urlunsplit(p._replace(query=urlencode(q)))


def parse_m3u8(text: str, base: str):
    """Return (variants, init_uri, segments[(uri, dur)])."""
    variants, segs, init = [], [], None
    dur = None
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            nxt = next((l for l in lines[i + 1:] if l and not l.startswith("#")), None)
            if nxt:
                variants.append(urljoin(base, nxt))
        elif line.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init = urljoin(base, m.group(1))
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((urljoin(base, line), dur))
            dur = None
    return variants, init, segs


def seek_index(segs: list[tuple[str, float]], start_s: float) -> int:
    """Segment index to start from. Jellyfin's playlist covers the whole item, so a start
    offset is a seek to the segment that contains it (Jellyfin restarts ffmpeg there)."""
    if not start_s or sum(d for _, d in segs) <= start_s + 60:
        return 0
    acc = 0.0
    for i, (_, d) in enumerate(segs):
        if acc >= start_s:
            return i
        acc += d
    return 0


# --------------------------------------------------------------------------- player model

@dataclass
class HealthConfig:
    startup_buffer: float = 6.0   # media s buffered before playback starts
    rebuffer: float = 4.0         # media s needed to resume after a stall
    max_buffer: float = 30.0      # player buffer ceiling
    min_speed: float = 1.0        # fill speed (x real time) below which a non-full stream is behind
    grace: float = 15.0           # s a stream may be behind before it counts as degraded
    max_startup: float = 30.0     # startup slower than this counts as degraded


@dataclass
class Player:
    """Buffer model of one viewer. Times are monotonic seconds supplied by the caller."""
    cfg: HealthConfig
    state: str = "init"           # init, starting, playing, stalled, done, error
    t_start: float = 0.0
    startup_s: float | None = None
    stalls: int = 0
    stall_s: float = 0.0
    downloaded_s: float = 0.0
    played_s: float = 0.0
    bytes: int = 0
    seg_speeds: list[float] = field(default_factory=list)
    seg_fetch_max: float = 0.0
    behind_since: float | None = None
    _last: float = 0.0
    _stall_t0: float = 0.0
    on_stall_start: object = None   # callable() hooks for metrics
    on_stall_end: object = None     # callable(seconds)
    on_startup: object = None       # callable(seconds)

    def begin(self, now: float) -> None:
        self.state, self.t_start, self._last = "starting", now, now

    @property
    def buffer(self) -> float:
        return self.downloaded_s - self.played_s

    def tick(self, now: float) -> None:
        if self.state == "playing":
            el = now - self._last
            if self.played_s + el >= self.downloaded_s:
                self._stall_t0 = self._last + (self.downloaded_s - self.played_s)
                self.played_s = self.downloaded_s
                self.state = "stalled"
                self.stalls += 1
                if self.on_stall_start:
                    self.on_stall_start()
            else:
                self.played_s += el
        self._last = now

    def got_media(self, now: float, secs: float, fetch_s: float | None = None) -> None:
        self.tick(now)
        self.downloaded_s += secs
        if fetch_s is not None:
            self.seg_speeds.append(secs / max(fetch_s, 1e-3))
            if len(self.seg_speeds) > 3:
                self.seg_fetch_max = max(self.seg_fetch_max, fetch_s)
        if self.state == "starting" and self.buffer >= self.cfg.startup_buffer:
            self.state = "playing"
            self.startup_s = now - self.t_start
            if self.on_startup:
                self.on_startup(self.startup_s)
        elif self.state == "stalled" and self.buffer >= self.cfg.rebuffer:
            d = now - self._stall_t0
            self.stall_s += d
            self.state = "playing"
            if self.on_stall_end:
                self.on_stall_end(d)
        self._last = now

    def room(self, now: float) -> bool:
        """True if the player wants more data now."""
        self.tick(now)
        return self.state != "playing" or self.buffer < self.cfg.max_buffer

    def speed(self, n: int = 5) -> float | None:
        s = self.seg_speeds[3:][-n:]  # skip ffmpeg spin-up segments
        return statistics.median(s) if s else None

    @property
    def active(self) -> bool:
        return self.state in ("starting", "playing", "stalled")

    @property
    def undecided(self) -> bool:
        """Too early to call this viewer healthy: still starting, too few fill samples
        to judge, or currently behind but inside the grace period. A ramp level only
        counts as held once no active viewer is undecided."""
        if self.state == "starting":
            return True
        if self.state == "playing":
            return len(self.seg_speeds) < 5 or self.behind_since is not None
        return False

    def degraded(self, now: float) -> str | None:
        """Reason a viewer would notice a problem right now, or None."""
        if self.state == "error":
            return "error"
        if self.state == "stalled":
            return "stalled"
        if self.state == "starting" and now - self.t_start > self.cfg.max_startup:
            return f"startup>{self.cfg.max_startup:.0f}s"
        sp = self.speed()
        if (self.state == "playing" and sp is not None and sp < self.cfg.min_speed
                and self.buffer < self.cfg.max_buffer * 0.9):
            self.behind_since = self.behind_since or now
            if now - self.behind_since >= self.cfg.grace:
                return f"fill {sp:.2f}x"
        else:
            self.behind_since = None
        return None
