"""Unit tests for webui/app.py (no cluster needed): python -m unittest discover -s tests"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "webui"))
sys.path.insert(0, ROOT)

import app  # noqa: E402
import jfstress  # noqa: E402


def defaults_form(spec):
    return {f["dest"]: f["default"] for g in spec for f in g["fields"]}


class OptionSpec(unittest.TestCase):
    def setUp(self):
        self.spec = app.option_spec({"server": "https://jf.example", "username": "alex"})
        self.fields = {f["dest"]: f for g in self.spec for f in g["fields"]}

    def test_every_cli_option_is_exposed_except_hidden(self):
        p = jfstress.build_parser()
        dests = {a.dest for a in p._actions if a.option_strings} - app.HIDDEN
        self.assertEqual(dests, set(self.fields))

    def test_kinds(self):
        self.assertEqual(self.fields["ramp"]["kind"], "bool")
        self.assertEqual(self.fields["codec"]["kind"], "choice")
        self.assertEqual(self.fields["param"]["kind"], "list")
        self.assertEqual(self.fields["streams"]["kind"], "int")
        self.assertEqual(self.fields["min_speed"]["kind"], "float")
        self.assertEqual(self.fields["random_start"]["off_flag"], "--from-start")
        self.assertEqual(self.fields["server"]["default"], "https://jf.example")

    def test_defaults_give_minimal_valid_argv(self):
        argv = app.build_argv(defaults_form(self.spec), self.spec)
        a = app.validate_argv(argv)
        self.assertEqual(a.server, "https://jf.example")
        self.assertTrue(a.random_start)
        self.assertNotIn("--from-start", argv)
        self.assertNotIn("--ramp", argv)

    def test_bools_lists_and_negation(self):
        form = defaults_form(self.spec) | {"ramp": True, "random_start": False,
                                           "param": ["VideoBitrate=4000000", " "], "item": "a\nb"}
        argv = app.build_argv(form, self.spec)
        a = app.validate_argv(argv)
        self.assertTrue(a.ramp)
        self.assertFalse(a.random_start)
        self.assertEqual(a.param, ["VideoBitrate=4000000"])
        self.assertEqual(a.item, ["a", "b"])

    def test_values_starting_with_dash_stay_values(self):
        form = defaults_form(self.spec) | {"library": "-weird"}
        self.assertEqual(app.validate_argv(app.build_argv(form, self.spec)).library, "-weird")

    def test_invalid_values_raise_valueerror(self):
        for bad in ({"streams": "x"}, {"codec": "mpeg2"}, {"mix": "bogus=1"}, {"filter": "8k"},
                    {"server": ""}, {"username": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.validate_argv(app.build_argv(defaults_form(self.spec) | bad, self.spec))


class PodAndJob(unittest.TestCase):
    def setUp(self):
        os.environ.update({
            "NODE_SELECTOR": json.dumps({"kubernetes.io/hostname": "archlinux"}),
            "TOLERATIONS": json.dumps([{"key": "gpu-worker", "operator": "Equal", "value": "true",
                                        "effect": "NoSchedule"}]),
            "RUNNER_IMAGE": "img@sha256:abc", "NAMESPACE": "ns1"})
        self.cfg = app.Config()

    def test_pod_defaults_and_overrides(self):
        pod = app.parse_pod({}, self.cfg)
        self.assertEqual(pod["node_selector"], {"kubernetes.io/hostname": "archlinux"})
        self.assertEqual(pod["tolerations"][0]["key"], "gpu-worker")
        pod = app.parse_pod({"node_selector": '{"a": "b"}', "tolerations": "[]", "max_run_seconds": "600"}, self.cfg)
        self.assertEqual((pod["node_selector"], pod["tolerations"], pod["max_run_seconds"]), ({"a": "b"}, [], 600))
        for bad in ({"node_selector": "[]"}, {"node_selector": '{"a": 1}'}, {"tolerations": "{}"},
                    {"tolerations": "not json"}, {"max_run_seconds": "5"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.parse_pod(bad, self.cfg)

    def test_job_manifest(self):
        pod = app.parse_pod({}, self.cfg)
        job = app.job_manifest("jfstress-x", ["--server=s", "--username=u"], pod, {"server": "s"}, self.cfg)
        spec = job["spec"]["template"]["spec"]
        c = spec["containers"][0]
        self.assertEqual(job["metadata"]["namespace"], "ns1")
        self.assertEqual(c["image"], "img@sha256:abc")
        self.assertEqual(c["args"][-2:], ["--jsonl", "--stop-file=/podinfo/stop"])
        self.assertEqual(c["env"][0]["valueFrom"]["secretKeyRef"]["name"], "jellyfin-stress-jellyfin")
        self.assertEqual(spec["nodeSelector"], {"kubernetes.io/hostname": "archlinux"})
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        item = spec["volumes"][0]["downwardAPI"]["items"][0]
        self.assertEqual(item["fieldRef"]["fieldPath"], "metadata.annotations['jfstress.amer.dev/stop']")
        self.assertTrue(app._is_run(job))


class LogParsing(unittest.TestCase):
    def test_parse_lines(self):
        lines = ['{"type":"event","t":1,"msg":"a"}', 'Traceback (most recent call last):',
                 '{"type":"status","t":5,"streams":[]}', '{"type":"status","t":10,"streams":[]}',
                 '{"type":"report","report":{"ramp_result_streams":3}}', "{not json"]
        out = app.Runs.parse_lines(lines)
        self.assertEqual(out["status"]["t"], 10)
        self.assertEqual(out["report"]["ramp_result_streams"], 3)
        self.assertEqual([e["msg"] for e in out["events"]], ["a"])
        self.assertEqual(out["other"], ["Traceback (most recent call last):", "{not json"])

    def test_state(self):
        job = {"spec": {}, "status": {"conditions": [{"type": "Failed", "status": "True",
                                                      "reason": "DeadlineExceeded"}]}}
        self.assertEqual(app.Runs.state(job, None)[0], "failed")
        pod = {"metadata": {"annotations": {app.STOP_ANN: "true"}}, "status": {"phase": "Running"}}
        self.assertEqual(app.Runs.state({"spec": {}, "status": {"active": 1}}, pod), ("stopping", ""))
        pod = {"metadata": {}, "status": {"phase": "Pending", "conditions": [
            {"type": "PodScheduled", "status": "False", "message": "0/6 nodes available"}]}}
        self.assertEqual(app.Runs.state({"spec": {}, "status": {}}, pod), ("pending", "0/6 nodes available"))



class Presets(unittest.TestCase):
    def test_every_preset_is_a_valid_run(self):
        spec = app.option_spec({"server": "https://jf.example", "username": "alex"})
        dests = {f["dest"] for g in spec for f in g["fields"]}
        names = set()
        for p in app.PRESETS:
            with self.subTest(preset=p["name"]):
                self.assertTrue(p["note"] and p["group"])
                self.assertLessEqual(set(p["values"]), dests)
                self.assertNotIn(p["name"], names)
                names.add(p["name"])
                form = defaults_form(spec) | p["values"]
                a = app.validate_argv(app.build_argv(form, spec))
                for k, v in p["values"].items():
                    self.assertEqual(getattr(a, k), v)


if __name__ == "__main__":
    unittest.main()
