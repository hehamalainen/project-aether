"""Generate reward cards describing validator policy per generation."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import select

from ..config import get_settings
from ..db import session_scope
from ..logging import get_logger
from ..models.entities import CuratedExample, Generation, Task, ValidationResult

log = get_logger(__name__)


class RewardCardService:
    """Produce reward cards that summarize validators and curated runs."""

    def __init__(self, *, output_dir: Path | None = None) -> None:
        settings = get_settings()
        self.output_dir = output_dir or Path(settings.reward_card_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, generation_label: str) -> Path:
        card = self._build_card(generation_label)
        filename = f"{generation_label}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        path = self.output_dir / filename
        path.write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("reward_card_generated", label=generation_label, path=str(path))
        return path

    def _build_card(self, generation_label: str) -> dict:
        with session_scope() as session:
            generation = session.exec(
                select(Generation).where(Generation.label == generation_label)
            ).first()
            if not generation:
                raise ValueError(f"Generation not found: {generation_label}")

            stmt = select(CuratedExample).where(CuratedExample.generation_id == generation.id)
            curated_examples = session.exec(stmt).all()
            validator_counter: Counter[str] = Counter()
            runs_payload: list[dict] = []

            for example in curated_examples:
                task = session.get(Task, example.task_id)
                validations = session.exec(
                    select(ValidationResult).where(ValidationResult.run_id == example.chosen_run_id)
                ).all()
                validator_names = [val.validator_name for val in validations]
                if validator_names:
                    validator_counter.update(validator_names)
                elif task:
                    validator_counter.update(task.validators_required)
                runs_payload.append(
                    {
                        "task_id": str(example.task_id),
                        "run_id": str(example.chosen_run_id),
                        "validators": validator_names,
                        "task_validators": task.validators_required if task else [],
                        "holdout": bool(task.holdout) if task else False,
                        "rationale": example.rationale,
                    }
                )

        summary = {
            "generation": {
                "id": str(generation.id),
                "label": generation.label,
                "promotion_status": generation.promotion_status.value,
                "curated_examples": len(curated_examples),
            },
            "validators": [
                {"name": name, "occurrences": count} for name, count in validator_counter.most_common()
            ],
            "runs": runs_payload,
        }
        return summary
