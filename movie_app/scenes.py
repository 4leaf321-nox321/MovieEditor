"""The scene list behind the video tab: what each scene shows, for how long, and timing it to a voice."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

from .subs import Cue

FPS = 30
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
TRANSITIONS = {"none": None, "fade": "fade", "fadeblack": "fadeblack", "slide": "slideleft", "circle": "circleopen"}
KINDS = ("image", "clip", "color")
FITS = ("cover", "contain")
MOTIONS = ("none", "zoom_in", "zoom_out", "pan_right", "pan_left")
TEXT_POSITIONS = ("center", "top", "bottom")
# What a clip does when it runs out before its scene ends.
CLIP_FILLS = ("freeze", "loop", "stretch")
MIN_SPEED, MAX_SPEED = 0.1, 8.0
MIN_CLIP_PART = 0.1  # shortest part of a clip a scene can use, in seconds


@dataclass
class TextStyle:
    font: str = "Malgun Gothic"
    size: int = 80  # pixels on a frame whose short side is 1080
    bold: bool = True
    color: str = "#ffffff"
    position: str = "center"  # one of TEXT_POSITIONS


@dataclass
class Scene:
    kind: str = "color"  # one of KINDS
    duration: float = DEFAULT_DURATION
    source: str = ""  # the image or clip file
    clip_start: float = 0.0  # the part of the clip used: from here...
    clip_end: float = 0.0  # ...to here (0: to the end of the clip)
    speed: float = 1.0  # 0.5 plays it at half speed, twice as long
    clip_fill: str = "freeze"  # one of CLIP_FILLS; "stretch" sets the speed so the part fills the scene
    clip_audio: bool = False  # keep the clip's own sound
    fit: str = "cover"  # "cover" fills the frame and crops; "contain" shows it whole on a blurred copy
    motion: str = "zoom_in"  # slow zoom or pan over an image
    color: str = "#1f2937"  # background of a colour scene
    color2: str = ""  # second colour for a top-to-bottom gradient; "" for a flat colour
    text: str = ""  # words drawn on the picture
    style: TextStyle = field(default_factory=TextStyle)
    narration: str = ""  # the voice lines this scene was timed to

    @property
    def is_blank(self) -> bool:
        return self.kind == "color" and not self.text and not self.color2


@dataclass
class Project:
    scenes: list[Scene] = field(default_factory=list)
    resolution: str = "16:9"
    transition: str = "fade"
    transition_duration: float = 0.5
    lines_per_scene: int = 1
    voice: str = ""  # the voice whose subtitles timed the scenes

    @property
    def size(self) -> tuple[int, int]:
        return RESOLUTIONS.get(self.resolution, RESOLUTIONS["16:9"])

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.scenes)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Project:
        data = json.loads(text)
        scenes = []
        for raw in data.pop("scenes", []):
            style = TextStyle(**_known(TextStyle, raw.pop("style", {})))
            scenes.append(Scene(style=style, **_known(Scene, raw)))
        return cls(scenes=scenes, **_known(cls, data))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Project:
        try:
            return cls.from_json(Path(path).read_text(encoding="utf-8"))
        except (ValueError, TypeError) as e:
            raise ValueError(f"프로젝트 파일을 읽을 수 없습니다: {Path(path).name}\n{e}") from e


def _known(cls, data: dict) -> dict:
    """Drop keys a newer or older version of the app wrote, so old project files still open."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


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


def clip_time(scene: Scene, clip_length: float, at: float) -> float:
    """The moment of the clip file shown `at` seconds into the scene."""
    start, end = clip_part(scene, clip_length)
    used = end - start
    position = at * clip_speed(scene, clip_length)
    if scene.clip_fill == "loop" and used > 0:
        position %= used
    return start + min(position, max(0.0, used - 1 / FPS))  # after the end, its last frame stays


def frame_counts(scenes: list[Scene], fps: int = FPS) -> list[int]:
    """Frames per scene, rounded on the running total so the whole never drifts from the sum of durations."""
    counts, total, done = [], 0.0, 0
    for scene in scenes:
        total += scene.duration
        end = round(total * fps)
        counts.append(max(1, end - done))
        done = end
    return counts
