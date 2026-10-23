# Project Aether - Phase 1 Implementation Plan
## MVP v0.1: Usable for Early Adopters

**Status:** Ready for Review
**Timeline:** 3-6 weeks (per PRD)
**Goal:** Aether usable by agent systems with real workloads

---

## 1. Phase 0 Recap (Completed)

| Deliverable | Status | Notes |
|-------------|--------|-------|
| Single-node server | ✅ | FastAPI on port 8100 |
| File-level CRDT | ✅ | RGA implementation |
| Proposal API | ✅ | Submit with confidence signals |
| Snapshot materialization | ✅ | Content-addressed storage |
| 51 tests passing | ✅ | Including swarm simulation |

---

## 2. Phase 1 Deliverables

### 2.1 Enhanced Conflict Resolution (Priority: High)

**Current state:** Basic conflict detection exists, but top-k evaluation is incomplete.

**Work items:**
1. **Conflict set scoring** - Evaluate all proposals in a conflict set, not just first-found
2. **Top-k alternatives** - Maintain configurable k candidates (default: 5)
3. **Parallel evaluation** - Run gates on multiple candidates concurrently
4. **Selection criteria** - Choose winner based on: gates pass → confidence → submission time

**Files to modify:**
- `packages/aether/services/promotion_engine.py`
- `packages/aether/services/proposal_service.py`

**Acceptance test:**
```
Given: 5 proposals for same file region
When: Promotion engine evaluates
Then: All 5 are tested, best score wins, others marked SUPERSEDED
```

---

### 2.2 Production-Ready Gate Runner (Priority: High)

**Current state:** Gates run sequentially with basic shell execution.

**Work items:**
1. **Configurable gate definitions** - YAML/JSON config per repo
2. **Parallel gate execution** - Run independent gates concurrently
3. **Artifact collection** - Store test output, coverage reports, build logs
4. **Gate result caching** - Skip re-running if snapshot hash matches
5. **Timeout and retry** - Configurable per gate with exponential backoff

**New config format:**
```yaml
gates:
  - name: syntax
    command: ruff check .
    required: true
    timeout: 60
    parallel_group: static

  - name: build
    command: pip install -e . && python -c "import packages.aether"
    required: true
    timeout: 300
    depends_on: [syntax]

  - name: unit_tests
    command: pytest tests/ -v --tb=short
    required: true
    timeout: 600
    depends_on: [build]
    artifacts:
      - junit.xml
      - coverage.xml
```

**Files to create/modify:**
- `packages/aether/services/gate_runner.py` (enhance)
- `packages/aether/models/gate_config.py` (new)
- `packages/aether/services/artifact_store.py` (new)

---

### 2.3 Git Import/Export (Priority: High)

**Current state:** No Git integration.

**Work items:**
1. **Git import** - Clone existing repo into Aether document store
   - Parse git history into initial snapshot
   - Support branch specification

2. **Git export** - Export snapshot as git commit(s)
   - Create deterministic commit from snapshot
   - Support incremental export (promotion → commit)

3. **git-remote helper** (stretch) - `git push aether://repo_id`

**New module:**
```
packages/aether/git/
├── __init__.py
├── importer.py      # Clone git repo → Aether
├── exporter.py      # Snapshot → git commit
└── remote_helper.py # git-remote-aether (future)
```

**API additions:**
```
POST /v1/repos/{id}/import-git
  body: { "git_url": "...", "branch": "main" }

POST /v1/repos/{id}/export-git
  body: { "snapshot_id": "...", "target_ref": "refs/heads/main" }
```

---

### 2.4 Minimal Web UI (Priority: Medium)

**Current state:** No UI.

**Proposed stack:** React + Tailwind (or simple Jinja2 templates for MVP)

**Views required:**
1. **Repository list** - Name, head sequence, last promotion
2. **Repository detail** - Files, current head, pending proposals
3. **Proposal queue** - Status, agent, confidence, affected files
4. **Promotion history** - Timeline of head advances with evidence
5. **Diff viewer** - Compare proposal against head
6. **Conflict map** (basic) - Show which files have competing proposals

**Files to create:**
```
apps/aether_ui/
├── __init__.py
├── main.py           # FastAPI mount point
├── templates/        # Jinja2 templates
│   ├── base.html
│   ├── repos.html
│   ├── repo_detail.html
│   ├── proposals.html
│   └── diff.html
└── static/
    └── styles.css
```

**Acceptance:** User can browse repo, see proposals, view diffs without CLI.

---

### 2.5 Improved Scoring & Calibration (Priority: Medium)

**Current state:** Simple weighted average (confidence + test pass + static).

**Work items:**
1. **Agent reliability tracking** - Historical success rate per agent_id
2. **Calibration profiles** - Map model confidence to actual success rate
3. **Risk-adjusted scoring** - Higher-risk paths require higher confidence
4. **Batch compatibility scoring** - Prefer batches that don't overlap

**New models:**
```python
class AgentProfile(SQLModel):
    agent_id: str
    proposals_submitted: int
    proposals_promoted: int
    average_confidence: float
    actual_success_rate: float  # promoted without rollback

class CalibrationProfile(SQLModel):
    model_id: str
    # Maps stated confidence to observed accuracy
    calibration_curve: str  # JSON: [[0.5, 0.4], [0.7, 0.65], ...]
```

---

### 2.6 Observability & Operations (Priority: Medium)

**Work items:**
1. **Structured logging** - Already using structlog, add correlation IDs
2. **Metrics endpoint** - `/v1/metrics` (Prometheus format)
   - `aether_proposals_total{status, repo_id}`
   - `aether_promotions_total{repo_id}`
   - `aether_gate_duration_seconds{gate_name}`
   - `aether_head_sequence{repo_id}`
3. **Health checks** - Liveness, readiness probes
4. **Rate limiting** - Per agent token
5. **Audit log query API** - Search proposals/promotions by time, agent, status

---

## 3. Testing Strategy

### 3.1 Scale Testing
- **Target:** 50-100 simulated agents
- **Duration:** 10-minute sustained run
- **Success criteria:**
  - Zero manual merge interventions
  - Head green ≥95% of promotions
  - Throughput ≥10 proposals/sec ingestion

### 3.2 Integration Tests
- Git import → propose changes → export → verify git history
- WebSocket client receives all expected events
- UI renders correctly with real data

### 3.3 Failure Scenarios
- Agent submits invalid diff → quarantine, no head impact
- Gate timeout → proposal rejected, not blocked
- Concurrent promotions → only one succeeds, no corruption

---

## 4. Proposed Work Order

| Week | Focus | Deliverables |
|------|-------|--------------|
| 1 | Conflict resolution | Top-k evaluation, parallel testing |
| 2 | Gate runner | Config format, parallel execution, artifacts |
| 3 | Git integration | Import from git, export snapshots |
| 4 | Web UI | Basic views for repos, proposals, diffs |
| 5 | Scoring & observability | Agent profiles, metrics, rate limits |
| 6 | Scale testing & polish | 100-agent simulation, bug fixes |

---

## 5. Open Questions for Review

1. **UI technology:** Full React SPA vs. server-rendered templates?
   - Recommendation: Start with Jinja2 templates (faster, simpler)

2. **Git export granularity:** One commit per promotion, or squash?
   - Recommendation: One commit per promotion (maintains audit trail)

3. **Agent authentication:** Token-based vs. mTLS?
   - Recommendation: Token-based for v0.1 (simpler), mTLS for v1+

4. **Multi-tenancy:** Needed for v0.1?
   - Recommendation: Defer to v1 (single-tenant per deployment)

5. **CRDT upgrade:** Replace custom RGA with Automerge?
   - Recommendation: Keep custom for Phase 1, plan migration for Phase 2

---

## 6. Success Metrics (Phase 1)

| Metric | Target |
|--------|--------|
| Concurrent agents supported | 100 |
| Proposal throughput | ≥10/sec sustained |
| Manual merge interventions | ~0 |
| Head buildability | ≥95% green |
| Time to answer "what changed?" | <2 min via UI |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| CRDT edge cases cause data loss | Medium | High | Extensive convergence tests, snapshot backups |
| Gate runner bottleneck | High | Medium | Parallel execution, result caching |
| Git export breaks determinism | Medium | Medium | Hash verification, reproducibility tests |
| UI scope creep | High | Low | Strict MVP feature set, iterate in Phase 2 |

---

## Next Steps

1. **Review this plan** - Confirm priorities and work order
2. **Create tracking issues** - Break into smaller tasks
3. **Start Week 1** - Enhanced conflict resolution

Ready to proceed when you approve the plan.
