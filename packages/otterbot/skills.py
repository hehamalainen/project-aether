"""Skill supply-chain admission controls."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from .contracts import ToolManifest
from .exceptions import SkillAdmissionError


@dataclass(frozen=True)
class SkillPackage:
    """Signed skill package submitted for admission."""

    manifest: dict[str, object]
    signature: str


class SkillRegistry:
    """Admission gate for signed skills with provenance and kill switch checks."""

    def __init__(
        self,
        signing_key: str,
        trusted_publishers: set[str],
        kill_switch_tools: set[str] | None = None,
    ):
        self._signing_key = signing_key.encode("utf-8")
        self._trusted_publishers = trusted_publishers
        self._kill_switch_tools = kill_switch_tools or set()

    def sign_manifest(self, manifest: dict[str, object]) -> str:
        payload = self._canonical_payload(manifest)
        digest = hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()
        return digest

    def admit(self, package: SkillPackage) -> ToolManifest:
        manifest = package.manifest

        expected_signature = self.sign_manifest(manifest)
        if not hmac.compare_digest(expected_signature, package.signature):
            raise SkillAdmissionError("SIGNATURE_INVALID")

        publisher = str(manifest.get("publisher", ""))
        if publisher not in self._trusted_publishers:
            raise SkillAdmissionError("PUBLISHER_UNTRUSTED")

        source_uri = str(manifest.get("source_uri", ""))
        if not source_uri.startswith("https://"):
            raise SkillAdmissionError("PROVENANCE_INVALID")

        tool_id = str(manifest.get("tool_id", ""))
        if tool_id in self._kill_switch_tools:
            raise SkillAdmissionError("TOOL_KILL_SWITCH_ACTIVE")

        runtime_isolation = str(manifest.get("runtime_isolation", ""))
        if runtime_isolation != "sandboxed":
            raise SkillAdmissionError("RUNTIME_ISOLATION_REQUIRED")

        required_fields = (
            "tool_id",
            "version",
            "required_permissions",
            "network_destinations",
            "data_classification",
            "source_uri",
            "signature",
        )
        for field in required_fields:
            if field not in manifest:
                raise SkillAdmissionError(f"MISSING_FIELD:{field}")

        return ToolManifest(
            tool_id=tool_id,
            version=str(manifest["version"]),
            required_permissions=[str(p) for p in manifest["required_permissions"]],
            network_destinations=[str(d) for d in manifest["network_destinations"]],
            data_classification=str(manifest["data_classification"]),
            signature=str(manifest["signature"]),
            provenance=source_uri,
            runtime_isolation=runtime_isolation,
            kill_switch=False,
            requires_approval=bool(manifest.get("requires_approval", False)),
        )

    def _canonical_payload(self, manifest: dict[str, object]) -> bytes:
        payload = dict(manifest)
        payload.pop("signature", None)
        normalized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return normalized.encode("utf-8")
