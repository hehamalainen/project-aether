"""Unit tests for the Step 3 Class 1 Structural AST-Merge Promotion Prototype."""

import os
import json
import pytest
import asyncio
from pathlib import Path
from sqlmodel import Session, create_engine, SQLModel, select

from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    Promotion,
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
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
async def test_semantic_merge_disabled_behavior(promotion_engine, db_session, repo_service, proposal_service, snapshot_service, monkeypatch):
    # Disable semantic merge explicitly
    monkeypatch.setenv("AETHER_SEMANTIC_MERGE_ENABLED", "false")
    promotion_engine.semantic_merge_enabled = False

    # Create repo
    repo = repo_service.create_repository("test-repo")

    # Initial document store setup
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = 1\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    # Create base snapshot
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Proposal 1: add get_name
    diff_a = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_name(self):",
            "         return 'Alice'",
        ]
    )
    p1 = Proposal(
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PENDING,
    )

    # Proposal 2: add get_email (overlaps with CLASS:User)
    diff_b = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_email(self):",
            "         return 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_email"]),
        status=ProposalStatus.PENDING,
    )

    db_session.add(p1)
    db_session.add(p2)
    db_session.commit()

    # Run promotion loop
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2)

    # Reload from DB
    db_session.refresh(p1)
    db_session.refresh(p2)

    # One should be promoted, the other superseded due to spatial exclusion
    states = {p1.status, p2.status}
    assert ProposalStatus.PROMOTED in states
    assert ProposalStatus.SUPERSEDED in states


@pytest.mark.asyncio
async def test_semantic_merge_enabled_success(promotion_engine, db_session, repo_service, proposal_service, snapshot_service, monkeypatch):
    # Enable semantic merge explicitly
    monkeypatch.setenv("AETHER_SEMANTIC_MERGE_ENABLED", "true")
    promotion_engine.semantic_merge_enabled = True

    repo = repo_service.create_repository("test-repo")

    # Initial document store setup
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = 1\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Proposal 1: add get_name
    diff_a = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_name(self):",
            "         return 'Alice'",
        ]
    )
    p1 = Proposal(
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PENDING,
    )

    # Proposal 2: add get_email
    diff_b = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_email(self):",
            "         return 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_email"]),
        status=ProposalStatus.PENDING,
    )

    db_session.add(p1)
    db_session.add(p2)
    db_session.commit()

    # Run promotion loop
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2)

    db_session.refresh(p1)
    db_session.refresh(p2)

    # BOTH should be promoted!
    assert p1.status == ProposalStatus.PROMOTED
    assert p2.status == ProposalStatus.PROMOTED

    db_session.refresh(repo)
    # Verify merged document contents in the repo head
    from packages.aether.models.entities import AetherHead
    head_record = db_session.get(AetherHead, repo.head_id)
    latest_snap = snapshot_service.get_snapshot(head_record.snapshot_id)
    merged_content = snapshot_service.get_file_content(latest_snap.id, "user.py")
    assert merged_content is not None
    assert "def get_name(self):" in merged_content
    assert "def get_email(self):" in merged_content


@pytest.mark.asyncio
async def test_concurrency_lock_prevents_deadlock(promotion_engine, db_session, repo_service, snapshot_service):
    repo = repo_service.create_repository("test-repo")
    doc_store = DocumentStore(promotion_engine.storage_path)
    base_file = "class User:\n    pass\n"
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    db_session.add(repo)
    db_session.commit()

    # Fire concurrent calls to verify the asyncio.Lock blocks and sequences them without deadlock
    tasks = [
        promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2),
        promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2)
    ]
    
    results = await asyncio.gather(*tasks)
    # The tasks must finish cleanly without raising exceptions or blocking indefinitely
    assert isinstance(results[0], list)
    assert isinstance(results[1], list)


def test_affected_symbols_canonical_matching_logic(promotion_engine):
    import json
    p1 = Proposal(
        repo_id="test-repo",
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"])
    )
    p2 = Proposal(
        repo_id="test-repo",
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_email"])
    )
    # 1. Separate methods in same class -> True
    assert promotion_engine._is_structurally_mergeable(p1, p2) is True

    # 2. Overlapping methods -> False
    p3 = Proposal(
        repo_id="test-repo",
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"])
    )
    assert promotion_engine._is_structurally_mergeable(p1, p3) is False

    # 3. Global function (non-whitelisted) -> False
    p4 = Proposal(
        repo_id="test-repo",
        affected_symbols=json.dumps(["user.py::FUNC:helper"])
    )
    assert promotion_engine._is_structurally_mergeable(p1, p4) is False

    # 4. Empty/Fallback symbols -> False
    p5 = Proposal(
        repo_id="test-repo",
        affected_symbols=json.dumps([])
    )
    assert promotion_engine._is_structurally_mergeable(p1, p5) is False


def test_find_temporal_conflicts_enforces_touched_symbols(promotion_engine, db_session, repo_service):
    import json
    from packages.aether.models.entities import Promotion
    
    # Create the repository to satisfy foreign key constraints
    repo = repo_service.create_repository("test-repo")
    
    # Setup promotion and proposal
    p_promoted = Proposal(
        id="prop_promoted",
        repo_id=repo.id,
        agent_id="agent-1",
        task_id="task-1",
        base_head_sequence=0,
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PROMOTED
    )
    db_session.add(p_promoted)
    db_session.commit()
    
    # Save a promotion record including this proposal
    prom = Promotion(
        id="prom_1",
        repo_id=repo.id,
        from_head_id="head-1",
        to_head_id="head-2",
        to_head_sequence=1,
        included_proposals=json.dumps(["prop_promoted"]),
        excluded_proposals="[]",
        evidence="{}"
    )
    db_session.add(prom)
    db_session.commit()
    
    # Proposal p_conflict touches the same symbol -> should conflict
    p_conflict = Proposal(
        id="prop_conflict",
        repo_id=repo.id,
        agent_id="agent-1",
        task_id="task-1",
        base_head_sequence=0,
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PENDING
    )
    conflicts = promotion_engine._find_temporal_conflicts(repo.id, p_conflict)
    assert len(conflicts) == 1
    assert conflicts[0].id == "prop_promoted"
    
    # Proposal p_no_conflict touches a different symbol -> should NOT conflict
    p_no_conflict = Proposal(
        id="prop_no_conflict",
        repo_id=repo.id,
        agent_id="agent-1",
        task_id="task-1",
        base_head_sequence=0,
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:get_email"]),
        status=ProposalStatus.PENDING
    )
    conflicts_no = promotion_engine._find_temporal_conflicts(repo.id, p_no_conflict)
    assert len(conflicts_no) == 0

@pytest.mark.asyncio
async def test_synthesis_merge_enabled_success(promotion_engine, db_session, repo_service):
    # Setup repo and files
    repo = repo_service.create_repository("test-repo-synthesis")
    repo_root = promotion_engine.storage_path
    
    # Enable semantic merge and synthesis worker
    promotion_engine.semantic_merge_enabled = True
    promotion_engine.synthesis_worker_enabled = True
    
    # Initialize document store
    from packages.aether.crdt.document_store import DocumentStore
    doc_store = DocumentStore(repo_root)
    
    # 1. Base content of user.py
    base_content = "class User:\n    def update_profile(self):\n        pass\n"
    doc_store.apply_full_file("user.py", base_content, "init")
    
    # Create base head in snapshot service
    head = await promotion_engine.snapshot_service.create_snapshot(
        repo.id, doc_store, included_proposals=[], created_by="base"
    )
    repo.head_id = head.id
    db_session.add(repo)
    db_session.commit()
    
    # Proposal 1 (touches update_profile and lands first)
    diff_a = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.name = 'Alice'",
        ]
    )
    p1 = Proposal(
        id="prop_left",
        repo_id=repo.id,
        agent_id="agent-1",
        task_id="task-1",
        base_head_id=head.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )
    db_session.add(p1)
    db_session.commit()
    
    # Promote Proposal 1 first
    await promotion_engine.process_pending_proposals(repo.id, doc_store)
    
    # Verify Proposal 1 is promoted
    p1_refreshed = db_session.get(Proposal, p1.id)
    assert p1_refreshed.status == ProposalStatus.PROMOTED
    
    # Proposal 2 (touches the SAME update_profile but was based on the old base head ID)
    diff_b = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.email = 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        id="prop_right",
        repo_id=repo.id,
        agent_id="agent-2",
        task_id="task-2",
        base_head_id=head.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )
    db_session.add(p2)
    db_session.commit()
    
    # Now run promotion loop for Proposal 2.
    promotions = await promotion_engine.process_pending_proposals(repo.id, doc_store)
    
    # Check that Proposal 2 got promoted successfully via synthesis merge!
    p2_refreshed = db_session.get(Proposal, p2.id)
    assert p2_refreshed.status == ProposalStatus.PROMOTED
    
    # Verify the merged content in document store has BOTH self.name and self.email!
    final_content = doc_store.get_file_content("user.py")
    assert "self.name = 'Alice'" in final_content
    assert "self.email = 'alice@example.com'" in final_content
