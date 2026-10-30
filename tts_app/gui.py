"""The voice tab: turns text into speech plus subtitles."""

from __future__ import annotations

import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from movie_app import autosave

from . import paths, pipeline, reading, subtitles, textproc
from .editor import TextEditor, format_length
from .engine import MAX_SPEED, MELO_VOICE, MIN_SPEED, Engine, list_voices
from .player import Player
from .reading_dialog import DictionaryDialog, ReadingDialog, ReadingsDialog
from .voice_dialog import VoiceDialog

APP_TITLE = "동영상 편집기"
ERROR_LOG = paths.DATA_DIR / "error.log"
WORK_DIR = paths.DATA_DIR / "작업"  # voices handed to the video tab are saved here

LANGUAGES = [
    ("ko", "한국어"),
    ("auto", "자동 (문장마다 감지)"),
    ("en", "English"),
    ("ja", "日本語"),
    ("na", "기타 언어"),
]
QUALITIES = [(4, "빠름"), (8, "표준"), (16, "고품질")]
FORMATS = [("mp3", "MP3"), ("wav", "WAV")]
VOICE_SAMPLES = {
    "ko": "안녕하세요. 이 목소리로 글을 읽어 드릴게요.",
    "en": "Hello! This is the voice that will read your text.",
    "ja": "こんにちは。この声で文章を読み上げます。",
}
PREVIEW_CHARS = 150
DEFAULTS = pipeline.Settings()
PURPOSE_LABELS = {"full": "전체 음성", "preview": "미리듣기", "voice": "목소리 샘플"}


def _log_error(text: str) -> None:
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
    except OSError:
        pass


def _format_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LoadThread(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            self.loaded.emit(Engine())
        except Exception as e:  # noqa: BLE001 - shown to the user
            _log_error(traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")


class MeloLoadThread(QThread):
    """Loads MeloTTS ahead of use: creating its ONNX sessions takes several seconds."""

    finished_loading = Signal(str)  # error message, or "" on success

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine

    def run(self):
        try:
            self.engine.load_melo()
        except Exception as e:  # noqa: BLE001 - shown to the user
            _log_error(traceback.format_exc())
            self.finished_loading.emit(f"{type(e).__name__}: {e}")
        else:
            self.finished_loading.emit("")


class EstimateThread(QThread):
    """Predicts each line's length from the duration models, without synthesizing audio."""

    estimated = Signal(object, object)  # (timing key it was computed for, Timeline)

    def __init__(self, engine, key, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.key = key
        self.settings = settings

    def run(self):
        try:
            segments = textproc.segment_text(self.key[0], self.settings.lang)
            timeline = pipeline.estimate(self.engine, segments, self.settings, self.isInterruptionRequested)
        except pipeline.Cancelled:
            return
        except Exception:  # noqa: BLE001 - estimates are a convenience; log and carry on
            _log_error(traceback.format_exc())
            return
        self.estimated.emit(self.key, timeline)


class RenderThread(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    note = Signal(str)

    def __init__(self, engine, segments, settings, purpose, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.segments = segments
        self.settings = settings
        self.purpose = purpose
        self.elapsed = 0.0
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        start = time.perf_counter()
        try:
            if self.settings.voice == MELO_VOICE and not self.engine.melo_loaded:
                self.note.emit("MeloTTS 모델을 불러오는 중… (처음 한 번만)")
                self.engine.load_melo()
            result = pipeline.render(
                self.engine, self.segments, self.settings, self.progress.emit, lambda: self._cancel
            )
        except pipeline.Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - shown to the user
            _log_error(traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.elapsed = time.perf_counter() - start
            self.done.emit(result)


class TtsPage(QWidget):
    voiceReady = Signal(object)  # Path of a voice saved for the video tab, with a same-named .srt beside it

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.store.beginGroup("tts")  # the video tab keeps its settings in the same file
        self.engine: Engine | None = None
        self.player = Player()
        self.full_result: pipeline.Result | None = None
        self.source_path: Path | None = None
        self.load_thread: LoadThread | None = None
        self.render_thread: RenderThread | None = None
        self.melo_thread: MeloLoadThread | None = None
        self.estimate_thread: EstimateThread | None = None
        self._estimate_again = False
        self.estimate: pipeline.Timeline | None = None
        self.generated_key: tuple | None = None  # timing key of full_result
        self._counts = (0, 0)
        self._seeking = False

        self._build_ui()
        self._restore()
        self.script_path = autosave.session().file(WORK_DIR / "대본.txt")
        self._restore_script()
        # The script is saved shortly after every change, so closing or a crash never loses it.
        self.script_timer = QTimer(self)
        self.script_timer.setSingleShot(True)
        self.script_timer.setInterval(1500)
        self.script_timer.timeout.connect(self._save_script)
        self.editor.textChanged.connect(self.script_timer.start)

        self.count_timer = QTimer(self)
        self.count_timer.setSingleShot(True)
        self.count_timer.setInterval(300)
        self.count_timer.timeout.connect(self._update_counts)
        self.editor.textChanged.connect(self.count_timer.start)
        self.lang_combo.currentIndexChanged.connect(self.count_timer.start)

        # Line lengths are re-predicted shortly after the text or a timing setting changes.
        self.estimate_timer = QTimer(self)
        self.estimate_timer.setSingleShot(True)
        self.estimate_timer.setInterval(400)
        self.estimate_timer.timeout.connect(self._run_estimate)
        for signal in (
            self.editor.textChanged,
            self.voice_combo.currentIndexChanged,
            self.lang_combo.currentIndexChanged,
            self.speed_slider.valueChanged,
            self.sentence_pause.valueChanged,
            self.paragraph_pause.valueChanged,
        ):
            signal.connect(self._timing_changed)

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(100)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()

        for keys, slot in (("Ctrl+O", self.open_file), ("Ctrl+S", self.save), ("Ctrl+Return", self.generate),
                           ("Ctrl+R", self.set_reading)):  # fmt: skip
            QShortcut(QKeySequence(keys), self).activated.connect(slot)

        self._update_counts()
        self._set_busy(False)
        self._tick()

    def showEvent(self, event):
        # The voice model takes a few seconds and a few hundred MB; load it only once this tab is used.
        super().showEvent(event)
        if self.load_thread is None and self.engine is None:
            self._load_engine()

    # ---- layout -------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(8)
        bar = QHBoxLayout()
        open_btn = QPushButton("텍스트 파일 열기…")
        open_btn.setToolTip("TXT 파일을 불러옵니다 (Ctrl+O). 창에 끌어다 놓아도 됩니다.")
        open_btn.clicked.connect(lambda: self.open_file())
        clear_btn = QPushButton("지우기")
        clear_btn.clicked.connect(self.clear_text)
        pause_btn = QToolButton()
        pause_btn.setText("쉼 넣기 ▾")
        pause_btn.setToolTip("커서 자리에 [쉼 N초]를 넣습니다. 그 자리에서 정확히 N초 쉽니다. 직접 [쉼 2초]처럼 써도 됩니다.")
        pause_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        pause_menu = QMenu(pause_btn)
        for seconds in (0.5, 1, 2, 3, 5):
            pause_menu.addAction(f"{seconds:g}초", lambda s=seconds: self.insert_pause(s))
        pause_btn.setMenu(pause_menu)
        reading_btn = QToolButton()
        reading_btn.setText("읽는 법 ▾")
        reading_btn.setToolTip("드래그해서 고른 글을 다르게 읽게 합니다(자막은 쓴 그대로). 예: 14번 → 열네 번.\n"
                               "대본에 {14번|열네 번}처럼 써도 되고, 자주 쓰는 말은 읽는 법 사전에 넣습니다.")  # fmt: skip
        reading_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        reading_menu = QMenu(reading_btn)
        reading_menu.addAction("고른 글 읽는 법 지정…\tCtrl+R", self.set_reading)
        reading_menu.addAction("읽는 법 사전…", self.edit_dictionary)
        reading_menu.addAction("실제로 읽을 글 보기…", self.show_readings)
        reading_menu.addSeparator()
        self.numbers_action = reading_menu.addAction("숫자 자동 읽기 (14개 → 열네 개)")
        self.numbers_action.setCheckable(True)
        self.numbers_action.setChecked(reading.load().numbers)
        self.numbers_action.toggled.connect(self._set_numbers)
        reading_menu.aboutToShow.connect(self._sync_numbers)
        reading_btn.setMenu(reading_menu)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        bar.addWidget(open_btn)
        bar.addWidget(clear_btn)
        bar.addWidget(pause_btn)
        bar.addWidget(reading_btn)
        bar.addStretch(1)
        bar.addWidget(self.count_label)
        left.addLayout(bar)

        self.editor = TextEditor()
        self.editor.setPlaceholderText(
            "여기에 읽을 글을 입력하거나 붙여넣으세요.\n"
            "TXT 파일을 끌어다 놓아도 됩니다.\n\n"
            "줄바꿈은 문장 구분, 빈 줄은 문단 구분(조금 더 길게 쉼)으로 처리됩니다.\n"
            "[쉼 2초]처럼 쓰면 그 자리에서 정확히 2초 쉽니다.\n"
            "{14번|열네 번}처럼 쓰면 자막은 14번, 소리는 열네 번이 됩니다(글을 고르고 읽는 법 ▾).\n\n"
            "오른쪽에 줄마다 읽는 데 걸리는 시간이 나옵니다."
        )
        self.editor.fileDropped.connect(self.open_file)
        set_reading = QAction("읽는 법 지정…", self.editor)
        set_reading.triggered.connect(self.set_reading)
        self.editor.context_actions = [set_reading]
        left.addWidget(self.editor, 1)
        body.addLayout(left, 1)

        side = QWidget()
        side.setFixedWidth(370)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        side_layout.addWidget(self._build_voice_box())
        side_layout.addWidget(self._build_output_box())
        side_layout.addStretch(1)
        body.addWidget(side)

        root.addWidget(self._build_action_row())
        root.addWidget(self._build_player_row())

    def _build_voice_box(self) -> QGroupBox:
        box = QGroupBox("음성 설정")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.voice_combo = QComboBox()
        self.voice_combo.setMaxVisibleItems(24)
        self.sample_btn = QPushButton("들어보기")
        self.sample_btn.setToolTip("선택한 목소리와 지금 설정으로 짧은 문장을 읽어 봅니다")
        self.sample_btn.clicked.connect(lambda: self.play_voice_sample())
        grid.addWidget(QLabel("목소리"), 0, 0)
        grid.addWidget(self.voice_combo, 0, 1)
        grid.addWidget(self.sample_btn, 0, 2)

        self.voices_btn = QPushButton("목소리 만들기…")
        self.voices_btn.setToolTip("목소리를 섞어 새 목소리를 만들거나 목소리 파일을 가져옵니다")
        self.voices_btn.clicked.connect(self.open_voice_dialog)
        grid.addWidget(self.voices_btn, 1, 1, 1, 2)

        self.lang_combo = QComboBox()
        for code, label in LANGUAGES:
            self.lang_combo.addItem(label, code)
        self.lang_combo.setToolTip(
            "한국어 글 속 영어 단어는 '한국어'로도 읽습니다. 여러 언어가 섞인 글은 '자동'을 고르세요."
        )
        grid.addWidget(QLabel("언어"), 2, 0)
        grid.addWidget(self.lang_combo, 2, 1, 1, 2)

        self.speed_slider = self._add_slider(
            grid, 3, "속도", round(MIN_SPEED * 100), round(MAX_SPEED * 100), lambda v: f"{v / 100:.2f}배"
        )
        self.pitch_slider = self._add_slider(
            grid, 4, "높낮이", -12, 12, lambda v: f"{v / 2:+g} 반음" if v else "0 반음"
        )
        self.volume_slider = self._add_slider(grid, 5, "볼륨", 10, 200, lambda v: f"{v}%")
        self.volume_slider.setToolTip(
            "100%는 소리가 깨지지 않는 최대 크기에 맞춘 값입니다. 그보다 크면 큰 소리를 부드럽게 눌러 줍니다."
        )

        self.sentence_pause = self._add_seconds(grid, 6, "문장 사이 쉼", 3.0)
        self.paragraph_pause = self._add_seconds(grid, 7, "문단 사이 쉼", 5.0)

        self.quality_combo = QComboBox()
        for steps, label in QUALITIES:
            self.quality_combo.addItem(label, steps)
        grid.addWidget(QLabel("품질"), 8, 0)
        grid.addWidget(self.quality_combo, 8, 1, 1, 2)

        reset_btn = QPushButton("기본값으로")
        reset_btn.clicked.connect(lambda: self._apply_settings(DEFAULTS))
        grid.addWidget(reset_btn, 9, 1, 1, 2, Qt.AlignmentFlag.AlignRight)

        self._fill_voices(DEFAULTS.voice)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        return box

    def _fill_voices(self, select: str) -> None:
        """List built-in, custom and MeloTTS voices, separated by lines."""
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        previous_kind = None
        for voice in list_voices():
            kind = "melo" if voice.is_melo else "custom" if voice.is_custom else "builtin"
            if previous_kind and kind != previous_kind:
                self.voice_combo.insertSeparator(self.voice_combo.count())
            self.voice_combo.addItem(voice.label, voice.id)
            previous_kind = kind
        index = self.voice_combo.findData(select)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        # Let the drop-down grow to fit long custom voice names instead of cutting them off.
        view = self.voice_combo.view()
        view.setMinimumWidth(view.sizeHintForColumn(0) + 24)
        self.voice_combo.blockSignals(False)
        self._on_voice_changed()

    def _on_voice_changed(self) -> None:
        melo = self.voice_combo.currentData() == MELO_VOICE
        if melo:
            self._preload_melo()
        self.quality_combo.setEnabled(not melo)
        self.quality_combo.setToolTip(
            "MeloTTS에는 품질 단계 설정이 없습니다" if melo else "고품질은 조금 더 깨끗하지만 만드는 데 시간이 더 걸립니다"
        )

    def _preload_melo(self) -> None:
        if self.engine is None or self.engine.melo_loaded or self.melo_thread is not None:
            return
        self.melo_thread = MeloLoadThread(self.engine, self)
        self.melo_thread.finished_loading.connect(self._melo_preloaded)
        self.melo_thread.start()
        if self.render_thread is None:
            self._show_status("MeloTTS 모델을 미리 불러오는 중…")

    def _melo_preloaded(self, error: str) -> None:
        self.melo_thread.wait()
        self.melo_thread.deleteLater()
        self.melo_thread = None
        if self.render_thread is None:
            self._show_status(f"MeloTTS를 불러오지 못했습니다: {error}" if error else "MeloTTS 준비 완료")
        self._run_estimate()

    def open_voice_dialog(self) -> None:
        if self.engine is None:
            return
        dialog = VoiceDialog(self.engine, self)
        dialog.previewRequested.connect(self.play_voice_sample)
        dialog.voicesChanged.connect(lambda voice: self._fill_voices(voice or self.voice_combo.currentData()))
        dialog.voicesChanged.connect(self._timing_changed)
        dialog.exec()

    def _add_slider(self, grid: QGridLayout, row: int, title: str, lo: int, hi: int, fmt) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        label = QLabel(fmt(slider.value()))
        label.setMinimumWidth(66)
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

    def _build_output_box(self) -> QGroupBox:
        box = QGroupBox("저장 옵션")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.format_combo = QComboBox()
        for code, label in FORMATS:
            self.format_combo.addItem(label, code)
        grid.addWidget(QLabel("파일 형식"), 0, 0)
        grid.addWidget(self.format_combo, 0, 1)

        self.srt_check = QCheckBox("자막(SRT) 파일도 함께 저장")
        grid.addWidget(self.srt_check, 1, 0, 1, 2)

        self.srt_line = QSpinBox()
        self.srt_line.setRange(10, 60)
        self.srt_line.setSuffix(" 자")
        self.srt_line.setToolTip("자막 한 줄의 최대 글자 수입니다. 자막 하나는 최대 두 줄입니다.")
        self.srt_check.toggled.connect(self.srt_line.setEnabled)
        grid.addWidget(QLabel("자막 줄 길이"), 2, 0)
        grid.addWidget(self.srt_line, 2, 1)
        return box

    def _build_action_row(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.preview_btn = QPushButton("미리듣기")
        self.preview_btn.setToolTip(f"선택한 부분(없으면 앞부분 약 {PREVIEW_CHARS}자)만 만들어 바로 들려줍니다")
        self.preview_btn.clicked.connect(self.preview)
        self.generate_btn = QPushButton("음성 만들기")
        self.generate_btn.setObjectName("primary")
        self.generate_btn.setToolTip("글 전체를 음성으로 만듭니다 (Ctrl+Enter)")
        self.generate_btn.clicked.connect(self.generate)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_render)
        for b in (self.preview_btn, self.generate_btn, self.cancel_btn):
            row.addWidget(b)
        row.addSpacing(8)

        col = QVBoxLayout()
        col.setSpacing(4)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.progress)
        col.addWidget(self.status)
        row.addLayout(col, 1)
        return w

    def _build_player_row(self) -> QFrame:
        box = QFrame()
        box.setObjectName("player")
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)

        self.play_btn = QPushButton("▶ 재생")
        self.play_btn.setFixedWidth(104)
        self.play_btn.clicked.connect(self.toggle_play)
        self.stop_btn = QPushButton("■")
        self.stop_btn.setToolTip("정지")
        self.stop_btn.setFixedWidth(40)
        self.stop_btn.clicked.connect(self.stop_playback)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1000)
        self.position_slider.sliderPressed.connect(self._seek_started)
        self.position_slider.sliderReleased.connect(self._seek_finished)
        self.time_label = QLabel()
        self.time_label.setMinimumWidth(110)
        self.loaded_label = QLabel()
        self.loaded_label.setObjectName("muted")
        self.full_btn = QPushButton("전체 음성 듣기")
        self.full_btn.setToolTip("미리듣기 대신 마지막으로 만든 전체 음성을 불러옵니다")
        self.full_btn.setVisible(False)
        self.full_btn.clicked.connect(lambda: self._load_player(self.full_result, "full"))
        self.save_btn = QPushButton("저장하기…")
        self.save_btn.setObjectName("primary")
        self.save_btn.setToolTip("마지막으로 만든 전체 음성을 파일로 저장합니다 (Ctrl+S)")
        self.save_btn.clicked.connect(self.save)
        self.send_btn = QPushButton("영상 만들기로 →")
        self.send_btn.setObjectName("primary")
        self.send_btn.setToolTip(
            "마지막으로 만든 전체 음성과 자막을 [영상 만들기] 탭으로 보내 문장마다 장면을 만듭니다.\n"
            "[합성하기] 탭에도 함께 들어갑니다."
        )
        self.send_btn.clicked.connect(self.send_to_video)

        row.addWidget(self.play_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.position_slider, 1)
        row.addWidget(self.time_label)
        row.addWidget(self.loaded_label)
        row.addWidget(self.full_btn)
        row.addSpacing(8)
        row.addWidget(self.save_btn)
        row.addWidget(self.send_btn)
        return box

    # ---- settings -----------------------------------------------------------

    @staticmethod
    def _select(combo: QComboBox, data) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_settings(self, st: pipeline.Settings) -> None:
        self._select(self.voice_combo, st.voice)
        self._select(self.lang_combo, st.lang)
        self.speed_slider.setValue(round(st.speed * 100))
        self.pitch_slider.setValue(round(st.pitch * 2))
        self.volume_slider.setValue(round(st.volume * 100))
        self.sentence_pause.setValue(st.sentence_pause)
        self.paragraph_pause.setValue(st.paragraph_pause)
        self._select(self.quality_combo, st.steps)

    def current_settings(self) -> pipeline.Settings:
        return pipeline.Settings(
            voice=self.voice_combo.currentData(),
            lang=self.lang_combo.currentData(),
            speed=self.speed_slider.value() / 100,
            pitch=self.pitch_slider.value() / 2,
            volume=self.volume_slider.value() / 100,
            steps=self.quality_combo.currentData(),
            sentence_pause=self.sentence_pause.value(),
            paragraph_pause=self.paragraph_pause.value(),
        )

    def _restore(self) -> None:
        s = self.store
        self._apply_settings(
            pipeline.Settings(
                voice=str(s.value("voice", DEFAULTS.voice)),
                lang=str(s.value("lang", DEFAULTS.lang)),
                speed=float(s.value("speed", DEFAULTS.speed)),
                pitch=float(s.value("pitch", DEFAULTS.pitch)),
                volume=float(s.value("volume", DEFAULTS.volume)),
                steps=int(s.value("steps", DEFAULTS.steps)),
                sentence_pause=float(s.value("sentence_pause", DEFAULTS.sentence_pause)),
                paragraph_pause=float(s.value("paragraph_pause", DEFAULTS.paragraph_pause)),
            )
        )
        self._select(self.format_combo, str(s.value("format", "mp3")))
        self.srt_check.setChecked(s.value("srt", True, type=bool))
        self.srt_line.setValue(int(s.value("srt_line", 30)))
        self.srt_line.setEnabled(self.srt_check.isChecked())

    def _restore_script(self) -> None:
        """The script being worked on last time, as it was then."""
        source = WORK_DIR / "대본.txt"
        try:
            text = source.read_text(encoding="utf-8") if source.exists() else ""
        except OSError:
            return
        if text.strip() and not self.editor.toPlainText().strip():
            self.editor.setPlainText(text)
            self._backup_script()

    def _backup_script(self) -> None:
        try:
            autosave.backup(self.editor.toPlainText(), "대본", ".txt")
        except OSError:
            _log_error(traceback.format_exc())  # a full disk must not stop the program from starting or closing

    def _save_script(self) -> None:
        try:
            autosave.write(self.script_path, self.editor.toPlainText())
        except OSError:
            pass  # tried again on the next change and on closing

    def _save_store(self) -> None:
        s = self.store
        st = self.current_settings()
        for key in ("voice", "lang", "speed", "pitch", "volume", "steps", "sentence_pause", "paragraph_pause"):
            s.setValue(key, getattr(st, key))
        s.setValue("format", self.format_combo.currentData())
        s.setValue("srt", self.srt_check.isChecked())
        s.setValue("srt_line", self.srt_line.value())
        s.sync()

    # ---- engine & rendering -------------------------------------------------

    def _load_engine(self) -> None:
        if Engine.is_downloaded():
            self._show_status("음성 모델을 불러오는 중…")
        else:
            self._show_status("음성 모델을 내려받는 중입니다 (처음 한 번만, 약 400MB). 잠시 기다려 주세요…")
        self.progress.setRange(0, 0)
        self.load_thread = LoadThread(self)
        self.load_thread.loaded.connect(self._engine_loaded)
        self.load_thread.failed.connect(self._engine_failed)
        self.load_thread.start()

    def _engine_loaded(self, engine: Engine) -> None:
        self.engine = engine
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._show_status("준비 완료. 글을 입력하고 [음성 만들기]를 누르세요.")
        self._set_busy(False)
        self._on_voice_changed()
        self._run_estimate()

    def _engine_failed(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self._show_status("음성 모델을 불러오지 못했습니다.")
        QMessageBox.critical(
            self,
            APP_TITLE,
            "음성 모델을 불러오지 못했습니다.\n"
            f"처음 실행이라면 인터넷 연결을 확인한 뒤 다시 실행하세요.\n\n{message}",
        )

    def _set_busy(self, busy: bool) -> None:
        ready = self.engine is not None and not busy
        for button in (self.preview_btn, self.generate_btn, self.sample_btn):
            button.setEnabled(ready)
        self.voices_btn.setEnabled(self.engine is not None)
        self.cancel_btn.setEnabled(busy)
        self.save_btn.setEnabled(self.full_result is not None)
        self.send_btn.setEnabled(self.full_result is not None)

    def _segments(self, text: str) -> list[textproc.Segment]:
        return textproc.segment_text(text, self.lang_combo.currentData())

    def generate(self) -> None:
        self._start_render(self._segments(self.editor.toPlainText()), "full")

    def preview(self) -> None:
        selected = self.editor.textCursor().selectedText().replace(" ", "\n")
        if selected.strip():
            self._start_render(self._segments(selected), "preview")
        else:
            segments = self._segments(self.editor.toPlainText())
            self._start_render(textproc.take_preview(segments, PREVIEW_CHARS), "preview")

    def play_voice_sample(self, voice: str | None = None) -> None:
        voice = voice or self.voice_combo.currentData()
        lang = self.lang_combo.currentData()
        if voice == MELO_VOICE or lang == "auto":
            lang = "ko"
        elif lang not in VOICE_SAMPLES:
            lang = "en"
        self._start_render(textproc.segment_text(VOICE_SAMPLES[lang], lang), "voice", voice)

    def _start_render(self, segments: list[textproc.Segment], purpose: str, voice: str | None = None) -> None:
        if self.engine is None or self.render_thread is not None:
            return
        if not segments:
            QMessageBox.information(self, APP_TITLE, "읽을 글을 먼저 입력하세요.")
            return
        settings = self.current_settings()
        if voice:
            settings.voice = voice
        thread = RenderThread(self.engine, segments, settings, purpose, self)
        thread.timing_key = self._timing_key()
        thread.note.connect(self._show_status)
        thread.progress.connect(self._on_progress)
        thread.done.connect(self._on_done)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(lambda: self._show_status("취소했습니다."))
        thread.finished.connect(self._on_thread_finished)
        self.render_thread = thread
        self.progress.setRange(0, len(segments))
        self.progress.setValue(0)
        self._set_busy(True)
        thread.start()

    def cancel_render(self) -> None:
        if self.render_thread is not None:
            self.render_thread.cancel()
            self._show_status("취소하는 중…")

    def _on_progress(self, index: int, total: int, text: str) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(index)
        if text:
            self._show_status(f"{index + 1} / {total} 문장 만드는 중 · {text}")

    def _on_done(self, result: pipeline.Result) -> None:
        thread = self.render_thread
        purpose = thread.purpose
        if purpose == "full":
            self.full_result = result
            self.generated_key = thread.timing_key
            self._show_timeline()
        self._load_player(result, purpose)
        message = (
            f"{PURPOSE_LABELS[purpose]} 완성: {_format_time(result.duration)} 분량 "
            f"({thread.elapsed:.1f}초 걸림)"
        )
        if result.removed_chars:
            message += f" · 읽을 수 없는 문자는 뺐습니다: {' '.join(result.removed_chars[:10])}"
        self._show_status(message)
        if purpose != "full":
            self.toggle_play()

    def _on_failed(self, message: str) -> None:
        self._show_status("음성을 만들지 못했습니다.")
        QMessageBox.warning(self, APP_TITLE, f"음성을 만들지 못했습니다.\n\n{message}")

    def _on_thread_finished(self) -> None:
        self.render_thread.deleteLater()
        self.render_thread = None
        self._set_busy(False)

    # ---- playback -----------------------------------------------------------

    def _load_player(self, result: pipeline.Result | None, purpose: str) -> None:
        if result is None:
            return
        self.player.load(result.audio, result.sample_rate)
        self.loaded_label.setText(PURPOSE_LABELS[purpose])
        self.full_btn.setVisible(purpose != "full" and self.full_result is not None)
        self._tick()

    def toggle_play(self) -> None:
        if self.player.is_playing:
            self.player.pause()
        else:
            try:
                self.player.play()
            except Exception as e:  # noqa: BLE001 - e.g. no audio output device
                QMessageBox.warning(self, APP_TITLE, f"재생할 수 없습니다.\n\n{e}")
        self._tick()

    def stop_playback(self) -> None:
        self.player.stop()
        self._tick()

    def _seek_started(self) -> None:
        self._seeking = True

    def _seek_finished(self) -> None:
        self.player.seek(self.position_slider.value() / 1000 * self.player.duration)
        self._seeking = False
        self._tick()

    def _tick(self) -> None:
        duration = self.player.duration
        playing = self.player.is_playing
        self.play_btn.setText("❚❚ 일시정지" if playing else "▶ 재생")
        for w in (self.play_btn, self.stop_btn, self.position_slider):
            w.setEnabled(duration > 0)
        if self._seeking:
            position = self.position_slider.value() / 1000 * duration
        else:
            position = self.player.position
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(int(position / duration * 1000) if duration else 0)
            self.position_slider.blockSignals(False)
        self.time_label.setText(f"{_format_time(position)} / {_format_time(duration)}")

    # ---- files --------------------------------------------------------------

    def open_file(self, path: str | None = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "텍스트 파일 열기",
                str(self.store.value("open_dir", "")),
                "텍스트 파일 (*.txt);;모든 파일 (*.*)",
            )
            if not path:
                return
        path = Path(path)
        try:
            text = textproc.read_text_file(path)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, f"파일을 열 수 없습니다.\n\n{e}")
            return
        if "\x00" in text:
            QMessageBox.warning(self, APP_TITLE, f"텍스트 파일이 아닌 것 같습니다.\n\n{path.name}")
            return
        # Replace through a cursor so Ctrl+Z can bring the old text back.
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)
        self.source_path = path
        self.store.setValue("open_dir", str(path.parent))
        self._show_status(f"불러왔습니다: {path.name}")

    def clear_text(self) -> None:
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        self.source_path = None

    def save(self) -> None:
        result = self.full_result
        if result is None:
            return
        fmt = self.format_combo.currentData()
        stem = self.source_path.stem if self.source_path else f"음성_{datetime.now():%Y%m%d_%H%M%S}"
        start_dir = Path(str(self.store.value("save_dir", Path.home())))
        filters = ["MP3 파일 (*.mp3)", "WAV 파일 (*.wav)"]
        if fmt == "wav":
            filters.reverse()
        path, chosen = QFileDialog.getSaveFileName(
            self, "음성 저장", str(start_dir / f"{stem}.{fmt}"), ";;".join(filters)
        )
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() not in (".mp3", ".wav"):
            path = path.with_name(path.name + (".wav" if "wav" in chosen.lower() else ".mp3"))
        srt_path = path.with_suffix(".srt") if self.srt_check.isChecked() else None

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            pipeline.export_audio(result, path)
            if srt_path:
                subtitles.write_srt(result.cues, srt_path, self.srt_line.value())
        except Exception as e:  # noqa: BLE001 - shown to the user
            _log_error(traceback.format_exc())
            QMessageBox.critical(self, APP_TITLE, f"저장하지 못했습니다.\n\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.store.setValue("save_dir", str(path.parent))
        self._show_status(f"저장했습니다: {path}" + (" (자막 포함)" if srt_path else ""))
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setText("저장했습니다.\n\n" + str(path) + (f"\n{srt_path}" if srt_path else ""))
        open_btn = box.addButton("폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton("확인", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            subprocess.Popen(["explorer", f"/select,{path}"])

    def send_to_video(self) -> None:
        """Save the voice and its subtitles to the work folder, without asking, and hand them over."""
        result = self.full_result
        if result is None:
            return
        stem = self.source_path.stem if self.source_path else "음성"
        path = WORK_DIR / f"{stem}_{datetime.now():%m%d_%H%M%S}.mp3"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            pipeline.export_audio(result, path)
            subtitles.write_srt(result.cues, path.with_suffix(".srt"), self.srt_line.value())
        except Exception as e:  # noqa: BLE001 - shown to the user
            _log_error(traceback.format_exc())
            QMessageBox.critical(self, APP_TITLE, f"음성을 넘기지 못했습니다.\n\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._show_status(f"영상 탭으로 보냈습니다: {path.name}")
        self.voiceReady.emit(path)

    # ---- misc ---------------------------------------------------------------

    def _update_counts(self) -> None:
        text = self.editor.toPlainText()
        spoken = textproc.PAUSE_MARK.sub("", reading.written(text))
        self._counts = (len(spoken) - spoken.count("\n"), textproc.count_sentences(self._segments(text)))
        self._refresh_count_label()

    def _refresh_count_label(self) -> None:
        chars, sentences = self._counts
        label = f"{chars:,}자 · {sentences:,}문장"
        timeline, exact = self._current_timeline()
        if timeline is not None and timeline.total > 0 and chars:
            label += f" · {'' if exact else '약 '}{format_length(timeline.total)}"
        self.count_label.setText(label)

    # ---- line lengths ---------------------------------------------------------

    def _timing_key(self) -> tuple:
        st = self.current_settings()
        return (self.editor.toPlainText(), st.voice, st.lang, st.speed, st.sentence_pause, st.paragraph_pause, reading.stamp())

    def _current_timeline(self) -> tuple[pipeline.Timeline | None, bool]:
        """The generated audio's timing if it matches the text and settings, else the latest prediction."""
        if self.full_result is not None and self.generated_key == self._timing_key():
            return self.full_result.timeline, True
        return self.estimate, False

    def _show_timeline(self) -> None:
        timeline, exact = self._current_timeline()
        self.editor.set_timeline(timeline, exact)
        self._refresh_count_label()

    def _timing_changed(self, *_args) -> None:
        self._show_timeline()
        self.estimate_timer.start()

    def _run_estimate(self) -> None:
        if self.engine is None:
            return
        settings = self.current_settings()
        if settings.voice == MELO_VOICE and not self.engine.melo_loaded:
            return  # estimated again once the preload finishes
        if self.estimate_thread is not None:
            self.estimate_thread.requestInterruption()
            self._estimate_again = True
            return
        self.estimate_thread = EstimateThread(self.engine, self._timing_key(), settings, self)
        self.estimate_thread.estimated.connect(self._on_estimated)
        self.estimate_thread.finished.connect(self._estimate_finished)
        self.estimate_thread.start()

    def _on_estimated(self, key: tuple, timeline: pipeline.Timeline) -> None:
        if key == self._timing_key():
            self.estimate = timeline
            self._show_timeline()

    def _estimate_finished(self) -> None:
        self.estimate_thread.deleteLater()
        self.estimate_thread = None
        if self._estimate_again:
            self._estimate_again = False
            self._run_estimate()

    def insert_pause(self, seconds: float) -> None:
        self.editor.insertPlainText(f"[쉼 {seconds:g}초]")
        self.editor.setFocus()

    # ---- how it is read --------------------------------------------------------

    def set_reading(self) -> None:
        """How to read the selected words (or the {written|said} mark the cursor is in): there only, or everywhere."""
        cursor = self.editor.textCursor()
        block = self.editor.document().findBlock(cursor.selectionStart())
        if block != self.editor.document().findBlock(cursor.selectionEnd()):
            self._show_status("읽는 법은 한 줄 안에서 고른 글에만 정할 수 있습니다.")
            return
        line = block.text()
        start, end = cursor.selectionStart() - block.position(), cursor.selectionEnd() - block.position()
        inside = next((m for m in reading.READING_MARK.finditer(line) if m.start() <= start and end <= m.end()
                       and (start < m.end() or start == end and m.start() < start)), None)  # fmt: skip
        if inside is not None:
            span, written, said = (inside.start(), inside.end()), inside.group(1), inside.group(2)
        else:
            written = line[start:end]
            start, end = start + len(written) - len(written.lstrip()), end - len(written) + len(written.rstrip())
            written = written.strip()  # a space dragged along at either end stays outside the mark
            if not written:
                self._show_status("읽는 법을 바꿀 글을 먼저 드래그해서 고르세요 (예: 14번).")
                return
            if any(ch in written for ch in "{}|") or any(m.start() < end and start < m.end() for m in reading.READING_MARK.finditer(line)):
                self._show_status("읽는 법 표시({글|읽기})의 일부만 고를 수는 없습니다. 표시 전체나 그 안을 고르세요.")
                return
            span = (start, end)
            said, changes = reading.reader(numbers=True).say(written, "ko")
            said = said if changes else written
        dialog = ReadingDialog(written, said, inside is not None, self)
        if dialog.exec() != ReadingDialog.DialogCode.Accepted:
            return
        if dialog.removed:
            replacement = written
        elif dialog.everywhere.isChecked():
            book = reading.load()
            book.words[written] = dialog.said()
            reading.save(book)
            replacement = written
            self._show_status(f"읽는 법 사전에 넣었습니다: {written} → {dialog.said() or '(읽지 않음)'}")
            self._timing_changed()
        else:
            replacement = reading.mark(written, dialog.said())
        edit = QTextCursor(block)
        edit.setPosition(block.position() + span[0])
        edit.setPosition(block.position() + span[1], QTextCursor.MoveMode.KeepAnchor)
        if edit.selectedText() != replacement:
            edit.insertText(replacement)  # one step to undo
        self.editor.setFocus()

    def edit_dictionary(self) -> None:
        if DictionaryDialog(self).exec() == DictionaryDialog.DialogCode.Accepted:
            self._sync_numbers()
            self._timing_changed()

    def show_readings(self) -> None:
        ReadingsDialog(self._segments(self.editor.toPlainText()), reading.reader(), self).exec()

    def _sync_numbers(self) -> None:
        self.numbers_action.blockSignals(True)
        self.numbers_action.setChecked(reading.load().numbers)
        self.numbers_action.blockSignals(False)

    def _set_numbers(self, on: bool) -> None:
        book = reading.load()
        book.numbers = on
        reading.save(book)
        self._show_status("숫자를 한국어로 읽습니다 (14개 → 열네 개)." if on else "숫자 자동 읽기를 껐습니다 (목소리 모델이 읽는 대로).")
        self._timing_changed()

    def _show_status(self, message: str) -> None:
        self.status.setText(message)
        self.status.setToolTip(message)

    def is_loading(self) -> bool:
        """A model is still loading or downloading; that cannot be interrupted."""
        return any(t is not None and t.isRunning() for t in (self.load_thread, self.melo_thread))

    def shutdown(self) -> None:
        """Called by the main window as it closes."""
        if self.estimate_thread is not None:
            self.estimate_thread.requestInterruption()
            self.estimate_thread.wait(5_000)
        if self.render_thread is not None:
            self.render_thread.cancel()
            self.render_thread.wait(10_000)
        self.player.stop()
        self.script_timer.stop()
        self._save_script()
        self._backup_script()
        self._save_store()
