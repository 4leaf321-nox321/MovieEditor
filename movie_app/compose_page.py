"""The compose tab: puts a voice track and subtitles on a video."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import media, paths, render, subs
from .media import MediaInfo, format_time
from .subs import SubStyle
from .widgets import APP_TITLE, ColorButton, Screen, log_error, select

FITS = [("audio", "음성 길이에 맞춤"), ("video", "영상 길이에 맞춤")]
EXTENDS = [("freeze", "마지막 장면에서 멈춤"), ("loop", "처음부터 반복")]
QUALITIES = [("fast", "빠름"), ("standard", "표준"), ("high", "고품질")]
SUBTITLE_MODES = [("burn", "영상에 입히기"), ("soft", "켜고 끄는 자막 (빠름)")]
POSITIONS = [("bottom", "아래"), ("middle", "가운데"), ("top", "위")]
DEFAULT_JOB = render.Job(Path(), Path())
DEFAULT_STYLE = SubStyle()
# Job fields kept in settings.ini between runs (files are not remembered).
JOB_KEYS = ("fit", "extend", "delay", "tail", "voice_volume", "original_volume", "subtitle_mode", "quality")


def _filter(title: str, suffixes: tuple[str, ...]) -> str:
    return f"{title} ({' '.join('*' + s for s in suffixes)})"


VIDEO_FILTER = _filter("영상 파일", media.VIDEO_SUFFIXES)
# kind -> (label, dialog filters, hint shown while empty)
FILE_KINDS = {
    "video": ("영상", [VIDEO_FILTER], "바탕이 될 영상"),
    "audio": (
        "음성",
        [_filter("음성 파일", media.AUDIO_SUFFIXES), VIDEO_FILTER],
        "[음성 만들기] 탭에서 보내면 자동으로 들어옵니다 · 같은 이름의 자막이 있으면 함께 불러옵니다",
    ),
    "subtitle": ("자막", [_filter("자막 파일", subs.SUBTITLE_SUFFIXES)], "SRT, ASS, VTT"),
}


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in media.VIDEO_SUFFIXES:
        return "video"
    if suffix in media.AUDIO_SUFFIXES:
        return "audio"
    if suffix in subs.SUBTITLE_SUFFIXES:
        return "subtitle"
    return None


class PreviewThread(QThread):
    ready = Signal(object)  # BMP bytes
    failed = Signal(str)

    def __init__(self, info: MediaInfo, at: float, text: str | None, style: SubStyle, parent=None):
        super().__init__(parent)
        self.info = info
        self.at = at
        self.text = text
        self.style = style

    def run(self):
        try:
            self.ready.emit(render.preview_frame(self.info, self.at, self.text, self.style))
        except Exception as e:  # noqa: BLE001 - shown under the preview
            self.failed.emit(str(e))


class RenderThread(QThread):
    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, job: render.Job, parent=None):
        super().__init__(parent)
        self.job = job
        self.elapsed = 0.0
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        start = time.perf_counter()
        try:
            plan = render.run(self.job, self.progress.emit, lambda: self._cancel)
        except render.Cancelled:
            self.cancelled.emit()
        except (OSError, ValueError, RuntimeError) as e:
            log_error(traceback.format_exc())
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user
            log_error(traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.elapsed = time.perf_counter() - start
            self.done.emit(plan)


class ComposePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.store.beginGroup("compose")  # the other tabs keep their settings in the same file
        self.files: dict[str, Path | None] = dict.fromkeys(FILE_KINDS)
        self.video_info: MediaInfo | None = None
        self.audio_info: MediaInfo | None = None
        self.cues: list[subs.Cue] = []
        self.preview_thread: PreviewThread | None = None
        self.render_thread: RenderThread | None = None
        self._preview_pending = False
        self._render_started = 0.0

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(250)
        self.preview_timer.timeout.connect(self._start_preview)

        self._build_ui()
        self._restore()
        self._connect_changes()

        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(lambda: self.browse("video"))
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.make_video)

        self._set_busy(False)
        self._refresh()
        self._show_status("영상과 음성, 자막을 고른 뒤 [합성하기]를 누르세요. 파일을 창에 끌어다 놓아도 됩니다.")

    # ---- layout -------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_files_box())
        left.addWidget(self._build_preview_panel(), 1)
        body.addLayout(left, 1)

        side = QWidget()
        side.setFixedWidth(380)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        side_layout.addWidget(self._build_sound_box())
        side_layout.addWidget(self._build_subtitle_box())
        side_layout.addStretch(1)
        body.addWidget(side)

        root.addWidget(self._build_action_row())

    def _build_files_box(self) -> QGroupBox:
        box = QGroupBox("파일")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        self.file_edits: dict[str, QLineEdit] = {}
        self.file_infos: dict[str, QLabel] = {}
        for i, (kind, (title, _, _)) in enumerate(FILE_KINDS.items()):
            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setPlaceholderText("파일을 고르거나 창에 끌어다 놓으세요")
            browse = QPushButton("찾기…")
            browse.clicked.connect(lambda _=False, k=kind: self.browse(k))
            clear = QPushButton("✕")
            clear.setFixedWidth(36)
            clear.setToolTip("빼기")
            clear.clicked.connect(lambda _=False, k=kind: self.set_file(k, None))
            info = QLabel()
            info.setObjectName("muted")
            grid.addWidget(QLabel(title), i * 2, 0)
            grid.addWidget(edit, i * 2, 1)
            grid.addWidget(browse, i * 2, 2)
            grid.addWidget(clear, i * 2, 3)
            grid.addWidget(info, i * 2 + 1, 1, 1, 3)
            self.file_edits[kind] = edit
            self.file_infos[kind] = info
        return box

    def _build_preview_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        self.screen = Screen()
        layout.addWidget(self.screen, 1)

        row = QHBoxLayout()
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1000)
        self.position_slider.setToolTip("미리 볼 장면을 고릅니다")
        self.position_slider.valueChanged.connect(self._on_position_changed)
        self.position_slider.sliderReleased.connect(self.preview_timer.start)
        self.time_label = QLabel()
        self.time_label.setMinimumWidth(130)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        full_btn = QPushButton("원본 크기로 보기")
        full_btn.setToolTip("이 장면을 확대 없이 실제 픽셀 그대로 봅니다 (미리보기를 두 번 클릭해도 됩니다)")
        full_btn.clicked.connect(self.screen.open_full_size)
        row.addWidget(self.position_slider, 1)
        row.addWidget(self.time_label)
        row.addWidget(full_btn)
        layout.addLayout(row)

        self.preview_note = QLabel()
        self.preview_note.setObjectName("muted")
        layout.addWidget(self.preview_note)
        return panel

    def _build_sound_box(self) -> QGroupBox:
        box = QGroupBox("길이와 소리")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.fit_combo = self._add_combo(grid, 0, "결과 길이", FITS)
        self.extend_combo = self._add_combo(grid, 1, "영상이 짧으면", EXTENDS)
        self.delay_spin = self._add_seconds(grid, 2, "음성 시작", 600.0)
        self.delay_spin.setToolTip("음성을 몇 초 뒤에 시작할지 정합니다. 자막도 같이 움직입니다.")
        self.tail_spin = self._add_seconds(grid, 3, "끝 여백", 30.0)
        self.tail_spin.setToolTip("음성이 끝난 뒤 영상을 조금 더 이어 갑니다")
        self.voice_slider = self._add_slider(grid, 4, "음성 볼륨", 0, 200, lambda v: f"{v}%")
        self.original_slider = self._add_slider(grid, 5, "원본 소리", 0, 100, lambda v: f"{v}%" if v else "끔")
        self.original_slider.setToolTip("영상에 원래 들어 있던 소리의 크기입니다. 0이면 뺍니다.")
        self.quality_combo = self._add_combo(grid, 6, "화질", QUALITIES)
        self.quality_combo.setToolTip("고품질은 파일이 조금 커지고 만드는 데 시간이 더 걸립니다")

        self.summary_label = QLabel()
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        grid.addWidget(self.summary_label, 7, 0, 1, 3)
        return box

    def _build_subtitle_box(self) -> QGroupBox:
        box = QGroupBox("자막")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.mode_combo = self._add_combo(grid, 0, "방식", SUBTITLE_MODES)
        self.mode_combo.setToolTip(
            "영상에 입히기: 어디서 재생해도 보입니다.\n"
            "켜고 끄는 자막: 다시 인코딩하지 않아 빠르지만, 재생 프로그램에서 자막을 켜야 보이고 글꼴 설정은 쓰이지 않습니다."
        )

        self.font_combo = QFontComboBox()
        self.font_combo.setWritingSystem(QFontDatabase.WritingSystem.Korean)
        self.font_combo.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
        self.font_combo.setMaxVisibleItems(20)
        grid.addWidget(QLabel("글꼴"), 1, 0)
        grid.addWidget(self.font_combo, 1, 1, 1, 2)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 200)
        self.size_spin.setToolTip("1080p 영상 기준 크기입니다. 다른 해상도에서도 같은 비율로 보입니다.")
        self.bold_check = QCheckBox("굵게")
        grid.addWidget(QLabel("크기"), 2, 0)
        grid.addLayout(self._row(self.size_spin, self.bold_check), 2, 1, 1, 2)

        self.color_btn = ColorButton("글자색")
        self.outline_color_btn = ColorButton("외곽선 색")
        self.outline_spin = QDoubleSpinBox()
        self.outline_spin.setRange(0.0, 20.0)
        self.outline_spin.setSingleStep(0.5)
        self.outline_spin.setDecimals(1)
        self.outline_spin.setToolTip("외곽선 두께 (0이면 없음)")
        grid.addWidget(QLabel("글자색"), 3, 0)
        grid.addLayout(self._row(self.color_btn, QLabel("외곽선"), self.outline_color_btn, self.outline_spin), 3, 1, 1, 2)

        self.box_check = QCheckBox("배경 상자")
        self.box_check.setToolTip("외곽선 대신 글자 뒤에 반투명 상자를 깝니다")
        self.box_color_btn = ColorButton("상자 색")
        self.box_opacity_spin = QSpinBox()
        self.box_opacity_spin.setRange(0, 100)
        self.box_opacity_spin.setSuffix(" %")
        self.box_opacity_spin.setToolTip("상자 불투명도")
        grid.addWidget(QLabel("배경"), 4, 0)
        grid.addLayout(self._row(self.box_check, self.box_color_btn, self.box_opacity_spin), 4, 1, 1, 2)

        self.position_combo = QComboBox()
        for code, label in POSITIONS:
            self.position_combo.addItem(label, code)
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 600)
        self.margin_spin.setToolTip("화면 가장자리에서 떨어진 거리 (1080p 기준)")
        grid.addWidget(QLabel("위치"), 5, 0)
        grid.addLayout(self._row(self.position_combo, QLabel("여백"), self.margin_spin), 5, 1, 1, 2)

        reset_btn = QPushButton("기본값으로")
        reset_btn.clicked.connect(lambda: self._apply_style(DEFAULT_STYLE))
        grid.addWidget(reset_btn, 6, 1, 1, 2, Qt.AlignmentFlag.AlignRight)
        self.style_widgets = [
            self.font_combo, self.size_spin, self.bold_check, self.color_btn, self.outline_color_btn,
            self.outline_spin, self.box_check, self.box_color_btn, self.box_opacity_spin,
            self.position_combo, self.margin_spin, reset_btn,
        ]  # fmt: skip
        return box

    @staticmethod
    def _row(*widgets: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for w in widgets:
            row.addWidget(w)
        row.addStretch(1)
        return row

    def _add_combo(self, grid: QGridLayout, row: int, title: str, items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        for code, label in items:
            combo.addItem(label, code)
        grid.addWidget(QLabel(title), row, 0)
        grid.addWidget(combo, row, 1, 1, 2)
        return combo

    def _add_slider(self, grid: QGridLayout, row: int, title: str, lo: int, hi: int, fmt) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        label = QLabel(fmt(slider.value()))
        label.setMinimumWidth(50)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(lambda v: label.setText(fmt(v)))
        grid.addWidget(QLabel(title), row, 0)
        grid.addWidget(slider, row, 1)
        grid.addWidget(label, row, 2)
        return slider

    def _add_seconds(self, grid: QGridLayout, row: int, title: str, hi: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, hi)
        spin.setSingleStep(0.1)
        spin.setDecimals(1)
        spin.setSuffix(" 초")
        grid.addWidget(QLabel(title), row, 0)
        grid.addWidget(spin, row, 1, 1, 2)
        return spin

    def _build_action_row(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.make_btn = QPushButton("합성하기")
        self.make_btn.setObjectName("primary")
        self.make_btn.setToolTip("영상에 음성과 자막을 합성해 MP4로 저장합니다 (Ctrl+Enter)")
        self.make_btn.clicked.connect(self.make_video)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_render)
        row.addWidget(self.make_btn)
        row.addWidget(self.cancel_btn)
        row.addSpacing(8)

        col = QVBoxLayout()
        col.setSpacing(4)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.progress)
        col.addWidget(self.status)
        row.addLayout(col, 1)
        return w

    def _connect_changes(self) -> None:
        for combo in (self.fit_combo, self.extend_combo, self.quality_combo, self.mode_combo, self.position_combo):
            combo.currentIndexChanged.connect(self._refresh)
        for spin in (self.delay_spin, self.tail_spin, self.size_spin, self.outline_spin,
                     self.box_opacity_spin, self.margin_spin):  # fmt: skip
            spin.valueChanged.connect(self._refresh)
        for slider in (self.voice_slider, self.original_slider):
            slider.valueChanged.connect(self._refresh)
        for check in (self.bold_check, self.box_check):
            check.toggled.connect(self._refresh)
        for button in (self.color_btn, self.outline_color_btn, self.box_color_btn):
            button.colorChanged.connect(self._refresh)
        self.font_combo.currentFontChanged.connect(self._refresh)

    # ---- settings -----------------------------------------------------------

    def _apply_job_settings(self, job: render.Job) -> None:
        select(self.fit_combo, job.fit)
        select(self.extend_combo, job.extend)
        self.delay_spin.setValue(job.delay)
        self.tail_spin.setValue(job.tail)
        self.voice_slider.setValue(round(job.voice_volume * 100))
        self.original_slider.setValue(round(job.original_volume * 100))
        select(self.mode_combo, job.subtitle_mode)
        select(self.quality_combo, job.quality)

    def _apply_style(self, st: SubStyle) -> None:
        self.font_combo.setCurrentFont(QFont(st.font))
        self.size_spin.setValue(st.size)
        self.bold_check.setChecked(st.bold)
        self.color_btn.set_color(st.color)
        self.outline_color_btn.set_color(st.outline_color)
        self.outline_spin.setValue(st.outline)
        self.box_check.setChecked(st.box)
        self.box_color_btn.set_color(st.box_color)
        self.box_opacity_spin.setValue(st.box_opacity)
        select(self.position_combo, st.position)
        self.margin_spin.setValue(st.margin)
        self._refresh()

    def current_style(self) -> SubStyle:
        return SubStyle(
            font=self.font_combo.currentFont().family(),
            size=self.size_spin.value(),
            bold=self.bold_check.isChecked(),
            color=self.color_btn.color(),
            outline_color=self.outline_color_btn.color(),
            outline=self.outline_spin.value(),
            box=self.box_check.isChecked(),
            box_color=self.box_color_btn.color(),
            box_opacity=self.box_opacity_spin.value(),
            position=self.position_combo.currentData(),
            margin=self.margin_spin.value(),
        )

    def current_job(self, output: Path) -> render.Job:
        return render.Job(
            video=self.files["video"],
            output=output,
            audio=self.files["audio"],
            subtitle=self.files["subtitle"],
            fit=self.fit_combo.currentData(),
            extend=self.extend_combo.currentData(),
            delay=self.delay_spin.value(),
            tail=self.tail_spin.value(),
            voice_volume=self.voice_slider.value() / 100,
            original_volume=self.original_slider.value() / 100,
            subtitle_mode=self.mode_combo.currentData(),
            style=self.current_style(),
            quality=self.quality_combo.currentData(),
        )

    def _restore(self) -> None:
        s = self.store
        job = {key: s.value(key, getattr(DEFAULT_JOB, key), type=type(getattr(DEFAULT_JOB, key))) for key in JOB_KEYS}
        self._apply_job_settings(dataclasses.replace(DEFAULT_JOB, **job))
        style = {}
        for f in dataclasses.fields(SubStyle):
            default = getattr(DEFAULT_STYLE, f.name)
            style[f.name] = s.value(f"style/{f.name}", default, type=type(default))
        self._apply_style(SubStyle(**style))

    def _save_store(self) -> None:
        s = self.store
        job = self.current_job(Path())
        for key in JOB_KEYS:
            s.setValue(key, getattr(job, key))
        for key, value in dataclasses.asdict(job.style).items():
            s.setValue(f"style/{key}", value)
        s.sync()

    # ---- files --------------------------------------------------------------

    def browse(self, kind: str) -> None:
        title, filters, _ = FILE_KINDS[kind]
        path, _ = QFileDialog.getOpenFileName(
            self, f"{title} 열기", str(self.store.value("open_dir", "")), ";;".join([*filters, "모든 파일 (*.*)"])
        )
        if path:
            self.store.setValue("open_dir", str(Path(path).parent))
            self.set_file(kind, Path(path))

    def set_file(self, kind: str, path: Path | None) -> bool:
        info: MediaInfo | None = None
        cues: list[subs.Cue] = []
        if path is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                if kind == "subtitle":
                    cues = subs.load_cues(path)
                else:
                    info = media.probe(path)
                    if kind == "video" and not info.has_video:
                        raise ValueError(f"영상이 들어 있지 않은 파일입니다: {path.name}")
                    if kind == "audio" and not info.has_audio:
                        raise ValueError(f"소리가 들어 있지 않은 파일입니다: {path.name}")
                    if info.duration <= 0:  # e.g. a WebM recorded by streaming, which never states its length
                        raise ValueError(f"길이를 알 수 없는 파일입니다: {path.name}\nMP4 등으로 바꾼 뒤 다시 넣어 주세요.")
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, APP_TITLE, str(e))
                return False
            finally:
                QApplication.restoreOverrideCursor()

        previous_audio = self.files["audio"]
        self.files[kind] = path
        if kind == "video":
            self.video_info = info
        elif kind == "audio":
            self.audio_info = info
        else:
            self.cues = cues
        edit = self.file_edits[kind]
        edit.setText(path.name if path else "")
        edit.setToolTip(str(path) if path else "")
        if path is not None:
            self._show_status(f"불러왔습니다: {path.name}")

        # The TTS app saves 음성.mp3 next to 음성.srt; pick that subtitle up along with the voice (read again
        # even when it is the same file, which may have been saved over).
        if kind == "audio" and path is not None:
            twin = next((path.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if path.with_suffix(s).exists()), None)
            subtitle = self.files["subtitle"]
            if twin is not None:
                if self.set_file("subtitle", twin):
                    self._show_status(f"같은 이름의 자막도 불러왔습니다: {twin.name}")
            elif subtitle and previous_audio and subtitle.with_suffix("") == previous_audio.with_suffix(""):
                # The subtitles came with the previous voice; they do not fit this one.
                self.set_file("subtitle", None)
                self._show_status(f"이 음성과 같은 이름의 자막이 없어서, 앞 음성의 자막({subtitle.name})을 뺐습니다.")
        self._refresh()
        return True

    def receive_voice(self, audio: Path) -> None:
        """A voice from the voice tab; its subtitles sit beside it under the same name."""
        if not self.set_file("audio", audio):
            return
        if self.files["video"] is None:
            self._show_status("음성과 자막을 받았습니다. [영상 만들기] 탭에서 영상을 만들거나 바탕 영상을 고르세요.")
        else:
            self._show_status("음성과 자막을 받았습니다. 확인한 뒤 [합성하기]를 누르세요.")

    def receive_video(self, video: Path) -> None:
        """A video made in the video tab, to put the voice and subtitles on."""
        if not self.set_file("video", video):
            return
        if self.video_info.has_audio and self.original_slider.value() == 0:
            # It only has sound when a scene asked for its clip's sound; "원본 소리: 끔" would drop it.
            self.original_slider.setValue(100)
            self._show_status("영상을 받았습니다. 장면의 클립 소리가 들어 있어서 [원본 소리]를 100%로 맞췄습니다.")
        else:
            self._show_status("영상을 받았습니다. 확인한 뒤 [합성하기]를 누르세요.")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        dropped = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        order = list(FILE_KINDS)
        # Video first and subtitles last, so a subtitle dropped along with a voice wins over its twin.
        known = sorted((p for p in dropped if _kind_of(p)), key=lambda p: order.index(_kind_of(p)))
        for path in known:
            self.set_file(_kind_of(path), path)
        skipped = [p.name for p in dropped if not _kind_of(p)]
        if skipped:
            self._show_status(f"알 수 없는 파일 형식이라 건너뛰었습니다: {', '.join(skipped)}")
        event.acceptProposedAction()

    # ---- state --------------------------------------------------------------

    def _result_duration(self) -> float:
        if self.video_info is None:
            return 0.0
        return render.result_duration(self.current_job(Path()), self.video_info, self.audio_info)

    def _refresh(self) -> None:
        """Bring labels, enabled states and the preview in line with the files and settings."""
        for kind, (_, _, hint) in FILE_KINDS.items():
            self.file_infos[kind].setText(self._file_info(kind) or hint)

        has_audio = self.audio_info is not None
        fits_audio = has_audio and self.fit_combo.currentData() == "audio"
        self.fit_combo.setEnabled(has_audio)
        self.extend_combo.setEnabled(self.video_info is None or self._result_duration() > self.video_info.duration)
        self.tail_spin.setEnabled(fits_audio)
        self.delay_spin.setEnabled(has_audio)
        self.voice_slider.setEnabled(has_audio)
        self.original_slider.setEnabled(self.video_info is None or self.video_info.has_audio)

        burn = self.mode_combo.currentData() == "burn"
        for w in self.style_widgets:
            w.setEnabled(burn)
        box = self.box_check.isChecked()
        self.outline_color_btn.setEnabled(burn and not box)
        self.outline_spin.setEnabled(burn and not box)
        self.box_color_btn.setEnabled(burn and box)
        self.box_opacity_spin.setEnabled(burn and box)

        self.summary_label.setText(self._summary())
        self._on_position_changed()
        self._set_busy(self.render_thread is not None)
        self.preview_timer.start()

    def _file_info(self, kind: str) -> str:
        if kind == "video" and self.video_info:
            return self.video_info.describe()
        if kind == "audio" and self.audio_info:
            return f"길이 {format_time(self.audio_info.duration, precise=True)}"
        if kind == "subtitle" and self.files["subtitle"]:
            if not self.cues:
                return "자막 줄이 없습니다"
            return (
                f"자막 {len(self.cues)}줄 · {format_time(self.cues[0].start, precise=True)}"
                f" ~ {format_time(max(c.end for c in self.cues), precise=True)}"
            )
        return ""

    def _summary(self) -> str:
        video = self.video_info
        if video is None:
            return "영상을 고르면 결과 길이를 알려 드립니다."
        job = self.current_job(Path())
        duration = self._result_duration()
        lines = [f"결과 길이 {format_time(duration, precise=True)}"]
        gap = duration - video.duration
        if gap > 0.05:
            how = "마지막 장면에서 멈춥니다" if job.extend == "freeze" else "처음부터 반복합니다"
            lines.append(f"영상이 {gap:.1f}초 짧아 {how}.")
        elif gap < -0.05:
            lines.append(f"영상 끝 {-gap:.1f}초는 잘립니다.")
        if self.cues and max(c.end for c in self.cues) + job.delay > duration + 0.05:
            lines.append("자막 일부가 결과보다 늦게 끝나 잘립니다.")
        if self.audio_info is None and not (video.has_audio and job.original_volume > 0):
            lines.append("결과에 소리가 없습니다.")
        return "\n".join(lines)

    def _set_busy(self, busy: bool) -> None:
        self.make_btn.setEnabled(not busy and self.video_info is not None)
        self.cancel_btn.setEnabled(busy)

    # ---- preview ------------------------------------------------------------

    def _preview_time(self) -> float:
        return self.position_slider.value() / 1000 * self._result_duration()

    def _on_position_changed(self) -> None:
        total = self._result_duration()
        self.time_label.setText(
            f"{format_time(self._preview_time(), precise=True)} / {format_time(total, precise=True)}"
        )
        if not self.position_slider.isSliderDown():
            self.preview_timer.start()

    def _source_time(self, t: float) -> float:
        """The moment of the original video that appears at `t` in the result."""
        info = self.video_info
        if t <= info.video_duration:
            return t
        if self.extend_combo.currentData() == "loop" and info.duration > 0:
            return min(t % info.duration, info.video_duration)
        return info.video_duration

    def _cue_at(self, t: float) -> tuple[subs.Cue | None, bool]:
        """The subtitle showing at `t`, or else the nearest one (so the style can still be seen)."""
        if not self.cues:
            return None, False
        delay = self.delay_spin.value() if self.audio_info else 0.0  # it only moves subtitles along with a voice
        for cue in self.cues:
            if cue.start + delay <= t < cue.end + delay:
                return cue, True
        nearest = min(self.cues, key=lambda c: min(abs(c.start + delay - t), abs(c.end + delay - t)))
        return nearest, False

    def _start_preview(self) -> None:
        if self.video_info is None:
            self.screen.show_message("영상을 고르면 여기에 미리보기가 나옵니다\n파일을 창에 끌어다 놓아도 됩니다")
            self.preview_note.setText("")
            return
        if self.preview_thread is not None:
            self._preview_pending = True
            return
        t = self._preview_time()
        burn = self.mode_combo.currentData() == "burn"
        cue, active = self._cue_at(t) if burn else (None, False)
        if self.cues and not burn:
            note = "켜고 끄는 자막은 재생 프로그램이 그리므로 미리보기에는 나오지 않습니다."
        elif cue and not active:
            note = "이 장면에는 자막이 없어서 가장 가까운 자막으로 모양을 보여 드립니다."
        else:
            note = ""
        self.preview_note.setText(note)
        thread = PreviewThread(self.video_info, self._source_time(t), cue.text if cue else None, self.current_style(), self)
        thread.ready.connect(self._preview_ready)
        thread.failed.connect(lambda message: self.preview_note.setText(f"미리보기를 만들지 못했습니다: {message}"))
        thread.finished.connect(self._preview_finished)
        self.preview_thread = thread
        thread.start()

    def _preview_ready(self, bmp: bytes) -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(bmp, "BMP")
        self.screen.show_frame(pixmap)

    def _preview_finished(self) -> None:
        self.preview_thread.deleteLater()
        self.preview_thread = None
        if self._preview_pending:
            self._preview_pending = False
            self._start_preview()

    # ---- rendering ----------------------------------------------------------

    def make_video(self) -> None:
        if self.render_thread is not None or self.files["video"] is None:
            return
        if self.files["audio"] is None and self.files["subtitle"] is None:
            QMessageBox.information(self, APP_TITLE, "영상에 입힐 음성이나 자막을 하나 이상 고르세요.")
            return
        default = render.default_output(self.files["video"], self.files["audio"])
        path, _ = QFileDialog.getSaveFileName(self, "영상 저장", str(default), "MP4 영상 (*.mp4)")
        if not path:
            return
        output = Path(path)
        if output.suffix.lower() != ".mp4":
            output = output.with_name(output.name + ".mp4")

        thread = RenderThread(self.current_job(output), self)
        thread.progress.connect(self._on_progress)
        thread.done.connect(self._on_done)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_thread_finished)
        self.render_thread = thread
        self.progress.setValue(0)
        self._render_started = time.perf_counter()
        self._show_status("영상을 만드는 중…")
        self._set_busy(True)
        thread.start()

    def cancel_render(self) -> None:
        if self.render_thread is not None:
            self.render_thread.cancel()
            self._show_status("취소하는 중…")

    def _on_progress(self, fraction: float) -> None:
        self.progress.setValue(round(fraction * 1000))
        text = f"영상을 만드는 중… {fraction * 100:.0f}%"
        if fraction > 0.03:
            elapsed = time.perf_counter() - self._render_started
            text += f" · 남은 시간 약 {format_time(elapsed * (1 - fraction) / fraction)}"
        self._show_status(text)

    def _on_done(self, plan: render.Plan) -> None:
        thread = self.render_thread
        output = thread.job.output
        self._show_status(
            f"완성: {output} ({format_time(plan.duration, precise=True)} 분량, {thread.elapsed:.1f}초 걸림)"
        )
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setText(f"영상을 만들었습니다.\n\n{output}")
        play_btn = box.addButton("재생", QMessageBox.ButtonRole.ActionRole)
        folder_btn = box.addButton("폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton("확인", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is play_btn:
            os.startfile(output)
        elif box.clickedButton() is folder_btn:
            subprocess.Popen(["explorer", f"/select,{output}"])

    def _on_failed(self, message: str) -> None:
        self.progress.setValue(0)
        self._show_status("영상을 만들지 못했습니다.")
        QMessageBox.warning(self, APP_TITLE, f"영상을 만들지 못했습니다.\n\n{message}")

    def _on_cancelled(self) -> None:
        self.progress.setValue(0)
        self._show_status("취소했습니다.")

    def _on_thread_finished(self) -> None:
        self.render_thread.deleteLater()
        self.render_thread = None
        self._set_busy(False)

    # ---- misc ---------------------------------------------------------------

    def _show_status(self, message: str) -> None:
        self.status.setText(message)
        self.status.setToolTip(message)

    def shutdown(self) -> None:
        """Called by the main window as it closes."""
        if self.render_thread is not None:
            self.render_thread.cancel()
            self.render_thread.wait(10_000)
        if self.preview_thread is not None:
            self.preview_thread.wait(5_000)
        self._save_store()
