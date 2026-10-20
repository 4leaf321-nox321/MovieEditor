"""Minimal seekable audio player on top of sounddevice."""

from __future__ import annotations

import numpy as np
import sounddevice as sd


class Player:
    def __init__(self):
        self._audio = np.zeros(0, dtype=np.float32)
        self._sr = 44100
        self._pos = 0  # next sample to play; advanced by the audio thread
        self._stream: sd.OutputStream | None = None

    def load(self, audio: np.ndarray, sample_rate: int) -> None:
        self.stop()
        self._audio = audio
        self._sr = sample_rate

    @property
    def duration(self) -> float:
        return len(self._audio) / self._sr

    @property
    def position(self) -> float:
        return self._pos / self._sr

    @property
    def is_playing(self) -> bool:
        return self._stream is not None and self._stream.active

    def play(self) -> None:
        if self.is_playing or not len(self._audio):
            return
        self._close()
        if self._pos >= len(self._audio):
            self._pos = 0
        self._stream = sd.OutputStream(
            samplerate=self._sr, channels=1, dtype="float32", callback=self._callback
        )
        self._stream.start()

    def pause(self) -> None:
        self._close()

    def stop(self) -> None:
        self._close()
        self._pos = 0

    def seek(self, seconds: float) -> None:
        self._pos = min(len(self._audio), max(0, int(seconds * self._sr)))

    def _close(self) -> None:
        if self._stream is not None:
            self._stream.abort()
            self._stream.close()
            self._stream = None

    def _callback(self, outdata, frames, _time, _status) -> None:
        chunk = self._audio[self._pos : self._pos + frames]
        n = len(chunk)
        outdata[:n, 0] = chunk
        self._pos += n
        if n < frames:
            outdata[n:] = 0
            raise sd.CallbackStop
