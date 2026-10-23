import pytest
from sqlmodel import SQLModel, create_engine, Session
from packages.aether.models.entities import (
    Repository,
    ProposalStatus,
    ProposalCreate,
)
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.promotion_engine import PromotionEngine, GateTimeoutError
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
    pe = PromotionEngine(
        test_db_session,
        event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        tmp_path
    )
    # Enable semantic merge globally for tests
    pe.semantic_merge_enabled = True
    return pe

@pytest.mark.asyncio
async def test_semantic_merge_rebase_path(test_db_session, proposal_service, promotion_engine, tmp_path):
    # Setup repository
    repo = Repository(id="repo_sem", name="test_repo_sem", workspace_path=str(tmp_path / "workspace_sem"))
    test_db_session.add(repo)
    test_db_session.commit()

    # 1. First proposal with semantic_merge policy
    p1_create = ProposalCreate(
        agent_id="agent_1",
        change_content="line1_winner\nline2\nline3\nline4\nline5\n",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_sem",
        contention_policy="semantic_merge"
    )
    p1 = await proposal_service.submit_proposal("repo_sem", p1_create)
    
    doc_store = DocumentStore("repo_sem", base_path=str(tmp_path / "workspace_sem"))
    # Populate base head file
    import os
    os.makedirs(tmp_path / "workspace_sem/tools", exist_ok=True)
    with open(tmp_path / "workspace_sem/tools/jsontool.py", "w") as f:
        f.write("line1\nline2\nline3\nline4\nline5\n")
        
    promotions = await promotion_engine.process_pending_proposals("repo_sem", doc_store)
    assert len(promotions) == 1
    
    test_db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    assert p1_db.status == ProposalStatus.PROMOTED

    # 2. Sibling proposal with same contention group ID (losing the race)
    p2_create = ProposalCreate(
        agent_id="agent_2",
        change_content="line1\nline2\nline3\nline4\nline5_loser\n",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_sem",
        contention_policy="semantic_merge"
    )
    p2 = await proposal_service.submit_proposal("repo_sem", p2_create)
    assert p2.status == ProposalStatus.PENDING

    # Run promotion. Instead of SUPERSEDED, p2 should transition to REBASING (since max attempts = 2)
    promotions_2 = await promotion_engine.process_pending_proposals("repo_sem", doc_store)
    assert len(promotions_2) == 0

    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.REBASING
    assert p2_db.rebase_attempts == 1

    # Verify event reason is STALE_BASE
    prov = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov is not None
    assert prov.event_reason == "STALE_BASE"

    # Verify pending queue contains p2
    pending_queue = proposal_service.get_pending_queue("repo_sem")
    print("PENDING QUEUE SIZE:", len(pending_queue))
    for pr in pending_queue:
        print(f"PROPOSAL ID: {pr.id}, STATUS: {pr.status}")

    # Now promote the rebased proposal.
    # It must bypass ContentionWinner check and successfully update winner claim
    promotions_3 = await promotion_engine.process_pending_proposals("repo_sem", doc_store)
    assert len(promotions_3) == 1

    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.PROMOTED

    # Verify audit chain lineage
    prov_p2 = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov_p2 is not None
    assert prov_p2.event_reason == "rebased_winner_promoted"
    assert prov_p2.previous_winner_proposal_id == p1.proposal_id

@pytest.mark.asyncio
async def test_non_semantic_policy_isolation(test_db_session, proposal_service, promotion_engine, tmp_path):
    # Setup repository
    repo = Repository(id="repo_iso", name="test_repo_iso", workspace_path=str(tmp_path / "workspace_iso"))
    test_db_session.add(repo)
    test_db_session.commit()

    # 1. Promote winner
    p1_create = ProposalCreate(
        agent_id="agent_1",
        change_content="print('winner')",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_iso",
        contention_policy="exactly_one_winner" # Non-semantic!
    )
    _p1 = await proposal_service.submit_proposal("repo_iso", p1_create)
    
    doc_store = DocumentStore("repo_iso", base_path=str(tmp_path / "workspace_iso"))
    promotions = await promotion_engine.process_pending_proposals("repo_iso", doc_store)
    assert len(promotions) == 1

    # 2. Submit second proposal with same group (should be immediately superseded, not rebased)
    p2_create = ProposalCreate(
        agent_id="agent_2",
        change_content="print('loser')",
        affected_paths=["tools/jsontool.py"],
        contention_mode=True,
        contention_group_id="group_iso",
        contention_policy="exactly_one_winner"
    )
    p2 = await proposal_service.submit_proposal("repo_iso", p2_create)
    
    promotions_2 = await promotion_engine.process_pending_proposals("repo_iso", doc_store)
    assert len(promotions_2) == 0

    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    # Must still be SUPERSEDED immediately (winner-take-all physics preserved)
    assert p2_db.status == ProposalStatus.SUPERSEDED
    assert p2_db.rebase_attempts == 0

@pytest.mark.asyncio
async def test_rebase_budget_exhaustion(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_exh", name="test_repo_exh", workspace_path=str(tmp_path / "workspace_exh"), max_rebase_attempts=1)
    test_db_session.add(repo)
    test_db_session.commit()

    _p1 = await proposal_service.submit_proposal("repo_exh", ProposalCreate(
        agent_id="agent_1", change_content="print('1')", affected_paths=["a.py"],
        contention_mode=True, contention_group_id="group_exh", contention_policy="semantic_merge"
    ))
    
    doc_store = DocumentStore("repo_exh", base_path=str(tmp_path / "workspace_exh"))
    await promotion_engine.process_pending_proposals("repo_exh", doc_store)

    p2 = await proposal_service.submit_proposal("repo_exh", ProposalCreate(
        agent_id="agent_2", change_content="print('2')", affected_paths=["a.py"],
        contention_mode=True, contention_group_id="group_exh", contention_policy="semantic_merge"
    ))

    # First contention loss: rebase_attempts becomes 1. Max allowed is 1, so it transitions to REBASING.
    await promotion_engine.process_pending_proposals("repo_exh", doc_store)
    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.REBASING
    assert p2_db.rebase_attempts == 1

    # Second contention loss (simulated by evaluating/promoting p2 again when another winner claim exists)
    # Let's verify that running promotion again and hitting contention lock transitions p2 to SUPERSEDED.
    p2_db.status = ProposalStatus.PENDING # Requeue to pending to force it to run
    test_db_session.add(p2_db)
    test_db_session.commit()

    await promotion_engine.process_pending_proposals("repo_exh", doc_store)
    test_db_session.expire_all()
    p2_db = proposal_service.get_proposal(p2.proposal_id)
    assert p2_db.status == ProposalStatus.SUPERSEDED
    assert p2_db.rebase_attempts == 2 # Incremented to 2, which is > max_rebase_attempts (1)

    prov = proposal_service.get_proposal_provenance(p2.proposal_id)
    assert prov.event_reason == "rebase_budget_exhaustion"

@pytest.mark.asyncio
async def test_gate_timeout_and_evaluation_retry_budget(test_db_session, proposal_service, promotion_engine, tmp_path):
    repo = Repository(id="repo_err", name="test_repo_err", workspace_path=str(tmp_path / "workspace_err"), max_rebase_attempts=1)
    test_db_session.add(repo)
    test_db_session.commit()

    p1 = await proposal_service.submit_proposal("repo_err", ProposalCreate(
        agent_id="agent_1", change_content="print('1')", affected_paths=["a.py"],
        contention_mode=True, contention_group_id="group_err", contention_policy="semantic_merge"
    ))
    doc_store = DocumentStore("repo_err", base_path=str(tmp_path / "workspace_err"))

    # Force a GateTimeoutError during evaluation by mocking run_all_gates
    async def mock_run_all_gates(*args, **kwargs):
        raise GateTimeoutError("Mock Timeout")
    promotion_engine.gate_runner.run_all_gates = mock_run_all_gates

    # Run evaluation
    await promotion_engine.process_pending_proposals("repo_err", doc_store)
    test_db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    
    # Within budget (rebase_attempts = 1 <= max_rebase_attempts = 1): should transition to REBASING
    assert p1_db.status == ProposalStatus.REBASING
    assert p1_db.rebase_attempts == 1

    # Run again: budget should exceed
    await promotion_engine.process_pending_proposals("repo_err", doc_store)
    test_db_session.expire_all()
    p1_db = proposal_service.get_proposal(p1.proposal_id)
    assert p1_db.status == ProposalStatus.SUPERSEDED
    assert p1_db.rebase_attempts == 2

    prov = proposal_service.get_proposal_provenance(p1.proposal_id)
    assert prov.event_reason == "SUPERSEDED_GATE_TIMEOUT"
