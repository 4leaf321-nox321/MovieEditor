"""Dialog for custom voices: blend two voices, import a voice file, delete one."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from .engine import Engine, clean_voice_name, custom_voice_id, list_voices

PREVIEW_VOICE = "blend:preview"
TITLE = "목소리 만들기"


class VoiceDialog(QDialog):
    previewRequested = Signal(str)  # voice id to read a sample with
    voicesChanged = Signal(str)  # after a save, import or delete; the voice to select ("" keeps the current one)

    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(self._build_blend_box())
        layout.addWidget(self._build_list_box())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _build_blend_box(self) -> QGroupBox:
        box = QGroupBox("두 목소리 섞기")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        self.a_combo = QComboBox()
        self.b_combo = QComboBox()
        grid.addWidget(QLabel("목소리 A"), 0, 0)
        grid.addWidget(self.a_combo, 0, 1)
        grid.addWidget(QLabel("목소리 B"), 1, 0)
        grid.addWidget(self.b_combo, 1, 1)

        self.ratio = QSlider(Qt.Orientation.Horizontal)
        self.ratio.setRange(0, 100)
        self.ratio.setSingleStep(5)
        self.ratio.setPageStep(10)
        self.ratio.setValue(50)
        self.ratio_label = QLabel()
        self.ratio_label.setObjectName("muted")
        self.ratio.valueChanged.connect(self._update_ratio_label)
        self._update_ratio_label()
        grid.addWidget(QLabel("섞는 비율"), 2, 0)
        grid.addWidget(self.ratio, 2, 1)
        grid.addWidget(self.ratio_label, 3, 1)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("예: 차분한 여성")
        self.name_edit.setMaxLength(40)
        grid.addWidget(QLabel("이름"), 4, 0)
        grid.addWidget(self.name_edit, 4, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        preview_btn = QPushButton("들어보기")
        preview_btn.setToolTip("저장하기 전에 섞은 목소리로 짧은 문장을 읽어 봅니다")
        preview_btn.clicked.connect(self._preview)
        save_btn = QPushButton("저장")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        row.addWidget(preview_btn)
        row.addWidget(save_btn)
        grid.addLayout(row, 5, 0, 1, 2)
        return box

    def _build_list_box(self) -> QGroupBox:
        box = QGroupBox("내 목소리")
        row = QHBoxLayout(box)
        self.voice_list = QListWidget()
        self.voice_list.setMinimumHeight(120)
        row.addWidget(self.voice_list, 1)
        col = QVBoxLayout()
        import_btn = QPushButton("파일 가져오기…")
        import_btn.setToolTip("Supertonic 목소리 파일(.json)을 가져옵니다")
        import_btn.clicked.connect(self._import)
        self.delete_btn = QPushButton("삭제")
        self.delete_btn.clicked.connect(self._delete)
        col.addWidget(import_btn)
        col.addWidget(self.delete_btn)
        col.addStretch(1)
        row.addLayout(col)
        self.voice_list.currentItemChanged.connect(lambda item, _: self.delete_btn.setEnabled(item is not None))
        return box

    def _update_ratio_label(self) -> None:
        b = self.ratio.value()
        self.ratio_label.setText(f"A {100 - b}%  ·  B {b}%")

    def _refresh(self, select: str = "") -> None:
        voices = [v for v in list_voices() if not v.is_melo]  # only Supertonic voices can be blended
        for combo, default in ((self.a_combo, "F1"), (self.b_combo, "F2")):
            current = combo.currentData() or default
            combo.clear()
            for v in voices:
                combo.addItem(v.label, v.id)
            combo.setCurrentIndex(max(0, combo.findData(current)))
        self.voice_list.clear()
        for v in voices:
            if v.is_custom:
                item = QListWidgetItem(v.label)
                item.setData(Qt.ItemDataRole.UserRole, v.id)
                self.voice_list.addItem(item)
                if v.id == select:
                    self.voice_list.setCurrentItem(item)
        self.delete_btn.setEnabled(self.voice_list.currentItem() is not None)

    def _mix(self) -> dict[str, float]:
        a, b = self.a_combo.currentData(), self.b_combo.currentData()
        share_b = self.ratio.value() / 100
        return {a: 1.0} if a == b else {a: 1 - share_b, b: share_b}

    def _preview(self) -> None:
        self.engine.use_style(PREVIEW_VOICE, self.engine.blend(self._mix()))
        self.previewRequested.emit(PREVIEW_VOICE)

    def _confirm_overwrite(self, name: str) -> bool:
        if not any(v.id == custom_voice_id(name) for v in list_voices()):
            return True
        answer = QMessageBox.question(self, TITLE, f"'{name}' 목소리가 이미 있습니다. 새로 바꿀까요?")
        return answer == QMessageBox.StandardButton.Yes

    def _save(self) -> None:
        name = clean_voice_name(self.name_edit.text())
        if not name:
            QMessageBox.information(self, TITLE, "새 목소리의 이름을 입력하세요.")
            self.name_edit.setFocus()
            return
        if not self._confirm_overwrite(name):
            return
        mix = self._mix()
        voice = self.engine.save_voice(name, self.engine.blend(mix), {"mix": mix})
        self.name_edit.clear()
        self._refresh(select=voice.id)
        self.voicesChanged.emit(voice.id)

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "목소리 파일 가져오기", "", "Supertonic 목소리 (*.json)")
        if not path:
            return
        name = clean_voice_name(Path(path).stem) or "가져온 목소리"
        if not self._confirm_overwrite(name):
            return
        try:
            voice = self.engine.import_voice(Path(path), name)
        except ValueError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self._refresh(select=voice.id)
        self.voicesChanged.emit(voice.id)

    def _delete(self) -> None:
        item = self.voice_list.currentItem()
        if item is None:
            return
        answer = QMessageBox.question(self, TITLE, f"'{item.text().removeprefix('★ ')}' 목소리를 지울까요?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.engine.delete_voice(item.data(Qt.ItemDataRole.UserRole))
        self._refresh()
        self.voicesChanged.emit("")
