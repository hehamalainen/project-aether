"""Tests for continuous swarm harness reliability and SLO evaluation."""

import asyncio
from collections import Counter
import json
import os

import pytest
from sqlmodel import SQLModel, Session, create_engine

from packages.aether.api import routes as api_routes
from packages.aether.models.entities import ProposalStatus, SystemMetricEventType
from packages.aether.services.swarm_harness import (
    ContinuousHarnessConfig,
    ContinuousHarnessMetrics,
    ContinuousHarnessReport,
    ContinuousSwarmHarness,
    ContinuousHarnessThresholds,
    is_transient_external_commit_status,
    run_continuous_swarm_harness,
)


def test_external_commit_transient_status_classification() -> None:
    for status_code in (409, 423, 429, 503):
        assert is_transient_external_commit_status(status_code)

    for status_code in (400, 404, 500):
        assert not is_transient_external_commit_status(status_code)


@pytest.mark.asyncio
async def test_continuous_swarm_harness_smoke() -> None:
    """Small continuous run should ingest and promote at least some proposals."""
    config = ContinuousHarnessConfig(
        num_agents=30,
        num_files=40,
        file_lines=80,
        run_seconds=2.0,
        drain_seconds=1.5,
        contention_mode="low",
        agent_interval_min_ms=40,
        agent_interval_max_ms=120,
        max_inflight_submissions=30,
        promotion_batch_size=15,
        promotion_interval_ms=30,
        monitor_interval_ms=200,
        adaptive_intake_throttle=False,
    )
    thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=10.0,
        min_promotion_throughput=0.05,
        max_submit_p95_ms=600.0,
        max_conflict_rate=0.90,
        max_backlog_end=5000,
        max_submit_failure_rate=0.50,
    )

    report = await run_continuous_swarm_harness(config=config, thresholds=thresholds, seed=123)

    assert report.metrics.submitted_success > 0
    assert report.metrics.promotions_created >= 1
    assert report.metrics.proposals_promoted >= 1
    assert report.metrics.proposals_completed >= report.metrics.proposals_promoted
    assert report.metrics.submit_throughput > 0
    assert report.metrics.completion_throughput >= 0
    assert report.metrics.lambda_events_window >= 0
    assert report.metrics.mu_events_window >= 0
    assert report.metrics.lambda_tps_window >= 0
    assert report.metrics.mu_tps_window >= 0
    assert report.metrics.fallback_rate_window >= 0
    assert report.metrics.p95_enqueue_to_ack_latency_ms > 0
    assert report.metrics.p95_ack_to_promote_latency_ms >= 0
    assert report.metrics.intake_deferred_submissions >= 0
    assert report.metrics.intake_backoff_events >= 0
    assert report.metrics.intake_backoff_seconds_total >= 0.0
    assert 0.0 <= report.metrics.intake_admission_rate_avg <= 1.0
    assert isinstance(report.metrics.intake_controller_reason_counts, dict)
    assert isinstance(report.metrics.intake_controller_reason_seconds, dict)
    assert report.metrics.intake_controller_transition_count >= 0
    assert isinstance(report.metrics.intake_controller_transition_samples, list)
    assert isinstance(report.metrics.intake_controller_latest_state, dict)
    assert report.metrics.submit_queue_peak >= 0
    assert isinstance(report.metrics.submit_buckets_5m, list)
    assert report.config_fingerprint
    assert "config" in report.config_fingerprint_payload
    assert "thresholds" in report.config_fingerprint_payload
    assert report.git_commit_sha
    assert report.git_commit_short
    assert "fallback_rate_ok" in report.checks
    assert "rho_sample_ok" in report.checks


@pytest.mark.asyncio
async def test_continuous_swarm_harness_api_route_backpressure() -> None:
    """API-route mode should stay healthy under a very low backlog cap."""
    previous_max = os.getenv("AETHER_MAX_COHERENCE_BACKLOG")
    previous_resume = os.getenv("AETHER_BACKLOG_RESUME_LEVEL")
    os.environ["AETHER_MAX_COHERENCE_BACKLOG"] = "25"
    os.environ["AETHER_BACKLOG_RESUME_LEVEL"] = "15"
    try:
        config = ContinuousHarnessConfig(
            num_agents=80,
            num_files=40,
            file_lines=40,
            run_seconds=4.0,
            drain_seconds=1.0,
            contention_mode="low",
            agent_interval_min_ms=1,
            agent_interval_max_ms=8,
            max_inflight_submissions=160,
            promotion_batch_size=12,
            promotion_interval_ms=40,
            monitor_interval_ms=100,
            target_mode="testclient",
            adaptive_intake_throttle=False,
        )
        thresholds = ContinuousHarnessThresholds(
            min_submit_throughput=5.0,
            min_promotion_throughput=0.01,
            max_submit_p95_ms=1000.0,
            max_conflict_rate=0.99,
            max_backlog_end=10000,
            max_submit_failure_rate=0.10,
        )

        report = await run_continuous_swarm_harness(config=config, thresholds=thresholds, seed=44)
    finally:
        if previous_max is None:
            os.environ.pop("AETHER_MAX_COHERENCE_BACKLOG", None)
        else:
            os.environ["AETHER_MAX_COHERENCE_BACKLOG"] = previous_max
        if previous_resume is None:
            os.environ.pop("AETHER_BACKLOG_RESUME_LEVEL", None)
        else:
            os.environ["AETHER_BACKLOG_RESUME_LEVEL"] = previous_resume

    assert report.metrics.submitted_success > 0
    assert report.metrics.submit_failure_rate <= 0.10
    assert report.metrics.submit_throttled >= 0


def test_api_route_mode_uses_separate_event_bus(tmp_path) -> None:
    """Route-path submissions must not share the harness EventBus across loops."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        harness = ContinuousSwarmHarness(
            session=session,
            storage_path=tmp_path / "aether_storage",
            config=ContinuousHarnessConfig(target_mode="testclient"),
            thresholds=ContinuousHarnessThresholds(),
        )

    assert harness._api_client is not None
    assert harness._api_event_bus is not None
    assert harness._api_event_bus is not harness.event_bus
    assert api_routes._event_bus is harness._api_event_bus


@pytest.mark.asyncio
async def test_continuous_swarm_harness_threshold_failure() -> None:
    """Impossible threshold should mark harness as failed deterministically."""
    config = ContinuousHarnessConfig(
        num_agents=20,
        num_files=20,
        run_seconds=1.5,
        drain_seconds=2.0,
        agent_interval_min_ms=60,
        agent_interval_max_ms=120,
        max_inflight_submissions=20,
        promotion_batch_size=10,
        promotion_interval_ms=40,
        monitor_interval_ms=200,
    )
    thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=1_000_000.0,
        min_promotion_throughput=0.0,
        max_submit_p95_ms=10_000.0,
        max_conflict_rate=1.0,
        max_backlog_end=1_000_000,
        max_submit_failure_rate=1.0,
    )

    report = await run_continuous_swarm_harness(config=config, thresholds=thresholds, seed=7)

    assert report.passed is False
    assert report.checks["submit_throughput_ok"] is False


@pytest.mark.asyncio
async def test_adaptive_intake_throttle_honors_global_backoff() -> None:
    """Shared permit controller should defer submits while no permit is available."""
    harness = object.__new__(ContinuousSwarmHarness)
    harness.config = ContinuousHarnessConfig(
        adaptive_intake_throttle=True,
        intake_throttle_check_interval_ms=40,
    )
    harness.metrics = ContinuousHarnessMetrics()
    harness._submit_stop = asyncio.Event()
    harness._admission_permits = asyncio.Semaphore(0)

    async def stop_soon() -> None:
        await asyncio.sleep(0.06)
        harness._submit_stop.set()
        harness._admission_permits.release()

    stopper = asyncio.create_task(stop_soon())
    allowed = await harness._apply_adaptive_intake_throttle()
    await stopper

    assert allowed is False
    assert harness.metrics.intake_deferred_submissions == 1
    assert harness.metrics.throttle_wait_seconds > 0.0


def test_promotion_throughput_check_uses_submit_phase_completion_rate() -> None:
    """Promotion gate should evaluate service rate against the active intake window."""
    harness = object.__new__(ContinuousSwarmHarness)
    harness.thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=0.8,
        min_promotion_throughput=0.8,
        max_submit_p95_ms=5000.0,
        max_conflict_rate=1.0,
        max_backlog_end=500,
        max_submit_failure_rate=1.0,
        max_window_rho=1.0,
    )
    harness.metrics = ContinuousHarnessMetrics(
        submit_phase_seconds=900.0,
        total_phase_seconds=1200.0,
        submitted_success=801,
        proposals_completed=801,
        proposals_promoted=711,
        submit_latencies_ms=[2000.0],
        backlog_end=0,
        lambda_events_window=10,
        mu_events_window=10,
        rho_window=1.0,
        rho_sample_ok=True,
    )

    checks = ContinuousSwarmHarness._evaluate_checks(harness)

    assert harness.metrics.promotion_throughput < 0.8
    assert harness.metrics.completion_throughput_submit_phase >= 0.8
    assert checks["promotion_throughput_ok"] is True


def test_intake_controller_rollup_tracks_limiters_and_transitions() -> None:
    """Controller telemetry should preserve limiter attribution and latest state."""
    harness = object.__new__(ContinuousSwarmHarness)
    harness.metrics = ContinuousHarnessMetrics()
    harness._started_perf = 100.0
    harness._current_backlog = 140
    harness._submit_queue = asyncio.Queue()
    harness._submit_queue.put_nowait(("agent-1", 1))
    harness._admission_credit = 1.5
    harness._latest_lambda_tps = 0.9
    harness._latest_mu_tps = 0.8
    harness._latest_lambda_events = 9
    harness._latest_mu_events = 8
    harness._intake_controller_last_signature = None

    ContinuousSwarmHarness._record_intake_controller_state(
        harness,
        now=112.0,
        elapsed=2.0,
        active_limiters=["ratio_limit", "backlog_taper"],
        target_tps=0.72,
        normalized_rate=0.6,
        permits_to_release=1,
        in_backoff=False,
        pressure=0.4,
        observed_limit=0.72,
    )

    payload = ContinuousHarnessReport(
        config=ContinuousHarnessConfig(),
        thresholds=ContinuousHarnessThresholds(),
        metrics=harness.metrics,
        checks={"submit_throughput_ok": True},
        passed=True,
        seed=1,
        started_at=100.0,
        completed_at=112.0,
    ).to_dict()["metrics"]

    assert payload["intake_controller_reason_counts"] == {"backlog_taper": 1, "ratio_limit": 1}
    assert payload["intake_controller_reason_seconds"] == {"backlog_taper": 2.0, "ratio_limit": 2.0}
    assert payload["intake_controller_transition_count"] == 1
    assert payload["intake_controller_transition_samples"][0]["limiters"] == ["backlog_taper", "ratio_limit"]
    assert payload["intake_controller_latest_state"]["target_tps"] == 0.72
    assert payload["intake_controller_latest_state"]["submit_queue_depth"] == 1


def test_submit_buckets_include_quarantine_phase_breakdown() -> None:
    """Bucket diagnostics should split snapshot and materialization quarantines."""
    harness = object.__new__(ContinuousSwarmHarness)
    harness.repo = object()
    harness._submit_observations = [
        {
            "offset_seconds": 10.0,
            "submit_latency_ms": 15.0,
            "route_handler_ms": 3.0,
            "db_write_ms": 1.0,
            "conflict_or_enqueue_ms": 0.5,
        }
    ]
    harness._load_completion_events = lambda started_at: [  # type: ignore[method-assign]
        (20.0, SystemMetricEventType.STATUS_QUARANTINED, "individual_promotion_failed:create_snapshot: RecursionError"),
        (30.0, SystemMetricEventType.STATUS_QUARANTINED, "individual_promotion_failed:materialize_gate_workspace: RecursionError"),
        (40.0, SystemMetricEventType.STATUS_PROMOTED, None),
    ]

    buckets = ContinuousSwarmHarness._build_submit_buckets(harness, 0.0)

    assert len(buckets) == 1
    assert buckets[0]["quarantines"] == 2
    assert buckets[0]["create_snapshot_quarantines"] == 1
    assert buckets[0]["materialize_gate_workspace_quarantines"] == 1
    assert buckets[0]["quarantine_phase_counts"] == {
        "create_snapshot": 1,
        "materialize_gate_workspace": 1,
    }


def test_quarantine_reason_rollup_keeps_exact_reason_examples() -> None:
    """Harness report should preserve exact quarantine reasons for dominant phases."""
    harness = object.__new__(ContinuousSwarmHarness)
    harness.repo = object()
    harness.metrics = ContinuousHarnessMetrics(
        status_counts={ProposalStatus.QUARANTINED.value: 3},
        submit_phase_seconds=10.0,
        total_phase_seconds=10.0,
    )
    harness.thresholds = ContinuousHarnessThresholds()
    harness._submit_observations = []
    harness._open_runtime_session = lambda: (None, False)  # type: ignore[method-assign]
    harness._count_conflict_sets = lambda session: 0  # type: ignore[method-assign]
    harness._status_counts = lambda session: {ProposalStatus.QUARANTINED.value: 3}  # type: ignore[method-assign]
    harness._build_submit_buckets = lambda started_at: []  # type: ignore[method-assign]
    harness._build_config_fingerprint_payload = lambda: {"config": {}, "thresholds": {}}  # type: ignore[method-assign]
    harness._compute_config_fingerprint = lambda payload: "fp"  # type: ignore[method-assign]
    harness._resolve_git_metadata = lambda: ("sha", "short", True)  # type: ignore[method-assign]
    harness._evaluate_checks = lambda: {"submit_throughput_ok": True}  # type: ignore[method-assign]
    harness._load_completion_events = lambda started_at: [  # type: ignore[method-assign]
        (20.0, SystemMetricEventType.STATUS_QUARANTINED, "individual_promotion_failed:mark_evaluating: OperationalError: db is locked @ proposal_service.py:update_status:1"),
        (21.0, SystemMetricEventType.STATUS_QUARANTINED, "individual_promotion_failed:mark_evaluating: OperationalError: db is locked @ proposal_service.py:update_status:1"),
        (22.0, SystemMetricEventType.STATUS_QUARANTINED, "individual_promotion_failed:create_snapshot: RecursionError: maximum recursion depth exceeded @ annotation.py:_gen_annotations_cache_key:97"),
    ]

    report = ContinuousHarnessReport(
        config=ContinuousHarnessConfig(),
        thresholds=ContinuousHarnessThresholds(),
        metrics=harness.metrics,
        checks={"submit_throughput_ok": True},
        passed=True,
        seed=1,
        started_at=0.0,
        completed_at=1.0,
    )
    # Mimic the end-of-run rollup path.
    quarantine_reason_counts = Counter()
    quarantine_reason_counts_exact = Counter()
    quarantine_reason_examples = {}
    for _, event_type, event_reason in harness._load_completion_events(0.0):
        if event_type != SystemMetricEventType.STATUS_QUARANTINED:
            continue
        phase = harness._quarantine_phase_from_reason(event_reason)
        quarantine_reason_counts[phase] += 1
        if event_reason:
            quarantine_reason_counts_exact[event_reason] += 1
            quarantine_reason_examples.setdefault(phase, event_reason)
    report.metrics.quarantine_reasons_by_phase = dict(quarantine_reason_counts)
    report.metrics.quarantine_reasons_exact = dict(quarantine_reason_counts_exact.most_common(10))
    report.metrics.quarantine_reason_example_by_phase = quarantine_reason_examples

    payload = report.to_dict()["metrics"]
    assert payload["quarantine_reasons_by_phase"] == {"mark_evaluating": 2, "create_snapshot": 1}
    assert payload["quarantine_reason_example_by_phase"]["mark_evaluating"].startswith(
        "individual_promotion_failed:mark_evaluating: OperationalError"
    )
    assert payload["quarantine_reasons_exact"][
        "individual_promotion_failed:mark_evaluating: OperationalError: db is locked @ proposal_service.py:update_status:1"
    ] == 2


@pytest.mark.skipif(
    os.getenv("AETHER_ENABLE_CONTINUOUS_SWARM_TESTS") != "1",
    reason="Set AETHER_ENABLE_CONTINUOUS_SWARM_TESTS=1 to run 1000-agent continuous profile",
)
@pytest.mark.asyncio
async def test_continuous_swarm_harness_1000_agents_profile() -> None:
    """Optional high-scale profile for sustained 1000-agent flow."""
    config = ContinuousHarnessConfig(
        num_agents=1000,
        num_files=500,
        file_lines=100,
        run_seconds=60.0,
        drain_seconds=20.0,
        contention_mode="low",
        agent_interval_min_ms=400,
        agent_interval_max_ms=1600,
        max_inflight_submissions=300,
        promotion_batch_size=30,
        promotion_interval_ms=40,
        monitor_interval_ms=500,
    )
    thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=40.0,
        min_promotion_throughput=0.2,
        max_submit_p95_ms=140.0,
        max_conflict_rate=0.35,
        max_backlog_end=2000,
        max_submit_failure_rate=0.02,
    )

    report = await run_continuous_swarm_harness(config=config, thresholds=thresholds, seed=2026)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))

    assert report.passed is True



@pytest.mark.asyncio
async def test_continuous_swarm_harness_external_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run swarm harness in target_mode=external to validate the Rust HTTP API path."""
    import httpx
    import asyncio
    import traceback
    
    submit_calls = []
    commit_calls = []
    events_published = []
    
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self._json_data = json_data
            self.status_code = status_code
            self.text = ""
            self.headers = {}
        def json(self):
            return self._json_data
        def raise_for_status(self):
            pass

    original_init = ContinuousSwarmHarness.__init__
    
    active_harness = None
    def patched_init(self, *args, **kwargs):
        nonlocal active_harness
        original_init(self, *args, **kwargs)
        active_harness = self
        
        original_publish = self.event_bus.publish
        async def mock_publish(event):
            events_published.append(event.event_type)
            await original_publish(event)
        self.event_bus.publish = mock_publish
        
    monkeypatch.setattr(ContinuousSwarmHarness, "__init__", patched_init)

    async def mock_async_post(self, url, *args, **kwargs):
        commit_calls.append(url)
        return MockResponse({
            "promotion_id": "prom_mock123",
            "new_head_id": "head_mock123",
            "new_head_sequence": 2
        })
        
    def mock_sync_post(self, url, *args, **kwargs):
        if "proposals" in str(url):
            submit_calls.append(url)
            payload = kwargs.get("json")
            if payload and active_harness:
                from packages.aether.models.entities import ProposalCreate
                from packages.aether.services.proposal_service import ProposalService
                try:
                    with active_harness.session_factory() as session:
                        temp_svc = ProposalService(session, active_harness.event_bus, active_harness.repo_service)
                        response, events, _, _ = temp_svc._submit_proposal_sync(
                            repo_id=str(url).split("/")[-2],
                            proposal_data=ProposalCreate(**payload)
                        )
                        return MockResponse({
                            "proposal_id": response.proposal_id,
                            "id": response.proposal_id,
                            "status": response.status.value
                        })
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    raise
            
            return MockResponse({
                "proposal_id": "prop_mock123",
                "id": "prop_mock123",
                "status": "PENDING"
            })
        return MockResponse({})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_async_post)
    monkeypatch.setattr(httpx.Client, "post", mock_sync_post)

    import os
    if "AETHER_HARNESS_RUNTIME_SEPARATE_ENGINE" in os.environ:
        del os.environ["AETHER_HARNESS_RUNTIME_SEPARATE_ENGINE"]
    config = ContinuousHarnessConfig(
        num_agents=2,
        num_files=10,
        run_seconds=1.0,
        drain_seconds=2.0,
        target_mode="external",
        external_api_url="http://mocked",
        promotion_batch_size=1,
    )
    thresholds = ContinuousHarnessThresholds()
    report = await run_continuous_swarm_harness(config=config, thresholds=thresholds, seed=42)
    

    
    assert report.metrics.submit_failures == 0, f"Submit failures > 0! metrics: {report.metrics}"
    assert len(submit_calls) > 0, "External submit path was not exercised" 
    assert len(commit_calls) > 0, "External commit path was not exercised" 
    assert report.metrics.submitted_success > 0, "No proposals submitted successfully"
    assert report.metrics.promotions_created >= 1 or report.metrics.proposals_promoted >= 1, "No promotions were created"
    assert "proposal.promoted" in events_published, "Event path was not hit"
