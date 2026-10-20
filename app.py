"""Launches the desktop app. Double-click run.bat, or run app.py with the .venv Python."""

import os
import sys

# pythonw.exe has no console, but the voice model downloader writes progress bars to it.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

from movie_app.main_window import main  # noqa: E402

if __name__ == "__main__":
    main()
