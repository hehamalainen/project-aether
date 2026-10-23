# MX Swarm Intake Preparation - 2026-03-13

## Objective

Prepare Aether to receive real code from MX Swarm instead of only synthetic harness traffic.

This is not a merge of the two products. MX Swarm remains the agent-runtime layer and Aether remains the repository convergence/control layer.

## Why this is the next meaningful step

Current Aether evidence is now strong on synthetic swarm pressure:
- reproducer passes;
- 25m strict passes;
- 60m strict passes;
- night suite passes;
- 4h soak passes;
- 10h soak passes.

The next useful truth source is real swarm-generated code:
- mixed file sizes;
- non-uniform task shapes;
- bursty multi-file edits;
- imperfect changed-path hints;
- real retry / backoff behavior from an external swarm runtime.

## Boundary to preserve

Keep the products separate:
- **MX Swarm**: long-running agent runtime, scheduling, execution, task orchestration
- **Aether**: canonicalization, promotion, deterministic replay, queue control, evidence

Integration should happen through a stable protocol boundary, not codebase fusion.

## Immediate preparation tasks

### 1. Define the intake contract

Minimum fields MX Swarm should send consistently:
- `agent_id`
- `task_id`
- `change_type`
- `change_content`
- `affected_paths`
- `confidence_score`
- optional `dependencies`
- optional `intent_metadata`

Needed next:
- explicit backoff / retry behavior when Aether returns `429`
- explicit resync behavior after repeated throttling
- stable correlation id for replay and postmortem analysis

### 2. Add intake classification in Aether

Add a lightweight source label for proposal origin:
- `source = synthetic_harness | mx_swarm | manual | other`

This should be visible in:
- proposal metadata
- promotion records
- physics / throughput diagnostics

Reason:
- once MX Swarm traffic arrives, we need to separate synthetic and real-runtime behavior immediately.

### 3. Create one controlled MX Swarm landing profile

Do not start with full open flood.

Start with:
- one repository
- one bounded file set
- one agreed change shape
- one stable backoff policy
- one fixed max concurrent submit budget

Goal:
- validate the protocol and observability first
- not maximize chaos on day one

### 4. Add intake observability for real-code landings

Before real MX Swarm traffic, make sure Aether can answer:
- how many proposals came from MX Swarm
- accept / reject / supersede / quarantine split by source
- changed-path quality by source
- throttle rate by source
- promotion success rate by source

### 5. Run staged integration phases

Phase A:
- replay-like low-volume MX Swarm traffic into a single repo
- no overnight, just short controlled sessions

Phase B:
- 25m mixed run:
  - synthetic harness + MX Swarm source-tagged proposals

Phase C:
- 60m mixed run

Phase D:
- overnight mixed soak

Do not jump directly to full overnight real-code pressure.

## Multi-day validation plan

After clean-baseline repeatability is done, the next duration ladder should be:

1. 12h soak on frozen baseline
2. 24h soak on frozen baseline
3. 12h mixed-source soak with MX Swarm input enabled
4. 24h mixed-source soak only after 12h mixed-source is stable

## Stop conditions

Stop MX Swarm rollout immediately if:
- snapshot/materialization quarantines reappear
- throttle behavior becomes source-specific and pathological
- changed-path metadata from MX Swarm is too noisy to support promotion reliably
- replay/provenance cannot correlate swarm task identity to promoted change identity

## Recommendation

The next architectural work should not be a redesign.

It should be:
1. finish clean-baseline repeatability;
2. prepare source-tagged MX Swarm intake;
3. run a controlled low-volume landing profile;
4. scale that gradually into longer mixed-source runs.
