"""Downloads ffmpeg.exe and ffprobe.exe into tools/ffmpeg (setup.bat runs this)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# gyan.dev's "essentials" build includes libass (subtitle rendering) and libx264.
URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
TARGET = Path(__file__).resolve().parent / "ffmpeg"
NAMES = ("ffmpeg.exe", "ffprobe.exe")
CHUNK = 1 << 20


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as f:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(CHUNK):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total}% ({done >> 20} / {total >> 20} MB)", end="", flush=True)
    print()


def _works(exe: Path) -> bool:
    """The file runs: an interrupted earlier download can leave a truncated exe behind."""
    try:
        return subprocess.run([str(exe), "-version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    if all(_works(TARGET / name) for name in NAMES):
        print("FFmpeg가 이미 준비되어 있습니다.")
        return 0
    TARGET.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.zip"
        print(f"FFmpeg를 내려받는 중… ({URL})")
        _download(URL, archive)
        found = []
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                folder, _, name = info.filename.rpartition("/")
                if name in NAMES and folder.endswith("/bin"):
                    with z.open(info) as src, (Path(tmp) / name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found.append(name)
        missing = [name for name in NAMES if name not in found]
        if missing:
            print(f"압축 파일에서 찾지 못했습니다: {', '.join(missing)}", file=sys.stderr)
            return 1
        # Fully extracted first, then moved into place, so a broken half-copy is never left in tools/ffmpeg.
        try:
            for name in NAMES:
                os.replace(Path(tmp) / name, TARGET / name)
        except PermissionError:
            print("FFmpeg를 바꿀 수 없습니다. 프로그램을 닫은 뒤 다시 실행해 주세요.", file=sys.stderr)
            return 1
    if not all(_works(TARGET / name) for name in NAMES):
        print("받은 FFmpeg가 실행되지 않습니다. 다시 실행해 주세요.", file=sys.stderr)
        return 1
    print(f"FFmpeg 준비 완료 → {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
