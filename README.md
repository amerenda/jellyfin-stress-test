# jfstress

Load-tests a Jellyfin server from the client side. Every simulated stream is a separate Jellyfin
client with its own DeviceId and session. It asks for `PlaybackInfo` with a device profile that
forces a playback method, then plays the result like a player would: it downloads HLS segments
(or the static file) at real-time pace into a bounded buffer, and records startup time, stalls
and how fast the buffer fills. In parallel it polls `/Sessions` for what the server reports
(transcode fps, hardware acceleration type, output codec and size, transcode reasons).

## Why not an existing tool

| Tool | What it tests | Gap |
|---|---|---|
| [albarago/JF_hw_stress](https://github.com/albarago/JF_hw_stress) | Runs Jellyfin-style ffmpeg HLS transcodes locally and adds one at a time until fps < 90% of source | Bypasses Jellyfin: no API, no network, no ingress, every stream reads the same file |
| [albarago/jellyfin-transcode-test](https://github.com/albarago/jellyfin-transcode-test) ([jellyfin-meta #163](https://github.com/jellyfin/jellyfin-meta/discussions/163)) | Proposed built-in dashboard stress test for Jellyfin, using the real playback code path | Dev build of an unreleased Jellyfin; not merged |
| SpaceInvaderOne `transcoding-gpu-benchmark` | GPU/CPU encode throughput on standard test clips | Raw hardware only |
| k6 / Locust | Generic HTTP load | Don't understand HLS or player buffering |

Those tools tell you what the GPU can do. jfstress tells you what viewers will see: it runs the whole
path through Jellyfin's API, ffmpeg, the transcode directory, the k3s ingress and the network,
plays different items from different positions, and judges by stalls and real-time fill speed.

## Web UI (Locust) + Grafana

`locustfile.py` runs the same player model under [Locust](https://locust.io), which gives you a web
page to start tests from, and exports Prometheus metrics for the Grafana dashboard in
`grafana/jfstress-dashboard.json`.

```sh
uv run --with locust==2.47.0 --with prometheus_client \
  locust -f locustfile.py --class-picker --host https://jellyfin-public-test.amer.dev
# or: docker build -t jfstress . && docker run -p 8089:8089 -p 9646:9646 jfstress --host https://...
```

Open http://localhost:8089:

- **Find the max:** pick *Shape class = StepRamp*. It adds one stream every *Ramp Step Seconds*
  and stops at the first level where a viewer degrades. The result is logged (Logs tab) and
  exported as `jfstress_ramp_result_streams`. A level only counts once every viewer on it has a
  verdict, so a slow-to-show failure isn't credited to the next level.
- **Hold a fixed load:** leave *Shape class = Default*. *Number of users* is the number of concurrent streams.
- **Custom parameters:** Jellyfin login (or API key), *Playback* (transcode / remux / direct / mix),
  *Source* (4k,hdr / 1080p / hevc ...), output codec, max height, bitrate, subtitle burn-in, library,
  and the health thresholds.

Locust's own tabs show request stats (segment latency, `PLAYER startup`, and `PLAYER stall`, which
count as failures) and three generic charts. The Grafana dashboard has the graphs for finding the
breaking point:

| Section | Panels |
|---|---|
| Result | max streams from the last ramp, active streams, degraded viewers, stalls, iGPU busy |
| Load vs saturation | iGPU busy %, node CPU %, pod CPU throttling %, % of viewer time stalled, plus active streams on the right axis. The resource that flattens near 100% as the stall line lifts off zero is the bottleneck |
| Viewer experience | streams by mode/state, stalls/min, segment download time vs real time (p50/p95, 1.0 line), segment latency p50/p95/p99, startup p50/p95, buffer per viewer, server transcode speed per viewer, fill speed per viewer |
| Jellyfin server | iGPU busy, Jellyfin CPU vs limit, node CPU, node network out (`lan0`) vs bytes received, transcode disk writes, iGPU memory |

The dashboard variables are the Jellyfin namespace and the node its pod runs on. Server metrics come from
node-exporter (`node_drm_gpu_busy_percent` for the AMD iGPU), cAdvisor and kube-state-metrics.
Prometheus must scrape the Locust process on `:9646/metrics` (job name doesn't matter).

## CLI

`jfstress.py` is the same test without a UI: a live table in the terminal and a JSON report.
Needs [uv](https://docs.astral.sh/uv/). Dependencies (httpx, rich) are declared inline in the script.

```sh
export JF_PASSWORD=...            # or JF_API_KEY=... with --api-key
./jfstress.py --server https://jellyfin-public-test.amer.dev --username alex \
  --ramp --mix transcode --filter 4k,hdr --out ramp-4k-hdr.json
```

Use a test instance or a quiet time. A ramp run deliberately pushes the server until playback degrades.

### Modes (`--mix`)

- `direct`: direct play. Fetches `/Videos/{id}/stream?static=true` paced at the file's bitrate. Measures disk and network, not CPU.
- `remux`: direct stream to HLS with stream copy. Cheap for the CPU, but goes through ffmpeg and the transcode directory.
- `transcode`: full video and audio transcode to `--codec` (h264/hevc/av1), at most `--max-height` and `--bitrate`. Pick heavy sources with `--filter 4k,hdr` (adds tone mapping) and `--burn-subs` (subtitle burn-in).

Combine them: `--mix transcode=3,direct=1`. In fixed mode the pattern is repeated until there are
`--streams` streams. In ramp mode it sets the order in which streams are added.

### Two ways to run

- **Fixed**: `--streams 6 --duration 600`. Holds 6 streams for 10 minutes of playback each, then reports.
- **Ramp**: `--ramp --interval 45 --max-streams 20`. Adds one stream at a time and holds each level for
  `--interval` seconds. It stops at the first level where any stream stalls, errors, takes more than
  `--max-startup` to start, or fills slower than `--min-speed`× real time for `--grace` seconds while
  its buffer is not full. The result is the last level that held. A stream that reaches `--duration` is
  replaced so the load stays constant.

### Useful scenarios

```sh
# The heavy case: 4K HDR -> 1080p h264 with tone mapping
--ramp --mix transcode --filter 4k,hdr
# Typical: 1080p h264 sources transcoded down to 720p 4 Mbps (remote viewers)
--ramp --mix transcode --filter 1080p --max-height 720 --bitrate 4000000
# Realistic household mix
--streams 8 --mix transcode=2,remux=2,direct=4 --duration 900
# PGS subtitle burn-in
--ramp --mix transcode --burn-subs
# HEVC output instead of h264
--ramp --mix transcode --codec hevc
```

## Reading the output

| Column | Meaning |
|---|---|
| `buf s` | Media seconds buffered ahead of the simulated playhead (ceiling `--max-buffer`, default 30) |
| `fill x` | Median of the last 5 segments: media seconds per wall second. At the live edge of a transcode this is the transcode speed; below 1.0 means the viewer will eventually stall. Shows ≥9 while serving from an already-encoded buffer |
| `startup` | Seconds from request until `--startup-buffer` seconds are buffered (time to first frame) |
| `stalls` | Times the buffer ran dry, plus total stalled seconds |
| `srv fps` | ffmpeg fps reported by Jellyfin, with speed relative to the source fps |
| `hw` / `out` | Hardware acceleration Jellyfin used (`none` means a software transcode) and the output codecs and size |

`--out` writes a JSON report with per-stream results and the event log.

While a run is going, watch the server side as well: the Jellyfin Grafana dashboard, node CPU
for the gmktec node running the pod, iGPU busy (`intel_gpu_top` or `radeontop` on the node), and
pod CPU throttling. The jellyfin-public pod has a CPU limit of 6, which caps software
transcodes and tone mapping that falls back to the CPU.

## Caveats

- If Jellyfin's "Throttle transcodes" setting is on, ffmpeg is paused once a stream is far enough
  ahead, so `srv fps` drops for healthy streams. The health check only uses fill speed while the
  buffer is not full, so this does not cause false failures.
- The client's own NIC can become the limit with many high-bitrate direct streams
  (10 × 80 Mbps 4K remuxes is 800 Mbps). Check the header's Mbit/s.
- With `--username`, each stream logs in as its own device and logs out at the end, which removes the
  device. Ctrl-C stops every stream, reports playback stopped and kills its encode
  (`DELETE /Videos/ActiveEncodings`).

## Tests

`tests/mock_jellyfin.py` is a fake Jellyfin with a fixed shared transcode capacity, used to
check the player model and the ramp logic offline:

```sh
uv run --script tests/mock_jellyfin.py --capacity 3.5 &
./jfstress.py --server http://127.0.0.1:18096 --username x --password x \
  --ramp --interval 10 --grace 4   # expect: 3 streams held, degraded at 4
uv run --with locust==2.47.0 --with prometheus_client locust -f locustfile.py --headless \
  --host http://127.0.0.1:18096 --jf-username x --jf-password x \
  --ramp-step-seconds 10 --grace 4  # same result via StepRamp; metrics on :9646
```

`jfcore.py` holds the shared parts (device profiles, HLS parsing, item filters, player buffer model).
