"""Task pack schema used for imports."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .base import TaskSource
from .validator_policy import ValidatorPolicy


class TaskPackMetadata(BaseModel):
    name: str
    description: Optional[str] = None
    source: TaskSource = TaskSource.LOCAL
    repo: Optional[str] = None
    base_sha: Optional[str] = None
    holdout: bool = False
    tags: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    validator_policy: ValidatorPolicy | None = None


class TaskSpec(BaseModel):
    external_id: Optional[str] = None
    prompt: str
    repo: Optional[str] = None
    base_sha: Optional[str] = None
    constraints: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    validator_policy: ValidatorPolicy | None = None
    holdout: Optional[bool] = None


class TaskPack(BaseModel):
    version: Literal["1.0"] = "1.0"
    metadata: TaskPackMetadata
    tasks: list[TaskSpec]


__all__ = ["TaskPack", "TaskSpec", "TaskPackMetadata"]
