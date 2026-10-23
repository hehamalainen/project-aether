# Sprint 4E Causal Frontiers Design Spike — Targeted Extension Report

**Author:** Antigravity (AI Lead Architect)  
**Workspace:** `<REPO_ROOT>` (Verified Local Path)  
**Status:** Completed Gated Extension Sweep (Stronger Physical-Feasibility Probe)  
**Baseline Parameters:** Concurrency $N = 80$ threads, Iterations $I = 150$ writes/thread ($12,000$ total transactions)  

---

## 1. Executive Summary

This report compiles the findings from our **Targeted Spike Extension** on Causal Frontiers relational storage layouts under Postgres 14. This extension acts strictly as a stronger physical-feasibility probe. It does **not** constitute a decision-grade closeout, and **all implementation remains strictly gated**.

We extended our zero-dependency microbenchmark script (`scratch/simulate_append_only.py`) to measure:
1. **A Partial Event-Log Replay Benchmark**: Formulated a dedicated `sim_frontier_events` table representing Candidate 2. The read-path query runs a GROUP BY max clock aggregation to measure a proxy for delta-log read overhead.
2. **A Matched-Load Compaction Test**: Spawned background write threads during the compaction pass under matching concurrency conditions ($N=80$ threads).
3. **A Post-Compaction Footprint Probe**: Stopped background write threads and ran a clean pruning pass followed by `VACUUM ANALYZE` to capture the post-compaction storage metrics.

While these results provide stronger physical-feasibility data, **crucial experimental design limitations and residual bottlenecks remain, keeping implementation strictly gated**:
- **Compaction Stall Telemetry is Not Isolated**: Background write latencies were collected across the entire write worker lifetime (including 0.5s before and after compaction executed). Because the timing population includes periods without active compaction, the compaction P99 write-stall metrics are diluted. True write stalls during active `DELETE` + `VACUUM` under load have **not** been isolated or conclusively proven to be absent.
- **Post-Compaction Footprint Shows Index Bloat**: While base table size was reduced, the post-compaction base relation still contains **258 dead tuples** and the index size actually *expanded* post-compaction (growing from **0.8281 MB** pre-compact to **0.8984 MB** post-compact). This does **not** match the clean footprint of UPDATE-in-place (which has exactly 0 dead tuples and a tiny **0.0156 MB** index), and does not prove the absence of infinite index expansion.
- **Option B is Only Partially Benchmarked**: The benchmarked query groups by `head_id` and computes `MAX(clock)`. It lacks the `PRUNE_HEAD` event semantics and deterministic causal replay/backtracking required by a full event-sourced causal log. Consequently, Option B is **not** disqualified.

From a physical storage and write-latency perspective, **the implementation gate is NOT lifted, and Option A is NOT authorized for implementation.**

---

## 2. Read-Path Reconstruction Latency Growth Sweep

To evaluate read viability, we bulk-seeded the database and measured pure query planning and execution times via `EXPLAIN (ANALYZE, FORMAT JSON)` under Postgres 14, isolating database performance from connection and process startup overhead.

* **Option A (Latest-Gen State Log)**: Retrieves the latest full vector from `sim_append_frontiers` using a `LIMIT 1 DESC` index scan on the B-tree index `(repo_id, repo_revision_id DESC)`.
* **Option B Proxy (Delta Event Group-By)**: Aggregates node clocks from `sim_frontier_events` using a GROUP BY query with a max clock filter:
  ```sql
  SELECT head_id, MAX(clock) 
  FROM sim_frontier_events 
  WHERE repo_id = 'repo_1' AND repo_revision_id <= :depth 
  GROUP BY head_id;
  ```

### Read Latency Growth Matrix (ms)

| Log Depth (Events) | UPDATE-in-place (Key Lookup) | Option A: Latest-Gen (`LIMIT 1 DESC` Index Scan) | Option B Proxy: Delta Event Group-By |
| :--- | :---: | :---: | :---: |
| **1,000** | 0.4450 ms | 0.4890 ms | 0.9769 ms (2.0x slower) |
| **5,000** | 0.4000 ms | 0.7008 ms | 2.2470 ms (3.2x slower) |
| **11,000** | 0.3568 ms | 0.5285 ms | 4.2961 ms (8.1x slower) |

### Key Findings & Analysis:
* **Option A Flat Cost**: Option A maintains flat, constant-time read latencies (**0.48 ms to 0.70 ms**) that scale independently of log depth.
* **Option B Proxy Sizing**: While Option B Proxy query shows linear latency growth (reaching **4.30 ms** at 11k depth), this query only measures simple grouping and clock aggregation. A true event-sourced causal log requires reconstructing pruning events and resolving out-of-order transaction sequences, which would add additional query and application-level overhead. Thus, this sweep is only a partial proxy for Option B.

---

## 3. Storage Footprint & Compaction Telemetry

The compaction pass unlinks and deletes historical state generations strictly below the maximum revision for a repository, then executes a `VACUUM` pass. 

### Simulation Sizing & Compaction Matrix

| Sizing Metric | UPDATE-in-place | Append Pre-Compact | Append Post-Compact |
| :--- | :---: | :---: | :---: |
| **Committed Transactions** | 12,000 | 12,000 | 11,989 |
| **Base Table Size** | 1.1953 MB | 8.9062 MB | **1.1562 MB** |
| **TOAST Relation Size** | 0.0391 MB | 0.0391 MB | 0.0391 MB |
| **Total Index Size** | 0.0156 MB | 0.8281 MB | **0.8984 MB** |
| **Base Live / Dead Tuples** | 1 / 0 | 11,989 / 0 | **1 / 258** |
| **TOAST Live / Dead Tuples** | 0 / 0 | 0 / 0 | 0 / 0 |

### Key Sizing Anomalies & Gaps:
* **Residual Dead Tuples**: Post-compaction, the base table still retains **258 dead tuples** from background insertions completed during the shutdown grace window.
* **Index Footprint Expansion**: The total index size *expanded* post-compaction from **0.8281 MB** to **0.8984 MB**, which is nearly 57 times larger than UPDATE-in-place's **0.0156 MB** index. This proves that while historical data was pruned in this run, the B-tree index remained bloated and expanded after the measured compaction pass. Multi-cycle behavior remains unproven.
* **Result**: Compaction does **not** cleanly recover a state matching UPDATE-in-place, and we have **not** proven that Option A prevents infinite index or storage footprint expansion.

### Compaction Execution & Write-Stall Telemetry ($N=80$ Matched Load)

To probe compaction liveness, we ran the compaction pass during an 80-thread background write storm:

* **Compaction Duration**: **1,726.67 ms** (complete pruning + VACUUM).
* **Write Latency Baselines (No Compaction)**:
  * P50: **520.93 ms**
  * P95: **766.96 ms**
  * P99: **950.28 ms**
* **Write Latencies DURING Active Compaction (Matched Load $N=80$)**:
  * P50: **497.60 ms**
  * P95: **619.43 ms**
  * P99: **719.72 ms**

> [!WARNING]
> **Compaction Stall Evidence is Diluted**:
> Because the background write threads started and ran for 0.5 seconds before compaction was triggered, and continued running for another 0.5 seconds after compaction completed, the timing population `compaction_write_latencies` includes periods without active compaction. Consequently, the write-path latencies are diluted and do **not** represent isolated write performance during the active `DELETE` + `VACUUM` window. Zero write stalls under load are **not** conclusively proven.

---

## 4. Decoupled Future Correctness Gate

> [!IMPORTANT]
> **Scope Disclaimer**: This microbenchmark confirms physical database feasibility (storage, index footprint, vacuum compaction, and write latencies). It does not validate, prove, or guarantee the logical correctness of concurrent version vector arbitration or revision-linearization in Aether application code.

A future decoupled test suite must validate:
1. **Linearization Invariants**: Advisory transaction locking or PostgreSQL serializable isolation must be verified to ensure gap-free, monotonic sequence generation and prevent concurrent revision race duplicates.
2. **Reconstruction Determinism**: The read-path reconstructed state must be mathematically proven to match the in-place equivalent version vector under all concurrent branch joins and forks.

---

## 5. Architectural Recommendations & Implementation Gate Status

Based on the findings and gaps identified in the Targeted Spike Extension, the go/no-go status is revised as follows:

1. **Option A exhibits flat read costs but residual footprint bloat**:
   * It exhibits flat read performance (0.48 ms to 0.70 ms) via B-tree index scan.
   * However, index size expands post-compaction (0.8984 MB) and dead tuples remain, showing that Option A does not achieve the clean, steady-state storage characteristics of UPDATE-in-place.
2. **Option B remains an active, un-invalidated candidate**:
   * Option B's event-log replay has only been partially proxied by a simple GROUP BY clock query. A true event-sourced causal log incorporating `PRUNE_HEAD` and deterministic causal graph replay has not yet been benchmarked. Thus, Option B is **not** disqualified.
3. **Compaction liveness and write-stall behavior remain unproven**:
   * Because write latencies were not isolated to the active compaction window, we have not proven the absence of write stalls under matched concurrency ($N=80$).
4. **Implementation Gated**:
   * **The implementation gate remains strictly locked, and transition to the implementation phase is NOT authorized.** The data is **not yet decision-grade**.
   * To lift the gate, the following subsequent steps are required:
     * Develop and run a faithful event-log replay benchmark for Candidate 2 that implements full prune and deterministic replay logic.
     * Isolate and measure write-path transaction latencies *strictly within the active compaction execution window* to prove the absence of write stalls under matched load concurrency ($N=80$).
     * Analyze and remediate the post-compaction index bloat and residual dead tuples to verify steady-state storage preservation.
