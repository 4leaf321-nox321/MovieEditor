"""Undo and redo for the video tab: a snapshot of the whole project after every change.

Changes that come in a quick burst under the same key (typing, dragging a slider) make one step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

MERGE_SECONDS = 1.5
LIMIT = 300


@dataclass
class Snapshot:
    project: str  # the project as JSON
    current: int  # the scene being edited
    selection: tuple[int, ...]


class History:
    def __init__(self):
        self._undo: list[Snapshot] = []  # the last one is the state shown now
        self._redo: list[Snapshot] = []
        self._key: str | None = None
        self._time = 0.0

    def reset(self, snapshot: Snapshot) -> None:
        self._undo = [snapshot]
        self._redo.clear()
        self._key = None

    def record(self, snapshot: Snapshot, key: str | None = None) -> bool:
        """Remember the state after a change; returns False when the project did not actually change."""
        if self._undo and self._undo[-1].project == snapshot.project:
            self._undo[-1] = snapshot  # only the selection moved
            return False
        now = time.monotonic()
        if key is not None and key == self._key and now - self._time < MERGE_SECONDS and len(self._undo) > 1:
            self._undo[-1] = snapshot
        else:
            self._undo.append(snapshot)
            del self._undo[:-LIMIT]
        self._redo.clear()
        self._key, self._time = key, now
        return True

    def note_selection(self, current: int, selection: tuple[int, ...], project: str) -> None:
        """Remember which scenes are chosen now, so undoing back to this state chooses them again.

        Only for a change of selection alone: in the middle of an edit (the project already changed but
        not yet recorded) the last state must keep the selection it had.
        """
        if self._undo and self._undo[-1].project == project:
            self._undo[-1].current, self._undo[-1].selection = current, selection

    def undo(self) -> Snapshot | None:
        if len(self._undo) < 2:
            return None
        self._redo.append(self._undo.pop())
        self._key = None
        return self._undo[-1]

    def redo(self) -> Snapshot | None:
        if not self._redo:
            return None
        self._undo.append(self._redo.pop())
        self._key = None
        return self._undo[-1]

    def can_undo(self) -> bool:
        return len(self._undo) > 1

    def can_redo(self) -> bool:
        return bool(self._redo)
