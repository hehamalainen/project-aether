# Deep Think Review Prompt Pack (Aether)

Prepared: 2026-02-13  
Use with dossier: `docs/deepthink_peer_review_dossier.md`

---

## How to use this pack

1. Run prompts in order: `P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8`.
2. Keep the model in strict technical mode (no marketing language).
3. Require confidence scores and explicit assumptions in every answer.
4. Ask for concrete pass/fail criteria, not general advice.

---

## P0 — Session Initialization (Context Lock)

### Exact prompt

```text
You are conducting an external technical peer review for Project Aether.

Primary source of truth:
- docs/deepthink_peer_review_dossier.md

Review mode constraints:
- Be brutally honest, first-principles, and falsification-oriented.
- Prefer explicit invariants, failure modes, and measurable criteria.
- No generic encouragement.
- If evidence is insufficient, explicitly say "insufficient evidence" and specify what is missing.
- Always separate: (a) directly evidenced claims, (b) inferred claims, (c) speculative claims.

Output exactly in this structure:
1) Context understood (5 bullets)
2) Claimed hypothesis in one sentence
3) Top 5 review axes you will use
4) Immediate missing artifacts you need before final confidence
5) Review plan (ordered steps)
```

### Expected response structure

- 5 bullets on context
- 1 sentence hypothesis
- numbered list of 5 axes
- missing artifacts list
- ordered review plan

---

## P1 — Architecture Verdict (Current State)

### Exact prompt

```text
Using only the dossier and its referenced internal evidence, evaluate whether Aether's current architecture is directionally correct for an agent-native Git replacement.

Required lens:
- Safety
- Liveness
- Scalability
- Deterministic replay/provenance
- Conflict precision

Output exactly in this structure:
1) Executive verdict: {Directionally Correct | Partially Correct | Incorrect}
2) Confidence score (0-100) + justification
3) Proven strengths (table: claim, direct evidence, confidence)
4) Critical weaknesses (table: weakness, failure mechanism, impact, confidence)
5) Non-negotiable blockers before claiming "Git replacement"
6) Minimum viable proof package (what evidence would change verdict)
```

### Expected response structure

- One categorical verdict
- 0-100 confidence
- 2 tables (strengths/weaknesses)
- blocker list
- evidence package list

---

## P2 — Formal Safety/Liveness Audit

### Exact prompt

```text
Construct a formal-ish model of Aether's current protocol from the dossier and code references:
- proposal ingest (INGESTED)
- materialization (INGESTED -> PENDING)
- evaluation/promotion
- operation graph persistence

Then produce safety and liveness analysis.

Output exactly in this structure:
1) State machine (states, transitions, guards)
2) Safety invariants (>=8) with status {likely holds | uncertain | likely violated}
3) Liveness properties (>=5) with status {likely holds | uncertain | likely violated}
4) Counterexample traces (>=3) with step-by-step event sequence
5) Instrumentation gaps preventing proof
6) Minimal code/protocol changes to close each gap
```

### Expected response structure

- state machine bullets/table
- 8+ safety invariants with status
- 5+ liveness properties with status
- 3+ counterexample traces
- instrumentation gaps + fixes mapping

---

## P3 — Queueing & Throughput Economics (1000-Agent Regime)

### Exact prompt

```text
Use the dossier metrics and artifact values to build a queueing-capacity analysis for Aether.

Goal:
- Explain why enqueue->ack is fast but ack->promote tail can explode.
- Quantify likely bottlenecks from phase timing (select/evaluate/snapshot/gates/commit).
- Determine feasible operating envelopes for 100, 500, and 1000 agents.

Output exactly in this structure:
1) Input metrics extracted (table)
2) Derived service-rate model (show formulas)
3) Bottleneck ranking (ordered with quantitative support)
4) Envelope table: agent_count, expected submit_tps, required promote_tps, backlog trend
5) Recommended control policy (watermarks, throttling, batching) with numeric defaults
6) Falsification experiment to disprove your model
```

### Expected response structure

- extracted metrics table
- formulas + derived rates
- ranked bottlenecks
- envelope table for 100/500/1000
- numeric control policy
- one disprovable experiment

---

## P4 — Semantic Conflict Model Review

### Exact prompt

```text
Review Aether's semantic conflict strategy:
- Python semantic IDs
- non-Python symbol adapters (tree-sitter + fallback)
- __file__ fallback semantics

Evaluate both correctness and scalability risk.

Output exactly in this structure:
1) What is sound today (with evidence)
2) False-positive and false-negative risk matrix
3) Adversarial scenarios (>=6) that can break semantic conflict assumptions
4) Determinism audit of fallback behavior
5) Regression test set required before production
6) Design recommendation: keep current model vs redesign (with rationale)
```

### Expected response structure

- soundness summary
- risk matrix (FP/FN)
- 6+ adversarial scenarios
- determinism audit
- test set checklist
- final design recommendation

---

## P5 — Operation Graph and Replay Protocol Audit

### Exact prompt

```text
Audit whether Aether's operation graph + provenance evidence is sufficient for deterministic replay and forensic auditability.

Focus on:
- node hash and payload hash semantics
- parent linking and causal ordering
- replay_from_hash / replay_to_hash
- gate vector reproducibility
- tamper and omission risk

Output exactly in this structure:
1) Protocol strengths
2) Replay trust boundaries (what is trusted vs verified)
3) Attack/failure matrix (>=8 rows)
4) Required cryptographic/protocol hardening steps
5) Verification API sufficiency (yes/no + why)
6) "Can this support distributed multi-writer promotion?" verdict with conditions
```

### Expected response structure

- strengths list
- trust-boundary map
- 8+ row attack/failure matrix
- hardening steps
- API sufficiency verdict
- distributed-multiwriter verdict

---

## P6 — Distributed-Scale Blueprint (Beyond Single Host)

### Exact prompt

```text
Design the minimum credible distributed architecture that preserves Aether semantics at high concurrency.

Must include:
- shard strategy
- ordering/consensus boundaries
- event bus and state stores
- worker topology
- failure handling and replay
- migration path from current single-node implementation

Output exactly in this structure:
1) Proposed target architecture (components + responsibilities)
2) Consistency model by component
3) Failure mode table (node loss, partition, duplicate delivery, clock skew)
4) Incremental migration plan in 4 stages
5) Cost/complexity tradeoff by stage
6) Kill criteria (when to stop and pivot)
```

### Expected response structure

- component architecture
- per-component consistency model
- failure mode table
- 4-stage migration plan
- cost/complexity table
- pivot criteria

---

## P7 — Red-Team Review vs "Git Replacement" Claim

### Exact prompt

```text
Act as a hostile but fair technical red-team reviewer.

Challenge the claim: "Aether can become a true Git replacement for high-tempo autonomous coding swarms."

Output exactly in this structure:
1) Strongest argument FOR the claim
2) Strongest argument AGAINST the claim
3) Top 10 ways this effort could fail
4) Top 5 indicators it is actually working
5) 12-month probability estimate (%):
   - succeeds as niche layer
   - succeeds as core workflow engine
   - displaces significant Git workflows
6) What single experiment would most change your probability estimates
```

### Expected response structure

- for/against arguments
- 10 failure modes
- 5 success indicators
- 3 probability estimates
- one high-information experiment

---

## P8 — Actionable Program Plan (Investor Week + Engineering Reality)

### Exact prompt

```text
Create an execution program from current Aether state with two horizons:
A) next 10 days (investor demo support)
B) next 12 weeks (technical de-risking)

Constraints:
- Keep dashboard/demo stability high.
- Continue deep scalability work in background.
- Every milestone must have explicit quality gates and artifact outputs.

Output exactly in this structure:
1) 10-day plan (daily milestones, owner roles, gates)
2) 12-week plan (weekly milestones, dependencies, gates)
3) Risk register (probability, impact, mitigation, trigger)
4) KPI tree (north-star + leading indicators)
5) Decision checkpoints with go/no-go criteria
6) Final recommendation: pursue aggressively / pursue cautiously / narrow scope
```

### Expected response structure

- day-by-day 10-day schedule
- week-by-week 12-week plan
- risk register table
- KPI tree
- go/no-go checkpoints
- one final recommendation

---

## Optional Add-on: Machine-Readable Output Contract

Use this after any prompt if you want strict parsable outputs.

### Exact prompt

```text
Reformat your previous answer as strict JSON with top-level keys:
- verdict
- confidence
- evidence_claims
- inferred_claims
- speculative_claims
- blockers
- actions
- metrics
- experiments

Rules:
- No markdown.
- Every claim must include: statement, support, confidence_0_to_1.
- Every action must include: owner_role, effort_days, expected_impact, success_metric.
```

### Expected response structure

- valid JSON only
- required top-level keys present
- per-claim confidence and support fields

---

## Notes for your handover

- Start with `P0` and `P1` for the first external review cycle.
- Use `P3` and `P6` for architecture/scalability deep dives.
- Use `P7` before investor narrative finalization to stress-test claims.
- Use `P8` to convert review into execution governance.

