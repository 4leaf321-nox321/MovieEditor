"""Which Desktop Duplication output (FFmpeg's ddagrab output_idx) shows a given point of the desktop.

Qt's screen order and coordinates do not reliably match DXGI's output numbering, so the outputs of the
first graphics adapter (the one ddagrab uses) are asked for their desktop rectangles directly.
Coordinates are physical pixels of the virtual desktop, as Win32 reports them to a DPI-aware app.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass


@dataclass
class Output:
    index: int  # ddagrab's output_idx
    name: str  # e.g. \\.\DISPLAY1
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _OutputDesc(ctypes.Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", ctypes.c_int),
        ("Monitor", wintypes.HMONITOR),
    ]


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_FACTORY1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
# Vtable slots: IUnknown (3) + IDXGIObject (4) come first in every DXGI interface.
_RELEASE = 2
_FACTORY1_ENUM_ADAPTERS1 = 12
_ADAPTER_ENUM_OUTPUTS = 7
_OUTPUT_GET_DESC = 7
_NOT_FOUND = 0x887A0002  # DXGI_ERROR_NOT_FOUND


def _call(obj: ctypes.c_void_p, slot: int, *args, argtypes=()):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
    return fn(obj, *args) & 0xFFFFFFFF


def _release(obj: ctypes.c_void_p) -> None:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)


def outputs() -> list[Output]:
    """The outputs ddagrab can capture, in its output_idx order."""
    factory = ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory)) != 0:
        return []
    found = []
    try:
        adapter = ctypes.c_void_p()
        if _call(factory, _FACTORY1_ENUM_ADAPTERS1, 0, ctypes.byref(adapter), argtypes=(ctypes.c_uint, ctypes.c_void_p)):
            return []
        try:
            index = 0
            while True:
                output = ctypes.c_void_p()
                if _call(adapter, _ADAPTER_ENUM_OUTPUTS, index, ctypes.byref(output), argtypes=(ctypes.c_uint, ctypes.c_void_p)):
                    break  # DXGI_ERROR_NOT_FOUND after the last one
                try:
                    desc = _OutputDesc()
                    if _call(output, _OUTPUT_GET_DESC, ctypes.byref(desc), argtypes=(ctypes.c_void_p,)) == 0:
                        r = desc.DesktopCoordinates
                        found.append(Output(index, desc.DeviceName, r.left, r.top, r.right, r.bottom))
                finally:
                    _release(output)
                index += 1
        finally:
            _release(adapter)
    finally:
        _release(factory)
    return found


def cursor() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def output_at(x: int, y: int) -> Output | None:
    return next((o for o in outputs() if o.contains(x, y)), None)


def exclude_from_capture(win_id: int) -> None:
    """Keep a window (the recording frame and its controls) out of screen recordings (Windows 10 2004+)."""
    WDA_EXCLUDEFROMCAPTURE = 0x11
    ctypes.windll.user32.SetWindowDisplayAffinity(wintypes.HWND(win_id), WDA_EXCLUDEFROMCAPTURE)
