#!/usr/bin/env python3
"""jfstress web UI.

Serves a form with every jfstress option (read from jfstress's own argparse parser, so the
two cannot drift) and runs each test as a Kubernetes Job in this pod's namespace. The Job
runs `jfstress.py --jsonl`; the UI reads its pod log for live status and the final report.
A graceful stop sets a pod annotation that the runner sees through a downward API file.

Config (env):
  NAMESPACE            namespace for runs (downward API)
  RUNNER_IMAGE         image for run pods (normally this pod's image)
  JF_SECRET_NAME/KEY   Secret holding the Jellyfin password (env JF_PASSWORD in the runner)
  UI_DEFAULTS          JSON: default form values, e.g. {"server": "...", "username": "alex"}
  SERVER_PRESETS       JSON list of server URLs offered in the form
  NODE_SELECTOR        JSON object: default nodeSelector for run pods
  TOLERATIONS          JSON list: default tolerations for run pods
  RUNNER_RESOURCES     JSON: container resources for run pods
  JOB_TTL_SECONDS      ttlSecondsAfterFinished for run Jobs (history length)
  MAX_RUN_SECONDS      default activeDeadlineSeconds for a run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shlex
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import jfcompat  # noqa: E402
import jfstress  # noqa: E402

GROUP = "jfstress.amer.dev"
RUN_LABELS = {"app.kubernetes.io/name": "jfstress-run", "app.kubernetes.io/managed-by": "jfstress-ui"}
RUN_SELECTOR = ",".join(f"{k}={v}" for k, v in RUN_LABELS.items())
STOP_ANN = f"{GROUP}/stop"
STOP_FILE = "/podinfo/stop"
# Options the UI sets itself or must not take from a browser.
HIDDEN = {"help", "password", "api_key", "out", "jsonl", "status_interval", "stop_file"}
GROUP_TITLES = {"options": "other"}

# Pre-canned tests. Fields not listed reset to the CLI defaults (server and username are kept).
# Notes are for the gmktec nodes: Ryzen 7 7730U, Radeon Vega 8 (VCN 2: H.264/HEVC/VP9 decode,
# H.264/HEVC encode, no AV1 at all, and no GPU decodes 10-bit H.264), and for the encoding
# settings of dean-tofu modules/jellyfin (tone mapping off, AV1 encoding off).
LAN = 200_000_000  # bitrate cap high enough that remux/direct are never refused for bitrate
PRESETS = [
    {"group": "GPU transcode", "name": "Baseline 1080p", "values": {
        "ramp": True, "filter": "1080p,h264,sdr"},
     "note": "1080p H.264 to 1080p H.264, all on the GPU. The cheapest real transcode: the ceiling."},
    {"group": "GPU transcode", "name": "Remote 720p 4 Mbps", "values": {
        "ramp": True, "filter": "1080p,sdr", "max_height": 720, "bitrate": 4_000_000},
     "note": "What a remote viewer on a capped connection costs."},
    {"group": "GPU transcode", "name": "HEVC output", "values": {
        "ramp": True, "filter": "1080p,sdr", "codec": "hevc"},
     "note": "Same sources as the baseline, HEVC encoder instead of H.264."},
    {"group": "GPU transcode", "name": "4K HDR to 1080p", "values": {
        "ramp": True, "filter": "4k,hdr"},
     "note": "4K 10-bit HEVC decode and downscale to 1080p H.264 on the iGPU. Tone mapping is off in "
             "Jellyfin, so this measures decode and scaling only."},
    {"group": "GPU transcode", "name": "Dolby Vision", "values": {
        "ramp": True, "filter": "dovi"},
     "note": "Dolby Vision sources (some profile 7 with an enhancement layer) transcoded to 1080p H.264."},
    {"group": "GPU transcode", "name": "4K HDR, one file", "values": {
        "ramp": True, "filter": "4k,hdr", "same_item": True},
     "note": "Every stream plays the same 4K HDR file from a different spot: a repeatable number."},
    {"group": "CPU fallback", "name": "AV1 source", "values": {
        "ramp": True, "filter": "av1", "max_streams": 10},
     "note": "Vega 8 has no AV1 decoder, so every stream decodes AV1 in software (dav1d) on the CPU."},
    {"group": "CPU fallback", "name": "10-bit H.264 anime", "values": {
        "ramp": True, "filter": "hi10p", "max_streams": 10},
     "note": "No GPU decodes 10-bit H.264 (Hi10P): software decode on the CPU."},
    {"group": "Subtitle burn-in", "name": "ASS (anime)", "values": {
        "ramp": True, "filter": "ass", "burn_subs": True, "max_startup": 90},
     "note": "Styled ASS rendered by libass on the CPU. First play of a file extracts every subtitle "
             "track and font first, hence the 90 s startup limit."},
    {"group": "Subtitle burn-in", "name": "PGS on 4K HDR", "values": {
        "ramp": True, "filter": "4k,hdr,pgs", "burn_subs": True, "max_startup": 60},
     "note": "The worst common case: 4K HDR decode and downscale plus a Blu-ray image subtitle overlay."},
    {"group": "Subtitle burn-in", "name": "DVD subs", "values": {
        "ramp": True, "filter": "dvdsub", "burn_subs": True, "max_startup": 60},
     "note": "DVD bitmap subtitles overlaid on the video."},
    {"group": "Subtitle burn-in", "name": "SRT 1080p", "values": {
        "ramp": True, "filter": "1080p,srt", "burn_subs": True},
     "note": "Plain text subtitles: the cheapest burn-in, compare with the baseline."},
    {"group": "Throughput", "name": "High-bitrate remux", "values": {
        "ramp": True, "mix": "remux", "filter": "highbitrate", "bitrate": LAN},
     "note": "Files over 40 Mbps remuxed to HLS: disk, NFS and network, almost no GPU."},
    {"group": "Throughput", "name": "4K direct play", "values": {
        "ramp": True, "mix": "direct", "filter": "4k", "bitrate": LAN},
     "note": "4K files streamed as-is at real-time pace: storage and network only."},
    {"group": "Realistic", "name": "Random library", "values": {
        "ramp": True, "mix": "transcode"},
     "note": "Transcode ramp over random files: what an average stream costs."},
    {"group": "Realistic", "name": "Household mix (8)", "values": {
        "streams": 8, "mix": "transcode=2,remux=2,direct=4", "duration": 900, "bitrate": 60_000_000},
     "note": "8 viewers for 15 minutes: 2 transcodes, 2 remuxes, 4 direct plays."},
    {"group": "Realistic", "name": "Heavy mix ramp", "values": {
        "ramp": True, "mix": "transcode=3,remux=1", "filter": "4k", "burn_subs": True,
        "max_startup": 60, "bitrate": 20_000_000},
     "note": "4K sources, 3 of 4 transcoded with subtitles burned in, 1 remuxed."},
]


def env_json(name: str, default):
    raw = os.environ.get(name)
    return json.loads(raw) if raw else default


class Config:
    def __init__(self) -> None:
        self.namespace = os.environ.get("NAMESPACE", "jellyfin-stress")
        self.image = os.environ.get("RUNNER_IMAGE", "")
        self.secret_name = os.environ.get("JF_SECRET_NAME", "jellyfin-stress-jellyfin")
        self.secret_key = os.environ.get("JF_SECRET_KEY", "password")
        self.defaults = env_json("UI_DEFAULTS", {})
        self.presets = env_json("SERVER_PRESETS", [])
        self.node_selector = env_json("NODE_SELECTOR", {})
        self.tolerations = env_json("TOLERATIONS", [])
        self.resources = env_json("RUNNER_RESOURCES", {})
        self.ttl = int(os.environ.get("JOB_TTL_SECONDS", str(7 * 86400)))
        self.max_run = int(os.environ.get("MAX_RUN_SECONDS", str(3 * 3600)))


# ---------- options <-> argv ----------

def option_spec(defaults: dict | None = None) -> list[dict]:
    """The jfstress CLI options as form fields, grouped like `jfstress.py --help`."""
    p = jfstress.build_parser()
    negs = {a.dest: max(a.option_strings, key=len) for a in p._actions
            if isinstance(a, argparse._StoreFalseAction)}
    groups = []
    for g in p._action_groups:
        fields = []
        for a in g._group_actions:
            if a.dest in HIDDEN or not a.option_strings or isinstance(a, argparse._StoreFalseAction):
                continue
            f = {"dest": a.dest, "flag": max(a.option_strings, key=len), "help": a.help or "",
                 "default": a.default, "cli_default": a.default, "required": bool(a.required)}
            if isinstance(a, argparse._StoreTrueAction):
                f["kind"] = "bool"
                if a.dest in negs:
                    f["off_flag"] = negs[a.dest]
            elif isinstance(a, argparse._AppendAction):
                f["kind"], f["default"], f["cli_default"] = "list", [], None
            elif a.choices:
                f["kind"], f["choices"] = "choice", list(a.choices)
            elif a.type is int:
                f["kind"] = "int"
            elif a.type is float:
                f["kind"] = "float"
            else:
                f["kind"] = "text"
            if defaults and a.dest in defaults:
                f["default"] = defaults[a.dest]
            fields.append(f)
        if fields:
            groups.append({"title": GROUP_TITLES.get(g.title, g.title), "fields": fields})
    return [g for g in groups if g["title"] != "other"] + [g for g in groups if g["title"] == "other"]


def _same_as_cli_default(f: dict, raw: str) -> bool:
    d = f["cli_default"]
    if d is None:
        return False
    try:
        conv = {"int": int, "float": float}.get(f["kind"], str)
        return conv(raw) == d
    except ValueError:
        return False


def build_argv(form: dict, spec: list[dict]) -> list[str]:
    argv: list[str] = []
    for g in spec:
        for f in g["fields"]:
            v = form.get(f["dest"])
            if f["kind"] == "bool":
                on = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
                if v is None:
                    on = bool(f["default"])
                if on and not f["default"]:
                    argv.append(f["flag"])
                elif not on and f["default"] and f.get("off_flag"):
                    argv.append(f["off_flag"])
            elif f["kind"] == "list":
                items = v if isinstance(v, list) else str(v or "").splitlines()
                argv += [f"{f['flag']}={s}" for s in (str(x).strip() for x in items) if s]
            elif v is not None and str(v).strip() != "" and not _same_as_cli_default(f, str(v).strip()):
                argv.append(f"{f['flag']}={str(v).strip()}")
    return argv


def validate_argv(argv: list[str]) -> argparse.Namespace:
    p = jfstress.build_parser()

    def fail(msg):
        raise ValueError(msg)

    p.error = fail
    return jfstress.parse_args(argv + ["--jsonl"], parser=p)


def parse_pod(pod: dict, cfg: Config) -> dict:
    def load(key, default, typ):
        raw = pod.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        val = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(val, typ):
            raise ValueError(f"{key} must be a JSON {typ.__name__}")
        return val

    ns = load("node_selector", cfg.node_selector, dict)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in ns.items()):
        raise ValueError("node_selector values must be strings")
    tol = load("tolerations", cfg.tolerations, list)
    if not all(isinstance(t, dict) for t in tol):
        raise ValueError("tolerations must be a list of objects")
    max_run = int(pod.get("max_run_seconds") or cfg.max_run)
    if max_run < 60:
        raise ValueError("max_run_seconds must be at least 60")
    return {"node_selector": ns, "tolerations": tol, "max_run_seconds": max_run}


def job_manifest(name: str, argv: list[str], pod: dict, form: dict, cfg: Config,
                 script: str = "/app/jfstress.py", kind: str = "stress") -> dict:
    labels = dict(RUN_LABELS)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name, "namespace": cfg.namespace, "labels": labels,
            "annotations": {f"{GROUP}/kind": kind,
                            f"{GROUP}/form": json.dumps(form, sort_keys=True),
                            f"{GROUP}/argv": json.dumps(argv),
                            f"{GROUP}/pod": json.dumps(pod, sort_keys=True)},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": cfg.ttl,
            "activeDeadlineSeconds": pod["max_run_seconds"],
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 60,
                    "nodeSelector": pod["node_selector"],
                    "tolerations": pod["tolerations"],
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "jfstress",
                        "image": cfg.image,
                        "command": ["python", "-u", script],
                        "args": argv + ["--jsonl", f"--stop-file={STOP_FILE}"],
                        "env": [{"name": "JF_PASSWORD", "valueFrom": {"secretKeyRef": {
                            "name": cfg.secret_name, "key": cfg.secret_key}}}],
                        "resources": cfg.resources,
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "podinfo", "mountPath": "/podinfo", "readOnly": True}],
                    }],
                    "volumes": [{"name": "podinfo", "downwardAPI": {"items": [{
                        "path": "stop",
                        "fieldRef": {"fieldPath": f"metadata.annotations['{STOP_ANN}']"}}]}}],
                },
            },
        },
    }


def command_line(argv: list[str], script: str = "jfstress.py") -> str:
    return f"JF_PASSWORD=... ./{script} " + " ".join(shlex.quote(a) for a in argv)


def compat_argv(body: dict, defaults: dict) -> tuple[list[str], dict]:
    """argv for jfcompat.py from the report form, checked by its own parser."""
    form = {"server": (body.get("server") or defaults.get("server") or "").strip(),
            "username": (body.get("username") or defaults.get("username") or "").strip(),
            "subtitles": body.get("subtitles", True) not in (False, "false", "off"),
            "concurrency": str(body.get("concurrency") or "8").strip(),
            "library": (body.get("library") or "").strip(),
            "types": (body.get("types") or "Movie,Episode").strip(),
            "devices": [d for d in body.get("devices") or [] if d],
            "limit": str(body.get("limit") or "").strip()}
    if not form["server"] or not form["username"]:
        raise ValueError("server and username are required")
    argv = [f"--server={form['server']}", f"--username={form['username']}", f"--types={form['types']}",
            f"--concurrency={form['concurrency']}"]
    if not form["subtitles"]:
        argv.append("--no-subtitles")
    if form["library"]:
        argv.append(f"--library={form['library']}")
    if form["limit"]:
        argv.append(f"--limit={form['limit']}")
    argv += [f"--device={d}" for d in form["devices"]]
    p = jfcompat.build_parser()

    def fail(msg):
        raise ValueError(msg)

    p.error = fail
    a = p.parse_args(argv)
    if not 1 <= a.concurrency <= 32:
        raise ValueError("concurrency must be 1-32")
    return argv, form


# ---------- Kubernetes ----------

class KubeError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class Kube:
    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, base: str | None = None):
        self.base = base or os.environ.get("KUBE_API", "https://kubernetes.default.svc")
        ca = f"{self.SA}/ca.crt"
        verify = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else True
        self.http = httpx.Client(verify=verify, timeout=20)

    def _headers(self, extra: dict | None = None) -> dict:
        h = dict(extra or {})
        try:
            with open(f"{self.SA}/token") as f:  # projected token rotates: read every call
                h["Authorization"] = "Bearer " + f.read().strip()
        except OSError:
            pass
        return h

    def req(self, method: str, path: str, *, headers=None, **kw) -> httpx.Response:
        r = self.http.request(method, self.base + path, headers=self._headers(headers), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise KubeError(r.status_code, msg[:500])
        return r

    def patch(self, path: str, body: dict) -> dict:
        return self.req("PATCH", path, headers={"Content-Type": "application/merge-patch+json"},
                        content=json.dumps(body)).json()


class Runs:
    def __init__(self, cfg: Config, kube: Kube):
        self.cfg, self.kube = cfg, kube
        ns = cfg.namespace
        self.jobs = f"/apis/batch/v1/namespaces/{ns}/jobs"
        self.pods = f"/api/v1/namespaces/{ns}/pods"
        self.events = f"/api/v1/namespaces/{ns}/events"

    def _pods_by_job(self) -> dict[str, dict]:
        items = self.kube.req("GET", self.pods, params={"labelSelector": RUN_SELECTOR}).json()["items"]
        out: dict[str, dict] = {}
        for p in sorted(items, key=lambda p: p["metadata"]["creationTimestamp"]):
            out[p["metadata"].get("labels", {}).get("job-name", "")] = p
        return out

    def _pod_for(self, name: str) -> dict | None:
        items = self.kube.req("GET", self.pods, params={"labelSelector": f"job-name={name}"}).json()["items"]
        return max(items, key=lambda p: p["metadata"]["creationTimestamp"]) if items else None

    @staticmethod
    def state(job: dict, pod: dict | None) -> tuple[str, str]:
        st = job.get("status", {})
        conds = {c["type"]: c for c in st.get("conditions", []) if c.get("status") == "True"}
        if "Complete" in conds or st.get("succeeded"):
            return "succeeded", ""
        if "Failed" in conds or st.get("failed"):
            c = conds.get("Failed", {})
            return "failed", c.get("reason", "") + (": " + c["message"] if c.get("message") else "")
        if job["spec"].get("suspend"):
            return "suspended", ""
        if pod is None:
            return "starting", ""
        stopping = (pod["metadata"].get("annotations") or {}).get(STOP_ANN) == "true"
        phase = pod.get("status", {}).get("phase", "")
        if phase == "Pending":
            why = ""
            for c in pod["status"].get("conditions", []):
                if c.get("type") == "PodScheduled" and c.get("status") == "False":
                    why = c.get("message", c.get("reason", ""))
            for cs in pod["status"].get("containerStatuses", []):
                w = cs.get("state", {}).get("waiting")
                if w:
                    why = w.get("reason", "") + (": " + w["message"] if w.get("message") else "")
            return "pending", why
        if phase == "Running":
            return ("stopping" if stopping else "running"), ""
        return phase.lower() or "unknown", ""

    def _log_lines(self, pod: dict, **params) -> list[str]:
        try:
            r = self.kube.req("GET", f"{self.pods}/{pod['metadata']['name']}/log",
                              params={"container": "jfstress", **params})
        except KubeError:
            return []
        return r.text.splitlines()

    @staticmethod
    def parse_lines(lines: list[str]) -> dict:
        status, report, events, other = None, None, [], []
        for ln in lines:
            obj = None
            if ln.startswith("{"):
                try:
                    obj = json.loads(ln)
                except ValueError:
                    obj = None
            if not isinstance(obj, dict):
                if ln.strip():
                    other.append(ln)
                continue
            t = obj.get("type")
            if t == "status":
                status = obj
            elif t == "event":
                events.append(obj)
            elif t == "report":
                report = obj.get("report")
        return {"status": status, "report": report, "events": events[-200:], "other": other[-60:]}

    @staticmethod
    def result_summary(rep: dict) -> dict:
        if rep.get("type") == "compat":
            out = {"kind": "compat", "files": len(rep.get("items") or []), "transcode_pct": {}}
            for d in rep.get("devices") or []:
                c = rep["summary"][d["id"]]["no_subs"]
                n = sum(c.values()) or 1
                out["transcode_pct"][d["id"]] = round(100 * c["transcode"] / n)
            return out
        return {k: rep.get(k) for k in ("ramp_result_streams", "ramp_reason", "streams_total", "errors",
                                        "stalls_total", "stall_seconds_total", "startup_p50_s",
                                        "startup_max_s", "stopped_by_user", "duration_s")}

    def _summary(self, job: dict, pod: dict | None) -> dict:
        ann = job["metadata"].get("annotations") or {}
        state, why = self.state(job, pod)
        form = json.loads(ann.get(f"{GROUP}/form", "{}"))
        result = json.loads(ann[f"{GROUP}/result"]) if f"{GROUP}/result" in ann else None
        if result is None and state in ("succeeded", "failed") and pod is not None:
            rep = self.parse_lines(self._log_lines(pod, tailLines=3))["report"]
            if rep:
                result = self.result_summary(rep)
                try:  # cache, so finished runs keep their result without re-reading logs
                    self.kube.patch(f"{self.jobs}/{job['metadata']['name']}",
                                    {"metadata": {"annotations": {f"{GROUP}/result": json.dumps(result)}}})
                except KubeError:
                    pass
        return {
            "name": job["metadata"]["name"], "kind": ann.get(f"{GROUP}/kind", "stress"),
            "created": job["metadata"]["creationTimestamp"],
            "finished": job.get("status", {}).get("completionTime"),
            "state": state, "reason": why,
            "node": (pod or {}).get("spec", {}).get("nodeName"),
            "server": form.get("server"),
            "mode": "compat" if ann.get(f"{GROUP}/kind") == "compat"
            else "ramp" if form.get("ramp") in (True, "true", "on") else "fixed",
            "mix": form.get("mix"), "streams": form.get("streams"), "filter": form.get("filter"),
            "result": result,
        }

    def list(self) -> list[dict]:
        jobs = self.kube.req("GET", self.jobs, params={"labelSelector": RUN_SELECTOR}).json()["items"]
        pods = self._pods_by_job()
        jobs.sort(key=lambda j: j["metadata"]["creationTimestamp"], reverse=True)
        return [self._summary(j, pods.get(j["metadata"]["name"])) for j in jobs]

    def get(self, name: str) -> dict:
        job = self.kube.req("GET", f"{self.jobs}/{name}").json()
        if not _is_run(job):
            raise KubeError(404, "not a jfstress run")
        pod = self._pod_for(name)
        out = self._summary(job, pod)
        ann = job["metadata"].get("annotations") or {}
        out["form"] = json.loads(ann.get(f"{GROUP}/form", "{}"))
        out["pod"] = json.loads(ann.get(f"{GROUP}/pod", "{}"))
        out["command"] = command_line(json.loads(ann.get(f"{GROUP}/argv", "[]")),
                                      "jfcompat.py" if out["kind"] == "compat" else "jfstress.py")
        out["live"] = self.parse_lines(self._log_lines(pod, tailLines=400)) if pod else None
        if out["kind"] == "compat" and out["live"] and out["live"].get("report"):
            out["live"]["report"] = None  # large; the report page fetches it
            out["has_report"] = True
        if out["state"] in ("pending", "starting") and pod:
            evs = self.kube.req("GET", self.events, params={
                "fieldSelector": f"involvedObject.name={pod['metadata']['name']}"}).json()["items"]
            out["pod_events"] = [f"{e.get('type')} {e.get('reason')}: {e.get('message')}" for e in evs[-10:]]
        return out

    def create(self, body: dict) -> dict:
        form = body.get("form") or {}
        spec = option_spec(self.cfg.defaults)
        argv = build_argv(form, spec)
        validate_argv(argv)
        pod = parse_pod(body.get("pod") or {}, self.cfg)
        if not body.get("allow_concurrent"):
            self._refuse_if_busy()
        name = self._new_name("jfstress")
        self.kube.req("POST", self.jobs, json=job_manifest(name, argv, pod, form, self.cfg))
        return {"name": name, "command": command_line(argv)}

    def create_compat(self, body: dict) -> dict:
        argv, form = compat_argv(body, self.cfg.defaults)
        pod = parse_pod(body.get("pod") or {}, self.cfg)
        if not body.get("allow_concurrent"):
            self._refuse_if_busy()
        name = self._new_name("jfcompat")
        self.kube.req("POST", self.jobs, json=job_manifest(name, argv, pod, form, self.cfg,
                                                            script="/app/jfcompat.py", kind="compat"))
        return {"name": name, "command": command_line(argv, "jfcompat.py")}

    def _refuse_if_busy(self) -> None:
        busy = [r["name"] for r in self.list() if r["state"] in ("starting", "pending", "running", "stopping")]
        if busy:
            raise KubeError(409, f"run {busy[0]} is still active; stop it first or allow concurrent runs")

    @staticmethod
    def _new_name(prefix: str) -> str:
        return f"{prefix}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

    def stop(self, name: str) -> None:
        self._own(name)
        pod = self._pod_for(name)
        if pod is None:
            raise KubeError(409, "run has no pod yet; delete it instead")
        self.kube.patch(f"{self.pods}/{pod['metadata']['name']}", {"metadata": {"annotations": {STOP_ANN: "true"}}})

    def delete(self, name: str) -> None:
        self._own(name)
        self.kube.req("DELETE", f"{self.jobs}/{name}", params={"propagationPolicy": "Background"})

    def report(self, name: str) -> dict | None:
        self._own(name)
        pod = self._pod_for(name)
        return self.parse_lines(self._log_lines(pod, tailLines=3))["report"] if pod else None

    def log(self, name: str) -> str:
        self._own(name)
        pod = self._pod_for(name)
        return "\n".join(self._log_lines(pod, limitBytes=20_000_000)) if pod else ""

    def _own(self, name: str) -> None:
        if not _is_run(self.kube.req("GET", f"{self.jobs}/{name}").json()):
            raise KubeError(404, "not a jfstress run")


def _is_run(job: dict) -> bool:
    labels = job["metadata"].get("labels") or {}
    return all(labels.get(k) == v for k, v in RUN_LABELS.items())


# ---------- HTTP ----------

NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    runs: Runs
    server_version = "jfstress-ui"

    def log_message(self, fmt, *args):  # quieter: no probe noise
        if not self.path.startswith("/healthz"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise KubeError(415, "send application/json")
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _route(self, method: str):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if method == "GET" and not parts:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and len(parts) == 2 and parts[0] == "compat" and set(parts[1]) <= NAME_OK:
                with open(os.path.join(HERE, "compat.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if parts == ["healthz"]:
                return self._send(200, "ok", "text/plain")
            if parts[:1] != ["api"]:
                return self._send(404, {"error": "not found"})
            rest = parts[1:]
            if method == "GET" and rest == ["options"]:
                return self._send(200, {
                    "groups": option_spec(self.cfg.defaults), "server_presets": self.cfg.presets,
                    "presets": PRESETS,
                    "devices": [{"id": d.id, "name": d.name, "app": d.app} for d in jfcompat.DEVICES],
                    "pod": {"node_selector": self.cfg.node_selector, "tolerations": self.cfg.tolerations,
                            "max_run_seconds": self.cfg.max_run},
                    "namespace": self.cfg.namespace, "ttl_seconds": self.cfg.ttl})
            if method == "POST" and rest == ["preview"]:
                body = self._body()
                argv = build_argv(body.get("form") or {}, option_spec(self.cfg.defaults))
                try:
                    validate_argv(argv)
                    err = None
                except ValueError as e:
                    err = str(e)
                return self._send(200, {"command": command_line(argv), "error": err})
            if method == "POST" and rest == ["compat"]:
                return self._send(201, self.runs.create_compat(self._body()))
            if rest == ["runs"]:
                if method == "GET":
                    return self._send(200, {"runs": self.runs.list()})
                if method == "POST":
                    return self._send(201, self.runs.create(self._body()))
            if len(rest) >= 2 and rest[0] == "runs" and set(rest[1]) <= NAME_OK:
                name, tail = rest[1], rest[2:]
                if method == "GET" and not tail:
                    return self._send(200, self.runs.get(name))
                if method == "DELETE" and not tail:
                    self.runs.delete(name)
                    return self._send(200, {"deleted": name})
                if method == "POST" and tail == ["stop"]:
                    self._body()
                    self.runs.stop(name)
                    return self._send(200, {"stopping": name})
                if method == "GET" and tail == ["report.json"]:
                    rep = self.runs.report(name)
                    if rep is None:
                        return self._send(404, {"error": "no report (yet)"})
                    return self._send(200, json.dumps(rep, indent=2), headers={
                        "Content-Disposition": f'attachment; filename="{name}.json"'})
                if method == "GET" and tail == ["log.txt"]:
                    return self._send(200, self.runs.log(name), "text/plain; charset=utf-8", {
                        "Content-Disposition": f'attachment; filename="{name}.log"'})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except KubeError as e:
            return self._send(e.status if 400 <= e.status < 600 else 502, {"error": str(e)})
        except httpx.HTTPError as e:
            return self._send(502, {"error": f"kubernetes API: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main() -> None:
    cfg = Config()
    Handler.cfg = cfg
    Handler.runs = Runs(cfg, Kube())
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080"))
    print(f"jfstress-ui on {host}:{port}, namespace {cfg.namespace}, image {cfg.image}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
