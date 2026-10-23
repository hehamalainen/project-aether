-- Aether Diagnostic Storage & Database Sizing
-- Measures table, index, TOAST, and total size along with row counts and HeadEvent counts

\echo '--- [1/2] PostgreSQL All User Tables Ranked By Size ---'
SELECT
    schemaname,
    relname AS table_name,
    n_live_tup AS row_count,
    round(pg_relation_size(relid) / (1024.0 * 1024.0), 3) AS table_size_mb,
    round(pg_indexes_size(relid) / (1024.0 * 1024.0), 3) AS index_size_mb,
    round((pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid)) / (1024.0 * 1024.0), 3) AS toast_size_mb,
    round(pg_total_relation_size(relid) / (1024.0 * 1024.0), 3) AS total_size_mb
FROM
    pg_stat_user_tables
ORDER BY
    total_size_mb DESC,
    row_count DESC;

\echo '--- [2/2] Causal Frontier (aether_head_frontiers) Metrics ---'
SELECT
    repo_id,
    length(frontier_json) AS frontier_json_bytes,
    -- NOTE: headevent_count is a lightweight SQL heuristic that counts substring matches of '"snapshot_id"'
    -- to estimate active frontier event cardinality without overhead. While useful for rough sizing,
    -- it is not a strict JSON parser and can have a small margin of error under adversarial inputs.
    CASE 
        WHEN frontier_json IS NULL OR frontier_json = '' THEN 0
        ELSE (length(frontier_json) - length(replace(frontier_json, '"snapshot_id"', ''))) / length('"snapshot_id"')
    END AS headevent_count
FROM
    aether_head_frontiers
ORDER BY
    frontier_json_bytes DESC;
