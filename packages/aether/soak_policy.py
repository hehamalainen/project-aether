"""Guardrails for long-running soak scheduling on shared development machines."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

DEFAULT_LONG_THRESHOLD_SECONDS = 4 * 60 * 60
DEFAULT_NIGHT_START_HOUR = 20
DEFAULT_NIGHT_END_HOUR = 8

CANONICAL_BASELINE = "canonical_baseline"
MIXED_SOURCE_STRESS = "mixed_source_stress"

MX_PROCESS_PATTERNS = (
    "scale-control-plane",
    "scale-worker-load",
    "run_aether_live_proof",
    "run_aether_worker_proof",
    "run_aether_adapter_proof",
    "run_delivery_proof",
    "mx aether-live-proof",
    "mx aether-worker-proof",
    "mx aether-adapter-proof",
    "cli.main scale-control-plane",
    "cli.main scale-worker-load",
)


@dataclass(slots=True)
class SoakPolicyDecision:
    allowed: bool
    reason: str
    run_class: str
    run_seconds: int
    current_hour: int
    matched_processes: tuple[str, ...] = ()

    def render_lines(self) -> list[str]:
        lines = [
            f"policy_allowed={1 if self.allowed else 0}",
            f"policy_reason={self.reason}",
            f"policy_run_class={self.run_class}",
            f"policy_run_seconds={self.run_seconds}",
            f"policy_current_hour={self.current_hour}",
        ]
        if self.matched_processes:
            lines.append(
                "policy_matched_processes="
                + " || ".join(process.replace("\n", " ").strip() for process in self.matched_processes)
            )
        return lines


def _is_night_hour(hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _matching_processes(snapshot: str) -> tuple[str, ...]:
    matches: list[str] = []
    for line in snapshot.splitlines():
        if any(pattern in line for pattern in MX_PROCESS_PATTERNS):
            matches.append(line.strip())
    return tuple(matches)


def _read_process_snapshot() -> str:
    override = os.getenv("AETHER_SOAK_POLICY_PROCESS_SNAPSHOT")
    if override is not None:
        return override
    proc = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def evaluate_soak_policy(
    *,
    run_seconds: int,
    run_class: str,
    current_hour: int,
    process_snapshot: str,
    long_threshold_seconds: int = DEFAULT_LONG_THRESHOLD_SECONDS,
    night_start_hour: int = DEFAULT_NIGHT_START_HOUR,
    night_end_hour: int = DEFAULT_NIGHT_END_HOUR,
    allow_daytime_long_run: bool = False,
    allow_mixed_source_stress: bool = False,
) -> SoakPolicyDecision:
    normalized_run_class = run_class or CANONICAL_BASELINE
    if run_seconds < long_threshold_seconds:
        return SoakPolicyDecision(
            allowed=True,
            reason="short_run",
            run_class=normalized_run_class,
            run_seconds=run_seconds,
            current_hour=current_hour,
        )

    if not allow_daytime_long_run and not _is_night_hour(current_hour, night_start_hour, night_end_hour):
        return SoakPolicyDecision(
            allowed=False,
            reason="daytime_long_run_blocked",
            run_class=normalized_run_class,
            run_seconds=run_seconds,
            current_hour=current_hour,
        )

    if normalized_run_class == MIXED_SOURCE_STRESS and not allow_mixed_source_stress:
        return SoakPolicyDecision(
            allowed=False,
            reason="mixed_source_requires_explicit_opt_in",
            run_class=normalized_run_class,
            run_seconds=run_seconds,
            current_hour=current_hour,
        )

    matches = _matching_processes(process_snapshot)
    if normalized_run_class != MIXED_SOURCE_STRESS and matches:
        return SoakPolicyDecision(
            allowed=False,
            reason="mx_overlap_detected",
            run_class=normalized_run_class,
            run_seconds=run_seconds,
            current_hour=current_hour,
            matched_processes=matches,
        )

    return SoakPolicyDecision(
        allowed=True,
        reason="allowed",
        run_class=normalized_run_class,
        run_seconds=run_seconds,
        current_hour=current_hour,
        matched_processes=matches if normalized_run_class == MIXED_SOURCE_STRESS else (),
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate whether a soak run should start.")
    parser.add_argument("--run-seconds", type=int, required=True)
    parser.add_argument("--run-class", default=CANONICAL_BASELINE)
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    current_hour = int(os.getenv("AETHER_SOAK_POLICY_CURRENT_HOUR", str(datetime.now().hour)))
    allow_daytime = os.getenv("AETHER_ALLOW_DAYTIME_LONG_RUN", "0") == "1"
    allow_mixed_source = os.getenv("AETHER_ALLOW_MIXED_SOURCE_STRESS", "0") == "1"
    long_threshold_seconds = int(
        os.getenv("AETHER_SOAK_LONG_THRESHOLD_SECONDS", str(DEFAULT_LONG_THRESHOLD_SECONDS))
    )
    night_start_hour = int(os.getenv("AETHER_SOAK_NIGHT_START_HOUR", str(DEFAULT_NIGHT_START_HOUR)))
    night_end_hour = int(os.getenv("AETHER_SOAK_NIGHT_END_HOUR", str(DEFAULT_NIGHT_END_HOUR)))

    try:
        process_snapshot = _read_process_snapshot()
    except Exception as exc:  # pragma: no cover - defensive path for local environments
        print("policy_allowed=0")
        print("policy_reason=process_snapshot_unavailable")
        print(f"policy_error={type(exc).__name__}:{exc}")
        return 1

    decision = evaluate_soak_policy(
        run_seconds=args.run_seconds,
        run_class=args.run_class,
        current_hour=current_hour,
        process_snapshot=process_snapshot,
        long_threshold_seconds=long_threshold_seconds,
        night_start_hour=night_start_hour,
        night_end_hour=night_end_hour,
        allow_daytime_long_run=allow_daytime,
        allow_mixed_source_stress=allow_mixed_source,
    )
    for line in decision.render_lines():
        print(line)
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    sys.exit(main())
