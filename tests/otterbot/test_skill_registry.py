"""Supply-chain admission tests for OtterBot skills."""

from __future__ import annotations

import pytest

from packages.otterbot.exceptions import SkillAdmissionError
from packages.otterbot.skills import SkillPackage, SkillRegistry


def _base_manifest() -> dict[str, object]:
    return {
        "tool_id": "partner.lookup",
        "version": "1.0.0",
        "required_permissions": ["network:read"],
        "network_destinations": ["api.mycorp.com"],
        "data_classification": "external",
        "source_uri": "https://partner.example.com/tool.json",
        "publisher": "trusted-inc",
        "runtime_isolation": "sandboxed",
        "signature": "publisher-signature",
    }


def test_unsigned_or_tampered_skill_is_rejected() -> None:
    registry = SkillRegistry(signing_key="skill-key", trusted_publishers={"trusted-inc"})
    manifest = _base_manifest()
    signature = registry.sign_manifest(manifest)

    tampered_manifest = dict(manifest)
    tampered_manifest["network_destinations"] = ["evil.com"]

    with pytest.raises(SkillAdmissionError):
        registry.admit(SkillPackage(manifest=tampered_manifest, signature=signature))


def test_untrusted_publisher_is_rejected() -> None:
    registry = SkillRegistry(signing_key="skill-key", trusted_publishers={"trusted-inc"})
    manifest = _base_manifest()
    manifest["publisher"] = "unknown-inc"
    signature = registry.sign_manifest(manifest)

    with pytest.raises(SkillAdmissionError):
        registry.admit(SkillPackage(manifest=manifest, signature=signature))


def test_kill_switch_blocks_tool_admission() -> None:
    manifest = _base_manifest()
    registry = SkillRegistry(
        signing_key="skill-key",
        trusted_publishers={"trusted-inc"},
        kill_switch_tools={"partner.lookup"},
    )
    signature = registry.sign_manifest(manifest)

    with pytest.raises(SkillAdmissionError):
        registry.admit(SkillPackage(manifest=manifest, signature=signature))
