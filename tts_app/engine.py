"""Speech engines and the voices they offer.

Supertonic supplies ten built-in voices plus custom ones: style vectors blended from other
voices or imported from Supertonic voice files, kept as JSON in the voices folder. MeloTTS adds
one Korean voice when its exported model is present (see tools/melo_export).
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from supertonic import TTS
from supertonic.config import MAX_SPEED, MIN_SPEED
from supertonic.core import Style

from . import melo, paths

MODEL_NAME = "supertonic-3"
DEFAULT_MODEL_DIR = paths.model_dir(MODEL_NAME)
MELO_MODEL_DIR = paths.model_dir("melotts-ko")
VOICES_DIR = paths.DATA_DIR / "voices"
DEFAULT_SPEED = 1.05

CUSTOM_PREFIX = "custom:"
MELO_VOICE = "melo:KR"
BUILTIN_LABELS = {
    **{f"F{i}": f"여성 {i}" for i in range(1, 6)},
    **{f"M{i}": f"남성 {i}" for i in range(1, 6)},
}
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

__all__ = [
    "Engine", "Voice", "list_voices", "clean_voice_name", "custom_voice_id",
    "MELO_VOICE", "MIN_SPEED", "MAX_SPEED", "DEFAULT_SPEED", "DEFAULT_MODEL_DIR",
]


@dataclass(frozen=True)
class Voice:
    id: str
    label: str

    @property
    def is_custom(self) -> bool:
        return self.id.startswith(CUSTOM_PREFIX)

    @property
    def is_melo(self) -> bool:
        return self.id == MELO_VOICE


def custom_voice_id(name: str) -> str:
    return CUSTOM_PREFIX + name


def clean_voice_name(name: str) -> str:
    """A custom voice's name is also its file name, so drop what Windows forbids there."""
    return _BAD_NAME_CHARS.sub("", name).strip().rstrip(".")[:40]


def _custom_path(voice: str) -> Path:
    return VOICES_DIR / f"{voice[len(CUSTOM_PREFIX):]}.json"


def list_voices() -> list[Voice]:
    """Every voice on offer, readable without loading a model."""
    voices = [Voice(code, label) for code, label in BUILTIN_LABELS.items()]
    if VOICES_DIR.exists():
        voices += [Voice(custom_voice_id(p.stem), f"★ {p.stem}") for p in sorted(VOICES_DIR.glob("*.json"))]
    if melo.is_available(MELO_MODEL_DIR):
        voices.append(Voice(MELO_VOICE, "MeloTTS 한국어"))
    return voices


class Engine:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR):
        # Downloads the Supertonic model (~400 MB) into model_dir on first use.
        self._tts = TTS(model=MODEL_NAME, model_dir=model_dir, auto_download=True)
        self.sample_rate: int = self._tts.sample_rate
        self._styles: dict[str, Style] = {}
        self._durations: dict[tuple[str, str, str], float] = {}  # (text, voice, lang) -> seconds at speed 1
        self._melo: melo.MeloEngine | None = None
        self._lock = threading.Lock()
        self._melo_lock = threading.Lock()

    @staticmethod
    def is_downloaded(model_dir: Path = DEFAULT_MODEL_DIR) -> bool:
        return (model_dir / "onnx" / "vocoder.onnx").exists()

    @property
    def melo_loaded(self) -> bool:
        return self._melo is not None

    def load_melo(self) -> melo.MeloEngine:
        """Load MeloTTS on first use (a few seconds; it is not needed for Supertonic voices)."""
        with self._melo_lock:
            if self._melo is None:
                engine = melo.MeloEngine(MELO_MODEL_DIR)
                if engine.sample_rate != self.sample_rate:
                    raise RuntimeError(
                        f"MeloTTS 샘플레이트({engine.sample_rate})가 Supertonic({self.sample_rate})과 다릅니다."
                    )
                self._melo = engine
            return self._melo

    def clean(self, text: str, voice: str) -> tuple[str, list[str]]:
        """Drop characters the voice's model cannot pronounce (Supertonic rejects the whole text otherwise)."""
        if voice == MELO_VOICE:
            return text, []  # MeloTTS's frontend already skips what it cannot say
        ok, bad = self._tts.model.text_processor.validate_text(text)
        if ok:
            return text, []
        return text.translate({ord(ch): None for ch in bad}), bad

    def synthesize(self, text: str, voice: str, lang: str, speed: float, steps: int) -> np.ndarray:
        """Synthesize one segment; returns mono float32 samples at self.sample_rate."""
        speed = min(MAX_SPEED, max(MIN_SPEED, speed))
        if voice == MELO_VOICE:
            return self.load_melo().synthesize(text, speed)  # Korean only; lang and steps do not apply
        style = self.style(voice)
        with self._lock:
            wav, dur = self._tts.model([text], style, steps, speed, lang)
        # The vocoder pads its output; trim to the predicted speech length.
        n = min(wav.shape[1], int(round(float(dur[0]) * self.sample_rate)))
        return np.ascontiguousarray(wav[0, :n], dtype=np.float32)

    def estimate(self, text: str, voice: str, lang: str, speed: float) -> float | None:
        """Predicted speech length in seconds, from the duration model alone (no audio).

        Exact for Supertonic, whose audio is trimmed to this length; for MeloTTS it leaves out
        the random variation synthesis adds. None when nothing in the text is readable.
        """
        text, _ = self.clean(text, voice)
        if not any(ch.isalnum() for ch in text):
            return None
        speed = min(MAX_SPEED, max(MIN_SPEED, speed))
        if voice == MELO_VOICE:
            return self.load_melo().estimate(text, speed)
        key = (text, voice, lang)
        if key not in self._durations:
            model = self._tts.model
            text_ids, text_mask = model.text_processor([text], lang)
            dur, *_ = model.dp_ort.run(None, {"text_ids": text_ids, "style_dp": self.style(voice).dp, "text_mask": text_mask})
            if len(self._durations) > 50_000:
                self._durations.clear()
            self._durations[key] = float(dur[0])
        return self._durations[key] / speed

    # ---- Supertonic styles and custom voices --------------------------------

    def _forget(self, voice: str) -> None:
        self._styles.pop(voice, None)
        self._durations = {k: v for k, v in self._durations.items() if k[1] != voice}

    def style(self, voice: str) -> Style:
        if voice not in self._styles:
            if voice.startswith(CUSTOM_PREFIX):
                self._styles[voice] = self._tts.get_voice_style_from_path(_custom_path(voice))
            else:
                self._styles[voice] = self._tts.get_voice_style(voice)
        return self._styles[voice]

    def blend(self, mix: dict[str, float]) -> Style:
        """Weighted average of Supertonic voices, e.g. {"F1": 0.7, "F3": 0.3}."""
        total = sum(mix.values())
        styles = {voice: self.style(voice) for voice in mix}
        ttl = sum(styles[v].ttl * (w / total) for v, w in mix.items())
        dp = sum(styles[v].dp * (w / total) for v, w in mix.items())
        return Style(ttl.astype(np.float32), dp.astype(np.float32))

    def use_style(self, voice: str, style: Style) -> None:
        """Offer an unsaved style under a temporary voice id, e.g. to preview a blend."""
        self._forget(voice)
        self._styles[voice] = style

    def save_voice(self, name: str, style: Style, metadata: dict) -> Voice:
        voice = custom_voice_id(name)
        data = {
            "style_ttl": {"dims": list(style.ttl.shape), "data": style.ttl.tolist()},
            "style_dp": {"dims": list(style.dp.shape), "data": style.dp.tolist()},
            "metadata": {"name": name, **metadata},
        }
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        # ASCII-only JSON: Supertonic reads voice files with the system code page.
        _custom_path(voice).write_text(json.dumps(data), encoding="ascii")
        self._forget(voice)
        self._styles[voice] = style
        return Voice(voice, f"★ {name}")

    def import_voice(self, path: Path, name: str) -> Voice:
        try:
            style = self._tts.get_voice_style_from_path(path)
        except (ValueError, OSError, UnicodeDecodeError) as e:
            raise ValueError(f"Supertonic 목소리 파일이 아닙니다.\n\n{e}") from e
        builtin = self.style("F1")
        if style.ttl.shape != builtin.ttl.shape or style.dp.shape != builtin.dp.shape:
            raise ValueError("이 목소리 파일은 다른 버전의 Supertonic용이라 쓸 수 없습니다.")
        voice = custom_voice_id(name)
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, _custom_path(voice))
        self._forget(voice)
        self._styles[voice] = style
        return Voice(voice, f"★ {name}")

    def delete_voice(self, voice: str) -> None:
        _custom_path(voice).unlink(missing_ok=True)
        self._forget(voice)
