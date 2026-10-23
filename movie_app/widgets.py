"""Pieces the video tabs share: the error log, frame viewers and a colour button."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
)

from . import paths

APP_TITLE = "동영상 편집기"
ERROR_LOG = paths.DATA_DIR / "error.log"


def log_error(text: str) -> None:
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
    except OSError:
        pass


def select(combo: QComboBox, data) -> None:
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)


class FrameViewer(QDialog):
    """A frame at 100%, one video pixel per screen pixel. Drag or scroll to look around."""

    def __init__(self, frame: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"원본 크기로 보기 · {frame.width()}×{frame.height()}")
        shown = QPixmap(frame)
        shown.setDevicePixelRatio((parent or self).devicePixelRatioF())  # undo Windows display scaling
        label = QLabel()
        label.setPixmap(shown)
        label.setStyleSheet("background: #111827;")
        self.area = QScrollArea()
        self.area.setWidget(label)
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.area.setStyleSheet("QScrollArea { background: #111827; border: none; }")
        self.area.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self.area.viewport().installEventFilter(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.area)
        available = (parent.screen() if parent else QGuiApplication.primaryScreen()).availableGeometry()
        size = shown.deviceIndependentSize()
        self.resize(min(round(size.width()) + 4, round(available.width() * 0.9)),
                    min(round(size.height()) + 4, round(available.height() * 0.9)))  # fmt: skip
        self._drag = None  # (where the press was, scroll positions then)

    def eventFilter(self, obj, event):
        kind = event.type()
        bars = self.area.horizontalScrollBar(), self.area.verticalScrollBar()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position(), bars[0].value(), bars[1].value())
            self.area.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if kind == QEvent.Type.MouseMove and self._drag:
            start, x, y = self._drag
            delta = event.position() - start
            bars[0].setValue(round(x - delta.x()))
            bars[1].setValue(round(y - delta.y()))
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._drag:
            self._drag = None
            self.area.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return True
        return super().eventFilter(obj, event)


class Screen(QLabel):
    """Shows a frame scaled to fit, keeping its aspect ratio; double-click shows it at 100%."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("screen")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(320, 200)
        self.setToolTip("두 번 클릭하면 원본 크기로 봅니다")
        self._frame = QPixmap()

    def show_frame(self, pixmap: QPixmap) -> None:
        self._frame = pixmap
        self._rescale()

    def show_message(self, text: str) -> None:
        self._frame = QPixmap()
        self.setText(text)

    def current_frame(self) -> QPixmap:
        """The frame shown, at its own size (empty while a message is shown)."""
        return self._frame

    def open_full_size(self) -> None:
        if not self._frame.isNull():
            viewer = FrameViewer(self._frame, self.window())
            viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # it holds a full-size frame (33 MB at 4K)
            viewer.exec()

    def mouseDoubleClickEvent(self, event):
        self.open_full_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._frame.isNull():
            return
        ratio = self.devicePixelRatioF()
        scaled = self._frame.scaled(
            self.size() * ratio, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        scaled.setDevicePixelRatio(ratio)
        self.setPixmap(scaled)


class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, tip: str, parent=None):
        super().__init__(parent)
        self.setToolTip(tip)
        self.setFixedSize(40, 28)
        self._color = "#ffffff"
        self.clicked.connect(self._pick)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = QColor(color).name()
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; border: 1px solid #9ca3af; border-radius: 6px; }}"
            "QPushButton:disabled { background: #e5e7eb; border-color: #e5e7eb; }"
        )

    def _pick(self) -> None:
        color = QColorDialog.getColor(QColor(self._color), self.window(), self.toolTip())
        if color.isValid() and color.name() != self._color:
            self.set_color(color.name())
            self.colorChanged.emit(self._color)
