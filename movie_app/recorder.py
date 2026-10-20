"""Screen recording for the video tab: pick an area (or a whole monitor) and record it with FFmpeg's ddagrab.

The red frame around the area and the little control panel are left out of the recording by Windows
(SetWindowDisplayAffinity), so they can sit anywhere, even over the recorded area.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen, QScreen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from . import media, paths, screens
from .media import format_time

FPS = 30
COUNTDOWN = 3  # seconds between pressing start and the recording starting
MIN_SIDE = 64  # physical pixels
SHADE = QColor(0, 0, 0, 110)
# Fully transparent pixels of a translucent window let clicks through to what is underneath; this
# nearly invisible fill keeps the chosen area clickable.
CLEAR = QColor(0, 0, 0, 1)
ACCENT = QColor("#2563eb")
RED = QColor("#ef4444")
HINT = "녹화할 영역을 끌어서 고르세요   ·   Enter: 이 모니터 전체   ·   Shift: 영상 비율 유지   ·   Esc: 취소"


def recordings_folder() -> Path:
    videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    return Path(videos or Path.home()) / "동영상 편집기 녹화"


@dataclass
class Region:
    output: int  # ddagrab output_idx
    x: int  # physical pixels within that output
    y: int
    width: int
    height: int
    area: QRect  # the same area in Qt's global coordinates, for drawing the frame around it


class AreaPicker(QWidget):
    """A dimmed layer over one screen on which an area is dragged out."""

    picked = Signal(object)  # Region
    cancelled = Signal()
    started = Signal(object)  # this picker, when a drag begins (the others then clear theirs)

    def __init__(self, screen: QScreen, aspect: float, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.screen_ = screen
        self.aspect = aspect
        self.anchor: QPoint | None = None
        self.selection = QRect()
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

        self.start_btn = QPushButton("● 녹화 시작", self)
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.confirm)
        self.cancel_btn = QPushButton("취소", self)
        self.cancel_btn.clicked.connect(self.cancelled.emit)
        for b in (self.start_btn, self.cancel_btn):
            b.setCursor(Qt.CursorShape.ArrowCursor)
            b.hide()

    # ---- area -----------------------------------------------------------

    def clear(self) -> None:
        self.selection = QRect()
        self.anchor = None
        self.start_btn.hide()
        self.cancel_btn.hide()
        self.update()

    def physical(self, rect: QRect) -> tuple[int, int, int, int]:
        """`rect` (this screen's logical pixels) in the screen's physical pixels, with even sides for H.264."""
        ratio = self.screen_.devicePixelRatio()
        x, y = round(rect.x() * ratio), round(rect.y() * ratio)
        return x, y, int(rect.width() * ratio) // 2 * 2, int(rect.height() * ratio) // 2 * 2

    def region(self, rect: QRect) -> Region | None:
        top_left = self.screen_.geometry().topLeft()
        # Qt keeps a screen's top-left at its position on the physical desktop; DXGI reports the same.
        output = screens.output_at(top_left.x() + 1, top_left.y() + 1) or screens.output_at(*screens.cursor())
        if output is None:
            return None
        x, y, w, h = self.physical(rect)
        w, h = min(w, output.width - x) // 2 * 2, min(h, output.height - y) // 2 * 2
        return Region(output.index, x, y, w, h, rect.translated(top_left))

    def confirm(self) -> None:
        rect = self.selection if not self.selection.isEmpty() else self.rect()
        region = self.region(rect)
        if region is not None and region.width >= MIN_SIDE and region.height >= MIN_SIDE:
            self.picked.emit(region)

    # ---- events -----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.started.emit(self)
        self.anchor = event.position().toPoint()
        self.selection = QRect(self.anchor, self.anchor)
        self.start_btn.hide()
        self.cancel_btn.hide()
        self.update()

    def mouseMoveEvent(self, event):
        if self.anchor is None:
            return
        end = event.position().toPoint()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Keep the video's shape, so the recording fills the frame without bars or cropping.
            dx, dy = end.x() - self.anchor.x(), end.y() - self.anchor.y()
            height = abs(dx) / self.aspect
            end = QPoint(end.x(), self.anchor.y() + round(height if dy >= 0 else -height))
        self.selection = QRect(self.anchor, end).normalized().intersected(self.rect())
        self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is None:
            return
        self.anchor = None
        if self.selection.width() < 8 or self.selection.height() < 8:
            self.clear()
            return
        # Buttons just below the area, or above it when there is no room below.
        size = self.start_btn.sizeHint()
        y = self.selection.bottom() + 10
        if y + size.height() > self.height():
            y = max(0, self.selection.top() - size.height() - 10)
        x = min(self.selection.left(), self.width() - size.width() - self.cancel_btn.sizeHint().width() - 20)
        self.start_btn.move(x, y)
        self.cancel_btn.move(x + size.width() + 8, y)
        self.start_btn.show()
        self.cancel_btn.show()
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.selection.contains(event.position().toPoint()):
            self.confirm()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirm()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), SHADE)
        if self.selection.isEmpty():
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            self._label(painter, QRect(0, 30, self.width(), 40), HINT)
            painter.end()
            return
        painter.fillRect(self.selection, CLEAR)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QPen(ACCENT, 2))
        painter.drawRect(self.selection.adjusted(-1, -1, 0, 0))
        _, _, w, h = self.physical(self.selection)
        self._label(painter, QRect(self.selection.left(), self.selection.top() - 34, 200, 28), f"{w} × {h}", center=False)
        painter.end()

    def _label(self, painter: QPainter, box: QRect, text: str, center: bool = True) -> None:
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.1)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 24
        x = box.center().x() - width // 2 if center else box.left()
        pill = QRect(x, box.top(), width, box.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(17, 24, 39, 220))
        painter.drawRoundedRect(pill, 8, 8)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


class RecordingFrame(QWidget):
    """A red outline around the recorded area. Clicks pass through it, and recordings leave it out."""

    MARGIN = 4

    def __init__(self, area: QRect, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(area.adjusted(-self.MARGIN, -self.MARGIN, self.MARGIN, self.MARGIN))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(QPen(RED, 3, Qt.PenStyle.DashLine))
        painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
        painter.end()


class RecordingPanel(QWidget):
    """Countdown, then elapsed time with Stop and Cancel. Recordings leave it out."""

    stop = Signal()
    cancel = Signal()

    def __init__(self, area: QRect, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setStyleSheet("QWidget#box { background: #111827; border-radius: 10px; } QLabel { color: #ffffff; }")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        box = QWidget(self)
        box.setObjectName("box")
        row = QHBoxLayout(box)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(8)
        self.label = QLabel()
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 1.15)
        font.setBold(True)
        self.label.setFont(font)
        self.label.setMinimumWidth(130)
        self.stop_btn = QPushButton("■ 정지")
        self.stop_btn.setObjectName("primary")
        self.stop_btn.clicked.connect(self.stop.emit)
        cancel_btn = QPushButton("취소")
        cancel_btn.setToolTip("녹화를 버립니다")
        cancel_btn.clicked.connect(self.cancel.emit)
        row.addWidget(self.label)
        row.addWidget(self.stop_btn)
        row.addWidget(cancel_btn)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)
        self.adjustSize()
        self._place(area)

    def _place(self, area: QRect) -> None:
        """Just below the area, else above it, else inside its top edge (it is left out of the recording)."""
        screen = QGuiApplication.screenAt(area.center()) or QGuiApplication.primaryScreen()
        bounds = screen.availableGeometry()
        x = min(max(bounds.left(), area.center().x() - self.width() // 2), bounds.right() - self.width())
        y = area.bottom() + 12
        if y + self.height() > bounds.bottom():
            y = area.top() - self.height() - 12
            if y < bounds.top():
                y = area.top() + 12
        self.move(x, y)

    def show_text(self, text: str) -> None:
        self.label.setText(text)


class ScreenRecorder(QObject):
    """Runs one recording: pick an area, count down, record, stop. Emits the saved file or None."""

    finished = Signal(object)  # Path, or None when cancelled
    failed = Signal(str)

    def __init__(self, aspect: float, parent=None):
        super().__init__(parent)
        self.aspect = aspect
        self.pickers: list[AreaPicker] = []
        self.frame: RecordingFrame | None = None
        self.panel: RecordingPanel | None = None
        self.proc: subprocess.Popen | None = None
        self.region: Region | None = None
        self.output: Path | None = None
        self._temp = tempfile.TemporaryDirectory(prefix="movie_rec_")
        self._log = Path(self._temp.name) / "ffmpeg.log"
        self._countdown = 0
        self._started_at = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not screens.outputs():
            self.failed.emit("화면 녹화를 쓸 수 없는 환경입니다 (원격 데스크톱 등).")
            return
        for screen in QGuiApplication.screens():
            picker = AreaPicker(screen, self.aspect)
            picker.picked.connect(self._picked)
            picker.cancelled.connect(self._cancel_picking)
            picker.started.connect(self._picker_started)
            picker.show()
            self.pickers.append(picker)
        under_mouse = QGuiApplication.screenAt(QCursor.pos())  # keyboard focus (Enter, Esc) goes to that screen
        for picker in self.pickers:
            if picker.screen_ is under_mouse:
                picker.activateWindow()
                picker.setFocus()

    def _picker_started(self, active: AreaPicker) -> None:
        for picker in self.pickers:
            if picker is not active:
                picker.clear()

    def _close_pickers(self) -> None:
        for picker in self.pickers:
            picker.close()
        self.pickers = []

    def _cancel_picking(self) -> None:
        self._close_pickers()
        self.finished.emit(None)

    def _picked(self, region: Region) -> None:
        self._close_pickers()
        self.region = region
        self.frame = RecordingFrame(region.area)
        self.panel = RecordingPanel(region.area)
        self.panel.stop.connect(self.stop)
        self.panel.cancel.connect(self.cancel)
        for window in (self.frame, self.panel):
            window.show()
            screens.exclude_from_capture(int(window.winId()))
        self._countdown = COUNTDOWN
        self.panel.stop_btn.setEnabled(False)
        self.panel.show_text(f"{self._countdown}초 뒤 녹화…")
        QTimer.singleShot(1000, self._count)

    def _count(self) -> None:
        if self.panel is None:
            return  # cancelled during the countdown
        self._countdown -= 1
        if self._countdown > 0:
            self.panel.show_text(f"{self._countdown}초 뒤 녹화…")
            QTimer.singleShot(1000, self._count)
            return
        self._record()

    def _record(self) -> None:
        region = self.region
        folder = recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        self.output = folder / f"화면녹화_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        # Matroska survives a hard stop; it is turned into MP4 once the recording ends.
        self._raw = Path(self._temp.name) / "recording.mkv"
        grab = (
            f"ddagrab=output_idx={region.output}:framerate={FPS}:draw_mouse=1"
            f":video_size={region.width}x{region.height}:offset_x={region.x}:offset_y={region.y}"
            ",hwdownload,format=bgra"
        )
        with self._log.open("w", encoding="utf-8") as err:
            self.proc = subprocess.Popen(
                [
                    paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", grab,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                    str(self._raw),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=err,
                creationflags=media.NO_WINDOW,
            )  # fmt: skip
        self._started_at = time.monotonic()
        self.panel.stop_btn.setEnabled(True)
        self.timer.start()
        self._tick()

    def _tick(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            # FFmpeg stopped on its own: the screen could not be captured.
            self.timer.stop()
            message = self._log.read_text(encoding="utf-8", errors="replace").strip()[-400:]
            self._cleanup()
            self.failed.emit(f"화면을 녹화하지 못했습니다.\n{message}")
            return
        self.panel.show_text(f"● 녹화 중  {format_time(time.monotonic() - self._started_at)}")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.timer.stop()
        self.panel.show_text("저장하는 중…")
        self.panel.stop_btn.setEnabled(False)
        QGuiApplication.processEvents()
        try:
            self.proc.communicate(b"q", timeout=15)  # FFmpeg finishes the file when it reads "q"
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None
        try:
            subprocess.run(
                [paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(self._raw),
                 "-c", "copy", "-movflags", "+faststart", str(self.output)],
                check=True, capture_output=True, creationflags=media.NO_WINDOW,
            )  # fmt: skip
        except (OSError, subprocess.CalledProcessError) as e:
            self._cleanup()
            self.failed.emit(f"녹화를 저장하지 못했습니다.\n{e}")
            return
        output = self.output
        self._cleanup()
        self.finished.emit(output)

    def cancel(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        self.timer.stop()
        self._cleanup()
        self.finished.emit(None)

    def _cleanup(self) -> None:
        for window in (self.frame, self.panel):
            if window is not None:
                window.close()
                window.deleteLater()
        self.frame = self.panel = None
        self._temp.cleanup()
