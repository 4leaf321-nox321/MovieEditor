"""Built-in sound effects, made here with numpy the first time each is used.

They are synthesised rather than shipped, so there is no file to download and nothing to license. Each is
saved as a WAV in the data folder's 효과음 folder and used from there like any sound file.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile

from . import paths

RATE = 48000
PEAK = 0.7  # of full scale
# name -> (menu label, file name)
BUILTIN = {
    "ding": ("띵 (알림)", "띵.wav"),
    "chime": ("딩동", "딩동.wav"),
    "pop": ("뽁", "뽁.wav"),
    "whoosh": ("휙 (넘김)", "휙.wav"),
    "rise": ("뾰로롱 (등장)", "뾰로롱.wav"),
    "click": ("딸깍", "딸깍.wav"),
    "success": ("짠! (성공)", "짠.wav"),
    "wrong": ("삐빅 (틀림)", "삐빅.wav"),
    "boom": ("쿵", "쿵.wav"),
}


def folder() -> Path:
    return paths.DATA_DIR / "효과음"


def path(name: str) -> Path:
    """The effect's WAV file, made now if it is not there yet."""
    file = folder() / BUILTIN[name][1]
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        sound = SYNTHS[name]()
        sound = sound / max(1e-9, float(np.abs(sound).max())) * PEAK
        tail = min(len(sound), RATE // 100)  # 10 ms fade at the end, so it never stops with a click
        sound[-tail:] *= np.linspace(1, 0, tail)
        partial = file.with_name(file.stem + ".part.wav")
        soundfile.write(partial, np.column_stack([sound, sound]).astype(np.float32), RATE, subtype="PCM_16")
        partial.replace(file)
    return file


# ---- synthesis ----------------------------------------------------------------


def _time(seconds: float) -> np.ndarray:
    return np.arange(int(RATE * seconds)) / RATE


def _attack(sound: np.ndarray, ms: float = 4) -> np.ndarray:
    n = min(len(sound), int(RATE * ms / 1000))
    sound[:n] *= np.linspace(0, 1, n)
    return sound


def _bell(freq: float, seconds: float, decay: float, partials=((1, 1.0), (2.76, 0.25), (5.4, 0.08))) -> np.ndarray:
    """A struck bell: a few inharmonic partials dying away, the higher ones faster."""
    t = _time(seconds)
    sound = sum(a * np.sin(2 * np.pi * freq * k * t) * np.exp(-t * k / decay) for k, a in partials)
    return _attack(sound)


def _place(length: float, *parts: tuple[float, np.ndarray]) -> np.ndarray:
    """Mix sounds, each starting at its own time (seconds)."""
    out = np.zeros(int(RATE * length))
    for start, sound in parts:
        i = int(RATE * start)
        n = min(len(sound), len(out) - i)
        out[i : i + n] += sound[:n]
    return out


def _noise(seconds: float, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(int(RATE * seconds))


def _sweep(start: float, end: float, seconds: float) -> np.ndarray:
    """A sine gliding from one pitch to another."""
    freq = np.geomspace(start, end, int(RATE * seconds))
    return np.sin(2 * np.pi * np.cumsum(freq) / RATE)


def _bandpass(signal: np.ndarray, centres: np.ndarray, q: float = 2.5) -> np.ndarray:
    """A resonant band-pass whose centre follows `centres` sample by sample."""
    out = [0.0] * len(signal)
    y1 = y2 = x1 = x2 = 0.0
    for i, (x, f) in enumerate(zip(signal.tolist(), centres.tolist())):
        w = 2 * math.pi * f / RATE
        alpha = math.sin(w) / (2 * q)
        a0 = 1 + alpha
        b0, a1, a2 = alpha / a0, -2 * math.cos(w) / a0, (1 - alpha) / a0
        y = b0 * x - b0 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        out[i] = y
    return np.array(out)


def _ding() -> np.ndarray:
    return _bell(1568.0, 1.4, 0.45)


def _chime() -> np.ndarray:
    return _place(1.7, (0.0, _bell(1318.5, 1.0, 0.4)), (0.32, _bell(1046.5, 1.35, 0.5)))


def _pop() -> np.ndarray:
    t = _time(0.09)
    body = _sweep(1100, 260, 0.09) * np.exp(-t / 0.025)
    click = np.zeros_like(body)
    click[: RATE // 500] = _noise(1 / 500)[: RATE // 500] * 0.3
    return _attack(body + click, 1)


def _whoosh() -> np.ndarray:
    seconds = 0.6
    t = _time(seconds)
    centres = 500 + 2200 * np.sin(np.pi * t / seconds) ** 2  # up and back down
    envelope = np.sin(np.pi * np.clip(t / seconds, 0, 1)) ** 1.5
    return _bandpass(_bandpass(_noise(seconds), centres), centres) * envelope  # twice: a softer, rounder rush


def _rise() -> np.ndarray:
    notes = (1046.5, 1318.5, 1568.0, 2093.0)
    return _place(0.9, *((0.07 * i, _bell(f, 0.6, 0.18, ((1, 1.0), (2, 0.2)))) for i, f in enumerate(notes)))


def _click() -> np.ndarray:
    t = _time(0.03)
    tick = np.sin(2 * np.pi * 1800 * t) * np.exp(-t / 0.003)
    burst = np.zeros_like(t)
    burst[: RATE // 1000 * 2] = _noise(0.002)[: RATE // 1000 * 2]
    return tick + 0.4 * burst


def _bright(freq: float, seconds: float, decay: float) -> np.ndarray:
    """A short, bright note (odd harmonics, like a triangle wave)."""
    return _bell(freq, seconds, decay, ((1, 1.0), (3, 0.12), (5, 0.05)))


def _success() -> np.ndarray:
    run = [(0.09 * i, _bright(f, 0.3, 0.12)) for i, f in enumerate((523.3, 659.3, 784.0))]
    chord = sum(_bright(f, 1.0, 0.35) for f in (1046.5, 1318.5, 1568.0)) / 2
    return _place(1.35, *run, (0.3, chord))


def _wrong() -> np.ndarray:
    def bip(freq: float) -> np.ndarray:
        t = _time(0.13)
        square = sum(np.sin(2 * np.pi * freq * k * t) / k for k in (1, 3, 5, 7))
        return _attack(square * np.exp(-t / 0.09), 2)

    return _place(0.35, (0.0, bip(440.0)), (0.17, bip(349.2)))


def _boom() -> np.ndarray:
    t = _time(0.9)
    body = _sweep(95, 38, 0.9) * np.exp(-t / 0.3)
    smooth = np.ones(240) / 240  # twice over 5 ms: only the low rumble of the noise is left
    thump = np.convolve(np.convolve(_noise(0.9), smooth, mode="same"), smooth, mode="same")
    thump = thump / max(1e-9, float(np.abs(thump).max())) * np.exp(-t / 0.06) * 0.6
    return _attack(body + thump, 3)


SYNTHS = {
    "ding": _ding,
    "chime": _chime,
    "pop": _pop,
    "whoosh": _whoosh,
    "rise": _rise,
    "click": _click,
    "success": _success,
    "wrong": _wrong,
    "boom": _boom,
}
