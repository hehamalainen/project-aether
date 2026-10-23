# Aether Response Memo - Copilot Review

Date: 2026-03-02
Audience: Internal architecture/reliability team and external technical stakeholders

## Purpose

This memo responds to recent Copilot feedback and aligns it with verified Aether run data.

## Executive Position

Copilot's strategic guidance is directionally correct, but generic. The operational truth is:

- Aether control-plane safety behavior works (bounded queue ending, deterministic gating path).
- Aether 1000-agent strict performance envelope is not yet where we need it.
- Current stability is being achieved with aggressive intake throttling, which degrades submit latency.

This is an engineering scaling gap, not a theory failure.

## What Copilot got right

- Control plane focus over model hype.
- Deterministic replay, auditability, and explainability as non-negotiables.
- Caution against premature "Git replacement now" messaging.
- Need for operator kill switches and explicit safety policy boundaries.

## What Copilot missed (repo-specific reality)

The feedback did not anchor to current soak outputs. Our latest strict phase data shows a repeated pattern:

- backlog_end = 0 (good)
- submit_p95 gate fails (bad)
- window_rho often > 1.0 in 60m passes (bad)
- throttled attempts ~99%+ (protective but costly)

Interpretation: queue safety is enforced, but at the cost of latency and intake smoothness.

## Verified evidence snapshot

From `.pytest-artifacts/phase1` runs dated 2026-03-02:

- 15m gate: submit_tps 1.40, promo_tps 1.13, rho 1.00, submit_p95 ~11.2s, backlog_end 0
- 60m pass1: submit_tps 1.34, promo_tps 1.06, rho 1.05, submit_p95 ~6.8s, backlog_end 0
- 60m pass2: submit_tps 1.39, promo_tps 1.10, rho 1.04, submit_p95 ~36.7s, backlog_end 0

Common strict failures: `submit_p95_ok`, and in 60m runs `window_rho_ok`.

## Decision

1. Keep strict performance gates active (do not hide true bottlenecks).
2. Keep demo lane and performance lane separated operationally.
3. Prioritize reducing throttle dependence by improving service-rate vs intake balance.
4. Continue overnight evidence collection and trend-based decisions (not single-run wins).

## Next engineering phase (current)

1. Backpressure calibration under short controlled runs to reduce pathological throttling while preserving bounded queue behavior.
2. If calibration alone cannot pass strict latency/rho gates, move to service-rate uplift in promotion path.
3. Re-validate with 15m + 2x60m strict sequence and compare trend against baseline.
4. Trigger formal peer review only after this phase is complete with artifact package.

## Peer review entry criteria

We request peer review when the following are true in repeated long runs:

- backlog remains bounded without near-total throttling behavior
- window_rho remains <= 1.0
- submit_p95 remains within target
- failures are deterministic and diagnosable when they occur

## Positioning guidance (external)

Near-term framing:

"Aether is an agent-safe coordination substrate under active hardening for high-concurrency autonomous development."

Avoid near-term absolute claims:

- Do not claim full Git replacement now.
- Do not claim 1000-agent production readiness until strict long-run evidence supports it.
