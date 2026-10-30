"""Text loading and segmentation.

Text is synthesized one segment at a time. A line break or sentence-ending
punctuation ends a segment, and a blank line ends a paragraph (a longer pause
follows). Each segment also becomes one subtitle cue. A pause mark such as
[쉼 2초] becomes a segment of its own that asks for exactly that much silence.
A reading mark such as {14번|열네 번} stays whole: the subtitle shows 14번 and the voice says 열네 번.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .reading import READING_MARK, written

AUTO_LANG = "auto"

# Supertonic recommends shorter chunks for Korean; Japanese is similarly dense.
MAX_CHARS_CJK = 120
MAX_CHARS_OTHER = 200


# "[쉼]", "[쉼 2]", "[쉼 1.5초]", "[pause 2s]": silence of exactly that many seconds (1 if no number).
PAUSE_MARK = re.compile(r"\[\s*(?:쉼|pause)\s*(\d+(?:\.\d+)?)?\s*(?:초|s|sec)?\s*\]", re.IGNORECASE)
DEFAULT_PAUSE = 1.0
MAX_PAUSE = 60.0


@dataclass
class Segment:
    text: str  # as shown in the subtitles; empty for a pause mark
    lang: str
    paragraph_end: bool = False
    line: int = 0  # the source line it came from, counting from 0
    pause: float | None = None  # seconds of silence asked for by a pause mark
    source: str = ""  # as written, with its reading marks; empty when it has none

    @property
    def to_read(self) -> str:
        """What the reader turns into speech: the text with its reading marks."""
        return self.source or self.text

    @property
    def is_pause(self) -> bool:
        return self.pause is not None


def read_text_file(path: str | Path) -> str:
    """Read a text file, trying UTF-16/UTF-8 first and then CP949 (common for Korean Windows files)."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp949")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_KANA = re.compile(r"[぀-ヿ]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Guess the language of one sentence: ko, ja, en, or na (unknown)."""
    if _HANGUL.search(text):
        return "ko"
    if _KANA.search(text):
        return "ja"
    letters = sum(ch.isalpha() for ch in text)
    if letters and len(_LATIN.findall(text)) * 2 >= letters:
        return "en"
    return "na"


# Sentence-ending punctuation (plus closing quotes/brackets) followed by whitespace.
# Requiring whitespace keeps decimals like "3.5" intact.
_SENTENCE_END = re.compile(r"[.!?…。！？]+[\"'”’)\]」』]*(?=\s)")
_ABBREVIATION = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|No)\.$")
# Where an overlong sentence may be broken. ASCII marks need a following space
# so times ("10:30") and numbers ("3,500") stay whole.
_CLAUSE_BREAK = re.compile(r"[,;:](?=\s)|[，、；：]")


def split_sentences(line: str) -> list[str]:
    sentences, start = [], 0
    for m in _SENTENCE_END.finditer(line):
        if _ABBREVIATION.search(line, 0, m.end()):
            continue
        sentences.append(line[start : m.end()])
        start = m.end()
    sentences.append(line[start:])
    return [" ".join(s.split()) for s in sentences if s.strip()]


def split_long(sentence: str, max_chars: int) -> list[str]:
    """Break a sentence longer than max_chars at clause marks, else spaces, else anywhere."""
    pieces = []
    rest = sentence
    while len(rest) > max_chars:
        window = rest[:max_chars]
        min_pos = max_chars // 3
        cut = 0
        for m in _CLAUSE_BREAK.finditer(window):
            if m.end() >= min_pos:
                cut = m.end()
        if not cut:
            space = window.rfind(" ", min_pos)
            cut = space if space > 0 else max_chars
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def _sentences(text: str, lang: str, line: int) -> list[Segment]:
    # Reading marks stand in as one private character each while splitting, so no mark is cut in two.
    marks: list[str] = []

    def hide(m: re.Match) -> str:
        marks.append(m.group(0))
        return chr(0xF000 + len(marks) - 1)

    def shown(piece: str) -> str:
        return re.sub("[\uf000-\uf8ff]", lambda m: marks[ord(m.group(0)) - 0xF000], piece)

    segments = []
    for sentence in split_sentences(READING_MARK.sub(hide, text.strip())):
        seg_lang = detect_lang(written(shown(sentence))) if lang == AUTO_LANG else lang
        limit = MAX_CHARS_CJK if seg_lang in ("ko", "ja") else MAX_CHARS_OTHER
        for piece in split_long(sentence, limit):
            source = shown(piece)
            segments.append(Segment(written(source), seg_lang, line=line, source=source if marks else ""))
    return segments


def segment_text(text: str, lang: str = AUTO_LANG) -> list[Segment]:
    segments: list[Segment] = []
    paragraph_start = 0

    def end_paragraph() -> None:
        for seg in reversed(segments[paragraph_start:]):
            if not seg.is_pause:
                seg.paragraph_end = True
                break

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines):
        if not line.strip():
            end_paragraph()
            paragraph_start = len(segments)
            continue
        pos = 0
        for m in PAUSE_MARK.finditer(line):
            segments += _sentences(line[pos : m.start()], lang, number)
            seconds = min(MAX_PAUSE, float(m.group(1)) if m.group(1) else DEFAULT_PAUSE)
            segments.append(Segment("", "", line=number, pause=seconds))
            pos = m.end()
        segments += _sentences(line[pos:], lang, number)
    end_paragraph()
    return segments


def count_sentences(segments: list[Segment]) -> int:
    return sum(not seg.is_pause for seg in segments)


def take_preview(segments: list[Segment], max_chars: int) -> list[Segment]:
    """The leading segments that fit in max_chars (always at least one)."""
    taken, total = [], 0
    for seg in segments:
        if taken and total + len(seg.text) > max_chars:
            break
        taken.append(seg)
        total += len(seg.text)
    return taken
