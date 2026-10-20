"""Filmstrips of clips for the timeline: a row of frames spread over each clip, made in the background."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from . import media, paths

STRIP_FRAMES = 24  # frames per clip; the timeline picks from these
STRIP_HEIGHT = 96  # pixels; sharp enough for the timeline on a high-DPI screen
STRIP_TIMEOUT = 120  # seconds before giving up on one clip


def filmstrip(
    source: str, length: float, count: int, height: int, started: Callable[[subprocess.Popen], None] | None = None
) -> QImage:
    """`count` frames spread evenly over the clip, side by side, each `height` pixels tall.

    `started` is handed the ffmpeg process, so a caller can kill it to stop early.
    """
    args = [paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error"]
    for i in range(count):
        # One decoding thread per input: with 24 inputs of a 4K clip, the default takes gigabytes of memory.
        args += ["-threads", "1", "-ss", f"{(i + 0.5) * length / count:.3f}", "-i", source]
    tiles = [f"[{i}:v]trim=end_frame=1,scale=-2:{height},setsar=1[f{i}]" for i in range(count)]
    joined = "".join(f"[f{i}]" for i in range(count))
    graph = ";".join(tiles) + (f";{joined}hstack=inputs={count}[v]" if count > 1 else ";[f0]null[v]")
    proc = subprocess.Popen(
        [*args, "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "bmp", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=media.NO_WINDOW,
    )
    if started:
        started(proc)
    try:
        out, err = proc.communicate(timeout=STRIP_TIMEOUT)
    except subprocess.TimeoutExpired:  # e.g. a network drive that stopped answering
        proc.kill()
        out, err = proc.communicate()
    image = QImage.fromData(out, "BMP")
    if proc.returncode != 0 or image.isNull():
        raise RuntimeError(err.decode("utf-8", errors="replace").strip() or "클립 그림을 만들지 못했습니다.")
    return image


class StripThread(QThread):
    """Makes the filmstrips of the given clips one after another."""

    ready = Signal(str, object)  # source, QImage or None

    def __init__(self, jobs: list[tuple[str, float]], parent=None):
        super().__init__(parent)
        self.jobs = jobs  # (source, length)
        self._proc: subprocess.Popen | None = None

    def stop(self) -> None:
        """Stop soon, killing the ffmpeg at work: one big clip can take a while."""
        self.requestInterruption()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _started(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        if self.isInterruptionRequested():
            proc.kill()

    def run(self):
        for source, length in self.jobs:
            if self.isInterruptionRequested():
                return
            try:
                image = filmstrip(source, length, STRIP_FRAMES, STRIP_HEIGHT, self._started)
            except Exception:  # noqa: BLE001 - the timeline works without pictures
                image = None
            if self.isInterruptionRequested():
                return
            self.ready.emit(source, image)
