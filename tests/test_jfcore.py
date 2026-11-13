"""Unit tests for jfcore item selection: python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jfcore  # noqa: E402


def item(video=None, subs=(), bitrate=8_000_000, runtime_s=3600):
    v = {"Type": "Video", "Codec": "hevc", "Width": 1920, "Height": 1080, "VideoRange": "SDR",
         "VideoRangeType": "SDR", "BitDepth": 8, "Index": 0} | (video or {})
    streams = [v] + [{"Type": "Subtitle", "Codec": c, "Index": i + 2, "Language": "eng"}
                     for i, c in enumerate(subs)]
    return {"RunTimeTicks": int(runtime_s * 1e7),
            "MediaSources": [{"Id": "s", "Bitrate": bitrate, "MediaStreams": streams}]}


def matches(it, spec):
    return jfcore.item_matches(it, jfcore.parse_filter(spec))


class Filters(unittest.TestCase):
    def test_resolution_uses_width_for_scope_files(self):
        self.assertTrue(matches(item({"Width": 1920, "Height": 804}), "1080p"))
        self.assertTrue(matches(item({"Width": 3840, "Height": 1600}), "4k"))
        self.assertTrue(matches(item({"Width": 1280, "Height": 536}), "720p"))
        self.assertTrue(matches(item({"Width": 720, "Height": 480}), "sd"))
        self.assertEqual(jfcore.describe_video({"Codec": "h264", "Width": 1920, "Height": 804}), "1080p h264")

    def test_ranges(self):
        dv = item({"VideoRange": "HDR", "VideoRangeType": "DOVIWithHDR10", "BitDepth": 10})
        self.assertTrue(matches(dv, "dovi"))
        self.assertTrue(matches(dv, "hdr10"))
        self.assertTrue(matches(dv, "hdr"))
        self.assertFalse(matches(dv, "sdr"))
        self.assertFalse(matches(dv, "hdr10plus"))
        self.assertTrue(matches(item({"VideoRange": "HDR", "VideoRangeType": "DOVIWithHDR10Plus"}), "hdr10plus"))

    def test_traits_and_codecs(self):
        hi10 = item({"Codec": "h264", "BitDepth": 10})
        self.assertTrue(matches(hi10, "hi10p"))
        self.assertTrue(matches(hi10, "10bit,h264"))
        self.assertFalse(matches(item({"BitDepth": 10}), "hi10p"))
        self.assertTrue(matches(item({"Codec": "av1"}), "av1"))
        self.assertTrue(matches(item({"Codec": "mpeg4"}), "mpeg4"))
        self.assertTrue(matches(item({"IsInterlaced": True}), "interlaced"))
        self.assertTrue(matches(item(bitrate=60_000_000), "highbitrate"))
        self.assertFalse(matches(item(), "highbitrate"))
        self.assertEqual(jfcore.describe_video({"Codec": "h264", "Height": 1080, "BitDepth": 10}), "1080p h264 10bit")

    def test_subtitles(self):
        it = item(subs=("subrip", "PGSSUB", "ass"))
        for f in ("srt", "pgs", "ass"):
            self.assertTrue(matches(it, f))
        self.assertFalse(matches(it, "dvdsub"))
        src = it["MediaSources"][0]
        self.assertEqual(jfcore.pick_subtitle(src)["Codec"], "subrip")
        self.assertEqual(jfcore.pick_subtitle(src, jfcore.subtitle_codecs({"pgs"}))["Codec"], "PGSSUB")
        body = jfcore.playback_info_body("transcode", "u", src, 0, codec="h264", bitrate=1, max_height=1080,
                                         burn_subs=True, sub_codecs=jfcore.subtitle_codecs({"ass", "4k"}))
        self.assertEqual(body["SubtitleStreamIndex"], 4)
        body = jfcore.playback_info_body("transcode", "u", src, 0, codec="h264", bitrate=1, max_height=1080,
                                         burn_subs=False)
        self.assertNotIn("SubtitleStreamIndex", body)

    def test_short_items_and_unknown_tokens(self):
        self.assertFalse(matches(item(runtime_s=300), ""))
        with self.assertRaises(ValueError):
            jfcore.parse_filter("8k")

    def test_items_query_pages_and_prefilters_when_filtered(self):
        q = jfcore.items_query("u", "Movie", None, {"4k", "pgs"}, start=500, limit=500)
        self.assertEqual((q["StartIndex"], q["Limit"], q["Is4K"], q["HasSubtitles"]), ("500", "500", "true", "true"))
        q = jfcore.items_query("u", "Movie")
        self.assertEqual((q["SortBy"], q["Limit"]), ("Random", "400"))
        self.assertNotIn("StartIndex", q)


class StallModel(unittest.TestCase):
    def test_stall_detected_without_new_segments(self):
        events = []
        p = jfcore.Player(jfcore.HealthConfig(startup_buffer=6, rebuffer=4))
        p.on_stall_start = lambda: events.append("stall")
        p.on_stall_end = lambda d: events.append(round(d, 1))
        p.begin(0)
        p.got_media(1, 6, 1)               # starts playing at t=1 with 6 s buffered
        self.assertEqual(p.state, "playing")
        self.assertEqual(p.degraded(8), "stalled")   # no segment for 7 s: ran dry at t=7
        self.assertEqual(events, ["stall"])
        p.got_media(10, 4, 1)              # resumes once 4 s are buffered
        self.assertEqual(p.state, "playing")
        self.assertEqual(events, ["stall", 3.0])


if __name__ == "__main__":
    unittest.main()
