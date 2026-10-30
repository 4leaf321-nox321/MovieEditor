"""Convert text files to speech from the command line.

    python tts_cli.py 원고.txt                         -> 원고.mp3 next to the input
    python tts_cli.py a.txt b.txt -o 결과폴더 --srt    -> 결과폴더/a.mp3, a.srt, b.mp3, b.srt
    python tts_cli.py -t "안녕하세요" -o hello.wav
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from tts_app import pipeline, subtitles, textproc
from tts_app.engine import MAX_SPEED, MIN_SPEED, Engine, list_voices

AUDIO_SUFFIXES = (".mp3", ".wav")


def _output_path(src: Path | None, output: str | None, fmt: str, many: bool) -> Path:
    if output and not many and Path(output).suffix.lower() in AUDIO_SUFFIXES:
        return Path(output)
    stem = src.stem if src else "output"
    folder = Path(output) if output else (src.parent if src else Path.cwd())
    return folder / f"{stem}.{fmt}"


def _resolve_voice(name: str) -> str | None:
    """Accept a voice id (F1, melo:KR, custom:이름) or its label, with or without the star."""
    for voice in list_voices():
        if name in (voice.id, voice.label, voice.label.removeprefix("★ ")):
            return voice.id
    return None


def _progress(index: int, total: int, _text: str) -> None:
    print(f"\r  {index}/{total} 문장", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="텍스트를 음성 파일로 바꿉니다 (오프라인, Supertonic).")
    parser.add_argument("inputs", nargs="*", help="변환할 TXT 파일")
    parser.add_argument("-t", "--text", help="파일 대신 이 글을 변환")
    parser.add_argument("-o", "--output", help="출력 파일(입력이 하나일 때) 또는 출력 폴더")
    parser.add_argument("--format", choices=["mp3", "wav"], default="mp3", help="기본: mp3")
    parser.add_argument("--voice", default="F1", help="목소리 ID나 이름 (목록은 --list-voices)")
    parser.add_argument("--list-voices", action="store_true", help="쓸 수 있는 목소리 목록을 보여 줌")
    parser.add_argument("--lang", default="ko", help="ko, en, ja, ... 또는 auto (문장마다 감지)")
    parser.add_argument("--speed", type=float, default=pipeline.Settings.speed, help=f"{MIN_SPEED}-{MAX_SPEED}")
    parser.add_argument("--pitch", type=float, default=0.0, help="반음 단위, 예: -2, 1.5")
    parser.add_argument("--volume", type=int, default=100, help="퍼센트, 기본 100")
    parser.add_argument("--steps", type=int, default=pipeline.Settings.steps, help="품질 단계 (4 빠름, 8 표준, 16 고품질)")
    parser.add_argument("--sentence-pause", type=float, default=pipeline.Settings.sentence_pause)
    parser.add_argument("--paragraph-pause", type=float, default=pipeline.Settings.paragraph_pause)
    parser.add_argument("--srt", action="store_true", help="자막(SRT)도 저장")
    parser.add_argument("--srt-line", type=int, default=30, help="자막 한 줄 최대 글자 수")
    parser.add_argument("--numbers", choices=["on", "off"], help="숫자 자동 읽기 (14개 → 열네 개). 기본: 읽기 사전 설정대로")
    parser.add_argument("--reading", action="append", default=[], metavar="글=읽기",
                        help="이번에만 다르게 읽을 말, 예: --reading FEM=에프이엠 (여러 번 쓸 수 있음). 자막은 쓴 그대로")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")

    if args.list_voices:
        for voice in list_voices():
            print(f"{voice.id:24s} {voice.label}")
        return 0
    voice = _resolve_voice(args.voice)
    if voice is None:
        parser.error(f"목소리 '{args.voice}'를 찾을 수 없습니다. --list-voices 로 목록을 확인하세요.")
    if not args.inputs and args.text is None:
        parser.error("TXT 파일이나 --text 를 지정하세요.")
    if not MIN_SPEED <= args.speed <= MAX_SPEED:
        parser.error(f"--speed 는 {MIN_SPEED}에서 {MAX_SPEED} 사이여야 합니다.")
    readings = {}
    for item in args.reading:
        written, sep, said = item.partition("=")
        if not sep or not written.strip():
            parser.error(f"--reading 은 글=읽기 모양이어야 합니다 (받은 값: {item})")
        readings[written.strip()] = said.strip()

    settings = pipeline.Settings(
        voice=voice,
        lang=args.lang,
        speed=args.speed,
        pitch=args.pitch,
        volume=args.volume / 100,
        steps=args.steps,
        sentence_pause=args.sentence_pause,
        paragraph_pause=args.paragraph_pause,
        numbers=None if args.numbers is None else args.numbers == "on",
        readings=readings,
    )
    jobs = [(None, args.text)] if args.text is not None else [(Path(p), None) for p in args.inputs]

    print("음성 모델 불러오는 중…")
    try:
        engine = Engine()
    except Exception as e:  # noqa: BLE001 - reported to the user, not a traceback
        print(f"음성 모델을 불러오지 못했습니다: {e}", file=sys.stderr)
        return 1
    failures = 0
    for src, text in jobs:
        out = _output_path(src, args.output, args.format, many=len(jobs) > 1)
        try:
            if text is None:
                text = textproc.read_text_file(src)
            segments = textproc.segment_text(text, args.lang)
            print(f"{src.name if src else '입력한 글'}: {textproc.count_sentences(segments)}문장")
            start = time.perf_counter()
            result = pipeline.render(engine, segments, settings, _progress)
            out.parent.mkdir(parents=True, exist_ok=True)
            pipeline.export_audio(result, out)
            if args.srt:
                subtitles.write_srt(result.cues, out.with_suffix(".srt"), args.srt_line)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest of the batch
            print(f"\n  실패: {e}", file=sys.stderr)
            failures += 1
            continue
        took = time.perf_counter() - start
        print(f"\r  완료 → {out} ({result.duration:.1f}초 분량, {took:.1f}초 걸림)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
