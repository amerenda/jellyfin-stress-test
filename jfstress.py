#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "rich>=13"]
# ///
"""jfstress: end-to-end concurrent playback load test for a Jellyfin server (CLI).

Each simulated stream is a separate Jellyfin client (its own DeviceId and session).
It asks the server for PlaybackInfo with a device profile that forces the playback
method you pick (direct / remux / transcode), then plays the stream the way a
player does: it downloads HLS segments (or the static file) at real-time pace,
keeps a bounded buffer, and records startup time, stalls and fill speed.
The server's own view (/Sessions TranscodingInfo: fps, hw accel, codecs) is
polled alongside. For a web UI + Grafana, see locustfile.py. For running in a
Kubernetes pod, --jsonl swaps the terminal table for JSON lines on stdout
(event, status, report) and --stop-file ends the run gracefully.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import statistics
import time
import uuid
from dataclasses import dataclass, field

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from jfcore import (CLIENT, SOURCE_FILTERS, TICKS, HealthConfig, Player, auth_header, describe_video,
                    first_source, item_label, item_matches, items_query, method_for,
                    parse_filter, parse_m3u8, parse_mix, playback_info_body, seek_index, subtitle_codecs,
                    transcoding_url, video_stream)

console = Console(stderr=True)


def emit(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":"), default=str), flush=True)


class Jellyfin:
    def __init__(self, base: str, http: httpx.AsyncClient, device_id: str, device: str,
                 token: str | None = None):
        self.base = base.rstrip("/")
        self.http = http
        self.device_id = device_id
        self.device = device
        self.token = token
        self.user_id: str | None = None

    def auth(self) -> str:
        return auth_header(self.device, self.device_id, self.token)

    async def req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        headers["Authorization"] = self.auth()
        url = path if path.startswith("http") else self.base + path
        r = await self.http.request(method, url, headers=headers, **kw)
        r.raise_for_status()
        return r

    async def login(self, username: str, password: str) -> None:
        j = (await self.req("POST", "/Users/AuthenticateByName",
                            json={"Username": username, "Pw": password})).json()
        self.token = j["AccessToken"]
        self.user_id = j["User"]["Id"]

    async def resolve_user(self, username: str | None) -> None:
        users = (await self.req("GET", "/Users")).json()
        pick = [u for u in users if username is None or u["Name"].lower() == username.lower()]
        if not pick:
            raise SystemExit(f"user {username!r} not found")
        admins = [u for u in pick if u.get("Policy", {}).get("IsAdministrator")]
        self.user_id = (admins or pick)[0]["Id"]

    async def logout(self) -> None:
        try:
            await self.req("POST", "/Sessions/Logout")
        except Exception:
            pass


async def pick_items(jf: Jellyfin, args) -> list[dict]:
    if args.item:
        return [(await jf.req("GET", f"/Items/{iid}",
                              params={"userId": jf.user_id, "fields": "MediaSources,MediaStreams,RunTimeTicks"})).json()
                for iid in args.item]
    parent = None
    if args.library:
        libs = (await jf.req("GET", f"/Users/{jf.user_id}/Views")).json()["Items"]
        lib = [l for l in libs if l["Name"].lower() == args.library.lower()]
        if not lib:
            raise SystemExit(f"library {args.library!r} not found: {[l['Name'] for l in libs]}")
        parent = lib[0]["Id"]
    filt = parse_filter(args.filter)
    if not filt:
        items = (await jf.req("GET", "/Items", params=items_query(jf.user_id, args.types, parent))).json()["Items"]
    else:  # whole library, a page at a time, so rare formats (DV, Hi10P, ...) are found
        items, start, page = [], 0, 500
        while True:
            r = (await jf.req("GET", "/Items", params=items_query(jf.user_id, args.types, parent, filt,
                                                                   start, page))).json()
            items += [i for i in r["Items"] if item_matches(i, filt)]
            start += page
            if start >= r.get("TotalRecordCount", 0) or not r["Items"]:
                break
    if not items:
        raise SystemExit(f"no items matched --filter {args.filter!r}; loosen --filter/--library/--types")
    return items


@dataclass
class Stream:
    idx: int
    mode: str
    device_id: str
    player: Player
    item: dict = field(default_factory=dict)
    method: str = ""
    error: str = ""
    srv_fps: float | None = None
    srv_hw: str = ""
    srv_codec: str = ""
    srv_reasons: str = ""
    src_fps: float | None = None
    burn: str = ""
    replaced: bool = False

    @property
    def label(self) -> str:
        return item_label(self.item)

    def summary(self) -> dict:
        p = self.player
        sp = p.speed(1000)
        return {
            "idx": self.idx, "mode": self.mode, "server_method": self.method,
            "item": self.label, "item_id": self.item.get("Id"),
            "source": describe_video(video_stream(first_source(self.item))),
            "state": p.state, "error": self.error,
            "startup_s": round(p.startup_s, 2) if p.startup_s else None,
            "stalls": p.stalls, "stall_s": round(p.stall_s, 1),
            "played_s": round(p.played_s, 1), "mbytes": round(p.bytes / 1e6, 1),
            "median_fill_speed": round(sp, 2) if sp else None,
            "max_segment_fetch_s": round(p.seg_fetch_max, 2),
            "server_fps": self.srv_fps, "source_fps": self.src_fps, "hw_accel": self.srv_hw,
            "server_codecs": self.srv_codec, "transcode_reasons": self.srv_reasons,
            "burned_subtitle": self.burn or None,
        }


class Runner:
    def __init__(self, args):
        self.args = args
        self.cfg = HealthConfig(args.startup_buffer, args.rebuffer, args.max_buffer,
                                args.min_speed, args.grace, args.max_startup)
        self.run_id = uuid.uuid4().hex[:8]
        self.streams: list[Stream] = []
        self.tasks: list[asyncio.Task] = []
        self.stop = asyncio.Event()
        self.items: list[dict] = []
        self.events: list[str] = []
        self.ramp_result: int | None = None
        self.ramp_reason = ""
        self.held_ok = 0
        self.stopped_by_user = False
        self.t0 = time.monotonic()
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=200)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10),
                                      limits=limits, verify=not args.insecure,
                                      follow_redirects=True)
        self.ctl = Jellyfin(args.server, self.http, f"{CLIENT}-{self.run_id}-ctl", f"{CLIENT}-ctl",
                            token=args.api_key)
        self.overrides = dict(kv.split("=", 1) for kv in (args.param or []))

    def log(self, msg: str) -> None:
        t = time.monotonic() - self.t0
        self.events.append(f"[{t:6.0f}s] {msg}")
        if self.args.jsonl:
            emit({"type": "event", "t": round(t, 1), "msg": msg})

    async def client_for(self, s: Stream) -> Jellyfin:
        jf = Jellyfin(self.args.server, self.http, s.device_id, f"{CLIENT}-{s.idx:02d}",
                      token=self.args.api_key)
        if self.args.api_key:
            jf.user_id = self.ctl.user_id
        else:
            await jf.login(self.args.username, self.args.password)
        return jf

    async def play(self, s: Stream) -> None:
        a, p = self.args, s.player
        jf = play_session = source_id = None
        try:
            jf = await self.client_for(s)
            src = first_source(s.item)
            vs = video_stream(src)
            if vs:
                s.src_fps = vs.get("RealFrameRate") or vs.get("AverageFrameRate")
            runtime = (s.item.get("RunTimeTicks") or 0) / TICKS
            start = 0.0
            if a.random_start and runtime > 600:
                start = random.uniform(60, max(61, runtime - a.duration - 120))
            body = playback_info_body(s.mode, jf.user_id, src, start, codec=a.codec,
                                      bitrate=a.bitrate, max_height=a.max_height, burn_subs=a.burn_subs,
                                      sub_codecs=subtitle_codecs(parse_filter(a.filter)))
            if "SubtitleStreamIndex" in body:
                sub = next(m for m in src["MediaStreams"] if m.get("Index") == body["SubtitleStreamIndex"])
                s.burn = (sub.get("Codec") or "?").lower()
                self.log(f"#{s.idx} burning in {s.burn} subtitle (track {sub['Index']}"
                         f"{', ' + sub['Language'] if sub.get('Language') else ''})")
            pi = (await jf.req("POST", f"/Items/{s.item['Id']}/PlaybackInfo",
                               params={"userId": jf.user_id}, json=body)).json()
            if pi.get("ErrorCode"):
                raise RuntimeError(f"PlaybackInfo: {pi['ErrorCode']}")
            ms = pi["MediaSources"][0]
            play_session, source_id = pi.get("PlaySessionId"), ms.get("Id")
            s.method = method_for(s.mode, ms)
            p.begin(time.monotonic())
            await self._report(jf, "/Sessions/Playing", s, play_session, source_id, start)
            prog = asyncio.create_task(self._progress_loop(jf, s, play_session, source_id, start))
            try:
                if s.method == "DirectPlay":
                    await self.play_static(jf, s, ms, play_session, start)
                else:
                    url = transcoding_url(s.mode, ms, self.overrides)
                    await self.play_hls(jf, s, jf.base + url if url.startswith("/") else url, start)
            finally:
                prog.cancel()
            p.state = "done"
        except asyncio.CancelledError:
            if p.state != "error":
                p.state = "done"
        except Exception as e:  # noqa: BLE001
            p.state = "error"
            s.error = f"{type(e).__name__}: {e}"[:200]
            self.log(f"#{s.idx} error: {s.error}")
        finally:
            if jf is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._cleanup(jf, s, play_session, source_id)), 15)
                except Exception:
                    pass

    async def _report(self, jf, path, s, ps, src_id, start, extra=None):
        body = {"ItemId": s.item["Id"], "MediaSourceId": src_id, "PlaySessionId": ps,
                "PlayMethod": s.method, "PositionTicks": int((start + s.player.played_s) * TICKS),
                "CanSeek": True, "IsPaused": False, "IsMuted": True}
        body.update(extra or {})
        try:
            await jf.req("POST", path, json=body)
        except Exception:
            pass

    async def _cleanup(self, jf, s, ps, src_id):
        if ps:
            await self._report(jf, "/Sessions/Playing/Stopped", s, ps, src_id, 0)
            try:
                await jf.req("DELETE", "/Videos/ActiveEncodings",
                             params={"deviceId": s.device_id, "playSessionId": ps})
            except Exception:
                pass
        if not self.args.api_key:
            await jf.logout()

    async def _progress_loop(self, jf, s, ps, src_id, start):
        while True:
            await asyncio.sleep(10)
            await self._report(jf, "/Sessions/Playing/Progress", s, ps, src_id, start,
                               {"EventName": "timeupdate"})

    async def _wait_for_room(self, s: Stream) -> bool:
        while True:
            now = time.monotonic()
            if self.stop.is_set() or s.player.played_s >= self.args.duration:
                return False
            if s.player.room(now):
                return True
            await asyncio.sleep(0.25)

    async def play_hls(self, jf, s, master_url, start):
        p = s.player
        r = await jf.req("GET", master_url)
        variants, init, segs = parse_m3u8(r.text, str(r.url))
        if variants:
            r = await jf.req("GET", variants[0])
            _, init, segs = parse_m3u8(r.text, str(r.url))
        if not segs:
            raise RuntimeError("empty HLS playlist")
        if init:
            p.bytes += len((await jf.req("GET", init)).content)
        for uri, dur in segs[seek_index(segs, start):]:
            if not await self._wait_for_room(s):
                break
            t0 = time.monotonic()
            async with self.http.stream("GET", uri, headers={"Authorization": jf.auth()}) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(65536):
                    p.bytes += len(chunk)
                    if self.stop.is_set():
                        break
            now = time.monotonic()
            p.got_media(now, dur, now - t0)

    async def play_static(self, jf, s, ms, ps, start):
        p = s.player
        bitrate = ms.get("Bitrate") or 0
        if not bitrate:
            raise RuntimeError("source has no bitrate; cannot pace direct play")
        bps = bitrate / 8
        params = {"static": "true", "mediaSourceId": ms["Id"], "playSessionId": ps, "deviceId": s.device_id}
        headers = {"Authorization": jf.auth()}
        if start and ms.get("Size"):
            headers["Range"] = f"bytes={int(start * bps)}-"
        async with self.http.stream("GET", f"{jf.base}/Videos/{s.item['Id']}/stream",
                                    params=params, headers=headers) as resp:
            resp.raise_for_status()
            t_last, pending = time.monotonic(), 0
            async for chunk in resp.aiter_bytes(256 * 1024):
                pending += len(chunk)
                p.bytes += len(chunk)
                if pending >= bps:  # account in ~1 s media slices
                    now = time.monotonic()
                    p.got_media(now, pending / bps, now - t_last)
                    pending = 0
                    if not await self._wait_for_room(s):
                        break
                    t_last = time.monotonic()

    async def poll_sessions(self) -> None:
        while not self.stop.is_set():
            try:
                sess = (await self.ctl.req("GET", "/Sessions", params={"activeWithinSeconds": 120})).json()
                by_dev = {s.device_id: s for s in self.streams}
                for x in sess:
                    s = by_dev.get(x.get("DeviceId"))
                    ti = x.get("TranscodingInfo") if s else None
                    if not ti:
                        continue
                    s.srv_fps = ti.get("Framerate")
                    s.srv_hw = ti.get("HardwareAccelerationType") or "none"
                    v = "copy" if ti.get("IsVideoDirect") else ti.get("VideoCodec", "?")
                    au = "copy" if ti.get("IsAudioDirect") else ti.get("AudioCodec", "?")
                    s.srv_codec = f"{v}/{au}" + (f" {ti['Width']}x{ti.get('Height')}" if ti.get("Width") else "")
                    s.srv_reasons = ",".join(ti.get("TranscodeReasons") or [])
            except Exception as e:  # noqa: BLE001
                self.log(f"/Sessions poll failed: {e}")
            await asyncio.sleep(3)

    def add_stream(self, mode: str) -> Stream:
        idx = len(self.streams) + 1
        s = Stream(idx=idx, mode=mode, device_id=f"{CLIENT}-{self.run_id}-{idx:02d}", player=Player(self.cfg))
        s.player.on_stall_start = lambda: self.log(
            f"#{idx} stalled (buffer empty) at {s.player.played_s:.0f}s of playback")
        s.player.on_stall_end = lambda d: self.log(f"#{idx} resumed after {d:.1f}s stalled")
        s.item = self.items[0] if self.args.same_item else self.items[(idx - 1) % len(self.items)]
        self.streams.append(s)
        self.tasks.append(asyncio.create_task(self.play(s)))
        self.log(f"+ #{idx} {mode}: {s.label} [{describe_video(video_stream(first_source(s.item)))}]")
        return s

    def active(self) -> list[Stream]:
        return [s for s in self.streams if s.player.active]

    async def run(self) -> None:
        a = self.args
        if a.api_key:
            await self.ctl.resolve_user(a.username)
        else:
            await self.ctl.login(a.username, a.password)
        info = (await self.ctl.req("GET", "/System/Info")).json()
        self.log(f"server {info.get('ServerName')} v{info.get('Version')}")
        self.items = await pick_items(self.ctl, a)
        random.shuffle(self.items)
        self.log(f"{len(self.items)} candidate items")
        poller = asyncio.create_task(self.poll_sessions())
        cycle = parse_mix(a.mix)
        watcher = asyncio.create_task(self._watch_stop_file()) if a.stop_file else None
        try:
            view = (contextlib.nullcontext() if a.jsonl
                    else Live(self.render(), console=console, refresh_per_second=2, transient=False))
            with view as live:
                ui = asyncio.create_task(self._status_loop() if a.jsonl else self._ui(live))
                if a.ramp:
                    await self._ramp(cycle)
                else:
                    for i in range(a.streams):
                        self.add_stream(cycle[i % len(cycle)])
                        await asyncio.sleep(a.stagger)
                    deadline = time.monotonic() + a.duration + a.max_startup
                    while (time.monotonic() < deadline and not self.stop.is_set()
                           and any(not t.done() for t in self.tasks)):
                        await asyncio.sleep(1)
                self.stop.set()
                await asyncio.wait(self.tasks, timeout=30)
                ui.cancel()
                if a.jsonl:
                    emit(self.status())
                else:
                    live.update(self.render())
        finally:
            self.stop.set()
            poller.cancel()
            if watcher:
                watcher.cancel()
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if not a.api_key:
                await self.ctl.logout()
            await self.http.aclose()

    async def _ramp(self, cycle: list[str]) -> None:
        a = self.args
        while not self.stop.is_set():
            n = len(self.active())
            if n >= a.max_streams:
                self.ramp_result, self.ramp_reason = self.held_ok, f"hit --max-streams {a.max_streams}"
                if await self._hold(a.interval) and not self.stop.is_set():
                    self.ramp_result = n
                return
            self.add_stream(cycle[len(self.streams) % len(cycle)])
            if not await self._hold(a.interval):
                self.ramp_result = self.held_ok
                return
            if self.stop.is_set():
                break
            self.held_ok = len(self.active())
            self.log(f"stable at {self.held_ok} stream(s)")
        if self.ramp_result is None:
            self.ramp_result = self.held_ok
            self.ramp_reason = "stopped before any level degraded (result is a lower bound)"

    async def _hold(self, secs: float) -> bool:
        """Hold the current level for `secs`, extended (up to 3x) while any viewer is
        still undecided, so a slow-to-show degradation isn't credited to the next level."""
        end = time.monotonic() + secs
        hard_end = time.monotonic() + 3 * secs
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= hard_end or (now >= end and not any(s.player.undecided for s in self.active())):
                break
            await asyncio.sleep(1)
            now = time.monotonic()
            bad = [(s, r) for s in self.streams if (r := s.player.degraded(now))]
            if bad:
                self.ramp_reason = (f"degraded at {len(self.active())} stream(s): "
                                    + ", ".join(f"#{s.idx} {r}" for s, r in bad))
                self.log(self.ramp_reason)
                return False
            for s in list(self.streams):  # replace finished streams to keep load constant
                if s.player.state == "done" and not s.replaced and not self.stop.is_set():
                    s.replaced = True
                    self.add_stream(s.mode)
        return True

    async def _ui(self, live: Live) -> None:
        while True:
            live.update(self.render())
            await asyncio.sleep(0.5)

    async def _status_loop(self) -> None:
        while True:
            emit(self.status())
            await asyncio.sleep(self.args.status_interval)

    async def _watch_stop_file(self) -> None:
        while not self.stop.is_set():
            try:
                with open(self.args.stop_file) as f:
                    flag = f.read().strip().strip('"').lower()
            except OSError:
                flag = ""
            if flag in ("1", "true", "yes", "stop"):
                self.stopped_by_user = True
                self.log("stop requested")
                self.stop.set()
                return
            await asyncio.sleep(2)

    def stream_row(self, s: Stream) -> dict:
        p = s.player
        sp = p.speed()
        return {
            "idx": s.idx, "mode": s.mode, "item": s.label,
            "source": describe_video(video_stream(first_source(s.item))),
            "state": p.state, "buffer_s": round(p.buffer, 1),
            "fill_x": round(sp, 2) if sp else None,
            "startup_s": round(p.startup_s, 1) if p.startup_s else None,
            "stalls": p.stalls, "stall_s": round(p.stall_s, 1),
            "server_fps": s.srv_fps, "source_fps": s.src_fps,
            "hw_accel": s.srv_hw, "server_codecs": s.srv_codec, "subs": s.burn, "error": s.error,
        }

    def status(self) -> dict:
        now = time.monotonic()
        for s in self.streams:
            s.player.tick(now)
        mbit = sum(x.player.bytes for x in self.streams) * 8 / 1e6 / max(now - self.t0, 1)
        return {
            "type": "status", "t": round(now - self.t0, 1), "server": self.args.server,
            "mode": "ramp" if self.args.ramp else "fixed", "active": len(self.active()),
            "streams_total": len(self.streams), "held_ok": self.held_ok if self.args.ramp else None,
            "mbit_s": round(mbit, 1), "stopping": self.stop.is_set(),
            "streams": [self.stream_row(s) for s in self.streams[-40:]],
        }

    def render(self):
        now = time.monotonic()
        for s in self.streams:
            s.player.tick(now)
        t = Table(expand=False, pad_edge=False, box=None, header_style="bold")
        for col, j in (("#", "right"), ("mode", "left"), ("item", "left"), ("source", "left"),
                       ("state", "left"), ("buf s", "right"), ("fill x", "right"), ("startup", "right"),
                       ("stalls", "right"), ("srv fps", "right"), ("hw", "left"), ("out", "left")):
            t.add_column(col, justify=j, no_wrap=True)
        colors = {"playing": "green", "starting": "yellow", "stalled": "red", "error": "red", "done": "dim"}
        for s in self.streams[-40:]:
            p = s.player
            sp = p.speed()
            fps = f"{s.srv_fps:.0f}" if s.srv_fps else ""
            if s.srv_fps and s.src_fps:
                fps += f" ({s.srv_fps / s.src_fps:.1f}x)"
            t.add_row(str(s.idx), s.mode, s.label[:28], describe_video(video_stream(first_source(s.item))),
                      Text(p.state, style=colors.get(p.state, "")),
                      f"{p.buffer:.0f}", ("≥9" if sp > 9 else f"{sp:.2f}") if sp else "",
                      f"{p.startup_s:.1f}" if p.startup_s else "",
                      f"{p.stalls}" + (f" ({p.stall_s:.0f}s)" if p.stall_s else ""),
                      fps, s.srv_hw, s.srv_codec)
        mbit = sum(x.player.bytes for x in self.streams) * 8 / 1e6 / max(now - self.t0, 1)
        head = Text(f"jfstress {self.args.server}  t={now - self.t0:.0f}s  active={len(self.active())}  "
                    f"mode={'ramp' if self.args.ramp else 'fixed'}  Mbit/s≈{mbit:.0f}", style="bold")
        return Group(head, t, Text("\n".join(self.events[-6:]), style="dim"))

    def report(self) -> dict:
        played = [s.player.startup_s for s in self.streams if s.player.startup_s]
        return {
            "server": self.args.server, "run_id": self.run_id,
            "args": {k: v for k, v in vars(self.args).items() if k not in ("password", "api_key")},
            "stopped_by_user": self.stopped_by_user,
            "duration_s": round(time.monotonic() - self.t0, 1),
            "ramp_result_streams": self.ramp_result, "ramp_reason": self.ramp_reason,
            "streams_total": len(self.streams),
            "errors": sum(1 for s in self.streams if s.player.state == "error"),
            "stalls_total": sum(s.player.stalls for s in self.streams),
            "stall_seconds_total": round(sum(s.player.stall_s for s in self.streams), 1),
            "startup_p50_s": round(statistics.median(played), 2) if played else None,
            "startup_max_s": round(max(played), 2) if played else None,
            "streams": [s.summary() for s in self.streams], "events": self.events,
        }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("server")
    g.add_argument("--server", required=True, help="e.g. https://jellyfin-public-test.amer.dev")
    g.add_argument("--username", help="user to log in as (one login per simulated stream)")
    g.add_argument("--password", default="", help="password (or env JF_PASSWORD)")
    g.add_argument("--api-key", help="admin API key instead of username/password (or env JF_API_KEY)")
    g.add_argument("--insecure", action="store_true", help="skip TLS verification")
    g.add_argument("--timeout", type=float, default=60, help="per-request timeout s (default 60)")

    g = p.add_argument_group("load")
    g.add_argument("--mix", default="transcode",
                   help="playback modes, e.g. 'transcode=3,direct=1'. Fixed mode: cycled to fill "
                        "--streams. Ramp mode: order streams are added in. Modes: direct, remux, transcode")
    g.add_argument("--streams", type=int, default=4, help="fixed mode: concurrent streams (default 4)")
    g.add_argument("--stagger", type=float, default=2, help="fixed mode: s between stream starts")
    g.add_argument("--duration", type=float, default=300, help="media seconds each stream plays (default 300)")
    g.add_argument("--ramp", action="store_true", help="add streams until one degrades; report the max")
    g.add_argument("--interval", type=float, default=45, help="ramp: s to hold each level (default 45)")
    g.add_argument("--max-streams", type=int, default=20, help="ramp: upper bound (default 20)")

    g = p.add_argument_group("transcode target")
    g.add_argument("--codec", default="h264", choices=["h264", "hevc", "av1"], help="output video codec")
    g.add_argument("--bitrate", type=int, default=8_000_000, help="max streaming bitrate bps (default 8M)")
    g.add_argument("--max-height", type=int, default=1080, help="transcode: max output height (default 1080)")
    g.add_argument("--burn-subs", action="store_true",
                   help="transcode: burn in a subtitle track (the first one, or the first of the --filter subtitle format)")
    g.add_argument("--param", action="append", metavar="K=V",
                   help="override a TranscodingUrl query param, e.g. --param VideoBitrate=4000000")

    g = p.add_argument_group("media selection")
    g.add_argument("--types", default="Movie,Episode", help="item types (default Movie,Episode)")
    g.add_argument("--library", help="restrict to a library by name")
    g.add_argument("--filter", help="comma list, all must match: " + ",".join(SOURCE_FILTERS)
                   + ". Subtitle tokens (ass,pgs,dvdsub,srt) also pick the track --burn-subs burns in")
    g.add_argument("--item", action="append", help="explicit item id (repeatable)")
    g.add_argument("--same-item", action="store_true", help="every stream plays the same item")
    g.add_argument("--random-start", action="store_true", default=True,
                   help="start each stream at a random position (default on)")
    g.add_argument("--from-start", dest="random_start", action="store_false", help="start at 0")

    g = p.add_argument_group("player model / health")
    g.add_argument("--startup-buffer", type=float, default=6, help="media s buffered before playback starts")
    g.add_argument("--rebuffer", type=float, default=4, help="media s needed to resume after a stall")
    g.add_argument("--max-buffer", type=float, default=30, help="player buffer ceiling, media s (default 30)")
    g.add_argument("--min-speed", type=float, default=1.0,
                   help="ramp: a stream filling slower than this (x real time) while not full is behind")
    g.add_argument("--grace", type=float, default=15, help="ramp: s a stream may be behind before failing")
    g.add_argument("--max-startup", type=float, default=30, help="startup longer than this counts as failure")

    p.add_argument("--out", help="write JSON report here")
    p.add_argument("--seed", type=int, help="random seed (item choice/start positions)")

    g = p.add_argument_group("output (for running in a pod)")
    g.add_argument("--jsonl", action="store_true",
                   help="no terminal table: print JSON lines to stdout (event, status, and a final report)")
    g.add_argument("--status-interval", type=float, default=5, help="--jsonl: s between status lines")
    g.add_argument("--stop-file", help="stop gracefully once this file contains true/1/yes/stop")
    return p


def parse_args(argv=None, parser: argparse.ArgumentParser | None = None):
    p = parser or build_parser()
    a = p.parse_args(argv)
    a.password = a.password or os.environ.get("JF_PASSWORD", "")
    a.api_key = a.api_key or os.environ.get("JF_API_KEY")
    if not a.api_key and not a.username:
        p.error("give --username/--password or --api-key")
    try:
        parse_mix(a.mix)
        parse_filter(a.filter)
    except ValueError as e:
        p.error(str(e))
    if a.seed is not None:
        random.seed(a.seed)
    return a


def print_summary(rep: dict) -> None:
    c = Console()
    c.rule("summary")
    if rep["ramp_result_streams"] is not None:
        c.print(f"[bold]Ramp result: {rep['ramp_result_streams']} concurrent stream(s) held "
                f"without degradation[/]  ({rep['ramp_reason']})")
    c.print(f"streams={rep['streams_total']} errors={rep['errors']} stalls={rep['stalls_total']} "
            f"stall_s={rep['stall_seconds_total']} startup p50={rep['startup_p50_s']}s "
            f"max={rep['startup_max_s']}s")
    for s in [s for s in rep["streams"] if s["error"]][:10]:
        c.print(f"  #{s['idx']} {s['item']}: [red]{s['error']}[/]")


async def amain(args) -> dict:
    r = Runner(args)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, r.stop.set)
        except NotImplementedError:
            pass
    await r.run()
    return r.report()


def main() -> None:
    args = parse_args()
    rep = asyncio.run(amain(args))
    if args.jsonl:
        emit({"type": "report", "report": rep})
    else:
        print_summary(rep)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(rep, f, indent=2)
        Console().print(f"report: {args.out}")


if __name__ == "__main__":
    main()
