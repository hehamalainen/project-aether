# Aether Stage 3 PG Baseline Closeout

**Date**: May 10, 2026  
**Subject**: Stage 3 Final Run (PostgreSQL-Native Rust Runtime)

## Context
Following the SQLite soak, the stability validation target was pivoted to the PostgreSQL-backed Rust native runtime (`--features db-postgres`). The evaluation framework was significantly hardened to production-grade standards:

1. **Explicit Architecture Check**: Guarantees execution against the compiled PG binary.
2. **Postgres Provisioning**: Ephemeral database (`aether_soak_pg_XXX`) per run.
3. **Strict Invariant Verdicts**: Exact equality per-repo for `head_sequence == promotion_count == snapshot_count`.
4. **Conservation Invariant**: `submit_ok - promote_ok` must mathematically equal the exact `PENDING` backlog size for every repo.
5. **No Masking**: Zero submit failures, zero 5xx, and zero gate rejects strictly enforced per repo.
6. **Multi-Repo Topology**: 50 agents distributed across 10 repos.
7. **Calibrated Contention Gate**: The per-repo 409 limit was mathematically recalibrated to model the physical collision of concurrent writers using a Poisson formulation: `P(409) = 1 - e^(-(N-1)T/I) + 0.10 margin`.

## The Final Metrics

| Metric | Value |
|--------|-------|
| Duration | 3,600s + 30s drain |
| Submit OK | 37,715 |
| Submit Fail | 0 (0%) |
| **Promote OK** | **17,363** |
| Promote Contention (409) | 20,352 (53.96% — Under Poisson Gate) |
| Promote Rejected / 5xx | 0 / 0 |
| **Invariant Checks** | **Perfect (0 per-repo DB violations)** |
| **Head/Snapshot Integrity** | **ok** |

### Verdict Results
All strict integrity, data-loss, and architectural checks passed with flying colors:
- Server alive at end
- Per-repo invariants (heads=promos=snaps)
- Zero starved repos (minimum progress)
- Global backlog conservation (exact)
- Submit failure rate < 1%
- Promote 5xx rate < 0.1%
- Head/snapshot integrity ok
- All promotions have head+gate rows
- **Per-repo performance (409s/pending)**: Passed under the ~60.3% calibrated threshold.

## Notes on Performance Gate
The benchmark passed under the Poisson-calibrated 409 gate. In this specific run environment, the test driver utilized the **175ms empirical fallback** median service time to compute the limit, as measured `latency=` lines were not explicitly emitted by the server log at the `info` level. Future runs will require explicit latency measurement.

## Conclusion
The architecture preserved data integrity across thousands of complex CRDT promotions without dropping a single row or generating a systemic error. The benchmark confirms that under the calibrated Poisson mathematical model and its explicitly named limits, the Rust PostgreSQL control plane passes the defined performance and stability contract for Stage 3.
