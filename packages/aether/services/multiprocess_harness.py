import os
import sys
import time
import uuid
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from sqlmodel import create_engine, select, func, Session
from packages.aether.models.entities import Proposal, Promotion
from packages.aether.services.swarm_harness import (
    ContinuousHarnessConfig,
    ContinuousHarnessThresholds,
)

class MultiprocessHarnessOrchestrator:
    """Orchestrates multiple independent swarm harness processes to isolate the GIL."""

    def __init__(
        self,
        config: ContinuousHarnessConfig,
        thresholds: ContinuousHarnessThresholds,
        num_processes: int = 4,
        seed: int = 2026,
        output_json: Optional[Path] = None,
    ):
        self.config = config
        self.thresholds = thresholds
        self.num_processes = num_processes
        self.seed = seed
        self.output_json = output_json
        self.run_id = f"run_mp_{uuid.uuid4().hex[:12]}"

    def run(self) -> dict[str, Any]:
        total_agents = self.config.num_agents
        agents_per_process = total_agents // self.num_processes
        if total_agents % self.num_processes != 0:
            raise ValueError(
                f"Total agents ({total_agents}) must be perfectly divisible by processes ({self.num_processes})"
            )

        processes = []
        temp_files = []

        # Ensure local_run.nosync/ exists for report storage
        Path("local_run.nosync").mkdir(parents=True, exist_ok=True)
        master_report_path = Path(f"local_run.nosync/master_report_{self.run_id}.json")
        temp_files.append(master_report_path)

        repo_name = f"continuous-swarm-{self.config.num_agents}"

        # Construct basic command line flags
        base_cmd = [
            sys.executable,
            "-m",
            "apps.aether_cli.main",
            "swarm-harness",
            "--agents", str(self.config.num_agents),
            "--files", str(self.config.num_files),
            "--run-seconds", str(self.config.run_seconds),
            "--drain-seconds", str(self.config.drain_seconds),
            "--contention", self.config.contention_mode,
            "--min-interval-ms", str(self.config.agent_interval_min_ms),
            "--max-interval-ms", str(self.config.agent_interval_max_ms),
            "--inflight", str(self.config.max_inflight_submissions),
            "--promotion-batch", str(self.config.promotion_batch_size),
            "--promotion-non-conflicting-batches", str(self.config.promotion_non_conflicting_batches),
            "--promotion-max-conflict-sets", str(self.config.promotion_max_conflict_sets),
            "--gate-execution-mode", self.config.gate_execution_mode,
            "--gate-max-parallel", str(self.config.gate_max_parallel),
            "--promotion-interval-ms", str(self.config.promotion_interval_ms),
            "--promotion-iteration-timeout-seconds", str(self.config.promotion_iteration_timeout_seconds),
            "--materialization-batch-size", str(self.config.materialization_batch_size),
            "--materialization-interval-ms", str(self.config.materialization_interval_ms),
            "--monitor-interval-ms", str(self.config.monitor_interval_ms),
            "--target-mode", self.config.target_mode,
            "--traffic-mix", self.config.traffic_mix,
            "--submit-worker-threads", str(self.config.submit_worker_threads),
            "--min-submit-throughput", str(self.thresholds.min_submit_throughput),
            "--min-promotion-throughput", str(self.thresholds.min_promotion_throughput),
            "--min-mu-eff", str(self.thresholds.min_mu_eff),
            "--max-submit-p95-ms", str(self.thresholds.max_submit_p95_ms),
            "--max-conflict-rate", str(self.thresholds.max_conflict_rate),
            "--max-backlog-end", str(self.thresholds.max_backlog_end),
            "--max-submit-failure-rate", str(self.thresholds.max_submit_failure_rate),
            "--max-window-rho", str(self.thresholds.max_window_rho),
            "--seed", str(self.seed),
            "--quiet",
            "--no-strict",
        ]

        if self.config.external_api_url:
            base_cmd.extend(["--external-api-url", self.config.external_api_url])
        if self.config.submit_in_worker:
            base_cmd.append("--submit-in-worker")
        else:
            base_cmd.append("--submit-inline")
        if self.config.defer_conflict_detection:
            base_cmd.append("--defer-conflict-detection")
        else:
            base_cmd.append("--inline-conflict-detection")
        if self.config.adaptive_intake_throttle:
            base_cmd.append("--adaptive-intake-throttle")
        else:
            base_cmd.append("--no-adaptive-intake-throttle")
        if self.config.intake_backlog_high_watermark:
            base_cmd.extend(["--intake-backlog-high-watermark", str(self.config.intake_backlog_high_watermark)])
        if self.config.intake_backlog_low_watermark:
            base_cmd.extend(["--intake-backlog-low-watermark", str(self.config.intake_backlog_low_watermark)])

        telemetry_records = []
        stop_telemetry = threading.Event()

        def sample_db():
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                db_url = f"sqlite:///{str(Path.home() / '.aether' / 'aether.db')}"
            cargs = {"timeout": 30} if db_url.startswith("sqlite") else {}
            engine = create_engine(db_url, connect_args=cargs)
            
            # Wait for setup to finish creating tables
            time.sleep(5.0)
            
            try:
                with Session(engine) as session:
                    # Query repository details to ensure it exists
                    repo_ids = []
                    while not stop_telemetry.is_set() and len(repo_ids) < 10:
                        from packages.aether.models.entities import Repository
                        repos = session.exec(select(Repository).where(Repository.name.like(f"{repo_name}-repo-%"))).all()
                        repo_ids = [r.id for r in repos]
                        if len(repo_ids) < 10:
                            time.sleep(0.5)

                    while not stop_telemetry.is_set():
                        # Measure queue depth
                        queue_depth = session.exec(
                            select(func.count(Proposal.id))
                            .where(Proposal.repo_id.in_(repo_ids))
                            .where(Proposal.status.in_(["INGESTED", "PENDING", "EVALUATING"]))
                        ).one()

                        # Measure total proposals promoted
                        promoted_count = session.exec(
                            select(func.count(Proposal.id))
                            .where(Proposal.repo_id.in_(repo_ids))
                            .where(Proposal.status == "PROMOTED")
                        ).one()

                        # Measure total promotions (head advances)
                        promotions_count = session.exec(
                            select(func.count(Promotion.id))
                            .where(Promotion.repo_id.in_(repo_ids))
                        ).one()

                        telemetry_records.append({
                            "timestamp": time.perf_counter(),
                            "queue_depth": queue_depth,
                            "promotions_count": promotions_count,
                            "promoted_count": promoted_count,
                        })
                        time.sleep(5.0)
            except Exception as e:
                # Do not crash the orchestrator if database sampling fails occasionally
                sys.stderr.write(f"Telemetry Sampling Warning: {e}\n")
                sys.stderr.flush()

        telemetry_thread = threading.Thread(target=sample_db, daemon=True)

        try:
            # 1. Launch Master Process
            master_env = os.environ.copy()
            master_env["AETHER_HARNESS_RUN_ID"] = self.run_id
            master_env["AETHER_HARNESS_REPO_NAME"] = repo_name
            master_env["AETHER_HARNESS_SKIP_SETUP"] = "0"
            master_env["AETHER_HARNESS_AGENT_START_IDX"] = "0"
            master_env["AETHER_HARNESS_AGENT_COUNT"] = str(agents_per_process)
            master_env["AETHER_HARNESS_MULTIPROCESS_MASTER"] = "1"

            master_cmd = base_cmd + ["--output-json", str(master_report_path)]
            p_master = subprocess.Popen(master_cmd, env=master_env)
            processes.append(p_master)

            # 2. Launch Worker Processes
            for w in range(1, self.num_processes):
                worker_env = os.environ.copy()
                worker_env["AETHER_HARNESS_RUN_ID"] = self.run_id
                worker_env["AETHER_HARNESS_REPO_NAME"] = repo_name
                worker_env["AETHER_HARNESS_SKIP_SETUP"] = "1"
                worker_env["AETHER_HARNESS_AGENT_START_IDX"] = str(w * agents_per_process)
                worker_env["AETHER_HARNESS_AGENT_COUNT"] = str(agents_per_process)
                worker_env["AETHER_HARNESS_DISABLE_PROMOTION"] = "1"
                worker_env["AETHER_HARNESS_DISABLE_MATERIALIZATION"] = "1"
                worker_env["AETHER_HARNESS_DISABLE_MONITOR"] = "1"

                p_worker = subprocess.Popen(base_cmd, env=worker_env)
                processes.append(p_worker)

            # Start sampling thread
            telemetry_thread.start()

            # 3. Monitor Processes (Fail-Fast)
            active = True
            while active:
                active = False
                for p in processes:
                    rc = p.poll()
                    if rc is not None:
                        if rc != 0:
                            # Terminate all subprocesses immediately
                            sys.stderr.write(f"\n[FAIL-FAST] Process PID {p.pid} exited with error code {rc}! Aborting all processes...\n")
                            sys.stderr.flush()
                            for p_kill in processes:
                                if p_kill.poll() is None:
                                    p_kill.terminate()
                                    p_kill.wait()
                            raise RuntimeError(f"Swarm harness subprocess failed with exit code {rc}")
                    else:
                        active = True
                time.sleep(0.5)

        finally:
            stop_telemetry.set()
            if telemetry_thread.is_alive():
                telemetry_thread.join(timeout=2.0)

        # 4. Analyze Saturation and Promotion Rate
        high_watermark = self.config.intake_backlog_high_watermark or 1440
        sustained_window = 6  # 6 samples * 5 seconds = 30 seconds
        max_mu_rate = 0.0
        saturated_window_found = False

        if len(telemetry_records) >= sustained_window:
            for i in range(len(telemetry_records) - sustained_window):
                window_slice = telemetry_records[i : i + sustained_window]
                is_saturated = all(r["queue_depth"] >= high_watermark for r in window_slice)
                if is_saturated:
                    saturated_window_found = True
                    prom_start = window_slice[0]["promoted_count"]
                    prom_end = window_slice[-1]["promoted_count"]
                    duration = window_slice[-1]["timestamp"] - window_slice[0]["timestamp"]
                    if duration > 0:
                        rate = (prom_end - prom_start) / duration
                        max_mu_rate = max(max_mu_rate, rate)

        # 5. Load and output unified Master Report
        if not master_report_path.exists():
            raise RuntimeError("Master process did not write report file successfully")

        # Log telemetry records for diagnostic visibility
        sys.stderr.write(f"\n--- Telemetry Samples ({len(telemetry_records)}) ---\n")
        for record in telemetry_records:
            sys.stderr.write(f"  timestamp={record['timestamp']:.2f} queue_depth={record['queue_depth']} promoted={record['promoted_count']} promotions={record['promotions_count']}\n")
        sys.stderr.write(f"  high_watermark={high_watermark} sustained_window={sustained_window} saturated_found={saturated_window_found} max_mu_rate={max_mu_rate:.2f}\n")
        sys.stderr.write("-------------------------------------\n\n")
        sys.stderr.flush()

        with open(master_report_path, "r") as f:
            report_data = json.load(f)

        # Inject multiprocess metrics
        report_data["metrics"]["multiprocess_saturated_promotion_rate"] = round(max_mu_rate, 2)
        report_data["metrics"]["multiprocess_queue_saturated"] = saturated_window_found
        report_data["metrics"]["telemetry_records"] = telemetry_records

        # Clean up temp files
        for f_temp in temp_files:
            if f_temp.exists():
                try:
                    f_temp.unlink()
                except OSError:
                    pass

        return report_data
