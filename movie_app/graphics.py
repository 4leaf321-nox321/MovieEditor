"""Graphic scenes: backgrounds, shapes, lines, arrows, words, counters, pictures, line-art props, colour tiles,
curves and bar charts, drawn frame by frame from a list of layers in a style.

It does what a hand-written frame renderer does, but the drawing is described (JSON) instead of coded,
so every video made in a style looks alike and Claude builds scenes from the same parts each time.

A style names the colours, fonts, backgrounds, colour maps, defaults for lines and shapes, props and the
timing. Built-in styles are in graphic_styles.py; a user's styles are JSON files in the 스타일 folder.
A style may start from another ("base") and change only part of it; every style starts from "기본".

Units: positions and sizes are shares of the frame (x and w of its width, y and h of its height, r of its
shorter side); line widths, font sizes, corner radii, blur, offsets and distances are pixels on a frame
whose shorter side is 1080, so a video looks the same at any size. Times are seconds into the scene.
Colours are palette names ("accent") or "#rrggbb" / "#rrggbbaa", with "/0.5" for half see-through.
"""

from __future__ import annotations

import ast
import copy
import glob
import hashlib
import importlib.util
import json
import math
import operator
import os
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer

from . import paths
from .graphic_plugins import BUILTIN as BUILTIN_PLUGINS
from .graphic_styles import BUILTIN, DEFAULT

REFERENCE_SIDE = 1080
SHADOW_SCALE = 0.25  # shadows are drawn this much smaller, then blurred and laid under at full size
TEXT_WRAP = 0.86  # words wrap at this share of the frame width unless the layer says otherwise
CURVE_SAMPLES = 240
STYLE_PARTS = {"base", "description", "colors", "fonts", "backgrounds", "shape", "line", "arrow", "text_box",
               "shadow", "glow", "colormaps", "motion", "props"}  # fmt: skip
CHAR_EFFECTS = ("chars_fade", "chars_rise", "chars_drop", "chars_pop", "chars_spin")  # words coming in a letter at a time
ENTER_EFFECTS = ("none", "fade", "draw", "grow", "pop", "slide_up", "slide_down", "slide_left", "slide_right",
                 "wipe", "type", "count", "sweep", *CHAR_EFFECTS)  # fmt: skip
EXIT_EFFECTS = ("none", "fade", "shrink", "slide_up", "slide_down", "slide_left", "slide_right", "wipe")
# Effects only some layers can do; the others fade instead.
SPECIAL = {
    "draw": {"rect", "ellipse", "circle", "line", "arrow", "polygon", "path", "prop", "curve", "plugin"},
    "type": {"text", "counter"},
    "count": {"counter"},
    "sweep": {"tiles", "bars"},
    **{effect: {"text", "counter"} for effect in CHAR_EFFECTS},
}
EASE_FAMILIES = ("quad", "cubic", "quart", "quint", "sine", "expo", "circ", "back", "elastic", "bounce")
EASES = ("linear", "smooth", *(f"{way}_{family}" for family in EASE_FAMILIES for way in ("in", "out", "in_out")))
CURVE_KINDS = ("normal", "lognormal", "exponential", "uniform", "sine", "linear", "logistic", "power")
FIELD_KINDS = ("noise", "hotspots", "radial", "linear", "bending", "wave")
COMMON_KEYS = {"type", "name", "note", "start", "end", "enter", "exit", "keys", "opacity", "rotation", "scale",
               "shadow", "glow", "blend", "ease", "shift"}  # fmt: skip
GROUP_KEYS = {"layers", "count", "cols", "dx", "dy", "origin", "clip"}
MEDIA_KEYS = {"x", "y", "w", "fit"}  # fit "cover" | "contain" fills the frame instead of x, y, w
BACKGROUND_KEYS = {"preset", "color", "colors", "gradient", "angle", "center", "radius", "vignette", "grid", "grain"}
TEXT_KEYS = {"text", "role", "font", "size", "bold", "color", "x", "y", "align", "wrap", "box", "outline", "line_height",
             "spans", "markup", "wave"}  # fmt: skip
LAYER_KEYS = {
    "background": BACKGROUND_KEYS,
    "rect": {"x", "y", "w", "h", "radius", "fill", "stroke", "width", "dash"},
    "ellipse": {"x", "y", "w", "h", "fill", "stroke", "width", "dash"},
    "circle": {"x", "y", "r", "fill", "stroke", "width", "dash"},
    "line": {"points", "smooth", "color", "width", "dash", "dx", "dy", "head", "head_size"},
    "arrow": {"from", "to", "points", "bend", "color", "width", "dash", "head_size", "double", "dx", "dy"},
    "polygon": {"points", "fill", "stroke", "width", "dx", "dy"},
    "path": {"d", "x", "y", "w", "h", "viewbox", "fill", "stroke", "width", "mode"},
    "text": TEXT_KEYS,
    "counter": (TEXT_KEYS - {"text"}) | {"from", "to", "decimals", "prefix", "suffix", "separator"},
    "image": {"file", "x", "y", "w"},
    "prop": {"name", "x", "y", "w", "color", "width", "fill"},
    "tiles": {"x", "y", "w", "h", "cols", "rows", "values", "field", "colormap", "gap", "radius", "legend", "min",
              "max", "unit", "order"},  # fmt: skip
    "curve": {"x", "y", "w", "h", "function", "points", "x_range", "y_range", "color", "width", "fill", "axes",
              "ticks", "x_label", "y_label", "shade", "shade_color", "marker", "samples"},  # fmt: skip
    "bars": {"x", "y", "w", "h", "values", "labels", "colors", "colormap", "max", "gap", "value_labels", "decimals",
             "suffix", "axes"},  # fmt: skip
    "group": GROUP_KEYS,
    "repeat": GROUP_KEYS,
    "frames": MEDIA_KEYS | {"folder", "pattern", "fps", "loop", "offset"},
    "video": MEDIA_KEYS | {"file", "offset", "loop", "speed", "chroma", "chroma_strength"},
    "plugin": {"plugin"},  # and the values the plugin reads
}
GROUPS = ("group", "repeat")
BLENDS = {
    "normal": QPainter.CompositionMode.CompositionMode_SourceOver,
    "multiply": QPainter.CompositionMode.CompositionMode_Multiply,
    "screen": QPainter.CompositionMode.CompositionMode_Screen,
    "overlay": QPainter.CompositionMode.CompositionMode_Overlay,
    "darken": QPainter.CompositionMode.CompositionMode_Darken,
    "lighten": QPainter.CompositionMode.CompositionMode_Lighten,
    "add": QPainter.CompositionMode.CompositionMode_Plus,
    "difference": QPainter.CompositionMode.CompositionMode_Difference,
    "soft_light": QPainter.CompositionMode.CompositionMode_SoftLight,
    "hard_light": QPainter.CompositionMode.CompositionMode_HardLight,
    "color_dodge": QPainter.CompositionMode.CompositionMode_ColorDodge,
    "color_burn": QPainter.CompositionMode.CompositionMode_ColorBurn,
}
CLIP_SHAPES = ("rect", "ellipse", "circle", "polygon", "path", "text")
SEQUENCE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
MAX_COPIES = 2000  # copies one repeat may draw
COLOUR_KEYS = {"color", "fill", "stroke", "shade_color", "chroma"}  # where a layer names a colour (fill/stroke may be false)


def styles_dir() -> Path:
    return paths.DATA_DIR / "스타일"


# ---- styles -------------------------------------------------------------------------------------------


def _user_styles() -> dict[str, Path]:
    folder = styles_dir()
    return {p.stem: p for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}


def _stamp() -> tuple:
    """What the user's style files are now, so a changed file is read again."""
    return tuple((name, p.stat().st_mtime_ns) for name, p in _user_styles().items())


def style_names() -> list[str]:
    return list(dict.fromkeys([*BUILTIN, *_user_styles()]))


def is_builtin(name: str) -> bool:
    return name in BUILTIN and name not in _user_styles()


def own_style(name: str) -> dict:
    """A style as it is written (only what it changes from its base)."""
    user = _user_styles()
    if name in user:
        try:
            data = json.loads(user[name].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise ValueError(f"스타일 파일을 읽을 수 없습니다: {user[name]}\n{e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"스타일 파일이 올바르지 않습니다: {user[name]}")
        return data
    if name in BUILTIN:
        return copy.deepcopy(BUILTIN[name])
    raise ValueError(f"스타일 '{name}'이(가) 없습니다. 있는 스타일: {', '.join(style_names())}")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "props":
            out[key] = _merge(out[key], value)
        elif key == "props" and isinstance(value, dict):
            out.setdefault("props", {}).update(copy.deepcopy(value))
        else:
            out[key] = copy.deepcopy(value)
    return out


@lru_cache(maxsize=32)
def _resolved(name: str, stamp: tuple) -> str:
    chain, current = [], name
    while True:
        if current in chain:
            raise ValueError(f"스타일의 base가 돌고 돕니다: {' → '.join([*chain, current])}")
        chain.append(current)
        data = own_style(current)
        base = data.get("base") or (DEFAULT if current != DEFAULT else None)
        if base is None:
            break
        current = base
    merged: dict = {}
    for part in reversed(chain):
        merged = _merge(merged, own_style(part))
    merged["name"] = name
    merged.pop("base", None)
    return json.dumps(merged, ensure_ascii=False)


def style(name: str = "") -> dict:
    """The whole style (its bases laid under), ready to draw with."""
    return json.loads(_resolved(name or DEFAULT, _stamp()))


def style_hash(name: str = "") -> str:
    return hashlib.sha1(_resolved(name or DEFAULT, _stamp()).encode("utf-8")).hexdigest()[:12]


def save_style(name: str, data: dict) -> Path:
    name = name.strip()
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise ValueError(f"스타일 이름으로 쓸 수 없습니다: {name!r}")
    errors = check_style(data, name)
    if errors:
        raise ValueError("스타일에 문제가 있습니다.\n" + "\n".join(errors[:20]))
    folder = styles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)
    _resolved.cache_clear()
    return path


def delete_style(name: str) -> None:
    path = _user_styles().get(name)
    if path is None:
        raise ValueError(f"지울 수 있는 스타일이 아닙니다(기본으로 들어 있는 스타일이거나 없음): {name}")
    path.unlink()
    _resolved.cache_clear()


# ---- colours, easing ----------------------------------------------------------------------------------


def _hex(text: str) -> QColor | None:
    if re.fullmatch(r"#[0-9a-fA-F]{3}", text):
        return QColor(*(int(c * 2, 16) for c in text[1:]))
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return QColor(int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
    if re.fullmatch(r"#[0-9a-fA-F]{8}", text):  # CSS order: the alpha comes last
        return QColor(int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), int(text[7:9], 16))
    return None


def colour(value, st: dict) -> QColor:
    """A colour from a palette name or code, with an optional "/opacity"."""
    text = str(value).strip()
    alpha = 1.0
    if "/" in text:
        text, _, share = text.rpartition("/")
        try:
            alpha = float(share)
        except ValueError:
            raise ValueError(f"색의 투명도를 읽을 수 없습니다: {value!r}") from None
    seen = set()
    while text in st.get("colors", {}) and text not in seen:  # a palette name may name another one
        seen.add(text)
        inner = str(st["colors"][text])
        if "/" in inner:
            inner, _, share = inner.rpartition("/")
            alpha *= float(share)
        text = inner
    found = _hex(text) or (QColor(text) if QColor.isValidColorName(text) else None)
    if found is None:
        raise ValueError(f"알 수 없는 색: {value!r} (팔레트 이름이나 #rrggbb)")
    found.setAlphaF(max(0.0, min(1.0, found.alphaF() * alpha)))
    return found


def ease(name: str, p: float) -> float:
    p = min(1.0, max(0.0, p))
    if name in ("linear", "", None):
        return p
    if name == "smooth":
        return p * p * (3 - 2 * p)
    way, _, family = name.partition("_")
    if way == "in" and family.startswith("out_"):  # "in_out_cubic"
        way, family = "in_out", family[4:]
    curve = _EASE_IN.get(family)
    if curve is None:
        return p
    if way == "in":
        return curve(p)
    if way == "out":
        return 1 - curve(1 - p)
    return curve(2 * p) / 2 if p < 0.5 else 1 - curve(2 - 2 * p) / 2


def _bounce_in(p: float) -> float:
    q = 1 - p
    if q < 1 / 2.75:
        v = 7.5625 * q * q
    elif q < 2 / 2.75:
        q -= 1.5 / 2.75
        v = 7.5625 * q * q + 0.75
    elif q < 2.5 / 2.75:
        q -= 2.25 / 2.75
        v = 7.5625 * q * q + 0.9375
    else:
        q -= 2.625 / 2.75
        v = 7.5625 * q * q + 0.984375
    return 1 - v


_EASE_IN = {
    "quad": lambda p: p**2,
    "cubic": lambda p: p**3,
    "quart": lambda p: p**4,
    "quint": lambda p: p**5,
    "sine": lambda p: 1 - math.cos(p * math.pi / 2),
    "expo": lambda p: 0.0 if p == 0 else 2 ** (10 * p - 10),
    "circ": lambda p: 1 - math.sqrt(max(0.0, 1 - p * p)),
    "back": lambda p: 2.70158 * p**3 - 1.70158 * p**2,
    "elastic": lambda p: 0.0 if p in (0, 1) else -(2 ** (10 * p - 10)) * math.sin((p * 10 - 10.75) * 2 * math.pi / 3),
    "bounce": _bounce_in,
}


# ---- formulas -----------------------------------------------------------------------------------------
# A number in a layer may be a formula of time and of its place among copies, e.g. "0.5 + 0.1*sin(2*pi*t)"
# or "0.1 + i*0.08". Formulas are read into a tree of arithmetic, comparisons and the functions below; names,
# attributes, imports and anything else are refused, so a formula can only ever compute a number.

VARIABLES = ("t", "lt", "d", "p", "i", "n", "col", "row")
TIME_VARIABLES = {"t", "lt", "p"}
CONSTANTS = {"pi": math.pi, "tau": 2 * math.pi, "e": math.e}
# Values that are names or words (colours, effects, files), never formulas.
WORD_KEYS = {"type", "name", "note", "text", "role", "font", "preset", "colormap", "color", "fill", "stroke",
             "shade_color", "file", "folder", "pattern", "d", "align", "enter", "exit", "head", "gradient", "mode",
             "order", "unit", "prefix", "suffix", "separator", "labels", "x_label", "y_label", "label", "plugin",
             "colors", "layers", "keys", "ease", "blend", "spans", "markup", "kind", "chroma", "fit", "track"}  # fmt: skip


def _rand(k: float, seed: float = 0.0) -> float:
    """A repeatable random number from 0 to 1 for the integer k."""
    x = math.sin(float(k) * 12.9898 + float(seed) * 78.233) * 43758.5453
    return x - math.floor(x)


def _noise(x: float, seed: float = 0.0) -> float:
    """Smooth random wandering from 0 to 1 along x."""
    i = math.floor(x)
    f = x - i
    a, b = _rand(i, seed), _rand(i + 1, seed)
    return a + (b - a) * f * f * (3 - 2 * f)


def _pow(a: float, b: float) -> float:
    a, b = float(a), float(b)
    if abs(b) > 64 or (abs(a) > 1e6 and abs(b) > 4):
        raise ValueError("거듭제곱이 너무 큽니다")
    return a**b


FUNCTIONS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "sqrt": math.sqrt, "exp": math.exp, "log": math.log, "log10": math.log10, "abs": abs,
    "min": min, "max": max, "floor": math.floor, "ceil": math.ceil, "round": round, "hypot": math.hypot,
    "radians": math.radians, "degrees": math.degrees,
    "sign": lambda x: float((x > 0) - (x < 0)),
    "clamp": lambda x, low=0.0, high=1.0: min(max(x, low), high),
    "lerp": lambda a, b, p: a + (b - a) * p,
    "step": lambda edge, x: 1.0 if x >= edge else 0.0,
    "smoothstep": lambda a, b, x: (lambda q: q * q * (3 - 2 * q))(min(1.0, max(0.0, (x - a) / (b - a) if b != a else 1.0))),
    "mod": lambda a, b: a % b,
    "pingpong": lambda x: 1 - abs((x % 2) - 1),
    "noise": _noise,
    "rand": _rand,
}  # fmt: skip
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: _pow}  # fmt: skip
_COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq,
            ast.NotEq: operator.ne}  # fmt: skip


@dataclass(frozen=True)
class Formula:
    source: str
    fn: object
    names: frozenset


@lru_cache(maxsize=4096)
def formula(source: str) -> Formula:
    """A formula read and checked once, to be worked out many times."""
    if len(source) > 2000:
        raise ValueError("식이 너무 깁니다.")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError:
        raise ValueError(f"식을 읽을 수 없습니다: {source!r}") from None
    names: set[str] = set()
    return Formula(source, _build(tree.body, source, names), frozenset(names))


def _build(node, source: str, names: set):
    def refuse(what: str):
        raise ValueError(f"식 {source!r}에 쓸 수 없는 것: {what}")

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float)):
            value = float(node.value)
            return lambda env: value
        refuse(repr(node.value))
    if isinstance(node, ast.Name):
        if node.id in CONSTANTS:
            value = CONSTANTS[node.id]
            return lambda env: value
        if node.id in VARIABLES:
            names.add(node.id)
            key = node.id
            return lambda env: env.get(key, 0.0)
        refuse(f"이름 '{node.id}' (변수: {', '.join(VARIABLES)}, 상수: {', '.join(CONSTANTS)})")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        fn, left, right = _BINARY[type(node.op)], _build(node.left, source, names), _build(node.right, source, names)
        return lambda env: fn(left(env), right(env))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        inner = _build(node.operand, source, names)
        if isinstance(node.op, ast.USub):
            return lambda env: -inner(env)
        if isinstance(node.op, ast.Not):
            return lambda env: float(not inner(env))
        return inner
    if isinstance(node, ast.BoolOp):
        parts = [_build(v, source, names) for v in node.values]
        if isinstance(node.op, ast.And):
            return lambda env: float(all(part(env) for part in parts))
        return lambda env: float(any(part(env) for part in parts))
    if isinstance(node, ast.Compare) and all(type(op) in _COMPARE for op in node.ops):
        left = _build(node.left, source, names)
        ops = [_COMPARE[type(op)] for op in node.ops]
        rights = [_build(c, source, names) for c in node.comparators]

        def compare(env):
            a = left(env)
            for op, right in zip(ops, rights):
                b = right(env)
                if not op(a, b):
                    return 0.0
                a = b
            return 1.0

        return compare
    if isinstance(node, ast.IfExp):
        test, body, other = (_build(n, source, names) for n in (node.test, node.body, node.orelse))
        return lambda env: body(env) if test(env) else other(env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        name = node.func.id
        if name == "ease":
            first = node.args[0] if node.args else None
            if len(node.args) != 2 or not (isinstance(first, ast.Constant) and first.value in EASES):
                refuse('ease는 ease("out_cubic", 값) 모양으로 씁니다')
            curve, value = first.value, _build(node.args[1], source, names)
            return lambda env: ease(curve, value(env))
        if name in FUNCTIONS:
            fn, args = FUNCTIONS[name], [_build(a, source, names) for a in node.args]
            return lambda env: fn(*(a(env) for a in args))
        refuse(f"함수 '{name}' (쓸 수 있는 함수: {', '.join([*FUNCTIONS, 'ease'])})")
    refuse(type(node).__name__)


def evaluate(source: str, env: dict) -> float:
    f = formula(source)
    try:
        value = f.fn(env)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
        raise ValueError(f"식 {source!r}을(를) 계산할 수 없습니다: {e}") from None
    if isinstance(value, complex) or value != value or value in (math.inf, -math.inf):
        raise ValueError(f"식 {source!r}의 값이 숫자가 아닙니다.")
    return float(value)


def _num(value, env: dict) -> float:
    return evaluate(value, env) if isinstance(value, str) else float(value)


def _resolve(value, env: dict, key: str | None = None):
    """A layer's values with their formulas worked out (names, words and child layers left alone)."""
    if key in WORD_KEYS:
        return value
    if isinstance(value, str):
        return evaluate(value, env)
    if isinstance(value, list):
        return [_resolve(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, env, k) for k, v in value.items()}
    return value


def _uses_time(value, key: str | None = None) -> bool:
    if key in WORD_KEYS:
        return False
    if isinstance(value, str):
        try:
            return bool(formula(value).names & TIME_VARIABLES)
        except ValueError:
            return False
    if isinstance(value, list):
        return any(_uses_time(v) for v in value)
    if isinstance(value, dict):
        return any(_uses_time(v, k) for k, v in value.items())
    return False


# ---- timing -------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    st: dict
    width: int
    height: int
    duration: float

    @property
    def u(self) -> float:
        """Pixels per pixel of a 1080 frame."""
        return min(self.width, self.height) / REFERENCE_SIDE

    def x(self, share: float) -> float:
        return float(share) * self.width

    def y(self, share: float) -> float:
        return float(share) * self.height

    def point(self, p) -> QPointF:
        return QPointF(self.x(p[0]), self.y(p[1]))

    def colour(self, value) -> QColor:
        return colour(value, self.st)


@dataclass
class State:
    t: float  # seconds since the layer came on
    enter: str
    p_in: float  # eased; 1 when it has no entrance
    raw_in: float
    enter_seconds: float
    leave: str
    p_out: float  # eased; 0 when it has no exit or has not started leaving
    enter_ease: str = "out_cubic"
    stagger: float = 0.0  # seconds between letters coming in one at a time
    env: dict = field(default_factory=dict)  # the formula variables of this layer now


IDLE = State(0.0, "none", 1.0, 1.0, 0.0, "none", 0.0)


def _effect(spec, st: dict, leaving: bool, env: dict | None = None) -> tuple[str, float, str, float]:
    """(effect, seconds, ease, letter stagger) from a layer's "enter" or "exit"."""
    motion = st.get("motion", {})
    seconds = float(motion.get("exit_duration" if leaving else "duration", 0.5))
    curve = motion.get("ease", "out_cubic")
    stagger = float(motion.get("char_stagger", 0.04))
    if spec in (None, False, "none", ""):
        return "none", 0.0, curve, stagger
    if spec is True:
        return ("fade" if leaving else motion.get("enter", "fade")), seconds, curve, stagger
    if isinstance(spec, str):
        return spec, seconds, curve, stagger
    env = env or {}
    return (str(spec.get("effect", "fade")), _num(spec.get("duration", seconds), env), str(spec.get("ease", curve)),
            _num(spec.get("stagger", stagger), env))  # fmt: skip


def _sample_keys(layer: dict, t: float, st: dict) -> dict:
    """The layer with its keyframed numbers at `t` seconds into the scene (or group)."""
    keys = sorted((k for k in layer.get("keys") or [] if isinstance(k, dict) and "t" in k), key=lambda k: float(k["t"]))
    if not keys:
        return layer
    out = dict(layer)
    default = layer.get("ease") or st.get("motion", {}).get("ease", "out_cubic")
    names = {n for k in keys for n in k if n not in ("t", "ease")}
    for name in names:
        marks = [k for k in keys if name in k]
        if t <= float(marks[0]["t"]):
            out[name] = marks[0][name]
            continue
        out[name] = marks[-1][name]
        for a, b in zip(marks, marks[1:]):
            ta, tb = float(a["t"]), float(b["t"])
            if t <= tb:
                p = ease(b.get("ease", default), (t - ta) / (tb - ta) if tb > ta else 1.0)
                va, vb = a[name], b[name]
                if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                    out[name] = va + (vb - va) * p
                elif isinstance(va, list) and isinstance(vb, list) and len(va) == len(vb):
                    out[name] = [x + (y - x) * p for x, y in zip(va, vb)] if all(isinstance(x, (int, float)) for x in va + vb) else vb
                else:
                    out[name] = va if p < 1 else vb
                break
    return out


def _timed(layer: dict, t: float, ctx: Ctx, env: dict) -> tuple[dict, State] | None:
    """The layer as it is `t` seconds into its scene (or group), or None when it is not on then."""
    # A part of a split scene carries the time it was cut at, so formulas of t carry on across the cut.
    base = {**env, "t": t + float(layer.get("shift", 0.0)), "d": ctx.duration}
    start = _num(layer.get("start", 0.0), base)
    end = _num(layer.get("end", ctx.duration), base)
    if t < start - 1e-9 or t > end + 1e-9:
        return None
    lt = t - start
    full = {**base, "lt": lt, "p": lt / (end - start) if end > start else 1.0}
    enter, in_seconds, in_ease, stagger = _effect(layer.get("enter"), ctx.st, False, full)
    leave, out_seconds, out_ease, _ = _effect(layer.get("exit"), ctx.st, True, full)
    if enter in SPECIAL and layer.get("type") not in SPECIAL[enter]:
        enter = "fade"
    raw_in = 1.0 if enter == "none" or in_seconds <= 0 else min(1.0, lt / in_seconds)
    p_in = 1.0 if enter == "none" else ease(in_ease, raw_in)
    p_out = 0.0
    if leave != "none" and out_seconds > 0 and t > end - out_seconds:
        p_out = ease(out_ease, (t - (end - out_seconds)) / out_seconds)
    resolved = {k: (v if k in ("keys", "layers") else _resolve(v, full, k)) for k, v in layer.items()}
    resolved["start"], resolved["end"] = start, end
    if layer.get("keys"):
        resolved["keys"] = [{k: (v if k == "ease" else _resolve(v, full, k)) for k, v in key.items()}
                            for key in layer["keys"] if isinstance(key, dict)]  # fmt: skip
        resolved = _sample_keys(resolved, t, ctx.st)
    return resolved, State(lt, enter, p_in, raw_in, in_seconds, leave, p_out, in_ease, stagger, full)


# ---- geometry helpers ---------------------------------------------------------------------------------


def _box(layer: dict, ctx: Ctx, w: float = 0.3, h: float = 0.2) -> QRectF:
    """A layer's box from its centre (x, y) and size (w, h), in pixels."""
    bw, bh = ctx.x(layer.get("w", w)), ctx.y(layer.get("h", h))
    return QRectF(ctx.x(layer.get("x", 0.5)) - bw / 2, ctx.y(layer.get("y", 0.5)) - bh / 2, bw, bh)


def _points(layer: dict, ctx: Ctx) -> list[QPointF]:
    dx, dy = ctx.x(layer.get("dx", 0)), ctx.y(layer.get("dy", 0))
    return [QPointF(ctx.x(p[0]) + dx, ctx.y(p[1]) + dy) for p in layer.get("points") or []]


def _smooth(points: list[QPointF]) -> QPainterPath:
    """A curve through the points (Catmull-Rom)."""
    path = QPainterPath(points[0])
    for i in range(len(points) - 1):
        p0, p1, p2 = points[max(0, i - 1)], points[i], points[i + 1]
        p3 = points[min(len(points) - 1, i + 2)]
        path.cubicTo(p1 + (p2 - p0) / 6, p2 - (p3 - p1) / 6, p2)
    return path


def _polyline(points: list[QPointF], closed: bool = False) -> QPainterPath:
    path = QPainterPath()
    if points:
        path.moveTo(points[0])
        for p in points[1:]:
            path.lineTo(p)
        if closed:
            path.closeSubpath()
    return path


def partial(path: QPainterPath, fraction: float) -> QPainterPath:
    """The first `fraction` of the path's length, for drawing lines on."""
    if fraction >= 1:
        return path
    polys = path.toSubpathPolygons()
    pieces = []
    total = 0.0
    for poly in polys:
        pts = [poly[i] for i in range(poly.size())]
        lengths = [math.dist((a.x(), a.y()), (b.x(), b.y())) for a, b in zip(pts, pts[1:])]
        pieces.append((pts, lengths))
        total += sum(lengths)
    left = total * max(0.0, fraction)
    out = QPainterPath()
    for pts, lengths in pieces:
        if left <= 0 or not pts:
            break
        out.moveTo(pts[0])
        for b, length in zip(pts[1:], lengths):
            if left >= length:
                out.lineTo(b)
                left -= length
            else:
                a = out.currentPosition()
                share = left / length if length else 0
                out.lineTo(a + (b - a) * share)
                left = 0
                break
    return out


def _tip(path: QPainterPath) -> tuple[QPointF, float] | None:
    """The end of the path and its heading (radians)."""
    polys = path.toSubpathPolygons()
    if not polys or polys[-1].size() < 2:
        return None
    poly = polys[-1]
    end = poly[poly.size() - 1]
    for i in range(poly.size() - 2, -1, -1):
        a = poly[i]
        if math.dist((a.x(), a.y()), (end.x(), end.y())) > 0.5:
            return end, math.atan2(end.y() - a.y(), end.x() - a.x())
    return None


def _head(at: QPointF, angle: float, size: float) -> QPainterPath:
    back = angle + math.pi
    left = QPointF(at.x() + size * math.cos(back + 0.45), at.y() + size * math.sin(back + 0.45))
    right = QPointF(at.x() + size * math.cos(back - 0.45), at.y() + size * math.sin(back - 0.45))
    return _polyline([at, left, right], closed=True)


# ---- SVG path data ----------------------------------------------------------------------------------------

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@lru_cache(maxsize=128)
def svg_path(d: str) -> QPainterPath:
    """A QPainterPath from SVG path data (M L H V C S Q T A Z, absolute and relative)."""
    tokens = _TOKEN.findall(d)
    path = QPainterPath()
    i, cmd = 0, None
    cur = start = QPointF(0, 0)
    last_ctrl = None

    def num() -> float:
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                path.closeSubpath()
                cur = start
                last_ctrl = None
                continue
        if cmd is None:
            raise ValueError("SVG 경로가 명령(M 등)으로 시작하지 않습니다.")
        rel = cmd.islower()
        c = cmd.upper()
        base = cur if rel else QPointF(0, 0)
        if c == "M":
            cur = start = base + QPointF(num(), num())
            path.moveTo(cur)
            cmd = "l" if rel else "L"  # more pairs after a move are lines
            last_ctrl = None
        elif c == "L":
            cur = base + QPointF(num(), num())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "H":
            cur = QPointF((cur.x() if rel else 0) + num(), cur.y())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "V":
            cur = QPointF(cur.x(), (cur.y() if rel else 0) + num())
            path.lineTo(cur)
            last_ctrl = None
        elif c == "C":
            c1, c2, end = base + QPointF(num(), num()), base + QPointF(num(), num()), base + QPointF(num(), num())
            path.cubicTo(c1, c2, end)
            cur, last_ctrl = end, ("C", c2)
        elif c == "S":
            c1 = cur * 2 - last_ctrl[1] if last_ctrl and last_ctrl[0] == "C" else cur
            c2, end = base + QPointF(num(), num()), base + QPointF(num(), num())
            path.cubicTo(c1, c2, end)
            cur, last_ctrl = end, ("C", c2)
        elif c == "Q":
            c1, end = base + QPointF(num(), num()), base + QPointF(num(), num())
            path.quadTo(c1, end)
            cur, last_ctrl = end, ("Q", c1)
        elif c == "T":
            c1 = cur * 2 - last_ctrl[1] if last_ctrl and last_ctrl[0] == "Q" else cur
            end = base + QPointF(num(), num())
            path.quadTo(c1, end)
            cur, last_ctrl = end, ("Q", c1)
        elif c == "A":
            rx, ry, phi, large, sweep = num(), num(), num(), num(), num()
            end = base + QPointF(num(), num())
            for p in _arc(cur, end, rx, ry, phi, bool(large), bool(sweep)):
                path.lineTo(p)
            cur, last_ctrl = end, None
    return path


def _arc(a: QPointF, b: QPointF, rx: float, ry: float, phi: float, large: bool, sweep: bool) -> list[QPointF]:
    """Points along an SVG elliptical arc (endpoint form, as in the SVG spec's appendix)."""
    if rx == 0 or ry == 0 or (a.x() == b.x() and a.y() == b.y()):
        return [b]
    rx, ry = abs(rx), abs(ry)
    cos_p, sin_p = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (a.x() - b.x()) / 2, (a.y() - b.y()) / 2
    x1, y1 = cos_p * dx + sin_p * dy, -sin_p * dx + cos_p * dy
    scale = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if scale > 1:
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    factor = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        factor = -factor
    cx1, cy1 = factor * rx * y1 / ry, -factor * ry * x1 / rx
    cx = cos_p * cx1 - sin_p * cy1 + (a.x() + b.x()) / 2
    cy = sin_p * cx1 + cos_p * cy1 + (a.y() + b.y()) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    theta = angle(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    delta = angle((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    steps = max(4, int(abs(delta) / math.radians(5)))
    out = []
    for k in range(1, steps + 1):
        th = theta + delta * k / steps
        x, y = rx * math.cos(th), ry * math.sin(th)
        out.append(QPointF(cos_p * x - sin_p * y + cx, sin_p * x + cos_p * y + cy))
    return out


# ---- backgrounds --------------------------------------------------------------------------------------


def _background_spec(layer: dict, st: dict) -> dict:
    """A background: a preset of the style (changed by what the layer says), or the layer's own colours alone."""
    presets = st.get("backgrounds", {})
    name = layer.get("preset")
    if name is None and ("color" in layer or "colors" in layer):
        spec = {}
    else:
        name = name or "default"
        if name not in presets:
            raise ValueError(f"스타일에 '{name}' 배경이 없습니다. 있는 배경: {', '.join(presets)}")
        spec = dict(presets[name])
    spec.update({k: v for k, v in layer.items() if k in BACKGROUND_KEYS - {"preset"}})
    return spec


@lru_cache(maxsize=16)
def _grain(width: int, height: int) -> QImage:
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, (height, width), dtype=np.uint8)
    image = QImage(noise.data, width, height, width, QImage.Format.Format_Grayscale8)
    return image.copy()


@lru_cache(maxsize=16)
def _background_image(spec_json: str, style_json: str, width: int, height: int) -> QImage:
    spec, st = json.loads(spec_json), json.loads(style_json)
    ctx = Ctx(st, width, height, 0.0)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.black)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colors = [ctx.colour(c) for c in spec.get("colors") or []]
    gradient = spec.get("gradient") or ("linear" if len(colors) > 1 else None)
    if gradient == "radial" and len(colors) > 1:
        cx, cy = spec.get("center", [0.5, 0.5])
        fill = QRadialGradient(QPointF(ctx.x(cx), ctx.y(cy)), float(spec.get("radius", 0.8)) * max(width, height))
    elif gradient and len(colors) > 1:
        angle = math.radians(float(spec.get("angle", 90)))
        half = QPointF(math.cos(angle), math.sin(angle)) * (abs(width * math.cos(angle)) + abs(height * math.sin(angle))) / 2
        middle = QPointF(width / 2, height / 2)
        fill = QLinearGradient(middle - half, middle + half)
    else:
        fill = None
    if fill is not None:
        for k, c in enumerate(colors):
            fill.setColorAt(k / (len(colors) - 1), c)
        painter.fillRect(0, 0, width, height, fill)
    else:
        painter.fillRect(0, 0, width, height, ctx.colour(spec.get("color", colors[0] if colors else "bg")))
    grid = spec.get("grid")
    if grid:
        step = max(4.0, float(grid.get("spacing", 0.05)) * width)
        size = max(1.0, float(grid.get("width", 1)) * ctx.u)
        c = ctx.colour(grid.get("color", "grid"))
        if grid.get("dots"):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            y = step / 2
            while y < height:
                x = step / 2
                while x < width:
                    painter.drawEllipse(QPointF(x, y), size / 2, size / 2)
                    x += step
                y += step
        else:
            painter.setPen(QPen(c, size))
            x = step
            while x < width:
                painter.drawLine(QPointF(x, 0), QPointF(x, height))
                x += step
            y = step
            while y < height:
                painter.drawLine(QPointF(0, y), QPointF(width, y))
                y += step
    vignette = float(spec.get("vignette", 0))
    if vignette > 0:
        shade = QRadialGradient(QPointF(width / 2, height / 2), math.hypot(width, height) / 2)
        shade.setColorAt(0.45, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, round(255 * min(1.0, vignette))))
        painter.fillRect(0, 0, width, height, shade)
    grain = float(spec.get("grain", 0))
    if grain > 0:
        painter.setOpacity(min(1.0, grain))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Overlay)
        painter.drawImage(0, 0, _grain(width, height))
    painter.end()
    return image


def _draw_background(painter: QPainter, layer: dict, ctx: Ctx) -> None:
    spec = _background_spec(layer, ctx.st)
    painter.drawImage(0, 0, _background_image(json.dumps(spec, sort_keys=True), json.dumps(ctx.st, sort_keys=True),
                                              ctx.width, ctx.height))  # fmt: skip


# ---- shapes and lines ---------------------------------------------------------------------------------


def _gradient(spec: dict, ctx: Ctx, rect: QRectF):
    """A gradient laid across `rect`: {"gradient": "linear"|"radial", "colors": [...], "angle", "center", "radius", "stops"}."""
    colours = [ctx.colour(c) for c in spec.get("colors") or []]
    if len(colours) < 2:
        raise ValueError("그라데이션에는 colors가 두 개 이상 필요합니다.")
    stops = spec.get("stops") or [k / (len(colours) - 1) for k in range(len(colours))]
    kind = spec.get("gradient", "linear")
    if kind == "radial":
        cx, cy = spec.get("center", [0.5, 0.5])
        centre = QPointF(rect.left() + float(cx) * rect.width(), rect.top() + float(cy) * rect.height())
        gradient = QRadialGradient(centre, max(1.0, float(spec.get("radius", 0.5)) * max(rect.width(), rect.height())))
    elif kind == "linear":
        angle = math.radians(float(spec.get("angle", 90)))
        reach = (abs(rect.width() * math.cos(angle)) + abs(rect.height() * math.sin(angle))) / 2
        half = QPointF(math.cos(angle), math.sin(angle)) * reach
        gradient = QLinearGradient(rect.center() - half, rect.center() + half)
    else:
        raise ValueError(f"gradient는 \"linear\"나 \"radial\"입니다 (받은 값: {kind!r})")
    for stop, c in zip(stops, colours):
        gradient.setColorAt(min(1.0, max(0.0, float(stop))), c)
    return gradient


def _brush(value, ctx: Ctx, rect: QRectF | None = None) -> QBrush:
    """A colour, or a gradient across `rect` (the shape's box)."""
    if isinstance(value, dict):
        return QBrush(_gradient(value, ctx, rect or QRectF(0, 0, ctx.width, ctx.height)))
    return QBrush(ctx.colour(value))


def _pen(ctx: Ctx, colour_value, width: float, dash=None, rect: QRectF | None = None) -> QPen:
    pen = QPen(_brush(colour_value, ctx, rect), max(0.5, float(width) * ctx.u))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if dash:
        on, off = dash
        pen.setDashPattern([max(0.1, on / max(0.5, float(width))), max(0.1, off / max(0.5, float(width)))])
    return pen


def _fill_and_stroke(painter: QPainter, path: QPainterPath, layer: dict, ctx: Ctx, defaults: dict, state: State,
                     closed: bool = True) -> None:  # fmt: skip
    """Fill and outline a shape; drawn on, the outline goes round first and the fill follows."""
    fill = layer.get("fill", defaults.get("fill")) if closed else None
    stroke = layer.get("stroke", defaults.get("stroke"))
    width = layer.get("width", defaults.get("width", 2))
    drawing = state.enter == "draw"
    p = state.p_in if drawing else 1.0
    box = path.boundingRect()
    if fill:
        painter.save()
        if drawing:
            painter.setOpacity(painter.opacity() * min(1.0, max(0.0, (p - 0.55) / 0.45)) if stroke else painter.opacity() * p)
        painter.fillPath(path, _brush(fill, ctx, box))
        painter.restore()
    if stroke and float(width) > 0:
        painter.strokePath(partial(path, p) if drawing else path, _pen(ctx, stroke, width, layer.get("dash"), box))


def _shape_path(layer: dict, ctx: Ctx) -> QPainterPath:
    kind = layer["type"]
    path = QPainterPath()
    if kind == "rect":
        radius = float(layer.get("radius", ctx.st.get("shape", {}).get("radius", 0))) * ctx.u
        box = _box(layer, ctx)
        path.addRoundedRect(box, min(radius, box.width() / 2), min(radius, box.height() / 2))
    elif kind == "ellipse":
        path.addEllipse(_box(layer, ctx, 0.2, 0.2))
    elif kind == "circle":
        r = float(layer.get("r", 0.1)) * min(ctx.width, ctx.height)
        path.addEllipse(QPointF(ctx.x(layer.get("x", 0.5)), ctx.y(layer.get("y", 0.5))), r, r)
    elif kind == "polygon":
        path = _polyline(_points(layer, ctx), closed=True)
    return path


def _draw_shape(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    _fill_and_stroke(painter, _shape_path(layer, ctx), layer, ctx, ctx.st.get("shape", {}), state)


def _line_path(layer: dict, ctx: Ctx) -> QPainterPath:
    if layer["type"] == "arrow" and not layer.get("points"):
        a = ctx.point(layer.get("from", [0.3, 0.5]))
        b = ctx.point(layer.get("to", [0.7, 0.5]))
        shift = QPointF(ctx.x(layer.get("dx", 0)), ctx.y(layer.get("dy", 0)))
        a, b = a + shift, b + shift
        bend = float(layer.get("bend", 0))
        path = QPainterPath(a)
        if bend:
            mid = (a + b) / 2
            normal = QPointF(-(b.y() - a.y()), b.x() - a.x())
            path.quadTo(mid + normal * bend, b)
        else:
            path.lineTo(b)
        return path
    points = _points(layer, ctx)
    if len(points) < 2:
        return QPainterPath()
    return _smooth(points) if layer.get("smooth") else _polyline(points)


def _draw_line(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    arrow = layer["type"] == "arrow"
    defaults = ctx.st.get("arrow" if arrow else "line", {})
    path = _line_path(layer, ctx)
    if path.isEmpty():
        return
    p = state.p_in if state.enter == "draw" else 1.0
    shown = partial(path, p)
    width = float(layer.get("width", defaults.get("width", 4)))
    pen = _pen(ctx, layer.get("color", defaults.get("color", "ink")), width, layer.get("dash"))
    painter.strokePath(shown, pen)
    head = layer.get("head", "end" if arrow else None)
    if arrow and layer.get("double"):
        head = "both"
    if not head or p <= 0:
        return
    size = float(layer.get("head_size", defaults.get("head", 24))) * ctx.u
    solid = QPen(pen)
    solid.setStyle(Qt.PenStyle.SolidLine)
    painter.save()
    painter.setPen(solid)
    painter.setBrush(pen.color())
    if head in ("end", "both"):
        tip = _tip(shown)
        if tip:
            painter.drawPath(_head(tip[0], tip[1], size))
    if head in ("start", "both"):
        reverse = path.toReversed()
        tip = _tip(partial(reverse, 1.0) if p >= 1 else QPainterPath())
        if tip:
            painter.drawPath(_head(tip[0], tip[1], size))
    painter.restore()


def _fitted_path(d: str, layer: dict, ctx: Ctx, viewbox=None) -> QPainterPath:
    """SVG path data placed in the layer's box (its width set; its height follows the drawing's shape)."""
    source = svg_path(d)
    vb = viewbox or layer.get("viewbox") or [0, 0, 24, 24]
    vx, vy, vw, vh = (float(v) for v in vb)
    w = ctx.x(layer.get("w", 0.1))
    h = ctx.y(layer["h"]) if "h" in layer else w * vh / vw
    cx, cy = ctx.x(layer.get("x", 0.5)), ctx.y(layer.get("y", 0.5))
    transform = QTransform()
    transform.translate(cx - w / 2, cy - h / 2)
    transform.scale(w / vw, h / vh)
    transform.translate(-vx, -vy)
    return transform.map(source)


def _draw_path(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    path = _fitted_path(layer.get("d", ""), layer, ctx)
    defaults = dict(ctx.st.get("shape", {}))
    if layer.get("mode") == "stroke":
        defaults.update(fill=None, stroke=ctx.st.get("line", {}).get("color", "ink"), width=ctx.st.get("line", {}).get("width", 4))
    _fill_and_stroke(painter, path, layer, ctx, defaults, state)


@lru_cache(maxsize=16)
def _svg(path: str, mtime: int) -> QSvgRenderer:
    renderer = QSvgRenderer(path)
    if not renderer.isValid():
        raise ValueError(f"SVG 그림을 열 수 없습니다: {path}")
    return renderer


def prop_file(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else styles_dir() / path


def _draw_prop(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    props = ctx.st.get("props", {})
    name = layer.get("name", "")
    if name not in props:
        raise ValueError(f"스타일에 '{name}' 소품이 없습니다. 있는 소품: {', '.join(props)}")
    prop = props[name]
    if isinstance(prop, str):  # an SVG file
        file = prop_file(prop)
        renderer = _svg(str(file), int(file.stat().st_mtime_ns) if file.exists() else 0)
        size = renderer.defaultSize()
        w = ctx.x(layer.get("w", 0.1))
        h = w * size.height() / max(1, size.width())
        box = QRectF(ctx.x(layer.get("x", 0.5)) - w / 2, ctx.y(layer.get("y", 0.5)) - h / 2, w, h)
        if layer.get("color"):  # one colour line art takes the colour asked for
            tinted = QImage(max(1, round(w)), max(1, round(h)), QImage.Format.Format_ARGB32_Premultiplied)
            tinted.fill(Qt.GlobalColor.transparent)
            inner = QPainter(tinted)
            renderer.render(inner)
            inner.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            inner.fillRect(tinted.rect(), ctx.colour(layer["color"]))
            inner.end()
            painter.drawImage(box, tinted)
        else:
            renderer.render(painter, box)
        return
    mode = prop.get("mode", "stroke")
    path = _fitted_path(prop["d"], layer, ctx, prop.get("viewbox"))
    line = ctx.st.get("line", {})
    colour_value = layer.get("color", line.get("color", "ink"))
    if mode == "fill":
        defaults = {"fill": colour_value, "stroke": None}
    else:
        # The stroke keeps the same weight whatever the prop's size: set in 1080 pixels, like lines.
        defaults = {"fill": layer.get("fill"), "stroke": colour_value, "width": layer.get("width", line.get("width", 4))}
    _fill_and_stroke(painter, path, {k: v for k, v in layer.items() if k not in ("color", "fill", "width")}, ctx, defaults, state)


def _draw_image(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    from .canvas import _load, _mtime  # the pictures the rest of the editor loads, cached the same way

    file = str(layer.get("file", ""))
    image = _load(file, _mtime(file), max(ctx.width, ctx.height))
    w = ctx.x(layer.get("w", 0.3))
    h = w * image.height() / max(1, image.width())
    painter.drawImage(QRectF(ctx.x(layer.get("x", 0.5)) - w / 2, ctx.y(layer.get("y", 0.5)) - h / 2, w, h), image)


# ---- words --------------------------------------------------------------------------------------------
# Words are laid out letter by letter, so parts can have their own colour, weight and size ("spans", or
# "[accent]...[/]" marks with "markup": true) and letters can come in one at a time or ride a wave.

_TAG = re.compile(r"\[\[|\[(/?)([^\[\]]*)\]")


@lru_cache(maxsize=64)
def _qfont(family: str, pixels: int, bold: bool) -> QFont:
    font = QFont(family)
    font.setPixelSize(max(6, pixels))
    font.setBold(bold)
    return font


@lru_cache(maxsize=8192)
def _advance(family: str, pixels: int, bold: bool, char: str) -> float:
    return QFontMetricsF(_qfont(family, pixels, bold)).horizontalAdvance(char)


@lru_cache(maxsize=256)
def _metrics(family: str, pixels: int, bold: bool) -> tuple[float, float]:
    metrics = QFontMetricsF(_qfont(family, pixels, bold))
    return metrics.lineSpacing(), metrics.ascent()


@lru_cache(maxsize=4096)
def _glyph(char: str, family: str, pixels: int, bold: bool) -> QPainterPath:
    path = QPainterPath()
    path.addText(QPointF(0, 0), _qfont(family, pixels, bold), char)
    return path


def _font_spec(layer: dict, fmt: dict, ctx: Ctx, role_default: str) -> tuple[tuple[str, int, bool], object]:
    """((family, pixel size, bold), fill) of a run of words: its own marks over the layer over the style's role."""
    fonts = ctx.st.get("fonts", {})
    role = fmt.get("role", layer.get("role", role_default))
    if role not in fonts:
        raise ValueError(f"스타일에 '{role}' 글꼴 역할이 없습니다. 있는 역할: {', '.join(fonts)}")
    spec = fonts[role]
    family = str(fmt.get("font", layer.get("font", spec.get("family", "Malgun Gothic"))))
    size = float(fmt.get("size", layer.get("size", spec.get("size", 48))))
    bold = bool(fmt.get("bold", layer.get("bold", spec.get("bold", False))))
    fill = fmt.get("color", layer.get("color", spec.get("color", "ink")))
    return (family, max(6, round(size * ctx.u)), bold), fill


def _font(layer: dict, ctx: Ctx, role_default: str) -> tuple[QFont, QColor]:
    key, fill = _font_spec(layer, {}, ctx, role_default)
    return _qfont(*key), ctx.colour(fill) if not isinstance(fill, dict) else ctx.colour("ink")


def _runs(layer: dict, text: str) -> list[tuple[str, dict]]:
    """(words, their marks) in order: from "spans", or from [accent] [b] [size=72] [role=title] ... [/] marks."""
    spans = layer.get("spans")
    if spans:
        keep = ("color", "bold", "size", "role", "font")
        return [(str(s.get("text", "")), {k: s[k] for k in keep if k in s}) for s in spans if isinstance(s, dict)]
    if not layer.get("markup"):
        return [(text, {})]
    runs, stack, pos = [], [], 0

    def marks() -> dict:
        out: dict = {}
        for m in stack:
            out.update(m)
        return out

    for m in _TAG.finditer(text):
        if m.start() > pos:
            runs.append((text[pos : m.start()], marks()))
        pos = m.end()
        if m.group(0) == "[[":
            runs.append(("[", marks()))
            continue
        closing, tag = m.group(1), m.group(2).strip()
        if closing:
            if stack:
                stack.pop()
        elif tag == "b":
            stack.append({"bold": True})
        elif tag.startswith("size="):
            stack.append({"size": float(tag[5:])})
        elif tag.startswith("role="):
            stack.append({"role": tag[5:]})
        else:
            stack.append({"color": tag})
    if pos < len(text):
        runs.append((text[pos:], marks()))
    return runs


@dataclass
class _Glyph:
    char: str
    font: tuple  # (family, pixel size, bold)
    fill: object
    x: float  # from its line's left
    advance: float
    index: int  # among all the letters, spaces included (for typing and letter timing)


@dataclass
class _Line:
    glyphs: list
    width: float
    height: float
    ascent: float


@dataclass
class _Layout:
    lines: list
    tops: list
    rect: QRectF
    count: int


def _text_layout(text: str, layer: dict, ctx: Ctx, role: str) -> _Layout:
    letters = []
    for piece, fmt in _runs(layer, text):
        key, fill = _font_spec(layer, fmt, ctx, role)
        letters += [(ch, key, fill) for ch in piece]
    limit = ctx.x(layer.get("wrap", TEXT_WRAP))
    base_key = _font_spec(layer, {}, ctx, role)[0]
    lines: list[_Line] = []
    current: list[_Glyph] = []
    x = 0.0
    index = 0

    def flush():
        nonlocal current, x
        while current and current[-1].char.isspace():
            current.pop()
        keys = {g.font for g in current} or {base_key}
        spacing = max(_metrics(*k)[0] for k in keys) * float(layer.get("line_height", 1.0))
        ascent = max(_metrics(*k)[1] for k in keys)
        width = current[-1].x + current[-1].advance if current else 0.0
        lines.append(_Line(current, width, spacing, ascent))
        current, x = [], 0.0

    # Words go whole to the next line when they do not fit; a word longer than a line is broken anywhere.
    tokens, word = [], []
    for item in letters:
        if item[0] == "\n" or item[0].isspace():
            if word:
                tokens.append(word)
                word = []
            tokens.append([item])
        else:
            word.append(item)
    if word:
        tokens.append(word)
    for token in tokens:
        if token[0][0] == "\n":
            flush()
            index += 1
            continue
        widths = [_advance(*key, ch) for ch, key, _ in token]
        if token[0][0].isspace():
            if current:
                current.append(_Glyph(token[0][0], token[0][1], token[0][2], x, widths[0], index))
                x += widths[0]
            index += 1
            continue
        if current and x + sum(widths) > limit:
            flush()
        for (ch, key, fill), w in zip(token, widths):
            if current and x + w > limit and sum(widths) > limit:
                flush()
            current.append(_Glyph(ch, key, fill, x, w, index))
            x += w
            index += 1
    flush()
    block_w = max((line.width for line in lines), default=0.0)
    block_h = sum(line.height for line in lines)
    cx, cy = ctx.x(layer.get("x", 0.5)), ctx.y(layer.get("y", 0.5))
    align = layer.get("align", "center")
    left = cx - block_w / 2 if align == "center" else (cx if align == "left" else cx - block_w)
    tops, top = [], cy - block_h / 2
    for line in lines:
        tops.append(top)
        top += line.height
    return _Layout(lines, tops, QRectF(left, cy - block_h / 2, block_w, block_h), index)


def _letter(effect: str, p_raw: float, p: float, em: float) -> tuple[float, float, float, float]:
    """(drop down, size, turn, see-through) of one letter coming in."""
    if effect == "chars_rise":
        return (1 - p) * 0.6 * em, 1.0, 0.0, p
    if effect == "chars_drop":
        return -(1 - p) * 0.6 * em, 1.0, 0.0, p
    if effect == "chars_pop":
        return 0.0, ease("out_back", p_raw), 0.0, min(1.0, p_raw * 3)
    if effect == "chars_spin":
        return 0.0, 0.6 + 0.4 * p, -90 * (1 - p), p
    return 0.0, 1.0, 0.0, p


def _draw_words(painter: QPainter, text: str, layer: dict, ctx: Ctx, state: State, role: str) -> None:
    lay = _text_layout(text, layer, ctx, role)
    if not lay.lines or lay.count == 0:
        return
    rect = lay.rect
    box = layer.get("box")
    if box:
        spec = dict(ctx.st.get("text_box", {}))
        if isinstance(box, dict):
            spec.update(box)
        pad = float(spec.get("padding", 20)) * ctx.u
        radius = float(spec.get("radius", 12)) * ctx.u
        area = rect.adjusted(-pad, -pad * 0.6, pad, pad * 0.6)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_brush(spec.get("fill", "#000000/0.35"), ctx, area))
        painter.drawRoundedRect(area, radius, radius)
        painter.restore()
    shown = lay.count
    if state.enter == "type":
        shown = int(lay.count * state.raw_in + 1e-6)
    letters = state.enter if state.enter in CHAR_EFFECTS else None
    wave = layer.get("wave")
    if wave is True:
        wave = {}
    outline = layer.get("outline")
    pen = None
    if outline:
        spec = outline if isinstance(outline, dict) else {}
        pen = _pen(ctx, spec.get("color", "#000000/0.8"), float(spec.get("width", 6)), rect=rect)
    brushes: dict = {}
    align = layer.get("align", "center")
    base_opacity = painter.opacity()
    for line, top in zip(lay.lines, lay.tops):
        left = rect.left() + ((rect.width() - line.width) / 2 if align == "center" else (rect.width() - line.width if align == "right" else 0))
        baseline = top + line.ascent
        for g in line.glyphs:
            if g.index >= shown or g.char.isspace():
                continue
            em = g.font[1]
            dy, size, turn, alpha = 0.0, 1.0, 0.0, 1.0
            if letters:
                p_raw = (state.t - g.index * state.stagger) / max(1e-6, state.enter_seconds)
                if p_raw <= 0:
                    continue
                p_raw = min(1.0, p_raw)
                dy, size, turn, alpha = _letter(letters, p_raw, ease(state.enter_ease, p_raw), em)
            if wave is not None:
                amplitude = float(wave.get("amplitude", 12)) * ctx.u
                phase = 2 * math.pi * (float(wave.get("speed", 1.0)) * state.t - g.index * float(wave.get("spread", 0.12)))
                dy += amplitude * math.sin(phase)
            transform = QTransform()
            transform.translate(left + g.x, baseline + dy)
            if size != 1.0 or turn:
                cx, cy = g.advance / 2, -em * 0.35
                transform.translate(cx, cy)
                transform.rotate(turn)
                transform.scale(size, size)
                transform.translate(-cx, -cy)
            path = transform.map(_glyph(g.char, *g.font))
            key = json.dumps(g.fill, sort_keys=True) if isinstance(g.fill, dict) else str(g.fill)
            if key not in brushes:
                brushes[key] = _brush(g.fill, ctx, rect)  # a gradient runs across the whole block
            painter.setOpacity(base_opacity * alpha)
            if pen is not None:
                painter.strokePath(path, pen)
            painter.fillPath(path, brushes[key])
    painter.setOpacity(base_opacity)


def _draw_text(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    _draw_words(painter, str(layer.get("text", "")), layer, ctx, state, "body")


def _counter_text(layer: dict, state: State) -> str:
    start, end = float(layer.get("from", 0)), float(layer.get("to", 100))
    p = state.p_in if state.enter == "count" else 1.0
    value = start + (end - start) * p
    decimals = int(layer.get("decimals", 0))
    text = f"{value:,.{decimals}f}".replace(",", layer.get("separator", ","))
    return f"{layer.get('prefix', '')}{text}{layer.get('suffix', '')}"


def _draw_counter(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    _draw_words(painter, _counter_text(layer, state), layer, ctx, state, "number")


# ---- colour tiles -------------------------------------------------------------------------------------


def _colormap(value, st: dict) -> list[QColor]:
    maps = st.get("colormaps", {})
    stops = maps.get(value) if isinstance(value, str) else value
    if not stops:
        raise ValueError(f"스타일에 '{value}' 색 지도가 없습니다. 있는 색 지도: {', '.join(maps)}")
    return [colour(c, st) for c in stops]


def _mapped(values: np.ndarray, stops: list[QColor]) -> np.ndarray:
    """RGBA (0-255) for values from 0 to 1 along the colour stops."""
    table = np.array([[c.red(), c.green(), c.blue(), c.alpha()] for c in stops], float)
    pos = np.clip(values, 0, 1) * (len(stops) - 1)
    low = np.clip(np.floor(pos).astype(int), 0, len(stops) - 1)
    high = np.clip(low + 1, 0, len(stops) - 1)
    share = (pos - low)[..., None]
    return table[low] * (1 - share) + table[high] * share


def _smooth_grid(grid: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return grid
    radius = max(1, int(3 * sigma))
    xs = np.arange(-radius, radius + 1)
    kernel = np.exp(-(xs**2) / (2 * sigma * sigma))
    kernel /= kernel.sum()
    padded = np.pad(grid, radius, mode="reflect")
    rows = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="valid"), 1, padded)
    return np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="valid"), 0, rows)


@lru_cache(maxsize=32)
def _field(spec_json: str, rows: int, cols: int) -> np.ndarray:
    spec = json.loads(spec_json)
    kind = spec.get("kind", "noise")
    ys, xs = np.mgrid[0:rows, 0:cols]
    u = (xs + 0.5) / cols
    v = (ys + 0.5) / rows
    if kind == "noise":
        rng = np.random.default_rng(int(spec.get("seed", 1)))
        grid = _smooth_grid(rng.random((rows, cols)), float(spec.get("smooth", 2.0)))
    elif kind == "hotspots":
        grid = np.zeros((rows, cols))
        for point in spec.get("points") or [[0.5, 0.5, 1.0, 0.2]]:
            px, py = point[0], point[1]
            strength = point[2] if len(point) > 2 else 1.0
            spread = point[3] if len(point) > 3 else 0.2
            grid += strength * np.exp(-((u - px) ** 2 + (v - py) ** 2) / (2 * spread * spread))
    elif kind == "radial":
        cx, cy = spec.get("center", [0.5, 0.5])
        grid = 1 - np.sqrt((u - cx) ** 2 + (v - cy) ** 2) / math.sqrt(0.5)
    elif kind == "linear":
        angle = math.radians(float(spec.get("angle", 0)))
        grid = u * math.cos(angle) + v * math.sin(angle)
    elif kind == "bending":
        # Like the stress in a cantilever: largest at the fixed end (left) on the top and bottom faces.
        grid = np.abs(v - 0.5) * 2 * (1 - u) ** float(spec.get("power", 1.0))
    elif kind == "wave":
        freq = float(spec.get("frequency", 2.0))
        grid = np.sin(2 * math.pi * freq * u) * np.cos(2 * math.pi * freq * v)
    else:
        raise ValueError(f"알 수 없는 값 분포: {kind} (고를 수 있는 것: {', '.join(FIELD_KINDS)})")
    if spec.get("normalize", True):
        low, high = float(grid.min()), float(grid.max())
        grid = (grid - low) / (high - low) if high > low else np.zeros_like(grid)
    return grid


def _tile_values(layer: dict) -> np.ndarray:
    if layer.get("values"):
        grid = np.array(layer["values"], float)
        if grid.ndim == 1:
            grid = grid[None, :]
        low, high = float(layer.get("min", grid.min())), float(layer.get("max", grid.max()))
        return (grid - low) / (high - low) if high > low else np.zeros_like(grid)
    spec = layer.get("field") or {"kind": "noise"}
    return _field(json.dumps(spec, sort_keys=True), int(layer.get("rows", 12)), int(layer.get("cols", 20)))


def _draw_tiles(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    values = _tile_values(layer)
    rows, cols = values.shape
    box = _box(layer, ctx, 0.5, 0.4)
    gap = float(layer.get("gap", 2)) * ctx.u
    radius = float(layer.get("radius", 0)) * ctx.u
    stops = _colormap(layer.get("colormap", "analysis"), ctx.st)
    cw, ch = (box.width() - gap * (cols - 1)) / cols, (box.height() - gap * (rows - 1)) / rows
    shown = np.ones_like(values)
    if state.enter == "sweep":
        order = layer.get("order", "diagonal")
        ys, xs = np.mgrid[0:rows, 0:cols]
        if order == "rows":
            rank = ys * cols + xs
        elif order == "columns":
            rank = xs * rows + ys
        elif order == "random":
            rank = np.random.default_rng(3).permutation(rows * cols).reshape(rows, cols)
        else:
            rank = xs + ys
        rank = rank / max(1, rank.max())
        shown = np.clip((state.raw_in * 1.6 - rank * 0.6) / 1.0, 0, 1)  # each cell fades in on its turn
    colours = _mapped(values, stops)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    base = painter.opacity()
    for r in range(rows):
        for c in range(cols):
            if shown[r, c] <= 0:
                continue
            red, green, blue, alpha = colours[r, c]
            painter.setOpacity(base * shown[r, c])
            painter.setBrush(QColor(int(red), int(green), int(blue), int(alpha)))
            cell = QRectF(box.left() + c * (cw + gap), box.top() + r * (ch + gap), cw, ch)
            if radius:
                painter.drawRoundedRect(cell, radius, radius)
            else:
                painter.drawRect(cell)
    painter.restore()
    legend = layer.get("legend")
    if legend:
        spec = legend if isinstance(legend, dict) else {}
        bar = QRectF(box.right() + 0.015 * ctx.width, box.top(), 0.014 * ctx.width, box.height())
        gradient = QLinearGradient(bar.bottomLeft(), bar.topLeft())
        for k, c in enumerate(stops):
            gradient.setColorAt(k / max(1, len(stops) - 1), c)
        painter.fillRect(bar, gradient)
        unit = str(spec.get("unit", layer.get("unit", "")))
        top = spec.get("max", layer.get("max", ""))
        bottom = spec.get("min", layer.get("min", ""))
        for text, y in ((f"{top}{unit}" if top != "" else "", bar.top()), (f"{bottom}{unit}" if bottom != "" else "", bar.bottom())):
            if text:
                label = {"text": text, "role": "caption", "x": (bar.right() + 0.01 * ctx.width) / ctx.width,
                         "y": y / ctx.height, "align": "left"}  # fmt: skip
                _draw_words(painter, text, label, ctx, State(0, "none", 1, 1, 0, "none", 0), "caption")
        if spec.get("label"):
            label = {"x": (bar.center().x()) / ctx.width, "y": (bar.top() - 0.03 * ctx.height) / ctx.height}
            _draw_words(painter, str(spec["label"]), label, ctx, State(0, "none", 1, 1, 0, "none", 0), "caption")


# ---- curves and bars ----------------------------------------------------------------------------------


def _curve_values(layer: dict) -> tuple[np.ndarray, np.ndarray]:
    if layer.get("points"):
        data = np.array(layer["points"], float)
        return data[:, 0], data[:, 1]
    spec = layer.get("function") or {"kind": "normal"}
    kind = spec.get("kind", "normal")
    mean, sd = float(spec.get("mean", 0)), float(spec.get("sd", 1))
    defaults = {"normal": [mean - 4 * sd, mean + 4 * sd], "lognormal": [0.001, math.exp(float(spec.get("mu", 0)) + 3 * float(spec.get("sigma", 0.5)))],
                "exponential": [0, 5 / float(spec.get("rate", 1))]}  # fmt: skip
    x0, x1 = layer.get("x_range") or defaults.get(kind, [0, 1])
    x = np.linspace(float(x0), float(x1), int(layer.get("samples", CURVE_SAMPLES)))
    if kind == "normal":
        y = np.exp(-((x - mean) ** 2) / (2 * sd * sd)) / (sd * math.sqrt(2 * math.pi))
    elif kind == "lognormal":
        mu, sigma = float(spec.get("mu", 0)), float(spec.get("sigma", 0.5))
        safe = np.maximum(x, 1e-9)
        y = np.exp(-((np.log(safe) - mu) ** 2) / (2 * sigma * sigma)) / (safe * sigma * math.sqrt(2 * math.pi))
    elif kind == "exponential":
        rate = float(spec.get("rate", 1))
        y = rate * np.exp(-rate * np.maximum(x, 0))
    elif kind == "uniform":
        a, b = float(spec.get("a", x0)), float(spec.get("b", x1))
        y = np.where((x >= a) & (x <= b), 1 / max(1e-9, b - a), 0.0)
    elif kind == "sine":
        y = float(spec.get("amplitude", 1)) * np.sin(2 * math.pi * float(spec.get("frequency", 1)) * x + float(spec.get("phase", 0)))
    elif kind == "linear":
        y = float(spec.get("slope", 1)) * x + float(spec.get("intercept", 0))
    elif kind == "logistic":
        y = 1 / (1 + np.exp(-float(spec.get("k", 10)) * (x - float(spec.get("x0", 0.5)))))
    elif kind == "power":
        y = np.sign(x) * np.abs(x) ** float(spec.get("k", 2))
    else:
        raise ValueError(f"알 수 없는 곡선: {kind} (고를 수 있는 것: {', '.join(CURVE_KINDS)})")
    return x, y


def _axes(painter: QPainter, box: QRectF, layer: dict, ctx: Ctx, x_range, y_range) -> None:
    pen = _pen(ctx, "muted", 2)
    painter.setPen(pen)
    painter.drawLine(box.bottomLeft(), box.bottomRight())
    painter.drawLine(box.bottomLeft(), box.topLeft())
    ticks = int(layer.get("ticks", 0))
    idle = State(0, "none", 1, 1, 0, "none", 0)
    if ticks > 1:
        for k in range(ticks):
            share = k / (ticks - 1)
            value = x_range[0] + (x_range[1] - x_range[0]) * share
            x = box.left() + box.width() * share
            painter.drawLine(QPointF(x, box.bottom()), QPointF(x, box.bottom() + 8 * ctx.u))
            label = {"x": x / ctx.width, "y": (box.bottom() + 28 * ctx.u) / ctx.height, "role": "caption"}
            _draw_words(painter, f"{value:g}", label, ctx, idle, "caption")
    for key, spot in (("x_label", (box.center().x(), box.bottom() + 70 * ctx.u)), ("y_label", (box.left(), box.top() - 30 * ctx.u))):
        if layer.get(key):
            label = {"x": spot[0] / ctx.width, "y": spot[1] / ctx.height, "role": "caption",
                     "align": "center" if key == "x_label" else "left"}  # fmt: skip
            _draw_words(painter, str(layer[key]), label, ctx, idle, "caption")


def _draw_curve(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    xs, ys = _curve_values(layer)
    box = _box(layer, ctx, 0.6, 0.45)
    x0, x1 = layer.get("x_range") or [float(xs.min()), float(xs.max())]
    if layer.get("y_range"):
        y0, y1 = layer["y_range"]
    else:
        y0 = min(0.0, float(ys.min()))
        y1 = float(ys.max()) * 1.1 if ys.max() > 0 else 1.0
    x0, x1, y0, y1 = float(x0), float(x1), float(y0), float(y1)

    def to_screen(x, y):
        return QPointF(box.left() + (x - x0) / (x1 - x0) * box.width(), box.bottom() - (y - y0) / (y1 - y0) * box.height())

    points = [to_screen(x, y) for x, y in zip(xs, ys)]
    path = _polyline(points)
    p = state.p_in if state.enter == "draw" else 1.0
    if layer.get("axes"):
        _axes(painter, box, layer, ctx, (x0, x1), (y0, y1))
    base_y = box.bottom() - (0 - y0) / (y1 - y0) * box.height() if y0 <= 0 <= y1 else box.bottom()
    reveal = QRectF(box.left() - 2, box.top() - box.height(), box.width() * p + 2, box.height() * 3)
    color = layer.get("color", "accent")
    fill = layer.get("fill")
    if fill:
        area = _polyline([QPointF(points[0].x(), base_y), *points, QPointF(points[-1].x(), base_y)], closed=True)
        painter.save()
        painter.setClipRect(reveal, Qt.ClipOperation.IntersectClip)
        if fill is True:
            fill = f"{color}/0.22" if isinstance(color, str) else "accent/0.22"
        painter.fillPath(area, _brush(fill, ctx, box))
        painter.restore()
    shade = layer.get("shade")
    if shade:
        a, b = float(shade[0]), float(shade[1])
        inside = [pt for x, pt in zip(xs, points) if a <= x <= b]
        if inside:
            area = _polyline([QPointF(inside[0].x(), base_y), *inside, QPointF(inside[-1].x(), base_y)], closed=True)
            painter.save()
            painter.setClipRect(reveal, Qt.ClipOperation.IntersectClip)
            painter.fillPath(area, ctx.colour(layer.get("shade_color", "accent2/0.45")))
            painter.restore()
    painter.strokePath(partial(path, p), _pen(ctx, color, float(layer.get("width", 5))))
    marker = layer.get("marker")
    if marker and p >= 1 and marker.get("x") is not None:
        mx = float(marker.get("x", (x0 + x1) / 2))
        my = float(np.interp(mx, xs, ys))
        top = to_screen(mx, my)
        mcolor = marker.get("color", "accent2")
        painter.save()
        painter.setPen(_pen(ctx, mcolor, 3, [10, 8]))
        painter.drawLine(QPointF(top.x(), base_y), top)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ctx.colour(mcolor))
        painter.drawEllipse(top, 9 * ctx.u, 9 * ctx.u)
        painter.restore()
        if marker.get("label"):
            label = {"x": top.x() / ctx.width, "y": (top.y() - 40 * ctx.u) / ctx.height, "role": "label", "color": mcolor}
            _draw_words(painter, str(marker["label"]), label, ctx, State(0, "none", 1, 1, 0, "none", 0), "label")


def _draw_bars(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    values = [float(v) for v in layer.get("values") or []]
    if not values:
        return
    box = _box(layer, ctx, 0.6, 0.45)
    top = float(layer.get("max", max(max(values), 1e-9) * 1.1))
    n = len(values)
    gap_share = float(layer.get("gap", 0.35))
    slot = box.width() / n
    bar_w = slot * (1 - gap_share)
    colours = layer.get("colors")
    if layer.get("colormap"):
        stops = _colormap(layer["colormap"], ctx.st)
        mapped = _mapped(np.array([v / top for v in values]), stops)
        fills = [QColor(*(int(c) for c in rgba)) for rgba in mapped]
    elif isinstance(colours, list):
        fills = [ctx.colour(colours[k % len(colours)]) for k in range(n)]
    else:
        fills = [ctx.colour(colours or "accent")] * n
    if layer.get("axes", True):
        _axes(painter, box, {}, ctx, (0, 1), (0, top))
    stagger = float(ctx.st.get("motion", {}).get("stagger", 0.12))
    labels = layer.get("labels") or []
    idle = State(0, "none", 1, 1, 0, "none", 0)
    decimals = int(layer.get("decimals", 0))
    for k, value in enumerate(values):
        grow = 1.0
        if state.enter == "sweep" and state.enter_seconds > 0:
            grow = ease(ctx.st.get("motion", {}).get("ease", "out_cubic"), (state.t - k * stagger) / state.enter_seconds)
        h = box.height() * min(1.0, value / top) * grow
        x = box.left() + slot * k + (slot - bar_w) / 2
        rect = QRectF(x, box.bottom() - h, bar_w, h)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fills[k])
        radius = min(10 * ctx.u, bar_w / 3)
        painter.drawRoundedRect(rect, radius, radius)
        painter.restore()
        if k < len(labels):
            label = {"x": (x + bar_w / 2) / ctx.width, "y": (box.bottom() + 30 * ctx.u) / ctx.height, "role": "caption"}
            _draw_words(painter, str(labels[k]), label, ctx, idle, "caption")
        if layer.get("value_labels", True) and grow > 0.05:
            text = f"{value * grow:,.{decimals}f}{layer.get('suffix', '')}"
            label = {"x": (x + bar_w / 2) / ctx.width, "y": (rect.top() - 26 * ctx.u) / ctx.height, "role": "label"}
            _draw_words(painter, text, label, ctx, idle, "label")


# ---- pictures that change: a run of numbered pictures, a video ---------------------------------------------


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _media_rect(layer: dict, ctx: Ctx, source_w: int, source_h: int) -> QRectF:
    """Where a picture or video goes: filling the frame ("fit": "cover" / "contain") or centred on x, y at width w."""
    fit = layer.get("fit")
    if fit in ("cover", "contain"):
        scale = (max if fit == "cover" else min)(ctx.width / max(1, source_w), ctx.height / max(1, source_h))
        w, h = source_w * scale, source_h * scale
        return QRectF((ctx.width - w) / 2, (ctx.height - h) / 2, w, h)
    w = ctx.x(layer.get("w", 0.3))
    h = w * source_h / max(1, source_w)
    return QRectF(ctx.x(layer.get("x", 0.5)) - w / 2, ctx.y(layer.get("y", 0.5)) - h / 2, w, h)


def _natural(name: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


@lru_cache(maxsize=16)
def _sequence_files(where: str, by_pattern: bool, stamp: int) -> tuple[str, ...]:
    if by_pattern:
        found = [Path(p) for p in glob.glob(where)]
    else:
        folder = Path(where)
        found = [p for p in folder.iterdir() if p.suffix.lower() in SEQUENCE_SUFFIXES] if folder.is_dir() else []
    return tuple(str(p) for p in sorted(found, key=lambda p: _natural(p.name)) if p.is_file())


def sequence(layer: dict) -> tuple[str, ...]:
    """The pictures of a "frames" layer, in number order."""
    if layer.get("folder"):
        folder = str(layer["folder"])
        return _sequence_files(folder, False, _mtime(folder))
    pattern = str(layer.get("pattern", ""))
    return _sequence_files(pattern, True, _mtime(str(Path(pattern).parent)))


@lru_cache(maxsize=6)
def _picture(path: str, mtime: int) -> QImage:
    image = QImage(path)
    if image.isNull():
        raise ValueError(f"그림을 열 수 없습니다: {path}")
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _draw_frames(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    pictures = sequence(layer)
    if not pictures:
        raise ValueError(f"그림 묶음에 그림이 없습니다: {layer.get('folder') or layer.get('pattern')}")
    fps = max(0.1, float(layer.get("fps", 30)))
    k = int(math.floor((state.t + float(layer.get("offset", 0))) * fps + 1e-6))
    k = k % len(pictures) if layer.get("loop") else min(max(0, k), len(pictures) - 1)
    image = _picture(pictures[k], _mtime(pictures[k]))
    painter.drawImage(_media_rect(layer, ctx, image.width(), image.height()), image)


_local = threading.local()  # each thread making frames keeps its own video readers


@contextmanager
def playing():
    """Frames are asked for one after another (a render): videos are read as a stream, not a frame at a time."""
    _local.playing = getattr(_local, "playing", 0) + 1
    try:
        yield
    finally:
        _local.playing -= 1
        if not _local.playing:
            close_readers()


def close_readers() -> None:
    for reader in getattr(_local, "readers", {}).values():
        reader.close()
    _local.readers = {}


class _Reader:
    """FFmpeg decoding a video into RGBA frames at its own frame rate, read one after another."""

    def __init__(self, args: list[str], width: int, height: int, start: float, fps: float):
        from .media import NO_WINDOW

        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW)  # fmt: skip
        self.size, self.start, self.fps = (width, height), start, fps
        self.index, self.image = -1, None

    def read_to(self, k: int) -> QImage | None:
        w, h = self.size
        need = w * h * 4
        while self.index < k:
            data = self.proc.stdout.read(need)
            if len(data) < need:  # the end: the last frame stays
                break
            self.image = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            self.index += 1
        return self.image

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


@lru_cache(maxsize=16)
def _probe(path: str, mtime: int):
    from . import media

    return media.probe(path)


def _video_args(path: str, info, at: float, w: int, h: int, layer: dict, one: bool) -> list[str]:
    from .canvas import CHROMA_BLEND, chroma_similarity

    filters = []
    if layer.get("chroma"):
        c = colour(layer["chroma"], {})
        filters.append(f"colorkey=0x{c.red():02x}{c.green():02x}{c.blue():02x}:"
                       f"{chroma_similarity(float(layer.get('chroma_strength', 0.25))):.4f}:{CHROMA_BLEND}")  # fmt: skip
    filters += [f"scale={w}:{h}:flags=bicubic", "format=rgba"]
    # FFmpeg's own decoders drop a WebM's see-through layer; libvpx keeps it.
    decoder = {"vp9": ["-c:v", "libvpx-vp9"], "vp8": ["-c:v", "libvpx"]}.get(info.video_codec, [])
    args = [paths.tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{at:.3f}", *decoder, "-i", path]
    if one:
        args += ["-frames:v", "1"]
    return [*args, "-vf", ",".join(filters), "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1"]


@lru_cache(maxsize=8)
def _video_still(path: str, mtime: int, at: float, w: int, h: int, chroma: str, strength: float) -> QImage | None:
    from .media import NO_WINDOW

    info = _probe(path, mtime)
    layer = {"chroma": chroma, "chroma_strength": strength}
    proc = subprocess.run(_video_args(path, info, at, w, h, layer, True), capture_output=True, creationflags=NO_WINDOW)
    if len(proc.stdout) < w * h * 4:
        return None
    return QImage(proc.stdout[: w * h * 4], w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def _draw_video(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    path = str(layer.get("file", ""))
    info = _probe(path, _mtime(path))
    if not info.has_video:
        raise ValueError(f"영상이 들어 있지 않은 파일입니다: {path}")
    fps = info.fps or 30.0
    length = max(1 / fps, info.video_duration)
    at = float(layer.get("offset", 0)) + state.t * float(layer.get("speed", 1.0))
    if layer.get("loop"):
        at %= length
    at = min(max(0.0, at), max(0.0, length - 1 / fps))
    rect = _media_rect(layer, ctx, info.width, info.height)
    w, h = max(2, round(rect.width())), max(2, round(rect.height()))
    chroma, strength = str(layer.get("chroma", "")), float(layer.get("chroma_strength", 0.25))
    image = None
    if getattr(_local, "playing", 0):
        readers = _local.__dict__.setdefault("readers", {})
        key = (path, w, h, chroma, strength)
        reader = readers.get(key)
        # FFmpeg starts at the first frame at or after the moment asked for, as a single frame taken there.
        k = math.ceil((at - reader.start) * fps - 1e-6) if reader else -1
        if reader is None or k < reader.index or k > reader.index + 4 * fps:  # a jump (a loop starting over): read anew
            if reader is not None:
                reader.close()
            first = math.ceil(at * fps - 1e-6) / fps
            reader = readers[key] = _Reader(_video_args(path, info, at, w, h, layer, False), w, h, first, fps)
            k = math.ceil((at - first) * fps - 1e-6)
        image = reader.read_to(k)
    else:
        image = _video_still(path, _mtime(path), round(at, 3), w, h, chroma, strength)
    if image is None:
        raise ValueError(f"영상에서 화면을 읽지 못했습니다: {path}")
    painter.drawImage(rect, image)


# ---- plugins ------------------------------------------------------------------------------------------


def plugins_dir() -> Path:
    return paths.DATA_DIR / "플러그인"


@lru_cache(maxsize=32)
def _plugin_module(path: str, mtime: int):
    # Compiled from the source each time it changes: Python's .pyc cache only notices changes a second apart.
    spec = importlib.util.spec_from_file_location(f"movie_plugin_{hashlib.sha1(path.encode()).hexdigest()[:8]}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        source = Path(path).read_text(encoding="utf-8-sig")
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102 - the user's own plugin file
    except Exception as e:  # noqa: BLE001 - the user's code: told in words
        raise ValueError(f"플러그인을 불러오지 못했습니다: {Path(path).name}\n{type(e).__name__}: {e}") from e
    if not callable(getattr(module, "draw", None)):
        raise ValueError(f"플러그인에 draw(painter, api, layer) 함수가 없습니다: {Path(path).name}")
    return module


def plugin_names() -> list[str]:
    folder = plugins_dir()
    user = [p.stem for p in sorted(folder.glob("*.py"))] if folder.is_dir() else []
    return list(dict.fromkeys([*BUILTIN_PLUGINS, *user]))


def get_plugin(name: str) -> dict:
    """{"name", "doc", "keys", "draw", "bounds", "file"}: a user's plugin file wins over a built-in one."""
    file = plugins_dir() / f"{name}.py"
    if name and file.is_file():
        module = _plugin_module(str(file), _mtime(str(file)))
        keys = getattr(module, "KEYS", None)
        return {"name": name, "doc": (module.__doc__ or "").strip(), "keys": dict(keys) if isinstance(keys, dict) else None,
                "draw": module.draw, "bounds": getattr(module, "bounds", None), "file": str(file)}  # fmt: skip
    if name in BUILTIN_PLUGINS:
        doc, keys, draw_fn = BUILTIN_PLUGINS[name]
        return {"name": name, "doc": doc, "keys": dict(keys), "draw": draw_fn, "bounds": None, "file": ""}
    raise ValueError(f"플러그인 '{name}'이(가) 없습니다. 있는 플러그인: {', '.join(plugin_names())}")


class Api:
    """What a plugin draws with: the frame, the style and the time. Sizes are like the layers' own."""

    def __init__(self, ctx: Ctx, state: State):
        self.ctx, self.state = ctx, state
        self.width, self.height, self.u = ctx.width, ctx.height, ctx.u
        self.style = ctx.st
        self.t = state.t  # seconds since the layer came on
        self.env = state.env  # t (scene time), lt, d, p, i, n, col, row
        self.enter, self.p_in, self.p_out = state.enter, state.p_in, state.p_out

    def x(self, share) -> float:
        return self.ctx.x(share)

    def y(self, share) -> float:
        return self.ctx.y(share)

    def px(self, value) -> float:
        """Pixels of a 1080 frame, in this frame's pixels."""
        return float(value) * self.u

    def point(self, xy) -> QPointF:
        return self.ctx.point(xy)

    def colour(self, value) -> QColor:
        return self.ctx.colour(value)

    color = colour

    def brush(self, value, rect: QRectF | None = None) -> QBrush:
        return _brush(value, self.ctx, rect)

    def pen(self, value, width=4, dash=None) -> QPen:
        return _pen(self.ctx, value, float(width), dash)

    def font(self, role: str = "body", size=None, bold=None) -> QFont:
        layer = {k: v for k, v in (("size", size), ("bold", bold)) if v is not None}
        return _qfont(*_font_spec(layer, {}, self.ctx, role)[0])

    def text(self, painter: QPainter, text: str, x, y, role: str = "body", align: str = "center", color=None, size=None) -> QRectF:
        layer = {"x": x, "y": y, "role": role, "align": align, **({"color": color} if color else {}), **({"size": size} if size else {})}
        _draw_words(painter, str(text), layer, self.ctx, IDLE, role)
        return _text_layout(str(text), layer, self.ctx, role).rect

    def ease(self, name: str, p: float) -> float:
        return ease(name, p)

    def rand(self, k, seed=0.0) -> float:
        return _rand(k, seed)

    def noise(self, x, seed=0.0) -> float:
        return _noise(x, seed)

    def path(self, d: str) -> QPainterPath:
        return svg_path(d)

    def partial(self, path: QPainterPath, fraction: float) -> QPainterPath:
        return partial(path, fraction)


def _draw_plugin(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    plugin = get_plugin(str(layer.get("plugin", "")))
    try:
        plugin["draw"](painter, Api(ctx, state), dict(layer))
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - the user's code: told in words
        raise ValueError(f"플러그인 '{plugin['name']}' 오류: {type(e).__name__}: {e}") from e


# ---- groups -------------------------------------------------------------------------------------------


def _clip_path(spec: dict, ctx: Ctx) -> QPainterPath:
    """The shape a group is cut to: a rect, ellipse, circle, polygon, SVG path or words; "invert" keeps the outside."""
    kind = spec.get("type", "rect")
    if kind in ("rect", "ellipse", "circle", "polygon"):
        path = _shape_path({**spec, "type": kind}, ctx)
    elif kind == "path":
        path = _fitted_path(str(spec.get("d", "")), spec, ctx)
    elif kind == "text":
        lay = _text_layout(str(spec.get("text", "")), spec, ctx, spec.get("role", "title"))
        path = QPainterPath()
        for line, top in zip(lay.lines, lay.tops):
            left = lay.rect.left() + (lay.rect.width() - line.width) / 2
            for g in line.glyphs:
                if not g.char.isspace():
                    path.addPath(_glyph(g.char, *g.font).translated(left + g.x, top + line.ascent))
    else:
        raise ValueError(f"clip.type은 {', '.join(CLIP_SHAPES)} 중 하나입니다 (받은 값: {kind!r})")
    if spec.get("invert"):
        whole = QPainterPath()
        whole.addRect(QRectF(0, 0, ctx.width, ctx.height))
        path = whole.subtracted(path)
    return path


def _draw_group(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    """Its layers drawn together (moved, cut and faded as one), `count` times with i = 0, 1, ...
    Times inside count from the group's start."""
    start = float(layer.get("start", 0.0))
    end = float(layer.get("end", ctx.duration))
    inner = Ctx(ctx.st, ctx.width, ctx.height, max(0.0, end - start))
    painter.translate(ctx.x(layer.get("dx", 0)), ctx.y(layer.get("dy", 0)))
    if isinstance(layer.get("clip"), dict):
        painter.setClipPath(_clip_path(layer["clip"], ctx), Qt.ClipOperation.IntersectClip)
    count = max(0, min(MAX_COPIES, int(layer.get("count", 1))))
    cols = max(0, int(layer.get("cols", 0)))
    for i in range(count):
        env = {**state.env, "i": float(i), "n": float(count), "col": float(i % cols if cols else i), "row": float(i // cols if cols else 0)}
        _draw_layers(painter, layer.get("layers") or [], inner, state.t, env)


PAINTERS = {
    "rect": _draw_shape,
    "ellipse": _draw_shape,
    "circle": _draw_shape,
    "polygon": _draw_shape,
    "line": _draw_line,
    "arrow": _draw_line,
    "path": _draw_path,
    "text": _draw_text,
    "counter": _draw_counter,
    "image": _draw_image,
    "prop": _draw_prop,
    "tiles": _draw_tiles,
    "curve": _draw_curve,
    "bars": _draw_bars,
    "frames": _draw_frames,
    "video": _draw_video,
    "plugin": _draw_plugin,
    "group": _draw_group,
    "repeat": _draw_group,
}


def bounds(layer: dict, ctx: Ctx) -> QRectF:
    """Roughly where a layer is drawn (for wiping it on and turning it about its middle)."""
    kind = layer.get("type")
    whole = QRectF(0, 0, ctx.width, ctx.height)
    if kind in ("line", "arrow"):
        return _line_path(layer, ctx).boundingRect()
    if kind == "polygon":
        return _polyline(_points(layer, ctx)).boundingRect()
    if kind == "circle":
        r = float(layer.get("r", 0.1)) * min(ctx.width, ctx.height)
        return QRectF(ctx.x(layer.get("x", 0.5)) - r, ctx.y(layer.get("y", 0.5)) - r, 2 * r, 2 * r)
    if kind in ("text", "counter"):
        text = layer.get("text", "") if kind == "text" else _counter_text(layer, IDLE)
        return _text_layout(str(text), layer, ctx, "body" if kind == "text" else "number").rect
    if kind in ("path", "prop"):
        w = ctx.x(layer.get("w", 0.1))
        h = ctx.y(layer["h"]) if "h" in layer else w
        return QRectF(ctx.x(layer.get("x", 0.5)) - w / 2, ctx.y(layer.get("y", 0.5)) - h / 2, w, h)
    if kind == "image":
        w = ctx.x(layer.get("w", 0.3))
        return QRectF(ctx.x(layer.get("x", 0.5)) - w / 2, ctx.y(layer.get("y", 0.5)) - w / 2, w, w)
    if kind in ("frames", "video") and layer.get("fit") in ("cover", "contain"):
        return whole
    if kind in GROUPS:
        return whole
    if kind == "plugin":
        try:
            plugin = get_plugin(str(layer.get("plugin", "")))
        except ValueError:
            return whole
        if plugin["bounds"] is not None:
            x, y, w, h = plugin["bounds"](Api(ctx, IDLE), layer)
            return QRectF(ctx.x(x) - ctx.x(w) / 2, ctx.y(y) - ctx.y(h) / 2, ctx.x(w), ctx.y(h))
        return _box(layer, ctx, 0.3, 0.3) if "w" in layer and "h" in layer else whole
    return _box(layer, ctx, 0.5, 0.4)


def _centre(layer: dict, ctx: Ctx) -> QPointF:
    if layer.get("type") in GROUPS:
        return ctx.point(layer.get("origin") or [0.5, 0.5])
    return bounds(layer, ctx).center()


def _paint(painter: QPainter, layer: dict, ctx: Ctx, state: State) -> None:
    """The layer with its entrance and exit, its turn, size, see-through and blending applied."""
    painter.save()
    opacity = float(layer.get("opacity", 1.0))
    scale = float(layer.get("scale", 1.0))
    rotation = float(layer.get("rotation", 0.0))
    distance = float(ctx.st.get("motion", {}).get("slide", 40)) * ctx.u
    shift = QPointF()
    for effect, p, arriving in ((state.enter, state.p_in, True), (state.leave, state.p_out, False)):
        away = 1 - p if arriving else p  # how far from its place
        if effect == "fade":
            opacity *= p if arriving else 1 - p
        elif effect in ("grow", "shrink"):
            scale *= p if arriving else 1 - p
        elif effect == "pop" and arriving:
            scale *= ease("out_back", state.raw_in)
            opacity *= min(1.0, state.raw_in * 3)
        elif effect.startswith("slide_"):
            direction = {"slide_up": (0, 1), "slide_down": (0, -1), "slide_left": (1, 0), "slide_right": (-1, 0)}[effect]
            sign = 1 if arriving else -1  # arriving from the far side, leaving towards the way it points
            shift += QPointF(direction[0], direction[1]) * distance * away * sign
            opacity *= p if arriving else 1 - p
        elif effect == "wipe":
            box = bounds(layer, ctx).adjusted(-4, -4, 4, 4)
            if arriving:
                box.setWidth(box.width() * p)
            else:
                box.setLeft(box.left() + box.width() * p)
            painter.setClipRect(box, Qt.ClipOperation.IntersectClip)
    if opacity <= 0.001 or scale <= 0.001:
        painter.restore()
        return
    painter.setOpacity(painter.opacity() * min(1.0, opacity))
    if layer.get("blend") in BLENDS:
        painter.setCompositionMode(BLENDS[layer["blend"]])
    painter.translate(shift)
    if scale != 1.0 or rotation:
        centre = _centre(layer, ctx)
        painter.translate(centre)
        painter.rotate(rotation)
        painter.scale(scale, scale)
        painter.translate(-centre)
    PAINTERS[layer["type"]](painter, layer, ctx, state)
    painter.restore()


def _halo(painter: QPainter, layer: dict, ctx: Ctx, state: State, kind: str) -> None:
    """A soft shadow under the layer, or a glow round it: its shape in one colour, blurred."""
    spec = dict(ctx.st.get(kind, {}))
    if isinstance(layer.get(kind), dict):
        spec.update(layer[kind])
    if kind == "glow":
        own = next((layer[k] for k in ("color", "stroke", "fill") if isinstance(layer.get(k), str)), "accent")
        tint, blur, offset = spec.get("color", own), float(spec.get("blur", 26)), spec.get("offset", [0, 0])
    else:
        tint, blur, offset = spec.get("color", "#000000/0.5"), float(spec.get("blur", 24)), spec.get("offset", [0, 10])
    small = QImage(max(1, round(ctx.width * SHADOW_SCALE)), max(1, round(ctx.height * SHADOW_SCALE)),
                   QImage.Format.Format_ARGB32_Premultiplied)  # fmt: skip
    small.fill(Qt.GlobalColor.transparent)
    inner = QPainter(small)
    try:
        inner.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner.scale(SHADOW_SCALE, SHADOW_SCALE)
        _paint(inner, {k: v for k, v in layer.items() if k != "blend"}, ctx, state)
        inner.resetTransform()
        inner.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        inner.fillRect(small.rect(), ctx.colour(tint))
    finally:
        inner.end()
    steps = max(1.0, blur * ctx.u * SHADOW_SCALE)
    w, h = small.width(), small.height()
    soft = small.scaled(max(1, round(w / steps)), max(1, round(h / steps)), Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)  # fmt: skip
    soft = soft.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    dx, dy = (float(v) * ctx.u for v in offset)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if kind == "glow":
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)  # light adds up
        strength = max(0.0, float(spec.get("strength", 1.0)))
        while strength > 0:
            painter.setOpacity(min(1.0, strength))
            painter.drawImage(QRectF(dx, dy, ctx.width, ctx.height), soft)
            strength -= 1.0
    else:
        painter.drawImage(QRectF(dx, dy, ctx.width, ctx.height), soft)
    painter.restore()


def _draw_layers(painter: QPainter, layers: list, ctx: Ctx, t: float, env: dict) -> None:
    for raw in layers:
        if not isinstance(raw, dict):
            continue
        timed = _timed(raw, t, ctx, env)
        if timed is None:
            continue
        layer, state = timed
        if layer.get("type") == "background":
            painter.save()
            opacity = float(layer.get("opacity", 1.0)) * (state.p_in if state.enter == "fade" else 1.0) * (1 - state.p_out)
            painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
            _draw_background(painter, layer, ctx)
            painter.restore()
            continue
        if layer.get("shadow"):
            _halo(painter, layer, ctx, state, "shadow")
        if layer.get("glow"):
            _halo(painter, layer, ctx, state, "glow")
        _paint(painter, layer, ctx, state)


def draw(painter: QPainter, layers: list[dict], style_name: str, width: int, height: int, t: float, duration: float) -> None:
    """Draw a graphic scene `t` seconds in onto `painter` (a frame of width × height)."""
    st = style(style_name)
    ctx = Ctx(st, width, height, duration)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if not any(isinstance(layer, dict) and layer.get("type") == "background" for layer in layers):
        _draw_background(painter, {"type": "background"}, ctx)
    _draw_layers(painter, layers, ctx, t, {})
    painter.restore()


def frame(layers: list[dict], style_name: str, width: int, height: int, t: float, duration: float) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.black)
    painter = QPainter(image)
    try:
        draw(painter, layers, style_name, width, height, t, duration)
    finally:
        painter.end()
    return image


# ---- where layers land -----------------------------------------------------------------------------------

LAYOUT_WIDTH = 640  # each layer is drawn on its own this wide to see where its pixels go
LAYOUT_MARGIN = 0.25  # room round the frame, as a share of its sides, to catch what spills off it
LAYOUT_SLACK = 0.004  # off the frame by less than this share of it (about 8 px across 1920) is only anti-aliasing
LAYOUT_ALPHA = 40  # pixels fainter than this (of 255) are a smoothed edge, not the layer
LAYOUT_UNDER = 30  # a layer with this many pixels (at LAYOUT_WIDTH) where the subtitles go is under them
FILL_KINDS = ("image", "frames", "video")  # with a fit they fill the frame on purpose
KEEP_CLEAR = ("text", "counter", "tiles", "curve", "bars", "prop", "image", "frames", "video", "plugin", *GROUPS)
SIDE_WORDS = {"left": "왼쪽으로", "right": "오른쪽으로", "top": "위로", "bottom": "아래로"}


def _landing(layer: dict, state: State, ctx: Ctx, scale: float, clear) -> tuple[tuple, int] | None:
    """Where one layer puts its pixels: (left, top, right, bottom) as shares of the frame, beyond 0–1 when off it,
    and how many of them fall in `clear` (a box like that, where the subtitles go). None when it draws nothing."""
    mx, my = ctx.width * LAYOUT_MARGIN, ctx.height * LAYOUT_MARGIN
    image = QImage(max(1, round((ctx.width + 2 * mx) * scale)), max(1, round((ctx.height + 2 * my) * scale)),
                   QImage.Format.Format_ARGB32_Premultiplied)  # fmt: skip
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(scale, scale)
        painter.translate(mx, my)
        _paint(painter, layer, ctx, state)
    finally:
        painter.end()
    pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
    solid = pixels[:, : image.width(), 3] >= LAYOUT_ALPHA
    rows, cols = np.flatnonzero(solid.any(axis=1)), np.flatnonzero(solid.any(axis=0))
    if not rows.size:
        return None

    def across(px: float) -> float:
        return (px / scale - mx) / ctx.width

    def down(px: float) -> float:
        return (px / scale - my) / ctx.height

    under = 0
    if clear:
        left, right = (round((share * ctx.width + mx) * scale) for share in (clear[0], clear[2]))
        top, bottom = (round((share * ctx.height + my) * scale) for share in (clear[1], clear[3]))
        under = int(solid[top:bottom, left:right].sum())
    return (across(cols[0]), down(rows[0]), across(cols[-1] + 1), down(rows[-1] + 1)), under


def _settled(layer: dict, state: State) -> bool:
    """Whether the layer has finished coming on and is not yet going (so it is where it means to be)."""
    if state.raw_in < 1 or state.p_out > 0:
        return False
    letters = len(str(layer.get("text", ""))) if state.enter in CHAR_EFFECTS else 0
    return state.t >= state.enter_seconds + state.stagger * max(0, letters - 1) - 1e-9


def _spill(box: tuple) -> dict:
    left, top, right, bottom = box
    sides = {"left": -left, "right": right - 1, "top": -top, "bottom": bottom - 1}
    return {side: amount for side, amount in sides.items() if amount > LAYOUT_SLACK}


def _named(layer: dict, index: int) -> str:
    kind = layer.get("type")
    words = layer.get("text") or layer.get("name") or layer.get("plugin") or ""
    if kind in GROUPS:
        words = f"안에 {len(layer.get('layers') or [])}개"
    words = " ".join(str(words).split())
    return f"layers[{index}] ({kind}" + (f' "{words[:24]}{"…" if len(words) > 24 else ""}"' if words else "") + ")"


def layout_problems(layers: list[dict], style_name: str, width: int, height: int, duration: float, times: list[float],
                    subtitles: tuple[float, float, float, float] | None = None) -> list[str]:  # fmt: skip
    """Layers that are partly off the frame, or under the subtitles (where they go: left, top, right, bottom as shares
    of the frame), at every one of `times` when they are on and settled — in words, with how far.

    A layer only off the frame while it slides or moves in is not one: it has to stay off once it has arrived."""
    try:
        st = style(style_name)
    except ValueError:
        return []
    ctx = Ctx(st, width, height, duration)
    scale = LAYOUT_WIDTH / max(width, height)
    problems = []
    for index, raw in enumerate(layers):
        kind = raw.get("type") if isinstance(raw, dict) else None
        if kind in (None, "background") or (kind in FILL_KINDS and raw.get("fit") in ("cover", "contain")):
            continue
        landed = []
        try:
            for t in times:
                timed = _timed(raw, t, ctx, {})
                if timed is not None and _settled(*timed):
                    spot = _landing(timed[0], timed[1], ctx, scale, subtitles if kind in KEEP_CLEAR else None)
                    if spot is not None:
                        landed.append(spot)
        except (ValueError, TypeError, KeyError, ZeroDivisionError, OverflowError):
            continue  # what is wrong with it is check_layers' to say
        if not landed:
            continue
        spills = [_spill(box) for box, _under in landed]
        if all(spills):
            worst = max(range(len(spills)), key=lambda k: max(spills[k].values()))
            left, top, right, bottom = landed[worst][0]
            # As far out as it is looked for: it may go further.
            sides = ", ".join(f"{SIDE_WORDS[side]} " + (f"{LAYOUT_MARGIN:.2f} 이상" if amount >= LAYOUT_MARGIN - 0.005 else f"{amount:.3f}")
                              for side, amount in spills[worst].items())  # fmt: skip
            problems.append(f"{_named(raw, index)}: 화면 밖으로 나갑니다 ({sides}, 화면 비율). "
                            f"그려지는 범위 x {left:.3f}–{right:.3f}, y {top:.3f}–{bottom:.3f}")  # fmt: skip
        if subtitles and all(under >= LAYOUT_UNDER for _box, under in landed):
            problems.append(f"{_named(raw, index)}: 자막 자리(세로 {subtitles[1]:.2f}–{subtitles[3]:.2f})와 겹칩니다. "
                            "위로 올리거나 줄이세요")  # fmt: skip
    return problems


# ---- about a scene's layers ------------------------------------------------------------------------------

_SAMPLE = {"t": 0.0, "lt": 0.0, "d": 1.0, "p": 0.0, "i": 0.0, "n": 1.0, "col": 0.0, "row": 0.0}


def _moves(layer: dict, duration: float) -> bool:
    if layer.get("keys") or layer.get("wave") or layer.get("type") in ("frames", "video", "plugin"):
        return True
    if _effect(layer.get("enter"), {}, False)[0] != "none" or _effect(layer.get("exit"), {}, True)[0] != "none":
        return True
    start, end = layer.get("start", 0), layer.get("end", duration)
    if isinstance(start, str) or isinstance(end, str) or float(start) > 0 or float(end) < duration:
        return True
    if any(_uses_time(v, k) for k, v in layer.items() if k not in ("layers", "keys")):
        return True
    if layer.get("type") in GROUPS:
        return any(_moves(child, duration) for child in layer.get("layers") or [] if isinstance(child, dict))
    return False


def is_static(layers: list[dict], duration: float) -> bool:
    """Whether every frame looks the same (nothing comes, goes or moves)."""
    return not any(_moves(layer, duration) for layer in layers if isinstance(layer, dict))


def _arrivals(layers: list, st: dict, duration: float, offset: float, arrived: list, leaving: list) -> None:
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        try:
            start = offset + _num(layer.get("start", 0), _SAMPLE)
            end = offset + _num(layer.get("end", duration), _SAMPLE)
            effect, seconds, _, stagger = _effect(layer.get("enter"), st, False, _SAMPLE)
        except ValueError:
            continue
        if effect in CHAR_EFFECTS:
            seconds += stagger * max(0, len(str(layer.get("text", ""))) - 1)
        arrived.append(start + seconds)
        if layer.get("exit"):
            leaving.append(end - _effect(layer.get("exit"), st, True, _SAMPLE)[1])
        if layer.get("type") in GROUPS:
            _arrivals(layer.get("layers") or [], st, end - start, start, arrived, leaving)


def poster_time(layers: list[dict], style_name: str, duration: float) -> float:
    """A moment showing the scene at its fullest: everything has come in and nothing has left yet."""
    try:
        st = style(style_name)
    except ValueError:
        st = style(DEFAULT)
    arrived: list[float] = []
    leaving: list[float] = []
    _arrivals(layers, st, duration, 0.0, arrived, leaving)
    t = max(arrived, default=0.0)
    if leaving:
        t = min(t, min(leaving) - 0.01)
    return min(max(0.0, t), duration)


def files(layers: list[dict], style_name: str) -> list[str]:
    """Files the scene's drawing reads (pictures, videos, picture runs, SVG props, plugins), for the render cache
    and missing-file checks."""
    try:
        props = style(style_name).get("props", {})
    except ValueError:
        props = {}
    out: list[str] = []

    def walk(items: list) -> None:
        for layer in items:
            if not isinstance(layer, dict):
                continue
            kind = layer.get("type")
            if kind in ("image", "video"):
                out.append(str(layer.get("file", "")))
            elif kind == "frames":
                found = sequence(layer)
                out.extend(found or [str(layer.get("folder") or layer.get("pattern", ""))])
            elif kind == "prop" and isinstance(props.get(layer.get("name")), str):
                out.append(str(prop_file(props[layer["name"]])))
            elif kind == "plugin" and (plugins_dir() / f"{layer.get('plugin')}.py").is_file():
                out.append(str(plugins_dir() / f"{layer['plugin']}.py"))
            elif kind in GROUPS:
                walk(layer.get("layers") or [])

    walk(layers)
    return out


# ---- checking -----------------------------------------------------------------------------------------


def _check_colour(value, st: dict, where: str, errors: list[str]) -> None:
    if value in (None, False):
        return
    try:
        if isinstance(value, dict):
            _gradient(value, Ctx(st, 100, 100, 1), QRectF(0, 0, 100, 100))
        else:
            colour(value, st)
    except (ValueError, TypeError) as e:
        errors.append(f"{where}: {e}")


def _number(value, where: str, errors: list[str]) -> None:
    if isinstance(value, str):
        try:
            formula(value)
        except ValueError as e:
            errors.append(f"{where}: {e}")
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where}: 숫자나 식이어야 합니다 ({value!r})")


def _check_formulas(value, where: str, errors: list[str], key: str | None = None) -> None:
    """Every formula among a layer's numbers can be read."""
    if key in WORD_KEYS:
        return
    if isinstance(value, str):
        try:
            formula(value)
        except ValueError as e:
            errors.append(f"{where}: {e}")
    elif isinstance(value, list):
        for k, v in enumerate(value):
            _check_formulas(v, f"{where}[{k}]", errors)
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_formulas(v, f"{where}.{k}", errors, k)


NUMBER_KEYS = ("x", "y", "w", "h", "r", "opacity", "rotation", "scale", "start", "end", "width", "radius", "size", "dx", "dy",
               "bend", "count", "cols", "fps", "offset", "speed", "chroma_strength")  # fmt: skip


def check_layers(layers, style_name: str) -> list[str]:
    """What is wrong with a scene's layers, in words (empty when nothing is)."""
    if not isinstance(layers, list):
        return ["layers는 목록이어야 합니다."]
    try:
        st = style(style_name)
    except ValueError as e:
        return [str(e)]
    errors: list[str] = []
    _check_list(layers, st, "layers", errors)
    return list(dict.fromkeys(errors))  # a formula may be reported by two checks


def _check_list(layers: list, st: dict, prefix: str, errors: list[str]) -> None:
    for i, layer in enumerate(layers):
        where = f"{prefix}[{i}]"
        if not isinstance(layer, dict):
            errors.append(f"{where}: dict여야 합니다.")
            continue
        kind = layer.get("type")
        if kind not in LAYER_KEYS:
            errors.append(f"{where}.type: {kind!r}는 없는 종류입니다. 고를 수 있는 것: {', '.join(LAYER_KEYS)}")
            continue
        allowed = LAYER_KEYS[kind] | COMMON_KEYS
        if kind == "plugin":
            try:
                plugin = get_plugin(str(layer.get("plugin", "")))
            except ValueError as e:
                errors.append(f"{where}.plugin: {e}")
                continue
            allowed = allowed | set(plugin["keys"] or layer)  # a plugin without KEYS takes any values
        unknown = sorted(set(layer) - allowed)
        if unknown:
            errors.append(f"{where}: {kind}에 없는 항목 {unknown}. 쓸 수 있는 항목: {sorted(allowed)}")
        for key in NUMBER_KEYS:
            if key in layer and not (kind == "plugin" and key not in LAYER_KEYS["plugin"] | COMMON_KEYS):
                _number(layer[key], f"{where}.{key}", errors)
        for key in COLOUR_KEYS & set(layer):
            if not (key == "fill" and kind == "curve" and layer[key] is True):
                _check_colour(layer[key], st, f"{where}.{key}", errors)
        for key, value in layer.items():
            if key not in ("layers", "keys"):
                _check_formulas(value, f"{where}.{key}", errors, key)
        for key, allowed_effects in (("enter", ENTER_EFFECTS), ("exit", EXIT_EFFECTS)):
            spec = layer.get(key)
            effect = spec.get("effect") if isinstance(spec, dict) else spec
            if effect not in (None, True, False) and effect not in allowed_effects:
                errors.append(f"{where}.{key}: {effect!r}는 없는 효과입니다. 고를 수 있는 것: {', '.join(allowed_effects)}")
            if isinstance(spec, dict):
                if spec.get("ease") and spec["ease"] not in EASES:
                    errors.append(f"{where}.{key}.ease: {spec['ease']!r}는 없는 시간 함수입니다 (예: {', '.join(EASES[:8])} …)")
                for part in ("duration", "stagger"):
                    if part in spec:
                        _number(spec[part], f"{where}.{key}.{part}", errors)
        if layer.get("ease") and layer["ease"] not in EASES:
            errors.append(f"{where}.ease: {layer['ease']!r}는 없는 시간 함수입니다 (예: {', '.join(EASES[:8])} …)")
        if layer.get("blend") and layer["blend"] not in BLENDS:
            errors.append(f"{where}.blend: {layer['blend']!r}는 없는 섞기입니다. 고를 수 있는 것: {', '.join(BLENDS)}")
        for part in ("shadow", "glow"):
            if isinstance(layer.get(part), dict) and "color" in layer[part]:
                _check_colour(layer[part]["color"], st, f"{where}.{part}.color", errors)
        for k, key in enumerate(layer.get("keys") or []):
            if not isinstance(key, dict) or "t" not in key:
                errors.append(f"{where}.keys[{k}]: {{\"t\": 초, ...}} 모양이어야 합니다.")
                continue
            if key.get("ease") and key["ease"] not in EASES:
                errors.append(f"{where}.keys[{k}].ease: {key['ease']!r}는 없는 시간 함수입니다.")
            for name, value in key.items():
                if name != "ease":
                    _check_formulas(value, f"{where}.keys[{k}].{name}", errors, name)
        try:
            sample = {k: (v if k in ("layers", "keys") else _resolve(v, _SAMPLE, k)) for k, v in layer.items()}
        except ValueError:
            continue  # said above
        try:
            _check_kind(kind, sample, st, where, errors)
        except (ValueError, TypeError, IndexError, KeyError) as e:
            errors.append(f"{where}: {e}")


def _check_kind(kind: str, layer: dict, st: dict, where: str, errors: list[str]) -> None:
    # Colours inside parts of a layer (a box, an outline, a marker, a grid, a list of bar colours).
    for part, key in (("box", "fill"), ("outline", "color"), ("marker", "color"), ("grid", "color")):
        if isinstance(layer.get(part), dict) and key in layer[part]:
            _check_colour(layer[part][key], st, f"{where}.{part}.{key}", errors)
    if kind in ("bars", "background") and layer.get("colors") is not None:
        values = layer["colors"] if isinstance(layer["colors"], list) else [layer["colors"]]
        for k, value in enumerate(values):
            _check_colour(value, st, f"{where}.colors[{k}]", errors)
    if kind == "background":
        spec = _background_spec(layer, st)
        if isinstance(spec.get("grid"), dict) and "color" in spec["grid"]:
            _check_colour(spec["grid"]["color"], st, f"{where}.grid.color", errors)
    elif kind in ("text", "counter"):
        role = "body" if kind == "text" else "number"
        for piece, fmt in _runs(layer, str(layer.get("text", ""))):
            _font_spec(layer, fmt, Ctx(st, 1920, 1080, 1), role)
            if "color" in fmt:
                _check_colour(fmt["color"], st, f"{where} 글자 표시 [{fmt['color']}]", errors)
        if layer.get("spans") is not None and not isinstance(layer["spans"], list):
            errors.append(f"{where}.spans: [{{\"text\", \"color\", \"bold\", \"size\"}}, ...] 목록이어야 합니다.")
    elif kind == "prop":
        props = st.get("props", {})
        if layer.get("name") not in props:
            errors.append(f"{where}.name: 스타일에 '{layer.get('name')}' 소품이 없습니다. 있는 소품: {', '.join(props)}")
    elif kind == "path":
        if not layer.get("d"):
            errors.append(f"{where}.d: SVG 경로 데이터가 필요합니다.")
        else:
            svg_path(str(layer["d"]))
    elif kind == "image":
        if not Path(str(layer.get("file", ""))).is_file():
            errors.append(f"{where}.file: 파일을 찾을 수 없습니다: {layer.get('file')}")
    elif kind == "tiles":
        if layer.get("colormap") is not None:
            _colormap(layer["colormap"], st)
        elif "analysis" not in st.get("colormaps", {}):
            errors.append(f"{where}.colormap: 색 지도를 정하세요.")
        if layer.get("field"):
            _field(json.dumps(layer["field"], sort_keys=True), 2, 2)
        if layer.get("values") is not None:
            np.array(layer["values"], float)
    elif kind == "curve":
        _curve_values(layer)
    elif kind == "bars":
        [float(v) for v in layer.get("values") or []]
        if layer.get("colormap"):
            _colormap(layer["colormap"], st)
    elif kind in ("line", "polygon") and len(layer.get("points") or []) < 2:
        errors.append(f"{where}.points: 점이 두 개 이상 있어야 합니다 ([[x, y], ...]).")
    elif kind in GROUPS:
        children = layer.get("layers")
        if not isinstance(children, list):
            errors.append(f"{where}.layers: 묶을 레이어 목록이 필요합니다.")
        else:
            _check_list(children, st, f"{where}.layers", errors)
        clip = layer.get("clip")
        if clip is not None:
            if not isinstance(clip, dict) or clip.get("type", "rect") not in CLIP_SHAPES:
                errors.append(f"{where}.clip: {{\"type\": {' | '.join(CLIP_SHAPES)}, ...}} 모양이어야 합니다.")
            else:
                _clip_path(clip, Ctx(st, 1920, 1080, 1))
    elif kind == "frames":
        if not (layer.get("folder") or layer.get("pattern")):
            errors.append(f"{where}: folder(그림 폴더)나 pattern(예: C:/작업/frame_*.png)이 필요합니다.")
        elif not sequence(layer):
            errors.append(f"{where}: 그림을 찾을 수 없습니다: {layer.get('folder') or layer.get('pattern')}")
    elif kind == "video":
        path = str(layer.get("file", ""))
        if not Path(path).is_file():
            errors.append(f"{where}.file: 파일을 찾을 수 없습니다: {path}")
        elif not _probe(path, _mtime(path)).has_video:
            errors.append(f"{where}.file: 영상이 들어 있지 않습니다: {path}")
    elif kind == "plugin":
        keys = get_plugin(str(layer.get("plugin", "")))["keys"]
        for key in keys or {}:
            if key in COLOUR_KEYS | {"track"} and key in layer:
                _check_colour(layer[key], st, f"{where}.{key}", errors)


def check_style(data: dict, name: str = "") -> list[str]:
    """What is wrong with a style definition, in words."""
    if not isinstance(data, dict):
        return ["스타일은 dict여야 합니다."]
    errors = []
    unknown = sorted(set(data) - STYLE_PARTS)
    if unknown:
        errors.append(f"알 수 없는 항목 {unknown}. 쓸 수 있는 항목: {sorted(STYLE_PARTS)}")
    base = data.get("base")
    if base is not None and base not in style_names():
        errors.append(f"base: 스타일 '{base}'이(가) 없습니다.")
    if base == name and name:
        errors.append("base: 자기 자신을 바탕으로 할 수 없습니다.")
    if errors:
        return errors
    try:
        merged = _merge(style(base or DEFAULT), data)
    except ValueError as e:
        return [str(e)]
    for key, value in merged.get("colors", {}).items():
        _check_colour(value, merged, f"colors.{key}", errors)
    for role, spec in merged.get("fonts", {}).items():
        if not isinstance(spec, dict):
            errors.append(f"fonts.{role}: {{family, size, bold, color}} 모양이어야 합니다.")
            continue
        _check_colour(spec.get("color", "ink"), merged, f"fonts.{role}.color", errors)
    for preset, spec in merged.get("backgrounds", {}).items():
        if not isinstance(spec, dict):
            errors.append(f"backgrounds.{preset}: dict여야 합니다.")
            continue
        unknown = sorted(set(spec) - BACKGROUND_KEYS)
        if unknown:
            errors.append(f"backgrounds.{preset}: 알 수 없는 항목 {unknown}")
        for c in spec.get("colors") or []:
            _check_colour(c, merged, f"backgrounds.{preset}.colors", errors)
        if "color" in spec:
            _check_colour(spec["color"], merged, f"backgrounds.{preset}.color", errors)
    for cmap, stops in merged.get("colormaps", {}).items():
        if not isinstance(stops, list) or len(stops) < 2:
            errors.append(f"colormaps.{cmap}: 색 두 개 이상의 목록이어야 합니다.")
            continue
        for c in stops:
            _check_colour(c, merged, f"colormaps.{cmap}", errors)
    for part in ("shape", "line", "arrow", "text_box", "shadow", "glow"):
        spec = merged.get(part, {})
        for key in ("fill", "stroke", "color"):
            if key in spec:
                _check_colour(spec[key], merged, f"{part}.{key}", errors)
    motion = merged.get("motion", {})
    if motion.get("ease") and motion["ease"] not in EASES:
        errors.append(f"motion.ease: {motion['ease']!r}는 없는 시간 함수입니다.")
    if motion.get("enter") and motion["enter"] not in ENTER_EFFECTS:
        errors.append(f"motion.enter: {motion['enter']!r}는 없는 효과입니다.")
    for prop, spec in merged.get("props", {}).items():
        try:
            if isinstance(spec, str):
                if not prop_file(spec).is_file():
                    errors.append(f"props.{prop}: SVG 파일을 찾을 수 없습니다: {prop_file(spec)}")
            elif isinstance(spec, dict) and spec.get("d"):
                svg_path(spec["d"])
            else:
                errors.append(f"props.{prop}: SVG 파일 경로나 {{\"d\": 경로 데이터, \"mode\": \"stroke\"|\"fill\"}}이어야 합니다.")
        except ValueError as e:
            errors.append(f"props.{prop}: {e}")
    return errors


# ---- a sheet showing a style --------------------------------------------------------------------------


def style_sheet(name: str, width: int = 1920, height: int = 1080) -> QImage:
    """Everything a style has on one picture: its colours, fonts, backgrounds, colour maps, props and parts."""
    st = style(name)
    text = {"type": "text", "role": "caption", "size": 20}
    layers: list[dict] = [{"type": "background", "preset": "default"},
                          {"type": "text", "text": f"스타일: {name}", "role": "heading", "x": 0.04, "y": 0.07, "align": "left"}]  # fmt: skip
    if st.get("description"):
        layers.append({**text, "text": st["description"], "size": 28, "x": 0.04, "y": 0.13, "align": "left", "wrap": 0.92})
    # Left: the palette, the backgrounds, the props.
    for k, key in enumerate(list(st.get("colors", {}))[:16]):
        x, y = 0.04 + (k % 8) * 0.055, 0.22 + (k // 8) * 0.12
        layers.append({"type": "rect", "x": x + 0.022, "y": y, "w": 0.044, "h": 0.065, "fill": key,
                       "stroke": "#808080/0.5", "width": 1, "radius": 8})  # fmt: skip
        layers.append({**text, "text": key, "x": x + 0.022, "y": y + 0.055})
    props = list(st.get("props", {}))[:16]
    for k, prop in enumerate(props):
        layers.append({"type": "prop", "name": prop, "x": 0.06 + (k % 8) * 0.055, "y": 0.66 + (k // 8) * 0.09, "w": 0.03})
        layers.append({**text, "text": prop, "size": 16, "x": 0.06 + (k % 8) * 0.055, "y": 0.66 + (k // 8) * 0.09 + 0.045})
    # Right: the fonts, the colour maps.
    for k, role in enumerate(list(st.get("fonts", {}))[:7]):
        size = min(52, float(st["fonts"][role].get("size", 48)))
        layers.append({"type": "text", "text": f"{role}: 가나다 ABC 123", "role": role, "size": size, "x": 0.52,
                       "y": 0.21 + k * 0.062, "align": "left"})  # fmt: skip
    for k, cmap in enumerate(list(st.get("colormaps", {}))[:5]):
        y = 0.65 + k * 0.04
        layers.append({"type": "tiles", "values": [[v / 15 for v in range(16)]], "colormap": cmap, "gap": 0,
                       "x": 0.62, "y": y, "w": 0.2, "h": 0.028})  # fmt: skip
        layers.append({**text, "text": cmap, "x": 0.73, "y": y, "align": "left"})
    # Along the bottom: a shape, a circle, an arrow, tiles and a curve as the style draws them.
    layers += [
        {"type": "rect", "x": 0.1, "y": 0.9, "w": 0.12, "h": 0.12, "shadow": True},
        {"type": "circle", "x": 0.22, "y": 0.9, "r": 0.055, "fill": "accent/0.25", "stroke": "accent", "width": 4},
        {"type": "arrow", "from": [0.28, 0.93], "to": [0.4, 0.87], "bend": 0.2},
        {"type": "tiles", "x": 0.5, "y": 0.9, "w": 0.13, "h": 0.12, "rows": 8, "cols": 12, "field": {"kind": "bending"}},
        {"type": "curve", "x": 0.68, "y": 0.91, "w": 0.13, "h": 0.1, "function": {"kind": "normal"}, "fill": True,
         "axes": True, "shade": [-1, 1]},  # fmt: skip
        {"type": "bars", "x": 0.87, "y": 0.91, "w": 0.12, "h": 0.1, "values": [3, 5, 4, 7], "value_labels": False},
    ]
    image = frame(layers, name, width, height, 0.0, 1.0)
    # Small copies of each background, between the palette and the props.
    painter = QPainter(image)
    presets = list(st.get("backgrounds", {}))[:5]
    for k, preset in enumerate(presets):
        small = QImage(round(0.08 * width), round(0.08 * width * 9 / 16), QImage.Format.Format_RGB32)
        inner = QPainter(small)
        try:
            _draw_background(inner, {"type": "background", "preset": preset}, Ctx(st, small.width(), small.height(), 1))
        finally:
            inner.end()
        x, y = round((0.04 + k * 0.09) * width), round(0.43 * height)
        painter.drawImage(x, y, small)
        label = {**text, "text": preset, "x": (x + small.width() / 2) / width, "y": (y + small.height()) / height + 0.025}
        _draw_words(painter, preset, label, Ctx(st, width, height, 1), State(0, "none", 1, 1, 0, "none", 0), "caption")
    painter.end()
    return image
