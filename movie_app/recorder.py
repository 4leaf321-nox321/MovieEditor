"""Screen recording for the video tab: pick an area (or a whole monitor) and record it with FFmpeg's ddagrab,
with the microphone and the computer's own sound if wanted.

The red frame around the area and the little control panel are left out of the recording by Windows
(SetWindowDisplayAffinity), so they can sit anywhere, even over the recorded area.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QKeyEvent, QPainter, QPen, QScreen
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QWidget

from . import audio_capture, media, paths, screens
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

    def __init__(self, screen: QScreen, aspect: float, sound: tuple[bool, bool] = (False, False), parent=None):
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
        # What sound goes in, chosen next to the buttons.
        self.sound_box = QWidget(self)
        self.sound_box.setStyleSheet("QWidget { background: #111827; border-radius: 8px; } QCheckBox { color: #ffffff; padding: 2px 6px; }")
        row = QHBoxLayout(self.sound_box)
        row.setContentsMargins(6, 4, 6, 4)
        self.mic_check = QCheckBox("마이크")
        self.mic_check.setChecked(sound[0])
        self.mic_check.setEnabled(audio_capture.has_microphone())
        self.system_check = QCheckBox("컴퓨터 소리")
        self.system_check.setChecked(sound[1])
        self.system_check.setToolTip("스피커로 나오는 소리 (영상, 음악, 알림 소리 등)")
        row.addWidget(self.mic_check)
        row.addWidget(self.system_check)
        self.sound_box.adjustSize()
        for b in (self.start_btn, self.cancel_btn, self.sound_box):
            b.setCursor(Qt.CursorShape.ArrowCursor)
            b.hide()

    def sound(self) -> tuple[bool, bool]:
        return self.mic_check.isChecked() and self.mic_check.isEnabled(), self.system_check.isChecked()

    # ---- area -----------------------------------------------------------

    def clear(self) -> None:
        self.selection = QRect()
        self.anchor = None
        self.start_btn.hide()
        self.cancel_btn.hide()
        self.sound_box.hide()
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
        self.sound_box.hide()
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
        cancel_w, sound_w = self.cancel_btn.sizeHint().width(), self.sound_box.width()
        y = self.selection.bottom() + 10
        if y + size.height() > self.height():
            y = max(0, self.selection.top() - size.height() - 10)
        x = max(0, min(self.selection.left(), self.width() - size.width() - cancel_w - sound_w - 30))
        self.start_btn.move(x, y)
        self.cancel_btn.move(x + size.width() + 8, y)
        self.sound_box.move(x + size.width() + cancel_w + 20, y + (size.height() - self.sound_box.height()) // 2)
        for widget in (self.start_btn, self.cancel_btn, self.sound_box):
            widget.show()
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
    """Runs one recording: pick an area, count down, record, stop. Emits the saved file or None.

    Sound is recorded beside the picture (see audio_capture) and lined up with it when the recording
    stops: FFmpeg's progress reports tell when its first frame was taken.
    """

    finished = Signal(object)  # Path, or None when cancelled
    failed = Signal(str)

    def __init__(self, aspect: float, store: QSettings | None = None, parent=None, fps: int = FPS):
        super().__init__(parent)
        self.aspect = aspect
        self.fps = fps  # frames per second recorded
        self.store = store  # remembers which sound was wanted last time
        self.pickers: list[AreaPicker] = []
        self.frame: RecordingFrame | None = None
        self.panel: RecordingPanel | None = None
        self.proc: subprocess.Popen | None = None
        self.region: Region | None = None
        self.output: Path | None = None
        self.sound = (False, False)  # (microphone, the computer's sound)
        self.captures: list[audio_capture._Capture] = []
        self.notes: list[str] = []  # told to the user with the result (e.g. a sound that could not be recorded)
        self._video_start: float | None = None  # perf_counter when the first frame was taken
        self._launched = 0.0  # perf_counter when FFmpeg was started
        self._reader: threading.Thread | None = None
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
        wanted = (False, False)
        if self.store is not None:
            wanted = (self.store.value("record_mic", False, type=bool), self.store.value("record_system", False, type=bool))
        for screen in QGuiApplication.screens():
            picker = AreaPicker(screen, self.aspect, wanted)
            picker.picked.connect(lambda region, p=picker: self._picked(region, p.sound()))
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

    def _picked(self, region: Region, sound: tuple[bool, bool] = (False, False)) -> None:
        self._close_pickers()
        self.region = region
        self.sound = sound
        if self.store is not None:
            self.store.setValue("record_mic", sound[0])
            self.store.setValue("record_system", sound[1])
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
            f"ddagrab=output_idx={region.output}:framerate={self.fps}:draw_mouse=1"
            f":video_size={region.width}x{region.height}:offset_x={region.x}:offset_y={region.y}"
            ",hwdownload,format=bgra"
        )
        # Sound starts first, so it is already running when the first frame is taken; the part before that
        # is cut off afterwards.
        folder = Path(self._temp.name)
        if self.sound[0]:
            self.captures.append(audio_capture.MicCapture(folder / "mic.wav"))
        if self.sound[1]:
            self.captures.append(audio_capture.LoopbackCapture(folder / "system.wav"))
        for capture in self.captures:
            capture.start()
        self._launched = time.perf_counter()
        with self._log.open("w", encoding="utf-8") as err:
            self.proc = subprocess.Popen(
                [
                    paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", grab,
                    # zerolatency: frames are written as soon as they are taken, so the progress reports tell
                    # when the first one was taken (x264 otherwise holds a frame per thread).
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-progress", "pipe:1", "-stats_period", "0.1", str(self._raw),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                creationflags=media.NO_WINDOW,
            )  # fmt: skip
        self._reader = threading.Thread(target=self._read_progress, args=(self.proc,), daemon=True)
        self._reader.start()
        self._started_at = time.monotonic()
        self.panel.stop_btn.setEnabled(True)
        self.timer.start()
        self._tick()

    def _read_progress(self, proc: subprocess.Popen) -> None:
        """When the first frame was taken: each report says how much video is out by now."""
        for raw in proc.stdout:
            key, _, value = raw.decode("utf-8", errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and int(value) > 0:
                # out_time is where the last frame written ends; that frame was taken one frame earlier.
                start = time.perf_counter() - int(value) / 1e6 + 1 / self.fps
                # A report can only come late, never early, so the earliest estimate is the best.
                if self._video_start is None or start < self._video_start:
                    self._video_start = start

    def _tick(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            # FFmpeg stopped on its own: the screen could not be captured.
            self.timer.stop()
            message = self._log.read_text(encoding="utf-8", errors="replace").strip()[-400:]
            self._stop_sound()
            self._cleanup()
            self.failed.emit(f"화면을 녹화하지 못했습니다.\n{message}")
            return
        sound = " · ".join(name for name, on in (("마이크", self.sound[0]), ("컴퓨터 소리", self.sound[1])) if on)
        self.panel.show_text(f"● 녹화 중  {format_time(time.monotonic() - self._started_at)}" + (f"  ({sound})" if sound else ""))

    def _stop_sound(self) -> None:
        for capture in self.captures:
            capture.stop()

    def stop(self) -> None:
        if self.proc is None:
            return
        self.timer.stop()
        self.panel.show_text("저장하는 중…")
        self.panel.stop_btn.setEnabled(False)
        QGuiApplication.processEvents()
        try:
            self.proc.stdin.write(b"q")  # FFmpeg finishes the file when it reads "q"
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None
        self._stop_sound()
        if self._reader is not None:
            self._reader.join(2)
        error = self._finish(self._finish_args())
        if error and any(c.ok for c in self.captures):
            # The sound would not go in: keep at least the picture.
            self.notes.append("소리를 합치지 못해 화면만 저장했습니다.")
            error = self._finish(self._finish_args(with_sound=False))
        if error:
            # Not even that: the raw recording is kept where the recordings go, rather than thrown away.
            kept = self.output.with_suffix(".mkv")
            try:
                shutil.move(str(self._raw), kept)
                error += f"\n\n원본 녹화는 남겨 두었습니다: {kept}"
            except OSError:
                pass
            self._cleanup()
            self.failed.emit(f"녹화를 저장하지 못했습니다.\n{error}")
            return
        output = self.output
        self._cleanup()
        self.finished.emit(output)

    def _finish(self, args: list[str]) -> str:
        """Run the final FFmpeg step; what went wrong, or "" when it worked."""
        try:
            subprocess.run(args, check=True, capture_output=True, creationflags=media.NO_WINDOW)
        except OSError as e:
            return str(e)
        except subprocess.CalledProcessError as e:
            return (e.stderr or b"").decode("utf-8", errors="replace").strip()[-600:] or f"FFmpeg 오류 (코드 {e.returncode})"
        return ""

    def _finish_args(self, with_sound: bool = True) -> list[str]:
        """FFmpeg arguments making the MP4: the picture as it is, and the sound lined up with it."""
        args = [paths.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(self._raw)]
        sounds = []
        if self._video_start is None and any(c.ok for c in self.captures):
            # No progress report was read (stopped at once): line the sound up with when FFmpeg was started.
            self._video_start = self._launched
            self.notes.append("소리와 화면이 조금 어긋날 수 있습니다.")
        for capture in self.captures if with_sound else []:
            if capture.ok:
                sounds.append(capture)
            elif capture.error:
                self.notes.append(f"{capture.error} — 소리 없이 저장했습니다.")
            if capture.ok and capture.note:
                self.notes.append(capture.note)
        if not sounds:
            return [*args, "-c", "copy", "-movflags", "+faststart", str(self.output)]
        graph, labels = [], []
        for k, capture in enumerate(sounds, 1):
            args += ["-i", str(capture.path)]
            offset = capture.start_time - self._video_start
            # Sound that began before the first frame is cut; sound that began after it is delayed.
            shift = f"adelay={round(offset * 1000)}:all=1" if offset >= 0 else f"atrim=start={-offset:.3f},asetpts=PTS-STARTPTS"
            graph.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,{shift}[s{k}]")
            labels.append(f"[s{k}]")
        mix = f"amix=inputs={len(labels)}:duration=longest:normalize=0," if len(labels) > 1 else ""
        graph.append(f"{''.join(labels)}{mix}apad[a]")
        return [*args, "-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(self.output)]  # fmt: skip

    def cancel(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        self._stop_sound()
        self.timer.stop()
        self._cleanup()
        self.finished.emit(None)

    def _cleanup(self) -> None:
        for window in (self.frame, self.panel):
            if window is not None:
                window.close()
                window.deleteLater()
        self.frame = self.panel = None
        try:
            self._temp.cleanup()
        except OSError:
            pass  # a sound file still held; the system clears its temporary folder later
