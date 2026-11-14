"""Stands in for PyAV (the `av` package) in the WebRTC peer image, where it is installed as av.py.

aiortc imports PyAV when it loads, for audio and video. The engine's peer opens data channels
only (rtc_peer.py), so none of it ever runs. PyAV's wheel carries a whole FFmpeg build with
x264 and x265 (GPL) and 30 more libraries, about 100 MB, which an image we hand out would have
to license and offer source for (apps/search/THIRD-PARTY-LICENSES.md). This file gives aiortc
the names it imports and nothing else: using any of them raises. No FFmpeg, no codec.

The names are the ones aiortc 1.15.0 touches outside aiortc.contrib; Dockerfile.rtc pins that
version and runs rtc_selfcheck.py, which fails the build if a newer aiortc needs more.
"""
import enum
import sys
import types

_WHY = "audio and video are not part of this build (av is a stand-in, see av_absent.py)"


class FFmpegError(Exception):
    pass


class _Absent:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(_WHY)

    @classmethod
    def create(cls, *args, **kwargs):
        raise RuntimeError(_WHY)


class Frame(_Absent):
    pass


class Packet(_Absent):
    pass


class AudioFrame(Frame):
    pass


class VideoFrame(Frame):
    pass


class AudioResampler(_Absent):
    pass


class CodecContext(_Absent):
    pass


class AudioCodecContext(CodecContext):
    pass


class VideoCodecContext(CodecContext):
    pass


class PictureType(enum.IntEnum):
    NONE = 0
    I = 1


def _submodule(name, **names):
    module = types.ModuleType(name, __doc__)
    module.__dict__.update(names)
    sys.modules[name] = module
    return module


frame = _submodule("av.frame", Frame=Frame)
packet = _submodule("av.packet", Packet=Packet)
audio = _submodule("av.audio")
audio.frame = _submodule("av.audio.frame", AudioFrame=AudioFrame)
video = _submodule("av.video")
video.frame = _submodule("av.video.frame", VideoFrame=VideoFrame, PictureType=PictureType)
video.codeccontext = _submodule("av.video.codeccontext", VideoCodecContext=VideoCodecContext)
