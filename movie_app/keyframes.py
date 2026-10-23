"""Movement across a scene: keyframes of position and size, and the values in between.

A keyframe says where something is (x, y as shares of the frame) and how big (scale, 1 as set) at a
moment of the scene. Between two keyframes the values glide, evenly or easing in and out; before the
first and after the last they stay put. The same arithmetic is written out as FFmpeg expressions for
videos laid over a scene, so the render moves them exactly as the preview does.
"""

from __future__ import annotations

from collections.abc import Sequence


def _progress(p: float, smooth: bool) -> float:
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p) if smooth else p


def sample(keys: Sequence, t: float, smooth: bool) -> tuple[float, float, float] | None:
    """(x, y, scale) at `t` seconds, from keyframes with .t .x .y .scale; None when there are none."""
    if not keys:
        return None
    keys = sorted(keys, key=lambda k: k.t)
    if t <= keys[0].t:
        k = keys[0]
        return k.x, k.y, k.scale
    for a, b in zip(keys, keys[1:]):
        if t <= b.t:
            p = _progress((t - a.t) / (b.t - a.t) if b.t > a.t else 1.0, smooth)
            return a.x + (b.x - a.x) * p, a.y + (b.y - a.y) * p, a.scale + (b.scale - a.scale) * p
    k = keys[-1]
    return k.x, k.y, k.scale


def span(keys: Sequence) -> tuple[float, float] | None:
    """When the movement happens (first to last keyframe), or None when nothing moves."""
    if len(keys) < 2:
        return None
    keys = sorted(keys, key=lambda k: k.t)
    if all((k.x, k.y, k.scale) == (keys[0].x, keys[0].y, keys[0].scale) for k in keys):
        return None
    return keys[0].t, keys[-1].t


def expression(keys: Sequence, value: str, smooth: bool, scale: float = 1.0, offset: float = 0.0) -> str:
    """An FFmpeg expression of `t` giving `value` ("x", "y" or "scale") times `scale` plus `offset`."""
    keys = sorted(keys, key=lambda k: k.t)

    def v(k) -> float:
        return getattr(k, value) * scale + offset

    expr = f"{v(keys[-1]):.4f}"
    for a, b in reversed(list(zip(keys, keys[1:]))):
        length = b.t - a.t
        if length <= 0:
            continue
        p = f"(clip((t-{a.t:.4f})/{length:.4f},0,1))"
        if smooth:
            p = f"({p}*{p}*(3-2*{p}))"
        expr = f"if(lt(t,{b.t:.4f}),{v(a):.4f}+({v(b) - v(a):.4f})*{p},{expr})"
    return f"if(lt(t,{keys[0].t:.4f}),{v(keys[0]):.4f},{expr})"
