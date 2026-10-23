"""The timeline under the video tab's preview.

Scenes sit side by side, each as wide as it lasts; a mark between two shows a transition. Under every
clip is its trim bar (the whole clip, with the part used between two handles), then the voice's
outline with its subtitle lines, then the background music. A red line marks the moment shown.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QScrollBar, QToolTip, QWidget

from .media import format_time
from .scenes import MIN_CLIP_PART, MIN_DURATION, Project, Scene, clip_part, clip_speed, clip_time, sentence_spans
from .subs import Cue
from .waveform import RATE

RULER_H = 22
SCENE_Y, SCENE_H = 26, 64
TRIM_Y, TRIM_H = SCENE_Y + SCENE_H + 6, 28
VOICE_Y, VOICE_H = TRIM_Y + TRIM_H + 6, 34
SOUND_Y, SOUND_H = VOICE_Y + VOICE_H + 6, 24
MUSIC_Y, MUSIC_H = SOUND_Y + SOUND_H + 6, 22
HEIGHT = MUSIC_Y + MUSIC_H + 6
ROWS = (("장면", SCENE_Y, SCENE_H), ("클립 구간", TRIM_Y, TRIM_H), ("음성·자막", VOICE_Y, VOICE_H),
        ("효과음", SOUND_Y, SOUND_H), ("배경 음악", MUSIC_Y, MUSIC_H))  # fmt: skip
MIN_SOUND_W = 14  # a very short sound still gets a block that can be grabbed
PAD = 10  # pixels before 0 s and after the end
EDGE = 6  # how close to a scene's right edge the pointer grabs its length
CUE_EDGE = 5  # the same for a subtitle line's ends
SNAP = 7  # pixels within which a dragged scene end snaps to a sentence boundary
HANDLE_W = 8
MIN_TRIM_WIDTH = 2 * HANDLE_W  # narrower trim bars (a short clip, zoomed far out) are not draggable; zoom in
DRAG_START = 8  # pixels a scene must travel before it is being moved
MIN_PPS, MAX_PPS = 4.0, 600.0  # pixels per second
MIN_CUE = 0.2  # shortest subtitle line, in seconds
TICK_STEPS = (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)
DUCKED = 0.35  # how loud the music outline is drawn while the voice speaks
ACCENT = QColor("#2563eb")
PLAYHEAD = QColor("#ef4444")
BADGE = QColor(17, 24, 39, 170)
OUTSIDE = QColor(17, 24, 39, 175)  # darkens the parts of a clip left out
SELECTED = QColor(37, 99, 235, 55)


class TimelineHeader(QWidget):
    """Row names, kept still to the left of the scrolling timeline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(70, HEIGHT)

    def paintEvent(self, event):
        painter = QPainter(self)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.9)
        painter.setFont(font)
        painter.setPen(QColor("#6b7280"))
        for name, top, height in ROWS:
            painter.drawText(QRectF(0, top, self.width() - 8, height), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, name)
        painter.end()


class Timeline(QWidget):
    sceneClicked = Signal(int, float, int)  # scene, the moment clicked, keyboard modifiers (Ctrl, Shift)
    seeked = Signal(float)  # the ruler or an empty spot was clicked or dragged
    lengthDragged = Signal(int, float, bool)  # scene, new length, drag finished
    trimDragged = Signal(int, float, float, bool)  # scene, clip start, clip end, drag finished
    trimScrubbed = Signal(int, float)  # scene, the clip time under the handle being dragged
    scenesMoved = Signal(list, int)  # the scenes moved (in order), where they go (an index before the move)
    filesDropped = Signal(list, int, float)  # paths, the scene they were dropped on (-1: after the last), the moment
    soundDragged = Signal(int, float, bool)  # sound effect, its new time, drag finished
    soundActivated = Signal(int)  # double click on a sound effect (to hear it)
    soundMenu = Signal(int, float, QPoint)  # right click in the effects row: the effect (-1: none), the moment, where
    sceneMenu = Signal(int, QPoint)  # right click on a scene (-1: on no scene), at this screen position
    cueDragged = Signal(int, float, float, bool)  # subtitle line, start, end, drag finished
    cueActivated = Signal(int)  # double click on a subtitle line
    cueMenu = Signal(int, QPoint)  # right click on a subtitle line
    musicClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.project = Project()
        self.cues: list[Cue] = []
        self.spans: list[Cue] = []
        self.current = -1
        self.selection: set[int] = set()
        self.playhead = 0.0
        self.pps = 40.0
        self.fit_mode = True  # follow the window width until the user zooms
        self.view_width = 600
        self.hbar: QScrollBar | None = None
        # Pictures and sound outlines come from the page, which makes and caches them in the background.
        self.thumb: Callable[[Scene], QPixmap | None] = lambda scene: None
        self.strip: Callable[[str], list[QPixmap] | None] = lambda source: None
        self.clip_length: Callable[[str], float] = lambda source: 0.0
        self.wave: Callable[[str], np.ndarray | None] = lambda path: None
        self.sound_length: Callable[[str], float] = lambda path: 0.0
        self.transition_label: Callable[[int], str] = lambda index: ""
        self._drag: dict | None = None
        self._area = QRect(0, 0, self.view_width, HEIGHT)  # the part being painted
        self._insert_at: int | None = None  # where moved scenes would land
        self._drop_row: int | None = None  # scene files are being dropped on
        self.setFixedSize(self.view_width, HEIGHT)

    # ---- data and geometry --------------------------------------------------

    def set_data(self, project: Project, cues: list[Cue], current: int, playhead: float, selection: set[int] | None = None) -> None:
        if self._drag is not None and (project is not self.project or len(project.scenes) != self._drag["scenes"]
                                       or len(cues) != self._drag["cues"] or len(project.sounds) != self._drag["sounds"]):  # fmt: skip
            # The scenes changed under a drag (undo pressed mid-drag): its indices no longer hold, so drop it.
            self._drag = None
            self._insert_at = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.project = project
        self.cues = list(cues)
        self.spans = sentence_spans(cues)
        self.current = current
        self.selection = set(selection) if selection is not None else ({current} if current >= 0 else set())
        self.playhead = playhead
        if self.fit_mode and self._drag is None:
            self.fit()
        else:
            # Refitting mid-drag would change the scale under the pointer and run away with the drag.
            self._resize()
        self.update()

    def set_playhead(self, seconds: float) -> None:
        self.playhead = seconds
        self.update()

    def total(self) -> float:
        voice_end = self.cues[-1].end if self.cues else 0.0
        sounds_end = max((s.at + self.sound_length(s.source) for s in self.project.sounds), default=0.0)
        return max(self.project.duration, voice_end, sounds_end, 1.0)

    def starts(self) -> list[float]:
        starts, t = [], 0.0
        for scene in self.project.scenes:
            starts.append(t)
            t += scene.duration
        return starts

    def x_of(self, seconds: float) -> float:
        return PAD + seconds * self.pps

    def t_of(self, x: float) -> float:
        return max(0.0, (x - PAD) / self.pps)

    def scene_rect(self, index: int, top: float = SCENE_Y, height: float = SCENE_H) -> QRectF:
        start = self.starts()[index]
        return QRectF(self.x_of(start), top, self.project.scenes[index].duration * self.pps, height)

    def scene_at(self, x: float) -> int | None:
        t = self.t_of(x)
        for i, start in enumerate(self.starts()):
            if start <= t < start + self.project.scenes[i].duration:
                return i
        return None

    def cue_at(self, x: float) -> int | None:
        t = self.t_of(x)
        return next((i for i, c in enumerate(self.cues) if c.start <= t < c.end), None)

    def sound_rect(self, index: int) -> QRectF:
        sound = self.project.sounds[index]
        width = max(MIN_SOUND_W, self.sound_length(sound.source) * self.pps)
        return QRectF(self.x_of(sound.at), SOUND_Y + 2, width, SOUND_H - 4)

    def sound_at(self, x: float, y: float) -> int | None:
        """The sound effect under the pointer; the one drawn last (on top) first."""
        for i in range(len(self.project.sounds) - 1, -1, -1):
            if self.sound_rect(i).contains(QPointF(x, y)):
                return i
        return None

    def _snap_sound(self, seconds: float) -> float:
        """Pull a dragged effect onto a nearby scene start or sentence start."""
        points = [*self.starts(), *(c.start for c in self.cues)]
        near = [t for t in points if abs(self.x_of(t) - self.x_of(seconds)) <= SNAP]
        return min(near, key=lambda t: abs(t - seconds)) if near else seconds

    def _resize(self) -> None:
        width = max(self.view_width, math.ceil(2 * PAD + self.total() * self.pps))
        self.setFixedSize(width, HEIGHT)

    def fit(self) -> None:
        self.fit_mode = True
        self.pps = min(MAX_PPS, max(MIN_PPS, (self.view_width - 2 * PAD) / self.total()))
        self._resize()
        self.update()

    def zoom_by(self, factor: float, anchor: float | None = None, view_x: float | None = None) -> None:
        """Zoom, keeping the moment `anchor` (default: the playhead) where it is on screen."""
        anchor = self.playhead if anchor is None else anchor
        offset = self.hbar.value() if self.hbar else 0
        view_x = self.x_of(anchor) - offset if view_x is None else view_x
        self.fit_mode = False
        self.pps = min(MAX_PPS, max(MIN_PPS, self.pps * factor))
        self._resize()
        if self.hbar:
            self.hbar.setValue(round(self.x_of(anchor) - view_x))
        self.update()

    def ensure_visible(self, seconds: float) -> None:
        if not self.hbar:
            return
        x = self.x_of(seconds)
        left = self.hbar.value()
        if not left + 20 <= x <= left + self.view_width - 20:
            self.hbar.setValue(round(x - self.view_width * 0.2))

    def _is_clip(self, index: int) -> bool:
        scene = self.project.scenes[index]
        return scene.kind == "clip" and bool(scene.source) and self.clip_length(scene.source) > 0

    def _trim_bar(self, index: int) -> QRectF:
        return self.scene_rect(index, TRIM_Y, TRIM_H).adjusted(2, 0, -2, 0)

    def _marker_at(self, x: float, y: float) -> int | None:
        """The transition mark under the pointer: the scene it leads out of."""
        if not SCENE_Y + SCENE_H / 2 - 9 <= y <= SCENE_Y + SCENE_H / 2 + 9:
            return None
        starts = self.starts()
        for i in range(len(starts) - 1):
            if self.transition_label(i) and abs(self.x_of(starts[i + 1]) - x) <= 8:
                return i
        return None

    # ---- painting -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.85)
        painter.setFont(small)
        area = event.rect()
        self._area = area  # long clips and sounds draw only their part in it
        self._paint_ruler(painter, area)
        for _, top, height in ROWS:
            painter.fillRect(QRectF(0, top, self.width(), height), QColor("#f3f4f6"))
        if not self.project.scenes:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(
                QRectF(PAD, SCENE_Y, self.view_width - 2 * PAD, SCENE_H),
                Qt.AlignmentFlag.AlignCenter,
                "장면이 없습니다. 이미지나 영상 파일을 여기에 끌어다 놓으세요.",
            )
        starts = self.starts()
        for i, (scene, start) in enumerate(zip(self.project.scenes, starts)):
            rect = QRectF(self.x_of(start), SCENE_Y, scene.duration * self.pps, SCENE_H)
            if rect.right() < area.left() or rect.left() > area.right():
                continue
            self._paint_scene(painter, i, scene, rect)
            if self._is_clip(i):
                self._paint_trim(painter, scene, self._trim_bar(i))
        for i in range(len(starts) - 1):
            if self.transition_label(i):
                self._paint_marker(painter, self.x_of(starts[i + 1]))
        self._paint_voice(painter, area)
        self._paint_sounds(painter, area)
        self._paint_music(painter, area)
        if self._insert_at is not None:
            x = self.x_of([*starts, self.project.duration][min(self._insert_at, len(starts))])
            painter.fillRect(QRectF(x - 2, SCENE_Y - 4, 4, SCENE_H + 8), ACCENT)
        x = self.x_of(self.playhead)
        painter.setPen(QPen(PLAYHEAD, 2))
        painter.drawLine(QPointF(x, 4), QPointF(x, self.height()))
        painter.setBrush(PLAYHEAD)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x, 11)]))
        painter.end()

    def _paint_ruler(self, painter: QPainter, area) -> None:
        step = next((s for s in TICK_STEPS if s * self.pps >= 70), TICK_STEPS[-1])
        painter.setPen(QColor("#9ca3af"))
        minor = step / 5
        # Only the ticks in the part being repainted: zoomed in, the whole ruler is thousands of ticks.
        t = max(0.0, math.floor(self.t_of(area.left() - 80) / step) * step)
        last = min(self.total() + step, self.t_of(area.right()) + step)
        while t <= last:
            x = self.x_of(t)
            major = abs(t / step - round(t / step)) < 1e-6
            painter.drawLine(QPointF(x, RULER_H - (9 if major else 4)), QPointF(x, RULER_H))
            if major:
                label = format_time(t, precise=step < 1)
                painter.drawText(QRectF(x + 3, 0, 80, RULER_H - 6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            t = round(t + (minor if minor * self.pps >= 10 else step), 6)

    def _badge(self, painter: QPainter, rect: QRectF, text: str, corner: str) -> None:
        metrics = QFontMetricsF(painter.font())
        w, h = metrics.horizontalAdvance(text) + 8, metrics.height() + 2
        if w > rect.width() - 4:
            return
        x = rect.left() + 3 if corner.endswith("left") else rect.right() - w - 3
        y = rect.top() + 3 if corner.startswith("top") else rect.bottom() - h - 3
        box = QRectF(x, y, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(BADGE)
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_scene(self, painter: QPainter, index: int, scene: Scene, rect: QRectF) -> None:
        shape = QPainterPath()
        shape.addRoundedRect(rect.adjusted(1, 0, -1, 0), 6, 6)
        painter.save()
        painter.setClipPath(shape)
        if scene.kind == "clip" and scene.source:
            painter.fillRect(rect, QColor("#1f2937"))
            self._paint_clip(painter, scene, rect)
        elif (scene.kind == "image" and scene.source) or scene.kind == "graphic":
            painter.fillRect(rect, QColor("#1f2937"))
            thumb = self.thumb(scene)
            if thumb is not None:
                self._tile(painter, thumb, rect)
        elif scene.is_blank:
            painter.fillRect(rect, QColor("#e5e7eb"))
            painter.setPen(QColor("#6b7280"))
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignCenter, "빈 장면")
        else:
            if scene.color2:
                gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                gradient.setColorAt(0, QColor(scene.color))
                gradient.setColorAt(1, QColor(scene.color2))
                painter.fillRect(rect, gradient)
            else:
                painter.fillRect(rect, QColor(scene.color))
            if scene.text:
                painter.setPen(QColor("#ffffff"))
                painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignCenter, scene.text.splitlines()[0])
        if index in self.selection and len(self.selection) > 1:
            painter.fillRect(rect, SELECTED)
        self._badge(painter, rect, str(index + 1), "top-left")
        self._badge(painter, rect, f"{scene.duration:.1f}초", "bottom-right")
        extras = []
        if scene.kind != "color" and scene.text:
            extras.append("글자")
        if scene.overlays:
            extras.append(f"겹침{len(scene.overlays)}")
        if extras:
            self._badge(painter, rect, "·".join(extras), "bottom-left")
        painter.restore()
        if index == self._drop_row:
            painter.setPen(QPen(QColor("#16a34a"), 3, Qt.PenStyle.DashLine))
        elif index == self.current:
            painter.setPen(QPen(ACCENT, 3))
        elif index in self.selection:
            painter.setPen(QPen(ACCENT, 2, Qt.PenStyle.DashLine))
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 0, -1, 0), 6, 6)

    @staticmethod
    def _paint_marker(painter: QPainter, x: float) -> None:
        """A small diamond on the line between two scenes that blend into each other."""
        y = SCENE_Y + SCENE_H / 2
        painter.setPen(QPen(ACCENT, 1.5))
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(QPolygonF([QPointF(x, y - 7), QPointF(x + 7, y), QPointF(x, y + 7), QPointF(x - 7, y)]))

    @staticmethod
    def _tile(painter: QPainter, pixmap: QPixmap, rect: QRectF) -> None:
        width = pixmap.width() * rect.height() / max(1, pixmap.height())
        x = rect.left()
        while x < rect.right():
            painter.drawPixmap(QRectF(x, rect.top(), width, rect.height()), pixmap, QRectF(pixmap.rect()))
            x += width

    def _paint_clip(self, painter: QPainter, scene: Scene, rect: QRectF) -> None:
        """What actually plays: frames through the scene, then the frozen or repeated part marked."""
        tiles = self.strip(scene.source)
        length = self.clip_length(scene.source)
        if not tiles or not length:
            return
        width = tiles[0].width() * rect.height() / max(1, tiles[0].height())
        first, last = self._visible_tiles(rect.left(), width, math.ceil(rect.width() / width))
        for k in range(first, last):
            at = min(scene.duration, (k + 0.5) * width / self.pps)
            moment = clip_time(scene, length, at)
            tile = tiles[min(len(tiles) - 1, int(moment / length * len(tiles)))]
            painter.drawPixmap(QRectF(rect.left() + k * width, rect.top(), width, rect.height()), tile, QRectF(tile.rect()))
        start, end = clip_part(scene, length)
        speed = clip_speed(scene, length)
        plays = (end - start) / speed
        if scene.clip_fill == "stretch":
            self._badge(painter, rect, f"{speed:.2f}× 맞춤", "top-right")
            return
        if abs(speed - 1) > 1e-6:
            self._badge(painter, rect, f"{speed:g}×", "top-right")
        if plays < scene.duration - 0.05:
            x = rect.left() + plays * self.pps
            if scene.clip_fill == "freeze":
                held = QRectF(x, rect.top(), rect.right() - x, rect.height())
                painter.fillRect(held, QColor(0, 0, 0, 90))
                painter.fillRect(held, QBrush(QColor(255, 255, 255, 90), Qt.BrushStyle.BDiagPattern))
                if held.width() > 34:
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(held, Qt.AlignmentFlag.AlignCenter, "멈춤")
            else:
                painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5, Qt.PenStyle.DashLine))
                step = max(1.0, plays * self.pps)
                if x < self._area.left():  # straight to the first repeat in view
                    x += math.floor((self._area.left() - x) / step) * step
                while x < min(rect.right() - 1, self._area.right() + 1):
                    painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                    x += step
        elif plays > scene.duration + 0.05:
            self._badge(painter, rect, "뒤 잘림", "top-right" if abs(speed - 1) < 1e-6 else "bottom-left")

    def _paint_trim(self, painter: QPainter, scene: Scene, bar: QRectF) -> None:
        """The whole clip squeezed into the scene's width, with the used part between two handles."""
        length = self.clip_length(scene.source)
        tiles = self.strip(scene.source) or []
        shape = QPainterPath()
        shape.addRoundedRect(bar, 4, 4)
        painter.save()
        painter.setClipPath(shape)
        painter.fillRect(bar, QColor("#111827"))
        if tiles:
            aspect = tiles[0].width() / max(1, tiles[0].height())
            count = max(1, int(bar.width() / (bar.height() * aspect)))
            width = bar.width() / count
            for k in range(*self._visible_tiles(bar.left(), width, count)):
                tile = tiles[min(len(tiles) - 1, int((k + 0.5) / count * len(tiles)))]
                painter.drawPixmap(QRectF(bar.left() + k * width, bar.top(), width, bar.height()), tile, QRectF(tile.rect()))
        start, end = clip_part(scene, length)
        x0 = bar.left() + bar.width() * start / length
        x1 = bar.left() + bar.width() * end / length
        painter.fillRect(QRectF(bar.left(), bar.top(), x0 - bar.left(), bar.height()), OUTSIDE)
        painter.fillRect(QRectF(x1, bar.top(), bar.right() - x1, bar.height()), OUTSIDE)
        painter.restore()
        painter.setPen(QPen(ACCENT, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(x0, bar.top() + 1, x1 - x0, bar.height() - 2))
        for x in (x0, x1):
            handle = QRectF(x - HANDLE_W / 2, bar.top() - 2, HANDLE_W, bar.height() + 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ACCENT)
            painter.drawRoundedRect(handle, 3, 3)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            centre = handle.center().x()
            painter.drawLine(QPointF(centre, handle.top() + 8), QPointF(centre, handle.bottom() - 8))

    def _visible_tiles(self, left: float, width: float, count: int) -> tuple[int, int]:
        """The range of `count` tiles `width` wide from `left` that falls in the area being painted."""
        first = max(0, math.floor((self._area.left() - left) / width))
        last = min(count, math.ceil((self._area.right() + 1 - left) / width))
        return first, max(first, last)

    @staticmethod
    def _outline(values: np.ndarray, x0: int, top: float, height: float) -> QPainterPath:
        """A loudness outline (one value per column from x0 to x1) mirrored around the row's middle."""
        middle = top + height / 2
        half = height / 2 - 2
        path = QPainterPath()
        path.moveTo(x0, middle)
        for k, v in enumerate(values):
            path.lineTo(x0 + k, middle - v * half)
        for k in range(len(values) - 1, -1, -1):
            path.lineTo(x0 + k, middle + values[k] * half)
        path.closeSubpath()
        return path

    def _columns(self, peaks: np.ndarray, area, end: float, loop: bool = False) -> tuple[int, np.ndarray] | None:
        """The loudest value under each pixel column in view, from 0 up to `end` seconds."""
        if peaks is None or not len(peaks):
            return None
        x0 = max(int(area.left()), int(self.x_of(0)))
        x1 = min(int(area.right()) + 1, int(self.x_of(end)))
        if x1 <= x0:
            return None
        edges = (np.arange(x0, x1 + 1) - PAD) / self.pps * RATE
        index = edges.astype(np.int64)
        if loop:
            index %= len(peaks)
        index = np.clip(index, 0, len(peaks) - 1)
        lo, hi = index[:-1], np.maximum(index[1:], index[:-1] + 1)
        if not loop and lo[0] >= len(peaks):
            return None
        # The maximum over each column's span of peaks (spans are short; a cumulative trick is not needed).
        values = np.array([peaks[a:b].max() if b > a else peaks[a] for a, b in zip(lo, np.minimum(hi, len(peaks)))])
        return x0, np.sqrt(values)  # square root: quiet parts stay visible

    def _paint_voice(self, painter: QPainter, area) -> None:
        voice = self.project.voice
        peaks = self.wave(voice) if voice else None
        if peaks is not None and len(peaks):
            cols = self._columns(peaks, area, len(peaks) / RATE)
            if cols:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(139, 92, 246, 110))
                painter.drawPath(self._outline(cols[1], cols[0], VOICE_Y, VOICE_H))
        metrics = QFontMetricsF(painter.font())
        edited = self.project.cues is not None
        for i, cue in enumerate(self.cues):
            rect = QRectF(self.x_of(cue.start), VOICE_Y + 3, max(2.0, (cue.end - cue.start) * self.pps), VOICE_H - 6)
            if rect.right() < area.left() or rect.left() > area.right():
                continue
            painter.setPen(QPen(QColor("#7c3aed") if edited else QColor("#a78bfa"), 1))
            painter.setBrush(QColor(245, 243, 255, 120))  # the voice's outline shows through
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            text = metrics.elidedText(" ".join(cue.text.split()), Qt.TextElideMode.ElideRight, rect.width() - 8)
            painter.setPen(QColor("#5b21b6"))
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_sounds(self, painter: QPainter, area) -> None:
        if not self.project.sounds:
            painter.setPen(QColor("#9ca3af"))
            painter.drawText(QRectF(PAD + 4, SOUND_Y, 500, SOUND_H), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "효과음 없음 · 위의 [+ 효과음]이나 여기를 오른쪽 클릭해서 넣기")  # fmt: skip
            return
        metrics = QFontMetricsF(painter.font())
        for i, sound in enumerate(self.project.sounds):
            rect = self.sound_rect(i)
            if rect.right() < area.left() or rect.left() > area.right():
                continue
            painter.setPen(QPen(QColor("#d97706"), 1))
            painter.setBrush(QColor(253, 230, 138, 220))
            painter.drawRoundedRect(rect, 4, 4)
            peaks = self.wave(sound.source)
            x0, x1 = max(int(rect.left()), int(area.left())), min(int(rect.right()), int(area.right()) + 1)
            if peaks is not None and len(peaks) and x1 > x0:
                columns = np.arange(x0, x1) - rect.left()
                index = np.minimum((columns / self.pps * RATE).astype(np.int64), len(peaks) - 1)
                values = np.sqrt(peaks[index]) * min(1.0, sound.volume)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(217, 119, 6, 150))
                painter.drawPath(self._outline(values, x0, rect.top(), rect.height()))
            name = Path(sound.source).stem
            text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, max(0.0, rect.width() - 6))
            if text and rect.width() > 24:
                painter.setPen(QColor("#78350f"))
                painter.drawText(rect.adjusted(3, 0, -3, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_music(self, painter: QPainter, area) -> None:
        project = self.project
        end = project.duration
        row = QRectF(self.x_of(0), MUSIC_Y, end * self.pps, MUSIC_H)
        if not project.music:
            painter.setPen(QColor("#9ca3af"))
            painter.drawText(QRectF(PAD + 4, MUSIC_Y, 400, MUSIC_H), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "배경 음악 없음 · 눌러서 고르기")  # fmt: skip
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(16, 185, 129, 40))
        painter.drawRoundedRect(row, 4, 4)
        cols = self._columns(self.wave(project.music), area, end, loop=True)
        if cols:
            x0, values = cols
            seconds = (np.arange(x0, x0 + len(values)) - PAD) / self.pps
            gain = np.full(len(values), min(1.0, project.music_volume * 2.5))
            fade = min(project.music_fade, end / 3)
            if fade > 0.05:
                gain *= np.clip(seconds / fade, 0, 1) * np.clip((end - seconds) / fade, 0, 1)
            if project.music_duck:
                for cue in self.cues:
                    gain[(seconds >= cue.start) & (seconds < cue.end)] *= DUCKED
            painter.setBrush(QColor("#10b981"))
            painter.drawPath(self._outline(values * gain, x0, MUSIC_Y, MUSIC_H))
        painter.setPen(QColor("#065f46"))
        painter.drawText(row.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         Path(project.music).name)  # fmt: skip

    # ---- mouse --------------------------------------------------------------

    def _edge_at(self, x: float) -> int | None:
        """The scene whose right edge is under the pointer, for changing its length."""
        for i, start in enumerate(self.starts()):
            duration = self.project.scenes[i].duration
            # A narrow scene keeps most of its width for clicking; only its last quarter grabs the edge.
            reach = min(EDGE, duration * self.pps / 4)
            if abs(self.x_of(start + duration) - x) <= reach:
                return i
        return None

    def _cue_edge_at(self, x: float) -> tuple[int, str] | None:
        """A subtitle line's start or end under the pointer."""
        best = None
        for i, cue in enumerate(self.cues):
            reach = min(CUE_EDGE, (cue.end - cue.start) * self.pps / 4)
            for edge, t in (("start", cue.start), ("end", cue.end)):
                distance = abs(self.x_of(t) - x)
                if distance <= reach and (best is None or distance < best[0]):
                    best = (distance, i, edge)
        return (best[1], best[2]) if best else None

    def _snap(self, seconds: float) -> float:
        """Pull a dragged scene end onto a nearby sentence boundary, so scenes line up with the voice."""
        near = [t for span in self.spans for t in (span.start, span.end) if abs(self.x_of(t) - self.x_of(seconds)) <= SNAP]
        return min(near, key=lambda t: abs(t - seconds)) if near else seconds

    def _start_drag(self, mode: str, x: float, **details) -> None:
        """Begin a drag, noting how many scenes, cues and sounds there are: a change under it cancels it."""
        self._drag = {"mode": mode, "x": x, "last_x": x, "moving": False, "scenes": len(self.project.scenes),
                      "cues": len(self.cues), "sounds": len(self.project.sounds), **details}  # fmt: skip

    def _trim_handle_at(self, index: int, x: float) -> str | None:
        """The trim handle ("start" or "end") under the pointer, if any: a click elsewhere on the bar changes nothing."""
        bar = self._trim_bar(index)
        scene = self.project.scenes[index]
        length = self.clip_length(scene.source)
        start, end = clip_part(scene, length)
        x0, x1 = (bar.left() + bar.width() * t / length for t in (start, end))
        handle = "start" if abs(x - x0) <= abs(x - x1) else "end"
        return handle if min(abs(x - x0), abs(x - x1)) <= HANDLE_W else None

    def mousePressEvent(self, event):
        x, y = event.position().x(), event.position().y()
        index = self.scene_at(x)
        if event.button() == Qt.MouseButton.RightButton:
            return  # contextMenuEvent
        if event.button() != Qt.MouseButton.LeftButton:
            return
        modifiers = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if SCENE_Y <= y < SCENE_Y + SCENE_H:
            edge = self._edge_at(x)
            if edge is not None and not ctrl:  # Shift held from the start drags without snapping
                self._start_drag("length", x, index=edge)
                return
            if index is not None:
                if modifiers:
                    self.sceneClicked.emit(index, self.t_of(x), modifiers.value)
                    return
                # Pressing inside a group of selected scenes may start moving them all; it only narrows the
                # selection to this one scene when the button comes up without a move.
                group = index in self.selection and len(self.selection) > 1
                self._start_drag("move", x, index=index, group=group, t=self.t_of(x))
                if not group:
                    self.sceneClicked.emit(index, self.t_of(x), 0)
                return
        elif (
            TRIM_Y - 3 <= y < TRIM_Y + TRIM_H + 3
            and index is not None
            and self._is_clip(index)
            and self._trim_bar(index).width() >= MIN_TRIM_WIDTH
            and (handle := self._trim_handle_at(index, x)) is not None
        ):
            self._start_drag("trim", x, index=index, handle=handle)
            return
        elif VOICE_Y <= y < VOICE_Y + VOICE_H and (edge := self._cue_edge_at(x)) is not None:
            self._start_drag("cue", x, index=edge[0], edge=edge[1])
            return
        elif SOUND_Y <= y < SOUND_Y + SOUND_H and (sound := self.sound_at(x, y)) is not None:
            self._start_drag("sound", x, index=sound, grab=self.t_of(x) - self.project.sounds[sound].at)
            return
        elif MUSIC_Y <= y < MUSIC_Y + MUSIC_H and not self.project.music:
            self.musicClicked.emit()
            return
        self._start_drag("seek", x)
        self.seeked.emit(min(self.t_of(x), self.project.duration))

    def mouseDoubleClickEvent(self, event):
        x, y = event.position().x(), event.position().y()
        if VOICE_Y <= y < VOICE_Y + VOICE_H and (i := self.cue_at(x)) is not None:
            self._drag = None
            self.cueActivated.emit(i)
        elif SOUND_Y <= y < SOUND_Y + SOUND_H and (i := self.sound_at(x, y)) is not None:
            self._drag = None
            self.soundActivated.emit(i)
        elif MUSIC_Y <= y < MUSIC_Y + MUSIC_H:
            self.musicClicked.emit()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        x, y = event.pos().x(), event.pos().y()
        if VOICE_Y <= y < VOICE_Y + VOICE_H and (i := self.cue_at(x)) is not None:
            self.cueMenu.emit(i, event.globalPos())
        elif SOUND_Y <= y < SOUND_Y + SOUND_H:
            i = self.sound_at(x, y)
            self.soundMenu.emit(-1 if i is None else i, min(self.t_of(x), self.total()), event.globalPos())
        elif SCENE_Y <= y < TRIM_Y + TRIM_H:
            index = self.scene_at(x)
            if index is not None and index not in self.selection:
                self.sceneClicked.emit(index, self.t_of(x), 0)
            self.sceneMenu.emit(-1 if index is None else index, event.globalPos())

    def mouseMoveEvent(self, event):
        x, y = event.position().x(), event.position().y()
        drag = self._drag
        if drag is not None and not event.buttons() & Qt.MouseButton.LeftButton:
            self._finish_drag()  # the button came up where this widget did not hear it (a dialog, another tab)
            drag = None
        if drag is None:
            near_edge = SCENE_Y <= y < SCENE_Y + SCENE_H and self._edge_at(x) is not None
            near_trim = TRIM_Y - 3 <= y < TRIM_Y + TRIM_H + 3 and (i := self.scene_at(x)) is not None and self._is_clip(i)
            near_cue = VOICE_Y <= y < VOICE_Y + VOICE_H and self._cue_edge_at(x) is not None
            on_sound = SOUND_Y <= y < SOUND_Y + SOUND_H and self.sound_at(x, y) is not None
            if on_sound:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor if near_edge or near_trim or near_cue else Qt.CursorShape.ArrowCursor)
            return
        drag["last_x"] = x
        if drag["mode"] in ("trim", "cue") and not drag["moving"]:
            if abs(x - drag["x"]) < 2:
                return  # a press without a move changes nothing
            drag["moving"] = True
        if drag["mode"] == "length":
            i = drag["index"]
            start = self.starts()[i]
            end = start + max(MIN_DURATION, self.t_of(x) - start)
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:  # Shift: no snapping
                end = self._snap(end)
            self.lengthDragged.emit(i, max(MIN_DURATION, end - start), False)
        elif drag["mode"] == "move":
            if not drag["moving"] and abs(x - drag["x"]) > DRAG_START:
                drag["moving"] = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if drag["moving"]:
                starts = [*self.starts(), self.project.duration]
                self._insert_at = min(range(len(starts)), key=lambda k: abs(self.x_of(starts[k]) - x))
                self.update()
        elif drag["mode"] == "trim":
            self._trim_to(x)
        elif drag["mode"] == "cue":
            self._cue_to(x, False)
        elif drag["mode"] == "sound":
            if not drag["moving"] and abs(x - drag["x"]) > 3:
                drag["moving"] = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if drag["moving"]:
                at = max(0.0, self.t_of(x) - drag["grab"])
                if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:  # Shift: no snapping
                    at = self._snap_sound(at)
                drag["at"] = at
                self.soundDragged.emit(drag["index"], round(at, 3), False)
        elif drag["mode"] == "seek":
            self.seeked.emit(min(self.t_of(x), self.project.duration))

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag["last_x"] = event.position().x()
        self._finish_drag()

    def hideEvent(self, event):
        self._finish_drag()  # another tab took over mid-drag
        super().hideEvent(event)

    def focusOutEvent(self, event):
        self._finish_drag()  # a dialog came up mid-drag
        super().focusOutEvent(event)

    def _finish_drag(self) -> None:
        """End the drag where the pointer last was, as a release there would."""
        drag, self._drag = self._drag, None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if drag is None:
            if self._insert_at is not None:
                self._insert_at = None
                self.update()
            return
        if drag["mode"] == "length":
            i = drag["index"]
            if 0 <= i < len(self.project.scenes):
                self.lengthDragged.emit(i, self.project.scenes[i].duration, True)
        elif drag["mode"] == "trim":
            i = drag["index"]
            if drag["moving"] and 0 <= i < len(self.project.scenes):
                scene = self.project.scenes[i]
                start, end = clip_part(scene, self.clip_length(scene.source))
                self.trimDragged.emit(i, start, end, True)
        elif drag["mode"] == "cue":
            if drag["moving"]:
                self._cue_to(drag["last_x"], True, drag)
        elif drag["mode"] == "sound":
            if drag["moving"]:
                self.soundDragged.emit(drag["index"], round(drag.get("at", 0.0), 3), True)
            else:  # a plain click: show that moment
                self.seeked.emit(min(self.t_of(drag["x"]), self.project.duration))
        elif drag["mode"] == "move":
            at = self._insert_at
            self._insert_at = None
            self.update()
            if not drag["moving"]:
                if drag["group"]:
                    self.sceneClicked.emit(drag["index"], drag["t"], 0)
                return
            moved = sorted(self.selection) if drag["group"] else [drag["index"]]
            if at is not None and not (len(moved) == 1 and at in (moved[0], moved[0] + 1)):
                self.scenesMoved.emit(moved, at)

    def _trim_to(self, x: float) -> None:
        i, handle = self._drag["index"], self._drag["handle"]
        scene = self.project.scenes[i]
        length = self.clip_length(scene.source)
        bar = self._trim_bar(i)
        if bar.width() <= 0 or not length:
            return
        moment = min(1.0, max(0.0, (x - bar.left()) / bar.width())) * length
        start, end = clip_part(scene, length)
        if handle == "start":
            start = max(0.0, min(moment, end - MIN_CLIP_PART))
        else:
            end = min(length, max(moment, start + MIN_CLIP_PART))
        self.trimDragged.emit(i, start, end, False)
        self.trimScrubbed.emit(i, start if handle == "start" else end)

    def _cue_to(self, x: float, finished: bool, drag: dict | None = None) -> None:
        drag = drag or self._drag
        i, edge = drag["index"], drag["edge"]
        if not 0 <= i < len(self.cues):
            return
        cue = self.cues[i]
        t = self.t_of(x)
        start, end = cue.start, cue.end
        if edge == "start":
            low = self.cues[i - 1].end if i > 0 else 0.0
            start = min(max(t, low), end - MIN_CUE)
        else:
            high = self.cues[i + 1].start if i + 1 < len(self.cues) else max(self.total(), end) + 30
            end = max(min(t, high), start + MIN_CUE)
        self.cueDragged.emit(i, round(start, 3), round(end, 3), finished)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() or event.angleDelta().x()
        if not steps:  # a touchpad's pixel-only scroll
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            x = event.position().x()
            offset = self.hbar.value() if self.hbar else 0
            self.zoom_by(1.25 if steps > 0 else 0.8, self.t_of(x), x - offset)
        elif self.hbar:
            self.hbar.setValue(self.hbar.value() - steps)
        event.accept()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            pos = event.pos()  # a QHelpEvent: it has pos(), not position() like mouse events
            text = self._tip(pos.x(), pos.y())
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def _tip(self, x: float, y: float) -> str:
        if VOICE_Y <= y < VOICE_Y + VOICE_H:
            i = self.cue_at(x)
            if i is None:
                return ""
            cue = self.cues[i]
            return (f"{format_time(cue.start, precise=True)} ~ {format_time(cue.end, precise=True)}\n{cue.text}\n"
                    "두 번 누르면 고칩니다 · 양 끝을 끌면 시간이 바뀝니다")  # fmt: skip
        if MUSIC_Y <= y < MUSIC_Y + MUSIC_H:
            return "배경 음악 · 두 번 누르면 음악 설정을 엽니다"
        if SOUND_Y <= y < SOUND_Y + SOUND_H:
            i = self.sound_at(x, y)
            if i is None:
                return "오른쪽 클릭: 여기에 효과음 넣기 · 소리 파일을 끌어다 놓아도 됩니다"
            sound = self.project.sounds[i]
            return (f"{Path(sound.source).name} · {format_time(sound.at, precise=True)}부터 · 크기 {round(sound.volume * 100)}%\n"
                    "끌어서 옮기기 (장면·문장 시작에 붙음, Shift: 자유롭게) · 두 번 누르면 들어보기 · 오른쪽 클릭: 크기·지우기")  # fmt: skip
        marker = self._marker_at(x, y)
        if marker is not None:
            return f"{marker + 1} → {marker + 2}번 장면 전환: {self.transition_label(marker)}"
        index = self.scene_at(x)
        if index is None or not SCENE_Y <= y < TRIM_Y + TRIM_H:
            return ""
        scene = self.project.scenes[index]
        if y >= TRIM_Y:
            if not self._is_clip(index):
                return ""
            length = self.clip_length(scene.source)
            start, end = clip_part(scene, length)
            return f"클립 {format_time(start, precise=True)} ~ {format_time(end, precise=True)} 사용 (전체 {format_time(length, precise=True)})\n파란 손잡이를 끌어 바꿉니다."
        what = Path(scene.source).name if scene.source else (scene.text.splitlines()[0] if scene.text else "빈 장면")
        lines = [f"{index + 1}번 장면 · {scene.duration:.1f}초 · {what}"]
        if scene.narration:
            lines.append(scene.narration)
        lines.append("오른쪽 끝을 끌면 길이가, 가운데를 끌면 순서가 바뀝니다. Ctrl·Shift를 누르고 누르면 여러 개를 고릅니다.")
        return "\n".join(lines)

    # ---- dropped files ------------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            y = event.position().y()
            row = self.scene_at(event.position().x()) if SCENE_Y <= y < TRIM_Y + TRIM_H else None
            if row != self._drop_row:
                self._drop_row = row
                self.update()

    def dragLeaveEvent(self, event):
        self._drop_row = None
        self.update()

    def dropEvent(self, event):
        files = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        row = -1 if self._drop_row is None else self._drop_row
        self._drop_row = None
        self.update()
        event.acceptProposedAction()
        self.filesDropped.emit(files, row, min(self.t_of(event.position().x()), self.total()))
