"""Scene templates: the layout of a graphic scene with blanks to fill in, so scenes of a kind (a title card, a
list, a result shown as colour tiles) are built the same way every time from a few values.

A template is {"description", "params": {name: {"default", "doc"}} (or name: default), "duration", "style",
"layers"}; see graphic_templates.py for how blanks, "each" and "if" work. Built-in templates are in
graphic_templates.py; a user's are JSON files in the 템플릿 folder (a file wins over a built-in one).
A scene made from a template keeps the layers it was given; changing the template later does not change it.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from . import graphics, paths
from .graphic_templates import BUILTIN

TEMPLATE_PARTS = {"description", "params", "duration", "style", "layers"}
_BLANK = re.compile(r"\{([^{}\s\"']+)\}")
_FALSE = {"", "0", "false", "no", "off", "아니오", "아니요", "끔", "x"}
WORD_FIELDS = {"text", "prefix", "suffix", "x_label", "y_label"}  # a number put in these becomes words


def templates_dir() -> Path:
    return paths.DATA_DIR / "템플릿"


def _user() -> dict[str, Path]:
    folder = templates_dir()
    return {p.stem: p for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}


def names() -> list[str]:
    return list(dict.fromkeys([*BUILTIN, *_user()]))


def is_builtin(name: str) -> bool:
    return name in BUILTIN and name not in _user()


def get(name: str) -> dict:
    user = _user()
    if name in user:
        try:
            data = json.loads(user[name].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise ValueError(f"템플릿 파일을 읽을 수 없습니다: {user[name]}\n{e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"템플릿 파일이 올바르지 않습니다: {user[name]}")
        return data
    if name in BUILTIN:
        return copy.deepcopy(BUILTIN[name])
    raise ValueError(f"템플릿 '{name}'이(가) 없습니다. 있는 템플릿: {', '.join(names())}")


def params(template: dict) -> dict[str, dict]:
    """{name: {"default", "doc"}} whichever way the template wrote them."""
    out = {}
    for name, spec in (template.get("params") or {}).items():
        out[name] = dict(spec) if isinstance(spec, dict) and "default" in spec else {"default": spec, "doc": ""}
        out[name].setdefault("doc", "")
    return out


# ---- values from words (a spreadsheet cell, a box in the window) ------------------------------------------


def _number(text: str):
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return int(value) if value.is_integer() and "." not in text else value


def parse_value(text, default):
    """A value typed as words, read the way the blank's default is: a number, a list ("a | b | c"), yes/no."""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':  # quoted: kept as written, spaces and all
        return s[1:-1]
    if s.startswith(("[", "{")) or s in ("null", "true", "false"):
        try:
            return json.loads(s)
        except ValueError:
            pass
    if isinstance(default, bool):
        return s.lower() not in _FALSE
    if isinstance(default, (int, float)):
        number = _number(s)
        return number if number is not None else s  # maybe a formula
    if isinstance(default, list) or (default is None and ("|" in s or "," in s)):
        if not s:
            return []
        parts = [p.strip() for p in s.split("|" if "|" in s or isinstance(default, list) else ",")]
        numbers = [_number(p) for p in parts]
        return numbers if parts and all(n is not None for n in numbers) else parts
    if default is None:
        if not s:
            return None
        number = _number(s)
        return number if number is not None else s
    return s


def parse_params(text, template: dict) -> dict:
    """Values written as "title=제목; items=가 | 나" (or one per line, or a JSON object)."""
    if isinstance(text, dict):
        return dict(text)
    if text is None or not str(text).strip():
        return {}
    s = str(text).strip()
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except ValueError as e:
            raise ValueError(f"템플릿 값을 읽을 수 없습니다: {e}") from e
        if isinstance(data, dict):
            return data
    spec = params(template)
    out = {}
    for part in re.split(r"[;\n]", s):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"템플릿 값은 이름=값 모양으로 적습니다 (받은 것: {part.strip()!r})")
        name, _, value = part.partition("=")
        name = name.strip()
        out[name] = parse_value(value, spec.get(name, {}).get("default", ""))
    return out


def as_text(value) -> str:
    """A value written back as words (the opposite of parse_value)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(as_text(v) for v in value)
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---- filling in -----------------------------------------------------------------------------------------


def _truthy(value) -> bool:
    return value not in (None, "", [], {}, False, 0)


def _text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def _fill(value, values: dict):
    if isinstance(value, str):
        whole = _BLANK.fullmatch(value)
        if whole and whole.group(1) in values:
            return copy.deepcopy(values[whole.group(1)])  # the value itself, number, list and all
        return _BLANK.sub(lambda m: _text(values[m.group(1)]) if m.group(1) in values else m.group(0), value)
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            filled = _fill(v, values)
            if filled is not None:  # a blank left empty takes its key away
                out[key] = filled
        return out
    return value


def _condition(cond, values: dict) -> bool:
    if isinstance(cond, bool):
        return cond
    text = str(cond).strip()
    if text in values:
        return _truthy(values[text])
    if text.startswith("!") and text[1:] in values:
        return not _truthy(values[text[1:]])
    return bool(graphics.evaluate(str(_fill(text, values)), {}))


def _layer(layer: dict, values: dict) -> dict | None:
    if "if" in layer and not _condition(layer["if"], values):
        return None
    out = {}
    for key, value in layer.items():
        if key in ("if", "each"):
            continue
        filled = _layers(value, values) if key == "layers" and isinstance(value, list) else _fill(value, values)
        if filled is not None:
            out[key] = _text(filled) if key in WORD_FIELDS and not isinstance(filled, str) else filled
    return out


def _layers(layers: list, values: dict) -> list:
    out = []
    for layer in layers:
        if not isinstance(layer, dict):
            out.append(layer)
            continue
        each = layer.get("each")
        if each is None:
            one = _layer(layer, values)
            if one is not None:
                out.append(one)
            continue
        items = values.get(each)
        if not isinstance(items, list):
            items = [] if items in (None, "") else [items]
        for k, item in enumerate(items):
            local = {**values, "item": item, "index": k, "number": k + 1, "count": len(items)}
            if isinstance(item, dict):
                local.update({f"item.{key}": v for key, v in item.items()})
            one = _layer(layer, local)
            if one is not None:
                out.append(one)
    return out


def expand(template: dict | str, values: dict | None = None) -> dict:
    """{"layers", "duration", "style"} of the template with its blanks filled (the defaults for the rest)."""
    data = get(template) if isinstance(template, str) else template
    spec = params(data)
    values = dict(values or {})
    unknown = sorted(set(values) - set(spec))
    if unknown:
        raise ValueError(f"템플릿에 없는 값 {unknown}. 받는 값: {sorted(spec)}")
    filled = {name: copy.deepcopy(p["default"]) for name, p in spec.items()}
    filled.update(values)
    return {"layers": _layers(data.get("layers") or [], filled), "duration": float(data.get("duration", 4.0)),
            "style": str(data.get("style", ""))}  # fmt: skip


# ---- keeping templates ----------------------------------------------------------------------------------


def check(data: dict, style_name: str = "") -> list[str]:
    """What is wrong with a template, in words: its parts, and its layers filled with the defaults."""
    if not isinstance(data, dict):
        return ["템플릿은 dict여야 합니다."]
    errors = []
    unknown = sorted(set(data) - TEMPLATE_PARTS)
    if unknown:
        errors.append(f"알 수 없는 항목 {unknown}. 쓸 수 있는 항목: {sorted(TEMPLATE_PARTS)}")
    if not isinstance(data.get("layers"), list):
        errors.append("layers(레이어 목록)가 필요합니다.")
    if not isinstance(data.get("params", {}), dict):
        errors.append("params는 {이름: {\"default\": 값, \"doc\": 설명}} 모양이어야 합니다.")
    if errors:
        return errors
    try:
        filled = expand(data)
    except (ValueError, TypeError) as e:
        return [f"기본값으로 채우지 못했습니다: {e}"]
    return [f"기본값으로 채웠을 때: {e}" for e in graphics.check_layers(filled["layers"], style_name or filled["style"])]


def save(name: str, data: dict) -> Path:
    name = name.strip()
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise ValueError(f"템플릿 이름으로 쓸 수 없습니다: {name!r}")
    errors = check(data)
    if errors:
        raise ValueError("템플릿에 문제가 있습니다.\n" + "\n".join(errors[:20]))
    folder = templates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)
    return path


def delete(name: str) -> None:
    path = _user().get(name)
    if path is None:
        raise ValueError(f"지울 수 있는 템플릿이 아닙니다(기본으로 들어 있거나 없음): {name}")
    path.unlink()


def from_layers(layers: list, replace: dict[str, str] | None = None, description: str = "", duration: float = 4.0,
                style: str = "") -> dict:  # fmt: skip
    """A template made from a scene's layers; each word in `replace` becomes a blank of that name (its default)."""
    replace = replace or {}

    def swap(value):
        if isinstance(value, str):
            for original, blank in replace.items():
                if original and original in value:
                    value = value.replace(original, "{" + blank + "}")
            return value
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: (v if k in ("type", "role", "preset", "plugin", "name") else swap(v)) for k, v in value.items()}
        return value

    return {"description": description, "params": {blank: {"default": original, "doc": ""} for original, blank in replace.items()},
            "duration": duration, "style": style, "layers": swap(copy.deepcopy(layers))}  # fmt: skip
