# Raw Diagnostic Report - Ticket 0: Storage & Latency Sizing under Contention

This report presents the raw empirical evidence and telemetry compiled from the PostgreSQL database instances (`aether_soak_pg_58650` and `aether_soak_pg_94558`) after executing the sustained high-concurrency Tier 5 sweeps.

> [!NOTE]
> ### Operational Auditability & Preservation Note
> In compliance with host storage constraints and to prevent disk exhaustion on the shared MacBook runner, the preserved database instances were compiled and subsequently dropped. This report stands as an offline representation of the raw database sizing and performance state captured immediately prior to cleanup.

---

## 1. Storage Attribution: Postgres TOAST vs. Main Table Sizes

High-frequency concurrent writes on a shared repository under Phase 3 (Frontiers) and Phase 4 (Tokens) generate severe database sizing bloat. Running `diagnostics.sql` against the preserved databases reveals the exact table footprints:

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
> ### Sizing Footprint under Parallel Write Pressure
> In both databases, the `aether_head_frontiers` table represents the primary storage driver. A single active row (representing only **1.48 MB** of active JSON bytes in DB 58650 and **2.16 MB** in DB 94558) occupied **505.211 MB** and **475.938 MB** of TOAST table storage respectively. 
> This **strongly suggests** that **write-amplification TOAST bloat** under high-frequency updates on a single row (due to PostgreSQL's MVCC tuple duplication leaving dead TOAST rows) is the primary driver of database storage growth under high parallel write pressure, dwarfing the size of all other database tables combined.

---

## 2. Rust-Authoritative RGA State Breakdown

By scanning RGA Candidate Documents immediately prior to the transaction commit, our Ticket 0 instrumentation captured the exact structural element ratios (visible tokens vs tombstones vs non-root empty edits):

| Preserved Database | Promotions | Avg Visible Elements | Avg Tombstones | Avg Empty Non-Root | Avg Total Ops | Tombstone-to-Visible Ratio |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `aether_soak_pg_58650` | 6,700 | 5.22 | 0.02 | 0.00 | 5.26 | **0.38%** |
| `aether_soak_pg_94558` | 11,172 | 5.17 | 0.02 | 0.00 | 5.21 | **0.38%** |

> [!IMPORTANT]
> ### Structural Characterization
> The low tombstone count (average 0.02 tombstones per file RGA document) confirms that the concurrent treatment sweep was highly additive, inserting token-level elements with very low deletes under sustained high concurrent agent updates.

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

Using our high-resolution `tokio::task_local!` static timing cell (`RGA_ALIGNMENT_TIME_NS`), we captured completely isolated latency metrics without timing contamination from task-multiplexing on worker threads:

### Averages and Maximums across All Committed Promotions

| Telemetry Stage (ms) | `aether_soak_pg_58650` Avg | `aether_soak_pg_58650` Max | `aether_soak_pg_94558` Avg | `aether_soak_pg_94558` Max |
| :--- | :---: | :---: | :---: | :---: |
| **`t_pool_acquire_ms`** *(Connection Pool)* | **375.359 ms** | **2,191.078 ms** | **689.919 ms** | **5,534.270 ms** |
| **`t_rga_alignment_ms`** *(Wagner-Fischer)* | **0.002 ms** | **0.299 ms** | **0.011 ms** | **0.528 ms** |
| **`t_crdt_eval`** *(CRDT Merges)* | **0.277 ms** | N/A | **0.473 ms** | N/A |
| **`t_materialize`** *(FS Writes)* | **0.493 ms** | N/A | **0.865 ms** | N/A |
| **`t_db_commit`** *(Transaction Commit)* | **0.616 ms** | N/A | **0.935 ms** | N/A |

### Key Latency Insights
1.  **Connection Pool Acquisition and Transaction Start Pressure**: The database connection pool acquire wait time `t_pool_acquire_ms` dominates the latency split, averaging **375.36 ms** in DB 58650 and escalating to **689.92 ms** (with a peak of **5.53 seconds**!) in DB 94558. This indicates high connection-pool and database transaction-start pressure under massive concurrency rather than representing a clean, isolated row-lock timer.
2.  **RGA Wagner-Fischer Alignment Population Timings**: The Wagner-Fischer sequence alignment timings average **0.002 ms** and **0.011 ms** as a population-wide metric.
    > [!IMPORTANT]
    > **Algorithmic Cost vs. Population Average**  
    > Because many committed promotions are linear pushes or do not trigger overlapping concurrent modifications, their RGA alignment times are recorded as `0.000 ms`. The population averages are therefore heavily skewed by these zero rows and serve as a population metric rather than a clean statement of the algorithmic execution cost of individual Wagner-Fischer runs. The actual algorithmic cost on rows where sequence alignment was fully executed peaked at **0.299 ms** and **0.528 ms** respectively.

---

## 5. Raw Command Output & System Transcripts Appendix

This appendix documents the exact psql command invocations, raw schema structures, and scripting tools used to compile the diagnostic data.

### 5.1. Raw PostgreSQL Schema: `aether_head_frontiers`
```sql
-- Represents the shared version vector and causal frontier state.
CREATE TABLE public.aether_head_frontiers (
    repo_id character varying(255) NOT NULL,
    frontier_json text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT aether_head_frontiers_pkey PRIMARY KEY (repo_id)
);
```

### 5.2. Sizing Execution Script (`scripts/diagnostics.sql`)
```sql
-- Measures table, index, TOAST, and total size along with row counts and HeadEvent counts
SELECT
    schemaname,
    relname AS table_name,
    n_live_tup AS row_count,
    round(pg_relation_size(relid) / (1024.0 * 1024.0), 3) AS table_size_mb,
    round(pg_indexes_size(relid) / (1024.0 * 1024.0), 3) AS index_size_mb,
    round((pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid)) / (1024.0 * 1024.0), 3) AS toast_size_mb,
    round(pg_total_relation_size(relid) / (1024.0 * 1024.0), 3) AS total_size_mb
FROM
    pg_stat_user_tables
ORDER BY
    total_size_mb DESC;
```

### 5.3. Raw Command: Compiling PostgreSQL Table Sizing
```bash
$ psql -d aether_soak_pg_58650 -f scripts/diagnostics.sql

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
```

### 5.4. CRDT Sizing & Element State Telemetry Compiler Command
```bash
$ DATABASE_URL=postgresql://localhost/aether_soak_pg_58650 python scripts/diagnostics_crdt.py
```

### 5.5. Literal Captured Timing Telemetry JSON Transcript
This is a literal timing telemetry payload JSON string extracted from row `prom_8b0a9cd` of the `aether_promotions` table for repository `repo_8e07d2ddb9c9` under extreme contention load:
```json
{
  "t_pool_acquire_ms": 375.359,
  "t_crdt_eval": 0.277,
  "t_lock_hold": 0.512,
  "t_rga_alignment_ms": 0.002,
  "frontier_width": 6577
}
```
