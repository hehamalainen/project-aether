"""Helpers for presenting validator policy metrics."""
from __future__ import annotations

from typing import Any

_PLACEHOLDER = "--"


def summarize_policy_status(metrics: dict[str, Any] | None, placeholder: str = _PLACEHOLDER) -> str:
    """Return a compact PASS/FAIL string for a validator policy."""
    if not metrics:
        return placeholder
    passed = metrics.get("passed")
    passed_count = metrics.get("passed_count")
    minimum_pass = metrics.get("minimum_pass")
    ratio = None
    if passed_count is not None and minimum_pass is not None:
        ratio = f"{passed_count}/{minimum_pass}"
    if passed is True:
        return f"PASS ({ratio})" if ratio else "PASS"
    if passed is False:
        return f"FAIL ({ratio})" if ratio else "FAIL"
    return ratio or placeholder


def summarize_policy_failures(metrics: dict[str, Any] | None, placeholder: str = _PLACEHOLDER) -> str:
    """Return a short summary of failing validators grouped by severity."""
    if not metrics:
        return placeholder
    parts: list[str] = []
    for label, key in (
        ("req", "required_failures"),
        ("adv", "advisory_failures"),
        ("missing", "missing"),
    ):
        values = metrics.get(key) or []
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    return " | ".join(parts) if parts else placeholder


def summarize_auto_remediation(metrics: dict[str, Any] | None, placeholder: str = _PLACEHOLDER) -> str:
    """Return a compact status string for auto-remediation attempts."""
    if not metrics:
        return placeholder
    status = "applied" if metrics.get("applied") else "attempted" if metrics.get("attempted") else "none"
    parts = [status]
    retries = metrics.get("retries")
    if isinstance(retries, int) and retries > 0:
        parts.append(f"{retries}x")
    validators = metrics.get("validators") or []
    if validators:
        parts.append(", ".join(validators))
    return " | ".join(parts) if parts else placeholder


__all__ = [
    "summarize_policy_status",
    "summarize_policy_failures",
    "summarize_auto_remediation",
]
