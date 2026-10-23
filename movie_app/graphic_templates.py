"""Scene templates that come with the editor (see templates.py). A user's template with the same name wins.

Each is a graphic scene's layers with blanks: "{title}" is filled with the value given (a blank that is a whole
value keeps the value's type, so "{values}" can be a list), "each": "items" repeats a layer for every item
({item}, {index} from 0, {number} from 1, {count}), and "if": "subtitle" keeps a layer only when that value
is set ("if" may also be a formula of the blanks, such as "{index} < {count} - 1").
Colours, fonts and props are the style's names, so a template looks right in every style.
"""

BUILTIN: dict[str, dict] = {
    "제목": {
        "description": "제목 카드: 위에 작은 머리말, 가운데 큰 제목(한 글자씩), 강조선, 아래 부제",
        "params": {
            "title": {"default": "제목", "doc": "큰 제목 ([accent]강조[/] 표시 가능)"},
            "subtitle": {"default": "", "doc": "부제 (없으면 빠짐)"},
            "kicker": {"default": "", "doc": "제목 위 작은 머리말 (없으면 빠짐)"},
        },
        "duration": 3.5,
        "layers": [
            {"type": "text", "if": "kicker", "text": "{kicker}", "role": "label", "color": "accent", "y": 0.36, "enter": "fade",
             "markup": True},  # fmt: skip
            {"type": "text", "text": "{title}", "role": "title", "y": 0.47, "wrap": 0.8, "markup": True,
             "enter": {"effect": "chars_rise", "duration": 0.45, "stagger": 0.03}},  # fmt: skip
            {"type": "line", "points": [[0.42, 0.575], [0.58, 0.575]], "color": "accent", "width": 5, "start": 0.4,
             "enter": {"effect": "draw", "duration": 0.6}},  # fmt: skip
            {"type": "text", "if": "subtitle", "text": "{subtitle}", "role": "body", "color": "muted", "y": 0.65, "start": 0.6,
             "enter": "slide_up", "markup": True, "wrap": 0.8},  # fmt: skip
        ],
    },
    "목록": {
        "description": "목록: 제목과 번호 붙은 항목들이 차례로 나타남 (6개까지 보기 좋음)",
        "params": {
            "heading": {"default": "핵심 정리", "doc": "제목"},
            "items": {"default": ["첫째 항목", "둘째 항목", "셋째 항목"], "doc": "항목들 (목록, 엑셀에서는 a | b | c)"},
        },
        "duration": 5.0,
        "layers": [
            {"type": "text", "text": "{heading}", "role": "heading", "x": 0.1, "y": 0.16, "align": "left", "enter": "slide_up",
             "markup": True},  # fmt: skip
            {"type": "circle", "each": "items", "x": 0.125, "y": "0.31 + {index}*0.11", "r": 0.03, "fill": "accent", "stroke": False,
             "start": "0.4 + {index}*0.35", "enter": "pop"},  # fmt: skip
            {"type": "text", "each": "items", "text": "{number}", "role": "label", "color": "bg", "x": 0.125, "y": "0.31 + {index}*0.11",
             "start": "0.45 + {index}*0.35", "enter": "fade"},  # fmt: skip
            {"type": "text", "each": "items", "text": "{item}", "role": "body", "x": 0.17, "y": "0.31 + {index}*0.11", "align": "left",
             "wrap": 0.75, "markup": True, "start": "0.5 + {index}*0.35", "enter": "slide_left"},  # fmt: skip
        ],
    },
    "큰숫자": {
        "description": "큰 숫자: 0부터 값까지 올라가는 숫자와 단위, 위에 설명, 아래 메모",
        "params": {
            "value": {"default": 182, "doc": "숫자"},
            "unit": {"default": "", "doc": "단위 (예: \" MPa\", \"%\")"},
            "label": {"default": "최대 응력", "doc": "위의 설명"},
            "note": {"default": "", "doc": "아래 메모 (없으면 빠짐)"},
            "decimals": {"default": 0, "doc": "소수 자리"},
            "start_value": {"default": 0, "doc": "세기 시작 값"},
        },
        "duration": 3.0,
        "layers": [
            {"type": "text", "text": "{label}", "role": "heading", "y": 0.3, "enter": "fade", "markup": True},
            {"type": "counter", "from": "{start_value}", "to": "{value}", "decimals": "{decimals}", "suffix": "{unit}", "size": 180,
             "y": 0.52, "start": 0.2, "enter": {"effect": "count", "duration": 1.5}, "glow": {"strength": 0.8}},  # fmt: skip
            {"type": "text", "if": "note", "text": "{note}", "role": "caption", "y": 0.72, "start": 1.4, "enter": "fade", "markup": True},
        ],
    },
    "막대비교": {
        "description": "막대 비교: 제목과 막대 그래프 (막대가 차례로 자라고 값이 붙음)",
        "params": {
            "heading": {"default": "비교", "doc": "제목"},
            "labels": {"default": ["A", "B", "C"], "doc": "막대 이름들 (목록)"},
            "values": {"default": [3, 5, 4], "doc": "값들 (목록)"},
            "suffix": {"default": "", "doc": "값 뒤에 붙일 단위"},
            "decimals": {"default": 0, "doc": "소수 자리"},
            "color": {"default": "accent", "doc": "막대 색"},
        },
        "duration": 4.0,
        "layers": [
            {"type": "text", "text": "{heading}", "role": "heading", "y": 0.13, "enter": "slide_up", "markup": True},
            {"type": "bars", "x": 0.5, "y": 0.57, "w": 0.7, "h": 0.52, "values": "{values}", "labels": "{labels}", "suffix": "{suffix}",
             "decimals": "{decimals}", "colors": "{color}", "start": 0.3, "enter": {"effect": "sweep", "duration": 1.0}},  # fmt: skip
        ],
    },
    "해석결과": {
        "description": "해석 결과: 색 타일(응력·온도 분포처럼)과 범례, 아래 설명",
        "params": {
            "heading": {"default": "해석 결과", "doc": "제목"},
            "field": {"default": "bending", "doc": "값 분포: noise | hotspots | radial | linear | bending | wave"},
            "colormap": {"default": "analysis", "doc": "색 지도 (스타일의 이름)"},
            "min": {"default": 0, "doc": "범례 최솟값"},
            "max": {"default": 182, "doc": "범례 최댓값"},
            "unit": {"default": " MPa", "doc": "범례 단위"},
            "legend": {"default": "응력", "doc": "범례 제목"},
            "caption": {"default": "", "doc": "아래 설명 (없으면 빠짐)"},
            "rows": {"default": 10, "doc": "타일 줄 수"},
            "cols": {"default": 36, "doc": "타일 칸 수"},
        },
        "duration": 4.5,
        "layers": [
            {"type": "text", "text": "{heading}", "role": "heading", "y": 0.12, "enter": "slide_up", "markup": True},
            {"type": "rect", "x": 0.46, "y": 0.5, "w": 0.62, "h": 0.33, "fill": False, "stroke": "line", "width": 2, "radius": 6,
             "enter": {"effect": "draw", "duration": 0.6}},  # fmt: skip
            {"type": "tiles", "x": 0.46, "y": 0.5, "w": 0.6, "h": 0.3, "rows": "{rows}", "cols": "{cols}", "gap": 1,
             "field": {"kind": "{field}"}, "colormap": "{colormap}", "start": 0.5, "enter": {"effect": "sweep", "duration": 1.4},
             "legend": {"min": "{min}", "max": "{max}", "unit": "{unit}", "label": "{legend}"}},  # fmt: skip
            {"type": "text", "if": "caption", "text": "{caption}", "role": "caption", "y": 0.78, "start": 1.6, "enter": "fade",
             "markup": True, "wrap": 0.8},  # fmt: skip
        ],
    },
    "분포": {
        "description": "분포 곡선: 정규분포를 그려 나가고, 구간을 칠하고, 값에 표시",
        "params": {
            "heading": {"default": "분포", "doc": "제목"},
            "mean": {"default": 150, "doc": "평균"},
            "sd": {"default": 15, "doc": "표준편차"},
            "shade": {"default": None, "doc": "칠할 구간 [a, b] (없으면 빠짐)"},
            "marker": {"default": None, "doc": "표시할 값 (없으면 빠짐)"},
            "marker_label": {"default": "", "doc": "표시 이름"},
            "x_label": {"default": "", "doc": "가로축 이름"},
        },
        "duration": 4.0,
        "layers": [
            {"type": "text", "text": "{heading}", "role": "heading", "y": 0.12, "enter": "slide_up", "markup": True},
            {"type": "curve", "x": 0.5, "y": 0.56, "w": 0.7, "h": 0.5, "function": {"kind": "normal", "mean": "{mean}", "sd": "{sd}"},
             "fill": True, "axes": True, "ticks": 5, "x_label": "{x_label}", "shade": "{shade}",
             "marker": {"x": "{marker}", "label": "{marker_label}"}, "start": 0.3, "enter": {"effect": "draw", "duration": 1.5}},  # fmt: skip
        ],
    },
    "흐름": {
        "description": "흐름도: 단계 상자들이 화살표로 이어지며 차례로 나타남 (5개까지 보기 좋음)",
        "params": {
            "heading": {"default": "진행 순서", "doc": "제목"},
            "items": {"default": ["준비", "해석", "검토"], "doc": "단계들 (목록)"},
        },
        "duration": 5.0,
        "layers": [
            {"type": "text", "text": "{heading}", "role": "heading", "y": 0.16, "enter": "slide_up", "markup": True},
            {"type": "rect", "each": "items", "x": "0.1 + 0.8*({index} + 0.5)/{count}", "y": 0.52, "w": "0.58/{count}", "h": 0.2,
             "radius": 18, "fill": "panel", "stroke": "accent", "width": 3, "start": "0.3 + {index}*0.6", "enter": "grow",
             "shadow": True},  # fmt: skip
            {"type": "text", "each": "items", "text": "{item}", "role": "body", "size": 40, "x": "0.1 + 0.8*({index} + 0.5)/{count}",
             "y": 0.52, "wrap": "0.55/{count}", "start": "0.4 + {index}*0.6", "enter": "fade", "markup": True},  # fmt: skip
            {"type": "arrow", "each": "items", "if": "{index} < {count} - 1",
             "from": ["0.1 + 0.8*({index} + 0.5)/{count} + 0.29/{count} + 0.01", 0.52],
             "to": ["0.1 + 0.8*({index} + 1.5)/{count} - 0.29/{count} - 0.01", 0.52],
             "start": "0.6 + {index}*0.6", "enter": {"effect": "draw", "duration": 0.4}},  # fmt: skip
        ],
    },
    "결론": {
        "description": "강조 문장: 왼쪽 강조 막대와 큰 문장, 아래 출처나 덧붙임",
        "params": {
            "text": {"default": "[accent]핵심[/]은 고정단에서 생기는 굽힘 응력입니다.", "doc": "문장 ([accent]강조[/] 표시 가능)"},
            "source": {"default": "", "doc": "아래 작은 글 (없으면 빠짐)"},
        },
        "duration": 4.0,
        "layers": [
            {"type": "rect", "x": 0.11, "y": 0.47, "w": 0.006, "h": 0.28, "radius": 4, "fill": "accent", "stroke": False, "enter": "grow"},
            {"type": "text", "text": "{text}", "role": "heading", "x": 0.14, "y": 0.47, "align": "left", "wrap": 0.74, "markup": True,
             "start": 0.2, "enter": "slide_up"},  # fmt: skip
            {"type": "text", "if": "source", "text": "{source}", "role": "caption", "x": 0.14, "y": 0.68, "align": "left", "start": 0.8,
             "enter": "fade"},  # fmt: skip
        ],
    },
    "사진설명": {
        "description": "사진 설명: 그림이 천천히 커지고 위 제목, 아래 설명 상자",
        "params": {
            "image": {"default": "", "doc": "그림 파일 (없으면 빠짐)"},
            "title": {"default": "", "doc": "위 제목 (없으면 빠짐)"},
            "caption": {"default": "", "doc": "아래 설명 (없으면 빠짐)"},
        },
        "duration": 4.0,
        "layers": [
            {"type": "image", "if": "image", "file": "{image}", "x": 0.5, "y": 0.48, "w": 0.56, "scale": "1 + 0.05*p", "shadow": True,
             "enter": "fade"},  # fmt: skip
            {"type": "text", "if": "title", "text": "{title}", "role": "heading", "y": 0.1, "enter": "slide_up", "markup": True},
            {"type": "text", "if": "caption", "text": "{caption}", "role": "body", "y": 0.86, "box": True, "start": 0.5, "enter": "fade",
             "markup": True, "wrap": 0.8},  # fmt: skip
        ],
    },
    "비교": {
        "description": "둘 비교: 왼쪽·오른쪽 제목과 항목들, 가운데 VS",
        "params": {
            "left_title": {"default": "기존", "doc": "왼쪽 제목"},
            "right_title": {"default": "개선", "doc": "오른쪽 제목"},
            "left_items": {"default": ["무겁다", "느리다"], "doc": "왼쪽 항목들 (목록)"},
            "right_items": {"default": ["가볍다", "빠르다"], "doc": "오른쪽 항목들 (목록)"},
        },
        "duration": 5.0,
        "layers": [
            {"type": "line", "points": [[0.5, 0.2], [0.5, 0.85]], "color": "line", "width": 3, "enter": {"effect": "draw", "duration": 0.6}},
            {"type": "text", "text": "{left_title}", "role": "heading", "color": "accent", "x": 0.27, "y": 0.18, "enter": "slide_up"},
            {"type": "text", "text": "{right_title}", "role": "heading", "color": "accent2", "x": 0.73, "y": 0.18, "start": 0.2,
             "enter": "slide_up"},  # fmt: skip
            {"type": "text", "each": "left_items", "text": "{item}", "role": "body", "x": 0.27, "y": "0.35 + {index}*0.11", "wrap": 0.4,
             "start": "0.6 + {index}*0.3", "enter": "slide_right", "markup": True},  # fmt: skip
            {"type": "text", "each": "right_items", "text": "{item}", "role": "body", "x": 0.73, "y": "0.35 + {index}*0.11", "wrap": 0.4,
             "start": "0.75 + {index}*0.3", "enter": "slide_left", "markup": True},  # fmt: skip
            {"type": "circle", "x": 0.5, "y": 0.52, "r": 0.055, "fill": "bg2", "stroke": "accent", "width": 3, "start": 0.4, "enter": "pop"},
            {"type": "text", "text": "VS", "role": "label", "x": 0.5, "y": 0.52, "start": 0.5, "enter": "fade"},
        ],
    },
}
