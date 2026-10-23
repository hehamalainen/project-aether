"""Core package for ID-ASDLC platform."""
from __future__ import annotations

from importlib import metadata

from .config import Settings, get_settings
from .db import init_db

try:
    __version__ = metadata.version("idasdlc")
except metadata.PackageNotFoundError:  # pragma: no cover - local dev fallback
    __version__ = "0.0.dev0"

__all__ = ["Settings", "get_settings", "init_db", "__version__"]
