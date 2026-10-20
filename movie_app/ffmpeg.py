"""Runs FFmpeg with a progress callback, cancellation and, optionally, raw frames fed on stdin."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from . import media, paths

LOG_TAIL_LINES = 12


class Cancelled(Exception):
    pass


def _log_tail(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL_LINES:])


def run(
    args: list[str],
    cwd: Path,
    duration: float,
    on_progress: Callable[[float], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    frames: Iterable[bytes | memoryview] | None = None,
) -> None:
    """Run ffmpeg in `cwd`. `args` should include "-progress pipe:1"; progress is reported from 0 to 1.

    With `frames`, each chunk is written to ffmpeg's stdin (for an input of "pipe:0") before the next
    one is taken, so a generator may reuse one buffer.
    """
    with tempfile.NamedTemporaryFile("w", dir=cwd, suffix=".log", delete=False, encoding="utf-8") as err:
        log = Path(err.name)
        proc = subprocess.Popen(
            [paths.tool("ffmpeg"), *args],
            cwd=cwd,
            stdin=subprocess.PIPE if frames is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            creationflags=media.NO_WINDOW,
        )
    stop = threading.Event()
    writer_error: list[BaseException] = []

    def feed():
        try:
            for chunk in frames:
                if stop.is_set():
                    break
                proc.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass  # ffmpeg stopped reading; its exit code and log say why
        except BaseException as e:  # noqa: BLE001 - re-raised on the calling thread
            writer_error.append(e)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True) if frames is not None else None
    if writer:
        writer.start()
    cancelled = False
    try:
        # -progress prints a block of key=value lines about twice a second.
        for raw in proc.stdout:
            if is_cancelled and is_cancelled():
                cancelled = True
                break
            key, _, value = raw.decode("utf-8", errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and on_progress and duration:
                on_progress(min(1.0, int(value) / 1e6 / duration))
    finally:
        if cancelled or (is_cancelled and is_cancelled()):
            cancelled = True
            stop.set()
            proc.kill()
        code = proc.wait()
        if writer:
            stop.set()
            writer.join()
        tail = _log_tail(log)
        log.unlink(missing_ok=True)
    if writer_error:
        raise writer_error[0]
    if cancelled:
        raise Cancelled
    if code != 0:
        raise RuntimeError(f"FFmpeg가 실패했습니다 (코드 {code}).\n{tail}")
