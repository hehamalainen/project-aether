# Aether Ticket 0 Diagnostic Summary: Sizing & Latency under Contention

This summary documents the empirical sizing and latency telemetry compiled from the post-mortem PostgreSQL database instances (`aether_soak_pg_58650` and `aether_soak_pg_94558`) after executing the sustained high-concurrency Tier 5 treatment sweeps.

> [!NOTE]
> ### Operational Auditability Trade-Off
> In accordance with the host safety floor guidelines (to prevent disk exhaustion and reclaim resources), the preserved database instances were dropped post-compilation. This summary serves as an offline representation of that captured telemetry.

---

## 1. Sizing Footprint: Postgres TOAST vs. Main Table sizes

High-frequency concurrent writes on a shared repository under Phase 3 (Frontiers) and Phase 4 (Tokens) generate significant database sizing footprints. Running our `diagnostics.sql` tool against the post-mortem databases yields the following exact table allocations:

### Database: `aether_soak_pg_58650` (6,700 Promotions Committed)
*   **Total committed promotions**: 6,700 rows
*   **Total active repositories**: 1

| Schema | Table Name | Row Count | Table Size | Index Size | TOAST Size | Total Relation Size |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **public** | **aether_head_frontiers** | **1** | **0.008 MB** | **0.016 MB** | **505.211 MB** | **505.234 MB** |
| public | aether_promotions | 6,700 | 10.484 MB | 0.969 MB | 0.039 MB | 11.492 MB |
| public | aether_proposals | 6,700 | 2.938 MB | 2.070 MB | 0.039 MB | 5.047 MB |
| public | aether_snapshots | 6,700 | 2.328 MB | 0.320 MB | 0.039 MB | 2.688 MB |
| public | aether_gate_results | 6,700 | 1.508 MB | 0.305 MB | 0.039 MB | 1.852 MB |
| public | aether_heads | 6,700 | 1.031 MB | 0.523 MB | 0.039 MB | 1.594 MB |
| public | aether_agent_metrics | 160 | 0.336 MB | 0.016 MB | 0.039 MB | 0.391 MB |
| public | aether_repositories | 1 | 0.063 MB | 0.047 MB | 0.039 MB | 0.148 MB |

### Database: `aether_soak_pg_94558` (11,172 Promotions Committed)
*   **Total committed promotions**: 11,172 rows
*   **Total active repositories**: 1

| Schema | Table Name | Row Count | Table Size | Index Size | TOAST Size | Total Relation Size |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **public** | **aether_head_frontiers** | **1** | **0.102 MB** | **0.016 MB** | **475.938 MB** | **476.055 MB** |
| public | aether_promotions | 11,172 | 16.969 MB | 1.594 MB | 0.039 MB | 18.602 MB |
| public | aether_proposals | 11,185 | 4.313 MB | 3.180 MB | 0.039 MB | 7.531 MB |
| public | aether_snapshots | 11,172 | 4.008 MB | 0.563 MB | 0.039 MB | 4.609 MB |
| public | aether_gate_results | 11,172 | 2.359 MB | 0.602 MB | 0.039 MB | 3.000 MB |
| public | aether_heads | 11,172 | 1.594 MB | 0.789 MB | 0.039 MB | 2.422 MB |
| public | aether_agent_metrics | 160 | 0.141 MB | 0.016 MB | 0.039 MB | 0.195 MB |
| public | aether_repositories | 1 | 0.008 MB | 0.047 MB | 0.039 MB | 0.094 MB |

> [!IMPORTANT]
> ### Storage Sizing Insight
> The data **strongly suggests** that write-amplification TOAST bloat under high-frequency updates on a single row (due to PostgreSQL's MVCC tuple duplication leaving dead TOAST rows) is the primary storage driver inside the database footprint under high parallel write pressure. While the active JSON bytes representing the version vector is only ~1.48 MB in DB 58650 and ~2.16 MB in DB 94558, the TOAST table footprint dominates the database footprint at **505.211 MB** and **475.938 MB** respectively.

---

## 2. Rust-Authoritative RGA State Breakdown

By scanning RGA Candidate Documents immediately prior to the transaction commit, our Ticket 0 instrumentation captured the exact structural element ratios:

| Preserved Database | Promotions | Avg Visible Elements | Avg Tombstones | Avg Empty Non-Root | Avg Total Ops | Tombstone-to-Visible Ratio |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `aether_soak_pg_58650` | 6,700 | 5.22 | 0.02 | 0.00 | 5.26 | **0.38%** |
| `aether_soak_pg_94558` | 11,172 | 5.17 | 0.02 | 0.00 | 5.21 | **0.38%** |

*   **Tombstone Ratio**: The low tombstone count (average 0.02 tombstones per file RGA document) confirms that the concurrent treatment sweep was highly additive, inserting token-level elements with very low deletes under sustained concurrent agent updates.

---

## 3. Phase 3 Causal Frontiers Footprint

The footprint of Phase 3 causal frontiers represents the relationship between committed event counts in the version vectors and active JSON serialization:

*   **Database `aether_soak_pg_58650`**:
    *   `frontier_json` serialized size: **1,482,298 bytes** (~1.48 MB)
    *   Heuristic `HeadEvent` count: **6,577 events**
*   **Database `aether_soak_pg_94558`**:
    *   `frontier_json` serialized size: **2,163,596 bytes** (~2.16 MB)
    *   Heuristic `HeadEvent` count: **11,021 events**

---

## 4. Latency Splits: Isolated Task-Local Timing Metrics (ms)

Using our high-resolution `tokio::task_local!` static timing cell (`RGA_ALIGNMENT_TIME_NS`), we captured completely isolated latency metrics across committed promotions:

### Averages and Maximums across All Committed Promotions

| Telemetry Stage (ms) | DB 58650 Avg | DB 58650 Max | DB 94558 Avg | DB 94558 Max |
| :--- | :---: | :---: | :---: | :---: |
| **`t_pool_acquire_ms`** *(Pool Wait)* | **375.359 ms** | **2,191.078 ms** | **689.919 ms** | **5,534.270 ms** |
| **`t_rga_alignment_ms`** *(WF Align)* | **0.002 ms** | **0.299 ms** | **0.011 ms** | **0.528 ms** |
| **`t_crdt_eval`** *(CRDT Merges)* | **0.277 ms** | N/A | **0.473 ms** | N/A |
| **`t_materialize`** *(FS Writes)* | **0.493 ms** | N/A | **0.865 ms** | N/A |
| **`t_db_commit`** *(Transaction Commit)* | **0.616 ms** | N/A | **0.935 ms** | N/A |

### Explanatory Timing Context
1.  **Connection Pool Wait**: The metric `t_pool_acquire_ms` measures connection pool acquisition and transaction-start wait times under concurrent client requests rather than an isolated, clean row-lock timer. The high wait times (averaging **375.36 ms** in DB 58650 and rising to **689.92 ms** in DB 94558) reflect connection-pool and database transaction-start pressure under massive parallel scaling.
2.  **RGA Wagner-Fischer Alignment**: The task-isolated sequence-alignment timings average **0.002 ms** and **0.011 ms** as a population-wide metric. 
    > [!IMPORTANT]
    > **Algorithmic Cost vs. Population Average**  
    > Because many committed promotions are linear pushes or do not trigger overlapping concurrent modifications, their RGA alignment times are recorded as `0.000 ms`. The population averages are therefore heavily skewed by these zero rows and serve as a population metric rather than a clean statement of the algorithmic execution cost of individual Wagner-Fischer runs. The actual algorithmic cost on rows where sequence alignment was fully executed peaked at **0.299 ms** and **0.528 ms** respectively.

---

## Appendix: Raw PostgreSQL Sizing Output

The raw output of `diagnostics.sql` executed against `aether_soak_pg_58650` is printed below for full auditable transparency:

```
--- [1/2] PostgreSQL All User Tables Ranked By Size ---
 schemaname |          table_name          | row_count | table_size_mb | index_size_mb | toast_size_mb | total_size_mb 
------------+------------------------------+-----------+---------------+---------------+---------------+---------------
 public     | aether_head_frontiers        |         1 |         0.008 |         0.016 |       505.211 |       505.234
 public     | aether_promotions            |      6700 |        10.484 |         0.969 |         0.039 |        11.492
 public     | aether_proposals             |      6700 |         2.938 |         2.070 |         0.039 |         5.047
 public     | aether_snapshots             |      6700 |         2.328 |         0.320 |         0.039 |         2.688
 public     | aether_gate_results          |      6700 |         1.508 |         0.305 |         0.039 |         1.852
 public     | aether_heads                 |      6700 |         1.031 |         0.523 |         0.039 |         1.594
 public     | aether_agent_metrics         |       160 |         0.336 |         0.016 |         0.039 |         0.391
 public     | aether_repositories          |         1 |         0.063 |         0.047 |         0.039 |         0.148
 public     | _sqlx_migrations             |         4 |         0.008 |         0.016 |         0.008 |         0.031
 public     | aether_conflict_sets         |         0 |         0.000 |         0.008 |         0.008 |         0.016
 public     | aether_system_metric_events  |         0 |         0.000 |         0.008 |         0.008 |         0.016
 public     | aether_operation_graph_nodes |         0 |         0.000 |         0.008 |         0.008 |         0.016
(12 rows)

--- [2/2] Causal Frontier (aether_head_frontiers) Metrics ---
      repo_id      | frontier_json_bytes | headevent_count 
-------------------+---------------------+-----------------
 repo_8e07d2ddb9c9 |             1482298 |            6577
(1 row)
```
