"""
Agent Trust Calibration Tests.

Verifies:
- Bayesian Beta updates (alpha/beta)
- Trust-aware scoring and tie-breaking
- Hard failure vs Neutral loss accounting
"""

import pytest
import asyncio
from pathlib import Path
from sqlmodel import SQLModel, Session, create_engine, select

from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.event_bus import EventBus
from packages.aether.services.trust_service import TrustService
from packages.aether.models.entities import ProposalCreate, Repository, AgentMetric, ProposalStatus

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

@pytest.fixture
def trust_svc(db_session):
    return TrustService(db_session)

def test_trust_calculation(trust_svc):
    # Initial: 3 / (3 + 3) = 0.5
    metric = trust_svc.get_metrics("agent1")
    assert trust_svc.calculate_trust_score(metric) == 0.5
    
    # Promotion: alpha += 1.0 -> 4 / (4 + 3) = 0.5714...
    trust_svc.record_promotion("agent1")
    assert trust_svc.calculate_trust_score(metric) > 0.5
    
    # Hard Fail: beta += 3.0 -> 4 / (4 + 6) = 0.4
    trust_svc.record_hard_fail("agent1")
    assert trust_svc.calculate_trust_score(metric) == 0.4

def test_logit_combined_scoring(trust_svc):
    # Equal confidence and trust
    score1 = trust_svc.compute_combined_score(0.5, 0.5)
    assert score1 == 0.5
    
    # High trust boosts confidence
    score2 = trust_svc.compute_combined_score(0.5, 0.8)
    assert score2 > 0.5
    
    # Low trust penalizes confidence
    score3 = trust_svc.compute_combined_score(0.5, 0.2)
    assert score3 < 0.5

def test_trust_aware_promotion(db_session):
    bus = EventBus()
    storage = Path("/tmp/aether_trust_test")
    storage.mkdir(exist_ok=True)
    
    doc_store = DocumentStore(repo_id="test-repo")
    prop_svc = ProposalService(db_session, bus, {"test-repo": doc_store})
    snap_svc = SnapshotService(db_session, bus, storage)
    gate_runner = GateRunner(db_session, bus)
    engine = PromotionEngine(db_session, bus, prop_svc, snap_svc, gate_runner, storage)
    trust_svc = engine.trust_service

    # Setup repo
    repo = Repository(name="test-repo", id="test-repo", head_id="head-1", head_sequence=1)
    db_session.add(repo)
    db_session.commit()
    
    # Setup conflicting proposals
    source = "def func1():\n    pass\n"
    doc_store.get_document("main.py").structured.set_content(source)
    
    # Agent 1: NEW (0.5 trust)
    p1_data = ProposalCreate(
        agent_id="agent1_new",
        task_id="task1",
        change_type="unified_diff",
        change_content='diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,2 @@\n def func1():\n-    pass\n+    return 1',
        affected_paths=["main.py"],
        confidence_score=0.6 # Higher raw confidence
    )
    
    # Agent 2: HIGH TRUST (0.8 trust)
    trust_svc.record_promotion("agent2_trusted", weight=12.0) # Boost alpha to ~15
    p2_data = ProposalCreate(
        agent_id="agent2_trusted",
        task_id="task2",
        change_type="unified_diff",
        change_content='diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,2 @@\n def func1():\n-    pass\n+    return 2',
        affected_paths=["main.py"],
        confidence_score=0.55 # Lower raw confidence than agent1
    )
    
    asyncio.run(prop_svc.submit_proposal("test-repo", p1_data))
    asyncio.run(prop_svc.submit_proposal("test-repo", p2_data))
    
    # Run promotion loop
    promotions = asyncio.run(engine.process_pending_proposals("test-repo", doc_store))
    
    assert len(promotions) == 1
    winner_ids = json.loads(promotions[0].included_proposals)
    
    # Retrieve winning proposal to check agent_id
    winner = prop_svc.get_proposal(winner_ids[0])
    # Agent 2 should win because of trust boost
    assert winner.agent_id == "agent2_trusted"
    
    # Verify metrics: agent2 should have another win, agent1 should have a neutral loss
    m1 = trust_svc.get_metrics("agent1_new")
    m2 = trust_svc.get_metrics("agent2_trusted")
    
    assert m1.neutral_loss_count == 1
    assert m2.promoted_count == 2 # 1 from setup, 1 from winner

import json
