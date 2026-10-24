// PostgreSQL backend module (compiled only with --features db-postgres)
#[cfg(feature = "db-postgres")]
mod db_postgres;
#[cfg(feature = "db-postgres")]
pub mod crdt_promotion;

#[cfg(feature = "db-sqlite")]
use anyhow::Context;
#[cfg(not(feature = "db-sqlite"))]
use axum::Json;
#[cfg(feature = "db-sqlite")]
use axum::{
    extract::{Path, State},
    routing::{get, post},
    Json, Router,
};
#[cfg(feature = "db-sqlite")]
use models::entities::{
    HeadResponse, PromotionListResponse, PromotionRequest, PromotionResponse, ProposalCreate,
    ProposalListResponse, ProposalResponse, ProposalStatus, Repository,
};
#[cfg(feature = "db-sqlite")]
use serde::{Deserialize, Serialize};
#[cfg(feature = "db-sqlite")]
use sqlx::{sqlite::SqliteConnectOptions, SqlitePool};
#[cfg(feature = "db-sqlite")]
use std::str::FromStr;
#[cfg(feature = "db-sqlite")]
use std::sync::Arc;
#[cfg(feature = "db-sqlite")]
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[cfg(feature = "db-sqlite")]
#[derive(Clone)]
struct AppState {
    pool: SqlitePool,
    trust_service: Arc<services::trust_service::TrustService>,
}

#[cfg(feature = "db-sqlite")]
#[derive(Debug, Serialize, Deserialize)]
struct RepoCreateRequest {
    name: String,
    #[serde(default)]
    description: Option<String>,
}

// ============================================================================
// Entrypoint: exactly one main() compiles per feature
// ============================================================================

#[cfg(feature = "db-postgres")]
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    db_postgres::pg_main().await
}

#[cfg(feature = "db-sqlite")]
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "server=debug,tower_http=debug".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    // 1. Resolve persistent DB Path (matching Python behavior: ~/.aether/aether.db)
    let db_url = std::env::var("AETHER_DB_PATH").unwrap_or_else(|_| {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        format!("sqlite://{}/.aether/aether.db", home)
    });

    tracing::info!("Connecting to Aether Control Plane Database at {}", db_url);

    // 2. Connect and create file if missing
    let conn_options = SqliteConnectOptions::from_str(&db_url)?
        .create_if_missing(true)
        .journal_mode(sqlx::sqlite::SqliteJournalMode::Wal)
        .busy_timeout(std::time::Duration::from_millis(30000))
        .synchronous(sqlx::sqlite::SqliteSynchronous::Normal)
        .foreign_keys(true);

    let pool = sqlx::sqlite::SqlitePoolOptions::new()
        .max_connections(50)
        .connect_with(conn_options)
        .await
        .context("Failed to connect to Aether Database")?;

    // 3. Run Migrations
    tracing::info!("Running database migrations...");
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .context("Failed to run database migrations")?;

    let trust_service = Arc::new(services::trust_service::TrustService::new(pool.clone()));

    let state = Arc::new(AppState {
        pool,
        trust_service,
    });

    // 4. Register strictly-typed Axum routes
    let app = Router::new()
        // Step 1: Health & root API parity
        .route("/", get(root_handler))
        .route("/v1/health", get(health_check))
        .route("/v1/repos", get(list_repos).post(create_repo))
        .route("/v1/repos/:id", get(get_repo))
        .route(
            "/v1/repos/:id/proposals",
            get(list_proposals).post(submit_proposal),
        )
        .route("/v1/repos/:id/head", get(get_head))
        .route(
            "/v1/repos/:id/promotions",
            get(list_promotions).post(request_promotion),
        )
        .route("/v1/repos/:id/commit_promotion", post(commit_promotion))
        .layer(axum::middleware::from_fn(api_token_auth_middleware))
        .with_state(state);

    let host = resolve_server_host_from_env()?;
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8100);
    let listener = tokio::net::TcpListener::bind(format!("{}:{}", host, port)).await?;
    tracing::info!("Aether-RS Server listening on {}", listener.local_addr()?);
    axum::serve(listener, app).await?;

    Ok(())
}

// ----------------------------------------------------------------------------
// Network Bind Validation & API Token Authentication — shared across backends
// ----------------------------------------------------------------------------

pub const DEFAULT_SERVER_HOST: &str = "127.0.0.1";

fn is_truthy_str(raw: Option<&str>) -> bool {
    match raw {
        Some(v) => matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "y" | "on"
        ),
        None => false,
    }
}

fn is_loopback_host(host: &str) -> bool {
    matches!(host, "127.0.0.1" | "localhost" | "::1")
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (&x, &y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn resolve_and_validate_bind_host(
    raw_host: Option<&str>,
    allow_unsafe_bind: Option<&str>,
    api_token: Option<&str>,
) -> Result<String, String> {
    let resolved = raw_host
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(DEFAULT_SERVER_HOST)
        .to_string();

    if !is_loopback_host(&resolved) {
        if !is_truthy_str(allow_unsafe_bind) {
            return Err(format!(
                "Refusing to bind Aether-RS server to non-loopback host '{}' by default. \
                 Network deployment requires authentication (AETHER_API_TOKEN) and execution isolation, \
                 plus explicit opt-in via AETHER_ALLOW_UNSAFE_NETWORK_BIND=true.",
                resolved
            ));
        }
        let token = api_token.map(str::trim).unwrap_or("");
        if token.is_empty() {
            return Err(format!(
                "Refusing to bind Aether-RS server to non-loopback host '{}' without AETHER_API_TOKEN set.",
                resolved
            ));
        }
    }

    Ok(resolved)
}

pub fn resolve_server_host_from_env() -> anyhow::Result<String> {
    let raw_host = std::env::var("AETHER_HOST").ok();
    let allow_unsafe = std::env::var("AETHER_ALLOW_UNSAFE_NETWORK_BIND").ok();
    let api_token = std::env::var("AETHER_API_TOKEN").ok();
    resolve_and_validate_bind_host(
        raw_host.as_deref(),
        allow_unsafe.as_deref(),
        api_token.as_deref(),
    )
    .map_err(anyhow::Error::msg)
}

pub fn is_authorized_request(
    method: &axum::http::Method,
    path: &str,
    headers: &axum::http::HeaderMap,
    configured_token: Option<&str>,
) -> bool {
    let token = configured_token.map(str::trim).unwrap_or("");
    if token.is_empty() {
        return true;
    }
    if *method == axum::http::Method::OPTIONS || path == "/" || path == "/v1/health" {
        return true;
    }

    let bearer_candidate = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            if v.len() >= 7 && v[..7].eq_ignore_ascii_case("bearer ") {
                Some(v[7..].trim())
            } else {
                None
            }
        })
        .filter(|s| !s.is_empty());

    let header_candidate = headers
        .get("x-aether-token")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty());

    let candidate = bearer_candidate.or(header_candidate).unwrap_or("");
    !candidate.is_empty() && constant_time_eq(candidate.as_bytes(), token.as_bytes())
}

pub async fn api_token_auth_middleware(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> Result<axum::response::Response, (axum::http::StatusCode, Json<serde_json::Value>)> {
    let configured_token = std::env::var("AETHER_API_TOKEN").ok();
    let path = req.uri().path();
    if !is_authorized_request(req.method(), path, req.headers(), configured_token.as_deref()) {
        return Err((
            axum::http::StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({
                "detail": "Unauthorized: valid AETHER_API_TOKEN required"
            })),
        ));
    }
    Ok(next.run(req).await)
}

// ----------------------------------------------------------------------------
// Health & Root Handlers — shared across backends
// ----------------------------------------------------------------------------

#[allow(dead_code)]
async fn root_handler() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "service": "aether-rs",
        "version": "0.1.0"
    }))
}

#[allow(dead_code)]
async fn health_check() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "healthy",
        "service": "aether",
        "version": "0.1.0"
    }))
}

// ----------------------------------------------------------------------------
// SQLite Route Handlers (compiled only with db-sqlite)
// ----------------------------------------------------------------------------

#[cfg(feature = "db-sqlite")]
async fn list_repos(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Vec<Repository>>, axum::http::StatusCode> {
    let repos = sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories")
        .fetch_all(&state.pool)
        .await
        .map_err(|e| {
            tracing::error!("Failed to fetch repos: {}", e);
            axum::http::StatusCode::INTERNAL_SERVER_ERROR
        })?;
    Ok(Json(repos))
}

#[cfg(feature = "db-sqlite")]
async fn create_repo(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<RepoCreateRequest>,
) -> Result<Json<Repository>, axum::http::StatusCode> {
    let id = format!(
        "repo_{}",
        uuid::Uuid::new_v4()
            .simple()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    );
    let now = chrono::Utc::now();

    let repo = Repository {
        id: id.clone(),
        name: payload.name.clone(),
        description: payload.description.clone(),
        head_id: None,
        head_sequence: 0,
        gate_config: None,
        created_at: now,
        updated_at: now,
        max_alternatives_per_conflict: 5,
        auto_promote: true,
        crdt_enabled: false,
    };

    sqlx::query(
        "INSERT INTO aether_repositories (id, name, description, head_sequence, created_at, updated_at, max_alternatives_per_conflict, auto_promote) 
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    .bind(&repo.id)
    .bind(&repo.name)
    .bind(&repo.description)
    .bind(repo.head_sequence)
    .bind(repo.created_at)
    .bind(repo.updated_at)
    .bind(repo.max_alternatives_per_conflict)
    .bind(repo.auto_promote)
    .execute(&state.pool)
    .await
    .map_err(|e| {
        tracing::error!("Failed to insert repo: {}", e);
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    })?;

    Ok(Json(repo))
}

#[cfg(feature = "db-sqlite")]
async fn get_repo(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
) -> Result<Json<Repository>, axum::http::StatusCode> {
    let repo = sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories WHERE id = ?")
        .bind(&id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?;

    repo.map(Json).ok_or(axum::http::StatusCode::NOT_FOUND)
}

#[cfg(feature = "db-sqlite")]
async fn submit_proposal(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
    Json(payload): Json<ProposalCreate>,
) -> Result<Json<ProposalResponse>, axum::http::StatusCode> {
    let prop_id = format!(
        "prop_{}",
        uuid::Uuid::new_v4()
            .simple()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    );

    let mut tx = state
        .pool
        .begin()
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?;

    let now = chrono::Utc::now();
    let change_content = payload.change_content.clone();
    let normalized_affected_paths = crdt::document_store::validate_proposal_paths(
        &payload.change_type,
        &change_content,
        &payload.affected_paths,
    )
    .map_err(|e| {
        tracing::warn!("Rejected proposal with invalid paths: {}", e);
        axum::http::StatusCode::BAD_REQUEST
    })?;
    let affected_paths_json =
        serde_json::to_string(&normalized_affected_paths).unwrap_or_else(|_| "[]".to_string());
    let dependencies_json =
        serde_json::to_string(&payload.dependencies).unwrap_or_else(|_| "[]".to_string());

    // Step 3: Resolve base_head provenance — fill each missing field
    // independently from the current repo head.
    let repo =
        sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories WHERE id = ?")
            .bind(&id)
            .fetch_optional(&mut *tx)
            .await
            .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?;
    let repo = match repo {
        Some(r) => r,
        None => return Err(axum::http::StatusCode::NOT_FOUND),
    };

    let head_snapshot_id: Option<String> = if let Some(ref h_id) = repo.head_id {
        sqlx::query_as::<_, (String,)>("SELECT snapshot_id FROM aether_heads WHERE id = ?")
            .bind(h_id)
            .fetch_optional(&mut *tx)
            .await
            .ok()
            .flatten()
            .map(|r| r.0)
    } else {
        None
    };

    let has_symbols = match &payload.affected_symbols {
        Some(syms) => !syms.is_empty(),
        None => false,
    };

    let computed_symbols = if !has_symbols {
        let mut doc_store = crdt::document_store::DocumentStore::new(id.clone());
        if let Some(ref snap_id) = head_snapshot_id {
            let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
            let snapshot_path = std::path::PathBuf::from(format!("{}/.aether/aether_storage/{}/snapshots/{}", home, id, snap_id));
            let _ = doc_store.load_from_snapshot(&snapshot_path);
        }
        crdt::symbol_adapters::compute_affected_symbols(
            &payload.change_type,
            &change_content,
            &normalized_affected_paths,
            &doc_store,
        )
    } else {
        payload.affected_symbols.clone().unwrap_or_default()
    };
    let affected_symbols_json = serde_json::to_string(&computed_symbols).unwrap_or_else(|_| "[]".to_string());

    let (base_head_id, base_head_sequence) = {
        let head_id = payload.base_head_id.clone().or(repo.head_id);
        let head_seq = payload.base_head_sequence.unwrap_or(repo.head_sequence);
        (head_id, head_seq)
    };

    // Step 2: Insert as PENDING (not INGESTED) — Rust server has no async
    // materialization worker yet, so proposals must be immediately promotable.
    // Step 3: Persist ALL provenance fields from ProposalCreate.
    sqlx::query(
        "INSERT INTO aether_proposals 
         (id, repo_id, agent_id, task_id, source, source_run_id, source_task_id, source_agent_id, source_attempt_id, intent_type, base_head_id, base_head_sequence, status, confidence_score, change_type, change_content, affected_paths, affected_symbols, risk_tier, model_id, calibration_profile, rationale, dependencies, requires_tests, requires_security_scan, idempotency_key, submitted_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    .bind(&prop_id)
    .bind(&id)
    .bind(&payload.agent_id)
    .bind(&payload.task_id)
    .bind(&payload.source)
    .bind(&payload.source_run_id)
    .bind(&payload.source_task_id)
    .bind(&payload.source_agent_id)
    .bind(&payload.source_attempt_id)
    .bind(&payload.intent_type)
    .bind(&base_head_id)
    .bind(base_head_sequence)
    .bind("PENDING")
    .bind(payload.confidence_score)
    .bind(&payload.change_type)
    .bind(&change_content)
    .bind(&affected_paths_json)
    .bind(&affected_symbols_json)
    .bind(payload.risk_tier)
    .bind(&payload.model_id)
    .bind(&payload.calibration_profile)
    .bind(&payload.rationale)
    .bind(&dependencies_json)
    .bind(payload.requires_tests)
    .bind(payload.requires_security_scan)
    .bind(&payload.idempotency_key)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(|e| {
        tracing::error!("Failed to insert proposal: {}", e);
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    })?;

    tx.commit().await.map_err(|e| {
        tracing::error!("Failed to commit submission transaction: {}", e);
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let delta = services::trust_service::AgentMetricDelta {
        total_proposals: 1,
        ..Default::default()
    };
    let trust_service = state.trust_service.clone();
    let agent_id_clone = payload.agent_id.clone();
    tokio::spawn(async move {
        if let Err(e) = trust_service
            .record_metric_deltas(&agent_id_clone, "global", &delta)
            .await
        {
            tracing::error!(
                "trust_upsert_failure: agent={} scope=global metric=total_proposals err={}",
                agent_id_clone,
                e
            );
        }
    });

    Ok(Json(ProposalResponse {
        proposal_id: prop_id,
        status: ProposalStatus::Pending,
        source: payload.source,
        conflict_set_id: None,
        immediate_gate_results: std::collections::HashMap::new(),
        message: format!("Proposal accepted for repo {}", id),
    }))
}

#[cfg(feature = "db-sqlite")]
async fn list_proposals(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
) -> Result<Json<ProposalListResponse>, axum::http::StatusCode> {
    let proposals = sqlx::query_as::<_, models::entities::Proposal>(
        "SELECT * FROM aether_proposals WHERE repo_id = ? ORDER BY submitted_at DESC LIMIT 100",
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await
    .map_err(|e| {
        tracing::error!("Failed to fetch proposals: {}", e);
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let total = proposals.len() as i64;
    Ok(Json(ProposalListResponse { proposals, total }))
}

#[cfg(feature = "db-sqlite")]
async fn get_head(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
) -> Result<Json<HeadResponse>, axum::http::StatusCode> {
    let repo = sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories WHERE id = ?")
        .bind(&id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
        .ok_or(axum::http::StatusCode::NOT_FOUND)?;

    let head_id = repo.head_id.clone().unwrap_or_default();

    // Also fetch snapshot id for reality
    let (snapshot_id, content_hash, last_promotion_id, last_promotion_at) = if !head_id.is_empty() {
        let head_row = sqlx::query_as::<_, (String, Option<String>)>(
            "SELECT snapshot_id, promotion_id FROM aether_heads WHERE id = ?",
        )
        .bind(&head_id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
        .ok_or(axum::http::StatusCode::NOT_FOUND)?;

        let snap_id = head_row.0;
        let prom_id_opt = head_row.1;

        let hash = sqlx::query_as::<_, (String,)>(
            "SELECT content_hash FROM aether_snapshots WHERE id = ?",
        )
        .bind(&snap_id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
        .map(|r| r.0)
        .ok_or(axum::http::StatusCode::NOT_FOUND)?;

        let mut prom_at = None;
        if let Some(prom_id) = &prom_id_opt {
            prom_at = sqlx::query_as::<_, (chrono::DateTime<chrono::Utc>,)>(
                "SELECT created_at FROM aether_promotions WHERE id = ?",
            )
            .bind(prom_id)
            .fetch_optional(&state.pool)
            .await
            .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
            .map(|r| r.0);
        }

        (snap_id, hash, prom_id_opt, prom_at)
    } else {
        ("".to_string(), "".to_string(), None, None)
    };

    Ok(Json(HeadResponse {
        head_id: repo.head_id.unwrap_or_default(),
        sequence: repo.head_sequence,
        snapshot_id,
        content_hash,
        last_promotion_id,
        last_promotion_at,
    }))
}

#[cfg(feature = "db-sqlite")]
async fn request_promotion(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
    Json(payload): Json<PromotionRequest>,
) -> Result<Json<PromotionResponse>, axum::http::StatusCode> {
    let prom_id = format!(
        "prom_{}",
        uuid::Uuid::new_v4()
            .simple()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    );
    let now = chrono::Utc::now();

    // 1. Resolve current repo
    let repo = sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories WHERE id = ?")
        .bind(&id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
        .ok_or(axum::http::StatusCode::NOT_FOUND)?;

    let old_head_id = repo.head_id.clone();
    let old_seq = repo.head_sequence;

    // 2. Resolve proposals
    let mut proposals = Vec::new();
    for prop_id in &payload.proposal_ids {
        let prop = sqlx::query_as::<_, models::entities::Proposal>(
            "SELECT * FROM aether_proposals WHERE id = ? AND repo_id = ? AND status = 'PENDING'",
        )
        .bind(prop_id)
        .bind(&id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?;

        if let Some(p) = prop {
            proposals.push(p);
        }
    }

    if proposals.is_empty() {
        return Ok(Json(PromotionResponse {
            promotion_id: prom_id,
            success: false,
            new_head_id: repo.head_id.unwrap_or_default(),
            new_head_sequence: repo.head_sequence,
            included_proposals: vec![],
            excluded_proposals: payload.proposal_ids.clone(),
            evidence: std::collections::HashMap::new(),
            final_score: 0.0,
            message: "No pending proposals found to promote.".to_string(),
        }));
    }

    // 3. Orchestrate
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let storage_base = std::path::PathBuf::from(format!("{}/.aether/aether_storage", home));
    let orchestrator =
        services::promotion_orchestrator::PromotionOrchestrator::new(storage_base.clone());

    let base_snapshot_id = if let Some(h_id) = &old_head_id {
        sqlx::query_as::<_, (String,)>("SELECT snapshot_id FROM aether_heads WHERE id = ?")
            .bind(h_id)
            .fetch_optional(&state.pool)
            .await
            .map_err(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR)?
            .map(|row| row.0)
            .unwrap_or_default()
    } else {
        "".to_string()
    };
    let trust_service = services::trust_service::TrustService::new(state.pool.clone());
    let is_batch = proposals.len() > 1;
    let lead_proposal = &proposals[0];
    let (trust_score, trust_timings) = if is_batch {
        let avg_confidence =
            proposals.iter().map(|p| p.confidence_score).sum::<f64>() / (proposals.len() as f64);
        trust_service
            .get_combined_agent_score_readonly(&state.pool, "batch", avg_confidence, "global")
            .await
            .unwrap_or((avg_confidence, std::collections::HashMap::new()))
    } else {
        trust_service
            .get_combined_agent_score_readonly(
                &state.pool,
                &lead_proposal.agent_id,
                lead_proposal.confidence_score,
                "global",
            )
            .await
            .unwrap_or((lead_proposal.confidence_score, std::collections::HashMap::new()))
    };
    let orch_res = match orchestrator
        .orchestrate_promotion(&repo, &proposals, &base_snapshot_id, trust_score, trust_timings)
        .await
    {
        Ok(res) => res,
        Err(services::promotion_orchestrator::OrchestrationError::BadRequest(e)) => {
            tracing::error!("Orchestration bad request: {}", e);
            return Err(axum::http::StatusCode::BAD_REQUEST);
        }
        Err(services::promotion_orchestrator::OrchestrationError::Internal(e)) => {
            tracing::error!("Orchestration failed: {}", e);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
    };

    let candidate_proposals = orch_res.included_proposals;
    let included: Vec<String> = candidate_proposals.iter().map(|p| p.id.clone()).collect();
    let excluded: Vec<String> = payload
        .proposal_ids
        .iter()
        .filter(|id| !included.contains(id))
        .cloned()
        .collect();

    let snapshot_id = orch_res.snapshot_id;
    let state_hash = orch_res.state_hash;
    let file_count = orch_res.file_count;
    let total_bytes = orch_res.total_bytes;
    let policy_score = orch_res.policy_score;
    let final_score = orch_res.final_score;
    let raw_score = orch_res.raw_score;

    // Move temp workspace to canonical
    let storage_path_str = storage_base
        .join(&id)
        .join("snapshots")
        .join(&snapshot_id)
        .to_string_lossy()
        .to_string();
    let canonical_path = std::path::PathBuf::from(&storage_path_str);
    if let Some(parent) = canonical_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(e) = std::fs::rename(&orch_res.temp_workspace, &canonical_path) {
        tracing::error!("Failed to move temp workspace to canonical path: {}", e);
        let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
        return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    }

    let mut included_agent_ids = Vec::new();
    for p in &candidate_proposals {
        included_agent_ids.push(p.agent_id.clone());
    }

    // 4. Commit transaction
    let mut tx = match state.pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            orchestrator.cleanup_snapshot(&repo.id, &snapshot_id);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
    };

    // Revalidate repo
    let repo_check =
        match sqlx::query_as::<_, Repository>("SELECT * FROM aether_repositories WHERE id = ?")
            .bind(&id)
            .fetch_optional(&mut *tx)
            .await
        {
            Ok(Some(r)) => r,
            _ => {
                orchestrator.cleanup_snapshot(&repo.id, &snapshot_id);
                return Err(axum::http::StatusCode::NOT_FOUND);
            }
        };

    if repo_check.head_id != old_head_id || repo_check.head_sequence != old_seq {
        let _ = std::fs::remove_dir_all(&canonical_path);
        return Err(axum::http::StatusCode::CONFLICT);
    }

    for prop_id in &included {
        let prop_check = match sqlx::query_as::<_, (String, String)>(
            "SELECT status, repo_id FROM aether_proposals WHERE id = ?",
        )
        .bind(prop_id)
        .fetch_optional(&mut *tx)
        .await
        {
            Ok(Some(p)) => p,
            _ => {
                let _ = std::fs::remove_dir_all(&canonical_path);
                return Err(axum::http::StatusCode::CONFLICT);
            }
        };

        if prop_check.0 != "PENDING" || prop_check.1 != id {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::CONFLICT);
        }
    }

    let included_json = serde_json::to_string(&included).unwrap_or_else(|_| "[]".to_string());
    let excluded_json = serde_json::to_string(&excluded).unwrap_or_else(|_| "[]".to_string());

    let mut api_evidence = std::collections::HashMap::new();
    for res in &orch_res.gate_results {
        api_evidence.insert(
            res.gate_name.clone(),
            format!("{:?}", res.status).to_uppercase(),
        );
    }

    let mut db_evidence = api_evidence.clone();
    db_evidence.insert("file_count".to_string(), orch_res.file_count.to_string());
    db_evidence.insert("total_bytes".to_string(), orch_res.total_bytes.to_string());
    db_evidence.insert("content_hash".to_string(), state_hash.clone());

    let evidence_json = serde_json::to_string(&db_evidence).unwrap_or_else(|_| "{}".to_string());
    let new_seq = old_seq + 1;

    // 1. Always write the snapshot
    if sqlx::query(
        "INSERT INTO aether_snapshots (id, repo_id, head_sequence, included_proposals, content_hash, storage_path, created_at, file_count, total_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    .bind(&snapshot_id)
    .bind(&id)
    .bind(new_seq)
    .bind(&included_json)
    .bind(&state_hash)
    .bind(&storage_path_str)
    .bind(now)
    .bind(file_count as i64)
    .bind(total_bytes as i64)
    .execute(&mut *tx)
    .await.is_err() {
        let _ = std::fs::remove_dir_all(&canonical_path);
        return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    }

    // 2. Always write gate results
    for g_res in orch_res.gate_results {
        if sqlx::query("INSERT INTO aether_gate_results (id, repo_id, snapshot_id, proposal_id, gate_name, gate_command, status, exit_code, stdout, stderr, duration_ms, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
            .bind(&g_res.id)
            .bind(&g_res.repo_id)
            .bind(&g_res.snapshot_id)
            .bind(&g_res.proposal_id)
            .bind(&g_res.gate_name)
            .bind(&g_res.gate_command)
            .bind(format!("{:?}", g_res.status).to_uppercase())
            .bind(g_res.exit_code)
            .bind(&g_res.stdout)
            .bind(&g_res.stderr)
            .bind(g_res.duration_ms)
            .bind(g_res.started_at)
            .bind(g_res.completed_at)
            .execute(&mut *tx)
            .await.is_err() {
                let _ = std::fs::remove_dir_all(&canonical_path);
                return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
            }
    }

    // 3. Check Policy Score
    if policy_score == 0.0 {
        // Commit transaction to save snapshot + gates
        if tx.commit().await.is_err() {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }

        if candidate_proposals.len() == 1 {
            let delta = services::trust_service::AgentMetricDelta {
                hard_fail_count: 1,
                trust_beta_delta: 3.0,
                ..Default::default()
            };
            let trust_service = state.trust_service.clone();
            let agent_id = candidate_proposals[0].agent_id.clone();
            tokio::spawn(async move {
                if let Err(e) = trust_service.record_metric_deltas(&agent_id, "global", &delta).await
                {
                    tracing::error!("hard_fail_trust_upsert_failure: agent={} err={}", agent_id, e);
                }
            });
        }

        return Ok(axum::Json(PromotionResponse {
            promotion_id: prom_id,
            success: false,
            new_head_id: old_head_id.unwrap_or_default(),
            new_head_sequence: old_seq,
            included_proposals: Vec::new(),
            excluded_proposals: payload.proposal_ids.clone(),
            evidence: api_evidence.clone(),
            final_score,
            message: "Promotion rejected: Required gates failed or missing.".to_string(),
        }));
    }

    // 4. Update proposals to PROMOTED, REJECTED, or SUPERSEDED
    for prop_id in &included {
        if sqlx::query(
            "UPDATE aether_proposals SET status = 'PROMOTED', promoted_at = ? WHERE id = ?",
        )
        .bind(now)
        .bind(prop_id)
        .execute(&mut *tx)
        .await
        .is_err()
        {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
    }
    for (prop_id, reason) in &orch_res.rejected_proposals {
        if sqlx::query("UPDATE aether_proposals SET status = 'REJECTED' WHERE id = ?")
            .bind(prop_id)
            .execute(&mut *tx)
            .await
            .is_err()
        {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
        if sqlx::query("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, event_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)")
            .bind(generate_id("evt"))
            .bind(&id)
            .bind(prop_id)
            .bind("STATUS_REJECTED")
            .bind(reason)
            .bind(now)
            .execute(&mut *tx)
            .await
            .is_err()
        {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
    }
    for (prop_id, reason) in &orch_res.superseded_proposals {
        if sqlx::query("UPDATE aether_proposals SET status = 'SUPERSEDED' WHERE id = ?")
            .bind(prop_id)
            .execute(&mut *tx)
            .await
            .is_err()
        {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
        if sqlx::query("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, event_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)")
            .bind(generate_id("evt"))
            .bind(&id)
            .bind(prop_id)
            .bind("STATUS_SUPERSEDED")
            .bind(reason)
            .bind(now)
            .execute(&mut *tx)
            .await
            .is_err()
        {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }
    }

    let head_id = format!(
        "head_{}",
        uuid::Uuid::new_v4()
            .simple()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    );

    if sqlx::query("INSERT INTO aether_promotions (id, repo_id, from_head_id, from_head_sequence, to_head_id, to_head_sequence, included_proposals, excluded_proposals, evidence, policy_score, confidence_score, final_score, triggered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        .bind(&prom_id)
        .bind(&id)
        .bind(&old_head_id)
        .bind(old_seq)
        .bind(&head_id)
        .bind(new_seq)
        .bind(&included_json)
        .bind(&excluded_json)
        .bind(&evidence_json)
        .bind(policy_score)
        .bind(raw_score) // Raw score is parity-correct!
        .bind(final_score)
        .bind("soak_driver")
        .bind(now)
        .execute(&mut *tx)
        .await.is_err() {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }

    if sqlx::query("INSERT INTO aether_heads (id, repo_id, sequence, snapshot_id, promotion_id, created_at, is_canonical) VALUES (?, ?, ?, ?, ?, ?, ?)")
        .bind(&head_id)
        .bind(&id)
        .bind(new_seq)
        .bind(&snapshot_id)
        .bind(&prom_id)
        .bind(now)
        .bind(true)
        .execute(&mut *tx)
        .await.is_err() {
            let _ = std::fs::remove_dir_all(&canonical_path);
            return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        }

    let cas_result = match sqlx::query("UPDATE aether_repositories SET head_id = ?, head_sequence = ?, updated_at = ? WHERE id = ? AND (head_id = ? OR (? IS NULL AND head_id IS NULL)) AND head_sequence = ?")
        .bind(&head_id)
        .bind(new_seq)
        .bind(now)
        .bind(&id)
        .bind(&old_head_id)
        .bind(&old_head_id)
        .bind(old_seq)
        .execute(&mut *tx)
        .await {
            Ok(r) => r,
            Err(_) => {
                orchestrator.cleanup_snapshot(&repo.id, &snapshot_id);
                return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
            }
        };

    if cas_result.rows_affected() == 0 {
        orchestrator.cleanup_snapshot(&repo.id, &snapshot_id);
        return Err(axum::http::StatusCode::CONFLICT);
    }

    if tx.commit().await.is_err() {
        orchestrator.cleanup_snapshot(&repo.id, &snapshot_id);
        return Err(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
    }

    let mut promotions: std::collections::HashMap<String, services::trust_service::AgentMetricDelta> =
        std::collections::HashMap::new();
    for agent_id in &included_agent_ids {
        let entry = promotions.entry(agent_id.clone()).or_default();
        entry.promoted_count += 1;
        entry.trust_alpha_delta += 1.0;
    }

    let trust_service = state.trust_service.clone();
    tokio::spawn(async move {
        for (agent_id, delta) in promotions {
            if let Err(e) = trust_service.record_metric_deltas(&agent_id, "global", &delta).await {
                tracing::error!("promotion_trust_upsert_failure: agent={} err={}", agent_id, e);
            }
        }
    });

    Ok(Json(PromotionResponse {
        promotion_id: prom_id,
        success: true,
        new_head_id: head_id,
        new_head_sequence: new_seq,
        included_proposals: included,
        excluded_proposals: excluded,
        evidence: api_evidence,
        final_score,
        message: "Promotion successful.".to_string(),
    }))
}
#[cfg(feature = "db-sqlite")]
async fn list_promotions(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
) -> Result<Json<PromotionListResponse>, axum::http::StatusCode> {
    let promotions = sqlx::query_as::<_, models::entities::Promotion>(
        "SELECT * FROM aether_promotions WHERE repo_id = ? ORDER BY created_at DESC LIMIT 100",
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await
    .map_err(|e| {
        tracing::error!("Failed to fetch promotions: {}", e);
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let total = promotions.len() as i64;
    Ok(Json(PromotionListResponse { promotions, total }))
}

#[cfg(feature = "db-sqlite")]
fn generate_id(prefix: &str) -> String {
    format!(
        "{}_{}",
        prefix,
        uuid::Uuid::new_v4()
            .simple()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    )
}

#[cfg(feature = "db-sqlite")]
fn is_sqlite_locked_error(error: &sqlx::Error) -> bool {
    match error {
        sqlx::Error::Database(db_error) => {
            db_error.code().as_deref() == Some("5")
                || db_error
                    .message()
                    .to_ascii_lowercase()
                    .contains("database is locked")
        }
        _ => false,
    }
}

#[cfg(feature = "db-sqlite")]
fn map_commit_db_err(error: sqlx::Error) -> axum::http::StatusCode {
    tracing::error!("commit_promotion db error: {:?}", error);
    if is_sqlite_locked_error(&error) {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    } else {
        axum::http::StatusCode::INTERNAL_SERVER_ERROR
    }
}

#[cfg(feature = "db-sqlite")]
async fn commit_promotion(
    Path(id): Path<String>,
    State(state): State<Arc<AppState>>,
    Json(payload): Json<models::entities::CommitPromotionRequest>,
) -> Result<Json<PromotionResponse>, axum::http::StatusCode> {
    let now = chrono::Utc::now();
    let mut tx = state.pool.begin().await.map_err(map_commit_db_err)?;

    let repo: Repository = sqlx::query_as("SELECT * FROM aether_repositories WHERE id = ?")
        .bind(&id)
        .fetch_one(&mut *tx)
        .await
        .map_err(|_| axum::http::StatusCode::NOT_FOUND)?;

    let mut included_agent_ids = Vec::new();
    let mut actual_included = Vec::new();

    // Revalidate and update included proposals
    for prop_id in &payload.included_proposals {
        let proposal = sqlx::query_as::<_, models::entities::Proposal>(
            "SELECT * FROM aether_proposals WHERE id = ? AND repo_id = ?",
        )
        .bind(prop_id)
        .bind(&id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(map_commit_db_err)?;

        if let Some(p) = proposal {
            if p.status != models::entities::ProposalStatus::Pending
                && p.status != models::entities::ProposalStatus::Evaluating
            {
                return Err(axum::http::StatusCode::CONFLICT);
            }
            included_agent_ids.push(p.agent_id.clone());
            actual_included.push(p.id.clone());

            sqlx::query(
                "UPDATE aether_proposals SET status = 'PROMOTED', promoted_at = ? WHERE id = ?",
            )
            .bind(now)
            .bind(&p.id)
            .execute(&mut *tx)
            .await
            .map_err(map_commit_db_err)?;

            sqlx::query("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, created_at) VALUES (?, ?, ?, ?, ?)")
                .bind(generate_id("evt"))
                .bind(&id)
                .bind(&p.id)
                .bind("STATUS_PROMOTED")
                .bind(now)
                .execute(&mut *tx)
                .await
                .map_err(map_commit_db_err)?;
        } else {
            return Err(axum::http::StatusCode::CONFLICT);
        }
    }

    // Insert Promotion
    let evidence_str =
        serde_json::to_string(&payload.evidence).unwrap_or_else(|_| "{}".to_string());

    let to_head_id = generate_id("head");
    let promotion_included = serde_json::to_string(&actual_included).unwrap_or_default();
    let promotion_excluded = serde_json::to_string(&payload.excluded_proposals).unwrap_or_default();

    sqlx::query("INSERT INTO aether_promotions (id, repo_id, from_head_id, from_head_sequence, to_head_id, to_head_sequence, included_proposals, excluded_proposals, evidence, policy_score, confidence_score, final_score, triggered_by, decision_rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        .bind(&payload.promotion_id)
        .bind(&id)
        .bind(&repo.head_id)
        .bind(repo.head_sequence)
        .bind(&to_head_id)
        .bind(repo.head_sequence + 1)
        .bind(&promotion_included)
        .bind(&promotion_excluded)
        .bind(&evidence_str)
        .bind(payload.policy_score)
        .bind(payload.confidence_score)
        .bind(payload.final_score)
        .bind(&payload.triggered_by)
        .bind(Some("External commit promotion".to_string()))
        .bind(now)
        .execute(&mut *tx)
        .await
        .map_err(map_commit_db_err)?;

    // Insert operation node
    if let Some(op) = &payload.operation_node {
        sqlx::query("INSERT INTO aether_operation_graph_nodes (id, repo_id, promotion_id, parent_node_id, parent_head_id, parent_head_sequence, to_head_id, to_head_sequence, snapshot_id, operation_type, node_hash, payload_sha256, parent_ref, replay_from_hash, replay_to_hash, replay_gate_vector, included_proposals, excluded_proposals, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
            .bind(&op.id)
            .bind(&id)
            .bind(&payload.promotion_id)
            .bind(&op.parent_node_id)
            .bind(&repo.head_id)
            .bind(repo.head_sequence)
            .bind(generate_id("head")) // Placeholder, will update
            .bind(repo.head_sequence + 1)
            .bind(&payload.snapshot_id)
            .bind(&op.operation_type)
            .bind(&op.node_hash)
            .bind(&op.payload_sha256)
            .bind(&op.parent_ref)
            .bind(&op.replay_from_hash)
            .bind(&op.replay_to_hash)
            .bind(&op.replay_gate_vector)
            .bind(serde_json::to_string(&actual_included).unwrap_or_default())
            .bind(serde_json::to_string(&payload.excluded_proposals).unwrap_or_default())
            .bind(now)
            .execute(&mut *tx)
            .await
            .map_err(map_commit_db_err)?;
    }

    // Conflict resolutions
    for res in &payload.conflict_resolutions {
        sqlx::query("UPDATE aether_conflict_sets SET resolved = 1, active_choice_id = ?, resolved_at = ? WHERE id = ?")
            .bind(&res.active_choice_id)
            .bind(now)
            .bind(&res.conflict_set_id)
            .execute(&mut *tx)
            .await
            .map_err(map_commit_db_err)?;

        for losing_id in &res.losing_proposal_ids {
            sqlx::query("UPDATE aether_proposals SET status = 'SUPERSEDED' WHERE id = ?")
                .bind(losing_id)
                .execute(&mut *tx)
                .await
                .map_err(map_commit_db_err)?;

            let reason = format!("Superseded by {}", res.active_choice_id);
            sqlx::query("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, event_reason, created_at) VALUES (?, ?, ?, ?, ?, ?)")
                .bind(generate_id("evt"))
                .bind(&id)
                .bind(losing_id)
                .bind("STATUS_SUPERSEDED")
                .bind(&reason)
                .bind(now)
                .execute(&mut *tx)
                .await
                .map_err(map_commit_db_err)?;
        }
    }

    // Update heads
    sqlx::query("UPDATE aether_heads SET is_canonical = 0 WHERE repo_id = ? AND is_canonical = 1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(map_commit_db_err)?;

    let head_id = to_head_id.clone();
    let head_sequence = repo.head_sequence + 1;
    let head_is_canonical = true;
    let _head_authentic_hash = "".to_string();
    let _head_parent_head_id = Some(repo.head_id.clone());
    let head_promotion_id = Some(payload.promotion_id.clone());

    sqlx::query("INSERT INTO aether_heads (id, repo_id, snapshot_id, sequence, is_canonical, promotion_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
        .bind(&head_id)
        .bind(&id)
        .bind(&payload.snapshot_id)
        .bind(head_sequence)
        .bind(head_is_canonical)
        .bind(&head_promotion_id)
        .bind(now)
        .execute(&mut *tx)
        .await
        .map_err(map_commit_db_err)?;

    // CAS Update repository
    let rows_affected = sqlx::query("UPDATE aether_repositories SET head_id = ?, head_sequence = ?, updated_at = ? WHERE id = ? AND head_id IS ? AND head_sequence = ?")
        .bind(&head_id)
        .bind(head_sequence)
        .bind(now)
        .bind(&id)
        .bind(&repo.head_id)
        .bind(repo.head_sequence)
        .execute(&mut *tx)
        .await
        .map_err(map_commit_db_err)?
        .rows_affected();

    if rows_affected == 0 {
        return Err(axum::http::StatusCode::CONFLICT);
    }

    if let Some(op) = &payload.operation_node {
        sqlx::query("UPDATE aether_operation_graph_nodes SET to_head_id = ? WHERE id = ?")
            .bind(&head_id)
            .bind(&op.id)
            .execute(&mut *tx)
            .await
            .map_err(map_commit_db_err)?;
    }

    tx.commit().await.map_err(map_commit_db_err)?;

    let mut deltas: std::collections::HashMap<String, services::trust_service::AgentMetricDelta> =
        std::collections::HashMap::new();
    for agent_id in &included_agent_ids {
        let entry = deltas.entry(agent_id.clone()).or_default();
        entry.promoted_count += 1;
        entry.trust_alpha_delta += 1.0;
    }

    let trust_service = state.trust_service.clone();
    tokio::spawn(async move {
        for (agent_id, delta) in deltas {
            if let Err(e) = trust_service.record_metric_deltas(&agent_id, "global", &delta).await {
                tracing::error!("commit_promotion_trust_upsert_failure: agent={} err={}", agent_id, e);
            }
        }
    });

    Ok(Json(PromotionResponse {
        promotion_id: payload.promotion_id.clone(),
        success: true,
        new_head_id: head_id,
        new_head_sequence: head_sequence,
        included_proposals: payload.included_proposals,
        excluded_proposals: payload.excluded_proposals,
        evidence: payload.evidence,
        final_score: payload.final_score,
        message: "External promotion committed successfully".to_string(),
    }))
}

#[cfg(test)]
mod security_tests {
    use super::*;
    use axum::http::{HeaderMap, HeaderValue, Method};

    #[test]
    fn test_resolve_and_validate_bind_host_enforces_loopback_default() {
        assert_eq!(
            resolve_and_validate_bind_host(None, None, None).unwrap(),
            "127.0.0.1"
        );
        assert_eq!(
            resolve_and_validate_bind_host(Some("localhost"), None, None).unwrap(),
            "localhost"
        );
        assert_eq!(
            resolve_and_validate_bind_host(Some("::1"), None, None).unwrap(),
            "::1"
        );

        let err_default = resolve_and_validate_bind_host(Some("0.0.0.0"), None, None).unwrap_err();
        assert!(err_default.contains("Refusing to bind Aether-RS server to non-loopback host"));

        let err_no_token =
            resolve_and_validate_bind_host(Some("0.0.0.0"), Some("true"), None).unwrap_err();
        assert!(err_no_token.contains("without AETHER_API_TOKEN set"));

        let ok_both = resolve_and_validate_bind_host(
            Some("0.0.0.0"),
            Some("true"),
            Some("release-secret-token"),
        )
        .unwrap();
        assert_eq!(ok_both, "0.0.0.0");
    }

    #[test]
    fn test_is_authorized_request_enforces_api_token() {
        let empty_headers = HeaderMap::new();

        // When no token is configured, requests are allowed (for local loopback operation)
        assert!(is_authorized_request(
            &Method::GET,
            "/v1/repos",
            &empty_headers,
            None
        ));

        // When token is configured, / and /v1/health remain accessible
        assert!(is_authorized_request(
            &Method::GET,
            "/",
            &empty_headers,
            Some("secret-123")
        ));
        assert!(is_authorized_request(
            &Method::GET,
            "/v1/health",
            &empty_headers,
            Some("secret-123")
        ));

        // Protected route without token -> rejected
        assert!(!is_authorized_request(
            &Method::GET,
            "/v1/repos",
            &empty_headers,
            Some("secret-123")
        ));

        // Protected route with wrong Bearer token -> rejected
        let mut wrong_headers = HeaderMap::new();
        wrong_headers.insert(
            axum::http::header::AUTHORIZATION,
            HeaderValue::from_static("Bearer wrong-token"),
        );
        assert!(!is_authorized_request(
            &Method::GET,
            "/v1/repos",
            &wrong_headers,
            Some("secret-123")
        ));

        // Protected route with valid Bearer token -> allowed
        let mut bearer_headers = HeaderMap::new();
        bearer_headers.insert(
            axum::http::header::AUTHORIZATION,
            HeaderValue::from_static("Bearer secret-123"),
        );
        assert!(is_authorized_request(
            &Method::GET,
            "/v1/repos",
            &bearer_headers,
            Some("secret-123")
        ));

        // Protected route with valid X-Aether-Token header -> allowed
        let mut custom_headers = HeaderMap::new();
        custom_headers.insert("x-aether-token", HeaderValue::from_static("secret-123"));
        assert!(is_authorized_request(
            &Method::POST,
            "/v1/repos",
            &custom_headers,
            Some("secret-123")
        ));
    }
}

