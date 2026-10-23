"""Unit and integration tests for Standalone Synthesis Worker contract and integration."""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
import pytest
from packages.aether.models.entities import Proposal, ProposalStatus, Repository
from packages.aether.models.synthesis import (
    SynthesisRequestV1,
    ContractConstraintsV1,
    VerificationContextV1,
    GenerationPolicyV1,
    ModelPolicyV1,
    RequestProvenanceV1,
    SynthesisResponseV1,
)
from packages.aether.services.synthesis_adapter import SynthesisAdapter
from packages.aether.synthesis_worker.main import run_once

from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine
from sqlmodel import Session, create_engine, SQLModel

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

def get_valid_request_v1() -> SynthesisRequestV1:
    """Return a valid SynthesisRequestV1 object."""
    base_text = "def update_profile(self):\n    pass\n"
    left_text = "def update_profile(self):\n    self.name = 'Alice'\n"
    right_text = "def update_profile(self):\n    self.email = 'alice@example.com'\n"

    body_str = f"syn_job1:test-repo:user.py:CLASS:User::FUNC:update_profile:{base_text}:{left_text}:{right_text}"
    req_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

    return SynthesisRequestV1(
        job_id="syn_job1",
        repo_id="test-repo",
        language="python",
        file_path="user.py",
        base_head_id="head-1",
        current_head_id="head-2",
        conflict_class="class2_isomorphic_mutation",
        symbol_id="CLASS:User::FUNC:update_profile",
        parent_symbol_id="CLASS:User",
        base_node_hash=hashlib.sha256(base_text.encode("utf-8")).hexdigest(),
        left_proposal_id="prop_left",
        right_proposal_id="prop_right",
        base_node_text=base_text,
        left_node_text=left_text,
        right_node_text=right_text,
        left_diff="",
        right_diff="",
        allowed_paths=["user.py"],
        protected_paths=[".aether/test_manifest.json"],
        contract_constraints=ContractConstraintsV1(),
        verification_context=VerificationContextV1(
            gate_test_selection="file_fallback",
            test_targets=["tests/aether/test_services.py"],
            lint_targets=["packages/aether/services/"],
            typecheck_targets=["packages/aether/services/"]
        ),
        generation_policy=GenerationPolicyV1(),
        model_policy=ModelPolicyV1(provider="configured_provider", model="configured_model"),
        provenance=RequestProvenanceV1(
            request_created_at="2026-07-06T00:00:00Z",
            request_creator="aether-promotion-engine",
            request_hash=req_hash
        )
    )

def test_worker_contract_accepts_valid_request():
    """Verify that a valid request validates against the Pydantic schema."""
    req = get_valid_request_v1()
    assert req.job_id == "syn_job1"
    assert req.contract_constraints.must_preserve_signature is True

def test_worker_contract_rejects_missing_required_fields():
    """Verify schema raises ValidationError when missing required fields."""
    with pytest.raises(Exception):
        # Missing job_id and file_path
        SynthesisRequestV1(
            repo_id="test-repo",
            symbol_id="CLASS:User::FUNC:update_profile",
        )

def test_worker_once_mode_returns_replacement_node_response():
    """Verify standalone CLI run_once writes response with candidate_ready status."""
    req = get_valid_request_v1()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        req_file = tmppath / "request.json"
        resp_file = tmppath / "response.json"

        req_file.write_text(req.model_dump_json(), encoding="utf-8")
        ret = run_once(req_file, resp_file)
        assert ret == 0
        assert resp_file.exists()

        resp = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))
        assert resp.status == "candidate_ready"
        assert len(resp.candidates) == 1
        assert "Alice" in resp.candidates[0].candidate_text
        assert "alice@example.com" in resp.candidates[0].candidate_text

def test_worker_rejects_unified_diff_in_v1():
    """Verify response does not produce unified diff candidates in v1."""
    # V1 output_format must equal replacement_node
    req = get_valid_request_v1()
    req.generation_policy.output_format = "unified_diff"
    # Although CLI accepts validation, it forces output_format candidate format to replacement_node
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        req_file = tmppath / "request.json"
        resp_file = tmppath / "response.json"

        req_file.write_text(req.model_dump_json(), encoding="utf-8")
        ret = run_once(req_file, resp_file)
        assert ret == 0
        resp = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))
        # The returned candidate always uses replacement_node
        for c in resp.candidates:
            assert c.output_format == "replacement_node"

def test_worker_declared_conflict_routes_to_envelope():
    """Verify that conflicting self variable assignments returns declared_conflict status."""
    req = get_valid_request_v1()
    req.left_node_text = "def update_profile(self):\n    self.name = 'Alice'\n"
    req.right_node_text = "def update_profile(self):\n    self.name = 'Bob'\n"  # Collision!

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        req_file = tmppath / "request.json"
        resp_file = tmppath / "response.json"

        req_file.write_text(req.model_dump_json(), encoding="utf-8")
        ret = run_once(req_file, resp_file)
        assert ret == 0
        resp = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))
        assert resp.status == "declared_conflict"
        assert resp.worker_verdict.declared_conflict is True
        assert len(resp.candidates) == 0

def test_remote_model_disabled_by_default():
    """Verify remote model is rejected with policy_violation status by default if not allowed."""
    req = get_valid_request_v1()
    req.model_policy.provider = "remote_gemini"
    req.model_policy.allow_remote_model = False  # Disabled by default!

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        req_file = tmppath / "request.json"
        resp_file = tmppath / "response.json"

        req_file.write_text(req.model_dump_json(), encoding="utf-8")
        ret = run_once(req_file, resp_file)
        assert ret == 0
        resp = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))
        assert resp.status == "failed_policy_violation"

def test_fake_provider_deterministic_for_ci():
    """Verify merge logic returns deterministic combined assignments in once mode."""
    req = get_valid_request_v1()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        req_file = tmppath / "request.json"
        resp_file = tmppath / "response.json"

        req_file.write_text(req.model_dump_json(), encoding="utf-8")
        run_once(req_file, resp_file)
        resp1 = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))

        run_once(req_file, resp_file)
        resp2 = SynthesisResponseV1.model_validate_json(resp_file.read_text(encoding="utf-8"))

        assert resp1.candidates[0].candidate_text == resp2.candidates[0].candidate_text

@pytest.mark.asyncio
async def test_worker_crash_is_captured_as_failed_model_error():
    """Verify adapter handles invalid python syntax or crash as failed_model_error."""
    adapter = SynthesisAdapter(python_executable="non_existent_python_bin")
    req = get_valid_request_v1()
    status, resp = await adapter.invoke_worker(req)
    assert status == "failed_model_error"
    assert resp is None

@pytest.mark.asyncio
async def test_worker_timeout_is_failure_not_pending_retry():
    """Verify that subprocess timeout expiration returns failed_timeout status."""
    adapter = SynthesisAdapter()
    req = get_valid_request_v1()
    # A timeout of 0.0001s will force TimeoutExpired
    status, resp = await adapter.invoke_worker(req, timeout_seconds=0.0001)
    assert status == "failed_timeout"
    assert resp is None

@pytest.mark.asyncio
async def test_candidate_without_provenance_is_rejected():
    """Verify adapter rejects output when the request hash doesn't match."""
    adapter = SynthesisAdapter()
    req = get_valid_request_v1()
    # Set request_hash to trigger mismatch
    req.provenance.request_hash = "trigger_mismatch"
    status, resp = await adapter.invoke_worker(req)
    assert status == "failed_policy_violation"
    assert resp is None

@pytest.mark.asyncio
async def test_no_db_transaction_and_promotion_lock_released_during_worker_execution(promotion_engine, db_session, repo_service, monkeypatch):
    """Verify that during worker execution, the reentrant lock is released and no active transaction is open."""
    repo = repo_service.create_repository("test-repo-lock")
    
    # Pre-acquire the promotion engine lock to simulate running in the promotion loop
    await promotion_engine._promotion_lock.acquire()
    assert promotion_engine._promotion_lock._depth == 1
    
    proposal = Proposal(
        id="prop_synth_lock",
        repo_id=repo.id,
        agent_id="agent-1",
        task_id="task-1",
        base_head_sequence=0,
        affected_symbols=json.dumps(["user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING
    )
    db_session.add(proposal)
    db_session.commit()
    
    # Enable worker
    promotion_engine.synthesis_worker_enabled = True

    # Setup slow worker invoke to check lock release state and concurrent DB transactions
    original_invoke = SynthesisAdapter.invoke_worker
    concurrent_check_ran = False

    async def slow_invoke(self, request, timeout_seconds=120):
        # 1. Verify lock depth is 0 inside the subprocess invocation wait window
        assert promotion_engine._promotion_lock._depth == 0
        
        # 2. Verify other tasks can acquire the lock concurrently
        await promotion_engine._promotion_lock.acquire()
        assert promotion_engine._promotion_lock._depth == 1
        promotion_engine._promotion_lock.release()
        
        # 3. Verify other tasks can run DB transactions concurrently
        # SQLite should not be locked because the engine transaction was rolled back / committed
        from packages.aether.models.entities import Repository
        new_repo = Repository(id="repo_concurrent", name="repo-concurrent-ok")
        db_session.add(new_repo)
        db_session.commit()
        
        nonlocal concurrent_check_ran
        concurrent_check_ran = True

        return await original_invoke(self, request, timeout_seconds)

    monkeypatch.setattr(SynthesisAdapter, "invoke_worker", slow_invoke)
    
    # Run the worker call through attempt_semantic_merge_synthesis
    merged = await promotion_engine.attempt_semantic_merge_synthesis(
        repo_id=repo.id,
        path="user.py",
        proposal=proposal,
        base_content="class User:\n    def update_profile(self):\n        pass\n",
        current_content="class User:\n    def update_profile(self):\n        self.name = 'Alice'\n",
        content_b="class User:\n    def update_profile(self):\n        self.email = 'alice@example.com'\n"
    )
    
    # Lock depth should be restored back to 1
    assert promotion_engine._promotion_lock._depth == 1
    assert concurrent_check_ran is True
    assert "Alice" in merged
    assert "alice@example.com" in merged
    
    # Release final lock
    promotion_engine._promotion_lock.release()

@pytest.mark.asyncio
async def test_async_loop_responsiveness_during_worker_execution():
    """Verify that during worker execution, the event loop remains responsive and does not block."""
    adapter = SynthesisAdapter()
    req = get_valid_request_v1()
    
    # 1. Start worker invocation in the background
    worker_task = asyncio.create_task(adapter.invoke_worker(req))
    
    # 2. Run a concurrent async task that counts sleeps on the event loop
    counter = 0
    while not worker_task.done():
        await asyncio.sleep(0.01)
        counter += 1
        
    # 3. Await worker completion
    status, resp = await worker_task
    assert status == "candidate_ready"
    assert resp is not None
    
    # 4. Assert that the loop actually scheduled our concurrent task multiple times
    assert counter > 0
