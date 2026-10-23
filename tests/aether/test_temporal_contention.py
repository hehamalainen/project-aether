import json
import pytest
import asyncio
from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session
from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    ProposalContention,
    ContentionWinner,
)
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.promotion_engine import PromotionEngine, ContentionWinnerExistsError
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.event_bus import EventBus
from packages.aether.crdt.document_store import DocumentStore

@pytest.fixture
def test_db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()

@pytest.fixture
def event_bus():
    return EventBus()

@pytest.fixture
def proposal_service(test_db_session, event_bus):
    return ProposalService(test_db_session, event_bus, {})

@pytest.fixture
def snapshot_service(test_db_session, event_bus, tmp_path):
    return SnapshotService(test_db_session, event_bus, tmp_path)

@pytest.fixture
def gate_runner(test_db_session, event_bus):
    return GateRunner(test_db_session, event_bus)

@pytest.fixture
def promotion_engine(test_db_session, event_bus, proposal_service, snapshot_service, gate_runner, tmp_path):
    return PromotionEngine(
        test_db_session,
        event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        tmp_path
    )

@pytest.mark.asyncio
async def test_contention_group_sequential_submissions_exactly_one_winner(test_db_session, proposal_service, promotion_engine, tmp_path):
    # Setup repo
    repo = Repository(id="repo_1", name="test_repo", workspace_path=str(tmp_path / "workspace_1"))
    test_db_session.add(repo)
    test_db_session.commit()

    # 1. First proposal in contention mode
    p1_create = ProposalCreate(
        agent_id="agent_1",
        change_content="print('winner')",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_1",
        contention_policy="exactly_one_winner"
    )
    p1 = await proposal_service.submit_proposal("repo_1", p1_create)
    assert p1.status == ProposalStatus.PENDING

    # Promote p1
    doc_store = DocumentStore("repo_1", base_path=str(tmp_path / "workspace_1"))
    promotions = await promotion_engine.process_pending_proposals("repo_1", doc_store)
    assert len(promotions) == 1
    
    # Reload and assert p1 is promoted
    test_db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    assert p1_db.status == ProposalStatus.PROMOTED

    # 2. Second proposal with same group ID
    p2_create = ProposalCreate(
        agent_id="agent_2",
        change_content="print('loser')",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_1",
        contention_policy="exactly_one_winner"
    )
    p2 = await proposal_service.submit_proposal("repo_1", p2_create)
    assert p2.status == ProposalStatus.PENDING

    # Run promotion - p2 should be superseded
    promotions_2 = await promotion_engine.process_pending_proposals("repo_1", doc_store)
    assert len(promotions_2) == 0

    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.SUPERSEDED

    # Verify provenance for p2 contains winner info
    prov = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov is not None
    assert prov.event_reason == "temporal_semantic_conflict"
    assert prov.winner_proposal_id == p1.proposal_id
    assert prov.contention_group_id == "group_1"

@pytest.mark.asyncio
async def test_contention_group_simultaneous_submissions_exactly_one_winner(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_2", name="test_repo_2", workspace_path=str(tmp_path / "workspace_2"))
    test_db_session.add(repo)
    test_db_session.commit()

    # Submit 3 proposals simultaneously
    p_creates = [
        ProposalCreate(
            agent_id=f"agent_{i}",
            change_content=f"print('prop_{i}')",
            affected_paths=["tools/jsontool.py"],
            contention_mode=True,
            contention_group_id="group_2",
            contention_policy="exactly_one_winner"
        )
        for i in range(3)
    ]

    p_ids = []
    for pc in p_creates:
        p = await proposal_service.submit_proposal("repo_2", pc)
        p_ids.append(p.proposal_id)

    # Run promotion engine
    doc_store = DocumentStore("repo_2", base_path=str(tmp_path / "workspace_2"))
    await promotion_engine.process_pending_proposals("repo_2", doc_store)

    test_db_session.expire_all()
    proposals = [proposal_service.get_proposal(pid) for pid in p_ids]
    
    promoted = [p for p in proposals if p.status == ProposalStatus.PROMOTED]
    superseded = [p for p in proposals if p.status == ProposalStatus.SUPERSEDED]

    assert len(promoted) == 1
    assert len(superseded) == 2
    assert all(p.status != ProposalStatus.PENDING for p in proposals)
    assert all(p.status != ProposalStatus.EVALUATING for p in proposals)

@pytest.mark.asyncio
async def test_non_contention_sequential_same_file_allows_multiple_promotions(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_3", name="test_repo_3", workspace_path=str(tmp_path / "workspace_3"))
    test_db_session.add(repo)
    test_db_session.commit()

    # Two proposals to same file, no contention mode
    p1 = await proposal_service.submit_proposal("repo_3", ProposalCreate(
        agent_id="agent_1", change_content="print('1')", affected_paths=["file.py"]
    ))
    doc_store = DocumentStore("repo_3", base_path=str(tmp_path / "workspace_3"))
    await promotion_engine.process_pending_proposals("repo_3", doc_store)

    p2 = await proposal_service.submit_proposal("repo_3", ProposalCreate(
        agent_id="agent_2", change_content="print('2')", affected_paths=["file.py"]
    ))
    await promotion_engine.process_pending_proposals("repo_3", doc_store)

    test_db_session.expire_all()
    assert proposal_service.get_proposal(p1.proposal_id).status == ProposalStatus.PROMOTED
    assert proposal_service.get_proposal(p2.proposal_id).status == ProposalStatus.PROMOTED

@pytest.mark.asyncio
async def test_contention_group_does_not_depend_on_pending_overlap(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_4", name="test_repo_4", workspace_path=str(tmp_path / "workspace_4"))
    test_db_session.add(repo)
    test_db_session.commit()

    # Promote p1 first
    p1 = await proposal_service.submit_proposal("repo_4", ProposalCreate(
        agent_id="agent_1", change_content="win", affected_paths=["file.py"], contention_mode=True, contention_group_id="g4"
    ))
    doc_store = DocumentStore("repo_4", base_path=str(tmp_path / "workspace_4"))
    await promotion_engine.process_pending_proposals("repo_4", doc_store)

    # Submit p2 when p1 is already promoted (no pending overlap)
    p2 = await proposal_service.submit_proposal("repo_4", ProposalCreate(
        agent_id="agent_2", change_content="lose", affected_paths=["file.py"], contention_mode=True, contention_group_id="g4"
    ))
    await promotion_engine.process_pending_proposals("repo_4", doc_store)

    test_db_session.expire_all()
    assert proposal_service.get_proposal(p1.proposal_id).status == ProposalStatus.PROMOTED
    assert proposal_service.get_proposal(p2.proposal_id).status == ProposalStatus.SUPERSEDED

@pytest.mark.asyncio
async def test_contention_provenance_available_for_losing_siblings(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_5", name="test_repo_5", workspace_path=str(tmp_path / "workspace_5"))
    test_db_session.add(repo)
    test_db_session.commit()

    p1 = await proposal_service.submit_proposal("repo_5", ProposalCreate(
        agent_id="agent_1", change_content="win", affected_paths=["file.py"], contention_mode=True, contention_group_id="g5"
    ))
    doc_store = DocumentStore("repo_5", base_path=str(tmp_path / "workspace_5"))
    await promotion_engine.process_pending_proposals("repo_5", doc_store)

    p2 = await proposal_service.submit_proposal("repo_5", ProposalCreate(
        agent_id="agent_2", change_content="lose", affected_paths=["file.py"], contention_mode=True, contention_group_id="g5"
    ))
    await promotion_engine.process_pending_proposals("repo_5", doc_store)

    prov = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov is not None
    assert prov.event_reason == "temporal_semantic_conflict"
    assert prov.winner_proposal_id == p1.proposal_id
    assert prov.contention_group_id == "g5"
    assert prov.target_paths == ["file.py"]
