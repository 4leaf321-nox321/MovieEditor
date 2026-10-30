"""Dialogs for how the voice reads: one piece of the text, the reading dictionary, and what will be said."""

from __future__ import annotations

import html

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from . import reading
from .textproc import Segment


class ReadingDialog(QDialog):
    """How to read one piece of the text: only there (a {written|said} mark) or everywhere (the dictionary)."""

    def __init__(self, text: str, said: str, editing: bool, parent=None):
        super().__init__(parent)
        self.removed = False
        self.setWindowTitle("읽는 법 지정")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        shown = QLabel(f"<b>{html.escape(text)}</b>")
        grid.addWidget(QLabel("글"), 0, 0)
        grid.addWidget(shown, 0, 1)
        self.said_edit = QLineEdit(said)
        self.said_edit.setPlaceholderText("예: 열네 번")
        self.said_edit.selectAll()
        grid.addWidget(QLabel("읽기"), 1, 0)
        grid.addWidget(self.said_edit, 1, 1)
        layout.addLayout(grid)
        hint = QLabel(f"자막에는 '{html.escape(text)}'가 그대로 나오고, 소리만 이렇게 읽습니다. 비워 두면 읽지 않고 넘어갑니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.here = QRadioButton("이 자리만 (대본에 {글|읽기}로 표시)")
        self.everywhere = QRadioButton("읽는 법 사전에 넣기 (모든 대본에서 이렇게 읽음)")
        self.here.setChecked(True)
        layout.addWidget(self.here)
        layout.addWidget(self.everywhere)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("확인")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        if editing:
            remove = buttons.addButton("표시 지우기", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setToolTip("원래대로 읽게 합니다 ({글|읽기} 표시를 글만 남기고 지움).")
            remove.clicked.connect(self._remove)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def said(self) -> str:
        return " ".join(self.said_edit.text().replace("{", "").replace("}", "").replace("|", " ").split())


class DictionaryDialog(QDialog):
    """The reading dictionary: the user's words, the built-in ones, numbers on or off, and a line to try it on."""

    def __init__(self, parent=None):
        super().__init__(parent)
        book = reading.load()
        self.setWindowTitle("읽는 법 사전")
        self.setMinimumSize(560, 600)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.numbers_check = QCheckBox("숫자 자동 읽기 (14개 → 열네 개, 3시 → 세 시, 182 MPa → 백팔십이 메가파스칼)")
        self.numbers_check.setChecked(book.numbers)
        self.numbers_check.setToolTip("한국어 문장의 숫자를 뒤에 오는 말에 맞춰 읽습니다. 다르게 읽을 곳은 사전이나 {글|읽기}로 정합니다.")
        layout.addWidget(self.numbers_check)
        builtin_row = QHBoxLayout()
        self.builtin_check = QCheckBox(f"기본 사전 쓰기 (단위·약어·그리스 문자 {len(reading.BUILTIN)}개)")
        self.builtin_check.setChecked(book.builtin)
        show_builtin = QPushButton("기본 사전 보기")
        show_builtin.setCheckable(True)
        builtin_row.addWidget(self.builtin_check)
        builtin_row.addStretch(1)
        builtin_row.addWidget(show_builtin)
        layout.addLayout(builtin_row)

        self.builtin_table = self._table(sorted(reading.BUILTIN.items(), key=lambda kv: kv[0].lower()), editable=False)
        self.builtin_table.setToolTip("바꾸려면 같은 글을 내 사전에 넣으세요. 내 사전이 이깁니다.")
        self.builtin_table.setVisible(False)
        show_builtin.toggled.connect(self.builtin_table.setVisible)
        layout.addWidget(self.builtin_table, 1)

        own = QGroupBox("내 사전")
        own_layout = QVBoxLayout(own)
        self.table = self._table(sorted(book.words.items()), editable=True)
        own_layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("추가")
        add.clicked.connect(self._add_row)
        remove = QPushButton("지우기")
        remove.clicked.connect(self._remove_rows)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        own_layout.addLayout(row)
        layout.addWidget(own, 2)

        test = QGroupBox("시험해 보기")
        test_layout = QVBoxLayout(test)
        self.test_edit = QLineEdit("최대 응력은 182 MPa, 반복은 14번, 3시 30분에 CAE 해석.")
        self.test_said = QLabel()
        self.test_said.setWordWrap(True)
        test_layout.addWidget(self.test_edit)
        test_layout.addWidget(self.test_said)
        layout.addWidget(test)
        for signal in (self.test_edit.textChanged, self.table.itemChanged, self.numbers_check.toggled, self.builtin_check.toggled):
            signal.connect(self._try)
        self._try()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _table(rows, editable: bool) -> QTableWidget:
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["글", "읽기"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        if not editable:
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, (written, said) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(written))
            table.setItem(r, 1, QTableWidgetItem(said))
        return table

    def _add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(""))
        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.editItem(self.table.item(row, 0))

    def _remove_rows(self) -> None:
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self._try()

    def words(self) -> dict[str, str]:
        words = {}
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c) for c in (0, 1)]
            written, said = (cell.text().strip() if cell is not None else "" for cell in cells)
            if written:
                words[written] = said
        return words

    def _reader(self) -> reading.Reader:
        builtin = reading.BUILTIN if self.builtin_check.isChecked() else {}
        return reading.Reader(self.words(), builtin, self.numbers_check.isChecked())

    def _try(self, *_args) -> None:
        said, changes = self._reader().say(self.test_edit.text(), "ko")
        self.test_said.setText(f"→ {said}" if changes else "→ 그대로 읽습니다")

    def _save(self) -> None:
        reading.save(reading.Dictionary(self.words(), self.builtin_check.isChecked(), self.numbers_check.isChecked()))
        self.accept()


class ReadingsDialog(QDialog):
    """What the voice will say for each sentence, where it differs from what is written."""

    def __init__(self, segments: list[Segment], reader: reading.Reader, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실제로 읽을 글")
        self.setMinimumSize(640, 480)
        layout = QVBoxLayout(self)
        rows, total = [], 0
        for seg in segments:
            if seg.is_pause:
                continue
            total += 1
            said, changes = reader.say(seg.to_read, seg.lang)
            if changes:
                swaps = ", ".join(f"{html.escape(a)} → <b>{html.escape(b)}</b>" for a, b in dict.fromkeys(changes))
                rows.append(f"<p><span style='color:#6b7280'>{seg.line + 1}줄 · {html.escape(seg.text)}</span><br>"
                            f"{html.escape(said)}<br><span style='color:#047857'>{swaps}</span></p>")  # fmt: skip
        summary = QLabel(f"읽는 법이 바뀌는 문장 {len(rows)}개 / 전체 {total}문장. 자막은 쓴 그대로 나옵니다.")
        layout.addWidget(summary)
        view = QTextBrowser()
        view.setHtml("".join(rows) or "<p>모든 문장을 쓴 그대로 읽습니다.</p>")
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
