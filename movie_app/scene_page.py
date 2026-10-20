"""The video tab: scenes (images, clips, title cards) timed to the voice and made into one video."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QFontDatabase, QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import canvas, media, paths, scene_render, subs
from .media import format_time
from .scenes import (
    DEFAULT_DURATION,
    MAX_SPEED,
    MIN_CLIP_PART,
    MIN_DURATION,
    MIN_SPEED,
    TAIL,
    Project,
    Scene,
    TextStyle,
    clip_part,
    clip_speed,
    sentence_spans,
    time_to_voice,
)
from .recorder import ScreenRecorder
from .timeline import HEIGHT as TIMELINE_HEIGHT
from .timeline import Timeline, TimelineHeader
from .trim import STRIP_FRAMES, StripThread
from .widgets import APP_TITLE, ColorButton, Screen, log_error, select

RESOLUTIONS = [
    ("16:9", "가로 16:9 · 1920×1080"),
    ("4K", "가로 16:9 · 4K 3840×2160 (느림)"),
    ("720p", "가로 16:9 · 1280×720 (빠름)"),
    ("9:16", "세로 9:16 · 쇼츠 1080×1920"),
    ("4K-9:16", "세로 9:16 · 4K 2160×3840 (느림)"),
    ("1:1", "정사각 1:1 · 1080×1080"),
]
TRANSITIONS = [
    ("fade", "부드럽게 겹치기"),
    ("fadeblack", "검은 화면 거치기"),
    ("slide", "밀어내기"),
    ("circle", "원형으로 열기"),
    ("none", "없음 (바로 넘김)"),
]
KINDS = [("image", "이미지"), ("clip", "영상 클립"), ("color", "글자·색 화면")]
FITS = [("cover", "꽉 채우기 (가장자리 잘림)"), ("contain", "전체 보이기 (흐린 배경)")]
MOTIONS = [
    ("zoom_in", "천천히 확대"),
    ("zoom_out", "천천히 축소"),
    ("pan_right", "왼쪽 → 오른쪽"),
    ("pan_left", "오른쪽 → 왼쪽"),
    ("none", "움직임 없음"),
]
TEXT_POSITIONS = [("center", "가운데"), ("top", "위"), ("bottom", "아래")]
CLIP_FILLS = [
    ("freeze", "마지막 화면에서 멈춤"),
    ("loop", "처음부터 반복"),
    ("stretch", "속도를 바꿔 장면에 꽉 맞춤"),
]
FIT_TOLERANCE = 1.43  # a picture this much wider or taller than the frame is shown whole instead of cropped
THUMB_SIZE = QSize(256, 144)  # largest thumbnail drawn for the timeline, before fitting the frame's shape
MAX_THUMBS = 300  # past this, thumbnails of looks no scene has any more are dropped
AUTOSAVE = paths.WORK_DIR / "마지막 장면.json"
PROJECT_FILTER = "장면 프로젝트 (*.json)"
IMAGE_FILTER = f"이미지 ({' '.join('*' + s for s in canvas.IMAGE_SUFFIXES)})"
VIDEO_FILTER = f"영상 ({' '.join('*' + s for s in media.VIDEO_SUFFIXES)})"


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in canvas.IMAGE_SUFFIXES:
        return "image"
    if suffix in media.VIDEO_SUFFIXES:
        return "clip"
    return None


def _scene_frame(scene: Scene, width: int, height: int, at: float) -> QImage:
    """The scene `at` seconds in, drawn exactly as the render will draw it."""
    if scene.kind == "clip" and scene.source:
        return scene_render.clip_frame(scene, width, height, at)
    return canvas.render_frame(scene, width, height, at / scene.duration if scene.duration else 0.0)


class ThumbThread(QThread):
    ready = Signal(str, object)  # key, QImage or None

    def __init__(self, jobs: list[tuple[str, Scene]], size: tuple[int, int], parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.size = size

    def run(self):
        for key, scene in self.jobs:
            if self.isInterruptionRequested():
                return
            try:
                image = _scene_frame(scene, *self.size, 0.0)
            except Exception:  # noqa: BLE001 - a missing file just gets a blank thumbnail
                image = None
            self.ready.emit(key, image)


class PreviewThread(QThread):
    """Draws the preview at the video's full size; at 4K that takes too long for the window's own thread."""

    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, scene: Scene, size: tuple[int, int], at: float, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.size = size
        self.at = at

    def run(self):
        try:
            self.ready.emit(_scene_frame(self.scene, *self.size, self.at))
        except Exception as e:  # noqa: BLE001 - shown in the preview
            self.failed.emit(str(e))


class RenderThread(QThread):
    progress = Signal(float)
    done = Signal(float)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, project: Project, output: Path, purpose: str, voice: Path | None = None, parent=None):
        super().__init__(parent)
        self.project = Project.from_json(project.to_json())  # edits made while rendering do not leak in
        self.output = output
        self.purpose = purpose  # "send" to the compose tab, "save" to a file, or "preview" to play here
        self.voice = voice
        self.key = ""  # what the preview was made from; set by the page
        self.elapsed = 0.0
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        start = time.perf_counter()
        try:
            if self.purpose == "preview":
                length = scene_render.preview(
                    self.project, self.output, self.voice, self.progress.emit, lambda: self._cancel
                )
            else:
                length = scene_render.render(self.project, self.output, self.progress.emit, lambda: self._cancel)
        except scene_render.Cancelled:
            self.cancelled.emit()
        except (OSError, ValueError, RuntimeError) as e:
            log_error(traceback.format_exc())
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user
            log_error(traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.elapsed = time.perf_counter() - start
            self.done.emit(length)


class ScenePage(QWidget):
    videoReady = Signal(object)  # Path of a finished video for the compose tab

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.store.beginGroup("scenes")  # the other tabs keep their settings in the same file
        self.project = Project()
        self.current = -1
        self.playhead = 0.0  # the moment shown in the preview, in seconds into the video
        self.voice_cues: list[subs.Cue] = []
        self.voice_duration = 0.0
        self.thumbs: dict[str, QPixmap] = {}
        self._thumb_queue: dict[str, Scene] = {}
        self.thumb_thread: ThumbThread | None = None
        self.preview_thread: PreviewThread | None = None
        self._preview_pending = False
        self.render_thread: RenderThread | None = None
        self._render_started = 0.0
        self._preview_file: Path | None = None  # the last preview video made
        self._preview_made = ""  # what it was made from (see _preview_key)
        self._preview_count = 0
        self._pending_start: int | None = None  # ms to start from once a newly set preview file has loaded
        self._scrub: float | None = None  # clip time under a trim handle being dragged
        self._strips: dict[str, list[QPixmap]] = {}  # clip filmstrips for the timeline, by _strip_key
        self._strip_queue: dict[str, float] = {}  # clips still to make a filmstrip of -> length
        self._strips_making: set[str] = set()  # _strip_key of the clips the running strip thread is on
        self.strip_thread: StripThread | None = None
        self.recorder: ScreenRecorder | None = None
        self._was_maximized = False
        self._loading = False  # true while the settings panel is being filled from a scene
        self._clip_lengths: dict[str, float] = {}

        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(120)
        self.preview_timer.timeout.connect(self._start_preview)
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.setInterval(300)
        self.thumb_timer.timeout.connect(self._start_thumbs)

        self._build_ui()
        self._connect_changes()
        for keys, slot in (
            ("Ctrl+O", self.add_images),
            ("Ctrl+S", self.save_project),
            ("Ctrl+Return", self.send_to_compose),
        ):
            QShortcut(QKeySequence(keys), self).activated.connect(slot)
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.timeline)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self.delete_scene)

        self._restore_autosave()
        self._set_busy(False)
        self._show_status("[음성 만들기] 탭에서 음성을 보내면 문장마다 장면이 생깁니다. 이미지나 영상을 끌어다 놓아도 됩니다.")

    # ---- layout -------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)
        root.addWidget(self._build_top_box())

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_preview_panel(), 1)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 4, 0)
        side_layout.addWidget(self._build_props_box())
        side_layout.addStretch(1)
        # The clip settings make the panel tall; it scrolls rather than squeezing on a small screen.
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setFixedWidth(356)
        body.addWidget(scroll)
        root.addLayout(body, 1)
        root.addWidget(self._build_timeline_panel())
        root.addWidget(self._build_action_row())

    def _build_top_box(self) -> QGroupBox:
        box = QGroupBox("영상 설정")
        col = QVBoxLayout(box)
        col.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.res_combo = self._combo(RESOLUTIONS)
        self.trans_combo = self._combo(TRANSITIONS)
        self.trans_spin = QDoubleSpinBox()
        self.trans_spin.setRange(0.1, 3.0)
        self.trans_spin.setSingleStep(0.1)
        self.trans_spin.setDecimals(1)
        self.trans_spin.setSuffix(" 초")
        self.trans_spin.setToolTip("장면이 바뀌는 데 걸리는 시간. 새 장면은 그 문장이 시작될 때부터 나타납니다.")
        project_btn = QToolButton()
        project_btn.setText("프로젝트 ▾")
        project_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(project_btn)
        menu.addAction("새로 만들기", self.new_project)
        menu.addAction("열기…", self.open_project)
        menu.addAction("다른 이름으로 저장… (Ctrl+S)", self.save_project)
        project_btn.setMenu(menu)
        for w in (QLabel("화면"), self.res_combo, QLabel("   장면 전환"), self.trans_combo, self.trans_spin):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(project_btn)
        col.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.voice_label = QLabel()
        self.voice_label.setObjectName("muted")
        self.voice_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        voice_btn = QPushButton("음성 고르기…")
        voice_btn.setToolTip("문장에 맞출 음성을 고릅니다. 같은 이름의 자막(SRT)이 옆에 있어야 합니다.")
        voice_btn.clicked.connect(self.choose_voice)
        self.lines_spin = QSpinBox()
        self.lines_spin.setRange(1, 20)
        self.lines_spin.setPrefix("장면마다 ")
        self.lines_spin.setSuffix(" 문장")
        self.fit_btn = QPushButton("음성에 맞추기")
        self.fit_btn.setToolTip(
            "문장(또는 정한 문장 수)마다 장면 하나가 되도록 길이를 맞춥니다.\n"
            "이미 넣은 그림은 순서대로 유지하고, 모자라면 빈 장면을 더합니다."
        )
        self.fit_btn.clicked.connect(self.fit_to_voice)
        row.addWidget(QLabel("음성"))
        row.addWidget(self.voice_label, 1)
        row.addWidget(voice_btn)
        row.addWidget(self.lines_spin)
        row.addWidget(self.fit_btn)
        col.addLayout(row)
        return box

    def _build_timeline_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        col = QVBoxLayout(panel)
        col.setContentsMargins(10, 8, 10, 6)
        col.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        for text, tip, slot in (
            ("+ 이미지", "이미지를 빈 장면에 차례로 넣고, 남으면 뒤에 붙입니다 (Ctrl+O)", self.add_images),
            ("+ 영상", "영상 클립을 빈 장면에 차례로 넣고, 남으면 뒤에 붙입니다", self.add_clips),
            ("+ 글자 화면", "제목이나 안내 글자를 띄울 장면을 지금 장면 뒤에 넣습니다", self.add_title),
            ("● 화면 녹화", "화면의 원하는 영역(또는 모니터 전체)을 녹화해서 장면으로 넣습니다", self.record_screen),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addSpacing(10)
        self.up_btn = QPushButton("◀")
        self.up_btn.setToolTip("지금 장면을 앞으로 (타임라인에서 끌어도 됩니다)")
        self.up_btn.clicked.connect(lambda: self.move_scene(-1))
        self.down_btn = QPushButton("▶")
        self.down_btn.setToolTip("지금 장면을 뒤로")
        self.down_btn.clicked.connect(lambda: self.move_scene(1))
        self.copy_btn = QPushButton("복제")
        self.copy_btn.clicked.connect(self.duplicate_scene)
        self.delete_btn = QPushButton("삭제")
        self.delete_btn.setToolTip("지금 장면을 지웁니다 (Delete)")
        self.delete_btn.clicked.connect(self.delete_scene)
        for b in (self.up_btn, self.down_btn, self.copy_btn, self.delete_btn):
            bar.addWidget(b)
        bar.addSpacing(10)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("muted")
        self.summary_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.summary_label, 1)
        for text, tip, slot in (
            ("－", "축소 (Ctrl+휠)", lambda: self.timeline.zoom_by(0.67)),
            ("맞춤", "전체가 한눈에 들어오게", lambda: self.timeline.fit()),
            ("＋", "확대 (Ctrl+휠)", lambda: self.timeline.zoom_by(1.5)),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setStyleSheet("padding: 6px 10px;")
            button.clicked.connect(slot)
            bar.addWidget(button)
        col.addLayout(bar)

        row = QHBoxLayout()
        row.setSpacing(0)
        row.addWidget(TimelineHeader(), 0, Qt.AlignmentFlag.AlignTop)
        self.timeline = Timeline()
        self.timeline.thumb = lambda scene: self.thumbs.get(self._thumb_key(scene))
        self.timeline.strip = lambda source: self._strips.get(self._strip_key(source))
        self.timeline.clip_length = self._clip_length
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidget(self.timeline)
        self.timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.timeline_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.timeline_scroll.setFixedHeight(TIMELINE_HEIGHT + self.timeline_scroll.horizontalScrollBar().sizeHint().height())
        self.timeline.hbar = self.timeline_scroll.horizontalScrollBar()
        self._timeline_view = self.timeline_scroll.viewport()
        self._timeline_view.installEventFilter(self)  # follows the panel's width (see eventFilter)
        row.addWidget(self.timeline_scroll, 1)
        col.addLayout(row)
        return panel

    def eventFilter(self, obj, event):
        # Compared by reference: asking the scroll area for its viewport fails while the window is torn down.
        if obj is self._timeline_view and event.type() == QEvent.Type.Resize:
            self.timeline.view_width = obj.width()
            if self.timeline.fit_mode:
                self.timeline.fit()
            else:
                self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead)
        return super().eventFilter(obj, event)

    def _build_preview_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        # A still frame of the chosen scene, or, while a preview plays, its frames as they come.
        self.screen = Screen()
        layout.addWidget(self.screen, 1)
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        self.player.setVideoSink(self.video_sink)
        self.player.positionChanged.connect(self._on_player_position)
        self.player.durationChanged.connect(lambda ms: self.seek_slider.setRange(0, ms))
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(lambda _error, message: self._show_status(f"재생할 수 없습니다: {message}"))

        self.still_bar = QWidget()
        row = QHBoxLayout(self.still_bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel()
        self.time_label.setToolTip("아래 타임라인의 눈금을 누르거나 끌어서 보고 싶은 순간을 고릅니다")
        full_btn = QPushButton("원본 크기로 보기")
        full_btn.setToolTip("이 순간을 확대 없이 실제 픽셀 그대로 봅니다 (미리보기를 두 번 클릭해도 됩니다)")
        full_btn.clicked.connect(self.screen.open_full_size)
        self.play_preview_btn = QPushButton("▶ 재생")
        self.play_preview_btn.setObjectName("primary")
        self.play_preview_btn.setToolTip(
            "타임라인의 빨간 선 위치부터 음성·자막과 함께 재생합니다.\n"
            "빨리 만들 수 있도록 작게(가로 960) 만들고, 한 번 만든 장면은 다시 만들지 않습니다."
        )
        self.play_preview_btn.clicked.connect(self.play_preview)
        row.addWidget(self.time_label, 1)
        row.addWidget(full_btn)
        row.addWidget(self.play_preview_btn)
        layout.addWidget(self.still_bar)

        self.play_bar = QWidget()
        row = QHBoxLayout(self.play_bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.pause_btn = QPushButton("❚❚")
        self.pause_btn.setFixedWidth(52)
        self.pause_btn.clicked.connect(self.toggle_playback)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.play_time_label = QLabel()
        self.play_time_label.setMinimumWidth(130)
        self.play_time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        close_btn = QPushButton("■ 멈추고 장면 보기")
        close_btn.setToolTip("재생을 멈추고, 멈춘 곳의 장면을 고릅니다")
        close_btn.clicked.connect(self.close_player)
        row.addWidget(self.pause_btn)
        row.addWidget(self.seek_slider, 1)
        row.addWidget(self.play_time_label)
        row.addWidget(close_btn)
        self.play_bar.setVisible(False)
        layout.addWidget(self.play_bar)

        self.narration_label = QLabel()
        self.narration_label.setObjectName("muted")
        self.narration_label.setWordWrap(True)
        layout.addWidget(self.narration_label)
        return panel

    def _build_props_box(self) -> QGroupBox:
        box = QGroupBox("장면 설정")
        form = QFormLayout(box)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)
        self.form = form

        self.kind_combo = self._combo(KINDS)
        form.addRow("종류", self.kind_combo)

        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("파일을 고르세요")
        browse = QPushButton("찾기…")
        browse.clicked.connect(self.browse_source)
        self.file_row = self._row(self.file_edit, browse)
        form.addRow("파일", self.file_row)

        self.clip_start_spin = self._seconds(0.0, 36000.0)
        self.clip_start_spin.setToolTip("클립의 몇 초부터 쓸지 (타임라인의 클립 구간 손잡이를 끌어도 됩니다)")
        self.clip_end_spin = self._seconds(0.0, 36000.0)
        self.clip_end_spin.setToolTip("클립의 몇 초까지 쓸지")
        whole_btn = QPushButton("전체")
        whole_btn.setToolTip("클립 전체를 씁니다")
        whole_btn.clicked.connect(self._use_whole_clip)
        self.clip_row = self._row(self.clip_start_spin, QLabel("~"), self.clip_end_spin, whole_btn)
        form.addRow("구간", self.clip_row)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSuffix(" 배속")
        self.speed_spin.setToolTip("0.5배속은 느리게 2배 길이로, 2배속은 빠르게 절반 길이로 재생합니다")
        speed_row = [self.speed_spin]
        for value in (0.5, 1.0, 2.0):
            button = QPushButton(f"{value:g}×")
            button.setFixedWidth(46)
            button.setStyleSheet("padding: 6px 2px;")  # the usual side padding leaves no room for the text
            button.setToolTip(f"{value:g}배속")
            button.clicked.connect(lambda _=False, v=value: self.speed_spin.setValue(v))
            speed_row.append(button)
        self.speed_row = self._row(*speed_row)
        form.addRow("속도", self.speed_row)
        self.clip_fill_combo = self._combo(CLIP_FILLS)
        self.clip_fill_combo.setMinimumWidth(10)  # let it shrink to the panel; the labels are long
        self.clip_fill_combo.setToolTip("클립이 장면보다 짧을 때 어떻게 할지")
        form.addRow("짧으면", self.clip_fill_combo)
        self.clip_info_label = QLabel()
        self.clip_info_label.setObjectName("muted")
        self.clip_info_label.setWordWrap(True)
        form.addRow(self.clip_info_label)
        fit_length_btn = QPushButton("장면 길이를 클립에 맞춤")
        fit_length_btn.setToolTip("쓰는 구간을 지금 속도로 재생한 길이만큼 장면 길이를 바꿉니다")
        fit_length_btn.clicked.connect(self._fit_scene_to_clip)
        self.clip_audio_check = QCheckBox("클립 소리 넣기")
        self.clip_audio_check.setToolTip("속도를 바꿔도 음 높이는 그대로입니다. 크기는 합성 탭의 [원본 소리]로 정합니다.")
        self.clip_tools_row = self._row(fit_length_btn, self.clip_audio_check)
        form.addRow(self.clip_tools_row)

        self.fit_combo = self._combo(FITS)
        form.addRow("맞춤", self.fit_combo)
        self.motion_combo = self._combo(MOTIONS)
        form.addRow("움직임", self.motion_combo)

        self.color_btn = ColorButton("배경색")
        self.gradient_check = QCheckBox("그라데이션")
        self.color2_btn = ColorButton("아래쪽 색")
        self.color_row = self._row(self.color_btn, self.gradient_check, self.color2_btn)
        form.addRow("배경", self.color_row)

        self.duration_spin = self._seconds(MIN_DURATION, 24 * 3600.0)
        self.duration_spin.setToolTip(
            "타임라인에서 장면 오른쪽 끝을 끌어도 바뀝니다 (음성 문장 경계에 붙음, Shift를 누르면 자유롭게).\n"
            "[음성에 맞추기]를 누르면 문장 길이에 맞춰 다시 정해집니다."
        )
        form.addRow("길이", self.duration_spin)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("화면에 띄울 글자 (비워 두면 없음)")
        self.text_edit.setFixedHeight(80)
        form.addRow("화면 글자", self.text_edit)
        self.font_combo = QFontComboBox()
        self.font_combo.setWritingSystem(QFontDatabase.WritingSystem.Korean)
        self.font_combo.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
        self.font_combo.setMaxVisibleItems(20)
        form.addRow("글꼴", self.font_combo)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 300)
        self.size_spin.setToolTip("1080p 기준 크기입니다. 세로 영상에서도 같은 비율로 보입니다.")
        self.bold_check = QCheckBox("굵게")
        self.text_color_btn = ColorButton("글자색")
        form.addRow("크기", self._row(self.size_spin, self.bold_check, self.text_color_btn))
        self.text_pos_combo = self._combo(TEXT_POSITIONS)
        form.addRow("글자 위치", self.text_pos_combo)

        apply_btn = QPushButton("이 모양을 모든 장면에 적용")
        apply_btn.setToolTip("맞춤·움직임과 글자 모양(글꼴, 크기, 색, 위치)을 모든 장면에 똑같이 적용합니다. 글자 내용은 그대로 둡니다.")
        apply_btn.clicked.connect(self.apply_to_all)
        form.addRow("", apply_btn)
        self.props_box = box
        return box

    def _build_action_row(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.save_btn = QPushButton("파일로 저장…")
        self.save_btn.setToolTip("음성·자막 없이 장면만 이어 붙인 영상을 저장합니다")
        self.save_btn.clicked.connect(self.save_to_file)
        self.send_btn = QPushButton("합성으로 →")
        self.send_btn.setObjectName("primary")
        self.send_btn.setToolTip("장면을 영상으로 만들어 [영상-음성-자막 합성하기] 탭으로 보냅니다 (Ctrl+Enter)")
        self.send_btn.clicked.connect(self.send_to_compose)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_render)
        for b in (self.save_btn, self.send_btn, self.cancel_btn):
            row.addWidget(b)
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

    @staticmethod
    def _combo(items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        for code, label in items:
            combo.addItem(label, code)
        return combo

    @staticmethod
    def _seconds(lo: float, hi: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(0.1)
        spin.setDecimals(2)
        spin.setSuffix(" 초")
        return spin

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        return w

    def _connect_changes(self) -> None:
        self.timeline.sceneClicked.connect(self._on_timeline_clicked)
        self.timeline.seeked.connect(self._on_timeline_seeked)
        self.timeline.lengthDragged.connect(self._on_timeline_length)
        self.timeline.trimDragged.connect(self._on_timeline_trim)
        self.timeline.trimScrubbed.connect(self._on_timeline_scrub)
        self.timeline.sceneMoved.connect(self._move)
        self.timeline.filesDropped.connect(self._place_files)
        for combo in (self.res_combo, self.trans_combo):
            combo.currentIndexChanged.connect(self._project_settings_changed)
        self.trans_spin.valueChanged.connect(self._project_settings_changed)
        self.lines_spin.valueChanged.connect(self._project_settings_changed)
        for combo in (self.kind_combo, self.fit_combo, self.motion_combo, self.text_pos_combo):
            combo.currentIndexChanged.connect(self._props_changed)
        for spin in (self.clip_start_spin, self.clip_end_spin, self.speed_spin, self.duration_spin, self.size_spin):
            spin.valueChanged.connect(self._props_changed)
        self.clip_fill_combo.currentIndexChanged.connect(self._props_changed)
        for check in (self.clip_audio_check, self.gradient_check, self.bold_check):
            check.toggled.connect(self._props_changed)
        for button in (self.color_btn, self.color2_btn, self.text_color_btn):
            button.colorChanged.connect(self._props_changed)
        self.text_edit.textChanged.connect(self._props_changed)
        self.font_combo.currentFontChanged.connect(self._props_changed)

    # ---- project & list -----------------------------------------------------

    def _apply_project(self, project: Project) -> None:
        self._stop_player()  # a preview of the old project must not keep playing over the new one
        self.project = project
        self._loading = True
        select(self.res_combo, project.resolution)
        select(self.trans_combo, project.transition)
        self.trans_spin.setValue(project.transition_duration)
        self.lines_spin.setValue(project.lines_per_scene)
        self._loading = False
        self.trans_spin.setEnabled(project.transition != "none")
        self.voice_cues, self.voice_duration = [], 0.0
        if project.voice and Path(project.voice).is_file():
            self._load_voice(Path(project.voice), quiet=True)
        self._update_voice_label()
        self.playhead = 0.0
        self.timeline.fit_mode = True  # a new project starts zoomed to fit
        self._reload_scenes(0 if project.scenes else -1)

    def _project_settings_changed(self) -> None:
        if self._loading:
            return
        resolution = self.res_combo.currentData()
        changed_size = resolution != self.project.resolution
        self.project.resolution = resolution
        self.project.transition = self.trans_combo.currentData()
        self.project.transition_duration = self.trans_spin.value()
        self.project.lines_per_scene = self.lines_spin.value()
        self.trans_spin.setEnabled(self.project.transition != "none")
        if changed_size:
            self._refresh_timeline()
        self._update_summary()
        self.preview_timer.start()

    def _thumb_size(self) -> tuple[int, int]:
        w, h = self.project.size
        scale = min(THUMB_SIZE.width() / w, THUMB_SIZE.height() / h)
        return max(2, round(w * scale)), max(2, round(h * scale))

    def _thumb_key(self, scene: Scene) -> str:
        data = dataclasses.asdict(scene)
        # Thumbnails show a scene's first frame, which these do not change.
        for name in ("duration", "narration", "clip_audio", "clip_end", "speed", "clip_fill"):
            data.pop(name)
        return json.dumps([self.project.resolution, data], sort_keys=True, ensure_ascii=False)

    def _reload_scenes(self, row: int) -> None:
        """After scenes were added, removed or reordered: redraw the timeline and select `row`."""
        self._refresh_timeline()
        self._select_scene(min(row, len(self.project.scenes) - 1))

    def _refresh_timeline(self) -> None:
        """Bring the timeline up to date after any change, queueing pictures it does not have yet."""
        for scene in self.project.scenes:
            if scene.kind == "clip" and scene.source:
                key = self._strip_key(scene.source)
                if key not in self._strips and key not in self._strips_making and self._clip_length(scene.source):
                    self._strip_queue[scene.source] = self._clip_length(scene.source)
            elif scene.kind == "image" and scene.source:
                key = self._thumb_key(scene)
                if key not in self.thumbs:
                    self._thumb_queue[key] = scene
        if self._thumb_queue:
            self.thumb_timer.start()
        if self._strip_queue:
            self._start_strips()
        self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead)
        self._update_summary()
        self._set_busy(self.render_thread is not None)

    @staticmethod
    def _strip_key(source: str) -> str:
        try:
            return f"{source}|{os.stat(source).st_mtime_ns}"
        except OSError:
            return source

    def _start_strips(self) -> None:
        if self.strip_thread is not None or not self._strip_queue:
            return
        jobs = list(self._strip_queue.items())
        self._strip_queue.clear()
        self._strips_making = {self._strip_key(source) for source, _ in jobs}
        self.strip_thread = StripThread(jobs, self)
        self.strip_thread.ready.connect(self._strip_ready)
        self.strip_thread.finished.connect(self._strips_finished)
        self.strip_thread.start()

    def _strip_ready(self, source: str, image: QImage | None) -> None:
        # A clip that gives no filmstrip is remembered as such, so it is not tried again on every edit.
        tiles = []
        if image is not None:
            width = image.width() // STRIP_FRAMES
            tiles = [QPixmap.fromImage(image.copy(i * width, 0, width, image.height())) for i in range(STRIP_FRAMES)]
        self._strips[self._strip_key(source)] = tiles
        self.timeline.update()

    def _strips_finished(self) -> None:
        self.strip_thread.deleteLater()
        self.strip_thread = None
        self._strips_making = set()
        if self._strip_queue:
            self._start_strips()

    def _start_thumbs(self) -> None:
        if self.thumb_thread is not None or not self._thumb_queue:
            return
        # Only what the scenes look like now; typing into a scene's text queues every keystroke's look.
        wanted = {self._thumb_key(s) for s in self.project.scenes}
        jobs = [(key, scene) for key, scene in self._thumb_queue.items() if key in wanted]
        self._thumb_queue.clear()
        if not jobs:
            return
        self.thumb_thread = ThumbThread(jobs, self._thumb_size(), self)
        self.thumb_thread.ready.connect(self._thumb_ready)
        self.thumb_thread.finished.connect(self._thumbs_finished)
        self.thumb_thread.start()

    def _thumb_ready(self, key: str, image: QImage | None) -> None:
        if image is None:
            return
        self.thumbs[key] = QPixmap.fromImage(image)
        self.timeline.update()

    def _thumbs_finished(self) -> None:
        self.thumb_thread.deleteLater()
        self.thumb_thread = None
        if len(self.thumbs) > MAX_THUMBS:
            wanted = {self._thumb_key(s) for s in self.project.scenes}
            self.thumbs = {key: pixmap for key, pixmap in self.thumbs.items() if key in wanted}
        if self._thumb_queue:
            self._start_thumbs()

    def _update_summary(self) -> None:
        scenes = self.project.scenes
        if not scenes:
            self.summary_label.setText("장면이 없습니다. 음성을 보내거나 이미지·영상을 추가하세요.")
            return
        parts = [f"장면 {len(scenes)}개 · 영상 {format_time(self.project.duration, precise=True)}"]
        blanks = sum(s.is_blank for s in scenes)
        if blanks:
            parts.append(f"그림 없는 장면 {blanks}개")
        if self.voice_cues:
            target = self.voice_duration + TAIL
            gap = self.project.duration - target
            if abs(gap) > 0.05:
                parts.append(f"음성보다 {abs(gap):.1f}초 {'김' if gap > 0 else '짧음'}")
        self.summary_label.setText(" · ".join(parts))

    def _move(self, source: int, target: int) -> None:
        scenes = self.project.scenes
        if not (0 <= source < len(scenes)) or source == target:
            return
        scenes.insert(target, scenes.pop(source))
        self._reload_scenes(target)

    def move_scene(self, step: int) -> None:
        i = self.current
        if 0 <= i < len(self.project.scenes) and 0 <= i + step < len(self.project.scenes):
            self._move(i, i + step)

    def duplicate_scene(self) -> None:
        if self.current < 0:
            return
        scene = self.project.scenes[self.current]
        self.project.scenes.insert(self.current + 1, dataclasses.replace(scene, style=dataclasses.replace(scene.style)))
        self._reload_scenes(self.current + 1)

    def delete_scene(self) -> None:
        if self.current < 0:
            return
        del self.project.scenes[self.current]
        self._reload_scenes(min(self.current, len(self.project.scenes) - 1))

    def add_title(self) -> None:
        at = self.current + 1 if self.current >= 0 else len(self.project.scenes)
        scene = Scene(kind="color", text="제목", color="#1e3a8a", color2="#0f172a", style=self._current_text_style())
        self.project.scenes.insert(at, scene)
        self._reload_scenes(at)
        self.text_edit.setFocus()
        self.text_edit.selectAll()

    def record_screen(self) -> None:
        """Record part of the screen and put the recording in as a clip scene."""
        if self.recorder is not None:
            return
        width, height = self.project.size
        self.recorder = ScreenRecorder(width / height, self)
        self.recorder.finished.connect(self._recording_done)
        self.recorder.failed.connect(self._recording_failed)
        window = self.window()
        self._was_maximized = window.isMaximized()
        window.showMinimized()  # out of the way of the screen being recorded
        QTimer.singleShot(300, self.recorder.start)  # once the window has gone

    def _restore_window(self) -> None:
        window = self.window()
        if self._was_maximized:
            window.showMaximized()
        else:
            window.showNormal()
        window.raise_()
        window.activateWindow()

    def _recording_failed(self, message: str) -> None:
        self.recorder.deleteLater()
        self.recorder = None
        self._restore_window()
        QMessageBox.warning(self, APP_TITLE, message)

    def _recording_done(self, path: Path | None) -> None:
        self.recorder.deleteLater()
        self.recorder = None
        self._restore_window()
        if path is None:
            self._show_status("화면 녹화를 취소했습니다.")
            return
        scenes = self.project.scenes
        if 0 <= self.current < len(scenes) and scenes[self.current].is_blank:
            self._place_files([path], self.current)  # fills the blank scene, keeping its length and narration
        else:
            at = self.current + 1 if self.current >= 0 else len(scenes)
            scenes.insert(at, self._with_source(Scene(duration=DEFAULT_DURATION, style=self._current_text_style()), path, "clip"))
            self._reload_scenes(at)
        self._show_status(f"녹화한 영상을 장면에 넣었습니다: {path}")

    def _current_text_style(self) -> TextStyle:
        if 0 <= self.current < len(self.project.scenes):
            return dataclasses.replace(self.project.scenes[self.current].style)
        return TextStyle()

    def add_images(self) -> None:
        self._add_files("이미지 추가", IMAGE_FILTER)

    def add_clips(self) -> None:
        self._add_files("영상 클립 추가", VIDEO_FILTER)

    def _add_files(self, title: str, file_filter: str) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, title, str(self.store.value("open_dir", "")), file_filter)
        if names:
            self.store.setValue("open_dir", str(Path(names[0]).parent))
            self._place_files([Path(n) for n in names], -1)

    def _auto_fit(self, path: Path, kind: str) -> str:
        """Fill the frame when the picture has roughly its shape; otherwise show it whole on a blurred copy."""
        width, height = self.project.size
        try:
            if kind == "image":
                image = canvas.load_image(str(path), width, height)
                source_w, source_h = image.width(), image.height()
            else:
                info = media.probe(path)
                source_w, source_h = info.width, info.height
        except (OSError, ValueError):
            return "cover"
        if not source_w or not source_h:
            return "cover"
        ratio = (source_w / source_h) / (width / height)
        return "cover" if 1 / FIT_TOLERANCE <= ratio <= FIT_TOLERANCE else "contain"

    def _with_source(self, scene: Scene, path: Path, kind: str) -> Scene:
        fit = self._auto_fit(path, kind)
        scene = dataclasses.replace(scene, kind=kind, source=str(path), clip_start=0.0, clip_end=0.0, fit=fit)
        if kind == "clip" and not scene.narration:
            length = self._clip_length(str(path))
            if length:
                scene.duration = max(MIN_DURATION, length)
        return scene

    def _place_files(self, files: list[Path], row: int) -> None:
        """Files dropped on a scene replace its picture and then fill the blank scenes after it.

        Otherwise they fill blank scenes from the current one on; whatever is left becomes new scenes.
        """
        skipped = [p.name for p in files if _kind_of(p) is None]
        scenes = self.project.scenes
        first = None
        cursor = row if row >= 0 else max(0, self.current)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            items = []
            for path in files:
                kind = _kind_of(path)
                if kind is None:
                    continue
                if self._can_open(path, kind):
                    items.append((path, kind))
                else:
                    skipped.append(path.name)
            for n, (path, kind) in enumerate(items):
                if n == 0 and 0 <= row < len(scenes):
                    target = row
                else:
                    target = next((i for i in range(cursor, len(scenes)) if scenes[i].is_blank), None)
                if target is None:
                    scenes.append(self._with_source(Scene(duration=DEFAULT_DURATION, style=self._current_text_style()), path, kind))
                    target = len(scenes) - 1
                else:
                    scenes[target] = self._with_source(scenes[target], path, kind)
                first = target if first is None else first
                cursor = target + 1
        finally:
            QApplication.restoreOverrideCursor()
        if first is not None:
            self._reload_scenes(first)
            self._show_status(f"{len(items)}개 파일을 장면에 넣었습니다.")
        if skipped:
            shown = "\n".join(skipped[:10]) + ("\n…" if len(skipped) > 10 else "")
            QMessageBox.warning(
                self, APP_TITLE, f"이미지나 영상으로 열 수 없어서 넣지 않은 파일이 있습니다.\n\n{shown}"
            )

    @staticmethod
    def _can_open(path: Path, kind: str) -> bool:
        """Whether the file really is a picture or a video (".ts" is also TypeScript source, for one)."""
        if kind == "image":
            return QImageReader(str(path)).canRead()
        try:
            info = media.probe(path)
        except (OSError, ValueError):
            return False
        return info.has_video and info.video_duration > 0  # a streamed WebM may not know its length

    def _clip_length(self, source: str) -> float:
        # Keyed by the file's modification time too: a clip exported again, or a drive that comes back
        # online, is probed afresh.
        key = self._strip_key(source)
        if key not in self._clip_lengths:
            try:
                self._clip_lengths[key] = media.probe(source).video_duration  # the picture's own length
            except (OSError, ValueError):
                self._clip_lengths[key] = 0.0
        return self._clip_lengths[key]

    def browse_source(self) -> None:
        if self.current < 0:
            return
        kind = self.kind_combo.currentData()
        filters = [VIDEO_FILTER, IMAGE_FILTER] if kind == "clip" else [IMAGE_FILTER, VIDEO_FILTER]
        name, _ = QFileDialog.getOpenFileName(self, "파일 고르기", str(self.store.value("open_dir", "")), ";;".join(filters))
        if name:
            self.store.setValue("open_dir", str(Path(name).parent))
            self._place_files([Path(name)], self.current)

    def apply_to_all(self) -> None:
        if self.current < 0:
            return
        model = self.project.scenes[self.current]
        for i, scene in enumerate(self.project.scenes):
            self.project.scenes[i] = dataclasses.replace(
                scene,
                fit=model.fit,
                motion=model.motion if scene.kind == "image" and model.kind == "image" else scene.motion,
                style=dataclasses.replace(model.style),
            )
        self._refresh_timeline()
        self._show_status(f"{len(self.project.scenes)}개 장면에 같은 모양을 적용했습니다.")

    # ---- the settings panel -------------------------------------------------

    def _select_scene(self, row: int, at: float | None = None) -> None:
        """Make `row` the scene being edited; the playhead goes to `at`, or to the scene's start."""
        self.current = row
        has = 0 <= row < len(self.project.scenes)
        self.props_box.setEnabled(has)
        for b in (self.up_btn, self.down_btn, self.copy_btn, self.delete_btn):
            b.setEnabled(has)
        if has:
            self._load_props(self.project.scenes[row])
            self.playhead = self._scene_start(row) if at is None else at
        else:
            self.playhead = 0.0
        if has and self.play_bar.isVisible():
            self.player.setPosition(round(self.playhead * 1000))  # jump the playing preview there
        self.timeline.current = row
        self._on_playhead_changed()

    # ---- the timeline -------------------------------------------------------

    def _on_timeline_clicked(self, index: int, at: float) -> None:
        if index != self.current:
            self._select_scene(index, at)
        else:
            self._seek(at)

    def _on_timeline_seeked(self, at: float) -> None:
        index = self._scene_at(at) if self.project.scenes else -1
        if index != self.current:
            self._select_scene(index, at)
        else:
            self._seek(at)

    def _seek(self, at: float) -> None:
        self.playhead = at
        if self.play_bar.isVisible():
            self.player.setPosition(round(at * 1000))
        self._on_playhead_changed()

    def _on_timeline_length(self, index: int, duration: float, finished: bool) -> None:
        scenes = self.project.scenes
        scenes[index] = dataclasses.replace(scenes[index], duration=duration)
        if index == self.current:
            self._loading = True
            self.duration_spin.setValue(duration)
            self._loading = False
            self._update_props_visibility(scenes[index])
        self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead)
        self._update_summary()
        if finished:
            self._refresh_timeline()
            self._on_playhead_changed()

    def _on_timeline_trim(self, index: int, start: float, end: float, finished: bool) -> None:
        scenes = self.project.scenes
        scenes[index] = dataclasses.replace(scenes[index], clip_start=start, clip_end=end)
        if index != self.current:
            self._select_scene(index, self.playhead)
        self._loading = True
        self.clip_start_spin.setValue(start)
        self.clip_end_spin.setValue(end)
        self._loading = False
        self._update_props_visibility(scenes[index])
        self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead)
        if finished:
            self._scrub = None
            self._refresh_timeline()
            self._seek(self._scene_start(index))  # show where the part now begins

    def _on_timeline_scrub(self, index: int, moment: float) -> None:
        self._scrub = moment  # the preview shows this moment of the clip while a handle is dragged
        self.preview_timer.start()

    def _load_props(self, scene: Scene) -> None:
        self._loading = True
        select(self.kind_combo, scene.kind)
        self.file_edit.setText(Path(scene.source).name if scene.source else "")
        self.file_edit.setToolTip(scene.source)
        length = self._clip_length(scene.source) if scene.kind == "clip" and scene.source else 0.0
        start, end = clip_part(scene, length) if length else (scene.clip_start, scene.clip_end)
        for spin in (self.clip_start_spin, self.clip_end_spin):
            spin.setMaximum(length or 36000.0)
        self.clip_start_spin.setValue(start)
        self.clip_end_spin.setValue(end)
        self.speed_spin.setValue(scene.speed)
        select(self.clip_fill_combo, scene.clip_fill)
        self.clip_audio_check.setChecked(scene.clip_audio)
        select(self.fit_combo, scene.fit)
        select(self.motion_combo, scene.motion)
        self.color_btn.set_color(scene.color)
        self.gradient_check.setChecked(bool(scene.color2))
        self.color2_btn.set_color(scene.color2 or "#000000")
        self.duration_spin.setValue(scene.duration)
        if self.text_edit.toPlainText() != scene.text:
            self.text_edit.setPlainText(scene.text)
        self.font_combo.setCurrentFont(QFont(scene.style.font))
        self.size_spin.setValue(scene.style.size)
        self.bold_check.setChecked(scene.style.bold)
        self.text_color_btn.set_color(scene.style.color)
        select(self.text_pos_combo, scene.style.position)
        self._loading = False
        self._update_props_visibility(scene)

    def _update_props_visibility(self, scene: Scene) -> None:
        kind = scene.kind
        clip = kind == "clip" and bool(scene.source)
        self.form.setRowVisible(self.file_row, kind in ("image", "clip"))
        for row in (self.clip_row, self.speed_row, self.clip_fill_combo, self.clip_info_label, self.clip_tools_row):
            self.form.setRowVisible(row, clip)
        self.form.setRowVisible(self.fit_combo, kind in ("image", "clip"))
        self.form.setRowVisible(self.motion_combo, kind == "image")
        self.form.setRowVisible(self.color_row, kind == "color" or not scene.source)
        self.color2_btn.setEnabled(self.gradient_check.isChecked())
        self.speed_row.setEnabled(scene.clip_fill != "stretch")
        self.clip_info_label.setText(self._clip_info(scene) if clip else "")

    def _clip_info(self, scene: Scene) -> str:
        """How the chosen part of the clip fills the scene, in words."""
        length = self._clip_length(scene.source)
        if not length:
            return "클립 길이를 알 수 없습니다."
        start, end = clip_part(scene, length)
        used = end - start
        speed = clip_speed(scene, length)
        text = f"클립 {format_time(length, precise=True)} 중 {used:.1f}초 사용"
        if scene.clip_fill == "stretch":
            return f"{text} → {speed:.2f}배속으로 장면 {scene.duration:.1f}초를 꽉 채웁니다."
        plays = used / speed
        if abs(speed - 1) > 1e-6:
            text += f", {speed:g}배속이면 {plays:.1f}초"
        gap = scene.duration - plays
        if gap > 0.05:
            how = "마지막 화면에서 멈춥니다" if scene.clip_fill == "freeze" else "처음부터 반복합니다"
            text += f" · 장면이 {gap:.1f}초 더 길어서 {how}."
        elif gap < -0.05:
            text += f" · 장면이 끝나서 뒤 {-gap * speed:.1f}초(클립 기준)는 안 나옵니다."
        return text

    def _use_whole_clip(self) -> None:
        self._loading = True
        self.clip_start_spin.setValue(0.0)
        self.clip_end_spin.setValue(self.clip_end_spin.maximum())
        self._loading = False
        self._props_changed()

    def _fit_scene_to_clip(self) -> None:
        if not (0 <= self.current < len(self.project.scenes)):
            return
        scene = self.project.scenes[self.current]
        length = self._clip_length(scene.source)
        if not length:
            return
        start, end = clip_part(scene, length)
        speed = scene.speed if scene.clip_fill != "stretch" else 1.0
        if scene.clip_fill == "stretch":
            # The length now comes from the clip, not the other way round: play it at normal speed. The
            # speed box was greyed out in stretch mode and may still hold an old value.
            select(self.clip_fill_combo, "freeze")
            self.speed_spin.setValue(1.0)
        self.duration_spin.setValue(max(MIN_DURATION, (end - start) / speed))
        if scene.narration:
            self._show_status("장면 길이를 클립에 맞췄습니다. 음성 문장 길이와는 달라졌으니 필요하면 [음성에 맞추기]로 되돌리세요.")

    def _props_changed(self) -> None:
        if self._loading or not (0 <= self.current < len(self.project.scenes)):
            return
        start, end = self.clip_start_spin.value(), self.clip_end_spin.value()
        if end - start < MIN_CLIP_PART:  # typed a start past the end, or the other way round
            start = max(0.0, end - MIN_CLIP_PART)
        scene = self.project.scenes[self.current]
        scene = dataclasses.replace(
            scene,
            kind=self.kind_combo.currentData(),
            clip_start=start,
            clip_end=end,
            speed=self.speed_spin.value(),
            clip_fill=self.clip_fill_combo.currentData(),
            clip_audio=self.clip_audio_check.isChecked(),
            fit=self.fit_combo.currentData(),
            motion=self.motion_combo.currentData(),
            color=self.color_btn.color(),
            color2=self.color2_btn.color() if self.gradient_check.isChecked() else "",
            duration=self.duration_spin.value(),
            text=self.text_edit.toPlainText(),
            style=TextStyle(
                font=self.font_combo.currentFont().family(),
                size=self.size_spin.value(),
                bold=self.bold_check.isChecked(),
                color=self.text_color_btn.color(),
                position=self.text_pos_combo.currentData(),
            ),
        )
        self.project.scenes[self.current] = scene
        self._update_props_visibility(scene)
        self._refresh_timeline()
        self._on_playhead_changed()

    # ---- preview ------------------------------------------------------------

    def _preview_time(self) -> float:
        """How far into the current scene the playhead is."""
        if not (0 <= self.current < len(self.project.scenes)):
            return 0.0
        scene = self.project.scenes[self.current]
        return min(max(0.0, self.playhead - self._scene_start(self.current)), scene.duration)

    def _on_playhead_changed(self) -> None:
        self.timeline.set_playhead(self.playhead)
        if 0 <= self.current < len(self.project.scenes):
            scene = self.project.scenes[self.current]
            self.time_label.setText(
                f"{format_time(self.playhead, precise=True)} / {format_time(self.project.duration, precise=True)}"
                f"   ·   {self.current + 1}번 장면 {format_time(self._preview_time(), precise=True)}"
            )
            self.narration_label.setText(f"이 장면의 나레이션: {scene.narration}" if scene.narration else "")
        else:
            self.time_label.setText("")
            self.narration_label.setText("")
        self.preview_timer.start()

    def _start_preview(self) -> None:
        if self.play_bar.isVisible():
            return  # the playing preview owns the screen; the still is drawn again when it stops
        if self.preview_thread is not None:
            self._preview_pending = True  # drawn once the running one is done, so it cannot paint over this
            return
        if not (0 <= self.current < len(self.project.scenes)):
            self.screen.show_message("장면이 없습니다\n[음성 만들기] 탭에서 음성을 보내거나,\n이미지·영상을 아래 타임라인에 끌어다 놓으세요")
            return
        scene, at = self.project.scenes[self.current], self._preview_time()
        if self._scrub is not None and scene.kind == "clip":
            # While a trim handle moves, show that moment of the clip itself.
            scene = dataclasses.replace(scene, clip_start=self._scrub, clip_end=0.0, speed=1.0, clip_fill="freeze")
            at = 0.0
        thread = PreviewThread(scene, self.project.size, at, self)
        thread.ready.connect(self._show_still)
        thread.failed.connect(self._show_preview_error)
        thread.finished.connect(self._preview_finished)
        self.preview_thread = thread
        thread.start()

    def _show_still(self, image: QImage) -> None:
        # Not over a playing preview, and not when the scene it was drawn for has gone (or a newer one is due).
        if not self.play_bar.isVisible() and not self._preview_pending and 0 <= self.current < len(self.project.scenes):
            self.screen.show_frame(QPixmap.fromImage(image))

    def _show_preview_error(self, message: str) -> None:
        if not self.play_bar.isVisible() and not self._preview_pending:
            self.screen.show_message(f"미리보기를 만들지 못했습니다\n{message}")

    def _preview_finished(self) -> None:
        self.preview_thread.deleteLater()
        self.preview_thread = None
        if self._preview_pending:
            self._preview_pending = False
            self._start_preview()

    # ---- voice --------------------------------------------------------------

    def _load_voice(self, audio: Path, quiet: bool = False) -> bool:
        srt = next((audio.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if audio.with_suffix(s).exists()), None)
        try:
            if srt is None:
                raise ValueError(f"'{audio.stem}' 이름의 자막 파일(SRT)이 음성 옆에 없어서 문장을 나눌 수 없습니다.")
            cues = subs.load_cues(srt)
            duration = media.probe(audio).duration
        except (OSError, ValueError) as e:
            if not quiet:
                QMessageBox.warning(self, APP_TITLE, str(e))
            return False
        self.voice_cues, self.voice_duration = cues, duration
        self.project.voice = str(audio)
        self._update_voice_label()
        self._update_summary()
        self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead)  # its 음성 row
        return True

    def _update_voice_label(self) -> None:
        if self.voice_cues:
            sentences = len(sentence_spans(self.voice_cues))
            self.voice_label.setText(
                f"{Path(self.project.voice).name} · {format_time(self.voice_duration, precise=True)} · 문장 {sentences}개"
            )
            self.voice_label.setToolTip(self.project.voice)
        else:
            self.voice_label.setText("없음 — [음성 만들기] 탭에서 보내거나 [음성 고르기]로 고르세요")
            self.voice_label.setToolTip("")
        self.fit_btn.setEnabled(bool(self.voice_cues))

    def choose_voice(self) -> None:
        audio_filter = f"음성 ({' '.join('*' + s for s in media.AUDIO_SUFFIXES)})"
        name, _ = QFileDialog.getOpenFileName(self, "음성 고르기", str(self.store.value("open_dir", "")), audio_filter)
        if name and self._load_voice(Path(name)):
            self._offer_fit()

    def receive_voice(self, audio: Path) -> None:
        """A voice from the voice tab: time the scenes to it."""
        if self._load_voice(audio):
            self._offer_fit()

    def _offer_fit(self) -> None:
        if all(s.is_blank for s in self.project.scenes):
            self.fit_to_voice()
            return
        answer = QMessageBox.question(
            self,
            APP_TITLE,
            "새 음성을 받았습니다. 장면 길이를 이 음성의 문장에 맞출까요?\n(넣어 둔 그림은 순서대로 그대로 둡니다)",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.fit_to_voice()
        else:
            self._show_status("음성을 받았습니다. 필요할 때 [음성에 맞추기]를 누르세요.")

    def fit_to_voice(self) -> None:
        if not self.voice_cues:
            return
        before = len(self.project.scenes)
        self.project.scenes = time_to_voice(
            self.project.scenes, self.voice_cues, self.voice_duration, self.lines_spin.value()
        )
        self._reload_scenes(max(0, self.current))
        added = len(self.project.scenes) - before
        message = f"장면 {len(self.project.scenes)}개의 길이를 음성 문장에 맞췄습니다."
        if added > 0:
            message += f" 빈 장면 {added}개를 더했습니다. 이미지나 영상을 끌어다 놓아 채우세요."
        self._show_status(message)

    # ---- project files ------------------------------------------------------

    def new_project(self) -> None:
        if self.project.scenes and QMessageBox.question(
            self, APP_TITLE, "지금 장면을 모두 지우고 새로 시작할까요?"
        ) != QMessageBox.StandardButton.Yes:
            return
        project = Project(
            resolution=self.project.resolution,
            transition=self.project.transition,
            transition_duration=self.project.transition_duration,
            lines_per_scene=self.project.lines_per_scene,
            voice=self.project.voice,
        )
        self._apply_project(project)

    def open_project(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "프로젝트 열기", str(self.store.value("project_dir", "")), PROJECT_FILTER)
        if not name:
            return
        try:
            project = Project.load(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return
        self.store.setValue("project_dir", str(Path(name).parent))
        self._apply_project(project)
        self._show_status(f"불러왔습니다: {Path(name).name}")

    def save_project(self) -> None:
        start = Path(str(self.store.value("project_dir", Path.home()))) / f"장면_{datetime.now():%Y%m%d_%H%M}.json"
        name, _ = QFileDialog.getSaveFileName(self, "프로젝트 저장", str(start), PROJECT_FILTER)
        if not name:
            return
        try:
            self.project.save(name)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, f"저장하지 못했습니다.\n\n{e}")
            return
        self.store.setValue("project_dir", str(Path(name).parent))
        self._show_status(f"프로젝트를 저장했습니다: {name}")

    def _restore_autosave(self) -> None:
        project = Project()
        if AUTOSAVE.exists():
            try:
                project = Project.load(AUTOSAVE)
            except (OSError, ValueError):
                log_error(traceback.format_exc())
        self._apply_project(project)
        self.trans_spin.setEnabled(project.transition != "none")

    # ---- rendering ----------------------------------------------------------

    def _ready_to_render(self) -> bool:
        if self.render_thread is not None:
            return False
        if not self.project.scenes:
            QMessageBox.information(self, APP_TITLE, "장면이 없습니다. 이미지나 영상을 추가하세요.")
            return False
        blanks = [str(i) for i, s in enumerate(self.project.scenes, 1) if s.is_blank]
        if blanks:
            shown = ", ".join(blanks[:12]) + (" …" if len(blanks) > 12 else "")
            answer = QMessageBox.question(
                self, APP_TITLE, f"그림이 없는 장면이 있습니다 ({shown}번).\n단색 화면으로 만들까요?"
            )
            return answer == QMessageBox.StandardButton.Yes
        return True

    def send_to_compose(self) -> None:
        if self._ready_to_render():
            self._start_render(paths.WORK_DIR / f"장면영상_{datetime.now():%m%d_%H%M%S}.mp4", "send")

    def save_to_file(self) -> None:
        if not self._ready_to_render():
            return
        start = Path(str(self.store.value("save_dir", Path.home()))) / f"영상_{datetime.now():%Y%m%d_%H%M}.mp4"
        name, _ = QFileDialog.getSaveFileName(self, "영상 저장", str(start), "MP4 영상 (*.mp4)")
        if not name:
            return
        output = Path(name)
        if output.suffix.lower() != ".mp4":
            output = output.with_name(output.name + ".mp4")
        self.store.setValue("save_dir", str(output.parent))
        self._start_render(output, "save")

    def _start_render(self, output: Path, purpose: str, voice: Path | None = None) -> RenderThread:
        thread = RenderThread(self.project, output, purpose, voice, self)
        thread.progress.connect(self._on_progress)
        thread.done.connect(self._on_done)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_thread_finished)
        self.render_thread = thread
        self.progress.setValue(0)
        self._render_started = time.perf_counter()
        self._show_status(self._progress_title() + "…")
        self._set_busy(True)
        thread.start()
        return thread

    def _progress_title(self) -> str:
        if self.render_thread is not None and self.render_thread.purpose == "preview":
            return "미리보기 영상을 만드는 중"
        return "장면을 영상으로 만드는 중"

    # ---- playing a preview --------------------------------------------------

    def _preview_key(self, voice: Path | None) -> str:
        """Everything the preview video depends on; when it is unchanged, the last one can be played again."""
        stamps = []
        if voice is not None:
            for path in (voice, *(voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES)):
                if path.exists():
                    stamps.append([str(path), path.stat().st_mtime_ns])
        return json.dumps([self.project.to_json(), stamps], ensure_ascii=False)

    def _scene_start(self, index: int) -> float:
        return sum(s.duration for s in self.project.scenes[:index])

    def _scene_at(self, seconds: float) -> int:
        """Index of the scene showing `seconds` into the video."""
        end = 0.0
        for i, scene in enumerate(self.project.scenes):
            end += scene.duration
            if seconds < end:
                return i
        return len(self.project.scenes) - 1

    def play_preview(self) -> None:
        """Play from the playhead, making (or reusing) a small preview video with the voice first."""
        if self.render_thread is not None or not self.project.scenes:
            return
        voice = Path(self.project.voice) if self.voice_cues and self.project.voice else None
        key = self._preview_key(voice)
        if key == self._preview_made and self._preview_file and self._preview_file.exists():
            self._show_player(self.playhead)
            return
        self._preview_count += 1
        output = scene_render.PREVIEW_DIR / f"미리보기_{os.getpid()}_{self._preview_count}.mp4"
        self._start_render(output, "preview", voice).key = key

    def _show_player(self, start: float) -> None:
        self.still_bar.setVisible(False)
        self.play_bar.setVisible(True)
        source = QUrl.fromLocalFile(str(self._preview_file))
        if self.player.source() != source:
            # A new file loads in the background and ignores seeking until it is ready. Letting go of
            # the old one first keeps the player from reloading midway and stalling.
            self.player.stop()
            self.player.setSource(QUrl())
            self._pending_start = round(start * 1000)  # only now: stopping the old file reports "loaded" too
            self.player.setSource(source)
            return
        self.player.setPosition(round(start * 1000))
        self._play_if_shown()

    def _play_if_shown(self) -> None:
        # A preview finished while another tab is showing waits, paused, for ▶ instead of talking unseen.
        if self.play_bar.isVisible() and self.isVisible():
            self.player.play()

    def _on_media_status(self, status) -> None:
        if self._pending_start is not None and status == QMediaPlayer.MediaStatus.LoadedMedia:
            QTimer.singleShot(0, self._start_pending)  # not from inside the player's own notification

    def _start_pending(self) -> None:
        if self._pending_start is None:
            return
        self.player.setPosition(self._pending_start)
        self._pending_start = None
        self._play_if_shown()

    def _stop_player(self) -> None:
        """Leave playback without choosing a scene (the project is being replaced)."""
        self._pending_start = None
        self.player.pause()
        self.play_bar.setVisible(False)
        self.still_bar.setVisible(True)

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def close_player(self) -> None:
        """Back to the still preview, on the scene and moment where playback stopped."""
        if not self.play_bar.isVisible():
            return
        self._pending_start = None  # a preview still loading must not start playing after this
        self.player.pause()
        seconds = min(self.player.position() / 1000, self.project.duration)
        self.play_bar.setVisible(False)
        self.still_bar.setVisible(True)
        if self.project.scenes:
            index = self._scene_at(seconds)
            if index != self.current:
                self._select_scene(index, seconds)
                return
            self.playhead = seconds
        self._on_playhead_changed()  # redraw the still, even if the scene and moment did not change

    def _on_video_frame(self, frame: QVideoFrame) -> None:
        if self.play_bar.isVisible() and frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                self.screen.show_frame(QPixmap.fromImage(image))

    def _on_player_position(self, ms: int) -> None:
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(ms)
        total = self.player.duration() / 1000
        seconds = ms / 1000
        self.play_time_label.setText(f"{format_time(seconds, precise=True)} / {format_time(total, precise=True)}")
        if self.play_bar.isVisible():
            self.timeline.set_playhead(seconds)
            self.timeline.ensure_visible(seconds)
        if self.project.scenes:
            index = self._scene_at(seconds)
            narration = self.project.scenes[index].narration
            self.narration_label.setText(
                f"장면 {index + 1} / {len(self.project.scenes)}" + (f" · {narration}" if narration else "")
            )

    def _on_playback_state(self, state) -> None:
        self.pause_btn.setText("❚❚" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _release_previews(self, keep: Path | None) -> None:
        """Delete preview videos other than `keep` (one still open in the player cannot be deleted yet)."""
        for path in scene_render.PREVIEW_DIR.glob("미리보기_*.mp4"):
            if path != keep:
                try:
                    path.unlink()
                except OSError:
                    pass

    def hideEvent(self, event):
        # Switching tabs should not leave the preview talking in the background.
        self.player.pause()
        super().hideEvent(event)

    def cancel_render(self) -> None:
        if self.render_thread is not None:
            self.render_thread.cancel()
            self._show_status("취소하는 중…")

    def _on_progress(self, fraction: float) -> None:
        self.progress.setValue(round(fraction * 1000))
        text = f"{self._progress_title()}… {fraction * 100:.0f}%"
        if fraction > 0.03:
            elapsed = time.perf_counter() - self._render_started
            text += f" · 남은 시간 약 {format_time(elapsed * (1 - fraction) / fraction)}"
        self._show_status(text)

    def _on_done(self, length: float) -> None:
        thread = self.render_thread
        output = thread.output
        if thread.purpose == "preview":
            self.progress.setValue(0)
            self._show_status(f"미리보기를 재생합니다 ({thread.elapsed:.1f}초 걸려 만듦). 다시 누르면 바로 재생됩니다.")
            self._preview_file, self._preview_made = output, thread.key
            self._show_player(self.playhead)
            self._release_previews(keep=output)
            return
        self._show_status(f"완성: {output.name} ({format_time(length, precise=True)} 분량, {thread.elapsed:.1f}초 걸림)")
        if thread.purpose == "send":
            self.videoReady.emit(output)
            return
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

    def _set_busy(self, busy: bool) -> None:
        has_scenes = bool(self.project.scenes)
        for button in (self.save_btn, self.send_btn, self.play_preview_btn):
            button.setEnabled(not busy and has_scenes)
        self.cancel_btn.setEnabled(busy)

    # ---- misc ---------------------------------------------------------------

    def _show_status(self, message: str) -> None:
        self.status.setText(message)
        self.status.setToolTip(message)

    def shutdown(self) -> None:
        """Called by the main window as it closes."""
        if self.recorder is not None:  # closed from the taskbar while recording: drop the recording
            self.recorder.finished.disconnect()
            self.recorder.failed.disconnect()
            self.recorder.cancel()
        self._timeline_view.removeEventFilter(self)  # the window is about to be destroyed
        self.player.stop()
        self.player.setSource(QUrl())  # lets the preview file go
        self._release_previews(keep=None)
        if self.render_thread is not None:
            self.render_thread.cancel()
            self.render_thread.wait(10_000)
        if self.strip_thread is not None:
            self.strip_thread.stop()
        for thread in (self.thumb_thread, self.strip_thread, self.preview_thread):
            if thread is not None:
                thread.requestInterruption()
                thread.wait(5_000)  # one still running past this is dealt with in main_window.main
        try:
            paths.WORK_DIR.mkdir(parents=True, exist_ok=True)
            self.project.save(AUTOSAVE)
        except OSError:
            log_error(traceback.format_exc())
        self.store.sync()
