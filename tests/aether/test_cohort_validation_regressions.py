import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path

async def _run_cohort_validation(session, proposal_ids, expected_error, proposal_service=None):
    import inspect
    from packages.aether.services.promotion_engine import PromotionEngine
    from packages.aether.models.entities import Proposal, ProposalStatus, Repository
    
    # 1. Fetch or build proposals list
    proposals_list = []
    for pid in proposal_ids:
        # Retrieve from mock session if mock_get returns it
        p = None
        if hasattr(session, "get"):
            p = session.get(Proposal, pid)
        if not isinstance(p, Proposal):
            p = Proposal(id=pid, repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
        proposals_list.append(p)
        
    # Ensure they have base heads set for same-base check compatibility
    for p in proposals_list:
        if not getattr(p, "base_head_id", None):
            p.base_head_id = "base1"
        if not getattr(p, "base_head_sequence", None):
            p.base_head_sequence = 0
            
    # Unique proposals for get_proposals_by_ids
    unique_proposals = []
    seen = set()
    for p in proposals_list:
        if p.id not in seen:
            unique_proposals.append(p)
            seen.add(p.id)
            
    if proposal_service is None:
        proposal_service = MagicMock()
    proposal_service.get_proposals_by_ids = MagicMock(return_value=unique_proposals)
    proposal_service.update_status_many = AsyncMock()
    
    # Mock Repository in session.get
    repo = Repository(id="repo1", head_id="base1")
    orig_get = getattr(session, "get", None)
    def wrapper_get(model, ident=None):
        if model == Repository:
            return repo
        if orig_get is not None:
            if ident is not None:
                return orig_get(model, ident)
            return orig_get(model)
        return None
    session.get = wrapper_get

    sig = inspect.signature(PromotionEngine.__init__)
    params = list(sig.parameters.keys())[1:]
    
    snapshot_service = MagicMock()
    snapshot_service.create_snapshot = AsyncMock()
    
    gate_runner = MagicMock()
    gate_runner.run_all_gates = AsyncMock()
    
    mock_map = {
        "session": session,
        "event_bus": MagicMock(),
        "proposal_service": proposal_service,
        "snapshot_service": snapshot_service,
        "gate_runner": gate_runner,
        "storage_path": Path("/tmp"),
    }
    args = [mock_map.get(name, MagicMock()) for name in params if sig.parameters[name].default is inspect.Parameter.empty]
    engine = PromotionEngine(*args)
    if hasattr(engine, "trust_service"):
        engine.trust_service.get_combined_agent_score = MagicMock(return_value=1.0)
        engine.trust_service.compute_combined_score = MagicMock(return_value=1.0)
    engine._promote_internal = AsyncMock()
    
    if not hasattr(engine, "evaluate_and_promote_cohort"):
        raise AssertionError("AssertionError: evaluate_and_promote_cohort not implemented in starting SHA")
        
    try:
        meth_sig = inspect.signature(engine.evaluate_and_promote_cohort)
        meth_params = list(meth_sig.parameters.keys())
        call_args = {"repo_id": "repo1", "proposal_ids": proposal_ids}
        if "document_store" in meth_params:
            call_args["document_store"] = MagicMock()
            
        res = engine.evaluate_and_promote_cohort(**call_args)
        if inspect.iscoroutine(res):
            await res
    except ValueError as e:
        if expected_error in str(e):
            return
        raise AssertionError(f"Expected {expected_error}, got: {e}")
    except Exception as e:
        raise AssertionError(f"Expected ValueError, got {type(e).__name__}: {e}")
    else:
        raise AssertionError("Expected ValueError, but no exception was raised")

@pytest.mark.asyncio
async def test_mixed_policy_rejected():
    """Aether-1: Cohorts with mixed contention policies must be rejected."""
    from packages.aether.models.entities import Proposal, ProposalContention, ProposalStatus
    
    session = MagicMock()
    prop1 = Proposal(id="prop1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
    prop2 = Proposal(id="prop2", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
    
    cont1 = ProposalContention(proposal_id="prop1", contention_group_id="group1", contention_policy="semantic_merge", defer_evaluation=True)
    cont2 = ProposalContention(proposal_id="prop2", contention_group_id="group1", contention_policy="standard", defer_evaluation=True)
    
    def mock_get(model, ident):
        if ident == "prop1": return prop1
        if ident == "prop2": return prop2
        return None
    session.get = mock_get
    
    proposal_service = MagicMock()
    proposal_service.get_contention_metadata = lambda pid: cont1 if pid == "prop1" else cont2
    
    await _run_cohort_validation(session, ["prop1", "prop2"], "mixed policy validation error", proposal_service)

@pytest.mark.asyncio
async def test_stale_shared_base_rejected():
    """Aether-2: Cohorts with stale shared base head must be rejected."""
    from packages.aether.models.entities import Proposal, ProposalContention, ProposalStatus, Repository
    
    session = MagicMock()
    prop1 = Proposal(id="prop1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING, base_head_id="base1", source_run_id="run1")
    prop2 = Proposal(id="prop2", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING, base_head_id="base2", source_run_id="run1")
    
    cont1 = ProposalContention(proposal_id="prop1", contention_group_id="group1", contention_policy="semantic_merge", defer_evaluation=True)
    cont2 = ProposalContention(proposal_id="prop2", contention_group_id="group1", contention_policy="semantic_merge", defer_evaluation=True)
    
    def mock_get(model, ident):
        if model == Repository:
            return Repository(id="repo1", head_id="base1")
        if ident == "prop1": return prop1
        if ident == "prop2": return prop2
        return None
    session.get = mock_get
    
    proposal_service = MagicMock()
    proposal_service.get_contention_metadata = lambda pid: cont1 if pid == "prop1" else cont2
    
    await _run_cohort_validation(session, ["prop1", "prop2"], "stale base validation error", proposal_service)

@pytest.mark.asyncio
async def test_single_proposal_rejected():
    """Aether-3: Cohorts with less than 2 proposals must be rejected."""
    session = MagicMock()
    await _run_cohort_validation(session, ["prop1"], "single proposal validation error")

@pytest.mark.asyncio
async def test_duplicate_proposal_ids_rejected():
    """Aether-4: Duplicate proposal IDs in the request must be rejected."""
    session = MagicMock()
    await _run_cohort_validation(session, ["prop1", "prop1"], "duplicated ID validation error")

@pytest.mark.asyncio
async def test_cross_repo_proposals_rejected():
    """Aether-5: Proposals belonging to different repositories must be rejected."""
    from packages.aether.models.entities import Proposal, ProposalStatus
    
    session = MagicMock()
    prop1 = Proposal(id="prop1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
    prop2 = Proposal(id="prop2", repo_id="repo2", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
    
    def mock_get(model, ident):
        if ident == "prop1": return prop1
        if ident == "prop2": return prop2
        return None
    session.get = mock_get
    
    await _run_cohort_validation(session, ["prop1", "prop2"], "cross-repo validation error")

@pytest.mark.asyncio
async def test_non_deferred_proposals_rejected():
    """Aether-6: Proposals that are not in PENDING status or not deferred must be rejected."""
    from packages.aether.models.entities import Proposal, ProposalContention, ProposalStatus
    
    session = MagicMock()
    prop1 = Proposal(id="prop1", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.EVALUATING)
    prop2 = Proposal(id="prop2", repo_id="repo1", agent_id="agent1", task_id="task1", status=ProposalStatus.PENDING)
    
    cont1 = ProposalContention(proposal_id="prop1", contention_group_id="group1", contention_policy="semantic_merge", defer_evaluation=True)
    cont2 = ProposalContention(proposal_id="prop2", contention_group_id="group1", contention_policy="semantic_merge", defer_evaluation=True)
    
    def mock_get(model, ident):
        if ident == "prop1": return prop1
        if ident == "prop2": return prop2
        return None
    session.get = mock_get
    
    proposal_service = MagicMock()
    proposal_service.get_contention_metadata = lambda pid: cont1 if pid == "prop1" else cont2
    
    await _run_cohort_validation(session, ["prop1", "prop2"], "non-deferred validation error", proposal_service)
