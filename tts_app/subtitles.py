"""SRT subtitle generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cue:
    start: float  # seconds
    end: float
    text: str


def _split_words(text: str, parts: int) -> list[str]:
    """Split text at spaces into `parts` groups of roughly equal length."""
    words = text.split()
    if parts <= 1 or len(words) <= 1:
        return [text]
    groups: list[list[str]] = [[] for _ in range(parts)]
    pos = 0
    for word in words:
        center = pos + len(word) / 2
        groups[min(parts - 1, int(center / len(text) * parts))].append(word)
        pos += len(word) + 1
    return [" ".join(g) for g in groups if g]


def _wrap_two_lines(text: str, line_chars: int) -> str:
    if len(text) <= line_chars:
        return text
    mid = len(text) // 2
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    cut = min(spaces, key=lambda i: abs(i - mid))
    return text[:cut] + "\n" + text[cut + 1 :]


def fit_cues(cues: list[Cue], line_chars: int) -> list[Cue]:
    """Split cues longer than two lines; their time is shared in proportion to text length."""
    out = []
    for cue in cues:
        parts = math.ceil(len(cue.text) / (line_chars * 2))
        pieces = _split_words(cue.text, parts)
        weights = [max(1, len(p.replace(" ", ""))) for p in pieces]
        total = sum(weights)
        t = cue.start
        for piece, w in zip(pieces, weights):
            end = t + (cue.end - cue.start) * w / total
            out.append(Cue(t, end, _wrap_two_lines(piece, line_chars)))
            t = end
    return out


def _timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue], line_chars: int = 30) -> str:
    blocks = []
    for i, cue in enumerate(fit_cues(cues, line_chars), 1):
        blocks.append(f"{i}\n{_timestamp(cue.start)} --> {_timestamp(cue.end)}\n{cue.text}\n")
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: str | Path, line_chars: int = 30) -> None:
    Path(path).write_text(to_srt(cues, line_chars), encoding="utf-8")
