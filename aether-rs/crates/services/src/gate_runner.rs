use chrono::Utc;
use models::entities::{GateConfig, GateResult, GateStatus};
use std::path::Path;
use std::time::Duration;
use std::time::Instant;
use tokio::process::Command;
use tokio::time::timeout;
use uuid::Uuid;

pub struct GateRunner;

impl GateRunner {
    pub async fn run_gate(
        repo_id: &str,
        snapshot_id: &str,
        proposal_id: Option<String>,
        gate_config: &GateConfig,
        working_dir: &Path,
    ) -> GateResult {
        let started_at = Utc::now();
        let start_time = Instant::now();

        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg(&gate_config.command);
        cmd.current_dir(working_dir);
        cmd.kill_on_drop(true);

        cmd.env("AETHER_SNAPSHOT_ID", snapshot_id);
        cmd.env("AETHER_GATE_NAME", &gate_config.name);
        if let Some(ref pid) = proposal_id {
            cmd.env("AETHER_PROPOSAL_ID", pid);
        }

        let timeout_duration = Duration::from_secs(gate_config.timeout_seconds as u64);

        let mut status = GateStatus::Error;
        let mut exit_code = Some(-1);
        let mut stdout_str = String::new();
        let stderr_str;

        match timeout(timeout_duration, cmd.output()).await {
            Ok(Ok(output)) => {
                stdout_str = String::from_utf8_lossy(&output.stdout).into_owned();
                stderr_str = String::from_utf8_lossy(&output.stderr).into_owned();
                let code = output.status.code().unwrap_or(-1);
                exit_code = Some(code as i64);
                if output.status.success() {
                    status = GateStatus::Pass;
                } else {
                    status = GateStatus::Fail;
                }
            }
            Ok(Err(e)) => {
                stderr_str = format!("Failed to execute command: {}", e);
            }
            Err(_) => {
                stderr_str = format!("Gate timed out after {}s", timeout_duration.as_secs());
            }
        }

        let duration_ms = start_time.elapsed().as_millis() as i64;
        let completed_at = Utc::now();

        GateResult {
            id: format!(
                "gres_{}",
                Uuid::new_v4()
                    .simple()
                    .to_string()
                    .chars()
                    .take(12)
                    .collect::<String>()
            ),
            repo_id: repo_id.to_string(),
            snapshot_id: snapshot_id.to_string(),
            proposal_id,
            gate_name: gate_config.name.clone(),
            gate_command: Some(gate_config.command.clone()),
            status,
            exit_code,
            stdout: Some(stdout_str),
            stderr: Some(stderr_str),
            duration_ms: Some(duration_ms),
            tests_passed: None,
            tests_failed: None,
            coverage_pct: None,
            started_at: Some(started_at),
            completed_at: Some(completed_at),
        }
    }

    pub async fn run_all_gates(
        repo_id: &str,
        snapshot_id: &str,
        proposal_id: Option<String>,
        mut gates: Vec<GateConfig>,
        working_dir: &Path,
    ) -> Vec<GateResult> {
        gates.sort_by_key(|g| g.order);
        let mut results = Vec::new();

        for gate in gates {
            let required = gate.required;
            let result = Self::run_gate(
                repo_id,
                snapshot_id,
                proposal_id.clone(),
                &gate,
                working_dir,
            )
            .await;

            let passed = matches!(result.status, GateStatus::Pass | GateStatus::Skip);
            results.push(result);

            if required && !passed {
                // Fast fail
                break;
            }
        }

        results
    }
}
