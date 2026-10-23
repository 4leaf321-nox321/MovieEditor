"""The app window: voice, video and compose tabs, sharing one look and one settings file.

① 음성 만들기 hands its voice and subtitles to ② 영상 만들기 (to time the scenes) and to ③ 합성하기;
② hands its finished video to ③.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QProxyStyle, QStyle, QTabWidget

from tts_app import paths as tts_paths
from tts_app.gui import TtsPage

from . import autosave, paths
from .compose_page import ComposePage
from .scene_page import ScenePage
from .widgets import APP_TITLE, log_error

STYLE = """
QMainWindow, QDialog { background: #f6f7f9; }
QTabWidget::tab-bar { left: 16px; }
QTabBar::tab {
    background: transparent; color: #6b7280; border: none; border-bottom: 3px solid transparent;
    padding: 10px 6px 7px 6px; margin-right: 22px; font-size: 11pt; font-weight: 600;
}
QTabBar::tab:selected { color: #111827; border-bottom-color: #2563eb; }
QTabBar::tab:hover:!selected { color: #374151; }
QGroupBox {
    background: #ffffff; border: 1px solid #e3e5e8; border-radius: 10px;
    margin-top: 14px; padding: 14px 12px 12px 12px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 12px; padding: 0 4px; color: #374151;
}
QFrame#player, QFrame#panel { background: #ffffff; border: 1px solid #e3e5e8; border-radius: 10px; }
QLabel#screen { background: #111827; color: #9ca3af; border-radius: 6px; }
QPlainTextEdit {
    background: #ffffff; border: 1px solid #d9dce1; border-radius: 10px; padding: 10px;
    font-size: 11pt; selection-background-color: #bfdbfe; selection-color: #111827;
}
QPlainTextEdit:focus { border-color: #2563eb; }
QLineEdit { background: #f9fafb; border: 1px solid #d9dce1; border-radius: 6px; padding: 4px 6px; }
QPushButton {
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 7px; padding: 6px 14px;
}
QPushButton:hover { background: #f3f4f6; }
QPushButton:pressed { background: #e5e7eb; }
QPushButton:disabled { color: #9ca3af; background: #f9fafb; border-color: #e5e7eb; }
QToolButton {
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 7px; padding: 6px 14px;
}
QToolButton:hover { background: #f3f4f6; }
QToolButton::menu-indicator { image: none; width: 0px; }
QPushButton#primary {
    background: #2563eb; color: #ffffff; border: none; font-weight: 600; padding: 7px 18px;
}
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: #a5b4fc; color: #eef2ff; }
QProgressBar { background: #e5e7eb; border: none; border-radius: 4px; }
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QLabel#muted { color: #6b7280; }
"""


class _AppStyle(QProxyStyle):
    """Fusion, except that clicking a slider groove jumps straight to that spot."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 820)
        self.store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)

        self.tts_page = TtsPage()
        self.scene_page = ScenePage()
        self.compose_page = ComposePage()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.tts_page, "① 음성 만들기")
        self.tabs.addTab(self.scene_page, "② 영상 만들기")
        self.tabs.addTab(self.compose_page, "③ 영상-음성-자막 합성하기")
        self.tabs.tabBar().setDrawBase(False)
        self.setCentralWidget(self.tabs)
        self.tts_page.voiceReady.connect(self._use_voice)
        self.scene_page.videoReady.connect(self._use_video)
        self.scene_page.subtitle_style = self.compose_page.current_style  # its preview shows the same subtitles

        geometry = self.store.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.tabs.setCurrentIndex(self.store.value("tab", 0, type=int))

    def _use_voice(self, audio: Path) -> None:
        self.tabs.setCurrentWidget(self.scene_page)  # first, so a question about retiming appears over it
        self.compose_page.receive_voice(audio)
        self.scene_page.receive_voice(audio)

    def _use_video(self, handoff: dict) -> None:
        self.compose_page.receive_video(handoff["video"], handoff)
        self.tabs.setCurrentWidget(self.compose_page)

    def closeEvent(self, event) -> None:
        # Each page saves its own work; one failing (say, a vanished audio device) must not stop the others.
        for page in (self.tts_page, self.scene_page, self.compose_page):
            try:
                page.shutdown()
            except Exception:  # noqa: BLE001 - logged; closing goes on
                log_error(traceback.format_exc())
        autosave.session().close()  # closed properly: the next start does not report a crash
        self.store.setValue("geometry", self.saveGeometry())
        self.store.setValue("tab", self.tabs.currentIndex())
        self.store.sync()
        super().closeEvent(event)


def _excepthook(exc_type, exc, tb) -> None:
    log_error("".join(traceback.format_exception(exc_type, exc, tb)))
    QMessageBox.critical(
        None, APP_TITLE, f"예기치 못한 오류가 생겼습니다.\n자세한 내용은 error.log에 남겼습니다.\n\n{exc}"
    )


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setWindowIcon(QIcon(str(tts_paths.ICON_PATH)))
    app.setStyle(_AppStyle("Fusion"))
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setFont(QFont("Malgun Gothic", 10))
    app.setStyleSheet(STYLE)
    sys.excepthook = _excepthook

    window = MainWindow()
    window.show()
    code = app.exec()
    if any(thread.isRunning() for thread in window.findChildren(QThread)):
        # Work that could not be stopped in time (a model still loading, ffmpeg on a slow drive): destroying a
        # running thread would abort the process, so leave without waiting. Settings were saved on close.
        os._exit(code)
    sys.exit(code)
