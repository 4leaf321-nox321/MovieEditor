"""Builds and runs the FFmpeg command that puts a voice track and subtitles on a video."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import ffmpeg, media, paths, subs
from .ffmpeg import Cancelled
from .media import MediaInfo
from .subs import SubStyle

SAMPLE_RATE = 48000
AUDIO_BITRATE = "192k"
# name -> (x264 preset, CRF); a lower CRF is better quality and a bigger file
QUALITIES = {"fast": ("veryfast", 23), "standard": ("medium", 20), "high": ("slow", 18), "preview": ("ultrafast", 26)}
# Codecs MP4 players handle, so the video can be copied untouched when nothing is drawn on it.
COPYABLE = {("h264", "yuv420p"), ("hevc", "yuv420p")}
SUBS_FILE = "subs.ass"  # written in the work folder; a bare name avoids escaping Windows paths in the filter

__all__ = ["Cancelled", "Job", "Plan", "build", "default_output", "preview_frame", "result_duration", "run"]


@dataclass
class Job:
    video: Path
    output: Path
    audio: Path | None = None
    subtitle: Path | None = None
    fit: str = "audio"  # "audio": the result lasts as long as the voice, "video": as long as the video
    extend: str = "freeze"  # when the video is shorter: "freeze" its last frame or "loop" it
    delay: float = 0.0  # seconds before the voice starts; the subtitles move with it
    tail: float = 0.5  # seconds kept after the voice ends (fit="audio")
    voice_volume: float = 1.0
    original_volume: float = 0.0  # the video's own sound; 0 drops it
    subtitle_mode: str = "burn"  # "burn" draws them into the picture, "soft" adds a track players can toggle
    style: SubStyle = field(default_factory=SubStyle)
    quality: str = "standard"


@dataclass
class Plan:
    args: list[str]  # ffmpeg arguments, run in the work folder
    duration: float  # length of the result in seconds
    files: dict[str, str] = field(default_factory=dict)  # name -> text, written to the work folder first
    notes: list[str] = field(default_factory=list)


def default_output(video: Path, audio: Path | None) -> Path:
    """Name the result after the voice (TTS output) when there is one, never over an input."""
    base = audio or video
    out = base.with_suffix(".mp4")
    if out in (video, audio):
        out = base.with_name(f"{base.stem}_합성.mp4")
    return out


def result_duration(job: Job, video: MediaInfo, audio: MediaInfo | None) -> float:
    if audio is not None and job.fit == "audio":
        return job.delay + audio.duration + job.tail
    return video.duration


def build(job: Job, video: MediaInfo, audio: MediaInfo | None) -> Plan:
    if not video.has_video:
        raise ValueError(f"영상이 들어 있지 않은 파일입니다: {video.path.name}")
    if audio is not None and not audio.has_audio:
        raise ValueError(f"소리가 들어 있지 않은 파일입니다: {audio.path.name}")

    duration = result_duration(job, video, audio)
    if duration <= 0:
        raise ValueError(f"영상 길이를 알 수 없습니다: {video.path.name}\n다른 형식(MP4 등)으로 바꾼 뒤 다시 넣어 주세요.")
    plan = Plan([], duration)
    # The picture may stop before the file does (a longer sound track): hold its last frame from there.
    longer = duration - video.video_duration > 0.05
    loop = longer and job.extend == "loop"
    burn = job.subtitle is not None and job.subtitle_mode == "burn"
    soft = job.subtitle is not None and job.subtitle_mode == "soft"
    if job.subtitle is not None:
        shift = job.delay if audio is not None else 0.0  # "음성 시작" moves the subtitles with the voice only
        plan.files[SUBS_FILE] = subs.make_ass(job.subtitle, job.style, video.width, video.height, shift)

    vf = []
    if longer and not loop:
        vf.append(f"tpad=stop_mode=clone:stop_duration={duration - video.video_duration + 1:.3f}")
    if video.width % 2 or video.height % 2:
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # H.264 in 4:2:0 needs even sides
    if burn:
        vf.append(f"ass={SUBS_FILE}")
    copy_video = not vf and (video.video_codec, video.pixel_format) in COPYABLE
    graph = [] if copy_video else [f"[0:v]{','.join(vf + ['format=yuv420p'])}[vout]"]

    mix = []
    if audio is not None:
        chain = [f"aresample={SAMPLE_RATE}"]
        if job.delay > 0:
            chain.append(f"adelay={round(job.delay * 1000)}:all=1")
        if job.voice_volume != 1:
            chain.append(f"volume={job.voice_volume:.2f}")
        graph.append(f"[1:a]{','.join(chain)}[voice]")
        mix.append("[voice]")
    if video.has_audio and job.original_volume > 0:
        graph.append(f"[0:a]aresample={SAMPLE_RATE},volume={job.original_volume:.2f}[orig]")
        mix.append("[orig]")
    if mix:
        joined = f"amix=inputs={len(mix)}:duration=longest:normalize=0" if len(mix) > 1 else "anull"
        # Pad with silence so the sound runs to the very end, like the picture.
        graph.append(f"{''.join(mix)}{joined},apad=whole_dur={duration:.3f}[aout]")

    args = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning"]
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", str(job.video)]
    if audio is not None:
        args += ["-i", str(job.audio)]
    if soft:
        args += ["-i", SUBS_FILE]
    if graph:
        args += ["-filter_complex", ";".join(graph)]

    args += ["-map", "0:v:0" if copy_video else "[vout]"]
    if mix:
        args += ["-map", "[aout]"]
    if soft:
        args += ["-map", f"{2 if audio is not None else 1}:s"]

    if copy_video:
        args += ["-c:v", "copy"]
        plan.notes.append("영상은 다시 인코딩하지 않고 그대로 복사합니다.")
    else:
        preset, crf = QUALITIES.get(job.quality, QUALITIES["standard"])
        args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
    if mix:
        args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    if soft:
        args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=kor"]
    args += ["-t", f"{duration:.3f}", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats"]
    args.append(str(job.output))
    plan.args = args
    return plan


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def run(
    job: Job,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Plan:
    """Make the video. Progress is reported as a fraction from 0 to 1."""
    # FFmpeg runs inside a temporary folder, so relative paths (as typed on the command line) must go.
    job = replace(
        job,
        video=Path(job.video).resolve(),
        output=Path(job.output).resolve(),
        audio=Path(job.audio).resolve() if job.audio else None,
        subtitle=Path(job.subtitle).resolve() if job.subtitle else None,
    )
    video = media.probe(job.video)
    audio = media.probe(job.audio) if job.audio else None
    if job.output.resolve() in {p.resolve() for p in (job.video, job.audio, job.subtitle) if p}:
        raise ValueError("결과 파일이 입력 파일과 같습니다. 다른 이름으로 저장하세요.")
    job.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
        work = Path(tmp)
        plan = build(job, video, audio)
        for name, text in plan.files.items():
            (work / name).write_text(text, encoding="utf-8")
        try:
            ffmpeg.run(plan.args, work, plan.duration, on_progress, is_cancelled)
        except (Cancelled, RuntimeError):
            _remove(job.output)
            raise
    if on_progress:
        on_progress(1.0)
    return plan


def preview_frame(video: MediaInfo, at: float, text: str | None, style: SubStyle) -> bytes:
    """One full-size frame of the video as BMP, with `text` drawn in `style` exactly as libass will draw it."""
    at = min(max(0.0, at), max(0.0, video.video_duration - 0.1))  # past the picture's end: its last frame
    with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
        work = Path(tmp)
        filters = []
        if text:
            (work / SUBS_FILE).write_text(subs.sample_ass(text, style, video.width, video.height), encoding="utf-8")
            filters = ["-vf", f"ass={SUBS_FILE}"]
        # BMP is uncompressed, so even a 4K frame comes back quickly.
        proc = subprocess.run(
            [
                paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", f"{at:.3f}", "-i", str(video.path),
                "-frames:v", "1", *filters, "-f", "image2pipe", "-c:v", "bmp", "pipe:1",
            ],
            cwd=work,
            capture_output=True,
            creationflags=media.NO_WINDOW,
        )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "미리보기를 만들지 못했습니다.")
    return proc.stdout
