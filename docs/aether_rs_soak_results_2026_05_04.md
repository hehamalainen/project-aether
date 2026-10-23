# Rust Server Soak Results (Post Async I/O Patch)

**Date**: 2026-05-04
**Target Mode**: `external` (Rust server on `localhost:8100`)
**Run Class**: `mixed_source_stress`

## Overview

Following the migration of `std::fs` to non-blocking `tokio::fs` within the materialization path, we immediately ran another 5-minute canonical baseline soak. The results **definitively prove Copilot's adversarial review hypothesis**: Async I/O alone is not enough to hit our throughput and latency targets. The primary bottleneck is elsewhere.

## Key Metrics

| Metric | Target | Actual (Pre-Patch) | Actual (Post-Patch) | Status |
|---|---|---|---|---|
| **Submit Throughput** | > 40.0/s | 2.68/s | 3.89/s | ❌ FAILED |
| **Submit p95 Latency** | < 140 ms | 1868 ms | 1894 ms | ❌ FAILED |
| **System Stability** | Crash-free | Passed | Passed | ✅ PASSED (Fail-closed) |
| **Promotion Throughput** | > 0.2/s | 0.90/s | 1.93/s | ✅ PASSED |
| **Backlog Peak** | < 2000 | 1598 | 643 | ⚠️ HIGH |

## Analysis

The async I/O patch successfully eliminated filesystem blocking, allowing the `tokio` worker threads to breathe slightly better (promotion throughput roughly doubled from 0.90 to 1.93/s, and the backlog peak dropped by half). 

However, the submit throughput (3.89/s) is still an order of magnitude off our target (40/s). The system remains choked by massive intake throttling and ratio limiters.

### The Remaining Bottleneck

As Copilot noted during the plan review, the hot path still:
1. **Holds a long-lived SQLite transaction** open across the entire promotion/materialization process.
2. Performs CPU-bound `diffy` patching inline.
3. Performs CPU-bound `StructuredDocument` parsing inline.
4. Performs synchronous hashing inline.

Because the SQLite transaction (which acts as a write-lock in SQLite WAL mode) is held open while we do async I/O and heavy CPU work, no other `submit_proposal` transactions can commit during that entire 500+ millisecond window. This causes intense database contention, driving up `submit_p95` latency, which in turn causes the harness to throttle.

## Next Steps

We need to decouple the CPU-bound materialization from the database transaction. 

1. **Transaction Scoping**: Calculate the canonical hash, apply diffs, and generate the `StructuredDocument` *before* opening the final promotion transaction, or decouple the promotion metadata write from the physical snapshot materialization entirely.
2. **Batching**: Process materialization offline in a background tokio task.

## Later correction: why the transaction-scope refactor did not move soak latency

After the narrowed transaction-scope refactor was implemented and re-tested, the next mixed-source stress result still showed:

- `submit_throughput` around `3.65/s`
- `submit_p95_ms` around `1950 ms`

That initially looked like the refactor had failed to move the real bottleneck. The deeper cause turned out to be a harness-path mismatch rather than a direct failure of the Rust promotion refactor itself.

### Confirmed harness behavior

The continuous harness currently behaves asymmetrically in `target_mode == "external"`:

- proposal submission is routed to the external API;
- promotion is **not** routed to the external API.

Instead, the continuous harness still runs the local Python promotion path in `_promotion_loop(...)`:

- `packages/aether/services/swarm_harness.py`
- `_promotion_loop(...)`
- `promotion_engine.process_pending_proposals(...)`

This means the Rust server receives external `submit_proposal` requests, but the heavy promotion/materialization loop still executes in the Python process against the same SQLite database.

### Consequence

Because the Python promotion loop is still the actor taking the long-lived WAL write lock during promotion/materialization, Rust-side `submit_proposal` inserts can still block behind Python-side promotion work.

That makes the measured submit latency during the supposed Rust soak path misleading:

- the Rust server is participating in intake;
- the Python process is still dominating promotion lock contention.

### Updated interpretation

The transaction-scope refactor in Rust is still a valid optimization, but the recent soak result does **not** isolate its benefit correctly.

Before judging the Rust promotion-path optimization fairly, the harness promotion loop must be refactored so that, in `external` mode, it promotes through the external API rather than the in-process Python `PromotionEngine`.
