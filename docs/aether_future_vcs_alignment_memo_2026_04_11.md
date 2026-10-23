# Aether vs. Future VCS Requirements

Date: 2026-04-11
Purpose: map the emerging "VCS after Git" requirement set against Aether's current architecture, current proof status, and post-v1 roadmap
Input: synthesis from `docs/inputs/findings.md`

---

## 1. Executive Summary

The external synthesis is directionally consistent with Aether.

It does not surface a showstopper. It sharpens the roadmap.

The strongest shared conclusion is this:

- Git is unlikely to remain the native control object for high-concurrency agent software delivery.
- The post-Git system will need semantic coordination, durable provenance, and machine-speed promotion logic.
- Aether is already operating in that design space, even though it does not yet implement the full end-state described by the research.

The clean way to say this is:

- **Aether is already a valid control-plane implementation for the post-Git transition.**
- **Aether is not yet the final fully semantic post-Git storage substrate.**

That distinction matters.

---

## 2. The Core Future-VCS Requirements

The research synthesis identifies seven durable requirements for a serious post-Git system:

1. commutative merge at semantic granularity
2. session as a first-class object
3. provenance that survives refactors
4. speculative branching as a cheap primitive
5. concurrent writes without serialization
6. queryable history
7. Git as compatibility target, not source of truth

These are the right axes for evaluating Aether.

---

## 3. Alignment Table

| Requirement | Aether now | Gap | Likely stage |
|---|---|---|---|
| Semantic, non-line-centric coordination | Partial | Aether reasons about proposals and promotion, but does not yet provide a full semantic patch-theory storage model | post-v1 / research-heavy |
| Session as first-class object | Not yet | Proposals have provenance, but not full session records with reasoning, alternatives, and tool lineage | v1.5 / v2 |
| Durable provenance / "why" | Partial to strong | Current source tuple is real, but richer decision records are still missing | near-term |
| Cheap speculative branching | Partial | Proposals and snapshots provide some of this shape, but not yet as first-class agent session branches | v1.5 |
| Concurrent writes without repo lock | Strong | This is already one of Aether's core validated claims under synthetic swarm conditions | now |
| Queryable history | Partial | There is lineage and telemetry, but not yet the richer query surface implied by session/intent history | v1.5 / v2 |
| Git as export target, not truth | Strong in philosophy, partial in implementation | Aether already behaves like a canonical substrate above Git-era assumptions, but export/interoperability needs clearer packaging | now to v1.5 |

---

## 4. What Aether Already Gets Right

### 4.1 Machine-speed control plane

The report's strongest claim is that human-era Git workflows break under agent fan-out because the control model is wrong.

This is exactly the problem Aether is built to solve.

Aether already implements:

- proposal intake separate from canonical head
- conflict grouping and promotion selection
- queue/backpressure control
- deterministic snapshot lineage
- gate-verified promotion
- runtime telemetry over throughput, latency, rho, backlog, and quarantines

This is not "Git with AI buttons." It is already a different control-plane object.

### 4.2 Provenance as real system state

The report's "Decision Shadow" section is important.

Aether already moved in the right direction by persisting source-aware provenance:

- `source`
- `source_run_id`
- `source_task_id`
- `source_agent_id`
- `source_attempt_id`
- `intent_type`

That is not yet a full decision record system, but it is the correct foundation.

### 4.3 Concurrent mutator handling

The report highlights Git index / pack / metadata contention and the need to avoid global serialization.

That aligns directly with Aether's validated value:

- the system already survives strict synthetic `1000`-agent intake/promotion pressure on the current baseline
- the live concern is service-rate headroom, not Git-era global repository mutation safety

That is strong evidence that Aether is solving a real next-generation systems problem, not a cosmetic workflow problem.

### 4.4 Canonicalization over branch ergonomics

The report argues that post-Git systems will treat Git as one compatibility target, not the main truth layer.

Aether already fits that worldview:

- proposals are the main integration object
- promotion is the canonicalizing primitive
- canonical head is a controlled result, not just "whatever merged"

This is strategically aligned.

---

## 5. Where Aether Is Not Yet There

### 5.1 Patch-theory / commutative semantic merge

The report is strongest where it argues that post-Git systems need a more powerful merge model than line diffs.

Aether does **not** yet implement:

- a patch-theoretic backend
- universal commutative merge semantics
- token-identity or AST-identity storage primitives as the main data model

This means Aether should not currently claim to be the mathematically final answer to semantic concurrency.

What it can claim:

- it is the control plane that can host and govern richer merge/storage models later

### 5.2 Session as first-class object

The report is also right that the natural unit of agent work is a session, not a commit.

Aether today has:

- proposals
- promotions
- snapshots
- operation lineage
- provenance tuple

But it does not yet have:

- session records with full reasoning trace
- alternatives considered
- tool invocation lineage
- explicit rejected-attempt history as first-class stored objects

This is one of the clearest future roadmap items.

### 5.3 Queryable intent history

Aether has observability and lineage, but not yet a mature query surface for:

- "why was this changed?"
- "which alternative paths were rejected?"
- "which sessions touched this semantic entity across time?"

That gap is not fatal for v1.
It is important for the longer-term moat.

---

## 6. What This Means for Product Positioning

### 6.1 Safe positioning

Safe statement:

- Aether is a working agent-native repository control plane aligned with the emerging requirements of a post-Git software ecosystem.

Also safe:

- Aether is already solving the canonicalization, promotion, provenance, and machine-speed coordination problem that Git-era workflows do not solve well.

### 6.2 Unsafe positioning

Do **not** say:

- Aether already fully replaces Git
- Aether already provides mathematically complete semantic commutative merge
- Aether already stores the entire agent reasoning session as first-class truth

Those are roadmap claims, not current-state claims.

### 6.3 Strategic positioning

Best framing:

- Aether is the control-plane layer beneath the future VCS transition.
- It is building the runtime-valid substrate that a truly post-Git agent system will require.

That is stronger than saying:

- "we are another Git competitor"

and more honest than saying:

- "we have already finished the post-Git architecture."

---

## 7. Recommended Roadmap Implications

### 7.1 Near-term: deepen provenance

Next step after current baseline hardening should be to deepen the provenance model beyond the source tuple.

Likely additions:

- session ID
- decision record ID
- parent session / attempt lineage
- explicit validation evidence references
- optional structured rationale payload

This is the lowest-risk way to attack the "Decision Shadow" problem.

### 7.2 Mid-term: first-class session objects

Introduce a first-class session layer above proposals:

- session start
- task objective
- attempts
- tool traces
- resulting proposals
- terminal outcome

This would make Aether much closer to the architecture described in the research without requiring an immediate storage-layer rewrite.

### 7.3 Longer-term: semantic change model

The deepest future work is not more queue tuning. It is evaluating where semantic change representation belongs:

- storage layer
- diff layer
- merge layer

This remains open research. Aether should not guess casually here.

The correct approach is:

- keep the control plane strong now
- design the semantic layer deliberately later

---

## 8. Recommended Language for Reviews and Investors

Use this:

- "Aether is already a credible control-plane implementation for a post-Git, agent-native development stack."

Use this too:

- "The system has validated canonicalization, promotion, provenance, and high-concurrency pressure handling; the remaining roadmap is richer session semantics and deeper semantic merge substrates."

Avoid this:

- "Aether is already the final VCS after Git."

---

## 9. Bottom Line

The autoresearch synthesis strengthens Aether's strategic case.

It says, in effect:

- the world is moving toward exactly the problem Aether is built for
- several projects are independently converging on pieces of that same future
- no single system has fully solved it yet

That is a good position for Aether.

Aether already has a real runtime control plane.
What it needs next is not a reinvention of its thesis.
It needs:

- longer-run reliability proof
- secure repository backup / push hygiene
- richer provenance and session semantics
- disciplined future work on semantic merge/storage

That is a coherent roadmap, not a contradiction.
