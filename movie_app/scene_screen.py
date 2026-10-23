"""The video tab's preview screen: the scene's words, laid-over pictures and hidden parts can be dragged
into place, pictures resized with the mouse wheel, and hidden parts by their corners too."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen

from .widgets import Screen

DRAG_START = 3  # pixels on screen before a press becomes a move
CORNER = 9  # pixels on screen around a hidden part's corner that grab it for resizing
MIN_SIDE = 8  # smallest side of a hidden part, in screen pixels, while it is resized
ACCENT = QColor("#2563eb")
MASK_COLOUR = QColor("#f97316")


class SceneScreen(Screen):
    picked = Signal(str, int)  # "text", "overlay" or "mask", its index in the scene
    moved = Signal(str, int, float, float)  # kind, index, its new centre as shares of the frame
    resized = Signal(str, int, float)  # kind ("overlay" or "mask"), index, how much bigger (below 1: smaller)
    reshaped = Signal(str, int, float, float, float, float)  # kind, index, centre x, y and width, height (shares)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setToolTip("글자와 겹친 그림은 끌어서 옮기고, 그림은 마우스 휠로 크기를 바꿉니다 · 두 번 클릭: 원본 크기")
        self.guides: list[tuple[str, int, QRectF]] = []  # (kind, index, rect in frame pixels), bottom first
        self.frame_size = (1, 1)
        self.selected: tuple[str, int] | None = None
        self._drag: dict | None = None

    def set_guides(self, guides: list[tuple[str, int, QRectF]], frame_size: tuple[int, int],
                   selected: tuple[str, int] | None) -> None:  # fmt: skip
        if self._drag is not None:
            return  # the picture under a drag is redrawn once it is dropped
        self.guides = guides
        self.frame_size = frame_size
        self.selected = selected
        self.update()

    def clear_guides(self) -> None:
        self._drag = None
        self.guides = []
        self.update()

    # ---- geometry -----------------------------------------------------------

    def _shown(self) -> QRectF | None:
        """Where the frame is drawn in the widget."""
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        size = pixmap.deviceIndependentSize()
        area = QRectF(self.contentsRect())
        return QRectF(area.left() + (area.width() - size.width()) / 2, area.top() + (area.height() - size.height()) / 2,
                      size.width(), size.height())  # fmt: skip

    def _scale(self) -> float:
        shown = self._shown()
        return shown.width() / max(1, self.frame_size[0]) if shown else 1.0

    def _to_widget(self, rect: QRectF) -> QRectF:
        shown, scale = self._shown(), self._scale()
        return QRectF(shown.left() + rect.left() * scale, shown.top() + rect.top() * scale, rect.width() * scale, rect.height() * scale)

    def _guide_at(self, pos: QPointF) -> tuple[str, int, QRectF] | None:
        if self._shown() is None:
            return None
        for kind, index, rect in reversed(self.guides):  # the top one first
            if self._to_widget(rect).adjusted(-4, -4, 4, 4).contains(pos):
                return kind, index, rect
        return None

    def _corner_at(self, pos: QPointF) -> tuple[tuple[str, int, QRectF], QPointF] | None:
        """A hidden part whose corner is under `pos`, and its opposite corner (which stays put)."""
        if self._shown() is None:
            return None
        for guide in reversed(self.guides):
            if guide[0] != "mask":
                continue
            box = self._to_widget(guide[2])
            for corner, opposite in ((box.topLeft(), box.bottomRight()), (box.topRight(), box.bottomLeft()),
                                     (box.bottomLeft(), box.topRight()), (box.bottomRight(), box.topLeft())):  # fmt: skip
                if abs(pos.x() - corner.x()) <= CORNER and abs(pos.y() - corner.y()) <= CORNER:
                    return guide, opposite
        return None

    # ---- painting -----------------------------------------------------------

    def _drawn_box(self, kind: str, index: int, rect: QRectF) -> tuple[QRectF, bool]:
        """Where a guide is drawn now (moved or reshaped while dragged), and whether it is being dragged."""
        box = self._to_widget(rect)
        drag = self._drag
        if drag is None or not drag["moving"] or (drag["kind"], drag["index"]) != (kind, index):
            return box, False
        if drag["mode"] == "resize":
            return drag["box"], True
        return box.translated(drag["offset"]), True

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.guides or self._shown() is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for kind, index, rect in self.guides:
            box, dragged = self._drawn_box(kind, index, rect)
            chosen = (kind, index) == self.selected or dragged
            mask = kind == "mask"
            painter.setPen(QPen(QColor(0, 0, 0, 120), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
            colour = MASK_COLOUR if mask else ACCENT
            painter.setPen(QPen(colour if chosen or mask else QColor(255, 255, 255, 200), 2 if chosen else 1, Qt.PenStyle.DashLine))
            painter.drawRect(box)
            if mask and chosen:
                painter.setPen(QPen(QColor(0, 0, 0, 160), 1))
                painter.setBrush(MASK_COLOUR)
                for corner in (box.topLeft(), box.topRight(), box.bottomLeft(), box.bottomRight()):
                    painter.drawRect(QRectF(corner.x() - 4, corner.y() - 4, 8, 8))
            if dragged:
                painter.fillRect(box, QColor(249, 115, 22, 50) if mask else QColor(37, 99, 235, 50))
        painter.end()

    # ---- mouse --------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        corner = self._corner_at(event.position())
        hit = corner[0] if corner else self._guide_at(event.position())
        if hit is None:
            return super().mousePressEvent(event)
        kind, index, rect = hit
        self.selected = (kind, index)
        self.picked.emit(kind, index)
        self._drag = {"kind": kind, "index": index, "rect": rect, "press": event.position(), "offset": QPointF(),
                      "moving": False, "mode": "resize" if corner else "move",
                      "anchor": corner[1] if corner else None, "box": self._to_widget(rect)}  # fmt: skip
        self.update()

    def mouseMoveEvent(self, event):
        drag = self._drag
        if drag is None:
            pos = event.position()
            corner = self._corner_at(pos)
            if corner is not None:
                anchor = corner[1]
                # Top-left/bottom-right corners resize along one diagonal, the other two along the other.
                same = (pos.x() < anchor.x()) == (pos.y() < anchor.y())
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if same else Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor if self._guide_at(pos) else Qt.CursorShape.ArrowCursor)
            return super().mouseMoveEvent(event)
        offset = event.position() - drag["press"]
        if not drag["moving"] and abs(offset.x()) + abs(offset.y()) >= DRAG_START:
            drag["moving"] = True
        if drag["moving"]:
            drag["offset"] = offset
            if drag["mode"] == "resize":
                shown = self._shown()
                pos = event.position()
                pos = QPointF(min(max(pos.x(), shown.left()), shown.right()), min(max(pos.y(), shown.top()), shown.bottom()))
                box = QRectF(drag["anchor"], pos).normalized()
                if box.width() >= MIN_SIDE and box.height() >= MIN_SIDE:
                    drag["box"] = box
            self.update()

    def mouseReleaseEvent(self, event):
        drag, self._drag = self._drag, None
        if drag is None:
            return super().mouseReleaseEvent(event)
        if drag["moving"]:
            scale = self._scale()
            width, height = self.frame_size
            if drag["mode"] == "resize":
                shown, box = self._shown(), drag["box"]
                centre = (box.center() - shown.topLeft()) / scale
                self.reshaped.emit(drag["kind"], drag["index"], centre.x() / width, centre.y() / height,
                                   box.width() / scale / width, box.height() / scale / height)  # fmt: skip
            else:
                centre = drag["rect"].center() + drag["offset"] / scale
                self.moved.emit(drag["kind"], drag["index"], centre.x() / width, centre.y() / height)
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self._guide_at(event.position()) is None:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        hit = self._guide_at(event.position())
        if hit is None or hit[0] not in ("overlay", "mask"):
            return super().wheelEvent(event)
        steps = event.angleDelta().y() / 120
        if steps:
            self.resized.emit(hit[0], hit[1], 1.08**steps)
        event.accept()
