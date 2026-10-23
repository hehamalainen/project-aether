# Sprint 2A: 200-Agent Scaling — Stage 6 Topology Design

## 1. Topology Choice
**Configuration:** 40 Repositories, 5 Agents per Repository (`40×5`)

**Justification:** 
We must preserve the per-repo contention density established and calibrated during Sprints 1B, 1C, and 1D (which used 10 repos at 5 agents/repo). By scaling to 40 repos at 5 agents per repo, we explicitly test the system's ability to scale *horizontally* across a larger state space without confounding the collision regime. 
Alternatives like 20 repos × 10 agents or 10 repos × 20 agents would artificially spike the per-repo contention rate, invalidating the carefully calibrated collision probability math we just verified. Scaling via repo count ensures we are strictly testing the global PostgreSQL and Rust server throughput ceilings, not local row-lock contention.

## 2. Calibrated Gate Model
Because the per-repo density remains strictly at 5 agents/repo, the mathematical model driving the per-repo Poisson `max_409_rate` remains identical to our Sprint 1D calibration. The existing harness (`scripts/soak_native_rust_pg.sh:439-448`) dynamically recomputes this threshold based on the measured median latency (`T`) for each run. Thus, while the threshold itself may adjust if `T` rises under 200-agent load, the calibration mechanism natively handles this without requiring harness modifications.

> [!IMPORTANT]
> **Aggregate Throughput Extrapolation:** 
> A linear extrapolation from Stage 4 (18 TPS at 50 agents) forecasts an aggregate throughput of **~72 TPS** at 200 agents. 
> This is framed purely as an extrapolation forecast, **NOT** a pass/fail gate. Given Sprint 1D proved the existence of shared system-level latency (general DB instance saturation and executor scheduling friction), we fully anticipate that throughput may scale sub-linearly at 200 agents. Finding sub-linear scaling is a valid and expected empirical result, not a failure condition.

## 3. Integrity Contract
All existing platform invariants must be explicitly preserved without any additions. "No new invariants" means we keep the existing rigid checks, mapping precisely to the current harness capabilities:

1. **Chained Invariants:** 
   - *Per-Repo:* `head_sequence == promotion_count == snapshot_count_db` with a permitted delta of 0 across all 40 repos.
   - *Global Parity:* `DB snapshots == disk snapshots` across the entire storage layer.
2. **Backlog Conservation:** `submit_ok - promote_ok == PENDING` exactly, tracked independently per repo.
3. **Conservation Accounting:** Zero-delta divergence per repo on accounting metrics.

## 4. Hypothesis Expectations (Pattern A/B/C)
Based on Sprint 1D's analyzer readout (which proved latency is a shared system-level friction rather than isolated to a single query), our expectations at 200 agents are asymmetric:

* **Expected:** **Pattern A (Pool Saturation)** and **Pattern B (WAL Back-pressure)** may become more visible and frequent, but they should **NOT** become singularly dominant (>80% zero-idle or >50% active wait) if the shared-friction thesis holds.
* **Expected to Stay Quiet:** **Pattern C (Transaction-Scope Hold)** must remain low (it was cleanly rejected at 4.2% occupancy in 1D). If `idle in transaction` rises materially at 200-agent scale, it represents a new architectural finding requiring immediate investigation.
* **Counted as New Findings (Anomalies):** 
    - Sustained zero-idle pool collapse (Pattern A dominant).
    - Dominant WAL/transaction lock waits (Pattern B dominant).
    - Per-repo or global integrity drift (failing the established integrity contract).

## 5. Soak Sequence
The scaling sprint will progress through three sequential, gated stages. Each stage is gated on the strict clean closeout (integrity and profiling) of the previous one:
1. **Stage 6:** 60-Minute Canary (Profile & Validate)
2. **Stage 7:** 4-Hour Extended Soak (Stability & Back-pressure)
3. **Stage 8:** 12-Hour Overnight Soak (Memory, File Descriptor leaks, WAL rollover)

## 6. Harness Change Inventory
**Verification:** The harness at `scripts/soak_native_rust_pg.sh` was audited. It is entirely parameterized dynamically via `SOAK_REPOS` and `SOAK_AGENTS` (specifically lines 27-29, 113-115, 181-198, 314-319, 379-407, 439-448). The agent-to-repo assignment is dynamically mapped using modulo arithmetic (`r_idx=$(( (i - 1) % SOAK_REPOS ))`), and the audit queries perform global counts across all repositories.

**Inventory:** 
No harness modifications are required. The current harness natively supports the `40×5` configuration for Stage 6.

---

> [!NOTE]
> This design document is ready for Copilot CLI adversarial review. Please submit to Copilot before authorizing execution of Phase 2 (Stage 6).
