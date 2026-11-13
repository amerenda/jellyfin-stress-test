#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Tiny fake Jellyfin for testing jfstress without a real server.

Transcodes share a fixed capacity (--capacity, in x real time): with N active
transcodes each one runs at min(4, capacity / N). Segments block until "encoded".
"""
import argparse
import asyncio
import re
import time

from aiohttp import web

SEG = 2.0          # segment seconds
RUNTIME = 3600.0   # item length
p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=18096)
p.add_argument("--capacity", type=float, default=3.0)
A = p.parse_args()

jobs: dict[str, dict] = {}  # device_id -> {"produced": s, "item": id}


def dev(req) -> str:
    m = re.search(r'DeviceId="([^"]+)"', req.headers.get("Authorization", ""))
    return m.group(1) if m else req.query.get("deviceId", "?")


ITEMS = [{
    "Id": f"item{i}", "Name": f"Movie {i}", "Type": "Movie", "RunTimeTicks": int(RUNTIME * 1e7),
    "MediaSources": [{"Id": f"src{i}", "Bitrate": 20_000_000, "Size": int(RUNTIME * 2.5e6),
                      "MediaStreams": [{"Type": "Video", "Codec": "hevc", "Height": 2160,
                                        "VideoRange": "HDR", "VideoRangeType": "HDR10",
                                        "RealFrameRate": 23.976, "Index": 0}]}],
} for i in range(5)]


async def ticker():
    last = time.monotonic()
    while True:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        n = len(jobs)
        if n:
            sp = min(4.0, A.capacity / n)
            for j in jobs.values():
                j["produced"] += (now - last) * sp
                j["speed"] = sp
        last = now


async def auth(req):
    return web.json_response({"AccessToken": "tok-" + dev(req), "User": {"Id": "u1", "Name": "alex"}})


async def items(req):
    return web.json_response({"Items": ITEMS})


async def playbackinfo(req):
    body = await req.json()
    iid = req.match_info["id"]
    src = dict(ITEMS[int(iid[-1])]["MediaSources"][0])
    if body.get("EnableDirectPlay"):
        src["SupportsDirectPlay"] = True
    else:
        src["TranscodingUrl"] = f"/videos/{iid}/master.m3u8?MediaSourceId={src['Id']}&VideoCodec=h264&AllowVideoStreamCopy=true"
    return web.json_response({"MediaSources": [src], "PlaySessionId": "ps-" + dev(req)})


async def master(req):
    return web.Response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000\nmain.m3u8?x=1\n")


async def main_pl(req):
    jobs[dev(req)] = {"produced": 0.0, "speed": 0}
    n = int(RUNTIME / SEG)
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for i in range(n):
        lines += [f"#EXTINF:{SEG:.6f},", f"hls1/main/{i}.ts?x=1"]
    return web.Response(text="\n".join(lines + ["#EXT-X-ENDLIST"]))


async def segment(req):
    d = dev(req)
    n = int(req.match_info["n"])
    j = jobs.get(d)
    if j is None:
        jobs[d] = j = {"produced": n * SEG, "speed": 0}
    if j["produced"] < n * SEG - 30:   # big seek forward: restart encoder there
        j["produced"] = n * SEG
    while j["produced"] < (n + 1) * SEG:
        await asyncio.sleep(0.02)
    return web.Response(body=b"\0" * 200_000)


async def stream(req):
    resp = web.StreamResponse()
    await resp.prepare(req)
    try:
        while True:
            await resp.write(b"\0" * 262144)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def sessions(req):
    return web.json_response([
        {"DeviceId": d, "TranscodingInfo": {"Framerate": 23.976 * j.get("speed", 0), "VideoCodec": "h264",
                                            "AudioCodec": "aac", "HardwareAccelerationType": "qsv",
                                            "TranscodeReasons": ["VideoCodecNotSupported"],
                                            "Width": 1920, "Height": 1080}}
        for d, j in jobs.items()])


async def kill(req):
    jobs.pop(req.query.get("deviceId", ""), None)
    return web.Response(status=204)


async def ok(req):
    return web.Response(status=204)


async def start_bg(app):
    app["t"] = asyncio.create_task(ticker())

app = web.Application()
app.on_startup.append(start_bg)
app.add_routes([
    web.post("/Users/AuthenticateByName", auth),
    web.get("/System/Info", lambda r: web.json_response({"ServerName": "mock", "Version": "0"})),
    web.get("/Items", items),
    web.post("/Items/{id}/PlaybackInfo", playbackinfo),
    web.get("/videos/{id}/master.m3u8", master),
    web.get("/videos/{id}/main.m3u8", main_pl),
    web.get("/videos/{id}/hls1/main/{n}.ts", segment),
    web.get("/Videos/{id}/stream", stream),
    web.get("/Sessions", sessions),
    web.post("/Sessions/{tail:.*}", ok),
    web.delete("/Videos/ActiveEncodings", kill),
])
web.run_app(app, port=A.port, print=None)
