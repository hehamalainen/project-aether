import os
import json
import pytest
import asyncio
from pathlib import Path
from sqlmodel import Session, create_engine, SQLModel, select, exists
from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    ProposalContention,
    ContentionWinner,
    ConflictSet,
    SystemMetricEvent,
    SystemMetricEventType,
    AetherHead,
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine, CohortResult

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

@pytest.fixture
def event_bus():
    return EventBus()

@pytest.fixture
def storage_path(tmp_path):
    return tmp_path / "aether_storage"

@pytest.fixture
def repo_service(db_session, storage_path):
    return RepositoryService(db_session, storage_path)

@pytest.fixture
def proposal_service(db_session, event_bus):
    return ProposalService(db_session, event_bus, {})

@pytest.fixture
def snapshot_service(db_session, event_bus, storage_path):
    return SnapshotService(db_session, event_bus, storage_path)

@pytest.fixture
def gate_runner(db_session, event_bus):
    return GateRunner(db_session, event_bus)

@pytest.fixture
def promotion_engine(db_session, event_bus, proposal_service, snapshot_service, gate_runner, storage_path):
    return PromotionEngine(
        db_session,
        event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        storage_path,
        gate_execution_mode="sequential",
    )

def make_diff(filename: str, old_start: int, old_count: int, new_start: int, new_count: int, lines: list[str]) -> str:
    diff_text = f"diff --git a/{filename} b/{filename}\n"
    diff_text += f"--- a/{filename}\n"
    diff_text += f"+++ b/{filename}\n"
    diff_text += f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"
    for line in lines:
        diff_text += f"{line}\n"
    return diff_text

@pytest.mark.asyncio
async def test_deferred_proposals_invisible_to_autopromote(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Submit 2 deferred proposals
    p1_create = ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    )
    p1 = await proposal_service.submit_proposal(repo.id, p1_create)

    p2_create = ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    )
    p2 = await proposal_service.submit_proposal(repo.id, p2_create)

    # Run autopromote pending Proposals
    promotions = await promotion_engine.process_pending_proposals(repo.id, doc_store)
    assert len(promotions) == 0

    db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p1_db.status == ProposalStatus.PENDING
    assert p2_db.status == ProposalStatus.PENDING

@pytest.mark.asyncio
async def test_deferred_invisible_to_physics(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Submit deferred proposal
    p_create = ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    )
    await proposal_service.submit_proposal(repo.id, p_create)

    # Call get_system_physics_snapshot
    physics = proposal_service.get_system_physics_snapshot(db_session, window_seconds=60)
    assert physics["queue_depth"]["active"] == 0
    assert physics["deferred_count"] == 1

@pytest.mark.asyncio
async def test_deferred_invisible_to_path_hotspot(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p_create = ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    )
    await proposal_service.submit_proposal(repo.id, p_create)

    path_counts = proposal_service.get_active_path_counts(db_session, repo_id=repo.id, paths=["user.py"])
    assert path_counts.get("user.py", 0) == 0

@pytest.mark.asyncio
async def test_deferred_no_conflict_set_at_intake(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Submit two overlapping deferred proposals
    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    assert p1.conflict_set_id is None
    assert p2.conflict_set_id is None

@pytest.mark.asyncio
async def test_cohort_merge_success(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    # Enable semantic merge
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    res = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )

    assert res.promotion is not None
    assert set(json.loads(res.promotion.included_proposals)) == {p1.proposal_id, p2.proposal_id}

    db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p1_db.status == ProposalStatus.PROMOTED
    assert p2_db.status == ProposalStatus.PROMOTED

    # Ensure contention winner is registered
    winners = db_session.exec(select(ContentionWinner)).all()
    assert len(winners) == 1
    assert winners[0].winner_proposal_id in {p1.proposal_id, p2.proposal_id}

    # Verify merged file content
    repo_refreshed = db_session.get(Repository, repo.id)
    head = db_session.get(AetherHead, repo_refreshed.head_id)
    assert head is not None
    merged_content = snapshot_service.get_file_content(head.snapshot_id, "user.py")
    assert "get_name" in merged_content
    assert "get_email" in merged_content

@pytest.mark.asyncio
async def test_cohort_stale_base_degrades(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Submit 2 deferred
    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    # Advance the head via a third non-deferred proposal
    p3 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-3",
        change_content=make_diff("user.py", 2, 2, 2, 4, ["     def __init__(self):", "         self.id = 99"]),
        affected_paths=["user.py"],
        source_run_id="run-1",
    ))
    promotions = await promotion_engine.process_pending_proposals(repo.id, doc_store)
    assert len(promotions) == 1

    # Now run evaluate_and_promote_cohort on stale ones
    res = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )

    assert res.promotion is None
    assert res.proposal_outcomes[0]["status"] == ProposalStatus.SUPERSEDED.value

    # Verify SEMANTIC_FALLBACK event is logged with stale_base
    events = db_session.exec(
        select(SystemMetricEvent).where(SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK)
    ).all()
    assert len(events) >= 1
    assert any(e.event_reason == "STALE_BASE" for e in events)

@pytest.mark.asyncio
async def test_cohort_write_write_collision(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Submit 2 deferred that modify the exact same method __init__
    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 4, ["     def __init__(self):", "         self.id = 10"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 4, ["     def __init__(self):", "         self.id = 20"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    res = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )

    assert res.promotion is None
    assert res.proposal_outcomes[0]["status"] == ProposalStatus.SUPERSEDED.value

    # Verify SEMANTIC_FALLBACK event is logged with WRITE_WRITE_COLLISION
    events = db_session.exec(
        select(SystemMetricEvent).where(SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK)
    ).all()
    assert len(events) >= 1
    assert any(e.event_reason == "WRITE_WRITE_COLLISION" for e in events)

@pytest.mark.asyncio
async def test_cohort_conflict_sets_terminalized(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    # Manually associate a conflict set to mimic external conflict set existence
    cs = ConflictSet(repo_id=repo.id, path="user.py", region_type="file", proposal_ids=json.dumps([p1.proposal_id, p2.proposal_id, "other_id"]))
    db_session.add(cs)
    db_session.commit()

    p1_db = proposal_service.get_proposal(p1.proposal_id)
    p1_db.conflict_set_id = cs.id
    db_session.add(p1_db)
    db_session.commit()

    p2_db = proposal_service.get_proposal(p2.proposal_id)
    p2_db.conflict_set_id = cs.id
    db_session.add(p2_db)
    db_session.commit()

    res = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )

    assert res.promotion is not None
    
    # Assert ConflictSet is resolved
    db_session.expire_all()
    cs_db = db_session.get(ConflictSet, cs.id)
    assert cs_db.resolved is True
    assert cs_db.active_choice_id.startswith("cohort:")

@pytest.mark.asyncio
async def test_cohort_provenance_queryable(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )

    prov1 = proposal_service.get_proposal_provenance(p1.proposal_id)
    assert prov1 is not None
    assert prov1.event_type == "structural_merge"

    prov2 = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov2 is not None
    assert prov2.event_type == "structural_merge"


@pytest.mark.asyncio
async def test_cohort_evaluate_rejects_non_deferred(promotion_engine, db_session, repo_service, proposal_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # 1. Non-deferred (regular) proposal
    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=False,
        source_run_id="run-1",
    ))

    # 2. Deferred proposal
    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    with pytest.raises(ValueError) as excinfo:
        await promotion_engine.evaluate_and_promote_cohort(
            repo_id=repo.id,
            proposal_ids=[p1.proposal_id, p2.proposal_id],
            document_store=doc_store,
        )
    assert "is not marked for deferred evaluation" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cohort_evaluate_rollback_on_promotion_failure(promotion_engine, db_session, repo_service, proposal_service, snapshot_service, monkeypatch):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    def __init__(self):\n        self.id = 1\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p1 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-1",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_name(self):", "         return 'Alice'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    p2 = await proposal_service.submit_proposal(repo.id, ProposalCreate(
        agent_id="agent-2",
        change_content=make_diff("user.py", 2, 2, 2, 6, ["     def __init__(self):", "         self.id = 1", "", "     def get_email(self):", "         return 'alice@example.com'"]),
        affected_paths=["user.py"],
        defer_evaluation=True,
        contention_group_id="group-1",
        contention_policy="semantic_merge",
        source_run_id="run-1",
    ))

    # Mock _promote_internal to fail
    async def mock_promote_internal(*args, **kwargs):
        raise RuntimeError("Forced promotion failure")
    monkeypatch.setattr(promotion_engine, "_promote_internal", mock_promote_internal)

    with pytest.raises(RuntimeError) as excinfo:
        await promotion_engine.evaluate_and_promote_cohort(
            repo_id=repo.id,
            proposal_ids=[p1.proposal_id, p2.proposal_id],
            document_store=doc_store,
        )
    assert "Forced promotion failure" in str(excinfo.value)

    # Verify that the original states are restored
    db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    assert p1_db.status == ProposalStatus.PENDING
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.PENDING
    
    # Check contention metadata restored
    cont_meta1 = proposal_service.get_contention_metadata(p1.proposal_id)
    assert cont_meta1 is not None
    assert cont_meta1.defer_evaluation is True
    assert cont_meta1.contention_policy == "semantic_merge"

    cont_meta2 = proposal_service.get_contention_metadata(p2.proposal_id)
    assert cont_meta2 is not None
    assert cont_meta2.defer_evaluation is True
    assert cont_meta2.contention_policy == "semantic_merge"


@pytest.mark.asyncio
async def test_cohort_merge_preserves_module_docstring_and_dunder_doc(
    promotion_engine, db_session, repo_service, proposal_service, snapshot_service
):
    import ast

    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo-docstring")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = (
        '"""Runtime orchestration module."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "class Runtime:\n"
        "    def __init__(self) -> None:\n"
        "        self.ready = True\n"
    )
    doc_store.apply_full_file("runtime.py", base_file, "init")
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    p1 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent-b",
            change_type="full_file",
            change_content=(
                base_file
                + "\n"
                + "    def get_name(self) -> str:\n"
                + "        return 'runtime'\n"
            ),
            affected_paths=["runtime.py"],
            defer_evaluation=True,
            contention_group_id="group-doc",
            contention_policy="semantic_merge",
            source_run_id="run-doc",
        ),
    )

    p2 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent-c",
            change_type="full_file",
            change_content=(
                base_file
                + "\n"
                + "    def get_pid(self) -> int:\n"
                + "        return os.getpid()\n"
            ),
            affected_paths=["runtime.py"],
            defer_evaluation=True,
            contention_group_id="group-doc",
            contention_policy="semantic_merge",
            source_run_id="run-doc",
        ),
    )

    res = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[p1.proposal_id, p2.proposal_id],
        document_store=doc_store,
    )
    assert res.promotion is not None
    assert set(json.loads(res.promotion.included_proposals)) == {p1.proposal_id, p2.proposal_id}

    repo_refreshed = db_session.get(Repository, repo.id)
    head = db_session.get(AetherHead, repo_refreshed.head_id)
    assert head is not None
    merged_content = snapshot_service.get_file_content(head.snapshot_id, "runtime.py")
    assert merged_content is not None

    assert merged_content.splitlines()[0] == '"""Runtime orchestration module."""'
    namespace: dict[str, object] = {}
    exec(compile(merged_content, "runtime.py", "exec"), namespace)
    assert namespace["__doc__"] == "Runtime orchestration module."

    base_tree = ast.parse(base_file)
    merged_tree = ast.parse(merged_content)
    for node in merged_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Runtime":
            node.body = [
                member
                for member in node.body
                if not (
                    isinstance(member, ast.FunctionDef)
                    and member.name in {"get_name", "get_pid"}
                )
            ]
    assert ast.dump(merged_tree) == ast.dump(base_tree)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["untouched_method_order", "missing_eof_newline"])
async def test_promoted_merge_preserves_untouched_method_order_and_missing_eof_newline(
    case, promotion_engine, db_session, repo_service, proposal_service, snapshot_service
):
    import ast

    promotion_engine.semantic_merge_enabled = True
    if case == "untouched_method_order":
        base = (
            '"""Runtime module."""\n'
            "import os\n\n"
            "class Runtime:\n"
            "    def z_decorator(fn):\n"
            "        return fn\n\n"
            "    @z_decorator\n"
            "    def alpha(self):\n"
            "        return 1\n"
        )
    else:
        base = (
            '"""Runtime module."""\n'
            "import os\n\n"
            "class Runtime:\n"
            "    def __init__(self):\n"
            "        self.ready = True"
        )
    contents = [
        base.rstrip("\n") + '\n\n    def get_name(self):\n        return "runtime"\n',
        base.rstrip("\n") + '\n\n    def get_pid(self):\n        return os.getpid()\n',
    ]

    repo = repo_service.create_repository(f"docstring-launch-review-{case}")
    store = DocumentStore(promotion_engine.storage_path)
    store.apply_full_file("runtime.py", base, "init")
    snap = await snapshot_service.create_snapshot(repo.id, store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    proposals = [
        await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id=f"agent-{index}",
                change_type="full_file",
                change_content=content,
                affected_paths=["runtime.py"],
                defer_evaluation=True,
                contention_group_id="review-group",
                contention_policy="semantic_merge",
                source_run_id="review-run",
            ),
        )
        for index, content in enumerate(contents)
    ]

    result = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[proposal.proposal_id for proposal in proposals],
        document_store=store,
    )
    assert result.promotion is not None
    assert set(json.loads(result.promotion.included_proposals)) == {
        proposal.proposal_id for proposal in proposals
    }

    head = db_session.get(AetherHead, db_session.get(Repository, repo.id).head_id)
    assert head is not None
    merged = snapshot_service.get_file_content(head.snapshot_id, "runtime.py")
    assert merged is not None
    assert merged.splitlines()[0] == base.splitlines()[0]

    namespace: dict[str, object] = {}
    exec(compile(merged, "runtime.py", "exec"), namespace)
    assert namespace["__doc__"] == "Runtime module."

    merged_tree = ast.parse(merged)
    runtime = next(node for node in merged_tree.body if isinstance(node, ast.ClassDef))
    runtime.body = [
        node
        for node in runtime.body
        if not (isinstance(node, ast.FunctionDef) and node.name in {"get_name", "get_pid"})
    ]
    assert ast.dump(merged_tree) == ast.dump(ast.parse(base))


@pytest.mark.asyncio
async def test_promoted_snapshot_rejects_compiler_invalid_merge(
    promotion_engine, db_session, repo_service, proposal_service, snapshot_service
):
    base = '"""Runtime module."""\nclass Runtime:\n    def existing(self):\n        return 0\n'
    left = base + "\n    def z_method(self):\n        return 1\n\n    global counter\n"
    right = base + "\n    def a_method(self):\n        return 2\n\n    counter = 1\n"
    for source in (base, left, right):
        compile(source, "input.py", "exec", dont_inherit=True)

    promotion_engine.semantic_merge_enabled = True
    repo = repo_service.create_repository("compiler-validation-review")
    store = DocumentStore(promotion_engine.storage_path)
    store.apply_full_file("runtime.py", base, "init")
    snap = await snapshot_service.create_snapshot(repo.id, store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    proposals = [
        await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id=f"agent-{index}",
                change_type="full_file",
                change_content=source,
                affected_paths=["runtime.py"],
                defer_evaluation=True,
                contention_group_id="review-group",
                contention_policy="semantic_merge",
                source_run_id="review-run",
            ),
        )
        for index, source in enumerate((left, right))
    ]

    result = await promotion_engine.evaluate_and_promote_cohort(
        repo_id=repo.id,
        proposal_ids=[proposal.proposal_id for proposal in proposals],
        document_store=store,
    )
    if result.promotion is not None:
        head = db_session.get(AetherHead, db_session.get(Repository, repo.id).head_id)
        assert head is not None
        merged = snapshot_service.get_file_content(head.snapshot_id, "runtime.py")
        assert merged is not None
        compile(merged, "runtime.py", "exec", dont_inherit=True)

    events = db_session.exec(
        select(SystemMetricEvent).where(
            SystemMetricEvent.repo_id == repo.id,
            SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK,
        )
    ).all()
    assert any(event.event_reason == "RECONSTRUCTION_FAILURE" for event in events)
