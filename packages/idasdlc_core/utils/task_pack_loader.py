"""Helpers for reading task packs from disk."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..models.task_pack import TaskPack


class TaskPackLoaderError(RuntimeError):
    """Raised when a task pack cannot be parsed."""


def load_task_pack(path: str | Path) -> TaskPack:
    """Load a task pack from YAML or JSON."""
    file_path = Path(path)
    if not file_path.exists():  # pragma: no cover - simple guard
        raise TaskPackLoaderError(f"Task pack not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")

    if file_path.suffix.lower() in {".yaml", ".yml"}:
        data: Any = yaml.safe_load(text)
    else:
        data = json.loads(text)

    try:
        return TaskPack.model_validate(data)
    except Exception as exc:  # pragma: no cover - delegated to Pydantic
        raise TaskPackLoaderError(f"Invalid task pack format: {file_path}") from exc
