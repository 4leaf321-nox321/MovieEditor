"""MeloTTS Korean voice, run from ONNX exports (see tools/melo_export) without torch."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .frontend import KoreanFrontend, ModelInput

MAX_BERT_TOKENS = 512
MODEL_FILES = ("melo_ko.onnx", "melo_ko_dur.onnx", "bert_ko.onnx", "tokenizer.json", "melo.json")
_G2P_DIR = Path(__file__).resolve().parent / "g2pk"


def is_available(model_dir: Path) -> bool:
    return all((model_dir / name).exists() for name in MODEL_FILES)


def _load_cmudict() -> dict[str, list[list[str]]]:
    # Shaped like nltk's cmudict.dict(); g2pkk only reads the first pronunciation.
    cmu = {}
    with open(_G2P_DIR / "cmudict.txt", encoding="utf-8") as f:
        for line in f:
            word, *phones = line.split()
            cmu[word] = [phones]
    return cmu


class MeloEngine:
    def __init__(self, model_dir: Path):
        import mecab
        from tokenizers import Tokenizer

        from .g2pk import G2p

        self.config = json.loads((model_dir / "melo.json").read_text(encoding="utf-8"))
        self.sample_rate: int = self.config["sampling_rate"]
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self._tts = ort.InferenceSession(str(model_dir / "melo_ko.onnx"), opts, providers=providers)
        self._dur = ort.InferenceSession(str(model_dir / "melo_ko_dur.onnx"), opts, providers=providers)
        self._bert = ort.InferenceSession(str(model_dir / "bert_ko.onnx"), opts, providers=providers)
        self._prepared: dict[str, tuple[ModelInput, np.ndarray]] = {}
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.frontend = KoreanFrontend(tokenizer, G2p(mecab.MeCab(), _load_cmudict()), self.config)
        self._lock = threading.Lock()  # MeCab's tagger is not thread-safe

    def _prepare(self, text: str) -> tuple[ModelInput, np.ndarray]:
        """Phones and BERT features of one sentence; cached, since estimates and synthesis share them."""
        cached = self._prepared.get(text)
        if cached is not None:
            return cached
        inp = self.frontend(text)
        n = len(inp.token_ids)
        if n > MAX_BERT_TOKENS:
            raise ValueError("MeloTTS로 읽기에는 문장이 너무 깁니다.")
        hidden = self._bert.run(None, {
            "input_ids": np.array([inp.token_ids], dtype=np.int64),
            "attention_mask": np.ones((1, n), dtype=np.int64),
            "token_type_ids": np.zeros((1, n), dtype=np.int64),
        })[0][0]
        ja_bert = np.repeat(hidden, inp.word2ph, axis=0).T[np.newaxis].astype(np.float32)
        if len(self._prepared) > 2000:
            self._prepared.clear()
        self._prepared[text] = (inp, ja_bert)
        return inp, ja_bert

    def _inputs(self, inp: ModelInput, ja_bert: np.ndarray, speed: float, noise_scale_w: float) -> dict:
        return {
            "x": np.array([inp.phone_ids], dtype=np.int64),
            "x_lengths": np.array([len(inp.phone_ids)], dtype=np.int64),
            "tone": np.array([inp.tone_ids], dtype=np.int64),
            "language": np.array([inp.language_ids], dtype=np.int64),
            "ja_bert": ja_bert,
            "sid": np.array([self.config["speaker_id"]], dtype=np.int64),
            "length_scale": np.array(1.0 / speed, dtype=np.float32),
            "noise_scale_w": np.array(noise_scale_w, dtype=np.float32),
            "sdp_ratio": np.array(self.config["sdp_ratio"], dtype=np.float32),
        }

    def synthesize(self, text: str, speed: float) -> np.ndarray:
        """Synthesize one Korean sentence; returns mono float32 samples at self.sample_rate."""
        with self._lock:
            inp, ja_bert = self._prepare(text)
            feeds = self._inputs(inp, ja_bert, speed, self.config["noise_scale_w"])
            feeds["noise_scale"] = np.array(self.config["noise_scale"], dtype=np.float32)
            audio = self._tts.run(None, feeds)[0][0]
        return np.ascontiguousarray(audio, dtype=np.float32)

    def estimate(self, text: str, speed: float) -> float:
        """Predicted length in seconds, with the duration predictor's random variation switched off."""
        with self._lock:
            inp, ja_bert = self._prepare(text)
            frames = self._dur.run(None, self._inputs(inp, ja_bert, speed, 0.0))[0]
        return float(frames.sum()) * self.config["hop_length"] / self.sample_rate
