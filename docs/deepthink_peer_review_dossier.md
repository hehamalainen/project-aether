# Aether Technical Dossier for Deep Think Peer Review

## 0) Document Metadata

- Project: Project Aether
- Repository root: `<REPO_ROOT>`
- Prepared for: External technical peer review (Google Deep Think)
- Prepared on: 2026-02-13
- Scope: Architecture, execution model, empirical results, falsifiable hypotheses, and corrective roadmap for an agent-native VCS that can exceed Git-era merge workflows under high parallelism.

---

## 1) Problem Statement and First-Principles Hypothesis

### 1.1 Problem (as implemented context, not marketing)

Aether targets a regime where many autonomous coding agents submit code changes concurrently. In this regime, line-diff merge workflows create increasing integration friction because:

1. Conflict units are coarse (line/file overlap rather than semantic intent units).
2. Integration is human-paced and pull-request gated.
3. Provenance is commit-centric, not operation/replay-centric.
4. Promotion pressure (intake rate vs integration capacity) can cause unbounded backlog.

### 1.2 Core hypothesis

A system built around:

- append-only proposal ingest,
- semantic conflict grouping,
- deterministic snapshot/replay lineage,
- evidence-gated probabilistic promotion,
- and bounded asynchronous workers,

can remain coherent under high agent concurrency and can outperform Git-centered workflows for autonomous swarm coding.

### 1.3 Falsifiable claims

The hypothesis above is only valid if these hold under controlled load:

1. **Safety**: Canonical head remains buildable and promotion is auditable/replayable.
2. **Liveness**: Proposal intake and promotion continue without deadlocks/hangs.
3. **Scalability**: Throughput and lag stay bounded with increasing agents.
4. **Semantic precision**: False conflicts decrease relative to file/line conflict detection.
5. **Determinism**: Promotion provenance and replay hashes remain stable.

If any claim fails in sustained 1000-agent conditions with strict gates, the hypothesis is only partially validated.

---

## 2) Current Architecture State (Code-Backed)

This section maps theory to the current codebase.

## 2.1 Data model primitives

### Proposals with explicit `INGESTED` stage

`ProposalStatus` now includes a decoupled intake state:

```python
class ProposalStatus(str, Enum):
    INGESTED = "INGESTED"
    PENDING = "PENDING"
    EVALUATING = "EVALUATING"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    SUPERSEDED = "SUPERSEDED"
```

Source: `packages/aether/models/entities.py:22`

### Explicit operation graph persistence

Operation lineage is no longer evidence-only metadata; there is a durable table:

```python
class OperationGraphNode(SQLModel, table=True):
    __tablename__ = "aether_operation_graph_nodes"

    id: str = SQLField(default_factory=lambda: generate_id("opn"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")
    promotion_id: str = SQLField(index=True, foreign_key="aether_promotions.id")
    parent_node_id: Optional[str] = SQLField(default=None, index=True)
    parent_head_id: Optional[str] = SQLField(default=None, index=True)
    parent_head_sequence: int = SQLField(default=0, index=True)
    to_head_id: Optional[str] = SQLField(default=None, index=True)
    to_head_sequence: int = SQLField(default=0, index=True)
    snapshot_id: str = SQLField(index=True, foreign_key="aether_snapshots.id")
    operation_type: str = SQLField(default="promotion")
    node_hash: str = SQLField(index=True)
    payload_sha256: str = SQLField(index=True)
    parent_ref: Optional[str] = SQLField(default=None)
    replay_from_hash: Optional[str] = SQLField(default=None)
    replay_to_hash: Optional[str] = SQLField(default=None)
    replay_gate_vector: str = SQLField(default="")
    included_proposals: str = SQLField(default="[]")
    excluded_proposals: str = SQLField(default="[]")
```

Source: `packages/aether/models/entities.py:237`

## 2.2 Ingestion and conflict materialization are decoupled

The intake path can ack without running full conflict scans:

```python
proposal = self._create_proposal_row(
    repo_id,
    proposal_data,
    initial_status=ProposalStatus.INGESTED,
)
self.session.add(proposal)
self.session.commit()
self.session.refresh(proposal)
...
return ProposalResponse(
    proposal_id=proposal.id,
    status=proposal.status,
    conflict_set_id=None,
    message="Proposal ingested; async conflict detection queued",
)
```

Source: `packages/aether/services/proposal_service.py:403`

Materialization is separately executed from queue:

```python
statement = (
    select(Proposal)
    .where(
        Proposal.repo_id == repo_id,
        Proposal.status == ProposalStatus.INGESTED,
    )
    .order_by(Proposal.submitted_at.asc())
    .limit(limit)
)
queued = list(self.session.exec(statement))
...
proposal.status = ProposalStatus.PENDING
self.session.add(proposal)
...
self.session.commit()
```

Source: `packages/aether/services/proposal_service.py:458`

## 2.3 Snapshot path moved toward true delta semantics

The snapshot subsystem now supports manifest-based delta snapshots:

```python
SNAPSHOT_MANIFEST_FILENAME = ".aether_snapshot_manifest.json"

...
if manifest and manifest.get("mode") == "delta":
    base_snapshot_id = manifest.get("base_snapshot_id")
    changed_paths = manifest.get("changed_paths", [])
    deleted_paths = manifest.get("deleted_paths", [])
```

Source: `packages/aether/services/snapshot_service.py:41`

Delta write path persists changed files plus base pointer:

```python
self._write_manifest(
    snapshot_path,
    {
        "version": 1,
        "mode": "delta",
        "base_snapshot_id": base_snapshot_id,
        "changed_paths": changed_manifest_paths,
        "deleted_paths": deleted_paths,
    },
)
```

Source: `packages/aether/services/snapshot_service.py:237`

Gate execution now materializes a temporary full workspace off hot snapshot path:

```python
gate_workspace = self.snapshot_service.materialize_gate_workspace(snapshot.id)
try:
    gate_results = await self.gate_runner.run_all_gates(..., gate_workspace, ...)
finally:
    if gate_workspace:
        self.snapshot_service.cleanup_gate_workspace(gate_workspace)
```

Source: `packages/aether/services/promotion_engine.py:398` and `packages/aether/services/promotion_engine.py:505`

## 2.4 Promotion internals instrumented by phase

Promotion iteration now records select/evaluate/snapshot/gates/commit sub-phases:

```python
phase_timing = {
    "select_ms": 0.0,
    "evaluate_ms": 0.0,
    "clone_ms": 0.0,
    "snapshot_ms": 0.0,
    "snapshot_hash_ms": 0.0,
    "snapshot_materialize_ms": 0.0,
    "snapshot_db_commit_ms": 0.0,
    "snapshot_publish_ms": 0.0,
    "gates_ms": 0.0,
    "commit_ms": 0.0,
    ...
}
```

Source: `packages/aether/services/promotion_engine.py:935`

## 2.5 Separate worker processes integrated into server lifecycle

### Ingest materialization worker

- file: `apps/aether_server/ingest_worker.py`
- enabled by default via env config loader
- moves `INGESTED -> PENDING` out of API path and promotion loop

### Autopromotion worker

- file: `apps/aether_server/promotion_worker.py`
- opt-in with `AETHER_AUTOPROMOTE_ENABLED`
- runs promotion cycles in a separate process

Server startup wiring:

```python
materialization_config = IngestMaterializationConfig.from_env()
ingest_worker_handle = start_ingest_worker(...)

auto_promotion_config = AutoPromotionConfig.from_env()
promotion_worker_handle = start_promotion_worker(...)
```

Source: `apps/aether_server/main.py:135`

## 2.6 Semantic conflict units extended for non-Python files

Adapter now supports tree-sitter-backed symbol extraction for JS/TS/Go/Java/Rust with fallback behavior:

```python
def _tree_sitter_language_for_extension(ext: str) -> Optional[str]:
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if ext in {".ts", ".tsx"}:
        return "typescript"
    if ext == ".go":
        return "go"
    if ext == ".java":
        return "java"
    if ext == ".rs":
        return "rust"
```

Source: `packages/aether/services/symbol_adapters.py:35`

Fallback rule to force deterministic safety on ambiguity remains explicit:

```python
if "__file__" in proposal_symbols[path] or "__file__" in other_symbols[path]:
    return True
```

Source: `packages/aether/services/proposal_service.py:241`

---

## 3) End-to-End Flow (Current)

```text
Agent proposes diff/full-file
  -> ProposalService.submit_proposal
      -> fast path stores Proposal(status=INGESTED)
      -> ack returned

Background materializer
  -> scans INGESTED proposals
  -> computes semantic touch sets
  -> conflict grouping
  -> marks Proposal(status=PENDING)

Promotion worker/loop
  -> select pending + conflict sets
  -> prioritize non-conflicting batches
  -> evaluate (clone/apply/snapshot/gates/score)
  -> promote if score > 0 and required gates pass
  -> write Promotion + OperationGraphNode + Head update

Observable outputs
  -> API routes, JSON artifacts, operation-graph queries, verify endpoint
```

---

## 4) Empirical Evidence from Aether Artifacts

All numbers below are from repository artifacts under `.pytest-artifacts/`.

## 4.1 Key run table

| Artifact | Pass | Submit TPS | Promotion TPS | Submit P95 (ms) | Ack->Promote P95 (ms) | Conflict Rate | Backlog End | Snapshot Avg (ms) | Snapshot Materialize Avg (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `.pytest-artifacts/chunk1/chunk1-burst-pass1.json` | yes | 111.93 | 2.68 | 7.33 | 116,478 | 0.095 | 9,741 | 1,390.12 | 1,162.83 |
| `.pytest-artifacts/chunk1/chunk1-burst-pass2.json` | yes | 116.96 | 1.17 | 9.06 | 108,906 | 0.092 | 10,330 | 1,669.41 | 1,506.09 |
| `.pytest-artifacts/chunk2/chunk2-burst-rerun1.json` | yes | 113.52 | 10.32 | 7.27 | 113,957 | 0.094 | 8,953 | 3,330.89 | 2,501.34 |
| `.pytest-artifacts/chunk2/chunk2-burst-rerun2.json` | yes | 114.19 | 10.42 | 7.07 | 109,874 | 0.094 | 9,014 | 3,046.61 | 2,252.62 |
| `.pytest-artifacts/chunk3/chunk3-burst-check1.json` | yes | 110.78 | 10.34 | 7.72 | 107,905 | 0.097 | 8,765 | 2,724.95 | 1,930.91 |
| `.pytest-artifacts/chunk3/chunk3-burst-check2.json` | yes | 113.41 | 10.37 | 7.45 | 107,682 | 0.095 | 8,981 | 2,820.33 | 2,032.63 |
| `.pytest-artifacts/chunk3/chunk3-burst-check3.json` | yes | 110.89 | 10.18 | 7.09 | 109,902 | 0.096 | 8,741 | 2,869.37 | 2,082.63 |
| `.pytest-artifacts/chunk3/chunk3-15m-stage-a.json` | no (`submit_throughput_ok`) | 11.32 | 8.36 | 6.63 | 113,010 | 0.053 | 1,360 | 3,886.31 | 1,955.07 |
| `.pytest-artifacts/chunk3/chunk3-15m-stage-b.json` | no (`submit_throughput_ok`) | 11.24 | 5.06 | 9.87 | 586,108 | 0.092 | 4,339 | 3,004.22 | 2,066.73 |
| `.pytest-artifacts/chunk3/chunk3-60m-pass1.json` | yes* | 5.29 | 1.31 | 8.79 | 2,833,019 | 0.068 | 9,570 | 265.77 | 204.25 |
| `.pytest-artifacts/chunk3/chunk3-60m-pass2.json` | yes* | 4.99 | 1.36 | 13.43 | 3,296,052 | 0.068 | 8,408 | 905.98 | 698.75 |
| `.pytest-artifacts/soak/swarm-soak-4h-1000-20260209-220610.json` | no (`submit_throughput_ok`) | 1.05 | 0.29 | 16.83 | 12,531,234 | 0.070 | 8,908 | 34,167.32 | 0.00 |

`*` 60-minute pair uses a stability profile with submit-throughput set non-blocking (`--min-submit-throughput 0` in script), so pass semantics differ from strict burst SLO profile.

Source scripts:

- `scripts/run_chunk3_60m_pair.sh`
- `scripts/run_chunk3_15m_ab.sh`

## 4.2 What improved

1. **Clone cost collapse after copy-on-write changes**
   - Mean `clone_ms` dropped from ~4309 ms (chunk1) to ~0.74 ms (chunk2 burst reruns), then stayed ~0.75 ms (chunk3 burst).
2. **Delta snapshot materialization improvement**
   - Chunk2 burst mean `snapshot_materialize_ms`: ~2376.98 ms
   - Chunk3 burst mean `snapshot_materialize_ms`: ~2015.39 ms
   - Relative improvement: ~15.2%
3. **Promotion throughput in short burst remained high (~10 proposals/sec)** after chunk2/chunk3 optimization.

## 4.3 What remains unresolved

1. **Sustained intake under strict long horizons** fails submit throughput gates when backlog pressure controls are active.
2. **Ack->Promote tail latency** grows into millions of ms under long runs (queueing dominates).
3. **Backlog remains high** (many thousands), meaning liveness is present but latency SLO is not yet solved for long horizon high submit rates.

## 4.4 Interpretation

Current architecture shows:

- **Safety and liveness:** mostly yes.
- **Latency control at sustained 1000-agent rates:** not yet.
- **Semantic conflict precision:** improved in supported language regions.
- **Deterministic provenance path:** implemented and test-covered.

This is a partial validation, not full hypothesis closure.

---

## 5) Validation Through Tests (Key Regression Locks)

## 5.1 Semantic fallback determinism (`__file__`)

Aether explicitly enforces file-level fallback on ambiguous/unparseable diffs and locks this with tests:

- `tests/aether/test_services.py:227`
- `tests/aether/test_services.py:271`

This is critical to avoid nondeterministic false non-conflicts.

## 5.2 Non-Python disjoint symbol edits

TypeScript disjoint function edits are validated as non-conflicting:

- `tests/aether/test_services.py:310`

Same-symbol edits still conflict:

- `tests/aether/test_services.py:377`

## 5.3 Delta snapshot correctness

True delta storage + logical full view + gate workspace reconstruction are tested:

- `tests/aether/test_services.py:592`
- `tests/aether/test_services.py:641`

## 5.4 Provenance and operation graph verification

Promotion evidence and operation node consistency are tested:

- `tests/aether/test_services.py:941`
- `tests/aether/test_services.py:987`

## 5.5 Optional soak gate in CI/test lane

`AETHER_ENABLE_SOAK_TESTS=1` conditionally enables 500-agent soak profile:

- `tests/aether/test_swarm_scale.py:522`

CI workflow includes soak artifact publishing:

- `.github/workflows/aether-ci.yml`

---

## 6) Alignment to Slide-Deck Theory (`The_Operating_System_for_AI_Developers.pdf`)

Source deck: `docs/inputs/The_Operating_System_for_AI_Developers.pdf`

Observed deck claims and current code alignment:

1. **"From file locking to swarm synchronization"**
   - Partially aligned: Aether has swarm harness + async workers + promotion engine.
2. **"Semantic convergence (structure not lines)"**
   - Partially aligned: Python semantic nodes + non-Python adapters + `__file__` safety fallback.
3. **"Bayesian trust calibration"**
   - Aligned: trust-adjusted scoring in `PromotionEngine.compute_score` via `TrustService`.
4. **"Benchmarked at 100 concurrent agents"**
   - Exceeded in burst harness scenarios; sustained strict 1000-agent outcomes are mixed.
5. **"From mock to production cockpit"**
   - Platform plumbing exists (server, UI, API, workers), but production-grade sustained backlog/latency controls need more work.

Important distinction for peer review:

- Deck messaging is forward-leaning.
- Code and artifacts show real progress but still unresolved scaling/lag bottlenecks in long strict scenarios.

---

## 7) Beyond Aether: External Research and Systems Basis

This section grounds the direction in external primary sources and clarifies what is validated vs inferred.

## 7.1 Benchmark-rigorous, execution-native coding agents

### SWE-bench (benchmark framing)

- [SWE-bench paper (arXiv:2310.06770)](https://arxiv.org/abs/2310.06770)
- [SWE-bench repository](https://github.com/SWE-bench/SWE-bench)

Relevance:

- Establishes realistic issue-to-code evaluation in real repos.
- Supports Aether design choice: quality must be measured in repository-grounded tasks, not synthetic toy edits.

### SWE-agent (agentic execution loop)

- [SWE-agent paper (arXiv:2405.15793)](https://arxiv.org/abs/2405.15793)

Relevance:

- Shows that tool-using agents with environment interaction can substantially improve over naive prompting.
- Supports Aether principle: execution-native loops + external tools are core, not optional.

### AutoCodeRover (repository-aware debugging/navigation)

- [AutoCodeRover paper (arXiv:2404.05427)](https://arxiv.org/abs/2404.05427)

Relevance:

- Emphasizes repository exploration and iterative reasoning for bug fixing.
- Supports Aether requirement for repository state as first-class runtime substrate.

### OpenHands trajectory data

- [OpenHands trajectories paper (arXiv:2407.16741)](https://arxiv.org/abs/2407.16741)

Relevance:

- Highlights value of large, realistic agent interaction trajectories.
- Supports Aether roadmap item: promotion decisions should be evidence/data-rich and replayable.

### SWE-smith (future synthetic training/eval direction)

- [SWE-smith paper (arXiv:2504.21798)](https://arxiv.org/abs/2504.21798)

Relevance:

- Reinforces that benchmark and data generation strategy affect agent software-engineering capability scaling.

## 7.2 CRDT and semantic merge theory

- [A comprehensive study of CRDTs (arXiv:1806.10254)](https://arxiv.org/abs/1806.10254)
- [Conflict-Free Replicated Data Types (Shapiro et al., INRIA report link)](https://pages.lip6.fr/Marc.Shapiro/papers/RR-7687.pdf)

Relevance:

- Convergence without global locking is well-founded.
- Aether uses CRDT and structured semantic mechanisms; however, promotion semantics must still enforce policy and buildability.

Inference:

- CRDT convergence alone is insufficient for production code quality; Aether's gate-evidence and promotion protocol is the right additional layer.

## 7.3 Git internals baseline (what is being replaced, what is retained)

- [Git objects internals](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)
- [Git packfiles internals](https://git-scm.com/book/en/v2/Git-Internals-Packfiles)
- [Git branching and merging (three-way merge baseline)](https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging)

Relevance:

- Git is highly optimized for human-initiated commit graphs and content-addressed storage.
- Aether should not attempt to "beat Git" on all dimensions; it should beat Git specifically on machine-native concurrent integration loops.

## 7.4 Patch-theory alternatives

- [Pijul project](https://pijul.org/)
- [Pijul manual](https://pijul.org/manual/theory.html)

Relevance:

- Prior art already questions commit/line-centric model.
- Supports Aether direction toward operation-centric history and better merge semantics.

## 7.5 Distributed ordering and coordination fundamentals

- [Time, Clocks, and the Ordering of Events (Lamport)](https://www.microsoft.com/en-us/research/publication/time-clocks-ordering-events-distributed-system/)
- [Raft resources](https://raft.github.io/)
- [USENIX ATC 2014 Raft paper page](https://www.usenix.org/conference/atc14/technical-sessions/presentation/ongaro)
- [Apache Kafka design docs](https://kafka.apache.org/documentation/)

Relevance:

- Any 1000+ agent coordination plane needs explicit ordering, deterministic replay, and fault-tolerant distributed scheduling.
- Supports roadmap requirement for sharded event/state coordination instead of single-process loops.

---

## 8) Direct Comparison: Git Model vs Aether Target Model

| Dimension | Git (baseline) | Aether target |
|---|---|---|
| Primary unit | Commit (file/line deltas) | Operation graph node with proposal set + evidence |
| Conflict granularity | Mostly line/file overlap and manual merge | Semantic units (symbol/API/AST region) with safe fallbacks |
| Integration gate | Human PR workflow | Protocol-level automated gates before promotion |
| Runtime orientation | Human-centric asynchronous collaboration | Agent-centric continuous integration/promotion loop |
| Provenance | Commit DAG + metadata | Deterministic replay hashes + operation graph lineage |
| Burst parallelism | Possible but socially/operationally constrained | First-class design target |
| Interop | Native ecosystem | Bridge mode via import/export required during migration |

Conclusion:

- The "replace Git" path is plausible only if Aether wins in the agent-native regime while preserving bridge interoperability.

---

## 9) Core Code Snippets for Peer Review (Copy-Paste Set)

This section includes representative code excerpts that capture current design decisions.

## 9.1 Append-only ingest ACK + deferred materialization

From `packages/aether/services/proposal_service.py`:

```python
def _ingest_proposal_sync(self, repo_id: str, proposal_data: ProposalCreate):
    if proposal_data.idempotency_key:
        existing = self._get_by_idempotency_key(proposal_data.idempotency_key)
        if existing:
            return self._build_idempotent_response(repo_id, existing)

    proposal = self._create_proposal_row(
        repo_id,
        proposal_data,
        initial_status=ProposalStatus.INGESTED,
    )
    self.session.add(proposal)
    self.session.commit()
    self.session.refresh(proposal)

    return (
        ProposalResponse(
            proposal_id=proposal.id,
            status=proposal.status,
            conflict_set_id=None,
            message="Proposal ingested; async conflict detection queued",
        ),
        events,
        log_context,
    )
```

```python
def _materialize_ingested_sync(self, repo_id: str, limit: int):
    statement = (
        select(Proposal)
        .where(
            Proposal.repo_id == repo_id,
            Proposal.status == ProposalStatus.INGESTED,
        )
        .order_by(Proposal.submitted_at.asc())
        .limit(limit)
    )
    queued = list(self.session.exec(statement))
    ...
    proposal.status = ProposalStatus.PENDING
    self.session.add(proposal)
    ...
    self.session.commit()
```

## 9.2 True-delta snapshot manifest semantics

From `packages/aether/services/snapshot_service.py`:

```python
def _materialize_delta_snapshot(
    self,
    document_store: DocumentStore,
    snapshot_path: Path,
    base_snapshot_id: str,
    base_paths: set[str],
    changed_paths: set[str],
    total_bytes: int,
) -> tuple[int, int, int, int, int, int]:
    current_paths = set(document_store.documents.keys())
    write_paths = sorted(
        rel_path
        for rel_path in current_paths
        if rel_path in changed_paths or rel_path not in base_paths
    )
    deleted_paths = sorted(base_paths - current_paths)

    for rel_path in write_paths:
        file_doc = document_store.documents.get(rel_path)
        if not file_doc:
            continue
        out_path = snapshot_path / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        content = file_doc.get_content()
        out_path.write_text(content, encoding="utf-8")

    changed_manifest_paths = sorted(set(write_paths) | set(deleted_paths))
    self._write_manifest(
        snapshot_path,
        {
            "version": 1,
            "mode": "delta",
            "base_snapshot_id": base_snapshot_id,
            "changed_paths": changed_manifest_paths,
            "deleted_paths": deleted_paths,
        },
    )
```

## 9.3 Gate workspace materialization off hot path

From `packages/aether/services/snapshot_service.py`:

```python
def materialize_gate_workspace(self, snapshot_id: str) -> Optional[Path]:
    snapshot = self.get_snapshot(snapshot_id)
    if not snapshot:
        return None

    file_index = self._resolve_snapshot_file_index(snapshot_id)
    workspace_root = self.storage_path / snapshot.repo_id / "gate_workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_path = workspace_root / f"{snapshot_id}_{uuid4().hex[:8]}"
    workspace_path.mkdir(parents=True, exist_ok=True)

    for rel_path, source_path in file_index.items():
        dest_path = workspace_path / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source_path, dest_path)
        except OSError:
            shutil.copy2(source_path, dest_path)

    return workspace_path
```

## 9.4 Promotion phase timing and bounded iteration controls

From `packages/aether/services/promotion_engine.py`:

```python
async def process_pending_proposals(
    self,
    repo_id: str,
    document_store: DocumentStore,
    batch_size: int = 10,
    proposal_scan_limit: Optional[int] = None,
    max_conflict_sets: Optional[int] = 1,
    max_non_conflicting_batches: int = 4,
    max_processing_seconds: Optional[float] = None,
    recover_stuck_after_seconds: Optional[float] = 60.0,
    prioritize_non_conflicting: bool = True,
) -> list[Promotion]:
    phase_timing = {
        "select_ms": 0.0,
        "evaluate_ms": 0.0,
        "clone_ms": 0.0,
        "snapshot_ms": 0.0,
        "snapshot_hash_ms": 0.0,
        "snapshot_materialize_ms": 0.0,
        "snapshot_db_commit_ms": 0.0,
        "snapshot_publish_ms": 0.0,
        "gates_ms": 0.0,
        "commit_ms": 0.0,
        "promote_ms": 0.0,
        ...
    }
```

## 9.5 Deterministic provenance fields

From `packages/aether/services/promotion_engine.py`:

```python
def _build_provenance_evidence(...):
    graph_payload = {
        "repo_id": repo_id,
        "parent_head_id": from_head_id,
        "parent_sequence": from_head_sequence,
        "to_sequence": to_head_sequence,
        "snapshot_id": candidate.snapshot_id,
        "score": round(candidate.score, 8),
        "triggered_by": triggered_by,
        "proposals": proposal_vector,
        "excluded_proposals": sorted(candidate.excluded_proposal_ids),
        "gates": gate_vector,
        "from_content_hash": from_content_hash,
        "to_content_hash": to_content_hash,
    }
    canonical_payload = json.dumps(graph_payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    return {
        "_op_graph_parent": parent,
        "_op_graph_node": payload_hash,
        "_op_graph_payload_sha256": payload_hash,
        "_op_graph_kind": "promotion",
        "_replay_snapshot_id": candidate.snapshot_id,
        "_replay_from_hash": from_content_hash,
        "_replay_to_hash": to_content_hash,
        "_replay_gate_vector": "|".join(gate_vector),
    }
```

## 9.6 Operation graph node persistence during promote

From `packages/aether/services/promotion_engine.py`:

```python
operation_node = OperationGraphNode(
    repo_id=repo_id,
    promotion_id=promotion.id,
    parent_node_id=parent_node.id if parent_node else None,
    parent_head_id=promotion.from_head_id,
    parent_head_sequence=promotion.from_head_sequence,
    to_head_id=new_head.id,
    to_head_sequence=new_head.sequence,
    snapshot_id=candidate.snapshot_id,
    operation_type=str(promotion_evidence.get("_op_graph_kind", "promotion")),
    node_hash=str(promotion_evidence.get("_op_graph_node", "")),
    payload_sha256=str(promotion_evidence.get("_op_graph_payload_sha256", "")),
    parent_ref=str(promotion_evidence.get("_op_graph_parent", "")),
    replay_from_hash=str(promotion_evidence.get("_replay_from_hash", "")),
    replay_to_hash=str(promotion_evidence.get("_replay_to_hash", "")),
    replay_gate_vector=str(promotion_evidence.get("_replay_gate_vector", "")),
    included_proposals=promotion.included_proposals,
    excluded_proposals=promotion.excluded_proposals,
)
self.session.add(operation_node)
self.session.commit()
```

## 9.7 Non-Python symbol extraction adapters

From `packages/aether/services/symbol_adapters.py`:

```python
def extract_symbol_spans(path: str, source_code: str) -> list[SymbolSpan]:
    ext = _file_extension(path)
    if ext in _JS_TS_EXTENSIONS:
        spans = _tree_sitter_extract(path, source_code)
        if spans:
            return spans
        return _regex_extract_js_ts(source_code)

    spans = _tree_sitter_extract(path, source_code)
    if spans:
        return spans

    return []
```

## 9.8 Dedicated worker process skeleton

From `apps/aether_server/ingest_worker.py`:

```python
while not stop_event.is_set():
    try:
        asyncio.run(
            _run_cycle(
                database_url=database_url,
                storage_path=worker_storage,
                config=config,
                stop_event=stop_event,
            )
        )
    except Exception as exc:
        logger.error("materialize_cycle_failed", error=str(exc))

    if stop_event.wait(config.interval_seconds):
        break
```

From `apps/aether_server/promotion_worker.py`:

```python
await asyncio.gather(*[_process_repo(repo_id) for repo_id in repo_ids])
```

---

## 10) Critical Technical Gaps (Brutally Honest)

This is the most important section for peer review.

## 10.1 Queueing-lag problem remains unsolved at target ambition

Evidence:

- Ack->Promote P95 in long runs enters very high tail values.
- Backlog remains high despite adaptive throttle.

Diagnosis:

- Promotion service capacity and gate/snapshot cost still define effective integration ceiling.
- Intake can remain fast, but canonicalization is slower than aggregate proposal generation under aggressive configurations.

## 10.2 One-node local harness is not evidence of cloud-scale architecture

Current harness and workers are still mostly single-host/single-DB process designs. This is enough for algorithmic prototyping, not enough for claiming internet-scale replacement.

## 10.3 Conflict materialization complexity risk

Although path-indexing reduced naive scans, conflict analysis can still drift toward expensive behavior under heavy overlap unless further batched/indexed methods are introduced.

## 10.4 Gate execution still dominant in some profiles

When gate phases dominate, throughput increases require either:

- better gate parallelization,
- cheaper pre-gates,
- or selective gate execution policies by risk tier.

## 10.5 SLO contracts need profile clarity

Burst and soak have different meaningful gate contracts. Mixing them into one strict threshold set can produce contradictory outcomes.

---

## 11) Corrective Engineering Plan (Chunked, Gate-Driven)

## 11.1 Immediate (next 10 days, investor-demo compatible)

1. **Protect demo stability path**
   - Keep dashboard/server stable baseline untouched during heavy soak windows.
   - Run long tests only on dedicated windows (overnight/power + caffeinate).
2. **Strengthen artifacts and reporting**
   - Standardize one JSON summary per run class with explicit profile label (`burst`, `stability`, `strict-soak`).
3. **Run reproducible 2x60m stability with interruption-safe orchestration scripts**
   - Ensure missing second-pass artifacts fail loudly and are retriable.

Quality gates:

- No dashboard regressions.
- Two consecutive stability runs with artifact completeness and identical command signatures.

## 11.2 Near-term (architecture uplift to close lag gap)

1. **Manifest/delta phase 2**
   - Store direct per-file hash/size metadata in manifest for faster index resolve.
2. **Conflict materialization batching**
   - Introduce batched path-index grouping and bounded per-tick conflict work.
3. **Promotion pipeline split in-process first, then multi-worker**
   - Separate evaluator and committer paths with explicit queue semantics.
4. **Multi-repo or shard-aware promotion evaluators**
   - Parallel evaluators per repo/shard once phase timings confirm dominance section.

Quality gates:

- Snapshot and gates phase median/95th reduction against locked baseline.
- Bounded backlog for specified profile.

## 11.3 Medium-term (true "beyond Git" infrastructure requirements)

1. **Distributed coordination plane**
   - Event bus + durable state shards (not single local SQLite center).
2. **Operation graph protocol hardening**
   - Explicit causal edges, conflict-resolution lineage, replay toolchain.
3. **Policy/evidence protocolization**
   - Make gate evidence and promotion decision packets verifiable across nodes.
4. **Git bridge formalization**
   - Import/export compatibility tests as migration contract.

Quality gates:

- Deterministic replay across independent nodes.
- Shard failover tests.
- Cross-node promotion consistency checks.

---

## 12) Proposed Peer-Review Questions for Deep Think

1. Is the operation-graph + evidence-gated promotion protocol sufficient as a replacement primitive for commit-centric merge flow?
2. Are semantic conflict units and fallback behavior defined strongly enough to avoid unsafe false non-conflicts?
3. What is the best formalism for proving liveness/safety under bounded worker and asynchronous queue scheduling?
4. Which queueing/control strategy is most appropriate for adaptive intake vs promotion throughput balancing at 1000+ agents?
5. What minimal distributed substrate (consensus/log/eventing) is required before claims about Git replacement are technically defensible?

---

## 13) Reproducibility Commands

## 13.1 Local tests

```bash
source .venv/bin/activate
python -m pytest tests/aether/test_services.py tests/aether/test_swarm_harness.py -q
```

## 13.2 Chunk 3 15m A/B

```bash
./scripts/run_chunk3_15m_ab.sh
```

## 13.3 Chunk 3 60m pair

```bash
./scripts/run_chunk3_60m_pair.sh
```

## 13.4 Strict long soak (example)

```bash
.venv/bin/python -m apps.aether_cli.main swarm-harness \
  --agents 1000 --files 500 --run-seconds 14400 --drain-seconds 1200 \
  --contention low --min-interval-ms 350 --max-interval-ms 1400 \
  --inflight 400 --promotion-batch 30 --promotion-non-conflicting-batches 12 \
  --promotion-max-conflict-sets 1 --gate-execution-mode parallel --gate-max-parallel 4 \
  --promotion-interval-ms 40 --promotion-iteration-timeout-seconds 30 \
  --materialization-batch-size 2000 --materialization-interval-ms 10 \
  --monitor-interval-ms 500 --min-submit-throughput 30 --min-promotion-throughput 0.15 \
  --max-submit-p95-ms 220 --max-conflict-rate 0.35 --max-backlog-end 12000 \
  --max-submit-failure-rate 0.02 --seed 2026 \
  --output-json .pytest-artifacts/soak/swarm-soak-4h-1000.json --strict
```

---

## 14) Final Technical Assessment

### 14.1 Is architecture direction correct?

**Yes, directionally**:

- Async ingest + deferred conflict detection
- semantic conflict handling
- operation graph + deterministic provenance
- explicit quality gates and harness artifacts

are the right primitives for agent-native collaboration.

### 14.2 Is "Git replacement" already proven?

**No. Not yet.**

What is proven:

- key architecture primitives work,
- burst performance can be high,
- provenance and replay metadata path exists.

What is not yet proven:

- sustained low-lag canonicalization at aggressive 1000-agent continuous rates,
- distributed/sharded correctness and availability,
- migration-grade interoperability semantics at production scale.

### 14.3 Practical verdict for upcoming review

Aether is in a strong **prototype-to-preproduct transition**:

- technically serious,
- empirically instrumented,
- with clear unresolved bottlenecks,
- and with a coherent path to test/falsify the remaining core claims.

That is a credible state for rigorous external peer review.

---

## 15) References

### Aether internal sources

- `docs/swarm_chunk_status.md`
- `docs/swarm_quality_gates.md`
- `docs/swarm_scalability_runbook.md`
- `docs/aether_native_vcs_roadmap.md`
- `packages/aether/services/proposal_service.py`
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/promotion_engine.py`
- `packages/aether/services/swarm_harness.py`
- `packages/aether/services/symbol_adapters.py`
- `packages/aether/models/entities.py`
- `apps/aether_server/main.py`
- `apps/aether_server/ingest_worker.py`
- `apps/aether_server/promotion_worker.py`
- `packages/aether/api/routes.py`
- `tests/aether/test_services.py`
- `tests/aether/test_swarm_scale.py`
- `.github/workflows/aether-ci.yml`

### External primary sources

1. SWE-bench paper (arXiv): https://arxiv.org/abs/2310.06770
2. SWE-agent paper (arXiv): https://arxiv.org/abs/2405.15793
3. AutoCodeRover paper (arXiv): https://arxiv.org/abs/2404.05427
4. OpenHands trajectories paper (arXiv): https://arxiv.org/abs/2407.16741
5. SWE-smith paper (arXiv): https://arxiv.org/abs/2504.21798
6. CRDT comprehensive study (arXiv): https://arxiv.org/abs/1806.10254
7. CRDT INRIA technical report: https://pages.lip6.fr/Marc.Shapiro/papers/RR-7687.pdf
8. Git objects internals: https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
9. Git packfiles internals: https://git-scm.com/book/en/v2/Git-Internals-Packfiles
10. Git branching/merging baseline: https://git-scm.com/book/en/v2/Git-Branching-Basic-Branching-and-Merging
11. Pijul project: https://pijul.org/
12. Pijul patch theory manual: https://pijul.org/manual/theory.html
13. Lamport ordering paper page: https://www.microsoft.com/en-us/research/publication/time-clocks-ordering-events-distributed-system/
14. Raft resource hub: https://raft.github.io/
15. Raft ATC paper page: https://www.usenix.org/conference/atc14/technical-sessions/presentation/ongaro
16. Apache Kafka documentation: https://kafka.apache.org/documentation/
17. Tree-sitter documentation: https://tree-sitter.github.io/tree-sitter/
18. Tree-sitter repository: https://github.com/tree-sitter/tree-sitter

### Deck source

- `docs/inputs/The_Operating_System_for_AI_Developers.pdf`

