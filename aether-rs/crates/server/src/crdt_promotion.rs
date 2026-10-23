use std::sync::Arc;
use axum::{extract::{Path, State}, Json};
use sha2::{Digest, Sha256};
use sqlx::Postgres;
use uuid::Uuid;

use models::entities::{
    PromotionRequest, PromotionResponse, Repository,
};
use crate::db_postgres::{PgAppState, PoolWaiterGuard, PROMO_METRICS, REPO_CACHE, RepoCache};

fn generate_id(prefix: &str) -> String {
    format!("{}_{}", prefix, Uuid::new_v4().simple().to_string().chars().take(12).collect::<String>())
}

pub async fn load_head_frontier(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    repo_id: &str,
) -> Result<crdt::head_crdt::HeadFrontier, sqlx::Error> {
    let row: Option<(String,)> = sqlx::query_as("SELECT frontier_json FROM aether_head_frontiers WHERE repo_id = $1")
        .bind(repo_id)
        .fetch_optional(&mut **tx)
        .await?;

    if let Some((json_str,)) = row {
        let frontier: crdt::head_crdt::HeadFrontier = serde_json::from_str(&json_str)
            .map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
        Ok(frontier)
    } else {
        Ok(crdt::head_crdt::HeadFrontier::new())
    }
}

pub async fn store_head_frontier(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    repo_id: &str,
    frontier: &crdt::head_crdt::HeadFrontier,
) -> Result<(), sqlx::Error> {
    let json_str = serde_json::to_string(frontier)
        .map_err(|e| sqlx::Error::Decode(Box::new(e)))?;

    sqlx::query("INSERT INTO aether_head_frontiers (repo_id, frontier_json, updated_at) \
                 VALUES ($1, $2, NOW()) \
                 ON CONFLICT (repo_id) \
                 DO UPDATE SET frontier_json = EXCLUDED.frontier_json, updated_at = NOW()")
        .bind(repo_id)
        .bind(json_str)
        .execute(&mut **tx)
        .await?;

    Ok(())
}

pub async fn resolve_head_context(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    head_id: &str,
) -> crdt::causality::VersionVector {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT evidence FROM aether_promotions WHERE to_head_id = $1"
    )
    .bind(head_id)
    .fetch_optional(&mut **tx)
    .await
    .unwrap_or(None);

    if let Some((evidence_str,)) = row {
        if let Ok(evidence_map) = serde_json::from_str::<std::collections::HashMap<String, String>>(&evidence_str) {
            if let Some(ctx_str) = evidence_map.get("crdt_context") {
                if let Ok(vv) = serde_json::from_str::<crdt::causality::VersionVector>(ctx_str) {
                    return vv;
                }
            }
        }
    }
    crdt::causality::VersionVector::new()
}

pub async fn resolve_head_dot(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    head_id: &str,
) -> Option<crdt::causality::Dot> {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT evidence FROM aether_promotions WHERE to_head_id = $1"
    )
    .bind(head_id)
    .fetch_optional(&mut **tx)
    .await
    .unwrap_or(None);

    if let Some((evidence_str,)) = row {
        if let Ok(evidence_map) = serde_json::from_str::<std::collections::HashMap<String, String>>(&evidence_str) {
            if let Some(dot_str) = evidence_map.get("crdt_dot") {
                if let Ok(dot) = serde_json::from_str::<crdt::causality::Dot>(dot_str) {
                    return Some(dot);
                }
            }
        }
    }
    None
}

pub async fn request_promotion_crdt_inner(
    path: Path<String>,
    state: State<Arc<PgAppState>>,
    json: Json<PromotionRequest>,
) -> Result<Json<PromotionResponse>, (axum::http::StatusCode, Json<PromotionResponse>)> {
    crdt::rga::RGA_ALIGNMENT_TIME_NS.scope(
        std::sync::atomic::AtomicU64::new(0),
        request_promotion_crdt_inner_scoped(path, state, json)
    ).await
}

async fn request_promotion_crdt_inner_scoped(
    Path(id): Path<String>,
    State(state): State<Arc<PgAppState>>,
    Json(payload): Json<PromotionRequest>,
) -> Result<Json<PromotionResponse>, (axum::http::StatusCode, Json<PromotionResponse>)> {
    use std::sync::atomic::Ordering;
    PROMO_METRICS.count.fetch_add(1, Ordering::Relaxed);

    let t_total_start = std::time::Instant::now();
    let prom_id = generate_id("prom");
    let now = chrono::Utc::now();
    let mut timings = std::collections::HashMap::new();

    // 1. Initial Resolution & Trust Math (Outside the Lock)
    let t_db_read_pre_orch_start = std::time::Instant::now();
    let t_db_read_repo_start = std::time::Instant::now();
    let repo_check = sqlx::query_as::<Postgres, Repository>("SELECT * FROM aether_repositories WHERE id = $1")
        .bind(&id)
        .fetch_optional(&state.pool)
        .await
        .map_err(|_| (
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            Json(PromotionResponse {
                promotion_id: prom_id.clone(),
                success: false,
                new_head_id: "".to_string(),
                new_head_sequence: 0,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence: std::collections::HashMap::new(),
                final_score: 0.0,
                message: "Internal error looking up repository".to_string(),
            })
        ))?
        .ok_or((
            axum::http::StatusCode::NOT_FOUND,
            Json(PromotionResponse {
                promotion_id: prom_id.clone(),
                success: false,
                new_head_id: "".to_string(),
                new_head_sequence: 0,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence: std::collections::HashMap::new(),
                final_score: 0.0,
                message: "Repository not found".to_string(),
            })
        ))?;
    let t_db_read_repo = t_db_read_repo_start.elapsed().as_secs_f64() * 1000.0;

    let t_db_read_proposals_start = std::time::Instant::now();
    let mut initial_proposals = Vec::new();
    for prop_id in &payload.proposal_ids {
        if let Some(p) = sqlx::query_as::<Postgres, models::entities::Proposal>(
            "SELECT * FROM aether_proposals WHERE id = $1 AND repo_id = $2 AND status = 'PENDING'"
        ).bind(prop_id).bind(&id).fetch_optional(&state.pool).await.unwrap_or(None) {
            initial_proposals.push(p);
        }
    }
    let t_db_read_proposals = t_db_read_proposals_start.elapsed().as_secs_f64() * 1000.0;
    let t_db_read_pre_orch = t_db_read_pre_orch_start.elapsed().as_secs_f64() * 1000.0;

    if initial_proposals.is_empty() {
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        timings.insert("t_db_read_pre_orch".to_string(), t_db_read_pre_orch);
        timings.insert("t_db_read_repo".to_string(), t_db_read_repo);
        timings.insert("t_db_read_proposals".to_string(), t_db_read_proposals);
        timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());

        return Err((
            axum::http::StatusCode::CONFLICT,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: repo_check.head_id.unwrap_or_default(),
                new_head_sequence: repo_check.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: 0.0,
                message: "No pending proposals found to promote.".to_string(),
            })
        ));
    }

    let trust_service = services::trust_service::TrustService::new(state.pool.clone());
    let is_batch = initial_proposals.len() > 1;
    let lead_proposal = &initial_proposals[0];
    
    let combined_trust = if is_batch {
        let avg_confidence = initial_proposals.iter().map(|p| p.confidence_score).sum::<f64>() / (initial_proposals.len() as f64);
        trust_service.get_combined_agent_score_readonly(&state.pool, "batch", avg_confidence, "global").await
            .unwrap_or((avg_confidence, std::collections::HashMap::new()))
    } else {
        trust_service.get_combined_agent_score_readonly(&state.pool, &lead_proposal.agent_id, lead_proposal.confidence_score, "global").await
            .unwrap_or((lead_proposal.confidence_score, std::collections::HashMap::new()))
    };
    let (trust_score, trust_timings) = combined_trust;

    // ══════════════════════════════════════════════════════════════════════════
    // PHASE A: SPECULATIVE PRE-LOCK (Read & Merge outside transaction)
    // ══════════════════════════════════════════════════════════════════════════
    let crdt_mode = std::env::var("AETHER_CRDT_MODE").unwrap_or_default();
    let run_frontier_logic = crdt_mode == "frontier" || crdt_mode == "token";

    let speculative_frontier = if run_frontier_logic {
        load_head_frontier_pool(&state.pool, &id).await.unwrap_or_else(|_| crdt::head_crdt::HeadFrontier::new())
    } else {
        crdt::head_crdt::HeadFrontier::new()
    };
    let speculative_frontier_json = serde_json::to_string(&speculative_frontier).unwrap_or_default();

    let mut parent_context = crdt::causality::VersionVector::new();
    let mut new_dot_early = None;
    let mut meet_frontier_clock = None;

    if run_frontier_logic {
        if let Some(ref base_head_id) = lead_proposal.base_head_id {
            parent_context = resolve_head_context_pool(&state.pool, base_head_id).await;
            if let Some(parent_dot) = resolve_head_dot_pool(&state.pool, base_head_id).await {
                parent_context.merge(&{
                    let mut vv = crdt::causality::VersionVector::new();
                    vv.versions.insert(parent_dot.node_id.clone(), parent_dot.sequence);
                    vv
                });
            }
        }

        let actor = &lead_proposal.agent_id;
        let active_ctx = speculative_frontier.causal_context();
        let next_seq = active_ctx.versions.get(actor).copied().unwrap_or(0) + 1;
        let nd = crdt::causality::Dot::new(actor, next_seq);
        new_dot_early = Some(nd.clone());

        let mut post_append_frontier = speculative_frontier.clone();
        let new_event = crdt::head_crdt::HeadEvent {
            snapshot_id: "".to_string(),
            dot: nd.clone(),
            context: parent_context.clone(),
            agent_id: lead_proposal.agent_id.clone(),
            frozen_trust: trust_score,
        };
        post_append_frontier.append(new_event);
        meet_frontier_clock = Some(post_append_frontier.meet_frontier());
    }

    let t_db_read_base_snapshot_start = std::time::Instant::now();
    let base_snapshot_res = if let Some(h_id) = &repo_check.head_id {
        sqlx::query_as::<Postgres, (String,)>("SELECT snapshot_id FROM aether_heads WHERE id = $1")
            .bind(h_id)
            .fetch_optional(&state.pool)
            .await
    } else {
        Ok(None)
    };
    let t_db_read_base_snapshot = t_db_read_base_snapshot_start.elapsed().as_secs_f64() * 1000.0;

    let base_snapshot_id = match base_snapshot_res {
        Ok(Some(row)) => row.0,
        Ok(None) => "".to_string(),
        Err(_) => {
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            timings.insert("t_db_read_pre_orch".to_string(), t_db_read_pre_orch);
            timings.insert("t_db_read_repo".to_string(), t_db_read_repo);
            timings.insert("t_db_read_proposals".to_string(), t_db_read_proposals);
            timings.insert("t_db_read_base_snapshot".to_string(), t_db_read_base_snapshot);
            timings.insert("t_total_inner".to_string(), t_total_inner);
            
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
            return Err((
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: repo_check.head_id.clone().unwrap_or_default(),
                    new_head_sequence: repo_check.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: 0.0,
                    message: "Internal error reading base snapshot".to_string(),
                })
            ));
        }
    };

    let mut base_snapshot_ids: Vec<String> = if run_frontier_logic {
        let mut snaps: Vec<String> = speculative_frontier.events.values().map(|e| e.snapshot_id.clone()).collect();
        snaps.sort();
        snaps.dedup();
        snaps
    } else {
        Vec::new()
    };
    if base_snapshot_ids.is_empty() && !base_snapshot_id.is_empty() {
        base_snapshot_ids.push(base_snapshot_id.clone());
    }

    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let storage_base = std::path::PathBuf::from(format!("{}/.aether/aether_storage", home));
    let orchestrator = services::promotion_orchestrator::PromotionOrchestrator::new(storage_base.clone());

    let orch_res = match orchestrator.orchestrate_promotion_bounded(
        &repo_check,
        &initial_proposals,
        &base_snapshot_ids,
        trust_score,
        trust_timings,
        new_dot_early.as_ref(),
        meet_frontier_clock.as_ref(),
    ).await {
        Ok(res) => res,
        Err(services::promotion_orchestrator::OrchestrationError::BadRequest(msg)) => {
            // Update proposal statuses to REJECTED in the database
            for prop_id in &payload.proposal_ids {
                let res: Option<(i64,)> = sqlx::query_as::<Postgres, (i64,)>("UPDATE aether_proposals SET status = 'REJECTED' WHERE id = $1 RETURNING base_head_sequence")
                    .bind(prop_id).fetch_optional(&state.pool).await.unwrap_or(None);
                if let Some((seq,)) = res {
                    crate::db_postgres::decrement_backlog(&id, seq);
                }
            }

            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            timings.insert("t_db_read_pre_orch".to_string(), t_db_read_pre_orch);
            timings.insert("t_db_read_repo".to_string(), t_db_read_repo);
            timings.insert("t_db_read_proposals".to_string(), t_db_read_proposals);
            timings.insert("t_db_read_base_snapshot".to_string(), t_db_read_base_snapshot);
            timings.insert("t_total_inner".to_string(), t_total_inner);
            
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
            
            let client_msg = if msg.contains("No valid changes to promote") {
                "Promotion rejected: required gates failed.".to_string()
            } else {
                msg
            };

            return Ok(Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: repo_check.head_id.clone().unwrap_or_default(),
                new_head_sequence: repo_check.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: 0.0,
                message: client_msg,
            }));
        }
        Err(_) => {
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            timings.insert("t_db_read_pre_orch".to_string(), t_db_read_pre_orch);
            timings.insert("t_db_read_repo".to_string(), t_db_read_repo);
            timings.insert("t_db_read_proposals".to_string(), t_db_read_proposals);
            timings.insert("t_db_read_base_snapshot".to_string(), t_db_read_base_snapshot);
            timings.insert("t_total_inner".to_string(), t_total_inner);
            
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
            return Err((
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: repo_check.head_id.clone().unwrap_or_default(),
                    new_head_sequence: repo_check.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: 0.0,
                    message: "Promotion orchestration failed".to_string(),
                })
            ));
        }
    };

    let candidate_proposals = orch_res.included_proposals.clone();
    let included: Vec<String> = candidate_proposals.iter().map(|p| p.id.clone()).collect();
    let excluded: Vec<String> = payload.proposal_ids.iter().filter(|id| !included.contains(id)).cloned().collect();

    let mut timings = orch_res.timings.clone();
    timings.insert("t_db_read_pre_orch".to_string(), t_db_read_pre_orch);
    timings.insert("t_db_read_repo".to_string(), t_db_read_repo);
    timings.insert("t_db_read_proposals".to_string(), t_db_read_proposals);
    timings.insert("t_db_read_base_snapshot".to_string(), t_db_read_base_snapshot);
    timings.insert("req_proposal_count".to_string(), payload.proposal_ids.len() as f64);

    // ══════════════════════════════════════════════════════════════════════════
    // PHASE B: TRANSACTION COMMIT (Lock & Strict CAS Validation)
    // ══════════════════════════════════════════════════════════════════════════
    let t_db_tx_start = std::time::Instant::now();
    let mut tx: sqlx::Transaction<'_, Postgres> = {
        let _guard = PoolWaiterGuard::new(state.waiter_count.clone());
        state.pool.begin().await.map_err(|_| {
            let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
            (
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(PromotionResponse {
                    promotion_id: prom_id.clone(),
                    success: false,
                    new_head_id: repo_check.head_id.clone().unwrap_or_default(),
                    new_head_sequence: repo_check.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence: std::collections::HashMap::new(),
                    final_score: 0.0,
                    message: "Failed to begin database transaction".to_string(),
                })
            )
        })?
    };
    let t_db_tx_time = t_db_tx_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_tx_start".to_string(), t_db_tx_time);

    let t_lock_wait_start = std::time::Instant::now();
    let mut hasher = Sha256::new();
    hasher.update(id.as_bytes());
    let result = hasher.finalize();
    let mut key_bytes = [0u8; 8];
    key_bytes.copy_from_slice(&result[0..8]);
    let lock_key = i64::from_be_bytes(key_bytes);

    let lock_query_res = sqlx::query::<Postgres>("SELECT pg_advisory_xact_lock($1::bigint)")
        .bind(lock_key)
        .execute(&mut *tx)
        .await;
    
    if lock_query_res.is_err() {
        let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
        let _ = tx.rollback().await;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
        return Err((
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: repo_check.head_id.unwrap_or_default(),
                new_head_sequence: repo_check.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: 0.0,
                message: "Failed to acquire pg_advisory_xact_lock".to_string(),
            })
        ));
    }
    PROMO_METRICS.t_promo_advisory_lock.fetch_add(t_lock_wait_start.elapsed().as_nanos() as u64, Ordering::Relaxed);
    
    let t_lock_wait = t_lock_wait_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_lock_wait".to_string(), t_lock_wait);
    let t_lock_hold_start = std::time::Instant::now();

    // 1. Re-read Latest Canonical Head and proposals under Lock
    let t_promo_val_start = std::time::Instant::now();
    let current_repo_res = sqlx::query_as::<Postgres, Repository>("SELECT * FROM aether_repositories WHERE id = $1")
        .bind(&id)
        .fetch_optional(&mut *tx)
        .await;

    let current_repo = match current_repo_res {
        Ok(Some(r)) => r,
        _ => {
            let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
            let _ = tx.rollback().await;
            let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            timings.insert("t_lock_hold".to_string(), t_lock_hold);
            timings.insert("t_total_inner".to_string(), t_total_inner);
            
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
            return Err((
                axum::http::StatusCode::NOT_FOUND,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: repo_check.head_id.unwrap_or_default(),
                    new_head_sequence: repo_check.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: 0.0,
                    message: "Repository not found under transaction lock".to_string(),
                })
            ));
        }
    };

    let t_db_revalidate_start = std::time::Instant::now();
    let mut current_proposals = Vec::new();
    for prop_id in &payload.proposal_ids {
        if let Some(p) = sqlx::query_as::<Postgres, models::entities::Proposal>(
            "SELECT * FROM aether_proposals WHERE id = $1 AND repo_id = $2"
        ).bind(prop_id).bind(&id).fetch_optional(&mut *tx).await.unwrap_or(None) {
            current_proposals.push(p);
        }
    }
    let t_db_revalidate = t_db_revalidate_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_revalidate".to_string(), t_db_revalidate);

    let current_frontier = if run_frontier_logic {
        load_head_frontier(&mut tx, &id).await.unwrap_or_else(|_| crdt::head_crdt::HeadFrontier::new())
    } else {
        crdt::head_crdt::HeadFrontier::new()
    };
    let current_frontier_json = serde_json::to_string(&current_frontier).unwrap_or_default();

    // 2. Strict CAS Validation
    let all_pending = all_proposals_still_pending(&current_proposals, &payload.proposal_ids);
    
    if current_frontier_json != speculative_frontier_json ||
       current_repo.head_id != repo_check.head_id ||
       current_repo.head_sequence != repo_check.head_sequence ||
       current_repo.gate_config != repo_check.gate_config ||
       !all_pending {
        let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
        let _ = tx.rollback().await;
        
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        timings.insert("t_lock_hold".to_string(), t_lock_hold);
        timings.insert("t_total_inner".to_string(), t_total_inner);

        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
        
        PROMO_METRICS.t_promo_validate_proposals.fetch_add(t_promo_val_start.elapsed().as_nanos() as u64, Ordering::Relaxed);
        return Err((
            axum::http::StatusCode::CONFLICT,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: current_repo.head_id.unwrap_or_default(),
                new_head_sequence: current_repo.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: 0.0,
                message: "CAS validation failed: Repository basis or proposals changed concurrently".to_string(),
            })
        ));
    }

    PROMO_METRICS.t_promo_validate_proposals.fetch_add(t_promo_val_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    // 3. Early Rejection Check (now safely under lock + after CAS verification)
    if orch_res.policy_score == 0.0 {
        let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
        for prop_id in &included {
            let res: Option<(i64,)> = sqlx::query_as::<Postgres, (i64,)>("UPDATE aether_proposals SET status = 'REJECTED' WHERE id = $1 RETURNING base_head_sequence")
                .bind(prop_id).fetch_optional(&mut *tx).await.unwrap_or(None);
            if let Some((seq,)) = res {
                crate::db_postgres::decrement_backlog(&id, seq);
            }
        }
        
        let t_db_commit_start = std::time::Instant::now();
        if tx.commit().await.is_err() {
            let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            
            let mut final_timings = timings.clone();
            final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
            final_timings.insert("t_total_inner".to_string(), t_total_inner);
            
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
            return Err((
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: current_repo.head_id.clone().unwrap_or_default(),
                    new_head_sequence: current_repo.head_sequence,
                    included_proposals: Vec::new(),
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: orch_res.final_score,
                    message: "Failed to commit rejected status updates".to_string(),
                })
            ));
        }
        let t_db_commit = t_db_commit_start.elapsed().as_secs_f64() * 1000.0;
        timings.insert("t_db_commit".to_string(), t_db_commit);
        
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        timings.insert("t_lock_hold".to_string(), t_lock_hold);
        timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut reject_evidence = std::collections::HashMap::new();
        reject_evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());
        
        return Ok(Json(PromotionResponse {
            promotion_id: prom_id,
            success: false,
            new_head_id: current_repo.head_id.unwrap_or_default(),
            new_head_sequence: current_repo.head_sequence,
            included_proposals: Vec::new(),
            excluded_proposals: payload.proposal_ids.clone(),
            evidence: reject_evidence,
            final_score: orch_res.final_score,
            message: "Promotion rejected: required gates failed.".to_string(),
        }));
    }

    // 4. Persist Canonical Snapshot (Move temp workspace to canonical path)
    let snapshot_id = orch_res.snapshot_id.clone();
    let canonical_path = storage_base.join(&id).join("snapshots").join(&snapshot_id);
    if let Some(parent) = canonical_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    
    let t_fs_rename_start = std::time::Instant::now();
    if std::fs::rename(&orch_res.temp_workspace, &canonical_path).is_err() {
        let _ = std::fs::remove_dir_all(&orch_res.temp_workspace);
        let _ = tx.rollback().await;
        let t_fs_rename = t_fs_rename_start.elapsed().as_secs_f64() * 1000.0;
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        
        let mut final_timings = timings.clone();
        final_timings.insert("t_fs_rename".to_string(), t_fs_rename);
        final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
        final_timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
        return Err((
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: current_repo.head_id.clone().unwrap_or_default(),
                new_head_sequence: current_repo.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: orch_res.final_score,
                message: "Failed to rename temp workspace to snapshot path".to_string(),
            })
        ));
    }
    let t_fs_rename = t_fs_rename_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_fs_rename".to_string(), t_fs_rename);

    let storage_path_str = canonical_path.to_string_lossy().to_string();
    let included_json = serde_json::to_string(&included).unwrap_or_else(|_| "[]".to_string());
    let new_seq = current_repo.head_sequence + 1;

    let t_db_insert_snapshot_gates_start = std::time::Instant::now();
    let snapshot_insert_res = sqlx::query("INSERT INTO aether_snapshots (id, repo_id, head_sequence, included_proposals, content_hash, storage_path, created_at, file_count, total_bytes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)")
        .bind(&snapshot_id)
        .bind(&id)
        .bind(new_seq)
        .bind(&included_json)
        .bind(&orch_res.state_hash)
        .bind(&storage_path_str)
        .bind(now)
        .bind(orch_res.file_count as i64)
        .bind(orch_res.total_bytes as i64)
        .execute(&mut *tx)
        .await;

    if snapshot_insert_res.is_err() {
        let _ = std::fs::remove_dir_all(&canonical_path);
        let _ = tx.rollback().await;
        let t_db_insert_snapshot_gates = t_db_insert_snapshot_gates_start.elapsed().as_secs_f64() * 1000.0;
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        
        let mut final_timings = timings.clone();
        final_timings.insert("t_db_insert_snapshot_gates".to_string(), t_db_insert_snapshot_gates);
        final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
        final_timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
        return Err((
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: current_repo.head_id.clone().unwrap_or_default(),
                new_head_sequence: current_repo.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: orch_res.final_score,
                message: "Failed to insert snapshot metadata".to_string(),
            })
        ));
    }

    for g_res in orch_res.gate_results {
        sqlx::query("INSERT INTO aether_gate_results (id, repo_id, snapshot_id, proposal_id, gate_name, gate_command, status, exit_code, stdout, stderr, duration_ms, started_at, completed_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)")
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
            .await.unwrap();
    }
    let t_db_insert_snapshot_gates = t_db_insert_snapshot_gates_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_insert_snapshot_gates".to_string(), t_db_insert_snapshot_gates);
    PROMO_METRICS.t_promo_snapshot_head.fetch_add(t_db_insert_snapshot_gates_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    // 5. DB Writes (Statuses & New Head)
    let t_db_update_proposals_trust_start = std::time::Instant::now();
    let to_head_id = generate_id("head");
    for p in &candidate_proposals {
        sqlx::query::<Postgres>("UPDATE aether_proposals SET status = 'PROMOTED', promoted_at = $1 WHERE id = $2")
            .bind(now).bind(&p.id).execute(&mut *tx).await.unwrap();
    }
    for (prop_id, reason) in &orch_res.rejected_proposals {
        sqlx::query::<Postgres>("UPDATE aether_proposals SET status = 'REJECTED' WHERE id = $1")
            .bind(prop_id).execute(&mut *tx).await.unwrap();
        sqlx::query::<Postgres>("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, event_reason, created_at) VALUES ($1, $2, $3, $4, $5, $6)")
            .bind(generate_id("evt"))
            .bind(&id)
            .bind(prop_id)
            .bind("STATUS_REJECTED")
            .bind(reason)
            .bind(now)
            .execute(&mut *tx).await.unwrap();
    }
    for (prop_id, reason) in &orch_res.superseded_proposals {
        sqlx::query::<Postgres>("UPDATE aether_proposals SET status = 'SUPERSEDED' WHERE id = $1")
            .bind(prop_id).execute(&mut *tx).await.unwrap();
        sqlx::query::<Postgres>("INSERT INTO aether_system_metric_events (id, repo_id, proposal_id, event_type, event_reason, created_at) VALUES ($1, $2, $3, $4, $5, $6)")
            .bind(generate_id("evt"))
            .bind(&id)
            .bind(prop_id)
            .bind("STATUS_SUPERSEDED")
            .bind(reason)
            .bind(now)
            .execute(&mut *tx).await.unwrap();
    }
    let t_db_update_proposals_trust = t_db_update_proposals_trust_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_update_proposals_trust".to_string(), t_db_update_proposals_trust);
    PROMO_METRICS.t_promo_update_proposals.fetch_add(t_db_update_proposals_trust_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    sqlx::query::<Postgres>("UPDATE aether_heads SET is_canonical = false WHERE repo_id = $1 AND is_canonical = true")
        .bind(&id).execute(&mut *tx).await.unwrap();

    let mut canonical_head_id = to_head_id.clone();
    let mut new_dot = crdt::causality::Dot::new("system", 0);
    let mut frontier_width_val = None;

    if run_frontier_logic {
        let mut frontier = current_frontier;
        if let Some(nd) = new_dot_early {
            new_dot = nd;
        }

        let new_event = crdt::head_crdt::HeadEvent {
            snapshot_id: snapshot_id.clone(),
            dot: new_dot.clone(),
            context: parent_context.clone(),
            agent_id: lead_proposal.agent_id.clone(),
            frozen_trust: trust_score,
        };

        let max_width = std::env::var("AETHER_MAX_FRONTIER_WIDTH")
            .ok()
            .and_then(|val| val.parse::<usize>().ok())
            .unwrap_or(crdt::head_crdt::DEFAULT_MAX_FRONTIER_WIDTH);

        if let Err(e) = frontier.try_append_checked(new_event, max_width) {
            if let Err(rm_err) = std::fs::remove_dir_all(&canonical_path) {
                tracing::error!("Failed to remove canonical_path on 409 abort! path: {:?}, err: {:?}", canonical_path, rm_err);
            }
            let _ = tx.rollback().await;
            let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;

            let mut final_timings = timings.clone();
            final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
            final_timings.insert("t_total_inner".to_string(), t_total_inner);

            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
            evidence.insert("error_code".to_string(), "FRONTIER_AT_CAPACITY".to_string());

            let err_msg = match e {
                crdt::head_crdt::HeadFrontierError::FrontierWidthExceeded { current, max } => {
                    format!("FRONTIER_AT_CAPACITY: Width cap exceeded (current width: {}, cap: {}), force convergence required", current, max)
                }
                crdt::head_crdt::HeadFrontierError::SelfInvalidCausality(dot) => {
                    format!("Causality conflict: self-invalid causality detected for dot {:?}", dot)
                }
                crdt::head_crdt::HeadFrontierError::DuplicateDotMismatch(dot) => {
                    format!("Causality conflict: duplicate dot mismatch for dot {:?}", dot)
                }
            };
            tracing::error!("try_append_checked failed: {}", err_msg);

            return Err((
                axum::http::StatusCode::CONFLICT,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: current_repo.head_id.clone().unwrap_or_default(),
                    new_head_sequence: current_repo.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: orch_res.final_score,
                    message: err_msg,
                })
            ));
        }
        frontier_width_val = Some(frontier.events.len() as f64);

        if let Err(e) = store_head_frontier(&mut tx, &id, &frontier).await {
            tracing::error!("Failed to store head frontier: {:?}", e);
        }

        let projected_snapshot_id = frontier.arbitrate_view().unwrap_or_else(|| snapshot_id.clone());
        if projected_snapshot_id != snapshot_id {
            if let Ok(Some((h_id,))) = sqlx::query_as::<Postgres, (String,)>("SELECT id FROM aether_heads WHERE snapshot_id = $1")
                .bind(&projected_snapshot_id)
                .fetch_optional(&mut *tx)
                .await
            {
                canonical_head_id = h_id;
            }
        }
    }

    let excluded_json = serde_json::to_string(&excluded).unwrap_or_else(|_| "[]".to_string());
    let t_db_insert_promotions_heads_cas_start = std::time::Instant::now();
    let mut db_timings = timings.clone();
    
    let mut visible_tokens = 0;
    let mut tombstones = 0;
    let mut empty_non_root = 0;
    let mut operations = 0;

    for file_doc in orch_res.candidate_store.documents.values() {
        for element in file_doc.document.elements.values() {
            let is_root = element.id == crdt::rga::ElementId::root();
            if element.deleted {
                tombstones += 1;
            } else if !is_root && !element.value.is_empty() {
                visible_tokens += 1;
            } else if !is_root && element.value.is_empty() {
                empty_non_root += 1;
            }
        }
        operations += file_doc.document.operations.len();
    }

    let alignment_ns = crdt::rga::RGA_ALIGNMENT_TIME_NS.try_with(|cell| cell.load(std::sync::atomic::Ordering::SeqCst)).unwrap_or(0);
    let alignment_ms = (alignment_ns as f64) / 1_000_000.0;
    
    db_timings.insert("t_pool_acquire_ms".to_string(), t_db_tx_time);
    db_timings.insert("t_rga_alignment_ms".to_string(), alignment_ms);
    if let Some(w) = frontier_width_val {
        db_timings.insert("frontier_width".to_string(), w);
    }

    timings = db_timings.clone();

    let mut db_evidence = std::collections::HashMap::new();
    if run_frontier_logic {
        db_evidence.insert("crdt_dot".to_string(), serde_json::to_string(&new_dot).unwrap_or_default());
        db_evidence.insert("crdt_context".to_string(), serde_json::to_string(&parent_context).unwrap_or_default());
    }
    db_evidence.insert("crdt_visible_elements".to_string(), visible_tokens.to_string());
    db_evidence.insert("crdt_tombstones".to_string(), tombstones.to_string());
    db_evidence.insert("crdt_empty_non_root".to_string(), empty_non_root.to_string());
    db_evidence.insert("crdt_operations".to_string(), operations.to_string());
    db_evidence.insert("_timings_ms".to_string(), serde_json::to_string(&db_timings).unwrap_or_default());
    let evidence_json = serde_json::to_string(&db_evidence).unwrap_or_else(|_| "{}".to_string());

    let t_promo_insert_start = std::time::Instant::now();
    sqlx::query("INSERT INTO aether_promotions (id, repo_id, from_head_id, from_head_sequence, to_head_id, to_head_sequence, included_proposals, excluded_proposals, evidence, policy_score, confidence_score, final_score, triggered_by, decision_rationale, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)")
        .bind(&prom_id)
        .bind(&id)
        .bind(&current_repo.head_id)
        .bind(current_repo.head_sequence)
        .bind(&to_head_id)
        .bind(new_seq)
        .bind(&included_json)
        .bind(&excluded_json)
        .bind(&evidence_json)
        .bind(orch_res.policy_score)
        .bind(trust_score)
        .bind(orch_res.final_score)
        .bind("system")
        .bind(Some("CRDT Semantic Merge".to_string()))
        .bind(now)
        .execute(&mut *tx).await.unwrap();
    PROMO_METRICS.t_promo_insert_promotion.fetch_add(t_promo_insert_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    let t_promo_head_start = std::time::Instant::now();
    let is_new_head_canonical = canonical_head_id == to_head_id;
    sqlx::query::<Postgres>("INSERT INTO aether_heads (id, repo_id, snapshot_id, sequence, is_canonical, promotion_id, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)")
        .bind(&to_head_id).bind(&id).bind(&snapshot_id).bind(new_seq).bind(is_new_head_canonical).bind(&prom_id).bind(now)
        .execute(&mut *tx).await.unwrap();

    if !is_new_head_canonical {
        sqlx::query::<Postgres>("UPDATE aether_heads SET is_canonical = true WHERE id = $1")
            .bind(&canonical_head_id).execute(&mut *tx).await.unwrap();
    }
    PROMO_METRICS.t_promo_snapshot_head.fetch_add(t_promo_head_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    let t_promo_cas_start = std::time::Instant::now();
    let cas_result = sqlx::query::<Postgres>(
        "UPDATE aether_repositories SET head_id = $1, head_sequence = $2, updated_at = $3 \
         WHERE id = $4 AND (head_id = $5 OR ($5::text IS NULL AND head_id IS NULL)) AND head_sequence = $6"
    )
    .bind(&canonical_head_id)
    .bind(new_seq)
    .bind(now)
    .bind(&id)
    .bind(&repo_check.head_id)
    .bind(repo_check.head_sequence)
    .execute(&mut *tx)
    .await;
    PROMO_METRICS.t_promo_repo_cas.fetch_add(t_promo_cas_start.elapsed().as_nanos() as u64, Ordering::Relaxed);

    let rows_affected = match cas_result {
        Ok(r) => r.rows_affected(),
        Err(e) => {
            let _ = std::fs::remove_dir_all(&canonical_path);
            let _ = tx.rollback().await;
            let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
            let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
            let mut final_timings = timings.clone();
            final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
            final_timings.insert("t_total_inner".to_string(), t_total_inner);
            let mut evidence = std::collections::HashMap::new();
            evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
            return Err((
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                Json(PromotionResponse {
                    promotion_id: prom_id,
                    success: false,
                    new_head_id: repo_check.head_id.unwrap_or_default(),
                    new_head_sequence: repo_check.head_sequence,
                    included_proposals: vec![],
                    excluded_proposals: payload.proposal_ids.clone(),
                    evidence,
                    final_score: 0.0,
                    message: format!("Failed to update repository under CAS: {:?}", e),
                })
            ));
        }
    };

    if rows_affected == 0 {
        let _ = std::fs::remove_dir_all(&canonical_path);
        let _ = tx.rollback().await;
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        let mut final_timings = timings.clone();
        final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
        final_timings.insert("t_total_inner".to_string(), t_total_inner);
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
        return Err((
            axum::http::StatusCode::CONFLICT,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: repo_check.head_id.unwrap_or_default(),
                new_head_sequence: repo_check.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: 0.0,
                message: "CAS validation failed: repository basis was modified by an external concurrent write".to_string(),
            })
        ));
    }

    let t_db_insert_promotions_heads_cas = t_db_insert_promotions_heads_cas_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_insert_promotions_heads_cas".to_string(), t_db_insert_promotions_heads_cas);

    let t_db_commit_start = std::time::Instant::now();
    if tx.commit().await.is_err() {
        let _ = std::fs::remove_dir_all(&canonical_path);
        let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
        let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
        
        let mut final_timings = timings.clone();
        final_timings.insert("t_lock_hold".to_string(), t_lock_hold);
        final_timings.insert("t_total_inner".to_string(), t_total_inner);
        
        let mut evidence = std::collections::HashMap::new();
        evidence.insert("_timings_ms".to_string(), serde_json::to_string(&final_timings).unwrap_or_default());
        return Err((
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            Json(PromotionResponse {
                promotion_id: prom_id,
                success: false,
                new_head_id: current_repo.head_id.clone().unwrap_or_default(),
                new_head_sequence: current_repo.head_sequence,
                included_proposals: vec![],
                excluded_proposals: payload.proposal_ids.clone(),
                evidence,
                final_score: orch_res.final_score,
                message: "Failed to commit transaction".to_string(),
            })
        ));
    }
    PROMO_METRICS.t_promo_commit.fetch_add(t_db_commit_start.elapsed().as_nanos() as u64, Ordering::Relaxed);
    let t_db_commit = t_db_commit_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_db_commit".to_string(), t_db_commit);

    let t_cache_start = std::time::Instant::now();
        if let Ok(mut cache) = REPO_CACHE.write() {
            let mut new_repo = repo_check.clone();
            new_repo.head_id = Some(canonical_head_id.clone());
            new_repo.head_sequence = new_seq;
            new_repo.updated_at = now;
            cache.insert(id.clone(), RepoCache {
                repo: new_repo,
                head_snapshot_id: Some(snapshot_id.clone()),
            });
        }
    crate::db_postgres::advance_backpressure_sequence(&id, new_seq);
    PROMO_METRICS.t_promo_cache_update.fetch_add(t_cache_start.elapsed().as_nanos() as u64, Ordering::Relaxed);
    
    let t_lock_hold = t_lock_hold_start.elapsed().as_secs_f64() * 1000.0;
    let t_total_inner = t_total_start.elapsed().as_secs_f64() * 1000.0;
    timings.insert("t_lock_hold".to_string(), t_lock_hold);
    timings.insert("t_total_inner".to_string(), t_total_inner);

    let mut api_evidence = std::collections::HashMap::new();
    api_evidence.insert("crdt_dot".to_string(), serde_json::to_string(&new_dot).unwrap_or_default());
    api_evidence.insert("crdt_context".to_string(), serde_json::to_string(&parent_context).unwrap_or_default());
    api_evidence.insert("crdt_visible_elements".to_string(), visible_tokens.to_string());
    api_evidence.insert("crdt_tombstones".to_string(), tombstones.to_string());
    api_evidence.insert("crdt_empty_non_root".to_string(), empty_non_root.to_string());
    api_evidence.insert("crdt_operations".to_string(), operations.to_string());
    api_evidence.insert("_timings_ms".to_string(), serde_json::to_string(&timings).unwrap_or_default());

    let api_evidence_json = serde_json::to_string(&api_evidence).unwrap_or_else(|_| "{}".to_string());
    if let Err(e) = sqlx::query("UPDATE aether_promotions SET evidence = $1 WHERE id = $2")
        .bind(&api_evidence_json)
        .bind(&prom_id)
        .execute(&state.pool)
        .await
    {
        tracing::error!("CRITICAL: failed to backfill timing evidence for CRDT promotion {}: {:?}", prom_id, e);
    }

    Ok(Json(PromotionResponse {
        promotion_id: prom_id,
        success: true,
        new_head_id: to_head_id,
        new_head_sequence: new_seq,
        included_proposals: included,
        excluded_proposals: excluded,
        evidence: api_evidence,
        final_score: orch_res.final_score,
        message: "Semantic merge committed successfully".to_string(),
    }))
}

fn all_proposals_still_pending(
    current_proposals: &[models::entities::Proposal],
    speculative_proposal_ids: &[String],
) -> bool {
    if current_proposals.len() != speculative_proposal_ids.len() {
        return false;
    }
    for p in current_proposals {
        if p.status != models::entities::ProposalStatus::Pending {
            return false;
        }
        if !speculative_proposal_ids.contains(&p.id) {
            return false;
        }
    }
    true
}

pub async fn load_head_frontier_pool(
    pool: &sqlx::PgPool,
    repo_id: &str,
) -> Result<crdt::head_crdt::HeadFrontier, sqlx::Error> {
    let row: Option<(String,)> = sqlx::query_as("SELECT frontier_json FROM aether_head_frontiers WHERE repo_id = $1")
        .bind(repo_id)
        .fetch_optional(pool)
        .await?;

    if let Some((json_str,)) = row {
        let frontier: crdt::head_crdt::HeadFrontier = serde_json::from_str(&json_str)
            .map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
        Ok(frontier)
    } else {
        Ok(crdt::head_crdt::HeadFrontier::new())
    }
}

pub async fn resolve_head_context_pool(
    pool: &sqlx::PgPool,
    head_id: &str,
) -> crdt::causality::VersionVector {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT evidence FROM aether_promotions WHERE to_head_id = $1"
    )
    .bind(head_id)
    .fetch_optional(pool)
    .await
    .unwrap_or(None);

    if let Some((evidence_str,)) = row {
        if let Ok(evidence_map) = serde_json::from_str::<std::collections::HashMap<String, String>>(&evidence_str) {
            if let Some(ctx_str) = evidence_map.get("crdt_context") {
                if let Ok(vv) = serde_json::from_str::<crdt::causality::VersionVector>(ctx_str) {
                    return vv;
                }
            }
        }
    }
    crdt::causality::VersionVector::new()
}

pub async fn resolve_head_dot_pool(
    pool: &sqlx::PgPool,
    head_id: &str,
) -> Option<crdt::causality::Dot> {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT evidence FROM aether_promotions WHERE to_head_id = $1"
    )
    .bind(head_id)
    .fetch_optional(pool)
    .await
    .unwrap_or(None);

    if let Some((evidence_str,)) = row {
        if let Ok(evidence_map) = serde_json::from_str::<std::collections::HashMap<String, String>>(&evidence_str) {
            if let Some(dot_str) = evidence_map.get("crdt_dot") {
                if let Ok(dot) = serde_json::from_str::<crdt::causality::Dot>(dot_str) {
                    return Some(dot);
                }
            }
        }
    }
    None
}
