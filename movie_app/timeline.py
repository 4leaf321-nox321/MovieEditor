"""The timeline under the video tab's preview.

Scenes sit side by side, each as wide as it lasts. Under every clip is its trim bar (the whole clip,
with the part used between two handles), under everything the voice's sentences, and a red line marks
the moment shown in the preview.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QScrollBar, QToolTip, QWidget

from .media import format_time
from .scenes import MIN_CLIP_PART, MIN_DURATION, Project, Scene, clip_part, clip_speed, clip_time, sentence_spans
from .subs import Cue

RULER_H = 22
SCENE_Y, SCENE_H = 26, 64
TRIM_Y, TRIM_H = SCENE_Y + SCENE_H + 6, 28
VOICE_Y, VOICE_H = TRIM_Y + TRIM_H + 6, 24
HEIGHT = VOICE_Y + VOICE_H + 6
ROWS = (("장면", SCENE_Y, SCENE_H), ("클립 구간", TRIM_Y, TRIM_H), ("음성", VOICE_Y, VOICE_H))
PAD = 10  # pixels before 0 s and after the end
EDGE = 6  # how close to a scene's right edge the pointer grabs its length
SNAP = 7  # pixels within which a dragged scene end snaps to a sentence boundary
HANDLE_W = 8
MIN_TRIM_WIDTH = 2 * HANDLE_W  # narrower trim bars (a short clip, zoomed far out) are not draggable; zoom in
DRAG_START = 8  # pixels a scene must travel before it is being moved
MIN_PPS, MAX_PPS = 4.0, 600.0  # pixels per second
TICK_STEPS = (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)
ACCENT = QColor("#2563eb")
PLAYHEAD = QColor("#ef4444")
BADGE = QColor(17, 24, 39, 170)
OUTSIDE = QColor(17, 24, 39, 175)  # darkens the parts of a clip left out


class TimelineHeader(QWidget):
    """Row names, kept still to the left of the scrolling timeline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, HEIGHT)

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
    sceneClicked = Signal(int, float)  # scene, the moment clicked (seconds into the video)
    seeked = Signal(float)  # the ruler or an empty spot was clicked or dragged
    lengthDragged = Signal(int, float, bool)  # scene, new length, drag finished
    trimDragged = Signal(int, float, float, bool)  # scene, clip start, clip end, drag finished
    trimScrubbed = Signal(int, float)  # scene, the clip time under the handle being dragged
    sceneMoved = Signal(int, int)  # from, to
    filesDropped = Signal(list, int)  # paths, the scene they were dropped on (-1: after the last)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.project = Project()
        self.spans: list[Cue] = []
        self.current = -1
        self.playhead = 0.0
        self.pps = 40.0
        self.fit_mode = True  # follow the window width until the user zooms
        self.view_width = 600
        self.hbar: QScrollBar | None = None
        # Pictures come from the page, which makes and caches them in the background.
        self.thumb: Callable[[Scene], QPixmap | None] = lambda scene: None
        self.strip: Callable[[str], list[QPixmap] | None] = lambda source: None
        self.clip_length: Callable[[str], float] = lambda source: 0.0
        self._drag: dict | None = None
        self._insert_at: int | None = None  # where a moved scene would land
        self._drop_row: int | None = None  # scene files are being dropped on
        self.setFixedSize(self.view_width, HEIGHT)

    # ---- data and geometry --------------------------------------------------

    def set_data(self, project: Project, cues: list[Cue], current: int, playhead: float) -> None:
        self.project = project
        self.spans = sentence_spans(cues)
        self.current = current
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
        voice_end = self.spans[-1].end if self.spans else 0.0
        return max(self.project.duration, voice_end, 1.0)

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

    # ---- painting -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.85)
        painter.setFont(small)
        self._paint_ruler(painter, event.rect())
        for top, height in ((SCENE_Y, SCENE_H), (TRIM_Y, TRIM_H), (VOICE_Y, VOICE_H)):
            painter.fillRect(QRectF(0, top, self.width(), height), QColor("#f3f4f6"))
        if not self.project.scenes:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(
                QRectF(PAD, SCENE_Y, self.view_width - 2 * PAD, SCENE_H),
                Qt.AlignmentFlag.AlignCenter,
                "장면이 없습니다. 이미지나 영상 파일을 여기에 끌어다 놓으세요.",
            )
        for i, (scene, start) in enumerate(zip(self.project.scenes, self.starts())):
            rect = QRectF(self.x_of(start), SCENE_Y, scene.duration * self.pps, SCENE_H)
            if rect.right() < event.rect().left() or rect.left() > event.rect().right():
                continue
            self._paint_scene(painter, i, scene, rect)
            if self._is_clip(i):
                self._paint_trim(painter, scene, self._trim_bar(i))
        self._paint_voice(painter)
        if self._insert_at is not None:
            starts = [*self.starts(), self.project.duration]
            x = self.x_of(starts[self._insert_at])
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
        elif scene.kind == "image" and scene.source:
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
            if scene.text.strip():
                painter.setPen(QColor("#ffffff"))
                painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignCenter, scene.text.strip().splitlines()[0])
        self._badge(painter, rect, str(index + 1), "top-left")
        self._badge(painter, rect, f"{scene.duration:.1f}초", "bottom-right")
        painter.restore()
        if index == self._drop_row:
            painter.setPen(QPen(QColor("#16a34a"), 3, Qt.PenStyle.DashLine))
        elif index == self.current:
            painter.setPen(QPen(ACCENT, 3))
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 0, -1, 0), 6, 6)

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
        for k in range(math.ceil(rect.width() / width)):
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
                while x < rect.right() - 1:
                    painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                    x += plays * self.pps
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
            for k in range(count):
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

    def _paint_voice(self, painter: QPainter) -> None:
        metrics = QFontMetricsF(painter.font())
        for span in self.spans:
            rect = QRectF(self.x_of(span.start), VOICE_Y, max(2.0, (span.end - span.start) * self.pps), VOICE_H)
            painter.setPen(QPen(QColor("#c4b5fd"), 1))
            painter.setBrush(QColor("#ede9fe"))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            text = metrics.elidedText(span.text, Qt.TextElideMode.ElideRight, rect.width() - 8)
            painter.setPen(QColor("#5b21b6"))
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

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

    def _snap(self, seconds: float) -> float:
        """Pull a dragged scene end onto a nearby sentence boundary, so scenes line up with the voice."""
        for t in (b for span in self.spans for b in (span.start, span.end)):
            if abs(self.x_of(t) - self.x_of(seconds)) <= SNAP:
                return t
        return seconds

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.position().x(), event.position().y()
        index = self.scene_at(x)
        if SCENE_Y <= y < SCENE_Y + SCENE_H:
            edge = self._edge_at(x)
            if edge is not None:
                self._drag = {"mode": "length", "index": edge}
                return
            if index is not None:
                self._drag = {"mode": "move", "index": index, "x": x, "moving": False}
                self.sceneClicked.emit(index, self.t_of(x))
                return
        elif (
            TRIM_Y - 3 <= y < TRIM_Y + TRIM_H + 3
            and index is not None
            and self._is_clip(index)
            and self._trim_bar(index).width() >= MIN_TRIM_WIDTH
        ):
            bar = self._trim_bar(index)
            scene = self.project.scenes[index]
            length = self.clip_length(scene.source)
            start, end = clip_part(scene, length)
            x0, x1 = (bar.left() + bar.width() * t / length for t in (start, end))
            handle = "start" if abs(x - x0) <= abs(x - x1) else "end"
            self._drag = {"mode": "trim", "index": index, "handle": handle}
            self._trim_to(x)
            return
        self._drag = {"mode": "seek"}
        self.seeked.emit(min(self.t_of(x), self.project.duration))

    def mouseMoveEvent(self, event):
        x, y = event.position().x(), event.position().y()
        drag = self._drag
        if drag is None:
            near_edge = SCENE_Y <= y < SCENE_Y + SCENE_H and self._edge_at(x) is not None
            near_trim = TRIM_Y - 3 <= y < TRIM_Y + TRIM_H + 3 and (i := self.scene_at(x)) is not None and self._is_clip(i)
            self.setCursor(Qt.CursorShape.SizeHorCursor if near_edge or near_trim else Qt.CursorShape.ArrowCursor)
            return
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
        elif drag["mode"] == "seek":
            self.seeked.emit(min(self.t_of(x), self.project.duration))

    def mouseReleaseEvent(self, event):
        drag, self._drag = self._drag, None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if drag is None:
            return
        if drag["mode"] == "length":
            i = drag["index"]
            self.lengthDragged.emit(i, self.project.scenes[i].duration, True)
        elif drag["mode"] == "trim":
            i = drag["index"]
            scene = self.project.scenes[i]
            start, end = clip_part(scene, self.clip_length(scene.source))
            self.trimDragged.emit(i, start, end, True)
        elif drag["mode"] == "move" and drag["moving"]:
            i, at = drag["index"], self._insert_at
            self._insert_at = None
            self.update()
            if at is not None and at not in (i, i + 1):
                self.sceneMoved.emit(i, at if at < i else at - 1)

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

    def wheelEvent(self, event):
        steps = event.angleDelta().y() or event.angleDelta().x()
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
            t = self.t_of(x)
            span = next((s for s in self.spans if s.start <= t < s.end), None)
            return f"{format_time(span.start, precise=True)} ~ {format_time(span.end, precise=True)}\n{span.text}" if span else ""
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
        what = Path(scene.source).name if scene.source else (scene.text.strip() or "빈 장면")
        lines = [f"{index + 1}번 장면 · {scene.duration:.1f}초 · {what}"]
        if scene.narration:
            lines.append(scene.narration)
        lines.append("오른쪽 끝을 끌면 길이가, 가운데를 끌면 순서가 바뀝니다.")
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
        self.filesDropped.emit(files, row)
