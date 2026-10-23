"""Database entities for the ID-ASDLC platform."""

import uuid
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import JSON, Column, DateTime, UniqueConstraint
from sqlmodel import Field, Relationship

from .base import IDMixin, PromotionStatus, RunStatus, TaskSource, TimestampMixin


class Task(IDMixin, TimestampMixin, table=True):
    __tablename__ = "tasks"

    source: TaskSource = Field(default=TaskSource.LOCAL, index=True)
    external_id: Optional[str] = Field(default=None, index=True)
    repo: str = Field(index=True)
    base_sha: Optional[str] = Field(default=None, index=True)
    prompt: str
    constraints: Optional[str] = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    validators_required: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    validator_policy: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    holdout: bool = Field(default=False, index=True)
    metadata_blob: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    runs: List["Run"] = Relationship(back_populates="task")
    curated_example: Optional["CuratedExample"] = Relationship(back_populates="task")


class Run(IDMixin, TimestampMixin, table=True):
    __tablename__ = "runs"

    task_id: uuid.UUID = Field(foreign_key="tasks.id", nullable=False, index=True)
    agent_id: str = Field(index=True)
    model_id: str = Field(index=True)
    generation_label: str = Field(default="baseline", index=True)
    status: RunStatus = Field(default=RunStatus.PENDING, index=True)
    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    metrics: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    validator_policy_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=False)))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=False)))

    task: Task = Relationship(back_populates="runs")
    trace: Optional["Trace"] = Relationship(back_populates="run")
    validation_results: List["ValidationResult"] = Relationship(back_populates="run")


class Trace(IDMixin, TimestampMixin, table=True):
    __tablename__ = "traces"

    run_id: uuid.UUID = Field(foreign_key="runs.id", nullable=False, unique=True, index=True)
    messages: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    diffs: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    artifacts_path: Optional[str] = Field(default=None)
    redaction_report: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    run: Run = Relationship(back_populates="trace")


class ValidationResult(IDMixin, TimestampMixin, table=True):
    __tablename__ = "validation_results"

    run_id: uuid.UUID = Field(foreign_key="runs.id", nullable=False, index=True)
    validator_name: str = Field(index=True)
    passed: bool = Field(default=False, index=True)
    score: Optional[float] = Field(default=None)
    logs: Optional[str] = None
    metadata_blob: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    run: Run = Relationship(back_populates="validation_results")


class Generation(IDMixin, TimestampMixin, table=True):
    __tablename__ = "generations"

    label: str = Field(default="baseline", index=True, unique=True)
    parent_generation_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="generations.id",
        nullable=True,
    )
    model_ref: Optional[str] = Field(default=None, index=True)
    dataset_hash: Optional[str] = Field(default=None, index=True)
    promotion_status: PromotionStatus = Field(default=PromotionStatus.DRAFT, index=True)
    metadata_blob: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    curated_examples: List["CuratedExample"] = Relationship(back_populates="generation")


class CuratedExample(IDMixin, TimestampMixin, table=True):
    __tablename__ = "curated_examples"
    __table_args__ = (UniqueConstraint("task_id", name="uq_curated_task"),)

    task_id: uuid.UUID = Field(foreign_key="tasks.id", nullable=False, index=True)
    chosen_run_id: uuid.UUID = Field(foreign_key="runs.id", nullable=False, index=True)
    generation_id: uuid.UUID = Field(foreign_key="generations.id", nullable=False, index=True)
    rationale: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    task: Task = Relationship(back_populates="curated_example")
    generation: Generation = Relationship(back_populates="curated_examples")


__all__ = [
    "Task",
    "Run",
    "Trace",
    "ValidationResult",
    "Generation",
    "CuratedExample",
    "TaskSource",
    "RunStatus",
    "PromotionStatus",
]
