#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""jfcompat: library compatibility report.

For every video in the library and every device in compat_profiles.DEVICES, asks Jellyfin's own
StreamBuilder (POST /Items/{id}/PlaybackInfo with that device's profile) what it would do:
  D  direct play  - the file is sent as is
  R  remux        - video is copied; only the container and/or audio change (cheap)
  T  transcode    - video is re-encoded (expensive), with Jellyfin's reasons
Each file is asked twice when it has subtitles: without subtitles, and with its default (or
first English, or first) subtitle track on, which is when image/ASS subtitles force a burn-in.
Nothing is played: PlaybackInfo only returns the plan.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from compat_profiles import DEVICES
from jfcore import auth_header, describe_video, first_source, item_label, report_lines, video_stream

CLIENT = "jfcompat"
# Reasons that mean the video itself has to be re-encoded.
VIDEO_REASONS = {
    "VideoCodecNotSupported", "VideoProfileNotSupported", "VideoLevelNotSupported",
    "VideoResolutionNotSupported", "VideoBitDepthNotSupported", "VideoFramerateNotSupported",
    "RefFramesNotSupported", "AnamorphicVideoNotSupported", "InterlacedVideoNotSupported",
    "VideoRangeTypeNotSupported", "VideoCodecTagNotSupported", "VideoBitrateNotSupported",
    "SubtitleCodecNotSupported", "ContainerBitrateExceedsLimit", "UnknownVideoStreamInfo",
}


def emit(obj: dict) -> None:
    print(json.dumps(obj, separators=(",", ":"), default=str), flush=True)


def report_subtitle(src: dict) -> dict | None:
    """The track a viewer most likely turns on: default, else first English, else first."""
    subs = [m for m in src.get("MediaStreams") or [] if m.get("Type") == "Subtitle"]
    for pick in (lambda m: m.get("IsDefault"), lambda m: (m.get("Language") or "").lower() in ("eng", "en"),
                 lambda m: True):
        hit = next((m for m in subs if pick(m)), None)
        if hit:
            return hit
    return None


def classify(ms: dict, src_video: str) -> tuple[str, list[str]]:
    url = ms.get("TranscodingUrl")
    if ms.get("SupportsDirectPlay") and not url:
        return "D", []
    if not url:
        return "E", ["NoPlaybackPath"]
    q = parse_qs(urlsplit(url).query)
    reasons = [r for r in (q.get("TranscodeReasons") or [""])[0].split(",") if r]
    out_video = {c.strip().lower() for c in (q.get("VideoCodec") or [""])[0].split(",") if c.strip()}
    if VIDEO_REASONS & set(reasons) or (src_video and out_video and src_video.lower() not in out_video):
        return "T", reasons
    return "R", reasons


def describe_audio(src: dict) -> str:
    a = next((m for m in src.get("MediaStreams") or [] if m.get("Type") == "Audio" and m.get("IsDefault")), None) \
        or next((m for m in src.get("MediaStreams") or [] if m.get("Type") == "Audio"), None)
    if not a:
        return "none"
    prof = (a.get("Profile") or "")
    extra = " Atmos" if "atmos" in prof.lower() or "joc" in prof.lower() else (" MA" if "MA" in prof else "")
    return f"{a.get('Codec', '?')}{extra} {a.get('Channels') or '?'}ch"


class Report:
    def __init__(self, args):
        self.args = args
        self.devices = [d for d in DEVICES if not args.device or d.id in args.device]
        self.stop = asyncio.Event()
        self.reasons: dict[str, int] = {}
        self.t0 = time.monotonic()
        self.done = self.failed = 0
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10), verify=not args.insecure,
                                      limits=httpx.Limits(max_connections=args.concurrency * 2))
        self.token = self.user_id = None
        self.device_id = f"{CLIENT}-{os.getpid()}"

    def log(self, msg: str) -> None:
        t = round(time.monotonic() - self.t0, 1)
        if self.args.jsonl:
            emit({"type": "event", "t": t, "msg": msg})
        else:
            print(f"[{t:6.0f}s] {msg}", file=sys.stderr, flush=True)

    def headers(self) -> dict:
        return {"Authorization": auth_header(CLIENT, self.device_id, self.token)}

    async def req(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self.http.request(method, self.args.server.rstrip("/") + path, headers=self.headers(), **kw)
        r.raise_for_status()
        return r

    def code(self, decision: str, reasons: list[str]) -> str:
        if not reasons:
            return decision
        return decision + ":" + ".".join(str(self.reasons.setdefault(r, len(self.reasons))) for r in reasons)

    async def items(self) -> list[dict]:
        parent = None
        if self.args.library:
            views = (await self.req("GET", f"/Users/{self.user_id}/Views")).json()["Items"]
            hit = [v for v in views if v["Name"].lower() == self.args.library.lower()]
            if not hit:
                raise SystemExit(f"library {self.args.library!r} not found: {[v['Name'] for v in views]}")
            parent = hit[0]["Id"]
        out, start, page = [], 0, 500
        while True:
            q = {"userId": self.user_id, "Recursive": "true", "IncludeItemTypes": self.args.types,
                 "MediaTypes": "Video", "Fields": "MediaSources,MediaStreams,ProductionYear",
                 "IsMissing": "false", "SortBy": "SortName", "StartIndex": str(start), "Limit": str(page)}
            if parent:
                q["ParentId"] = parent
            r = (await self.req("GET", "/Items", params=q)).json()
            out += [i for i in r["Items"] if video_stream(first_source(i))]
            start += page
            if start >= r.get("TotalRecordCount", 0) or not r["Items"] or (self.args.limit and len(out) >= self.args.limit):
                break
        return out[: self.args.limit] if self.args.limit else out

    async def decide(self, item: dict, device, sub: dict | None) -> str:
        src = first_source(item)
        body = {"UserId": self.user_id, "DeviceProfile": device.profile,
                "MaxStreamingBitrate": device.profile.get("MaxStreamingBitrate"),
                "EnableDirectPlay": True, "EnableDirectStream": True, "EnableTranscoding": True,
                "AllowVideoStreamCopy": True, "AllowAudioStreamCopy": True,
                "MediaSourceId": src.get("Id"), "AutoOpenLiveStream": False}
        # -1 = subtitles off; without it Jellyfin applies the user's default subtitle choice
        body["SubtitleStreamIndex"] = sub["Index"] if sub is not None else -1
        for attempt in range(3):
            try:
                r = await self.req("POST", f"/Items/{item['Id']}/PlaybackInfo",
                                   params={"userId": self.user_id}, json=body)
                ms = (r.json().get("MediaSources") or [{}])[0]
                return self.code(*classify(ms, (video_stream(src) or {}).get("Codec", "")))
            except (httpx.HTTPError, ValueError) as e:
                if attempt == 2:
                    self.failed += 1
                    return self.code("E", [type(e).__name__])
                await asyncio.sleep(1 + attempt)
        return "E"

    async def run(self) -> dict:
        a = self.args
        j = (await self.req("POST", "/Users/AuthenticateByName",
                            json={"Username": a.username, "Pw": a.password})).json()
        self.token, self.user_id = j["AccessToken"], j["User"]["Id"]
        try:
            info = (await self.req("GET", "/System/Info")).json()
            self.log(f"server {info.get('ServerName')} v{info.get('Version')}")
            items = await self.items()
            subs = [report_subtitle(first_source(i)) if a.subtitles else None for i in items]
            jobs = [(i, d, k) for i in range(len(items)) for d in range(len(self.devices))
                    for k in ((0, 1) if subs[i] is not None else (0,))]
            self.log(f"{len(items)} files x {len(self.devices)} devices: {len(jobs)} decisions to ask for")
            res = {d.id: [None] * len(items) for d in self.devices}
            res_subs = {d.id: [None] * len(items) for d in self.devices}
            queue: asyncio.Queue = asyncio.Queue()
            for job in jobs:
                queue.put_nowait(job)
            step = max(len(jobs) // 20, 1)

            async def worker():
                while not self.stop.is_set():
                    try:
                        i, d, k = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    dev = self.devices[d]
                    code = await self.decide(items[i], dev, subs[i] if k else None)
                    (res_subs if k else res)[dev.id][i] = code
                    self.done += 1
                    if self.done % step == 0:
                        rate = self.done / max(time.monotonic() - self.t0, 1)
                        self.log(f"{self.done}/{len(jobs)} ({100 * self.done // len(jobs)}%), "
                                 f"{rate:.0f}/s, {self.failed} failed")

            watcher = asyncio.create_task(self._watch_stop_file()) if a.stop_file else None
            await asyncio.gather(*(worker() for _ in range(a.concurrency)))
            if watcher:
                watcher.cancel()
            if self.stop.is_set():
                self.log("stopped early: the report covers the decisions made so far")
        finally:
            try:
                await self.req("POST", "/Sessions/Logout")
            except httpx.HTTPError:
                pass
            await self.http.aclose()
        return self.build(items, subs, res, res_subs)

    async def _watch_stop_file(self):
        while not self.stop.is_set():
            try:
                with open(self.args.stop_file) as f:
                    if f.read().strip().strip('"').lower() in ("1", "true", "yes", "stop"):
                        self.log("stop requested")
                        self.stop.set()
            except OSError:
                pass
            await asyncio.sleep(2)

    def build(self, items, subs, res, res_subs) -> dict:
        def counts(codes):
            c = {"direct": 0, "remux": 0, "transcode": 0, "error": 0, "pending": 0}
            for x in codes:
                key = {"D": "direct", "R": "remux", "T": "transcode", "E": "error"}.get((x or "?")[0], "pending")
                c[key] += 1
            return c

        rows = []
        for it, sub in zip(items, subs):
            src = first_source(it)
            vs = video_stream(src) or {}
            rows.append({
                "id": it["Id"], "title": item_label(it), "type": it.get("Type"),
                "year": it.get("ProductionYear"), "series": it.get("SeriesName"),
                "container": src.get("Container"), "video": describe_video(vs),
                "profile": vs.get("Profile"), "audio": describe_audio(src),
                "mbps": round((src.get("Bitrate") or 0) / 1e6, 1),
                "sub": f"{(sub.get('Codec') or '?').lower()} {sub.get('Language') or ''}".strip() if sub else None,
            })
        summary = {}
        for d in self.devices:
            with_subs = [s if s is not None else n for s, n in zip(res_subs[d.id], res[d.id])]
            summary[d.id] = {"no_subs": counts(res[d.id]), "subs_on": counts(with_subs)}
        return {
            "type": "compat", "server": self.args.server,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - self.t0, 1), "stopped_early": self.stop.is_set(),
            "devices": [{"id": d.id, "name": d.name, "app": d.app, "hardware": d.hardware,
                         "assumptions": d.assumptions} for d in self.devices],
            "reasons": sorted(self.reasons, key=self.reasons.get),
            "items": rows, "results": res, "results_subs": res_subs, "summary": summary,
        }


def compat_result(rep: dict) -> dict:
    out = {"kind": "compat", "files": len(rep.get("items") or []), "transcode_pct": {}}
    for d in rep.get("devices") or []:
        c = rep["summary"][d["id"]]["no_subs"]
        out["transcode_pct"][d["id"]] = round(100 * c["transcode"] / (sum(c.values()) or 1))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password", default="", help="or env JF_PASSWORD")
    p.add_argument("--types", default="Movie,Episode", help="item types (default Movie,Episode)")
    p.add_argument("--library", help="only this library")
    p.add_argument("--device", action="append", choices=[d.id for d in DEVICES], help="only these devices")
    p.add_argument("--no-subtitles", dest="subtitles", action="store_false",
                   help="skip the subtitles-on pass (halves the run)")
    p.add_argument("--concurrency", type=int, default=8, help="PlaybackInfo requests in flight (default 8)")
    p.add_argument("--limit", type=int, help="only the first N files (for a quick look)")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--out", help="write the JSON report here")
    p.add_argument("--jsonl", action="store_true", help="JSON lines on stdout: events, then the report")
    p.add_argument("--stop-file", help="stop early once this file contains true/1/yes/stop")
    return p


async def amain(args) -> dict:
    r = Report(args)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, r.stop.set)
        except NotImplementedError:
            pass
    return await r.run()


def main() -> None:
    args = build_parser().parse_args()
    args.password = args.password or os.environ.get("JF_PASSWORD", "")
    rep = asyncio.run(amain(args))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(rep, f)
    if args.jsonl:
        for line in report_lines(rep, compat_result(rep)):
            print(line, flush=True)
    else:
        for d in rep["devices"]:
            s = rep["summary"][d["id"]]
            n = sum(s["no_subs"].values()) or 1
            print(f"{d['name'] + ' / ' + d['app']:62s} direct {100 * s['no_subs']['direct'] // n:3d}%  "
                  f"remux {100 * s['no_subs']['remux'] // n:3d}%  transcode {100 * s['no_subs']['transcode'] // n:3d}%  "
                  f"(subs on: transcode {100 * s['subs_on']['transcode'] // n}%)")


if __name__ == "__main__":
    main()
