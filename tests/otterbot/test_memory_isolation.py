"""Session isolation checks for OtterBot memory store."""

from __future__ import annotations

import pytest

from packages.otterbot.exceptions import MemoryAccessDenied
from packages.otterbot.memory import SessionMemoryStore


def test_cross_session_memory_leak_is_blocked() -> None:
    store = SessionMemoryStore()
    store.put("s1", "secret", "token-abc")

    with pytest.raises(MemoryAccessDenied):
        store.get("s1", "secret", actor_session_id="s2")

    assert store.get("s1", "secret", actor_session_id="s1") == "token-abc"
