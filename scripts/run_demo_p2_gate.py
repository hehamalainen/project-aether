#!/usr/bin/env python3
"""Run P2 pre-demo guardrails: risk freeze + readiness checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], *, cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"exec error: {exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def _latest_p1_artifact(root: Path) -> Path | None:
    demo_dir = root / ".pytest-artifacts" / "demo"
    if not demo_dir.exists():
        return None
    candidates = sorted(demo_dir.glob("p1-flow-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def run_gate(*, root: Path, base_ref: str, output_json: Path) -> dict[str, Any]:
    started = time.perf_counter()
    report: dict[str, Any] = {
        "run_id": f"p2-guardrails-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "started_at": _iso_now(),
        "base_ref": base_ref,
        "checks": {},
        "details": {},
        "passed": False,
    }

    ok, output = _run(["./scripts/demo_stack.sh", "status"], cwd=root)
    report["checks"]["stack_healthy"] = ok
    report["details"]["stack_status"] = output

    baseline_file = root / ".run" / "demo_risk_freeze_baseline.txt"
    baseline_initialized = baseline_file.exists()
    if not baseline_initialized:
        init_ok, init_output = _run(
            ["./scripts/check_demo_risk_freeze.sh", "--init", "--base-ref", base_ref],
            cwd=root,
        )
        report["details"]["risk_freeze_init"] = init_output
        baseline_initialized = init_ok and baseline_file.exists()

    report["checks"]["risk_freeze_baseline_ready"] = baseline_initialized

    ok, output = _run(
        ["./scripts/check_demo_risk_freeze.sh", "--base-ref", base_ref],
        cwd=root,
    )
    report["checks"]["risk_freeze_clean"] = ok
    report["details"]["risk_freeze"] = output

    ok, output = _run(["./scripts/check_research_alignment.sh"], cwd=root)
    report["checks"]["research_alignment_ready"] = ok
    report["details"]["research_alignment"] = output

    latest_p1 = _latest_p1_artifact(root)
    report["checks"]["p1_artifact_present"] = latest_p1 is not None
    report["details"]["latest_p1_artifact"] = str(latest_p1) if latest_p1 else ""

    p1_passed = False
    if latest_p1 is not None:
        try:
            payload = json.loads(latest_p1.read_text(encoding="utf-8"))
            p1_passed = bool(payload.get("passed"))
            report["details"]["latest_p1_checks"] = payload.get("checks", {})
        except (json.JSONDecodeError, OSError) as exc:
            report["details"]["latest_p1_read_error"] = str(exc)
    report["checks"]["p1_latest_passed"] = p1_passed

    ok, output = _run(["pgrep", "-af", "scripts/demo_keepalive.sh"], cwd=root)
    report["checks"]["keepalive_running"] = ok
    report["details"]["keepalive_processes"] = output

    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    report["ended_at"] = _iso_now()
    report["passed"] = all(bool(v) for v in report["checks"].values())

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P2 pre-demo guardrail gate.")
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git base ref used for risk-freeze diff checks (default: HEAD)",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional output path. Defaults to .pytest-artifacts/demo/p2-guardrails-<timestamp>.json",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.output_json:
        output = Path(args.output_json)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output = root / ".pytest-artifacts" / "demo" / f"p2-guardrails-{ts}.json"

    report = run_gate(root=root, base_ref=args.base_ref, output_json=output)
    print(f"P2 guardrail report: {output}")
    print(f"passed={report['passed']}")
    print("checks:")
    for key, value in report["checks"].items():
        print(f"  {key}={value}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
