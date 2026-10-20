"""Put a voice track and subtitles on a video from the command line.

    python cli.py 영상.mp4 음성.mp3                 -> 음성.mp4 (음성.srt가 옆에 있으면 자막도 입힘)
    python cli.py 영상.mp4 음성.mp3 --srt 자막.srt -o 결과.mp4
    python cli.py 영상.mp4 --srt 자막.srt           -> 자막만 입히기
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from movie_app import media, render
from movie_app.subs import POSITIONS, SubStyle

DEFAULTS = render.Job(Path(), Path())


def _progress(fraction: float) -> None:
    print(f"\r  {fraction * 100:5.1f}%", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="영상에 음성과 자막을 합성합니다 (FFmpeg).")
    parser.add_argument("video", help="바탕 영상")
    parser.add_argument("audio", nargs="?", help="입힐 음성 (MP3, WAV 등)")
    parser.add_argument("--srt", help="자막 파일 (기본: 음성과 같은 이름의 .srt)")
    parser.add_argument("--no-srt", action="store_true", help="자막을 넣지 않음")
    parser.add_argument("-o", "--output", help="결과 MP4 (기본: 음성 이름.mp4)")
    parser.add_argument("--fit", choices=["audio", "video"], default=DEFAULTS.fit, help="결과 길이 기준")
    parser.add_argument("--extend", choices=["freeze", "loop"], default=DEFAULTS.extend,
                        help="영상이 짧을 때: 마지막 장면 멈춤(freeze) 또는 반복(loop)")
    parser.add_argument("--delay", type=float, default=DEFAULTS.delay, help="음성 시작 시점(초), 자막도 함께 이동")
    parser.add_argument("--tail", type=float, default=DEFAULTS.tail, help="음성이 끝난 뒤 남길 시간(초)")
    parser.add_argument("--voice-volume", type=int, default=100, help="음성 볼륨 퍼센트")
    parser.add_argument("--original-volume", type=int, default=0, help="원본 영상 소리 퍼센트 (0이면 뺌)")
    parser.add_argument("--subs", choices=["burn", "soft"], default=DEFAULTS.subtitle_mode,
                        help="burn: 영상에 입힘, soft: 켜고 끄는 자막 트랙")
    parser.add_argument("--font", default=SubStyle.font)
    parser.add_argument("--size", type=int, default=SubStyle.size, help="글자 크기 (1080p 기준 픽셀)")
    parser.add_argument("--position", choices=list(POSITIONS), default=SubStyle.position)
    parser.add_argument("--margin", type=int, default=SubStyle.margin, help="위아래 여백 (1080p 기준 픽셀)")
    parser.add_argument("--box", action="store_true", help="외곽선 대신 반투명 배경 상자")
    parser.add_argument("--quality", choices=list(render.QUALITIES), default=DEFAULTS.quality)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")

    video = Path(args.video)
    audio = Path(args.audio) if args.audio else None
    subtitle = None
    if args.srt and not args.no_srt:
        subtitle = Path(args.srt)
    elif audio and not args.no_srt and audio.with_suffix(".srt").exists():
        subtitle = audio.with_suffix(".srt")
    output = Path(args.output) if args.output else render.default_output(video, audio)
    if audio is None and subtitle is None:
        parser.error("입힐 음성이나 --srt 자막 중 하나는 있어야 합니다.")
    if output.suffix.lower() != ".mp4":
        parser.error("결과는 .mp4 파일로만 저장합니다.")

    job = render.Job(
        video=video,
        output=output,
        audio=audio,
        subtitle=subtitle,
        fit=args.fit,
        extend=args.extend,
        delay=args.delay,
        tail=args.tail,
        voice_volume=args.voice_volume / 100,
        original_volume=args.original_volume / 100,
        subtitle_mode=args.subs,
        style=SubStyle(font=args.font, size=args.size, position=args.position, margin=args.margin, box=args.box),
        quality=args.quality,
    )
    print(f"영상: {video.name}" + (f" · 음성: {audio.name}" if audio else "") + (f" · 자막: {subtitle.name}" if subtitle else ""))
    start = time.perf_counter()
    try:
        plan = render.run(job, _progress)
    except Exception as e:  # noqa: BLE001 - reported to the user, not a traceback
        print(f"\n실패: {e}", file=sys.stderr)
        return 1
    took = time.perf_counter() - start
    print(f"\r  완료 → {output} ({media.format_time(plan.duration, precise=True)} 분량, {took:.1f}초 걸림)")
    for note in plan.notes:
        print(f"  · {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
