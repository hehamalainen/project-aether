"""Logging helpers built on structlog."""
from __future__ import annotations

import logging
from pathlib import Path

import structlog

from .config import get_settings


def configure_logging() -> None:
    """Configure structlog and standard logging."""
    settings = get_settings()
    log_path = Path(settings.logs_dir) / "idasdlc.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    processors: list[structlog.types.Processor] = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer()
        if log_path.suffix == ".json"
        else structlog.dev.ConsoleRenderer(colors=True),
    ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Instantiate a logger bound to a component."""
    configure_logging()
    return structlog.get_logger(component=component)
