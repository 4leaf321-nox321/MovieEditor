"""Renders a scene project into one video: each scene to a cached clip, then all joined with transitions.

A scene that runs into a transition gets that many extra frames, and each transition starts where the
next scene's narration starts, so the finished video is exactly as long as the scenes add up to.

Inside a scene, from the bottom up: the background (an image or colour drawn here, or a clip FFmpeg
fits) with its masked parts, videos laid over it, then the "layer" of pictures and words drawn here.
The project's logo and the fades at the start and end go on last, over the joined video.
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
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from . import adjust, canvas, encoder, ffmpeg, graphics, keyframes, media, paths, subs
from . import render as compose
from .ffmpeg import Cancelled
from .scenes import FPS, TRANSITIONS, Mask, Overlay, Project, Scene, clip_part, clip_speed, clip_time, frame_counts, transition_at
from .subs import SubStyle

__all__ = ["Cancelled", "preview", "render", "still"]

CACHE_DIR = paths.WORK_DIR / "장면캐시"
PREVIEW_DIR = paths.WORK_DIR / "미리보기"
PREVIEW_CACHE_DIR = PREVIEW_DIR / "캐시"  # kept apart, so making a preview never evicts the full-size clips
PREVIEW_LONG_SIDE = 960
PREVIEW_MAX_FPS = 30  # previews of 50 and 60 fps projects are made at 30, which is quicker
PREVIEW_SCENE_SHARE = 0.85  # of a preview's progress bar; adding the voice and subtitles is the rest
CACHE_VERSION = 4  # bump when scenes are drawn differently, so stale cached clips are not reused
SAMPLE_RATE = 48000
COMMON = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
# Every scene is converted to and tagged as ordinary HD video, so clips from cameras or screen recorders
# with full-range or other colour settings match each other and the image scenes.
TO_HD_COLOUR = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
COLOUR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
SCENE_AUDIO = ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]
SILENCE = ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
ONE_THREAD = ["-threads", "1"]  # per input: each decoder otherwise allocates frames for every CPU thread
BLUR_RADIUS = 24  # behind a clip shown whole, on a frame whose short side is 1080
MAX_LOOPS = 100  # a looping clip is repeated at most this many times in one scene
# A clip played backwards is read in pieces this long (seconds), each turned round in memory on its own.
REVERSE_PIECE = 1.0
MAX_CLIP_INPUTS = 400  # pieces of one clip read at once (looping or backwards); more is refused
# Scene clips joined by one ffmpeg run with transitions. Every input stays open while it runs, so more are
# joined a group at a time, and then the groups (this also keeps the command line short).
JOIN_GROUP = 24
# Scene settings that do not change how the scene itself looks (transitions are made when joining).
NOT_IN_PICTURE = ("narration", "transition", "transition_duration")


def _codecs(quick: bool) -> tuple[list[str], list[str]]:
    """(scene clips and partial joins, the finished video): near-lossless for real renders, fast for previews.

    Scene clips share one format so they can be joined directly.
    """
    if quick:
        quick_args = [*encoder.video_args("quick"), *COLOUR_TAGS]
        return quick_args, quick_args
    return [*encoder.video_args("scene"), *COLOUR_TAGS], [*encoder.video_args("final"), *COLOUR_TAGS]


@dataclass
class _Piece:
    scene: Scene
    frames: int  # including the frames that overlap the next scene during a transition
    path: Path
    shown: int  # frames before the transition into the next scene begins (where leaving effects end)
    fps: int


@dataclass
class _Part:
    """A stretch of the video ready to be joined: a scene clip, or several already joined."""

    path: Path
    frames: int  # frames it shows before the next part's transition begins
    tail: int  # extra frames at its end that blend into the next part (0: a straight cut)
    xfade: str | None  # the FFmpeg transition into the next part


@dataclass
class _Inputs:
    """FFmpeg inputs of one run, numbered in the order they are added."""

    args: list[str] = field(default_factory=list)
    count: int = 0

    def add(self, *args: str) -> int:
        self.args += args
        self.count += 1
        return self.count - 1


@dataclass
class _Finish:
    """What goes over the whole joined video: the logo, and fades from and to black."""

    logo: str | None = None  # a PNG in the work folder
    fade_in: float = 0.0
    fade_out: float = 0.0
    seconds: float = 0.0  # the video's length

    def __bool__(self) -> bool:
        return bool(self.logo) or self.fade_in > 0 or self.fade_out > 0

    def video(self) -> list[str]:
        out = []
        if self.fade_in > 0:
            out.append(f"fade=t=in:st=0:d={min(self.fade_in, self.seconds):.3f}")
        if self.fade_out > 0:
            fade = min(self.fade_out, self.seconds)
            out.append(f"fade=t=out:st={self.seconds - fade:.3f}:d={fade:.3f}")
        return out

    def audio(self) -> list[str]:
        out = []
        if self.fade_in > 0:
            out.append(f"afade=t=in:d={min(self.fade_in, self.seconds):.3f}")
        if self.fade_out > 0:
            fade = min(self.fade_out, self.seconds)
            out.append(f"afade=t=out:st={self.seconds - fade:.3f}:d={fade:.3f}")
        return out


def fit_filter(fit: str, width: int, height: int, source: str, out: str, before: list[str] | None = None,
               fps: int = FPS) -> str:  # fmt: skip
    """Filter graph that fits a clip into the frame the same way images are fitted (after `before`)."""
    head = ",".join([*(before or []), f"fps={fps}"])
    fill = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if fit == "contain":
        blur = max(2, round(BLUR_RADIUS * min(width, height) / 1080))  # the same softness at any resolution
        return (
            f"{source}{head},split[bg0][fg0];"
            f"[bg0]{fill},boxblur={blur}:2,eq=brightness=-0.12[bg];"
            f"[fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{out}"
        )
    return f"{source}{head},{fill},setsar=1{out}"


def mask_filter(masks: list[Mask], width: int, height: int, source: str, out: str) -> list[str]:
    """Filters hiding parts of the picture under a mosaic or blur, as canvas.apply_masks does for images."""
    if not masks:
        return [f"{source}null{out}"]
    lines, label = [], source
    for k, mask in enumerate(masks):
        rect = canvas.mask_rect(mask, width, height)
        small_w, small_h = canvas.mask_reduction(mask, width, height)
        down, up = ("neighbor", "neighbor") if mask.kind == "mosaic" else ("area", "bilinear")
        lines.append(f"{label}split[mask_base{k}][mask_cut{k}]")
        lines.append(f"[mask_cut{k}]crop={rect.width()}:{rect.height()}:{rect.x()}:{rect.y()},"
                     f"scale={small_w}:{small_h}:flags={down},scale={rect.width()}:{rect.height()}:flags={up}[mask_part{k}]")  # fmt: skip
        label = out if k == len(masks) - 1 else f"[masked{k}]"
        lines.append(f"[mask_base{k}][mask_part{k}]overlay={rect.x()}:{rect.y()}{label}")
    return lines


def _colour_key(colour: str, strength: float) -> str:
    """FFmpeg's colorkey for a colour made see-through (on RGB, as canvas.colour_key does for pictures)."""
    hex_colour = QColor(colour).name()[1:].upper()
    return f"format=rgba,colorkey=0x{hex_colour}:{canvas.chroma_similarity(strength):.4f}:{canvas.CHROMA_BLEND}"


def _grab(source: str, at: float, graph: str, alpha: bool = False) -> QImage:
    """One frame of a video at `at` seconds, through `graph` (from [0:v] to [v]); `alpha` keeps see-through."""
    info = media.probe(source)
    length = info.video_duration
    moment = min(max(0.0, at), max(0.0, length - 0.1))
    # Seeking returns the first frame at or after the time; half a frame earlier gets the nearest one.
    seek = max(0.0, moment - 0.5 / (info.fps or FPS))
    # BMP is uncompressed (a 4K frame comes back much faster than as PNG), but has no see-through.
    encoding = ["-c:v", "png", "-pix_fmt", "rgba"] if alpha else ["-c:v", "bmp"]
    proc = subprocess.run(
        [
            paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{seek:.6f}", "-i", source, "-frames:v", "1",
            "-filter_complex", graph, "-map", "[v]", "-f", "image2pipe", *encoding, "pipe:1",
        ],
        capture_output=True,
        creationflags=media.NO_WINDOW,
    )  # fmt: skip
    frame = QImage.fromData(proc.stdout, "PNG" if alpha else "BMP")
    if proc.returncode != 0 or frame.isNull():
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "클립 화면을 읽지 못했습니다.")
    return frame


def _overlay_box(overlay: Overlay, width: int, height: int) -> tuple[media.MediaInfo, QRect]:
    info = media.probe(overlay.source)
    return info, canvas.overlay_rect(overlay, info.width, info.height, width, height)


def still(scene: Scene, width: int, height: int, at: float, logo: QImage | None = None) -> QImage:
    """The scene `at` seconds in, as the render draws it (moving things where they are then; entrance and
    exit effects left out, so everything can be seen)."""
    if scene.kind == "clip" and scene.source:
        length = media.probe(scene.source).video_duration
        graph = [fit_filter(scene.fit, width, height, "[0:v]", "[fit]", adjust.filters(scene.adjustments)),
                 *mask_filter(scene.masks, width, height, "[fit]", "[v]")]  # fmt: skip
        frame = _grab(scene.source, clip_time(scene, length, at), ";".join(graph)).convertToFormat(QImage.Format.Format_RGB32)
    else:
        progress = min(1.0, at / scene.duration) if scene.duration else 0.0
        frame = canvas.render_frame(scene, width, height, progress, t=at, with_layer=False)
    groups = canvas.layer_groups(scene)
    if not (groups or logo):
        return frame
    painter = QPainter(frame)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for group in groups:  # in the order the render lays them on
        if isinstance(group, Overlay):
            overlay = canvas.at_time(group, at)
            try:
                info, rect = _overlay_box(overlay, width, height)
                keyed = f"{_colour_key(overlay.chroma, overlay.chroma_strength)}," if overlay.chroma else ""
                picture = _grab(overlay.source, overlay.clip_start + at, f"[0:v]{keyed}scale={rect.width()}:{rect.height()}[v]",
                                alpha=bool(overlay.chroma))  # fmt: skip
            except (OSError, ValueError, RuntimeError):
                continue  # shown without it; the render says what is wrong
            canvas.draw_overlay(painter, overlay, picture, width, height, None, rect)
        else:
            painter.drawImage(0, 0, canvas.layer_image(group, width, height, at, effects=False))
    if logo is not None:
        painter.drawImage(0, 0, logo)
    painter.end()
    return frame


def _cache_key(scene: Scene, width: int, height: int, frames: int, fps: int, codec: list[str]) -> str:
    data = dataclasses.asdict(scene)
    for name in NOT_IN_PICTURE:
        data.pop(name)
    files = [scene.source, *(o.source for o in scene.overlays)]
    if scene.kind == "graphic":
        # Drawn in its style, which lives in its own file and may change, and from the pictures it uses.
        data["style_hash"] = graphics.style_hash(scene.style) if not scene.style or scene.style in graphics.style_names() else ""
        files += graphics.files(scene.layers, scene.style)
    stamps = []
    for path in files:
        try:
            st = os.stat(path)
            stamps.append([st.st_size, st.st_mtime_ns])
        except OSError:
            stamps.append(None)
    data["files"] = stamps
    # The encoder too: clips made by x264 and by a graphics card do not join cleanly without encoding again.
    blob = json.dumps([CACHE_VERSION, data, width, height, fps, frames, codec], sort_keys=True, ensure_ascii=False)
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


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def _clip_graph(scene: Scene, seconds: float, width: int, height: int, inputs: _Inputs,
                fps: int) -> tuple[list[str], str | None]:  # fmt: skip
    """Inputs and filters fitting a clip scene into [base], and its sound's label if it keeps its sound."""
    info = media.probe(scene.source)
    length = info.video_duration  # the picture's length; a longer sound track must not count
    start, end = clip_part(scene, length)
    used = end - start
    if length <= 0 or used <= 0:
        raise ValueError(f"클립의 길이를 알 수 없습니다: {Path(scene.source).name}\nMP4 등으로 바꾼 뒤 다시 넣어 주세요.")
    speed = clip_speed(scene, length)
    with_audio = scene.clip_audio and info.has_audio
    # The stretches of the file shown, in order: the part, or for a loop the part as many times as the
    # scene needs, the first from where an earlier half of a split scene left off.
    stretches = [(start, end)]
    if scene.clip_fill == "loop":
        phase = scene.clip_phase % used
        stretches = [(start, end)] * max(1, min(MAX_LOOPS, math.ceil((seconds * speed + phase) / used)))
        if phase > 1e-3:
            stretches[0] = (start, end - phase) if scene.reverse else (start + phase, end)
    # Played backwards, each stretch is read in short pieces from its end, each turned round on its own:
    # FFmpeg's reverse holds every frame in memory, which for a long stretch would be gigabytes.
    reads = []  # (from, to, backwards)
    for a, b in stretches:
        if not scene.reverse:
            reads.append((a, b, False))
            continue
        t = b
        while t - a > 1e-3:
            reads.append((max(a, t - REVERSE_PIECE), t, True))
            t -= REVERSE_PIECE
    if len(reads) > MAX_CLIP_INPUTS:
        raise ValueError(f"{Path(scene.source).name}: 거꾸로 재생하거나 반복할 부분이 너무 깁니다. "
                         "구간을 줄이거나 장면을 나눠 주세요.")  # fmt: skip
    shrink = ""
    if scene.reverse and info.width and info.height:
        # Frames held for turning round are first made no bigger than the frame needs (in any rotation).
        factor = max(width, height) / min(info.width, info.height)
        if factor < 1:
            shrink = f"scale={_even(info.width * factor)}:{_even(info.height * factor)},"
    graph, streams = [], []
    for a, b, backwards in reads:
        i = inputs.add(*ONE_THREAD, "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", scene.source)
        video, audio = f"[{i}:v]", f"[{i}:a]"
        if backwards:
            graph.append(f"{video}{shrink}reverse[back{i}]")
            video = f"[back{i}]"
            if with_audio:
                graph.append(f"{audio}areverse[aback{i}]")
                audio = f"[aback{i}]"
        streams.append((video, audio))
    if len(streams) > 1:
        joined = "".join(v + (a if with_audio else "") for v, a in streams)
        graph.append(f"{joined}concat=n={len(streams)}:v=1:a={int(with_audio)}[cv]" + ("[ca]" if with_audio else ""))
        video, audio = "[cv]", "[ca]"
    else:
        video, audio = streams[0]
    graph.append(f"{video}setpts=(PTS-STARTPTS)/{speed:.6f}[sp]")
    graph.append(fit_filter(scene.fit, width, height, "[sp]", "[fit]", adjust.filters(scene.adjustments), fps))
    # Once the clip runs out, its last frame stays on screen.
    graph.append(f"[fit]tpad=stop_mode=clone:stop_duration={seconds:.3f}[held]")
    graph += mask_filter(scene.masks, width, height, "[held]", "[base]")
    if not with_audio:
        return graph, None
    chain = ["asetpts=PTS-STARTPTS", *_atempo(speed)]
    if not scene.reverse:
        # Recorders often start the sound a little after the picture; keep that gap instead of losing it.
        late = info.audio_start - max(reads[0][0], info.video_start)
        if late > 0.01:
            chain.append(f"adelay={round(late / speed * 1000)}:all=1")
    chain += [f"aresample={SAMPLE_RATE}", "aformat=channel_layouts=stereo"]
    if abs(scene.clip_volume - 1) > 1e-3:
        chain.append(f"volume={max(0.0, scene.clip_volume):.3f}")
    chain.append("apad")
    graph.append(f"{audio}{','.join(chain)}[a]")
    return graph, "[a]"


def _video_overlay(overlay: Overlay, base: str, out: str, seconds: float, end: float, width: int, height: int,
                   inputs: _Inputs, fps: int) -> list[str]:  # fmt: skip
    """A video laid over the scene (picture in picture), as canvas.draw_overlay draws its still.

    With keyframes its size and place are expressions of the time, worked out frame by frame.
    """
    info, rect = _overlay_box(overlay, width, height)
    if not info.has_video or info.video_duration <= 0:
        raise ValueError(f"겹친 영상을 읽을 수 없습니다: {Path(overlay.source).name}")
    start = min(max(0.0, overlay.clip_start), max(0.0, info.video_duration - 0.1))
    index = inputs.add(*ONE_THREAD, "-ss", f"{start:.3f}", "-i", overlay.source)
    chain = [f"fps={fps}"]
    if overlay.chroma:
        chain.append(_colour_key(overlay.chroma, overlay.chroma_strength))
    b = canvas.border_width(rect) if overlay.border else 0
    moving = keyframes.span(overlay.keys) is not None
    if moving:
        w = keyframes.expression(overlay.keys, "scale", overlay.smooth, rect.width())
        h = keyframes.expression(overlay.keys, "scale", overlay.smooth, rect.height())
        chain.append(f"scale=w='max(2,trunc(({w})/2)*2)':h='max(2,trunc(({h})/2)*2)':eval=frame")
    else:
        chain.append(f"scale={rect.width()}:{rect.height()}")
    chain.append("setsar=1")
    if b:
        chain.append(f"pad=w=iw+{2 * b}:h=ih+{2 * b}:x={b}:y={b}:color=white" + (":eval=frame" if moving else ""))
    # It holds its last frame if the scene is longer, like a clip scene.
    chain += [f"tpad=stop_mode=clone:stop_duration={seconds:.3f}", "format=yuva420p"]
    if overlay.opacity < 0.999:
        chain.append(f"lut=a=val*{max(0.0, overlay.opacity):.3f}")
    if overlay.effect == "fade":
        chain.append(f"fade=t=in:st=0:d={canvas.EFFECT_SECONDS['fade']}:alpha=1")
    if overlay.exit == "fade":
        fade = canvas.EXIT_SECONDS["fade"]
        chain.append(f"fade=t=out:st={max(0.0, end - fade):.3f}:d={fade}:alpha=1")
    label = f"[ov{index}]"
    if moving:
        x = keyframes.expression(overlay.keys, "x", overlay.smooth, width)
        y = keyframes.expression(overlay.keys, "y", overlay.smooth, height)
        place = f"x='({x})-overlay_w/2':y='({y})-overlay_h/2':eval=frame"
    else:
        place = f"x={rect.x() - b}:y={rect.y() - b}"
    return [f"[{index}:v]{','.join(chain)}{label}", f"{base}{label}overlay={place}:eof_action=repeat{out}"]


def _layer_input(scene: Scene, width: int, height: int, shown: int, work: Path, inputs: _Inputs,
                 name: str, fps: int) -> int | None:  # fmt: skip
    """The scene's pictures and words as an input: one PNG, or, when they come in, go away or move, a list of
    PNGs read by FFmpeg's concat demuxer (a frame each while they change, one picture for each still stretch)."""
    if not canvas.has_layer(scene):
        return None
    if not canvas.is_animated(scene):
        canvas.layer_image(scene, width, height).save(str(work / f"{name}.png"))
        return inputs.add("-i", f"{name}.png")
    entries = []  # (file, frames it lasts)
    n = 0
    while n <= shown:  # the frame at `shown` is the scene's very end: everything leaving is gone by then
        last = n + 1
        if canvas.is_settled(scene, n / fps):
            while last <= shown and canvas.is_settled(scene, last / fps):
                last += 1
        file = f"{name}_{n:05d}.png"
        canvas.layer_image(scene, width, height, n / fps).save(str(work / file))
        entries.append((file, last - n))
        n = last
    entries.append(entries[-1])  # the demuxer ignores the last entry's duration; the overlay holds the last frame
    listing = ["ffconcat version 1.0"]
    for file, count in entries:
        # Each PNG gets the video's frame rate: by default it is read at 25 fps and its time rounded to that.
        listing += [f"file '{file}'", f"option framerate {fps}", f"duration {count / fps:.6f}"]
    (work / f"{name}.txt").write_text("\n".join(listing) + "\n", encoding="utf-8")
    return inputs.add("-f", "concat", "-safe", "0", "-i", f"{name}.txt")


def _scene_job(piece: _Piece, width: int, height: int, work: Path, out: Path,
               codec: list[str]) -> tuple[list[str], Iterable[memoryview] | None]:  # fmt: skip
    scene, fps = piece.scene, piece.fps
    seconds = piece.frames / fps
    shown = min(piece.shown or piece.frames, piece.frames)
    inputs, graph, feed, audio = _Inputs(), [], None, None
    pips = canvas.clip_overlays(scene)
    layer_here = False  # drawn into the frames by canvas instead of laid on by FFmpeg
    if scene.kind == "clip" and scene.source:
        graph, audio = _clip_graph(scene, seconds, width, height, inputs, fps)
    else:
        # Words and pictures are drawn with the background, unless videos go between them, or the background
        # stands still while they change (then one background picture and a few layer frames are quicker).
        moving = not canvas.is_static(scene, with_layer=False)
        layer_here = not pips and (moving or not canvas.is_animated(scene))
        if canvas.is_static(scene, with_layer=layer_here):
            canvas.render_frame(scene, width, height, with_layer=layer_here).save(str(work / "frame.png"))
            inputs.add("-loop", "1", "-framerate", str(fps), "-i", "frame.png")
        else:
            inputs.add("-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "pipe:0")
            feed = canvas.frames(scene, width, height, piece.frames, fps, with_layer=layer_here)
        graph.append("[0:v]null[base]")
    video = "[base]"
    if not layer_here:
        # Videos and runs of pictures in their list order, the words on top.
        for k, group in enumerate(canvas.layer_groups(scene)):
            if isinstance(group, Overlay):
                graph += _video_overlay(group, video, f"[g{k}]", seconds, shown / fps, width, height, inputs, fps)
            else:
                layer = _layer_input(group, width, height, shown, work, inputs, f"layer{k}", fps)
                if layer is None:
                    continue
                graph.append(f"{video}[{layer}:v]overlay=0:0:eof_action=repeat[g{k}]")
            video = f"[g{k}]"
    graph.append(f"{video}{TO_HD_COLOUR}[v]")
    if audio is None:
        audio = f"{inputs.add(*SILENCE)}:a"
    # The graph goes in a file: keyframe expressions and many clip pieces make it long.
    (work / "graph.txt").write_text(";\n".join(graph), encoding="utf-8")
    args = [*COMMON, *inputs.args, "-/filter_complex", "graph.txt", "-map", "[v]", "-map", audio]
    args += [*codec, "-r", str(fps), *SCENE_AUDIO, "-frames:v", str(piece.frames), "-t", f"{seconds:.6f}", str(out)]
    return args, feed


def _xfade_args(parts: list[_Part], keep_audio: bool, script: Path, output: Path, codec: list[str], fps: int,
                finish: _Finish | None = None) -> list[str]:  # fmt: skip
    """One ffmpeg run joining `parts`, with a transition or a straight cut between each; the graph goes in `script`."""
    args = list(COMMON)
    for part in parts:
        args += [*ONE_THREAD, "-i", str(part.path)]
    graph = [f"[{i}:v]settb=AVTB[s{i}]" for i in range(len(parts))]
    video, audio, start = "[s0]", "[a0]", 0
    if keep_audio:
        graph.append("[0:a]anull[a0]")  # a label of the graph, even when this group holds a single part
    for i in range(1, len(parts)):
        before = parts[i - 1]
        start += before.frames  # the transition begins where part i's first scene begins
        if before.tail > 0:
            seconds = before.tail / fps
            graph.append(f"{video}[s{i}]xfade=transition={before.xfade}:duration={seconds:.6f}:offset={start / fps:.6f}[v{i}]")
            if keep_audio:
                graph.append(f"{audio}[{i}:a]acrossfade=d={seconds:.6f}:c1=tri:c2=tri[a{i}]")
        else:
            graph.append(f"{video}[s{i}]concat=n=2:v=1:a=0[v{i}]")
            if keep_audio:
                graph.append(f"{audio}[{i}:a]concat=n=2:v=0:a=1[a{i}]")
        video, audio = f"[v{i}]", f"[a{i}]"
    if finish:
        video, audio = _finishing(graph, finish, video, audio if keep_audio else None, len(parts), args)
    script.write_text(";\n".join(graph), encoding="utf-8")
    args += ["-/filter_complex", str(script), "-map", video]
    args += ["-map", audio] if keep_audio else []
    return [*args, *codec, str(output)]


def _finishing(graph: list[str], finish: _Finish, video: str, audio: str | None, logo_index: int,
               args: list[str]) -> tuple[str, str | None]:  # fmt: skip
    """Add the logo and the fades to `graph`; returns the labels of the finished picture and sound."""
    if finish.logo:
        args += ["-i", finish.logo]
        graph.append(f"{video}[{logo_index}:v]overlay=0:0:eof_action=repeat,format=yuv420p[logo]")
        video = "[logo]"
    if finish.video():
        graph.append(f"{video}{','.join(finish.video())}[faded]")
        video = "[faded]"
    if audio is not None and finish.audio():
        graph.append(f"{audio}{','.join(finish.audio())}[afaded]")
        audio = "[afaded]"
    return video, audio


def _join_passes(count: int) -> int:
    """How many times every frame is encoded while joining `count` scene clips with transitions."""
    passes = 1
    while count > JOIN_GROUP:
        count = math.ceil(count / JOIN_GROUP)
        passes += 1
    return passes


def _check_sources(scenes: list[Scene]) -> None:
    missing = []
    for i, scene in enumerate(scenes, 1):
        if scene.kind in ("image", "clip") and scene.source and not Path(scene.source).is_file():
            missing.append(f"{i}번 장면: {scene.source}")
        missing += [f"{i}번 장면의 겹치기: {o.source}" for o in scene.overlays if o.source and not Path(o.source).is_file()]
        if scene.kind == "graphic":
            missing += [f"{i}번 장면의 그래픽: {f}" for f in graphics.files(scene.layers, scene.style) if not Path(f).exists()]
    if missing:
        raise ValueError("파일을 찾을 수 없는 장면이 있습니다.\n" + "\n".join(missing[:10]))
    for i, scene in enumerate(scenes, 1):
        problems = graphics.check_layers(scene.layers, scene.style) if scene.kind == "graphic" else []
        if problems:
            raise ValueError(f"{i}번 장면의 그래픽에 문제가 있습니다.\n" + "\n".join(problems[:10]))


def _check_clip(path: Path, frames: int, number: int, fps: int) -> None:
    """A scene clip that came out without its picture must not be kept: it would shorten every render."""
    try:
        info = media.probe(path)
    except (OSError, ValueError):
        info = None
    if info is None or not info.has_video or info.video_duration < (frames - 2) / fps:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"{number}번 장면을 만들지 못했습니다. 영상 클립이라면 고른 구간에 화면이 있는지 확인해 주세요.")


def _prune_cache(cache: Path, keep: set[Path]) -> None:
    for path in cache.glob("*"):
        if path not in keep:
            try:
                path.unlink()
            except OSError:
                pass  # still open somewhere; it goes next time


def _transitions(project: Project, counts: list[int], fps: int) -> list[tuple[str | None, int]]:
    """(FFmpeg transition, overlapping frames) from each scene into the next; the last has none."""
    out = []
    for i in range(len(counts) - 1):
        name, seconds = transition_at(project, i)
        xfade = TRANSITIONS.get(name)
        # A transition never takes more than half of either scene it joins.
        overlap = min(round(seconds * fps), counts[i] // 2, counts[i + 1] // 2) if xfade else 0
        out.append((xfade, overlap) if overlap > 0 else (None, 0))
    return [*out, (None, 0)]


def render(
    project: Project,
    output: Path,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    size: tuple[int, int] | None = None,
    quick: bool = False,
    fades: bool = True,
) -> float:
    """Make the video and return its length in seconds. Progress is reported from 0 to 1.

    `size` overrides the project's frame size; `quick` encodes fast and keeps its own cache, for previews;
    without `fades` the fades at the start and end are left to whoever finishes the video (the compose tab).
    If the graphics card fails the job, it is made again on the processor.
    """
    try:
        return _render(project, output, on_progress, is_cancelled, size, quick, fades)
    except RuntimeError:
        if not encoder.gpu_in_use():
            raise
        with encoder.processor_only():
            return _render(project, output, on_progress, is_cancelled, size, quick, fades)


def _render(project: Project, output: Path, on_progress, is_cancelled, size, quick: bool, fades: bool) -> float:
    scenes = project.scenes
    if not scenes:
        raise ValueError("장면이 없습니다. 이미지나 영상을 추가하세요.")
    _check_sources(scenes)
    width, height = size or project.size
    fps = min(project.frame_rate, PREVIEW_MAX_FPS) if quick else project.frame_rate
    cache = PREVIEW_CACHE_DIR if quick else CACHE_DIR
    scene_codec, final_codec = _codecs(quick)
    counts = frame_counts(scenes, fps)
    joins = _transitions(project, counts, fps)
    pieces = []
    for scene, n, (_, overlap) in zip(scenes, counts, joins):
        frames = n + overlap
        key = _cache_key(scene, width, height, frames, fps, scene_codec)
        pieces.append(_Piece(scene, frames, cache / f"{key}.mov", n, fps))
    keep_audio = any(s.kind == "clip" and s.clip_audio for s in scenes)
    logo = canvas.logo_layer(project, width, height)
    blended = any(overlap for _, overlap in joins)
    finish = _Finish("logo.png" if logo is not None else None, project.fade_in if fades else 0.0,
                     project.fade_out if fades else 0.0, sum(counts) / fps)  # fmt: skip

    todo = [(number, p) for number, p in enumerate(pieces, 1) if not p.path.exists()]
    join_work = sum(counts) * (_join_passes(len(pieces)) if blended else 1)
    total = sum(p.frames for _, p in todo) + join_work  # scene clips still to make, then the join
    done = 0

    def report(frames_done: float) -> None:
        if on_progress:
            on_progress(min(1.0, frames_done / total))

    def run(args: list[str], frames: int, cwd: Path, feed=None) -> None:
        nonlocal done
        base = done
        ffmpeg.run(args, cwd, frames / fps, lambda f: report(base + f * frames), is_cancelled, feed)
        done = base + frames

    cache.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
        work = Path(tmp)
        for number, piece in todo:
            part = piece.path.with_name(piece.path.stem + ".part.mov")
            scene_work = work / f"scene_{number}"
            scene_work.mkdir()
            args, feed = _scene_job(piece, width, height, scene_work, part, scene_codec)
            try:
                run(args, piece.frames, scene_work, feed)
                _check_clip(part, piece.frames, number, fps)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            os.replace(part, piece.path)
        if logo is not None:
            logo.save(str(work / "logo.png"))
        parts = [_Part(p.path, n, overlap, xfade) for p, n, (xfade, overlap) in zip(pieces, counts, joins)]
        try:
            _join(parts, keep_audio, work, output, scene_codec, final_codec, run, finish, fps)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    _prune_cache(cache, {p.path for p in pieces})
    report(total)
    return sum(counts) / fps


def _join(parts: list[_Part], keep_audio: bool, work: Path, output: Path, scene_codec: list[str],
          final_codec: list[str], run, finish: _Finish, fps: int) -> None:  # fmt: skip
    audio_map = ["-map", "0:a"] if keep_audio else []
    finish_args = [*(AUDIO_CODEC if keep_audio else []), "-movflags", "+faststart", str(output)]
    total = sum(p.frames for p in parts)
    if not any(p.tail for p in parts):
        # Straight cuts only: the clips are put end to end without encoding them again (unless a logo or a
        # fade goes on).
        args = list(COMMON)
        if len(parts) == 1:
            args += ["-i", str(parts[0].path)]
        else:
            # The concat demuxer reads paths from a file; inside quotes, ' is written as '\''.
            listing = "".join("file '" + p.path.as_posix().replace("'", "'\\''") + "'\n" for p in parts)
            (work / "scenes.txt").write_text(listing, encoding="utf-8")
            args += ["-f", "concat", "-safe", "0", "-i", "scenes.txt"]
        if finish:
            graph = []
            video, audio = _finishing(graph, finish, "[0:v]", "[0:a]" if keep_audio else None, 1, args)
            if video == "[0:v]":
                graph.append("[0:v]null[plain]")
                video = "[plain]"
            args += ["-filter_complex", ";".join(graph), "-map", video, *(["-map", audio] if keep_audio else [])]
            run([*args, *final_codec, *finish_args], total, work)
        else:
            run([*args, "-map", "0:v", *audio_map, "-c:v", "copy", *finish_args], total, work)
        return

    level = 0
    while len(parts) > JOIN_GROUP:
        joined = []
        for k in range(0, len(parts), JOIN_GROUP):
            group = parts[k : k + JOIN_GROUP]
            out = work / f"join_{level}_{k // JOIN_GROUP}.mov"
            frames = sum(p.frames for p in group)
            args = _xfade_args(group, keep_audio, work / f"join_{level}_{k}.txt", out, [*scene_codec, *SCENE_AUDIO], fps)
            run(args, frames, work)
            joined.append(_Part(out, frames, group[-1].tail, group[-1].xfade))
        for part in parts:
            if part.path.parent == work:
                part.path.unlink(missing_ok=True)  # an earlier level's group, now joined further
        parts, level = joined, level + 1
    args = _xfade_args(parts, keep_audio, work / "join.txt", output, final_codec, fps, finish)
    run([*args[:-1], *finish_args], total, work)


def preview(
    project: Project,
    output: Path,
    voice: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    subtitle: Path | None = None,
    style: SubStyle | None = None,
) -> float:
    """A small, quickly made copy of the video for playing in the app, with the voice, subtitles and music.

    `subtitle` replaces the subtitle file found next to the voice (e.g. subtitles edited in the timeline).
    """
    width, height = project.size
    scale = PREVIEW_LONG_SIDE / max(width, height)
    size = (round(width * scale / 2) * 2, round(height * scale / 2) * 2)
    music = Path(project.music) if project.music and Path(project.music).is_file() else None
    voice = voice if voice is not None and voice.is_file() else None
    effects = sound_effects(project)

    def part(start: float, share: float):
        return (lambda f: on_progress(start + f * share)) if on_progress else None

    if voice is None and music is None and not effects:
        return render(project, output, on_progress, is_cancelled, size=size, quick=True)
    scenes_only = output.with_name(f"{output.stem}_장면{output.suffix}")
    try:
        # The fades go on in the compose step, over the voice and music too.
        length = render(project, scenes_only, part(0.0, PREVIEW_SCENE_SHARE), is_cancelled, size=size, quick=True,
                        fades=False)  # fmt: skip
        srt = subtitle
        if srt is None and voice is not None:
            srt = next((voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if voice.with_suffix(s).exists()), None)
        try:
            if srt is not None and not subs.load_cues(srt):
                srt = None
        except ValueError:
            srt = None  # an empty or broken subtitle file next to the voice: preview without subtitles
        # Clip sound a scene asked for is heard in the preview (its level is set later, in the compose tab).
        job = compose.Job(
            video=scenes_only, output=output, audio=voice, subtitle=srt, fit="video", original_volume=1.0,
            quality="preview", style=style or SubStyle(), music=music, music_volume=project.music_volume,
            music_duck=project.music_duck, music_fade=project.music_fade, effects=effects,
            fade_in=project.fade_in, fade_out=project.fade_out,
        )  # fmt: skip
        compose.run(job, part(PREVIEW_SCENE_SHARE, 1 - PREVIEW_SCENE_SHARE), is_cancelled)
    finally:
        scenes_only.unlink(missing_ok=True)
    return length


def sound_effects(project: Project) -> list[tuple[Path, float, float]]:
    """The project's sound effects whose files are there, as the compose step takes them."""
    return [(Path(s.source), s.at, s.volume) for s in project.sounds if s.source and Path(s.source).is_file()]
