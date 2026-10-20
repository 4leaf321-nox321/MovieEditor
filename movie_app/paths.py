"""Where the app keeps its files and finds FFmpeg, whether run from source or as the bundled exe.

Every tab (and tts_app) takes its folders from here, so they can never end up in different places.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "동영상편집기"

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent  # the folder holding the exe
    RESOURCE_DIR = Path(sys._MEIPASS)  # bundled read-only files
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = APP_DIR


def _is_writable(folder: Path) -> bool:
    # os.access is unreliable on Windows (it ignores ACLs such as Program Files), so try a write.
    probe = folder / ".write_test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


# Settings, logs and work files sit next to the app so the folder stays portable,
# or under %LOCALAPPDATA% when the app was put somewhere read-only.
DATA_DIR = APP_DIR if _is_writable(APP_DIR) else Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = DATA_DIR / "작업"  # files passed between tabs, the scene cache and the last scene project
FFMPEG_DIR = APP_DIR / "tools" / "ffmpeg"


def tool(name: str) -> str:
    """Path to ffmpeg or ffprobe: the copy in tools/ffmpeg wins, then one on PATH."""
    bundled = FFMPEG_DIR / f"{name}.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name}을(를) 찾을 수 없습니다. tools\\ffmpeg 폴더에 FFmpeg가 있는지 확인해 주세요.")
