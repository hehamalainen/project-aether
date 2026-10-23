"""Evaluation helpers for benchmark suites."""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any, Dict, Iterable, cast

from sqlalchemy import asc
from sqlmodel import select

from ..db import session_scope
from ..models.base import RunStatus
from ..models.entities import Run, Task


class EvaluationService:
    def solved_counts(self, *, holdout: bool | None = None) -> Dict[str, int]:
        with session_scope() as session:
            stmt = select(Run).where(Run.status == RunStatus.SUCCEEDED)
            if holdout is not None:
                stmt = stmt.join(Task).where(Task.holdout == holdout)
            counter: Counter[str] = Counter()
            for run in session.exec(stmt):
                counter[run.model_id] += 1
            return dict(counter)

    def solved_at_k(self, task_ids: Iterable[uuid.UUID], k: int = 3) -> float:
        if k <= 0:
            return 0.0
        with session_scope() as session:
            solved = 0
            total = 0
            for task_id in task_ids:
                created_column = cast(Any, Run.created_at)
                stmt = select(Run).where(Run.task_id == task_id).order_by(asc(created_column)).limit(k)
                runs = session.exec(stmt).all()
                if not runs:
                    continue
                total += 1
                if any(run.status == RunStatus.SUCCEEDED for run in runs):
                    solved += 1
            return solved / total if total else 0.0
