"""The video tab: scenes (images, clips, title cards) timed to the voice and made into one video."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QRectF, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QFont, QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
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
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import (autosave, canvas, encoder, formats, graphics, media, paths, render, scene_render, sfx, storyboard, subs,
               templates, waveform)  # fmt: skip
from .history import History, Snapshot
from .media import format_time
from .recorder import ScreenRecorder
from .scene_panels import AdjustPanel, MaskPanel, OverlayPanel, ProjectPanel, TextPanel
from .scene_screen import SceneScreen
from .scenes import (
    DEFAULT_DURATION,
    FRAME_RATES,
    MAX_SPEED,
    MIN_CLIP_PART,
    MIN_DURATION,
    MIN_SPEED,
    TAIL,
    Overlay,
    Project,
    Scene,
    SoundEffect,
    TextBox,
    TextStyle,
    clip_part,
    clip_speed,
    copy_scene,
    from_dict,
    key_at,
    sentence_spans,
    split_scene,
    time_to_voice,
    transition_at,
)
from .subs import Cue, SubStyle
from .timeline import HEIGHT as TIMELINE_HEIGHT
from .timeline import Timeline, TimelineHeader
from .trim import STRIP_FRAMES, StripThread
from .widgets import APP_TITLE, ColorButton, FrameViewer, log_error, select

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
    ("fadewhite", "흰 화면 거치기"),
    ("dissolve", "점점이 바뀜"),
    ("slide", "옆으로 밀어내기"),
    ("slideup", "위로 밀어내기"),
    ("wipe", "닦아내기"),
    ("smooth", "부드럽게 닦기"),
    ("circle", "원형으로 열기"),
    ("radial", "시계 방향으로"),
    ("zoom", "확대하며 바뀜"),
    ("pixelize", "모자이크"),
    ("blur", "흐려지며 바뀜"),
    ("none", "없음 (바로 넘김)"),
]
TRANSITION_NAMES = dict(TRANSITIONS)
SCENE_TRANSITIONS = [("", "영상 설정을 따름"), *TRANSITIONS]
KINDS = [("image", "이미지"), ("clip", "영상 클립"), ("color", "글자·색 화면"), ("graphic", "그래픽 (도형·그래프 애니메이션)")]
FITS = [("cover", "꽉 채우기 (가장자리 잘림)"), ("contain", "전체 보이기 (흐린 배경)")]
MOTIONS = [
    ("zoom_in", "천천히 확대"),
    ("zoom_out", "천천히 축소"),
    ("pan_right", "왼쪽 → 오른쪽"),
    ("pan_left", "오른쪽 → 왼쪽"),
    ("none", "움직임 없음"),
]
CLIP_FILLS = [
    ("freeze", "마지막 화면에서 멈춤"),
    ("loop", "처음부터 반복"),
    ("stretch", "속도를 바꿔 장면에 꽉 맞춤"),
]
FRAME_RATE_LABELS = {24: "24fps (영화 느낌)", 25: "25fps", 30: "30fps (기본)", 50: "50fps", 60: "60fps (부드러움)"}
VIDEO_FORMATS = [f for f in formats.FORMATS if f.video]  # [파일로 저장] has no voice: a picture is wanted
CONVERT_SHARE = 0.2  # of the progress bar, for saving the made MP4 in another format
FIT_TOLERANCE = 1.43  # a picture this much wider or taller than the frame is shown whole instead of cropped
THUMB_SIZE = QSize(256, 144)  # largest thumbnail drawn for the timeline, before fitting the frame's shape
MAX_THUMBS = 300  # past this, thumbnails of looks no scene has any more are dropped
AUTOSAVE = paths.WORK_DIR / "마지막 장면.json"
PROJECT_FILTER = "장면 프로젝트 (*.json)"
IMAGE_FILTER = f"이미지 ({' '.join('*' + s for s in canvas.IMAGE_SUFFIXES)})"
VIDEO_FILTER = f"영상 ({' '.join('*' + s for s in media.VIDEO_SUFFIXES)})"
AUDIO_FILTER = f"음악 ({' '.join('*' + s for s in media.AUDIO_SUFFIXES)})"
SCENES_MIME = "application/x-movie-editor-scenes"
# Settings of the 장면 and 보정 tabs that go to every selected scene when they are changed on one of them.
SHARED_FIELDS = ("duration", "speed", "clip_fill", "reverse", "clip_audio", "clip_volume", "fit", "motion", "color", "color2", "style", "transition",
                 "transition_duration", "crop", "rotate", "flip_h", "flip_v", "brightness", "contrast", "saturation",
                 "look")  # fmt: skip
COLOUR_FIELDS = ("brightness", "contrast", "saturation", "look")
AUTOSAVE_DELAY = 1500  # ms after a change before the project is saved
BACKUP_EVERY = 300  # seconds between dated copies in 작업\자동저장


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in canvas.IMAGE_SUFFIXES:
        return "image"
    if suffix in media.VIDEO_SUFFIXES:
        return "clip"
    return None


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
                at = graphics.poster_time(scene.layers, scene.style, scene.duration) if scene.kind == "graphic" else 0.0
                image = scene_render.still(scene, *self.size, at)
            except Exception:  # noqa: BLE001 - a missing file just gets a blank thumbnail
                image = None
            self.ready.emit(key, image)


class PreviewThread(QThread):
    """Draws the preview at the video's full size; at 4K that takes too long for the window's own thread."""

    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, scene: Scene, size: tuple[int, int], at: float, project: Project, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.size = size
        self.at = at
        self.project = Project.from_json(project.to_json())  # the logo settings, safe from edits

    def run(self):
        try:
            logo = canvas.logo_layer(self.project, *self.size)
            self.ready.emit(scene_render.still(self.scene, *self.size, self.at, logo))
        except Exception as e:  # noqa: BLE001 - shown in the preview
            self.failed.emit(str(e))


class RenderThread(QThread):
    progress = Signal(float)
    done = Signal(float)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, project: Project, output: Path, purpose: str, voice: Path | None = None,
                 subtitle: Path | None = None, style: SubStyle | None = None, fmt: formats.Format = formats.DEFAULT,
                 parent=None):  # fmt: skip
        super().__init__(parent)
        self.project = Project.from_json(project.to_json())  # edits made while rendering do not leak in
        self.output = output
        self.purpose = purpose  # "send" to the compose tab, "save" to a file, or "preview" to play here
        self.format = fmt
        self.voice = voice
        self.subtitle = subtitle
        self.style = style
        self.key = ""  # what the preview was made from; set by the page
        self.elapsed = 0.0
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        start = time.perf_counter()
        try:
            if self.purpose == "preview":
                length = scene_render.preview(self.project, self.output, self.voice, self.progress.emit,
                                              lambda: self._cancel, self.subtitle, self.style)  # fmt: skip
            else:
                length = self._make()
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

    def _make(self) -> float:
        """The scenes as a video: straight into an MP4, or into one first and then saved in the chosen format."""
        progress, cancelled = self.progress.emit, lambda: self._cancel
        # Sent on, the compose tab puts the fades on, over the voice and the music too.
        fades = self.purpose != "send"
        if self.format.key == formats.DEFAULT.key:
            return scene_render.render(self.project, self.output, progress, cancelled, fades=fades)
        with tempfile.TemporaryDirectory(prefix="movie_") as tmp:
            made = Path(tmp) / "장면.mp4"
            share = 1 - CONVERT_SHARE
            length = scene_render.render(self.project, made, lambda f: progress(f * share), cancelled, fades=fades)
            job = render.Job(video=made, output=self.output, fit="video", original_volume=1.0, quality="high",
                             format=self.format.key)  # fmt: skip
            render.run(job, lambda f: progress(share + f * CONVERT_SHARE), cancelled)
        return length


class CueDialog(QDialog):
    """Change one subtitle line's words and times."""

    DELETE = 2

    def __init__(self, cue: Cue, low: float, high: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("자막 고치기")
        self.cue = cue
        layout = QFormLayout(self)
        self.text_edit = QPlainTextEdit(cue.text)
        self.text_edit.setFixedHeight(90)
        layout.addRow("자막", self.text_edit)
        self.start_spin, self.end_spin = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, value in ((self.start_spin, cue.start), (self.end_spin, cue.end)):
            spin.setRange(low, high)
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            spin.setSuffix(" 초")
            spin.setValue(value)
        layout.addRow("시작", self.start_spin)
        layout.addRow("끝", self.end_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        delete = buttons.addButton("이 자막 지우기", QDialogButtonBox.ButtonRole.DestructiveRole)
        delete.clicked.connect(lambda: self.done(self.DELETE))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.text_edit.setFocus()
        self.text_edit.selectAll()

    def result_cue(self) -> Cue:
        # Times left alone stay exactly as they were (the boxes show them rounded).
        start = self.cue.start if abs(self.start_spin.value() - self.cue.start) < 0.005 else self.start_spin.value()
        end = self.cue.end if abs(self.end_spin.value() - self.cue.end) < 0.005 else self.end_spin.value()
        return Cue(start, max(end, start + 0.05), self.text_edit.toPlainText().strip())


class LayersDialog(QDialog):
    """A graphic scene's layers as JSON, checked against its style before they are kept."""

    def __init__(self, layers: list[dict], style_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"그래픽 레이어 고치기 · 스타일 {style_name or graphics.DEFAULT}")
        self.style_name = style_name
        self.layers = layers
        layout = QVBoxLayout(self)
        hint = QLabel("레이어마다 type(rect, circle, line, arrow, text, counter, tiles, curve, bars, prop, image, path, "
                      "group, frames, video, plugin, background)과 위치·색·나타남을 적습니다. 위치는 화면 비율(0–1), "
                      "굵기·글자 크기는 1080p 기준 px입니다. 숫자 칸에는 \"0.5+0.1*sin(2*pi*t)\" 같은 식도 씁니다. "
                      "Claude(MCP)의 graphic_guide에 전체 설명이 있습니다.")  # fmt: skip
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.edit = QPlainTextEdit(json.dumps(layers, ensure_ascii=False, indent=2))
        self.edit.setFont(QFont("Consolas", 10))
        self.edit.setMinimumSize(760, 520)
        layout.addWidget(self.edit, 1)
        self.error = QLabel()
        self.error.setStyleSheet("color: #dc2626;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _check(self) -> None:
        try:
            layers = json.loads(self.edit.toPlainText() or "[]")
        except ValueError as e:
            self.error.setText(f"JSON을 읽을 수 없습니다: {e}")
            return
        problems = graphics.check_layers(layers, self.style_name)
        if problems:
            self.error.setText("\n".join(problems[:8]))
            return
        self.layers = layers
        self.accept()


class TemplateDialog(QDialog):
    """A graphic scene from a template: pick one, fill in its values, and see it as it will look."""

    PREVIEW_WIDTH = 480

    def __init__(self, style_name: str, size: tuple[int, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("템플릿에서 만들기")
        self.style_name = style_name
        self.size = size
        self.filled: dict = {}  # the template's layers once accepted
        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        self.combo = QComboBox()
        for name in templates.names():
            self.combo.addItem(name, name)
        left.addWidget(self.combo)
        self.about = QLabel()
        self.about.setObjectName("muted")
        self.about.setWordWrap(True)
        left.addWidget(self.about)
        box = QWidget()
        self.form = QFormLayout(box)
        self.form.setContentsMargins(0, 0, 0, 0)
        left.addWidget(box)
        hint = QLabel("목록은 가 | 나 | 다, 비워 두면 그 부분이 빠집니다. 글자에는 [accent]강조[/]도 됩니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        left.addWidget(hint)
        left.addStretch(1)
        self.error = QLabel()
        self.error.setStyleSheet("color: #dc2626;")
        self.error.setWordWrap(True)
        left.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        left.addWidget(buttons)
        layout.addLayout(left, 1)
        width, height = size
        self.preview = QLabel()
        self.preview.setFixedSize(self.PREVIEW_WIDTH, round(self.PREVIEW_WIDTH * height / width))
        self.preview.setStyleSheet("background: #111827;")
        layout.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
        self.edits: dict[str, QLineEdit] = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._show)
        self.combo.currentIndexChanged.connect(self._load)
        self._load()

    def _template(self) -> dict:
        return templates.get(self.combo.currentData())

    def _load(self) -> None:
        template = self._template()
        self.about.setText(template.get("description", ""))
        while self.form.rowCount():
            self.form.removeRow(0)
        self.edits = {}
        for name, spec in templates.params(template).items():
            edit = QLineEdit(templates.as_text(spec["default"]))
            edit.setToolTip(spec.get("doc", ""))
            edit.setPlaceholderText(spec.get("doc", ""))
            edit.textChanged.connect(self.timer.start)
            label = QLabel(name)
            label.setToolTip(spec.get("doc", ""))
            self.form.addRow(label, edit)
            self.edits[name] = edit
        self.timer.start()

    def _filled(self) -> dict:
        spec = templates.params(self._template())
        values = {name: templates.parse_value(edit.text(), spec[name]["default"]) for name, edit in self.edits.items()}
        filled = templates.expand(self.combo.currentData(), values)
        problems = graphics.check_layers(filled["layers"], self.style_name)
        if problems:
            raise ValueError("\n".join(problems[:5]))
        return filled

    def _show(self) -> None:
        try:
            filled = self._filled()
            width, height = self.size
            w = 960
            at = graphics.poster_time(filled["layers"], self.style_name, filled["duration"])
            image = graphics.frame(filled["layers"], self.style_name, w, round(w * height / width), at, filled["duration"])
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.error.setText("")
        self.preview.setPixmap(QPixmap.fromImage(image.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                              Qt.TransformationMode.SmoothTransformation)))  # fmt: skip

    def _accept(self) -> None:
        try:
            self.filled = self._filled()
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.accept()


class ScenePage(QWidget):
    videoReady = Signal(object)  # dict: the finished video for the compose tab, with its music and subtitles

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.store.beginGroup("scenes")  # the other tabs keep their settings in the same file
        self.shared = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)  # for all tabs
        encoder.use_gpu = self.shared.value("gpu", False, type=bool)
        self.project = Project()
        self.current = -1
        self.selection: set[int] = set()
        self.playhead = 0.0  # the moment shown in the preview, in seconds into the video
        self._file_cues: list[subs.Cue] = []  # the voice's own subtitle file
        self.voice_duration = 0.0
        self.history = History()
        self._restoring = False
        # Set by the main window: the compose tab's subtitle look, so the preview shows the same.
        self.subtitle_style: Callable[[], SubStyle] | None = None
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
        self._waves: dict[str, object] = {}  # sound file -> its loudness outline (None: could not be read)
        self._wave_queue: list[str] = []
        self.wave_thread: waveform.WaveThread | None = None
        self.recorder: ScreenRecorder | None = None
        self._was_maximized = False
        self._loading = False  # true while the settings panel is being filled from a scene
        self._clip_lengths: dict[str, float] = {}
        self._source_sizes: dict[str, tuple[int, int]] = {}  # overlays' picture sizes, by _strip_key
        self._sound_lengths: dict[str, float] = {}  # sound effects' lengths, by _strip_key
        self.autosave_path = autosave.session().file(AUTOSAVE)
        self._last_backup = time.monotonic()
        self._autosave_failed = False
        self._startup_note = ""

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(AUTOSAVE_DELAY)
        self.autosave_timer.timeout.connect(self._autosave)
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
        self._make_shortcuts()

        self._restore_autosave()
        self.history.reset(self._snapshot())
        self._update_undo_buttons()
        self._set_busy(False)
        self._show_status(self._startup_note or "[음성 만들기] 탭에서 음성을 보내면 문장마다 장면이 생깁니다. 이미지나 영상을 끌어다 놓아도 됩니다.")

    # ---- layout -------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)
        root.addWidget(self._build_top_box())

        self.work_area = QWidget()
        work = QVBoxLayout(self.work_area)
        work.setContentsMargins(0, 0, 0, 0)
        work.setSpacing(12)
        body = QHBoxLayout()
        body.setSpacing(16)
        # The preview and the timeline are where the editing keys (arrows, Delete, copy and paste) work;
        # the settings beside them keep those keys for their own boxes and lists.
        self.preview_panel = self._build_preview_panel()
        body.addWidget(self.preview_panel, 1)
        body.addWidget(self._build_settings())
        work.addLayout(body, 1)
        self.timeline_panel = self._build_timeline_panel()
        work.addWidget(self.timeline_panel)
        root.addWidget(self.work_area, 1)
        root.addWidget(self._build_action_row())

    def _build_top_box(self) -> QGroupBox:
        box = QGroupBox("영상 설정")
        col = QVBoxLayout(box)
        col.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.res_combo = self._combo(RESOLUTIONS)
        self.trans_combo = self._combo(TRANSITIONS)
        self.trans_combo.setToolTip("장면이 바뀌는 방법 (장면마다 따로 정할 수도 있습니다: [장면] 탭의 [다음 전환])")
        self.trans_spin = QDoubleSpinBox()
        self.trans_spin.setRange(0.1, 3.0)
        self.trans_spin.setSingleStep(0.1)
        self.trans_spin.setDecimals(1)
        self.trans_spin.setSuffix(" 초")
        self.trans_spin.setToolTip("장면이 바뀌는 데 걸리는 시간. 새 장면은 그 문장이 시작될 때부터 나타납니다.")
        self.fps_combo = self._combo([(rate, FRAME_RATE_LABELS[rate]) for rate in FRAME_RATES])
        self.fps_combo.setToolTip(
            "1초에 몇 장의 그림으로 만들지 정합니다.\n"
            "30fps가 보통이고, 60fps는 게임·화면 녹화처럼 빠른 움직임이 부드럽지만 만드는 데 더 걸리고 파일이 커집니다.\n"
            "24fps는 영화 같은 느낌입니다. 클립은 이 빠르기에 맞춰 바뀝니다."
        )
        self.gpu_check = QCheckBox("그래픽 카드로 인코딩")
        self.gpu_check.setToolTip(
            "NVIDIA·Intel·AMD 그래픽 카드의 인코더로 영상을 만듭니다. 큰 영상(4K)에서 더 빠르고, 파일은 조금 커집니다.\n"
            "쓸 수 없는 PC에서는 저절로 CPU로 만듭니다. [③ 합성] 탭에도 같이 적용됩니다."
        )
        self.gpu_check.setChecked(encoder.use_gpu)
        project_btn = QToolButton()
        project_btn.setText("프로젝트 ▾")
        project_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(project_btn)
        menu.addAction("새로 만들기", self.new_project)
        menu.addAction("열기…", lambda: self.open_project())
        menu.addAction("다른 이름으로 저장… (Ctrl+S)", self.save_project)
        menu.addSeparator()
        menu.addAction("자동 저장 기록에서 열기…", lambda: self.open_project(autosave.backup_folder()))
        menu.addSeparator()
        menu.addAction("콘티 엑셀에서 만들기…", self.import_storyboard)
        menu.addAction("콘티 엑셀로 내보내기…", self.export_storyboard)
        menu.addAction("스토리보드 그림 저장…", self.save_storyboard_image)
        menu.addAction("장면 그림 모두 저장…", self.save_keyframes)
        menu.addAction("빈 콘티 양식 만들기…", self.new_storyboard_file)
        project_btn.setMenu(menu)
        for w in (QLabel("화면"), self.res_combo, self.fps_combo, QLabel("   장면 전환"), self.trans_combo, self.trans_spin):
            row.addWidget(w)
        row.addSpacing(10)
        row.addWidget(self.gpu_check)
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
            ("+ 템플릿", "제목·목록·큰 숫자·막대 비교·해석 결과 같은 그래픽 장면을 템플릿에서 골라 지금 장면 뒤에 넣습니다", self.add_from_template),
            ("● 화면 녹화", "화면의 원하는 영역(또는 모니터 전체)을 녹화해서 장면으로 넣습니다", self.record_screen),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        sound_btn = QToolButton()
        sound_btn.setText("+ 효과음")
        sound_btn.setToolTip("빨간 선 위치에 효과음을 넣습니다 (소리 파일을 타임라인에 끌어다 놓아도 됩니다)")
        sound_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sound_btn.setMenu(self._sound_add_menu(sound_btn, lambda: self.playhead))
        bar.addWidget(sound_btn)
        bar.addSpacing(10)
        self.undo_btn = self._tool_button("↶", "실행 취소 (Ctrl+Z)", self.undo)
        self.redo_btn = self._tool_button("↷", "다시 실행 (Ctrl+Y)", self.redo)
        bar.addWidget(self.undo_btn)
        bar.addWidget(self.redo_btn)
        bar.addSpacing(10)
        self.up_btn = self._tool_button("◀", "고른 장면을 앞으로 (타임라인에서 끌어도 됩니다)", lambda: self.move_scene(-1))
        self.down_btn = self._tool_button("▶", "고른 장면을 뒤로", lambda: self.move_scene(1))
        self.copy_btn = self._tool_button("복제", "고른 장면을 바로 뒤에 하나 더 (Ctrl+D)", self.duplicate_scene)
        self.split_btn = self._tool_button("✂ 나누기", "빨간 선 위치에서 장면을 둘로 나눕니다 (Ctrl+B)", self.split_current)
        self.delete_btn = self._tool_button("삭제", "고른 장면을 지웁니다 (Delete)", self.delete_scene)
        for b in (self.up_btn, self.down_btn, self.copy_btn, self.split_btn, self.delete_btn):
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
            bar.addWidget(self._tool_button(text, tip, slot))
        col.addLayout(bar)

        row = QHBoxLayout()
        row.setSpacing(0)
        row.addWidget(TimelineHeader(), 0, Qt.AlignmentFlag.AlignTop)
        self.timeline = Timeline()
        self.timeline.thumb = lambda scene: self.thumbs.get(self._thumb_key(scene))
        self.timeline.strip = lambda source: self._strips.get(self._strip_key(source))
        self.timeline.clip_length = self._clip_length
        self.timeline.wave = lambda path: self._waves.get(path)
        self.timeline.sound_length = self._sound_length
        self.timeline.transition_label = self._transition_label
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

    @staticmethod
    def _tool_button(text: str, tip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setStyleSheet("padding: 6px 10px;")
        button.clicked.connect(slot)
        return button

    def eventFilter(self, obj, event):
        # Compared by reference: asking the scroll area for its viewport fails while the window is torn down.
        if obj is self._timeline_view and event.type() == QEvent.Type.Resize:
            self.timeline.view_width = obj.width()
            if self.timeline.fit_mode:
                self.timeline.fit()
            else:
                self._update_timeline()
        return super().eventFilter(obj, event)

    def _build_preview_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        # A still frame of the chosen scene, or, while a preview plays, its frames as they come.
        self.screen = SceneScreen()
        self.screen.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
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
        # Sound effects are heard on their own through a second player.
        self.sound_player = QMediaPlayer(self)
        self.sound_out = QAudioOutput(self)
        self.sound_player.setAudioOutput(self.sound_out)

        self.still_bar = QWidget()
        row = QHBoxLayout(self.still_bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel()
        self.time_label.setToolTip("아래 타임라인의 눈금을 누르거나 끌어서 보고 싶은 순간을 고릅니다 (←/→: 한 프레임, Shift: 1초)")
        full_btn = QPushButton("원본 크기로 보기")
        full_btn.setToolTip("이 순간을 확대 없이 실제 픽셀 그대로 봅니다 (미리보기를 두 번 클릭해도 됩니다)")
        full_btn.clicked.connect(self.screen.open_full_size)
        save_btn = QPushButton("이미지로 저장…")
        save_btn.setToolTip("이 순간을 원본 크기 그대로 PNG·JPG로 저장합니다 (썸네일 만들기)")
        save_btn.clicked.connect(self.save_frame)
        self.play_preview_btn = QPushButton("▶ 재생")
        self.play_preview_btn.setObjectName("primary")
        self.play_preview_btn.setToolTip(
            "타임라인의 빨간 선 위치부터 음성·자막·음악과 함께 재생합니다 (Space).\n"
            "빨리 만들 수 있도록 작게(가로 960) 만들고, 한 번 만든 장면은 다시 만들지 않습니다."
        )
        self.play_preview_btn.clicked.connect(self.play_preview)
        row.addWidget(self.time_label, 1)
        row.addWidget(full_btn)
        row.addWidget(save_btn)
        row.addWidget(self.play_preview_btn)
        layout.addWidget(self.still_bar)

        self.play_bar = QWidget()
        row = QHBoxLayout(self.play_bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.pause_btn = QPushButton("❚❚")
        self.pause_btn.setFixedWidth(52)
        self.pause_btn.setToolTip("멈춤·계속 (Space)")
        self.pause_btn.clicked.connect(self.toggle_playback)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.play_time_label = QLabel()
        self.play_time_label.setMinimumWidth(130)
        self.play_time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        close_btn = QPushButton("■ 멈추고 장면 보기")
        close_btn.setToolTip("재생을 멈추고, 멈춘 곳의 장면을 고릅니다 (Esc)")
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

    def _build_settings(self) -> QWidget:
        side = QWidget()
        side.setFixedWidth(372)
        col = QVBoxLayout(side)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        self.selection_label = QLabel()
        self.selection_label.setObjectName("muted")
        self.selection_label.setWordWrap(True)
        col.addWidget(self.selection_label)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabBar::tab { padding: 6px 4px 5px 4px; margin-right: 6px; font-size: 10pt; }"
            "QTabWidget::pane { border: 1px solid #e3e5e8; border-radius: 8px; background: #ffffff; top: -1px; }"
        )
        self.text_panel = TextPanel(self._new_text_style, self._preview_time)
        self.overlay_panel = OverlayPanel(self._ask_overlay_file, self._overlay_height_share, self._preview_time)
        self.mask_panel = MaskPanel()
        self.adjust_panel = AdjustPanel()
        self.project_panel = ProjectPanel(self._ask_project_file)
        for widget, title in (
            (self._build_props_box(), "장면"),
            (self.text_panel, "글자"),
            (self.overlay_panel, "겹치기"),
            (self.mask_panel, "가리기"),
            (self.adjust_panel, "보정"),
            (self.project_panel, "전체"),
        ):
            page = QWidget()
            inner = QVBoxLayout(page)
            inner.setContentsMargins(10, 10, 10, 10)
            inner.addWidget(widget)
            scroll = QScrollArea()
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
            self.tabs.addTab(scroll, title)
        col.addWidget(self.tabs, 1)
        return side

    def _build_props_box(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
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
        self.reverse_check = QCheckBox("거꾸로 재생")
        self.reverse_check.setToolTip("고른 구간을 끝에서부터 거꾸로 보여 줍니다 (소리도 거꾸로)")
        form.addRow("방향", self.reverse_check)
        self.clip_info_label = QLabel()
        self.clip_info_label.setObjectName("muted")
        self.clip_info_label.setWordWrap(True)
        form.addRow(self.clip_info_label)
        fit_length_btn = QPushButton("장면 길이를 클립에 맞춤")
        fit_length_btn.setToolTip("쓰는 구간을 지금 속도로 재생한 길이만큼 장면 길이를 바꿉니다")
        fit_length_btn.clicked.connect(self._fit_scene_to_clip)
        self.clip_tools_row = self._row(fit_length_btn)
        form.addRow(self.clip_tools_row)
        self.clip_audio_check = QCheckBox("넣기")
        self.clip_audio_check.setToolTip("클립에 들어 있던 소리를 씁니다. 속도를 바꿔도 음 높이는 그대로입니다.")
        self.clip_volume_spin = QSpinBox()
        self.clip_volume_spin.setRange(0, 300)
        self.clip_volume_spin.setSingleStep(10)
        self.clip_volume_spin.setSuffix(" %")
        self.clip_volume_spin.setToolTip("이 클립 소리의 크기 (전체 크기는 합성 탭의 [원본 소리]로도 정합니다)")
        self.clip_sound_row = self._row(self.clip_audio_check, QLabel("크기"), self.clip_volume_spin)
        form.addRow("클립 소리", self.clip_sound_row)

        self.fit_combo = self._combo(FITS)
        self.fit_combo.setMinimumWidth(10)
        form.addRow("맞춤", self.fit_combo)
        self.motion_combo = self._combo(MOTIONS)
        form.addRow("움직임", self.motion_combo)

        self.style_combo = QComboBox()
        self.style_combo.setMinimumWidth(10)
        self.style_combo.setToolTip("그래픽 장면을 그리는 스타일 (색, 글꼴, 배경, 색 지도, 소품, 움직임)")
        style_btn = QPushButton("보기")
        style_btn.setToolTip("이 스타일의 색·글꼴·배경·소품을 한 장으로 봅니다")
        style_btn.clicked.connect(self.show_style)
        self.style_row = self._row(self.style_combo, style_btn)
        form.addRow("스타일", self.style_row)
        layers_btn = QPushButton("레이어 고치기…")
        layers_btn.setToolTip("그래픽 장면의 도형·글자·그래프 레이어를 JSON으로 고칩니다 (Claude에게 맡겨도 됩니다)")
        layers_btn.clicked.connect(self.edit_layers)
        template_btn = QPushButton("템플릿에서…")
        template_btn.setToolTip("이 장면의 그림을 템플릿(제목, 목록, 큰 숫자, 해석 결과 …)에서 다시 만듭니다. 길이와 나레이션은 그대로입니다.")
        template_btn.clicked.connect(self.layers_from_template)
        self.layers_label = QLabel()
        self.layers_label.setObjectName("muted")
        self.layers_row = self._row(layers_btn, template_btn, self.layers_label)
        form.addRow("레이어", self.layers_row)

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

        self.scene_trans_combo = self._combo(SCENE_TRANSITIONS)
        self.scene_trans_combo.setMinimumWidth(10)
        self.scene_trans_combo.setToolTip("이 장면에서 다음 장면으로 넘어가는 방법")
        self.scene_trans_spin = QDoubleSpinBox()
        self.scene_trans_spin.setRange(0.0, 3.0)
        self.scene_trans_spin.setSingleStep(0.1)
        self.scene_trans_spin.setDecimals(1)
        self.scene_trans_spin.setSuffix(" 초")
        self.scene_trans_spin.setSpecialValueText("기본 시간")
        self.scene_trans_spin.setToolTip("전환에 걸리는 시간 (0이면 영상 설정의 시간)")
        form.addRow("다음 전환", self._row(self.scene_trans_combo, self.scene_trans_spin))

        apply_btn = QPushButton("맞춤·움직임을 모든 장면에 적용")
        apply_btn.setToolTip("이 장면의 맞춤과 움직임을 모든 장면에 똑같이 적용합니다")
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
        self.save_btn.setToolTip("음성·자막 없이 장면만 이어 붙인 영상을 저장합니다. MP4 말고 MOV, MKV, WebM, GIF로도 저장할 수 있습니다.")
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
        self.timeline.scenesMoved.connect(self._move_many)
        self.timeline.filesDropped.connect(self._place_files)
        self.timeline.sceneMenu.connect(self._scene_menu)
        self.timeline.cueDragged.connect(self._on_cue_dragged)
        self.timeline.cueActivated.connect(self.edit_cue)
        self.timeline.cueMenu.connect(self._cue_menu)
        self.timeline.soundDragged.connect(self._on_sound_dragged)
        self.timeline.soundActivated.connect(lambda i: self.play_sound(self.project.sounds[i].source))
        self.timeline.soundMenu.connect(self._sound_menu)
        self.timeline.musicClicked.connect(lambda: self.tabs.setCurrentWidget(self._tab_of(self.project_panel)))
        for combo in (self.res_combo, self.fps_combo, self.trans_combo):
            combo.currentIndexChanged.connect(self._project_settings_changed)
        self.trans_spin.valueChanged.connect(self._project_settings_changed)
        self.lines_spin.valueChanged.connect(self._project_settings_changed)
        self.gpu_check.toggled.connect(self._gpu_toggled)
        # Each box of the 장면 tab changes its own setting and nothing else (a box showing a rounded value
        # must not write it back when another one is used).
        for signal, field in (
            (self.kind_combo.currentIndexChanged, "kind"),
            (self.clip_start_spin.valueChanged, "clip_start"),
            (self.clip_end_spin.valueChanged, "clip_end"),
            (self.speed_spin.valueChanged, "speed"),
            (self.clip_fill_combo.currentIndexChanged, "clip_fill"),
            (self.reverse_check.toggled, "reverse"),
            (self.clip_audio_check.toggled, "clip_audio"),
            (self.clip_volume_spin.valueChanged, "clip_volume"),
            (self.fit_combo.currentIndexChanged, "fit"),
            (self.motion_combo.currentIndexChanged, "motion"),
            (self.color_btn.colorChanged, "color"),
            (self.gradient_check.toggled, "color2"),
            (self.color2_btn.colorChanged, "color2"),
            (self.duration_spin.valueChanged, "duration"),
            (self.scene_trans_combo.currentIndexChanged, "transition"),
            (self.scene_trans_spin.valueChanged, "transition_duration"),
            (self.style_combo.currentIndexChanged, "style"),
        ):
            signal.connect(lambda *_, f=field: self._prop_edited(f))
        self.text_panel.edited.connect(self._texts_edited)
        self.text_panel.selected.connect(lambda _: self._update_guides())
        self.text_panel.styleToAll.connect(self._text_style_to_all)
        self.overlay_panel.edited.connect(self._overlays_edited)
        self.overlay_panel.selected.connect(lambda _: self._update_guides())
        for panel in (self.text_panel, self.overlay_panel):
            panel.seek.connect(lambda at: self._seek_anywhere(self._scene_start(self.current) + at))
        self.mask_panel.edited.connect(self._masks_edited)
        self.mask_panel.selected.connect(lambda _: self._update_guides())
        self.adjust_panel.edited.connect(self._adjust_edited)
        self.adjust_panel.toAll.connect(self._adjust_to_all)
        self.project_panel.edited.connect(self._project_panel_edited)
        self.project_panel.resetCues.connect(self.reset_cues)
        self.screen.picked.connect(self._on_screen_picked)
        self.screen.moved.connect(self._on_screen_moved)
        self.screen.resized.connect(self._on_screen_resized)
        self.screen.reshaped.connect(self._on_screen_reshaped)
        self.tabs.currentChanged.connect(lambda _: self._update_guides())

    def _make_shortcuts(self) -> None:
        def key(keys, slot, where: QWidget = self, context=Qt.ShortcutContext.WindowShortcut):
            shortcut = QShortcut(QKeySequence(keys), where)
            shortcut.setContext(context)
            shortcut.activated.connect(slot)

        # Anywhere on the page; boxes being typed into keep their own Ctrl+Z, Space and the like.
        for keys, slot in (
            ("Ctrl+O", self.add_images),
            ("Ctrl+S", self.save_project),
            ("Ctrl+Return", self.send_to_compose),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+Shift+Z", self.redo),
            ("Space", self.toggle_play),
            ("Ctrl+B", self.split_current),
            ("Escape", self.close_player),
        ):
            key(keys, slot)
        # Only with the preview or the timeline in focus, so a list or box in the settings keeps its keys.
        area = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for keys, slot in (
            (QKeySequence.StandardKey.Delete, self.delete_scene),
            ("Ctrl+C", self.copy_scenes),
            ("Ctrl+X", self.cut_scenes),
            ("Ctrl+V", self.paste_scenes),
            ("Ctrl+D", self.duplicate_scene),
            ("Ctrl+A", self.select_all),
            ("Left", lambda: self.step_playhead(-self._frame())),
            ("Right", lambda: self.step_playhead(self._frame())),
            ("Shift+Left", lambda: self.step_playhead(-1.0)),
            ("Shift+Right", lambda: self.step_playhead(1.0)),
            ("Up", lambda: self.step_scene(-1)),
            ("Down", lambda: self.step_scene(1)),
            ("Home", lambda: self._seek_anywhere(0.0)),
            ("End", lambda: self._seek_anywhere(max(0.0, self.project.duration - self._frame()))),
        ):
            for where in (self.preview_panel, self.timeline_panel):
                key(keys, slot, where, area)

    # ---- undo ---------------------------------------------------------------

    def _snapshot(self) -> Snapshot:
        return Snapshot(self.project.to_json(), self.current, tuple(sorted(self.selection)))

    def _commit(self, key: str | None = None) -> None:
        """Remember the project as it is now, after a change, for undo (and save it shortly)."""
        if self._restoring:
            return
        self.history.record(self._snapshot(), key)
        self._update_undo_buttons()
        self.autosave_timer.start()

    def _autosave(self) -> None:
        """Save the project as it is, so a crash loses at most a few seconds; now and then keep a dated copy."""
        text = self.project.to_json()
        try:
            autosave.write(self.autosave_path, text)
            if time.monotonic() - self._last_backup >= BACKUP_EVERY:
                autosave.backup(text, "장면", ".json")
                self._last_backup = time.monotonic()
        except OSError as e:
            if not self._autosave_failed:  # said once, not on every change
                log_error(traceback.format_exc())
                self._show_status(f"자동 저장을 하지 못했습니다: {e}")
            self._autosave_failed = True
        else:
            self._autosave_failed = False

    def _update_undo_buttons(self) -> None:
        self.undo_btn.setEnabled(self.history.can_undo())
        self.redo_btn.setEnabled(self.history.can_redo())

    def undo(self) -> None:
        snapshot = self.history.undo()
        if snapshot is not None:
            self._restore(snapshot)
            self._show_status("되돌렸습니다. (다시 실행: Ctrl+Y)")

    def redo(self) -> None:
        snapshot = self.history.redo()
        if snapshot is not None:
            self._restore(snapshot)
            self._show_status("다시 실행했습니다.")

    def _restore(self, snapshot: Snapshot) -> None:
        self._restoring = True
        try:
            voice = self.project.voice
            self.project = Project.from_json(snapshot.project)
            if self.project.voice != voice:
                self._read_voice_file()
            self._load_project_controls()
            self.selection = {i for i in snapshot.selection if 0 <= i < len(self.project.scenes)}
            self._refresh_timeline()
            self._select_scene(min(snapshot.current, len(self.project.scenes) - 1), self.playhead, keep_selection=True)
        finally:
            self._restoring = False
        self._update_undo_buttons()
        self.autosave_timer.start()  # what is on screen now is what a crash should bring back

    # ---- project & list -----------------------------------------------------

    def _apply_project(self, project: Project) -> None:
        self._stop_player()  # a preview of the old project must not keep playing over the new one
        self.project = project
        self._read_voice_file()
        self._load_project_controls()
        self.playhead = 0.0
        self.timeline.fit_mode = True  # a new project starts zoomed to fit
        self.selection = set()
        self._reload_scenes(0 if project.scenes else -1)

    def _load_project_controls(self) -> None:
        """The project-wide settings into their boxes."""
        project = self.project
        self._loading = True
        select(self.res_combo, project.resolution)
        select(self.fps_combo, project.frame_rate)
        select(self.trans_combo, project.transition)
        self.trans_spin.setValue(project.transition_duration)
        self.lines_spin.setValue(project.lines_per_scene)
        self._loading = False
        self.trans_spin.setEnabled(project.transition != "none")
        self.project_panel.load(project, len(self._file_cues))
        self._update_voice_label()
        self._queue_waves()

    def _project_settings_changed(self) -> None:
        if self._loading:
            return
        resolution = self.res_combo.currentData()
        changed_size = resolution != self.project.resolution
        self.project.resolution = resolution
        self.project.fps = self.fps_combo.currentData()
        self.project.transition = self.trans_combo.currentData()
        self.project.transition_duration = self.trans_spin.value()
        self.project.lines_per_scene = self.lines_spin.value()
        self.trans_spin.setEnabled(self.project.transition != "none")
        if changed_size:
            self._refresh_timeline()
        else:
            self._update_timeline()
        self._update_summary()
        self.preview_timer.start()
        self._commit(f"project:{id(self.sender())}")

    def _gpu_toggled(self, on: bool) -> None:
        if on:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                label = encoder.gpu_label()
            finally:
                QApplication.restoreOverrideCursor()
            if label is None:
                self.gpu_check.setChecked(False)
                QMessageBox.information(self, APP_TITLE, "이 PC에서는 그래픽 카드 인코더를 쓸 수 없어서 CPU로 만듭니다.")
                return
            self._show_status(f"{label} 그래픽 카드로 인코딩합니다.")
        encoder.use_gpu = on
        self.shared.setValue("gpu", on)

    def showEvent(self, event):
        super().showEvent(event)
        if self.gpu_check.isChecked() != encoder.use_gpu:  # changed in the compose tab
            self.gpu_check.blockSignals(True)
            self.gpu_check.setChecked(encoder.use_gpu)
            self.gpu_check.blockSignals(False)

    def _thumb_size(self) -> tuple[int, int]:
        w, h = self.project.size
        scale = min(THUMB_SIZE.width() / w, THUMB_SIZE.height() / h)
        return max(2, round(w * scale)), max(2, round(h * scale))

    def _thumb_key(self, scene: Scene) -> str:
        data = dataclasses.asdict(scene)
        # Thumbnails show a scene's first frame, which these do not change (played backwards, it is the end's).
        for name in ("duration", "narration", "clip_audio", "clip_volume", "speed", "clip_fill", "transition",
                     "transition_duration", *(() if scene.reverse else ("clip_end",))):  # fmt: skip
            data.pop(name)
        if scene.kind == "graphic":  # a changed style file changes the picture too
            try:
                data["style_hash"] = graphics.style_hash(scene.style)
            except ValueError:
                data["style_hash"] = ""
        return json.dumps([self.project.resolution, data], sort_keys=True, ensure_ascii=False)

    def _reload_scenes(self, row: int, keep_selection: bool = False) -> None:
        """After scenes were added, removed or reordered: redraw the timeline and select `row`."""
        self._refresh_timeline()
        self._select_scene(min(row, len(self.project.scenes) - 1), keep_selection=keep_selection)

    def _update_timeline(self) -> None:
        self.timeline.set_data(self.project, self.voice_cues, self.current, self.playhead, self.selection)

    def _refresh_timeline(self) -> None:
        """Bring the timeline up to date after any change, queueing pictures it does not have yet."""
        for scene in self.project.scenes:
            if scene.kind == "clip" and scene.source:
                key = self._strip_key(scene.source)
                if key not in self._strips and key not in self._strips_making and self._clip_length(scene.source):
                    self._strip_queue[scene.source] = self._clip_length(scene.source)
            elif (scene.kind == "image" and scene.source) or scene.kind == "graphic":
                key = self._thumb_key(scene)
                if key not in self.thumbs:
                    self._thumb_queue[key] = scene
        if self._thumb_queue:
            self.thumb_timer.start()
        if self._strip_queue:
            self._start_strips()
        self._update_timeline()
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

    def _queue_waves(self) -> None:
        """Loudness outlines of the voice and the music, for the timeline."""
        for path in (self.project.voice, self.project.music, *(s.source for s in self.project.sounds)):
            if path and path not in self._waves and path not in self._wave_queue and Path(path).is_file():
                self._wave_queue.append(path)
        if self._wave_queue and self.wave_thread is None:
            self.wave_thread = waveform.WaveThread(self._wave_queue, self)
            self._wave_queue = []
            self.wave_thread.ready.connect(self._wave_ready)
            self.wave_thread.finished.connect(self._waves_finished)
            self.wave_thread.start()

    def _wave_ready(self, path: str, peaks) -> None:
        self._waves[path] = peaks
        self.timeline.update()

    def _waves_finished(self) -> None:
        self.wave_thread.deleteLater()
        self.wave_thread = None
        if self._wave_queue:
            self._queue_waves()

    def _transition_label(self, index: int) -> str:
        """How scene `index` leads into the next, for the timeline's marks ("" for a straight cut)."""
        name, seconds = transition_at(self.project, index)
        return "" if name == "none" else f"{TRANSITION_NAMES.get(name, name)} {seconds:.1f}초"

    def _update_summary(self) -> None:
        scenes = self.project.scenes
        if not scenes:
            self.summary_label.setText("장면이 없습니다. 음성을 보내거나 이미지·영상을 추가하세요.")
            return
        parts = [f"장면 {len(scenes)}개 · 영상 {format_time(self.project.duration, precise=True)}"]
        if len(self.selection) > 1:
            parts.append(f"{len(self.selection)}개 고름")
        blanks = sum(s.is_blank for s in scenes)
        if blanks:
            parts.append(f"그림 없는 장면 {blanks}개")
        if self.voice_cues:
            target = self.voice_duration + TAIL
            gap = self.project.duration - target
            if abs(gap) > 0.05:
                parts.append(f"음성보다 {abs(gap):.1f}초 {'김' if gap > 0 else '짧음'}")
        self.summary_label.setText(" · ".join(parts))

    def _chosen(self) -> list[int]:
        """The selected scenes in order (at least the current one)."""
        chosen = sorted(i for i in self.selection if 0 <= i < len(self.project.scenes))
        if not chosen and 0 <= self.current < len(self.project.scenes):
            chosen = [self.current]
        return chosen

    def _move_many(self, indices: list[int], at: int) -> None:
        """Move the scenes at `indices` (kept in order) to before position `at` of the current list."""
        scenes = self.project.scenes
        indices = sorted(i for i in indices if 0 <= i < len(scenes))
        if not indices:
            return
        moving = set(indices)
        rest = [s for i, s in enumerate(scenes) if i not in moving]
        place = at - sum(1 for i in indices if i < at)
        current = self.current
        self.project.scenes = rest[:place] + [scenes[i] for i in indices] + rest[place:]
        self.selection = set(range(place, place + len(indices)))
        new_current = place + indices.index(current) if current in moving else place
        self._reload_scenes(new_current, keep_selection=True)
        self._commit()

    def move_scene(self, step: int) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        if step < 0 and chosen[0] > 0:
            self._move_many(chosen, chosen[0] - 1)
        elif step > 0 and chosen[-1] < len(self.project.scenes) - 1:
            self._move_many(chosen, chosen[-1] + 2)

    def duplicate_scene(self) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        copies = [copy_scene(self.project.scenes[i]) for i in chosen]
        self._insert(chosen[-1] + 1, copies)
        self._show_status(f"장면 {len(copies)}개를 복제했습니다.")

    def _insert(self, at: int, scenes: list[Scene]) -> None:
        self.project.scenes[at:at] = scenes
        self.selection = set(range(at, at + len(scenes)))
        self._reload_scenes(at, keep_selection=True)
        self._commit()

    def delete_scene(self) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        for i in reversed(chosen):
            del self.project.scenes[i]
        self.selection = set()
        self._reload_scenes(min(chosen[0], len(self.project.scenes) - 1))
        self._commit()
        if len(chosen) > 1:
            self._show_status(f"장면 {len(chosen)}개를 지웠습니다. (되돌리기: Ctrl+Z)")

    def select_all(self) -> None:
        if self.project.scenes:
            self.selection = set(range(len(self.project.scenes)))
            self._select_scene(max(0, self.current), self.playhead, keep_selection=True)

    def copy_scenes(self) -> None:
        chosen = self._chosen()
        if not chosen:
            return
        data = json.dumps([dataclasses.asdict(self.project.scenes[i]) for i in chosen], ensure_ascii=False)
        mime = QMimeData()
        mime.setData(SCENES_MIME, data.encode("utf-8"))
        mime.setText(f"[동영상 편집기] 장면 {len(chosen)}개")
        QApplication.clipboard().setMimeData(mime)
        self._show_status(f"장면 {len(chosen)}개를 복사했습니다. 붙여넣기: Ctrl+V")

    def cut_scenes(self) -> None:
        if self._chosen():
            self.copy_scenes()
            self.delete_scene()

    def paste_scenes(self) -> None:
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return
        at = self.current + 1 if self.current >= 0 else len(self.project.scenes)
        if mime.hasFormat(SCENES_MIME):
            try:
                raw = json.loads(bytes(mime.data(SCENES_MIME)).decode("utf-8"))
                scenes = [from_dict(Scene, item) for item in raw]
            except (ValueError, TypeError):
                return
            self._insert(at, scenes)
            self._show_status(f"장면 {len(scenes)}개를 붙여넣었습니다.")
        elif mime.hasUrls():  # files copied in Explorer
            files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
            self._place_files(files, -1)

    def split_current(self) -> None:
        """Cut the current scene in two at the playhead."""
        i = self.current
        if not 0 <= i < len(self.project.scenes):
            return
        scene = self.project.scenes[i]
        at = self.playhead - self._scene_start(i)
        if at < MIN_DURATION - 1e-6 or scene.duration - at < MIN_DURATION - 1e-6:
            self._show_status(f"나눌 곳에 빨간 선을 두고 누르세요 (양쪽이 {MIN_DURATION}초 이상이어야 합니다).")
            return
        length = self._clip_length(scene.source) if scene.kind == "clip" and scene.source else 0.0
        first, second = split_scene(scene, at, length)
        self.project.scenes[i : i + 1] = [first, second]
        self.selection = {i + 1}
        self._refresh_timeline()
        self._select_scene(i + 1, self.playhead, keep_selection=True)
        self._commit()
        self._show_status(f"{i + 1}번 장면을 {at:.1f}초 지점에서 둘로 나눴습니다.")

    def add_title(self) -> None:
        at = self.current + 1 if self.current >= 0 else len(self.project.scenes)
        box = TextBox(text="제목", style=self._new_text_style(), y=0.5)
        self._insert(at, [Scene(kind="color", texts=[box], color="#1e3a8a", color2="#0f172a")])
        self.tabs.setCurrentWidget(self._tab_of(self.text_panel))
        self.text_panel.select(0)
        self.text_panel.text_edit.setFocus()
        self.text_panel.text_edit.selectAll()

    def _tab_of(self, panel: QWidget) -> QWidget:
        """The tab page holding `panel`."""
        for i in range(self.tabs.count()):
            scroll = self.tabs.widget(i)
            if scroll.widget().isAncestorOf(panel):
                return scroll
        return self.tabs.widget(0)

    def _new_text_style(self) -> TextStyle:
        """New words look like the ones in the current scene."""
        if 0 <= self.current < len(self.project.scenes) and self.project.scenes[self.current].texts:
            return copy.deepcopy(self.project.scenes[self.current].texts[-1].style)
        return TextStyle()

    def record_screen(self) -> None:
        """Record part of the screen and put the recording in as a clip scene."""
        if self.recorder is not None:
            return
        width, height = self.project.size
        self.recorder = ScreenRecorder(width / height, self.store, self, fps=max(30, self.project.frame_rate))
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
        notes = getattr(self.recorder, "notes", [])
        self.recorder.deleteLater()
        self.recorder = None
        self._restore_window()
        if path is None:
            self._show_status("화면 녹화를 취소했습니다.")
            return
        scenes = self.project.scenes
        try:
            with_sound = media.probe(path).has_audio
        except (OSError, ValueError):
            with_sound = False
        if 0 <= self.current < len(scenes) and scenes[self.current].is_blank:
            at = self.current  # fills the blank scene, keeping its length and narration
            scenes[at] = self._with_source(scenes[at], path, "clip")
        else:
            at = self.current + 1 if self.current >= 0 else len(scenes)
            scenes.insert(at, self._with_source(Scene(duration=DEFAULT_DURATION), path, "clip"))
        scenes[at].clip_audio = with_sound  # the recorded sound is kept
        self.selection = set()
        self._reload_scenes(at)
        self._commit()
        self._show_status(f"녹화한 영상을 장면에 넣었습니다: {path}" + (f" ({' '.join(notes)})" if notes else ""))

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
        # A new picture keeps the scene's words, overlays and colour, but not the old one's turning and cropping.
        scene = dataclasses.replace(scene, kind=kind, source=str(path), clip_start=0.0, clip_end=0.0, fit=fit,
                                    rotate=0, flip_h=False, flip_v=False, crop=[0.0, 0.0, 0.0, 0.0])  # fmt: skip
        if kind == "clip" and not scene.narration:
            length = self._clip_length(str(path))
            if length:
                scene.duration = max(MIN_DURATION, length)
        return scene

    def _place_files(self, files: list[Path], row: int, at: float | None = None) -> None:
        """Files dropped on a scene replace its picture and then fill the blank scenes after it.

        Otherwise they fill blank scenes from the current one on; whatever is left becomes new scenes.
        Sound files become sound effects at `at` seconds (where they were dropped), or at the playhead.
        """
        sounds = [p for p in files if p.suffix.lower() in media.AUDIO_SUFFIXES]
        files = [p for p in files if p not in sounds]
        added_sounds = 0
        for path in sounds:
            if self._sound_length(str(path)) > 0:
                self.add_sound(str(path), self.playhead if at is None else at, record=False)
                added_sounds += 1
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
                    scenes.append(self._with_source(Scene(duration=DEFAULT_DURATION), path, kind))
                    target = len(scenes) - 1
                else:
                    scenes[target] = self._with_source(scenes[target], path, kind)
                first = target if first is None else first
                cursor = target + 1
        finally:
            QApplication.restoreOverrideCursor()
        if first is not None:
            self.selection = set()
            self._reload_scenes(first)
            self._show_status(f"{len(items)}개 파일을 장면에 넣었습니다.")
        if first is not None or added_sounds:
            self._commit()  # one undo step for everything dropped together
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
            )
        self._refresh_timeline()
        self._commit()
        self._show_status(f"{len(self.project.scenes)}개 장면에 같은 맞춤·움직임을 적용했습니다.")

    # ---- choosing scenes ----------------------------------------------------

    def _select_scene(self, row: int, at: float | None = None, keep_selection: bool = False) -> None:
        """Make `row` the scene being edited; the playhead goes to `at`, or to the scene's start."""
        if row != self.current:
            self.screen.clear_guides()  # the outlines of the last scene's words must not be dragged
        self.current = row
        has = 0 <= row < len(self.project.scenes)
        if not keep_selection or not self.selection:
            self.selection = {row} if has else set()
        self.selection = {i for i in self.selection if 0 <= i < len(self.project.scenes)}
        if has:
            self.selection.add(row)
        for panel in (self.props_box, self.text_panel, self.overlay_panel):
            panel.setEnabled(has)
        for b in (self.up_btn, self.down_btn, self.copy_btn, self.split_btn, self.delete_btn):
            b.setEnabled(has)
        if has:
            self._load_props(self.project.scenes[row])
            self.playhead = self._scene_start(row) if at is None else at
        else:
            self.playhead = 0.0
            self.adjust_panel.load(None)
        if has and self.play_bar.isVisible():
            self.player.setPosition(round(self.playhead * 1000))  # jump the playing preview there
        self._update_selection_label()
        self._update_timeline()
        self._update_summary()
        self._on_playhead_changed()
        if not self._restoring:
            self.history.note_selection(self.current, tuple(sorted(self.selection)), self.project.to_json())

    def _update_selection_label(self) -> None:
        count = len(self.selection)
        if count > 1:
            self.selection_label.setText(f"장면 {count}개를 골랐습니다. [장면]·[보정] 탭에서 바꾼 값은 모두에 적용됩니다.")
        elif 0 <= self.current < len(self.project.scenes):
            self.selection_label.setText(f"{self.current + 1}번 장면")
        else:
            self.selection_label.setText("장면을 고르세요")

    def step_playhead(self, seconds: float) -> None:
        if self.project.scenes:
            self._seek_anywhere(min(max(0.0, self.playhead + seconds), max(0.0, self.project.duration - self._frame())))

    def _frame(self) -> float:
        """One frame of the video, in seconds."""
        return 1 / self.project.frame_rate

    def step_scene(self, step: int) -> None:
        if not self.project.scenes:
            return
        row = min(max(0, self.current + step), len(self.project.scenes) - 1)
        self._select_scene(row)
        self.timeline.ensure_visible(self.playhead)

    def _seek_anywhere(self, at: float) -> None:
        self._on_timeline_seeked(at)
        self.timeline.ensure_visible(at)

    # ---- the timeline -------------------------------------------------------

    def _on_timeline_clicked(self, index: int, at: float, modifiers: int) -> None:
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier.value)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier.value)
        if ctrl:
            if index in self.selection and len(self.selection) > 1:
                # Taken out of the selection; the scene being edited stays, or the nearest chosen one takes over.
                self.selection.discard(index)
                if self.current in self.selection:
                    self._select_scene(self.current, self.playhead, keep_selection=True)
                else:
                    self._select_scene(min(self.selection, key=lambda i: abs(i - index)), keep_selection=True)
                return
            self.selection.add(index)
            self._select_scene(index, at, keep_selection=True)
        elif shift and self.current >= 0:
            low, high = sorted((self.current, index))
            self.selection = set(range(low, high + 1))
            self._select_scene(index, at, keep_selection=True)
        elif index != self.current or len(self.selection) > 1:
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
        if not 0 <= index < len(scenes):
            return
        scenes[index] = dataclasses.replace(scenes[index], duration=duration)
        if index == self.current:
            self._loading = True
            self.duration_spin.setValue(duration)
            self._loading = False
            self._update_props_visibility(scenes[index])
        self._update_timeline()
        self._update_summary()
        if finished:
            self._refresh_timeline()
            self._on_playhead_changed()
            self._commit()

    def _on_timeline_trim(self, index: int, start: float, end: float, finished: bool) -> None:
        scenes = self.project.scenes
        if not 0 <= index < len(scenes):
            return
        scenes[index] = dataclasses.replace(scenes[index], clip_start=start, clip_end=end)
        if index != self.current:
            self._select_scene(index, self.playhead)
        self._loading = True
        self.clip_start_spin.setValue(start)
        self.clip_end_spin.setValue(end)
        self._loading = False
        self._update_props_visibility(scenes[index])
        self._update_timeline()
        if finished:
            self._scrub = None
            self._refresh_timeline()
            self._seek(self._scene_start(index))  # show where the part now begins
            self._commit()

    def _on_timeline_scrub(self, index: int, moment: float) -> None:
        self._scrub = moment  # the preview shows this moment of the clip while a handle is dragged
        self.preview_timer.start()

    def _scene_menu(self, index: int, pos) -> None:
        menu = QMenu(self)
        has = index >= 0
        count = len(self._chosen()) if has else 0
        what = f"장면 {count}개" if count > 1 else "장면"
        for text, slot, enabled in (
            (f"{what} 복사 (Ctrl+C)", self.copy_scenes, has),
            (f"{what} 잘라내기 (Ctrl+X)", self.cut_scenes, has),
            ("붙여넣기 (Ctrl+V)", self.paste_scenes, True),
            (f"{what} 복제 (Ctrl+D)", self.duplicate_scene, has),
            ("빨간 선에서 나누기 (Ctrl+B)", self.split_current, has and count == 1),
            (f"{what} 삭제 (Delete)", self.delete_scene, has),
        ):
            action = menu.addAction(text, slot)
            action.setEnabled(enabled)
        menu.addSeparator()
        menu.addAction("모든 장면 고르기 (Ctrl+A)", self.select_all)
        menu.exec(pos)

    # ---- subtitles ------------------------------------------------------------

    @property
    def voice_cues(self) -> list[Cue]:
        """The subtitles the scenes and the preview follow: edited ones, or the voice's own file."""
        return self.project.cues if self.project.cues is not None else self._file_cues

    def _editable_cues(self) -> list[Cue]:
        if self.project.cues is None:
            self.project.cues = [Cue(c.start, c.end, c.text) for c in self._file_cues]
        return self.project.cues

    def _on_cue_dragged(self, index: int, start: float, end: float, finished: bool) -> None:
        if not 0 <= index < len(self.voice_cues):
            return
        cue = self.voice_cues[index]
        if self.project.cues is None and (start, end) == (cue.start, cue.end):
            return  # pressed without moving: the subtitles stay the voice's own
        cues = self._editable_cues()
        cues[index] = Cue(start, end, cues[index].text)
        self._update_timeline()
        if finished:
            self._cues_changed()

    def edit_cue(self, index: int) -> None:
        cues = self.voice_cues
        if not 0 <= index < len(cues):
            return
        cue = cues[index]
        # Neighbours that already overlap it must not push its times when nothing is changed.
        low = min(cues[index - 1].end, cue.start) if index > 0 else 0.0
        high = max(cues[index + 1].start, cue.end) if index + 1 < len(cues) else max(self.project.duration, cue.end) + 60
        dialog = CueDialog(cues[index], low, high, self)
        result = dialog.exec()
        if result == CueDialog.DELETE:
            self.delete_cue(index)
        elif result:  # accepted
            cue = dialog.result_cue()
            if cue.text:
                self._editable_cues()[index] = cue
                self._cues_changed()
            else:
                self.delete_cue(index)

    def delete_cue(self, index: int) -> None:
        cues = self._editable_cues()
        if 0 <= index < len(cues):
            del cues[index]
            self._cues_changed()

    def _cue_menu(self, index: int, pos) -> None:
        menu = QMenu(self)
        menu.addAction("자막 고치기…", lambda: self.edit_cue(index))
        menu.addAction("이 자막 지우기", lambda: self.delete_cue(index))
        if self.project.cues is not None:
            menu.addSeparator()
            menu.addAction("자막을 원래대로", self.reset_cues)
        menu.exec(pos)

    def reset_cues(self) -> None:
        if self.project.cues is not None:
            self.project.cues = None
            self._cues_changed()
            self._show_status("자막을 음성의 자막 파일 그대로 되돌렸습니다.")

    def _cues_changed(self) -> None:
        self._update_timeline()
        self._update_summary()
        self._update_voice_label()
        self.project_panel.load(self.project, len(self._file_cues))
        self._commit()

    def _edited_subtitle(self, folder: Path, name: str) -> Path | None:
        """The edited subtitles written out as SRT, or None when the voice's own file is used."""
        if self.project.cues is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        subs.write_srt(self.project.cues, path)
        return path

    # ---- sound effects ----------------------------------------------------------

    def _sound_add_menu(self, parent: QWidget, moment: Callable[[], float]) -> QMenu:
        """The built-in effects to put in at `moment()`, a file of one's own, and the built-ins to listen to."""
        menu = QMenu(parent)
        for name, (label, _) in sfx.BUILTIN.items():
            menu.addAction(label, lambda n=name: self._add_builtin_sound(n, moment()))
        menu.addSeparator()
        menu.addAction("파일에서 고르기…", lambda: self._choose_sound(moment()))
        listen = menu.addMenu("들어보기")
        for name, (label, _) in sfx.BUILTIN.items():
            listen.addAction(label, lambda n=name: self._listen_builtin(n))
        return menu

    def _builtin_sound(self, name: str) -> str | None:
        try:
            return str(sfx.path(name))
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, f"효과음을 만들지 못했습니다.\n{e}")
            return None

    def _add_builtin_sound(self, name: str, at: float) -> None:
        path = self._builtin_sound(name)
        if path:
            self.add_sound(path, at)

    def _listen_builtin(self, name: str) -> None:
        path = self._builtin_sound(name)
        if path:
            self.play_sound(path)

    def _choose_sound(self, at: float) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "효과음 고르기", str(self.store.value("sound_dir", "")), AUDIO_FILTER)
        if not name:
            return
        self.store.setValue("sound_dir", str(Path(name).parent))
        if self._sound_length(name) <= 0:
            QMessageBox.warning(self, APP_TITLE, f"소리 파일로 열 수 없습니다: {Path(name).name}")
            return
        self.add_sound(name, at)

    def add_sound(self, path: str, at: float, record: bool = True) -> None:
        self.project.sounds.append(SoundEffect(source=path, at=round(max(0.0, at), 3)))
        self._queue_waves()
        self._update_timeline()
        if record:
            self._commit()
        self._show_status(f"효과음 '{Path(path).stem}'을 {format_time(at, precise=True)}에 넣었습니다. 타임라인에서 끌어 옮길 수 있습니다.")

    def _sound_length(self, source: str) -> float:
        key = self._strip_key(source)
        if key not in self._sound_lengths:
            try:
                info = media.probe(source)
                self._sound_lengths[key] = info.duration if info.has_audio else 0.0
            except (OSError, ValueError):
                self._sound_lengths[key] = 0.0
        return self._sound_lengths[key]

    def play_sound(self, source: str) -> None:
        self.sound_player.stop()
        self.sound_player.setSource(QUrl.fromLocalFile(source))
        self.sound_player.play()

    def _on_sound_dragged(self, index: int, at: float, finished: bool) -> None:
        if 0 <= index < len(self.project.sounds):
            self.project.sounds[index].at = at
            self._update_timeline()  # widens the timeline when it is dragged past the end
            if finished:
                self._commit()

    def _sound_menu(self, index: int, at: float, pos) -> None:
        if not 0 <= index < len(self.project.sounds):
            menu = self._sound_add_menu(self, lambda: at)
            menu.setTitle("여기에 효과음 넣기")
            menu.exec(pos)
            menu.deleteLater()
            return
        sound = self.project.sounds[index]
        menu = QMenu(self)
        menu.addAction("들어보기", lambda: self.play_sound(sound.source))
        menu.addAction(f"소리 크기… (지금 {round(sound.volume * 100)}%)", lambda: self._set_sound_volume(index))
        menu.addAction("빨간 선 위치로 옮기기", lambda: self._on_sound_dragged(index, round(self.playhead, 3), True))
        menu.addSeparator()
        menu.addAction("지우기", lambda: self.delete_sound(index))
        menu.exec(pos)
        menu.deleteLater()

    def _set_sound_volume(self, index: int) -> None:
        sound = self.project.sounds[index]
        value, ok = QInputDialog.getInt(self, APP_TITLE, f"'{Path(sound.source).stem}' 효과음 크기 (%)",
                                        round(sound.volume * 100), 0, 300, 10)  # fmt: skip
        if ok:
            sound.volume = value / 100
            self.timeline.update()
            self._commit()

    def delete_sound(self, index: int) -> None:
        if 0 <= index < len(self.project.sounds):
            del self.project.sounds[index]
            self._update_timeline()
            self._commit()

    # ---- the settings panels --------------------------------------------------

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
        self.reverse_check.setChecked(scene.reverse)
        self.clip_audio_check.setChecked(scene.clip_audio)
        self.clip_volume_spin.setValue(round(scene.clip_volume * 100))
        select(self.fit_combo, scene.fit)
        select(self.motion_combo, scene.motion)
        self.color_btn.set_color(scene.color)
        self.gradient_check.setChecked(bool(scene.color2))
        self.color2_btn.set_color(scene.color2 or "#000000")
        self.duration_spin.setValue(scene.duration)
        select(self.scene_trans_combo, scene.transition if scene.transition in dict(SCENE_TRANSITIONS) else "")
        self.scene_trans_spin.setValue(scene.transition_duration)
        # Styles may have been added (by Claude) since the list was filled.
        self.style_combo.clear()
        self.style_combo.addItem(f"{graphics.DEFAULT} (기본)", "")
        for name in graphics.style_names():
            if name != graphics.DEFAULT:
                self.style_combo.addItem(name, name)
        if scene.style and self.style_combo.findData(scene.style) < 0:
            self.style_combo.addItem(f"{scene.style} (없음)", scene.style)
        select(self.style_combo, scene.style)
        self.layers_label.setText(f"{len(scene.layers)}개")
        self._loading = False
        self._update_props_visibility(scene)
        self.text_panel.show_items(scene.texts)
        self.overlay_panel.show_items(scene.overlays)
        self.mask_panel.show_items(scene.masks)
        self.adjust_panel.load(scene)

    def _update_props_visibility(self, scene: Scene) -> None:
        kind = scene.kind
        clip = kind == "clip" and bool(scene.source)
        self.form.setRowVisible(self.file_row, kind in ("image", "clip"))
        for row in (self.clip_row, self.speed_row, self.clip_fill_combo, self.reverse_check, self.clip_info_label,
                    self.clip_tools_row, self.clip_sound_row):  # fmt: skip
            self.form.setRowVisible(row, clip)
        self.clip_volume_spin.setEnabled(scene.clip_audio)
        self.form.setRowVisible(self.fit_combo, kind in ("image", "clip"))
        self.form.setRowVisible(self.motion_combo, kind == "image")
        self.form.setRowVisible(self.color_row, kind == "color" or (kind != "graphic" and not scene.source))
        for row in (self.style_row, self.layers_row):
            self.form.setRowVisible(row, kind == "graphic")
        self.color2_btn.setEnabled(self.gradient_check.isChecked())
        self.speed_row.setEnabled(scene.clip_fill != "stretch")
        self.scene_trans_spin.setEnabled(scene.transition != "none")
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

    def edit_layers(self) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        scene = self.project.scenes[self.current]
        dialog = LayersDialog(scene.layers, scene.style, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.layers != scene.layers:
            self._set_props({"layers": dialog.layers}, everywhere=False)
            self._load_props(self.project.scenes[self.current])

    def _template_dialog(self, style_name: str) -> dict | None:
        dialog = TemplateDialog(style_name, self.project.size, self)
        return dialog.filled if dialog.exec() == QDialog.DialogCode.Accepted else None

    def layers_from_template(self) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        scene = self.project.scenes[self.current]
        filled = self._template_dialog(scene.style or self.project.style)
        if filled is not None:
            self._set_props({"kind": "graphic", "layers": filled["layers"]}, everywhere=False)
            self._load_props(self.project.scenes[self.current])

    def add_from_template(self) -> None:
        filled = self._template_dialog(self.project.style)
        if filled is None:
            return
        at = self.current + 1 if self.current >= 0 else len(self.project.scenes)
        self._insert(at, [Scene(kind="graphic", style=self.project.style, layers=filled["layers"], duration=filled["duration"])])
        self.tabs.setCurrentIndex(0)

    def show_style(self) -> None:
        name = self.style_combo.currentData() or ""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            sheet = graphics.style_sheet(name or graphics.DEFAULT)
        except ValueError as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        viewer = FrameViewer(QPixmap.fromImage(sheet), self.window())
        viewer.setWindowTitle(f"스타일: {name or graphics.DEFAULT}")
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.exec()

    def _use_whole_clip(self) -> None:
        if 0 <= self.current < len(self.project.scenes):
            self._set_props({"clip_start": 0.0, "clip_end": 0.0}, everywhere=False)  # 0: to the end
            self._load_props(self.project.scenes[self.current])

    def _fit_scene_to_clip(self) -> None:
        """The current scene's length set to its clip part played at its speed (only this scene)."""
        if not (0 <= self.current < len(self.project.scenes)):
            return
        scene = self.project.scenes[self.current]
        length = self._clip_length(scene.source)
        if not length:
            return
        start, end = clip_part(scene, length)
        values = {"duration": max(MIN_DURATION, (end - start) / scene.speed)}
        if scene.clip_fill == "stretch":
            # The length now comes from the clip, not the other way round: play it at normal speed. The
            # speed box was greyed out in stretch mode and may still hold an old value.
            values = {"duration": max(MIN_DURATION, end - start), "clip_fill": "freeze", "speed": 1.0}
        self._set_props(values, everywhere=False)
        self._load_props(self.project.scenes[self.current])
        if scene.narration:
            self._show_status("장면 길이를 클립에 맞췄습니다. 음성 문장 길이와는 달라졌으니 필요하면 [음성에 맞추기]로 되돌리세요.")

    def _prop_value(self, field: str):
        """A scene setting as its box in the 장면 tab shows it now."""
        if field == "color2":
            return self.color2_btn.color() if self.gradient_check.isChecked() else ""
        if field == "clip_volume":
            return self.clip_volume_spin.value() / 100
        if field == "style":
            return self.style_combo.currentData() or ""
        widget = {
            "kind": self.kind_combo, "clip_start": self.clip_start_spin, "clip_end": self.clip_end_spin,
            "speed": self.speed_spin, "clip_fill": self.clip_fill_combo, "reverse": self.reverse_check,
            "clip_audio": self.clip_audio_check, "fit": self.fit_combo, "motion": self.motion_combo,
            "color": self.color_btn, "duration": self.duration_spin, "transition": self.scene_trans_combo,
            "transition_duration": self.scene_trans_spin,
        }[field]  # fmt: skip
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, ColorButton):
            return widget.color()
        return widget.value()

    def _prop_edited(self, field: str) -> None:
        if self._loading or not (0 <= self.current < len(self.project.scenes)):
            return
        self._set_props({field: self._prop_value(field)})

    def _set_props(self, values: dict, everywhere: bool = True) -> None:
        """Change settings of the current scene and, with several scenes chosen, the shared ones of those too."""
        scene = self.project.scenes[self.current]
        if "clip_start" in values or "clip_end" in values:
            # A start typed past the end (or the other way round) moves the other end along, when the
            # clip's length is known; 0 as the end means "to the end of the clip".
            length = self._clip_length(scene.source) if scene.source else 0.0
            start = values.get("clip_start", scene.clip_start)
            end = values.get("clip_end", scene.clip_end)
            if length and end > 0 and end - start < MIN_CLIP_PART:
                if "clip_start" in values:
                    values["clip_end"] = min(length, start + MIN_CLIP_PART)
                else:
                    values["clip_start"] = max(0.0, end - MIN_CLIP_PART)
        self.project.scenes[self.current] = scene = dataclasses.replace(scene, **values)
        shared = {f: v for f, v in values.items() if f in SHARED_FIELDS}
        if everywhere and shared and len(self.selection) > 1:
            for i in self.selection - {self.current}:
                if 0 <= i < len(self.project.scenes):
                    self.project.scenes[i] = dataclasses.replace(self.project.scenes[i], **copy.deepcopy(shared))
        self._update_props_visibility(scene)
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit(f"props:{self.current}:{','.join(sorted(values))}")

    def _apply_to_selection(self, old: Scene, new: Scene, fields: tuple[str, ...]) -> None:
        """Copy what changed between `old` and `new` (among `fields`) to the other selected scenes."""
        changed = {f: getattr(new, f) for f in fields if getattr(new, f) != getattr(old, f)}
        if not changed or len(self.selection) < 2:
            return
        for i in self.selection - {self.current}:
            if 0 <= i < len(self.project.scenes):
                self.project.scenes[i] = dataclasses.replace(self.project.scenes[i], **copy.deepcopy(changed))

    def _texts_edited(self, key: str) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        self.project.scenes[self.current].texts = self.text_panel.items()
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit(f"text:{self.current}:{key}")

    def _overlays_edited(self, key: str) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        self.project.scenes[self.current].overlays = self.overlay_panel.items()
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit(f"overlay:{self.current}:{key}")

    def _masks_edited(self, key: str) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        self.project.scenes[self.current].masks = self.mask_panel.items()
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit(f"mask:{self.current}:{key}")

    def _adjust_edited(self, key: str) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        values = self.adjust_panel.values()
        # Only what was touched, so values the boxes show rounded are not written over the others.
        fields = {"rotate": ("rotate",), "flip": ("flip_h", "flip_v"), "crop": ("crop",), "brightness": ("brightness",),
                  "contrast": ("contrast",), "saturation": ("saturation",), "look": ("look",)}.get(key, tuple(values))  # fmt: skip
        changed = {f: values[f] for f in fields}
        self.project.scenes[self.current] = dataclasses.replace(self.project.scenes[self.current], **changed)
        if len(self.selection) > 1:
            for i in self.selection - {self.current}:
                if 0 <= i < len(self.project.scenes):
                    self.project.scenes[i] = dataclasses.replace(self.project.scenes[i], **copy.deepcopy(changed))
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit(f"adjust:{self.current}:{key}")

    def _adjust_to_all(self) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        model = self.project.scenes[self.current]
        values = {f: getattr(model, f) for f in COLOUR_FIELDS}
        for i, scene in enumerate(self.project.scenes):
            if scene.kind in ("image", "clip"):
                self.project.scenes[i] = dataclasses.replace(scene, **values)
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit()
        self._show_status("이미지·영상 장면 모두에 같은 색 보정을 적용했습니다.")

    def _text_style_to_all(self, style: TextStyle) -> None:
        count = 0
        for scene in self.project.scenes:
            for box in scene.texts:
                box.style = copy.deepcopy(style)
                count += 1
        self._reload_scenes(self.current, keep_selection=True)
        self._commit()
        self._show_status(f"글자 {count}개에 같은 모양을 적용했습니다.")

    def _project_panel_edited(self, key: str) -> None:
        self.project_panel.apply(self.project)
        if key in ("music", "logo"):
            self._queue_waves()
            self.preview_timer.start()
        if key.startswith("logo"):
            self.preview_timer.start()
        self.timeline.update()
        self._commit(f"panel:{key}")

    def _ask_overlay_file(self, kind: str) -> str | None:
        file_filter = IMAGE_FILTER if kind == "image" else VIDEO_FILTER
        name, _ = QFileDialog.getOpenFileName(self, "겹칠 그림" if kind == "image" else "겹칠 영상",
                                              str(self.store.value("open_dir", "")), file_filter)  # fmt: skip
        if not name:
            return None
        self.store.setValue("open_dir", str(Path(name).parent))
        if not self._can_open(Path(name), kind):
            QMessageBox.warning(self, APP_TITLE, f"열 수 없는 파일입니다: {Path(name).name}")
            return None
        return name

    def _ask_project_file(self, kind: str) -> str | None:
        file_filter = AUDIO_FILTER if kind == "music" else IMAGE_FILTER
        name, _ = QFileDialog.getOpenFileName(self, "배경 음악" if kind == "music" else "로고 그림",
                                              str(self.store.value("open_dir", "")), file_filter)  # fmt: skip
        if not name:
            return None
        self.store.setValue("open_dir", str(Path(name).parent))
        try:
            if kind == "music" and not media.probe(name).has_audio:
                raise ValueError
            if kind == "logo" and not QImageReader(name).canRead():
                raise ValueError
        except (OSError, ValueError):
            QMessageBox.warning(self, APP_TITLE, f"열 수 없는 파일입니다: {Path(name).name}")
            return None
        return name

    def _source_size(self, overlay: Overlay) -> tuple[int, int]:
        key = self._strip_key(overlay.source)
        if key not in self._source_sizes:
            try:
                if overlay.kind == "image":
                    size = canvas.sticker_size(overlay.source)
                else:
                    info = media.probe(overlay.source)
                    size = (info.width, info.height)
            except (OSError, ValueError):
                size = (16, 9)
            self._source_sizes[key] = size if all(size) else (16, 9)
        return self._source_sizes[key]

    def _overlay_height_share(self, overlay: Overlay) -> float:
        w, h = self._source_size(overlay)
        frame_w, frame_h = self.project.size
        return overlay.width * frame_w * h / w / frame_h

    # ---- dragging on the preview ------------------------------------------------

    def _update_guides(self) -> None:
        """Outlines on the preview around the scene's words and overlays, which can be dragged."""
        if self.play_bar.isVisible() or not 0 <= self.current < len(self.project.scenes):
            self.screen.clear_guides()
            return
        # Where the words and pictures are at the playhead (they may move by keyframes).
        scene = canvas.placed(self.project.scenes[self.current], self._preview_time())
        width, height = self.project.size
        guides = []
        for i, overlay in enumerate(scene.overlays):
            if overlay.source:
                w, h = self._source_size(overlay)
                rect = canvas.overlay_rect(overlay, w, h, width, height)
                if overlay.border:
                    b = canvas.border_width(rect)
                    rect = rect.adjusted(-b, -b, b, b)
                guides.append(("overlay", i, QRectF(rect)))
        for i, box in enumerate(scene.texts):
            if box.text.strip():
                guides.append(("text", i, canvas.text_rect(box, width, height)))
        tab = self.tabs.currentWidget()
        selected = None
        if tab is self._tab_of(self.text_panel):
            selected = ("text", self.text_panel.index())
        elif tab is self._tab_of(self.overlay_panel):
            selected = ("overlay", self.overlay_panel.index())
        elif tab is self._tab_of(self.mask_panel):
            # Hidden parts can be grabbed only on their own tab, so they do not get in the way of the rest.
            guides += [("mask", i, QRectF(canvas.mask_rect(m, width, height))) for i, m in enumerate(scene.masks)]
            selected = ("mask", self.mask_panel.index())
        self.screen.set_guides(guides, (width, height), selected)

    def _on_screen_picked(self, kind: str, index: int) -> None:
        panel = {"text": self.text_panel, "overlay": self.overlay_panel, "mask": self.mask_panel}[kind]
        self.tabs.setCurrentWidget(self._tab_of(panel))
        panel.select(index)

    def _on_screen_moved(self, kind: str, index: int, x: float, y: float) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        scene = self.project.scenes[self.current]
        items = {"text": scene.texts, "overlay": scene.overlays, "mask": scene.masks}[kind]
        if not 0 <= index < len(items):
            return
        item = items[index]
        if kind == "mask":
            item.x, item.y = round(x, 4), round(y, 4)
            self.mask_panel.show_items(scene.masks, index)
        elif item.keys:
            # It moves by keyframes: this is where it is at the playhead (a keyframe there is added if needed).
            key = key_at(item, self._preview_time())
            key.x, key.y = round(x, 4), round(y, 4)
        else:
            item.x, item.y = round(x, 4), round(y, 4)
            if kind == "text":
                # A block is kept inside the frame; remember where it really shows, so the spot matches.
                rect = canvas.text_rect(item, *self.project.size)
                width, height = self.project.size
                item.x, item.y = round(rect.center().x() / width, 4), round(rect.center().y() / height, 4)
        if kind == "text":
            self.text_panel.show_items(scene.texts, index)
        elif kind == "overlay":
            self.overlay_panel.show_items(scene.overlays, index)
        self._refresh_timeline()
        self._on_playhead_changed()
        self._commit()

    def _on_screen_resized(self, kind: str, index: int, factor: float) -> None:
        if not 0 <= self.current < len(self.project.scenes):
            return
        scene = self.project.scenes[self.current]
        if kind == "mask":
            if 0 <= index < len(scene.masks):
                mask = scene.masks[index]
                mask.width = round(min(1.0, max(0.02, mask.width * factor)), 4)
                mask.height = round(min(1.0, max(0.02, mask.height * factor)), 4)
                self.mask_panel.show_items(scene.masks, index)
                self._refresh_timeline()
                self._on_playhead_changed()
                self._commit(f"resize:mask:{index}")
            return
        overlays = scene.overlays
        if 0 <= index < len(overlays):
            overlay = overlays[index]
            if overlay.keys:  # its size at the playhead, kept in the keyframe there
                key = key_at(overlay, self._preview_time())
                key.scale = round(min(1.0 / overlay.width, max(0.03 / overlay.width, key.scale * factor)), 4)
            else:
                overlay.width = round(min(1.0, max(0.03, overlay.width * factor)), 4)
            self.overlay_panel.show_items(overlays, index)
            self.tabs.setCurrentWidget(self._tab_of(self.overlay_panel))
            self._on_playhead_changed()
            self._commit(f"resize:{index}:{self._preview_time():.3f}")

    def _on_screen_reshaped(self, kind: str, index: int, x: float, y: float, width: float, height: float) -> None:
        """A hidden part's corner was dragged."""
        if kind != "mask" or not 0 <= self.current < len(self.project.scenes):
            return
        masks = self.project.scenes[self.current].masks
        if 0 <= index < len(masks):
            mask = masks[index]
            mask.x, mask.y = round(x, 4), round(y, 4)
            mask.width, mask.height = round(min(1.0, max(0.02, width)), 4), round(min(1.0, max(0.02, height)), 4)
            self.mask_panel.show_items(masks, index)
            self._refresh_timeline()
            self._on_playhead_changed()
            self._commit()

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
            for panel in (self.text_panel, self.overlay_panel):
                panel.time_changed()
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
            self.screen.clear_guides()
            self.screen.show_message("장면이 없습니다\n[음성 만들기] 탭에서 음성을 보내거나,\n이미지·영상을 아래 타임라인에 끌어다 놓으세요")
            return
        scene, at = self.project.scenes[self.current], self._preview_time()
        if self._scrub is not None and scene.kind == "clip":
            # While a trim handle moves, show that moment of the clip itself.
            scene = dataclasses.replace(scene, clip_start=self._scrub, clip_end=0.0, speed=1.0, clip_fill="freeze")
            at = 0.0
        thread = PreviewThread(scene, self.project.size, at, self.project, self)
        thread.ready.connect(self._show_still)
        thread.failed.connect(self._show_preview_error)
        thread.finished.connect(self._preview_finished)
        self.preview_thread = thread
        thread.start()

    def _show_still(self, image: QImage) -> None:
        # Not over a playing preview, and not when the scene it was drawn for has gone (or a newer one is due).
        if not self.play_bar.isVisible() and not self._preview_pending and 0 <= self.current < len(self.project.scenes):
            self.screen.show_frame(QPixmap.fromImage(image))
            self._update_guides()

    def _show_preview_error(self, message: str) -> None:
        if not self.play_bar.isVisible() and not self._preview_pending:
            self.screen.clear_guides()
            self.screen.show_message(f"미리보기를 만들지 못했습니다\n{message}")

    def _preview_finished(self) -> None:
        self.preview_thread.deleteLater()
        self.preview_thread = None
        if self._preview_pending:
            self._preview_pending = False
            self._start_preview()

    def save_frame(self) -> None:
        """Save the moment at the playhead as a picture, at the video's full size."""
        if not 0 <= self.current < len(self.project.scenes):
            return
        if self.play_bar.isVisible():
            self.close_player()
        start = Path(str(self.store.value("save_dir", Path.home()))) / f"장면_{datetime.now():%Y%m%d_%H%M%S}.png"
        name, _ = QFileDialog.getSaveFileName(self, "이미지로 저장", str(start), "PNG 이미지 (*.png);;JPG 이미지 (*.jpg)")
        if not name:
            return
        path = Path(name)
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            path = path.with_suffix(".png")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            width, height = self.project.size
            image = scene_render.still(self.project.scenes[self.current], width, height, self._preview_time(),
                                       canvas.logo_layer(self.project, width, height))  # fmt: skip
            saved = image.save(str(path), quality=95)
        except (OSError, ValueError, RuntimeError) as e:
            saved, error = False, str(e)
        else:
            error = ""
        finally:
            QApplication.restoreOverrideCursor()
        if not saved:
            QMessageBox.warning(self, APP_TITLE, f"이미지를 저장하지 못했습니다.\n{error}")
            return
        self.store.setValue("save_dir", str(path.parent))
        self._show_status(f"이미지로 저장했습니다: {path} ({width}×{height})")

    # ---- voice --------------------------------------------------------------

    def _read_voice_file(self) -> bool:
        """The project's voice: its subtitles and length (quietly; a missing file just leaves them empty)."""
        self._file_cues, self.voice_duration = [], 0.0
        audio = Path(self.project.voice) if self.project.voice else None
        if audio is None or not audio.is_file():
            return False
        srt = next((audio.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if audio.with_suffix(s).exists()), None)
        try:
            self.voice_duration = media.probe(audio).duration
            if srt is not None:
                self._file_cues = subs.load_cues(srt)
        except (OSError, ValueError):
            return False
        return True

    def _load_voice(self, audio: Path) -> bool:
        srt = next((audio.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if audio.with_suffix(s).exists()), None)
        try:
            if srt is None:
                raise ValueError(f"'{audio.stem}' 이름의 자막 파일(SRT)이 음성 옆에 없어서 문장을 나눌 수 없습니다.")
            cues = subs.load_cues(srt)
            duration = media.probe(audio).duration
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return False
        self._file_cues, self.voice_duration = cues, duration
        self.project.voice = str(audio)
        self.project.cues = None  # subtitles edited for the old voice do not fit this one
        self._waves.pop(str(audio), None)  # the voice may have been made again under the same name
        self._load_project_controls()
        self._update_summary()
        self._update_timeline()
        return True

    def _update_voice_label(self) -> None:
        if self.voice_cues:
            sentences = len(sentence_spans(self.voice_cues))
            edited = " · 자막 고침" if self.project.cues is not None else ""
            self.voice_label.setText(
                f"{Path(self.project.voice).name} · {format_time(self.voice_duration, precise=True)} · 문장 {sentences}개{edited}"
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
            self._commit()
            self._offer_fit()

    def receive_voice(self, audio: Path) -> None:
        """A voice from the voice tab: time the scenes to it."""
        if self._load_voice(audio):
            self._commit()
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
        self.selection = set()
        self._reload_scenes(max(0, self.current))
        self._commit()
        added = len(self.project.scenes) - before
        message = f"장면 {len(self.project.scenes)}개의 길이를 음성 문장에 맞췄습니다."
        if added > 0:
            message += f" 빈 장면 {added}개를 더했습니다. 이미지나 영상을 끌어다 놓아 채우세요."
        self._show_status(message)

    # ---- project files ------------------------------------------------------

    def new_project(self) -> None:
        if self.project.scenes and QMessageBox.question(
            self, APP_TITLE, "지금 장면을 모두 지우고 새로 시작할까요?\n(실행 취소로 되돌릴 수 있습니다)"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._keep_copy()
        # The video's settings, voice, music and logo stay; the scenes and sound effects go.
        project = Project.from_json(self.project.to_json())
        project.scenes, project.sounds = [], []
        self._apply_project(project)
        self._commit()

    def import_storyboard(self) -> None:
        """Scenes from a 콘티 workbook: a scene a row (pictures, templates, words), timed to the voice if wanted."""
        name, _ = QFileDialog.getOpenFileName(self, "콘티 엑셀 열기", str(self.store.value("open_dir", "")), "엑셀 콘티 (*.xlsx *.xlsm)")
        if not name:
            return
        self.store.setValue("open_dir", str(Path(name).parent))
        try:
            board = storyboard.read(name)
        except ValueError as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return
        voice = Path(self.project.voice) if self.project.voice else None
        if voice is not None and voice.is_file():
            answer = QMessageBox.question(self, APP_TITLE, f"장면 길이를 지금 음성({voice.name})에 맞출까요?\n"
                                          "아니오를 누르면 콘티의 길이 칸이나 나레이션 길이로 정합니다.")  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                voice = None
        else:
            voice = None
        if self.project.scenes:
            answer = QMessageBox.question(self, APP_TITLE, f"지금 장면 {len(self.project.scenes)}개를 콘티의 장면 {len(board.rows)}개로 바꿉니다.\n"
                                          "바꾸기 전 상태는 되돌리기(Ctrl+Z)와 자동 저장 기록에 남습니다.")  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            project, notes = storyboard.build(board, self.project, voice)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, f"콘티로 장면을 만들지 못했습니다.\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._keep_copy()
        self._apply_project(project)
        self._commit()
        self._show_status(f"콘티에서 장면 {len(project.scenes)}개를 만들었습니다 ({board.file.name}).")
        if notes:
            QMessageBox.information(self, APP_TITLE, "\n".join(notes[:20]))

    def _save_target(self, title: str, stem: str, file_filter: str, suffixes: tuple[str, ...]) -> Path | None:
        if not self.project.scenes:
            QMessageBox.information(self, APP_TITLE, "장면이 없습니다.")
            return None
        start = Path(str(self.store.value("save_dir", Path.home()))) / f"{stem}_{datetime.now():%Y%m%d_%H%M}{suffixes[0]}"
        name, _ = QFileDialog.getSaveFileName(self, title, str(start), file_filter)
        if not name:
            return None
        path = Path(name)
        if path.suffix.lower() not in suffixes:
            path = path.with_name(path.name + suffixes[0])
        self.store.setValue("save_dir", str(path.parent))
        return path

    def _busy(self, job: Callable[[], object], failure: str):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return job()
        except (OSError, ValueError, RuntimeError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_TITLE, f"{failure}\n{e}")
            return None
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def export_storyboard(self) -> None:
        path = self._save_target("콘티 엑셀로 내보내기", "콘티", "엑셀 (*.xlsx)", (".xlsx",))
        if path is None:
            return
        notes = self._busy(lambda: storyboard.export(self.project, path, True, path.stem), "콘티를 만들지 못했습니다 (엑셀에서 열려 있으면 닫아 주세요).")
        if notes is None:
            return
        self._show_status(f"콘티로 내보냈습니다: {path} (썸네일·소스목록{'·나레이션 비교' if self.project.voice else ''})")
        if notes:
            QMessageBox.information(self, APP_TITLE, "\n".join(notes[:20]))

    def save_storyboard_image(self) -> None:
        path = self._save_target("스토리보드 그림 저장", "스토리보드", "JPG 그림 (*.jpg);;PNG 그림 (*.png)", (".jpg", ".jpeg", ".png"))
        if path is None:
            return
        sheet = self._busy(lambda: storyboard.contact_sheet(self.project, path.stem), "스토리보드 그림을 만들지 못했습니다.")
        if sheet is not None and sheet.save(str(path), quality=92):
            self._show_status(f"스토리보드 그림을 저장했습니다: {path} ({sheet.width()}×{sheet.height()})")

    def save_keyframes(self) -> None:
        if not self.project.scenes:
            return
        folder = QFileDialog.getExistingDirectory(self, "장면 그림을 저장할 폴더", str(self.store.value("save_dir", Path.home())))
        if not folder:
            return
        made = self._busy(lambda: storyboard.keyframes(self.project, Path(folder)), "장면 그림을 저장하지 못했습니다.")
        if made:
            self._show_status(f"장면 그림 {len(made)}장을 저장했습니다: {folder}")

    def new_storyboard_file(self) -> None:
        start = Path(str(self.store.value("save_dir", Path.home()))) / "콘티.xlsx"
        name, _ = QFileDialog.getSaveFileName(self, "빈 콘티 양식 만들기", str(start), "엑셀 (*.xlsx)")
        if not name:
            return
        path = Path(name) if name.lower().endswith(".xlsx") else Path(name + ".xlsx")
        if self._busy(lambda: storyboard.blank(path) or True, "콘티 양식을 만들지 못했습니다."):
            self._show_status(f"빈 콘티 양식을 만들었습니다: {path} (도움말 시트에 열과 템플릿 설명)")

    def _keep_copy(self) -> None:
        """A dated copy of the project before it is replaced, so it outlives this run's undo."""
        try:
            autosave.backup(self.project.to_json(), "장면", ".json")
        except OSError:
            log_error(traceback.format_exc())

    def open_project(self, folder: Path | None = None) -> None:
        start = str(folder) if folder else str(self.store.value("project_dir", ""))
        name, _ = QFileDialog.getOpenFileName(self, "프로젝트 열기", start, PROJECT_FILTER)
        if not name:
            return
        try:
            project = Project.load(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return
        if not folder:  # the autosave folder is not remembered as the place for projects
            self.store.setValue("project_dir", str(Path(name).parent))
        self._keep_copy()
        self._apply_project(project)
        self._commit()
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
        """Start where the last run left off (saved as it went, so even after a crash)."""
        session = autosave.session()
        project = Project()
        if AUTOSAVE.exists():
            text = None
            try:
                text = AUTOSAVE.read_text(encoding="utf-8")
                autosave.backup(text, "장면", ".json")  # kept as it was, whatever happens to it in this run
            except OSError:
                log_error(traceback.format_exc())
            try:
                project = Project.from_json(text or "")
            except (ValueError, TypeError, AttributeError):
                log_error(traceback.format_exc())
                # A damaged file is set aside (not saved over) and the user is told.
                broken = AUTOSAVE.with_name(f"{AUTOSAVE.stem}_읽지못함_{datetime.now():%Y%m%d_%H%M%S}.json")
                try:
                    os.replace(AUTOSAVE, broken)
                except OSError:
                    broken = AUTOSAVE
                self._startup_note = f"지난 장면 파일을 읽을 수 없어서 빈 프로젝트로 시작합니다. 원래 파일은 '{broken.name}'로 남겨 두었습니다."
            else:
                if session.crashed:
                    saved = datetime.fromtimestamp(AUTOSAVE.stat().st_mtime)
                    self._startup_note = (f"지난번에 프로그램이 제대로 닫히지 않았습니다. {saved:%H:%M:%S}에 자동 저장된 장면을 불러왔습니다."
                                          " 그 전 상태는 [프로젝트 ▾ → 자동 저장 기록에서 열기]에 있습니다.")  # fmt: skip
        if session.other:
            self._startup_note = (f"이 프로그램이 다른 창에서도 실행 중이라, 이 창의 장면은 '{self.autosave_path.name}'에 따로 자동 저장합니다.")
        try:
            self._apply_project(project)
        except Exception:  # noqa: BLE001 - a file that loads but cannot be shown must not stop the program
            log_error(traceback.format_exc())
            self._startup_note = "지난 장면을 여는 중에 문제가 생겨 빈 프로젝트로 시작합니다. 자세한 내용은 error.log에 있습니다."
            self._apply_project(Project())

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
        last = formats.by_key(self.store.value("save_format", formats.DEFAULT.key, type=str))
        last = last if last in VIDEO_FORMATS else formats.DEFAULT
        start = Path(str(self.store.value("save_dir", Path.home()))) / f"영상_{datetime.now():%Y%m%d_%H%M}{last.suffix}"
        name, chosen = QFileDialog.getSaveFileName(self, "영상 저장", str(start), formats.dialog_filters(VIDEO_FORMATS),
                                                   last.dialog_filter)  # fmt: skip
        if not name:
            return
        output, fmt = formats.from_dialog(name, chosen, VIDEO_FORMATS)
        self.store.setValue("save_dir", str(output.parent))
        self.store.setValue("save_format", fmt.key)
        self._start_render(output, "save", fmt=fmt)

    def _start_render(self, output: Path, purpose: str, voice: Path | None = None, subtitle: Path | None = None,
                      style: SubStyle | None = None, fmt: formats.Format = formats.DEFAULT) -> RenderThread:  # fmt: skip
        thread = RenderThread(self.project, output, purpose, voice, subtitle, style, fmt, self)
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

    def _subtitle_style(self) -> SubStyle:
        try:
            return self.subtitle_style() if self.subtitle_style else SubStyle()
        except Exception:  # noqa: BLE001 - the preview still plays with the default look
            return SubStyle()

    def _preview_key(self, voice: Path | None, style: SubStyle) -> str:
        """Everything the preview video depends on; when it is unchanged, the last one can be played again."""
        stamps = []
        files = [voice, *(voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES)] if voice else []
        extra = (self.project.music, self.project.logo, *(s.source for s in self.project.sounds))
        for path in [*files, *(Path(p) for p in extra if p)]:
            if path.exists():
                stamps.append([str(path), path.stat().st_mtime_ns])
        return json.dumps([self.project.to_json(), stamps, dataclasses.asdict(style), encoder.use_gpu], ensure_ascii=False)

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

    def toggle_play(self) -> None:
        """Space: play from the playhead, or pause and go on."""
        if self.play_bar.isVisible():
            self.toggle_playback()
        else:
            self.play_preview()

    def play_preview(self) -> None:
        """Play from the playhead, making (or reusing) a small preview video with the voice first."""
        if self.render_thread is not None or not self.project.scenes:
            return
        voice = Path(self.project.voice) if self.voice_cues and self.project.voice else None
        style = self._subtitle_style()
        key = self._preview_key(voice, style)
        if key == self._preview_made and self._preview_file and self._preview_file.exists():
            self._show_player(self.playhead)
            return
        self._preview_count += 1
        output = scene_render.PREVIEW_DIR / f"미리보기_{os.getpid()}_{self._preview_count}.mp4"
        subtitle = self._edited_subtitle(scene_render.PREVIEW_DIR, f"자막_{os.getpid()}.srt")
        self._start_render(output, "preview", voice, subtitle, style).key = key

    def _show_player(self, start: float) -> None:
        self.still_bar.setVisible(False)
        self.play_bar.setVisible(True)
        self.screen.clear_guides()
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
            self.videoReady.emit(self._handoff(output))
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

    def _handoff(self, video: Path) -> dict:
        """What the compose tab gets along with the video: the subtitles to use, the music, the sound effects
        and the fades at the start and end (which the compose tab puts on, over everything)."""
        project = self.project
        subtitle = None
        if project.cues is not None and project.voice:
            try:
                subtitle = self._edited_subtitle(paths.WORK_DIR, f"{Path(project.voice).stem}_고친자막.srt")
            except OSError:
                log_error(traceback.format_exc())
        elif project.voice:
            # The voice's own subtitles, so an edited copy sent earlier is replaced again.
            voice = Path(project.voice)
            subtitle = next((voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if voice.with_suffix(s).exists()), None)
        music = project.music if project.music and Path(project.music).is_file() else None
        return {
            "video": video,
            "subtitle": subtitle,
            "music": Path(music) if music else None,
            "music_volume": project.music_volume,
            "music_duck": project.music_duck,
            "music_fade": project.music_fade,
            "effects": scene_render.sound_effects(project),
            "fade_in": project.fade_in,
            "fade_out": project.fade_out,
        }

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
        for thread in (self.thumb_thread, self.strip_thread, self.preview_thread, self.wave_thread):
            if thread is not None:
                thread.requestInterruption()
                thread.wait(5_000)  # one still running past this is dealt with in main_window.main
        self.autosave_timer.stop()
        try:
            text = self.project.to_json()
            autosave.write(self.autosave_path, text)
            autosave.backup(text, "장면", ".json")
        except OSError:
            log_error(traceback.format_exc())
        self.store.sync()
