"""jfstress for Locust: web UI to start Jellyfin playback load tests, Prometheus metrics
for Grafana.

    locust -f locustfile.py --class-picker --host https://jellyfin-public-test.amer.dev

Open http://localhost:8089, fill in the jfstress fields, then:
  * pick shape "StepRamp" to add one stream per step until a viewer would notice
    (stall, slow start, transcode below real time) - the result is the max stream count;
  * or pick no shape and set "Number of users" (= concurrent streams) yourself.

Each Locust user is one viewer: its own Jellyfin device/session, playing one item at a
time with the jfcore player model. Prometheus metrics are served on --prom-port (9646).
"""
from __future__ import annotations

import itertools
import logging
import random
import time
import uuid

import gevent
from locust import HttpUser, LoadTestShape, constant, events, task
from locust.exception import StopUser
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from jfcore import (CLIENT, TICKS, HealthConfig, Player, auth_header, describe_video, first_source,
                    item_label, item_matches, items_query, method_for, parse_filter, parse_m3u8,
                    parse_mix, playback_info_body, seek_index, transcoding_url, video_stream)

log = logging.getLogger("jfstress")

SOURCES = ["any", "4k,hdr", "4k", "1080p", "1080p,h264", "1080p,hevc", "hevc", "h264", "sdr"]

# --------------------------------------------------------------------------- metrics

L = ["mode"]
M_STREAMS = Gauge("jfstress_streams", "Viewers by playback mode and player state", ["mode", "state"])
M_TARGET = Gauge("jfstress_target_streams", "Streams Locust is asked to run")
M_STALLS = Counter("jfstress_stalls_total", "Times a viewer's buffer ran dry", L)
M_STALL_S = Counter("jfstress_stall_seconds_total", "Seconds viewers spent stalled", L)
M_STARTUP = Histogram("jfstress_startup_seconds", "Request to first frame", L,
                      buckets=(0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60))
M_FETCH = Histogram("jfstress_segment_fetch_seconds", "HLS segment download time", L,
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 30))
M_FETCH_RATIO = Histogram("jfstress_segment_fetch_ratio",
                          "Segment download time / segment duration (>1 = slower than real time)", L,
                          buckets=(0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1, 1.25, 1.5, 2, 4))
M_BYTES = Counter("jfstress_bytes_total", "Bytes received", L)
M_ERRORS = Counter("jfstress_errors_total", "Failed viewer sessions", ["mode", "stage"])
M_BUF = Gauge("jfstress_stream_buffer_seconds", "Media seconds buffered, per viewer", ["stream", "mode"])
M_FILL = Gauge("jfstress_stream_fill_speed", "Recent fill speed (x real time), per viewer", ["stream", "mode"])
M_SRV = Gauge("jfstress_stream_server_speed", "Server transcode fps / source fps, per viewer",
              ["stream", "mode", "hw"])
M_DEGRADED = Gauge("jfstress_degraded_streams", "Viewers that would notice a problem now")
M_RAMP_LEVEL = Gauge("jfstress_ramp_level", "Current StepRamp level (streams)")
M_RAMP_RESULT = Gauge("jfstress_ramp_result_streams", "Last StepRamp result: max streams held (-1 = none yet)")
M_RUN = Gauge("jfstress_run_info", "Current test settings",
              ["run_id", "playback", "mix", "source", "codec", "max_height", "bitrate_mbps", "burn_subs"])
M_RAMP_RESULT.set(-1)


# --------------------------------------------------------------------------- options (web UI)

@events.init_command_line_parser.add_listener
def _options(parser):
    ui = {"include_in_web_ui": True}
    parser.add_argument("--jf-username", default="", help="Jellyfin user", **ui)
    parser.add_argument("--jf-password", default="", help="Jellyfin password", is_secret=True, **ui)
    parser.add_argument("--jf-api-key", default="", help="or: admin API key", is_secret=True, **ui)
    parser.add_argument("--playback", default="transcode", choices=["transcode", "remux", "direct", "mix"],
                        help="transcode = full re-encode, remux = stream copy, direct = static file", **ui)
    parser.add_argument("--mix", default="transcode=3,direct=1",
                        help="used when playback=mix, e.g. transcode=2,remux=1,direct=1", **ui)
    parser.add_argument("--source", default="any", choices=SOURCES,
                        help="which items to play (4k,hdr adds tone mapping)", **ui)
    parser.add_argument("--codec", default="h264", choices=["h264", "hevc", "av1"],
                        help="transcode output codec", **ui)
    parser.add_argument("--max-height", default="1080", choices=["2160", "1080", "720", "480"],
                        help="transcode output max height", **ui)
    parser.add_argument("--bitrate-mbps", type=int, default=8, help="max streaming bitrate", **ui)
    parser.add_argument("--burn-subs", action="store_true", default=False,
                        help="burn in the first subtitle track", **ui)
    parser.add_argument("--library", default="", help="restrict to a library by name (blank = all)", **ui)
    parser.add_argument("--watch-seconds", type=int, default=600,
                        help="media seconds per session before the viewer moves to another item", **ui)
    parser.add_argument("--ramp-step-seconds", type=int, default=45, help="StepRamp: hold each level", **ui)
    parser.add_argument("--ramp-max", type=int, default=20, help="StepRamp: upper bound", **ui)
    parser.add_argument("--min-speed", type=float, default=1.0, help="health: min fill speed while not full")
    parser.add_argument("--grace", type=float, default=15, help="health: s a viewer may be behind")
    parser.add_argument("--max-startup", type=float, default=30, help="health: max startup s")
    parser.add_argument("--max-buffer", type=float, default=30, help="player buffer ceiling, media s")
    parser.add_argument("--prom-port", type=int, default=9646, help="Prometheus metrics port (0 = off)",
                        include_in_web_ui=False)


@events.init.add_listener
def _init(environment, **_):
    port = environment.parsed_options.prom_port if environment.parsed_options else 0
    if port and not isinstance(environment.runner, _worker_runner_type()):
        start_http_server(port)
        log.info("Prometheus metrics on :%d/metrics", port)


def _worker_runner_type():
    from locust.runners import WorkerRunner
    return WorkerRunner


# --------------------------------------------------------------------------- shared run state

class Run:
    """State for the current test run, reset on test_start."""

    def __init__(self):
        self.id = ""
        self.items: list[dict] = []
        self.next_item = itertools.count()
        self.next_viewer = itertools.count(1)
        self.cycle: list[str] = ["transcode"]
        self.viewers: dict[str, "Viewer"] = {}
        self.cfg = HealthConfig()
        self.degraded: list[str] = []
        self.ctl_token: str | None = None
        self.user_id: str | None = None
        self.greenlets: list[gevent.Greenlet] = []
        self.ready = False


RUN = Run()


class Viewer:
    def __init__(self, idx: int, mode: str, device_id: str, cfg: HealthConfig):
        self.idx, self.mode, self.device_id = idx, mode, device_id
        self.player = Player(cfg)
        self.item: dict = {}
        self.src_fps: float | None = None
        self.srv_ratio: float | None = None
        self.srv_hw = ""
        self.player.on_stall_start = self._stall_start
        self.player.on_stall_end = self._stall_end
        self.player.on_startup = self._startup

    def _fire(self, name, ms, exc=None):
        events.request.fire(request_type="PLAYER", name=f"{name} [{self.mode}]", response_time=ms,
                            response_length=0, exception=exc, context={})

    def _stall_start(self):
        M_STALLS.labels(self.mode).inc()
        self._fire("stall", 0, RuntimeError(f"viewer {self.idx} stalled"))

    def _stall_end(self, secs):
        M_STALL_S.labels(self.mode).inc(secs)
        self._fire("stall duration", secs * 1000)

    def _startup(self, secs):
        M_STARTUP.labels(self.mode).observe(secs)
        self._fire("startup", secs * 1000)


def _ctl_headers(token=None):
    return {"Authorization": auth_header(f"{CLIENT}-ctl", f"{CLIENT}-{RUN.id}-ctl", token)}


@events.test_start.add_listener
def _test_start(environment, **_):
    import requests
    o = environment.parsed_options
    host = environment.host.rstrip("/")
    RUN.__init__()
    RUN.id = uuid.uuid4().hex[:8]
    RUN.cfg = HealthConfig(max_buffer=o.max_buffer, min_speed=o.min_speed, grace=o.grace,
                           max_startup=o.max_startup)
    RUN.cycle = parse_mix(o.mix) if o.playback == "mix" else [o.playback]
    M_RUN.clear()
    M_RUN.labels(RUN.id, o.playback, o.mix if o.playback == "mix" else "", o.source, o.codec,
                 o.max_height, str(o.bitrate_mbps), str(bool(o.burn_subs))).set(1)
    M_RAMP_RESULT.set(-1)
    M_RAMP_LEVEL.set(0)
    s = requests.Session()
    try:
        if o.jf_api_key:
            RUN.ctl_token = o.jf_api_key
            users = s.get(f"{host}/Users", headers=_ctl_headers(o.jf_api_key), timeout=30).json()
            pick = [u for u in users if not o.jf_username or u["Name"].lower() == o.jf_username.lower()]
            RUN.user_id = pick[0]["Id"]
        else:
            r = s.post(f"{host}/Users/AuthenticateByName", headers=_ctl_headers(),
                       json={"Username": o.jf_username, "Pw": o.jf_password}, timeout=30)
            r.raise_for_status()
            RUN.ctl_token, RUN.user_id = r.json()["AccessToken"], r.json()["User"]["Id"]
        h = _ctl_headers(RUN.ctl_token)
        parent = None
        if o.library:
            libs = s.get(f"{host}/Users/{RUN.user_id}/Views", headers=h, timeout=30).json()["Items"]
            lib = [l for l in libs if l["Name"].lower() == o.library.lower()]
            if not lib:
                raise RuntimeError(f"library {o.library!r} not found: {[l['Name'] for l in libs]}")
            parent = lib[0]["Id"]
        items = s.get(f"{host}/Items", headers=h, params=items_query(RUN.user_id, "Movie,Episode", parent),
                      timeout=60).json()["Items"]
        filt = parse_filter(o.source)
        RUN.items = [i for i in items if item_matches(i, filt)]
        random.shuffle(RUN.items)
        if not RUN.items:
            raise RuntimeError(f"no items match source={o.source!r} library={o.library!r}")
        info = s.get(f"{host}/System/Info", headers=h, timeout=30).json()
        log.info("run %s: %s v%s, %d candidate items, playback=%s", RUN.id, info.get("ServerName"),
                 info.get("Version"), len(RUN.items), "/".join(sorted(set(RUN.cycle))))
        RUN.ready = True
    except Exception as e:  # noqa: BLE001
        log.error("test start failed: %s", e)
        RUN.ready = False
        if environment.runner:
            gevent.spawn_later(0.5, environment.runner.quit if environment.runner else None)
        return
    RUN.greenlets = [gevent.spawn(_poll_sessions, host), gevent.spawn(_monitor, environment)]


@events.test_stop.add_listener
def _test_stop(environment, **_):
    gevent.killall(RUN.greenlets, block=False)
    RUN.greenlets = []
    for g in (M_STREAMS, M_BUF, M_FILL, M_SRV):
        g.clear()
    M_TARGET.set(0)
    M_DEGRADED.set(0)
    if RUN.ctl_token and not environment.parsed_options.jf_api_key:
        try:
            import requests
            requests.post(f"{environment.host.rstrip('/')}/Sessions/Logout",
                          headers=_ctl_headers(RUN.ctl_token), timeout=10)
        except Exception:
            pass


def _poll_sessions(host: str):
    import requests
    s = requests.Session()
    while True:
        try:
            sess = s.get(f"{host}/Sessions", headers=_ctl_headers(RUN.ctl_token),
                         params={"activeWithinSeconds": 120}, timeout=20).json()
            for x in sess:
                v = RUN.viewers.get(x.get("DeviceId"))
                ti = x.get("TranscodingInfo") if v else None
                if ti and ti.get("Framerate") and v.src_fps:
                    v.srv_ratio = ti["Framerate"] / v.src_fps
                    v.srv_hw = ti.get("HardwareAccelerationType") or "none"
        except Exception as e:  # noqa: BLE001
            log.warning("/Sessions poll failed: %s", e)
        gevent.sleep(3)


def _monitor(environment):
    while True:
        now = time.monotonic()
        counts: dict[tuple[str, str], int] = {}
        for g in (M_BUF, M_FILL, M_SRV):
            g.clear()
        bad = []
        for v in list(RUN.viewers.values()):
            p = v.player
            counts[(v.mode, p.state)] = counts.get((v.mode, p.state), 0) + 1
            if not p.active:
                continue
            stream = f"{v.idx:03d}"
            M_BUF.labels(stream, v.mode).set(round(p.buffer, 1))
            sp = p.speed()
            if sp is not None:
                M_FILL.labels(stream, v.mode).set(round(min(sp, 20), 2))
            if v.srv_ratio is not None:
                M_SRV.labels(stream, v.mode, v.srv_hw).set(round(v.srv_ratio, 2))
            reason = p.degraded(now)
            if reason:
                bad.append(f"#{v.idx} {v.mode} {reason}")
        M_STREAMS.clear()
        for (mode, state), n in counts.items():
            M_STREAMS.labels(mode, state).set(n)
        M_TARGET.set(environment.runner.target_user_count if environment.runner else 0)
        M_DEGRADED.set(len(bad))
        if bad and bad != RUN.degraded:
            log.warning("degraded: %s", ", ".join(bad))
        RUN.degraded = bad
        gevent.sleep(1)


# --------------------------------------------------------------------------- the viewer

class JellyfinViewer(HttpUser):
    """One simulated viewer. Plays an item for --watch-seconds, then starts another."""
    wait_time = constant(1)

    def on_start(self):
        o = self.environment.parsed_options
        if not RUN.ready:
            raise StopUser()
        n = next(RUN.next_viewer)
        self.mode = RUN.cycle[(n - 1) % len(RUN.cycle)]
        self.device_id = f"{CLIENT}-{RUN.id}-{n:03d}"
        self.viewer = Viewer(n, self.mode, self.device_id, RUN.cfg)
        self.token = o.jf_api_key or None
        self._set_auth()
        if not self.token:
            r = self.client.post("/Users/AuthenticateByName", name="login",
                                 json={"Username": o.jf_username, "Pw": o.jf_password})
            r.raise_for_status()
            self.token = r.json()["AccessToken"]
            self._set_auth()
        self.session: tuple | None = None

    def _set_auth(self):
        self.client.headers["Authorization"] = auth_header(
            f"{CLIENT}-{self.viewer.idx:03d}", self.device_id, self.token)

    def on_stop(self):
        self._end_session()
        if not self.environment.parsed_options.jf_api_key and getattr(self, "token", None):
            try:
                self.client.post("/Sessions/Logout", name="logout")
            except Exception:
                pass
        RUN.viewers.pop(getattr(self, "device_id", ""), None)

    @task
    def watch(self):
        o = self.environment.parsed_options
        item = RUN.items[next(RUN.next_item) % len(RUN.items)]
        v = self.viewer = Viewer(self.viewer.idx, self.mode, self.device_id, RUN.cfg)
        v.item = item
        RUN.viewers[self.device_id] = v
        src = first_source(item)
        vs = video_stream(src)
        if vs:
            v.src_fps = vs.get("RealFrameRate") or vs.get("AverageFrameRate")
        runtime = (item.get("RunTimeTicks") or 0) / TICKS
        start = random.uniform(60, max(61, runtime - o.watch_seconds - 120)) if runtime > 600 else 0.0
        stage = "playbackinfo"
        try:
            body = playback_info_body(self.mode, RUN.user_id, src, start, codec=o.codec,
                                      bitrate=o.bitrate_mbps * 1_000_000, max_height=int(o.max_height),
                                      burn_subs=o.burn_subs)
            r = self.client.post(f"/Items/{item['Id']}/PlaybackInfo", name="PlaybackInfo",
                                 params={"userId": RUN.user_id}, json=body)
            r.raise_for_status()
            pi = r.json()
            if pi.get("ErrorCode"):
                raise RuntimeError(f"PlaybackInfo: {pi['ErrorCode']}")
            ms = pi["MediaSources"][0]
            method = method_for(self.mode, ms)
            self.session = (item["Id"], ms.get("Id"), pi.get("PlaySessionId"), method, start)
            v.player.begin(time.monotonic())
            self._report("/Sessions/Playing")
            log.info("viewer %d %s/%s: %s [%s]", v.idx, self.mode, method, item_label(item), describe_video(vs))
            stage = "playback"
            progress = gevent.spawn(self._progress_loop)
            try:
                if method == "DirectPlay":
                    self._play_static(ms)
                else:
                    self._play_hls(transcoding_url(self.mode, ms, {}))
            finally:
                progress.kill(block=False)
            v.player.state = "done"
        except gevent.GreenletExit:
            raise
        except Exception as e:  # noqa: BLE001
            v.player.state = "error"
            M_ERRORS.labels(self.mode, stage).inc()
            log.warning("viewer %d %s error in %s: %s", v.idx, self.mode, stage, e)
            gevent.sleep(5)
        finally:
            self._end_session()

    # ---------------------------------------------------------- helpers
    def _report(self, path, extra=None):
        if not self.session:
            return
        item_id, src_id, ps, method, start = self.session
        body = {"ItemId": item_id, "MediaSourceId": src_id, "PlaySessionId": ps, "PlayMethod": method,
                "PositionTicks": int((start + self.viewer.player.played_s) * TICKS),
                "CanSeek": True, "IsPaused": False, "IsMuted": True}
        body.update(extra or {})
        try:
            self.client.post(path, json=body, name=path)
        except Exception:
            pass

    def _progress_loop(self):
        while True:
            gevent.sleep(10)
            self._report("/Sessions/Playing/Progress", {"EventName": "timeupdate"})

    def _end_session(self):
        if not self.session:
            return
        _, _, ps, _, _ = self.session
        self._report("/Sessions/Playing/Stopped")
        try:
            self.client.delete("/Videos/ActiveEncodings", name="stop encoding",
                               params={"deviceId": self.device_id, "playSessionId": ps})
        except Exception:
            pass
        self.session = None

    def _wait_for_room(self) -> bool:
        p = self.viewer.player
        while True:
            if p.played_s >= self.environment.parsed_options.watch_seconds:
                return False
            if p.room(time.monotonic()):
                return True
            gevent.sleep(0.25)

    def _play_hls(self, url):
        v, p = self.viewer, self.viewer.player
        r = self.client.get(url, name="master.m3u8")
        r.raise_for_status()
        variants, init, segs = parse_m3u8(r.text, r.url)
        if variants:
            r = self.client.get(variants[0], name="main.m3u8")
            r.raise_for_status()
            _, init, segs = parse_m3u8(r.text, r.url)
        if not segs:
            raise RuntimeError("empty HLS playlist")
        if init:
            p.bytes += len(self.client.get(init, name="init segment").content)
        for uri, dur in segs[seek_index(segs, self.session[4]):]:
            if not self._wait_for_room():
                break
            t0 = time.monotonic()
            r = self.client.get(uri, name=f"segment [{v.mode}]")
            r.raise_for_status()
            now = time.monotonic()
            n = len(r.content)
            p.bytes += n
            M_BYTES.labels(v.mode).inc(n)
            M_FETCH.labels(v.mode).observe(now - t0)
            M_FETCH_RATIO.labels(v.mode).observe((now - t0) / dur)
            p.got_media(now, dur, now - t0)

    def _play_static(self, ms):
        v, p = self.viewer, self.viewer.player
        bitrate = ms.get("Bitrate") or 0
        if not bitrate:
            raise RuntimeError("source has no bitrate; cannot pace direct play")
        bps = bitrate / 8
        start = self.session[4]
        headers = {}
        if start and ms.get("Size"):
            headers["Range"] = f"bytes={int(start * bps)}-"
        params = {"static": "true", "mediaSourceId": ms["Id"], "playSessionId": self.session[2],
                  "deviceId": self.device_id}
        with self.client.get(f"/Videos/{v.item['Id']}/stream", params=params, headers=headers,
                             name="direct stream", stream=True) as r:
            r.raise_for_status()
            t_last, pending = time.monotonic(), 0
            for chunk in r.iter_content(256 * 1024):
                pending += len(chunk)
                p.bytes += len(chunk)
                M_BYTES.labels(v.mode).inc(len(chunk))
                if pending >= bps:
                    now = time.monotonic()
                    M_FETCH_RATIO.labels(v.mode).observe((now - t_last) / (pending / bps))
                    p.got_media(now, pending / bps, now - t_last)
                    pending = 0
                    if not self._wait_for_room():
                        break
                    t_last = time.monotonic()


# --------------------------------------------------------------------------- ramp shape

class StepRamp(LoadTestShape):
    """Add one stream every --ramp-step-seconds until a viewer degrades or --ramp-max holds.
    The result (last level that held a full step) is logged and exported as
    jfstress_ramp_result_streams."""

    def __init__(self):
        super().__init__()
        self.level = 0
        self.level_t0 = 0.0
        self.held = 0
        self.done = False

    def reset_time(self):
        super().reset_time()
        self.level, self.level_t0, self.held, self.done = 0, 0.0, 0, False

    def _finish(self, result: int, why: str):
        self.done = True
        M_RAMP_RESULT.set(result)
        log.warning("StepRamp result: %d stream(s) held without degradation (%s)", result, why)
        return None

    def tick(self):
        if self.done:
            return None
        o = self.runner.environment.parsed_options
        t = self.get_run_time()
        if RUN.degraded and self.level:
            return self._finish(self.held, f"degraded at {self.level}: {', '.join(RUN.degraded)}")
        step = o.ramp_step_seconds
        undecided = any(v.player.active and v.player.undecided for v in RUN.viewers.values())
        if self.level == 0 or (t - self.level_t0 >= step and not undecided) or t - self.level_t0 >= 3 * step:
            if self.level:
                self.held = self.level
                log.info("StepRamp: %d stream(s) held for %ds", self.level, o.ramp_step_seconds)
            if self.level >= o.ramp_max:
                return self._finish(self.held, f"reached ramp-max {o.ramp_max}")
            self.level += 1
            self.level_t0 = t
            M_RAMP_LEVEL.set(self.level)
        return (self.level, 1)
