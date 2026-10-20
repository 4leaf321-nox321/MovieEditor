"""Draws scene frames with QPainter: backgrounds, the slow zoom or pan over images, and on-screen text.

The preview, the thumbnails and the render all draw through here, so what you see is what gets made.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
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

from .scenes import Scene, TextStyle

IMAGE_SUFFIXES = tuple(f".{bytes(f).decode()}" for f in QImageReader.supportedImageFormats())
# Qt refuses to decode images needing more than 256 MB, which a 70-megapixel PNG already does.
QImageReader.setAllocationLimit(2048)
ZOOM = 1.15  # how far the slow zoom and the pan move in
SOURCE_SCALE = 1.25  # images are kept at most this much bigger than the frame, enough for ZOOM
REFERENCE_SIDE = 1080  # text sizes are pixels on a frame whose short side is this long
TEXT_MARGIN_X = 0.07  # of the frame width, on each side
TEXT_MARGIN_Y = 0.09  # of the frame height, for text at the top or bottom
OUTLINE = 0.07  # outline width as a share of the font size
BLUR_STEPS = (32, 8)  # the blurred backdrop is scaled down by the first, then up through the second
BACKDROP_SHADE = QColor(0, 0, 0, 80)


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


def load_image(path: str, width: int, height: int) -> QImage:
    return _load(path, _mtime(path), round(max(width, height) * SOURCE_SCALE))


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
def _backdrop(path: str, mtime: int, width: int, height: int) -> QImage:
    """A blurred, darkened copy of the image filling the frame, behind an image shown whole."""
    image = load_image(path, width, height)
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


def _draw_background(painter: QPainter, scene: Scene, width: int, height: int, progress: float) -> None:
    if scene.kind == "image" and scene.source:
        image = load_image(scene.source, width, height)
        if scene.fit == "contain":
            painter.drawImage(0, 0, _backdrop(scene.source, _mtime(scene.source), width, height))
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


@lru_cache(maxsize=32)
def _text_layer(text: str, style: tuple, width: int, height: int) -> QImage:
    st = TextStyle(*style)
    layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    font = QFont(st.font)
    font.setPixelSize(max(6, round(st.size * min(width, height) / REFERENCE_SIDE)))
    font.setBold(st.bold)
    metrics = QFontMetricsF(font)
    margin_x = width * TEXT_MARGIN_X
    lines = _wrap(text, font, width - 2 * margin_x)
    block = metrics.lineSpacing() * len(lines)
    if st.position == "top":
        top = height * TEXT_MARGIN_Y
    elif st.position == "bottom":
        top = height * (1 - TEXT_MARGIN_Y) - block
    else:
        top = (height - block) / 2

    path = QPainterPath()
    for i, line in enumerate(lines):
        x = (width - metrics.horizontalAdvance(line)) / 2
        path.addText(QPointF(x, top + i * metrics.lineSpacing() + metrics.ascent()), font, line)
    outline = font.pixelSize() * OUTLINE
    painter = QPainter(layer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillPath(path.translated(outline * 0.4, outline * 0.9), QColor(0, 0, 0, 140))  # soft drop shadow
    pen = QPen(QColor(0, 0, 0, 225), outline * 2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.strokePath(path, pen)
    painter.fillPath(path, QColor(st.color))
    painter.end()
    return layer


def text_layer(scene: Scene, width: int, height: int) -> QImage | None:
    """The scene's words on a transparent frame, or None when it has none."""
    if not scene.text.strip():
        return None
    st = scene.style
    return _text_layer(scene.text.strip(), (st.font, st.size, st.bold, st.color, st.position), width, height)


def is_static(scene: Scene) -> bool:
    """True when every frame of the scene looks the same (no clip, no zoom or pan)."""
    return scene.kind != "clip" and not (scene.kind == "image" and scene.source and scene.motion != "none")


def render_frame(scene: Scene, width: int, height: int, progress: float = 0.0, frame: QImage | None = None) -> QImage:
    """One frame of an image or colour scene; `progress` runs from 0 at its start to 1 at its end.

    Pass `frame` to draw into an existing image of that size instead of allocating a new one.
    """
    if frame is None:
        frame = QImage(width, height, QImage.Format.Format_RGB32)
    # A new image holds leftover memory and a reused one the last frame; transparent pictures would show it.
    frame.fill(Qt.GlobalColor.black)
    painter = QPainter(frame)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _draw_background(painter, scene, width, height, progress)
    layer = text_layer(scene, width, height)
    if layer is not None:
        painter.drawImage(0, 0, layer)
    painter.end()
    return frame


def with_text(frame: QImage, scene: Scene) -> QImage:
    """`frame` (e.g. from a clip) with the scene's words drawn on top."""
    layer = text_layer(scene, frame.width(), frame.height())
    if layer is None:
        return frame
    frame = frame.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(frame)
    painter.drawImage(0, 0, layer)
    painter.end()
    return frame


def frames(scene: Scene, width: int, height: int, count: int) -> Iterator[memoryview]:
    """Raw BGR0 frames for FFmpeg ("-f rawvideo -pix_fmt bgr0").

    Every frame is drawn into the same buffer (a 4K frame is 33 MB), so each one must be written out
    before the next is asked for, as ffmpeg.run does.
    """
    frame = QImage(width, height, QImage.Format.Format_RGB32)
    for n in range(count):
        render_frame(scene, width, height, n / max(1, count - 1), frame)
        yield frame.constBits()
