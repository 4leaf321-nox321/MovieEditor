"""Draws scene frames with QPainter: backgrounds, the slow zoom or pan over images, overlays and text.

The preview, the thumbnails and the render all draw through here, so what you see is what gets made.
A scene is drawn in two layers: the background (an image or colour, or a clip that FFmpeg fits), and
on top the "layer" of pictures laid over it and its words, which can come in with an effect.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextLayout,
    QTextOption,
)

from . import adjust, graphics, keyframes
from .scenes import LOGO_CORNERS, NO_ADJUSTMENTS, Mask, Overlay, Project, Scene, TextBox, from_dict

IMAGE_SUFFIXES = tuple(f".{bytes(f).decode()}" for f in QImageReader.supportedImageFormats())
# Qt refuses to decode images needing more than 256 MB, which a 70-megapixel PNG already does.
QImageReader.setAllocationLimit(2048)
ZOOM = 1.15  # how far the slow zoom and the pan move in
SOURCE_SCALE = 1.25  # images are kept at most this much bigger than the frame, enough for ZOOM
REFERENCE_SIDE = 1080  # text sizes are pixels on a frame whose short side is this long
TEXT_MARGIN_X = 0.07  # of the frame width, on each side
TEXT_EDGE = 0.02  # a text block is kept at least this far inside the frame
OUTLINE = 0.07  # outline width as a share of the font size
BLUR_STEPS = (32, 8)  # the blurred backdrop is scaled down by the first, then up through the second
BACKDROP_SHADE = QColor(0, 0, 0, 80)
BORDER = 0.015  # white frame around an overlay, as a share of its width
LOGO_MARGIN = 0.03  # of the frame's shorter side
# Entrance effects: how long they take (seconds) and, for typing, characters per second.
EFFECT_SECONDS = {"fade": 0.5, "pop": 0.5, "slide": 0.45}
EXIT_SECONDS = {"fade": 0.5, "pop": 0.4, "slide": 0.45}  # leaving, ending with the scene
CHROMA_BLEND = 0.1  # how soft the edge of a colour made see-through is (FFmpeg colorkey's blend)
TYPE_RATE = 14.0
SLIDE_SHARE = 0.06  # of the frame height


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


@lru_cache(maxsize=24)
def _load(path: str, mtime: int, max_side: int) -> QImage:
    reader = QImageReader(path)
    reader.setAutoTransform(True)  # honour the camera's rotation flag
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > 2 * max_side:
        # Decode big photos (100+ megapixels) straight to a smaller size: much quicker and lighter.
        size.scale(2 * max_side, 2 * max_side, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"이미지를 열 수 없습니다: {Path(path).name} ({reader.errorString()})")
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(
            max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


@lru_cache(maxsize=24)
def _prepared(path: str, mtime: int, max_side: int, adjustments: tuple) -> QImage:
    # A cropped image is decoded bigger first, so the part kept is still sharp.
    keep = adjust.kept_share(adjustments[0])
    image = adjust.apply(_load(path, mtime, round(max_side / keep)), adjustments)
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(
            max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return image


def load_image(path: str, width: int, height: int, adjustments: tuple = NO_ADJUSTMENTS) -> QImage:
    max_side = round(max(width, height) * SOURCE_SCALE)
    if adjustments == NO_ADJUSTMENTS:
        return _load(path, _mtime(path), max_side)
    return _prepared(path, _mtime(path), max_side, adjustments)


def _cover(iw: float, ih: float, width: int, height: int) -> QRectF:
    """The largest part of the image with the frame's shape, centred."""
    scale = min(iw / width, ih / height)
    w, h = width * scale, height * scale
    return QRectF((iw - w) / 2, (ih - h) / 2, w, h)


def _zoom(motion: str, progress: float) -> float:
    if motion == "zoom_in":
        return 1 + (ZOOM - 1) * progress
    if motion == "zoom_out":
        return ZOOM - (ZOOM - 1) * progress
    if motion in ("pan_right", "pan_left"):
        return ZOOM
    return 1.0


def _moved(rect: QRectF, motion: str, progress: float) -> QRectF:
    """The part of `rect` in view at `progress` (0 to 1) through the scene."""
    z = _zoom(motion, progress)
    w, h = rect.width() / z, rect.height() / z
    x = rect.x() + (rect.width() - w) / 2
    if motion == "pan_right":
        x = rect.x() + (rect.width() - w) * progress
    elif motion == "pan_left":
        x = rect.x() + (rect.width() - w) * (1 - progress)
    return QRectF(x, rect.y() + (rect.height() - h) / 2, w, h)


@lru_cache(maxsize=8)
def _backdrop(path: str, mtime: int, width: int, height: int, adjustments: tuple) -> QImage:
    """A blurred, darkened copy of the image filling the frame, behind an image shown whole."""
    image = load_image(path, width, height, adjustments)
    part = image.copy(_cover(image.width(), image.height(), width, height).toRect())
    small, middle = BLUR_STEPS
    smooth, ignore = Qt.TransformationMode.SmoothTransformation, Qt.AspectRatioMode.IgnoreAspectRatio
    part = part.scaled(max(1, width // small), max(1, height // small), ignore, smooth)
    part = part.scaled(max(1, width // middle), max(1, height // middle), ignore, smooth)
    part = part.scaled(width, height, ignore, smooth).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(part)
    painter.fillRect(part.rect(), BACKDROP_SHADE)
    painter.end()
    return part


def _draw_background(painter: QPainter, scene: Scene, width: int, height: int, progress: float, t: float) -> None:
    if scene.kind == "graphic":  # its shapes, words and tiles are its picture
        graphics.draw(painter, scene.layers, scene.style, width, height, t, scene.duration)
        return
    # A split scene shows only its stretch of the zoom or pan, so the halves join up.
    progress = scene.motion_from + (scene.motion_to - scene.motion_from) * progress
    if scene.kind == "image" and scene.source:
        image = load_image(scene.source, width, height, scene.adjustments)
        if scene.fit == "contain":
            painter.drawImage(0, 0, _backdrop(scene.source, _mtime(scene.source), width, height, scene.adjustments))
            scale = min(width / image.width(), height / image.height())
            scale *= _zoom(scene.motion if scene.motion.startswith("zoom") else "none", progress)
            w, h = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF((width - w) / 2, (height - h) / 2, w, h), image, QRectF(image.rect()))
        else:
            source = _moved(_cover(image.width(), image.height(), width, height), scene.motion, progress)
            painter.drawImage(QRectF(0, 0, width, height), image, source)
    elif scene.color2:
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, QColor(scene.color))
        gradient.setColorAt(1, QColor(scene.color2))
        painter.fillRect(0, 0, width, height, gradient)
    else:
        painter.fillRect(0, 0, width, height, QColor(scene.color))


# ---- text ---------------------------------------------------------------------


@dataclass
class _Layout:
    font: QFont
    lines: list[str]
    widths: list[float]
    spacing: float
    ascent: float
    width: float  # of the block
    height: float


def _wrap(text: str, font: QFont, width: float) -> list[str]:
    lines = []
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        layout = QTextLayout(paragraph, font)
        layout.setTextOption(option)
        layout.beginLayout()
        while (line := layout.createLine()).isValid():
            line.setLineWidth(width)
            lines.append(paragraph[line.textStart() : line.textStart() + line.textLength()].strip())
        layout.endLayout()
    return lines


@lru_cache(maxsize=64)
def _layout(text: str, font_name: str, size: int, bold: bool, width: int, height: int) -> _Layout:
    font = QFont(font_name)
    font.setPixelSize(max(6, round(size * min(width, height) / REFERENCE_SIDE)))
    font.setBold(bold)
    metrics = QFontMetricsF(font)
    lines = _wrap(text, font, width * (1 - 2 * TEXT_MARGIN_X))
    widths = [metrics.horizontalAdvance(line) for line in lines]
    return _Layout(font, lines, widths, metrics.lineSpacing(), metrics.ascent(), max(widths, default=0.0),
                   metrics.lineSpacing() * len(lines))  # fmt: skip


def _box_layout(box: TextBox, width: int, height: int) -> _Layout:
    st = box.style
    return _layout(box.text.strip(), st.font, st.size, st.bold, width, height)


def text_rect(box: TextBox, width: int, height: int) -> QRectF:
    """Where the block of words sits: centred on its spot, kept inside the frame.

    Words that move (keyframes) may go out of the frame, so they can slide in from outside.
    """
    layout = _box_layout(box, width, height)
    w, h = layout.width, layout.height
    x, y = box.x * width - w / 2, box.y * height - h / 2
    if not box.keys:
        edge_x, edge_y = width * TEXT_EDGE, height * TEXT_EDGE
        x = min(max(x, edge_x), max(edge_x, width - edge_x - w))
        y = min(max(y, edge_y), max(edge_y, height - edge_y - h))
    return QRectF(x, y, w, h)


def _text_path(box: TextBox, width: int, height: int, shown: int | None = None) -> QPainterPath:
    """The words as outlines in place; `shown` limits them to the first characters (typing in)."""
    layout = _box_layout(box, width, height)
    rect = text_rect(box, width, height)
    path = QPainterPath()
    left = shown if shown is not None else 1 << 30
    for i, (line, line_width) in enumerate(zip(layout.lines, layout.widths)):
        if left <= 0:
            break
        part = line[:left]
        left -= len(line)
        x = rect.left() + (rect.width() - line_width) / 2  # each line centred; typing does not move it
        path.addText(QPointF(x, rect.top() + i * layout.spacing + layout.ascent), layout.font, part)
    return path


def _draw_words(painter: QPainter, box: TextBox, width: int, height: int, shown: int | None = None) -> None:
    layout = _box_layout(box, width, height)
    path = _text_path(box, width, height, shown)
    outline = layout.font.pixelSize() * OUTLINE
    painter.fillPath(path.translated(outline * 0.4, outline * 0.9), QColor(0, 0, 0, 140))  # soft drop shadow
    pen = QPen(QColor(0, 0, 0, 225), outline * 2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.strokePath(path, pen)
    painter.fillPath(path, QColor(box.style.color))


def effect_end(effect: str, text: str = "") -> float:
    """Seconds into the scene when an entrance effect is over."""
    if effect == "type":
        return len(text.strip()) / TYPE_RATE
    return EFFECT_SECONDS.get(effect, 0.0)


def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def _entrance(effect: str, t: float) -> tuple[float, float, float]:
    """(opacity, scale, drop as a share of the frame height) of something coming in, `t` seconds in."""
    seconds = EFFECT_SECONDS.get(effect)
    if not seconds or t >= seconds:
        return 1.0, 1.0, 0.0
    x = max(0.0, t) / seconds
    if effect == "fade":
        return _ease(x), 1.0, 0.0
    if effect == "pop":
        # Grows from 30% past full size and settles back, like a bubble popping up.
        c = 1.70158
        back = 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2
        return min(1.0, x * 5), 0.3 + 0.7 * back, 0.0
    if effect == "slide":
        return _ease(x), 1.0, (1 - _ease(x)) * SLIDE_SHARE
    return 1.0, 1.0, 0.0


def _leaving(effect: str, left: float) -> tuple[float, float, float]:
    """(opacity, scale, drop) of something going away, `left` seconds before the scene ends."""
    seconds = EXIT_SECONDS.get(effect)
    if not seconds or left >= seconds:
        return 1.0, 1.0, 0.0
    x = min(1.0, max(0.0, left) / seconds)
    x = x * x * (3 - 2 * x)  # 1 while still there, 0 once gone, easing in and out
    if effect == "pop":
        return x, 0.4 + 0.6 * x, 0.0  # shrinks away
    if effect == "slide":
        return x, 1.0, (1 - x) * SLIDE_SHARE  # sinks down
    return x, 1.0, 0.0


def exit_start(scene: Scene) -> float | None:
    """Seconds into the scene when the first of its words and pictures starts to leave (None: none leave)."""
    leaving = [b for b in scene.texts if b.text.strip() and b.exit != "none"]
    leaving += [o for o in _image_overlays(scene) if o.exit != "none"]
    if not leaving:
        return None
    return max(0.0, min(scene.duration - EXIT_SECONDS.get(item.exit, 0.0) for item in leaving))


def _with_effects(painter: QPainter, effect: str, leave: str, t: float | None, end: float | None, rect: QRectF,
                  height: int) -> bool:  # fmt: skip
    """Set the painter up for something coming in or going away; False when it is not visible now."""
    if t is None:
        return True
    opacity, scale, drop = _entrance(effect, t)
    if end is not None and leave != "none":
        out_opacity, out_scale, out_drop = _leaving(leave, end - t)
        opacity, scale, drop = opacity * out_opacity, scale * out_scale, drop + out_drop
    if opacity <= 0.001:
        return False
    painter.setOpacity(painter.opacity() * opacity)
    centre = rect.center()
    painter.translate(centre.x(), centre.y() + drop * height)
    painter.scale(scale, scale)
    painter.translate(-centre.x(), -centre.y())
    return True


def draw_text(painter: QPainter, box: TextBox, width: int, height: int, t: float | None = None,
              end: float | None = None) -> None:  # fmt: skip
    """One block of words; at `t` seconds into a scene lasting `end` seconds it comes in and goes away as set.

    With `t` None it is simply all there.
    """
    if not box.text.strip():
        return
    painter.save()
    shown = None
    entrance = box.effect
    if t is not None and box.effect == "type":
        shown = math.floor(max(0.0, t) * TYPE_RATE)
        entrance = "none"
    if not _with_effects(painter, entrance, box.exit, t, end, text_rect(box, width, height), height):
        painter.restore()
        return
    _draw_words(painter, box, width, height, shown)
    painter.restore()


# ---- overlays -----------------------------------------------------------------


def overlay_rect(overlay: Overlay, source_w: int, source_h: int, width: int, height: int) -> QRect:
    """Where an overlay of that size sits in the frame (without its border), with even sides for video."""
    w = max(2, round(overlay.width * width / 2) * 2)
    h = max(2, round(w * source_h / max(1, source_w) / 2) * 2)
    return QRect(round(overlay.x * width - w / 2), round(overlay.y * height - h / 2), w, h)


def border_width(rect: QRect) -> int:
    return max(2, round(rect.width() * BORDER))


@lru_cache(maxsize=16)
def _sticker(path: str, mtime: int, w: int, h: int, chroma: str, strength: float) -> QImage:
    image = _load(path, mtime, max(w, h) * 2)
    image = image.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return colour_key(image, chroma, strength) if chroma else image


def colour_key(image: QImage, colour: str, strength: float) -> QImage:
    """The image with `colour` made see-through, the way FFmpeg's colorkey does it for videos."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)  # straight alpha, laid out B, G, R, A
    width, height = image.width(), image.height()
    pixels = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())[:, : width * 4]
    pixels = pixels.reshape(height, width, 4)
    key = QColor(colour)
    target = np.array([key.blue(), key.green(), key.red()], np.float32)
    distance = np.sqrt(((pixels[..., :3].astype(np.float32) - target) ** 2).sum(axis=2) / (3 * 255.0**2))
    alpha = np.clip((distance - chroma_similarity(strength)) / CHROMA_BLEND, 0, 1)
    pixels[..., 3] = (pixels[..., 3] * alpha + 0.5).astype(np.uint8)
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def chroma_similarity(strength: float) -> float:
    """FFmpeg colorkey's similarity for a strength from 0 to 1."""
    return 0.02 + 0.5 * min(1.0, max(0.0, strength))


def sticker_size(path: str) -> tuple[int, int]:
    """The picture's shape as shown (a camera's rotation flag applied); only the ratio matters."""
    image = _load(path, _mtime(path), 1024)
    return image.width(), image.height()


def _frame_ring(rect: QRect, b: int) -> QPainterPath:
    """The border around `rect`: a ring, so a see-through picture inside does not show white."""
    ring = QPainterPath()
    ring.setFillRule(Qt.FillRule.OddEvenFill)
    ring.addRect(QRectF(rect.adjusted(-b, -b, b, b)))
    ring.addRect(QRectF(rect))
    return ring


def draw_overlay(painter: QPainter, overlay: Overlay, picture: QImage, width: int, height: int,
                 t: float | None = None, rect: QRect | None = None, end: float | None = None) -> None:  # fmt: skip
    """A picture (or a video's frame) laid over the scene, with its border, see-through, entrance and exit."""
    rect = rect or overlay_rect(overlay, picture.width(), picture.height(), width, height)
    painter.save()
    if overlay.kind == "image":
        effects = overlay.effect, overlay.exit
    else:  # videos only fade in and out (FFmpeg does the same)
        effects = ("fade" if overlay.effect == "fade" else "none"), ("fade" if overlay.exit == "fade" else "none")
    if not _with_effects(painter, *effects, t, end, QRectF(rect), height):
        painter.restore()
        return
    painter.setOpacity(painter.opacity() * min(1.0, max(0.0, overlay.opacity)))
    if overlay.border:
        painter.fillPath(_frame_ring(rect, border_width(rect)), QColor("#ffffff"))
    painter.drawImage(QRectF(rect), picture, QRectF(picture.rect()))
    painter.restore()


def _image_overlays(scene: Scene) -> list[Overlay]:
    return [o for o in scene.overlays if o.kind == "image" and o.source]


def clip_overlays(scene: Scene) -> list[Overlay]:
    return [o for o in scene.overlays if o.kind == "clip" and o.source]


def draw_sticker(painter: QPainter, overlay: Overlay, width: int, height: int, t: float | None = None,
                 end: float | None = None) -> None:  # fmt: skip
    """A picture laid over the scene."""
    try:
        w, h = sticker_size(overlay.source)
    except ValueError:
        return  # a picture that cannot be read is left out, as in the render
    rect = overlay_rect(overlay, w, h, width, height)
    picture = _sticker(overlay.source, _mtime(overlay.source), rect.width(), rect.height(), overlay.chroma,
                       overlay.chroma_strength)  # fmt: skip
    draw_overlay(painter, overlay, picture, width, height, t, rect, end)


def _draw_layer(painter: QPainter, scene: Scene, width: int, height: int, t: float | None, effects: bool) -> None:
    played = t if effects else None
    for overlay in _image_overlays(scene):
        draw_sticker(painter, overlay, width, height, played, scene.duration)
    for box in scene.texts:
        draw_text(painter, box, width, height, played, scene.duration)


def layer_groups(scene: Scene) -> list[Overlay | Scene]:
    """What goes over the background, bottom first: videos, and between them the pictures (with the words last),
    each run of pictures as a Scene holding only those, so it can be drawn as one layer."""
    groups: list[Overlay | Scene] = []
    for overlay in scene.overlays:
        if not overlay.source:
            continue
        if overlay.kind == "clip":
            groups.append(overlay)
        elif groups and isinstance(groups[-1], Scene):
            groups[-1].overlays.append(overlay)
        else:
            groups.append(Scene(duration=scene.duration, overlays=[overlay]))  # as long, for leaving on time
    if any(b.text.strip() for b in scene.texts):
        if not (groups and isinstance(groups[-1], Scene)):
            groups.append(Scene(duration=scene.duration))
        groups[-1].texts = list(scene.texts)
    return groups


def has_layer(scene: Scene) -> bool:
    return bool(_image_overlays(scene)) or any(b.text.strip() for b in scene.texts)


def _layered(scene: Scene) -> list:
    """The words and pictures that are drawn here (videos on top are laid on by FFmpeg)."""
    return [b for b in scene.texts if b.text.strip()] + _image_overlays(scene)


def animation_end(scene: Scene) -> float:
    """Seconds into the scene when every text and picture laid over it has finished coming in."""
    ends = [effect_end(b.effect, b.text) for b in scene.texts if b.text.strip()]
    ends += [effect_end(o.effect) for o in _image_overlays(scene)]
    return max(ends, default=0.0)


def moving(scene: Scene) -> list[tuple[float, float]]:
    """When words or pictures drawn here move by keyframes: (from, to) seconds, one per item that moves."""
    return [s for item in _layered(scene) if (s := keyframes.span(item.keys))]


def at_time(item, t: float):
    """A text box or overlay as it is `t` seconds into the scene: placed and sized by its keyframes."""
    where = keyframes.sample(item.keys, t, item.smooth)
    if where is None:
        return item
    x, y, scale = where
    item = copy.deepcopy(item)
    item.x, item.y = x, y
    if isinstance(item, TextBox):
        item.style.size = max(6, round(item.style.size * scale))
    else:
        item.width = max(0.01, item.width * scale)
    return item


def placed(scene: Scene, t: float) -> Scene:
    """The scene with its words and overlays where their keyframes put them `t` seconds in."""
    if not any(item.keys for item in (*scene.texts, *scene.overlays)):
        return scene
    return dataclasses.replace(scene, texts=[at_time(b, t) for b in scene.texts],
                               overlays=[at_time(o, t) for o in scene.overlays])  # fmt: skip


def _layer_json(scene: Scene) -> str:
    """What the finished layer looks like, as a cache key (with the pictures' file times)."""
    overlays = _image_overlays(scene)
    return json.dumps(
        {
            "duration": scene.duration,
            "texts": [dataclasses.asdict(b) for b in scene.texts if b.text.strip()],
            "overlays": [dataclasses.asdict(o) for o in overlays],
            "times": [_mtime(o.source) for o in overlays],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@lru_cache(maxsize=32)
def _static_layer(layer_json: str, width: int, height: int) -> QImage:
    data = json.loads(layer_json)
    scene = Scene(duration=data["duration"], texts=[from_dict(TextBox, d) for d in data["texts"]],
                  overlays=[from_dict(Overlay, d) for d in data["overlays"]])  # fmt: skip
    return _paint_layer(scene, width, height, None, effects=False)


def _paint_layer(scene: Scene, width: int, height: int, t: float | None, effects: bool) -> QImage:
    layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    painter = QPainter(layer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _draw_layer(painter, scene, width, height, t, effects)
    painter.end()
    return layer


def is_settled(scene: Scene, t: float | None) -> bool:
    """True when, `t` seconds in, everything laid over the scene has come in, nothing is leaving yet and
    nothing is moving: the layer then looks the same as in the frames around it."""
    if t is None:
        return True
    leaving = exit_start(scene)
    if t < animation_end(scene) or (leaving is not None and t >= leaving):
        return False
    return not any(a <= t <= b for a, b in moving(scene))


def layer_image(scene: Scene, width: int, height: int, t: float | None = None, effects: bool = True) -> QImage | None:
    """The overlays and words on a transparent frame `t` seconds in, moved by their keyframes and, with
    `effects`, coming in or going away. With `t` None everything is in place, as at the start."""
    if not has_layer(scene):
        return None
    scene_then = placed(scene, t or 0.0)
    if not effects or is_settled(scene, t):
        return _static_layer(_layer_json(scene_then), width, height)
    return _paint_layer(scene_then, width, height, t, effects=True)


def is_static(scene: Scene, with_layer: bool = True) -> bool:
    """True when every frame of the image or colour scene looks the same (no zoom, pan or entrance)."""
    if scene.kind == "clip":
        return False
    if scene.kind == "image" and scene.source and scene.motion != "none":
        return False
    if scene.kind == "graphic" and not graphics.is_static(scene.layers, scene.duration):
        return False
    return not (with_layer and is_animated(scene))


def is_animated(scene: Scene) -> bool:
    """Whether the words and pictures laid over the scene change during it (coming in, going away, moving)."""
    return animation_end(scene) > 0 or exit_start(scene) is not None or bool(moving(scene))


# ---- masks --------------------------------------------------------------------


def mask_rect(mask: Mask, width: int, height: int) -> QRect:
    """The hidden part of the frame, with even sides and inside the frame (FFmpeg crops it the same)."""
    w = min(width // 2 * 2, max(4, round(mask.width * width / 2) * 2))
    h = min(height // 2 * 2, max(4, round(mask.height * height / 2) * 2))
    x = min(max(0, round(mask.x * width - w / 2) // 2 * 2), (width - w) // 2 * 2)
    y = min(max(0, round(mask.y * height - h / 2) // 2 * 2), (height - h) // 2 * 2)
    return QRect(x, y, w, h)


def mask_reduction(mask: Mask, width: int, height: int) -> tuple[int, int]:
    """How small the hidden part is made before it is blown up again: the mosaic's blocks, or the blur's
    sample, from the mask's strength."""
    rect = mask_rect(mask, width, height)
    strength = min(1.0, max(0.0, mask.strength))
    if mask.kind == "blur":
        step = 4 + 28 * strength  # times smaller
    else:
        step = max(4.0, min(width, height) * (0.01 + 0.05 * strength))  # pixels per block
    return max(1, round(rect.width() / step)), max(1, round(rect.height() / step))


def apply_masks(frame: QImage, masks: list[Mask]) -> None:
    """Hide the masks' parts of `frame` under a mosaic or blur, in place."""
    if not masks:
        return
    width, height = frame.width(), frame.height()
    painter = QPainter(frame)
    for mask in masks:
        rect = mask_rect(mask, width, height)
        part = frame.copy(rect)
        small_w, small_h = mask_reduction(mask, width, height)
        how = Qt.TransformationMode.FastTransformation if mask.kind == "mosaic" else Qt.TransformationMode.SmoothTransformation
        ignore = Qt.AspectRatioMode.IgnoreAspectRatio
        part = part.scaled(small_w, small_h, ignore, how).scaled(rect.width(), rect.height(), ignore, how)
        painter.drawImage(rect.topLeft(), part)
    painter.end()


def render_frame(scene: Scene, width: int, height: int, progress: float = 0.0, frame: QImage | None = None,
                 t: float | None = None, with_layer: bool = True, effects: bool = True) -> QImage:  # fmt: skip
    """One frame of an image, colour or graphic scene; `progress` runs from 0 at its start to 1 at its end.

    `t` (seconds into the scene) places moving words and plays the entrance and exit effects (unless
    `effects` is off); None shows everything in place. Pass `frame` to draw into an existing image of
    that size instead of allocating a new one.
    """
    if frame is None:
        frame = QImage(width, height, QImage.Format.Format_RGB32)
    # A new image holds leftover memory and a reused one the last frame; transparent pictures would show it.
    frame.fill(Qt.GlobalColor.black)
    painter = QPainter(frame)
    try:  # a graphic scene's plugin may fail; the painter must still be finished
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_background(painter, scene, width, height, progress, t if t is not None else progress * scene.duration)
    finally:
        painter.end()
    apply_masks(frame, scene.masks)  # the hidden parts belong to the picture, under the words and overlays
    if with_layer:
        layer = layer_image(scene, width, height, t, effects)
        if layer is not None:
            painter = QPainter(frame)
            painter.drawImage(0, 0, layer)
            painter.end()
    return frame


def frames(scene: Scene, width: int, height: int, count: int, fps: int, with_layer: bool = True) -> Iterator[memoryview]:
    """Raw BGR0 frames for FFmpeg ("-f rawvideo -pix_fmt bgr0").

    The zoom or pan goes by time, so the frames after the scene's end (blending into the next one) hold
    its last position. Every frame is drawn into the same buffer (a 4K frame is 33 MB), so each one must
    be written out before the next is asked for, as ffmpeg.run does.
    """
    frame = QImage(width, height, QImage.Format.Format_RGB32)
    with graphics.playing():  # videos in a graphic scene are read as a stream while the frames go out
        for n in range(count):
            t = n / fps
            progress = min(1.0, t / scene.duration) if scene.duration else 0.0
            render_frame(scene, width, height, progress, frame, t, with_layer)
            yield frame.constBits()


# ---- the logo -----------------------------------------------------------------


@lru_cache(maxsize=4)
def _logo_layer(path: str, mtime: int, corner: str, size: float, opacity: float, width: int, height: int) -> QImage:
    layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    image = _load(path, mtime, max(width, height))
    w = max(2, round(width * size))
    h = max(2, round(w * image.height() / max(1, image.width())))
    margin = round(min(width, height) * LOGO_MARGIN)
    x = margin if corner.endswith("left") else width - margin - w
    y = margin if corner.startswith("top") else height - margin - h
    painter = QPainter(layer)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setOpacity(min(1.0, max(0.0, opacity)))
    painter.drawImage(QRectF(x, y, w, h), image, QRectF(image.rect()))
    painter.end()
    return layer


def logo_layer(project: Project, width: int, height: int) -> QImage | None:
    """The project's logo in its corner on a transparent frame, or None."""
    if not project.logo or not Path(project.logo).is_file():
        return None
    corner = project.logo_corner if project.logo_corner in LOGO_CORNERS else "top_right"
    return _logo_layer(project.logo, _mtime(project.logo), corner, project.logo_size, project.logo_opacity, width, height)
