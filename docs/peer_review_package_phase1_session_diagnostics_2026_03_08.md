# Aether Phase-1 Deep Peer Review Package — Session-State / Snapshot Diagnostics

Date: 2026-03-08
Scope: Long-window (25m) throughput degradation after admission-control stabilization
Prepared from local artifacts under `.pytest-artifacts/phase1-repro/` and `peer-review-artifacts/`

## Executive verdict

The admission-control problem is no longer the dominant blocker.

Current evidence shows:
- submit tail latency is now controlled well enough for medium windows,
- backlog stays bounded and drains cleanly,
- rho remains at or below 1.0 in the diagnostic runs,
- the original late-window throughput loss came from proposal quarantines in the snapshot path,
- separate-engine isolation did not eliminate those quarantines,
- the latest snapshot hot-path simplification eliminated quarantines in the 25m diagnostic,
- the remaining failure is now a small final-window rho overshoot, not snapshot-path recursion.

This is a real improvement in the diagnosis quality. The problem has narrowed from "system unstable under 1000 agents" to a specific late-window ORM/session failure in the promotion worker path.

## Artifact index

### Core diagnostic runs
- `/.pytest-artifacts/phase1-repro/m4-15m-strict-r043-dispatch4-route0-v5.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v11.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v12.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v13.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v14-sessioniso.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v15-separate-engine.json`
- `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v17-snapshot-hotpath-20260309-195115.json`

### Harness wrapper artifacts
- `/.pytest-artifacts/phase1/swarm-safe-20260308-101145.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-101145.heartbeat.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-123350.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-123350.heartbeat.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-194631.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-194631.heartbeat.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-221439.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260308-221439.heartbeat.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260309-195115.log`
- `/.pytest-artifacts/phase1/swarm-safe-20260309-195115.heartbeat.log`

### Fresh-session replay artifact
- `/peer-review-artifacts/phase1-session-replay-20260308.txt`

## Configuration under test

Shared configuration for the 25m diagnostics:
- agents: `1000`
- files: `500`
- contention: `low`
- run_seconds: `1500`
- drain_seconds: `300`
- inflight: `200`
- promotion_batch: `30`
- non_conflicting_batches: `12`
- max_conflict_sets: `1`
- gate_execution_mode: `parallel`
- gate_max_parallel: `4`
- submit_via_api_route: `true`
- adaptive_intake_throttle: `true`
- intake_high/low: `170 / 130`
- throttle check: `40 ms`
- local controller ratio: `0.43`
- route-side ratio limiter: `disabled` via `AETHER_ROUTE_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0`

Relevant environment:
- `AETHER_MAX_COHERENCE_BACKLOG=260`
- `AETHER_BACKLOG_RESUME_LEVEL=180`
- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.43`
- `AETHER_ROUTE_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0`
- `AETHER_RATIO_CONTROL_MIN_MU_TPS=0.05`
- `AETHER_RATIO_CONTROL_MIN_MU_EVENTS=1`
- `AETHER_COHERENCE_WINDOW_SECONDS=60`
- `AETHER_RETRY_AFTER_MAX_SECONDS=300`
- `AETHER_GATE_HARD_TIMEOUT_SECONDS=15`

## Timeline of results

### 1) Stable 15m strict baseline
Artifact: `/.pytest-artifacts/phase1-repro/m4-15m-strict-r043-dispatch4-route0-v5.json`

Result:
- `passed = true`
- submit throughput: `0.858/s`
- completion throughput during submit phase: `0.858/s`
- promotion throughput: `0.641/s`
- submit p95: `596.881 ms`
- rho: `0.9961`
- backlog_end: `0`
- quarantined: `0`

Interpretation:
- the current controller is viable at 15 minutes,
- the remaining blocker is specifically a long-window failure.

### 2) 25m diagnostic v11
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v11.json`

Result:
- failed checks: `submit_throughput_ok`, `promotion_throughput_ok`
- submit throughput: `0.694/s`
- promotion throughput: `0.528/s`
- submit p95: `514.410 ms`
- rho: `0.9492`
- backlog_end: `0`
- quarantined: `77`

Initial conclusion:
- control loop is stable,
- late-window throughput falls because proposals move to `QUARANTINED`.

### 3) 25m diagnostic v12
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v12.json`

Result:
- failed checks: `submit_throughput_ok`, `promotion_throughput_ok`
- submit throughput: `0.695/s`
- promotion throughput: `0.527/s`
- submit p95: `27.924 ms`
- rho: `0.9412`
- backlog_end: `0`
- quarantined: `89`

Important shift:
- p95 is excellent,
- route instability is no longer the explanation,
- throughput loss is almost entirely explained by quarantines.

### 4) 25m diagnostic v13 with phase-tagged exceptions
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v13.json`

Result:
- failed checks: `submit_throughput_ok`, `promotion_throughput_ok`
- submit throughput: `0.745/s`
- completion throughput during submit phase: `0.745/s`
- promotion throughput: `0.585/s`
- submit p95: `1176.167 ms`
- rho: `0.9875`
- backlog_end: `0`
- quarantined: `57`

This is the most important artifact in the package. It proves:
- throughput improves versus v11/v12,
- the system remains bounded,
- the failure source is now phase-localized.

### 5) 25m diagnostic v14 with session-isolated promotion/materialization loops
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v14-sessioniso.json`

Change under test:
- promotion loop now uses a fresh `Session` per iteration,
- materialization loop now uses a fresh `Session` per iteration,
- monitor/physics snapshot queries also use fresh sessions.

Result:
- failed checks: `submit_throughput_ok`, `promotion_throughput_ok`
- submit throughput: `0.691/s`
- promotion throughput: `0.521/s`
- submit p95: `394.479 ms`
- rho: `0.9130`
- backlog_end: `0`
- quarantined: `71`

Interpretation:
- session isolation by itself does **not** eliminate the recursion failure,
- it does keep p95/rho/backlog well behaved,
- the long-window throughput drop still correlates with snapshot-path quarantines,
- therefore the dominant failure is narrower than \"long-lived session state alone\".

Revised conclusion:
- the problem likely sits inside a specific snapshot-service ORM/query path that is exercised under sustained concurrent promotion/materialization traffic,
- fresh-session replay remains useful evidence, but it is no longer sufficient to claim session accumulation is the single root cause.

### 6) 25m diagnostic v15 with separate-engine runtime path
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v15-separate-engine.json`

Change under test:
- API/submit path stayed on engine A,
- promotion/materialization/runtime monitoring moved to engine B against the same SQLite file,
- same seed, same harness config, same thresholds.

Result:
- failed checks: `submit_throughput_ok`, `promotion_throughput_ok`
- submit throughput: `0.680/s`
- promotion throughput: `0.515/s`
- submit p95: `336.194 ms`
- rho: `0.8286`
- backlog_end: `0`
- quarantined: `62`

Interpretation:
- separate-engine isolation did **not** eliminate the failure,
- quarantines remained high and still pointed to `create_snapshot`,
- identity-map sizes in the new snapshot sub-phase metrics stayed tiny/flat,
- therefore neither shared-engine topology nor identity-map growth is the primary root cause.

Revised conclusion:
- the remaining issue is the snapshot ORM/query path itself,
- not generic session accumulation and not engine-level cache sharing alone.

### 7) 25m diagnostic v17 after snapshot hot-path simplification
Artifact: `/.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v17-snapshot-hotpath-20260309-195115.json`

Code change under test:
- `create_snapshot()` now bypasses proposal ORM lookup when changed paths are already known,
- repository/head resolution now uses scalar queries instead of ORM object loading,
- promotion evaluation passes changed paths directly into snapshot creation.

Result:
- failed checks: `window_rho_ok` only
- submit throughput: `0.835/s`
- completion throughput during submit phase: `0.835/s`
- promotion throughput: `0.695/s`
- submit p95: `1981.240 ms`
- rho: `1.0045`
- backlog_end: `0`
- quarantined: `0`

This is the key shift:
- all snapshot-path quarantines disappeared,
- throughput recovered above the 0.8 submit-phase gate,
- submit latency remained inside the SLO,
- the only remaining miss is a very small final-window rho overshoot.

Decision from v17:
- snapshot hot-path simplification is the correct fix direction,
- engine topology experiments are no longer the main path,
- the next work item should return to admission/control tuning for the final rho margin, not ORM recursion.

## 5-minute bucket analysis (v17)

From `m4-25m-diagnostic-route0-v17-snapshot-hotpath-20260309-195115.json`:

| Bucket | Accepted | Completions | Submit P95 | Route P95 | DB Write P95 |
|---|---:|---:|---:|---:|---:|
| 0 (0-300s) | 258 | 255 | 1749 ms | 85.1 ms | 5.21 ms |
| 1 (300-600s) | 256 | 257 | 1994 ms | 196.6 ms | 30.94 ms |
| 2 (600-900s) | 258 | 259 | 709 ms | 21.6 ms | 2.53 ms |
| 3 (900-1200s) | 256 | 256 | 19.8 ms | 13.0 ms | 1.76 ms |
| 4 (1200-1500s) | 223 | 222 | 10104 ms | 532.5 ms | 44.09 ms |
| 5 drain | 1 | 3 | 2026 ms | 21.8 ms | 2.07 ms |

Interpretation:
- Buckets 0-4 all remain active; there is no late quarantine cliff anymore.
- The remaining weakness is control margin in the final active bucket: route/db latency rises enough to push the final-window rho slightly over 1.0.
- This is now a throughput-shaping issue, not a snapshot-recursion issue.

## Exact failure signature

Database inspection for v17 is consistent with the report totals:
- `PROMOTED = 1252`
- `QUARANTINED = 0`

This removes the former dominant late-window fault from the snapshot path.

## Fresh-session replay result

Artifact: `/peer-review-artifacts/phase1-session-replay-20260308.txt`

Replay setup:
- copied the failing harness directory from a prior run to `/tmp/aether_repro_9b74`
- opened a **fresh SQLModel Session** against the copied `harness.db`
- loaded one previously quarantined proposal: `prop_fab9d3777519`
- re-ran `evaluate_proposal()` and `promote()` against the same repo state

Result:
- evaluation succeeded
- promotion succeeded
- no recursion error

Interpretation:
- the proposal data itself is not poisoned,
- the repo state itself is not inherently invalid,
- the failure depends on long-lived promotion-session state accumulated during the sustained run.

This is the strongest evidence in the package.

## First-principles diagnosis

### What is no longer the main blocker
- Not the controller math: rho stays under control.
- Not queue blow-up: backlog ends at zero.
- Not gate tails as the primary issue in these latest 25m diagnostics.
- Not route-handler latency alone: v12 submit p95 was ~28 ms and still throughput failed.

### What is the main blocker now
The main blocker is no longer snapshot-path recursion. After the hot-path simplification, the blocker moved back to **control margin on the final active bucket**.

Most likely mechanism:
- the system now runs cleanly through 25 minutes with zero quarantines,
- but the final 5-minute window still sees enough route/db write latency rise to push the trailing rho sample just over `1.0`,
- that leaves the system operationally healthy but just outside the strict `window_rho_ok` gate.

This is consistent with all evidence:
- quarantines dropped to zero in v17,
- throughput recovered above the submit-phase gate,
- backlog still ended at zero,
- the only failing check is `window_rho_ok` with `rho = 1.0045`.

## Code-path implications

Snapshot hot-path work that proved effective:
- `packages/aether/services/snapshot_service.py:create_snapshot()`
  - scalar repo/head lookup instead of `self.session.get(Repository, repo_id)`
  - `_get_by_hash()`
  - scalar `_resolve_base_snapshot_id()`
  - caller-provided changed-path override instead of `_collect_changed_paths()` on the hot path
  - `self.session.add(snapshot)` / `self.session.commit()`

The main product question has changed. It is now whether to:
1. soften the final-window admission rate slightly so rho stays below `1.0`, or
2. further flatten route/db latency in bucket 4 without sacrificing throughput.

## Recommended next steps

### P1 — Narrow control tuning on the now-clean snapshot path
Highest-confidence next move now.

Change:
- keep the snapshot hot-path simplification in place,
- run a narrow admission/backpressure sweep aimed only at the final-window rho margin.

Reason:
- the ORM recursion is gone in the valid 25m run,
- the system is now failing by a tiny amount on one strict gate,
- this is the right time to tune control rather than reopen snapshot internals.

Acceptance gate:
- rerun the same 25m diagnostic,
- keep quarantines at zero,
- bring `rho <= 1.0`.

### P2 — Revisit route/db latency in bucket 4 if rho tuning is insufficient
If small controller adjustments do not clear the final rho overshoot, the next target is the late-bucket route/db write rise.

### P3 — Only then rerun 60m strict
Do **not** run 60m strict before the 25m diagnostic is flat and the final-window rho passes.

Go gate:
- `submit_throughput >= 0.8/s`
- `completion_throughput_submit_phase >= 0.8/s`
- `rho <= 1.0`
- `backlog_end = 0`
- no quarantines
- bucket 4 remains healthy

## Bottom line

Aether is no longer failing for vague reasons.

At this point the problem is specific:
- snapshot-path SQLAlchemy recursion was real, and the hot-path simplification removed it in the valid 25m rerun,
- throughput and submit latency are now inside the intended band,
- the only remaining miss is a tiny final-window rho overshoot,
- therefore the next step is to tune the control margin, not to keep digging inside `SnapshotService`.

That is a much smaller problem.


## Update: March 11, 2026 - Snapshot Reproducer and 60m Strict Pass

### New artifacts
- Snapshot reproducer failure before iterative-chain fix:
  - `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v4.json`
- Snapshot reproducer pass after iterative-chain fix:
  - `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v5.json`
- 25m diagnostic pass after snapshot fix:
  - `.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v20-snapshot-fix-20260310-2205.json`
- 60m strict pass on the same baseline:
  - `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

### Root cause, now proven
The residual long-run collapse was caused by recursive snapshot-chain resolution, not by admission control and not by shared engine topology.

The dedicated reproducer made this clear:
- before the fix, `create_snapshot()` failed reproducibly at iteration `960`
- identity-map size remained flat at `1.0`
- failure stayed in the snapshot path

This falsifies the earlier session/identity-map accumulation hypothesis as the primary cause.

### Fix that changed the result
The decisive fix was replacing recursive delta-chain resolution with iterative chain replay in:
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/repository_service.py`

Additional supporting changes:
- scalar/text-query snapshot lookups and inserts in `packages/aether/services/snapshot_service.py`
- explicit quarantine phase counters in `packages/aether/services/swarm_harness.py`
- dedicated reproducer CLI in `apps/aether_cli/main.py`

### Reproducer results
Before iterative-chain fix:
- `passed = false`
- `first_failure_iteration = 960`
- `exception_type = RecursionError`
- `failing_phase = create_snapshot`
- `max_identity_map_size = 1.0`

After iterative-chain fix:
- `passed = true`
- `total_iterations = 1200`
- `first_failure_iteration = null`
- `max_identity_map_size = 1.0`

Interpretation:
- the recursion was in the snapshot-chain walk itself
- removing ORM hydration helped, but recursion was still present until the chain walk was made iterative

### 25m diagnostic result after snapshot fix
Artifact:
- `.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v20-snapshot-fix-20260310-2205.json`

Result:
- `passed = true`
- `submit_tps = 0.8567`
- `completion_submit_tps = 0.8567`
- `promotion_tps = 0.7133`
- `submit_p95_ms = 30.2`
- `rho = 0.9961`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

Bucket result:
- buckets 0-4 flat and healthy
- no late quarantine wave

### 60m strict result after snapshot fix
Artifact:
- `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

Result:
- `passed = true`
- all strict checks passed
- `submit_tps = 0.8556`
- `completion_submit_tps = 0.8556`
- `promotion_tps = 0.7330`
- `submit_p95_ms = 135.1`
- `rho = 0.9961`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

Bucket result:
- buckets 0-11 healthy
- no late-window degradation
- no quarantine regime reappeared

### Updated conclusion
Phase-1 is now in a materially different state:
- reproducer survives beyond the old failure zone
- 25m strict passes on the locked controller baseline
- 60m strict passes on the same fingerprint

This is the first clean evidence package showing the sustained-load blocker has been removed for the current 1000-agent strict profile.
