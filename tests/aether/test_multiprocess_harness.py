import pytest
import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from packages.aether.services.swarm_harness import (
    ContinuousHarnessConfig,
    ContinuousHarnessThresholds,
)
from packages.aether.services.multiprocess_harness import MultiprocessHarnessOrchestrator

def test_partitioning_validation():
    config = ContinuousHarnessConfig(num_agents=1000)
    thresholds = ContinuousHarnessThresholds()
    
    # 1000 agents cannot be divided by 3 processes
    orchestrator = MultiprocessHarnessOrchestrator(
        config=config,
        thresholds=thresholds,
        num_processes=3,
    )
    with pytest.raises(ValueError, match="Total agents .* must be perfectly divisible"):
        orchestrator.run()

@patch("subprocess.Popen")
def test_command_construction(mock_popen):
    # Set up mocks
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0
    mock_proc.pid = 123
    mock_popen.return_value = mock_proc
    
    config = ContinuousHarnessConfig(
        num_agents=100,
        num_files=10,
        run_seconds=10,
        drain_seconds=5,
    )
    thresholds = ContinuousHarnessThresholds()
    
    orchestrator = MultiprocessHarnessOrchestrator(
        config=config,
        thresholds=thresholds,
        num_processes=4,
        seed=42,
    )
    
    master_report = {
        "passed": True,
        "checks": {"submit_throughput": True},
        "metrics": {
            "submit_throughput": 45.5,
            "promotion_throughput": 35.0,
            "mu_eff": 35.0,
            "p95_submit_latency_ms": 50.0,
            "conflict_rate": 0.05,
            "backlog_end": 0,
            "promotions_created": 10,
            "proposals_promoted": 10,
            "submitted_success": 100,
            "submitted_total": 100,
        }
    }
    
    report_path = Path(f"local_run.nosync/master_report_{orchestrator.run_id}.json")
    # Write mock master report file
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(master_report))
    
    try:
        report = orchestrator.run()
        
        # Verify 4 processes were spawned
        assert mock_popen.call_count == 4
        
        # Verify environment variables for Master (process 0)
        master_env = mock_popen.call_args_list[0][1]["env"]
        assert master_env["AETHER_HARNESS_SKIP_SETUP"] == "0"
        assert master_env["AETHER_HARNESS_AGENT_START_IDX"] == "0"
        assert master_env["AETHER_HARNESS_AGENT_COUNT"] == "25"
        assert master_env["AETHER_HARNESS_MULTIPROCESS_MASTER"] == "1"
        assert master_env["AETHER_HARNESS_RUN_ID"] == orchestrator.run_id
        
        # Verify environment variables for Workers (processes 1-3)
        worker1_env = mock_popen.call_args_list[1][1]["env"]
        assert worker1_env["AETHER_HARNESS_SKIP_SETUP"] == "1"
        assert worker1_env["AETHER_HARNESS_AGENT_START_IDX"] == "25"
        assert worker1_env["AETHER_HARNESS_AGENT_COUNT"] == "25"
        assert worker1_env["AETHER_HARNESS_DISABLE_PROMOTION"] == "1"
        assert worker1_env["AETHER_HARNESS_DISABLE_MATERIALIZATION"] == "1"
        assert worker1_env["AETHER_HARNESS_DISABLE_MONITOR"] == "1"
        
        # Verify report outputs are correct and injected
        assert report["passed"] is True
        assert "multiprocess_saturated_promotion_rate" in report["metrics"]
        assert "multiprocess_queue_saturated" in report["metrics"]
    finally:
        if report_path.exists():
            report_path.unlink()

@patch("subprocess.Popen")
def test_fail_fast(mock_popen):
    # Setup worker process to fail (exit code 2)
    mock_failed_proc = MagicMock()
    mock_failed_proc.poll.return_value = 2
    mock_failed_proc.pid = 999
    
    mock_ok_proc = MagicMock()
    mock_ok_proc.poll.return_value = None # Still running
    mock_ok_proc.pid = 888
    
    # Let popen return mock_failed_proc for the first one (Master)
    mock_popen.side_effect = [mock_failed_proc, mock_ok_proc, mock_ok_proc, mock_ok_proc]
    
    config = ContinuousHarnessConfig(num_agents=100)
    thresholds = ContinuousHarnessThresholds()
    orchestrator = MultiprocessHarnessOrchestrator(
        config=config,
        thresholds=thresholds,
        num_processes=4,
    )
    
    with pytest.raises(RuntimeError, match="subprocess failed with exit code 2"):
        orchestrator.run()
        
    # Verify terminate was called on the running processes
    assert mock_ok_proc.terminate.call_count >= 1
