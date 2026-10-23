"""API coverage for system queue-physics telemetry."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlmodel import SQLModel, Session, create_engine

from packages.aether.api.routes import router, set_services
from packages.aether.models.entities import (
    Proposal,
    ProposalSource,
    ProposalStatus,
    SystemMetricEvent,
    SystemMetricEventType,
)
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService


@pytest.fixture
def physics_client(tmp_path: Path):
    """Build API client backed by file SQLite for system telemetry endpoint tests."""
    db_path = tmp_path / "physics.db"
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


def test_system_physics_endpoint_reports_rates_and_queue_depth(physics_client):
    client, engine, storage_path = physics_client

    now = datetime.now(UTC)
    with Session(engine) as session:
        repo_service = RepositoryService(session, storage_path)
        repo = repo_service.create_repository(name="physics-api")
        manual_ids: list[str] = []
        mx_ids: list[str] = []

        for i in range(60):
            proposal = Proposal(
                repo_id=repo.id,
                agent_id=f"agent_ing_{i}",
                change_content="ingested",
                affected_paths='["src/a.py"]',
                status=ProposalStatus.INGESTED,
                source=ProposalSource.SYNTHETIC_HARNESS.value,
            )
            session.add(proposal)
            mx_ids.append(proposal.id)
        for i in range(40):
            proposal = Proposal(
                repo_id=repo.id,
                agent_id=f"agent_pen_{i}",
                change_content="pending",
                affected_paths='["src/b.py"]',
                status=ProposalStatus.PENDING,
                source=ProposalSource.MANUAL.value,
            )
            session.add(proposal)
            manual_ids.append(proposal.id)

        metric_rows = (
            [
                SystemMetricEvent(
                    repo_id=repo.id,
                    proposal_id=proposal_id,
                    event_type=SystemMetricEventType.SUBMIT_ACCEPTED,
                    created_at=now,
                )
                for proposal_id in mx_ids[:60] + manual_ids[:40]
            ]
            + [
                SystemMetricEvent(
                    repo_id=repo.id,
                    proposal_id=proposal_id,
                    event_type=SystemMetricEventType.STATUS_PROMOTED,
                    created_at=now,
                )
                for proposal_id in mx_ids[:20]
            ]
            + [
                SystemMetricEvent(
                    repo_id=repo.id,
                    proposal_id=proposal_id,
                    event_type=SystemMetricEventType.STATUS_REJECTED,
                    created_at=now,
                )
                for proposal_id in manual_ids[:5]
            ]
            + [
                SystemMetricEvent(
                    repo_id=repo.id,
                    proposal_id=mx_ids[0],
                    event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                    event_reason="non_python_symbol_parse_failed",
                    path="src/a.py",
                    created_at=now,
                )
                for _ in range(7)
            ]
            + [
                SystemMetricEvent(
                    repo_id=repo.id,
                    proposal_id=mx_ids[1],
                    event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                    event_reason="non_python_symbol_parse_failed",
                    path="src/web.ts",
                    created_at=now,
                )
                for _ in range(5)
            ]
        )
        session.add_all(metric_rows)
        session.commit()

    response = client.get("/v1/system/physics", params={"window_seconds": 60})
    assert response.status_code == 200
    payload = response.json()

    assert payload["events"]["submit_accepted"] == 100
    assert payload["events"]["status_promoted"] == 20
    assert payload["events"]["status_rejected"] == 5
    assert payload["events"]["status_superseded"] == 0
    assert payload["events"]["semantic_fallback_hits"] == 12
    assert payload["semantic_fallback"]["hits"] == 12
    assert payload["semantic_fallback"]["by_language"]["python"] == 7
    assert payload["semantic_fallback"]["by_language"]["typescript"] == 5
    assert payload["semantic_fallback"]["by_path"]["src/a.py"] == 7
    assert payload["semantic_fallback"]["by_path"]["src/web.ts"] == 5
    assert payload["semantic_fallback"]["by_reason"]["non_python_symbol_parse_failed"] == 12
    assert payload["queue_depth"]["ingested"] == 60
    assert payload["queue_depth"]["pending"] == 40
    assert payload["queue_depth"]["ingested_pending"] == 100
    assert payload["by_source"]["synthetic_harness"]["events"]["submit_accepted"] == 60
    assert payload["by_source"]["synthetic_harness"]["events"]["status_promoted"] == 20
    assert payload["by_source"]["manual"]["events"]["submit_accepted"] == 40
    assert payload["by_source"]["manual"]["events"]["status_rejected"] == 5
    assert payload["by_source"]["synthetic_harness"]["status_counts"]["INGESTED"] == 60
    assert payload["by_source"]["manual"]["status_counts"]["PENDING"] == 40
    assert payload["lambda_tps"] == pytest.approx(100 / 60.0, rel=1e-3)
    assert payload["mu_tps"] == pytest.approx(25 / 60.0, rel=1e-3)
    assert payload["fallback_rate"] == pytest.approx(12 / 100.0, rel=1e-3)
    assert payload["query_latency_ms"] < 20.0
