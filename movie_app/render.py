"""Builds and runs the FFmpeg command that puts a voice track and subtitles on a video.

Besides the voice it mixes in the video's own sound, background music (pushed down while the voice
speaks) and sound effects; it can fade the whole result in from black and out to black, bring its
loudness to a target (measured in a first, sound-only pass), and save it in several formats.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import encoder, ffmpeg, formats, media, paths, subs
from .ffmpeg import Cancelled
from .media import MediaInfo
from .subs import SubStyle

SAMPLE_RATE = 48000
QUALITIES = ("fast", "standard", "high", "preview")  # see encoder.QUALITIES
# Background music is pushed down while the voice speaks: the voice drives a compressor on the music.
DUCKING = "threshold=0.02:ratio=8:attack=20:release=500:makeup=1"
# Loudness targets in LUFS (EBU R128), by name.
LOUDNESS = {"youtube": -14.0, "podcast": -16.0, "broadcast": -24.0}
TRUE_PEAK = -1.5
MEASURE_SHARE = 0.15  # of the progress bar, for measuring the loudness first
SUBS_FILE = "subs.ass"  # written in the work folder; a bare name avoids escaping Windows paths in the filter
SOFT_SUBS_FILE = "subs.srt"

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
    music: Path | None = None  # background music, repeated as long as the video
    music_volume: float = 0.3
    music_duck: bool = True  # quieter while the voice speaks
    music_fade: float = 2.0  # seconds faded in at the start and out at the end
    effects: list[tuple[Path, float, float]] = field(default_factory=list)  # (sound, seconds into the video, volume)
    fade_in: float = 0.0  # seconds coming up from black (and silence) at the start
    fade_out: float = 0.0  # seconds going down to black at the end
    loudness: str = ""  # a key of LOUDNESS to bring the sound to, "" to leave it as mixed
    format: str = "mp4"  # a key of formats.FORMATS


@dataclass
class Plan:
    args: list[str]  # ffmpeg arguments, run in the work folder
    duration: float  # length of the result in seconds
    files: dict[str, str] = field(default_factory=dict)  # name -> text, written to the work folder first
    notes: list[str] = field(default_factory=list)
    has_sound: bool = False


def default_output(video: Path, audio: Path | None, fmt: formats.Format = formats.DEFAULT) -> Path:
    """Name the result after the voice (TTS output) when there is one, never over an input."""
    base = audio or video
    out = base.with_suffix(fmt.suffix)
    if out in (video, audio):
        out = base.with_name(f"{base.stem}_합성{fmt.suffix}")
    return out


def result_duration(job: Job, video: MediaInfo, audio: MediaInfo | None) -> float:
    if audio is not None and job.fit == "audio":
        return job.delay + audio.duration + job.tail
    return video.duration


def _video_codec(fmt: formats.Format, quality: str) -> list[str]:
    if fmt.video == "hevc":
        return encoder.hevc_args(quality)
    if fmt.video == "vp9":
        return encoder.vp9_args(quality)
    if fmt.video == "gif":
        return ["-c:v", "gif"]
    return encoder.video_args(quality)


def build(job: Job, video: MediaInfo, audio: MediaInfo | None, music: MediaInfo | None = None,
          loudnorm: str | None = None, measure: bool = False) -> Plan:  # fmt: skip
    """The FFmpeg run for `job`. `loudnorm` is the loudness filter to finish the sound with; `measure`
    builds the sound-only first pass that measures it instead (the result goes nowhere)."""
    if not video.has_video:
        raise ValueError(f"영상이 들어 있지 않은 파일입니다: {video.path.name}")
    if audio is not None and not audio.has_audio:
        raise ValueError(f"소리가 들어 있지 않은 파일입니다: {audio.path.name}")
    if music is not None and not music.has_audio:
        raise ValueError(f"소리가 들어 있지 않은 파일입니다: {music.path.name}")

    fmt = formats.by_key(job.format)
    duration = result_duration(job, video, audio)
    if duration <= 0:
        raise ValueError(f"영상 길이를 알 수 없습니다: {video.path.name}\n다른 형식(MP4 등)으로 바꾼 뒤 다시 넣어 주세요.")
    plan = Plan([], duration)
    picture = fmt.video is not None and not measure
    # The picture may stop before the file does (a longer sound track): hold its last frame from there.
    longer = duration - video.video_duration > 0.05
    loop = longer and job.extend == "loop"
    # A format without a subtitle track (GIF) gets them drawn into the picture.
    burn = picture and job.subtitle is not None and (job.subtitle_mode == "burn" or fmt.subtitles is None)
    soft = picture and not burn and job.subtitle is not None
    shift = job.delay if audio is not None else 0.0  # "음성 시작" moves the subtitles with the voice only
    if burn:
        plan.files[SUBS_FILE] = subs.make_ass(job.subtitle, job.style, video.width, video.height, shift, duration)
    if soft:
        plan.files[SOFT_SUBS_FILE] = subs.make_srt(job.subtitle, shift, duration)

    graph = []
    copy_video = False
    if picture:
        vf = []
        if longer and not loop:
            vf.append(f"tpad=stop_mode=clone:stop_duration={duration - video.video_duration + 1:.3f}")
        if video.width % 2 or video.height % 2:
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # 4:2:0 video needs even sides
        if burn:
            vf.append(f"ass={SUBS_FILE}")
        vf += _fades(job, duration, "fade")  # after the subtitles, so they fade with the picture
        copy_video = (not vf and fmt.video in ("h264", "hevc") and video.video_codec == fmt.video
                      and video.pixel_format == "yuv420p")  # fmt: skip
        if fmt.video == "gif":
            side = formats.GIF_MAX_SIDE
            vf += [f"fps={formats.GIF_FPS}",
                   f"scale='if(gt(iw,ih),min(iw,{side}),-2)':'if(gt(iw,ih),-2,min(ih,{side}))':flags=lanczos"]  # fmt: skip
            graph.append(f"[0:v]{','.join(vf)},split[gif0][gif1]")
            graph.append("[gif0]palettegen=stats_mode=diff[palette]")
            graph.append("[gif1][palette]paletteuse=dither=bayer:bayer_scale=4[vout]")
        elif not copy_video:
            graph.append(f"[0:v]{','.join(vf + ['format=yuv420p'])}[vout]")

    # Inputs: the video, then the voice, the music, the sound effects and soft subtitles when there are any.
    sound = fmt.audio is not None
    effects = [(path, at, volume) for path, at, volume in job.effects if 0 <= at < duration] if sound else []
    voice_index = 1 if audio is not None else None
    music_index = 1 + (audio is not None) if music is not None else None
    effects_index = 1 + (audio is not None) + (music is not None)
    subs_index = effects_index + len(effects)

    mix = []
    if sound:
        duck = music is not None and audio is not None and job.music_duck
        if audio is not None:
            chain = [f"aresample={SAMPLE_RATE}"]
            if job.delay > 0:
                chain.append(f"adelay={round(job.delay * 1000)}:all=1")
            if job.voice_volume != 1:
                chain.append(f"volume={job.voice_volume:.2f}")
            if duck:
                graph.append(f"[{voice_index}:a]{','.join(chain)},asplit=2[voice][key0]")
                # Silence after the voice keeps the compressor going: it ends when its key does.
                graph.append("[key0]aformat=sample_fmts=fltp:channel_layouts=stereo,apad[key]")
            else:
                graph.append(f"[{voice_index}:a]{','.join(chain)}[voice]")
            mix.append("[voice]")
        if video.has_audio and job.original_volume > 0:
            graph.append(f"[0:a]aresample={SAMPLE_RATE},volume={job.original_volume:.2f}[orig]")
            mix.append("[orig]")
        if music is not None:
            chain = [f"aresample={SAMPLE_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo",
                     f"atrim=0:{duration:.3f}", "asetpts=PTS-STARTPTS", f"volume={job.music_volume:.3f}"]  # fmt: skip
            fade = min(job.music_fade, duration / 3)
            if fade > 0.05:
                chain += [f"afade=t=in:d={fade:.3f}", f"afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"]
            graph.append(f"[{music_index}:a]{','.join(chain)}" + ("[music0]" if duck else "[music]"))
            if duck:
                graph.append(f"[music0][key]sidechaincompress={DUCKING}[music]")
            mix.append("[music]")
            plan.notes.append("배경 음악은 결과 길이만큼 반복합니다.")
        for k, (_, at, volume) in enumerate(effects):
            graph.append(f"[{effects_index + k}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"volume={volume:.3f},adelay={round(at * 1000)}:all=1[fx{k}]")  # fmt: skip
            mix.append(f"[fx{k}]")
    plan.has_sound = bool(mix)
    if mix:
        chain = [f"amix=inputs={len(mix)}:duration=longest:normalize=0" if len(mix) > 1 else "anull"]
        chain.append(f"atrim=0:{duration:.3f}")
        chain += _fades(job, duration, "afade")
        if measure:
            chain.append(f"loudnorm=I={LOUDNESS.get(job.loudness, -14.0)}:TP={TRUE_PEAK}:LRA=11:print_format=json")
        elif loudnorm:
            chain += [loudnorm, f"aresample={SAMPLE_RATE}"]
        # Pad with silence so the sound runs to the very end, like the picture.
        chain.append(f"apad=whole_dur={duration:.3f}")
        graph.append(f"{''.join(mix)}{','.join(chain)}[aout]")

    args = ["-hide_banner", "-nostdin", "-y", "-loglevel", "info" if measure else "warning"]
    if loop and picture:
        args += ["-stream_loop", "-1"]
    args += ["-i", str(job.video)]
    if audio is not None:
        args += ["-i", str(job.audio)]
    if music is not None:
        args += ["-stream_loop", "-1", "-i", str(job.music)]
    for path, _, _ in effects:
        args += ["-i", str(path)]
    if soft:
        args += ["-i", SOFT_SUBS_FILE]
    if graph:
        args += ["-filter_complex", ";".join(graph)]
    if measure:
        plan.args = [*args, "-map", "[aout]", "-vn", "-t", f"{duration:.3f}", "-f", "null", "-"]
        return plan

    if picture:
        args += ["-map", "0:v:0" if copy_video else "[vout]"]
    if mix:
        args += ["-map", "[aout]"]
    if soft:
        args += ["-map", f"{subs_index}:s"]
    if not picture and not mix:
        raise ValueError("소리만 저장하려면 음성, 원본 소리, 배경 음악, 효과음 중 하나는 있어야 합니다.")
    if copy_video:
        args += ["-c:v", "copy"]
        plan.notes.append("영상은 다시 인코딩하지 않고 그대로 복사합니다.")
    elif picture:
        args += _video_codec(fmt, job.quality)
    if mix:
        args += formats.audio_args(fmt)
    if soft:
        args += ["-c:s", fmt.subtitles, "-metadata:s:s:0", "language=kor"]
    args += ["-t", f"{duration:.3f}", *formats.container_args(fmt), "-progress", "pipe:1", "-nostats"]
    args.append(str(job.output))
    plan.args = args
    return plan


def _fades(job: Job, duration: float, name: str) -> list[str]:
    """`fade` (picture) or `afade` (sound) filters for the start and the end of the result."""
    out = []
    if job.fade_in > 0:
        out.append(f"{name}=t=in:st=0:d={min(job.fade_in, duration):.3f}")
    if job.fade_out > 0:
        fade = min(job.fade_out, duration)
        out.append(f"{name}=t=out:st={duration - fade:.3f}:d={fade:.3f}")
    return out


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _measure(job: Job, video: MediaInfo, audio: MediaInfo | None, music: MediaInfo | None, work: Path) -> str | None:
    """The first pass of loudness matching: measure the mixed sound, and return the filter that brings it
    to the target in one even step (or None when there is no sound)."""
    plan = build(job, video, audio, music, measure=True)
    if not plan.has_sound:
        return None
    for name, text in plan.files.items():
        (work / name).write_text(text, encoding="utf-8")
    proc = subprocess.run([paths.tool("ffmpeg"), *plan.args], cwd=work, capture_output=True, creationflags=media.NO_WINDOW)
    report = proc.stderr.decode("utf-8", errors="replace")
    found = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", report)
    if proc.returncode != 0 or not found:
        raise RuntimeError("소리 크기를 재지 못했습니다.\n" + report.strip()[-600:])
    m = json.loads(found.group(0))
    if m["input_i"] in ("-inf", "inf") or float(m["input_i"]) < -70:
        return None  # silence: nothing to bring up
    target = LOUDNESS.get(job.loudness, -14.0)
    return (f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}"
            ":linear=true:print_format=none")  # fmt: skip


def run(
    job: Job,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Plan:
    """Make the video. Progress is reported as a fraction from 0 to 1.

    If the graphics card fails the job, it is made again on the processor.
    """
    try:
        return _run(job, on_progress, is_cancelled)
    except RuntimeError:
        if not encoder.gpu_in_use():
            raise
        with encoder.processor_only():
            return _run(job, on_progress, is_cancelled)


def _run(job: Job, on_progress, is_cancelled) -> Plan:
    # FFmpeg runs inside a temporary folder, so relative paths (as typed on the command line) must go.
    job = replace(
        job,
        video=Path(job.video).resolve(),
        output=Path(job.output).resolve(),
        audio=Path(job.audio).resolve() if job.audio else None,
        subtitle=Path(job.subtitle).resolve() if job.subtitle else None,
        music=Path(job.music).resolve() if job.music else None,
        effects=[(Path(path).resolve(), at, volume) for path, at, volume in job.effects],
    )
    video = media.probe(job.video)
    audio = media.probe(job.audio) if job.audio else None
    music = media.probe(job.music) if job.music else None
    for path, _, _ in job.effects:
        if not path.is_file():
            raise ValueError(f"효과음 파일을 찾을 수 없습니다: {path}")
    if job.output.resolve() in {p.resolve() for p in (job.video, job.audio, job.subtitle, job.music) if p}:
        raise ValueError("결과 파일이 입력 파일과 같습니다. 다른 이름으로 저장하세요.")
    job.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
        work = Path(tmp)
        share, progress = 0.0, on_progress
        loudnorm = None
        if job.loudness in LOUDNESS and formats.by_key(job.format).audio:
            if is_cancelled and is_cancelled():
                raise Cancelled
            loudnorm = _measure(job, video, audio, music, work)
            share = MEASURE_SHARE
            if on_progress:
                on_progress(share)
                progress = lambda f: on_progress(share + f * (1 - share))  # noqa: E731
        plan = build(job, video, audio, music, loudnorm)
        if loudnorm:
            plan.notes.append(f"소리 크기를 {LOUDNESS[job.loudness]:g} LUFS에 맞췄습니다.")
        for name, text in plan.files.items():
            (work / name).write_text(text, encoding="utf-8")
        try:
            ffmpeg.run(plan.args, work, plan.duration, progress, is_cancelled)
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


def subtitle_stills(folder: Path, texts: list[str | None], style: SubStyle, width: int, height: int) -> list[Path]:
    """Stills folder/still000.bmp… (width × height) with texts[k] drawn on still k exactly as libass draws it on the
    video, in one run of FFmpeg: they play one a second, each line shown for its second. Returns the new stills."""
    (folder / SUBS_FILE).write_text(subs.stills_ass(texts, style, width, height), encoding="utf-8")
    proc = subprocess.run(
        [
            paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-framerate", "1", "-i", "still%03d.bmp", "-vf", f"ass={SUBS_FILE}", "-start_number", "0", "burnt%03d.bmp",
        ],
        cwd=folder,
        capture_output=True,
        creationflags=media.NO_WINDOW,
    )  # fmt: skip
    made = [folder / f"burnt{k:03d}.bmp" for k in range(len(texts))]
    if proc.returncode != 0 or not all(p.exists() for p in made):
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "자막을 입히지 못했습니다.")
    return made
