"""Regression tests for explicit simulation gate defaults and real gate rejection of broken proposals."""

import json
import pytest
from sqlmodel import SQLModel, Session, create_engine

from examples.reproducible_gate_verification_demo import run_reproducible_demo
from packages.aether.models.entities import GateStatus, ProposalCreate
from packages.aether.services.event_bus import EventBus
from packages.aether.services.gate_runner import DEFAULT_GATES, GateRunner, is_safe_simulation_command
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.crdt.document_store import DocumentStore


@pytest.fixture
def db_session():
    """Create in-memory database session."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def event_bus():
    """Create event bus."""
    return EventBus()


@pytest.fixture
def storage_path(tmp_path):
    """Create temporary storage path."""
    return tmp_path / "aether_storage"


def test_default_gates_are_explicitly_marked_simulated() -> None:
    """Default no-op gates must clearly declare SIMULATED mode rather than silent echo strings."""
    assert len(DEFAULT_GATES) >= 2
    for gate in DEFAULT_GATES:
        assert gate.command.startswith("SIMULATED:"), (
            f"Default gate {gate.name!r} must start with 'SIMULATED:' to avoid silent no-op confusion"
        )
        assert is_safe_simulation_command(gate.command)


@pytest.mark.asyncio
async def test_simulated_default_promotion_records_simulated_verification_mode(
    db_session, event_bus, storage_path
) -> None:
    """Promotions using DEFAULT_GATES must record _verification_mode='SIMULATED' in evidence."""
    stores: dict[str, DocumentStore] = {}
    repo_service = RepositoryService(db_session, storage_path)
    proposal_service = ProposalService(db_session, event_bus, stores)
    snapshot_service = SnapshotService(db_session, event_bus, storage_path)
    gate_runner = GateRunner(db_session, event_bus)
    promotion_engine = PromotionEngine(
        db_session, event_bus, proposal_service, snapshot_service, gate_runner, storage_path
    )

    repo = repo_service.create_repository(name="simulated-default-repo")
    store = repo_service.get_document_store(repo.id)
    proposal_service.set_document_store(repo.id, store)
    prop = await proposal_service.submit_proposal(
        repo.id,
        ProposalCreate(
            agent_id="agent-sim",
            change_type="full_file",
            change_content="x = 1\n",
            affected_paths=["app.py"],
            confidence_score=0.9,
        ),
    )
    promotions = await promotion_engine.process_pending_proposals(repo.id, store)
    assert len(promotions) == 1
    promotion = promotions[0]
    evidence = json.loads(promotion.evidence)
    assert evidence.get("_verification_mode") == "SIMULATED"

    gate_results = gate_runner.get_latest_results(repo.id)
    assert gate_results
    assert all("[SIMULATED]" in (r.stdout or "") for r in gate_results)


@pytest.mark.asyncio
async def test_real_verification_gate_rejects_broken_proposal_and_promotes_fix(tmp_path) -> None:
    """End-to-end demo must reject a broken proposal via real pytest gate and promote a valid fix."""
    summary = await run_reproducible_demo(work_dir=tmp_path)
    assert summary["v1_promoted_head_sequence"] == 1
    assert summary["v1_verification_mode"] == "EXECUTED"
    assert summary["broken_proposal_status"] == "REJECTED"
    assert summary["broken_gate_status"] == GateStatus.FAIL.value
    assert summary["broken_gate_exit_code"] != 0
    assert summary["head_sequence_after_broken"] == 1
    assert summary["v3_promoted_head_sequence"] == 2
    assert "return int(a) + int(b)" in summary["canonical_calculator_source"]


@pytest.mark.parametrize("force", [False, True])
def test_api_mixed_batch_promotion_preserves_promoted_winner_and_rejects_failing_proposal(
    monkeypatch, tmp_path, force: bool
) -> None:
    """
    Regression test for mixed-batch promotion via POST /v1/repos/{id}/promotions:
    When a batch contains one valid proposal and one gate-failing proposal, sequential degradation
    promotes the valid proposal and rejects the failing one. The API must NOT overwrite the
    promoted proposal to REJECTED and must report partial promotion accurately for both
    force=False and force=True, without weakening required gates.
    """
    import sys
    from fastapi.testclient import TestClient
    from apps.aether_server.main import app
    from packages.aether.api.routes import set_services

    monkeypatch.setenv("AETHER_ALLOW_CUSTOM_GATE_COMMANDS", "true")
    monkeypatch.delenv("AETHER_API_TOKEN", raising=False)

    db_path = tmp_path / f"mixed_batch_{force}.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    event_bus = EventBus()
    storage_dir = tmp_path / f"storage_{force}"
    storage_dir.mkdir(parents=True, exist_ok=True)
    set_services(engine, event_bus, storage_dir)

    client = TestClient(app)

    # Create a repository with a real Python syntax/import check gate across all .py files
    gate_cmd = (
        f'"{sys.executable}" -c '
        '"import pathlib; '
        '[compile(p.read_text(), str(p), \'exec\') for p in pathlib.Path(\'.\').glob(\'*.py\')]"'
    )
    repo_resp = client.post(
        "/v1/repos",
        json={
            "name": f"mixed-batch-repo-{force}",
            "config": {
                "gates": [
                    {
                        "name": "py-compile",
                        "command": gate_cmd,
                        "required": True,
                        "timeout_seconds": 15,
                    }
                ]
            },
        },
    )
    assert repo_resp.status_code == 200, repo_resp.text
    repo_id = repo_resp.json()["id"]

    # Submit valid proposal on module_ok.py
    valid_resp = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent-valid",
            "change_type": "full_file",
            "change_content": "def ok_func() -> int:\n    return 42\n",
            "affected_paths": ["module_ok.py"],
            "confidence_score": 0.95,
        },
    )
    assert valid_resp.status_code == 200, valid_resp.text
    valid_id = valid_resp.json()["proposal_id"]

    # Submit broken proposal on module_bad.py (syntax error fails the py-compile gate)
    bad_resp = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent-bad",
            "change_type": "full_file",
            "change_content": "def broken_func(:\n    return 0\n",
            "affected_paths": ["module_bad.py"],
            "confidence_score": 0.60,
        },
    )
    assert bad_resp.status_code == 200, bad_resp.text
    bad_id = bad_resp.json()["proposal_id"]

    # Request promotion of the mixed batch [valid_id, bad_id] under both force=False and force=True
    promo_resp = client.post(
        f"/v1/repos/{repo_id}/promotions",
        json={"proposal_ids": [valid_id, bad_id], "force": force},
    )
    assert promo_resp.status_code == 200, promo_resp.text
    promo_data = promo_resp.json()

    # Partial promotion must report success=True, head sequence 1, valid included, bad excluded
    assert promo_data["success"] is True, promo_data
    assert promo_data["new_head_sequence"] == 1
    assert promo_data["included_proposals"] == [valid_id]
    assert promo_data["excluded_proposals"] == [bad_id]
    assert promo_data["promotion_id"] != ""

    # Verify individual proposal states via GET /v1/repos/{repo_id}/proposals/{proposal_id}
    valid_after = client.get(f"/v1/repos/{repo_id}/proposals/{valid_id}").json()
    bad_after = client.get(f"/v1/repos/{repo_id}/proposals/{bad_id}").json()
    assert valid_after["status"] == "PROMOTED", valid_after
    assert bad_after["status"] == "REJECTED", bad_after

    # Verify canonical head is at sequence 1
    head_after = client.get(f"/v1/repos/{repo_id}/head").json()
    assert head_after["sequence"] == 1

    # Also verify that an all-failing promotion with force=force never weakens required gates
    bad2_resp = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent-bad-2",
            "change_type": "full_file",
            "change_content": "def still_broken(:\n    pass\n",
            "affected_paths": ["module_bad2.py"],
            "confidence_score": 0.90,
        },
    )
    assert bad2_resp.status_code == 200, bad2_resp.text
    bad2_id = bad2_resp.json()["proposal_id"]

    fail_promo_resp = client.post(
        f"/v1/repos/{repo_id}/promotions",
        json={"proposal_ids": [bad2_id], "force": force},
    )
    assert fail_promo_resp.status_code == 200, fail_promo_resp.text
    fail_promo_data = fail_promo_resp.json()
    assert fail_promo_data["success"] is False, fail_promo_data
    assert fail_promo_data["included_proposals"] == []
    assert fail_promo_data["excluded_proposals"] == [bad2_id]
    assert client.get(f"/v1/repos/{repo_id}/proposals/{bad2_id}").json()["status"] == "REJECTED"
    assert client.get(f"/v1/repos/{repo_id}/head").json()["sequence"] == 1

