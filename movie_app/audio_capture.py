"""Sound for screen recordings: the microphone (through sounddevice) and what the computer plays (Windows'
WASAPI loopback, called through ctypes).

Each source goes to its own WAV file as it comes in, together with the moment (time.perf_counter) its
first sample was heard, so the sound can be lined up with the recorded picture afterwards.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import uuid
from ctypes import POINTER, byref, c_int64, c_uint32, c_uint64, c_void_p, wintypes
from pathlib import Path

import numpy as np
import soundfile

POLL = 0.01  # seconds between looks for new loopback sound
BUFFER = 10_000_000  # 1 s, in WASAPI's 100-nanosecond units
GAP = 0.03  # silence longer than this (nothing playing) is written out as zeros


class _Capture:
    def __init__(self, path: Path):
        self.path = path
        self.start_time: float | None = None  # perf_counter of the file's first sample
        self.rate = 48000
        self.error = ""  # nothing usable was recorded
        self.note = ""  # recorded, but with something to tell (it stopped early)

    @property
    def ok(self) -> bool:
        return not self.error and self.start_time is not None and self.path.exists()


class MicCapture(_Capture):
    """The default microphone."""

    def start(self) -> None:
        import sounddevice as sd  # its PortAudio library is loaded only when sound is recorded

        try:
            device = sd.query_devices(kind="input")
            self.rate = int(device["default_samplerate"]) or 48000
            channels = min(2, int(device["max_input_channels"])) or 1
            self._queue: queue.Queue = queue.Queue()
            self._file = soundfile.SoundFile(self.path, "w", samplerate=self.rate, channels=channels, subtype="PCM_16")
            self._writer = threading.Thread(target=self._write, daemon=True)
            self._writer.start()
            self._stream = sd.InputStream(samplerate=self.rate, channels=channels, dtype="float32", callback=self._heard)
            self._stream.start()
        except Exception as e:  # noqa: BLE001 - recorded without it, and said so
            self.error = f"마이크: {e}"
            self._close()

    def _heard(self, data, frames, times, status) -> None:
        if self.start_time is None:
            # How long ago the first sample of this block was picked up, from PortAudio's clock when it tells.
            age = times.currentTime - times.inputBufferAdcTime
            self.start_time = time.perf_counter() - (age if 0 < age < 1 else frames / self.rate)
        self._queue.put(data.copy())

    def _write(self) -> None:
        while (block := self._queue.get()) is not None:
            self._file.write(block)

    def stop(self) -> None:
        if self.error:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._close()

    def _close(self) -> None:
        if getattr(self, "_writer", None) is not None:
            self._queue.put(None)
            self._writer.join(5)
            self._writer = None
        if getattr(self, "_file", None) is not None:
            self._file.close()
            self._file = None


# ---- WASAPI loopback ----------------------------------------------------------


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _WaveFormat(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


CLSID_MM_DEVICE_ENUMERATOR = _GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_MM_DEVICE_ENUMERATOR = _GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_AUDIO_CLIENT = _GUID.of("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_AUDIO_CAPTURE_CLIENT = _GUID.of("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
FLOAT_SUBTYPE = uuid.UUID("00000003-0000-0010-8000-00AA00389B71").bytes_le
CLSCTX_ALL = 0x17
E_RENDER, E_CONSOLE = 0, 0
LOOPBACK = 0x00020000  # AUDCLNT_STREAMFLAGS_LOOPBACK
SILENT = 0x2  # AUDCLNT_BUFFERFLAGS_SILENT
WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_EXTENSIBLE = 3, 0xFFFE


def _call(obj: c_void_p, slot: int, *args, argtypes=()) -> int:
    """Call method number `slot` of a COM object's table; a failure raises OSError."""
    table = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    method = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(table[slot])
    return method(obj, *args)


def _release(obj: c_void_p) -> None:
    if obj:
        table = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
        ctypes.WINFUNCTYPE(c_uint32, c_void_p)(table[2])(obj)


class LoopbackCapture(_Capture):
    """Whatever the default speakers play. Silence (nothing playing) comes as gaps, filled in by time."""

    def start(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if getattr(self, "_thread", None) is None:
            return
        self._stop.set()
        self._thread.join(5)
        self._thread = None

    def _run(self) -> None:
        ole = ctypes.windll.ole32
        ole.CoInitializeEx(None, 0)
        enumerator, device, client, capture = c_void_p(), c_void_p(), c_void_p(), c_void_p()
        fmt = POINTER(_WaveFormat)()
        out = None
        try:
            ole.CoCreateInstance(byref(CLSID_MM_DEVICE_ENUMERATOR), None, CLSCTX_ALL, byref(IID_MM_DEVICE_ENUMERATOR),
                                 byref(enumerator))  # fmt: skip
            if not enumerator:
                raise OSError("소리 장치를 찾지 못했습니다")
            _call(enumerator, 4, E_RENDER, E_CONSOLE, byref(device), argtypes=(c_uint32, c_uint32, POINTER(c_void_p)))
            _call(device, 3, byref(IID_AUDIO_CLIENT), CLSCTX_ALL, None, byref(client),
                  argtypes=(POINTER(_GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)))  # fmt: skip
            _call(client, 8, byref(fmt), argtypes=(POINTER(POINTER(_WaveFormat)),))
            wave = fmt.contents
            channels, self.rate, block = wave.nChannels, wave.nSamplesPerSec, wave.nBlockAlign
            is_float = wave.wFormatTag == WAVE_FORMAT_IEEE_FLOAT or (
                wave.wFormatTag == WAVE_FORMAT_EXTENSIBLE and ctypes.string_at(ctypes.addressof(wave) + 24, 16) == FLOAT_SUBTYPE
            )
            if not (is_float and wave.wBitsPerSample == 32) and wave.wBitsPerSample != 16:
                raise OSError(f"지원하지 않는 소리 형식입니다 ({wave.wBitsPerSample}비트)")
            _call(client, 3, 0, LOOPBACK, BUFFER, 0, fmt, None,
                  argtypes=(c_uint32, wintypes.DWORD, c_int64, c_int64, POINTER(_WaveFormat), c_void_p))  # fmt: skip
            _call(client, 14, byref(IID_AUDIO_CAPTURE_CLIENT), byref(capture), argtypes=(POINTER(_GUID), POINTER(c_void_p)))
            out = soundfile.SoundFile(self.path, "w", samplerate=self.rate, channels=channels, subtype="PCM_16")
            _call(client, 10)  # Start
            self.start_time = time.perf_counter()
            self._ready.set()
            written = 0
            data, frames, flags = POINTER(ctypes.c_byte)(), c_uint32(), wintypes.DWORD()
            position, qpc, waiting = c_uint64(), c_uint64(), c_uint32()
            dtype = np.float32 if is_float and wave.wBitsPerSample == 32 else np.int16

            def silence_until(moment: float) -> None:
                nonlocal written
                missing = int((moment - self.start_time) * self.rate) - written
                if missing > GAP * self.rate:
                    out.write(np.zeros((missing, channels), np.float32))
                    written += missing

            while not self._stop.is_set():
                _call(capture, 5, byref(waiting), argtypes=(POINTER(c_uint32),))
                if not waiting.value:
                    silence_until(time.perf_counter() - 0.1)  # nothing is playing; keep the file in step
                    time.sleep(POLL)
                    continue
                while waiting.value:
                    _call(capture, 3, byref(data), byref(frames), byref(flags), byref(position), byref(qpc),
                          argtypes=(POINTER(POINTER(ctypes.c_byte)), POINTER(c_uint32), POINTER(wintypes.DWORD),
                                    POINTER(c_uint64), POINTER(c_uint64)))  # fmt: skip
                    count = frames.value
                    silence_until(qpc.value / 1e7)  # the packet's first frame was heard then
                    if flags.value & SILENT:
                        samples = np.zeros((count, channels), np.float32)
                    else:
                        raw = np.frombuffer(ctypes.string_at(data, count * block), dtype).reshape(count, channels)
                        samples = raw.astype(np.float32) / (32768.0 if dtype == np.int16 else 1.0)
                    out.write(samples)
                    written += count
                    _call(capture, 4, count, argtypes=(c_uint32,))
                    _call(capture, 5, byref(waiting), argtypes=(POINTER(c_uint32),))
            silence_until(time.perf_counter())
            _call(client, 11)  # Stop
        except OSError as e:
            if self.start_time is None:
                self.error = f"컴퓨터 소리: {e}"
            else:
                # The speakers were unplugged or switched mid-recording: what came before is kept.
                self.note = "녹화 중에 소리 장치가 바뀌어 그 뒤의 컴퓨터 소리는 빠졌습니다."
        finally:
            self._ready.set()
            if out is not None:
                out.close()
            if fmt:
                ole.CoTaskMemFree(fmt)
            for obj in (capture, client, device, enumerator):
                _release(obj)
            ole.CoUninitialize()


def has_microphone() -> bool:
    try:
        import sounddevice as sd

        return sd.query_devices(kind="input")["max_input_channels"] > 0
    except Exception:  # noqa: BLE001 - no device, or no sound system at all
        return False
