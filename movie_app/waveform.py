"""Loudness outlines of sound files for the timeline, read with FFmpeg in the background."""

from __future__ import annotations

import subprocess

import numpy as np
from PySide6.QtCore import QThread, Signal

from . import media, paths

RATE = 100  # peaks per second
SAMPLE_RATE = 8000
TIMEOUT = 180


def peaks(path: str) -> np.ndarray:
    """The loudest sample in each 1/RATE of a second, from 0 to 1."""
    proc = subprocess.run(
        [paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-vn",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True, timeout=TIMEOUT, creationflags=media.NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip())
    samples = np.frombuffer(proc.stdout, np.int16)
    step = SAMPLE_RATE // RATE
    whole = len(samples) // step * step
    if not whole:
        return np.zeros(0, np.float32)
    return (np.abs(samples[:whole].astype(np.int32)).reshape(-1, step).max(axis=1) / 32768).astype(np.float32)


class WaveThread(QThread):
    ready = Signal(str, object)  # path, peaks or None

    def __init__(self, files: list[str], parent=None):
        super().__init__(parent)
        self.files = files

    def run(self):
        for path in self.files:
            if self.isInterruptionRequested():
                return
            try:
                result = peaks(path)
            except Exception:  # noqa: BLE001 - the timeline works without the outline
                result = None
            self.ready.emit(path, result)
