"""MCP server: Claude (Claude Code, Claude Desktop) makes voices, builds scene projects, looks at frames and
renders videos with this editor, without its window.

The client starts it over stdio:  .venv\\Scripts\\python.exe mcp_server.py

Projects are the same .json files the video tab saves, so a project made here opens there (프로젝트 ▾ →
열기) and the other way round. Work that takes a while runs as a job: a call waits for it a little, and
hands back the job's number when it is not done yet (see job_status).
"""

import dataclasses
import functools
import inspect
import json
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PySide6.QtCore import QBuffer, QIODevice, QSettings, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

from . import autosave, canvas, encoder, formats, graphics, media, paths, render, scene_render, sfx, storyboard, subs, templates
from .media import format_time
from .scenes import (
    CLIP_FILLS,
    EXITS,
    FITS,
    FRAME_RATES,
    KINDS,
    LOGO_CORNERS,
    LOOKS,
    MASK_KINDS,
    MIN_DURATION,
    MOTIONS,
    OVERLAY_EFFECTS,
    RESOLUTIONS,
    ROTATIONS,
    TEXT_EFFECTS,
    TRANSITIONS,
    Keyframe,
    Mask,
    Overlay,
    Project,
    Scene,
    SoundEffect,
    TextBox,
    TextStyle,
    from_dict,
    time_to_voice,
)
from .subs import Cue, SubStyle

FIT_TOLERANCE = 1.43  # as in the video tab: a picture this much wider or taller is shown whole
WAIT = 40.0  # seconds a call waits for its job by default (some clients give up on a call after a minute)
MAX_WAIT = 300.0
PREVIEW_WIDTH = 960
RENDER_SHARE = 0.75  # of a finished video's progress, for making the scenes (the rest puts the sound on)
LOUDNESS_KEYS = ("", *render.LOUDNESS)
FORMAT_KEYS = tuple(f.key for f in formats.FORMATS)

# Allowed values of fields, and the dataclasses lists and nested fields hold, for checking what Claude sends.
CHOICES = {
    (Scene, "kind"): KINDS,
    (Scene, "fit"): FITS,
    (Scene, "motion"): MOTIONS,
    (Scene, "clip_fill"): CLIP_FILLS,
    (Scene, "transition"): ("", *TRANSITIONS),
    (Scene, "look"): LOOKS,
    (Scene, "rotate"): ROTATIONS,
    (TextBox, "effect"): TEXT_EFFECTS,
    (TextBox, "exit"): EXITS,
    (Overlay, "kind"): ("image", "clip"),
    (Overlay, "effect"): OVERLAY_EFFECTS,
    (Overlay, "exit"): EXITS,
    (Mask, "kind"): MASK_KINDS,
    (Project, "resolution"): tuple(RESOLUTIONS),
    (Project, "fps"): FRAME_RATES,
    (Project, "transition"): tuple(TRANSITIONS),
    (Project, "logo_corner"): LOGO_CORNERS,
}
NESTED = {
    (Scene, "texts"): TextBox,
    (Scene, "overlays"): Overlay,
    (Scene, "masks"): Mask,
    (TextBox, "style"): TextStyle,
    (TextBox, "keys"): Keyframe,
    (Overlay, "keys"): Keyframe,
    (Project, "sounds"): SoundEffect,
    (Project, "cues"): Cue,
}
FILE_FIELDS = {(Scene, "source"), (Overlay, "source"), (Project, "music"), (Project, "logo"), (SoundEffect, "source")}
# What only the scene tools change, or what is worked out rather than set.
PROJECT_FIXED = {"scenes", "voice"}
SCENE_FIXED = {"clip_phase", "motion_from", "motion_to"}


def _choices(cls, name: str) -> str:
    return " | ".join(json.dumps(v, ensure_ascii=False) for v in CHOICES[(cls, name)])


FIELD_GUIDE = f"""장면(scene) 필드 — add_scenes / update_scene에 dict로 넣습니다. 넣지 않은 항목은 그대로 둡니다(새 장면은 기본값).
- kind: {_choices(Scene, "kind")} (source를 주면 확장자로 정해짐). "graphic"은 도형·글자·그래프 애니메이션 장면:
    style(스타일 이름, ""=프로젝트 기본)과 layers(그릴 것 목록)로 정합니다. layers는 graphic_guide를 보세요.
- source: 이미지·영상 파일 경로. 바꾸면 맞춤(fit)도 그림 모양에 맞춰 자동으로 정해지고, 회전·잘라내기는 초기화됩니다.
- duration: 장면 길이(초, 최소 {MIN_DURATION})
- 영상 클립: clip_start, clip_end(쓸 구간, 초. clip_end 0 = 끝까지), speed(0.1–8배속), clip_fill({_choices(Scene, "clip_fill")}: 클립이 장면보다 짧을 때 멈춤·반복·속도를 바꿔 꽉 채움), reverse(거꾸로), clip_audio(클립 소리 넣기), clip_volume(0–3)
- fit: "cover"(화면을 채우고 가장자리 잘림) | "contain"(전체 보이기, 빈 곳은 흐린 배경)
- motion(이미지): {_choices(Scene, "motion")}
- color, color2: 글자·색 화면의 배경색 "#rrggbb", color2를 주면 위→아래 그라데이션
- transition: 이 장면에서 다음 장면으로 넘어가는 효과("" = 프로젝트 설정), transition_duration(초, 0 = 프로젝트 설정)
- 보정: crop [왼쪽, 위, 오른쪽, 아래] 잘라낼 비율(0–0.45), rotate({_choices(Scene, "rotate")}, 시계 방향), flip_h, flip_v, brightness(-0.5–0.5), contrast(0.5–2), saturation(0–2), look({_choices(Scene, "look")})
- texts: 글자 목록. 문자열만 넣어도 됩니다. 각 글자 dict:
    text, x, y(글자 덩어리 가운데, 화면 비율 0–1), style{{font, size(1080p 기준 px, 기본 80), bold, color}},
    effect(나타남: {_choices(TextBox, "effect")}), exit(사라짐: {_choices(TextBox, "exit")}), keys, smooth
- overlays: 겹치기(그림·영상) 목록. 각: source, kind("image" | "clip"), x, y(가운데), width(화면 너비 비율), opacity(0–1),
    border(흰 테두리), effect(겹친 영상은 "none"·"fade"만), exit, clip_start(겹친 영상의 시작 초),
    chroma(투명하게 할 색 "#00ff00" 등, "" = 끔), chroma_strength(0–1, 기본 0.25), keys, smooth
- masks: 가리기 목록(얼굴·번호판 등). 각: x, y(가운데), width, height(화면 비율), kind({_choices(Mask, "kind")}), strength(0–1)
- keys(키프레임, 글자·겹치기): [{{"t": 장면 시작부터 초, "x", "y", "scale": 크기 배율}}, ...] 2개 이상이면 그 사이를 움직임.
    smooth(기본 true)는 천천히 출발·도착.
texts·overlays·masks·keys는 주면 목록이 통째로 바뀝니다(하나만 고칠 때도 전체 목록을 주세요. get_project(full=true)로 지금 값을 볼 수 있음).

프로젝트 필드 — update_project:
- resolution: {_choices(Project, "resolution")} (16:9 = 1920×1080, 9:16 = 세로 쇼츠)
- fps: {_choices(Project, "fps")}
- transition: 장면 전환 {_choices(Project, "transition")}, transition_duration(초)
- fade_in, fade_out: 영상 처음·끝 페이드(초, 소리도 같이)
- music(배경 음악 파일, 짧으면 반복), music_volume(0–1, 기본 0.3), music_duck(목소리 나올 때 줄임), music_fade(초)
- logo(로고 그림), logo_corner({_choices(Project, "logo_corner")}), logo_size(화면 너비 비율), logo_opacity
- sounds: 효과음 [{{"source": 파일 또는 효과음 이름(list_sound_effects), "at": 영상 시작부터 초, "volume": 0–2}}]
- cues: 고친 자막 [{{"start", "end", "text"}}], null이면 음성의 자막 파일을 그대로 씀
- lines_per_scene: fit_to_voice가 장면 하나에 묶을 문장 수
- style: 새 그래픽 장면이 쓸 스타일(list_styles)
음성은 fit_to_voice(voice=...)로 바꿉니다."""

INSTRUCTIONS = """동영상 편집기(이 PC의 Windows 프로그램)로 음성·영상을 만드는 도구입니다.

보통 순서:
1. make_voice: 대본을 음성(MP3)과 자막(SRT)으로 만듭니다. list_voices로 목소리를 고릅니다.
2. new_project(voice=...): 음성의 문장마다 빈 장면을 만들고, 장면 길이를 문장에 맞춥니다(장면의 narration에 문장).
3. fill_scenes(이미지·영상을 빈 장면에 차례로), update_scene, add_scenes로 그림·글자·겹치기·가리기를 넣습니다.
   필드는 field_guide로 확인합니다.
   도형·선·화살표·숫자·색 타일(해석 결과 같은)·분포 곡선·막대 그래프 같은 설명 그림은 kind "graphic" 장면의 layers로
   만듭니다(graphic_guide). 색·글꼴·배경·소품·움직임은 스타일(list_styles, style_sheet, save_style)이 정합니다.
   움직임은 키프레임이나 식("0.5+0.1*sin(2*pi*t)"), 여러 개는 group의 count(i번째로 달라지는 식)로 만듭니다.
   레이어로 안 되는 그림은 이 순서로 합니다:
   ① 플러그인(list_plugins). 맞는 것이 없으면, 파일을 쓸 수 있는 Claude(Claude Code)가 플러그인 폴더에 파이썬 파일로
      만들어 둡니다. 한 번 만들면 이후 모든 영상에서 이름으로 다시 씁니다.
   ② 그래도 안 되면 코드로 PNG 묶음이나 투명 영상을 만들어 frames·video 레이어로 넣습니다. 색·글꼴은
      get_style(resolved=true)의 값을 써서 스타일을 맞춥니다.
   영상마다 일회용 그리기 코드(Pillow·MoviePy 등)를 짜는 것보다 이 순서를 먼저 쓰세요.
   같은 종류의 장면(제목, 목록, 큰 숫자, 막대 비교, 해석 결과, 분포, 흐름도, 강조 문장, 사진 설명, 둘 비교)은
   템플릿(list_templates → apply_template)으로 빈칸만 채워 만듭니다. 자주 쓰는 새 틀은 save_template·scene_to_template로 저장.
4. preview_frame(한 순간)·preview_frames(한 장면의 여러 순간)로 화면을 보고 확인합니다.
5. render_video: 장면 + 음성 + 자막 + 배경 음악 + 효과음을 넣은 최종 영상을 만듭니다(MP4, WebM, GIF 등).
다른 영상에 음성·자막만 입힐 때는 compose를 씁니다.

콘티 엑셀에서 시작할 때: read_storyboard(열을 제대로 알아봤는지 확인) → storyboard_script(대본 .txt) →
make_voice(text_file=대본) → storyboard_to_project(voice=그 음성: 장면 길이가 음성에 맞춰짐) → preview_frames → render_video.
만든 뒤에는 project_to_storyboard(썸네일 콘티·소스목록·나레이션 비교 엑셀), storyboard_image(스토리보드 JPG),
export_keyframes(장면 PNG), compare_narration(대본과 실제 음성 비교). 새 콘티 양식은 new_storyboard.

오래 걸리는 작업(make_voice, render_video, compose)은 wait_seconds만큼 기다리고, 끝나지 않았으면 작업 번호를 돌려줍니다.
그러면 job_status로 이어서 기다립니다. 작업은 한 번에 하나씩 차례로 합니다.
상대 경로는 기본 작업 폴더 기준이고, 결과는 따로 정하지 않으면 그 폴더에 저장됩니다. 만든 프로젝트(.json)는 편집기의
② 영상 만들기 탭에서 프로젝트 ▾ → 열기로 열어 손볼 수 있습니다."""

GRAPHIC_GUIDE = f"""그래픽 장면 — kind "graphic"인 장면은 layers 목록을 차례로 그립니다(뒤에 적은 것이 위). style은 스타일 이름.
단위: x, y(가운데), w, h는 화면 비율 0–1 (x·w는 가로, y·h는 세로), r은 짧은 변의 비율. width(선 굵기)·size(글자)·radius·
blur·offset·head_size는 1080p 기준 px(화면 크기가 달라도 같은 비율). 시간(start, end, keys의 t)은 장면 시작부터 초.
색: 스타일 팔레트 이름("accent", "ink" …) 또는 "#rrggbb"·"#rrggbbaa". 뒤에 "/0.5"를 붙이면 반투명. fill·stroke는 false로 끔.

모든 레이어 공통: start, end(보이는 동안), enter(나타남), exit(사라짐), keys, opacity, rotation(도), scale,
  shadow(true 또는 {{color, offset:[x,y], blur}}), ease, name·note(메모)
- enter: {", ".join(graphics.ENTER_EFFECTS[1:])}
    draw=선·도형·곡선·소품을 그려 나감, type=글자를 한 자씩, count=counter 숫자가 올라감, sweep=tiles 칸이 차례로·bars 막대가 차례로
    또는 {{"effect": "draw", "duration": 1.2, "ease": "out_cubic"}} (기본 시간·시간 함수는 스타일 motion)
- exit(end에 맞춰 끝남): {", ".join(graphics.EXIT_EFFECTS[1:])}
- keys: [{{"t": 0, "x": 0.2, "opacity": 0}}, {{"t": 1.5, "x": 0.6, "opacity": 1, "ease": "in_out_cubic"}}]
    숫자 항목(x, y, w, h, r, opacity, rotation, scale, dx, dy, from·to 좌표 등)이 키프레임 사이를 움직임
- ease(시간 함수): linear, smooth, 그리고 in_·out_·in_out_ + {", ".join(graphics.EASE_FAMILIES)} (예: out_cubic, in_out_sine, out_back)

종류별 항목:
- background: preset(스타일의 backgrounds 이름) 또는 color / colors[..] + gradient("linear"|"radial"), angle, center[x,y], radius,
    vignette(0–1), grid{{spacing, color, width, dots}}, grain. 배경 레이어가 없으면 스타일의 default 배경이 깔림
- rect: x, y, w, h, radius, fill, stroke, width, dash[칸, 틈]   - ellipse: x, y, w, h   - circle: x, y, r
- line: points[[x,y],...], smooth(곡선), color, width, dash, head("end"|"start"|"both"), head_size, dx, dy
- arrow: from[x,y], to[x,y](또는 points), bend(-1–1 휘어짐), color, width, head_size, double
- polygon: points, fill, stroke, width
- path: d(SVG 경로 데이터), x, y, w(세로는 viewbox 비율대로, h로 정할 수도), viewbox[0,0,24,24], fill, stroke, width
- text: text, role(스타일 글꼴 역할), size, bold, color, font, x, y, align("center"|"left"|"right": x가 가운데·왼쪽 끝·오른쪽 끝),
    wrap(줄바꿈 폭, 화면 비율), line_height, box(true 또는 {{fill, padding, radius}}), outline{{color, width}}
- counter: from, to, decimals, prefix, suffix, separator + text의 모양 항목(기본 role "number")
- image: file(그림 파일), x, y, w
- prop: name(스타일 소품 이름), x, y, w, color, width(선 굵기), fill
- tiles(해석 결과 같은 색 타일): x, y, w, h, rows, cols, values(2차원 숫자 목록, min·max로 범위) 또는
    field{{kind: {"|".join(graphics.FIELD_KINDS)}, seed, smooth, points[[x,y,세기,퍼짐]], center, angle, frequency, power}},
    colormap(스타일 색 지도 이름 또는 색 목록), gap, radius, legend(true 또는 {{min, max, unit, label}}), order(sweep 순서: diagonal|rows|columns|random)
- curve: x, y, w, h, function{{kind: normal(mean, sd)|lognormal(mu, sigma)|exponential(rate)|uniform(a, b)|sine(frequency, amplitude, phase)|
    linear(slope, intercept)|logistic(k, x0)|power(k)}} 또는 points[[x,y],...], x_range, y_range, samples, color, width,
    fill(true 또는 색), axes, ticks(눈금 수), x_label, y_label, shade[a, b](칠할 구간), shade_color, marker{{x, label, color}}
- bars: x, y, w, h, values, labels, colors(색 하나 또는 목록), colormap, max, gap(0–1), value_labels, decimals, suffix, axes

식 — 숫자 칸(좌표·크기·투명도·시간·데이터 값 등)에 문자열로 쓰면 그때그때 계산됩니다.
  변수: t(장면·그룹 시작부터 초), lt(이 레이어가 나온 뒤 초), d(장면·그룹 길이), p(보이는 동안 0→1),
        i·n(group 안에서 몇 번째·모두 몇 개), col·row(cols를 주면 칸·줄 번호). 상수: pi, tau, e.
  함수: {", ".join(graphics.FUNCTIONS)}, ease("out_cubic", 값). 비교·and·or·"a if 조건 else b"도 됩니다.
  예: "x": "0.1 + i*0.08", "y": "0.5 + 0.05*sin(2*pi*(t - i*0.1))", "opacity": "pingpong(t)", "start": "i*0.15"
- group(또는 repeat): layers(안의 레이어; 안의 시간은 그룹 시작부터), count(복사 수, i=0…n-1), cols(격자로 col·row),
    dx·dy(통째로 옮김), origin[x,y](rotation·scale 중심), 그리고 공통 항목(나타남·키프레임 등)이 통째로 적용
    clip{{type: rect|ellipse|circle|polygon|path|text + 그 모양의 항목, invert}}: 안을 그 모양으로 잘라 보임
    (text면 글자 모양 창, invert면 바깥만)
- 색 칸(fill, stroke, color, 글자 색, box.fill)에 그라데이션: {{"gradient": "linear"|"radial", "colors": [...], "angle": 90,
    "center": [0.5, 0.5], "radius": 0.5, "stops": [0, 1]}} (도형·글자 덩어리의 상자 기준)
- 모든 레이어: glow(true 또는 {{color, blur, strength}}: 빛 번짐), blend({", ".join(graphics.BLENDS)})
- text·counter 추가: spans[{{text, color, bold, size, role, font}}, ...] 또는 markup: true와
    "[accent]강조[/] [b]굵게[/] [size=72]크게[/] [role=title]제목체[/]" ("[["는 대괄호 글자),
    wave{{amplitude(px), speed(초당), spread(글자 사이 어긋남)}}: 글자가 출렁임,
    enter "chars_rise|chars_drop|chars_pop|chars_spin|chars_fade": 한 글자씩 ({{"effect", "duration", "stagger"}})
- frames: folder(그림 폴더, 이름의 숫자 순서) 또는 pattern("C:/작업/f_*.png"), fps, loop, offset(초), x, y, w 또는 fit("cover"|"contain")
- video: file, offset(영상의 몇 초부터), speed, loop, chroma(빼낼 색), chroma_strength, x, y, w 또는 fit
    (투명 영상: MOV ProRes 4444·QuickTime Animation, WebM VP9 알파)
- plugin: plugin(이름, list_plugins) + 그 플러그인이 받는 항목. 나타남·키프레임·회전·크기·그림자·빛 번짐은 엔진이 해 줌

예: [{{"type": "background", "preset": "grid"}},
     {{"type": "text", "text": "굽힘 응력", "role": "title", "y": 0.12, "enter": "slide_up"}},
     {{"type": "tiles", "x": 0.45, "y": 0.5, "w": 0.5, "h": 0.12, "rows": 6, "cols": 40, "field": {{"kind": "bending"}},
       "start": 0.6, "enter": {{"effect": "sweep", "duration": 1.2}}, "legend": {{"min": 0, "max": 182, "unit": " MPa"}}}},
     {{"type": "counter", "from": 0, "to": 182, "suffix": " MPa", "x": 0.2, "y": 0.8, "start": 1.5, "enter": "count"}}]

스타일(save_style) — base(바탕 스타일, 없으면 "{graphics.DEFAULT}")를 주면 바꿀 부분만 적습니다:
- description, colors{{이름: 색}}, fonts{{역할: {{family, size, bold, color}}}}, backgrounds{{이름: background 항목, default가 기본}},
  shape{{fill, stroke, width, radius}}, line{{color, width}}, arrow{{color, width, head}}, text_box{{fill, padding, radius}},
  shadow{{color, offset, blur}}, colormaps{{이름: [색, ...]}}, motion{{ease, enter, duration, exit_duration, stagger, slide}},
  props{{이름: {{"d": SVG 경로(기본 24×24), "mode": "stroke"|"fill", "viewbox": [...]}} 또는 "SVG 파일 경로"}}
- 레이어와 스타일 모두 색·글꼴 역할·소품 이름을 이름으로 부르므로, 스타일만 바꾸면 같은 장면이 다른 모양으로 그려집니다."""

server = MCPServer("movie-editor", title="동영상 편집기", instructions=INSTRUCTIONS)
_project_lock = threading.Lock()  # one change to a project file at a time


def work_dir() -> Path:
    """Where projects and results go when no folder is given (relative paths count from here)."""
    return paths.WORK_DIR / "MCP"


def configure() -> None:
    """Settings for this process: its own scene cache, and the graphics card setting of the app."""
    # Its own cache: a render clears out every piece it did not use, which would empty the app's.
    scene_render.CACHE_DIR = paths.WORK_DIR / "장면캐시_MCP"
    settings = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
    encoder.use_gpu = settings.value("gpu", False, type=bool) and encoder.gpu() is not None


def tool(fn):
    """Register `fn` as a tool; a failure the user can act on is told to Claude in words."""

    def failed(e: Exception):
        traceback.print_exc(file=sys.stderr)
        return ToolError(str(e) or type(e).__name__)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
                raise failed(e) from e

    else:

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
                raise failed(e) from e

    server.add_tool(wrapped, structured_output=False)
    return fn


# ---- paths --------------------------------------------------------------------------------------------


def _path(value: str, suffix: str = "") -> Path:
    path = Path(value.strip().strip('"')).expanduser()
    if not path.is_absolute():
        path = work_dir() / path
    if suffix and path.suffix.lower() != suffix:
        path = path.with_name(path.name + suffix)
    return path


def _file(value: str, what: str) -> Path:
    path = _path(value)
    if not path.is_file():
        raise ToolError(f"{what} 파일을 찾을 수 없습니다: {path}")
    return path


def _output(value: str, default_stem: str, suffix: str) -> Path:
    """Where a result goes: as given (its suffix put right), or in the work folder."""
    if value:
        path = _path(value)
        return path if path.suffix.lower() == suffix else path.with_name(path.name + suffix)
    return work_dir() / f"{default_stem}_{datetime.now():%m%d_%H%M%S}{suffix}"


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in canvas.IMAGE_SUFFIXES:
        return "image"
    if suffix in media.VIDEO_SUFFIXES:
        return "clip"
    return None


# ---- jobs ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Job:
    id: str
    what: str
    state: str = "waiting"  # waiting, running, done, failed, cancelled
    progress: float = 0.0
    result: str = ""
    error: str = ""
    started: float = dataclasses.field(default_factory=time.monotonic)
    finished: float = 0.0
    cancel: bool = False


JOBS: dict[str, Job] = {}
_one_at_a_time = threading.Lock()  # renders share the scene cache and use every core


def _start(what: str, work) -> Job:
    """Run `work(progress, is_cancelled) -> result text` on its own thread, after any job before it."""
    job = Job(uuid.uuid4().hex[:8], what)
    JOBS[job.id] = job

    def report(fraction: float) -> None:
        job.progress = min(1.0, max(job.progress, fraction))

    def run() -> None:
        with _one_at_a_time:
            if job.cancel:
                job.state, job.finished = "cancelled", time.monotonic()
                return
            job.state, job.started = "running", time.monotonic()
            try:
                job.result = work(report, lambda: job.cancel)
                job.state, job.progress = "done", 1.0
            except Exception as e:  # noqa: BLE001 - told to Claude through job_status
                if type(e).__name__ == "Cancelled":  # FFmpeg's or the voice pipeline's
                    job.state = "cancelled"
                else:
                    traceback.print_exc(file=sys.stderr)
                    job.state, job.error = "failed", str(e) or type(e).__name__
            finally:
                job.finished = time.monotonic()

    threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
    return job


def _describe(job: Job) -> str:
    if job.state == "done":
        return job.result
    if job.state == "failed":
        raise ToolError(f"{job.what}에 실패했습니다 (작업 {job.id}).\n{job.error}")
    if job.state == "cancelled":
        return f"{job.what} 작업을 취소했습니다 (작업 {job.id})."
    if job.state == "waiting":
        return f"{job.what}: 앞 작업이 끝나기를 기다리는 중입니다. job_status(\"{job.id}\")로 이어서 기다리세요."
    elapsed = time.monotonic() - job.started
    left = f", 남은 시간 약 {format_time(elapsed * (1 - job.progress) / job.progress)}" if job.progress > 0.03 else ""
    return (f"{job.what} 중: {job.progress * 100:.0f}% ({format_time(elapsed)} 지남{left}). "
            f"job_status(\"{job.id}\")로 이어서 기다리세요.")  # fmt: skip


async def _follow(job: Job, wait: float, ctx: Context | None) -> str:
    end = time.monotonic() + min(max(0.0, wait), MAX_WAIT)
    while job.state in ("waiting", "running") and time.monotonic() < end:
        if ctx is not None:
            try:
                await ctx.report_progress(job.progress, 1.0, f"{job.what} {job.progress * 100:.0f}%")
            except Exception:  # noqa: BLE001 - a client that does not take progress still gets the result
                ctx = None
        await anyio.sleep(0.5)
    return _describe(job)


@tool
async def job_status(job_id: str, ctx: Context, wait_seconds: float = WAIT) -> str:
    """오래 걸리는 작업(make_voice, render_video, compose)의 진행 상황을 보고, 끝날 때까지 최대 wait_seconds초 기다립니다.
    끝났으면 결과(만든 파일 등)를, 실패했으면 이유를 돌려줍니다."""
    job = JOBS.get(job_id.strip())
    if job is None:
        raise ToolError(f"작업 {job_id}이(가) 없습니다. 서버가 다시 시작되었으면 작업을 다시 시작하세요.")
    return await _follow(job, wait_seconds, ctx)


@tool
def cancel_job(job_id: str) -> str:
    """진행 중이거나 기다리는 작업을 취소합니다."""
    job = JOBS.get(job_id.strip())
    if job is None:
        raise ToolError(f"작업 {job_id}이(가) 없습니다.")
    if job.state not in ("waiting", "running"):
        return f"{job.what} 작업({job.id})은 이미 끝났습니다 ({job.state})."
    job.cancel = True
    return f"{job.what} 작업({job.id})을 취소하는 중입니다."


# ---- voices -------------------------------------------------------------------------------------------

_engine = None  # the speech engine, loaded by the first voice job


@tool
def list_voices() -> str:
    """음성 만들기에 쓸 수 있는 목소리 목록 (ID와 이름). ★는 사용자가 만든 목소리입니다."""
    from tts_app.engine import list_voices as voices

    return "\n".join(f"{v.id}\t{v.label}" for v in voices())


def _voice_id(name: str) -> str:
    from tts_app.engine import list_voices as voices

    for voice in voices():
        if name.strip() in (voice.id, voice.label, voice.label.removeprefix("★ ")):
            return voice.id
    raise ToolError(f"목소리 '{name}'를 찾을 수 없습니다. list_voices로 목록을 확인하세요.")


@tool
async def make_voice(
    ctx: Context,
    text: str = "",
    text_file: str = "",
    output: str = "",
    voice: str = "F1",
    lang: str = "ko",
    speed: float = 1.05,
    pitch: float = 0.0,
    volume: int = 100,
    steps: int = 8,
    sentence_pause: float = 0.3,
    paragraph_pause: float = 0.8,
    subtitle_line_chars: int = 30,
    wait_seconds: float = WAIT,
) -> str:
    """대본(text 또는 text_file)을 음성 파일(output: .mp3 또는 .wav)과 같은 이름의 자막(.srt)으로 만듭니다. 인터넷 없이 이 PC에서 합성합니다.
    - 문장마다 자막 한 줄이 생기고, 빈 줄은 문단 사이 쉼(paragraph_pause)이 됩니다. 글 중간의 [쉼 2초]는 그 자리에서 2초 쉽니다.
    - voice: list_voices의 ID나 이름. lang: "ko", "en", "ja" 등 또는 "auto"(문장마다 감지). speed 0.7–2.0, pitch는 반음 단위.
    - steps: 품질 단계(4 빠름, 8 표준, 16 고품질). 숫자·영어 약어는 한글로 풀어 쓰면 더 자연스럽게 읽습니다.
    결과 음성을 new_project(voice=...)나 compose(audio=...)에 넣으면 자막도 같이 쓰입니다."""
    from tts_app import pipeline, subtitles, textproc
    from tts_app.engine import MAX_SPEED, MIN_SPEED

    if text_file:
        text = textproc.read_text_file(_file(text_file, "대본"))
    if not text.strip():
        raise ToolError("text나 text_file로 읽을 글을 주세요.")
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ToolError(f"speed는 {MIN_SPEED}에서 {MAX_SPEED} 사이여야 합니다.")
    out = _output(output, "음성", Path(output).suffix.lower() if Path(output).suffix.lower() in (".mp3", ".wav") else ".mp3")
    settings = pipeline.Settings(voice=_voice_id(voice), lang=lang, speed=speed, pitch=pitch, volume=volume / 100,
                                 steps=steps, sentence_pause=sentence_pause, paragraph_pause=paragraph_pause)  # fmt: skip
    segments = textproc.segment_text(text, lang)
    count = textproc.count_sentences(segments)
    if not count:
        raise ToolError("읽을 문장이 없습니다.")

    def work(progress, cancelled) -> str:
        global _engine
        if _engine is None:
            from tts_app.engine import Engine

            _engine = Engine()
        progress(0.02)
        result = pipeline.render(_engine, segments, settings, lambda i, total, _t: progress(i / max(1, total)), cancelled)
        out.parent.mkdir(parents=True, exist_ok=True)
        pipeline.export_audio(result, out)
        srt = out.with_suffix(".srt")
        subtitles.write_srt(result.cues, srt, subtitle_line_chars)
        note = f"\n읽지 못해 뺀 글자: {' '.join(result.removed_chars)}" if result.removed_chars else ""
        return f"음성: {out}\n자막: {srt}\n길이 {result.duration:.1f}초 · 문장 {count}개 · 자막 {len(result.cues)}줄{note}"

    return await _follow(_start("음성 만들기", work), wait_seconds, ctx)


# ---- files --------------------------------------------------------------------------------------------


@tool
def media_info(path: str) -> str:
    """영상·음성 파일의 길이, 해상도, 초당 프레임, 소리 여부를 알려 줍니다."""
    info = media.probe(_file(path, "미디어"))
    lines = [f"{info.path}", info.describe()]
    if info.has_video:
        lines.append(f"화면 길이 {info.video_duration:.2f}초 · 코덱 {info.video_codec} · {info.pixel_format}")
    lines.append(f"길이 {info.duration:.3f}초 · 화면 {'있음' if info.has_video else '없음'} · 소리 {'있음' if info.has_audio else '없음'}")
    return "\n".join(lines)


@tool
def list_sound_effects() -> str:
    """기본 효과음 이름과 파일. 이름을 update_project의 sounds[].source에 그대로 써도 됩니다(저작권 걱정 없는 직접 만든 소리)."""
    return "\n".join(f"{name}\t{label}\t{sfx.path(name)}" for name, (label, _file_name) in sfx.BUILTIN.items())


@tool
def field_guide() -> str:
    """장면(add_scenes, update_scene)과 프로젝트(update_project)에 넣을 수 있는 항목과 값의 설명."""
    return FIELD_GUIDE


# ---- projects -----------------------------------------------------------------------------------------


def _check(cls, data: dict, where: str) -> None:
    """Unknown names, values off the list and missing files, said in words (the loader would quietly
    put defaults in their place)."""
    if not isinstance(data, dict):
        raise ToolError(f"{where}: 항목은 dict여야 합니다.")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ToolError(f"{where}: 알 수 없는 항목 {unknown}. 쓸 수 있는 항목: {sorted(names)} (field_guide 참고)")
    for name, value in data.items():
        allowed = CHOICES.get((cls, name))
        if allowed is not None and value not in allowed:
            raise ToolError(f"{where}.{name}: {value!r}는 쓸 수 없습니다. 고를 수 있는 값: {list(allowed)}")
        if (cls, name) in FILE_FIELDS and value and not Path(value).is_file():
            raise ToolError(f"{where}.{name}: 파일을 찾을 수 없습니다: {value}")
        inner = NESTED.get((cls, name))
        if inner is None or value is None:
            continue
        if isinstance(value, list):
            for i, item in enumerate(value):
                _check(inner, item, f"{where}.{name}[{i}]")
        else:
            _check(inner, value, f"{where}.{name}")


def _absolute(data: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        if data.get(key):
            data[key] = str(_path(data[key]))


def _absolute_layers(layers) -> None:
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        if layer.get("type") in ("image", "video"):
            _absolute(layer, ("file",))
        elif layer.get("type") == "frames":
            _absolute(layer, ("folder", "pattern"))
        elif layer.get("type") in ("group", "repeat"):
            _absolute_layers(layer.get("layers"))


def _scene_data(fields: dict, where: str) -> dict:
    """What was sent for a scene, with paths made absolute, words given as plain strings made text boxes,
    and checked."""
    data = dict(fields)
    unknown_fixed = SCENE_FIXED & set(data)
    if unknown_fixed:
        raise ToolError(f"{where}: {sorted(unknown_fixed)}는 직접 정할 수 없습니다.")
    _absolute(data, ("source",))
    if isinstance(data.get("texts"), list):
        data["texts"] = [{"text": t} if isinstance(t, str) else t for t in data["texts"]]
    _absolute_layers(data.get("layers"))
    for overlay in data.get("overlays") or []:
        if isinstance(overlay, dict):
            _absolute(overlay, ("source",))
            if overlay.get("source") and "kind" not in overlay:
                overlay["kind"] = "clip" if _kind_of(Path(overlay["source"])) == "clip" else "image"
    _check(Scene, data, where)
    return data


def _auto_fit(project: Project, path: Path, kind: str) -> str:
    width, height = project.size
    try:
        if kind == "image":
            source_w, source_h = canvas.sticker_size(str(path))
        else:
            info = media.probe(path)
            source_w, source_h = info.width, info.height
    except (OSError, ValueError):
        return "cover"
    if not source_w or not source_h:
        return "cover"
    ratio = (source_w / source_h) / (width / height)
    return "cover" if 1 / FIT_TOLERANCE <= ratio <= FIT_TOLERANCE else "contain"


def _clip_length(path: Path) -> float:
    try:
        return media.probe(path).video_duration
    except (OSError, ValueError):
        return 0.0


def _changed_scene(project: Project, scene: Scene, data: dict) -> Scene:
    """`scene` with `data` put in. A new picture is placed the way the video tab places one: its kind from
    the file, fitted to its shape, without the old one's turning and cropping, and a clip (on a scene not
    timed to the voice) as long as the clip."""
    merged = dataclasses.asdict(scene)
    source = data.get("source")
    if source and source != scene.source:
        kind = data.get("kind") or _kind_of(Path(source))
        if kind not in ("image", "clip"):
            raise ToolError(f"이미지나 영상으로 쓸 수 없는 파일입니다: {source}")
        merged.update(kind=kind, clip_start=0.0, clip_end=0.0, fit=_auto_fit(project, Path(source), kind), rotate=0,
                      flip_h=False, flip_v=False, crop=[0.0, 0.0, 0.0, 0.0], clip_phase=0.0)  # fmt: skip
        if kind == "clip" and not scene.narration and "duration" not in data:
            length = _clip_length(Path(source))
            if length:
                merged["duration"] = max(MIN_DURATION, length)
    merged.update(data)
    if merged.get("kind") == "graphic" and "style" not in data and not scene.style:
        merged["style"] = project.style  # a new graphic scene takes the project's style
    new = from_dict(Scene, merged)
    if new.kind == "graphic":
        if new.style and new.style not in graphics.style_names():
            raise ToolError(f"스타일 '{new.style}'이(가) 없습니다. 있는 스타일: {', '.join(graphics.style_names())}")
        problems = graphics.check_layers(new.layers, new.style)
        if problems:
            raise ToolError("그래픽 레이어에 문제가 있습니다 (graphic_guide 참고).\n" + "\n".join(problems[:12]))
    if new.kind in ("image", "clip") and not new.source:
        raise ToolError(f"{'이미지' if new.kind == 'image' else '영상'} 장면에는 source(파일)가 있어야 합니다.")
    return new


def _load(project: str) -> tuple[Path, Project]:
    path = _file(project if project.lower().endswith(".json") else project + ".json", "프로젝트")
    return path, Project.load(path)


def _save(path: Path, project: Project) -> None:
    autosave.write(path, project.to_json())


def _number(project: Project, scene: int) -> int:
    if not 1 <= scene <= len(project.scenes):
        raise ToolError(f"장면 번호는 1부터 {len(project.scenes)}까지입니다 (받은 값: {scene}).")
    return scene - 1


def _voice_cues(voice: Path) -> tuple[list[Cue], float]:
    srt = next((voice.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if voice.with_suffix(s).exists()), None)
    if srt is None:
        raise ToolError(f"음성 옆에 같은 이름의 자막 파일(.srt)이 없어서 문장을 나눌 수 없습니다: {voice}")
    return subs.load_cues(srt), media.probe(voice).duration


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scene_line(number: int, scene: Scene, start: float) -> str:
    parts = [f"{number}. [{start:.2f}–{start + scene.duration:.2f}초]"]
    if scene.kind in ("image", "clip") and scene.source:
        label = "이미지" if scene.kind == "image" else "영상"
        parts.append(f"{label} {Path(scene.source).name} ({scene.fit}{', ' + scene.motion if scene.kind == 'image' else ''})")
        if scene.kind == "clip":
            end = f"{scene.clip_end:g}" if scene.clip_end else "끝"
            extra = (", 거꾸로" if scene.reverse else "") + (", 소리" if scene.clip_audio else "")
            parts.append(f"구간 {scene.clip_start:g}–{end}초, {scene.speed:g}배속, {scene.clip_fill}{extra}")
    elif scene.kind == "graphic":
        kinds = ", ".join(dict.fromkeys(str(layer.get("type")) for layer in scene.layers)) or "배경만"
        parts.append(f"그래픽 (스타일 {scene.style or graphics.DEFAULT}, 레이어 {len(scene.layers)}개: {kinds})")
    elif scene.is_blank:
        parts.append("빈 장면(그림 없음)")
    else:
        parts.append(f"색 화면 {scene.color}{' → ' + scene.color2 if scene.color2 else ''}")
    if scene.texts:
        parts.append("글자 " + ", ".join(f"\"{_short(t.text, 24)}\"" for t in scene.texts))
    if scene.overlays:
        parts.append(f"겹치기 {len(scene.overlays)}개")
    if scene.masks:
        parts.append(f"가리기 {len(scene.masks)}개")
    if scene.adjustments != Scene().adjustments:
        parts.append("보정 있음")
    if scene.transition:
        parts.append(f"다음 전환 {scene.transition}")
    if scene.narration:
        parts.append(f"나레이션 \"{_short(scene.narration, 80)}\"")
    return " · ".join(parts)


def _summary(path: Path, project: Project) -> str:
    width, height = project.size
    lines = [
        f"프로젝트: {path}",
        f"화면 {project.resolution} ({width}×{height}) · {project.frame_rate}fps · 전환 {project.transition} "
        f"{project.transition_duration:g}초 · 길이 {format_time(project.duration, precise=True)} · 장면 {len(project.scenes)}개",
    ]
    extras = []
    if project.voice:
        extras.append(f"음성 {project.voice}" + (" (고친 자막)" if project.cues is not None else ""))
    if project.music:
        extras.append(f"배경 음악 {Path(project.music).name} {project.music_volume:.0%}")
    if project.logo:
        extras.append(f"로고 {Path(project.logo).name}")
    if project.fade_in or project.fade_out:
        extras.append(f"페이드 {project.fade_in:g}/{project.fade_out:g}초")
    if project.sounds:
        extras.append(f"효과음 {len(project.sounds)}개")
    if project.style:
        extras.append(f"그래픽 스타일 {project.style}")
    if extras:
        lines.append(" · ".join(extras))
    start = 0.0
    for i, scene in enumerate(project.scenes, 1):
        lines.append(_scene_line(i, scene, start))
        start += scene.duration
    blanks = [str(i) for i, s in enumerate(project.scenes, 1) if s.is_blank]
    if blanks:
        lines.append(f"빈 장면: {', '.join(blanks)}번 (이대로 만들면 단색 화면이 됩니다)")
    return "\n".join(lines)


@tool
def new_project(
    name: str,
    voice: str = "",
    resolution: str = "16:9",
    fps: int = 30,
    lines_per_scene: int = 1,
    transition: str = "fade",
    transition_duration: float = 0.5,
    overwrite: bool = False,
) -> str:
    """새 장면 프로젝트(.json)를 만듭니다. name은 파일 이름이나 경로입니다.
    voice(음성 파일, 옆에 같은 이름의 .srt 필요)를 주면 문장(lines_per_scene개)마다 빈 장면을 만들고, 장면 길이를
    문장이 시작될 때부터 다음 문장이 시작될 때까지로 맞춥니다. 각 장면의 narration에 그 문장이 들어갑니다.
    resolution: "16:9" | "4K" | "720p" | "9:16"(세로 쇼츠) | "4K-9:16" | "1:1". fps: 24 | 25 | 30 | 50 | 60."""
    path = _path(name, ".json")
    if path.exists() and not overwrite:
        raise ToolError(f"이미 있는 프로젝트입니다: {path} (덮어쓰려면 overwrite=true, 고치려면 update_project)")
    settings = {"resolution": resolution, "fps": fps, "transition": transition,
                "transition_duration": transition_duration, "lines_per_scene": lines_per_scene}  # fmt: skip
    _check(Project, settings, "프로젝트")
    project = from_dict(Project, settings)
    if voice:
        audio = _file(voice, "음성")
        cues, duration = _voice_cues(audio)
        project.voice = str(audio)
        project.scenes = time_to_voice([], cues, duration, lines_per_scene)
    _save(path, project)
    return _summary(path, project)


@tool
def get_project(project: str, full: bool = False) -> str:
    """프로젝트의 설정과 장면 목록(번호, 시간, 그림, 글자, 나레이션)을 보여 줍니다. full=true면 모든 값을 JSON으로 줍니다."""
    path, data = _load(project)
    return data.to_json() if full else _summary(path, data)


@tool
def update_project(project: str, settings: dict) -> str:
    """프로젝트 전체 설정을 바꿉니다: 화면, fps, 전환, 처음·끝 페이드, 배경 음악, 로고, 효과음(sounds), 고친 자막(cues) 등.
    넣을 수 있는 항목은 field_guide에 있습니다. 장면은 add_scenes / update_scene으로, 음성은 fit_to_voice로 바꿉니다."""
    data = dict(settings)
    fixed = PROJECT_FIXED & set(data)
    if fixed:
        raise ToolError(f"{sorted(fixed)}는 update_project로 바꿀 수 없습니다 (장면: add_scenes·update_scene, 음성: fit_to_voice).")
    _absolute(data, ("music", "logo"))
    for sound in data.get("sounds") or []:
        if isinstance(sound, dict) and sound.get("source"):
            name = sound["source"]
            sound["source"] = str(sfx.path(name)) if name in sfx.BUILTIN else str(_path(name))
    _check(Project, data, "프로젝트")
    if data.get("style") and data["style"] not in graphics.style_names():
        raise ToolError(f"style: 스타일 '{data['style']}'이(가) 없습니다. 있는 스타일: {', '.join(graphics.style_names())}")
    with _project_lock:
        path, current = _load(project)
        merged = dataclasses.asdict(current)
        merged.update(data)
        updated = from_dict(Project, merged)
        _save(path, updated)
    return _summary(path, updated)


@tool
def add_scenes(project: str, scenes: list[dict], before: int = 0) -> str:
    """장면을 더합니다. scenes의 각 dict는 장면 필드(field_guide)입니다. 예: {"source": "C:/사진/a.jpg", "duration": 4,
    "texts": ["제목"]}, {"kind": "color", "color": "#000000", "texts": [{"text": "끝", "style": {"size": 120}}]}.
    before: 이 번호의 장면 앞에 넣습니다(0이면 맨 뒤)."""
    with _project_lock:
        path, data = _load(project)
        at = len(data.scenes) if before == 0 else _number(data, before)
        made = []
        for i, fields in enumerate(scenes):
            fields = _scene_data(fields, f"scenes[{i}]")
            made.append(_changed_scene(data, Scene(kind=fields.get("kind", "color")), fields))
        data.scenes[at:at] = made
        _save(path, data)
    return f"장면 {len(made)}개를 {at + 1}번부터 넣었습니다.\n" + _summary(path, data)


@tool
def update_scene(project: str, scene: int | list[int], fields: dict) -> str:
    """장면을 고칩니다. scene은 장면 번호(1부터) 또는 번호 목록(여러 장면에 같은 값). fields는 바꿀 항목만(field_guide).
    source를 바꾸면 그림 모양에 맞게 맞춤(fit)이 자동으로 정해집니다. 나레이션으로 정한 길이는 그대로 둡니다."""
    data = _scene_data(fields, "fields")
    with _project_lock:
        path, current = _load(project)
        numbers = [scene] if isinstance(scene, int) else list(scene)
        for number in numbers:
            index = _number(current, number)
            current.scenes[index] = _changed_scene(current, current.scenes[index], data)
        _save(path, current)
    return _summary(path, current)


@tool
def fill_scenes(project: str, files: list[str], start: int = 1) -> str:
    """이미지·영상 파일을 start번부터 빈 장면(그림 없는 장면)에 차례로 넣고, 빈 장면이 모자라면 뒤에 새 장면으로 붙입니다.
    음성에 맞춰 만든 장면의 길이와 나레이션은 그대로입니다. 파일 순서가 장면 순서입니다."""
    paths_ = [_file(f, "그림") for f in files]
    for path in paths_:
        if _kind_of(path) is None:
            raise ToolError(f"이미지나 영상으로 쓸 수 없는 파일입니다: {path}")
    with _project_lock:
        path, current = _load(project)
        index = max(0, start - 1)
        placed = []
        for file in paths_:
            while index < len(current.scenes) and not current.scenes[index].is_blank:
                index += 1
            if index < len(current.scenes):
                current.scenes[index] = _changed_scene(current, current.scenes[index], {"source": str(file)})
            else:
                current.scenes.append(_changed_scene(current, Scene(), {"source": str(file)}))
            placed.append(f"{index + 1}번 ← {file.name}")
            index += 1
        _save(path, current)
    return "\n".join(placed) + "\n\n" + _summary(path, current)


@tool
def remove_scenes(project: str, scenes: list[int]) -> str:
    """장면 번호 목록의 장면을 지웁니다."""
    with _project_lock:
        path, current = _load(project)
        indices = sorted({_number(current, n) for n in scenes}, reverse=True)
        for index in indices:
            del current.scenes[index]
        _save(path, current)
    return f"장면 {len(indices)}개를 지웠습니다.\n" + _summary(path, current)


@tool
def move_scene(project: str, scene: int, to: int) -> str:
    """scene번 장면을 to번 자리로 옮깁니다. 음성에 맞춘 길이는 옮겨도 그대로이니, 필요하면 fit_to_voice를 다시 부르세요."""
    with _project_lock:
        path, current = _load(project)
        moved = current.scenes.pop(_number(current, scene))
        current.scenes.insert(min(max(0, to - 1), len(current.scenes)), moved)
        _save(path, current)
    return _summary(path, current)


@tool
def fit_to_voice(project: str, voice: str = "", lines_per_scene: int = 0) -> str:
    """장면 길이를 음성의 문장에 맞춥니다(문장 lines_per_scene개마다 장면 하나). 장면의 그림은 순서대로 그대로이고, 문장이 더
    많으면 빈 장면을 더합니다. voice를 주면 그 음성으로 바꿉니다(옆에 같은 이름의 .srt 필요, 고친 자막은 버림)."""
    with _project_lock:
        path, current = _load(project)
        if voice:
            current.voice = str(_file(voice, "음성"))
            current.cues = None  # subtitles edited for the old voice do not fit this one
        if not current.voice:
            raise ToolError("프로젝트에 음성이 없습니다. voice로 음성 파일을 주세요.")
        if lines_per_scene > 0:
            current.lines_per_scene = lines_per_scene
        cues, duration = _voice_cues(_file(current.voice, "음성"))
        if current.cues is not None:
            cues = current.cues
        current.scenes = time_to_voice(current.scenes, cues, duration, current.lines_per_scene)
        _save(path, current)
    return _summary(path, current)


# ---- looking at frames --------------------------------------------------------------------------------


def _scene_at(project: Project, at: float) -> tuple[int, float]:
    start = 0.0
    for i, scene in enumerate(project.scenes):
        if at < start + scene.duration or i == len(project.scenes) - 1:
            return i, min(max(0.0, at - start), scene.duration)
        start += scene.duration
    raise ToolError("장면이 없습니다.")


def _frame(project: Project, at: float, scene: int) -> tuple[int, float, object]:
    if not project.scenes:
        raise ToolError("장면이 없습니다.")
    if scene:
        index = _number(project, scene)
        moment = min(max(0.0, at), project.scenes[index].duration)
    else:
        index, moment = _scene_at(project, at)
    width, height = project.size
    image = scene_render.still(project.scenes[index], width, height, moment, canvas.logo_layer(project, width, height))
    return index, moment, image


@tool
def preview_frame(project: str, at: float = 0.0, scene: int = 0, width: int = PREVIEW_WIDTH) -> list:
    """영상의 한 순간을 그림으로 보여 줍니다(결과와 같은 방식으로 그림: 글자, 겹치기, 가리기, 보정, 로고 포함. 장면 전환과
    처음·끝 페이드, 자막은 빠짐). at: 영상 시작부터 초. scene을 주면 at은 그 장면 시작부터 초. width: 돌려줄 그림의 가로 크기."""
    _path_, data = _load(project)
    index, moment, image = _frame(data, at, scene)
    width = min(max(160, width), image.width())
    image = image.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", 85)
    start = sum(s.duration for s in data.scenes[:index])
    caption = f"{index + 1}번 장면 {moment:.2f}초째 (영상 {start + moment:.2f}초) · {image.width()}×{image.height()}로 줄임"
    return [Image(data=bytes(buffer.data()), format="jpeg"), caption]


@tool
def export_frame(project: str, output: str, at: float = 0.0, scene: int = 0) -> str:
    """영상의 한 순간을 원본 크기 그대로 PNG 또는 JPG 파일로 저장합니다(썸네일 만들기). at·scene은 preview_frame과 같습니다."""
    _path_, data = _load(project)
    index, moment, image = _frame(data, at, scene)
    suffix = Path(output).suffix.lower()
    out = _output(output, "장면", suffix if suffix in (".png", ".jpg", ".jpeg") else ".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(out), quality=95):
        raise ToolError(f"그림을 저장하지 못했습니다: {out}")
    return f"저장했습니다: {out} ({image.width()}×{image.height()}, {index + 1}번 장면 {moment:.2f}초째)"

PLUGIN_GUIDE = """새 플러그인 만들기 — 플러그인 폴더(FOLDER)에 파이썬 파일 하나를 두면 그 파일 이름이 플러그인 이름이 됩니다
(파일을 쓸 수 있는 Claude Code나 사람이 만들고, 이후에는 어디서나 {"type": "plugin", "plugin": 이름, ...}로 씁니다).

    \"\"\"한두 줄 설명 (목록에 나옴)\"\"\"
    KEYS = {"count": "점 개수 (40)", "color": "색 (accent)"}   # 받는 항목과 설명 (검사와 안내에 쓰임)

    def draw(painter, api, layer):
        # painter: 화면 픽셀 단위 QPainter. layer: 레이어 항목 (식은 계산된 값). 아래는 api로 스타일에 맞춰 그리는 예.
        for k in range(int(layer.get("count", 40))):
            x, y = api.x(api.rand(k)), api.y(api.rand(k, 1))
            painter.setBrush(api.colour(layer.get("color", "accent")))
            painter.drawEllipse(QPointF(x, y), api.px(6), api.px(6))

api: width, height, x(비율)·y(비율)→픽셀, px(1080 기준 px)→픽셀, point([x, y]), colour(이름·#색)→QColor,
     brush(색 또는 그라데이션, rect), pen(색, 굵기px, dash), font(role, size, bold)→QFont,
     text(painter, 글자, x, y, role, align, color, size), ease(이름, p), rand(k, seed), noise(x, seed),
     path(SVG d)→QPainterPath, partial(path, 0–1), t(레이어가 나온 뒤 초), env(t, lt, d, p, i, n, col, row),
     enter·p_in·p_out(나타남 진행, "draw"면 p_in만큼 그리기), style(스타일 전체 dict)
나타남·사라짐·키프레임·회전·크기·투명도·그림자·빛 번짐·섞기는 엔진이 해 줍니다. PySide6·numpy·math를 import해 써도 됩니다.
선택: bounds(api, layer) → (x, y, w, h) 화면 비율 (닦아내기·회전 중심용). 파일을 고치면 다음 그릴 때 바로 반영됩니다."""


@tool
def list_plugins() -> str:
    """그래픽 플러그인(파이썬으로 그리는 레이어) 목록과 받는 항목, 그리고 새 플러그인을 만드는 방법(폴더와 파일 모양)."""
    lines = []
    for name in graphics.plugin_names():
        try:
            plugin = graphics.get_plugin(name)
        except ValueError as e:
            lines.append(f"{name}: 불러오지 못했습니다 — {e}")
            continue
        lines.append(f"{name} [{plugin['file'] or '기본 제공'}]: {plugin['doc']}")
        lines += [f"  {key}: {doc}" for key, doc in (plugin["keys"] or {}).items()]
    return "\n".join([*lines, "", PLUGIN_GUIDE.replace("FOLDER", str(graphics.plugins_dir()))])


# ---- graphic styles -----------------------------------------------------------------------------------


def _jpeg(image) -> Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", 85)
    return Image(data=bytes(buffer.data()), format="jpeg")


@tool
def graphic_guide() -> str:
    """그래픽 장면(kind "graphic")의 layers와 스타일 항목 설명: 도형, 선, 화살표, 글자, 숫자, 색 타일, 곡선, 막대, 소품, 나타남·움직임."""
    return GRAPHIC_GUIDE


@tool
def list_styles() -> str:
    """그래픽 스타일 목록과 설명. 스타일마다 색·글꼴·배경·색 지도·소품 이름이 다르니 get_style이나 style_sheet로 확인하세요."""
    lines = []
    for name in graphics.style_names():
        st = graphics.style(name)
        where = "기본 제공" if graphics.is_builtin(name) else f"사용자 ({graphics.styles_dir() / (name + '.json')})"
        lines.append(f"{name} [{where}]: {st.get('description', '')}")
        lines.append(f"  색: {', '.join(st.get('colors', {}))}")
        lines.append(f"  글꼴 역할: {', '.join(st.get('fonts', {}))} · 배경: {', '.join(st.get('backgrounds', {}))}")
        lines.append(f"  색 지도: {', '.join(st.get('colormaps', {}))} · 소품: {', '.join(st.get('props', {}))}")
    return "\n".join(lines)


@tool
def get_style(name: str = "", resolved: bool = False) -> str:
    """스타일 정의(JSON). resolved=false면 그 스타일에 적힌 것만(바탕 스타일에서 바꾼 부분), true면 바탕까지 합친 전체."""
    name = name or graphics.DEFAULT
    data = graphics.style(name) if resolved else graphics.own_style(name)
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool
def save_style(name: str, style: dict, overwrite: bool = False) -> str:
    """그래픽 스타일을 저장합니다(편집기의 스타일 폴더에 JSON). base로 바탕 스타일을 정하면 바꿀 부분만 적으면 됩니다.
    항목은 graphic_guide 맨 아래에 있습니다. 같은 이름의 기본 제공 스타일은 overwrite=true일 때만 덮어씁니다(파일이 우선)."""
    name = name.strip()
    if name in graphics.style_names() and not overwrite:
        raise ToolError(f"이미 있는 스타일입니다: {name} (바꾸려면 overwrite=true)")
    path = graphics.save_style(name, style)
    st = graphics.style(name)
    return (f"저장했습니다: {path}\n색 {len(st.get('colors', {}))}개, 글꼴 역할 {len(st.get('fonts', {}))}개, "
            f"배경 {len(st.get('backgrounds', {}))}개, 색 지도 {len(st.get('colormaps', {}))}개, 소품 {len(st.get('props', {}))}개. "
            f"style_sheet(\"{name}\")로 모양을 확인하세요.")  # fmt: skip


@tool
def delete_style(name: str) -> str:
    """사용자가 저장한 스타일을 지웁니다(기본 제공 스타일은 지울 수 없음)."""
    graphics.delete_style(name.strip())
    return f"지웠습니다: {name}"


@tool
def style_sheet(name: str = "", width: int = 1280) -> list:
    """스타일 한 장 보기: 팔레트, 글꼴 역할, 배경, 색 지도, 소품, 도형·화살표·타일·곡선·막대 견본을 그림으로 돌려줍니다."""
    name = name or graphics.DEFAULT
    image = graphics.style_sheet(name)
    image = image.scaledToWidth(min(max(320, width), image.width()), Qt.TransformationMode.SmoothTransformation)
    return [_jpeg(image), f"스타일 {name} · {image.width()}×{image.height()}로 줄임"]


@tool
def preview_frames(project: str, scene: int, count: int = 6, width: int = 1600) -> list:
    """한 장면의 여러 순간(처음부터 끝까지 고르게 count개)을 한 장의 그림으로 보여 줍니다. 나타남·움직임을 확인할 때 씁니다."""
    _path_, data = _load(project)
    index = _number(data, scene)
    item = data.scenes[index]
    count = min(max(2, count), 12)
    columns = 3 if count > 4 else 2
    rows = -(-count // columns)
    frame_w, frame_h = data.size
    cell_w = max(160, min(width, 2400) // columns)
    cell_h = round(cell_w * frame_h / frame_w)
    sheet = QImage(cell_w * columns, (cell_h + 28) * rows, QImage.Format.Format_RGB32)
    sheet.fill(Qt.GlobalColor.black)
    painter = QPainter(sheet)
    last = max(0.0, item.duration - 1 / data.frame_rate)
    logo = canvas.logo_layer(data, frame_w, frame_h)
    times = [last * k / (count - 1) for k in range(count)]
    for k, at in enumerate(times):
        picture = scene_render.still(item, frame_w, frame_h, at, logo)
        x, y = (k % columns) * cell_w, (k // columns) * (cell_h + 28)
        painter.drawImage(x, y, picture.scaled(cell_w, cell_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(x + 8, y + cell_h + 20, f"{at:.2f}초")
    painter.end()
    note = " (장면 글자 texts의 나타남 효과는 정지 화면에서 빠짐; 그래픽 레이어는 그대로)" if item.texts else ""
    return [_jpeg(sheet), f"{scene}번 장면, {', '.join(f'{t:.2f}' for t in times)}초{note}"]


# ---- templates ---------------------------------------------------------------------------------------------

TEMPLATE_GUIDE = """템플릿 만들기 — 그래픽 장면의 layers에 빈칸을 둔 것입니다 (save_template, 또는 만든 장면을 scene_to_template로).
{"description": "설명", "params": {"title": {"default": "제목", "doc": "큰 글자"}, "items": {"default": ["가", "나"], "doc": "항목들"}},
 "duration": 4, "style": "", "layers": [...]}
- 빈칸: "{title}"처럼 씁니다. 값 전체가 빈칸이면 그 값의 모양(숫자·목록) 그대로 들어가고, 글자나 식 안에서는 글자로 들어갑니다
  (예: "y": "0.3 + {index}*0.1"). 값이 null인 빈칸은 그 항목이 빠집니다.
- "each": "items" — 목록 값의 항목마다 그 레이어를 하나씩: {item}, {index}(0부터), {number}(1부터), {count}
- "if": "subtitle" — 그 값이 있을 때만 그 레이어를 씀 ("!subtitle"은 없을 때만, "{index} < {count} - 1" 같은 식도 됨)
- 색·글꼴·소품은 스타일의 이름으로 쓰면 어느 스타일에서나 어울립니다."""


@tool
def list_templates() -> str:
    """장면 템플릿 목록: 설명과 받는 값(기본값·설명). apply_template로 빈칸만 채워 그래픽 장면을 만듭니다."""
    lines = []
    for name in templates.names():
        data = templates.get(name)
        where = "기본 제공" if templates.is_builtin(name) else str(templates.templates_dir() / f"{name}.json")
        lines.append(f"{name} [{where}]: {data.get('description', '')}")
        for blank, spec in templates.params(data).items():
            lines.append(f"  {blank} = {json.dumps(spec['default'], ensure_ascii=False)} · {spec['doc']}")
    return "\n".join([*lines, "", TEMPLATE_GUIDE])


@tool
def get_template(name: str) -> str:
    """템플릿 정의(JSON): 받는 값과 빈칸이 있는 레이어."""
    return json.dumps(templates.get(name.strip()), ensure_ascii=False, indent=2)


@tool
def save_template(name: str, template: dict, overwrite: bool = False) -> str:
    """템플릿을 저장합니다(편집기의 템플릿 폴더에 JSON). 모양은 list_templates 맨 아래 설명을 보세요. 기본값으로 채워 검사합니다."""
    name = name.strip()
    if name in templates.names() and not overwrite:
        raise ToolError(f"이미 있는 템플릿입니다: {name} (바꾸려면 overwrite=true)")
    path = templates.save(name, template)
    return f"저장했습니다: {path}\n받는 값: {', '.join(templates.params(template)) or '없음'}. preview_template(\"{name}\")로 확인하세요."


@tool
def delete_template(name: str) -> str:
    """사용자가 저장한 템플릿을 지웁니다(기본 제공은 지울 수 없음)."""
    templates.delete(name.strip())
    return f"지웠습니다: {name}"


@tool
def preview_template(name: str, params: dict | None = None, style: str = "", resolution: str = "16:9", at: float = -1.0,
                     width: int = 1280) -> list:  # fmt: skip
    """템플릿을 값으로 채워 한 순간을 그림으로 보여 줍니다(프로젝트 없이). at<0이면 다 나타난 순간."""
    _check_choice("resolution", resolution, tuple(RESOLUTIONS))
    filled = templates.expand(name.strip(), params or {})
    style = style or filled["style"]
    problems = graphics.check_layers(filled["layers"], style)
    if problems:
        raise ToolError("채운 레이어에 문제가 있습니다.\n" + "\n".join(problems[:10]))
    frame_w, frame_h = RESOLUTIONS[resolution]
    moment = graphics.poster_time(filled["layers"], style, filled["duration"]) if at < 0 else min(at, filled["duration"])
    image = graphics.frame(filled["layers"], style, frame_w, frame_h, moment, filled["duration"])
    image = image.scaledToWidth(min(max(320, width), frame_w), Qt.TransformationMode.SmoothTransformation)
    return [_jpeg(image), f"템플릿 {name} · {moment:.2f}초째 · 길이 {filled['duration']:g}초"]


@tool
def apply_template(project: str, template: str, params: dict | None = None, scene: int = 0, before: int = 0, duration: float = 0.0,
                   style: str = "") -> str:  # fmt: skip
    """템플릿을 값으로 채운 그래픽 장면을 넣습니다. scene(번호)을 주면 그 장면을 이 템플릿으로 바꾸고(길이·나레이션은 그대로),
    아니면 before번 앞에(0이면 맨 뒤) 새로 넣습니다. duration을 주면 그 길이로. params는 템플릿이 받는 값(list_templates)."""
    filled = templates.expand(template.strip(), params or {})
    with _project_lock:
        path, data = _load(project)
        style = style or data.style or filled["style"]
        if style and style not in graphics.style_names():
            raise ToolError(f"스타일 '{style}'이(가) 없습니다. 있는 스타일: {', '.join(graphics.style_names())}")
        problems = graphics.check_layers(filled["layers"], style)
        if problems:
            raise ToolError("채운 레이어에 문제가 있습니다.\n" + "\n".join(problems[:10]))
        if scene:
            index = _number(data, scene)
            old = data.scenes[index]
            data.scenes[index] = dataclasses.replace(old, kind="graphic", style=style, layers=filled["layers"], source="",
                                                     duration=duration or old.duration)  # fmt: skip
            where = f"{scene}번 장면을 바꿨습니다"
        else:
            at = len(data.scenes) if before == 0 else _number(data, before)
            data.scenes.insert(at, Scene(kind="graphic", style=style, layers=filled["layers"], duration=duration or filled["duration"]))
            where = f"{at + 1}번에 넣었습니다"
        _save(path, data)
    return f"템플릿 '{template}'으로 {where}.\n" + _summary(path, data)


@tool
def scene_to_template(project: str, scene: int, name: str, replace: dict | None = None, description: str = "",
                      overwrite: bool = False) -> str:  # fmt: skip
    """그래픽 장면을 템플릿으로 저장합니다. replace는 {"장면 속 글자": "빈칸 이름"}: 그 글자가 빈칸이 되고 기본값이 됩니다."""
    _path_, data = _load(project)
    item = data.scenes[_number(data, scene)]
    if item.kind != "graphic":
        raise ToolError("그래픽 장면만 템플릿으로 저장할 수 있습니다.")
    name = name.strip()
    if name in templates.names() and not overwrite:
        raise ToolError(f"이미 있는 템플릿입니다: {name} (바꾸려면 overwrite=true)")
    made = templates.from_layers(item.layers, replace or {}, description, item.duration, item.style)
    path = templates.save(name, made)
    return f"저장했습니다: {path}\n빈칸: {', '.join(templates.params(made)) or '없음 (replace로 글자를 빈칸으로 만들 수 있음)'}"


# ---- storyboards (콘티) -----------------------------------------------------------------------------------


def _board(file: str, sheet: str, columns: dict | None) -> storyboard.Board:
    return storyboard.read(_file(file, "콘티"), sheet, columns)


@tool
def new_storyboard(output: str = "", overwrite: bool = False) -> str:
    """빈 콘티 엑셀 양식을 만듭니다: 장면·나레이션·화면 설명·화면 글자·소스·템플릿·템플릿 값·길이·메모 열과 예시, 도움말 시트."""
    out = _output(output, "콘티", ".xlsx")
    if out.exists() and not overwrite:
        raise ToolError(f"이미 있는 파일입니다: {out} (덮어쓰려면 overwrite=true)")
    storyboard.blank(out)
    return f"만들었습니다: {out}"


@tool
def read_storyboard(file: str, sheet: str = "", columns: dict | None = None) -> str:
    """콘티 엑셀을 읽어 알아본 열과 장면들을 보여 줍니다. 머리글 이름으로 열을 찾습니다(나레이션·대사·스크립트, 화면설명·연출,
    소스·파일, 템플릿, 길이·시간 ...). 틀리게 알아봤으면 columns로 정합니다: {"narration": "C", "visual": "화면 내용", ...}
    (항목: number, narration, visual, text, source, template, params, duration, note, credit)."""
    return storyboard.describe(_board(file, sheet, columns))


@tool
def storyboard_script(file: str, output: str = "", sheet: str = "", columns: dict | None = None) -> str:
    """콘티의 나레이션을 음성용 대본(.txt)으로: 장면마다 한 문단, 나레이션 없는 장면은 그 길이만큼 쉼([쉼 N초]).
    이 대본으로 make_voice를 하고 그 음성을 storyboard_to_project에 주면 장면이 음성에 맞춰집니다."""
    board = _board(file, sheet, columns)
    text = storyboard.script(board)
    out = _output(output, board.file.stem + "_대본", ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return f"대본: {out}\n장면 {len(board.rows)}개 · {len(text)}자\n다음: make_voice(text_file=\"{out}\")\n\n{text[:1500]}"


@tool
def storyboard_to_project(file: str, project: str, sheet: str = "", columns: dict | None = None, voice: str = "",
                          resolution: str = "16:9", fps: int = 30, style: str = "", overwrite: bool = False) -> str:  # fmt: skip
    """콘티 엑셀로 프로젝트를 만듭니다: 줄마다 장면 하나. 소스가 있으면 그 이미지·영상, 템플릿이 있으면 그 그래픽 장면(템플릿 값 칸),
    화면 글자만 있으면 제목 화면, 아무것도 없으면 빈 장면. voice(storyboard_script 대본으로 만든 음성)를 주면 장면 길이를
    음성에 맞추고, 없으면 길이 칸이나 나레이션 길이로 정합니다."""
    path = _path(project, ".json")
    if path.exists() and not overwrite:
        raise ToolError(f"이미 있는 프로젝트입니다: {path} (덮어쓰려면 overwrite=true)")
    settings = {"resolution": resolution, "fps": fps, "style": style}
    _check(Project, settings, "프로젝트")
    if style and style not in graphics.style_names():
        raise ToolError(f"스타일 '{style}'이(가) 없습니다.")
    board = _board(file, sheet, columns)
    made, notes = storyboard.build(board, from_dict(Project, settings), _file(voice, "음성") if voice else None)
    _save(path, made)
    return "\n".join([*notes, "", _summary(path, made)]).strip()


@tool
async def project_to_storyboard(project: str, ctx: Context, output: str = "", thumbnails: bool = True,
                                wait_seconds: float = WAIT) -> str:  # fmt: skip
    """프로젝트를 콘티 엑셀로: 장면마다 썸네일·시작·끝·길이·나레이션·종류·소스·화면 글자, 그리고 소스목록 시트와
    (음성이 있으면) 나레이션 비교 시트."""
    path, data = _load(project)
    out = _output(output, path.stem + "_콘티", ".xlsx")

    def work(progress, cancelled) -> str:
        notes = storyboard.export(data, out, thumbnails, path.stem)
        return "\n".join([f"만들었습니다: {out}", f"장면 {len(data.scenes)}개, 소스 {len(storyboard.sources(data))}개", *notes])

    return await _follow(_start("콘티 만들기", work), wait_seconds, ctx)


@tool
def compare_narration(script: str, voice: str, output: str = "", sheet: str = "", columns: dict | None = None) -> str:
    """대본과 실제 음성을 장면(문단)마다 비교합니다. script: 콘티 엑셀(.xlsx), 대본(.txt, 빈 줄로 문단), 프로젝트(.json, 장면 나레이션).
    voice: 음성 파일(옆의 같은 이름 .srt) 또는 자막 파일. output(.xlsx)을 주면 다른 곳을 색으로 표시한 비교표도 만듭니다."""
    source = _file(script, "대본")
    if source.suffix.lower() in (".xlsx", ".xlsm"):
        narrations = [r.narration for r in _board(script, sheet, columns).rows]
    elif source.suffix.lower() == ".json":
        narrations = [s.narration for s in Project.load(source).scenes]
    else:
        narrations = [" ".join(p.split()) for p in re.split(r"\n\s*\n", source.read_text(encoding="utf-8-sig")) if p.strip()]
    spoken = _file(voice, "음성")
    srt = spoken if spoken.suffix.lower() in subs.SUBTITLE_SUFFIXES else next(
        (spoken.with_suffix(x) for x in subs.SUBTITLE_SUFFIXES if spoken.with_suffix(x).exists()), None)  # fmt: skip
    if srt is None:
        raise ToolError(f"음성 옆에 같은 이름의 자막(.srt)이 없습니다: {spoken}")
    checks = storyboard.compare(narrations, subs.load_cues(srt))
    lines = [f"비교한 문단 {len(checks)}개 · 평균 일치 {sum(c.ratio for c in checks) / max(1, len(checks)) * 100:.1f}%"]
    for c in checks:
        if c.ratio < 0.98:
            lines.append(f"{c.number}. {c.ratio * 100:.0f}% · 대본: {c.written[:80]} / 음성: {c.spoken[:80]}")
    if output:
        out = _output(output, "나레이션비교", ".xlsx")
        from openpyxl import Workbook  # noqa: PLC0415 - only for this

        book = Workbook()
        book.remove(book.active)
        storyboard.write_check(book, checks)
        out.parent.mkdir(parents=True, exist_ok=True)
        book.save(str(out))
        lines.append(f"비교표: {out}")
    return "\n".join(lines)


@tool
def storyboard_image(project: str, output: str = "", columns: int = 4, width: int = 2400) -> list:
    """스토리보드 그림(JPG/PNG): 장면마다 그림과 번호·시간·나레이션을 한 장에. 만든 파일과 줄인 그림을 돌려줍니다."""
    path, data = _load(project)
    suffix = Path(output).suffix.lower() if output else ".jpg"
    out = _output(output, path.stem + "_스토리보드", suffix if suffix in (".jpg", ".jpeg", ".png") else ".jpg")
    sheet = storyboard.contact_sheet(data, path.stem, columns, min(max(800, width), 6000))
    out.parent.mkdir(parents=True, exist_ok=True)
    if not sheet.save(str(out), quality=92):
        raise ToolError(f"그림을 저장하지 못했습니다: {out}")
    shown = sheet.scaledToWidth(min(1600, sheet.width()), Qt.TransformationMode.SmoothTransformation)
    return [_jpeg(shown), f"만들었습니다: {out} ({sheet.width()}×{sheet.height()})"]


@tool
async def export_keyframes(project: str, ctx: Context, folder: str = "", wait_seconds: float = WAIT) -> str:
    """장면마다 대표 순간을 원본 크기 PNG로 저장합니다(장면_01.png ...). 그래픽 장면은 다 나타난 순간."""
    path, data = _load(project)
    where = _path(folder) if folder else work_dir() / f"{path.stem}_장면그림"

    def work(progress, cancelled) -> str:
        made = storyboard.keyframes(data, where)
        return f"저장했습니다: {where} (그림 {len(made)}장)"

    return await _follow(_start("장면 그림 저장", work), wait_seconds, ctx)


# ---- making videos ------------------------------------------------------------------------------------


def _saved_style(changes: dict) -> SubStyle:
    """The subtitle look set in the compose tab, with `changes` on top."""
    store = QSettings(str(paths.DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
    store.beginGroup("compose")
    style = {}
    for f in dataclasses.fields(SubStyle):
        default = getattr(SubStyle(), f.name)
        style[f.name] = store.value(f"style/{f.name}", default, type=type(default))
    unknown = sorted(set(changes) - set(style))
    if unknown:
        raise ToolError(f"subtitle_style: 알 수 없는 항목 {unknown}. 쓸 수 있는 항목: {sorted(style)}")
    style.update(changes)
    return SubStyle(**style)


def _check_choice(name: str, value, allowed) -> None:
    if value not in allowed:
        raise ToolError(f"{name}: {value!r}는 쓸 수 없습니다. 고를 수 있는 값: {list(allowed)}")


def _finished(out: Path, plan_notes: list[str], started: float) -> str:
    info = media.probe(out)
    size = out.stat().st_size / 1_000_000
    lines = [f"만들었습니다: {out}", f"{info.describe()} · {size:.1f}MB · {time.monotonic() - started:.0f}초 걸림"]
    lines += plan_notes
    return "\n".join(lines)


@tool
async def render_video(
    project: str,
    ctx: Context,
    output: str = "",
    format: str = "mp4",
    voice: bool = True,
    subtitles: str = "burn",
    subtitle_style: dict | None = None,
    loudness: str = "",
    quality: str = "standard",
    wait_seconds: float = WAIT,
) -> str:
    """프로젝트를 최종 영상으로 만듭니다: 장면(전환, 글자, 겹치기, 로고) + 음성 + 자막 + 배경 음악 + 효과음 + 처음·끝 페이드.
    - format: "mp4"(H.264), "mp4_hevc"(H.265, 작음), "mov", "mkv", "webm", "gif"(소리 없음), "mp3"·"wav"(소리만)
    - voice: false면 음성과 자막 없이 장면만. subtitles: "burn"(영상에 입힘) | "soft"(켜고 끄는 자막) | "none"
    - subtitle_style: 자막 모양 바꾸기, 예 {"size": 60, "position": "top", "box": true, "highlight": "fill"}
      (기본은 편집기 ③ 탭에 저장된 모양. 항목: font, size, bold, color, outline_color, outline, box, box_color,
      box_opacity, position(bottom|middle|top), margin, highlight(none|fill|word), highlight_color, effect(none|fade|pop|slide))
    - loudness: "" | "youtube"(-14 LUFS) | "podcast"(-16) | "broadcast"(-24) 소리 크기 맞추기
    - quality: "fast" | "standard" | "high". 빈 장면은 단색 화면이 됩니다."""
    _check_choice("format", format, FORMAT_KEYS)
    _check_choice("subtitles", subtitles, ("burn", "soft", "none"))
    _check_choice("loudness", loudness, LOUDNESS_KEYS)
    _check_choice("quality", quality, tuple(render.QUALITIES))
    path, data = _load(project)
    if not data.scenes:
        raise ToolError("장면이 없습니다.")
    style = _saved_style(subtitle_style or {})
    fmt = formats.by_key(format)
    out = _output(output, path.stem, fmt.suffix)
    audio = _file(data.voice, "음성") if voice and data.voice else None
    music = _file(data.music, "배경 음악") if data.music else None

    def work(progress, cancelled) -> str:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="movie_mcp_") as tmp:
            made = Path(tmp) / "장면.mp4"
            # The fades go on in the second step, over the voice and the music too.
            length = scene_render.render(data, made, lambda f: progress(f * RENDER_SHARE), cancelled, fades=False)
            subtitle = None
            if audio is not None and subtitles != "none":
                if data.cues is not None:
                    subtitle = Path(tmp) / "자막.srt"
                    subs.write_srt(data.cues, subtitle)
                else:
                    subtitle = next((audio.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if audio.with_suffix(s).exists()), None)
            voice_length = media.probe(audio).duration if audio is not None else 0.0
            job = render.Job(
                video=made, output=out, audio=audio, subtitle=subtitle,
                fit="audio" if audio is not None and voice_length > length - 0.05 else "video",
                original_volume=1.0, subtitle_mode="soft" if subtitles == "soft" else "burn", style=style,
                quality=quality, music=music, music_volume=data.music_volume, music_duck=data.music_duck,
                music_fade=data.music_fade, effects=scene_render.sound_effects(data), fade_in=data.fade_in,
                fade_out=data.fade_out, loudness=loudness, format=fmt.key,
            )  # fmt: skip
            out.parent.mkdir(parents=True, exist_ok=True)
            plan = render.run(job, lambda f: progress(RENDER_SHARE + f * (1 - RENDER_SHARE)), cancelled)
        notes = list(plan.notes)
        blanks = [str(i) for i, s in enumerate(data.scenes, 1) if s.is_blank]
        if blanks:
            notes.append(f"빈 장면 {', '.join(blanks)}번은 단색 화면으로 만들었습니다.")
        return _finished(out, notes, started)

    return await _follow(_start("영상 만들기", work), wait_seconds, ctx)


@tool
async def compose(
    video: str,
    ctx: Context,
    output: str = "",
    audio: str = "",
    subtitle: str = "",
    music: str = "",
    format: str = "mp4",
    fit: str = "audio",
    extend: str = "freeze",
    delay: float = 0.0,
    tail: float = 0.5,
    voice_volume: float = 1.0,
    original_volume: float = 0.0,
    music_volume: float = 0.3,
    music_duck: bool = True,
    music_fade: float = 2.0,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    loudness: str = "",
    subtitle_mode: str = "burn",
    subtitle_style: dict | None = None,
    quality: str = "standard",
    wait_seconds: float = WAIT,
) -> str:
    """이미 있는 영상(video)에 음성·자막·배경 음악을 입힙니다(편집기 ③ 탭과 같음).
    - subtitle을 비워 두면 음성 옆의 같은 이름 자막(.srt)을 씁니다. "none"이면 자막 없이.
    - fit: "audio"(음성 시작 + 음성 길이 + tail) | "video"(영상 길이). extend: 영상이 짧으면 "freeze"(마지막 장면 멈춤) | "loop"
    - delay: 음성을 몇 초 뒤에 시작(자막도 같이). original_volume: 영상 원래 소리 크기(0이면 뺌)
    - format, loudness, subtitle_style, quality는 render_video와 같습니다. subtitle_mode: "burn" | "soft"."""
    _check_choice("format", format, FORMAT_KEYS)
    _check_choice("fit", fit, ("audio", "video"))
    _check_choice("extend", extend, ("freeze", "loop"))
    _check_choice("loudness", loudness, LOUDNESS_KEYS)
    _check_choice("subtitle_mode", subtitle_mode, ("burn", "soft"))
    _check_choice("quality", quality, tuple(render.QUALITIES))
    video_path = _file(video, "영상")
    audio_path = _file(audio, "음성") if audio else None
    if subtitle.strip().lower() == "none":
        subtitle_path = None
    elif subtitle:
        subtitle_path = _file(subtitle, "자막")
    elif audio_path is not None:
        subtitle_path = next((audio_path.with_suffix(s) for s in subs.SUBTITLE_SUFFIXES if audio_path.with_suffix(s).exists()), None)
    else:
        subtitle_path = None
    music_path = _file(music, "배경 음악") if music else None
    fmt = formats.by_key(format)
    out = _output(output, (audio_path or video_path).stem + "_합성", fmt.suffix)
    job = render.Job(
        video=video_path, output=out, audio=audio_path, subtitle=subtitle_path, fit=fit, extend=extend, delay=delay,
        tail=tail, voice_volume=voice_volume, original_volume=original_volume, subtitle_mode=subtitle_mode,
        style=_saved_style(subtitle_style or {}), quality=quality, music=music_path, music_volume=music_volume,
        music_duck=music_duck, music_fade=music_fade, fade_in=fade_in, fade_out=fade_out, loudness=loudness,
        format=fmt.key,
    )  # fmt: skip

    def work(progress, cancelled) -> str:
        started = time.monotonic()
        out.parent.mkdir(parents=True, exist_ok=True)
        plan = render.run(job, progress, cancelled)
        return _finished(out, list(plan.notes), started)

    return await _follow(_start("합성", work), wait_seconds, ctx)


def main() -> int:
    if QGuiApplication.instance() is None:
        QGuiApplication(["movie-editor-mcp"])  # pictures and fonts are drawn with Qt
    configure()
    work_dir().mkdir(parents=True, exist_ok=True)
    server.run("stdio")
    return 0
