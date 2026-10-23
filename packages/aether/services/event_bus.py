"""
Event bus for Aether realtime events.

Provides pub/sub functionality for:
- Proposal lifecycle events
- Head updates
- Gate results
- Conflict detection
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional, Any
from datetime import UTC, datetime
import structlog

from ..models.events import AetherEvent, EventType
from ..models.entities import generate_id

logger = structlog.get_logger()


@dataclass
class Subscription:
    """A subscription to events."""
    id: str
    repo_id: Optional[str]  # None = all repos
    event_types: Optional[set[EventType]]  # None = all types
    callback: Callable[[AetherEvent], Any]
    is_async: bool = False


class EventBus:
    """
    In-process event bus for Aether.

    For Phase 0, this is a simple in-memory pub/sub.
    Phase 2+ will add external message broker support.
    """

    def __init__(self):
        self._subscriptions: dict[str, Subscription] = {}
        self._repo_subscriptions: dict[str, set[str]] = defaultdict(set)
        self._event_history: list[AetherEvent] = []
        self._max_history = 1000
        self._lock = asyncio.Lock()

    async def publish(self, event: AetherEvent) -> None:
        """Publish an event to all matching subscribers."""
        async with self._lock:
            # Store in history
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history = self._event_history[-self._max_history:]

        logger.debug(
            "event_published",
            event_type=event.event_type.value,
            repo_id=event.repo_id,
            event_id=event.event_id,
        )

        # Find matching subscriptions
        matching_subs = []
        for sub in list(self._subscriptions.values()):
            # Check repo filter
            if sub.repo_id and sub.repo_id != event.repo_id:
                continue

            # Check event type filter
            if sub.event_types and event.event_type not in sub.event_types:
                continue

            matching_subs.append(sub)

        # Dispatch to subscribers
        for sub in matching_subs:
            try:
                if sub.is_async:
                    await sub.callback(event)
                else:
                    sub.callback(event)
            except Exception as e:
                logger.error(
                    "subscription_callback_error",
                    subscription_id=sub.id,
                    error=str(e),
                )

    def subscribe(
        self,
        callback: Callable[[AetherEvent], Any],
        repo_id: Optional[str] = None,
        event_types: Optional[list[EventType]] = None,
        is_async: bool = False,
    ) -> str:
        """
        Subscribe to events.

        Args:
            callback: Function to call with each event
            repo_id: Filter to specific repo (None = all)
            event_types: Filter to specific event types (None = all)
            is_async: Whether callback is async

        Returns:
            Subscription ID for unsubscribing
        """
        sub_id = generate_id("sub")
        sub = Subscription(
            id=sub_id,
            repo_id=repo_id,
            event_types=set(event_types) if event_types else None,
            callback=callback,
            is_async=is_async,
        )
        self._subscriptions[sub_id] = sub

        if repo_id:
            self._repo_subscriptions[repo_id].add(sub_id)

        logger.debug(
            "subscription_created",
            subscription_id=sub_id,
            repo_id=repo_id,
            event_types=[et.value for et in event_types] if event_types else "all",
        )

        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription."""
        if subscription_id not in self._subscriptions:
            return False

        sub = self._subscriptions.pop(subscription_id)
        if sub.repo_id:
            self._repo_subscriptions[sub.repo_id].discard(subscription_id)

        logger.debug("subscription_removed", subscription_id=subscription_id)
        return True

    def get_recent_events(
        self,
        repo_id: Optional[str] = None,
        event_types: Optional[list[EventType]] = None,
        limit: int = 100,
    ) -> list[AetherEvent]:
        """Get recent events from history."""
        events = self._event_history[-limit:]

        if repo_id:
            events = [e for e in events if e.repo_id == repo_id]

        if event_types:
            type_set = set(event_types)
            events = [e for e in events if e.event_type in type_set]

        return events

    def clear_history(self) -> None:
        """Clear event history."""
        self._event_history.clear()

    @property
    def subscription_count(self) -> int:
        """Get number of active subscriptions."""
        return len(self._subscriptions)


# Helper functions for creating events

def create_proposal_event(
    event_type: EventType,
    repo_id: str,
    proposal_id: str,
    agent_id: str,
    affected_paths: list[str],
    **extra_payload,
) -> AetherEvent:
    """Create a proposal-related event."""
    return AetherEvent(
        event_id=generate_id("evt"),
        event_type=event_type,
        repo_id=repo_id,
        timestamp=datetime.now(UTC),
        payload={
            "proposal_id": proposal_id,
            "agent_id": agent_id,
            "affected_paths": affected_paths,
            **extra_payload,
        },
    )


def create_head_event(
    event_type: EventType,
    repo_id: str,
    old_head_id: Optional[str],
    new_head_id: str,
    old_sequence: int,
    new_sequence: int,
    snapshot_id: str,
    **extra_payload,
) -> AetherEvent:
    """Create a head update event."""
    return AetherEvent(
        event_id=generate_id("evt"),
        event_type=event_type,
        repo_id=repo_id,
        timestamp=datetime.now(UTC),
        payload={
            "old_head_id": old_head_id,
            "new_head_id": new_head_id,
            "old_sequence": old_sequence,
            "new_sequence": new_sequence,
            "snapshot_id": snapshot_id,
            **extra_payload,
        },
    )


def create_gate_event(
    event_type: EventType,
    repo_id: str,
    gate_name: str,
    snapshot_id: str,
    status: str,
    proposal_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    **extra_payload,
) -> AetherEvent:
    """Create a gate execution event."""
    return AetherEvent(
        event_id=generate_id("evt"),
        event_type=event_type,
        repo_id=repo_id,
        timestamp=datetime.now(UTC),
        payload={
            "gate_name": gate_name,
            "snapshot_id": snapshot_id,
            "status": status,
            "proposal_id": proposal_id,
            "duration_ms": duration_ms,
            **extra_payload,
        },
    )
