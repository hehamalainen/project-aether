"""Default v1 tool manifests for OtterBot."""

from __future__ import annotations

from .contracts import ToolManifest


def default_tool_manifests() -> list[ToolManifest]:
    """Return secure-by-default tool set (7 tools) for OtterBot v1."""
    return [
        ToolManifest(
            tool_id="doc.read",
            version="1.0.0",
            required_permissions=["docs:read"],
            network_destinations=["none"],
            data_classification="internal",
            signature="builtin",
            provenance="otterbot://builtin/doc.read",
        ),
        ToolManifest(
            tool_id="doc.write",
            version="1.0.0",
            required_permissions=["docs:write"],
            network_destinations=["none"],
            data_classification="internal",
            signature="builtin",
            provenance="otterbot://builtin/doc.write",
            requires_approval=True,
        ),
        ToolManifest(
            tool_id="code.search",
            version="1.0.0",
            required_permissions=["code:read"],
            network_destinations=["none"],
            data_classification="internal",
            signature="builtin",
            provenance="otterbot://builtin/code.search",
        ),
        ToolManifest(
            tool_id="web.fetch",
            version="1.0.0",
            required_permissions=["network:read"],
            network_destinations=["api.mycorp.com"],
            data_classification="external",
            signature="builtin",
            provenance="otterbot://builtin/web.fetch",
        ),
        ToolManifest(
            tool_id="integration.notify",
            version="1.0.0",
            required_permissions=["integrations:write"],
            network_destinations=["api.mycorp.com"],
            data_classification="sensitive",
            signature="builtin",
            provenance="otterbot://builtin/integration.notify",
            requires_approval=True,
        ),
        ToolManifest(
            tool_id="memory.store",
            version="1.0.0",
            required_permissions=["memory:write"],
            network_destinations=["none"],
            data_classification="internal",
            signature="builtin",
            provenance="otterbot://builtin/memory.store",
        ),
        ToolManifest(
            tool_id="memory.query",
            version="1.0.0",
            required_permissions=["memory:read"],
            network_destinations=["none"],
            data_classification="internal",
            signature="builtin",
            provenance="otterbot://builtin/memory.query",
        ),
    ]
