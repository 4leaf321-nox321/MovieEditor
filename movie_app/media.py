"""Reads the length, frame size and streams of a media file with ffprobe."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths

# Keeps FFmpeg from flashing a console window when the app runs under pythonw.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts")
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma")


@dataclass
class MediaInfo:
    path: Path
    duration: float  # seconds
    has_video: bool = False
    has_audio: bool = False
    width: int = 0  # as displayed, i.e. after applying any rotation
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    pixel_format: str = ""
    picture_length: float = 0.0  # the video stream's own length, when the file states it
    video_start: float = 0.0  # when each stream begins in the file; recorders often start sound late
    audio_start: float = 0.0

    @property
    def video_duration(self) -> float:
        """How long there is picture: a sound track running longer makes the file outlast it."""
        return self.picture_length if 0 < self.picture_length < self.duration else self.duration

    def describe(self) -> str:
        parts = []
        if self.has_video:
            parts.append(f"{self.width}×{self.height}")
            if self.fps:
                parts.append(f"{self.fps:.3g}fps")
        parts.append(format_time(self.duration, precise=True))
        if self.has_video:
            parts.append("소리 있음" if self.has_audio else "소리 없음")
        return " · ".join(parts)


def format_time(seconds: float, precise: bool = False) -> str:
    whole = int(seconds)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    text = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{text}.{int((seconds - whole) * 10)}" if precise else text


def _rate(text: str) -> float:
    num, _, den = text.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _seconds(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stream_length(stream: dict) -> float:
    """A stream's length: the "duration" field, or the DURATION tag Matroska files carry instead."""
    try:
        if stream.get("duration"):
            return float(stream["duration"])
        h, m, s = stream.get("tags", {}).get("DURATION", "").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


def _rotation(stream: dict) -> int:
    for item in stream.get("side_data_list", []):
        if "rotation" in item:
            return int(float(item["rotation"]))
    return int(float(stream.get("tags", {}).get("rotate", 0)))


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"파일이 없습니다: {path}")
    proc = subprocess.run(
        [paths.tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise ValueError(f"영상이나 음성 파일로 읽을 수 없습니다: {path.name}\n{proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    info = MediaInfo(path, float(data.get("format", {}).get("duration") or 0))

    # Cover art inside an MP3 shows up as a one-frame video stream; it is not a video.
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    sound = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info.has_audio = sound is not None
    if sound:
        info.audio_start = _seconds(sound.get("start_time"))
    if video:
        info.video_start = _seconds(video.get("start_time"))
        info.has_video = True
        info.width, info.height = int(video.get("width", 0)), int(video.get("height", 0))
        if _rotation(video) % 180:
            info.width, info.height = info.height, info.width
        info.fps = _rate(video.get("avg_frame_rate", "")) or _rate(video.get("r_frame_rate", ""))
        info.video_codec = video.get("codec_name", "")
        info.pixel_format = video.get("pix_fmt", "")
        info.picture_length = _stream_length(video)
    if not info.duration:
        info.duration = max((float(s.get("duration") or 0) for s in streams), default=0.0)
    return info
