import multiprocessing

from sqlmodel import SQLModel, Session, create_engine

from apps.aether_server.promotion_worker import (
    AutoPromotionConfig,
    WorkerHandle,
    _list_active_repo_ids,
    start_worker,
    stop_worker,
)
from packages.aether.models.entities import Proposal, ProposalStatus, Repository


def test_autopromotion_config_defaults(monkeypatch):
    monkeypatch.delenv("AETHER_AUTOPROMOTE_ENABLED", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_BATCH_SIZE", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS", raising=False)

    config = AutoPromotionConfig.from_env()

    assert config.enabled is False
    assert config.interval_seconds == 15
    assert config.repo_timeout_seconds == 30
    assert config.batch_size == 10
    assert config.shutdown_grace_seconds == 8


def test_autopromotion_config_env_overrides(monkeypatch):
    monkeypatch.setenv("AETHER_AUTOPROMOTE_ENABLED", "true")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_BATCH_SIZE", "4")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS", "2")

    config = AutoPromotionConfig.from_env()

    assert config.enabled is True
    assert config.interval_seconds == 5
    assert config.repo_timeout_seconds == 12
    assert config.batch_size == 4
    assert config.shutdown_grace_seconds == 2


def test_autopromotion_config_invalid_ints_clamped_or_default(monkeypatch):
    monkeypatch.setenv("AETHER_AUTOPROMOTE_ENABLED", "yes")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", "abc")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_BATCH_SIZE", "-2")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS", "NaN")

    config = AutoPromotionConfig.from_env()

    # Invalid values use defaults; non-positive values are clamped to minimum 1.
    assert config.enabled is True
    assert config.interval_seconds == 15
    assert config.repo_timeout_seconds == 1
    assert config.batch_size == 1
    assert config.shutdown_grace_seconds == 8


def test_start_worker_returns_none_when_disabled(tmp_path):
    config = AutoPromotionConfig(enabled=False)

    handle = start_worker(
        database_url="sqlite:///:memory:",
        storage_path=tmp_path,
        config=config,
    )

    assert handle is None


def test_stop_worker_noop_for_dead_process():
    # Build a handle around an unstarted process; stop should no-op without error.
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=lambda: None)
    event = ctx.Event()
    handle = WorkerHandle(
        process=process,
        stop_event=event,
        config=AutoPromotionConfig(enabled=True, shutdown_grace_seconds=1),
    )

    stop_worker(handle)


def test_list_active_repo_ids_only_returns_pending_or_evaluating(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'aether.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        active_repo = Repository(id="repo_active", name="active")
        evaluating_repo = Repository(id="repo_eval", name="evaluating")
        inactive_repo = Repository(id="repo_done", name="done")
        session.add(active_repo)
        session.add(evaluating_repo)
        session.add(inactive_repo)
        session.add(
            Proposal(
                id="prop_pending",
                repo_id=active_repo.id,
                agent_id="agent",
                change_content="diff",
                affected_paths='["a.py"]',
                dependencies="[]",
                status=ProposalStatus.PENDING,
            )
        )
        session.add(
            Proposal(
                id="prop_eval",
                repo_id=evaluating_repo.id,
                agent_id="agent",
                change_content="diff",
                affected_paths='["b.py"]',
                dependencies="[]",
                status=ProposalStatus.EVALUATING,
            )
        )
        session.add(
            Proposal(
                id="prop_done",
                repo_id=inactive_repo.id,
                agent_id="agent",
                change_content="diff",
                affected_paths='["c.py"]',
                dependencies="[]",
                status=ProposalStatus.PROMOTED,
            )
        )
        session.commit()

        repo_ids = _list_active_repo_ids(session)

    assert set(repo_ids) == {"repo_active", "repo_eval"}
