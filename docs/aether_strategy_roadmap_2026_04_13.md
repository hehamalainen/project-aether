# Aether Strategy Roadmap

## Strategic thesis

Aether should not remain only a control plane forever, and it should not spend the near term chasing a full semantic-storage rewrite.

The practical path is layered:

- harden the control plane first
- make agent-work objects canonical next
- run bounded substrate experiments after that
- keep Git and GitHub at the edge until deeper substrate replacement is earned by evidence

## Phase 1 - Hardened control plane

Primary goal:
- clean runtime proof, repo safety, and operator hygiene

Required outcomes:
- secure remote backup
- reviewable commit structure
- first-class readiness documentation
- repeatable long-run baselines
- throughput-profile tuning after the completed `24h` endurance run

## Phase 2 - Canonical agent-work objects

First canonical objects should be:
- `session`
- `decision_record`
- `validation_record`
- snapshot/promotion lineage links

Near-term rules:
- keep schemas compact and queryable
- paths required, symbols optional
- full prompts and full tool transcripts remain optional cold-path artifacts
- shape fields so they can map cleanly to `PROV-AGENT` later

## Phase 3 - Semantic mutation path

Promote semantic mutation into a first-class ingress path where evidence justifies it.

Sequencing:
- merge layer first
- gate layer consumes semantic evidence later
- storage-layer rewrite remains deferred research

## Phase 4 - Bounded substrate experiments

Run explicit falsification experiments rather than thesis-driven migrations.

First experiment candidates:
- `jj` as agent-facing repo frontend for one Aether-managed repo
- merge-layer semantic merge comparison on one representative workload

Success standard:
- materially better operator experience or useful throughput
- no unacceptable interop or recovery regression

## Phase 5 - Edge compatibility remains

Even as deeper truth shifts toward Aether-native objects:
- Git remains export and compatibility surface
- GitHub remains review, identity, and distribution surface

This is a long transition, not an overnight replacement.
