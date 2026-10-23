"""API coverage for coherence backpressure on proposal submission."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlmodel import SQLModel, Session, create_engine

from packages.aether.api.routes import router, set_services
from packages.aether.models.entities import Proposal, ProposalStatus
from packages.aether.services.event_bus import EventBus
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService


@pytest.fixture
def backpressure_client(tmp_path: Path):
    """Build API client with sqlite+router for backpressure tests."""
    db_path = tmp_path / "backpressure.db"
    storage_path = tmp_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    set_services(engine, EventBus(), storage_path)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    try:
        yield client, engine, storage_path
    finally:
        client.close()


def _create_repo(engine, storage_path: Path, name: str) -> str:
    with Session(engine) as session:
        repo = RepositoryService(session, storage_path).create_repository(name=name)
        return repo.id


def test_submit_rejected_with_429_when_backlog_above_limit(backpressure_client, monkeypatch):
    client, engine, storage_path = backpressure_client
    repo_id = _create_repo(engine, storage_path, "backpressure-429")

    monkeypatch.setenv("AETHER_MAX_COHERENCE_BACKLOG", "3")
    monkeypatch.setenv("AETHER_BACKLOG_RESUME_LEVEL", "2")
    monkeypatch.setenv("AETHER_COHERENCE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AETHER_RETRY_AFTER_MAX_SECONDS", "17")

    with Session(engine) as session:
        for i in range(4):
            session.add(
                Proposal(
                    repo_id=repo_id,
                    agent_id=f"agent_{i}",
                    change_content="noop",
                    affected_paths='["src/a.py"]',
                    status=ProposalStatus.INGESTED,
                )
            )
        session.commit()

    response = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent_submit",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/a.py b/src/a.py",
            "affected_paths": ["src/a.py"],
            "confidence_score": 0.8,
        },
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "17"
    payload = response.json()["detail"]
    assert payload["queue_depth_active"] >= 4
    assert payload["queue_depth_limit"] == 3
    assert payload["queue_depth_resume"] == 2
    assert "canonicalization" in payload["error"].lower()


def test_submit_allowed_below_backlog_limit(backpressure_client, monkeypatch):
    client, engine, storage_path = backpressure_client
    repo_id = _create_repo(engine, storage_path, "backpressure-allow")

    monkeypatch.setenv("AETHER_MAX_COHERENCE_BACKLOG", "100")
    monkeypatch.setenv("AETHER_BACKLOG_RESUME_LEVEL", "80")
    monkeypatch.setenv("AETHER_COHERENCE_WINDOW_SECONDS", "60")

    response = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent_submit_ok",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/b.py b/src/b.py",
            "affected_paths": ["src/b.py"],
            "confidence_score": 0.7,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal_id"].startswith("prop_")
    assert payload["status"] in ("INGESTED", "PENDING")
    assert float(response.headers["X-Aether-Route-Handler-Ms"]) >= 0.0
    assert float(response.headers["X-Aether-Submit-Service-Ms"]) >= 0.0
    assert float(response.headers["X-Aether-Submit-DB-Write-Ms"]) >= 0.0
    assert float(response.headers["X-Aether-Submit-Conflict-Enqueue-Ms"]) >= 0.0


def test_submit_rejected_with_429_when_rate_ratio_exceeded(backpressure_client, monkeypatch):
    client, engine, storage_path = backpressure_client
    repo_id = _create_repo(engine, storage_path, "backpressure-ratio")

    monkeypatch.setenv("AETHER_MAX_COHERENCE_BACKLOG", "9999")
    monkeypatch.setenv("AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT", "0.9")
    monkeypatch.setenv("AETHER_RATIO_CONTROL_MIN_MU_TPS", "0.1")
    monkeypatch.setenv("AETHER_RATIO_CONTROL_MIN_MU_EVENTS", "1")
    monkeypatch.setenv("AETHER_COHERENCE_WINDOW_SECONDS", "60")

    def _physics_snapshot(_cls, _session, *, window_seconds: int = 60):
        return {
            "window_seconds": window_seconds,
            "lambda_tps": 15.0,
            "mu_tps": 10.0,
            "events": {
                "submit_accepted": 900,
                "status_promoted": 500,
                "status_rejected": 100,
                "semantic_fallback_hits": 0,
            },
            "queue_depth": {
                "active": 42,
                "ingested": 20,
                "pending": 20,
                "evaluating": 2,
                "ingested_pending": 40,
            },
            "fallback_rate": 0.0,
            "status_counts": {
                "promoted": 1000,
                "rejected": 100,
                "ingested": 20,
                "pending": 20,
                "evaluating": 2,
            },
        }

    monkeypatch.setattr(
        ProposalService,
        "get_system_physics_snapshot",
        classmethod(_physics_snapshot),
    )

    response = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent_ratio",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/c.py b/src/c.py",
            "affected_paths": ["src/c.py"],
            "confidence_score": 0.9,
        },
    )

    assert response.status_code == 429
    payload = response.json()["detail"]
    assert payload["throttle_reason"] == "accepted_rate_ratio_exceeded"
    assert payload["lambda_tps"] > payload["lambda_limit_tps"]
    assert payload["ratio_limit"] == 0.9


def test_submit_rejected_with_429_when_path_hotspot_exceeded(backpressure_client, monkeypatch):
    client, engine, storage_path = backpressure_client
    repo_id = _create_repo(engine, storage_path, "backpressure-path-hotspot")

    monkeypatch.setenv("AETHER_MAX_COHERENCE_BACKLOG", "9999")
    monkeypatch.setenv("AETHER_MAX_PATH_ACTIVE_PROPOSALS", "2")
    monkeypatch.setenv("AETHER_COHERENCE_WINDOW_SECONDS", "60")

    with Session(engine) as session:
        session.add_all(
            [
                Proposal(
                    repo_id=repo_id,
                    agent_id="agent_hot_1",
                    change_content="noop",
                    affected_paths='["src/hot.py"]',
                    status=ProposalStatus.INGESTED,
                ),
                Proposal(
                    repo_id=repo_id,
                    agent_id="agent_hot_2",
                    change_content="noop",
                    affected_paths='["src/hot.py"]',
                    status=ProposalStatus.PENDING,
                ),
            ]
        )
        session.commit()

    response = client.post(
        f"/v1/repos/{repo_id}/proposals",
        json={
            "agent_id": "agent_hot_submit",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/hot.py b/src/hot.py",
            "affected_paths": ["src/hot.py"],
            "confidence_score": 0.8,
        },
    )

    assert response.status_code == 429
    payload = response.json()["detail"]
    assert payload["throttle_reason"] == "path_hotspot_exceeded"
    assert payload["hottest_path"] == "src/hot.py"
    assert payload["hottest_count"] >= 2
    assert payload["max_path_active_proposals"] == 2


def test_ratio_cooldown_is_scoped_per_repository(backpressure_client, monkeypatch):
    client, engine, storage_path = backpressure_client
    repo_hot = _create_repo(engine, storage_path, "backpressure-hot")
    repo_cold = _create_repo(engine, storage_path, "backpressure-cold")

    monkeypatch.setenv("AETHER_MAX_COHERENCE_BACKLOG", "9999")
    monkeypatch.setenv("AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT", "0.9")
    monkeypatch.setenv("AETHER_RATIO_CONTROL_MIN_MU_TPS", "0.1")
    monkeypatch.setenv("AETHER_RATIO_CONTROL_MIN_MU_EVENTS", "1")
    monkeypatch.setenv("AETHER_COHERENCE_WINDOW_SECONDS", "60")

    def _hot_physics(_cls, _session, *, window_seconds: int = 60):
        return {
            "window_seconds": window_seconds,
            "lambda_tps": 15.0,
            "mu_tps": 10.0,
            "events": {
                "submit_accepted": 900,
                "status_promoted": 500,
                "status_rejected": 100,
                "semantic_fallback_hits": 0,
            },
            "queue_depth": {
                "active": 0,
                "ingested": 0,
                "pending": 0,
                "evaluating": 0,
                "ingested_pending": 0,
            },
            "fallback_rate": 0.0,
            "status_counts": {
                "promoted": 1000,
                "rejected": 100,
                "ingested": 0,
                "pending": 0,
                "evaluating": 0,
            },
        }

    monkeypatch.setattr(
        ProposalService,
        "get_system_physics_snapshot",
        classmethod(_hot_physics),
    )

    hot_response = client.post(
        f"/v1/repos/{repo_hot}/proposals",
        json={
            "agent_id": "agent_hot",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/hot.py b/src/hot.py",
            "affected_paths": ["src/hot.py"],
            "confidence_score": 0.9,
        },
    )
    assert hot_response.status_code == 429
    assert hot_response.json()["detail"]["throttle_reason"] == "accepted_rate_ratio_exceeded"

    def _cold_physics(_cls, _session, *, window_seconds: int = 60):
        return {
            "window_seconds": window_seconds,
            "lambda_tps": 2.0,
            "mu_tps": 10.0,
            "events": {
                "submit_accepted": 120,
                "status_promoted": 500,
                "status_rejected": 100,
                "semantic_fallback_hits": 0,
            },
            "queue_depth": {
                "active": 0,
                "ingested": 0,
                "pending": 0,
                "evaluating": 0,
                "ingested_pending": 0,
            },
            "fallback_rate": 0.0,
            "status_counts": {
                "promoted": 1000,
                "rejected": 100,
                "ingested": 0,
                "pending": 0,
                "evaluating": 0,
            },
        }

    monkeypatch.setattr(
        ProposalService,
        "get_system_physics_snapshot",
        classmethod(_cold_physics),
    )

    cold_response = client.post(
        f"/v1/repos/{repo_cold}/proposals",
        json={
            "agent_id": "agent_cold",
            "change_type": "unified_diff",
            "change_content": "diff --git a/src/cold.py b/src/cold.py",
            "affected_paths": ["src/cold.py"],
            "confidence_score": 0.8,
        },
    )
    assert cold_response.status_code == 200
