"""Model base classes and enums."""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class TimestampMixin(SQLModel):
    """Add created/updated timestamps."""

    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column_kwargs={"nullable": False},
    )
    updated_at: datetime = Field(
        default_factory=_utc_now,
        sa_column_kwargs={"nullable": False},
    )


class IDMixin(SQLModel):
    """UUID primary key mixin."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)


class TaskSource(str, enum.Enum):
    GITHUB = "github"
    JIRA = "jira"
    LINEAR = "linear"
    LOCAL = "local"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VALIDATION_FAILED = "validation_failed"


class PromotionStatus(str, enum.Enum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    PROMOTED = "promoted"
    ARCHIVED = "archived"
