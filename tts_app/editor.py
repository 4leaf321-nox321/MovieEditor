"""The text box: dropped files, highlighted pause and reading marks, and each line's length beside it."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QToolTip, QWidget

from .pipeline import Timeline
from .reading import READING_MARK
from .textproc import PAUSE_MARK

GUTTER_WIDTH = 78


def format_length(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}초"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}분 {rest:02d}초"


def format_clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


class PauseHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.mark_format = QTextCharFormat()
        self.mark_format.setForeground(QColor("#1d4ed8"))
        self.mark_format.setBackground(QColor("#dbeafe"))
        self.reading_format = QTextCharFormat()  # {14번|열네 번}
        self.reading_format.setForeground(QColor("#047857"))
        self.reading_format.setBackground(QColor("#d1fae5"))

    def highlightBlock(self, text):
        for m in PAUSE_MARK.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self.mark_format)
        for m in READING_MARK.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self.reading_format)


class DurationGutter(QWidget):
    """How long each line takes, drawn to the right of the text."""

    def __init__(self, editor: "TextEditor"):
        super().__init__(editor)
        self.editor = editor
        self.timeline: Timeline | None = None
        self.exact = False  # measured from generated audio rather than predicted

    def paintEvent(self, event):
        if self.timeline is None or not self.timeline.lines:
            return
        ed = self.editor
        painter = QPainter(self)
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() * 0.9)
        painter.setFont(font)
        line_height = ed.fontMetrics().height()
        block = ed.firstVisibleBlock()
        top = ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            info = self.timeline.lines.get(block.blockNumber())
            if info is not None and block.isVisible():
                label = format_length(info.length) if info.speech else f"쉼 {info.length:g}초"
                painter.setPen(QColor("#374151" if self.exact and info.speech else "#9ca3af"))
                painter.drawText(
                    QRect(0, int(top), self.width() - 8, line_height),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )
            top += ed.blockBoundingRect(block).height()
            block = block.next()

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            info = None
            if self.timeline is not None:
                block = self.editor.cursorForPosition(QPoint(4, event.pos().y())).block()
                info = self.timeline.lines.get(block.blockNumber())
            if info is None:
                QToolTip.hideText()
            else:
                basis = "만든 음성 기준" if self.exact else "예상"
                QToolTip.showText(event.globalPos(), f"{format_clock(info.start)} ~ {format_clock(info.end)} ({basis})", self)
            return True
        return super().event(event)


class TextEditor(QPlainTextEdit):
    """Text box that accepts a dropped file, highlights pause marks and shows line lengths."""

    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.context_actions = []  # put at the top of the right-click menu
        self.highlighter = PauseHighlighter(self.document())
        self.gutter = DurationGutter(self)
        self.setViewportMargins(0, 0, GUTTER_WIDTH, 0)
        self.updateRequest.connect(self._update_gutter)

    def set_timeline(self, timeline: Timeline | None, exact: bool) -> None:
        self.gutter.timeline = timeline
        self.gutter.exact = exact
        self.gutter.update()

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        viewport = self.viewport().geometry()
        self.gutter.setGeometry(viewport.right() + 1, viewport.top(), GUTTER_WIDTH, viewport.height())

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu(event.pos())
        if self.context_actions:
            first = menu.actions()[0] if menu.actions() else None
            for action in self.context_actions:
                menu.insertAction(first, action)
            menu.insertSeparator(first)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if files:
            event.acceptProposedAction()
            self.fileDropped.emit(files[0])
        else:
            super().dropEvent(event)
