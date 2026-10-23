# Aether Agent Loop and Rust Soak Plan Review

## Purpose

This document records the operating workflow now being used for Aether recovery and Rust-path performance work, plus the adversarial review of the latest Gemini-generated soak remediation plan.

This exists so the execution loop is durable in-repo even if a live agent session crashes.

## Current operating loop

The working loop is now:

1. Gemini prepares the implementation plan.
2. Copilot performs adversarial plan review and writes the review into the repo.
3. After the plan is accepted, Gemini implements.
4. Copilot performs adversarial code review on the implementation.
5. The cycle repeats, with important decisions and findings documented in `docs/`.

Additional current rules:

- keep the Python implementation and the Rust implementation separate;
- preserve the Rust workspace and its recovery docs as first-class artifacts;
- do not rely on chat memory alone for active technical decisions;
- write the important review trail into the repository as work proceeds.

## Reviewed input plan

Gemini proposed that the latest Rust mixed-source stress failure was primarily caused by synchronous filesystem I/O inside `resolve_snapshot_file_content` in `aether-rs/crates/server/src/main.rs`, and suggested replacing:

- `PathBuf::exists()`
- `PathBuf::is_dir()`
- `std::fs::read_to_string(...)`

with async `tokio::fs` equivalents.

Gemini also proposed re-running the mixed-source stress soak after the change and treating improved submit throughput / submit p95 as the success condition.

## Current evidence base

The latest observed Rust-path run was:

- `~/.aether/runtime_artifacts/phase1/swarm-safe-mixed_source_stress-20260504-143946.json`

Observed outcome:

- server stayed alive;
- no fail-open behavior observed;
- no submit failure spike;
- `submit_throughput_ok = false`;
- `submit_p95_ok = false`;
- intake ratio limiting and defer pressure dominated the run.

The performance collapse looked like queueing/backpressure under low promotion service rate, not a process crash.

## Adversarial review verdict

## Status

**Directionally valid, but not accepted as stated.**

The plan identifies a real blocking path, but it overstates root-cause certainty and proposes one dangerous implementation detail that should not be accepted as-is.

## What the plan gets right

### 1. Sync filesystem I/O is a real problem

The Rust promotion path does perform synchronous file operations inside async request handling:

- snapshot directory checks;
- manifest existence checks;
- file existence checks;
- manifest reads;
- snapshot file reads.

These happen inside `resolve_snapshot_file_content(...)`, which is called during promotion/materialization.

### 2. This is a plausible contributor to the throughput collapse

Under heavy promotion load, blocking I/O on tokio worker threads can absolutely reduce request concurrency and amplify queue pressure.

### 3. The plan correctly aims to preserve fail-closed behavior

The intent to preserve strict `INTERNAL_SERVER_ERROR` behavior for broken snapshot state is correct and should remain non-negotiable.

## Where the plan is too confident

### 1. The plan over-attributes the failure to sync file I/O alone

The current code shows a broader hot path than just `std::fs::read_to_string(...)`.

The promotion route:

- opens a SQLite transaction at the beginning of `request_promotion`;
- keeps that transaction open across proposal resolution;
- performs history reads inside that transaction;
- walks snapshot manifests and file state inside that transaction;
- applies diffs;
- reparses content with `StructuredDocument`;
- hashes the materialized content;
- only commits at the very end.

That means async file reads may help, but they do **not** by themselves prove that the dominant bottleneck is solved.

### 2. CPU-bound work remains on the async request path

The current hot path still includes:

- diff parsing and patch application via `diffy`;
- `StructuredDocument::set_content(...)`, which creates a `tree_sitter::Parser` and reparses source text;
- canonical content hashing via `Sha256`.

Those are CPU-bound costs, not async filesystem costs. Changing file reads to `tokio::fs` will not eliminate those costs.

### 3. SQLite transaction scope is likely part of the problem

The current route begins a transaction before the expensive materialization path and holds it until after:

- snapshot reconstruction;
- scoring;
- hashing;
- head creation;
- CAS update.

Even if WAL mode is enabled, that transaction scope can still become a serialization hotspot and should remain a primary suspect in the next review round.

### 4. Historical proposal loading is also part of the scaling story

The route currently fetches all promoted proposals for the repo before constructing the pending promotion state. That cost grows with repo history and is independent of sync-vs-async file reads.

## Adversarial rejection of one proposed implementation detail

### Do **not** accept `tokio::fs::try_exists(...).await.unwrap_or(false)`

This is the most important implementation objection.

Using `unwrap_or(false)` on async file existence checks would silently collapse some I/O errors into “file missing.” That is exactly the kind of convenience fallback that the Rust recovery memo already identified as dangerous.

If this refactor goes forward, existence checks must be written with explicit branching:

- handle `NotFound` intentionally where absence is part of the current contract;
- surface unexpected I/O failures explicitly;
- preserve the current strict error behavior for broken snapshot state.

The refactor must not smuggle in new best-effort behavior under the banner of async I/O cleanup.

## Revised adversarial guidance

The correct review stance is:

1. **Yes**, sync filesystem I/O should be removed from the async promotion path.
2. **No**, that change should not be sold as the proven single root cause.
3. **No**, `unwrap_or(false)` style existence handling should not be accepted.
4. **Yes**, the next implementation round should also measure whether transaction scope and CPU-bound canonicalization remain dominant after the async I/O change.

## Hidden bottlenecks Gemini should keep in scope

These are the main non-I/O suspects still visible in code:

### 1. Long-lived transaction scope

`request_promotion(...)` begins the transaction before the expensive path and commits after all reconstruction and writes complete.

### 2. CPU-bound canonicalization

`StructuredDocument::set_content(...)` reparses source using tree-sitter inside the request path.

### 3. Patch application and hashing

`diffy::Patch::from_str(...)`, patch apply, and `Sha256` hashing all happen inline during promotion.

### 4. Per-proposal DB work inside the hot path

`evaluate_proposal(...)` still does DB reads for gate results and trust metrics.

### 5. History growth cost

Fetching all promoted proposals up front may become increasingly expensive as repository history grows.

## Revised acceptance criteria for the plan

The plan should be accepted only if it is rewritten to say the following:

### Required scope

- replace sync filesystem reads/metadata checks in `resolve_snapshot_file_content(...)` with async equivalents;
- preserve fail-closed semantics without `unwrap_or(false)` fallbacks;
- keep behavior around delta manifests and missing snapshot state explicit and audited.

### Required caveat

- this is a **first bottleneck-reduction step**, not a proven complete fix.

### Required follow-up measurement

After implementation, re-run the same Rust mixed-source stress profile and compare:

- submit throughput;
- submit p95;
- ack-to-promote p95;
- backlog peak/end;
- throttle wait seconds;
- intake deferred submissions;
- promotion phase timing, especially evaluate/snapshot;
- whether transaction-scope and CPU-bound work still dominate.

### Required bar for success

The implementation can be considered promising if it materially improves throughput and latency while preserving strict failure semantics.

However, the team should **not** promise that async file I/O alone will immediately achieve:

- `submit_throughput > 40.0/s`
- `submit_p95 < 140 ms`

Those are acceptable soak targets, but they are too strong to use as the guaranteed outcome of this one narrowly scoped refactor.

## Concrete questions Gemini should answer before coding

1. Which current error cases should remain `500` and which, if any, intentionally map to “file not present”?
2. Will the refactor avoid all `unwrap_or(false)` / silent existence fallbacks?
3. After async I/O is done, what is the next suspected limiter: transaction scope, tree-sitter parsing, hashing, diff apply, or SQLite contention?
4. Does the implementation add measurements that let us distinguish those suspects in the next soak?

## Bottom line

Gemini's plan is **good as a first optimization direction**, but it is **not yet precise enough to approve unchanged**.

The safest adversarial position is:

- approve async I/O refactoring as a targeted improvement;
- reject any silent fallback semantics;
- require explicit acknowledgment that transaction scope and CPU-bound canonicalization may still dominate;
- require the implementation and the next soak report to document what improved and what did not.

## Implementation review outcome

Gemini then implemented the accepted narrowed version of the refactor in `aether-rs/crates/server/src/main.rs`.

### Reviewed change

The implementation replaced synchronous snapshot-path filesystem checks and reads with async `tokio::fs` calls in `resolve_snapshot_file_content(...)`.

Observed properties of the implementation:

- `std::fs::read_to_string(...)` was replaced with `tokio::fs::read_to_string(...).await`;
- directory/file/manifest existence checks were rewritten using explicit `tokio::fs::metadata(...).await` matching;
- no `try_exists(...).await.unwrap_or(false)` pattern was introduced;
- unexpected filesystem access errors still return `500 INTERNAL_SERVER_ERROR`;
- plain `NotFound` remains the only case that maps to “file absent” in the non-manifest fallback branches;
- delta `changed_paths` reads still fail closed if the manifest claims a changed file exists but the later read fails.

### Adversarial verdict on the implementation

**Accepted on correctness grounds.**

I did not find a fail-open regression in the async I/O patch.

Important clarification:

- the delta `changed_paths` branch does not preflight with `metadata(...)` before `read_to_string(...)`;
- that is still acceptable for correctness here because a missing or inaccessible changed file continues to fail closed as `500`, which is the required behavior for manifest/data corruption or unexpected I/O failure.

### What this acceptance does and does not mean

This code review acceptance means:

- the async I/O patch matches the strict error-handling bar;
- the patch does not introduce the silent fallback behavior that was explicitly rejected.

This does **not** mean:

- the throughput-collapse root cause is fully solved;
- the next soak is guaranteed to hit the target submit throughput and latency thresholds.

The remaining primary suspects still include:

- long-lived SQLite transaction scope across materialization;
- CPU-bound tree-sitter parsing in `StructuredDocument`;
- diff application and hashing on the hot path;
- general SQLite contention under promotion load.

### Validation status recorded with this review

The Rust workspace still completes:

- `cargo check`
- `cargo test`

with only pre-existing warnings unrelated to the async I/O semantics review.

## Transaction-scope plan review

After the first post-async-I/O soak, Gemini proposed a second remediation plan centered on tightening SQLite transaction scope in `request_promotion(...)`.

That plan is based on the observed result that:

- promotion throughput improved materially after the async I/O patch;
- backlog pressure dropped materially;
- submit throughput and submit p95 still remained far from target;
- the remaining dominant suspect is the long-lived transaction around promotion/materialization.

### Adversarial verdict

**Strong direction, but not accepted unchanged.**

The plan is much closer to the true bottleneck than the prior I/O-only plan, but it still needs one critical correctness safeguard before approval:

- explicit revalidation inside the final short transaction.

### What the plan gets right

### 1. Moving materialization out of the write transaction is the right next step

The current code holds one transaction open across:

- repo/head reads;
- proposal reads;
- trust/gate evaluation;
- snapshot resolution;
- diff application;
- `StructuredDocument` parsing;
- hashing;
- proposal updates;
- promotion/head/snapshot writes;
- final CAS.

That is exactly the shape of lock scope that can starve concurrent submit paths.

### 2. The current soak evidence supports the hypothesis

The first Rust soak after the async I/O patch showed:

- better promotion throughput;
- lower backlog peak;
- essentially unchanged submit latency and still-collapsed submit throughput.

That is consistent with “filesystem blocking mattered, but transaction scope is still the dominant limiter.”

### 3. Optimistic concurrency is the correct model

Letting concurrent contenders do speculative CPU work and then having only one win the final compare-and-swap is acceptable.

Wasted CPU under contention is preferable to silent state corruption.

## What the plan still misses

### 1. CAS alone is not enough unless the final transaction revalidates all precomputed assumptions

If proposal loading, trust scoring, gate reads, head lookup, snapshot base resolution, diff application, and hashing are moved outside the transaction, the final short transaction must explicitly prove that the precomputed work is still valid.

At minimum, before any durable write, the final transaction should re-check:

- the repo still has the same `head_id` / `head_sequence` used during precompute;
- each included proposal still belongs to the same repo;
- each included proposal is still `PENDING`;
- any other proposal properties required by the precompute remain the same if they were relied upon.

Without that revalidation, the system risks persisting:

- a snapshot hash computed from a stale base head;
- evidence or scores computed against a prior trust/gate state;
- proposal application order derived from a state that no longer exists.

### 2. Moving `evaluate_proposal(...)` outside the transaction changes the consistency model

Today `evaluate_proposal(...)` reads:

- gate results;
- trust metrics.

If those reads move fully outside the final transaction, then the persisted `policy_score`, `final_score`, and evidence payload become non-atomic with respect to the actual promotion commit.

That may be acceptable, but only if it is explicit.

The plan must choose one of these two positions:

1. **Strict path**: recompute score/evidence inside the final transaction after revalidation.
2. **Advisory path**: treat score/evidence as best-effort precomputed diagnostics and explicitly accept that they may drift slightly from commit-time truth.

The current plan does not state which contract it wants.

### 3. `get_combined_agent_score(...)` is not purely read-only for first-seen agents

`TrustService::get_metrics(...)` can insert a new metric row if none exists.

That means moving trust scoring outside the promotion transaction does not automatically make it a read-only phase.

This is not necessarily a blocker, because the submit path usually creates metrics first, but the plan should acknowledge that trust lookup may still perform writes in edge cases.

### 4. `resolve_snapshot_file_content(...)` should not float on an unpinned view without a head contract

Changing it from transaction-bound reads to pool-based reads is acceptable only if:

- the materialization phase is pinned to a specific precomputed base head / snapshot lineage;
- the final transaction rejects the work if that head changed before commit.

Otherwise the function would be correct per individual query, but the overall reconstructed state could drift across a concurrent head change.

## Required approval conditions

I would approve the transaction-scope plan if Gemini rewrites it with these explicit requirements:

### 1. Precompute phase contract

Outside the transaction, compute derived state from:

- `repo_id`
- `old_head_id`
- `old_head_sequence`
- the selected proposal set

and treat that derived result as provisional only.

### 2. Final transaction contract

Inside the final short transaction, before durable writes:

- re-read the repo row;
- verify the same `old_head_id` / `old_head_sequence`;
- re-read the selected proposals and verify they are still `PENDING` and still belong to the repo;
- abort with `409 CONFLICT` if any prerequisite changed.

### 3. Evidence/scoring contract

Either:

- recompute trust/gate-derived scores in the final transaction;

or:

- explicitly accept and document that the persisted score/evidence is advisory and may reflect slightly stale trust/gate state.

### 4. Keep fail-closed behavior

The refactor must not turn any invalid state into silent retries, best-effort promotion, or “just continue with stale precompute.”

If revalidation fails, the correct result is conflict/abort, not automatic coercion.

### 5. Keep success criteria honest

The stated goals:

- `submit_throughput >= 40.0/s`
- `submit_p95 < 140ms`

are legitimate targets for the soak program, but they should still be treated as **aspirational soak thresholds**, not guaranteed outcomes of this one refactor.

## Additional adversarial suggestion

If Gemini wants to minimize wasted transaction time further, the final short transaction should attempt to detect loser status as early as possible rather than doing avoidable database writes before the decisive repo-head check.

The exact ordering can vary, but the principle is:

- revalidate first;
- write only after the contender still looks eligible to win.

## Bottom line on this plan

This is the **right next plan category**.

I would describe it as:

- **approved in principle**
- **not yet approved for implementation unchanged**

The missing piece is a precise revalidation contract for precomputed state. Without that, the refactor risks trading lock contention for stale-state correctness bugs.

## Revised transaction-scope plan review

Gemini then revised the transaction-scope plan to explicitly include:

- final short-transaction revalidation;
- an advisory score/evidence contract;
- acknowledgment that first-seen trust lookup may still create a metric row;
- honest soak targets rather than guaranteed outcomes.

### Updated adversarial verdict

**Approved with two narrow amendments.**

This revised plan is now good enough to implement, but I would still require two small constraints to be stated explicitly in the implementation notes:

1. revalidate **both** `head_id` and `head_sequence`, not sequence alone;
2. perform the loser-detection/revalidation step **before any durable writes inside the final transaction**.

### Why the revised plan is now acceptable

### 1. It now has the critical provisional-state contract

The revised plan correctly states that everything computed outside the transaction is provisional and must be re-verified once the short transaction begins.

That removes the main correctness objection from the prior version.

### 2. The advisory score/evidence contract is explicit

The revised plan explicitly accepts that:

- `final_score`
- `policy_score`
- evidence payloads

are snapshots of the precompute phase rather than guaranteed commit-time truth.

That is acceptable as long as this remains a conscious product/engineering contract rather than an accidental side effect.

### 3. The first-seen trust-row caveat is now correctly surfaced

The revised plan no longer treats trust lookup as magically read-only. It acknowledges that a new metric row may be created outside the promotion transaction and accepts that this is operationally safe.

That is a reasonable tradeoff for this performance-focused step.

## Remaining required amendments

### 1. Revalidate `head_id` as well as `head_sequence`

The revised text currently says:

- verify `repo.head_sequence` matches the provisional sequence

That is close, but the implementation should verify:

- the same provisional `old_head_id`
- the same provisional `old_head_sequence`

Using both gives a tighter lineage check and better protects the promotion record fields that still reference the prior head.

### 2. Revalidate before writes, not merely after `tx.begin()`

The plan should be explicit that once the final transaction starts, it must:

1. re-read the repo row;
2. re-read the selected proposals;
3. reject on mismatch with `409 CONFLICT`;
4. only then perform:
   - `UPDATE aether_proposals`
   - `record_promotion_tx`
   - snapshot/promotion/head inserts
   - final repo/head mutation

This matters because the whole point of the refactor is to shorten the lock-holding section. If the transaction does several writes before discovering it has already lost the race, it gives back unnecessary lock time.

## Implementation guardrails

If Gemini proceeds, these are the guardrails I want preserved:

- no silent retry loops in the server route;
- no best-effort reuse of stale precomputed materialization after failed revalidation;
- `409 CONFLICT` on stale head or stale proposal eligibility;
- unchanged fail-closed behavior for broken snapshots/manifests/files;
- no weakening of repository CAS semantics.

## Bottom line on the revised plan

This plan is now:

- **approved for implementation**

with these two precise amendments:

1. validate `old_head_id` and `old_head_sequence`;
2. do revalidation before any final-transaction writes.

With those constraints, the plan is consistent with the current correctness bar and is the right next optimization to test.

## Transaction-scope implementation review outcome

Gemini then implemented the transaction-scope refactor and requested adversarial code review against the approved constraints.

### High-level result

**Not accepted yet. One material blocker remains.**

The implementation gets the main structure right:

- materialization and hashing moved out of the final write transaction;
- final transaction revalidates repo head lineage;
- final transaction revalidates proposal repo/status;
- conflict cases return `409 CONFLICT`;
- no obvious fail-open regression was introduced in the main promotion flow.

However, one hidden write path still escapes the intended transaction boundary.

### Blocking issue

#### Trust lookup can still perform a write during the provisional phase

`PromotionEngine::evaluate_proposal(...)` was moved to use `&sqlx::SqlitePool` outside the final transaction.

That function now calls:

- `trust_service.get_combined_agent_score(...)`

which delegates to:

- `get_combined_agent_score_tx(...)`
- `get_metrics_tx(...)`

and `get_metrics_tx(...)` inserts a new row into `aether_agent_metrics` if the agent has no metric row yet.

That means the supposedly provisional precompute phase is **not actually write-free** for first-seen agents.

### Why this is a real problem

This violates the intended lock-scope contract in two ways:

1. it allows durable database mutation during the precompute/materialization phase;
2. it can create racey behavior for first-seen agents, including unnecessary write contention and possible uniqueness-conflict failures outside the final promotion transaction.

In other words, the refactor successfully shortened the main promotion write transaction, but it did **not** fully remove writes from the provisional phase.

### Required fix before approval

The trust read used during provisional scoring needs a true read-only path.

Acceptable patterns would be:

1. add a read-only trust snapshot function that:
   - reads existing metric state if present;
   - returns a default in-memory metric if absent;
   - does **not** insert anything during provisional evaluation;

or

2. defer missing-row creation to the final transaction, for example by:
   - ensuring the metric row exists inside the short transaction before `record_promotion_tx(...)`;
   - keeping provisional scoring read-only even when a metric row is absent.

### Non-blocking observations

- the final-tx revalidation of `head_id` and `head_sequence` is present and correct in spirit;
- the final-tx proposal revalidation is also present and correctly rejects stale eligibility;
- the main transaction-scope narrowing appears directionally correct once the trust-write leak is fixed.

### Verdict

**No green light for the final soak yet.**

Fix the hidden trust-row write during provisional scoring first, then re-run adversarial code review on that patch.

## Readonly trust-fix review outcome

Gemini then implemented the trust-service fix by adding explicit read-only trust lookup helpers and switching provisional scoring to those helpers.

### Reviewed changes

- `trust_service.rs`
  - added `get_metrics_readonly(...)`
  - added `get_combined_agent_score_readonly(...)`
- `promotion_engine.rs`
  - provisional scoring now uses the read-only trust path
- `main.rs`
  - narrowed transaction-scope refactor remains in place with final-tx revalidation

### Adversarial verdict

**Accepted.**

I did not find a remaining hidden database write in the provisional phase after this change.

The important invariant now holds:

- the large precompute/materialization/scoring phase is read-only with respect to the database;
- durable writes are deferred to the final short transaction;
- the final transaction still revalidates repo head lineage and proposal eligibility before commit-time writes.

### Why this now passes review

`get_metrics_readonly(...)` returns an in-memory default `AgentMetric` when no row exists rather than inserting one.

That removes the prior blocker where first-seen agents could trigger an unexpected write during provisional scoring.

### Validation state

The current Rust workspace still completes:

- `cargo check`
- `cargo test`

with only pre-existing warnings unrelated to the correctness of the transaction-scope fix.

### Final verdict for this round

**Green-lit for the next mixed_source_stress soak.**

At this point the right next step is execution and measurement, not another design review.

## Harness external-promotion plan review

After the next soak result, Gemini correctly identified that the Rust promotion path was still being bypassed during `target_mode == "external"` runs.

That diagnosis is correct:

- proposal submission goes through the external API;
- `_promotion_loop(...)` still calls the in-process Python `PromotionEngine`;
- this means Python-side promotion/materialization can still dominate SQLite WAL lock contention during an "external" soak.

### Adversarial verdict

**Correct diagnosis, but the proposed implementation is not approved unchanged.**

The main problem is that the proposed external loop would call the thin API route:

- `POST /v1/repos/{repo_id}/promotions`

with locally selected `PENDING` proposal IDs.

That route is **not semantically equivalent** to the current in-process harness behavior.

## Why the plan is not safe as written

### 1. `/promotions` is a request primitive, not a replacement for `process_pending_proposals(...)`

The current local harness loop calls:

- `promotion_engine.process_pending_proposals(...)`

which performs:

- conflict-set resolution;
- non-conflicting batch selection;
- bounded scan/processing behavior;
- rejection of non-positive-score proposals;
- quarantine/requeue behavior on failures/timeouts;
- promotion timing accumulation.

By contrast, the current API route:

- evaluates exactly the proposals you pass in;
- promotes them if the score is positive;
- returns `success=False` if gates do not pass;
- does **not** replicate the full `process_pending_proposals(...)` lifecycle.

So blindly selecting local `PENDING` proposal IDs and posting them to `/promotions` is not behaviorally equivalent.

### 2. Arbitrary pending batches can be semantically wrong

`evaluate_batch(...)` is explicitly documented for:

- non-conflicting proposals evaluated together.

The proposed external loop would select pending proposals directly from SQLite without the conflict-aware selection logic the local engine currently applies.

That creates a real risk of:

- batching conflicting proposals together when the local loop would not;
- bypassing conflict-set selection semantics;
- measuring a different algorithm rather than just a different execution target.

### 3. Non-promotable proposals can get stuck in endless retries

The API route currently returns `200` with `success=False` when candidate score is non-positive and `force` is not used.

Unlike `process_pending_proposals(...)`, that route does **not** mark those proposals `REJECTED` in the same way the local engine does during batch processing.

So under the proposed plan, the external loop could repeatedly retry the same non-promotable proposals forever, distorting both performance and correctness semantics.

### 4. Local DB polling makes `external` mode only partially external

The proposed loop still relies on local SQLite reads to discover pending proposals.

That may be acceptable for a short-term localhost-only harness, but it is not a general external-mode contract.

If that compromise is chosen temporarily, it needs to be stated explicitly as:

- **localhost/shared-DB external soak mode**, not a fully remote external harness design.

## What I would approve instead

There are two safe directions.

### Option A: Add a server-side batch-processing API that mirrors harness semantics

Preferred.

Expose an endpoint that causes the server to run the equivalent of:

- `process_pending_proposals(...)`

for one iteration with explicit parameters:

- `batch_size`
- `proposal_scan_limit`
- `max_conflict_sets`
- `max_non_conflicting_batches`
- `max_processing_seconds`
- `prioritize_non_conflicting`

That keeps:

- proposal selection;
- conflict handling;
- status transitions;
- promotion execution

inside the same runtime that is being benchmarked.

### Option B: Externalize the harness loop, but preserve local engine semantics exactly

Only acceptable if Gemini can faithfully mirror the current Python loop behavior:

- separate conflict-set processing from non-conflicting batches;
- avoid sending arbitrary mixed pending IDs to `evaluate_batch(...)`;
- preserve reject/quarantine/requeue behavior;
- preserve timing/ack accounting.

That is much harder than the current proposal suggests.

## Minimum constraints if Gemini wants to keep the current direction

If Gemini still wants a harness-side external promotion loop without adding a new API endpoint, I would require all of the following:

1. Do **not** batch arbitrary `PENDING` proposals directly into `/promotions`.
2. Either:
   - promote one proposal at a time through the API;
   - or locally reconstruct the same conflict-aware/non-conflicting selection semantics before calling the API.
3. Handle `success=False` explicitly so non-promotable proposals do not spin forever.
4. Keep fail-closed behavior: no fallback from external promotion back to in-process Python promotion.
5. Explicitly label the mode as localhost/shared-DB constrained if local SQLite reads remain part of the design.

## Bottom line

The diagnosis is right, but the implementation plan is still too shallow.

I would summarize it as:

- **approved in principle**
- **not approved for implementation unchanged**

The cleanest next plan is to externalize promotion selection/execution through a server-side API that mirrors `process_pending_proposals(...)`, rather than trying to approximate that behavior by posting arbitrary pending IDs to the current `/promotions` route.

## ExternalPromotionEngine plan review

Gemini then proposed a more careful harness-side design:

- keep `process_pending_proposals(...)` orchestration in Python;
- add an `ExternalPromotionEngine` subclass;
- preserve local conflict-aware selection;
- short-circuit heavy non-conflicting batch evaluation by returning a mock candidate and delegating promotion to the external API.

### Adversarial verdict

**Still not approved unchanged.**

This is a much better direction than the previous plan, but it still introduces one material semantic mismatch.

### Main blocker

#### Mock `evaluate_batch(score=1.0)` changes the batch rejection lifecycle

The proposal says that `evaluate_batch(...)` would return a mock `PromotionCandidate` with `score=1.0`, and `promote(...)` would then call the external API. If the API returns `success=False`, the external engine would raise `ValueError`, causing the existing Python batch path to fall back to individual evaluation.

That is not equivalent to the current local engine.

In the current Python implementation, when a non-conflicting batch evaluates to a non-positive score, the engine rejects the entire batch directly:

- `update_status_many(..., ProposalStatus.REJECTED, "non_positive_score")`

Under the proposed external engine, a batch-level external rejection would instead become an exception and then an **individual fallback path**.

### Why this matters

Batch-level rejection and individual evaluation are not interchangeable.

A batch can fail because of combined gate/evidence behavior that is specific to the batch, not to each proposal individually. If the harness falls back to individual evaluation after a batch-level external rejection, it can:

- promote proposals that the true batch path would reject together;
- change reject/quarantine outcomes;
- distort the soak by changing the algorithm instead of only the execution target.

### Secondary semantic risk

#### Harness-side `document_store` can drift from external success

After a successful promotion, the current Python loop updates the harness-side `document_store` using:

- `candidate.proposals`

not the API response body.

If the externally promoted proposal set differs from the provisional mock batch candidate, the harness-side `document_store` will drift unless the external engine rewrites the candidate to exactly match the externally included proposal set before the success path continues.

The current plan does not make that reconciliation explicit.

## What would make this plan acceptable

I would approve a revised version if Gemini makes one of these contracts explicit:

### Option A: Preserve batch rejection semantics

If the external API returns `success=False` for a non-conflicting batch, the harness must map that outcome to the same batch-level lifecycle the local engine would have used, rather than automatically falling back to individual evaluation.

That means the implementation must define when external batch failure means:

- reject the whole batch;
- retry later;
- quarantine;
- or fall back to individual evaluation.

### Option B: External response must classify failure modes

If fallback behavior is still desired, the external path must distinguish at least:

- non-positive score / gate rejection;
- conflict / stale state;
- transient failure;
- internal error.

Without that distinction, the harness cannot preserve the current lifecycle rules.

### Option C: Reconcile local candidate state after external success

On successful external promotion, the harness must ensure that the proposals applied to the local `document_store` match the externally included proposal set, not merely the provisional mock candidate.

## Bottom line

This plan is closer than the previous one, but it remains:

- **approved in principle**
- **not approved for implementation unchanged**

The remaining required contract is:

1. batch-level rejection handling;
2. external failure classification;
3. local candidate/document-store reconciliation with the externally included proposal set.

## Updated ExternalPromotionEngine plan review

Gemini then revised the `ExternalPromotionEngine` plan to add:

- explicit external failure classification;
- explicit candidate/document-store reconciliation on external success;
- an explicit note that the current synthetic soak should remain in the positive-score regime.

### Adversarial verdict

**Still not approved unchanged.**

This revision fixes one of the earlier concerns, but it still leaves a concrete stale-state bug in the fallback path.

### What improved

The revised plan now explicitly says that, on successful external promotion, the candidate proposal list will be filtered to the externally included proposal set before the harness updates the local `document_store`.

That is the right reconciliation direction.

### Remaining blocker

#### Individual fallback would reuse stale proposal objects after external batch failure

The current Python batch loop already contains an exception path that falls back from batch evaluation to per-proposal evaluation:

- `_process_non_conflicting(...)`
- `except Exception as e:`
- iterate `for proposal in batch:`
- call `evaluate_proposal(proposal, document_store)`

That fallback path assumes the proposals in `batch` are still valid local candidates.

Under the proposed external engine, batch failure conditions such as:

- `HTTP 200` with `success=False`
- `HTTP 409`
- some `HTTP 400` cases

can arise **after** the external API has already observed newer repository/proposal state than the stale local `batch` objects represent.

If the harness then falls back directly to `evaluate_proposal(proposal, document_store)` on those stale objects without re-fetching them, it can:

- re-evaluate proposals that are no longer `PENDING`;
- mark already-changed proposals back to `EVALUATING`;
- violate the intended lifecycle of proposals that should simply be re-scanned next iteration instead.

### Why this is a real correctness problem

This is not just a performance concern.

The whole reason the previous harness plan was rejected was semantic drift between:

- current local lifecycle rules;
- proposed externalized batch behavior.

Reusing stale proposal objects after external batch failure is exactly that kind of drift.

### What would make this acceptable

I would approve the next revision if Gemini makes one of these changes explicit:

#### Option A: No immediate individual fallback for stale-state failures

For clearly stale-state failures such as:

- `HTTP 409`
- `200 success=False` caused by now-ineligible batch state

do **not** immediately fall back to local individual evaluation.

Instead:

- log the stale batch;
- return control to the loop;
- let the next iteration re-scan fresh `PENDING` state from the database.

#### Option B: Re-fetch proposal state before any individual fallback

If Gemini still wants fallback to individual evaluation after external batch failure, the engine must first:

- re-fetch those proposal IDs from the database;
- keep only proposals that are still `PENDING` and belong to the expected repo;
- only then run local individual evaluation on that refreshed subset.

Without that refresh step, fallback is not safe.

### Updated bottom line

This revised plan is:

- **approved in principle**
- **not approved for implementation unchanged**

The remaining blocker is now more precise:

1. success reconciliation is addressed;
2. failure classification is partly addressed;
3. stale-object fallback semantics are still unsafe.

## Further updated ExternalPromotionEngine plan review

Gemini then revised the plan again and added a concrete fallback safety fix inside `promotion_engine.py`:

- refresh proposal state before individual fallback evaluation;
- skip fallback work if the proposal is no longer `PENDING`.

### Adversarial verdict

**Still not approved unchanged.**

The fallback refresh is directionally correct and should likely be adopted independently as a general correctness hardening change.

However, the larger externalization plan still does **not** preserve strict semantic parity with the current Python batch path.

### What improved

The proposed fallback fix addresses a real race in the current batch exception path.

Today, after `batch_promotion_failed`, the engine iterates the original in-memory `batch` objects and immediately calls `evaluate_proposal(...)` on them. If the world changed between the batch attempt and that fallback path, those stale objects can be wrong.

Refreshing the proposal row first and skipping anything no longer `PENDING` is a valid correctness improvement.

### Remaining blocker

#### The Rust `/promotions` endpoint does not implement Python-equivalent batch evaluation semantics

The updated plan still assumes that `ExternalPromotionEngine.evaluate_batch(...)` can safely return a synthetic candidate and let `ExternalPromotionEngine.promote(...)` offload the real batch decision to the Rust API while preserving local semantics.

That assumption is false against the current Rust implementation.

In the current Python engine, `evaluate_batch(...)` does the following for a non-conflicting batch:

- clone the current `document_store`;
- apply **all** batch proposals onto one candidate store;
- create **one** combined snapshot;
- run gates against that **combined** workspace;
- compute one aggregate batch score.

In the current Rust `request_promotion(...)` implementation, the server does **not** evaluate the batch that way.

Instead, it currently:

- loops over the requested proposal IDs;
- evaluates each proposal individually via `engine.evaluate_proposal(...)`;
- accumulates those per-proposal results;
- then materializes and commits the included set.

That is not the same contract as Python `evaluate_batch(...)`.

### Why this matters

This is the core semantic gap.

A non-conflicting batch can be valid proposal-by-proposal, but still fail when evaluated as one combined repository state.

Examples include:

- combined gate failures;
- aggregate workspace effects that only appear once multiple proposals are applied together;
- score/evidence differences that depend on the final combined snapshot rather than the per-proposal intermediate states.

Because the Rust endpoint currently does not run a Python-equivalent combined batch gate evaluation, the harness cannot claim strict semantic parity by delegating batch promotion to it.

### Why the fallback refresh does not solve this

The refresh fix only makes the exception fallback safer.

It does **not** change the fact that the success path would still be driven by an external API whose batch semantics differ from the local engine.

So the plan improves concurrency correctness in the fallback path, but it still fails the larger parity requirement.

### What would make this acceptable

I would approve the next revision if Gemini chooses one of these explicit paths:

#### Option A: Narrow the claim and treat this as a soak-only approximation

If the goal is explicitly changed from:

- strict semantic parity

to:

- a soak-targeted external promotion approximation for synthetic non-conflicting traffic

then this could be acceptable as an experiment, provided that limitation is stated plainly.

That would be a different approval than the one currently requested.

#### Option B: Add true combined batch evaluation to the Rust endpoint

If strict semantic parity is required, the Rust promotion path must implement the actual Python `evaluate_batch(...)` contract:

- combined candidate store;
- combined snapshot;
- combined gate run;
- combined score/evidence decision.

Only after that exists can `ExternalPromotionEngine` safely delegate non-conflicting batch promotion there without changing semantics.

### Updated bottom line

This newest plan is:

- **approved in principle** for the fallback refresh hardening;
- **not approved** as a parity-preserving external promotion refactor.

The remaining blocker is no longer just stale fallback state.

It is now the primary architectural mismatch:

- the current Rust `/promotions` endpoint is still not equivalent to Python batch evaluation.

## Commit-promotion primitive plan review

Gemini then proposed a stricter split:

- keep Python `evaluate_batch(...)` and `evaluate_proposal(...)` unchanged;
- add a new Rust `POST /v1/repos/{id}/commit_promotion` route;
- offload only the final promotion commit transaction to Rust;
- keep the fallback refresh fix in Python.

### Adversarial verdict

**Much closer, but still not approved unchanged.**

This is the first version that actually addresses the earlier batch-semantics blocker.

By keeping Python batch evaluation in control, this design can preserve combined snapshot/gate/score semantics **if** the final handoff contract is tightened.

### What improved

This plan fixes the biggest architectural mistake in the earlier externalization attempts.

The earlier versions tried to use Rust `/promotions` as if it were equivalent to Python `evaluate_batch(...)`.
It is not.

The new plan instead treats Rust as a commit sequencer while Python remains the evaluator.

That is the right direction for strict semantic parity.

### Remaining blocker 1

#### `409` / indeterminate external commit failures must not fall back to local individual evaluation

The plan currently says:

- Rust `commit_promotion` returns `409` for CAS / no-longer-pending failures;
- Python raises `RuntimeError("external_head_conflict")`;
- the outer orchestration loop then falls back to individual evaluation.

That is not safe.

Why:

At the point `commit_promotion(...)` is called, Python has already evaluated the candidate against a specific in-memory `document_store` representing an earlier head state.

If Rust returns `409`, that means the repository head or proposal eligibility changed before commit.

At that moment, the local `document_store` is stale.

So falling back to:

- `evaluate_proposal(proposal, document_store)`

would evaluate against the wrong repository state, even if the proposal row itself is refreshed first.

Refreshing the proposal row is not enough.

The proposal object may be fresh while the `document_store` still reflects the old head.

### Why this is a correctness bug

This is exactly the kind of stale-state semantic drift we have been trying to eliminate.

After a `409`, the correct behavior is not "try locally with the same store."

The correct behavior is:

- abort the current promotion attempt;
- return control to the outer loop;
- reload fresh repository/head state;
- rebuild or re-fetch the canonical `document_store`;
- let the next iteration re-evaluate from the new head.

### Remaining blocker 2

#### Timeout / connection-loss outcomes are indeterminate and also cannot safely trigger local fallback

For an external commit primitive, transport failures such as:

- timeout;
- connection reset;
- unknown `5xx` after partial server work;

are not equivalent to local evaluation errors.

They are **indeterminate commit outcomes**.

Python cannot assume the promotion failed just because the client did not receive a clean success response.

If the Rust server committed successfully but the client timed out reading the response, then local fallback or retry logic can corrupt semantics.

For indeterminate external commit outcomes, the safe path is the same:

- stop the current attempt;
- rescan DB/head state;
- rebuild local state before doing any more evaluation.

### Remaining blocker 3

#### Durable side effects are still underspecified

The current local Python `promote()` contract does more than the proposed "6-table" commit summary.

It also currently performs or triggers:

- `aether_operation_graph_nodes` insertion for replay/provenance;
- conflict-set resolution (`SUPERSEDED` losers + conflict-set metadata);
- head-update event publication.

The new plan does not yet make it explicit which side owns those effects after a successful external commit.

That ownership must be defined precisely.

At minimum, the implementation must specify whether:

- Rust `commit_promotion` creates the operation-graph row;
- Python still resolves conflict sets after success;
- Python still publishes head/conflict events after success;
- or some of these effects are intentionally being dropped.

Dropping them would not preserve parity.

### Important deployment assumption

This plan also only works when Python and Rust share:

- the same SQLite database;
- and the same reachable snapshot storage paths.

Python snapshot creation currently writes `aether_snapshots.storage_path` directly, and the Rust server later resolves snapshots by reading that path from SQLite.

So this design is valid for the current co-located soak setup, but it is **not** a generic remote external-mode design unless shared storage is guaranteed.

### What would make this plan acceptable

I would approve the next revision if Gemini makes these contracts explicit:

1. On `409` or any indeterminate external commit error:
   - do **not** fall back to local individual evaluation with the current `document_store`;
   - abort and force a fresh next-iteration rescan/rebuild.
2. Define the post-success ownership of:
   - operation-graph insertion;
   - conflict-set resolution;
   - event publication.
3. State the shared DB/shared snapshot-storage assumption explicitly if this is intended only for the current soak topology.

### Updated bottom line

This plan is:

- **approved in principle** for the architectural direction;
- **not approved unchanged** until the commit-failure and durable-side-effect contracts are tightened.

## Finalized commit-promotion plan review

Gemini then tightened the plan again:

- introduce `ExternalCommitAbortedError`;
- abort the whole iteration on `409` / timeout / indeterminate external commit failures;
- move `aether_operation_graph_nodes` and conflict-set resolution into the Rust transaction;
- keep Python-side event publication after successful external commit.

### Adversarial verdict

**Still not green-lit unchanged, but now the remaining gap is narrow and specific.**

The new abort contract is the correct fix for the stale-`document_store` bug.

This version is the closest plan so far.

### What is now correct

#### Abort + rescan on external commit failure

This part is now aligned with the real engine invariants.

If the external commit returns:

- `409`;
- timeout;
- connection loss;
- or other indeterminate commit outcomes,

then aborting the whole iteration and forcing a fresh rescan/rebuild on the next loop is the correct behavior.

That fixes the earlier stale-state fallback problem.

### Remaining blocker

#### Conflict-set parity is still incomplete unless losing proposals are also updated atomically

The plan says Rust will atomically resolve:

- `aether_operation_graph_nodes`;
- `aether_conflict_sets`.

But the current Python `resolve_conflict_set(...)` contract does more than update the conflict-set row.

It also:

- marks all losing proposals in the set as `SUPERSEDED`;
- records `STATUS_SUPERSEDED` system metric events for those losers;
- publishes a `CONFLICT_RESOLVED` event after the DB updates complete.

The current proposal text only mentions updating `aether_conflict_sets` inside Rust.

That is not enough for parity.

### Why this matters

If Rust commits:

- the winner as promoted;
- the head advance;
- the conflict-set row as resolved;

but leaves losing proposals as `PENDING`, then Python and Rust state diverge immediately.

Those loser proposals can be picked up again in future scans even though the conflict set is already resolved.

That would break queue semantics and skew soak metrics.

### Additional parity detail

The current Python status-update path also records system metric rows such as:

- `STATUS_PROMOTED`;
- `STATUS_SUPERSEDED`;
- `STATUS_REJECTED`;
- `STATUS_QUARANTINED`.

If Rust directly updates proposal statuses during `commit_promotion`, the plan must explicitly decide where those metric rows are created.

Otherwise the harness telemetry will drift from the current local path.

### Event naming mismatch

The plan text says Python will publish:

- `HEAD_UPDATED`;
- `PROMOTION_CREATED`;
- `CONFLICT_RESOLVED`.

But `PROMOTION_CREATED` is not an existing event type in the current Python event model.

The current local `promote()` path publishes:

- head-update events directly;
- proposal lifecycle events indirectly via `update_status_many(...)`;
- conflict-resolved events via `resolve_conflict_set(...)`.

So the parity target should be phrased in terms of the **actual current events**, not a new event name that does not currently exist.

### What would make this green-lightable

I would approve the next revision if Gemini makes these final contracts explicit:

1. Rust `commit_promotion` must atomically update conflict losers to `SUPERSEDED`, not just mark the conflict set resolved.
2. The design must specify where `SystemMetricEvent` rows are created for:
   - promoted winners;
   - superseded losers.
3. Python post-success publication must mirror the **existing** event model, not invent `PROMOTION_CREATED`.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **very close**.

The last material blocker is conflict-resolution/status-metric parity, not the overall architecture.

## Latest commit-promotion plan review

Gemini then revised the plan again and explicitly added:

- loser proposal `SUPERSEDED` updates inside Rust;
- metric-row insertion for promoted and superseded proposals;
- Python-side publication of `HEAD_UPDATED`, `PROPOSAL_PROMOTED`, and `PROPOSAL_SUPERSEDED`.

### Adversarial verdict

**Still not green-lit unchanged, but the remaining gap is now mostly schema/event naming accuracy.**

The architectural direction is now sound.

### What is now correct

This revision finally addresses the previous semantic blocker around conflict losers remaining `PENDING`.

If implemented against the real schema, atomically:

- promoting winners;
- superseding losers;
- inserting system metric rows;
- resolving the conflict set;

would preserve the important queue-state and telemetry behavior.

### Remaining blocker 1

#### The telemetry table name in the plan is wrong

The plan text says Rust will insert rows into:

- `aether_system_metrics`

But the actual table is:

- `aether_system_metric_events`

This is not a small wording issue; it is the real persisted telemetry table used by the Python code and the Rust schema.

### Remaining blocker 2

#### The conflict-set field name in the plan is wrong

The plan text says Rust will update:

- `resolved_by_proposal_id`

But the actual conflict-set model uses:

- `active_choice_id`

along with:

- `resolved`
- `resolved_at`

So the Rust transaction must update the real schema fields, not the name used in the prose plan.

### Remaining blocker 3

#### `PROPOSAL_SUPERSEDED` is not an existing event type, so the current event-parity text is still wrong

The updated plan says Python will publish:

- `PROPOSAL_PROMOTED`
- `PROPOSAL_SUPERSEDED`

But the current event model has:

- `PROPOSAL_PROMOTED`
- `PROPOSAL_REJECTED`
- `PROPOSAL_QUARANTINED`
- no `PROPOSAL_SUPERSEDED`

This is important because the current local Python path also does **not** emit a dedicated superseded proposal lifecycle event from `update_status_many(...)`.

Instead, the current local conflict-resolution behavior is:

- losers are marked `SUPERSEDED`;
- `STATUS_SUPERSEDED` system metric rows are recorded;
- a single `CONFLICT_RESOLVED` event is published.

So publishing `PROPOSAL_SUPERSEDED` would actually introduce new behavior rather than preserve parity.

### What would make this green-lightable

I would approve the next revision if Gemini makes these final corrections explicit:

1. Use the real telemetry table name:
   - `aether_system_metric_events`
2. Use the real conflict-set field names:
   - `active_choice_id`
   - `resolved`
   - `resolved_at`
3. Match the real event model after successful external commit:
   - `HEAD_UPDATED`
   - `PROPOSAL_PROMOTED` for winners if that remains the chosen parity mechanism
   - `CONFLICT_RESOLVED` for resolved conflict sets
   - no invented `PROPOSAL_SUPERSEDED` unless the product is intentionally changing behavior

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **extremely close**.

The remaining blockers are now concrete schema/event mismatches, not architectural flaws.

## Latest naming-corrected commit-promotion plan review

Gemini then corrected the prior schema and event names:

- `aether_system_metric_events` instead of the wrong telemetry table name;
- `active_choice_id` instead of the wrong conflict-set field name;
- `CONFLICT_RESOLVED` instead of the non-existent `PROPOSAL_SUPERSEDED`.

### Adversarial verdict

**Still not green-lit unchanged, but the remaining gap is now extremely small.**

The design is now effectively correct in architecture, transaction ownership, and high-level parity intent.

### Remaining blocker 1

#### The plan still references a conflict-event helper that does not exist

The plan says Python will publish:

- `create_conflict_resolved_event(EventType.CONFLICT_RESOLVED, ...)`

There is no such helper in the current codebase.

The actual current implementation publishes conflict resolution using:

- `create_proposal_event(...)`

with:

- `event_type=EventType.CONFLICT_RESOLVED`
- `proposal_id=chosen_proposal_id`
- `affected_paths=[conflict_set.path]`
- extra payload including `conflict_set_id` and `superseded_proposals`

So if the goal is strict parity, the plan should reference the real helper/payload shape rather than a new helper name.

### Remaining blocker 2

#### Conflict-set parity still needs `resolved_at`, not just `resolved` + `active_choice_id`

The plan currently says Rust will update:

- `resolved = 1`
- `active_choice_id = ...`

But the current Python implementation also sets:

- `resolved_at = datetime.now(UTC)`

That timestamp is part of the persisted lifecycle state and should be included if the goal is full parity.

### Remaining blocker 3

#### Proposal-promoted event parity should preserve the current payload shape

The current local path publishes `PROPOSAL_PROMOTED` events via `update_status_many(...)`, which builds them through `create_proposal_event(...)` and includes the existing proposal fields, especially:

- `proposal_id`
- `agent_id`
- `affected_paths`
- `old_status`

The plan says Python will publish `PROPOSAL_PROMOTED`, but it does not yet say that it will mirror this existing payload shape.

That is a smaller gap than the earlier blockers, but if the standard remains "absolute parity", it should be stated explicitly.

### What would make this green-lightable

I would approve the next revision if Gemini makes these final corrections explicit:

1. Use the real current helper for conflict resolution publication:
   - `create_proposal_event(EventType.CONFLICT_RESOLVED, ...)`
2. Include `resolved_at` in the Rust conflict-set update.
3. State that Python post-success `PROPOSAL_PROMOTED` publication mirrors the existing `create_proposal_event(...)` payload shape, including `agent_id`, `affected_paths`, and `old_status`.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **one revision away**.

The remaining issues are now helper/payload fidelity details, not design flaws.

## Final payload-shape plan review

Gemini then revised the plan again to match:

- `create_proposal_event(...)` for both `PROPOSAL_PROMOTED` and `CONFLICT_RESOLVED`;
- `resolved_at` in the Rust conflict-set update;
- stricter event-payload language.

### Adversarial verdict

**Still not green-lit unchanged.**

This is now down to a few exact payload-shape mismatches in the plan text.

### Remaining blocker 1

#### `create_proposal_event(...)` does not take a `payload={...}` wrapper

The plan says Python will call:

- `create_proposal_event(..., payload={"old_status": "PENDING"})`

and similarly for conflict-resolution payloads.

That is not how the helper works.

The real helper signature is:

- `create_proposal_event(..., **extra_payload)`

and those keyword arguments are flattened directly into the event payload.

So exact parity requires:

- `old_status="PENDING"`
- `reason=None`
- `conflict_set_id=...`
- `superseded_proposals=...`

not a nested `payload={...}` object.

### Remaining blocker 2

#### `proposal.affected_paths` must be decoded before event publication

The plan says `PROPOSAL_PROMOTED` will use:

- `proposal.affected_paths`

But in the current model, `Proposal.affected_paths` is stored as a JSON string, not a `list[str]`.

The current local path publishes events using:

- `json.loads(proposal.affected_paths)`

So passing the raw field would not preserve the payload shape.

### Remaining blocker 3

#### Current `CONFLICT_RESOLVED` parity uses an empty `agent_id`, not the chosen proposal agent

The plan says the conflict-resolved event will use the chosen proposal's `agent_id`.

But the current Python implementation publishes:

- `agent_id=""`

for `EventType.CONFLICT_RESOLVED`, with a comment that agent id is not relevant there.

If the goal is strict parity with today's implementation, the plan should keep that exact behavior.

### Remaining blocker 4

#### `HEAD_UPDATED` parity should also include the current extra payload fields

The plan says Python will publish `create_head_event(...)`, but for exact parity it should explicitly preserve the current extra payload fields:

- `promotion_id=promotion.id`
- `included_proposals=[p.id for p in candidate.proposals]`

Those are part of the current local head-update event payload.

### What would make this green-lightable

I would approve the next revision if Gemini makes these last payload details explicit:

1. Use flattened kwargs with `create_proposal_event(...)`, not `payload={...}`.
2. Decode `proposal.affected_paths` with `json.loads(...)` before publishing promoted events.
3. Use `agent_id=""` for `CONFLICT_RESOLVED` if strict parity with current behavior is the goal.
4. Include `promotion_id` and `included_proposals` in `HEAD_UPDATED` publication.
5. Include `reason=None` alongside `old_status="PENDING"` for promoted proposal events if the standard is exact payload parity.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **down to exact event-payload mechanics**.

## Event-order parity review

Gemini then tightened the remaining payload details again:

- `promoted_at` is explicitly written in Rust;
- promoted events include `old_status="PENDING"` and `reason=None`;
- superseded telemetry includes `event_reason="Superseded by {active_choice_id}"`.

### Adversarial verdict

**Still not green-lit unchanged.**

At this point the remaining mismatch is not schema or payload shape. It is **event ordering**.

### Remaining blocker

#### The plan's post-success event sequence still does not match the current Python engine

In the current local Python `promote()` flow, the order is:

1. `PROPOSAL_PROMOTED` events via `update_status_many(...)`
2. `CONFLICT_RESOLVED` events via `resolve_conflict_set(...)` when applicable
3. `HEAD_UPDATED` event last

That ordering is visible in the current implementation:

- status updates happen first;
- conflict resolution happens next;
- head event is published only afterward.

The plan text still describes post-success publication in the order:

1. `HEAD_UPDATED`
2. `PROPOSAL_PROMOTED`
3. `CONFLICT_RESOLVED`

That is not strict parity.

### Why this matters

If any event consumer, harness metric hook, or realtime subscriber depends on the current sequence, changing the order is an observable behavior change.

Since the stated goal is exact parity, the order should match the current engine.

### What would make this green-lightable

I would approve the next revision if Gemini makes the event ordering explicit as:

1. publish `PROPOSAL_PROMOTED` events first;
2. publish `CONFLICT_RESOLVED` events second;
3. publish `HEAD_UPDATED` last.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **down to exact event ordering**.

## Trust-metric parity review

Gemini then corrected the event ordering and the remaining payload/timestamp details.

### Adversarial verdict

**Still not green-lit unchanged.**

There is still one material persisted side effect missing from the plan:

- promotion trust-metric updates.

### Remaining blocker

#### The plan still omits `aether_agent_metrics` promotion updates

In the current local Python `promote()` flow, successful promotion does not only:

- update proposal statuses;
- resolve conflicts;
- write promotion/head rows;
- publish events.

It also updates agent trust metrics for winning proposals via:

- `self.trust_service.record_promotion(proposal.agent_id, commit=False)`

followed by:

- `self.session.commit()`

That means local promotion currently increments persisted trust state in `aether_agent_metrics`.

The current external `commit_promotion` plan does not mention any equivalent Rust-side trust update.

### Why this matters

This is not cosmetic telemetry.

Trust metrics feed back into later scoring and candidate ordering via:

- `get_combined_agent_score(...)`

So omitting promotion trust updates would change future evaluation behavior, not just observability.

That breaks the stated goal of strict semantic parity.

### What would make this green-lightable

I would approve the next revision if Gemini makes one of these explicit:

1. Rust `commit_promotion` calls the Rust trust service equivalent of:
   - `record_promotion_tx(..., agent_id, "global", 1.0)`
   for each promoted winner inside the same transaction; or
2. Python performs an equivalent persisted trust update after external success in a way that preserves the current semantics and ordering.

For strict parity, the first option is cleaner and safer.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **still missing trust-metric persistence parity**.

## Final trust-cardinality review

Gemini then added Rust-side trust persistence to `commit_promotion` by passing `agent_ids` and calling:

- `trust_service.record_promotion_tx(&mut *tx, agent_id, "global", 1.0)`

inside the Rust transaction.

### Adversarial verdict

**Still not green-lit unchanged.**

The remaining issue is now extremely narrow:

- trust-update cardinality and source of truth.

### Remaining blocker

#### Trust promotion updates must happen once per promoted proposal, not once per distinct agent

In the current Python implementation, trust promotion updates happen in this loop:

- `for proposal in candidate.proposals:`
- `self.trust_service.record_promotion(proposal.agent_id, commit=False)`

That means trust is incremented **once per promoted proposal**.

If one agent has two promoted proposals in the same batch, Python increments:

- `promoted_count += 2`
- `trust_alpha += 2 * weight`

The current plan says the request payload will include:

- `agent_ids: A list of agent_id strings belonging to the included proposals`

and Rust will iterate that list.

That can preserve parity **only if** the list preserves per-proposal multiplicity and is not deduplicated anywhere.

If the payload is interpreted as a set of winning agents rather than one entry per winning proposal, trust updates will undercount.

### Why this matters

This is a real semantic difference, not a stylistic one.

`record_promotion_tx(...)` increments:

- `metric.promoted_count += 1`
- `metric.trust_alpha += weight`

per call.

So losing multiplicity changes the future trust-adjusted scoring behavior.

### Stronger contract

For strict parity, the cleanest implementation is:

- do **not** trust client-supplied `agent_ids` as the authoritative source for trust updates;
- instead derive the agent IDs from the already revalidated included proposal rows inside the Rust handler;
- call `record_promotion_tx(...)` once for each promoted proposal row.

That automatically preserves multiplicity and avoids payload drift.

### What would make this green-lightable

I would approve the next revision if Gemini makes one of these explicit:

1. Rust derives the agent ID from each revalidated included proposal row and calls `record_promotion_tx(...)` once per row; or
2. The request contract explicitly states that `agent_ids` is a per-proposal list preserving duplicates and Rust validates it against the included proposal rows before using it.

Option 1 is the safer and more parity-faithful design.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **down to trust-update multiplicity/source fidelity**.

## Head-canonical parity review

Gemini then tightened trust multiplicity by deriving agent IDs from the revalidated proposal rows inside Rust and calling `record_promotion_tx(...)` once per included proposal.

### Adversarial verdict

**Still not green-lit unchanged.**

There is still one material persisted side effect missing from the plan:

- old-head canonical demotion.

### Remaining blocker

#### The external commit path must mark the previous head `is_canonical = False`

In the current local Python implementation, `RepositoryService.update_head(...)` does not only:

- insert the new head row;
- update `aether_repositories.head_id` / `head_sequence`.

It also explicitly marks the prior head row as non-canonical:

- `old_head.is_canonical = False`

before committing.

The current `commit_promotion` plan describes:

- inserting the new `aether_heads` row;
- updating the repository row;

but it does not explicitly include demoting the previous head.

### Why this matters

This is persisted state, not just an internal convenience flag.

If the old head remains `is_canonical = True`, the external path will diverge from the local repository history model and any code that relies on canonical head markers can observe inconsistent state.

### What would make this green-lightable

I would approve the next revision if Gemini makes this explicit in the Rust transaction:

1. if the repository currently has `head_id`, update that existing `aether_heads` row to:
   - `is_canonical = 0`
2. then insert the new head row with:
   - `is_canonical = 1`
3. then update the repository row to point to the new head.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved unchanged**;
- **still missing old-head canonical demotion parity**.

## Event-state interleaving review

Gemini then added old-head canonical demotion and the remaining trust-multiplicity detail.

### Adversarial verdict

**Still not green-lit unchanged.**

The remaining mismatch is now deeper than field parity:

- event-to-database interleaving.

### Remaining blocker

#### The external single-transaction design changes the DB state visible at event time

In the current local Python flow, the persisted state is not committed all at once before all events.

The live ordering is:

1. promotion record committed
2. head update committed
3. operation-graph row committed
4. promoted proposal statuses committed and `PROPOSAL_PROMOTED` events published
5. conflict losers superseded + conflict-set resolved, then `CONFLICT_RESOLVED` published
6. trust metric updates committed
7. `HEAD_UPDATED` published last

Because `EventBus.publish(...)` dispatches synchronously to subscribers, event consumers can observe the database in these intermediate states.

Under the proposed external path, Rust commits:

- head changes
- promoted statuses
- superseded losers
- conflict-set resolution
- trust updates
- telemetry
- operation graph

all inside one transaction **before** Python publishes the first event.

That means when external Python publishes `PROPOSAL_PROMOTED`, subscribers would already observe:

- losers superseded
- conflict sets resolved
- trust metrics updated

which is **not** what the current local path exposes at that event boundary.

### Why this matters

This is an observable behavioral difference.

Since `EventBus.publish(...)` calls subscribers immediately, any subscriber that inspects DB state during:

- `PROPOSAL_PROMOTED`
- `CONFLICT_RESOLVED`
- `HEAD_UPDATED`

can distinguish the two implementations.

So despite matching tables, payloads, timestamps, and event order, the plan still does not achieve strict 1:1 runtime semantics.

### What would make this green-lightable

There are only two honest options:

1. **Narrow the goal** and explicitly accept this as behaviorally close but not perfectly identical, because the external commit primitive intentionally collapses several local commit boundaries into one atomic transaction; or
2. Redesign the external path so the commit boundaries mirror the Python flow instead of using one monolithic transaction.

Option 1 is likely the pragmatic path.

### Updated bottom line

This plan is:

- **approved in principle**;
- **not approved as strict 1:1 parity**;
- **still observably different at event-time DB state boundaries**.

## Final architectural framing verdict

After the final review loop, the decisive conclusion is:

- the external `commit_promotion` design is **implementation-approved in principle**;
- the remaining issue is **claim scope**, not a code-level blocker.

### Final Copilot verdict

The external path should **not** be described as:

- strict semantic parity;
- absolute 100% 1:1 behavioral parity.

It **should** be described as:

- behavioral parity with intentionally stronger atomicity at the event-publication boundary.

### Why this is the correct framing

The current local Python engine exposes intermediate committed states between event publications.

The external Rust path intentionally collapses those staged commit boundaries into one stronger atomic transaction and publishes the equivalent events only after the database has reached a fully settled state.

That is:

- observably different for subscribers that query DB state during event callbacks;
- but generally better transactional behavior.

### Subscriber audit result

A quick repository audit found only one in-repo event subscriber path:

- `packages/aether/api/websocket.py`

That subscriber:

- subscribes to the in-process `EventBus`;
- forwards event payloads over WebSocket;
- does **not** query the database during event callbacks.

So there is currently no in-repo subscriber that appears to depend on intermediate-state observation at event boundaries.

### Final practical outcome

That means the external `commit_promotion` plan is:

- **green-lightable for implementation**
- **provided the documentation and plan language are updated to describe the stronger atomicity honestly**

### Required wording change

Recommended wording:

> behavioral parity with intentionally stronger atomicity at the event-publication boundary. Under the external path, subscribers observing `PROPOSAL_PROMOTED` may see fully settled state, including conflict resolution and trust updates, where the local Python path exposes intermediate states between event boundaries. This is a documented property of the external path, not a regression.

### Final bottom line

**Implementation approved in principle.**

**Do not claim exact runtime parity at event-time DB boundaries.**

## Post-implementation review outcome

Gemini then reported the implementation complete and claimed the harness was ready to resume soak testing.

### Adversarial verdict

**Not approved for soak yet.**

The implementation contains runtime blockers on the actual external path even though:

- `cargo check` succeeds;
- the current Python harness tests pass.

### Confirmed blocker 1

#### `ExternalPromotionEngine` is referenced but not implemented

`ContinuousSwarmHarness._build_runtime_services(...)` now branches to:

- `ExternalPromotionEngine(...)`

when `target_mode == "external"`.

But there is no `ExternalPromotionEngine` definition in `swarm_harness.py` and no import providing it from elsewhere.

Direct runtime confirmation:

- module import succeeds;
- external harness service construction fails with:
  - `NameError: name 'ExternalPromotionEngine' is not defined`

That means the external harness path cannot run.

### Confirmed blocker 2

#### `commit_promotion` inserts into `aether_heads` with columns that do not exist in the migrated schema

The new Rust handler currently executes:

- `INSERT INTO aether_heads (..., authentic_hash, parent_head_id, ...)`

But the actual Rust migration defines `aether_heads` with only:

- `id`
- `repo_id`
- `sequence`
- `snapshot_id`
- `promotion_id`
- `created_at`
- `is_canonical`

There is no:

- `authentic_hash`
- `parent_head_id`

This is a runtime SQL failure that `cargo check` will not catch.

Direct SQLite reproduction confirms:

- `OperationalError: table aether_heads has no column named authentic_hash`

### Confirmed blocker 3

#### `commit_promotion` inserts `aether_operation_graph_nodes` before the referenced promotion row exists

The new Rust handler inserts `aether_operation_graph_nodes` before inserting the corresponding `aether_promotions` row, while the schema declares:

- `promotion_id TEXT NOT NULL REFERENCES aether_promotions(id)`

The Rust server enables:

- `foreign_keys(true)`

on the SQLite connection.

So this ordering causes a runtime foreign-key violation on the external path.

Direct SQLite reproduction confirms:

- `IntegrityError: FOREIGN KEY constraint failed`

### Coverage note

The currently passing Python test update only moves harness API-route tests to:

- `target_mode="testclient"`

There is no test coverage in the current suite for:

- `target_mode="external"`
- `ExternalPromotionEngine`
- the Rust `commit_promotion` happy path

So the green test run does not validate the new external implementation.

### Minor hygiene note

There is also an untracked file in the worktree:

- `patch_main.rs`

which appears to be a leftover artifact and should not be mistaken for committed implementation.

### Bottom line

The design review loop is complete, but this implementation is **not yet ready** for the true external soak.

The next step is not soak:

1. implement or correctly import `ExternalPromotionEngine`;
2. fix the `aether_heads` insert to match the real schema;
3. reorder `commit_promotion` writes so foreign keys remain valid;
4. add/adjust coverage for real external-mode execution.
