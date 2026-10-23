import pytest
from sqlmodel import SQLModel, create_engine, Session
from packages.aether.models.entities import Proposal, ProposalStatus, SystemMetricEventType, Repository
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.event_bus import EventBus
import json

@pytest.mark.asyncio
async def test_sqlite_proposal_provenance():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    
    try:
        eb = EventBus()
        ps = ProposalService(session, eb, {})
        
        # Setup repository
        repo = Repository(id="repo_1", name="test_repo", workspace_path="/tmp/test_provenance_workspace")
        session.add(repo)
        session.commit()
        
        # 1. Create a proposal
        prop = Proposal(
            id="prop_1",
            repo_id="repo_1",
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
        prov_init = ps.get_proposal_provenance("prop_1")
        assert prov_init is None
        
        # 2. Update to REJECTED with a reason
        await ps.update_status("prop_1", ProposalStatus.REJECTED, reason="gate_failed_compilation")
        session.expire_all()
        
        prov_rejected = ps.get_proposal_provenance("prop_1")
        assert prov_rejected is not None
        assert prov_rejected.event_reason == "gate_failed_compilation"
        assert prov_rejected.event_type == "status_rejected"
        assert prov_rejected.created_at is not None
        
        # 3. Update to QUARANTINED with a reason
        await ps.update_status("prop_1", ProposalStatus.QUARANTINED, reason="oracle_insufficiency_escalated")
        session.expire_all()
        
        prov_quarantined = ps.get_proposal_provenance("prop_1")
        assert prov_quarantined is not None
        assert prov_quarantined.event_reason == "oracle_insufficiency_escalated"
        assert prov_quarantined.event_type == "status_quarantined"
        
        # 4. Update to SUPERSEDED with a reason
        await ps.update_status("prop_1", ProposalStatus.SUPERSEDED, reason="spatial_exclusion")
        session.expire_all()
        
        prov_superseded = ps.get_proposal_provenance("prop_1")
        assert prov_superseded is not None
        assert prov_superseded.event_reason == "spatial_exclusion"
        assert prov_superseded.event_type == "status_superseded"
        
        # 5. Promoted has no terminal failure reason in the query filter
        await ps.update_status("prop_1", ProposalStatus.PROMOTED)
        session.expire_all()
        
        # Should return the last matching terminal failure status (which was SUPERSEDED "spatial_exclusion")
        # because the query filters strictly for the failing statuses.
        prov_promoted = ps.get_proposal_provenance("prop_1")
        assert prov_promoted is not None
        assert prov_promoted.event_reason == "spatial_exclusion"
        assert prov_promoted.event_type == "status_superseded"
        
    finally:
        session.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_api_proposal_provenance():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.aether.api.routes import router, set_services
    from sqlalchemy.pool import StaticPool
    
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    
    eb = EventBus()
    storage_path = "/tmp/test_provenance_api"
    set_services(engine, eb, storage_path)
    
    app = FastAPI()
    app.include_router(router)
    
    with TestClient(app) as client:
        with Session(engine) as session:
            ps = ProposalService(session, eb, {})
            repo = Repository(id="repo_2", name="test_repo_2", workspace_path="/tmp/test_provenance_workspace_2")
            session.add(repo)
            session.commit()
            
            prop = Proposal(
                id="prop_2",
                repo_id="repo_2",
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
        resp_init = client.get("/v1/repos/repo_2/proposals/prop_2/provenance")
        assert resp_init.status_code == 200
        assert resp_init.json()["event_reason"] is None
        assert resp_init.json()["event_type"] is None
        
        # Update status to quarantined
        with Session(engine) as session:
            ps = ProposalService(session, eb, {})
            await ps.update_status("prop_2", ProposalStatus.QUARANTINED, reason="oracle_insufficiency_escalated")
            
        resp_quarantined = client.get("/v1/repos/repo_2/proposals/prop_2/provenance")
        assert resp_quarantined.status_code == 200
        data = resp_quarantined.json()
        assert data["event_reason"] == "oracle_insufficiency_escalated"
        assert data["event_type"] == "status_quarantined"
        assert data["created_at"] is not None

