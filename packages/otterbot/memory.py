"""Session-isolated memory store."""

from __future__ import annotations

from .exceptions import MemoryAccessDenied


class SessionMemoryStore:
    """Simple memory store enforcing strict session boundaries."""

    def __init__(self):
        self._store: dict[str, dict[str, str]] = {}

    def put(self, session_id: str, key: str, value: str) -> None:
        bucket = self._store.setdefault(session_id, {})
        bucket[key] = value

    def get(self, session_id: str, key: str, actor_session_id: str) -> str | None:
        if session_id != actor_session_id:
            raise MemoryAccessDenied("Cross-session memory access denied")
        return self._store.get(session_id, {}).get(key)
