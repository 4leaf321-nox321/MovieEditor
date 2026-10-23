"""The styles that come with the editor. A style saved in the 스타일 folder with the same name replaces one.

Every other style is laid over "기본" (and its own "base" first), so a style only needs what it changes.
See graphics.py for what each part means and the units.
"""

DEFAULT = "기본"

BUILTIN: dict[str, dict] = {
    "기본": {
        "description": "어두운 남색 바탕의 기술·해석 설명 스타일. 은은한 그라데이션과 비네트, 해석 결과 같은 색 타일.",
        "colors": {
            "bg": "#0b1324",
            "bg2": "#1b2a47",
            "panel": "#ffffff/0.06",
            "line": "#ffffff/0.14",
            "ink": "#eef3fb",
            "muted": "#94a3b8",
            "accent": "#38bdf8",
            "accent2": "#f59e0b",
            "good": "#34d399",
            "bad": "#f87171",
            "grid": "#ffffff/0.05",
            "shadow": "#000000/0.55",
        },
        "fonts": {
            "title": {"family": "Malgun Gothic", "size": 92, "bold": True, "color": "ink"},
            "heading": {"family": "Malgun Gothic", "size": 64, "bold": True, "color": "ink"},
            "body": {"family": "Malgun Gothic", "size": 46, "bold": False, "color": "ink"},
            "caption": {"family": "Malgun Gothic", "size": 32, "bold": False, "color": "muted"},
            "label": {"family": "Malgun Gothic", "size": 28, "bold": True, "color": "ink"},
            "number": {"family": "Bahnschrift", "size": 120, "bold": True, "color": "accent"},
        },
        "backgrounds": {
            "default": {"gradient": "radial", "colors": ["bg2", "bg"], "center": [0.5, 0.35], "radius": 0.9,
                        "vignette": 0.45, "grain": 0.02},  # fmt: skip
            "grid": {"gradient": "linear", "colors": ["bg2", "bg"], "angle": 90, "vignette": 0.4,
                     "grid": {"spacing": 0.04, "color": "grid", "width": 1}},  # fmt: skip
            "dots": {"gradient": "linear", "colors": ["bg2", "bg"], "angle": 90, "vignette": 0.35,
                     "grid": {"spacing": 0.03, "color": "#ffffff/0.09", "width": 3, "dots": True}},  # fmt: skip
            "plain": {"color": "bg"},
        },
        "shape": {"fill": "panel", "stroke": "line", "width": 2, "radius": 18},
        "line": {"color": "ink", "width": 5},
        "arrow": {"color": "accent", "width": 6, "head": 26},
        "text_box": {"fill": "#000000/0.35", "padding": 22, "radius": 14},
        "shadow": {"color": "shadow", "offset": [0, 10], "blur": 28},
        "glow": {"blur": 26, "strength": 1.0},  # its colour is the layer's own unless given
        "colormaps": {
            "analysis": ["#1e3a8a", "#2563eb", "#06b6d4", "#22c55e", "#eab308", "#f97316", "#dc2626"],
            "heat": ["#111827", "#7f1d1d", "#dc2626", "#f97316", "#fde047", "#fffbeb"],
            "cool": ["#0b1324", "#1e3a8a", "#0ea5e9", "#a5f3fc"],
            "diverging": ["#2563eb", "#93c5fd", "#f1f5f9", "#fca5a5", "#dc2626"],
            "mono": ["bg2", "accent"],
        },
        "motion": {"ease": "out_cubic", "enter": "fade", "duration": 0.6, "exit_duration": 0.4, "stagger": 0.12,
                   "char_stagger": 0.04, "slide": 40},  # fmt: skip
        # Line art in a 24 by 24 box (SVG path data); "stroke" draws its lines, "fill" fills it.
        "props": {
            "check": {"d": "M4 12.5 L9.5 18 L20 6.5", "mode": "stroke"},
            "cross": {"d": "M6 6 L18 18 M18 6 L6 18", "mode": "stroke"},
            "plus": {"d": "M12 4 L12 20 M4 12 L20 12", "mode": "stroke"},
            "arrow_right": {"d": "M3 12 L20 12 M14 6 L20 12 L14 18", "mode": "stroke"},
            "warning": {"d": "M12 3 L22 20.5 L2 20.5 Z M12 9.5 L12 14.5 M12 17.6 L12 17.8", "mode": "stroke"},
            "magnifier": {"d": "M10 3.5 A6.5 6.5 0 1 1 9.99 3.5 Z M14.8 14.8 L21 21", "mode": "stroke"},
            "bulb": {"d": "M9 17 C9 14 6 12.5 6 9 A6 6 0 0 1 18 9 C18 12.5 15 14 15 17 Z M9.5 20 L14.5 20 M10.5 22.5 L13.5 22.5",
                     "mode": "stroke"},  # fmt: skip
            "document": {"d": "M6 2.5 L14 2.5 L19 7.5 L19 21.5 L6 21.5 Z M14 2.5 L14 7.5 L19 7.5 M9 12 L16 12 M9 16 L16 16",
                         "mode": "stroke"},  # fmt: skip
            "person": {"d": "M12 3.5 A3.6 3.6 0 1 1 11.99 3.5 Z M4.5 21 C4.5 15.5 8 13 12 13 C16 13 19.5 15.5 19.5 21",
                       "mode": "stroke"},  # fmt: skip
            "star": {"d": "M12 2 L14.9 8.6 L22 9.3 L16.6 14 L18.2 21 L12 17.3 L5.8 21 L7.4 14 L2 9.3 L9.1 8.6 Z",
                     "mode": "fill"},  # fmt: skip
            "beam": {"d": "M2 6 L22 6 M2 18 L22 18 M12 6 L12 18 M2 5 L2 7 M22 5 L22 7 M2 17 L2 19 M22 17 L22 19",
                     "mode": "stroke"},  # fmt: skip
            "support": {"d": "M12 4 L4 17 L20 17 Z M2 20.5 L22 20.5", "mode": "stroke"},
            "gauge": {"d": "M3.5 17 A9 9 0 1 1 20.5 17 M12 13.5 L16.5 8", "mode": "stroke"},
        },
    },
    "밝게": {
        "base": "기본",
        "description": "흰 바탕의 깔끔한 발표 자료 스타일. 진한 남색 글자와 파란 강조색.",
        "colors": {
            "bg": "#f8fafc",
            "bg2": "#e2e8f0",
            "panel": "#ffffff",
            "line": "#0f172a/0.12",
            "ink": "#0f172a",
            "muted": "#64748b",
            "accent": "#2563eb",
            "accent2": "#ea580c",
            "good": "#16a34a",
            "bad": "#dc2626",
            "grid": "#0f172a/0.06",
            "shadow": "#0f172a/0.16",
        },
        "backgrounds": {
            "default": {"gradient": "linear", "colors": ["bg", "bg2"], "angle": 90, "vignette": 0.08},
        },
        "text_box": {"fill": "#ffffff/0.85"},
        "colormaps": {"mono": ["bg2", "accent"]},
    },
}
