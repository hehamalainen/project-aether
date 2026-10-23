"""Audit event collection and lightweight dashboard summary."""

from __future__ import annotations

from collections import Counter

from .contracts import AuditEvent


class AuditLogger:
    """In-memory audit sink for deterministic policy and runtime outcomes."""

    def __init__(self):
        self.events: list[AuditEvent] = []

    def log(self, event: AuditEvent) -> None:
        self.events.append(event)

    def recent(self, limit: int = 50) -> list[AuditEvent]:
        return self.events[-limit:]

    def dashboard(self) -> dict[str, object]:
        reason_counts = Counter(reason for event in self.events for reason in event.reason_codes)
        decision_counts = Counter(event.policy_decision for event in self.events)
        return {
            "event_count": len(self.events),
            "decision_counts": dict(decision_counts),
            "reason_counts": dict(reason_counts),
        }
