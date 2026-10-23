# ADR 2026-02: Agentic Semantic Control Plane

- Status: Accepted (for implementation planning)
- Date: 2026-02-22
- Owners: Aether core team
- Source inputs:
  - `docs/inputs/deep-research-report.md`
  - "BEYOND TEXT: The Morphogenetic Codebase" whitepaper draft (user-provided)

## Context

Aether targets high-tempo, multi-agent software integration where line-diff Git workflows become a bottleneck. Current evidence supports:

- Event-oriented ingestion and promotion is required for machine-speed throughput.
- File/line conflict detection is insufficient for behavioral safety.
- Deterministic replay, provenance, and hard gates are mandatory for trust.

At the same time, claims like "proof of correctness" or "conflicts disappear" are not defensible without strict falsification and long-run evidence.

## Decision

Aether will execute a staged architecture with falsifiable gates:

1. **Operation-centric core (now)**  
   Keep append-only ingest, queued promotion, deterministic replay, and evidence-gated promotion as the canonical core.

2. **Semantic conflict units (near-term)**  
   Expand parser adapters (tree-sitter and language-specific symbol extraction) to reduce coarse file-level fallback and improve non-conflicting parallelism.

3. **AST-native mutator interface (AIP) (near-term)**  
   Add structured mutation protocol (`insert_node`, `move_node`, `delete_node`, `update_node`) in parallel with diff input, with a Git bridge maintained.

4. **Ledger and zk constraints (phase-2 pilot only)**  
   Treat event-sourced ledgers and zk proofs as targeted pilots for bounded invariants, not as a blanket replacement for all storage/verification.

5. **Human role as adversarial steering (ongoing)**  
   Move humans toward policy, stress scenarios, and red-team orchestration while retaining hard production guardrails.

## Non-Negotiable Constraints

- Behavioral safety gates remain the final merge firewall (tests/static/security/policy).
- Semantic disjointness is treated as a **parallelization hint**, not a correctness proof.
- Every promoted change must remain replayable with provenance.
- No architectural phase advances without objective artifact-backed gate passes.

## Falsification Criteria (Go/No-Go)

The thesis is considered invalid for current stage if any of these fail under strict soak profile:

- `lambda_accepted <= 0.9 * mu_promoted` sustained over configured window.
- `ack_to_promote_p95_ms < 60000` (or agreed stage threshold).
- Backlog stays bounded (watermark oscillation, no monotonic divergence).
- `semantic_fallback_rate < 0.05` for target language mix.
- Deterministic replay reproduces identical head hash for repeated runs.

## Immediate Implementation Scope (next stages)

- Parser/symbol adapter expansion for non-Python repositories.
- AIP mutator schema and ingestion path.
- Promotion parallelism bounded by conflict-group disjointness.
- Runtime metrics and artifact reporting for per-phase timing.

## Explicitly Deferred

- Full "Git replacement" positioning in production.
- Universal zk proofing of arbitrary program semantics.
- Full replacement of relational projections with ledger-only runtime.

## Consequences

Positive:

- Engineering direction stays ambitious but testable.
- Claims become evidence-based and investor/demo safe.
- Architecture can evolve without breaking interoperability.

Costs:

- Requires continuous benchmark discipline and long-run soak evidence.
- Slower narrative than "instant replacement" messaging.
- More instrumentation and operational complexity.
