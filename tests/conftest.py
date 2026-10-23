"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path
import pytest

from packages.idasdlc_core import config, db


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use a temp SQLite DB and artifact dirs for each test."""
    db_path = tmp_path / "idasdlc_test.db"
    obj_dir = tmp_path / "artifacts"
    trace_dir = tmp_path / "traces"
    logs_dir = tmp_path / "logs"
    reward_dir = tmp_path / "reward"

    monkeypatch.setenv("IDASDLC_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("IDASDLC_OBJECT_STORE_DIR", str(obj_dir))
    monkeypatch.setenv("IDASDLC_TRACE_STORE_DIR", str(trace_dir))
    monkeypatch.setenv("IDASDLC_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("IDASDLC_REWARD_CARD_DIR", str(reward_dir))

    config.get_settings.cache_clear()
    monkeypatch.setattr(db, "_engine", None)

    settings = config.get_settings()
    settings.ensure_directories()
    db.init_db()

    yield settings

    config.get_settings.cache_clear()
    monkeypatch.setattr(db, "_engine", None)
