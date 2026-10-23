"""The scene list behind the video tab: what each scene shows, for how long, and timing it to a voice."""

from __future__ import annotations

import copy
import dataclasses
import json
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path

from . import keyframes
from .subs import Cue

FPS = 30  # the default frame rate
FRAME_RATES = (24, 25, 30, 50, 60)
DEFAULT_DURATION = 4.0
MIN_DURATION = 0.5
TAIL = 0.5  # kept after the voice ends; matches the compose tab's default "끝 여백"
SAME_SENTENCE_GAP = 0.05  # the TTS app splits a long sentence into back-to-back subtitle cues
# name -> (width, height)
RESOLUTIONS = {
    "16:9": (1920, 1080),
    "4K": (3840, 2160),
    "720p": (1280, 720),
    "9:16": (1080, 1920),
    "4K-9:16": (2160, 3840),
    "1:1": (1080, 1080),
}
# name -> FFmpeg xfade transition (None: a hard cut)
TRANSITIONS = {
    "none": None,
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "dissolve": "dissolve",
    "slide": "slideleft",
    "slideup": "slideup",
    "wipe": "wipeleft",
    "smooth": "smoothleft",
    "circle": "circleopen",
    "radial": "radial",
    "zoom": "zoomin",
    "pixelize": "pixelize",
    "blur": "hblur",
}
KINDS = ("image", "clip", "color", "graphic")  # graphic: drawn from layers in a style (graphics.py)
FITS = ("cover", "contain")
MOTIONS = ("none", "zoom_in", "zoom_out", "pan_right", "pan_left")
# Where the preset buttons put a text block: the height of its centre, as a share of the frame.
TEXT_PRESETS = {"top": 0.15, "center": 0.5, "bottom": 0.85}
TEXT_EFFECTS = ("none", "fade", "pop", "slide", "type")
OVERLAY_EFFECTS = ("none", "fade", "pop", "slide")  # a video on top only fades in
EXITS = ("none", "fade", "pop", "slide")  # how words and overlays leave as the scene ends; a video only fades
MASK_KINDS = ("mosaic", "blur")
KEY_NEAR = 0.002  # seconds: keyframes this close are the same moment (the playhead moves a frame at a time)
LOOKS = ("none", "bw", "sepia", "warm", "cool", "vivid", "vintage")
ROTATIONS = (0, 90, 180, 270)
# What a clip does when it runs out before its scene ends.
CLIP_FILLS = ("freeze", "loop", "stretch")
MIN_SPEED, MAX_SPEED = 0.1, 8.0
MIN_CLIP_PART = 0.1  # shortest part of a clip a scene can use, in seconds
LOGO_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")


@dataclass
class Keyframe:
    """Where something is, and how big, at a moment of its scene (see keyframes.py)."""

    t: float = 0.0  # seconds into the scene
    x: float = 0.5  # centre, as shares of the frame's width and height
    y: float = 0.5
    scale: float = 1.0  # size, against the size set


@dataclass
class Mask:
    """A part of the picture hidden behind a mosaic or a blur (a face, a number plate)."""

    x: float = 0.5  # centre, as shares of the frame's width and height
    y: float = 0.5
    width: float = 0.25  # shares of the frame's width and height
    height: float = 0.2
    kind: str = "mosaic"  # one of MASK_KINDS
    strength: float = 0.5  # 0 to 1: how coarse the blocks or how strong the blur


@dataclass
class TextStyle:
    font: str = "Malgun Gothic"
    size: int = 80  # pixels on a frame whose short side is 1080
    bold: bool = True
    color: str = "#ffffff"


@dataclass
class TextBox:
    """Words on the picture. A scene can have several, each placed anywhere."""

    text: str = ""
    style: TextStyle = field(default_factory=TextStyle)
    x: float = 0.5  # centre of the block, as shares of the frame's width and height
    y: float = 0.5
    effect: str = "none"  # one of TEXT_EFFECTS: how it comes in when the scene starts
    exit: str = "none"  # one of EXITS: how it leaves as the scene ends
    keys: list[Keyframe] = field(default_factory=list)  # movement across the scene; none: it stays at x, y
    smooth: bool = True  # ease in and out between keyframes


@dataclass
class Overlay:
    """A picture (a logo, a sticker) or a video (picture in picture) laid over a scene."""

    source: str = ""
    kind: str = "image"  # "image" or "clip"
    x: float = 0.75  # centre, as shares of the frame's width and height
    y: float = 0.25
    width: float = 0.35  # share of the frame's width
    opacity: float = 1.0
    border: bool = False  # a white frame around it
    effect: str = "none"  # one of OVERLAY_EFFECTS
    exit: str = "none"  # one of EXITS
    clip_start: float = 0.0  # a video plays from here; it holds its last frame if the scene is longer
    chroma: str = ""  # a colour made see-through (a green screen), "" for none
    chroma_strength: float = 0.25  # how far from that colour still counts as it (0 to 1)
    keys: list[Keyframe] = field(default_factory=list)  # movement across the scene; none: it stays at x, y
    smooth: bool = True


@dataclass
class SoundEffect:
    """A short sound (a ding, a whoosh) at a moment of the video."""

    source: str = ""
    at: float = 0.0  # seconds into the video
    volume: float = 1.0


@dataclass
class Scene:
    kind: str = "color"  # one of KINDS
    duration: float = DEFAULT_DURATION
    source: str = ""  # the image or clip file
    clip_start: float = 0.0  # the part of the clip used: from here...
    clip_end: float = 0.0  # ...to here (0: to the end of the clip)
    speed: float = 1.0  # 0.5 plays it at half speed, twice as long
    clip_fill: str = "freeze"  # one of CLIP_FILLS; "stretch" sets the speed so the part fills the scene
    clip_phase: float = 0.0  # a looping clip starts this many seconds into its part (the back half of a split)
    reverse: bool = False  # play the part backwards
    clip_audio: bool = False  # keep the clip's own sound
    clip_volume: float = 1.0  # of that sound
    fit: str = "cover"  # "cover" fills the frame and crops; "contain" shows it whole on a blurred copy
    motion: str = "zoom_in"  # slow zoom or pan over an image
    motion_from: float = 0.0  # the stretch of the zoom or pan this scene shows (a split scene shows part)
    motion_to: float = 1.0
    color: str = "#1f2937"  # background of a colour scene
    color2: str = ""  # second colour for a top-to-bottom gradient; "" for a flat colour
    texts: list[TextBox] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    masks: list[Mask] = field(default_factory=list)  # parts of the picture under a mosaic or blur
    narration: str = ""  # the voice lines this scene was timed to
    transition: str = ""  # into the next scene, a key of TRANSITIONS; "" follows the project's
    transition_duration: float = 0.0  # seconds; 0 follows the project's
    # Corrections to the image or clip, applied before it is fitted into the frame.
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])  # left, top, right, bottom shares
    rotate: int = 0  # one of ROTATIONS, clockwise
    flip_h: bool = False
    flip_v: bool = False
    brightness: float = 0.0  # -0.5 to 0.5
    contrast: float = 1.0  # 0.5 to 2
    saturation: float = 1.0  # 0 to 2
    look: str = "none"  # one of LOOKS
    # A graphic scene: its style ("" for the default) and its layers (see graphics.py).
    style: str = ""
    layers: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        """All its words, for labels."""
        return "\n".join(t.text.strip() for t in self.texts if t.text.strip())

    @property
    def is_blank(self) -> bool:
        return self.kind == "color" and not self.text and not self.color2 and not self.overlays

    @property
    def adjustments(self) -> tuple:
        """The corrections, as a hashable key; all defaults means none."""
        crop = tuple((list(self.crop) + [0.0] * 4)[:4])  # a damaged file may hold more or fewer
        return (crop, self.rotate, self.flip_h, self.flip_v, self.brightness, self.contrast, self.saturation, self.look)


NO_ADJUSTMENTS = Scene().adjustments


@dataclass
class Project:
    scenes: list[Scene] = field(default_factory=list)
    resolution: str = "16:9"
    fps: int = FPS  # frames per second, one of FRAME_RATES
    transition: str = "fade"
    transition_duration: float = 0.5
    lines_per_scene: int = 1
    voice: str = ""  # the voice whose subtitles timed the scenes
    cues: list[Cue] | None = None  # subtitles edited in the timeline; None uses the voice's subtitle file
    music: str = ""  # background music, looped to the video's length
    music_volume: float = 0.3
    music_duck: bool = True  # quieter while the voice speaks
    music_fade: float = 2.0  # seconds to fade in at the start and out at the end
    sounds: list[SoundEffect] = field(default_factory=list)  # sound effects, by time
    fade_in: float = 0.0  # seconds the video takes to come up from black (and its sound from silence)
    fade_out: float = 0.0  # seconds it takes to go down to black at the end
    logo: str = ""  # a picture kept in one corner for the whole video
    logo_corner: str = "top_right"  # one of LOGO_CORNERS
    logo_size: float = 0.12  # share of the frame's width
    logo_opacity: float = 0.85
    style: str = ""  # the style new graphic scenes get ("" for the default)

    @property
    def size(self) -> tuple[int, int]:
        return RESOLUTIONS.get(self.resolution, RESOLUTIONS["16:9"])

    @property
    def frame_rate(self) -> int:
        return self.fps if self.fps in FRAME_RATES else FPS

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.scenes)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Project:
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
            raise ValueError("장면 프로젝트 파일이 아닙니다.")
        for raw in data["scenes"]:
            _upgrade_scene(raw)
        return from_dict(cls, data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Project:
        try:
            return cls.from_json(Path(path).read_text(encoding="utf-8"))
        except (ValueError, TypeError, AttributeError) as e:
            raise ValueError(f"프로젝트 파일을 읽을 수 없습니다: {Path(path).name}\n{e}") from e


def _upgrade_scene(raw: dict) -> None:
    """Older files kept one text per scene, placed top, centre or bottom."""
    if not isinstance(raw, dict) or "texts" in raw:
        return
    style = dict(raw.get("style") or {})
    position = style.pop("position", "center")
    if str(raw.get("text", "")).strip():
        raw["texts"] = [{"text": raw["text"], "style": style, "y": TEXT_PRESETS.get(position, 0.5)}]


class _Unusable(Exception):
    """A value of the wrong kind; the field keeps its default instead."""


def from_dict(cls, data: dict):
    """A dataclass from JSON, nested ones included.

    Keys an older or newer version wrote are skipped, and values of the wrong kind (a hand-edited or
    damaged file) fall back to the defaults, so a file never opens into objects that fail later.
    """
    if not isinstance(data, dict):
        raise _Unusable
    hints = typing.get_type_hints(cls)
    values = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            try:
                values[f.name] = _convert(hints[f.name], data[f.name])
            except _Unusable:
                pass
    return cls(**values)


def _convert(kind, value):
    if dataclasses.is_dataclass(kind):
        return from_dict(kind, value)
    origin = typing.get_origin(kind)
    if origin is list:
        if not isinstance(value, list):
            raise _Unusable
        (item,) = typing.get_args(kind)
        items = []
        for v in value:
            try:
                items.append(_convert(item, v))
            except _Unusable:
                pass  # one bad entry is left out, the rest kept
        return items
    if origin in (types.UnionType, typing.Union):  # X | None
        if value is None:
            return None
        return _convert(next(a for a in typing.get_args(kind) if a is not type(None)), value)
    if kind is bool:
        if not isinstance(value, (bool, int)):
            raise _Unusable
        return bool(value)
    if kind in (int, float):
        if isinstance(value, bool):
            raise _Unusable
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise _Unusable from None
        if number != number or number in (float("inf"), float("-inf")):
            raise _Unusable
        return int(number) if kind is int else number
    if kind is str:
        if not isinstance(value, str):
            raise _Unusable
        return value
    if kind is dict and not isinstance(value, dict):
        raise _Unusable
    return value


def key_at(item: TextBox | Overlay, t: float) -> Keyframe:
    """The item's keyframe at `t` seconds into its scene; one is added, where the item is then, if there is
    none (so moving the item at another moment makes it travel there)."""
    for key in item.keys:
        if abs(key.t - t) < KEY_NEAR:
            return key
    where = keyframes.sample(item.keys, t, item.smooth) or (item.x, item.y, 1.0)
    key = Keyframe(round(t, 4), *where)
    item.keys.append(key)
    item.keys.sort(key=lambda k: k.t)
    return key


def copy_scene(scene: Scene) -> Scene:
    """A copy sharing nothing with the original (its text boxes and overlays are lists)."""
    return from_dict(Scene, json.loads(json.dumps(dataclasses.asdict(scene))))


def sentence_spans(cues: list[Cue]) -> list[Cue]:
    """Join the subtitle pieces of each sentence back together."""
    spans: list[Cue] = []
    for cue in cues:
        text = " ".join(cue.text.split())
        if spans and cue.start - spans[-1].end < SAME_SENTENCE_GAP:
            spans[-1] = Cue(spans[-1].start, cue.end, f"{spans[-1].text} {text}")
        else:
            spans.append(Cue(cue.start, cue.end, text))
    return spans


def time_to_voice(scenes: list[Scene], cues: list[Cue], voice_duration: float, lines_per_scene: int) -> list[Scene]:
    """One scene per `lines_per_scene` sentences, each lasting until the next one's first word.

    Existing scenes keep their pictures and are retimed in order; blank scenes are added when the
    voice has more sentences, and scenes beyond the voice keep their own length.
    """
    spans = sentence_spans(cues)
    if not spans:
        return list(scenes)
    n = max(1, lines_per_scene)
    groups = [spans[i : i + n] for i in range(0, len(spans), n)]
    # Scene boundaries sit at sentence starts. A scene shorter than MIN_DURATION borrows from the next
    # one instead of pushing everything after it later, so later scenes still start on their sentences.
    bounds = [0.0]
    for group in groups[1:]:
        bounds.append(max(group[0].start, bounds[-1] + MIN_DURATION))
    bounds.append(max(voice_duration, groups[-1][-1].end, bounds[-1]) + TAIL)
    result = list(scenes)
    for i, group in enumerate(groups):
        narration = " ".join(c.text for c in group)
        duration = max(MIN_DURATION, bounds[i + 1] - bounds[i])
        if i < len(result):
            result[i] = dataclasses.replace(result[i], duration=duration, narration=narration)
        else:
            result.append(Scene(duration=duration, narration=narration))
    return result


def clip_part(scene: Scene, clip_length: float) -> tuple[float, float]:
    """The (start, end) of the clip a scene uses, kept inside the file."""
    end = scene.clip_end if 0 < scene.clip_end <= clip_length else clip_length
    start = min(max(0.0, scene.clip_start), max(0.0, end - MIN_CLIP_PART))
    return start, end


def clip_speed(scene: Scene, clip_length: float) -> float:
    """Playback speed of the clip; "stretch" picks the one that makes the used part last the whole scene."""
    if scene.clip_fill == "stretch" and scene.duration > 0:
        start, end = clip_part(scene, clip_length)
        if end > start:  # a clip of unknown length (0) falls back to the normal speed
            return (end - start) / scene.duration
    return min(MAX_SPEED, max(MIN_SPEED, scene.speed))


def clip_time(scene: Scene, clip_length: float, at: float, fps: float = FPS) -> float:
    """The moment of the clip file shown `at` seconds into the scene."""
    start, end = clip_part(scene, clip_length)
    used = end - start
    position = at * clip_speed(scene, clip_length)
    if scene.clip_fill == "loop" and used > 0:
        position = (position + scene.clip_phase) % used
    position = min(position, max(0.0, used - 1 / fps))  # after the end, its last frame stays
    # Played backwards, the part starts from its last frame.
    return max(start, end - 1 / fps - position) if scene.reverse else start + position


def split_scene(scene: Scene, at: float, clip_length: float = 0.0) -> tuple[Scene, Scene]:
    """The scene cut in two `at` seconds in; together they look the same as the whole did."""
    first, second = copy_scene(scene), copy_scene(scene)
    first.duration, second.duration = at, scene.duration - at
    # The zoom or pan carries on across the cut instead of starting over.
    middle = scene.motion_from + (scene.motion_to - scene.motion_from) * at / scene.duration
    first.motion_to = second.motion_from = middle
    first.transition = "none"  # nothing between the two halves
    # Words and overlays come in once, at the start, and leave once, at the end.
    for item in (*second.texts, *second.overlays):
        item.effect = "none"
    for item in (*first.texts, *first.overlays):
        item.exit = "none"
    for item in (*second.texts, *second.overlays):
        for key in item.keys:  # keyframe times count from the scene's start
            key.t = round(key.t - at, 4)
    for overlay in second.overlays:
        if overlay.kind == "clip":
            overlay.clip_start += at
    if scene.kind == "graphic":
        first.layers, second.layers = _split_layers(scene.layers, at, scene.duration)
    if scene.kind == "clip" and clip_length > 0:
        start, end = clip_part(scene, clip_length)
        used = end - start
        travelled = at * clip_speed(scene, clip_length)  # how far into the part the cut falls
        if scene.clip_fill == "loop":
            # Both halves keep the whole part to loop over; the second starts where the first stopped.
            second.clip_phase = (scene.clip_phase + travelled) % used if used > 0 else 0.0
        else:
            # Each half gets its own stretch of the part (with "stretch", each keeps the same speed).
            travelled = min(travelled, used)
            if scene.reverse:  # played backwards: the first half shows the end of the part
                cut = end - travelled
                first.clip_start, first.clip_end = cut, end
                second.clip_start, second.clip_end = start, cut
            else:
                cut = start + travelled
                first.clip_start, first.clip_end = start, cut
                second.clip_start, second.clip_end = cut, end
    return first, second


def _split_layers(layers: list[dict], at: float, duration: float) -> tuple[list[dict], list[dict]]:
    """A graphic scene's layers cut `at` seconds in: the second part carries on where the first stopped
    (nothing comes in again, things already moving keep moving) and the first does not leave at the cut."""
    first, second = [], []
    for layer in layers:
        start, end = float(layer.get("start", 0.0)), float(layer.get("end", duration))
        if start < at:
            first.append({**copy.deepcopy(layer), "end": end})  # leaving stays at the real end, past the cut
        if end > at:
            # "shift" keeps formulas of t going on from where the cut was.
            later = {**copy.deepcopy(layer), "start": round(start - at, 4), "end": round(end - at, 4),
                     "shift": round(float(layer.get("shift", 0.0)) + at, 4)}  # fmt: skip
            if later.get("keys"):
                later["keys"] = [{**k, "t": round(float(k["t"]) - at, 4)} for k in later["keys"] if isinstance(k, dict) and "t" in k]
            second.append(later)
    return first, second


def transition_at(project: Project, index: int) -> tuple[str, float]:
    """The transition (a key of TRANSITIONS) from scene `index` into the next, and its length in seconds."""
    scene = project.scenes[index]
    name = scene.transition if scene.transition in TRANSITIONS else project.transition
    seconds = scene.transition_duration if scene.transition_duration > 0 else project.transition_duration
    return (name if name in TRANSITIONS else "none"), seconds


def frame_counts(scenes: list[Scene], fps: int = FPS) -> list[int]:
    """Frames per scene, rounded on the running total so the whole never drifts from the sum of durations."""
    counts, total, done = [], 0.0, 0
    for scene in scenes:
        total += scene.duration
        end = round(total * fps)
        counts.append(max(1, end - done))
        done = end
    return counts
