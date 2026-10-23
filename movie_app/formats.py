"""What a finished video can be saved as: the container, its codecs, and how the save dialog offers it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GIF_FPS = 15
GIF_MAX_SIDE = 720  # GIFs get big quickly; the longer side is kept at most this long


@dataclass(frozen=True)
class Format:
    key: str
    label: str  # in the save dialog
    suffix: str
    video: str | None  # "h264", "hevc", "vp9", "gif", or None for sound only
    audio: str | None  # "aac", "opus", "mp3", "pcm", or None for no sound
    subtitles: str | None  # codec for subtitles players can switch on; None when the file cannot carry them

    @property
    def dialog_filter(self) -> str:
        return f"{self.label} (*{self.suffix})"


FORMATS = (
    Format("mp4", "MP4 영상 · H.264 (어디서나 재생)", ".mp4", "h264", "aac", "mov_text"),
    Format("mp4_hevc", "MP4 영상 · H.265 (파일이 작음, 만들기 느림)", ".mp4", "hevc", "aac", "mov_text"),
    Format("mov", "MOV 영상 (맥·편집 프로그램용)", ".mov", "h264", "aac", "mov_text"),
    Format("mkv", "MKV 영상", ".mkv", "h264", "aac", "srt"),
    Format("webm", "WebM 영상 · VP9 (웹용)", ".webm", "vp9", "opus", "webvtt"),
    Format("gif", "GIF 움짤 (소리 없음, 가로세로 720 이하)", ".gif", "gif", None, None),
    Format("mp3", "MP3 소리만", ".mp3", None, "mp3", None),
    Format("wav", "WAV 소리만 (무손실)", ".wav", None, "pcm", None),
)
DEFAULT = FORMATS[0]
VIDEO_FORMATS = tuple(f for f in FORMATS if f.video and f.video != "gif")


def by_key(key: str) -> Format:
    return next((f for f in FORMATS if f.key == key), DEFAULT)


def dialog_filters(formats=FORMATS) -> str:
    return ";;".join(f.dialog_filter for f in formats)


def from_dialog(path: str, chosen_filter: str, formats=FORMATS) -> tuple[Path, Format]:
    """The file and format picked in a save dialog: the chosen filter wins, else the typed suffix decides."""
    fmt = next((f for f in formats if f.dialog_filter == chosen_filter), None)
    output = Path(path)
    if fmt is None:
        fmt = next((f for f in formats if f.suffix == output.suffix.lower()), DEFAULT)
    if output.suffix.lower() != fmt.suffix:
        output = output.with_name(output.name + fmt.suffix) if output.suffix.lower() not in {f.suffix for f in formats} \
            else output.with_suffix(fmt.suffix)  # fmt: skip
    return output, fmt


def audio_args(fmt: Format) -> list[str]:
    return {
        "aac": ["-c:a", "aac", "-b:a", "192k"],
        "opus": ["-c:a", "libopus", "-b:a", "160k"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
        "pcm": ["-c:a", "pcm_s16le"],
    }.get(fmt.audio, [])


def container_args(fmt: Format) -> list[str]:
    """Muxer options: MP4 and MOV start playing before they are fully downloaded."""
    return ["-movflags", "+faststart"] if fmt.suffix in (".mp4", ".mov") else []
