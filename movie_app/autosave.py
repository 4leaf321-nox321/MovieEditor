"""Keeping work safe while it is being done: the scenes and the script are saved as they change, so a crash or
a power cut loses at most a few seconds.

Files are written whole under a temporary name and then swapped in, so a crash in the middle of saving
leaves the previous copy. A lock file left while the program runs tells the next start whether it was
closed properly, and whether another copy of the program is running now (that copy then keeps its work
in files of its own, so the two do not save over each other).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from . import paths

BACKUP_FOLDER = "자동저장"
KEEP = 20  # backups kept per kind of file
STILL_ACTIVE = 259
QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION


def write(path: Path, text: str) -> None:
    """Save `text` so that the file is always either the old or the new version, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    with partial.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # on the disk before it replaces the old file, even if the power goes
    os.replace(partial, path)


def backup_folder() -> Path:
    return paths.WORK_DIR / BACKUP_FOLDER


def backup(text: str, name: str, suffix: str) -> Path | None:
    """Keep a dated copy (for going back to an older state); only the newest KEEP of each kind stay."""
    if not text.strip():
        return None
    folder = backup_folder()
    previous = sorted(folder.glob(f"{name}_*{suffix}"))
    if previous and previous[-1].read_text(encoding="utf-8", errors="replace") == text:
        return previous[-1]  # nothing new since the last copy
    path = folder / f"{name}_{datetime.now():%Y%m%d_%H%M%S}{suffix}"
    write(path, text)
    for old in sorted(folder.glob(f"{name}_*{suffix}"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def _kernel():
    """kernel32 with the prototypes used here (handles are pointers; the default int would cut them)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel


def _started(pid: int) -> int | None:
    """When process `pid` started (Windows file time), or None when it is not running.

    A process number is used again once its process has ended; with the start time, a lock left by a
    program that crashed cannot be mistaken for a later process that happens to get the same number.
    """
    kernel = _kernel()
    handle = kernel.OpenProcess(QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _stamp(pid: int) -> str:
    return f"{pid} {_started(pid) or 0}"


def _ours_running(stamp: str) -> bool:
    """Whether the program that wrote the lock `stamp` ("pid start-time") is still running."""
    parts = stamp.split()
    if len(parts) == 1 and parts[0].isdigit():
        # Written by an older version (the pid alone): running if that process still is.
        pid = int(parts[0])
        return pid != os.getpid() and _started(pid) is not None
    try:
        pid, started = (int(part) for part in parts)
    except ValueError:
        return False
    return pid != os.getpid() and started != 0 and _started(pid) == started


class Session:
    """This run of the program, as the autosaves see it."""

    def __init__(self):
        self.lock = paths.WORK_DIR / "실행 중.lock"
        self.crashed = False  # the last run ended without closing properly
        self.other = False  # another copy of the program is running now
        self.stamp = _stamp(os.getpid())
        try:
            stamp = self.lock.read_text(encoding="utf-8").strip()
        except OSError:
            stamp = None
        if stamp is not None:
            if _ours_running(stamp):
                self.other = True
            else:
                self.crashed = True
        if not self.other:
            try:
                write(self.lock, self.stamp)
            except OSError:
                pass  # a read-only folder: autosaves fail too, and say so where they are written

    def file(self, path: Path) -> Path:
        """Where this run keeps what would otherwise go in `path`."""
        if not self.other:
            return path
        return path.with_name(f"{path.stem}_창{os.getpid()}{path.suffix}")

    def close(self) -> None:
        """The program is closing properly (the lock goes only if it is still this run's)."""
        try:
            if self.lock.read_text(encoding="utf-8").strip() == self.stamp:
                self.lock.unlink()
        except OSError:
            pass


_session: Session | None = None


def session() -> Session:
    """The current run (made the first time it is asked for)."""
    global _session
    if _session is None:
        _session = Session()
    return _session
