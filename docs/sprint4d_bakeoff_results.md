# Sprint 4D Directional Overnight Two-Tier Probe Results

**Start Time:** Wed May 27 20:21:23 EEST 2026  
**Matrix:** 10 Repositories, 3600 seconds (1 hour) run duration per tier.  
**System Configuration:** ulimit -n 1048575, max_connections = 250.

---

## Run Configurations & Status

| Run | Lane | Tier | Agents | CRDT | Status |
| :--- | :--- | :--- | :---: | :---: | :---: |
| control_tier3 | control | tier3 | 40 | 0 | FAILED ❌ (Exit Code 1) |
| advisory_lock_tier3 | advisory_lock | tier3 | 40 | 1 | PASSED ✅ |
| phase3_frontiers_tier3 | phase3_frontiers | tier3 | 40 | 2 | PASSED ✅ |
| phase4_token_tier3 | phase4_token | tier3 | 40 | 3 | PASSED ✅ |
| control_tier5 | control | tier5 | 160 | 0 | PASSED ✅ |
| advisory_lock_tier5 | advisory_lock | tier5 | 160 | 1 | PASSED ✅ |
| phase3_frontiers_tier5 | phase3_frontiers | tier5 | 160 | 2 | FAILED ❌ (Exit Code 1) |
| phase4_token_tier5 | phase4_token | tier5 | 160 | 3 | FAILED ❌ (Exit Code 1) |

## Detailed Telemetry & Metrics Analysis

### Run: control_tier3 (control, tier3)

**Status:** FAILED ❌ (Exit Code 1)

```
🔍 Found 40 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                112545
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        60508
  Reject Path:         52037
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          112545
  Contention Waste Ratio:            46.24%
  Sum Total Request Latency:         8289453.34 ms
  Mean Request-Population Latency:   73.65 ms (sum latency / total attempts)
  Amortized Cost per Success:        137.00 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median:   11.35 ms
  p95:     167.14 ms
  p99:     315.55 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |    0.65 |   29.87 |   72.97
t_crdt_eval                         |    0.53 |    1.73 |    4.08
t_materialize                       |    0.41 |    1.87 |    4.95
t_gates                             |    4.14 |   30.84 |   56.09
t_trust_math                        |    0.00 |    0.01 |    0.02
t_fs_rename                         |    0.10 |    0.50 |    1.64
t_db_tx_start                       |    0.19 |   16.10 |   40.36
t_db_revalidate                     |    0.23 |    9.38 |   28.32
t_db_insert_snapshot_gates          |    0.31 |    9.91 |   32.21
t_db_update_proposals_trust         |    0.17 |    4.39 |   15.48
t_db_insert_promotions_heads_cas    |    0.47 |   15.24 |   44.24
t_db_commit                         |    0.19 |    8.03 |   29.13
t_total_inner                       |   53.99 |  200.06 |  372.35
t_db_read_repo                      |    1.21 |   15.30 |   43.87
t_db_read_proposals                 |    1.27 |   14.74 |   41.22
t_db_read_base_snapshot             |    1.26 |   15.47 |   43.03
t_db_insert_promotion               |    0.62 |    7.64 |   25.92
t_db_insert_head                    |    0.66 |    7.84 |   25.80
t_db_cas_update                     |    0.52 |    7.70 |   24.01
t_db_checkout_repo                  |    0.42 |    6.92 |   22.21
t_db_exec_repo                      |    0.52 |    7.52 |   27.39
t_db_checkout_proposals             |    0.39 |    6.45 |   20.37
t_db_exec_proposals                 |    0.58 |    7.82 |   26.52
t_db_checkout_base_snapshot         |    0.40 |    7.01 |   21.83
t_db_exec_base_snapshot             |    0.54 |    8.21 |   26.46
t_db_checkout_tx                    |    1.83 |   24.38 |   51.20
t_db_exec_promotion                 |    0.62 |    7.64 |   25.92
t_db_exec_head                      |    0.66 |    7.84 |   25.80
t_db_exec_cas                       |    0.52 |    7.70 |   24.01
t_lock_wait                         | N/A     | N/A     | N/A    
t_lock_hold                         | N/A     | N/A     | N/A    

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T18:20:36Z|234|130043
2026-05-27T18:20:52Z|234|130040
2026-05-27T18:21:07Z|234|130032
2026-05-27T18:21:22Z|235|130022
2026-05-27T18:21:37Z|236|130015
2026-05-27T18:21:53Z|238|130005
2026-05-27T18:22:08Z|239|130029
2026-05-27T18:22:23Z|240|130020
2026-05-27T18:22:38Z|242|130013
2026-05-27T18:22:53Z|242|130010
```

---

### Run: advisory_lock_tier3 (advisory_lock, tier3)

**Status:** PASSED ✅

```
🔍 Found 40 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                115448
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        115448
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          115448
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         7542788.09 ms
  Mean Request-Population Latency:   65.33 ms (sum latency / total attempts)
  Amortized Cost per Success:        65.33 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median:    5.44 ms
  p95:      78.94 ms
  p99:     219.09 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |    2.31 |   24.30 |   77.01
t_crdt_eval                         |    0.83 |    2.44 |    5.93
t_materialize                       |    0.62 |    2.99 |    7.15
t_gates                             |   10.49 |   36.93 |   70.37
t_trust_math                        |    0.01 |    0.01 |    0.04
t_fs_rename                         |    0.28 |    0.93 |    2.38
t_db_tx_start                       |    0.68 |   11.21 |   38.60
t_db_revalidate                     |    0.41 |    7.34 |   23.12
t_db_insert_snapshot_gates          |    2.08 |   27.18 |   64.76
t_db_update_proposals_trust         |    0.61 |    6.40 |   21.85
t_db_insert_promotions_heads_cas    |    1.82 |   16.30 |   50.51
t_db_commit                         |    0.76 |   10.43 |   34.98
t_total_inner                       |   37.92 |  204.35 |  457.40
t_db_read_repo                      |    0.94 |   11.55 |   39.17
t_db_read_proposals                 |    0.96 |   11.77 |   42.93
t_db_read_base_snapshot             |    0.53 |    8.49 |   26.96
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |    0.32 |   52.24 |  167.08
t_lock_hold                         |   28.16 |  114.18 |  260.55

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T19:23:35Z|321|129990
2026-05-27T19:23:50Z|323|129979
2026-05-27T19:24:05Z|324|129971
2026-05-27T19:24:20Z|325|129958
2026-05-27T19:24:35Z|327|129943
2026-05-27T19:24:50Z|328|129931
2026-05-27T19:25:05Z|330|129906
2026-05-27T19:25:20Z|330|129895
2026-05-27T19:25:36Z|331|129884
2026-05-27T19:25:51Z|332|129878
```

---

### Run: phase3_frontiers_tier3 (phase3_frontiers, tier3)

**Status:** PASSED ✅

```
🔍 Found 40 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                101110
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        101110
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          101110
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         23739772.94 ms
  Mean Request-Population Latency:   234.79 ms (sum latency / total attempts)
  Amortized Cost per Success:        234.79 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median:   95.24 ms
  p95:     574.70 ms
  p99:     950.75 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |    4.84 |   38.59 |   99.89
t_crdt_eval                         |    1.04 |    2.74 |    5.85
t_materialize                       |    0.68 |    2.32 |    5.44
t_gates                             |   13.72 |   31.25 |   65.13
t_trust_math                        |    0.01 |    0.01 |    0.04
t_fs_rename                         |    0.29 |    0.81 |    1.97
t_db_tx_start                       |    1.51 |   18.06 |   49.98
t_db_revalidate                     |    0.68 |   10.69 |   28.07
t_db_insert_snapshot_gates          |    3.61 |   31.03 |   76.03
t_db_update_proposals_trust         |    0.99 |   10.96 |   30.57
t_db_insert_promotions_heads_cas    |    5.23 |   39.49 |  101.05
t_db_commit                         |    1.18 |   15.83 |   42.08
t_total_inner                       |  154.76 |  705.66 | 1124.78
t_db_read_repo                      |    1.96 |   19.10 |   53.94
t_db_read_proposals                 |    1.94 |   19.55 |   56.77
t_db_read_base_snapshot             |    0.76 |   11.12 |   27.68
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |    1.14 |  347.34 |  680.24
t_lock_hold                         |  111.50 |  422.28 |  725.17

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T20:26:13Z|837|126900
2026-05-27T20:26:28Z|839|126895
2026-05-27T20:26:44Z|840|126881
2026-05-27T20:26:59Z|769|126956
2026-05-27T20:27:14Z|770|126944
2026-05-27T20:27:29Z|772|126918
2026-05-27T20:27:44Z|773|126909
2026-05-27T20:27:59Z|774|126893
2026-05-27T20:28:14Z|781|126869
2026-05-27T20:28:29Z|781|126865
```

---

### Run: phase4_token_tier3 (phase4_token, tier3)

**Status:** PASSED ✅

```
🔍 Found 40 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                102496
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        102496
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          102496
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         23159558.35 ms
  Mean Request-Population Latency:   225.96 ms (sum latency / total attempts)
  Amortized Cost per Success:        225.96 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median:   85.59 ms
  p95:     588.75 ms
  p99:     991.64 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |    4.12 |   34.52 |   93.63
t_crdt_eval                         |    0.59 |    1.94 |    4.16
t_materialize                       |    0.61 |    2.04 |    4.76
t_gates                             |   13.12 |   29.01 |   56.76
t_trust_math                        |    0.01 |    0.01 |    0.04
t_fs_rename                         |    0.28 |    0.78 |    1.91
t_db_tx_start                       |    1.26 |   16.36 |   46.20
t_db_revalidate                     |    0.57 |    9.52 |   24.24
t_db_insert_snapshot_gates          |    3.19 |   27.89 |   68.59
t_db_update_proposals_trust         |    0.87 |    9.87 |   25.34
t_db_insert_promotions_heads_cas    |    4.49 |   35.75 |   88.57
t_db_commit                         |    1.02 |   14.35 |   36.79
t_total_inner                       |  137.89 |  709.85 | 1171.39
t_db_read_repo                      |    1.65 |   17.54 |   50.21
t_db_read_proposals                 |    1.61 |   17.50 |   52.53
t_db_read_base_snapshot             |    0.65 |    9.92 |   25.71
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |    0.89 |  358.33 |  697.14
t_lock_hold                         |  102.46 |  422.42 |  742.68

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T21:28:31Z|789|125461
2026-05-27T21:28:46Z|804|125436
2026-05-27T21:29:01Z|806|125425
2026-05-27T21:29:16Z|807|125412
2026-05-27T21:29:31Z|809|125400
2026-05-27T21:29:46Z|810|125392
2026-05-27T21:30:02Z|616|125592
2026-05-27T21:30:17Z|617|125586
2026-05-27T21:30:32Z|619|125573
2026-05-27T21:30:47Z|619|125578
```

---

### Run: control_tier5 (control, tier5)

**Status:** PASSED ✅

```
🔍 Found 160 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                172298
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        22265
  Reject Path:         150033
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          172298
  Contention Waste Ratio:            87.08%
  Sum Total Request Latency:         263740881.56 ms
  Mean Request-Population Latency:   1530.73 ms (sum latency / total attempts)
  Amortized Cost per Success:        11845.54 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median: 1337.11 ms
  p95:    2613.54 ms
  p99:    4043.75 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |    0.00 |  647.99 | 1039.40
t_crdt_eval                         |    0.00 |    5.20 |   26.13
t_materialize                       |    0.00 |    5.13 |   18.64
t_gates                             |    0.00 |   68.80 |  159.23
t_trust_math                        |    0.00 |    0.01 |    0.01
t_fs_rename                         |    0.00 |    0.23 |    1.05
t_db_tx_start                       |    0.00 |  199.26 |  349.48
t_db_revalidate                     |    0.00 |   31.87 |  110.55
t_db_insert_snapshot_gates          |    0.00 |   37.01 |  131.27
t_db_update_proposals_trust         |    0.00 |   11.57 |   67.94
t_db_insert_promotions_heads_cas    |    0.00 |   60.95 |  177.46
t_db_commit                         |    0.00 |   29.06 |  122.36
t_total_inner                       | 1378.13 | 2675.29 | 4106.38
t_db_read_repo                      |  206.72 |  496.70 | 1001.16
t_db_read_proposals                 |  187.74 |  425.50 |  734.78
t_db_read_base_snapshot             |  174.43 |  399.29 |  686.50
t_db_insert_promotion               |    7.29 |   89.00 |  217.20
t_db_insert_head                    |    7.51 |   87.64 |  202.41
t_db_cas_update                     |    7.25 |   92.33 |  225.86
t_db_checkout_repo                  |  183.08 |  431.32 |  799.31
t_db_exec_repo                      |    8.58 |  105.37 |  339.87
t_db_checkout_proposals             |  167.48 |  385.06 |  661.09
t_db_exec_proposals                 |    7.67 |   87.70 |  187.72
t_db_checkout_base_snapshot         |  155.59 |  361.21 |  615.91
t_db_exec_base_snapshot             |    7.27 |   85.04 |  183.79
t_db_checkout_tx                    |  172.93 |  401.11 |  695.56
t_db_exec_promotion                 |    7.29 |   89.00 |  217.20
t_db_exec_head                      |    7.51 |   87.64 |  202.41
t_db_exec_cas                       |    7.25 |   92.33 |  225.86
t_lock_wait                         | N/A     | N/A     | N/A    
t_lock_hold                         | N/A     | N/A     | N/A    

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T22:30:00Z|168|124804
2026-05-27T22:30:16Z|168|124796
2026-05-27T22:30:31Z|168|124784
2026-05-27T22:30:46Z|168|124774
2026-05-27T22:31:02Z|169|124762
2026-05-27T22:31:17Z|170|124752
2026-05-27T22:31:32Z|170|124772
2026-05-27T22:31:47Z|171|124762
2026-05-27T22:32:03Z|171|124751
2026-05-27T22:32:18Z|171|124750
```

---

### Run: advisory_lock_tier5 (advisory_lock, tier5)

**Status:** PASSED ✅

```
🔍 Found 160 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                180854
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        180854
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          180854
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         235046635.00 ms
  Mean Request-Population Latency:   1299.65 ms (sum latency / total attempts)
  Amortized Cost per Success:        1299.65 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median:  574.43 ms
  p95:    1715.44 ms
  p99:    2463.22 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |  287.90 |  518.69 |  783.57
t_crdt_eval                         |    4.01 |   44.88 |   95.55
t_materialize                       |    3.82 |   25.00 |   59.62
t_gates                             |   38.42 |  137.31 |  207.70
t_trust_math                        |    0.01 |    0.01 |    0.23
t_fs_rename                         |    0.22 |    3.05 |   12.55
t_db_tx_start                       |  127.93 |  270.79 |  414.18
t_db_revalidate                     |    2.49 |   26.37 |   77.79
t_db_insert_snapshot_gates          |   10.55 |  112.20 |  344.48
t_db_update_proposals_trust         |    2.62 |   28.65 |  102.13
t_db_insert_promotions_heads_cas    |    9.62 |   78.48 |  254.95
t_db_commit                         |    4.32 |   42.11 |  124.56
t_total_inner                       | 1183.93 | 2370.85 | 3130.52
t_db_read_repo                      |  142.88 |  302.48 |  472.14
t_db_read_proposals                 |  132.38 |  278.46 |  448.06
t_db_read_base_snapshot             |    2.81 |   27.96 |   79.88
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |  402.26 | 1538.35 | 2272.69
t_lock_hold                         |  146.13 |  440.86 |  841.72

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-27T23:37:38Z|504|121616
2026-05-27T23:37:53Z|507|121541
2026-05-27T23:38:09Z|509|121480
2026-05-27T23:38:24Z|510|121402
2026-05-27T23:38:40Z|512|121342
2026-05-27T23:38:55Z|514|121283
2026-05-27T23:39:10Z|516|121212
2026-05-27T23:39:26Z|518|121177
2026-05-27T23:39:41Z|518|121183
2026-05-27T23:39:56Z|518|121173
```

---

### Run: phase3_frontiers_tier5 (phase3_frontiers, tier5)

**Status:** FAILED ❌ (Exit Code 1)

```
🔍 Found 160 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                86676
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        86676
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          86676
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         201607373.21 ms
  Mean Request-Population Latency:   2325.99 ms (sum latency / total attempts)
  Amortized Cost per Success:        2325.99 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median: 1431.22 ms
  p95:    2597.84 ms
  p99:    3288.00 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |  434.35 |  934.83 | 1299.33
t_crdt_eval                         |    1.26 |    3.28 |    8.40
t_materialize                       |    1.11 |    4.05 |   10.46
t_gates                             |   17.50 |   48.86 |  100.02
t_trust_math                        |    0.01 |    0.01 |    0.05
t_fs_rename                         |    0.25 |    0.82 |    2.30
t_db_tx_start                       |  254.56 |  737.75 | 1030.14
t_db_revalidate                     |    1.64 |   12.94 |   48.75
t_db_insert_snapshot_gates          |    6.73 |   51.94 |  169.16
t_db_update_proposals_trust         |    2.07 |   15.96 |   70.23
t_db_insert_promotions_heads_cas    |   13.91 |   86.81 |  196.99
t_db_commit                         |    3.41 |   25.58 |   83.13
t_total_inner                       | 2300.53 | 3647.38 | 4437.57
t_db_read_repo                      |  218.47 |  532.55 |  761.07
t_db_read_proposals                 |  198.91 |  490.27 |  717.56
t_db_read_base_snapshot             |    1.58 |   12.83 |   49.08
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |  961.66 | 2090.34 | 2655.58
t_lock_hold                         |  245.44 |  645.92 |  912.55

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-28T00:19:06Z|7550|110489
2026-05-28T00:19:21Z|7633|110391
2026-05-28T00:19:36Z|7708|110309
2026-05-28T00:19:51Z|7776|110232
2026-05-28T00:20:06Z|7851|110136
2026-05-28T00:20:21Z|7917|110057
2026-05-28T00:20:36Z|7996|109960
2026-05-28T00:20:52Z|8085|109865
2026-05-28T00:21:07Z|8174|109754
2026-05-28T00:21:22Z|8252|109675
```

---

### Run: phase4_token_tier5 (phase4_token, tier5)

**Status:** FAILED ❌ (Exit Code 1)

```
🔍 Found 160 agent log files to analyze.
=============================================
AETHER TIMING ANALYSIS RESULTS
=============================================
Sample Counts:
  Rows Analyzed:                89782
  Missing Timing Blob:          0
  Missing Required Stage Keys:  0
  Malformed JSON:               0
Path Split:
  Success Path:        89782
  Reject Path:         0
Anti-Bias Telemetry Normalization (Visibility Phase):
  Total Promotion Attempts:          89782
  Contention Waste Ratio:            0.00%
  Sum Total Request Latency:         203531498.70 ms
  Mean Request-Population Latency:   2266.95 ms (sum latency / total attempts)
  Amortized Cost per Success:        2266.95 ms (sum latency / successful commits)

Residual Stats (t_total_inner - sum(tracked_stages)):
  Median: 1393.89 ms
  p95:    2617.49 ms
  p99:    3269.55 ms

Individual Tracked Stages (ms):
Stage                               | Median  | p95     | p99    
-----------------------------------------------------------------
t_db_read_pre_orch                  |  403.36 |  935.57 | 1337.78
t_crdt_eval                         |    0.56 |    1.56 |    4.34
t_materialize                       |    0.87 |    3.11 |    7.28
t_gates                             |   17.63 |   43.34 |   83.03
t_trust_math                        |    0.01 |    0.01 |    0.05
t_fs_rename                         |    0.25 |    0.79 |    2.22
t_db_tx_start                       |  241.03 |  779.72 | 1048.75
t_db_revalidate                     |    1.54 |   12.04 |   40.41
t_db_insert_snapshot_gates          |    6.44 |   47.53 |  155.42
t_db_update_proposals_trust         |    1.99 |   14.76 |   57.86
t_db_insert_promotions_heads_cas    |   13.08 |   81.84 |  185.47
t_db_commit                         |    3.14 |   23.47 |   74.99
t_total_inner                       | 2218.52 | 3677.92 | 4476.73
t_db_read_repo                      |  204.49 |  514.73 |  790.36
t_db_read_proposals                 |  187.36 |  480.98 |  741.25
t_db_read_base_snapshot             |    1.47 |   11.85 |   39.26
t_db_insert_promotion               | N/A     | N/A     | N/A    
t_db_insert_head                    | N/A     | N/A     | N/A    
t_db_cas_update                     | N/A     | N/A     | N/A    
t_db_checkout_repo                  | N/A     | N/A     | N/A    
t_db_exec_repo                      | N/A     | N/A     | N/A    
t_db_checkout_proposals             | N/A     | N/A     | N/A    
t_db_exec_proposals                 | N/A     | N/A     | N/A    
t_db_checkout_base_snapshot         | N/A     | N/A     | N/A    
t_db_exec_base_snapshot             | N/A     | N/A     | N/A    
t_db_checkout_tx                    | N/A     | N/A     | N/A    
t_db_exec_promotion                 | N/A     | N/A     | N/A    
t_db_exec_head                      | N/A     | N/A     | N/A    
t_db_exec_cas                       | N/A     | N/A     | N/A    
t_lock_wait                         |  936.92 | 2098.90 | 2649.96
t_lock_hold                         |  236.63 |  656.10 |  940.80

Request Metadata (count):
req_proposal_count                  |    1.00 |    1.00 |    1.00
=============================================
```

**Host Capacity & Disk Telemetry (Sampling):**
```
2026-05-28T01:03:23Z|7386|107806
2026-05-28T01:03:38Z|7478|107695
2026-05-28T01:03:53Z|7573|107599
2026-05-28T01:04:08Z|7671|107472
2026-05-28T01:04:23Z|7766|107374
2026-05-28T01:04:38Z|7860|107263
2026-05-28T01:04:53Z|7954|107155
2026-05-28T01:05:08Z|8052|107045
2026-05-28T01:05:23Z|8150|106936
2026-05-28T01:05:39Z|8235|106841
```

---

