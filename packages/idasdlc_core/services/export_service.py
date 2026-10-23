"""Dataset export helpers."""
from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import select

from ..db import session_scope
from ..logging import get_logger
from ..models.entities import CuratedExample, Task, Trace

log = get_logger(__name__)


class ExportService:
    def export_curated(self, out_path: Path, *, include_trace: bool = True) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        records = self._collect(include_trace=include_trace)
        with out_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("dataset_exported", path=str(out_path), rows=len(records))
        return out_path

    def _collect(self, *, include_trace: bool) -> list[dict]:
        with session_scope() as session:
            stmt = select(CuratedExample).join(Task)
            records: list[dict] = []
            for example in session.exec(stmt):
                task = example.task
                trace = None
                if include_trace:
                    trace = session.exec(
                        select(Trace).where(Trace.run_id == example.chosen_run_id)
                    ).first()
                record = {
                    "task": {
                        "id": str(task.id),
                        "prompt": task.prompt,
                        "repo": task.repo,
                        "base_sha": task.base_sha,
                        "tags": task.tags,
                        "validators": task.validators_required,
                    },
                    "run_id": str(example.chosen_run_id),
                    "generation_id": str(example.generation_id),
                    "rationale": example.rationale,
                    "metrics": example.metrics,
                }
                if trace:
                    record["trace"] = {
                        "messages": trace.messages,
                        "tool_calls": trace.tool_calls,
                        "diffs": trace.diffs,
                    }
                records.append(record)
            return records
