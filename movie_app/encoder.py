"""Which H.264 encoder makes the videos: x264 on the processor, or the graphics card when asked for.

A graphics card encoder is used only after a short test encode with it has worked on this PC, so a
missing driver or an unsupported card quietly falls back to the processor.
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager

from . import media, paths

# quality -> (x264 preset, x264 CRF, graphics card speed, its quality level); a speed of None always uses
# the processor. Lower numbers are better quality and bigger files. The card's levels were picked to match
# x264's picture quality (SSIM) at 4K; its files come out somewhat bigger.
QUALITIES = {
    "scene": ("veryfast", 16, "fast", 18),  # scene clips, encoded again when joined: close to lossless
    "final": ("medium", 18, "medium", 21),
    "quick": ("ultrafast", 23, None, 0),  # small previews; the processor is quick enough
    "fast": ("veryfast", 23, "fast", 26),
    "standard": ("medium", 20, "medium", 23),
    "high": ("slow", 18, "slow", 20),
    "preview": ("ultrafast", 26, None, 0),
}


def _nvenc(speed: str, level: int) -> list[str]:
    preset = {"fast": "p3", "medium": "p4", "slow": "p5"}[speed]
    return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", str(level), "-b:v", "0", "-pix_fmt", "yuv420p"]


def _qsv(speed: str, level: int) -> list[str]:
    preset = {"fast": "veryfast", "medium": "medium", "slow": "slow"}[speed]
    return ["-c:v", "h264_qsv", "-preset", preset, "-global_quality", str(level), "-pix_fmt", "nv12"]


def _amf(speed: str, level: int) -> list[str]:
    quality = {"fast": "speed", "medium": "balanced", "slow": "quality"}[speed]
    return ["-c:v", "h264_amf", "-quality", quality, "-rc", "cqp", "-qp_i", str(level), "-qp_p", str(level),
            "-pix_fmt", "yuv420p"]  # fmt: skip


# Tried in this order; the first that works is used.
GPU_ENCODERS = {"h264_nvenc": ("NVIDIA", _nvenc), "h264_qsv": ("Intel", _qsv), "h264_amf": ("AMD", _amf)}
HEVC_ENCODERS = {"h264_nvenc": "hevc_nvenc", "h264_qsv": "hevc_qsv", "h264_amf": "hevc_amf"}

use_gpu = False  # the pages set this from the settings
_found: str | None = None
_checked = False
_lock = threading.Lock()
_here = threading.local()  # a job retried on the processor after the graphics card failed it


@contextmanager
def processor_only():
    """Within this block (on this thread), encode with x264 whatever the setting says."""
    _here.cpu = True
    try:
        yield
    finally:
        _here.cpu = False


def gpu_in_use() -> bool:
    """Whether jobs on this thread are encoded by the graphics card now."""
    return use_gpu and not getattr(_here, "cpu", False) and gpu() is not None


def _works(args: list[str]) -> bool:
    try:
        proc = subprocess.run(
            [paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=gray:s=256x256:r=30:d=0.2", *args, "-f", "null", "-"],
            capture_output=True, timeout=30, creationflags=media.NO_WINDOW,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def gpu() -> str | None:
    """The graphics card encoder that works on this PC (checked once), or None."""
    global _found, _checked
    with _lock:
        if not _checked:
            _found = next((name for name, (_, args) in GPU_ENCODERS.items() if _works(args("medium", 23))), None)
            _checked = True
        return _found


def gpu_label() -> str | None:
    name = gpu()
    return GPU_ENCODERS[name][0] if name else None


def video_args(quality: str) -> list[str]:
    """Encoder arguments for `quality` (a key of QUALITIES)."""
    preset, crf, speed, level = QUALITIES.get(quality, QUALITIES["standard"])
    if speed is not None and gpu_in_use():
        return GPU_ENCODERS[gpu()][1](speed, level)
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def hevc_args(quality: str) -> list[str]:
    """H.265 arguments for `quality` (smaller files than H.264 at the same look, slower on the processor)."""
    preset, crf, speed, level = QUALITIES.get(quality, QUALITIES["standard"])
    if speed is not None and gpu_in_use():
        name = HEVC_ENCODERS.get(gpu())
        if name:
            args = GPU_ENCODERS[gpu()][1](speed, level + 2)
            args[1] = name
            return [*args, "-tag:v", "hvc1"]
    return ["-c:v", "libx265", "-preset", preset, "-crf", str(crf + 4), "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
            "-x265-params", "log-level=error"]  # fmt: skip


def vp9_args(quality: str) -> list[str]:
    """VP9 for WebM, on the processor (row-based threading keeps it bearable)."""
    cpu_used, crf = {"fast": (6, 36), "high": (2, 30)}.get(quality, (4, 33))
    return ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0", "-deadline", "good", "-cpu-used", str(cpu_used),
            "-row-mt", "1", "-pix_fmt", "yuv420p"]  # fmt: skip
