"""Corrections to an image or clip: rotation, mirroring, cropping and colour.

Clips get them as FFmpeg filters and images through numpy. Both do the same arithmetic in the same
order (turn, mirror, crop, then brightness/contrast/saturation, then the look), so a correction looks
alike on either.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QTransform

from .scenes import NO_ADJUSTMENTS

LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)  # BT.709
_SEPIA = ((0.393, 0.769, 0.189), (0.349, 0.686, 0.168), (0.272, 0.534, 0.131))
# look -> (RGB matrix or None, then (brightness, contrast, saturation))
LOOKS = {
    "none": (None, (0.0, 1.0, 1.0)),
    "bw": (None, (0.0, 1.0, 0.0)),
    "sepia": (_SEPIA, (0.0, 1.0, 1.0)),
    "warm": (((1.10, 0, 0), (0, 1.02, 0), (0, 0, 0.88)), (0.0, 1.0, 1.0)),
    "cool": (((0.90, 0, 0), (0, 1.0, 0), (0, 0, 1.10)), (0.0, 1.0, 1.0)),
    "vivid": (None, (0.0, 1.12, 1.45)),
    "vintage": (
        tuple(tuple(0.6 * (r == c) + 0.4 * _SEPIA[r][c] for c in range(3)) for r in range(3)),
        (0.03, 0.88, 1.0),
    ),
}
ROWS_AT_ONCE = 256  # numpy works through big images in bands of rows, to keep memory low
MIN_KEEP = 0.05  # a crop keeps at least this share of each side


def crop_box(crop: tuple | list, width: int, height: int) -> QRect:
    left, top, right, bottom = (min(max(0.0, float(v)), 0.95) for v in crop)
    keep_w = max(MIN_KEEP, 1 - left - right)
    keep_h = max(MIN_KEEP, 1 - top - bottom)
    return QRect(round(width * left), round(height * top), max(1, round(width * keep_w)), max(1, round(height * keep_h)))


def kept_share(crop: tuple | list) -> float:
    """How much of the shorter kept side remains; an image is decoded that much bigger to stay sharp."""
    left, top, right, bottom = (min(max(0.0, float(v)), 0.95) for v in crop)
    return max(MIN_KEEP, min(1 - left - right, 1 - top - bottom))


def _eq_filter(brightness: float, contrast: float, saturation: float) -> str | None:
    if (brightness, contrast, saturation) == (0.0, 1.0, 1.0):
        return None
    return f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f}"


def filters(adjustments: tuple) -> list[str]:
    """FFmpeg filters for a clip, in the order images get them."""
    if adjustments == NO_ADJUSTMENTS:
        return []
    crop, rotate, flip_h, flip_v, brightness, contrast, saturation, look = adjustments
    out = []
    if rotate == 90:
        out.append("transpose=clock")
    elif rotate == 180:
        out.append("hflip,vflip")
    elif rotate == 270:
        out.append("transpose=cclock")
    if flip_h:
        out.append("hflip")
    if flip_v:
        out.append("vflip")
    if any(crop):
        left, top, right, bottom = (min(max(0.0, float(v)), 0.95) for v in crop)
        w, h = max(MIN_KEEP, 1 - left - right), max(MIN_KEEP, 1 - top - bottom)
        out.append(f"crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{left:.4f}:y=ih*{top:.4f}")
    eq = _eq_filter(brightness, contrast, saturation)
    matrix, look_eq = LOOKS.get(look, LOOKS["none"])
    if eq or matrix or _eq_filter(*look_eq):
        # eq works on the stored values: in TV range (16-235) its brightness step comes out about 1.17 times
        # what images get, so the colour work is done on full-range values, like images.
        out.append("scale=out_range=full")
    if eq:
        out.append(eq)
    if matrix:
        # "rg" is how much of the input's green goes into the output's red, i.e. matrix[0][1].
        mixes = (f"{out_c}{in_c}={matrix[r][c]:.4f}" for r, out_c in enumerate("rgb") for c, in_c in enumerate("rgb"))
        out.append("colorchannelmixer=" + ":".join(mixes))
    look_eq = _eq_filter(*look_eq)
    if look_eq:
        out.append(look_eq)
    return out


def _eq(rgb: np.ndarray, brightness: float, contrast: float, saturation: float) -> np.ndarray:
    """What FFmpeg's eq does: contrast and brightness on the brightness, saturation on the colour."""
    luma = rgb @ LUMA
    shifted = (luma - 0.5) * contrast + 0.5 + brightness
    return shifted[..., None] + (rgb - luma[..., None]) * saturation


def _colour(image: QImage, brightness: float, contrast: float, saturation: float, look: str) -> QImage:
    matrix, look_eq = LOOKS.get(look, LOOKS["none"])
    if (brightness, contrast, saturation) == (0.0, 1.0, 1.0) and matrix is None and look_eq == (0.0, 1.0, 1.0):
        return image
    image = image.convertToFormat(QImage.Format.Format_ARGB32)  # straight alpha, laid out B, G, R, A
    width, height = image.width(), image.height()
    pixels = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())[:, : width * 4]
    pixels = pixels.reshape(height, width, 4)
    mix = np.array(matrix, np.float32).T if matrix else None
    for top in range(0, height, ROWS_AT_ONCE):
        band = pixels[top : top + ROWS_AT_ONCE]
        rgb = band[..., 2::-1].astype(np.float32) / 255  # R, G, B
        rgb = _eq(rgb, brightness, contrast, saturation)
        if mix is not None:
            rgb = np.clip(rgb, 0, 1) @ mix
        if look_eq != (0.0, 1.0, 1.0):
            rgb = _eq(np.clip(rgb, 0, 1), *look_eq)
        band[..., 2::-1] = np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8)
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def apply(image: QImage, adjustments: tuple) -> QImage:
    """The image turned, mirrored, cropped and coloured."""
    if adjustments == NO_ADJUSTMENTS:
        return image
    crop, rotate, flip_h, flip_v, brightness, contrast, saturation, look = adjustments
    if rotate in (90, 180, 270):
        image = image.transformed(QTransform().rotate(rotate))
    if flip_h and flip_v:
        image = image.flipped(Qt.Orientation.Horizontal | Qt.Orientation.Vertical)
    elif flip_h or flip_v:
        image = image.flipped(Qt.Orientation.Horizontal if flip_h else Qt.Orientation.Vertical)
    if any(crop):
        image = image.copy(crop_box(crop, image.width(), image.height()))
    return _colour(image, brightness, contrast, saturation, look)
