"""Settings panels of the video tab beside the basic scene settings: words, overlays, hidden parts,
corrections, and the project's fades, music, logo and subtitles.

Each panel edits its own copy of what it shows and says so through `edited(key)`; the page then copies
the values back into the project (the key lets a burst of edits become one undo step).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import keyframes
from .scenes import KEY_NEAR, TEXT_PRESETS, Mask, Overlay, Project, Scene, TextBox, TextStyle, key_at
from .widgets import ColorButton, select

TEXT_EFFECTS = [
    ("none", "없음 (처음부터 보임)"),
    ("fade", "서서히 나타남"),
    ("pop", "톡 튀어나옴"),
    ("slide", "아래에서 올라옴"),
    ("type", "한 글자씩 (타자기)"),
]
IMAGE_EFFECTS = TEXT_EFFECTS[:4]
VIDEO_EFFECTS = TEXT_EFFECTS[:2]
EXITS = [
    ("none", "없음 (끝까지 보임)"),
    ("fade", "서서히 사라짐"),
    ("pop", "작아지며 사라짐"),
    ("slide", "아래로 내려가며 사라짐"),
]
VIDEO_EXITS = EXITS[:2]
MASKS = [("mosaic", "모자이크"), ("blur", "흐리게")]
CORNERS = [("center", "가운데"), ("top_left", "왼쪽 위"), ("top_right", "오른쪽 위"), ("bottom_left", "왼쪽 아래"),
           ("bottom_right", "오른쪽 아래")]  # fmt: skip
LOGO_CORNERS = CORNERS[1:]
LOOKS = [
    ("none", "없음"),
    ("bw", "흑백"),
    ("sepia", "세피아 (옛날 사진)"),
    ("warm", "따뜻하게"),
    ("cool", "차갑게"),
    ("vivid", "선명하게"),
    ("vintage", "빈티지"),
]
EDGE = 0.04  # corner presets leave this share of the frame free


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    combo = QComboBox()
    for code, label in items:
        combo.addItem(label, code)
    return combo


def _row(*widgets: QWidget, stretch: bool = True) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    for widget in widgets:
        row.addWidget(widget)
    if stretch:
        row.addStretch(1)
    return w


def _percent(lo: int, hi: int, suffix: str = " %") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(lo, hi)
    spin.setSuffix(suffix)
    return spin


def _slider(lo: int, hi: int, fmt: Callable[[int], str]) -> tuple[QSlider, QWidget]:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(lo, hi)
    label = QLabel(fmt(slider.value()))
    label.setMinimumWidth(44)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    slider.valueChanged.connect(lambda v: label.setText(fmt(v)))
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(slider, 1)
    row.addWidget(label)
    return slider, w


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


def corner_spot(corner: str, width_share: float, height_share: float) -> tuple[float, float]:
    """The centre that puts something of that size (shares of the frame) in a corner."""
    if corner == "center":
        return 0.5, 0.5
    x = EDGE + width_share / 2 if corner.endswith("left") else 1 - EDGE - width_share / 2
    y = EDGE + height_share / 2 if corner.startswith("top") else 1 - EDGE - height_share / 2
    return x, y


class _ListPanel(QWidget):
    """A list of items (text blocks, overlays, hidden parts) with an editor for the chosen one.

    Text blocks and overlays can move by keyframes: `now` tells the playhead's time in the scene, where
    keyframes are added and where moving the item changes its keyframe.
    """

    edited = Signal(str)
    selected = Signal(int)
    seek = Signal(float)  # seconds into the scene: show the moment of a keyframe
    KEYED_HINT = "옮기면"  # what changes the keyframe at the playhead, said under the keyframes

    def __init__(self, now: Callable[[], float] | None = None, parent=None):
        super().__init__(parent)
        self.now = now
        self._items: list = []
        self._loading = False
        self.keys_label: QLabel | None = None  # set by _build_keys
        self.list = QListWidget()
        self.list.setFixedHeight(84)
        self.list.currentRowChanged.connect(self._on_row)

    def items(self) -> list:
        return copy.deepcopy(self._items)

    def index(self) -> int:
        return self.list.currentRow()

    def show_items(self, items: list, index: int | None = None) -> None:
        self._items = copy.deepcopy(items)
        keep = self.list.currentRow() if index is None else index
        self._loading = True
        self.list.clear()
        for i, item in enumerate(self._items):
            self.list.addItem(self._label(i, item))
        self.list.setCurrentRow(min(max(keep, 0), len(self._items) - 1) if self._items else -1)
        self._loading = False
        self._load_editor()

    def select(self, index: int) -> None:
        if 0 <= index < len(self._items):
            self.list.setCurrentRow(index)

    def _on_row(self, row: int) -> None:
        if self._loading:
            return
        self._load_editor()
        self.selected.emit(row)

    def _current(self):
        row = self.list.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _changed(self, key: str) -> None:
        """The editor changed the chosen item (only what `key` names: other boxes show rounded values)."""
        if self._loading or self._current() is None:
            return
        self._from_editor(self._current(), key)
        row = self.list.currentRow()
        self.list.item(row).setText(self._label(row, self._current()))
        if self.keys_label is not None:
            self._show_keys(self._current())  # moving it may have added a keyframe
        self.edited.emit(f"{key}:{row}")

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._items):
            del self._items[row]
            self.show_items(self._items, min(row, len(self._items) - 1))
            self.edited.emit("remove")

    def _append(self, item) -> None:
        self._items.append(item)
        self.show_items(self._items, len(self._items) - 1)
        self.edited.emit("add")

    # ---- keyframes (text blocks and overlays) ----------------------------------

    def _build_keys(self, form: QFormLayout) -> None:
        """The 움직임 rows: keyframes of the chosen item at the playhead."""
        self.key_add_btn = QPushButton("◆ 키프레임")
        self.key_add_btn.setToolTip("지금 시각(타임라인의 빨간 선)의 위치와 크기를 키프레임으로 기억합니다.\n"
                                    "다른 시각으로 가서 옮기면 그 사이를 움직입니다.")  # fmt: skip
        self.key_add_btn.clicked.connect(self._add_key)
        self.key_remove_btn = QPushButton("삭제")
        self.key_remove_btn.setToolTip("지금 시각의 키프레임을 지웁니다")
        self.key_remove_btn.clicked.connect(self._remove_key)
        self.key_prev_btn, self.key_next_btn = QPushButton("◀"), QPushButton("▶")
        for button, step, tip in ((self.key_prev_btn, -1, "앞 키프레임으로"), (self.key_next_btn, 1, "다음 키프레임으로")):
            button.setFixedWidth(34)
            button.setStyleSheet("padding: 6px 2px;")
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=step: self._jump(s))
        form.addRow("움직임", _row(self.key_add_btn, self.key_remove_btn, self.key_prev_btn, self.key_next_btn))
        self.smooth_check = QCheckBox("천천히 출발·도착")
        self.smooth_check.setToolTip("끄면 처음부터 끝까지 같은 빠르기로 움직입니다")
        self.smooth_check.toggled.connect(lambda: self._changed("smooth"))
        self.key_clear_btn = QPushButton("모두 지우기")
        self.key_clear_btn.setToolTip("키프레임을 모두 지우고, 지금 보이는 자리에 멈춰 둡니다")
        self.key_clear_btn.clicked.connect(self._clear_keys)
        form.addRow("", _row(self.smooth_check, self.key_clear_btn))
        self.keys_label = _hint("")
        form.addRow(self.keys_label)

    def _now(self) -> float:
        return self.now() if self.now else 0.0

    def _key_here(self, item) -> int | None:
        t = self._now()
        return next((i for i, k in enumerate(item.keys) if abs(k.t - t) < KEY_NEAR), None)

    def _place_now(self, item) -> tuple[float, float]:
        """Where the item is at the playhead."""
        where = keyframes.sample(item.keys, self._now(), item.smooth)
        return (where[0], where[1]) if where else (item.x, item.y)

    def _move(self, item, x: float, y: float, key: str) -> None:
        """Put the item at x, y (shares of the frame); when it moves by keyframes, in the one at the playhead."""
        target = key_at(item, self._now()) if item.keys else item
        if key in ("x", "place"):
            target.x = x
        if key in ("y", "place"):
            target.y = y

    def _add_key(self) -> None:
        item = self._current()
        if item is not None and self._key_here(item) is None:
            key_at(item, self._now())
            self._keys_changed()

    def _remove_key(self) -> None:
        item = self._current()
        index = self._key_here(item) if item is not None else None
        if index is None:
            return
        if len(item.keys) == 1:  # the last one: the item stays where it was shown
            item.x, item.y = item.keys[0].x, item.keys[0].y
        del item.keys[index]
        self._keys_changed()

    def _clear_keys(self) -> None:
        item = self._current()
        if item is None or not item.keys:
            return
        x, y = self._place_now(item)
        item.x, item.y = round(x, 4), round(y, 4)
        item.keys = []
        self._keys_changed()

    def _jump(self, step: int) -> None:
        item = self._current()
        if item is None:
            return
        t = self._now()
        times = [k.t for k in item.keys if (k.t > t + KEY_NEAR if step > 0 else k.t < t - KEY_NEAR)]
        if times:
            self.seek.emit(min(times) if step > 0 else max(times))

    def _keys_changed(self) -> None:
        row = self.list.currentRow()
        self._load_editor()
        self.edited.emit(f"keys:{row}")

    def _show_keys(self, item) -> None:
        """The keyframe rows brought in line with the item and the playhead."""
        keys = item.keys if item is not None else []
        t = self._now()
        here = item is not None and self._key_here(item) is not None
        self.key_remove_btn.setEnabled(here)
        self.key_clear_btn.setEnabled(bool(keys))
        self.smooth_check.setEnabled(len(keys) > 1)
        self.key_prev_btn.setEnabled(any(k.t < t - KEY_NEAR for k in keys))
        self.key_next_btn.setEnabled(any(k.t > t + KEY_NEAR for k in keys))
        if not keys:
            text = "키프레임이 없어 장면 내내 한 자리에 있습니다."
        else:
            times = " · ".join(f"{round(k.t, 2):g}초" for k in keys[:8]) + (" …" if len(keys) > 8 else "")
            text = f"키프레임 {len(keys)}개: {times}\n지금 {round(t, 2):g}초" + (" (키프레임 위)" if here else "")
            if len(keys) == 1:
                text += "\n다른 시각으로 가서 옮기면 그 사이를 움직입니다."
            else:
                text += f"\n{self.KEYED_HINT} 지금 시각의 키프레임이 바뀝니다 (없으면 생김)."
        self.keys_label.setText(text)

    def time_changed(self) -> None:
        """The playhead moved: show where the chosen item is then, and the keyframes around."""
        item = self._current()
        if self.keys_label is None or item is None:
            return
        self._show_keys(item)
        if item.keys:
            self._loading = True
            x, y = self._place_now(item)
            for spin, value in ((self.x_spin, round(x * 100)), (self.y_spin, round(y * 100))):
                if spin.value() != value:
                    spin.setValue(value)
            self._loading = False

    # Subclasses fill these in.
    def _label(self, index: int, item) -> str:
        raise NotImplementedError

    def _load_editor(self) -> None:
        raise NotImplementedError

    def _from_editor(self, item, key: str) -> None:
        raise NotImplementedError


class TextPanel(_ListPanel):
    """The scene's words: several blocks, each with its own look, place and entrance."""

    styleToAll = Signal(object)  # TextStyle

    def __init__(self, default_style: Callable[[], TextStyle], now: Callable[[], float] | None = None, parent=None):
        super().__init__(now, parent)
        self.default_style = default_style
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.list)
        add_btn = QPushButton("+ 글자 추가")
        add_btn.clicked.connect(self.add_box)
        self.remove_btn = QPushButton("삭제")
        self.remove_btn.clicked.connect(self._remove)
        layout.addWidget(_row(add_btn, self.remove_btn))

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("화면에 띄울 글자")
        self.text_edit.setFixedHeight(70)
        form.addRow("글자", self.text_edit)
        self.font_combo = QFontComboBox()
        self.font_combo.setWritingSystem(QFontDatabase.WritingSystem.Korean)
        self.font_combo.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
        self.font_combo.setMaxVisibleItems(20)
        self.font_combo.setMinimumWidth(10)
        form.addRow("글꼴", self.font_combo)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(12, 300)
        self.size_spin.setToolTip("1080p 기준 크기입니다. 세로 영상에서도 같은 비율로 보입니다.")
        self.bold_check = QCheckBox("굵게")
        self.color_btn = ColorButton("글자색")
        form.addRow("크기", _row(self.size_spin, self.bold_check, self.color_btn))
        presets = []
        for code, label in (("top", "위"), ("center", "가운데"), ("bottom", "아래")):
            button = QPushButton(label)
            button.setStyleSheet("padding: 6px 8px;")
            button.clicked.connect(lambda _=False, c=code: self._preset(c))
            presets.append(button)
        form.addRow("위치", _row(*presets))
        self.x_spin = _percent(0, 100)
        self.x_spin.setPrefix("가로 ")
        self.y_spin = _percent(0, 100)
        self.y_spin.setPrefix("세로 ")
        form.addRow("", _row(self.x_spin, self.y_spin))
        self.effect_combo = _combo(TEXT_EFFECTS)
        self.effect_combo.setToolTip("장면이 시작될 때 글자가 나타나는 방법")
        form.addRow("나타남", self.effect_combo)
        self.exit_combo = _combo(EXITS)
        self.exit_combo.setToolTip("장면이 끝날 때 글자가 사라지는 방법 (장면 끝에 맞춰 0.5초쯤)")
        form.addRow("사라짐", self.exit_combo)
        self._build_keys(form)
        layout.addWidget(self.editor)
        to_all = QPushButton("이 글자 모양을 모든 장면에 적용")
        to_all.setToolTip("글꼴, 크기, 굵기, 색을 모든 장면의 글자에 똑같이 적용합니다. 내용과 위치는 그대로 둡니다.")
        to_all.clicked.connect(self._style_to_all)
        layout.addWidget(to_all)
        layout.addWidget(_hint("미리보기에서 글자를 끌어 옮길 수 있습니다."))
        layout.addStretch(1)

        self.text_edit.textChanged.connect(lambda: self._changed("text"))
        self.font_combo.currentFontChanged.connect(lambda: self._changed("font"))
        self.size_spin.valueChanged.connect(lambda: self._changed("size"))
        self.bold_check.toggled.connect(lambda: self._changed("bold"))
        self.color_btn.colorChanged.connect(lambda: self._changed("color"))
        self.x_spin.valueChanged.connect(lambda: self._changed("x"))
        self.y_spin.valueChanged.connect(lambda: self._changed("y"))
        self.effect_combo.currentIndexChanged.connect(lambda: self._changed("effect"))
        self.exit_combo.currentIndexChanged.connect(lambda: self._changed("exit"))
        self._load_editor()

    def add_box(self, text: str = "글자") -> None:
        y = 0.5 if not self._items else min(0.85, self._items[-1].y + 0.15)
        self._append(TextBox(text=text if isinstance(text, str) else "글자", style=self.default_style(), y=y))
        self.text_edit.setFocus()
        self.text_edit.selectAll()

    def _label(self, index: int, box: TextBox) -> str:
        first = box.text.strip().splitlines()[0] if box.text.strip() else "(빈 글자)"
        return f"{index + 1}. {first}"

    def _load_editor(self) -> None:
        box = self._current()
        self.editor.setEnabled(box is not None)
        self.remove_btn.setEnabled(box is not None)
        if box is None:
            return
        self._loading = True
        if self.text_edit.toPlainText() != box.text:
            self.text_edit.setPlainText(box.text)
        self.font_combo.setCurrentFont(QFont(box.style.font))
        self.size_spin.setValue(box.style.size)
        self.bold_check.setChecked(box.style.bold)
        self.color_btn.set_color(box.style.color)
        x, y = self._place_now(box)
        self.x_spin.setValue(round(x * 100))
        self.y_spin.setValue(round(y * 100))
        select(self.effect_combo, box.effect)
        select(self.exit_combo, box.exit)
        self.smooth_check.setChecked(box.smooth)
        self._loading = False
        self._show_keys(box)

    def _from_editor(self, box: TextBox, key: str) -> None:
        if key == "text":
            box.text = self.text_edit.toPlainText()
        elif key == "font":
            box.style.font = self.font_combo.currentFont().family()
        elif key == "size":
            box.style.size = self.size_spin.value()
        elif key == "bold":
            box.style.bold = self.bold_check.isChecked()
        elif key == "color":
            box.style.color = self.color_btn.color()
        elif key in ("x", "y", "place"):
            self._move(box, self.x_spin.value() / 100, self.y_spin.value() / 100, key)
        elif key == "effect":
            box.effect = self.effect_combo.currentData()
        elif key == "exit":
            box.exit = self.exit_combo.currentData()
        elif key == "smooth":
            box.smooth = self.smooth_check.isChecked()

    def _preset(self, code: str) -> None:
        if self._current() is None:
            return
        self._loading = True
        self.x_spin.setValue(50)
        self.y_spin.setValue(round(TEXT_PRESETS[code] * 100))
        self._loading = False
        self._changed("place")

    def _style_to_all(self) -> None:
        box = self._current()
        if box is not None:
            self.styleToAll.emit(copy.deepcopy(box.style))


class OverlayPanel(_ListPanel):
    """Pictures and videos laid over the scene."""

    KEYED_HINT = "옮기거나 미리보기에서 휠로 크기를 바꾸면"

    def __init__(self, ask_file: Callable[[str], str | None], height_share: Callable[[Overlay], float],
                 now: Callable[[], float] | None = None, parent=None):  # fmt: skip
        super().__init__(now, parent)
        self.ask_file = ask_file  # "image" or "clip" -> a file the user picked, or None
        self.height_share = height_share  # an overlay's height as a share of the frame's
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.list)
        image_btn = QPushButton("+ 그림")
        image_btn.setToolTip("로고, 스티커, 사진을 장면 위에 올립니다 (PNG 투명 배경 그대로)")
        image_btn.clicked.connect(lambda: self._add("image"))
        clip_btn = QPushButton("+ 영상")
        clip_btn.setToolTip("화면 속 작은 화면으로 영상을 올립니다 (소리는 넣지 않습니다)")
        clip_btn.clicked.connect(lambda: self._add("clip"))
        self.remove_btn = QPushButton("삭제")
        self.remove_btn.clicked.connect(self._remove)
        layout.addWidget(_row(image_btn, clip_btn, self.remove_btn))

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.file_label = QLabel()
        self.file_label.setMinimumWidth(10)
        change_btn = QPushButton("바꾸기…")
        change_btn.clicked.connect(self._change_file)
        form.addRow("파일", _row(self.file_label, change_btn))
        self.width_spin = _percent(3, 100)
        self.width_spin.setToolTip("화면 너비에 대한 크기 (미리보기에서 마우스 휠로도 바꿉니다)")
        form.addRow("크기", self.width_spin)
        self.opacity_slider, opacity_row = _slider(5, 100, lambda v: f"{v}%")
        form.addRow("불투명도", opacity_row)
        self.corner_combo = _combo([("", "직접 (끌어서 옮김)"), *CORNERS])
        form.addRow("위치", self.corner_combo)
        self.x_spin = _percent(-50, 150)
        self.x_spin.setPrefix("가로 ")
        self.y_spin = _percent(-50, 150)
        self.y_spin.setPrefix("세로 ")
        form.addRow("", _row(self.x_spin, self.y_spin))
        self.border_check = QCheckBox("흰 테두리")
        form.addRow("", self.border_check)
        self.chroma_check = QCheckBox("이 색을 투명하게")
        self.chroma_check.setToolTip(
            "초록 화면(그린 스크린) 같은 배경색을 빼서 뒤의 장면이 보이게 합니다.\n"
            "색 버튼의 색 고르기 창에서 [Pick Screen Color]를 누르면 미리보기 속 색을 바로 찍을 수 있습니다."
        )
        self.chroma_btn = ColorButton("투명하게 할 색")
        form.addRow("크로마키", _row(self.chroma_check, self.chroma_btn))
        self.chroma_slider, self.chroma_row = _slider(1, 100, lambda v: f"{v}%")
        self.chroma_slider.setToolTip("클수록 비슷한 색까지 더 많이 빠집니다. 사람이나 물건까지 비치면 줄이세요.")
        form.addRow("빼는 범위", self.chroma_row)
        self.effect_combo = _combo(IMAGE_EFFECTS)
        form.addRow("나타남", self.effect_combo)
        self.exit_combo = _combo(EXITS)
        form.addRow("사라짐", self.exit_combo)
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 36000.0)
        self.start_spin.setDecimals(1)
        self.start_spin.setSingleStep(0.5)
        self.start_spin.setSuffix(" 초부터")
        self.start_spin.setToolTip("겹친 영상의 어디서부터 재생할지. 장면보다 짧으면 마지막 화면에서 멈춥니다.")
        form.addRow("재생", self.start_spin)
        self._build_keys(form)
        self.form = form
        layout.addWidget(self.editor)
        layout.addWidget(_hint("미리보기에서 끌어 옮기고, 마우스 휠로 크기를 바꿀 수 있습니다."))
        layout.addStretch(1)

        self.chroma_check.toggled.connect(lambda: self._changed("chroma"))
        self.chroma_btn.colorChanged.connect(lambda: self._changed("chroma"))
        self.chroma_slider.valueChanged.connect(lambda: self._changed("chroma_strength"))
        self.width_spin.valueChanged.connect(lambda: self._changed("width"))
        self.opacity_slider.valueChanged.connect(lambda: self._changed("opacity"))
        self.corner_combo.currentIndexChanged.connect(self._corner)
        self.x_spin.valueChanged.connect(lambda: self._changed("x"))
        self.y_spin.valueChanged.connect(lambda: self._changed("y"))
        self.border_check.toggled.connect(lambda: self._changed("border"))
        self.effect_combo.currentIndexChanged.connect(lambda: self._changed("effect"))
        self.exit_combo.currentIndexChanged.connect(lambda: self._changed("exit"))
        self.start_spin.valueChanged.connect(lambda: self._changed("start"))
        self._load_editor()

    def _add(self, kind: str) -> None:
        path = self.ask_file(kind)
        if path:
            overlay = Overlay(source=path, kind=kind, width=0.25 if kind == "image" else 0.35)
            overlay.x, overlay.y = corner_spot("top_right", overlay.width, self.height_share(overlay))
            self._append(overlay)

    def _change_file(self) -> None:
        overlay = self._current()
        if overlay is None:
            return
        path = self.ask_file(overlay.kind)
        if path:
            overlay.source = path
            self._load_editor()
            self.list.item(self.list.currentRow()).setText(self._label(self.list.currentRow(), overlay))
            self.edited.emit(f"file:{self.list.currentRow()}")

    def _label(self, index: int, overlay: Overlay) -> str:
        kind = "그림" if overlay.kind == "image" else "영상"
        return f"{index + 1}. [{kind}] {Path(overlay.source).name}"

    def _load_editor(self) -> None:
        overlay = self._current()
        self.editor.setEnabled(overlay is not None)
        self.remove_btn.setEnabled(overlay is not None)
        if overlay is None:
            self.file_label.setText("")
            return
        self._loading = True
        self.file_label.setText(Path(overlay.source).name)
        self.file_label.setToolTip(overlay.source)
        self.width_spin.setValue(round(overlay.width * 100))
        self.opacity_slider.setValue(round(overlay.opacity * 100))
        self.corner_combo.setCurrentIndex(0)
        x, y = self._place_now(overlay)
        self.x_spin.setValue(round(x * 100))
        self.y_spin.setValue(round(y * 100))
        self.border_check.setChecked(overlay.border)
        self.chroma_check.setChecked(bool(overlay.chroma))
        self.chroma_btn.set_color(overlay.chroma or "#00ff00")
        self.chroma_slider.setValue(round(overlay.chroma_strength * 100))
        self.chroma_btn.setEnabled(bool(overlay.chroma))
        self.chroma_row.setEnabled(bool(overlay.chroma))
        self.smooth_check.setChecked(overlay.smooth)
        image = overlay.kind == "image"
        for combo, items, value in ((self.effect_combo, IMAGE_EFFECTS if image else VIDEO_EFFECTS, overlay.effect),
                                    (self.exit_combo, EXITS if image else VIDEO_EXITS, overlay.exit)):  # fmt: skip
            combo.clear()
            for code, label in items:
                combo.addItem(label, code)
            select(combo, value)
        self.start_spin.setValue(overlay.clip_start)
        self.form.setRowVisible(self.start_spin, overlay.kind == "clip")
        self._loading = False
        self._show_keys(overlay)

    def _from_editor(self, overlay: Overlay, key: str) -> None:
        if key == "width":
            overlay.width = self.width_spin.value() / 100
        elif key == "opacity":
            overlay.opacity = self.opacity_slider.value() / 100
        elif key in ("x", "y", "place"):
            self._move(overlay, self.x_spin.value() / 100, self.y_spin.value() / 100, key)
        elif key == "chroma":
            on = self.chroma_check.isChecked()
            overlay.chroma = self.chroma_btn.color() if on else ""
            self.chroma_btn.setEnabled(on)
            self.chroma_row.setEnabled(on)
        elif key == "chroma_strength":
            overlay.chroma_strength = self.chroma_slider.value() / 100
        elif key == "smooth":
            overlay.smooth = self.smooth_check.isChecked()
        elif key == "border":
            overlay.border = self.border_check.isChecked()
        elif key == "effect":
            overlay.effect = self.effect_combo.currentData() or "none"
        elif key == "exit":
            overlay.exit = self.exit_combo.currentData() or "none"
        elif key == "start":
            overlay.clip_start = self.start_spin.value()

    def _corner(self) -> None:
        overlay, corner = self._current(), self.corner_combo.currentData()
        if self._loading or overlay is None or not corner:
            return
        x, y = corner_spot(corner, overlay.width, self.height_share(overlay))
        self._loading = True
        self.x_spin.setValue(round(x * 100))
        self.y_spin.setValue(round(y * 100))
        self._loading = False
        self._changed("place")


class MaskPanel(_ListPanel):
    """Parts of the scene's picture hidden under a mosaic or a blur (faces, number plates, addresses)."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.list)
        mosaic_btn = QPushButton("+ 모자이크")
        mosaic_btn.clicked.connect(lambda: self._add("mosaic"))
        blur_btn = QPushButton("+ 흐리게")
        blur_btn.clicked.connect(lambda: self._add("blur"))
        self.remove_btn = QPushButton("삭제")
        self.remove_btn.clicked.connect(self._remove)
        layout.addWidget(_row(mosaic_btn, blur_btn, self.remove_btn))

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.kind_combo = _combo(MASKS)
        form.addRow("방법", self.kind_combo)
        self.strength_slider, strength_row = _slider(0, 100, lambda v: f"{v}%")
        self.strength_slider.setToolTip("클수록 모자이크 칸이 커지고, 더 흐려집니다")
        form.addRow("세기", strength_row)
        self.x_spin = _percent(0, 100)
        self.x_spin.setPrefix("가로 ")
        self.y_spin = _percent(0, 100)
        self.y_spin.setPrefix("세로 ")
        form.addRow("위치", _row(self.x_spin, self.y_spin))
        self.width_spin = _percent(2, 100)
        self.width_spin.setPrefix("너비 ")
        self.height_spin = _percent(2, 100)
        self.height_spin.setPrefix("높이 ")
        form.addRow("크기", _row(self.width_spin, self.height_spin))
        layout.addWidget(self.editor)
        layout.addWidget(_hint("미리보기에서 상자를 끌어 옮기고, 모서리를 끌거나 마우스 휠을 돌려 크기를 바꿉니다. "
                               "장면 내내 같은 자리를 가립니다. 위에 올린 글자와 그림은 가리지 않습니다."))  # fmt: skip
        layout.addStretch(1)

        self.kind_combo.currentIndexChanged.connect(lambda: self._changed("kind"))
        self.strength_slider.valueChanged.connect(lambda: self._changed("strength"))
        self.x_spin.valueChanged.connect(lambda: self._changed("x"))
        self.y_spin.valueChanged.connect(lambda: self._changed("y"))
        self.width_spin.valueChanged.connect(lambda: self._changed("width"))
        self.height_spin.valueChanged.connect(lambda: self._changed("height"))
        self._load_editor()

    def _add(self, kind: str) -> None:
        shift = 0.05 * (len(self._items) % 5)  # not right on top of the last one
        self._append(Mask(x=0.5 + shift, y=0.5 + shift, kind=kind))

    def _label(self, index: int, mask: Mask) -> str:
        return f"{index + 1}. {dict(MASKS).get(mask.kind, mask.kind)}"

    def _load_editor(self) -> None:
        mask = self._current()
        self.editor.setEnabled(mask is not None)
        self.remove_btn.setEnabled(mask is not None)
        if mask is None:
            return
        self._loading = True
        select(self.kind_combo, mask.kind)
        self.strength_slider.setValue(round(mask.strength * 100))
        self.x_spin.setValue(round(mask.x * 100))
        self.y_spin.setValue(round(mask.y * 100))
        self.width_spin.setValue(round(mask.width * 100))
        self.height_spin.setValue(round(mask.height * 100))
        self._loading = False

    def _from_editor(self, mask: Mask, key: str) -> None:
        if key == "kind":
            mask.kind = self.kind_combo.currentData()
        elif key == "strength":
            mask.strength = self.strength_slider.value() / 100
        elif key == "x":
            mask.x = self.x_spin.value() / 100
        elif key == "y":
            mask.y = self.y_spin.value() / 100
        elif key == "width":
            mask.width = self.width_spin.value() / 100
        elif key == "height":
            mask.height = self.height_spin.value() / 100


class AdjustPanel(QWidget):
    """Corrections to the scene's image or clip."""

    edited = Signal(str)
    toAll = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.note = _hint("")
        layout.addWidget(self.note)
        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.rotate_combo = _combo([(0, "그대로"), (90, "오른쪽으로 90°"), (180, "180°"), (270, "왼쪽으로 90°")])
        form.addRow("회전", self.rotate_combo)
        self.flip_h_check = QCheckBox("좌우")
        self.flip_v_check = QCheckBox("상하")
        form.addRow("뒤집기", _row(self.flip_h_check, self.flip_v_check))
        self.crop_spins = []
        for side in ("왼쪽 ", "오른쪽 ", "위 ", "아래 "):
            spin = _percent(0, 45)
            spin.setPrefix(side)
            self.crop_spins.append(spin)
        left, right, top, bottom = self.crop_spins
        form.addRow("잘라내기", _row(left, right))
        form.addRow("", _row(top, bottom))
        self.brightness_slider, brightness_row = _slider(-50, 50, lambda v: f"{v:+d}")
        form.addRow("밝기", brightness_row)
        self.contrast_slider, contrast_row = _slider(50, 200, lambda v: f"{v}%")
        form.addRow("대비", contrast_row)
        self.saturation_slider, saturation_row = _slider(0, 200, lambda v: f"{v}%")
        form.addRow("채도", saturation_row)
        self.look_combo = _combo(LOOKS)
        form.addRow("필터", self.look_combo)
        layout.addWidget(self.editor)
        reset_btn = QPushButton("보정 초기화")
        reset_btn.clicked.connect(self.reset)
        all_btn = QPushButton("모든 장면에 적용")
        all_btn.setToolTip("이 보정(회전·뒤집기·잘라내기 빼고)을 이미지와 영상 장면 모두에 적용합니다")
        all_btn.clicked.connect(self.toAll.emit)
        self.buttons = _row(reset_btn, all_btn)
        layout.addWidget(self.buttons)
        layout.addStretch(1)

        self.rotate_combo.currentIndexChanged.connect(lambda: self._changed("rotate"))
        for check in (self.flip_h_check, self.flip_v_check):
            check.toggled.connect(lambda: self._changed("flip"))
        for spin in self.crop_spins:
            spin.valueChanged.connect(lambda: self._changed("crop"))
        self.brightness_slider.valueChanged.connect(lambda: self._changed("brightness"))
        self.contrast_slider.valueChanged.connect(lambda: self._changed("contrast"))
        self.saturation_slider.valueChanged.connect(lambda: self._changed("saturation"))
        self.look_combo.currentIndexChanged.connect(lambda: self._changed("look"))

    def load(self, scene: Scene | None) -> None:
        usable = scene is not None and scene.kind in ("image", "clip") and bool(scene.source)
        self.editor.setEnabled(usable)
        self.buttons.setEnabled(usable)
        self.note.setText("이미지나 영상 클립 장면에서 쓸 수 있습니다." if not usable else
                          "이 장면의 이미지나 클립에 적용합니다. 위에 올린 글자와 그림은 그대로입니다.")  # fmt: skip
        if scene is None:
            return
        self._loading = True
        select(self.rotate_combo, scene.rotate)
        self.flip_h_check.setChecked(scene.flip_h)
        self.flip_v_check.setChecked(scene.flip_v)
        left, top, right, bottom = scene.crop
        for spin, value in zip(self.crop_spins, (left, right, top, bottom)):
            spin.setValue(round(value * 100))
        self.brightness_slider.setValue(round(scene.brightness * 100))
        self.contrast_slider.setValue(round(scene.contrast * 100))
        self.saturation_slider.setValue(round(scene.saturation * 100))
        select(self.look_combo, scene.look)
        self._loading = False

    def values(self) -> dict:
        left, right, top, bottom = (s.value() / 100 for s in self.crop_spins)
        return {
            "rotate": self.rotate_combo.currentData(),
            "flip_h": self.flip_h_check.isChecked(),
            "flip_v": self.flip_v_check.isChecked(),
            "crop": [left, top, right, bottom],
            "brightness": self.brightness_slider.value() / 100,
            "contrast": self.contrast_slider.value() / 100,
            "saturation": self.saturation_slider.value() / 100,
            "look": self.look_combo.currentData(),
        }

    def reset(self) -> None:
        self.load(Scene(kind="image", source="-"))
        self.edited.emit("reset")

    def _changed(self, key: str) -> None:
        if not self._loading:
            self.edited.emit(key)


class ProjectPanel(QWidget):
    """The whole video's fades, background music, logo and subtitles."""

    edited = Signal(str)
    resetCues = Signal()

    def __init__(self, ask_file: Callable[[str], str | None], parent=None):
        super().__init__(parent)
        self.ask_file = ask_file  # "music" or "logo" -> a file, or None
        self._loading = False
        self.music = ""
        self.logo = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title = QLabel("시작·끝 페이드")
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        self.fade_in_spin, self.fade_out_spin = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.fade_in_spin, self.fade_out_spin):
            spin.setRange(0.0, 10.0)
            spin.setSingleStep(0.5)
            spin.setDecimals(1)
            spin.setSuffix(" 초")
        self.fade_in_spin.setToolTip("검은 화면에서 서서히 밝아지며 시작하는 시간 (0이면 없음)")
        self.fade_out_spin.setToolTip("끝에서 서서히 어두워지는 시간 (0이면 없음)")
        layout.addWidget(_row(QLabel("시작"), self.fade_in_spin, QLabel("끝"), self.fade_out_spin))
        layout.addWidget(_hint("영상 전체의 처음과 끝에 들어갑니다. 소리도 같이 서서히 커지고 작아집니다."))

        title = QLabel("배경 음악")
        title.setStyleSheet("font-weight: 600; margin-top: 8px;")
        layout.addWidget(title)
        self.music_label = QLabel()
        self.music_label.setMinimumWidth(10)
        music_btn = QPushButton("고르기…")
        music_btn.clicked.connect(lambda: self._choose("music"))
        self.music_clear = QPushButton("빼기")
        self.music_clear.clicked.connect(lambda: self._clear("music"))
        layout.addWidget(_row(self.music_label, music_btn, self.music_clear, stretch=False))
        self.music_box = QWidget()
        form = QFormLayout(self.music_box)
        form.setContentsMargins(0, 0, 0, 0)
        self.volume_slider, volume_row = _slider(0, 100, lambda v: f"{v}%")
        form.addRow("크기", volume_row)
        self.duck_check = QCheckBox("목소리가 나올 때 음악을 줄임")
        self.duck_check.setToolTip("말하는 동안에는 음악이 작아지고, 쉬는 동안에는 다시 커집니다")
        form.addRow("", self.duck_check)
        self.fade_spin = QDoubleSpinBox()
        self.fade_spin.setRange(0.0, 10.0)
        self.fade_spin.setSingleStep(0.5)
        self.fade_spin.setDecimals(1)
        self.fade_spin.setSuffix(" 초")
        self.fade_spin.setToolTip("처음에 서서히 커지고 끝에 서서히 작아지는 시간 (0이면 바로)")
        form.addRow("페이드", self.fade_spin)
        layout.addWidget(self.music_box)
        layout.addWidget(_hint("음악이 영상보다 짧으면 반복합니다. [합성으로 →]를 누르면 합성 탭에도 같이 넘어갑니다. "
                               "짧은 효과음은 타임라인 위의 [+ 효과음]으로 넣습니다."))  # fmt: skip

        title = QLabel("로고 · 워터마크")
        title.setStyleSheet("font-weight: 600; margin-top: 8px;")
        layout.addWidget(title)
        self.logo_label = QLabel()
        self.logo_label.setMinimumWidth(10)
        logo_btn = QPushButton("고르기…")
        logo_btn.clicked.connect(lambda: self._choose("logo"))
        self.logo_clear = QPushButton("빼기")
        self.logo_clear.clicked.connect(lambda: self._clear("logo"))
        layout.addWidget(_row(self.logo_label, logo_btn, self.logo_clear, stretch=False))
        self.logo_box = QWidget()
        form = QFormLayout(self.logo_box)
        form.setContentsMargins(0, 0, 0, 0)
        self.corner_combo = _combo(LOGO_CORNERS)
        form.addRow("위치", self.corner_combo)
        self.logo_size_spin = _percent(3, 50)
        self.logo_size_spin.setToolTip("화면 너비에 대한 크기")
        form.addRow("크기", self.logo_size_spin)
        self.logo_opacity_slider, opacity_row = _slider(10, 100, lambda v: f"{v}%")
        form.addRow("불투명도", opacity_row)
        layout.addWidget(self.logo_box)
        layout.addWidget(_hint("영상 전체에 계속 보입니다."))

        title = QLabel("자막")
        title.setStyleSheet("font-weight: 600; margin-top: 8px;")
        layout.addWidget(title)
        self.cues_label = _hint("")
        layout.addWidget(self.cues_label)
        self.reset_cues_btn = QPushButton("자막을 원래대로")
        self.reset_cues_btn.setToolTip("타임라인에서 고친 자막을 버리고 음성의 자막 파일을 다시 씁니다")
        self.reset_cues_btn.clicked.connect(self.resetCues.emit)
        layout.addWidget(_row(self.reset_cues_btn))
        layout.addWidget(_hint("타임라인의 [음성·자막] 줄에서 자막을 두 번 누르면 고치고, 양 끝을 끌면 시간이 바뀝니다. "
                               "글자 모양과 단어 강조·효과는 [③ 합성] 탭에서 정합니다."))  # fmt: skip
        layout.addStretch(1)

        self.fade_in_spin.valueChanged.connect(lambda: self._changed("fade_in"))
        self.fade_out_spin.valueChanged.connect(lambda: self._changed("fade_out"))
        self.volume_slider.valueChanged.connect(lambda: self._changed("music_volume"))
        self.duck_check.toggled.connect(lambda: self._changed("music_duck"))
        self.fade_spin.valueChanged.connect(lambda: self._changed("music_fade"))
        self.corner_combo.currentIndexChanged.connect(lambda: self._changed("logo_corner"))
        self.logo_size_spin.valueChanged.connect(lambda: self._changed("logo_size"))
        self.logo_opacity_slider.valueChanged.connect(lambda: self._changed("logo_opacity"))

    def load(self, project: Project, cue_count: int) -> None:
        self._loading = True
        self.music, self.logo = project.music, project.logo
        self.fade_in_spin.setValue(project.fade_in)
        self.fade_out_spin.setValue(project.fade_out)
        self.volume_slider.setValue(round(project.music_volume * 100))
        self.duck_check.setChecked(project.music_duck)
        self.fade_spin.setValue(project.music_fade)
        select(self.corner_combo, project.logo_corner)
        self.logo_size_spin.setValue(round(project.logo_size * 100))
        self.logo_opacity_slider.setValue(round(project.logo_opacity * 100))
        self._loading = False
        self._show_files()
        if project.cues is not None:
            self.cues_label.setText(f"타임라인에서 고친 자막을 씁니다 ({len(project.cues)}줄).")
        elif cue_count:
            self.cues_label.setText(f"음성의 자막 파일을 그대로 씁니다 ({cue_count}줄).")
        else:
            self.cues_label.setText("음성을 넣으면 자막이 타임라인에 나옵니다.")
        self.reset_cues_btn.setEnabled(project.cues is not None)

    def apply(self, project: Project) -> None:
        project.music, project.logo = self.music, self.logo
        project.fade_in = self.fade_in_spin.value()
        project.fade_out = self.fade_out_spin.value()
        project.music_volume = self.volume_slider.value() / 100
        project.music_duck = self.duck_check.isChecked()
        project.music_fade = self.fade_spin.value()
        project.logo_corner = self.corner_combo.currentData()
        project.logo_size = self.logo_size_spin.value() / 100
        project.logo_opacity = self.logo_opacity_slider.value() / 100

    def _show_files(self) -> None:
        for path, label, clear, box in ((self.music, self.music_label, self.music_clear, self.music_box),
                                        (self.logo, self.logo_label, self.logo_clear, self.logo_box)):  # fmt: skip
            label.setText(Path(path).name if path else "없음")
            label.setToolTip(path)
            clear.setEnabled(bool(path))
            box.setEnabled(bool(path))

    def _choose(self, kind: str) -> None:
        path = self.ask_file(kind)
        if path:
            setattr(self, kind, path)
            self._show_files()
            self.edited.emit(kind)

    def _clear(self, kind: str) -> None:
        setattr(self, kind, "")
        self._show_files()
        self.edited.emit(kind)

    def _changed(self, key: str) -> None:
        if not self._loading:
            self.edited.emit(key)
