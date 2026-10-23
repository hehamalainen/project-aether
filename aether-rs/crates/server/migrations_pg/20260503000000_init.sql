-- PostgreSQL init schema for Aether-RS
-- Dialect-converted from SQLite migrations/20260503000000_init.sql
--
-- Key differences from SQLite version:
--   INTEGER → BIGINT (PG INT4 vs Rust i64 requires INT8/BIGINT)
--   DATETIME → TIMESTAMPTZ
--   BOOLEAN → BOOLEAN (native, not integer)
--   DEFAULT 1 (boolean) → DEFAULT TRUE
--   DEFAULT 0 (boolean) → DEFAULT FALSE
--   No AUTOINCREMENT (TEXT PKs, not needed)

CREATE TABLE aether_repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    head_id TEXT,
    head_sequence BIGINT NOT NULL DEFAULT 0,
    gate_config TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    max_alternatives_per_conflict BIGINT NOT NULL DEFAULT 5,
    auto_promote BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX ix_aether_repositories_name ON aether_repositories (name);
CREATE INDEX ix_aether_repositories_head_id ON aether_repositories (head_id);

CREATE TABLE aether_proposals (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    agent_id TEXT NOT NULL,
    task_id TEXT,
    source TEXT NOT NULL,
    source_run_id TEXT,
    source_task_id TEXT,
    source_agent_id TEXT,
    source_attempt_id TEXT,
    intent_type TEXT,
    base_head_id TEXT,
    base_head_sequence BIGINT NOT NULL DEFAULT 0,
    change_type TEXT NOT NULL DEFAULT 'unified_diff',
    change_content TEXT NOT NULL,
    affected_paths TEXT NOT NULL DEFAULT '[]',
    affected_symbols TEXT,
    risk_tier BIGINT NOT NULL DEFAULT 1,
    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    model_id TEXT,
    calibration_profile TEXT,
    rationale TEXT,
    dependencies TEXT NOT NULL DEFAULT '[]',
    requires_tests BOOLEAN NOT NULL DEFAULT TRUE,
    requires_security_scan BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'PENDING',
    conflict_set_id TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evaluated_at TIMESTAMPTZ,
    promoted_at TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX ix_aether_proposals_repo_id ON aether_proposals (repo_id);
CREATE INDEX ix_aether_proposals_agent_id ON aether_proposals (agent_id);

CREATE TABLE aether_conflict_sets (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    path TEXT NOT NULL,
    region_type TEXT NOT NULL DEFAULT 'text_range',
    start_line BIGINT,
    end_line BIGINT,
    symbol_name TEXT,
    proposal_ids TEXT NOT NULL DEFAULT '[]',
    active_choice_id TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE aether_snapshots (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    head_sequence BIGINT NOT NULL,
    included_proposals TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    storage_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT,
    file_count BIGINT NOT NULL DEFAULT 0,
    total_bytes BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE aether_gate_results (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    snapshot_id TEXT NOT NULL REFERENCES aether_snapshots(id),
    proposal_id TEXT,
    gate_name TEXT NOT NULL,
    gate_command TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    exit_code BIGINT,
    stdout TEXT,
    stderr TEXT,
    duration_ms BIGINT,
    tests_passed BIGINT,
    tests_failed BIGINT,
    coverage_pct DOUBLE PRECISION,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE aether_promotions (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    from_head_id TEXT,
    from_head_sequence BIGINT NOT NULL DEFAULT 0,
    to_head_id TEXT NOT NULL,
    to_head_sequence BIGINT NOT NULL,
    included_proposals TEXT NOT NULL DEFAULT '[]',
    excluded_proposals TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    policy_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    final_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    decision_rationale TEXT,
    triggered_by TEXT NOT NULL DEFAULT 'auto',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE aether_operation_graph_nodes (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    promotion_id TEXT NOT NULL REFERENCES aether_promotions(id),
    parent_node_id TEXT,
    parent_head_id TEXT,
    parent_head_sequence BIGINT NOT NULL DEFAULT 0,
    to_head_id TEXT,
    to_head_sequence BIGINT NOT NULL DEFAULT 0,
    snapshot_id TEXT NOT NULL REFERENCES aether_snapshots(id),
    operation_type TEXT NOT NULL DEFAULT 'promotion',
    node_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    parent_ref TEXT,
    replay_from_hash TEXT,
    replay_to_hash TEXT,
    replay_gate_vector TEXT NOT NULL DEFAULT '',
    included_proposals TEXT NOT NULL DEFAULT '[]',
    excluded_proposals TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE aether_system_metric_events (
    id TEXT PRIMARY KEY,
    repo_id TEXT REFERENCES aether_repositories(id),
    proposal_id TEXT REFERENCES aether_proposals(id),
    event_type TEXT NOT NULL,
    event_reason TEXT,
    path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE aether_heads (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES aether_repositories(id),
    sequence BIGINT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES aether_snapshots(id),
    promotion_id TEXT REFERENCES aether_promotions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_canonical BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE aether_agent_metrics (
    agent_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    total_proposals BIGINT NOT NULL DEFAULT 0,
    promoted_count BIGINT NOT NULL DEFAULT 0,
    hard_fail_count BIGINT NOT NULL DEFAULT 0,
    neutral_loss_count BIGINT NOT NULL DEFAULT 0,
    trust_alpha DOUBLE PRECISION NOT NULL DEFAULT 3.0,
    trust_beta DOUBLE PRECISION NOT NULL DEFAULT 3.0,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, scope)
);

CREATE INDEX ix_aether_proposals_superseded_lookup ON aether_proposals (repo_id, agent_id) WHERE status = 'PENDING';
CREATE INDEX ix_aether_proposals_active_lookup ON aether_proposals (repo_id, status);

CREATE INDEX ix_aether_heads_repo_canonical ON aether_heads (repo_id) WHERE is_canonical = TRUE;
CREATE INDEX ix_aether_heads_snapshot_id ON aether_heads (snapshot_id);
