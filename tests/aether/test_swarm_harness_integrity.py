import pytest
from unittest.mock import MagicMock
from sqlmodel import SQLModel, create_engine, Session
from packages.aether.services.swarm_harness import ContinuousSwarmHarness
from packages.aether.models.entities import Proposal, ProposalStatus, ConflictSet

def test_run_scoped_metrics_residue_isolation():
    """Aether-10: Metric counts must isolate pre-seeded residue from other runs."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    harness = MagicMock()
    harness.run_id = "run_current"
    repo = MagicMock()
    repo.id = "repo1"
    harness.repos = [repo]
    
    with Session(engine) as session:
        prop_current = Proposal(id="p1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PROMOTED, source_run_id="run_current")
        prop_foreign = Proposal(id="p2", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PROMOTED, source_run_id="run_foreign")
        session.add(prop_current)
        session.add(prop_foreign)
        session.commit()
        
        counts = ContinuousSwarmHarness._status_counts(harness, session=session)
        assert counts.get(ProposalStatus.PROMOTED.value, 0) == 1

def test_mixed_run_conflict_sets_integrity_failure():
    """Aether-11: Conflict sets with proposals from mixed runs must fail integrity."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    harness = MagicMock()
    harness.run_id = "run_current"
    repo = MagicMock()
    repo.id = "repo1"
    harness.repos = [repo]
    
    if not hasattr(ContinuousSwarmHarness, "verify_integrity"):
        raise AssertionError("AssertionError: verify_integrity not implemented on ContinuousSwarmHarness")
        
    with Session(engine) as session:
        prop1 = Proposal(id="p1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PROMOTED, source_run_id="run_current")
        prop2 = Proposal(id="p2", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PROMOTED, source_run_id="run_foreign")
        session.add(prop1)
        session.add(prop2)
        
        import json
        cs = ConflictSet(id="cs1", repo_id="repo1", path="foo.py", proposal_ids=json.dumps(["p1", "p2"]))
        session.add(cs)
        session.commit()
        
        res = ContinuousSwarmHarness.verify_integrity(harness, session)
        assert res is False, "verify_integrity should return False when conflict set contains mixed runs"

def test_invariant_violating_metrics_failed_passed():
    """Aether-12: Integrity check fails when invariant is violated."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    harness = MagicMock()
    harness.run_id = "run_current"
    repo = MagicMock()
    repo.id = "repo1"
    harness.repos = [repo]
    
    if not hasattr(ContinuousSwarmHarness, "verify_integrity"):
        raise AssertionError("AssertionError: verify_integrity not implemented on ContinuousSwarmHarness")
        
    with Session(engine) as session:
        prop = Proposal(id="p1", repo_id="repo1", agent_id="agent1", task_id="task1", status="INGESTED", source_run_id="run_current")
        session.add(prop)
        session.commit()
        
        res = ContinuousSwarmHarness.verify_integrity(harness, session)
        assert res is False, "verify_integrity should return False when invariant counts mismatch"
