-- Migration to create the head frontiers table for SQLite
CREATE TABLE IF NOT EXISTS aether_head_frontiers (
    repo_id TEXT PRIMARY KEY,
    frontier_json TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
