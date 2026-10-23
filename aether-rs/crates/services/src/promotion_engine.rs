use anyhow::Result;
use chrono::{DateTime, Utc};
use models::entities::{GateConfig, GateResult, GateStatus, Proposal, Repository};
use std::collections::{HashMap, HashSet};

// trust math moved to a separate trust_service module (or expects pre-computed adjusted confidence)

pub struct ScoringWeights {
    pub confidence: f64,
    pub tests: f64,
    pub static_analysis: f64,
}

impl Default for ScoringWeights {
    fn default() -> Self {
        Self {
            confidence: 0.3,
            tests: 0.5,
            static_analysis: 0.2,
        }
    }
}

pub struct PromotionCandidate {
    pub proposals: Vec<Proposal>,
    pub score: f64,
    pub policy_score: f64,
    pub evidence: HashMap<String, String>,
    pub gate_results: Vec<GateResult>,
    pub snapshot_id: String,
    pub timings: HashMap<String, f64>,
    pub excluded_proposal_ids: Vec<String>,
    pub earliest_submission: Option<DateTime<Utc>>,
}

pub struct PromotionEngine {
    // pool: sqlx::SqlitePool, // To be injected
    pub weights: ScoringWeights,
    // pub gate_runner: Arc<GateRunner>,
    // pub snapshot_service: Arc<SnapshotService>,
}

impl Default for PromotionEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl PromotionEngine {
    pub fn new() -> Self {
        Self {
            weights: ScoringWeights::default(),
        }
    }

    /// Computes the final promotion score based on confidence and gate evidence.
    pub fn compute_score(
        &self,
        _proposal: &Proposal,
        gate_results: &[GateResult],
        gates: &[GateConfig],
        trust_adjusted_confidence: f64,
    ) -> (f64, f64, f64) {
        let required_gates: HashSet<_> = gates
            .iter()
            .filter(|g| g.required)
            .map(|g| &g.name)
            .collect();
        let result_map: HashMap<_, _> = gate_results.iter().map(|r| (&r.gate_name, r)).collect();

        let mut policy_score = 1.0;

        for gate_name in required_gates {
            if let Some(result) = result_map.get(gate_name) {
                if result.status != GateStatus::Pass && result.status != GateStatus::Skip {
                    policy_score = 0.0;
                    break;
                }
            } else {
                // Fail closed: a required gate is missing
                policy_score = 0.0;
                break;
            }
        }

        let confidence = trust_adjusted_confidence;

        // Calculate test evidence
        let test_results: Vec<_> = gate_results
            .iter()
            .filter(|r| r.gate_name.to_lowercase().contains("test"))
            .collect();

        let e_tests = if !test_results.is_empty() {
            let passes = test_results
                .iter()
                .filter(|r| r.status == GateStatus::Pass)
                .count() as f64;
            passes / test_results.len() as f64
        } else {
            0.5
        };

        // Calculate static evidence
        let static_results: Vec<_> = gate_results
            .iter()
            .filter(|r| {
                r.gate_name == "lint" || r.gate_name == "syntax" || r.gate_name == "security"
            })
            .collect();

        let e_static = if !static_results.is_empty() {
            let passes = static_results
                .iter()
                .filter(|r| r.status == GateStatus::Pass)
                .count() as f64;
            passes / static_results.len() as f64
        } else {
            0.5
        };

        let raw_score = self.weights.confidence * confidence
            + self.weights.tests * e_tests
            + self.weights.static_analysis * e_static;

        let final_score = policy_score * raw_score;

        (final_score, policy_score, raw_score)
    }

    pub async fn evaluate_proposal(
        &self,
        proposal: &Proposal,
        repo: &Repository,
        pool: &crate::db::Pool,
        trust_service: &crate::trust_service::TrustService,
    ) -> Result<PromotionCandidate> {
        let mut gates: Vec<GateConfig> = vec![];
        if let Some(config_str) = &repo.gate_config {
            gates = serde_json::from_str(config_str)?;
        }

        #[cfg(all(feature = "db-sqlite", not(feature = "db-postgres")))]
        let gate_query = "SELECT * FROM aether_gate_results WHERE repo_id = ? AND proposal_id = ?";
        #[cfg(feature = "db-postgres")]
        let gate_query =
            "SELECT * FROM aether_gate_results WHERE repo_id = $1 AND proposal_id = $2";

        let gate_results = sqlx::query_as::<_, GateResult>(gate_query)
            .bind(&repo.id)
            .bind(&proposal.id)
            .fetch_all(pool)
            .await?;

        // In a real flow, we might run the GateRunner here if results are missing.
        // For the soak test, we assume results are prepopulated or we score based on what's available.

        let combined_trust = trust_service
            .get_combined_agent_score_readonly(
                pool,
                &proposal.agent_id,
                proposal.confidence_score,
                "global",
            )
            .await?;
            
        let (trust_score, _timings) = combined_trust;

        let (final_score, policy_score, raw_score) =
            self.compute_score(proposal, &gate_results, &gates, trust_score);

        let mut evidence = HashMap::new();
        let mut gate_summary = HashMap::new();
        for gr in &gate_results {
            gate_summary.insert(gr.gate_name.clone(), format!("{:?}", gr.status));
        }
        let evidence_payload = serde_json::json!({
            "policy_score": policy_score,
            "raw_score": raw_score,
            "trust_adjusted": trust_score,
            "gates": gate_summary
        });
        evidence.insert(proposal.id.clone(), evidence_payload.to_string());

        Ok(PromotionCandidate {
            proposals: vec![proposal.clone()],
            score: final_score,
            policy_score,
            evidence,
            gate_results,
            snapshot_id: "".to_string(),
            timings: HashMap::new(),
            excluded_proposal_ids: vec![],
            earliest_submission: Some(proposal.submitted_at),
        })
    }
}
