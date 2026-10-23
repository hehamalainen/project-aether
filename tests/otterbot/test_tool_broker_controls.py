"""Worker pool and execution quota behavior checks."""

from __future__ import annotations

import pytest

from packages.otterbot.contracts import ToolManifest
from packages.otterbot.exceptions import QuotaExceededError
from packages.otterbot.tool_broker import ToolBroker, ToolBrokerConfig


def test_tool_broker_enforces_session_quota() -> None:
    broker = ToolBroker(ToolBrokerConfig(max_executions_per_minute=1))
    manifest = ToolManifest(
        tool_id="doc.read",
        version="1.0.0",
        required_permissions=["docs:read"],
        network_destinations=["none"],
        data_classification="internal",
        signature="builtin",
        provenance="otterbot://builtin/doc.read",
    )
    broker.register_tool(manifest)

    envelope = broker.build_execution_envelope(
        request=type("Req", (), {"target": "workspace/a.md", "session_id": "s1"})(),
        tool_id="doc.read",
        capability_token="token",
    )

    broker.execute(envelope)
    with pytest.raises(QuotaExceededError):
        broker.execute(envelope)


def test_tool_broker_tracks_warm_worker_hits() -> None:
    broker = ToolBroker(ToolBrokerConfig(warm_workers_per_tool=1, max_executions_per_minute=5))
    manifest = ToolManifest(
        tool_id="doc.read",
        version="1.0.0",
        required_permissions=["docs:read"],
        network_destinations=["none"],
        data_classification="internal",
        signature="builtin",
        provenance="otterbot://builtin/doc.read",
    )
    broker.register_tool(manifest)

    envelope = broker.build_execution_envelope(
        request=type("Req", (), {"target": "workspace/a.md", "session_id": "s1"})(),
        tool_id="doc.read",
        capability_token="token",
    )

    broker.execute(envelope)
    broker.execute(envelope)

    assert broker.metrics.warm_hits >= 2
    assert broker.metrics.cold_starts == 0
