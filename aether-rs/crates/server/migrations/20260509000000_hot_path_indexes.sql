-- Hot-path composite indexes for Aether-RS query performance.
-- These indexes target the heaviest scan patterns in the proposal
-- lifecycle, promotion, and listing hot paths.

-- Proposal status scans (used by request_promotion and commit_promotion)
CREATE INDEX IF NOT EXISTS ix_proposals_repo_status 
  ON aether_proposals (repo_id, status);

-- Canonical head lookup
CREATE INDEX IF NOT EXISTS ix_heads_repo_canonical 
  ON aether_heads (repo_id, is_canonical);

-- Promotion history hot path
CREATE INDEX IF NOT EXISTS ix_promotions_repo_created 
  ON aether_promotions (repo_id, created_at DESC);

-- Proposals ordered by promoted_at for canonical materialization
CREATE INDEX IF NOT EXISTS ix_proposals_repo_promoted 
  ON aether_proposals (repo_id, status, promoted_at);

-- Proposals ordered by submitted_at for listing (list_proposals)
CREATE INDEX IF NOT EXISTS ix_proposals_repo_submitted
  ON aether_proposals (repo_id, submitted_at DESC);
