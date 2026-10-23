# Semantic Merge Bake-Off Phase 1

## Why this exists

The A1 density sweep established two important facts about the current Rust/Postgres path:

1. At **40 repos / 1 agent per repo**, the scalar promotion path sustained **26.74 promote TPS** with flat backlog and passing invariants.
2. At **40 repos / 2 agents per repo**, the system overloaded with rising backlog and elevated `409` contention while still preserving integrity.

This means the dominant limiter is now **scalar CAS contention density**, not the earlier pool-collapse hypothesis.

## What Phase 1 is

Phase 1 is a **serialized semantic merge lane**, not native CRDT heads.

The current Rust `DocumentStore` does not yet persist durable CRDT operation logs or frontier metadata across requests. It rebuilds state from materialized snapshot contents. Because of that, blindly removing the head CAS check would risk **lost updates** and **fractured lineage**.

The safe first bake-off is therefore:

- add a repo-scoped `crdt_enabled` feature flag (default `false`);
- keep the existing scalar CAS path as the default;
- on the flagged path, serialize `request_promotion` per repo with a Postgres advisory lock;
- re-read the latest canonical head inside the lock;
- revalidate proposal state inside the lock;
- merge against the latest snapshot state and persist a new canonical snapshot/head;
- track queueing tradeoffs with lock wait/hold telemetry.

## What this phase should prove

Phase 1 is meant to answer a narrow question:

> Can we eliminate client-visible head-conflict `409`s for mergeable workloads without introducing lost updates?

The intended verification points are:

- head-conflict `409`s drop to zero for the targeted workload;
- request accounting stays exact;
- no updates are lost under concurrency;
- `head_sequence` advances exactly with successful promotions;
- proposals do not get stranded in transient states.

## What this phase does **not** prove

This phase does **not** deliver native CRDT heads.

That would still require:

- durable CRDT op/state persistence;
- replayable frontier semantics across restarts;
- compaction/snapshotting rules for persisted CRDT state;
- convergence and replay-determinism tests beyond the current snapshot-diff model.

## Practical caution

Running merge and gate evaluation inside a transaction-level advisory lock is correctness-safe but can trade `409` conflicts for **queueing latency** and longer connection hold time. Lock wait/hold telemetry should therefore be treated as first-class evidence in the bake-off.
