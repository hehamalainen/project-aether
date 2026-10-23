# Aether 2-Week Execution Board (Architecture Bottlenecks)

Window: 2026-02-26 -> 2026-03-11  
Goal: reduce queue growth risk and improve natural swarm convergence

## North-star success criteria

- 2 consecutive strict runs pass with bounded backlog trend.
- Intake pressure on the same path is controlled automatically.
- Promotion throughput improves without increasing fallback rate.

## Workstream A - Natural intake shaping (same-file hotspot control)

Owner: Codex (implementation), Copilot (tests), Claude (failure triage)

Tasks:
- Add per-path active proposal limiter at API intake.
- Return `429` with explicit hotspot reason and retry guidance.
- Add tests for hotspot throttle and active-path counting semantics.

Exit gate:
- `tests/aether/test_backpressure_api.py` and `tests/aether/test_services.py` pass.
- New throttle reason appears in API response: `path_hotspot_exceeded`.

## Workstream B - Promotion service-rate uplift

Owner: Codex (engine changes), Claude (profiling), Copilot (regression suite)

Tasks:
- Add phase-budget aware prioritization for proposals with low contention.
- Reduce hot-path recomputation in promotion iteration.
- Run A/B strict harness and compare promotion throughput.

Exit gate:
- 2 strict runs with improved `promotion_throughput`.
- No regression in `submit_p95` and `fallback_rate`.

## Workstream C - Determinism and safety guardrails

Owner: Copilot (test expansion), Claude (edge-case scenarios), Codex (fixes)

Tasks:
- Add replay consistency checks under mixed diff + AIP inputs.
- Add regression tests for fallback behavior under parser failures.

Exit gate:
- deterministic replay tests pass.
- fallback telemetry remains observable by language/path.

## Workstream D - Demo and communication readiness

Owner: Antigravity (storyline), Codex (technical facts), Claude (runbook validation)

Tasks:
- Keep P0-P4 green and dashboard recoverable.
- Prepare one primary and one fallback demo path.
- Keep artifact links updated in status docs.

Exit gate:
- latest P-gate artifacts linked in `docs/swarm_chunk_status.md`.

## Day-by-day sequence

Week 1:
- Day 1-2: Workstream A complete and merged.
- Day 3-4: Workstream B first pass + strict run pair.
- Day 5: triage + patch for regressions.

Week 2:
- Day 6-7: Workstream C tests and fixes.
- Day 8: Workstream B second optimization pass.
- Day 9: strict run pair + artifact review.
- Day 10: final review, no-go/go for next long soak.
