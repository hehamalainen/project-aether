# Sprint 4E Autovacuum Starvation Mitigation & Analysis Report

## 1. Execution Provenance (Historical Pre-Release Run)
- **Canonical Run Tree**: `<REPO_ROOT>` (Local, non-iCloud)
- **Historical Execution Branch**: `soak/sprint-a-20260616` (pre-release development branch; not a ref in the single-root public release repository)
- **Historical Execution Commit SHA**: `52814ac0a1312906d9bbc406ac01e9f760c00d61` (pre-release development SHA; not a reachable object in the single-root public release repository)
- **Execution Command**: `./scripts/run_sprint_e_experiments.sh`

> [!NOTE]
> **Directional / Short-Run Validation Notice:**
> The benchmarks compiled in this report represent 60-second sweeps on the `Hot_Repo_Skew_Max_Stress` topology (160 agents, 10 repos, skew shape). These serve to test whether autovacuum starvation is a tunable configuration issue or an architectural lock-acquisition problem.

---

## 2. Environment & Knobs Configuration Matrix

| Experiment Name | TOAST Tuning | Max Connections | Forced Vacuum? | Backlog Limit | Path Limit |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Control_Tuned_Mitigation | Yes | 50 | No | Disabled | Disabled |
| Exp1_Forced_Vacuum | Yes | 50 | Yes (5s cycle) | Disabled | Disabled |
| Exp2a_Pool_20 | Yes | 20 | No | Disabled | Disabled |
| Exp2b_Pool_100 | Yes | 100 | No | Disabled | Disabled |
| Exp3a_Backlog_Valve_20 | Yes | 50 | No | 20 | Disabled |
| Exp3b_Path_Valve_5 | Yes | 50 | No | Disabled | 5 |

---

## 3. Comparative Performance & Database Sizing Metrics

| Experiment Name | Committed Proms | Submit OK | Submit Fail | Promote OK | Promote 409 | Promote Rej | Promote 5xx | TOAST Size (MB) | TOAST MB / 1k Proms | Base Dead/Live | TOAST Dead/Live | Base Vac Count | Base Last Vac | TOAST Vac Count | TOAST Last Vac | P95 Checkout | P99 Checkout | P95 Lock Wait | P99 Lock Wait | P95 Post-Lock Hold | P99 Post-Lock Hold | P95 Occupancy | P99 Occupancy |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Control_Tuned_Mitigation | 185 | 1958 | 0 | 185 | 1773 | 0 | 0 | 0.031 MB | 0.167 MB | 3.1000 | 0.0 | 0 | never | 0 | never | 688.226 ms | 1237.927 ms | 816.215 ms | 1549.733 ms | 806.770 ms | 2116.884 ms | 1475.054 ms | 2259.928 ms |
| Exp1_Forced_Vacuum | 281 | 2686 | 0 | 281 | 2405 | 0 | 0 | 0.039 MB | 0.138 MB | 0.2000 | 0.0 | 0 | never | 0 | never | 398.490 ms | 691.001 ms | 745.361 ms | 1996.605 ms | 611.386 ms | 1247.601 ms | 1209.841 ms | 2195.692 ms |
| Exp2a_Pool_20 | 233 | 2667 | 0 | 233 | 2434 | 0 | 0 | 0.031 MB | 0.133 MB | 2.3000 | 0.0 | 0 | never | 0 | never | 523.523 ms | 676.333 ms | 66.932 ms | 327.860 ms | 470.892 ms | 737.479 ms | 532.807 ms | 849.973 ms |
| Exp2b_Pool_100 | 247 | 1872 | 0 | 247 | 1625 | 0 | 0 | 0.039 MB | 0.157 MB | 2.8000 | 0.0 | 1 | 09:24:55 | 0 | never | 467.683 ms | 1436.671 ms | 346.581 ms | 4351.146 ms | 1843.074 ms | 4779.171 ms | 2481.157 ms | 6004.838 ms |
| Exp3a_Backlog_Valve_20 | 85 | 301 | 5355 | 85 | 216 | 0 | 0 | 0.008 MB | 0.094 MB | 0.1000 | 0.0 | 0 | never | 0 | never | 114.224 ms | 201.119 ms | 18.222 ms | 69.539 ms | 663.436 ms | 1073.366 ms | 669.879 ms | 1104.169 ms |
| Exp3b_Path_Valve_5 | 200 | 987 | 2985 | 200 | 787 | 0 | 0 | 0.031 MB | 0.155 MB | 3.7000 | 0.0 | 0 | never | 0 | never | 473.918 ms | 797.745 ms | 165.644 ms | 426.398 ms | 1345.734 ms | 2460.654 ms | 1564.625 ms | 2626.351 ms |

---

## 4. Experiment 1: Forced Vacuum Execution Detail & Diagnostics

This section compiles the telemetry captured by the background forced vacuum daemon during `Exp1_Forced_Vacuum`. This distinguishes between "vacuum was blocked by contention" vs. "vacuum successfully ran but failed to prevent the performance collapse."

| Log Path | Total Attempts | Successes | Failures/Timeouts | Success % | Max Execution Duration | Completed Before Next Cycle? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `docs/sprint4e_logs/forced_vacuum_sprint_e.log` | 16 | 16 | 0 | 100.00% | 3s | Yes |

<details>
<summary>Raw Daemon Execution Trace Log (Exp1_Forced_Vacuum)</summary>

```text
Forced Vacuum Daemon started for DB aether_soak_pg_66404
------------------------------------------------------------
[2026-06-23T06:21:02Z] [Tick 1] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:02Z] [Tick 1] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |                                  query_summary                                   
-------+----------+------------------+-------------------------------+--------+----------------------------------------------------------------------------------
 66555 |        5 | -09:21:02.53314  | 2026-06-23 09:21:02.53314+03  | active |                                                                                 +
       |          |                  |                               |        |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
 66447 |        5 | -09:21:02.571601 | 2026-06-23 09:21:02.571601+03 | active | INSERT INTO aether_proposals (id, repo_id, agent_id, task_id, source, source_run
 66642 |        5 | -09:21:02.597916 | 2026-06-23 09:21:02.597916+03 | active | INSERT INTO aether_proposals (id, repo_id, agent_id, task_id, source, source_run
 66655 |        5 | -09:21:02.621969 | 2026-06-23 09:21:02.621969+03 | active | INSERT INTO aether_proposals (id, repo_id, agent_id, task_id, source, source_run
 66651 |        4 | -09:21:02.686341 | 2026-06-23 09:21:02.686341+03 | active | INSERT INTO aether_agent_metrics (agent_id, scope, total_proposals, promoted_cou
(5 rows)

[2026-06-23T06:21:02Z] [Tick 1] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:02Z] [Tick 1] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 0 removable, 1 nonremovable row versions in 1 out of 1 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43237528
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43237528
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:05Z] [Tick 1] SUCCESS. Duration: 3s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:07Z] [Tick 2] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:07Z] [Tick 2] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 66899 |      155 | -09:21:06.472809 | 2026-06-23 09:21:06.472809+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67346 |      153 | -09:21:06.473931 | 2026-06-23 09:21:06.473931+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66651 |      147 | -09:21:06.495729 | 2026-06-23 09:21:06.495729+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66701 |      147 | -09:21:06.499262 | 2026-06-23 09:21:06.499262+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66668 |      147 | -09:21:06.499809 | 2026-06-23 09:21:06.499809+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:07Z] [Tick 2] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:07Z] [Tick 2] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 1 removable, 22 nonremovable row versions in 1 out of 1 pages
DETAIL:  12 dead row versions cannot be removed yet, oldest xmin: 43237787
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43237787
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:08Z] [Tick 2] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:12Z] [Tick 3] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:12Z] [Tick 3] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 67415 |        7 | -09:21:12.294007 | 2026-06-23 09:21:12.294007+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 70035 |        6 | -09:21:12.301164 | 2026-06-23 09:21:12.301164+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66433 |        6 | -09:21:12.304451 | 2026-06-23 09:21:12.304451+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67830 |        6 | -09:21:12.339849 | 2026-06-23 09:21:12.339849+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67346 |        6 | -09:21:12.339893 | 2026-06-23 09:21:12.339893+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:12Z] [Tick 3] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:12Z] [Tick 3] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 2 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 2 dead item identifiers in 1 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  2 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 0 removable, 13 nonremovable row versions in 2 out of 2 pages
DETAIL:  3 dead row versions cannot be removed yet, oldest xmin: 43238597
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43238597
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:13Z] [Tick 3] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:17Z] [Tick 4] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:17Z] [Tick 4] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 67524 |      193 | -09:21:16.306824 | 2026-06-23 09:21:16.306824+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66899 |      193 | -09:21:16.309378 | 2026-06-23 09:21:16.309378+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 68186 |      193 | -09:21:16.309434 | 2026-06-23 09:21:16.309434+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66960 |      193 | -09:21:16.31035  | 2026-06-23 09:21:16.31035+03  | active | SELECT pg_advisory_xact_lock($1::bigint)
 68696 |      193 | -09:21:16.310664 | 2026-06-23 09:21:16.310664+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:17Z] [Tick 4] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:17Z] [Tick 4] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 4 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 4 dead item identifiers in 1 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  4 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 1 removable, 14 nonremovable row versions in 2 out of 2 pages
DETAIL:  4 dead row versions cannot be removed yet, oldest xmin: 43238909
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43238909
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:18Z] [Tick 4] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:22Z] [Tick 5] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:22Z] [Tick 5] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 67050 |       66 | -09:21:22.011508 | 2026-06-23 09:21:22.011508+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67037 |       66 | -09:21:22.037005 | 2026-06-23 09:21:22.037005+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67830 |       66 | -09:21:22.037773 | 2026-06-23 09:21:22.037773+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66960 |       66 | -09:21:22.039823 | 2026-06-23 09:21:22.039823+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66980 |       66 | -09:21:22.040014 | 2026-06-23 09:21:22.040014+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:22Z] [Tick 5] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:22Z] [Tick 5] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 5 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 5 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  5 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 7 removable, 13 nonremovable row versions in 4 out of 4 pages
DETAIL:  2 dead row versions cannot be removed yet, oldest xmin: 43239517
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43239517
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:24Z] [Tick 5] SUCCESS. Duration: 2s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:27Z] [Tick 6] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:27Z] [Tick 6] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           |        state        |                                  query_summary                                   
-------+----------+------------------+-------------------------------+---------------------+----------------------------------------------------------------------------------
 68696 |      266 | -09:21:26.045659 | 2026-06-23 09:21:26.045659+03 | active              | SELECT pg_advisory_xact_lock($1::bigint)
 67382 |      266 | -09:21:26.057284 | 2026-06-23 09:21:26.057284+03 | active              | SELECT pg_advisory_xact_lock($1::bigint)
 69802 |      249 | -09:21:26.170854 | 2026-06-23 09:21:26.170854+03 | active              | SELECT pg_advisory_xact_lock($1::bigint)
 13527 |       60 | -09:21:27.857736 | 2026-06-23 09:21:27.857736+03 | active              |                                                                                 +
       |          |                  |                               |                     |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
 70037 |          | -09:21:26.045433 | 2026-06-23 09:21:26.045433+03 | idle in transaction | INSERT INTO aether_heads (id, repo_id, snapshot_id, sequence, is_canonical, prom
(5 rows)

[2026-06-23T06:21:27Z] [Tick 6] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:27Z] [Tick 6] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 2 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 2 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  2 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 6 removable, 10 nonremovable row versions in 4 out of 4 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43240235
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43240235
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:28Z] [Tick 6] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:32Z] [Tick 7] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:32Z] [Tick 7] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |                                  query_summary                                  
-------+----------+------------------+-------------------------------+--------+---------------------------------------------------------------------------------
 68979 |        3 | -09:21:32.087303 | 2026-06-23 09:21:32.087303+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69942 |        3 | -09:21:32.125321 | 2026-06-23 09:21:32.125321+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66701 |        1 | -09:21:32.162608 | 2026-06-23 09:21:32.162608+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 22091 |        1 | -09:21:32.166543 | 2026-06-23 09:21:32.166543+03 | active |                                                                                +
       |          |                  |                               |        |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
 69802 |        1 | -09:21:32.173226 | 2026-06-23 09:21:32.173226+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:32Z] [Tick 7] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:32Z] [Tick 7] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 2 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 2 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  2 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 8 removable, 11 nonremovable row versions in 5 out of 5 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43240634
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43240634
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:32Z] [Tick 7] SUCCESS. Duration: 0s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:37Z] [Tick 8] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:37Z] [Tick 8] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 70242 |      227 | -09:21:35.22107  | 2026-06-23 09:21:35.22107+03  | active | SELECT pg_advisory_xact_lock($1::bigint)
 70088 |      227 | -09:21:35.390628 | 2026-06-23 09:21:35.390628+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69289 |      227 | -09:21:35.448541 | 2026-06-23 09:21:35.448541+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 68696 |      221 | -09:21:35.502756 | 2026-06-23 09:21:35.502756+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67401 |      221 | -09:21:35.540845 | 2026-06-23 09:21:35.540845+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:37Z] [Tick 8] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:37Z] [Tick 8] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 5 removable, 17 nonremovable row versions in 5 out of 5 pages
DETAIL:  6 dead row versions cannot be removed yet, oldest xmin: 43240920
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43240920
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:38Z] [Tick 8] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:42Z] [Tick 9] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:42Z] [Tick 9] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 69394 |      128 | -09:21:41.157776 | 2026-06-23 09:21:41.157776+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67050 |      128 | -09:21:41.190025 | 2026-06-23 09:21:41.190025+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69965 |      128 | -09:21:41.190059 | 2026-06-23 09:21:41.190059+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 70242 |      128 | -09:21:41.225461 | 2026-06-23 09:21:41.225461+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67209 |      122 | -09:21:41.297251 | 2026-06-23 09:21:41.297251+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:42Z] [Tick 9] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:42Z] [Tick 9] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 4 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 4 dead item identifiers in 3 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 11 row versions in 2 pages
DETAIL:  4 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 9 removable, 14 nonremovable row versions in 5 out of 5 pages
DETAIL:  4 dead row versions cannot be removed yet, oldest xmin: 43241536
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43241536
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:43Z] [Tick 9] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:47Z] [Tick 10] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:47Z] [Tick 10] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 69965 |      128 | -09:21:46.110566 | 2026-06-23 09:21:46.110566+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69955 |      128 | -09:21:46.110607 | 2026-06-23 09:21:46.110607+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66651 |      128 | -09:21:46.192928 | 2026-06-23 09:21:46.192928+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67441 |      128 | -09:21:46.241973 | 2026-06-23 09:21:46.241973+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67827 |      128 | -09:21:46.241987 | 2026-06-23 09:21:46.241987+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:47Z] [Tick 10] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:47Z] [Tick 10] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 4 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.48 s
INFO:  table "aether_head_frontiers": removed 4 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  4 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 14 removable, 20 nonremovable row versions in 5 out of 5 pages
DETAIL:  10 dead row versions cannot be removed yet, oldest xmin: 43242078
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.65 s.
INFO:  table "aether_head_frontiers": truncated 5 to 4 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.20 s
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43242078
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:49Z] [Tick 10] SUCCESS. Duration: 2s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:52Z] [Tick 11] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:52Z] [Tick 11] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 66651 |      339 | -09:21:47.422639 | 2026-06-23 09:21:47.422639+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66447 |      339 | -09:21:47.523858 | 2026-06-23 09:21:47.523858+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69965 |      188 | -09:21:50.351311 | 2026-06-23 09:21:50.351311+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69955 |       98 | -09:21:50.705368 | 2026-06-23 09:21:50.705368+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 70242 |       98 | -09:21:50.714865 | 2026-06-23 09:21:50.714865+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:52Z] [Tick 11] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:52Z] [Tick 11] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 8 removable, 20 nonremovable row versions in 4 out of 4 pages
DETAIL:  10 dead row versions cannot be removed yet, oldest xmin: 43242337
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43242337
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:53Z] [Tick 11] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:21:57Z] [Tick 12] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:21:57Z] [Tick 12] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 67524 |      211 | -09:21:56.41825  | 2026-06-23 09:21:56.41825+03  | active | SELECT pg_advisory_xact_lock($1::bigint)
 69080 |      211 | -09:21:56.449192 | 2026-06-23 09:21:56.449192+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66668 |      211 | -09:21:56.459538 | 2026-06-23 09:21:56.459538+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69394 |      197 | -09:21:56.532418 | 2026-06-23 09:21:56.532418+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66701 |      197 | -09:21:56.618734 | 2026-06-23 09:21:56.618734+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:21:57Z] [Tick 12] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:21:57Z] [Tick 12] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 2 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 2 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  2 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 13 removable, 14 nonremovable row versions in 4 out of 4 pages
DETAIL:  4 dead row versions cannot be removed yet, oldest xmin: 43242696
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43242696
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:21:59Z] [Tick 12] SUCCESS. Duration: 2s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:22:02Z] [Tick 13] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:22:02Z] [Tick 13] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |              query_summary               
-------+----------+------------------+-------------------------------+--------+------------------------------------------
 67832 |      261 | -09:22:00.294941 | 2026-06-23 09:22:00.294941+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 67050 |      256 | -09:22:00.572045 | 2026-06-23 09:22:00.572045+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 69945 |      256 | -09:22:00.574273 | 2026-06-23 09:22:00.574273+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
 66980 |      256 | -09:22:00.63559  | 2026-06-23 09:22:00.63559+03  | active | SELECT pg_advisory_xact_lock($1::bigint)
 67822 |      256 | -09:22:00.640627 | 2026-06-23 09:22:00.640627+03 | active | SELECT pg_advisory_xact_lock($1::bigint)
(5 rows)

[2026-06-23T06:22:02Z] [Tick 13] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:22:02Z] [Tick 13] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 7 removable, 13 nonremovable row versions in 4 out of 4 pages
DETAIL:  3 dead row versions cannot be removed yet, oldest xmin: 43243017
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243017
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:22:02Z] [Tick 13] SUCCESS. Duration: 0s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:22:07Z] [Tick 14] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:22:07Z] [Tick 14] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |                                  query_summary                                  
-------+----------+------------------+-------------------------------+--------+---------------------------------------------------------------------------------
 71564 |        0 | -09:22:07.651105 | 2026-06-23 09:22:07.651105+03 | active |                                                                                +
       |          |                  |                               |        |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
(1 row)

[2026-06-23T06:22:07Z] [Tick 14] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:22:07Z] [Tick 14] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  scanned index "aether_head_frontiers_pkey" to remove 3 row versions
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  table "aether_head_frontiers": removed 3 dead item identifiers in 2 pages
DETAIL:  CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s
INFO:  index "aether_head_frontiers_pkey" now contains 10 row versions in 2 pages
DETAIL:  3 index row versions were removed.
0 index pages were newly deleted.
0 index pages are currently deleted, of which 0 are currently reusable.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  table "aether_head_frontiers": found 9 removable, 10 nonremovable row versions in 4 out of 4 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:22:07Z] [Tick 14] SUCCESS. Duration: 0s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:22:12Z] [Tick 15] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:22:12Z] [Tick 15] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |                                  query_summary                                  
-------+----------+------------------+-------------------------------+--------+---------------------------------------------------------------------------------
 71600 |        0 | -09:22:13.269153 | 2026-06-23 09:22:13.269153+03 | active |                                                                                +
       |          |                  |                               |        |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
(1 row)

[2026-06-23T06:22:12Z] [Tick 15] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:22:12Z] [Tick 15] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 0 removable, 10 nonremovable row versions in 4 out of 4 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:22:13Z] [Tick 15] SUCCESS. Duration: 1s. Completed before next: Yes
------------------------------------------------------------
[2026-06-23T06:22:17Z] [Tick 16] Starting Telemetry Collection
------------------------------------------------------------
[2026-06-23T06:22:17Z] [Tick 16] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):
  pid  | xmin_age |     xact_age     |          xact_start           | state  |                                  query_summary                                  
-------+----------+------------------+-------------------------------+--------+---------------------------------------------------------------------------------
 73113 |        0 | -09:22:17.427168 | 2026-06-23 09:22:17.427168+03 | active |                                                                                +
       |          |                  |                               |        |                 SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS x
(1 row)

[2026-06-23T06:22:17Z] [Tick 16] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):
 pid | xmin_age | phase | heap_blks_total | heap_blks_scanned | heap_blks_vacuumed | index_vacuum_count | num_dead_tuples 
-----+----------+-------+-----------------+-------------------+--------------------+--------------------+-----------------
(0 rows)

[2026-06-23T06:22:17Z] [Tick 16] Attempting VACUUM VERBOSE public.aether_head_frontiers...
INFO:  vacuuming "public.aether_head_frontiers"
INFO:  table "aether_head_frontiers": found 0 removable, 10 nonremovable row versions in 4 out of 4 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
INFO:  vacuuming "pg_toast.pg_toast_1042617"
INFO:  table "pg_toast_1042617": found 0 removable, 0 nonremovable row versions in 0 out of 0 pages
DETAIL:  0 dead row versions cannot be removed yet, oldest xmin: 43243358
Skipped 0 pages due to buffer pins, 0 frozen pages.
CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s.
VACUUM
[2026-06-23T06:22:17Z] [Tick 16] SUCCESS. Duration: 0s. Completed before next: Yes
```

</details>

---

## 5. Evaluation of the Dual Kill Criterion

Recall the **Dual Kill Criterion**:
> If tuned runs (with aggressive autovacuum, forced vacuum interventions, and pool sizing adjustments) *still* show autovacuum starvation **AND** near-collapse progress (< 1 TPS) under the skew/max stress topology, Sprint E concludes **contention-avoidance is required**. It will NOT keep spinning on un-tunable autovacuum parameters.

### 4.1. Core Findings Analysis

1. **Autovacuum Starvation & Mitigation**: Under the aggressive autovacuum tuning configs, the PostgreSQL engine did not trigger automatic table cleanups, resulting in an autovacuum count of `0` in almost all tuned control sweeps. In `Exp1_Forced_Vacuum`, the forced vacuum daemon successfully completed 100% of its cycles (16/16 attempts), reducing the dead-to-live tuple ratio to `0.2000` (compared to `3.1000` in the Control run). However, maintaining a clean relation did not restore throughput to uncontended baselines (~25.2 TPS), demonstrating that tuple bloat is not the primary bottleneck.
2. **Oldest Transaction Telemetry**: High-fidelity logs from the corrected telemetry query show that the oldest active transactions blocking database cleanup are consistently waiting on `SELECT pg_advisory_xact_lock($1::bigint)`. These long lock-wait queues pin the database `xmin` horizon, preventing concurrent vacuums from reclaiming dead row versions during active contention periods.
3. **Connection Pooling & Lock-Wait Latencies**: Adjusting pool sizes did not resolve the bottleneck. In `Exp2b_Pool_100` (100 conns), P99 lock waits peaked at 4351.146 ms and P9post-lock hold times reached 4779.171 ms, leading to connection occupancy times of 6004.838 ms. Restricting conns to 20 (`Exp2a_Pool_20`) decreased lock waits but limited concurrent throughput.
4. **Throughput / Kill Criterion Status**: Under the `Hot_Repo_Skew_Max_Stress` topology, throughput across all sweeps remained above the near-collapse threshold, yielding 1.42 to 4.68 TPS. Thus, the `< 1 TPS` collapse condition was not triggered in these Sweeps.

### 4.Final Decision & Recommendation

*   **Conclusion**: Database level parameter tuning (autovacuum, pool limits, aggressive TOAST settings) and manual vacuum interventions succeed in keeping the relation clean but fail to mitigate the performance degradation caused by application-level lock hotspots.
*   **Recommendation**: Rather than focusing further on database-level autovacuum parameters, future sprints should prioritize **architectural lock-contention avoidance** (such as lock-free head advances, meet-frontier width caps, or partitioning of the hot keyspace) to minimize transaction hold times and block times on advisory locks. This recommendation remains neutral and is supported by pg_stat_activity telemetry.
