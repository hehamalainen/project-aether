import pytest
import os
import json
import hashlib
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

def test_versioned_filenames_and_output_dir():
    """Aether-13: Qualification runner writes versioned reports to --output-dir."""
    import subprocess
    import sys
    
    qual_script = Path(__file__).resolve().parents[2] / "qualification" / "semantic_suite" / "run.py"
    
    res = subprocess.run(
        [sys.executable, str(qual_script), "--db-profile", "sqlite", "--output-dir", "/tmp"],
        capture_output=True,
        text=True
    )
    assert res.returncode == 0, f"Expected qualification run to pass with --output-dir, got exit code {res.returncode}"

def test_postgres_hard_stop():
    """Aether-14: Postgres failure in qualification hard-stops with exit status 1 (expected RED = 0)."""
    from qualification.semantic_suite import run as qual_run
    import sys
    
    with patch("qualification.semantic_suite.run.create_engine", side_effect=Exception("connection error")):
        with patch("sys.argv", ["run.py", "--db-profile", "postgres"]):
            with pytest.raises(SystemExit) as excinfo:
                qual_run.main()
            assert excinfo.value.code == 1

def test_sqlite_migration_idx_creation():
    """Aether-15: SQLite migration creates the required columns and idx_metric_events_source_run_id index."""
    try:
        from apps.aether_server.main import _ensure_metric_event_columns_and_indices
    except ImportError as e:
        raise AssertionError("ImportError: _ensure_metric_event_columns_and_indices does not exist") from e

    from sqlmodel import create_engine, text
    engine = create_engine("sqlite:///:memory:")
    
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE aether_system_metric_events (
                id INTEGER PRIMARY KEY,
                repo_id TEXT,
                proposal_id TEXT,
                event_type TEXT,
                event_reason TEXT,
                path TEXT,
                created_at TIMESTAMP
            )
        """))
        
    _ensure_metric_event_columns_and_indices(engine)
    # Double invocation to verify idempotence
    _ensure_metric_event_columns_and_indices(engine)
    
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info('aether_system_metric_events')")).fetchall()
        columns = {row[1] for row in rows}
        assert "synthesis_engine" in columns, "synthesis_engine column not created"
        assert "source_run_id" in columns, "source_run_id column not created"
        
        indices = conn.execute(text("PRAGMA index_list('aether_system_metric_events')")).fetchall()
        idx_names = {idx[1] for idx in indices}
        assert "idx_metric_events_source_run_id" in idx_names, "idx_metric_events_source_run_id index not created"

def test_postgres_migration_idx_creation():
    """Aether-16: PostgreSQL migration creates the required columns and idx_metric_events_source_run_id index."""
    try:
        from apps.aether_server.main import _ensure_metric_event_columns_and_indices
    except ImportError as e:
        raise AssertionError("ImportError: _ensure_metric_event_columns_and_indices does not exist") from e

    engine = MagicMock()
    engine.dialect.name = "postgresql"
    
    conn = MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    
    _ensure_metric_event_columns_and_indices(engine)
    # Double invocation to verify idempotence
    _ensure_metric_event_columns_and_indices(engine)
    
    executed_sqls = []
    for call in conn.execute.call_args_list:
        args, kwargs = call
        executed_sqls.append(str(args[0]))
        
    assert any("synthesis_engine" in sql for sql in executed_sqls), "Missing synthesis_engine migration SQL"
    assert any("source_run_id" in sql for sql in executed_sqls), "Missing source_run_id migration SQL"
    assert any("idx_metric_events_source_run_id" in sql for sql in executed_sqls), "Missing index creation SQL"

def test_telemetry_filtering():
    """Aether-17: /repos/{repo_id}/telemetry/events filters correctly by repo, run, group, and time."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session, select
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    from datetime import datetime, timedelta
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        p2 = Proposal(id="p2", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        session.add(p2)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        c2 = ProposalContention(proposal_id="p2", contention_group_id="g2", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        session.add(c2)
        
        now = datetime.fromisoformat("2026-07-18T12:00:00")
        
        ev1 = SystemMetricEvent(
            id=1, repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1", created_at=now - timedelta(minutes=10)
        )
        ev2 = SystemMetricEvent(
            id=2, repo_id="repo1", proposal_id="p2", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason2", source_run_id="run2", created_at=now
        )
        session.add(ev1)
        session.add(ev2)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            since=(now - timedelta(minutes=5)).isoformat(),
            until=(now + timedelta(minutes=5)).isoformat(),
            session=session
        ))
        events = result["events"]
        assert len(events) == 1
        assert str(events[0]["id"]) == "2"

def test_telemetry_verbatim_returned():
    """Aether-18: Telemetry returns stored values verbatim (dishonest values are not normalized)."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    if not hasattr(SystemMetricEvent, "synthesis_engine"):
        raise AssertionError("AssertionError: synthesis_engine column not found on SystemMetricEvent model")

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        ev = SystemMetricEvent(
            id=1, repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="raw un-normalized dishonest string value !",
            synthesis_engine="dishonest_synthesis_engine_123",
            source_run_id="run1"
        )
        session.add(ev)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(repo_id="repo1", session=session))
        events = result["events"]
        assert len(events) == 1
        assert events[0]["event_reason"] == "raw un-normalized dishonest string value !"
        assert events[0]["synthesis_engine"] == "dishonest_synthesis_engine_123"

def test_original_report_byte_immutability():
    """Aether-19: Running qualification must not overwrite or modify original report_sqlite.json (expected RED = 1)."""
    import sys
    from unittest.mock import patch
    
    qual_dir = Path(__file__).resolve().parents[2] / "qualification" / "semantic_suite"
    orig_file = qual_dir / "report_sqlite.json"
    
    sys.path.insert(0, str(qual_dir))
    try:
        import run as qual_run
    except ImportError as e:
        raise AssertionError("ImportError: run.py not found") from e
    finally:
        sys.path.pop(0)
        
    orig_content = orig_file.read_text(encoding="utf-8") if orig_file.exists() else None
    
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("sys.argv", ["run.py", "--db-profile", "sqlite", "--output-dir", tmpdir]):
            try:
                qual_run.main()
            except SystemExit as e:
                if e.code == 2:
                    raise AssertionError("AssertionError: run.py does not support --output-dir argument")
                elif e.code != 0:
                    raise AssertionError(f"AssertionError: run.py failed with exit code {e.code}")
                
    if orig_content is not None:
        assert orig_file.exists(), "Original report_sqlite.json was deleted!"
        assert orig_file.read_text(encoding="utf-8") == orig_content, "Original report_sqlite.json was modified!"
    else:
        assert not orig_file.exists(), "Original report_sqlite.json was created but should not exist!"

def test_fingerprint_redaction():
    """Aether-20: Qualification report contains config fingerprint with allowlist-only keys (secrets redacted)."""
    import sys
    qual_dir = Path(__file__).resolve().parents[2] / "qualification" / "semantic_suite"
    sys.path.insert(0, str(qual_dir))
    try:
        import run as qual_run
    except ImportError as e:
        raise AssertionError("ImportError: run.py not found") from e
    finally:
        sys.path.pop(0)

    if not hasattr(qual_run, "compute_config_fingerprint"):
        raise AssertionError("AssertionError: compute_config_fingerprint not implemented in starting SHA")

    # Calculate baseline fingerprint
    env = {"AETHER_SERVER_PROFILE": "dev"}
    fp1 = qual_run.compute_config_fingerprint(["run.py"], env)

    # Assert secret exclusion: adding secrets doesn't change the fingerprint
    env_with_secrets = {
        "AETHER_SERVER_PROFILE": "dev",
        "DATABASE_URL": "postgresql://user:pass@host/db",
        "AETHER_TOKEN": "secret_token_abc",
        "GITHUB_TOKEN": "github_secret_xyz"
    }
    fp2 = qual_run.compute_config_fingerprint(["run.py"], env_with_secrets)
    assert fp1 == fp2, "Secrets must not affect the fingerprint calculation"

    # Assert fingerprint sensitivity: changing server profile changes the fingerprint
    env_diff_profile = {"AETHER_SERVER_PROFILE": "prod"}
    fp3 = qual_run.compute_config_fingerprint(["run.py"], env_diff_profile)
    assert fp1 != fp3, "Fingerprint must change when approved configuration changes"

    # Check secrets don't leak anywhere in inputs or allowlists
    approved_keys = getattr(qual_run, "approved_env_allowlist", None)
    if approved_keys:
        for secret_key in ("DATABASE_URL", "AETHER_TOKEN", "GITHUB_TOKEN"):
            assert secret_key not in approved_keys, f"Secret {secret_key} leaked in approved allowlist!"

def test_proposal_ids_deduplication():
    """Aether-21: proposal_ids query parameter validation and deduplication on telemetry events."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        
        ev = SystemMetricEvent(
            id=1, repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1"
        )
        session.add(ev)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            proposal_ids=["p1", "p1"],
            session=session
        ))
        events = result["events"]
        assert len(events) == 1
        assert str(events[0]["id"]) == "1"

def test_proposal_less_event_attribution():
    """Aether-22: Homogeneous batches attribute run ID; mixed batches attribute run_member_count & total_batch_size."""
    try:
        from packages.aether.services.proposal_service import ProposalService
        from packages.aether.models.entities import Proposal, SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: ProposalService or entities not found") from e
        
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="agent1", task_id="task1", status="PENDING", source_run_id="run1")
        p2 = Proposal(id="p2", repo_id="repo1", agent_id="agent1", task_id="task1", status="PENDING", source_run_id="run1")
        
        import inspect
        sig = inspect.signature(ProposalService.__init__)
        params = list(sig.parameters.keys())[1:]
        mock_map = {
            "session": session,
            "event_bus": MagicMock(),
            "repo_service": MagicMock(),
        }
        args = [mock_map.get(name, MagicMock()) for name in params if sig.parameters[name].default is inspect.Parameter.empty]
        service = ProposalService(*args)
        service._record_metric_event(
            repo_id="repo1",
            event_type=SystemMetricEventType.BATCH_SIZE_K,
            proposals=[p1, p2]
        )
        session.commit()
        
        events = session.query(SystemMetricEvent).all()
        assert len(events) == 1
        assert events[0].source_run_id == "run1"
        reason = json.loads(events[0].event_reason)
        assert reason["total_batch_size"] == 2
        assert reason["mixed_run"] is False
        
        session.query(SystemMetricEvent).delete()
        session.commit()
        
        p3 = Proposal(id="p3", repo_id="repo1", agent_id="agent1", task_id="task1", status="PENDING", source_run_id="run2")
        service._record_metric_event(
            repo_id="repo1",
            event_type=SystemMetricEventType.BATCH_SIZE_K,
            proposals=[p1, p2, p3]
        )
        session.commit()
        
        events = session.query(SystemMetricEvent).order_by(SystemMetricEvent.source_run_id).all()
        assert len(events) == 2
        
        assert events[0].source_run_id == "run1"
        r1 = json.loads(events[0].event_reason)
        assert r1["total_batch_size"] == 3
        assert r1["mixed_run"] is True
        assert r1["run_member_count"]["run1"] == 2
        assert r1["run_member_count"]["run2"] == 1
        
        assert events[1].source_run_id == "run2"
        r2 = json.loads(events[1].event_reason)
        assert r2["total_batch_size"] == 3
        assert r2["mixed_run"] is True
        assert r2["run_member_count"]["run1"] == 2
        assert r2["run_member_count"]["run2"] == 1

def test_mixed_run_telemetry_filtering():
    """Aether-23: Telemetry endpoint mixed run event filter validation."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        p2 = Proposal(id="p2", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        session.add(p2)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        c2 = ProposalContention(proposal_id="p2", contention_group_id="g2", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        session.add(c2)
        
        ev1 = SystemMetricEvent(
            id=1, repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1"
        )
        ev2 = SystemMetricEvent(
            id=2, repo_id="repo1", proposal_id="p2", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason2", source_run_id="run2"
        )
        session.add(ev1)
        session.add(ev2)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            source_run_id="run1",
            session=session
        ))
        events = result["events"]
        assert len(events) == 1
        assert str(events[0]["id"]) == "1"

def test_telemetry_filters_combination():
    """Aether-24: Combined filters (proposal list + contention group) intersection semantics check."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        p2 = Proposal(id="p2", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        session.add(p2)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        c2 = ProposalContention(proposal_id="p2", contention_group_id="g2", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        session.add(c2)
        
        ev1 = SystemMetricEvent(
            id=1, repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1"
        )
        ev2 = SystemMetricEvent(
            id=2, repo_id="repo1", proposal_id="p2", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason2", source_run_id="run2"
        )
        session.add(ev1)
        session.add(ev2)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            proposal_ids=["p1", "p2"],
            contention_group_id="g1",
            session=session
        ))
        events = result["events"]
        assert len(events) == 1
        assert str(events[0]["id"]) == "1"

def test_get_proposal_provenance_event_correlation():
    """Aether-25: get_proposal_provenance associates synthesis_engine correctly from selected merge event."""
    try:
        from packages.aether.services.proposal_service import ProposalService, ProposalProvenance
        from packages.aether.models.entities import Proposal, ProposalContention, SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: ProposalService or entities not found") from e

    if not hasattr(ProposalProvenance, "synthesis_engine"):
        raise AssertionError("AssertionError: ProposalProvenance missing synthesis_engine field")

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    from datetime import datetime, timedelta
    now = datetime.fromisoformat("2026-07-18T12:00:00")
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        
        # ev1 (older event)
        ev1 = SystemMetricEvent(
            id="met_1", repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.SYNTHESIS_MERGE,
            event_reason="synthesis_applied", synthesis_engine="old_engine", created_at=now - timedelta(minutes=5)
        )
        # ev2 (latest event by time)
        ev2 = SystemMetricEvent(
            id="met_2", repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.SYNTHESIS_MERGE,
            event_reason="synthesis_applied", synthesis_engine="deterministic_simulation", created_at=now
        )
        # ev3 (older event by ID tie-breaker if same time)
        ev3 = SystemMetricEvent(
            id="met_0", repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.SYNTHESIS_MERGE,
            event_reason="synthesis_applied", synthesis_engine="deterministic_simulation", created_at=now
        )
        session.add(ev1)
        session.add(ev2)
        session.add(ev3)
        session.commit()
        
        import inspect
        sig = inspect.signature(ProposalService.__init__)
        params = list(sig.parameters.keys())[1:]
        mock_map = {
            "session": session,
            "event_bus": MagicMock(),
            "repo_service": MagicMock(),
        }
        args = [mock_map.get(name, MagicMock()) for name in params if sig.parameters[name].default is inspect.Parameter.empty]
        service = ProposalService(*args)
        
        provenance = service.get_proposal_provenance("p1")
        assert provenance is not None
        assert provenance.synthesis_engine == "deterministic_simulation"
        assert provenance.created_at == now

def test_telemetry_proposal_id_without_contention_metadata():
    """Verify proposal_ids query parameter filters events without contention metadata."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        # Create a proposal but NO ProposalContention row
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        
        ev = SystemMetricEvent(
            id="met_1", repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1"
        )
        session.add(ev)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            proposal_ids=["p1"],
            session=session
        ))
        events = result["events"]
        assert len(events) == 1
        assert events[0]["id"] == "met_1"

def test_telemetry_combined_filtering_intersection():
    """Verify telemetry combined group/proposal filtering is an intersection."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        p1 = Proposal(id="p1", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        p2 = Proposal(id="p2", repo_id="repo1", agent_id="a1", task_id="t1", status="PENDING")
        session.add(p1)
        session.add(p2)
        
        c1 = ProposalContention(proposal_id="p1", contention_group_id="g1", contention_policy="semantic_merge", defer_evaluation=True)
        c2 = ProposalContention(proposal_id="p2", contention_group_id="g2", contention_policy="semantic_merge", defer_evaluation=True)
        session.add(c1)
        session.add(c2)
        
        ev1 = SystemMetricEvent(
            id="met_1", repo_id="repo1", proposal_id="p1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason1", source_run_id="run1"
        )
        ev2 = SystemMetricEvent(
            id="met_2", repo_id="repo1", proposal_id="p2", event_type=SystemMetricEventType.BATCH_SIZE_K,
            event_reason="reason2", source_run_id="run2"
        )
        session.add(ev1)
        session.add(ev2)
        session.commit()
        
        # intersection: p1 in proposal_ids, but contention group is g2. Intersection should be empty!
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            proposal_ids=["p1"],
            contention_group_id="g2",
            session=session
        ))
        assert len(result["events"]) == 0

def test_telemetry_ordering_created_at_id():
    """Verify telemetry results are ordered by (created_at, id) ascending."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    from datetime import datetime, timedelta
    now = datetime.fromisoformat("2026-07-18T12:00:00")
    
    with Session(engine) as session:
        # ev1 has later created_at but earlier ID
        ev1 = SystemMetricEvent(
            id="met_a", repo_id="repo1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            created_at=now + timedelta(minutes=5)
        )
        # ev2 has earlier created_at but later ID
        ev2 = SystemMetricEvent(
            id="met_b", repo_id="repo1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            created_at=now
        )
        # ev3 has same created_at as ev2 but earlier ID
        ev3 = SystemMetricEvent(
            id="met_9", repo_id="repo1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            created_at=now
        )
        session.add(ev1)
        session.add(ev2)
        session.add(ev3)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(repo_id="repo1", session=session))
        events = result["events"]
        assert len(events) == 3
        # Should be ordered by created_at first, then ID
        assert events[0]["id"] == "met_9"
        assert events[1]["id"] == "met_b"
        assert events[2]["id"] == "met_a"

def test_telemetry_since_until_inclusive_boundaries():
    """Verify that filtering by since/until has inclusive boundaries."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    from datetime import datetime
    now = datetime.fromisoformat("2026-07-18T12:00:00")
    
    with Session(engine) as session:
        ev = SystemMetricEvent(
            id="met_1", repo_id="repo1", event_type=SystemMetricEventType.BATCH_SIZE_K,
            created_at=now
        )
        session.add(ev)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(
            repo_id="repo1",
            since=now.isoformat(),
            until=now.isoformat(),
            session=session
        ))
        assert len(result["events"]) == 1

def test_telemetry_cross_repo_isolation():
    """Verify cross-repository telemetry isolation."""
    try:
        from packages.aether.api.routes import get_telemetry_events
        from packages.aether.models.entities import SystemMetricEvent, SystemMetricEventType
        from sqlmodel import SQLModel, create_engine, Session
    except ImportError as e:
        raise AssertionError("ImportError: get_telemetry_events or models not found") from e

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    
    with Session(engine) as session:
        ev1 = SystemMetricEvent(
            id="met_1", repo_id="repo1", event_type=SystemMetricEventType.BATCH_SIZE_K
        )
        ev2 = SystemMetricEvent(
            id="met_2", repo_id="repo2", event_type=SystemMetricEventType.BATCH_SIZE_K
        )
        session.add(ev1)
        session.add(ev2)
        session.commit()
        
        result = asyncio.run(get_telemetry_events(repo_id="repo1", session=session))
        events = result["events"]
        assert len(events) == 1
        assert events[0]["id"] == "met_1"


def test_evaluate_cohort_endpoint_dependency_injection(tmp_path):
    """Verify that evaluate_cohort endpoint works and dependency injection resolves cleanly."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.aether.api.routes import router, set_services, get_promotion_engine
    from sqlmodel import SQLModel, create_engine, Session
    from packages.aether.services.event_bus import EventBus
    from pathlib import Path
    from packages.aether.models.entities import (
        Repository, Proposal, ConflictSet, Snapshot, GateResult, Promotion, SystemMetricEvent, AetherHead
    )

    db_path = tmp_path / "eval.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    set_services(engine, EventBus(), tmp_path / "storage_eval")

    app = FastAPI()
    app.include_router(router)
    
    with TestClient(app) as client:
        # Call get_promotion_engine. In RED, this raises NameError: name 'PromotionEngine' is not defined.
        with Session(engine) as session:
            engine_inst = get_promotion_engine(session)
            assert engine_inst is not None


def test_historical_head_resolution(tmp_path):
    """Verify that historical head can be resolved and doesn't return 404."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.aether.api.routes import router, set_services
    from sqlmodel import SQLModel, create_engine
    from packages.aether.services.event_bus import EventBus
    from pathlib import Path
    from packages.aether.models.entities import (
        Repository, Proposal, ConflictSet, Snapshot, GateResult, Promotion, SystemMetricEvent, AetherHead
    )
    
    db_path = tmp_path / "hist.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    set_services(engine, EventBus(), tmp_path / "storage_hist")
    
    app = FastAPI()
    app.include_router(router)
    
    with TestClient(app) as client:
        resp = client.post("/v1/repos", json={"name": "test-repo"})
        assert resp.status_code == 200
        repo_id = resp.json()["id"]
        
        resp = client.post(f"/v1/repos/{repo_id}/proposals", json={
            "agent_id": "a1",
            "change_content": "diff --git a/file.py b/file.py\nnew file mode 100644\n--- /dev/null\n+++ b/file.py\n@@ -0,0 +1,1 @@\n+print('v1')\n",
            "affected_paths": ["file.py"]
        })
        assert resp.status_code == 200
        p1_id = resp.json()["proposal_id"]
        
        resp = client.post(f"/v1/repos/{repo_id}/promotions", json={"proposal_ids": [p1_id]})
        assert resp.status_code == 200
        head1_id = resp.json()["new_head_id"]
        
        resp = client.get(f"/v1/repos/{repo_id}/promotions?snapshot_id={head1_id}")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


def test_initialize_atomicity_failures(tmp_path):
    """Verify that snapshot file initialization handles atomicity cleanly and does not return 404."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.aether.api.routes import router, set_services
    from sqlmodel import SQLModel, create_engine
    from packages.aether.services.event_bus import EventBus
    from pathlib import Path
    from packages.aether.models.entities import (
        Repository, Proposal, ConflictSet, Snapshot, GateResult, Promotion, SystemMetricEvent, AetherHead
    )
    
    db_path = tmp_path / "atom.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    set_services(engine, EventBus(), tmp_path / "storage_atom")
    
    app = FastAPI()
    app.include_router(router)
    
    with TestClient(app) as client:
        resp = client.post("/v1/repos", json={"name": "test-repo"})
        assert resp.status_code == 200
        repo_id = resp.json()["id"]
        
        resp = client.post(f"/v1/repos/{repo_id}/proposals", json={
            "agent_id": "a1",
            "change_content": "diff --git a/file.py b/file.py\nnew file mode 100644\n--- /dev/null\n+++ b/file.py\n@@ -0,0 +1,1 @@\n+print('v1')\n",
            "affected_paths": ["file.py"]
        })
        assert resp.status_code == 200
        p1_id = resp.json()["proposal_id"]
        
        resp = client.post(f"/v1/repos/{repo_id}/promotions", json={"proposal_ids": [p1_id]})
        assert resp.status_code == 200
        head1_id = resp.json()["new_head_id"]
        
        resp = client.get(f"/v1/repos/{repo_id}/snapshots/{head1_id}/files/file.py")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


def test_canary_cohort_no_fallback(tmp_path):
    """Verify that a cohort is evaluated and promoted without falling back to semantic conflict."""
    import os
    os.environ["AETHER_DEFER_CONFLICT_DETECTION"] = "False"
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from packages.aether.api.routes import router, set_services
        from sqlmodel import SQLModel, create_engine
        from packages.aether.services.event_bus import EventBus
        from pathlib import Path
        from packages.aether.models.entities import (
            Repository, Proposal, ProposalContention, ConflictSet, Snapshot, GateResult, Promotion, SystemMetricEvent, AetherHead
        )
        
        db_path = tmp_path / "canary.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        set_services(engine, EventBus(), tmp_path / "storage_canary")
        
        app = FastAPI()
        app.include_router(router)
        
        with TestClient(app) as client:
            resp = client.post("/v1/repos", json={"name": "test-repo"})
            assert resp.status_code == 200
            repo_id = resp.json()["id"]
            
            # Submit two proposals in the same group that can merge structurally
            resp = client.post(f"/v1/repos/{repo_id}/proposals", json={
                "agent_id": "a1",
                "change_content": "diff --git a/file.py b/file.py\nnew file mode 100644\n--- /dev/null\n+++ b/file.py\n@@ -0,0 +1,5 @@\n+class User:\n+    def __init__(self):\n+        self.id = 1\n",
                "affected_paths": ["file.py"],
                "contention_group_id": "group-1",
                "contention_policy": "semantic_merge",
                "defer_evaluation": True,
            })
            assert resp.status_code == 200
            p1_id = resp.json()["proposal_id"]
            
            resp = client.post(f"/v1/repos/{repo_id}/proposals", json={
                "agent_id": "a2",
                "change_content": "diff --git a/file.py b/file.py\nnew file mode 100644\n--- /dev/null\n+++ b/file.py\n@@ -0,0 +1,5 @@\n+class User:\n+    def __init__(self):\n+        self.id = 1\n",
                "affected_paths": ["file.py"],
                "contention_group_id": "group-1",
                "contention_policy": "semantic_merge",
                "defer_evaluation": True,
            })
            assert resp.status_code == 200
            p2_id = resp.json()["proposal_id"]
            
            from sqlmodel import Session
            with Session(engine) as session:
                p1_db = session.get(Proposal, p1_id)
                c1 = session.query(ProposalContention).filter_by(proposal_id=p1_id).first()
                print("P1 STATUS:", p1_db.status)
                print("C1:", c1.__dict__ if c1 else None)
            
            # Evaluate cohort
            resp = client.post(f"/v1/repos/{repo_id}/cohort/evaluate", json={
                "proposal_ids": [p1_id, p2_id]
            })
            print("COHORT EVAL RESPONSE:", resp.json())
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    finally:
        os.environ.pop("AETHER_DEFER_CONFLICT_DETECTION", None)
