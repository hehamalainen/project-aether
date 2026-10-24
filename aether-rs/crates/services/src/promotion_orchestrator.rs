use crate::gate_runner::GateRunner;
use crate::promotion_engine::PromotionEngine;
use crdt::document_store::DocumentStore;
use models::entities::{GateConfig, GateResult, GateStatus};
use models::entities::{Proposal, Repository};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use uuid::Uuid;

#[derive(Debug)]
pub enum OrchestrationError {
    BadRequest(String),
    Internal(String),
}

pub struct OrchestratedPromotion {
    pub candidate_store: DocumentStore,
    pub included_proposals: Vec<Proposal>,
    pub rejected_proposals: Vec<(String, String)>,
    pub superseded_proposals: Vec<(String, String)>,
    pub snapshot_id: String,
    pub gate_results: Vec<GateResult>,
    pub policy_score: f64,
    pub final_score: f64,
    pub raw_score: f64,
    pub file_count: usize,
    pub total_bytes: usize,
    pub state_hash: String,
    pub temp_workspace: PathBuf,
    pub timings: std::collections::HashMap<String, f64>,
}

pub struct PromotionOrchestrator {
    pub storage_root: PathBuf,
    pub crdt_mode: Option<String>,
}

impl PromotionOrchestrator {
    pub fn new(storage_root: PathBuf) -> Self {
        Self {
            storage_root,
            crdt_mode: None,
        }
    }

    fn all_required_gates_pass(
        gate_results: &[GateResult],
        gates: &[GateConfig],
    ) -> bool {
        for gate in gates {
            if gate.required {
                let res = gate_results.iter().find(|r| r.gate_name == gate.name);
                match res {
                    Some(r) => {
                        if !matches!(r.status, GateStatus::Pass | GateStatus::Skip) {
                            return false;
                        }
                    }
                    None => return false,
                }
            }
        }
        true
    }

    fn get_snapshot_path(&self, repo_id: &str, snapshot_id: &str) -> PathBuf {
        self.storage_root
            .join(repo_id)
            .join("snapshots")
            .join(snapshot_id)
    }

    /// Evaluates a batch of proposals by applying them to a cloned document store.
    /// Unified diffs are applied in a single batch using apply_diff_batch.
    pub async fn evaluate_batch_bounded(
        &self,
        repo: &Repository,
        proposals: &[Proposal],
        base_snapshot_ids: &[String],
        active_dot: Option<&crdt::causality::Dot>,
    ) -> Result<(DocumentStore, Vec<Proposal>), OrchestrationError> {
        // Conflict check: reject batch if multiple proposals share the same conflict_set_id
        let mut seen_conflict_sets = HashSet::new();
        for p in proposals {
            if let Some(cs_id) = &p.conflict_set_id {
                if !seen_conflict_sets.insert(cs_id.clone()) {
                    return Err(OrchestrationError::BadRequest(format!(
                        "Batch contains multiple proposals for conflict set {}",
                        cs_id
                     )));
                }
            }
        }

        let mut store = DocumentStore::new(repo.id.clone());
        store.crdt_mode = self.crdt_mode.clone();
        // Load base state if it exists
        for snapshot_id in base_snapshot_ids {
            if !snapshot_id.is_empty() {
                let snapshot_path = self.get_snapshot_path(&repo.id, snapshot_id);
                let mut temp_store = DocumentStore::new(repo.id.clone());
                temp_store.crdt_mode = self.crdt_mode.clone();
                if let Err(e) = temp_store.load_from_snapshot(&snapshot_path) {
                    // Fail closed on delta resolution error
                    return Err(OrchestrationError::Internal(format!(
                        "Failed to load base snapshot {}: {}",
                        snapshot_id, e
                    )));
                }
                store.merge(&temp_store);
            }
        }

        // Separate full_file and unified_diff proposals in the batch
        let mut unified_diffs = Vec::new();
        let mut full_files = Vec::new();

        for p in proposals {
            let parsed_paths: Vec<String> = serde_json::from_str(&p.affected_paths).map_err(|_| {
                OrchestrationError::BadRequest(format!(
                    "Proposal {} affected_paths cannot be empty or malformed",
                    p.id
                ))
            })?;
            crdt::document_store::validate_proposal_paths(
                &p.change_type,
                &p.change_content,
                &parsed_paths,
            )
            .map_err(|e| {
                OrchestrationError::BadRequest(format!(
                    "Proposal {} has invalid path(s): {}",
                    p.id, e
                ))
            })?;

            if p.change_type == "unified_diff" {
                unified_diffs.push(p);
            } else if p.change_type == "full_file" {
                full_files.push(p);
            } else {
                return Err(OrchestrationError::BadRequest(format!(
                    "Unsupported change type: {}",
                    p.change_type
                )));
            }
        }

        let mut candidate_store = store.cow_clone();

        // Apply full_file proposals
        for p in &full_files {
            let paths_res: Result<Vec<String>, _> = serde_json::from_str(&p.affected_paths);
            match paths_res {
                Ok(paths) if !paths.is_empty() => {
                    let norm_paths = crdt::document_store::validate_proposal_paths(
                        &p.change_type,
                        &p.change_content,
                        &paths,
                    )
                    .map_err(OrchestrationError::BadRequest)?;
                    for path in norm_paths {
                        if candidate_store.crdt_mode.as_deref() == Some("token") {
                            candidate_store.apply_token_smart_edit_bounded(&path, &p.change_content, &p.id, active_dot);
                        } else {
                            candidate_store.apply_full_file_bounded(&path, &p.change_content, &p.id, active_dot);
                        }
                    }
                }
                _ => {
                    return Err(OrchestrationError::BadRequest(format!(
                        "Proposal {} affected_paths cannot be empty or malformed",
                        p.id
                    )));
                }
            }
        }

        // Apply unified_diff proposals using apply_diff_batch
        if !unified_diffs.is_empty() {
            let diff_payloads: Vec<(&str, &str)> = unified_diffs
                .iter()
                .map(|p| (p.change_content.as_str(), p.id.as_str()))
                .collect();

            if let Err(e) = candidate_store.apply_diff_batch(&diff_payloads) {
                return Err(OrchestrationError::Internal(format!(
                    "Batch apply_diff_batch failed: {}",
                    e
                )));
            }
        }

        let included = proposals.to_vec();
        Ok((candidate_store, included))
    }

    pub async fn evaluate_batch(
        &self,
        repo: &Repository,
        proposals: &[Proposal],
        base_snapshot_id: &str,
    ) -> Result<(DocumentStore, Vec<Proposal>), OrchestrationError> {
        self.evaluate_batch_bounded(repo, proposals, &vec![base_snapshot_id.to_string()], None).await
    }

    pub fn materialize_snapshot_to_path(
        repo_id: &str,
        snapshot_id: &str,
        state_hash: &str,
        candidate_store: &DocumentStore,
        target_path: &Path,
    ) -> Result<(usize, usize), String> {
        let (files, bytes) = candidate_store.materialize_snapshot(target_path)?;

        let manifest = serde_json::json!({
            "mode": "full",
            "snapshot_id": snapshot_id,
            "repo_id": repo_id,
            "file_count": files,
            "total_bytes": bytes,
            "content_hash": state_hash,
            "changed_paths": candidate_store.documents.keys().collect::<Vec<_>>(),
        });
        let manifest_path = target_path.join(".aether_snapshot_manifest.json");
        if let Err(e) = std::fs::write(
            &manifest_path,
            serde_json::to_string_pretty(&manifest).unwrap_or_default(),
        ) {
            tracing::error!("Failed to write manifest: {}", e);
        }

        Ok((files, bytes))
    }

    pub async fn orchestrate_promotion_bounded(
        &self,
        repo: &Repository,
        proposals: &[Proposal],
        base_snapshot_ids: &[String],
        trust_score: f64,
        trust_timings: std::collections::HashMap<String, f64>,
        active_dot: Option<&crdt::causality::Dot>,
        meet_frontier_clock: Option<&crdt::causality::VersionVector>,
    ) -> Result<OrchestratedPromotion, OrchestrationError> {
        let mut timings = std::collections::HashMap::new();

        let mut sequential_fallback = false;
        let mut candidate_store = crdt::document_store::DocumentStore::new(repo.id.clone());
        candidate_store.crdt_mode = self.crdt_mode.clone();
        let mut included = Vec::new();
        let mut run_gates_res = Vec::new();
        let mut final_snapshot_id = String::new();
        let mut final_state_hash = String::new();
        let mut final_file_count = 0;
        let mut final_total_bytes = 0;

        let mut rejected_proposals = Vec::new();
        let mut superseded_proposals = Vec::new();

        let mut temp_workspace = self
            .storage_root
            .join(&repo.id)
            .join("gate_workspaces")
            .join(Uuid::new_v4().to_string());

        // 1. Sort proposals deterministically by priority (confidence score desc, submitted_at asc, id asc)
        let mut sorted_proposals = proposals.to_vec();
        sorted_proposals.sort_by(|a, b| {
            b.confidence_score.partial_cmp(&a.confidence_score).unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.submitted_at.cmp(&b.submitted_at))
                .then_with(|| a.id.cmp(&b.id))
        });

        // 2. Pre-filter semantic overlap (Batch Assembly)
        let mut batch_candidates = Vec::new();
        for p in sorted_proposals {
            let mut overlap = false;
            for active_p in &batch_candidates {
                if crdt::symbol_adapters::check_overlap(&p, active_p) {
                    overlap = true;
                    break;
                }
            }

            if overlap {
                tracing::info!("proposal_superseded_by_spatial_exclusion: {}", p.id);
                superseded_proposals.push((p.id.clone(), "spatial_batching_exclusion".to_string()));
            } else {
                batch_candidates.push(p);
            }
        }

        let t_crdt = std::time::Instant::now();
        let eval_res = self
            .evaluate_batch_bounded(repo, &batch_candidates, base_snapshot_ids, active_dot)
            .await;
        timings.insert("t_crdt_eval".to_string(), t_crdt.elapsed().as_secs_f64() * 1000.0);

        let mut gates_passed = false;

        if let Ok((store, inc)) = eval_res {
            candidate_store = store;
            included = inc;

            if let Err(e) = std::fs::create_dir_all(&temp_workspace) {
                return Err(OrchestrationError::Internal(format!(
                    "Failed to create temp workspace: {}",
                    e
                )));
            }

            // Phase 4: Vacuum tombstones before materializing to actively reduce stored snapshot bloat on disk.
            for file_doc in candidate_store.documents.values_mut() {
                file_doc.document.vacuum_tombstones_bounded(meet_frontier_clock);
                file_doc.last_hash = None;
            }

            let snapshot_id = format!(
                "snap_{}",
                Uuid::new_v4().simple().to_string().chars().take(12).collect::<String>()
            );
            let state_hash = candidate_store.get_state_hash();

            let t_mat = std::time::Instant::now();
            let mat_res = Self::materialize_snapshot_to_path(
                &repo.id,
                &snapshot_id,
                &state_hash,
                &candidate_store,
                &temp_workspace,
            );
            timings.insert("t_materialize".to_string(), t_mat.elapsed().as_secs_f64() * 1000.0);

            if let Ok((files, bytes)) = mat_res {
                let gates: Vec<GateConfig> =
                    serde_json::from_str(&repo.gate_config.clone().unwrap_or_default()).unwrap_or_default();

                let is_batch = included.len() > 1;
                let attribution_proposal_id = if is_batch {
                    None
                } else {
                    Some(included[0].id.clone())
                };

                let t_gate = std::time::Instant::now();
                let gate_results = GateRunner::run_all_gates(
                    &repo.id,
                    &snapshot_id,
                    attribution_proposal_id,
                    gates.clone(),
                    &temp_workspace,
                )
                .await;
                timings.insert("t_gates".to_string(), t_gate.elapsed().as_secs_f64() * 1000.0);

                gates_passed = Self::all_required_gates_pass(&gate_results, &gates);
                run_gates_res = gate_results;
                final_snapshot_id = snapshot_id;
                final_state_hash = state_hash;
                final_file_count = files;
                final_total_bytes = bytes;
            }

            if !gates_passed {
                sequential_fallback = true;
                let _ = std::fs::remove_dir_all(&temp_workspace);
            }
        } else {
            sequential_fallback = true;
        }

        if sequential_fallback {
            tracing::info!("Batch evaluation failed or gates failed; triggering sequential degradation fallback");
            
            let mut working_store = DocumentStore::new(repo.id.clone());
            working_store.crdt_mode = self.crdt_mode.clone();
            for snapshot_id in base_snapshot_ids {
                if !snapshot_id.is_empty() {
                    let snapshot_path = self.get_snapshot_path(&repo.id, snapshot_id);
                    let mut temp_store = DocumentStore::new(repo.id.clone());
                    temp_store.crdt_mode = self.crdt_mode.clone();
                    if let Err(e) = temp_store.load_from_snapshot(&snapshot_path) {
                        return Err(OrchestrationError::Internal(format!(
                            "Failed to load base snapshot {}: {}",
                            snapshot_id, e
                        )));
                    }
                    working_store.merge(&temp_store);
                }
            }

            let ordered_proposals = batch_candidates.clone();

            let mut accepted_winners = Vec::new();

            for p in ordered_proposals {
                let mut overlap = false;
                for winner in &accepted_winners {
                    if crdt::symbol_adapters::check_overlap(&p, winner) {
                        overlap = true;
                        break;
                    }
                }

                if overlap {
                    tracing::info!("sequential_degradation_superseded: {}", p.id);
                    superseded_proposals.push((p.id.clone(), "sequential_degradation_overlap".to_string()));
                    continue;
                }

                let pre_apply_store = working_store.cow_clone();
                let parsed_paths: Result<Vec<String>, _> = serde_json::from_str(&p.affected_paths);
                let apply_res = match parsed_paths {
                    Ok(raw_paths) => match crdt::document_store::validate_proposal_paths(
                        &p.change_type,
                        &p.change_content,
                        &raw_paths,
                    ) {
                        Ok(norm_paths) => {
                            if p.change_type == "unified_diff" {
                                working_store.apply_diff_batch(&[(&p.change_content, &p.id)])
                            } else if p.change_type == "full_file" {
                                for path in &norm_paths {
                                    if working_store.crdt_mode.as_deref() == Some("token") {
                                        working_store.apply_token_smart_edit_bounded(
                                            path,
                                            &p.change_content,
                                            &p.id,
                                            active_dot,
                                        );
                                    } else {
                                        working_store.apply_full_file_bounded(
                                            path,
                                            &p.change_content,
                                            &p.id,
                                            active_dot,
                                        );
                                    }
                                }
                                Ok(norm_paths)
                            } else {
                                Err(format!("Unsupported change type: {}", p.change_type))
                            }
                        }
                        Err(e) => Err(e),
                    },
                    Err(_) => Err("Empty or malformed affected_paths".to_string()),
                };

                let _changed_paths = match apply_res {
                    Ok(paths) => paths,
                    Err(e) => {
                        tracing::warn!("sequential_degradation_apply_failed for {}: {}", p.id, e);
                        rejected_proposals.push((p.id.clone(), format!("sequential_degradation_apply_failed: {}", e)));
                        working_store = pre_apply_store;
                        continue;
                    }
                };

                let temp_p_workspace = self
                    .storage_root
                    .join(&repo.id)
                    .join("gate_workspaces")
                    .join(Uuid::new_v4().to_string());
                if let Err(e) = std::fs::create_dir_all(&temp_p_workspace) {
                    return Err(OrchestrationError::Internal(format!(
                        "Failed to create temp workspace: {}",
                        e
                    )));
                }

                // Vacuum tombstones
                let mut store_to_materialize = working_store.cow_clone();
                for file_doc in store_to_materialize.documents.values_mut() {
                    file_doc.document.vacuum_tombstones_bounded(meet_frontier_clock);
                    file_doc.last_hash = None;
                }

                let snapshot_id = format!(
                    "snap_{}",
                    Uuid::new_v4().simple().to_string().chars().take(12).collect::<String>()
                );
                let state_hash = store_to_materialize.get_state_hash();

                let mat_res = Self::materialize_snapshot_to_path(
                    &repo.id,
                    &snapshot_id,
                    &state_hash,
                    &store_to_materialize,
                    &temp_p_workspace,
                );

                if let Ok((files, bytes)) = mat_res {
                    let gates: Vec<GateConfig> =
                        serde_json::from_str(&repo.gate_config.clone().unwrap_or_default()).unwrap_or_default();

                    let gate_results = GateRunner::run_all_gates(
                        &repo.id,
                        &snapshot_id,
                        Some(p.id.clone()),
                        gates.clone(),
                        &temp_p_workspace,
                    )
                    .await;

                    if Self::all_required_gates_pass(&gate_results, &gates) {
                        // Winner!
                        accepted_winners.push(p.clone());
                        candidate_store = store_to_materialize;
                        run_gates_res = gate_results;
                        final_snapshot_id = snapshot_id;
                        final_state_hash = state_hash;
                        final_file_count = files;
                        final_total_bytes = bytes;
                        temp_workspace = temp_p_workspace;
                    } else {
                        tracing::info!("sequential_degradation_rejected: {}", p.id);
                        rejected_proposals.push((p.id.clone(), "sequential_degradation_gate_failed".to_string()));
                        let _ = std::fs::remove_dir_all(&temp_p_workspace);
                        working_store = pre_apply_store;
                    }
                } else {
                    let _ = std::fs::remove_dir_all(&temp_p_workspace);
                    working_store = pre_apply_store;
                }
            }

            included = accepted_winners;
        }

        if included.is_empty() {
            return Err(OrchestrationError::BadRequest(
                "No valid changes to promote".to_string(),
            ));
        }

        let t_trust = std::time::Instant::now();
        for (k, v) in trust_timings {
            timings.insert(k, v);
        }

        let engine = PromotionEngine::new();
        let lead_proposal = &included[0];

        let gates: Vec<GateConfig> =
            serde_json::from_str(&repo.gate_config.clone().unwrap_or_default()).unwrap_or_default();
        let (final_score, policy_score, raw_score) =
            engine.compute_score(lead_proposal, &run_gates_res, &gates, trust_score);
        timings.insert("t_trust_math".to_string(), t_trust.elapsed().as_secs_f64() * 1000.0);

        Ok(OrchestratedPromotion {
            candidate_store,
            included_proposals: included,
            rejected_proposals,
            superseded_proposals,
            snapshot_id: final_snapshot_id,
            gate_results: run_gates_res,
            policy_score,
            final_score,
            raw_score,
            file_count: final_file_count,
            total_bytes: final_total_bytes,
            state_hash: final_state_hash,
            temp_workspace,
            timings,
        })
    }

    pub async fn orchestrate_promotion(
        &self,
        repo: &Repository,
        proposals: &[Proposal],
        base_snapshot_id: &str,
        trust_score: f64,
        trust_timings: std::collections::HashMap<String, f64>,
    ) -> Result<OrchestratedPromotion, OrchestrationError> {
        self.orchestrate_promotion_bounded(repo, proposals, &vec![base_snapshot_id.to_string()], trust_score, trust_timings, None, None).await
    }

    pub fn cleanup_snapshot(&self, repo_id: &str, snapshot_id: &str) {
        let snapshot_path = self.get_snapshot_path(repo_id, snapshot_id);
        if snapshot_path.exists() {
            if let Err(e) = std::fs::remove_dir_all(&snapshot_path) {
                tracing::warn!(
                    "Failed to clean up orphaned snapshot {:?}: {}",
                    snapshot_path,
                    e
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use models::entities::{Repository, Proposal};

    #[tokio::test]
    async fn test_vacuum_before_materialization() {
        let temp_dir_path = std::env::temp_dir().join(format!("aether_test_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temp_dir_path).unwrap();

        let mut candidate_store = DocumentStore::new("repo_vacuum_test".to_string());
        candidate_store.crdt_mode = Some("token".to_string());

        // Apply edits manually to simulate merge
        candidate_store.apply_token_smart_edit_bounded("src/test.txt", "token1token2token3", "prop_1", None);
        candidate_store.apply_token_smart_edit_bounded("src/test.txt", "token1token3", "prop_2", None);

        // Verify that the deletion tombstone for token2 is vacuumed
        for file_doc in candidate_store.documents.values_mut() {
            file_doc.document.vacuum_tombstones_bounded(None);
        }

        let doc = candidate_store.documents.get("src/test.txt").unwrap();
        // token1 and token3 active, plus root = 3 elements.
        assert_eq!(doc.document.elements.len(), 3);
        assert!(!doc.document.elements.values().any(|e| e.deleted));

        let _ = std::fs::remove_dir_all(&temp_dir_path);
    }

    #[tokio::test]
    async fn test_vacuum_before_materialization_bounded() {
        use crdt::causality::{Dot, VersionVector};

        let temp_dir_path = std::env::temp_dir().join(format!("aether_test_bounded_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temp_dir_path).unwrap();

        let mut candidate_store = DocumentStore::new("repo_vacuum_bounded_test".to_string());
        candidate_store.crdt_mode = Some("token".to_string());

        let promotion_dot = Dot::new("agent_1", 1);
        let meet_clock = VersionVector::new(); // empty, does not cover (agent_1, 1)

        // Apply edits manually to simulate merge
        candidate_store.apply_token_smart_edit_bounded("src/test.txt", "token1token2token3", "prop_1", Some(&promotion_dot));
        candidate_store.apply_token_smart_edit_bounded("src/test.txt", "token1token3", "prop_2", Some(&promotion_dot));

        // Vacuum tombstones
        for file_doc in candidate_store.documents.values_mut() {
            file_doc.document.vacuum_tombstones_bounded(Some(&meet_clock));
            file_doc.last_hash = None;
        }

        // Because meet_clock does NOT include promotion_dot, the deletion tombstone for token2 is preserved.
        // Elements should be: root + token1 + token3 + tombstoned(token2) = 4 elements.
        let doc = candidate_store.documents.get("src/test.txt").unwrap();
        assert_eq!(doc.document.elements.len(), 4);
        assert!(doc.document.elements.values().any(|e| e.deleted));

        // Materialize to a snapshot directory
        let snap_dir = temp_dir_path.join("snapshots").join("snap_test");
        std::fs::create_dir_all(&snap_dir).unwrap();
        let (files, bytes) = PromotionOrchestrator::materialize_snapshot_to_path(
            "repo_vacuum_bounded_test",
            "snap_test",
            &candidate_store.get_state_hash(),
            &candidate_store,
            &snap_dir,
        ).unwrap();
        assert_eq!(files, 1);
        assert!(bytes > 0);

        // Reload from the snapshot directory on disk (the reload boundary)
        let _ = std::fs::remove_file(snap_dir.join(".aether_crdt_state.json"));
        let mut reloaded_store = DocumentStore::new("repo_vacuum_bounded_test".to_string());
        reloaded_store.crdt_mode = Some("token".to_string());
        reloaded_store.load_from_snapshot(&snap_dir).unwrap();

        // Verify that on reload, the document has no tombstones and the text is correct
        let reloaded_doc = reloaded_store.documents.get("src/test.txt").unwrap();
        assert_eq!(reloaded_doc.document.get_text(), "token1token3");
        assert!(!reloaded_doc.document.elements.values().any(|e| e.deleted));
        assert_eq!(reloaded_doc.document.elements.len(), 2); // root + "token1token3"

        let _ = std::fs::remove_dir_all(&temp_dir_path);
    }

    #[tokio::test]
    async fn test_orchestration_semantic_overlap_exclusion() {
        let temp_dir_path = std::env::temp_dir().join(format!("aether_test_excl_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temp_dir_path).unwrap();
        let mut orchestrator = PromotionOrchestrator::new(temp_dir_path.clone());
        orchestrator.crdt_mode = Some("text".to_string());

        let repo = Repository {
            id: "repo_excl_test".to_string(),
            name: "test".to_string(),
            description: None,
            head_id: None,
            head_sequence: 0,
            gate_config: Some("[]".to_string()),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            max_alternatives_per_conflict: 5,
            auto_promote: true,
            crdt_enabled: true,
        };

        let prop1 = Proposal {
            id: "prop_1".to_string(),
            repo_id: repo.id.clone(),
            agent_id: "agent_1".to_string(),
            task_id: Some("task_1".to_string()),
            source: "manual".to_string(),
            source_run_id: None,
            source_task_id: None,
            source_agent_id: None,
            source_attempt_id: None,
            intent_type: None,
            base_head_id: None,
            base_head_sequence: 0,
            change_type: "full_file".to_string(),
            change_content: "x = 10".to_string(),
            affected_paths: "[\"main.py\"]".to_string(),
            affected_symbols: Some("[\"main.py::__raw__\"]".to_string()),
            risk_tier: 1,
            confidence_score: 0.9,
            model_id: None,
            calibration_profile: None,
            rationale: None,
            dependencies: "[]".to_string(),
            requires_tests: false,
            requires_security_scan: false,
            status: models::entities::ProposalStatus::Pending,
            conflict_set_id: None,
            submitted_at: chrono::Utc::now(),
            evaluated_at: None,
            promoted_at: None,
            idempotency_key: None,
        };

        let prop2 = Proposal {
            id: "prop_2".to_string(),
            repo_id: repo.id.clone(),
            agent_id: "agent_2".to_string(),
            task_id: Some("task_2".to_string()),
            source: "manual".to_string(),
            source_run_id: None,
            source_task_id: None,
            source_agent_id: None,
            source_attempt_id: None,
            intent_type: None,
            base_head_id: None,
            base_head_sequence: 0,
            change_type: "full_file".to_string(),
            change_content: "x = 20".to_string(),
            affected_paths: "[\"main.py\"]".to_string(),
            affected_symbols: Some("[\"main.py::__raw__\"]".to_string()),
            risk_tier: 1,
            confidence_score: 0.8,
            model_id: None,
            calibration_profile: None,
            rationale: None,
            dependencies: "[]".to_string(),
            requires_tests: false,
            requires_security_scan: false,
            status: models::entities::ProposalStatus::Pending,
            conflict_set_id: None,
            submitted_at: chrono::Utc::now() + chrono::Duration::seconds(5),
            evaluated_at: None,
            promoted_at: None,
            idempotency_key: None,
        };

        let result = orchestrator
            .orchestrate_promotion(&repo, &[prop1, prop2], "", 0.9, std::collections::HashMap::new())
            .await
            .unwrap();

        assert_eq!(result.included_proposals.len(), 1);
        assert_eq!(result.included_proposals[0].id, "prop_1");

        assert_eq!(result.superseded_proposals.len(), 1);
        assert_eq!(result.superseded_proposals[0].0, "prop_2");
        assert_eq!(result.superseded_proposals[0].1, "spatial_batching_exclusion");

        let _ = std::fs::remove_dir_all(&temp_dir_path);
    }

    #[tokio::test]
    async fn test_intake_to_materialization_rejects_outside_storage_writes_and_git_aliases() {
        let root_dir = std::env::temp_dir().join(format!("aether_containment_{}", uuid::Uuid::new_v4()));
        let storage_dir = root_dir.join("storage");
        std::fs::create_dir_all(&storage_dir).unwrap();

        let outside_abs_victim = root_dir.join("outside_abs_victim.txt");
        let outside_rel_victim = root_dir.join("outside_rel_victim.txt");
        std::fs::write(&outside_abs_victim, "CANARY_INTACT\n").unwrap();
        std::fs::write(&outside_rel_victim, "CANARY_INTACT\n").unwrap();

        let abs_victim_str = outside_abs_victim.to_string_lossy().to_string();

        // 1. Intake validation rejects unsafe paths and case-insensitive .git aliases
        for bad_path in &[
            abs_victim_str.as_str(),
            "../outside_rel_victim.txt",
            "sub/../../outside_rel_victim.txt",
            ".git/config",
            ".GIT/config",
            "sub/.Git/hooks/pre-commit",
            ".git./config",
            "git~1/config",
            ".aether_snapshot_manifest.json",
            ".AETHER_SNAPSHOT_MANIFEST.JSON",
            ".aether_crdt_state.json",
            ".AETHER_CRDT_STATE.JSON",
        ] {
            assert!(
                crdt::document_store::validate_repo_relative_path(bad_path).is_err(),
                "Expected path {:?} to be rejected",
                bad_path
            );
            assert!(
                crdt::document_store::validate_proposal_paths(
                    "full_file",
                    "OVERWRITTEN\n",
                    &[bad_path.to_string()],
                )
                .is_err(),
                "Expected proposal path {:?} to be rejected at intake",
                bad_path
            );
        }

        // Intake validation also rejects unified_diff with malicious headers despite benign affected_paths
        let malicious_diff = format!(
            "diff --git a/safe.py b/safe.py\n--- a/safe.py\n+++ b/../outside_rel_victim.txt\n@@ -0,0 +1 @@\n+OVERWRITTEN\n"
        );
        assert!(
            crdt::document_store::validate_proposal_paths(
                "unified_diff",
                &malicious_diff,
                &["safe.py".to_string()],
            )
            .is_err()
        );

        // 2. PromotionOrchestrator rejects both absolute and ../ traversal proposals (full_file & unified_diff)
        let orchestrator = PromotionOrchestrator::new(storage_dir.clone());
        let repo = Repository {
            id: "repo_containment".to_string(),
            name: "containment".to_string(),
            description: None,
            head_id: None,
            head_sequence: 0,
            gate_config: Some("[]".to_string()),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            max_alternatives_per_conflict: 5,
            auto_promote: true,
            crdt_enabled: true,
        };

        let make_proposal = |id: &str, change_type: &str, content: &str, paths_json: &str| Proposal {
            id: id.to_string(),
            repo_id: repo.id.clone(),
            agent_id: "attacker".to_string(),
            task_id: None,
            source: "manual".to_string(),
            source_run_id: None,
            source_task_id: None,
            source_agent_id: None,
            source_attempt_id: None,
            intent_type: None,
            base_head_id: None,
            base_head_sequence: 0,
            change_type: change_type.to_string(),
            change_content: content.to_string(),
            affected_paths: paths_json.to_string(),
            affected_symbols: None,
            risk_tier: 1,
            confidence_score: 0.95,
            model_id: None,
            calibration_profile: None,
            rationale: None,
            dependencies: "[]".to_string(),
            requires_tests: false,
            requires_security_scan: false,
            status: models::entities::ProposalStatus::Pending,
            conflict_set_id: None,
            submitted_at: chrono::Utc::now(),
            evaluated_at: None,
            promoted_at: None,
            idempotency_key: None,
        };

        let prop_abs = make_proposal(
            "prop_abs",
            "full_file",
            "OVERWRITTEN\n",
            &serde_json::to_string(&vec![abs_victim_str.clone()]).unwrap(),
        );
        let prop_rel = make_proposal(
            "prop_rel",
            "full_file",
            "OVERWRITTEN\n",
            "[\"../../../../outside_rel_victim.txt\"]",
        );
        let prop_diff_rel = make_proposal(
            "prop_diff_rel",
            "unified_diff",
            "diff --git a/../../../../outside_rel_victim.txt b/../../../../outside_rel_victim.txt\n--- a/../../../../outside_rel_victim.txt\n+++ b/../../../../outside_rel_victim.txt\n@@ -1 +1 @@\n-CANARY_INTACT\n+OVERWRITTEN\n",
            "[\"safe.py\"]",
        );

        for bad_prop in &[prop_abs, prop_rel, prop_diff_rel] {
            assert!(
                orchestrator
                    .evaluate_batch(&repo, std::slice::from_ref(bad_prop), "")
                    .await
                    .is_err()
            );
            assert!(
                orchestrator
                    .orchestrate_promotion(
                        &repo,
                        std::slice::from_ref(bad_prop),
                        "",
                        0.9,
                        std::collections::HashMap::new(),
                    )
                    .await
                    .is_err()
            );
        }

        // 3. Direct DocumentStore::materialize_snapshot also blocks absolute paths, traversal, and symlink escapes
        let snap_out = storage_dir.join("snap_direct");
        std::fs::create_dir_all(&snap_out).unwrap();

        let mut bad_abs_store = DocumentStore::new("repo_containment".to_string());
        bad_abs_store.apply_full_file(&abs_victim_str, "OVERWRITTEN\n", "prop_abs");
        assert!(bad_abs_store.materialize_snapshot(&snap_out).is_err());

        let mut bad_rel_store = DocumentStore::new("repo_containment".to_string());
        bad_rel_store.apply_full_file("../../outside_rel_victim.txt", "OVERWRITTEN\n", "prop_rel");
        assert!(bad_rel_store.materialize_snapshot(&snap_out).is_err());

        #[cfg(unix)]
        {
            let symlink_snap = storage_dir.join("snap_symlink");
            std::fs::create_dir_all(&symlink_snap).unwrap();
            std::os::unix::fs::symlink(&root_dir, symlink_snap.join("sub")).unwrap();
            let mut sym_store = DocumentStore::new("repo_containment".to_string());
            sym_store.apply_full_file("sub/outside_rel_victim.txt", "OVERWRITTEN\n", "prop_sym");
            assert!(sym_store.materialize_snapshot(&symlink_snap).is_err());
        }

        assert_eq!(
            std::fs::read_to_string(&outside_abs_victim).unwrap(),
            "CANARY_INTACT\n"
        );
        assert_eq!(
            std::fs::read_to_string(&outside_rel_victim).unwrap(),
            "CANARY_INTACT\n"
        );

        let _ = std::fs::remove_dir_all(&root_dir);
    }
}

