"""Plugin layers that come with the editor, written the way a user's plugin is (see graphics.py, "plugins").

A plugin draws one kind of layer with Python: draw(painter, api, layer). `api` gives the frame size, the
style's colours and fonts, easing and the time; `layer` holds the layer's values with formulas worked out.
The engine adds what every layer has (coming in and going, keyframes, turning, size, shadow, glow).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath


def _particles(painter, api, layer) -> None:
    count = max(1, min(2000, int(layer.get("count", 60))))
    seed = float(layer.get("seed", 1))
    mode = layer.get("mode", "burst")
    size = float(layer.get("size", 6))
    speed = float(layer.get("speed", 1.0))
    colour = api.colour(layer.get("color", "accent"))
    painter.setPen(Qt.PenStyle.NoPen)
    t = api.t * speed
    for k in range(count):
        r1, r2, r3 = api.rand(k, seed), api.rand(k, seed + 1), api.rand(k, seed + 2)
        radius = api.px(size) * (0.5 + r3)
        if mode == "burst":
            angle = r1 * 2 * math.pi
            reach = float(layer.get("spread", 0.25)) * min(api.width, api.height) * (0.35 + 0.65 * r2)
            p = api.ease("out_cubic", min(1.0, t / max(0.1, float(layer.get("life", 1.2)))))
            x = api.x(layer.get("x", 0.5)) + math.cos(angle) * reach * p
            y = api.y(layer.get("y", 0.5)) + math.sin(angle) * reach * p + api.px(60) * p * p * r3
            alpha = 1 - p
        elif mode == "snow":
            x = api.x(r1) + math.sin(t * (0.6 + r2) + k) * api.px(30)
            y = ((r2 + t * (0.05 + 0.08 * r3)) % 1.1 - 0.05) * api.height
            alpha = 0.4 + 0.6 * r3
        else:  # float: soft dots drifting up
            x = api.x(r1) + math.sin(t * 0.5 + k) * api.px(20)
            y = ((r2 - t * (0.02 + 0.05 * r3)) % 1.1 - 0.05) * api.height
            alpha = (0.3 + 0.7 * r3) * (0.6 + 0.4 * math.sin(t * 2 + k))
        if alpha <= 0:
            continue
        c = type(colour)(colour)
        c.setAlphaF(max(0.0, min(1.0, colour.alphaF() * alpha)))
        painter.setBrush(c)
        painter.drawEllipse(QPointF(x, y), radius, radius)


def _wave(painter, api, layer) -> None:
    left = api.x(float(layer.get("x", 0.5)) - float(layer.get("w", 0.8)) / 2)
    width = api.x(layer.get("w", 0.8))
    middle = api.y(layer.get("y", 0.5))
    amplitude = api.px(layer.get("amplitude", 60))
    wavelength = max(0.01, float(layer.get("wavelength", 0.25))) * width
    lines = max(1, min(12, int(layer.get("lines", 1))))
    shown = api.p_in if api.enter == "draw" else 1.0
    for k in range(lines):
        phase = float(layer.get("phase", 0)) + k * 0.6 + api.t * float(layer.get("speed", 0.5)) * 2 * math.pi
        path = QPainterPath()
        steps = 160
        for s in range(steps + 1):
            x = left + width * s / steps
            y = middle + amplitude * (1 - 0.25 * k) * math.sin(2 * math.pi * (x - left) / wavelength - phase)
            if s == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setOpacity(painter.opacity() * (1.0 if k == 0 else 0.5))
        painter.strokePath(api.partial(path, shown), api.pen(layer.get("color", "accent"), layer.get("width", 4)))


def _progress(painter, api, layer) -> None:
    value = max(0.0, min(1.0, float(layer.get("value", 0.75))))
    if api.enter == "draw":
        value *= api.p_in
    colour, track = layer.get("color", "accent"), layer.get("track", "line")
    width = float(layer.get("width", 18))
    cx, cy = api.x(layer.get("x", 0.5)), api.y(layer.get("y", 0.5))
    if layer.get("kind", "ring") == "bar":
        w, h = api.x(layer.get("w", 0.5)), max(api.px(width), api.y(layer.get("h", 0.0)))
        rect = QRectF(cx - w / 2, cy - h / 2, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(api.colour(track))
        painter.drawRoundedRect(rect, h / 2, h / 2)
        painter.setBrush(api.brush(colour, rect))
        painter.drawRoundedRect(QRectF(rect.left(), rect.top(), max(h, w * value), h), h / 2, h / 2)
        label_y = layer.get("y", 0.5) - h / api.height - 0.04
    else:
        r = float(layer.get("r", 0.12)) * min(api.width, api.height)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(api.pen(track, width))
        painter.drawEllipse(rect)
        pen = api.pen(colour, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 90 * 16, round(-360 * 16 * value))
        label_y = layer.get("y", 0.5)
    if layer.get("label", True):
        api.text(painter, f"{value * 100:.0f}%", layer.get("x", 0.5), label_y, role=layer.get("role", "heading"))


# name -> (what it draws, its values, draw)
BUILTIN = {
    "particles": (
        "점(입자)들: burst=한 점에서 터져 퍼짐, float=화면에 떠오르는 점, snow=내리는 눈",
        {"mode": "burst | float | snow", "x": "터지는 곳 가로 (burst)", "y": "터지는 곳 세로 (burst)", "count": "점 개수 (60)",
         "spread": "퍼지는 거리, 짧은 변 비율 (0.25)", "life": "burst가 퍼지는 시간 초 (1.2)", "size": "점 크기 px (6)",
         "speed": "빠르기 배율 (1)", "color": "색 (accent)", "seed": "무작위 씨앗 (1)"},  # fmt: skip
        _particles,
    ),
    "wave": (
        "출렁이는 물결선 (계속 움직임, enter \"draw\"로 그려 나감)",
        {"x": "가운데 가로", "y": "가운데 세로", "w": "폭 (0.8)", "amplitude": "높이 px (60)", "wavelength": "물결 길이, 폭 비율 (0.25)",
         "speed": "초당 물결 수 (0.5)", "lines": "겹친 선 수 (1)", "phase": "시작 위상", "color": "색 (accent)", "width": "굵기 px (4)"},  # fmt: skip
        _wave,
    ),
    "progress": (
        "진행률 고리나 막대와 % 글자 (value에 식을 쓰면 움직임, enter \"draw\"로 차오름)",
        {"kind": "ring | bar", "value": "0–1 (0.75)", "x": "가운데 가로", "y": "가운데 세로", "r": "고리 반지름, 짧은 변 비율 (0.12)",
         "w": "막대 폭 (0.5)", "h": "막대 높이", "width": "굵기 px (18)", "color": "색 (accent)", "track": "바탕 색 (line)",
         "label": "% 글자 (true)", "role": "글자 역할 (heading)"},  # fmt: skip
        _progress,
    ),
}
