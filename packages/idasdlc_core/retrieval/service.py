"""Trace retrieval for v1 improvement mechanism."""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from typing import List

from sqlmodel import select

from ..db import session_scope
from ..logging import get_logger
from ..models.entities import CuratedExample, Run, Task, Trace

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text)}


@dataclass(slots=True)
class RetrievalResult:
    task_id: uuid.UUID
    run_id: uuid.UUID
    prompt: str
    score: float
    diffs: list[str]


class TraceRetrievalService:
    def search(self, query: str, limit: int = 5) -> List[RetrievalResult]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        with session_scope() as session:
            stmt = select(CuratedExample).join(Task).join(Run)
            curated = session.exec(stmt).all()
            scored: list[RetrievalResult] = []
            for example in curated:
                task = example.task
                run = session.get(Run, example.chosen_run_id)
                trace = self._trace_for_run(session, run.id) if run else None
                score = self._score(query_tokens, tokenize(task.prompt) | set(task.tags))
                if score <= 0:
                    continue
                scored.append(
                    RetrievalResult(
                        task_id=task.id,
                        run_id=run.id if run else example.chosen_run_id,
                        prompt=task.prompt,
                        score=score,
                        diffs=trace.diffs if trace else [],
                    )
                )
            scored.sort(key=lambda item: item.score, reverse=True)
            log.info("retrieval_search", query=query, results=len(scored))
            return scored[:limit]

    def _trace_for_run(self, session, run_id: uuid.UUID) -> Trace | None:
        stmt = select(Trace).where(Trace.run_id == run_id)
        return session.exec(stmt).first()

    def _score(self, query_tokens: set[str], doc_tokens: set[str]) -> float:
        overlap = query_tokens & doc_tokens
        if not overlap:
            return 0.0
        return len(overlap) / math.sqrt(len(doc_tokens) or 1)
