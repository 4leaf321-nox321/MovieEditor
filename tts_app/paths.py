"""Where the voice tab finds its models and icon. The folders themselves come from movie_app.paths."""

from __future__ import annotations

from pathlib import Path

from movie_app.paths import APP_DIR, DATA_DIR, RESOURCE_DIR

__all__ = ["APP_DIR", "DATA_DIR", "ICON_PATH", "RESOURCE_DIR", "model_dir"]

ICON_PATH = RESOURCE_DIR / "assets" / "app.ico"


def model_dir(name: str) -> Path:
    """A model shipped next to the app wins; otherwise it is downloaded into DATA_DIR."""
    shipped = APP_DIR / "models" / name
    return shipped if shipped.exists() else DATA_DIR / "models" / name
