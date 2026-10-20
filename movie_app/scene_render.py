"""Renders a scene project into one video: each scene to a cached clip, then all joined with transitions.

A scene that runs into a transition gets that many extra frames, and each transition starts where the
next scene's narration starts, so the finished video is exactly as long as the scenes add up to.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage

from . import canvas, ffmpeg, media, paths, subs
from . import render as compose
from .ffmpeg import Cancelled
from .scenes import FPS, TRANSITIONS, Project, Scene, clip_part, clip_speed, clip_time, frame_counts

__all__ = ["Cancelled", "clip_frame", "preview", "render"]

CACHE_DIR = paths.WORK_DIR / "장면캐시"
PREVIEW_DIR = paths.WORK_DIR / "미리보기"
PREVIEW_CACHE_DIR = PREVIEW_DIR / "캐시"  # kept apart, so making a preview never evicts the full-size clips
PREVIEW_LONG_SIDE = 960
PREVIEW_SCENE_SHARE = 0.85  # of a preview's progress bar; adding the voice and subtitles is the rest
CACHE_VERSION = 2  # bump when scenes are drawn differently, so stale cached clips are not reused
SAMPLE_RATE = 48000
COMMON = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
# Every scene is converted to and tagged as ordinary HD video, so clips from cameras or screen recorders
# with full-range or other colour settings match each other and the image scenes.
TO_HD_COLOUR = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
COLOUR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def _video_codec(preset: str, crf: int) -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", *COLOUR_TAGS]


# (scene clips and partial joins, the finished video): near-lossless for real renders, fast for previews.
# Scene clips share one format so they can be joined directly.
CODECS = {
    False: (_video_codec("veryfast", 16), _video_codec("medium", 18)),
    True: (_video_codec("ultrafast", 23), _video_codec("ultrafast", 23)),
}
SCENE_AUDIO = ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]
SILENCE = ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
ONE_THREAD = ["-threads", "1"]  # per input: each decoder otherwise allocates frames for every CPU thread
BLUR_RADIUS = 24  # behind a clip shown whole, on a frame whose short side is 1080
MAX_LOOPS = 100  # a looping clip is repeated at most this many times in one scene
# Scene clips joined by one ffmpeg run with transitions. Every input stays open while it runs, so more are
# joined a group at a time, and then the groups (this also keeps the command line short).
JOIN_GROUP = 24


@dataclass
class _Piece:
    scene: Scene
    frames: int  # including the frames that overlap the next scene during a transition
    path: Path


@dataclass
class _Part:
    """A stretch of the video ready to be joined: a scene clip, or several already joined."""

    path: Path
    frames: int  # frames it shows before the next part's transition begins
    tail: bool  # carries the extra frames that blend into the next part


def fit_filter(fit: str, width: int, height: int, source: str, out: str) -> str:
    """Filter graph that fits a clip into the frame the same way images are fitted."""
    fill = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if fit == "contain":
        blur = max(2, round(BLUR_RADIUS * min(width, height) / 1080))  # the same softness at any resolution
        return (
            f"{source}fps={FPS},split[bg0][fg0];"
            f"[bg0]{fill},boxblur={blur}:2,eq=brightness=-0.12[bg];"
            f"[fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{out}"
        )
    return f"{source}fps={FPS},{fill},setsar=1{out}"


def clip_frame(scene: Scene, width: int, height: int, at: float) -> QImage:
    """The frame of a clip scene `at` seconds in, fitted and with its words, as the render will show it."""
    info = media.probe(scene.source)
    length = info.video_duration
    moment = min(clip_time(scene, length, at), max(0.0, length - 0.1))
    # Seeking returns the first frame at or after the time; half a frame earlier gets the nearest one.
    seek = max(0.0, moment - 0.5 / (info.fps or FPS))
    proc = subprocess.run(
        [
            paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{seek:.6f}", "-i", scene.source, "-frames:v", "1",
            "-filter_complex", fit_filter(scene.fit, width, height, "[0:v]", "[v]"),
            "-map", "[v]", "-f", "image2pipe", "-c:v", "bmp", "pipe:1",
        ],
        capture_output=True,
        creationflags=media.NO_WINDOW,
    )  # fmt: skip
    frame = QImage.fromData(proc.stdout, "BMP")  # uncompressed: a 4K frame comes back much faster than as PNG
    if proc.returncode != 0 or frame.isNull():
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "클립 화면을 읽지 못했습니다.")
    return canvas.with_text(frame, scene)


def _cache_key(scene: Scene, width: int, height: int, frames: int) -> str:
    data = dataclasses.asdict(scene)
    data.pop("narration")  # does not change the picture
    if scene.source:
        try:
            st = os.stat(scene.source)
            data["file"] = [st.st_size, st.st_mtime_ns]
        except OSError:
            pass
    blob = json.dumps([CACHE_VERSION, data, width, height, FPS, frames], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def _atempo(speed: float) -> list[str]:
    """atempo filters for a speed change; each handles 0.5x to 2x, so bigger changes are chained."""
    if speed <= 0:
        return []
    steps = []
    while speed < 0.5:
        steps.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        steps.append("atempo=2.0")
        speed /= 2.0
    if abs(speed - 1) > 1e-6:
        steps.append(f"atempo={speed:.6f}")
    return steps


def _clip_job(scene: Scene, seconds: float, width: int, height: int, work: Path) -> tuple[list[str], list[str], str]:
    """Inputs, filter graph and audio stream for a clip scene lasting `seconds`."""
    info = media.probe(scene.source)
    length = info.video_duration  # the picture's length; a longer sound track must not count
    start, end = clip_part(scene, length)
    used = end - start
    if length <= 0 or used <= 0:
        raise ValueError(f"클립의 길이를 알 수 없습니다: {Path(scene.source).name}\nMP4 등으로 바꾼 뒤 다시 넣어 주세요.")
    speed = clip_speed(scene, length)
    with_audio = scene.clip_audio and info.has_audio
    # A looping clip reads its part as many times as the scene needs, joined end to end.
    repeats = max(1, min(MAX_LOOPS, math.ceil(seconds * speed / used))) if scene.clip_fill == "loop" else 1
    args = []
    for _ in range(repeats):
        args += [*ONE_THREAD, "-ss", f"{start:.3f}", "-t", f"{used:.3f}", "-i", scene.source]
    graph, video, audio = [], "[0:v]", "[0:a]"
    if repeats > 1:
        streams = "".join(f"[{i}:v]" + (f"[{i}:a]" if with_audio else "") for i in range(repeats))
        graph.append(f"{streams}concat=n={repeats}:v=1:a={int(with_audio)}[cv]" + ("[ca]" if with_audio else ""))
        video, audio = "[cv]", "[ca]"
    graph.append(f"{video}setpts=(PTS-STARTPTS)/{speed:.6f}[sp]")
    graph.append(fit_filter(scene.fit, width, height, "[sp]", "[fit]"))
    # Once the clip runs out, its last frame stays on screen.
    graph.append(f"[fit]tpad=stop_mode=clone:stop_duration={seconds:.3f}[base]")
    layer = canvas.text_layer(scene, width, height)
    if layer is not None:
        layer.save(str(work / "text.png"))
        args += ["-i", "text.png"]
        graph.append(f"[base][{repeats}:v]overlay=0:0,{TO_HD_COLOUR}[v]")
    else:
        graph.append(f"[base]{TO_HD_COLOUR}[v]")
    if with_audio:
        chain = ["asetpts=PTS-STARTPTS", *_atempo(speed)]
        # Recorders often start the sound a little after the picture; keep that gap instead of losing it.
        late = info.audio_start - max(start, info.video_start)
        if late > 0.01:
            chain.append(f"adelay={round(late / speed * 1000)}:all=1")
        chain += [f"aresample={SAMPLE_RATE}", "aformat=channel_layouts=stereo", "apad"]
        graph.append(f"{audio}{','.join(chain)}[a]")
        return args, graph, "[a]"
    return args + SILENCE, graph, f"{repeats + (layer is not None)}:a"


def _scene_job(piece: _Piece, width: int, height: int, work: Path, out: Path,
               codec: list[str]) -> tuple[list[str], Iterable[memoryview] | None]:  # fmt: skip
    scene, seconds = piece.scene, piece.frames / FPS
    args, graph, feed = list(COMMON), [], None
    if scene.kind == "clip" and scene.source:
        inputs, graph, audio = _clip_job(scene, seconds, width, height, work)
        args += inputs
    else:
        if canvas.is_static(scene):
            canvas.render_frame(scene, width, height).save(str(work / "frame.png"))
            args += ["-loop", "1", "-framerate", str(FPS), "-i", "frame.png"]
        else:
            args += ["-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{width}x{height}", "-framerate", str(FPS), "-i", "pipe:0"]
            feed = canvas.frames(scene, width, height, piece.frames)
        graph.append(f"[0:v]{TO_HD_COLOUR}[v]")
        audio = "1:a"
        args += SILENCE
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", audio, *codec, "-r", str(FPS), *SCENE_AUDIO]
    args += ["-frames:v", str(piece.frames), "-t", f"{seconds:.6f}", str(out)]
    return args, feed


def _xfade_args(parts: list[_Part], xfade: str, overlap: int, keep_audio: bool, script: Path,
                output: Path, codec: list[str]) -> list[str]:  # fmt: skip
    """One ffmpeg run joining `parts` with transitions; the filter graph goes in `script`."""
    args = list(COMMON)
    for part in parts:
        args += [*ONE_THREAD, "-i", str(part.path)]
    seconds = overlap / FPS
    graph = [f"[{i}:v]settb=AVTB[s{i}]" for i in range(len(parts))]
    video, audio, start = "[s0]", "[0:a]", 0
    for i in range(1, len(parts)):
        start += parts[i - 1].frames  # the transition begins where part i's first scene begins
        graph.append(f"{video}[s{i}]xfade=transition={xfade}:duration={seconds:.6f}:offset={start / FPS:.6f}[v{i}]")
        video = f"[v{i}]"
        if keep_audio:
            graph.append(f"{audio}[{i}:a]acrossfade=d={seconds:.6f}:c1=tri:c2=tri[a{i}]")
            audio = f"[a{i}]"
    script.write_text(";\n".join(graph), encoding="utf-8")
    args += ["-/filter_complex", str(script), "-map", video]
    args += ["-map", audio] if keep_audio else []
    return [*args, *codec, str(output)]


def _join_passes(count: int) -> int:
    """How many times every frame is encoded while joining `count` scene clips with transitions."""
    passes = 1
    while count > JOIN_GROUP:
        count = math.ceil(count / JOIN_GROUP)
        passes += 1
    return passes


def _check_sources(scenes: list[Scene]) -> None:
    missing = [
        f"{i}번 장면: {s.source}" for i, s in enumerate(scenes, 1) if s.kind in ("image", "clip") and s.source and not Path(s.source).is_file()
    ]
    if missing:
        raise ValueError("파일을 찾을 수 없는 장면이 있습니다.\n" + "\n".join(missing[:10]))


def _check_clip(path: Path, frames: int, number: int) -> None:
    """A scene clip that came out without its picture must not be kept: it would shorten every render."""
    try:
        info = media.probe(path)
    except (OSError, ValueError):
        info = None
    if info is None or not info.has_video or info.video_duration < (frames - 2) / FPS:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"{number}번 장면을 만들지 못했습니다. 영상 클립이라면 고른 구간에 화면이 있는지 확인해 주세요.")


def _prune_cache(cache: Path, keep: set[Path]) -> None:
    for path in cache.glob("*"):
        if path not in keep:
            try:
                path.unlink()
            except OSError:
                pass  # still open somewhere; it goes next time


def render(
    project: Project,
    output: Path,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    size: tuple[int, int] | None = None,
    quick: bool = False,
) -> float:
    """Make the video and return its length in seconds. Progress is reported from 0 to 1.

    `size` overrides the project's frame size; `quick` encodes fast and keeps its own cache, for previews.
    """
    scenes = project.scenes
    if not scenes:
        raise ValueError("장면이 없습니다. 이미지나 영상을 추가하세요.")
    _check_sources(scenes)
    width, height = size or project.size
    cache = PREVIEW_CACHE_DIR if quick else CACHE_DIR
    scene_codec, final_codec = CODECS[quick]
    counts = frame_counts(scenes)
    xfade = TRANSITIONS.get(project.transition)
    overlap = 0
    if xfade and len(scenes) > 1:
        # A transition never takes more than half of the shortest scene.
        overlap = min(round(project.transition_duration * FPS), min(counts) // 2)
        xfade = xfade if overlap > 0 else None
    pieces = []
    for i, (scene, n) in enumerate(zip(scenes, counts)):
        frames = n + (overlap if xfade and i < len(scenes) - 1 else 0)
        pieces.append(_Piece(scene, frames, cache / f"{_cache_key(scene, width, height, frames)}.mov"))
    keep_audio = any(s.kind == "clip" and s.clip_audio for s in scenes)

    todo = [(number, p) for number, p in enumerate(pieces, 1) if not p.path.exists()]
    join_work = sum(counts) * (_join_passes(len(pieces)) if xfade else 1)
    total = sum(p.frames for _, p in todo) + join_work  # scene clips still to make, then the join
    done = 0

    def report(frames_done: float) -> None:
        if on_progress:
            on_progress(min(1.0, frames_done / total))

    def run(args: list[str], frames: int, cwd: Path, feed=None) -> None:
        nonlocal done
        base = done
        ffmpeg.run(args, cwd, frames / FPS, lambda f: report(base + f * frames), is_cancelled, feed)
        done = base + frames

    cache.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
        work = Path(tmp)
        for number, piece in todo:
            part = piece.path.with_name(piece.path.stem + ".part.mov")
            args, feed = _scene_job(piece, width, height, work, part, scene_codec)
            try:
                run(args, piece.frames, work, feed)
                _check_clip(part, piece.frames, number)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            os.replace(part, piece.path)
        try:
            _join(pieces, counts, xfade, overlap, keep_audio, work, output, scene_codec, final_codec, run)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    _prune_cache(cache, {p.path for p in pieces})
    report(total)
    return sum(counts) / FPS


def _join(pieces: list[_Piece], counts: list[int], xfade: str | None, overlap: int, keep_audio: bool,
          work: Path, output: Path, scene_codec: list[str], final_codec: list[str], run) -> None:  # fmt: skip
    audio_map = ["-map", "0:a"] if keep_audio else []
    finish = [*(AUDIO_CODEC if keep_audio else []), "-movflags", "+faststart", str(output)]
    if len(pieces) == 1 or not xfade:
        args = list(COMMON)
        if len(pieces) == 1:
            args += ["-i", str(pieces[0].path)]
        else:
            # The concat demuxer reads paths from a file; inside quotes, ' is written as '\''.
            listing = "".join("file '" + p.path.as_posix().replace("'", "'\\''") + "'\n" for p in pieces)
            (work / "scenes.txt").write_text(listing, encoding="utf-8")
            args += ["-f", "concat", "-safe", "0", "-i", "scenes.txt"]
        run([*args, "-map", "0:v", *audio_map, "-c:v", "copy", *finish], sum(counts), work)
        return

    parts = [_Part(p.path, n, i < len(pieces) - 1) for i, (p, n) in enumerate(zip(pieces, counts))]
    level = 0
    while len(parts) > JOIN_GROUP:
        joined = []
        for k in range(0, len(parts), JOIN_GROUP):
            group = parts[k : k + JOIN_GROUP]
            out = work / f"join_{level}_{k // JOIN_GROUP}.mov"
            frames = sum(p.frames for p in group)
            args = _xfade_args(group, xfade, overlap, keep_audio, work / f"join_{level}_{k}.txt", out, [*scene_codec, *SCENE_AUDIO])
            run(args, frames, work)
            joined.append(_Part(out, frames, group[-1].tail))
        for part in parts:
            if part.path.parent == work:
                part.path.unlink(missing_ok=True)  # an earlier level's group, now joined further
        parts, level = joined, level + 1
    args = _xfade_args(parts, xfade, overlap, keep_audio, work / "join.txt", output, final_codec)
    run([*args[:-1], *finish], sum(p.frames for p in parts), work)


def preview(
    project: Project,
    output: Path,
    voice: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> float:
    """A small, quickly made copy of the video for playing in the app, with the voice and its subtitles."""
    width, height = project.size
    scale = PREVIEW_LONG_SIDE / max(width, height)
    size = (round(width * scale / 2) * 2, round(height * scale / 2) * 2)

    def part(start: float, share: float):
        return (lambda f: on_progress(start + f * share)) if on_progress else None

    if voice is None or not voice.is_file():
        return render(project, output, on_progress, is_cancelled, size=size, quick=True)
    scenes_only = output.with_name(f"{output.stem}_장면{output.suffix}")
    try:
        length = render(project, scenes_only, part(0.0, PREVIEW_SCENE_SHARE), is_cancelled, size=size, quick=True)
        srt = next((voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if voice.with_suffix(s).exists()), None)
        try:
            if srt is not None and not subs.load_cues(srt):
                srt = None
        except ValueError:
            srt = None  # an empty or broken subtitle file next to the voice: preview without subtitles
        # Clip sound a scene asked for is heard in the preview (its level is set later, in the compose tab).
        job = compose.Job(video=scenes_only, output=output, audio=voice, subtitle=srt, fit="video",
                          original_volume=1.0, quality="preview")  # fmt: skip
        compose.run(job, part(PREVIEW_SCENE_SHARE, 1 - PREVIEW_SCENE_SHARE), is_cancelled)
    finally:
        scenes_only.unlink(missing_ok=True)
    return length
