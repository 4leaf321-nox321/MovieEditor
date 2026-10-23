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
# How the words being spoken stand out: "fill" colours them in as they are read (karaoke),
# "word" colours only the current one. Word timings are estimated from their lengths.
HIGHLIGHTS = ("none", "fill", "word")
EFFECTS = ("none", "fade", "pop", "slide")  # how each subtitle comes in
GUARD = "​"  # a zero-width space after a backslash of plain text, so libass shows it as it is
FADE_MS = 150
POP_MS = 220
SLIDE_MS = 220
SLIDE_DISTANCE = 40  # in the 1080-high subtitle canvas
WORD = re.compile(r"(\s+)")


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
    highlight: str = "none"  # one of HIGHLIGHTS
    highlight_color: str = "#facc15"
    effect: str = "none"  # one of EFFECTS


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
            # SRT/VTT text is just text, but ASS reads {...} as styling and \n, \h as codes. A backslash gets
            # an invisible space after it (so "C:\new" stays as written) and braces are escaped.
            text = text.replace("\\", "\\" + GUARD).replace("{", "\\{").replace("}", "\\}")
        subs = pysubs2.SSAFile.from_string(text)
        # Only comments and empty lines go: pysubs2's own clean-up also drops one-letter lines ("네").
        subs.events = [e for e in subs if not e.is_comment and _plain(e.text).strip()]
        return subs
    except (OSError, ValueError, pysubs2.Pysubs2Error) as e:
        raise ValueError(f"자막 파일을 읽을 수 없습니다: {path.name}\n{e}") from e


def _plain(text: str, guarded: bool = False) -> str:
    """The words of an ASS line: styling blocks removed, escaped braces kept as braces.

    With `guarded`, backslashes keep the invisible space that stops them being read as codes again.
    """
    text = OVERRIDE_BLOCK.sub("", text)
    for code, char in (("\\N", "\n"), ("\\n", "\n"), ("\\h", " "), ("\\{", "{"), ("\\}", "}")):
        text = text.replace(code, char)
    return text if guarded else text.replace("\\" + GUARD, "\\")


def load_cues(path: str | Path) -> list[Cue]:
    return [Cue(e.start / 1000, e.end / 1000, _plain(e.text)) for e in _load(path)]


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


def _styled(subs: pysubs2.SSAFile, style: SubStyle, width: int, height: int, animate: bool = True) -> str:
    """Apply one style to every line and size the canvas so the style looks alike at any resolution.

    With `animate` off (a still preview), highlighting is shown part-way through and effects are left out.
    """
    scale = REFERENCE_SIDE / max(1, min(width, height))
    play = (round(width * scale), round(height * scale))
    subs.info["PlayResX"] = str(play[0])
    subs.info["PlayResY"] = str(play[1])
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"
    subs.styles.clear()
    subs.styles["Default"] = _ass_style(style)
    if style.highlight == "fill":
        # Karaoke fills each word from the secondary colour to the primary one as it is read.
        subs.styles["Default"].secondarycolor = _color(style.color)
        subs.styles["Default"].primarycolor = _color(style.highlight_color)
    events = []
    for event in subs:
        event.style = "Default"
        events += _decorated(event, style, play, animate)
    subs.events = events
    return subs.to_string("ass")


def _escape(text: str) -> str:
    """Plain text as ASS text: backslashes guarded, braces escaped, line breaks as \\N."""
    return text.replace("\\", "\\" + GUARD).replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _bgr(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    return f"&H{value[4:6]}{value[2:4]}{value[0:2]}&".upper()


def _anchor(style: SubStyle, play: tuple[int, int]) -> tuple[int, int]:
    """Where libass puts a line by itself (the middle of its bottom, middle or top edge)."""
    x = play[0] // 2
    if style.position == "top":
        return x, style.margin
    if style.position == "middle":
        return x, play[1] // 2
    return x, play[1] - style.margin


def _decorated(event: pysubs2.SSAEvent, style: SubStyle, play: tuple[int, int], animate: bool) -> list[pysubs2.SSAEvent]:
    """The event with its words highlighted and its entrance effect; "word" turns it into one event per word."""
    if style.highlight == "none" and (style.effect == "none" or not animate):
        return [event]
    tokens = WORD.split(_plain(event.text))
    words = [i for i, t in enumerate(tokens) if t and not t.isspace()]
    if not words:
        return [event]
    weights = [len(tokens[i]) for i in words]
    total = sum(weights)
    span = event.end - event.start
    # Start of each word in ms from the event's start, rounded on the running total so they add up.
    starts = [round(span * sum(weights[:k]) / total) for k in range(len(words) + 1)]

    def effect(first: bool, last: bool) -> str:
        if not animate:
            return ""
        x, y = _anchor(style, play)
        fade_in, fade_out = (FADE_MS if first else 0), (FADE_MS if last else 0)
        if style.effect == "fade" and (fade_in or fade_out):
            return f"\\fad({fade_in},{fade_out})"
        if style.effect == "pop" and first:
            grow = POP_MS * 2 // 3
            return f"\\fscx60\\fscy60\\t(0,{grow},\\fscx108\\fscy108)\\t({grow},{POP_MS},\\fscx100\\fscy100)"
        if style.effect == "slide":
            # Every piece of a line gets a position, so the words do not shift between them.
            return f"\\move({x},{y + SLIDE_DISTANCE},{x},{y},0,{SLIDE_MS})\\fad({FADE_MS},0)" if first else f"\\pos({x},{y})"
        return ""

    def tagged(text: str, tags: str) -> str:
        return (f"{{{tags}}}" if tags else "") + text

    if style.highlight == "fill":
        if animate:
            parts = []
            for i, token in enumerate(tokens):
                if i in words:
                    k = words.index(i)
                    parts.append(f"{{\\kf{max(1, round((starts[k + 1] - starts[k]) / 10))}}}{_escape(token)}")
                else:
                    parts.append(_escape(token))
            text = "".join(parts)
        else:
            # A still preview shows the first half already read.
            done = words[: max(1, len(words) // 2)]
            text = "".join(_escape(t) if i in done or i not in words else f"{{\\1c{_bgr(style.color)}}}{_escape(t)}{{\\1c{_bgr(style.highlight_color)}}}"
                           for i, t in enumerate(tokens))  # fmt: skip
        event.text = tagged(text, effect(True, True))
        return [event]

    def lit(k: int) -> str:
        """The line with word k in the highlight colour."""
        return "".join(
            f"{{\\1c{_bgr(style.highlight_color)}}}{_escape(t)}{{\\1c{_bgr(style.color)}}}" if i == words[k] else _escape(t)
            for i, t in enumerate(tokens)
        )

    if style.highlight == "word":
        if not animate:
            event.text = lit(len(words) // 2)
            return [event]
        pieces = []
        for k in range(len(words)):
            piece = event.copy()
            piece.start = event.start + starts[k]
            piece.end = event.start + starts[k + 1]
            piece.text = tagged(lit(k), effect(k == 0, k == len(words) - 1))
            pieces.append(piece)
        return pieces

    event.text = tagged(event.text, effect(True, True))
    return [event]


def _clamped(subs: pysubs2.SSAFile, shift: float, duration: float | None) -> pysubs2.SSAFile:
    """Moved by `shift` seconds and cut at `duration`, so no subtitle outlasts the video."""
    if shift:
        subs.shift(s=shift)
    if duration is not None:
        end = round(duration * 1000)
        subs.events = [e for e in subs if e.start < end]
        for e in subs:
            e.end = min(e.end, end)
    return subs


def make_ass(path: str | Path, style: SubStyle, width: int, height: int, shift: float = 0.0,
             duration: float | None = None) -> str:  # fmt: skip
    """The subtitles styled for burning into the picture."""
    return _styled(_clamped(_load(path), shift, duration), style, width, height)


def make_srt(path: str | Path, shift: float = 0.0, duration: float | None = None) -> str:
    """The subtitles as plain SRT, for a subtitle track players switch on: no styling, the words as written.

    Backslashes keep their invisible space: FFmpeg-based players read "C:\\new" in SRT as a line break.
    """
    subs = _clamped(_load(path), shift, duration)
    cues = [Cue(e.start / 1000, e.end / 1000, _plain(e.text, guarded=True)) for e in subs]
    return _srt(cues)


def sample_ass(text: str, style: SubStyle, width: int, height: int) -> str:
    """A single line shown the whole time, for previewing the style on a still frame."""
    subs = pysubs2.SSAFile()
    subs.append(pysubs2.SSAEvent(start=0, end=EVENT_FOREVER_MS, text=_escape(text)))
    return _styled(subs, style, width, height, animate=False)


def _timestamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{_timestamp(c.start)} --> {_timestamp(c.end)}\n{c.text.strip()}\n" for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: str | Path) -> None:
    Path(path).write_text(_srt(cues), encoding="utf-8")
