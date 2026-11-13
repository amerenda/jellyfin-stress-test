"""Unit tests for jfcompat.py and compat_profiles.py: python -m unittest discover -s tests"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webui"))
import app  # noqa: E402
import compat_profiles  # noqa: E402
import jfcompat  # noqa: E402


def ms(url=None, direct=False):
    return {"SupportsDirectPlay": direct, "TranscodingUrl": url}


class Classify(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(jfcompat.classify(ms(direct=True), "hevc"), ("D", []))

    def test_remux_when_only_container_or_audio(self):
        url = "/videos/x/master.m3u8?VideoCodec=h264,hevc&AudioCodec=aac&TranscodeReasons=ContainerNotSupported,AudioCodecNotSupported"
        self.assertEqual(jfcompat.classify(ms(url), "hevc"), ("R", ["ContainerNotSupported", "AudioCodecNotSupported"]))

    def test_transcode_on_video_reason_or_codec_not_in_target(self):
        url = "/x?VideoCodec=h264,hevc&TranscodeReasons=SubtitleCodecNotSupported"
        self.assertEqual(jfcompat.classify(ms(url), "h264")[0], "T")
        url = "/x?VideoCodec=h264,hevc&TranscodeReasons=ContainerNotSupported"
        self.assertEqual(jfcompat.classify(ms(url), "vp9")[0], "T")

    def test_no_path(self):
        self.assertEqual(jfcompat.classify(ms(), "h264")[0], "E")


class Subtitle(unittest.TestCase):
    def test_pick_order(self):
        s = lambda **k: {"Type": "Subtitle", **k}
        src = {"MediaStreams": [s(Index=2, Language="jpn"), s(Index=3, Language="eng"), s(Index=4, IsDefault=True)]}
        self.assertEqual(jfcompat.report_subtitle(src)["Index"], 4)
        src["MediaStreams"].pop()
        self.assertEqual(jfcompat.report_subtitle(src)["Index"], 3)
        self.assertIsNone(jfcompat.report_subtitle({"MediaStreams": []}))


class Profiles(unittest.TestCase):
    def test_devices_are_unique_and_complete(self):
        ids = [d.id for d in compat_profiles.DEVICES]
        self.assertEqual(len(ids), len(set(ids)))
        for d in compat_profiles.DEVICES:
            with self.subTest(device=d.id):
                json.dumps(d.profile)
                for key in ("DirectPlayProfiles", "TranscodingProfiles", "CodecProfiles", "SubtitleProfiles",
                            "MaxStreamingBitrate"):
                    self.assertIn(key, d.profile)
                for cp in d.profile["CodecProfiles"]:
                    for c in cp["Conditions"] + cp.get("ApplyConditions", []):
                        self.assertIsInstance(c["Value"], str)
                self.assertTrue(any(t["Type"] == "Video" for t in d.profile["TranscodingProfiles"]))

    def test_android_tv_hdr_gates(self):
        shield = next(d for d in compat_profiles.DEVICES if d.id == "shield").profile
        fire = next(d for d in compat_profiles.DEVICES if d.id == "firetv").profile

        def bad(p, codec):
            return next(cp for cp in p["CodecProfiles"] if cp.get("Codec") == codec
                        and cp["Conditions"][0]["Property"] == "VideoRangeType")["Conditions"][0]["Value"].split("|")
        self.assertNotIn("DOVIWithEL", bad(shield, "hevc"))       # Shield plays profile 7
        self.assertIn("HDR10Plus", bad(shield, "hevc"))           # but has no HDR10+
        self.assertIn("DOVIWithHDR10Plus", bad(fire, "hevc"))     # Fire TV DV+HDR10+ bug
        self.assertNotIn("HDR10Plus", bad(fire, "hevc"))


class CompatForm(unittest.TestCase):
    def test_argv(self):
        argv, form = app.compat_argv({"server": "https://jf", "username": "u", "subtitles": False,
                                      "devices": ["roku-ultra"], "limit": "50"}, {})
        self.assertIn("--no-subtitles", argv)
        self.assertIn("--device=roku-ultra", argv)
        self.assertIn("--limit=50", argv)
        argv, _ = app.compat_argv({}, {"server": "https://jf", "username": "u"})
        self.assertIn("--server=https://jf", argv)

    def test_rejects_bad_input(self):
        for bad in ({"devices": ["toaster"]}, {"concurrency": "0"}, {"concurrency": "x"}, {"server": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.compat_argv({"server": "https://jf", "username": "u"} | bad, {})

    def test_job_runs_jfcompat(self):
        cfg = app.Config()
        job = app.job_manifest("jfcompat-x", ["--server=s"], app.parse_pod({}, cfg), {}, cfg,
                               script="/app/jfcompat.py", kind="compat")
        self.assertEqual(job["metadata"]["annotations"]["jfstress.amer.dev/kind"], "compat")
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["command"][-1], "/app/jfcompat.py")


if __name__ == "__main__":
    unittest.main()
