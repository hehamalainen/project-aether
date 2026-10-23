"""CLI coverage for swarm run status inspection."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from apps.aether_cli.main import app


runner = CliRunner()


def test_swarm_run_status_reads_latest_runtime_heartbeat(tmp_path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime_artifacts"
    heartbeat_dir = runtime_root / "phase1"
    heartbeat_dir.mkdir(parents=True)
    json_path = tmp_path / "result.json"
    heartbeat_path = heartbeat_dir / "swarm-safe-20260414-010000.heartbeat.log"
    heartbeat_path.write_text(
        "\n".join(
            [
                "start=2026-04-14 01:00:00",
                "json_path=" + str(json_path),
                "run_class=canonical_baseline",
                "run_seconds=3600",
                "drain_seconds=1200",
                "heartbeat_seconds=30",
                "policy_allowed=1",
                "policy_reason=short_run",
                "pid=12345",
                "t+900s log_bytes=0 rc=None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AETHER_RUNTIME_ARTIFACT_ROOT", str(runtime_root))

    result = runner.invoke(app, ["swarm-run-status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["heartbeat_path"] == str(heartbeat_path.resolve())
    assert payload["state"] == "running"
    assert payload["json_exists"] is False
    assert payload["run_class"] == "canonical_baseline"
    assert payload["policy_reason"] == "short_run"
    assert payload["elapsed_seconds"] == 900


def test_swarm_run_status_reports_completed_explicit_heartbeat(tmp_path) -> None:
    json_path = tmp_path / "result.json"
    json_path.write_text("{}", encoding="utf-8")
    heartbeat_path = tmp_path / "explicit.heartbeat.log"
    heartbeat_path.write_text(
        "\n".join(
            [
                "start=2026-04-14 01:00:00",
                "end=2026-04-14 02:20:00",
                "json_path=" + str(json_path),
                "run_class=canonical_baseline",
                "run_seconds=3600",
                "drain_seconds=1200",
                "heartbeat_seconds=30",
                "policy_allowed=1",
                "policy_reason=short_run",
                "end_rc=0",
                "timed_out=0",
                "post_json_forced_stop=0",
                "t+4800s log_bytes=0 rc=None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["swarm-run-status", "--heartbeat", str(heartbeat_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "completed"
    assert payload["json_exists"] is True
    assert payload["end_rc"] == "0"
    assert payload["timed_out"] == "0"
