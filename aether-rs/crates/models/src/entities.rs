use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, sqlx::Type)]
#[sqlx(type_name = "TEXT", rename_all = "UPPERCASE")]
#[serde(rename_all = "UPPERCASE")]
pub enum ProposalStatus {
    Ingested,
    Pending,
    Evaluating,
    Promoted,
    Rejected,
    Quarantined,
    Superseded,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, sqlx::Type)]
#[sqlx(type_name = "TEXT", rename_all = "UPPERCASE")]
#[serde(rename_all = "UPPERCASE")]
pub enum GateStatus {
    Pending,
    Running,
    Pass,
    Fail,
    Skip,
    Error,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, sqlx::Type)]
#[sqlx(type_name = "TEXT", rename_all = "snake_case")]
#[serde(rename_all = "snake_case")]
pub enum SystemMetricEventType {
    SubmitAccepted,
    StatusPromoted,
    StatusRejected,
    StatusQuarantined,
    StatusSuperseded,
    SemanticFallback,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, sqlx::Type)]
#[sqlx(type_name = "TEXT", rename_all = "snake_case")]
#[serde(rename_all = "snake_case")]
pub enum ProposalSource {
    SyntheticHarness,
    MxSwarm,
    Manual,
    Other,
}

// ============================================================================
// SQLx entities (persisted)
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Repository {
    pub id: String,
    pub name: String,
    pub description: Option<String>,
    pub head_id: Option<String>,
    pub head_sequence: i64,
    pub gate_config: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub max_alternatives_per_conflict: i64,
    pub auto_promote: bool,
    #[serde(default)]
    pub crdt_enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Proposal {
    pub id: String,
    pub repo_id: String,
    pub agent_id: String,
    pub task_id: Option<String>,
    pub source: String, // String mapped to ProposalSource values
    pub source_run_id: Option<String>,
    pub source_task_id: Option<String>,
    pub source_agent_id: Option<String>,
    pub source_attempt_id: Option<String>,
    pub intent_type: Option<String>,
    pub base_head_id: Option<String>,
    pub base_head_sequence: i64,
    pub change_type: String,
    pub change_content: String,
    pub affected_paths: String,
    pub affected_symbols: Option<String>,
    pub risk_tier: i64,
    pub confidence_score: f64,
    pub model_id: Option<String>,
    pub calibration_profile: Option<String>,
    pub rationale: Option<String>,
    pub dependencies: String,
    pub requires_tests: bool,
    pub requires_security_scan: bool,
    pub status: ProposalStatus,
    pub conflict_set_id: Option<String>,
    pub submitted_at: DateTime<Utc>,
    pub evaluated_at: Option<DateTime<Utc>>,
    pub promoted_at: Option<DateTime<Utc>>,
    pub idempotency_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct ConflictSet {
    pub id: String,
    pub repo_id: String,
    pub path: String,
    pub region_type: String,
    pub start_line: Option<i64>,
    pub end_line: Option<i64>,
    pub symbol_name: Option<String>,
    pub proposal_ids: String,
    pub active_choice_id: Option<String>,
    pub resolved: bool,
    pub resolved_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Snapshot {
    pub id: String,
    pub repo_id: String,
    pub head_sequence: i64,
    pub included_proposals: String,
    pub content_hash: String,
    pub storage_path: Option<String>,
    pub created_at: DateTime<Utc>,
    pub created_by: Option<String>,
    pub file_count: i64,
    pub total_bytes: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct GateResult {
    pub id: String,
    pub repo_id: String,
    pub snapshot_id: String,
    pub proposal_id: Option<String>,
    pub gate_name: String,
    pub gate_command: Option<String>,
    pub status: GateStatus,
    pub exit_code: Option<i64>,
    pub stdout: Option<String>,
    pub stderr: Option<String>,
    pub duration_ms: Option<i64>,
    pub tests_passed: Option<i64>,
    pub tests_failed: Option<i64>,
    pub coverage_pct: Option<f64>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Promotion {
    pub id: String,
    pub repo_id: String,
    pub from_head_id: Option<String>,
    pub from_head_sequence: i64,
    pub to_head_id: String,
    pub to_head_sequence: i64,
    pub included_proposals: String,
    pub excluded_proposals: String,
    pub evidence: String,
    pub policy_score: f64,
    pub confidence_score: f64,
    pub final_score: f64,
    pub decision_rationale: Option<String>,
    pub triggered_by: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct OperationGraphNode {
    pub id: String,
    pub repo_id: String,
    pub promotion_id: String,
    pub parent_node_id: Option<String>,
    pub parent_head_id: Option<String>,
    pub parent_head_sequence: i64,
    pub to_head_id: Option<String>,
    pub to_head_sequence: i64,
    pub snapshot_id: String,
    pub operation_type: String,
    pub node_hash: String,
    pub payload_sha256: String,
    pub parent_ref: Option<String>,
    pub replay_from_hash: Option<String>,
    pub replay_to_hash: Option<String>,
    pub replay_gate_vector: String,
    pub included_proposals: String,
    pub excluded_proposals: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct SystemMetricEvent {
    pub id: String,
    pub repo_id: Option<String>,
    pub proposal_id: Option<String>,
    pub event_type: SystemMetricEventType,
    pub event_reason: Option<String>,
    pub path: Option<String>,
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct AetherHead {
    pub id: String,
    pub repo_id: String,
    pub sequence: i64,
    pub snapshot_id: String,
    pub promotion_id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub is_canonical: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct AgentMetric {
    pub agent_id: String,
    pub scope: String,
    pub total_proposals: i64,
    pub promoted_count: i64,
    pub hard_fail_count: i64,
    pub neutral_loss_count: i64,
    pub trust_alpha: f64,
    pub trust_beta: f64,
    pub last_seen: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

// ============================================================================
// API Models (Serde parsing structs)
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposalCreate {
    pub agent_id: String,
    pub task_id: Option<String>,
    #[serde(default = "default_proposal_source")]
    pub source: String,
    pub source_run_id: Option<String>,
    pub source_task_id: Option<String>,
    pub source_agent_id: Option<String>,
    pub source_attempt_id: Option<String>,
    pub intent_type: Option<String>,
    // Provenance: which canonical head this proposal was based on.
    // If omitted, the server fills from the current repo head.
    pub base_head_id: Option<String>,
    pub base_head_sequence: Option<i64>,
    #[serde(default = "default_change_type")]
    pub change_type: String,
    pub change_content: String,
    pub affected_paths: Vec<String>,
    pub affected_symbols: Option<Vec<String>>,
    #[serde(default = "default_risk_tier")]
    pub risk_tier: i64,
    #[serde(default = "default_confidence_score")]
    pub confidence_score: f64,
    pub model_id: Option<String>,
    pub calibration_profile: Option<String>,
    pub rationale: Option<String>,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default = "default_true")]
    pub requires_tests: bool,
    #[serde(default = "default_false")]
    pub requires_security_scan: bool,
    pub idempotency_key: Option<String>,
}

fn default_proposal_source() -> String {
    "manual".to_string()
}
fn default_change_type() -> String {
    "unified_diff".to_string()
}
fn default_risk_tier() -> i64 {
    1
}
fn default_confidence_score() -> f64 {
    0.5
}
fn default_true() -> bool {
    true
}
fn default_false() -> bool {
    false
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposalResponse {
    pub proposal_id: String,
    pub status: ProposalStatus,
    pub source: String,
    pub conflict_set_id: Option<String>,
    pub immediate_gate_results: std::collections::HashMap<String, GateStatus>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotCreate {
    #[serde(default = "default_selection_type")]
    pub selection_type: String,
    pub proposal_ids: Option<Vec<String>>,
    pub at_sequence: Option<i64>,
}

fn default_selection_type() -> String {
    "head".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotResponse {
    pub snapshot_id: String,
    pub content_hash: String,
    pub file_count: i64,
    pub total_bytes: i64,
    pub download_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromotionRequest {
    pub proposal_ids: Vec<String>,
    #[serde(default)]
    pub skip_gates: Vec<String>,
    #[serde(default = "default_false")]
    pub force: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromotionResponse {
    pub promotion_id: String,
    pub success: bool,
    pub new_head_id: String,
    pub new_head_sequence: i64,
    pub included_proposals: Vec<String>,
    pub excluded_proposals: Vec<String>,
    pub evidence: std::collections::HashMap<String, String>,
    pub final_score: f64,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposalListResponse {
    pub proposals: Vec<Proposal>,
    pub total: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromotionListResponse {
    pub promotions: Vec<Promotion>,
    pub total: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HeadResponse {
    pub head_id: String,
    pub sequence: i64,
    pub snapshot_id: String,
    pub content_hash: String,
    pub last_promotion_id: Option<String>,
    pub last_promotion_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GateConfig {
    pub name: String,
    pub command: String,
    #[serde(default = "default_true")]
    pub required: bool,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: i64,
    #[serde(default = "default_order")]
    pub order: i64,
}

fn default_timeout() -> i64 {
    300
}
fn default_order() -> i64 {
    0
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoConfig {
    #[serde(default)]
    pub gates: Vec<GateConfig>,
    #[serde(default = "default_max_alternatives")]
    pub max_alternatives_per_conflict: i64,
    #[serde(default = "default_true")]
    pub auto_promote: bool,
    #[serde(default = "default_promotion_threshold")]
    pub promotion_threshold: f64,
}

fn default_max_alternatives() -> i64 {
    5
}
fn default_promotion_threshold() -> f64 {
    0.7
}

#[derive(Debug, Serialize, Deserialize)]
pub struct OperationNodeData {
    pub id: String,
    pub parent_node_id: Option<String>,
    pub operation_type: String,
    pub node_hash: String,
    pub payload_sha256: String,
    pub parent_ref: String,
    pub replay_from_hash: String,
    pub replay_to_hash: String,
    pub replay_gate_vector: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ConflictResolutionData {
    pub conflict_set_id: String,
    pub active_choice_id: String,
    pub losing_proposal_ids: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CommitPromotionRequest {
    pub promotion_id: String,
    pub snapshot_id: String,
    pub included_proposals: Vec<String>,
    pub excluded_proposals: Vec<String>,
    pub evidence: std::collections::HashMap<String, String>,
    pub policy_score: f64,
    pub final_score: f64,
    pub confidence_score: f64,
    pub triggered_by: String,
    pub operation_node: Option<OperationNodeData>,
    pub conflict_resolutions: Vec<ConflictResolutionData>,
}
