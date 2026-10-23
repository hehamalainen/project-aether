# MX Swarm ↔ Aether Proof Ladder And Slice Matrix

Date: 2026-03-15

## Milestone

The first real model-generated MX Swarm artifact has landed in Aether and been promoted canonically.

Sink-side confirmation from Aether:
- repo: `repo_905b0c53d610`
- proposal: `prop_c7f8b901fdb1`
- proposal status: `PROMOTED`
- provenance persisted:
  - `source = mx_swarm`
  - `source_run_id = mx-aether-local-mistral-proof-20260315t2115`
  - `source_task_id = 584d0062`
  - `source_agent_id = worker-0`
  - `source_attempt_id = attempt-1`
  - `intent_type = feature`

Primary MX proof artifacts:
- `<MX_SWARM_ROOT>/reports/aether_live_proof/20260315T190825Z/aether_live_proof.md`
- `<MX_SWARM_ROOT>/reports/aether_live_proof/20260315T190825Z/aether_live_proof.json`
- `<MX_SWARM_ROOT>/reports/aether_live_proof/20260315T190825Z/delivery_proof/delivery_proof.md`

This closes the core boundary question:
- MX can generate a real artifact
- MX can submit it through the real Aether adapter
- Aether can persist and promote it
- provenance survives end to end

## Proof Ladder Status

Frozen MX-side gates:
- `mx aether-adapter-proof`
- `mx aether-worker-proof`
- `mx aether-live-proof`

Current proof ladder state:
1. deterministic adapter proof: green
2. canned worker proof: green
3. local model-backed live proof (`jsonpeek`, `ollama/mistral:7b`): green
4. local repeatability on `jsonpeek`: `3/3` green

Repeatability artifact:
- `<MX_SWARM_ROOT>/reports/AETHER_LOCAL_MISTRAL_REPEATABILITY_2026-03-15.md`

## Aether-Side Changes That Must Not Be Lost

Current uncommitted Aether-side MX intake scope:
- `apps/aether_server/main.py`
- `packages/aether/api/routes.py`
- `packages/aether/models/entities.py`
- `packages/aether/services/proposal_service.py`
- `packages/aether/services/swarm_harness.py`
- `tests/aether/test_services.py`
- `tests/aether/test_system_physics_api.py`

What this scope does:
- adds source-tagged intake metadata to proposals
- persists MX provenance tuple on Aether proposal rows
- exposes source metadata via proposal APIs
- adds source-aware physics reporting
- tags harness traffic as `synthetic_harness`
- backfills proposal source columns for existing SQLite databases

Recommended eventual commit scope:
- commit title: `feat: add source-tagged MX intake provenance`

## Evidence Quality Notes

Two proof-pack cleanup items were identified:
1. canonical proposal URL in MX artifacts should use `/v1/repos/...`
2. windowed `/v1/system/physics` output is global telemetry, not run-scoped evidence

Those are evidence-pack quality issues, not correctness issues.

## Current First-Principles Conclusion

Closed:
- integration correctness
- provenance persistence
- Aether settlement correctness for the narrow slice

Open:
- latency/economics of the local inference lane
- broader slice coverage
- later: concurrency and mixed-source behavior

Gemini is not needed for baseline correctness.
Gemini is now optional for:
- speed
- capability margin
- broader slice diversity

## Broader Slice Coverage Matrix

Purpose:
- move from one narrow green slice to a small, disciplined coverage envelope
- avoid random proof selection
- keep concurrency low while expanding task diversity

### Phase A: Narrow local coverage

Run each slice with:
- one worker
- one repo per run
- same Aether sink
- same proof harness
- no concurrency expansion
- target model: local `ollama/mistral:7b`

Suggested first matrix:

| Slice | Intent | Expected artifact shape | Why it matters |
|------|--------|-------------------------|----------------|
| `jsonpeek` | `feature` | new small CLI file | baseline anchor already green |
| `bugfix_patch` | `bugfix` | modify one existing function | exercises edit-in-place instead of new file |
| `test_addition` | `qa` | add or update one test file | verifies test-oriented artifact landing |
| `ops_script` | `ops` | small script/config helper | verifies non-app code path |
| `small_refactor` | `replan` or `feature` | multi-hunk but single-file change | increases structural complexity slightly |

Acceptance per slice:
- reaches `submit_started`
- proposal lands in Aether
- final status observable
- post-landing verification passes
- provenance tuple intact

Success rule for Phase A:
- at least `4/5` green
- no integration regressions
- failures classified before retry

### Phase B: Repeatability on the expanded set

For any slice that passes once:
- rerun `3x`
- capture:
  - pass/fail
  - model-to-first-token latency
  - submit-to-terminal latency
  - post-landing verification result

Stop/go:
- if a slice is below `2/3`, do not widen further
- classify whether failures are:
  - model lane
  - local verification
  - Aether settlement

### Phase C: Controlled concurrency

Only after Phase A/B are stable:
- run `2` workers on `2` different slices
- then `3` workers on `3` different slices

Questions this phase should answer:
- does provenance stay clean under concurrent MX traffic
- does Aether source-aware visibility remain readable
- do local worker lanes interfere with each other more than Aether does

Do not jump to high concurrency before low-concurrency slice diversity is stable.

## Stop/Go Rules

Stop and debug immediately if:
- `submit_started` is not reached
- proposal lands but provenance tuple is missing/malformed
- post-landing verification fails for a previously green slice
- Aether returns a successful submission but proposal is not queryable

Proceed if:
- failure is clearly model-lane latency only
- Aether sink remains correct
- canned worker proof and adapter proof remain green

## Next Recommended Actions

1. Commit the Aether-side source-tagged intake scope.
2. Keep MX regression gates frozen.
3. Run the broader slice coverage matrix Phase A.
4. Reassess whether Gemini is needed based on:
   - slice diversity failure rate
   - local latency cost
   - local model quality limits

## Aether LoC Snapshot

Tracked code files (`*.py`, `*.ts`, `*.tsx`, `*.js`, `*.jsx`, `*.sh`) at this point:
- `27,245`

This snapshot was computed from the current Aether worktree on 2026-03-15.
