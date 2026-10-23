# Aether Stage 3 PG Baseline Closeout (Historical Synthetic Contention Benchmark)

**Date**: May 10, 2026  
**Subject**: Stage 3 Synthetic Harness Run (PostgreSQL-Backed Rust Runtime, `--features db-postgres`)

> [!IMPORTANT]
> **Workload & Evidence Qualification:**
> - **Synthetic Full-File Replacement Workload:** This benchmark was driven by [`scripts/soak_native_rust_pg.sh`](../scripts/soak_native_rust_pg.sh), in which 50 bash agent loops across 10 repositories submit 3-line `"full_file"` replacements (`src/agent_<id>_file.py`) and configure the repository gate to `echo SOAK_GATE_OK`.
> - **CRDT Mode Not Recorded in This Summary:** Although `scripts/soak_native_rust_pg.sh` supports an optional `SOAK_CRDT_ENABLED=1` flag (`AETHER_CRDT_MODE=true`), whether CRDT mode was enabled during this May 10, 2026 run is **not recorded/verified** in the preserved summary, and the harness payload itself uses `"type": "full_file"` file replacements rather than character-level RGA delta editing or AST semantic merges.
> - **Raw Runtime Logs Not Bundled:** The ephemeral PostgreSQL database (`aether_soak_pg_XXX`) and `/tmp/aether_soak_pg_*` logs from this run are not bundled in the repository. See [`docs/sprint4d_topology_baseline.md`](sprint4d_topology_baseline.md) and [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md) for subsequent topology sweeps that documented severe PostgreSQL table bloat (`15 MB` to `340 MB`) and `90.1%–95.3%` HTTP `409` contention when 50–100 agents target a single repository.

## Context
Following earlier SQLite experiments, stability testing was executed against the PostgreSQL-backed Rust server (`--features db-postgres`) using [`scripts/soak_native_rust_pg.sh`](../scripts/soak_native_rust_pg.sh). The harness checked the following database and process invariants across the 1-hour run:

1. **Explicit Binary Check**: Verified execution against the compiled `aether-server` binary built with `--features db-postgres`.
2. **Postgres Provisioning**: Created an ephemeral PostgreSQL database (`aether_soak_pg_XXX`) for the run.
3. **Per-Repo Sequence Invariants**: Checked exact equality per repository for `head_sequence == promotion_count == snapshot_count`.
4. **Conservation Invariant**: Checked that `submit_ok - promote_ok` equaled the remaining `PENDING` backlog size for every repository.
5. **Failure Counters**: Checked for zero submit HTTP failures, zero HTTP `5xx` responses, and zero gate rejections under the `echo SOAK_GATE_OK` gate.
6. **Multi-Repo Topology**: Distributed `50` agent loops across `10` repositories (`5` agents per repository on average).
7. **Poisson Contention Formula**: Compared the per-repository HTTP `409` conflict ratio against a heuristic Poisson collision formula implemented in the script: `P(409) = 1 - e^(-(N-1)T/I) + 0.10 margin`.

## Recorded Summary Metrics (`50` Agent Loops Across `10` Repositories)

| Metric | Value |
|--------|-------|
| Duration | `3,600s` active + `30s` drain |
| Submit OK (`200`/`201`) | `37,715` |
| Submit Fail | `0` (`0%`) |
| **Promote OK (`200`)** | **`17,363` (~`4.82` promotions/s across 10 repos)** |
| **Promote Contention (`409 Conflict`)** | **`20,352` (`53.96%` of promotion attempts returned HTTP `409`)** |
| Promote Rejected / `5xx` | `0` / `0` |
| **Per-Repo Sequence & Conservation Checks** | **Passed (`0` per-repo DB counter violations)** |
| **Head/Snapshot Integrity Check** | **`ok`** |

### Harness Verdict Checks
Within the bounds of the synthetic 10-repository harness:
- Server process remained alive at end of run
- Per-repo counter invariants held (`head_sequence == promotion_count == snapshot_count`)
- Zero starved repos (every repo advanced past sequence 0)
- Global backlog conservation held (`submit_ok - promote_ok == pending`)
- Submit failure rate `< 1%` (`0%`)
- Promote `5xx` rate `< 0.1%` (`0%`)
- Head/snapshot row integrity `ok`
- All promotions recorded corresponding head and gate rows
- **Per-repo HTTP `409` contention (`53.96%`)**: Fell below the script's `~60.3%` heuristic Poisson threshold.

## Methodological Limitations & Interpretation
1. **Hardcoded `175ms` Fallback Service Time in Poisson Threshold:** In this run, the harness script used its hardcoded **`175ms` fallback** service time (`T=0.175s`) to compute the `~60.3%` allowable HTTP `409` threshold because `latency=` lines were not emitted at the server's `info` log level. Passing a self-referential Poisson formula with a hardcoded fallback service time is **not** proof of workload validity, general scalability, content correctness, or real test-suite performance.
2. **High Contention Even at 5 Agents per Repo (`53.96%` HTTP `409`):** More than half of all promotion attempts (`20,352` out of `37,715`) were rejected with HTTP `409 Conflict` even with only 5 agents per repository.
3. **No Semantic Merge or Real Test Gate Verification:** All proposals replaced a 3-line file (`full_file`) and executed `echo SOAK_GATE_OK`. This run measures PostgreSQL row-lock/CAS accounting and process liveness on the Rust control plane, not CRDT semantic convergence or real verification gates.

## Conclusion
Under the synthetic 50-agent / 10-repository `full_file` workload with `echo SOAK_GATE_OK` gates, the Rust PostgreSQL server completed a 3,600-second run with `17,363` committed promotions, `20,352` (`53.96%`) HTTP `409` contention responses, zero HTTP `5xx` errors, and exact per-repository counter conservation (`head_sequence == promotion_count == snapshot_count`). Subsequent higher-density topology sweeps (`docs/sprint4d_topology_baseline.md`) showed that scaling higher agent-to-repository ratios requires addressing PostgreSQL MVCC bloat and per-repository lock serialization.
