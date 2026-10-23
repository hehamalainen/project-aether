import os
import json
import asyncio
import difflib
import pytest
from pathlib import Path
from sqlmodel import Session, create_engine, SQLModel, select

from packages.aether.models.entities import Repository, Proposal, ProposalStatus, ProposalCreate
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine

async def run_scenario_evaluation(scenario_name: str, session: Session, tmp_path: Path):
    event_bus = EventBus()
    storage_path = tmp_path / f"aether_storage_{scenario_name}"
    storage_path.mkdir(parents=True, exist_ok=True)
    
    # Load Oracle config
    fixture_dir = Path("tests/fixtures/phase4") / scenario_name
    oracle_path = fixture_dir / "oracle.json"
    with open(oracle_path) as f:
        oracle = json.load(f)
        
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, event_bus, {})
    snapshot_service = SnapshotService(session, event_bus, storage_path)
    gate_runner = GateRunner(session, event_bus)
    
    promotion_engine = PromotionEngine(
        session,
        event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        storage_path,
        gate_execution_mode="sequential",
    )
    promotion_engine.synthesis_worker_enabled = True
    promotion_engine.semantic_merge_enabled = True
    
    # 1. Create Repository
    repo = repo_service.create_repository(scenario_name)
    gate_config = dict(oracle["gate_config"]) if "gate_config" in oracle else {}
    if "golden_assertions" in oracle:
        gate_config["golden_assertions"] = oracle["golden_assertions"]
    if gate_config:
        repo.gate_config = json.dumps(gate_config)
        session.add(repo)
        session.commit()
        
    doc_store = DocumentStore(repo.id, str(storage_path))
    
    # Load Base Fixture
    base_dir = fixture_dir / "base"
    if base_dir.exists():
        for filepath in base_dir.glob("**/*"):
            if filepath.is_file():
                rel_path = filepath.relative_to(base_dir).as_posix()
                with open(filepath, "r") as f:
                    doc_store.apply_full_file(rel_path, f.read(), "base_fixture")
                    
    # Commit base files
    await snapshot_service.create_snapshot(repo.id, doc_store, "base fixture")
    
    # 2. Submit proposals
    initial_base_head_id = repo.head_id
    initial_base_head_sequence = repo.head_sequence
    initial_contents = {}
    
    agents = oracle.get("agents", [])
    for agent_def in agents:
        path = agent_def["proposal"]["path"]
        if path not in initial_contents:
            initial_contents[path] = doc_store.get_document(path).get_content() if doc_store.has_document(path) else ""
            
    for agent_def in agents:
        path = agent_def["proposal"]["path"]
        base_content = initial_contents[path]
        new_content = agent_def["proposal"]["content"]
        
        diff_lines = list(difflib.unified_diff(
            base_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3
        ))
        diff_str = f"diff --git a/{path} b/{path}\n" + "".join(diff_lines)
        
        prop_data = ProposalCreate(
            repo_id=repo.id,
            agent_id=agent_def["id"],
            task_id=f"task_{agent_def['id']}",
            confidence_score=0.9,
            affected_paths=[path],
            affected_symbols=agent_def["proposal"]["affected_symbols"],
            dependencies=[],
            change_type="unified_diff",
            change_content=diff_str,
            base_head_id=initial_base_head_id,
            base_head_sequence=initial_base_head_sequence
        )
        await proposal_service.submit_proposal(repo.id, prop_data)
        
    # Process promotions
    while True:
        batch = await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=10)
        if not batch:
            break
            
    # Return all proposals from database
    return list(session.exec(select(Proposal).where(Proposal.repo_id == repo.id)).all())

@pytest.mark.asyncio
async def test_regression_class2_subtle_reject_fail_closed(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        proposals = await run_scenario_evaluation("class2_subtle_reject", session, tmp_path)
        
        # ASSERTION at scenario level: NO proposal reaches PROMOTED
        promoted_proposals = [p for p in proposals if p.status == ProposalStatus.PROMOTED]
        assert len(promoted_proposals) == 0, f"Expected 0 promoted proposals, but found: {[p.id for p in promoted_proposals]}"

@pytest.mark.asyncio
async def test_regression_oracle_false_green_fail_closed(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        proposals = await run_scenario_evaluation("oracle_false_green", session, tmp_path)
        
        # ASSERTION at scenario level: NO proposal reaches PROMOTED
        promoted_proposals = [p for p in proposals if p.status == ProposalStatus.PROMOTED]
        assert len(promoted_proposals) == 0, f"Expected 0 promoted proposals, but found: {[p.id for p in promoted_proposals]}"
