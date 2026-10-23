# PyInstaller spec: one folder holding the desktop app and three console tools (compose from the command
# line, text to speech, the MCP server for Claude) that share the same bundled libraries. Run build.py
# rather than this file directly.

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

APP_NAME = "동영상편집기"
ICON = "assets/app.ico"

# MeloTTS's Korean frontend reads data files at runtime: the vendored g2pkk rules and
# English dictionary, jamo's tables, and the MeCab Korean dictionary.
datas = [(ICON, "assets")]
datas += [(f"tts_app/melo/g2pk/{pattern}", "tts_app/melo/g2pk") for pattern in ("*.txt", "*.csv", "LICENSE")]
datas += collect_data_files("jamo")
binaries, hiddenimports = [], []
for package in ("mecab", "mecab_ko_dic", "mcp", "mcp_types"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
# Loaded by name at run time: anyio's event loop backend, and Pillow, which puts thumbnails in Excel files.
hiddenimports += collect_submodules("anyio._backends") + ["PIL.Image", "PIL.PngImagePlugin"]

shared = dict(datas=datas, binaries=binaries, hiddenimports=hiddenimports, excludes=["tkinter"])
gui = Analysis(["app.py"], **shared)
movie_cli = Analysis(["cli.py"], **shared)
tts_cli = Analysis(["tts_cli.py"], **shared)
mcp = Analysis(["mcp_server.py"], **shared)


def program(analysis, name, console):
    return EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        icon=ICON,
        console=console,
    )


COLLECT(
    program(gui, APP_NAME, console=False),
    gui.binaries,
    gui.datas,
    program(movie_cli, "movie-cli", console=True),
    movie_cli.binaries,
    movie_cli.datas,
    program(tts_cli, "tts-cli", console=True),
    tts_cli.binaries,
    tts_cli.datas,
    program(mcp, "movie-mcp", console=True),  # Claude talks to it over its console's input and output
    mcp.binaries,
    mcp.datas,
    name=APP_NAME,
)
