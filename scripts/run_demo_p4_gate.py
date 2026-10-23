#!/usr/bin/env python3
"""Run P4 semantic/AIP gate with tests + dual strict harness passes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any


PYTEST_TARGETS = [
    "tests/aether/test_aip_mutations.py",
    "tests/aether/test_symbol_adapters.py",
    "tests/aether/test_crdt.py::TestDocumentStore::test_apply_aip_mutations_updates_structured_python_file",
    "tests/aether/test_crdt.py::TestDocumentStore::test_apply_aip_mutations_rejects_non_structured_paths",
    "tests/aether/test_services.py::TestProposalService::test_aip_submission_populates_paths_and_symbols",
    "tests/aether/test_services.py::TestProposalService::test_aip_invalid_payload_is_rejected",
    "tests/aether/test_services.py::TestProposalService::test_non_python_parser_failure_uses_deterministic_file_fallback",
    "tests/aether/test_services.py::TestPromotionEngine::test_evaluate_proposal_supports_aip_v0",
    "tests/aether/test_system_physics_api.py",
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], *, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_p3_fallback_rate(path: Path) -> float | None:
    if not path.exists():
        return None
    payload = _read_json(path)
    body = (
        payload.get("physics", {}).get("body", {})
        if isinstance(payload, dict)
        else {}
    )
    if not isinstance(body, dict):
        return None
    try:
        return float(body.get("fallback_rate"))
    except Exception:
        return None


def _run_harness_case(
    *,
    root: Path,
    output_json: Path,
    seed: int,
    max_fallback_rate: float,
    max_window_rho: float,
) -> tuple[bool, dict[str, Any], str]:
    cmd = [
        str(root / ".venv" / "bin" / "python"),
        "-m",
        "apps.aether_cli.main",
        "swarm-harness",
        "--agents",
        "200",
        "--files",
        "180",
        "--run-seconds",
        "25",
        "--drain-seconds",
        "8",
        "--contention",
        "low",
        "--min-interval-ms",
        "70",
        "--max-interval-ms",
        "260",
        "--inflight",
        "120",
        "--promotion-batch",
        "20",
        "--promotion-non-conflicting-batches",
        "6",
        "--promotion-max-conflict-sets",
        "1",
        "--promotion-interval-ms",
        "25",
        "--monitor-interval-ms",
        "200",
        "--min-submit-throughput",
        "5",
        "--min-promotion-throughput",
        "0.05",
        "--max-submit-p95-ms",
        "250",
        "--max-conflict-rate",
        "0.35",
        "--max-backlog-end",
        "3000",
        "--max-submit-failure-rate",
        "0.05",
        "--max-window-rho",
        str(max_window_rho),
        "--max-fallback-rate",
        str(max_fallback_rate),
        "--physics-window-seconds",
        "60",
        "--seed",
        str(seed),
        "--output-json",
        str(output_json),
        "--strict",
    ]
    ok, output = _run(cmd, cwd=root)
    payload = _read_json(output_json)
    return ok, payload, output


def run_gate(
    *,
    root: Path,
    output_json: Path,
    fallback_target: float,
    max_window_rho: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    demo_dir = root / ".pytest-artifacts" / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)

    strict1_json = demo_dir / f"p4-strict-pass1-{ts}.json"
    strict2_json = demo_dir / f"p4-strict-pass2-{ts}.json"
    p3_dashboard_state = demo_dir / "p3-dashboard-state.json"

    report: dict[str, Any] = {
        "run_id": f"p4-semantic-aip-{ts}",
        "started_at": _iso_now(),
        "checks": {},
        "details": {
            "fallback_target": fallback_target,
            "p3_dashboard_state": str(p3_dashboard_state),
            "strict_artifacts": {
                "pass1": str(strict1_json),
                "pass2": str(strict2_json),
            },
        },
        "passed": False,
    }

    stack_ok, stack_out = _run(["./scripts/demo_stack.sh", "status"], cwd=root)
    report["checks"]["stack_check_completed"] = True
    report["details"]["stack_healthy"] = stack_ok
    report["details"]["stack_status"] = stack_out

    pytest_cmd = [str(root / ".venv" / "bin" / "python"), "-m", "pytest", "-q", *PYTEST_TARGETS]
    tests_ok, tests_out = _run(pytest_cmd, cwd=root)
    report["checks"]["semantic_aip_regression_tests_pass"] = tests_ok
    report["details"]["pytest_output"] = tests_out

    strict1_ok, strict1_payload, strict1_out = _run_harness_case(
        root=root,
        output_json=strict1_json,
        seed=5401,
        max_fallback_rate=fallback_target,
        max_window_rho=max_window_rho,
    )
    strict2_ok, strict2_payload, strict2_out = _run_harness_case(
        root=root,
        output_json=strict2_json,
        seed=5402,
        max_fallback_rate=fallback_target,
        max_window_rho=max_window_rho,
    )
    report["checks"]["strict_run1_pass"] = strict1_ok and bool(strict1_payload.get("passed"))
    report["checks"]["strict_run2_pass"] = strict2_ok and bool(strict2_payload.get("passed"))
    report["details"]["strict_run1_output"] = strict1_out
    report["details"]["strict_run2_output"] = strict2_out

    strict1_fallback = float(strict1_payload.get("metrics", {}).get("fallback_rate_window", 0.0) or 0.0)
    strict2_fallback = float(strict2_payload.get("metrics", {}).get("fallback_rate_window", 0.0) or 0.0)
    report["details"]["strict_fallback_rates"] = {
        "pass1": strict1_fallback,
        "pass2": strict2_fallback,
    }
    report["checks"]["fallback_rate_target_run1"] = strict1_fallback <= fallback_target
    report["checks"]["fallback_rate_target_run2"] = strict2_fallback <= fallback_target
    report["checks"]["fallback_rate_nonincreasing"] = strict2_fallback <= (strict1_fallback + 1e-9)

    p3_baseline = _read_p3_fallback_rate(p3_dashboard_state)
    report["details"]["p3_fallback_rate_baseline"] = p3_baseline
    if p3_baseline is not None:
        report["checks"]["fallback_rate_vs_p3_baseline"] = max(strict1_fallback, strict2_fallback) <= (
            p3_baseline + 1e-6
        )
    else:
        report["checks"]["fallback_rate_vs_p3_baseline"] = True

    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    report["ended_at"] = _iso_now()
    report["passed"] = all(bool(v) for v in report["checks"].values())

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P4 semantic/AIP gate.")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--fallback-target", type=float, default=0.05)
    parser.add_argument("--max-window-rho", type=float, default=12.0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_json = (
        Path(args.output_json)
        if args.output_json
        else root / ".pytest-artifacts" / "demo" / f"p4-semantic-aip-{ts}.json"
    )

    report = run_gate(
        root=root,
        output_json=output_json,
        fallback_target=float(args.fallback_target),
        max_window_rho=float(args.max_window_rho),
    )

    print(f"P4 semantic/AIP report: {output_json}")
    print(f"passed={report['passed']}")
    print("checks:")
    for key, value in report["checks"].items():
        print(f"  {key}={value}")
    fallback_details = report["details"].get("strict_fallback_rates", {})
    print(f"strict_fallback_rates={fallback_details}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
