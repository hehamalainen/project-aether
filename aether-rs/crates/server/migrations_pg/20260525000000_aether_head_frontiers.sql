-- Migration to create the head frontiers table for Postgres
CREATE TABLE IF NOT EXISTS aether_head_frontiers (
    repo_id VARCHAR(255) PRIMARY KEY,
    frontier_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
