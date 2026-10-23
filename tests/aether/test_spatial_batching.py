import pytest
from sqlmodel import Session, create_engine, SQLModel, select
import json

from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    SystemMetricEvent,
    SystemMetricEventType,
    GateConfig,
    GateStatus,
    GateResult,
    Snapshot,
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine, PromotionCandidate

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

def test_descending_line_order_splicing():
    """Verify coordinate stability under descending line-order splicing."""
    store = DocumentStore("test-replica")
    store.apply_full_file(
        "math_utils.py",
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n",
        "init",
    )

    diff1 = make_diff("math_utils.py", 2, 1, 2, 2, ["-    return a + b", "+    print('adding')", "+    return a + b"])
    diff2 = make_diff("math_utils.py", 5, 1, 5, 2, ["-    return a * b", "+    print('multiplying')", "+    return a * b"])

    # Apply both as a batch
    affected, conflicts = store.apply_diff_batch([(diff1, "prop1"), (diff2, "prop2")])
    
    assert "math_utils.py" in affected
    content = store.get_document("math_utils.py").get_content()
    expected = "def add(a, b):\n    print('adding')\n    return a + b\n\ndef multiply(a, b):\n    print('multiplying')\n    return a * b\n"
    assert content.strip() == expected.strip()

def test_spatial_overlap_detection():
    """Verify that spatial overlap is correctly detected and rejected."""
    store = DocumentStore("test-replica")
    store.apply_full_file("math_utils.py", "a = 1\nb = 2\nc = 3\n", "init")

    diff1 = make_diff("math_utils.py", 1, 2, 1, 2, ["-a = 1", "-b = 2", "+a = 10", "+b = 20"])
    diff2 = make_diff("math_utils.py", 2, 2, 2, 2, ["-b = 2", "-c = 3", "+b = 20", "+c = 30"])

    with pytest.raises(ValueError, match="Spatial overlap detected"):
        store.apply_diff_batch([(diff1, "prop1"), (diff2, "prop2")])

@pytest.mark.asyncio
async def test_full_file_wildcard_fallback(db_session, proposal_service, repo_service):
    """Verify that full_file changes fallback to __raw__ wildcards."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "print('old')\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)

    # Create full_file proposal
    response = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="full_file",
            change_content="print('new content')",
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )
    p1 = db_session.get(Proposal, response.proposal_id)
    symbols = proposal_service._decode_affected_symbols(p1.affected_symbols)
    assert "__raw__" in symbols.get("main.py", set())

@pytest.mark.asyncio
async def test_multi_node_diff_wildcard_fallback(db_session, proposal_service, repo_service):
    """Verify that diffs spanning multiple locations fallback to __raw__ wildcards."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file(
        "main.py",
        "def my_func():\n    a = 1\n\ndef other_func():\n    b = 2\n",
        "init",
    )
    proposal_service.set_document_store(repo.id, doc_store)

    # Create a single hunk spanning from my_func to other_func
    diff_text = make_diff(
        "main.py",
        2,
        4,
        2,
        4,
        [
            "-    a = 1",
            "-",
            "-def other_func():",
            "-    b = 2",
            "+    a = 10",
            "+",
            "+def other_func():",
            "+    b = 20",
        ],
    )

    response = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="unified_diff",
            change_content=diff_text,
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )
    p1 = db_session.get(Proposal, response.proposal_id)
    symbols = proposal_service._decode_affected_symbols(p1.affected_symbols)
    assert "__raw__" in symbols.get("main.py", set())


@pytest.mark.asyncio
async def test_proposal_local_failure_isolation(db_session, promotion_engine, proposal_service, repo_service):
    """Verify proposal-local failure isolation (one bad proposal does not poison the batch)."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "x = 1\ny = 2\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)

    # Create 3 proposals:
    # P1: valid diff
    diff1 = make_diff("main.py", 1, 1, 1, 1, ["-x = 1", "+x = 10"])
    resp1 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="unified_diff",
            change_content=diff1,
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )

    # P2: malformed diff (invalid hunk start coordinate 0)
    diff2 = make_diff("main.py", 0, 1, 0, 1, ["-y = 2", "+y = 20"])
    resp2 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent2",
            change_type="unified_diff",
            change_content=diff2,
            affected_paths=["main.py"],
            confidence_score=0.8,
        )
    )

    # P3: valid diff
    diff3 = make_diff("main.py", 2, 1, 2, 1, ["-y = 2", "+y = 20"])
    resp3 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent3",
            change_type="unified_diff",
            change_content=diff3,
            affected_paths=["main.py"],
            confidence_score=0.7,
        )
    )

    p1 = db_session.get(Proposal, resp1.proposal_id)
    p2 = db_session.get(Proposal, resp2.proposal_id)
    p3 = db_session.get(Proposal, resp3.proposal_id)

    # Evaluate the batch
    candidate = await promotion_engine.evaluate_batch([p1, p2, p3], doc_store)

    db_session.refresh(p1)
    db_session.refresh(p2)
    db_session.refresh(p3)

    assert p2.status == ProposalStatus.REJECTED
    
    # Query SystemMetricEvent for status update reason
    metrics_stmt = select(SystemMetricEvent).where(
        SystemMetricEvent.repo_id == repo.id,
        SystemMetricEvent.proposal_id == p2.id,
        SystemMetricEvent.event_type == SystemMetricEventType.STATUS_REJECTED,
    )
    metric_events = db_session.exec(metrics_stmt).all()
    assert len(metric_events) == 1
    assert "proposal_local_failure" in metric_events[0].event_reason

    assert p1.id in [p.id for p in candidate.proposals]
    assert p3.id in [p.id for p in candidate.proposals]

@pytest.mark.asyncio
async def test_sequential_degradation_evolving_head(db_session, promotion_engine, proposal_service, repo_service, gate_runner):
    """Verify that sequential degradation fallback runs correctly against the evolving head."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "def func1():\n    x = 1\n\ndef func2():\n    y = 2\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)
    
    from packages.aether.models.entities import RepoConfig
    config = RepoConfig(
        gates=[
            GateConfig(
                name="test-gate",
                command="exit 0",
                required=True,
            )
        ]
    )
    repo_service.update_repo_config(repo.id, config)

    # P1: valid
    diff1 = make_diff("main.py", 2, 1, 2, 1, ["-    x = 1", "+    x = 10"])
    resp1 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="unified_diff",
            change_content=diff1,
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )

    # P2: valid
    diff2 = make_diff("main.py", 5, 1, 5, 1, ["-    y = 2", "+    y = 20"])
    resp2 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent2",
            change_type="unified_diff",
            change_content=diff2,
            affected_paths=["main.py"],
            confidence_score=0.8,
        )
    )

    p1 = db_session.get(Proposal, resp1.proposal_id)
    p2 = db_session.get(Proposal, resp2.proposal_id)

    original_run_all_gates = gate_runner.run_all_gates
    
    async def mock_run_all_gates(snapshot_id, gates, workspace, **kwargs):
        repo_db = db_session.get(Repository, repo.id)
        if repo_db.head_sequence == 0:
            snap = db_session.get(Snapshot, snapshot_id)
            included = json.loads(snap.included_proposals) if snap.included_proposals else []
            if len(included) > 1:
                return [GateResult(gate_name="test-gate", status=GateStatus.FAIL, exit_code=1)]
        return [GateResult(gate_name="test-gate", status=GateStatus.PASS, exit_code=0)]

    gate_runner.run_all_gates = mock_run_all_gates

    candidate = await promotion_engine.evaluate_batch([p1, p2], doc_store)

    assert len(candidate.proposals) == 0

    db_session.refresh(p1)
    db_session.refresh(p2)

    assert p1.status == ProposalStatus.PROMOTED
    assert p2.status == ProposalStatus.PROMOTED

    metrics_stmt = select(SystemMetricEvent).where(
        SystemMetricEvent.repo_id == repo.id,
        SystemMetricEvent.event_type == SystemMetricEventType.BATCH_GATE_FAIL,
    )
    metric_events = db_session.exec(metrics_stmt).all()
    assert len(metric_events) > 0

    gate_runner.run_all_gates = original_run_all_gates

@pytest.mark.asyncio
async def test_superseded_vs_rejected_attribution(db_session, promotion_engine, proposal_service, repo_service, gate_runner):
    """Verify strict separation of SUPERSEDED vs REJECTED status attribution."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "x = 1\ny = 2\nz = 3\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)

    diff1 = make_diff("main.py", 1, 1, 1, 1, ["-x = 1", "+x = 10"])
    resp1 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="unified_diff",
            change_content=diff1,
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )

    diff2 = make_diff("main.py", 1, 1, 1, 1, ["-x = 1", "+x = 20"])
    resp2 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent2",
            change_type="unified_diff",
            change_content=diff2,
            affected_paths=["main.py"],
            confidence_score=0.8,
        )
    )

    p1 = db_session.get(Proposal, resp1.proposal_id)
    p2 = db_session.get(Proposal, resp2.proposal_id)

    # Delete any conflict sets created by submission, to treat them as non-conflicting proposals
    # that overlap spatially.
    from packages.aether.models.entities import ConflictSet
    for cs in db_session.exec(select(ConflictSet).where(ConflictSet.repo_id == repo.id)).all():
        db_session.delete(cs)
    db_session.commit()

    promotions = await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2)

    db_session.refresh(p1)
    db_session.refresh(p2)

    assert p1.status == ProposalStatus.PROMOTED
    assert p2.status == ProposalStatus.SUPERSEDED
    
    # Query SystemMetricEvent for status update reason
    metrics_stmt = select(SystemMetricEvent).where(
        SystemMetricEvent.repo_id == repo.id,
        SystemMetricEvent.proposal_id == p2.id,
        SystemMetricEvent.event_type == SystemMetricEventType.STATUS_SUPERSEDED,
    )
    metric_events = db_session.exec(metrics_stmt).all()
    assert len(metric_events) == 1
    assert metric_events[0].event_reason == "spatial_batching_exclusion"


@pytest.mark.asyncio
async def test_raw_wildcard_conflict_detection(db_session, proposal_service, repo_service):
    """Verify that a __raw__ wildcard lock conflicts with any specific symbol edits on the same file."""
    repo = repo_service.create_repository(name="test-repo")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "class Foo:\n    pass\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)

    # P1: full_file change (produces __raw__ symbol)
    resp1 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="full_file",
            change_content="class Foo:\n    # comment\n    pass\n",
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
    )

    # P2: AST-level edit in class Foo (produces CLASS:Foo symbol)
    diff2 = make_diff("main.py", 2, 1, 2, 1, ["-    pass", "+    pass # modified"])
    resp2 = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent2",
            change_type="unified_diff",
            change_content=diff2,
            affected_paths=["main.py"],
            confidence_score=0.8,
        )
    )

    p1 = db_session.get(Proposal, resp1.proposal_id)
    p2 = db_session.get(Proposal, resp2.proposal_id)

    p1_paths = proposal_service._normalize_paths(proposal_service._safe_json_loads(p1.affected_paths, []))
    p2_paths = proposal_service._normalize_paths(proposal_service._safe_json_loads(p2.affected_paths, []))
    p1_symbols = proposal_service._decode_affected_symbols(p1.affected_symbols)
    p2_symbols = proposal_service._decode_affected_symbols(p2.affected_symbols)

    # Check p1 has __raw__
    assert "main.py" in p1_symbols
    assert "__raw__" in p1_symbols["main.py"]

    # Check p2 has CLASS:Foo::__head__
    assert "main.py" in p2_symbols
    assert "CLASS:Foo::__head__" in p2_symbols["main.py"]

    # Verify conflict
    assert proposal_service._has_semantic_conflict(p1_paths, p1_symbols, p2_paths, p2_symbols) is True


@pytest.mark.asyncio
async def test_hunk_coordinate_validation_rules(db_session, proposal_service, repo_service, promotion_engine):
    """Regression test for hunk coordinate validation rules:
    1. Accept: old_start=0, old_count=0 (new-file additions, with wildcard fallback)
    2. Reject: old_start=0, old_count>0 (invalid coordinate usage)
    3. Reject: old_start<0 (malformed hunk header parse failure)
    """
    repo = repo_service.create_repository(name="test-hunk-rules")
    doc_store = repo_service.get_document_store(repo.id)
    doc_store.apply_full_file("main.py", "x = 1\n", "init")
    proposal_service.set_document_store(repo.id, doc_store)

    # 1. Accept: old_start=0, old_count=0
    diff_accept = (
        "diff --git a/new_file.py b/new_file.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+y = 2\n"
    )
    resp_accept = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent1",
            change_type="unified_diff",
            change_content=diff_accept,
            affected_paths=["new_file.py"],
            confidence_score=0.9,
        )
    )
    p_accept = db_session.get(Proposal, resp_accept.proposal_id)
    assert p_accept.status == ProposalStatus.PENDING
    symbols = proposal_service._decode_affected_symbols(p_accept.affected_symbols)
    assert "__file__" in symbols.get("new_file.py", set()) or "__raw__" in symbols.get("new_file.py", set())

    # 2. Reject: old_start=0, old_count>0
    diff_reject_zero_count = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -0,1 +0,1 @@\n"
        "+y = 2\n"
    )
    resp_reject_zero_count = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent2",
            change_type="unified_diff",
            change_content=diff_reject_zero_count,
            affected_paths=["main.py"],
            confidence_score=0.8,
        )
    )
    p_reject_zero_count = db_session.get(Proposal, resp_reject_zero_count.proposal_id)
    assert p_reject_zero_count.status == ProposalStatus.PENDING
    symbols_reject = proposal_service._decode_affected_symbols(p_reject_zero_count.affected_symbols)
    assert "__file__" in symbols_reject.get("main.py", set())
    assert "__raw__" in symbols_reject.get("main.py", set())

    # Verify that the fallback reason in the system metrics is recorded as "invalid_hunk_start"
    stmt = select(SystemMetricEvent).where(
        SystemMetricEvent.repo_id == repo.id,
        SystemMetricEvent.proposal_id == p_reject_zero_count.id,
        SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK,
    )
    metric_events = db_session.exec(stmt).all()
    assert len(metric_events) == 1
    assert "invalid_hunk_start" in metric_events[0].event_reason

    # Verify that the promotion engine also rejects it during batch evaluation
    await promotion_engine.evaluate_batch([p_reject_zero_count], doc_store)
    db_session.refresh(p_reject_zero_count)
    assert p_reject_zero_count.status == ProposalStatus.REJECTED

    # 3. Reject: old_start < 0
    diff_reject_neg = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ --1,1 +1,1 @@\n"
        "+y = 2\n"
    )
    try:
        from packages.aether.crdt.document_store import parse_unified_diff
        parse_unified_diff(diff_reject_neg)
        parsed_ok = True
    except ValueError:
        parsed_ok = False
    assert parsed_ok is False

    # Submit should raise ValueError directly due to parse failure
    with pytest.raises(ValueError, match="Malformed hunk header"):
        await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent3",
                change_type="unified_diff",
                change_content=diff_reject_neg,
                affected_paths=["main.py"],
                confidence_score=0.7,
            )
        )
