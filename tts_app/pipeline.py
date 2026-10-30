"""Turns text segments into finished audio, subtitle cues and per-line timings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
from pedalboard import PitchShift
from pedalboard.io import AudioFile

from . import reading
from .engine import DEFAULT_SPEED, Engine
from .subtitles import Cue
from .textproc import Segment

PEAK_TARGET = 10 ** (-1 / 20)  # -1 dBFS: the level that volume 100% means
# Above 100% volume, peaks past SOFT_KNEE are bent smoothly toward SOFT_CEILING
# instead of clipping (a lookahead-free limiter still lets a few samples clip).
SOFT_KNEE = 0.7
SOFT_CEILING = 0.95
MP3_BITRATE = "192k"


class Cancelled(Exception):
    pass


@dataclass
class Settings:
    voice: str = "F1"
    lang: str = "ko"  # a Supertonic language code, or "auto" for per-sentence detection
    speed: float = DEFAULT_SPEED
    pitch: float = 0.0  # semitones
    volume: float = 1.0  # 1.0 = peak-normalized to -1 dBFS
    steps: int = 8  # diffusion steps: more is slower and slightly cleaner
    sentence_pause: float = 0.3  # seconds
    paragraph_pause: float = 0.8
    numbers: bool | None = None  # read numbers the Korean way (14개 → 열네 개); None: as the reading dictionary says
    readings: dict[str, str] = field(default_factory=dict)  # written → said, for this voice on top of the dictionary


@dataclass
class LineTime:
    start: float
    end: float
    speech: bool  # False when the line holds only pause marks

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Timeline:
    """When each source line is heard, from render() (actual) or estimate() (predicted)."""

    lines: dict[int, LineTime] = field(default_factory=dict)
    total: float = 0.0

    def add(self, line: int, start: float, end: float, speech: bool) -> None:
        known = self.lines.get(line)
        if known is None:
            self.lines[line] = LineTime(start, end, speech)
        else:
            known.start, known.end = min(known.start, start), max(known.end, end)
            known.speech = known.speech or speech


class _Spacing:
    """The silence before each spoken piece: an automatic sentence or paragraph pause,
    or exactly what the pause marks since the previous piece ask for."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.started = False
        self.auto = 0.0
        self.marks: list[tuple[int, float]] = []  # (line, seconds)

    def mark(self, seg: Segment) -> None:
        self.marks.append((seg.line, seg.pause))

    def skip(self, seg: Segment) -> None:
        """A segment with nothing readable still ends its paragraph."""
        if seg.paragraph_end:
            self.auto = max(self.auto, self.settings.paragraph_pause)

    def gap(self) -> float:
        if self.marks:
            return self.marked()
        return self.auto if self.started else 0.0

    def marked(self) -> float:
        return sum(seconds for _, seconds in self.marks)

    def place_marks(self, at: float, timeline: Timeline) -> None:
        for line, seconds in self.marks:
            timeline.add(line, at, at + seconds, speech=False)
            at += seconds

    def spoke(self, seg: Segment) -> None:
        self.started = True
        self.marks = []
        self.auto = self.settings.paragraph_pause if seg.paragraph_end else self.settings.sentence_pause


@dataclass
class Result:
    audio: np.ndarray  # mono float32
    sample_rate: int
    cues: list[Cue]
    timeline: Timeline
    removed_chars: list[str] = field(default_factory=list)
    readings: list[tuple[str, str]] = field(default_factory=list)  # (written, said) for each place read differently

    @property
    def duration(self) -> float:
        return len(self.audio) / self.sample_rate


def render(
    engine: Engine,
    segments: list[Segment],
    settings: Settings,
    on_progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Result:
    sr = engine.sample_rate
    shifter = PitchShift(semitones=settings.pitch) if settings.pitch else None
    pieces: list[np.ndarray] = []
    cues: list[Cue] = []
    removed: set[str] = set()
    timeline = Timeline()
    spacing = _Spacing(settings)
    reader = reading.reader(settings.numbers, settings.readings)
    changed: dict[tuple[str, str], None] = {}
    t = 0  # samples written so far

    def silence(seconds: float) -> None:
        nonlocal t
        n = int(round(seconds * sr))
        if n > 0:
            pieces.append(np.zeros(n, dtype=np.float32))
            t += n

    for i, seg in enumerate(segments):
        if is_cancelled and is_cancelled():
            raise Cancelled
        if seg.is_pause:
            spacing.mark(seg)
            continue
        if on_progress:
            on_progress(i, len(segments), seg.text)

        said, changes = reader.say(seg.to_read, seg.lang)
        changed.update(dict.fromkeys(changes))
        text, bad = engine.clean(said, settings.voice)
        removed.update(bad)
        if not any(ch.isalnum() for ch in text):
            spacing.skip(seg)
            continue

        wav = engine.synthesize(text, settings.voice, seg.lang, settings.speed, settings.steps)
        if shifter is not None:
            wav = shifter(wav[np.newaxis, :], sr)[0]

        spacing.place_marks(t / sr, timeline)
        silence(spacing.gap())
        cues.append(Cue(t / sr, (t + len(wav)) / sr, seg.text))
        timeline.add(seg.line, t / sr, (t + len(wav)) / sr, speech=True)
        pieces.append(wav)
        t += len(wav)
        spacing.spoke(seg)

    if on_progress:
        on_progress(len(segments), len(segments), "")
    if not cues:
        raise ValueError("읽을 수 있는 글자가 없습니다.")
    spacing.place_marks(t / sr, timeline)
    silence(spacing.marked())  # pause marks after the last sentence
    timeline.total = t / sr

    audio = _finalize(np.concatenate(pieces), settings.volume)
    return Result(audio, sr, cues, timeline, sorted(removed), list(changed))


def estimate(
    engine: Engine,
    segments: list[Segment],
    settings: Settings,
    is_cancelled: Callable[[], bool] | None = None,
) -> Timeline:
    """Predict the timeline render() would produce, from the duration models alone (no audio)."""
    timeline = Timeline()
    spacing = _Spacing(settings)
    reader = reading.reader(settings.numbers, settings.readings)
    t = 0.0
    for seg in segments:
        if is_cancelled and is_cancelled():
            raise Cancelled
        if seg.is_pause:
            spacing.mark(seg)
            continue
        length = engine.estimate(reader.say(seg.to_read, seg.lang)[0], settings.voice, seg.lang, settings.speed)
        if length is None:
            spacing.skip(seg)
            continue
        spacing.place_marks(t, timeline)
        t += spacing.gap()
        timeline.add(seg.line, t, t + length, speech=True)
        t += length
        spacing.spoke(seg)
    spacing.place_marks(t, timeline)
    timeline.total = t + spacing.marked()
    return timeline


def _finalize(audio: np.ndarray, volume: float) -> np.ndarray:
    peak = float(np.max(np.abs(audio)))
    if peak > 0:
        audio = audio * (PEAK_TARGET / peak * volume)
    if volume > 1:
        loud = np.abs(audio) > SOFT_KNEE
        span = SOFT_CEILING - SOFT_KNEE
        audio[loud] = np.sign(audio[loud]) * (
            SOFT_KNEE + span * np.tanh((np.abs(audio[loud]) - SOFT_KNEE) / span)
        )
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def export_audio(result: Result, path: str | Path) -> None:
    path = Path(path)
    if path.suffix.lower() == ".mp3":
        with AudioFile(str(path), "w", result.sample_rate, 1, quality=MP3_BITRATE) as f:
            f.write(result.audio[np.newaxis, :])
    else:
        sf.write(str(path), result.audio, result.sample_rate, subtype="PCM_16")
