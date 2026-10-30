"""How the voice reads what is written; the subtitles keep what is written.

- {14번|열네 번} in the text is said as the part after the bar and shown as the part before it.
- The reading dictionary (읽기사전.json in the data folder): words said another way wherever they are, on top of a
  built-in one of units, abbreviations and Greek letters (MPa → 메가파스칼, CAE → 씨에이이, σ → 시그마).
- Numbers in Korean sentences are read the Korean way, by what follows them: 14개 → 열네 개, 3시 → 세 시,
  2024년 → 이천이십사 년, 6월 → 유월, 182 MPa → 백팔십이 메가파스칼, 1.37 → 일 점 삼칠, 15% → 십오 퍼센트,
  3~5개 → 세 개에서 다섯 개, 2024.10.30 → 이천이십사 년 시월 삼십 일, 14:30 → 십사 시 삼십 분.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import paths

READING_MARK = re.compile(r"\{([^{}|\n]+)\|([^{}\n]*)\}")  # {written|said}
DICTIONARY_FILE = "읽기사전.json"
KEEP = 0xE000  # private-use characters stand in for what is already said, while numbers are read

# ---- numbers ---------------------------------------------------------------------------------------------

_SINO = "영일이삼사오육칠팔구"
_PLACES = ("", "십", "백", "천")
_MYRIADS = ("", "만", "억", "조", "경")
_ONES = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")
_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
_SPOKEN_ZERO = "공일이삼사오육칠팔구"  # digits read one by one, as in phone numbers

# Counters read with 한, 두, 세 … (1 to 99; above that, and with decimals, with 일, 이, 삼 …)
NATIVE_COUNTERS = (
    "개", "명", "사람", "살", "마리", "가지", "권", "대", "잔", "병", "그루", "송이", "켤레", "벌", "채", "척", "곳",
    "군데", "자루", "장", "줄", "칸", "달", "시간", "시", "배", "판", "통", "알", "바퀴", "방울", "조각", "묶음", "쌍",
    "걸음", "발짝", "마디", "번째", "차례", "문장", "단어", "장면", "끼", "그릇", "봉지", "상자",
)  # fmt: skip
# Counters read with 일, 이, 삼 …: those that begin like one above (개월, 시즌, 달러 …) must be here to win
SINO_COUNTERS = (
    "개월", "개국", "개년", "개소", "달러", "시즌", "시리즈", "대학", "대회", "년", "월", "일", "분", "초", "원", "층",
    "호", "회", "차", "번", "등", "위", "점", "쪽", "페이지", "단계", "주", "주년", "주차", "세", "도", "인", "인분",
    "학년", "학기", "교시", "편", "화", "부", "조", "항", "절", "배수", "배치",
)  # fmt: skip
_COUNTERS = sorted(NATIVE_COUNTERS + SINO_COUNTERS, key=len, reverse=True)
_MAGNITUDES = ("십", "백", "천", "만", "억", "조")  # 3만, 1.5억: part of the number

# Units after a number, read after it with 일, 이, 삼 …; single letters only right after the number (5m, not 5 m)
UNITS = {
    "%": "퍼센트", "％": "퍼센트", "‰": "퍼밀", "℃": "도", "°C": "도", "°": "도",
    "nm": "나노미터", "μm": "마이크로미터", "µm": "마이크로미터", "um": "마이크로미터", "mm": "밀리미터", "cm": "센티미터",
    "m": "미터", "km": "킬로미터", "mm²": "제곱밀리미터", "cm²": "제곱센티미터", "m²": "제곱미터", "km²": "제곱킬로미터",
    "mm³": "세제곱밀리미터", "cm³": "세제곱센티미터", "m³": "세제곱미터",
    "mg": "밀리그램", "g": "그램", "kg": "킬로그램", "t": "톤", "ton": "톤",
    "N": "뉴턴", "kN": "킬로뉴턴", "MN": "메가뉴턴", "N·m": "뉴턴미터", "Nm": "뉴턴미터", "kgf": "킬로그램힘",
    "Pa": "파스칼", "kPa": "킬로파스칼", "MPa": "메가파스칼", "GPa": "기가파스칼", "bar": "바", "psi": "피에스아이",
    "J": "줄", "kJ": "킬로줄", "MJ": "메가줄", "W": "와트", "kW": "킬로와트", "MW": "메가와트", "kWh": "킬로와트시",
    "V": "볼트", "kV": "킬로볼트", "mV": "밀리볼트", "mA": "밀리암페어", "Ω": "옴", "kΩ": "킬로옴",
    "Hz": "헤르츠", "kHz": "킬로헤르츠", "MHz": "메가헤르츠", "GHz": "기가헤르츠", "rpm": "알피엠",
    "s": "초", "sec": "초", "ms": "밀리초", "μs": "마이크로초", "min": "분", "h": "시간", "hr": "시간",
    "L": "리터", "mL": "밀리리터", "ml": "밀리리터", "dB": "데시벨", "fps": "프레임", "px": "픽셀", "dpi": "디피아이",
    "KB": "킬로바이트", "MB": "메가바이트", "GB": "기가바이트", "TB": "테라바이트",
}  # fmt: skip
SPEEDS = {"km/h": ("시속", "킬로미터"), "m/s": ("초속", "미터")}  # 60km/h → 시속 육십 킬로미터
TIMES = {"x": "배", "×": "배"}  # 2x → 두 배

# The built-in reading dictionary: said this way wherever they are (the user's dictionary wins)
BUILTIN = {
    # units written without a number
    "MPa": "메가파스칼", "GPa": "기가파스칼", "kPa": "킬로파스칼", "kN": "킬로뉴턴", "kgf": "킬로그램힘", "mm": "밀리미터",
    "cm": "센티미터", "km": "킬로미터", "μm": "마이크로미터", "kg": "킬로그램", "Hz": "헤르츠", "kHz": "킬로헤르츠",
    "MHz": "메가헤르츠", "GHz": "기가헤르츠", "rpm": "알피엠", "dB": "데시벨", "℃": "도", "°C": "도",
    # analysis and design
    "CAE": "씨에이이", "CAD": "캐드", "CAM": "캠", "FEM": "에프이엠", "FEA": "에프이에이", "CFD": "씨에프디",
    "PLM": "피엘엠", "BOM": "봄", "DFM": "디에프엠", "FMEA": "에프엠이에이", "GD&T": "지디앤티", "R&D": "알앤디",
    "von Mises": "폰 미제스", "Von Mises": "폰 미제스", "S-N": "에스엔", "SUS": "서스", "ASTM": "에이에스티엠",
    "ISO": "아이에스오", "KS": "케이에스", "JIS": "지아이에스",
    # Greek letters
    "σ": "시그마", "ε": "엡실론", "τ": "타우", "δ": "델타", "Δ": "델타", "μ": "뮤", "π": "파이", "θ": "세타",
    "α": "알파", "β": "베타", "γ": "감마", "λ": "람다", "ω": "오메가", "Ω": "옴",
    # common abbreviations
    "AI": "에이아이", "PC": "피씨", "CPU": "씨피유", "GPU": "지피유", "USB": "유에스비", "PDF": "피디에프",
    "IoT": "아이오티", "KPI": "케이피아이", "QA": "큐에이", "QC": "큐씨", "OEM": "오이엠", "IT": "아이티", "TV": "티브이",
    "SNS": "에스엔에스", "3D": "쓰리디", "2D": "투디", "4K": "포케이", "8K": "에잇케이", "5G": "파이브지",
    "vs": "대", "vs.": "대", "YouTube": "유튜브",
}  # fmt: skip


def sino(n: int) -> str:
    """일, 이, 삼 …: 182 → 백팔십이, 10000 → 만, 120000000 → 일억 이천만."""
    if n == 0:
        return "영"
    if n >= 10**20:
        return "".join(_SINO[int(d)] for d in str(n))
    groups = []
    for k in range(len(_MYRIADS)):
        n, part = divmod(n, 10000)
        if part:
            words = ""
            for place in (3, 2, 1, 0):
                d = part // 10**place % 10
                if d:
                    words += ("" if d == 1 and place else _SINO[d]) + _PLACES[place]
            groups.append(("" if k == 1 and part == 1 else words) + _MYRIADS[k])  # 만, not 일만
        if not n:
            break
    return " ".join(reversed(groups))


def native(n: int) -> str:
    """한, 두, 세 … as said before a counter: 14 → 열네, 20 → 스무, 21 → 스물한 (1 to 99)."""
    tens, ones = divmod(n, 10)
    return "스무" if (tens, ones) == (2, 0) else _TENS[tens] + _ONES[ones]


def _digits(digits: str) -> str:
    return "".join(_SINO[int(d)] for d in digits)


def _plain(whole: str, fraction: str | None) -> str:
    """A number with nothing telling how to read it: 1,234 → 천이백삼십사, 0.5 → 영 점 오, 007 → 공공칠."""
    whole = whole.replace(",", "")
    if len(whole) > 1 and whole.startswith("0") and fraction is None:
        return "".join(_SPOKEN_ZERO[int(d)] for d in whole)
    said = sino(int(whole))
    return f"{said} 점 {_digits(fraction)}" if fraction is not None else said


def _counted(whole: str, fraction: str | None, counter: str, sino_only: bool) -> str:
    """The number as said before `counter`."""
    if fraction is not None or sino_only or counter not in NATIVE_COUNTERS:
        if counter == "월" and fraction is None:
            return {"6": "유", "10": "시"}.get(whole.lstrip("0"), _plain(whole, None))
        return _plain(whole, fraction)
    n = int(whole.replace(",", ""))
    if counter == "번째" and n == 1:
        return "첫"
    if (counter == "시" and n > 12) or (counter == "대" and n % 10 == 0) or not 1 <= n <= 99:
        return _plain(whole, None)
    return native(n)


def _unit_pattern(units) -> str:
    """Units longest first, each not running on into a word (5m, not 5mHz)."""
    return "|".join(re.escape(u) + ("(?![A-Za-z0-9])" if u[-1].isascii() and u[-1].isalnum() else "")
                    for u in sorted(units, key=len, reverse=True))  # fmt: skip


_NUM = r"\d{1,3}(?:,\d{3})+(?!\d)|\d+"
_LETTERS = [u for u in UNITS if len(u) == 1 and u.isascii() and u.isalpha()]  # m, g, N …: only right after the number
_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9.])(?P<sign>[-−+](?=\d))?(?P<a>{_NUM})(?:\.(?P<af>\d+))?"
    rf"(?:\s*(?P<dash>[~∼～-])\s*(?P<b>{_NUM})(?:\.(?P<bf>\d+))?)?"
    rf"(?: ?(?:(?P<speed>{_unit_pattern(SPEEDS)})|(?P<unit>{_unit_pattern([u for u in UNITS if u not in _LETTERS])}))"
    rf"|(?P<times>{_unit_pattern(TIMES)})|(?P<letter>{_unit_pattern(_LETTERS)}))?"
)
_DATE = re.compile(r"(?<![\d.])(\d{4})([./-])(\d{1,2})\2(\d{1,2})(?![\d.])")
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_PHONE = re.compile(r"(?<![\d-])0\d{1,2}-\d{3,4}-\d{4}(?![\d-])")


def _said_dates(text: str, note) -> str:
    def date(m: re.Match) -> str:
        month, day = int(m.group(3)), int(m.group(4))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return m.group(0)
        said = f"{sino(int(m.group(1)))} 년 {_counted(m.group(3), None, '월', True)}월 {sino(day)} 일"
        return note(m.group(0), said)

    def clock(m: re.Match) -> str:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 24 or minute > 59:
            return m.group(0)
        said = f"{_counted(m.group(1), None, '시', False)} 시" + (f" {sino(minute)} 분" if minute else "")
        return note(m.group(0), said)

    def phone(m: re.Match) -> str:
        return note(m.group(0), ", ".join("".join(_SPOKEN_ZERO[int(d)] for d in part) for part in m.group(0).split("-")))

    return _PHONE.sub(phone, _CLOCK.sub(clock, _DATE.sub(date, text)))


def _said_numbers(text: str, note) -> str:
    """Numbers in a Korean sentence spelled out as they are said."""
    out, pos = [], 0
    for m in _NUMBER.finditer(text):
        start, end = m.start(), m.end()
        rest = text[end:]
        sign = {"-": "마이너스 ", "−": "마이너스 ", "+": "플러스 "}.get(m.group("sign") or "", "")
        if sign and start > 0 and not text[start - 1].isspace() and text[start - 1] not in "([":
            sign, start = "", start + 1  # 온도-5도: the dash is not a minus
        tail = m.group("speed") or m.group("unit") or m.group("times") or m.group("letter")
        if m.group("dash") == "-" and m.group("b") and not tail and not any(rest.lstrip(" ").startswith(c) for c in _COUNTERS):
            continue  # 2-3 with nothing after it: maybe not a range, so left as it is
        if not tail and rest[:1].isascii() and rest[:1].isalpha():
            continue  # 5G, 3rd: not a number this knows how to read
        numbers = [(m.group("a"), m.group("af"))] + ([(m.group("b"), m.group("bf"))] if m.group("b") else [])
        before = text[start - 1] if start > 0 else ""
        counter = ""
        if m.group("speed"):
            first, unit = SPEEDS[m.group("speed")]
            said = f"{first} " + "에서 ".join(_plain(w, f) for w, f in numbers) + f" {unit}"
        elif m.group("times"):
            said = "에서 ".join(_counted(w, f, "배", False) + " 배" for w, f in numbers)
        elif tail:
            said = "에서 ".join(_plain(w, f) for w, f in numbers) + f" {UNITS[tail]}"
        else:
            magnitude = next((u for u in _MAGNITUDES if rest.startswith(u)), "")
            if not magnitude and not rest.startswith("여"):  # 10여 개: about ten, read 십여
                counter = next((c for c in _COUNTERS if rest.lstrip(" ").startswith(c)), "")
            if counter:
                joiner = "" if counter == "월" else " "
                said = f"{joiner}{counter}에서 ".join(_counted(w, f, counter, before == "제") for w, f in numbers)
                said += joiner if not rest.startswith(" ") else ""
            else:
                said = "에서 ".join(_plain(w, f) for w, f in numbers)
                if magnitude and numbers == [("1", None)] and magnitude in "십백천만":
                    said = ""  # 1만 → 만
                elif rest[:1] and KEEP <= ord(rest[0]) < KEEP + 6400:
                    said += " "  # 182MPa with MPa in the user's dictionary
        written = text[start:end]
        shown = (written + counter, (sign + said).strip() + (" " + counter if counter and counter != "월" else counter))
        out.append(text[pos:start] + note(written, sign + said, shown))
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---- the dictionary ---------------------------------------------------------------------------------------


@dataclass
class Dictionary:
    words: dict[str, str] = field(default_factory=dict)  # the user's: written → said
    builtin: bool = True  # use BUILTIN as well
    numbers: bool = True  # read numbers the Korean way


def dictionary_path():
    return paths.DATA_DIR / DICTIONARY_FILE


def load() -> Dictionary:
    """The reading dictionary as saved (the defaults when there is none or it cannot be read)."""
    try:
        data = json.loads(dictionary_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Dictionary()
    if not isinstance(data, dict):
        return Dictionary()
    words = data.get("words") if isinstance(data.get("words"), dict) else {}
    return Dictionary({str(k): str(v) for k, v in words.items() if str(k).strip()},
                      bool(data.get("builtin", True)), bool(data.get("numbers", True)))  # fmt: skip


def save(book: Dictionary) -> None:
    path = dictionary_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"numbers": book.numbers, "builtin": book.builtin, "words": dict(sorted(book.words.items()))},
                      ensure_ascii=False, indent=1)  # fmt: skip
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)  # a reader never sees half a file


def stamp() -> float:
    """Changes when the saved dictionary does (to know the line lengths need predicting again)."""
    try:
        return dictionary_path().stat().st_mtime
    except OSError:
        return 0.0


# ---- reading ----------------------------------------------------------------------------------------------


def written(text: str) -> str:
    """The text as shown: {14번|열네 번} → 14번."""
    return READING_MARK.sub(lambda m: m.group(1), text)


def mark(text: str, said: str) -> str:
    return "{" + text + "|" + said + "}"


def _edge(word: str) -> str:
    """A dictionary word as a pattern that does not match inside a longer Latin word or number: CAD, not CADENCE;
    14개, not 114개; but MPa right after its number (182MPa)."""

    def guard(ch: str, behind: bool) -> str:
        if not ch.isascii() or not ch.isalnum():
            return ""
        chars = "A-Za-z" if ch.isalpha() else "A-Za-z0-9" if behind else "0-9"
        return f"(?<![{chars}])" if behind else f"(?![{chars}])"

    return guard(word[0], True) + re.escape(word) + guard(word[-1], False)


def _words_pattern(words: dict[str, str]):
    ordered = sorted((w for w in words if w), key=len, reverse=True)
    return re.compile("|".join(_edge(w) for w in ordered)) if ordered else None


class Reader:
    """Turns what is written into what is said, and tells what it changed.

    In this order: {written|said} marks, the user's words, numbers (Korean sentences only), the built-in words — so
    the user's 14개 wins over the number rules, and 182MPa is read as a number with its unit before MPa alone is."""

    def __init__(self, words: dict[str, str], builtin: dict[str, str] | None = None, numbers: bool = True):
        self.words, self.builtin, self.numbers = dict(words), dict(builtin or {}), numbers
        self.patterns = [(_words_pattern(self.words), self.words), (_words_pattern(self.builtin), self.builtin)]

    def say(self, text: str, lang: str = "ko") -> tuple[str, list[tuple[str, str]]]:
        """(what is said, [(written, said), …] for each place read differently)."""
        kept: list[str] = []
        changes: list[tuple[str, str]] = []

        def note(was: str, said: str, shown: tuple[str, str] | None = None) -> str:
            kept.append(said)
            changes.append(shown or (was, said.strip()))
            return chr(KEEP + len(kept) - 1)

        def words(out: str, pattern, book: dict[str, str]) -> str:
            return pattern.sub(lambda m: note(m.group(0), book[m.group(0)]), out) if pattern is not None else out

        out = READING_MARK.sub(lambda m: note(m.group(1), m.group(2)), text)
        out = words(out, *self.patterns[0])
        if self.numbers and lang == "ko":
            out = _said_numbers(_said_dates(out, note), note)
        out = words(out, *self.patterns[1])
        out = re.sub(f"[{chr(KEEP)}-{chr(KEEP + 6399)}]", lambda m: kept[ord(m.group(0)) - KEEP], out)
        return " ".join(out.split()), changes


def reader(numbers: bool | None = None, extra: dict[str, str] | None = None) -> Reader:
    """A reader with the saved dictionary (and the built-in one when it is on), `extra` words on top of the user's;
    `numbers` None reads numbers as the dictionary says."""
    book = load()
    return Reader({**book.words, **(extra or {})}, BUILTIN if book.builtin else {}, book.numbers if numbers is None else numbers)
