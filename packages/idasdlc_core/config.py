"""Application configuration loaded from environment."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration surface for the ID-ASDLC platform."""

    model_config = SettingsConfigDict(
        env_prefix="IDASDLC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./idasdlc.db"
    object_store_dir: Path = Path(".idasdlc/artifacts")
    trace_store_dir: Path = Path(".idasdlc/traces")
    logs_dir: Path = Path(".idasdlc/logs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_task_pack_dir: Path = Path("task_packs")
    max_parallel_runs: int = 4
    default_generation_label: str = "baseline"
    reward_card_dir: Path = Path(".idasdlc/reward_cards")

    def ensure_directories(self) -> None:
        """Create required directories if they are missing."""
        for path in [
            self.object_store_dir,
            self.trace_store_dir,
            self.logs_dir,
            self.default_task_pack_dir,
            self.reward_card_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    settings = Settings()
    settings.ensure_directories()
    return settings
