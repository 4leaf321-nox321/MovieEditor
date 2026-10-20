r"""Build a folder (and zip) that runs on Windows PCs without Python installed.

    .venv\Scripts\python build.py              dist/동영상편집기/ and dist/동영상편집기.zip
    .venv\Scripts\python build.py --no-melo    leave MeloTTS out (about 590 MB smaller)
    .venv\Scripts\python build.py --no-zip     the folder only
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
APP_NAME = "동영상편집기"  # must match movie.spec
MODELS = {
    # What the app reads from each model folder, plus the licenses that must travel with it.
    "supertonic-3": ["onnx", "voice_styles", "config.json", "LICENSE", "README.md"],
    # Made by 79_TTS's tools/melo_export; MeloTTS cannot be downloaded at runtime.
    "melotts-ko": ["melo_ko.onnx", "bert_ko.onnx", "melo_ko_dur.onnx", "tokenizer.json", "melo.json", "LICENSE-MeloTTS.txt"],
}
FFMPEG = ("ffmpeg.exe", "ffprobe.exe")
# Qt's video playback backend (the scene tab's preview player): checked after the build.
MULTIMEDIA_PLUGIN = Path("_internal/PySide6/plugins/multimedia/ffmpegmediaplugin.dll")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-melo", action="store_true", help="MeloTTS를 넣지 않음")
    parser.add_argument("--no-zip", action="store_true", help="zip 파일을 만들지 않음")
    args = parser.parse_args()

    missing = [name for name in FFMPEG if not (ROOT / "tools" / "ffmpeg" / name).exists()]
    if missing:
        print("tools\\ffmpeg 에 FFmpeg가 없습니다. 먼저 setup.bat 또는 tools\\get_ffmpeg.py 를 실행하세요.")
        return 1

    dist = ROOT / "dist"
    PyInstaller.__main__.run(
        ["--noconfirm", "--clean", "--distpath", str(dist), "--workpath", str(ROOT / "build"), str(ROOT / "movie.spec")]
    )
    out = dist / APP_NAME

    for model, files in MODELS.items():
        if model == "melotts-ko" and args.no_melo:
            continue
        src = ROOT / "models" / model
        if not all((src / name).exists() for name in files[:2]):
            print(f"모델이 {src} 에 없습니다. 모델 폴더를 준비하거나 --no-melo 로 빌드하세요.")
            return 1
        dst = out / "models" / model
        dst.mkdir(parents=True, exist_ok=True)
        for name in files:
            item = src / name
            if item.is_dir():
                shutil.copytree(item, dst / name, dirs_exist_ok=True)
            elif item.exists():
                shutil.copy2(item, dst / name)

    # FFmpeg goes next to the exe, where paths.tool() looks for it.
    (out / "tools" / "ffmpeg").mkdir(parents=True, exist_ok=True)
    for name in FFMPEG:
        shutil.copy2(ROOT / "tools" / "ffmpeg" / name, out / "tools" / "ffmpeg" / name)
    shutil.copy2(ROOT / "README.md", out / "README.md")

    if not (out / MULTIMEDIA_PLUGIN).exists():
        print(f"경고: 영상 재생 모듈이 빠졌습니다 ({MULTIMEDIA_PLUGIN}). 장면 미리보기 재생이 안 될 수 있습니다.")

    print(f"\n완성: {out / (APP_NAME + '.exe')}")
    if not args.no_zip:
        print("zip 만드는 중…")
        archive = shutil.make_archive(str(dist / APP_NAME), "zip", dist, APP_NAME)
        print(f"배포용 zip: {archive} ({Path(archive).stat().st_size / 2**20:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
