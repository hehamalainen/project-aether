"""Select best traces per task according to policy."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional, cast

from sqlalchemy import asc
from sqlmodel import Session, select

from ..db import session_scope
from ..logging import get_logger
from ..models.base import PromotionStatus, RunStatus
from ..models.entities import CuratedExample, Generation, Run

log = get_logger(__name__)


@dataclass(slots=True)
class CurationPolicy:
    """Weights for multi-objective scoring."""

    diff_size_weight: float = 0.4
    tool_calls_weight: float = 0.2
    token_weight: float = 0.2
    duration_weight: float = 0.2


DEFAULT_POLICY = CurationPolicy()


class CurationService:
    """Implements FR-11..FR-14."""

    def __init__(self, policy: CurationPolicy = DEFAULT_POLICY) -> None:
        self.policy = policy

    def curate_task(self, task_id: uuid.UUID, generation_label: str) -> Optional[CuratedExample]:
        with session_scope() as session:
            runs = self._valid_runs_for_task(session, task_id)
            if not runs:
                log.warning("no_valid_runs", task_id=str(task_id))
                return None

            chosen = min(runs, key=self._score_run)
            generation = self._ensure_generation(session, generation_label)

            stmt = select(CuratedExample).where(CuratedExample.task_id == task_id)
            curated = session.exec(stmt).first()
            if curated:
                curated.chosen_run_id = chosen.id
                curated.generation_id = generation.id
                curated.metrics = chosen.metrics
                rationale = self._build_rationale(chosen)
                curated.rationale = rationale
                log.info("curated_example_updated", task_id=str(task_id), run_id=str(chosen.id))
            else:
                curated = CuratedExample(
                    task_id=task_id,
                    chosen_run_id=chosen.id,
                    generation_id=generation.id,
                    rationale=self._build_rationale(chosen),
                    metrics=chosen.metrics,
                )
                session.add(curated)
                log.info("curated_example_created", task_id=str(task_id), run_id=str(chosen.id))
            return curated

    def _valid_runs_for_task(self, session: Session, task_id: uuid.UUID) -> list[Run]:
        created_column = cast(Any, Run.created_at)
        stmt = (
            select(Run)
            .where(Run.task_id == task_id)
            .where(Run.status == RunStatus.SUCCEEDED)
            .order_by(asc(created_column))
        )
        return list(session.exec(stmt))

    def _score_run(self, run: Run) -> float:
        metrics = run.metrics or {}
        diff = float(metrics.get("diff_size", 0.0))
        tool_calls = float(metrics.get("tool_calls", 0.0))
        tokens = float(metrics.get("tokens", 0.0))
        duration = float(metrics.get("duration_sec", 0.0))
        score = (
            self.policy.diff_size_weight * diff
            + self.policy.tool_calls_weight * tool_calls
            + self.policy.token_weight * tokens
            + self.policy.duration_weight * duration
        )
        return score

    def _build_rationale(self, run: Run) -> str:
        metrics = run.metrics or {}
        parts = [
            f"diff={metrics.get('diff_size', 'n/a')}",
            f"tools={metrics.get('tool_calls', 'n/a')}",
            f"tokens={metrics.get('tokens', 'n/a')}",
        ]
        return " | ".join(parts)

    def _ensure_generation(self, session: Session, label: str) -> Generation:
        stmt = select(Generation).where(Generation.label == label)
        generation = session.exec(stmt).first()
        if generation:
            return generation
        generation = Generation(label=label, promotion_status=PromotionStatus.DRAFT)
        session.add(generation)
        session.flush()
        return generation
