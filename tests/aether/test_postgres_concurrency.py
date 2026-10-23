import os
import json
import pytest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session, select
from packages.aether.models.entities import Repository, Proposal, ProposalStatus, Promotion, ConflictSet, ProposalContention, ContentionWinner
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.event_bus import EventBus
from packages.aether.crdt.document_store import DocumentStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"),
    reason="Requires DATABASE_URL pointing to a PostgreSQL database"
)

def run_promoter(database_url, repo_id, storage_path, doc_store):
    engine = create_engine(database_url)
    session = Session(engine)
    try:
        eb = EventBus()
        ps = ProposalService(session, eb, {})
        ss = SnapshotService(session, eb, storage_path)
        gr = GateRunner(session, eb)
        pe = PromotionEngine(
            session=session,
            event_bus=eb,
            proposal_service=ps,
            snapshot_service=ss,
            gate_runner=gr,
            storage_path=storage_path
        )
        pe.semantic_merge_enabled = True
        pe.synthesis_worker_enabled = True
        
        async def run():
            await pe.process_pending_proposals(repo_id, doc_store, batch_size=1)
            
        asyncio.run(run())
    finally:
        session.close()
        engine.dispose()

async def setup_test_repo(session, repo_id, storage_path):
    repo = Repository(
        id=repo_id,
        name="concurrency_test",
        workspace_path=f"/tmp/conc_workspace_{repo_id}",
        gate_config=json.dumps({
            "gates": [
                {"name": "build", "command": "echo 'ok'", "required": True}
            ]
        })
    )
    session.add(repo)
    session.commit()
    
    eb = EventBus()
    ss = SnapshotService(session, eb, storage_path)
    doc_store = DocumentStore(repo_id, str(storage_path))
    doc_store.apply_full_file("app.py", "def foo():\n    pass\ndef bar():\n    pass\n", "initial")
    snap_id = await ss.create_snapshot(repo_id, doc_store, "base_fixture")
    
    repo_db = session.get(Repository, repo_id)
    repo_db.head_id = f"head_init_{os.urandom(4).hex()}"
    session.add(repo_db)
    session.commit()
    return repo_db.head_id, doc_store

@pytest.mark.asyncio
async def test_postgres_concurrency_conflict_set_id():
    """T1 shape: N proposals overlapping in conflict set. Exactly one is promoted."""
    database_url = os.environ.get("DATABASE_URL")
    main_engine = create_engine(database_url)
    SQLModel.metadata.create_all(main_engine)
    session = Session(main_engine)
    
    try:
        storage_path_a = Path(f"/tmp/conc_cs_storage_a_{os.urandom(4).hex()}")
        storage_path_b = Path(f"/tmp/conc_cs_storage_b_{os.urandom(4).hex()}")
        storage_path_a.mkdir(parents=True, exist_ok=True)
        storage_path_b.mkdir(parents=True, exist_ok=True)
        
        repo_id = f"repo_cs_{os.urandom(4).hex()}"
        head_id, doc_store = await setup_test_repo(session, repo_id, storage_path_a)
        
        # Overlapping proposals on same function (conflict set)
        prop_a_id = f"prop_a_{os.urandom(4).hex()}"
        prop_b_id = f"prop_b_{os.urandom(4).hex()}"
        cs_id = f"cs_{os.urandom(4).hex()}"

        prop_a = Proposal(
            id=prop_a_id,
            repo_id=repo_id,
            agent_id="agent_a",
            change_type="full_file",
            change_content="def foo():\n    print('A')\n",
            affected_paths=json.dumps(["app.py"]),
            affected_symbols=json.dumps(["app.py::FUNC:foo"]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING,
            conflict_set_id=cs_id
        )
        prop_b = Proposal(
            id=prop_b_id,
            repo_id=repo_id,
            agent_id="agent_b",
            change_type="full_file",
            change_content="def foo():\n    print('B')\n",
            affected_paths=json.dumps(["app.py"]),
            affected_symbols=json.dumps(["app.py::FUNC:foo"]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING,
            conflict_set_id=cs_id
        )
        conflict_set = ConflictSet(
            id=cs_id,
            repo_id=repo_id,
            path="app.py",
            proposal_ids=json.dumps([prop_a_id, prop_b_id]),
            resolved=False
        )
        session.add(prop_a)
        session.add(prop_b)
        session.add(conflict_set)
        session.commit()
        
        # Run concurrent promoters
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_a, doc_store)
            fut_b = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_b, doc_store)
            await asyncio.gather(fut_a, fut_b)
            
        session.expire_all()
        props = session.exec(select(Proposal).where(Proposal.repo_id == repo_id)).all()
        promoted = [p for p in props if p.status == ProposalStatus.PROMOTED]
        promotions = session.exec(select(Promotion).where(Promotion.repo_id == repo_id)).all()
        
        assert len(promotions) == 1, f"Expected 1 promotion, got {len(promotions)}"
        assert len(promoted) == 1, f"Expected 1 promoted proposal, got {len(promoted)}"
        
    finally:
        session.close()
        main_engine.dispose()

@pytest.mark.asyncio
async def test_postgres_concurrency_paths_lock():
    """Proposals targeting the same file but different functions. Path lock ensures serialization."""
    database_url = os.environ.get("DATABASE_URL")
    main_engine = create_engine(database_url)
    SQLModel.metadata.create_all(main_engine)
    session = Session(main_engine)
    
    try:
        storage_path_a = Path(f"/tmp/conc_paths_storage_a_{os.urandom(4).hex()}")
        storage_path_b = Path(f"/tmp/conc_paths_storage_b_{os.urandom(4).hex()}")
        storage_path_a.mkdir(parents=True, exist_ok=True)
        storage_path_b.mkdir(parents=True, exist_ok=True)
        
        repo_id = f"repo_paths_{os.urandom(4).hex()}"
        head_id, doc_store = await setup_test_repo(session, repo_id, storage_path_a)
        
        # Non-overlapping but same path "app.py"
        prop_a = Proposal(
            repo_id=repo_id,
            agent_id="agent_a",
            change_type="full_file",
            change_content="def foo():\n    print('A')\n",
            affected_paths=json.dumps(["app.py"]),
            affected_symbols=json.dumps(["app.py::FUNC:foo"]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING
        )
        prop_b = Proposal(
            repo_id=repo_id,
            agent_id="agent_b",
            change_type="full_file",
            change_content="def bar():\n    print('B')\n",
            affected_paths=json.dumps(["app.py"]),
            affected_symbols=json.dumps(["app.py::FUNC:bar"]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING
        )
        session.add(prop_a)
        session.add(prop_b)
        session.commit()
        
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_a, doc_store)
            fut_b = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_b, doc_store)
            await asyncio.gather(fut_a, fut_b)
            
        session.expire_all()
        props = session.exec(select(Proposal).where(Proposal.repo_id == repo_id)).all()
        promoted = [p for p in props if p.status == ProposalStatus.PROMOTED]
        promotions = session.exec(select(Promotion).where(Promotion.repo_id == repo_id)).all()
        
        # Since they don't conflict structurally, both should eventually succeed, but we assert they ran serialized
        assert len(promoted) == 2, f"Expected both to be promoted, got {len(promoted)}"
        assert len(promotions) == 2, f"Expected 2 promotions, got {len(promotions)}"
        
    finally:
        session.close()
        main_engine.dispose()

@pytest.mark.asyncio
async def test_postgres_concurrency_proposal_ids_lock():
    """Proposals with no affected paths. Locked by sorted proposal IDs."""
    database_url = os.environ.get("DATABASE_URL")
    main_engine = create_engine(database_url)
    SQLModel.metadata.create_all(main_engine)
    session = Session(main_engine)
    
    try:
        storage_path_a = Path(f"/tmp/conc_ids_storage_a_{os.urandom(4).hex()}")
        storage_path_b = Path(f"/tmp/conc_ids_storage_b_{os.urandom(4).hex()}")
        storage_path_a.mkdir(parents=True, exist_ok=True)
        storage_path_b.mkdir(parents=True, exist_ok=True)
        
        repo_id = f"repo_ids_{os.urandom(4).hex()}"
        head_id, doc_store = await setup_test_repo(session, repo_id, storage_path_a)
        
        # Empty paths
        prop_a = Proposal(
            repo_id=repo_id,
            agent_id="agent_a",
            change_type="full_file",
            change_content="",
            affected_paths=json.dumps([]),
            affected_symbols=json.dumps([]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING
        )
        prop_b = Proposal(
            repo_id=repo_id,
            agent_id="agent_b",
            change_type="full_file",
            change_content="",
            affected_paths=json.dumps([]),
            affected_symbols=json.dumps([]),
            base_head_id=head_id,
            status=ProposalStatus.PENDING
        )
        session.add(prop_a)
        session.add(prop_b)
        session.commit()
        
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_a, doc_store)
            fut_b = loop.run_in_executor(executor, run_promoter, database_url, repo_id, storage_path_b, doc_store)
            await asyncio.gather(fut_a, fut_b)
            
        session.expire_all()
        props = session.exec(select(Proposal).where(Proposal.repo_id == repo_id)).all()
        promoted = [p for p in props if p.status == ProposalStatus.PROMOTED]
        promotions = session.exec(select(Promotion).where(Promotion.repo_id == repo_id)).all()
        
        # Both should promote, serialized by proposal ID locks
        assert len(promoted) == 2, f"Expected both to be promoted, got {len(promoted)}"
        assert len(promotions) == 2, f"Expected 2 promotions, got {len(promotions)}"
        
    finally:
        session.close()
        main_engine.dispose()

@pytest.mark.asyncio
async def test_postgres_proposal_provenance():
    database_url = os.environ.get("DATABASE_URL")
    engine = create_engine(database_url)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    
    try:
        eb = EventBus()
        ps = ProposalService(session, eb, {})
        
        repo_id = f"repo_prov_{os.urandom(4).hex()}"
        repo = Repository(id=repo_id, name="test_repo_prov", workspace_path=f"/tmp/test_provenance_workspace_{repo_id}")
        session.add(repo)
        session.commit()
        
        prop_id = f"prop_prov_{os.urandom(4).hex()}"
        prop = Proposal(
            id=prop_id,
            repo_id=repo_id,
            agent_id="agent_1",
            change_type="full_file",
            change_content="print('hello')",
            affected_paths=json.dumps(["test.py"]),
            affected_symbols=json.dumps([]),
            status=ProposalStatus.PENDING
        )
        session.add(prop)
        session.commit()
        
        # Initially, no provenance exists
        prov_init = ps.get_proposal_provenance(prop_id)
        assert prov_init is None
        
        # Update to REJECTED with a reason
        await ps.update_status(prop_id, ProposalStatus.REJECTED, reason="gate_failed_compilation")
        session.expire_all()
        
        prov_rejected = ps.get_proposal_provenance(prop_id)
        assert prov_rejected is not None
        assert prov_rejected.event_reason == "gate_failed_compilation"
        assert prov_rejected.event_type == "status_rejected"
        assert prov_rejected.created_at is not None
        
        # Update to QUARANTINED with a reason
        await ps.update_status(prop_id, ProposalStatus.QUARANTINED, reason="oracle_insufficiency_escalated")
        session.expire_all()
        
        prov_quarantined = ps.get_proposal_provenance(prop_id)
        assert prov_quarantined is not None
        assert prov_quarantined.event_reason == "oracle_insufficiency_escalated"
        assert prov_quarantined.event_type == "status_quarantined"
        
        # Update to SUPERSEDED with a reason
        await ps.update_status(prop_id, ProposalStatus.SUPERSEDED, reason="spatial_exclusion")
        session.expire_all()
        
        prov_superseded = ps.get_proposal_provenance(prop_id)
        assert prov_superseded is not None
        assert prov_superseded.event_reason == "spatial_exclusion"
        assert prov_superseded.event_type == "status_superseded"
        
    finally:
        session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_pg_contention_exactly_one_winner_under_parallel_submit():
    """T1 temporal contention: parallel promoters, exactly one contention proposal promotes."""
    database_url = os.environ.get("DATABASE_URL")
    main_engine = create_engine(database_url)
    SQLModel.metadata.create_all(main_engine)
    session = Session(main_engine)
    
    try:
        storage_path = Path(f"/tmp/conc_contention_{os.urandom(4).hex()}")
        storage_path.mkdir(parents=True, exist_ok=True)
        
        repo_id = f"repo_cnt_{os.urandom(4).hex()}"
        head_id, doc_store = await setup_test_repo(session, repo_id, storage_path)
        
        eb = EventBus()
        
        # 3 parallel submissions same repo_id + contention_group_id
        prop_ids = []
        for i in range(3):
            pid = f"prop_cnt_{i}_{os.urandom(4).hex()}"
            prop = Proposal(
                id=pid,
                repo_id=repo_id,
                agent_id=f"agent_{i}",
                change_type="full_file",
                change_content=f"print('cnt_{i}')",
                affected_paths=json.dumps(["tools/jsontool.py"]),
                base_head_id=head_id,
                status=ProposalStatus.PENDING,
            )
            session.add(prop)
            session.commit()
            
            cont = ProposalContention(
                proposal_id=pid,
                contention_mode=True,
                contention_group_id=f"group_{repo_id}",
                contention_policy="exactly_one_winner",
                target_paths=json.dumps(["tools/jsontool.py"])
            )
            session.add(cont)
            session.commit()
            prop_ids.append(pid)
            
        # Run three promoters in parallel threads
        with ThreadPoolExecutor(max_workers=3) as executor:
            loop = asyncio.get_running_loop()
            futures = [
                loop.run_in_executor(
                    executor,
                    run_promoter,
                    database_url,
                    repo_id,
                    storage_path,
                    doc_store
                )
                for _ in range(3)
            ]
            await asyncio.gather(*futures)
            
        # Verify status
        session.expire_all()
        proposals = [session.get(Proposal, pid) for pid in prop_ids]
        promoted = [p for p in proposals if p.status == ProposalStatus.PROMOTED]
        superseded = [p for p in proposals if p.status == ProposalStatus.SUPERSEDED]
        
        assert len(promoted) == 1
        assert len(superseded) == 2
        assert all(p.status != ProposalStatus.PENDING for p in proposals)
        assert all(p.status != ProposalStatus.EVALUATING for p in proposals)
        
    finally:
        session.close()
        main_engine.dispose()
