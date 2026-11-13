"""Jellyfin device profiles for the library compatibility report (jfcompat.py).

Each profile is what one client app would send on one device, built the way the app builds it:
  - Jellyfin Android TV: app/.../util/profile/deviceProfile.kt (jellyfin/jellyfin-androidtv)
  - Swiftfin: Shared/Objects/MediaPlayerType/MediaPlayerType+AVPlayer.swift and +MPV.swift
  - Jellyfin Roku: source/utils/deviceCapabilities.bs (jellyfin/jellyfin-roku)
  - Streamyfin: utils/profiles/native.ts and subtitles.ts (streamyfin/streamyfin)
The apps probe the hardware at runtime (MediaCodec, roDeviceInfo, VideoToolbox); the answers
for each device are filled in below in `Caps`, from the published specs. Edit them, or add a
device, here. App settings are left at their defaults; the assumptions are listed per device.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Jellyfin level numbers: H.264 = level x 10 (5.1 -> 51); HEVC = level x 30 (5.1 -> 153).
AVC_51, AVC_52 = 51, 52
HEVC_51, HEVC_52 = 153, 156

SDR, HLG, HDR10, HDR10P = "SDR", "HLG", "HDR10", "HDR10Plus"
DOVI, DOVI_SDR, DOVI_HLG = "DOVI", "DOVIWithSDR", "DOVIWithHLG"
DOVI_HDR10, DOVI_HDR10P = "DOVIWithHDR10", "DOVIWithHDR10Plus"
DOVI_EL, DOVI_EL_HDR10P, DOVI_INVALID = "DOVIWithEL", "DOVIWithELHDR10Plus", "DOVIInvalid"


def cond(condition: str, prop: str, value, required: bool = False) -> dict:
    if isinstance(value, (list, tuple, set)):
        value = "|".join(value)
    return {"Condition": condition, "Property": prop, "Value": str(value), "IsRequired": required}


def codec_profile(codec: str | None, conditions: list[dict], apply: list[dict] | None = None,
                  type_: str = "Video") -> dict:
    p = {"Type": type_, "Conditions": conditions}
    if codec:
        p["Codec"] = codec
    if apply:
        p["ApplyConditions"] = apply
    return p


def subs(fmt_methods: list[tuple[str, str]]) -> list[dict]:
    return [{"Format": f, "Method": m} for f, m in fmt_methods]


# --------------------------------------------------------------------------- Jellyfin Android TV

@dataclass
class AndroidCaps:
    avc_level: int = AVC_52
    avc_high10: bool = False
    hevc: bool = True
    hevc_main10: bool = True
    hevc_level: int = HEVC_51
    av1: bool = False
    av1_main10: bool = False
    vc1: bool = False
    max_res: tuple[int, int] = (4096, 2304)
    hevc_hdr10: bool = True
    hevc_hdr10plus: bool = False
    hevc_dovi: bool = False
    hevc_dovi_el: bool = False
    dovi_p7_unreported: bool = False   # KnownDefects: Shield plays P7 without reporting it
    dovi_hdr10plus_bug: bool = False   # KnownDefects: Fire TV 4K models
    av1_hdr10: bool = False
    av1_hdr10plus: bool = False
    av1_dovi: bool = False
    passthrough: tuple[str, ...] = ("ac3", "eac3")   # bitstream formats the HDMI sink accepts
    max_bitrate: int = 100_000_000                   # app default: 100 Mbit/s
    ass_direct: bool = False                         # app default: libass off
    pgs_direct: bool = True                          # app default: PGS on


ANDROID_AUDIO = ["aac", "aac_latm", "ac3", "alac", "dca", "dts", "eac3", "flac", "mlp", "mp2", "mp3",
                 "opus", "pcm_alaw", "pcm_mulaw", "pcm_s16le", "pcm_s20le", "pcm_s24le", "truehd", "vorbis"]


def android_tv_profile(c: AndroidCaps) -> dict:
    gated = {"ac3": "ac3", "eac3": "eac3", "truehd": "truehd", "dts": "dts"}
    audio = [a for a in ANDROID_AUDIO if a not in gated or gated[a] in c.passthrough]
    hls_codecs = (["hevc"] if c.hevc else []) + ["h264"]
    ts_audio = [a for a in ("aac", "ac3", "eac3", "mp3") if a in audio]
    mp4_audio = [a for a in ("aac", "ac3", "eac3", "mp3", "alac", "flac", "opus", "dts") if a in audio]
    w, h = c.max_res

    cps = [codec_profile("h264", [cond("EqualsAny", "VideoProfile",
                                       ["high", "main", "baseline", "constrained baseline"]
                                       + (["high 10"] if c.avc_high10 else []))]),
           codec_profile("h264", [cond("LessThanEqual", "VideoLevel", c.avc_level)],
                         [cond("EqualsAny", "VideoProfile", ["high", "main", "baseline", "constrained baseline"])])]
    if c.avc_high10:
        cps.append(codec_profile("h264", [cond("LessThanEqual", "VideoLevel", c.avc_level)],
                                 [cond("Equals", "VideoProfile", "high 10")]))
    cps += [codec_profile("h264", [cond("LessThanEqual", "RefFrames", 12)], [cond("GreaterThanEqual", "Width", 1200)]),
            codec_profile("h264", [cond("LessThanEqual", "RefFrames", 4)], [cond("GreaterThanEqual", "Width", 1900)])]
    cps.append(codec_profile("hevc", [cond("Equals", "VideoProfile", "none")] if not c.hevc else
                             [cond("EqualsAny", "VideoProfile", ["main"] + (["main 10"] if c.hevc_main10 else []))]))
    if c.hevc:
        cps.append(codec_profile("hevc", [cond("LessThanEqual", "VideoLevel", c.hevc_level)],
                                 [cond("Equals", "VideoProfile", "main")]))
    if c.hevc_main10:
        cps.append(codec_profile("hevc", [cond("LessThanEqual", "VideoLevel", c.hevc_level)],
                                 [cond("Equals", "VideoProfile", "main 10")]))
    av1 = (cond("Equals", "VideoProfile", "none") if not c.av1 else
           cond("NotEquals", "VideoProfile", "main 10") if not c.av1_main10 else
           cond("NotEquals", "VideoProfile", "none"))
    cps.append(codec_profile("av1", [av1]))
    cps.append(codec_profile("vc1", [cond("Equals" if not c.vc1 else "NotEquals", "VideoProfile", "none")]))
    for codec in ("h264", "hevc", "av1", "vc1"):
        cps.append(codec_profile(codec, [cond("LessThanEqual", "Width", w), cond("LessThanEqual", "Height", h)]))

    bad_av1 = {DOVI_INVALID}
    if not c.av1_dovi:
        bad_av1.add(DOVI)
        if not c.av1_hdr10:
            bad_av1.add(DOVI_HDR10)
        if not c.av1_hdr10plus:
            bad_av1.add(DOVI_HDR10P)
    if not c.av1_hdr10plus:
        bad_av1.add(HDR10P)
        if not c.av1_hdr10:
            bad_av1.add(HDR10)
    bad_hevc = {DOVI_INVALID}
    if not c.hevc_dovi_el:
        if not (c.dovi_p7_unreported and c.hevc_dovi and c.hevc_main10 and c.hevc_hdr10):
            bad_hevc.add(DOVI_EL)
            if not c.hevc_hdr10plus and not c.dovi_hdr10plus_bug:
                bad_hevc.add(DOVI_EL_HDR10P)
        if not c.hevc_dovi:
            bad_hevc.add(DOVI)
            if not c.hevc_hdr10:
                bad_hevc.add(DOVI_HDR10)
            if not c.hevc_hdr10plus and not c.dovi_hdr10plus_bug:
                bad_hevc.add(DOVI_HDR10P)
    if not c.hevc_hdr10plus:
        bad_hevc.add(HDR10P)
        if not c.hevc_hdr10:
            bad_hevc.add(HDR10)
    if c.dovi_hdr10plus_bug:
        bad_hevc |= {DOVI_HDR10P, DOVI_EL_HDR10P}
    for codec, bad in (("av1", bad_av1), ("hevc", bad_hevc)):
        bad = sorted(bad)
        cps.append(codec_profile(codec, [cond("NotEquals", "VideoRangeType", bad)],
                                 [cond("EqualsAny", "VideoRangeType", bad)]))
    cps.append(codec_profile(None, [cond("LessThanEqual", "AudioChannels", 8)], type_="VideoAudio"))

    return {
        "Name": "AndroidTV-Default",
        "MaxStaticBitrate": c.max_bitrate, "MaxStreamingBitrate": c.max_bitrate,
        "TranscodingProfiles": [
            {"Type": "Video", "Context": "Streaming", "Container": "ts", "Protocol": "hls",
             "VideoCodec": ",".join(hls_codecs), "AudioCodec": ",".join(ts_audio),
             "CopyTimestamps": False, "EnableSubtitlesInManifest": True},
            {"Type": "Video", "Context": "Streaming", "Container": "mp4", "Protocol": "hls",
             "VideoCodec": ",".join(hls_codecs), "AudioCodec": ",".join(mp4_audio),
             "CopyTimestamps": False, "EnableSubtitlesInManifest": True},
        ],
        "DirectPlayProfiles": [{
            "Type": "Video",
            "Container": "asf,hls,m4v,mkv,mov,mp4,ogm,ogv,ts,vob,webm,wmv,xvid",
            "VideoCodec": "av1,h264,hevc,mpeg,mpeg2video,vc1,vp8,vp9",
            "AudioCodec": ",".join(audio),
        }],
        "CodecProfiles": cps,
        "SubtitleProfiles": subs(
            [("vtt", m) for m in ("Embed", "Hls", "External")] + [("webvtt", m) for m in ("Embed", "Hls", "External")]
            + [(f, m) for f in ("srt", "subrip", "ttml") for m in ("Embed", "External")]
            + [(f, m) for f in ("dvbsub", "dvdsub", "idx") for m in ("Embed", "Encode")]
            + [(f, m) for f in ("pgs", "pgssub") for m in (["Embed"] if c.pgs_direct else []) + ["Encode"]]
            + [(f, m) for f in ("ass", "ssa") for m in ["Encode"] + (["Embed", "External"] if c.ass_direct else [])]),
    }


# --------------------------------------------------------------------------- Swiftfin (tvOS / iOS)

@dataclass
class AppleCaps:
    hevc: bool = True
    vp9: bool = True
    av1: bool = False
    dolby_vision: bool = True
    max_bitrate: int = 120_000_000


def _swift_h264_base():
    return [cond("NotEquals", "IsAnamorphic", "true"),
            cond("EqualsAny", "VideoProfile", ["high", "main", "baseline", "constrained baseline"]),
            cond("LessThanEqual", "VideoLevel", 80)]


def _swift_hevc_base():
    return [cond("NotEquals", "IsAnamorphic", "true"),
            cond("EqualsAny", "VideoProfile", ["main", "main 10"]),
            cond("LessThanEqual", "VideoLevel", 175),
            cond("NotEquals", "IsInterlaced", "true")]


def swiftfin_native_profile(c: AppleCaps) -> dict:
    hdr = [SDR, DOVI_SDR] + ([HLG, DOVI_HLG, HDR10, HDR10P] if c.hevc else [])
    if c.hevc or c.dolby_vision:
        hdr += [DOVI_HDR10, DOVI_HDR10P, DOVI_EL, DOVI_EL_HDR10P]
    if c.dolby_vision:
        hdr.append(DOVI)
    mp4_video = ["h264", "mpeg4"] + (["av1"] if c.av1 else []) + (["hevc"] if c.hevc else []) + (["vp9"] if c.vp9 else [])
    return {
        "Name": "Swiftfin AVPlayer",
        "MaxStaticBitrate": c.max_bitrate, "MaxStreamingBitrate": c.max_bitrate,
        "DirectPlayProfiles": [
            {"Type": "Video", "Container": "mp4,m4v", "VideoCodec": ",".join(mp4_video),
             "AudioCodec": "aac,ac3,alac,eac3,flac"},
            {"Type": "Video", "Container": "mov", "VideoCodec": "h264,mjpeg,mpeg4" + (",hevc" if c.hevc else ""),
             "AudioCodec": "aac,ac3,alac,eac3,mp3,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le"},
        ],
        "TranscodingProfiles": [{
            "Type": "Video", "Context": "Streaming", "Protocol": "hls", "Container": "mp4",
            "VideoCodec": ",".join((["av1"] if c.av1 else []) + (["hevc"] if c.hevc else []) + ["h264", "mpeg4"]),
            "AudioCodec": "aac,ac3,alac,eac3,flac", "MaxAudioChannels": "8", "MinSegments": 2,
            "BreakOnNonKeyFrames": True, "EnableSubtitlesInManifest": True,
        }],
        "CodecProfiles": [
            codec_profile("h264", _swift_h264_base() + [cond("EqualsAny", "VideoRangeType", [SDR, DOVI_SDR]),
                                                       cond("NotEquals", "IsInterlaced", "true")]),
            codec_profile("hevc", _swift_hevc_base() + [cond("EqualsAny", "VideoRangeType", hdr),
                                                       cond("EqualsAny", "VideoCodecTag", ["hvc1", "dvh1"], True),
                                                       cond("LessThanEqual", "VideoFramerate", 60, True)]),
            codec_profile("av1", [cond("NotEquals", "IsAnamorphic", "true"), cond("NotEquals", "IsInterlaced", "true"),
                                  cond("EqualsAny", "VideoRangeType", hdr)]),
        ],
        "SubtitleProfiles": subs([("cc_dec", "Embed"), ("ttml", "Embed")]
                                 + [(f, "Encode") for f in ("dvbsub", "dvdsub", "pgssub", "xsub")]
                                 + [("vtt", "Hls")]),
    }


MPV_AUDIO = ("aac,ac3,alac,amr_nb,amr_wb,dts,dts_hd,eac3,flac,mp1,mp2,mp3,opus,pcm_alaw,pcm_bluray,pcm_dvd,"
             "pcm_mulaw,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le,pcm_u8,truehd,vorbis,wmalossless,wmapro,wmav1,wmav2")


def swiftfin_mpv_profile(c: AppleCaps) -> dict:
    hdr = [SDR, HLG, HDR10, HDR10P]
    hevc_hdr = hdr + ([DOVI, DOVI_HLG, DOVI_HDR10, DOVI_HDR10P] if c.dolby_vision else [])
    video = (["av1"] if c.av1 else []) + ["ffv1", "flv1", "h263", "h264", "hevc", "mjpeg", "mpeg1video", "mpeg2video",
                                          "mpeg4", "prores", "vc1", "vp8", "vp9", "wmv1", "wmv2", "wmv3"]
    no_anam_int = [cond("NotEquals", "IsAnamorphic", "true"), cond("NotEquals", "IsInterlaced", "true")]
    return {
        "Name": "Swiftfin MPV",
        "MaxStaticBitrate": c.max_bitrate, "MaxStreamingBitrate": c.max_bitrate,
        "DirectPlayProfiles": [{"Type": "Video", "AudioCodec": MPV_AUDIO, "VideoCodec": ",".join(video),
                                "Container": "avi,flv,m4v,mkv,mov,mp4,mpegts,ts,3g2,3gp,webm"}],
        "TranscodingProfiles": [{
            "Type": "Video", "Context": "Streaming", "Protocol": "hls", "Container": "mp4",
            "VideoCodec": ",".join((["av1"] if c.av1 else []) + ["h264", "hevc", "vp9"]),
            "AudioCodec": "aac,ac3,alac,dts,eac3,flac,mp3,opus,truehd", "MaxAudioChannels": "8",
            "MinSegments": 2, "BreakOnNonKeyFrames": True,
        }],
        "CodecProfiles": [
            codec_profile("h264", [cond("NotEquals", "IsAnamorphic", "true"),
                                   cond("EqualsAny", "VideoProfile",
                                        ["high", "high 10", "main", "baseline", "constrained baseline"]),
                                   cond("LessThanEqual", "VideoLevel", 80),
                                   cond("EqualsAny", "VideoRangeType", [SDR], True)]),
            codec_profile("hevc", [cond("NotEquals", "IsAnamorphic", "true"),
                                   cond("EqualsAny", "VideoProfile", ["main", "main 10"]),
                                   cond("NotEquals", "IsInterlaced", "true"),
                                   cond("EqualsAny", "VideoRangeType", hevc_hdr, True)]),
            codec_profile("av1", no_anam_int + [cond("EqualsAny", "VideoRangeType", hdr, True)]),
            codec_profile("vp9", no_anam_int + [cond("EqualsAny", "VideoRangeType", hdr, True)]),
        ],
        "SubtitleProfiles": subs(
            [(f, "Embed") for f in ("ass", "cc_dec", "dvbsub", "dvdsub", "mov_text", "mpl2", "pgssub", "ssa",
                                    "subrip", "vtt", "xsub")]
            + [(f, "External") for f in ("subrip", "ass", "ssa", "vtt")]
            + [(f, "Encode") for f in ("dvbsub", "dvdsub", "pgssub", "vtt", "xsub")]),
    }


# --------------------------------------------------------------------------- Jellyfin Roku

@dataclass
class RokuCaps:
    hevc: bool = True
    av1: bool = False
    vp9: bool = True
    hdr10: bool = True
    hdr10plus: bool = False
    hlg: bool = True
    dolby_vision: bool = False
    audio: tuple[str, ...] = ("mp3", "mp2", "pcm", "lpcm", "wav", "ac3", "flac", "alac", "aac", "opus", "vorbis",
                              "eac3", "mpg123")   # decodable or passed through (dts: only with a DTS receiver)
    surround: bool = True                         # Roku audio output set to 5.1 surround
    max_channels: int = 6


def roku_profile(c: RokuCaps) -> dict:
    video = ["h264", "mpeg4 avc"] + (["vp9"] if c.vp9 else []) + (["hevc", "h265"] if c.hevc else []) \
        + (["av1"] if c.av1 else [])
    audio = ",".join(c.audio)
    dpp = [{"Container": cont, "Type": "Video", "VideoCodec": ",".join(video), "AudioCodec": audio}
           for cont in ("mp4,mov,m4v", "hls", "mkv,webm", "ts")]
    ranges = {"h264": [SDR, DOVI_SDR], "hevc": [SDR, DOVI_SDR], "vp9": [SDR, DOVI_SDR], "av1": [SDR, DOVI_SDR]}
    for k in ("hevc", "vp9", "av1"):
        if c.hdr10:
            ranges[k] += [HDR10, DOVI_HDR10]
        if c.hdr10plus:
            ranges[k] += [HDR10P, DOVI_HDR10P]
        if c.hlg:
            ranges[k] += [HLG, DOVI_HLG]
    if c.dolby_vision:
        for k in ("h264", "hevc", "av1"):
            ranges[k] += [DOVI, DOVI_HDR10, DOVI_HDR10P, DOVI_HLG]
    tvideo = (["hevc", "h265"] if c.hevc else []) + ["h264", "mpeg4 avc"] + (["vp9"] if c.vp9 else []) \
        + (["av1"] if c.av1 else [])
    taudio = "AC3," + ",".join(a for a in ("eac3", "ac3", "dts", "mp3", "vorbis", "opus", "flac", "alac", "pcm")
                               if a in c.audio)
    ch = str(c.max_channels if c.surround else 2)
    cps = []
    for a in c.audio:
        n = 2 if a in ("mp3", "mp2", "mpg123") else c.max_channels
        conds = [cond("LessThanEqual", "AudioChannels", n, True)]
        if a == "aac":
            conds.append(cond("NotEquals", "AudioProfile", "Main", True))
        cps.append(codec_profile(a, conds, type_="VideoAudio"))
    cps.append(codec_profile("h264", [cond("LessThanEqual", "VideoBitDepth", 8),
                                      cond("EqualsAny", "VideoProfile",
                                           ["baseline", "main", "high", "constrained baseline", "constrainedbaseline"]),
                                      cond("EqualsAny", "VideoRangeType", ranges["h264"]),
                                      cond("NotEquals", "IsAnamorphic", "true"),
                                      cond("LessThanEqual", "VideoLevel", 51)]))
    if c.av1:
        cps.append(codec_profile("av1", [cond("EqualsAny", "VideoProfile", ["main", "main 10"]),
                                         cond("EqualsAny", "VideoRangeType", ranges["av1"]),
                                         cond("LessThanEqual", "VideoLevel", 612),
                                         cond("NotEquals", "IsAnamorphic", "true")]))
    if c.hevc:
        cps.append(codec_profile("hevc", [cond("EqualsAny", "VideoProfile", ["main", "main 10"]),
                                          cond("EqualsAny", "VideoRangeType", ranges["hevc"]),
                                          cond("NotEquals", "IsAnamorphic", "true"),
                                          cond("LessThanEqual", "VideoLevel", HEVC_51)]))
    if c.vp9:
        cps.append(codec_profile("vp9", [cond("EqualsAny", "VideoProfile", ["profile 0", "profile 2"]),
                                         cond("EqualsAny", "VideoRangeType", ranges["vp9"]),
                                         cond("NotEquals", "IsAnamorphic", "true")]))
    return {
        "Name": "Official Roku Client",
        "MaxStreamingBitrate": 120_000_000, "MaxStaticBitrate": 100_000_000,
        "DirectPlayProfiles": dpp,
        "TranscodingProfiles": [
            {"Container": cont, "Context": "Streaming", "Protocol": "hls", "Type": "Video", "AudioCodec": taudio,
             "VideoCodec": ",".join(tvideo), "MaxAudioChannels": ch, "MinSegments": 1, "SegmentLength": 6,
             "BreakOnNonKeyFrames": False} for cont in ("ts", "mp4")],
        "CodecProfiles": cps,
        "SubtitleProfiles": subs([(f, "External") for f in ("vtt", "srt", "ttml", "sub")]),
    }


# --------------------------------------------------------------------------- Streamyfin (MPV)

def streamyfin_mpv_profile(av1: bool, audio_mode_channels: str = "6") -> dict:
    image = ["dvdsub", "idx", "pgs", "pgssub", "teletext", "vobsub", "xsub"]
    text = ["webvtt", "vtt", "srt", "subrip", "ttml", "ass", "ssa", "microdvd", "mov_text", "mpl2", "pjs",
            "realtext", "scc", "smi", "stl", "sub", "subviewer", "text", "vplayer"]
    cp_codecs = ["h264", "mpeg4", "divx", "xvid", "wmv", "vc1", "vp8", "vp9"] + (["av1"] if av1 else [])
    dp_codecs = ["h264", "hevc", "mpeg4", "divx", "xvid", "wmv", "vc1", "vp8", "vp9"] + (["av1"] if av1 else []) \
        + ["avi", "mpeg", "mpeg2video"]
    return {
        "Name": "1. MPV",
        "MaxStaticBitrate": 999_999_999, "MaxStreamingBitrate": 999_999_999,
        "CodecProfiles": [
            codec_profile(",".join(cp_codecs), []),
            codec_profile("hevc,h265", [cond("LessThanEqual", "VideoLevel", 153),
                                        cond("NotEquals", "VideoRangeType", DOVI, True)]),
            codec_profile("aac,ac3,eac3,mp3,flac,vorbis,opus,pcm", [], type_="Audio"),
        ],
        "DirectPlayProfiles": [{"Type": "Video", "Container": "mp4,mkv,avi,mov,flv,ts,m2ts,webm,ogv,3gp,hls",
                                "VideoCodec": ",".join(dp_codecs),
                                "AudioCodec": "aac,mp3,flac,opus,vorbis,ac3,eac3,dts,truehd,wma"}],
        "TranscodingProfiles": [{"Type": "Video", "Context": "Streaming", "Protocol": "hls", "Container": "ts",
                                 "VideoCodec": "h264,hevc", "AudioCodec": "aac,mp3,ac3,dts",
                                 "MaxAudioChannels": audio_mode_channels}],
        "SubtitleProfiles": subs([(f, m) for f in image for m in ("Embed", "Encode")]
                                 + [(f, m) for f in text for m in ("Embed", "External")]),
    }


# --------------------------------------------------------------------------- the devices

@dataclass
class Device:
    id: str
    name: str
    app: str
    hardware: str
    assumptions: list[str] = field(default_factory=list)
    profile: dict = field(default_factory=dict)


DEVICES = [
    Device("shield", "NVIDIA Shield TV Pro (2019)", "Jellyfin for Android TV",
           "Tegra X1+: H.264, HEVC Main10 4K60, VP9, VC-1, MPEG-2; AV1 only via Android's software "
           "decoder; HDR10 and Dolby Vision (incl. profile 7), no HDR10+",
           ["Receiver passes AC3, E-AC3, DTS and TrueHD through", "App defaults: 100 Mbit/s, PGS on, ASS off"],
           android_tv_profile(AndroidCaps(avc_level=AVC_52, vc1=True, av1=True, av1_main10=False,
                                          hevc_dovi=True, dovi_p7_unreported=True,
                                          passthrough=("ac3", "eac3", "dts", "truehd")))),
    Device("firetv", "Fire TV Stick 4K Max (2nd gen)", "Jellyfin for Android TV",
           "MediaTek MT8696T: H.264, HEVC Main10, VP9, AV1 Main10; HDR10, HDR10+, Dolby Vision "
           "(the app avoids DV+HDR10+ files on this model)",
           ["Passes AC3, E-AC3 and DTS through; no TrueHD", "App defaults: 100 Mbit/s, PGS on, ASS off"],
           android_tv_profile(AndroidCaps(avc_level=AVC_51, av1=True, av1_main10=True, av1_hdr10=True,
                                          av1_hdr10plus=True, hevc_hdr10plus=True, hevc_dovi=True,
                                          dovi_hdr10plus_bug=True, max_res=(3840, 2160),
                                          passthrough=("ac3", "eac3", "dts")))),
    Device("appletv-native", "Apple TV 4K (2022)", "Swiftfin, native player",
           "A15: H.264, HEVC (hvc1/dvh1 tagged only), VP9; no AV1; HDR10, HDR10+, Dolby Vision",
           ["No bitstream audio: DTS and TrueHD need audio transcoding",
            "MKV and ASS are not supported by AVPlayer"],
           swiftfin_native_profile(AppleCaps())),
    Device("appletv-mpv", "Apple TV 4K (2022)", "Swiftfin, MPV player",
           "A15 via libmpv: most codecs incl. VC-1 and MPEG-2; AV1 left off (stalls without hardware); "
           "Dolby Vision profiles 5 and 8 only",
           ["Swiftfin's MPV player (newer builds)"],
           swiftfin_mpv_profile(AppleCaps())),
    Device("roku-ultra", "Roku Ultra (2024)", "Jellyfin for Roku",
           "H.264, HEVC Main10, VP9, AV1; HDR10, HDR10+, HLG, Dolby Vision; no 10-bit H.264",
           ["Audio output set to 5.1 surround; DTS needs a DTS receiver (not assumed)",
            "Text subtitles only: ASS and image subtitles are burned in"],
           roku_profile(RokuCaps(av1=True, hdr10plus=True, dolby_vision=True))),
    Device("roku-express", "Roku Express 4K+", "Jellyfin for Roku",
           "H.264, HEVC Main10, VP9; no AV1; HDR10, HDR10+, HLG; no Dolby Vision",
           ["Audio output set to 5.1 surround", "Text subtitles only: ASS and image subtitles are burned in"],
           roku_profile(RokuCaps(av1=False, hdr10plus=True))),
    Device("streamyfin", "Android phone (Pixel 8)", "Streamyfin (MPV)",
           "Tensor G3 via libmpv: nearly everything, incl. AV1 in hardware; no Dolby Vision profile 5",
           ["Audio mode Auto: more than 6 channels is downmixed by the server",
            "Image subtitles burned in only if the player can't embed them"],
           streamyfin_mpv_profile(av1=True)),
]
