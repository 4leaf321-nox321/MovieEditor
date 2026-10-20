"""Reads subtitle files and writes them back as a styled ASS file for FFmpeg's libass."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from pathlib import Path

import pysubs2

SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt")
PLAIN_TEXT_FORMATS = (".srt", ".vtt")  # formats whose text carries no ASS styling
OVERRIDE_BLOCK = re.compile(r"(?<!\\)\{.*?(?<!\\)\}")  # {\i1} and the like, but not an escaped \{
REFERENCE_SIDE = 1080  # style sizes are pixels on a frame whose short side is this long
POSITIONS = {
    "bottom": pysubs2.Alignment.BOTTOM_CENTER,
    "middle": pysubs2.Alignment.MIDDLE_CENTER,
    "top": pysubs2.Alignment.TOP_CENTER,
}
SIDE_MARGIN = 60
EVENT_FOREVER_MS = 9 * 3600 * 1000  # ASS timestamps stop at 9:59:59.99


@dataclass
class SubStyle:
    font: str = "Malgun Gothic"
    size: int = 56
    bold: bool = True
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline: float = 3.0
    box: bool = False  # a semi-transparent box behind the text instead of an outline
    box_color: str = "#000000"
    box_opacity: int = 60  # percent
    position: str = "bottom"  # a key of POSITIONS
    margin: int = 60  # distance from the top or bottom edge


@dataclass
class Cue:
    start: float  # seconds
    end: float
    text: str


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # Notepad's "Unicode"
        text = raw.decode("utf-16")
    else:
        # Subtitles written on Korean Windows are often CP949 rather than UTF-8.
        for encoding in ("utf-8-sig", "cp949"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _load(path: str | Path) -> pysubs2.SSAFile:
    path = Path(path)
    try:
        text = _read_text(path)
        if not text.strip():
            raise ValueError("빈 파일입니다.")
        if path.suffix.lower() in PLAIN_TEXT_FORMATS:
            # Braces in SRT/VTT are just text, but ASS reads {...} as styling and hides it; escape them.
            text = text.replace("{", "\\{").replace("}", "\\}")
        return pysubs2.SSAFile.from_string(text)
    except (OSError, ValueError, pysubs2.Pysubs2Error) as e:
        raise ValueError(f"자막 파일을 읽을 수 없습니다: {path.name}\n{e}") from e


def _plain(text: str) -> str:
    """The words of an ASS line: styling blocks removed, escaped braces kept as braces."""
    text = OVERRIDE_BLOCK.sub("", text)
    for code, char in (("\\N", "\n"), ("\\n", "\n"), ("\\h", " "), ("\\{", "{"), ("\\}", "}")):
        text = text.replace(code, char)
    return text


def load_cues(path: str | Path) -> list[Cue]:
    return [Cue(e.start / 1000, e.end / 1000, _plain(e.text)) for e in _load(path) if not e.is_comment]


def _color(hex_color: str, opacity: int = 100) -> pysubs2.Color:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return pysubs2.Color(r, g, b, round(255 * (100 - opacity) / 100))  # ASS alpha: 0 is opaque


def _ass_style(style: SubStyle) -> pysubs2.SSAStyle:
    ass = pysubs2.SSAStyle(
        fontname=style.font,
        fontsize=style.size,
        bold=style.bold,
        primarycolor=_color(style.color),
        outlinecolor=_color(style.outline_color),
        backcolor=_color("#000000", 0),
        borderstyle=1,
        outline=style.outline,
        shadow=0,
        alignment=POSITIONS.get(style.position, pysubs2.Alignment.BOTTOM_CENTER),
        marginl=SIDE_MARGIN,
        marginr=SIDE_MARGIN,
        marginv=style.margin,
    )
    if style.box:
        # Border style 3 draws an opaque box in the outline colour; the outline width is its padding.
        ass.borderstyle = 3
        ass.outlinecolor = _color(style.box_color, style.box_opacity)
        ass.outline = max(4, round(style.size * 0.18))
    return ass


def _styled(subs: pysubs2.SSAFile, style: SubStyle, width: int, height: int) -> str:
    """Apply one style to every line and size the canvas so the style looks alike at any resolution."""
    scale = REFERENCE_SIDE / max(1, min(width, height))
    subs.info["PlayResX"] = str(round(width * scale))
    subs.info["PlayResY"] = str(round(height * scale))
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"
    subs.styles.clear()
    subs.styles["Default"] = _ass_style(style)
    for event in subs:
        event.style = "Default"
    return subs.to_string("ass")


def make_ass(path: str | Path, style: SubStyle, width: int, height: int, shift: float = 0.0) -> str:
    subs = _load(path)
    subs.remove_miscellaneous_events()
    if shift:
        subs.shift(s=shift)
    return _styled(subs, style, width, height)


def sample_ass(text: str, style: SubStyle, width: int, height: int) -> str:
    """A single line shown the whole time, for previewing the style on a still frame."""
    subs = pysubs2.SSAFile()
    subs.append(pysubs2.SSAEvent(start=0, end=EVENT_FOREVER_MS, text=text.replace("\n", "\\N")))
    return _styled(subs, style, width, height)
