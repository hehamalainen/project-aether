-- Aether PostgreSQL TOAST & Autovacuum Sizing Diagnostics
-- Isolates base table vs. TOAST relation dead tuple bloat and autovacuum stats.

\echo '=== [1/2] Base Table & TOAST Relation Details ==='
WITH base_info AS (
    SELECT
        c.oid AS base_oid,
        c.relname AS base_name,
        c.reltoastrelid AS toast_oid
    FROM
        pg_class c
    JOIN
        pg_namespace n ON n.oid = c.relnamespace
    WHERE
        n.nspname = 'public'
        AND c.relname = 'aether_head_frontiers'
),
toast_info AS (
    SELECT
        base_oid,
        base_name,
        toast_oid,
        'pg_toast_' || base_oid AS toast_expected_name
    FROM
        base_info
)
SELECT
    base_name AS base_table,
    base_oid AS base_table_oid,
    toast_oid AS toast_table_oid,
    toast_expected_name AS toast_table_name
FROM
    toast_info;


\echo '=== [2/2] Live/Dead Tuples & Autovacuum Cadence Statistics ==='
WITH base_info AS (
    SELECT
        c.oid AS base_oid,
        c.reltoastrelid AS toast_oid
    FROM
        pg_class c
    JOIN
        pg_namespace n ON n.oid = c.relnamespace
    WHERE
        n.nspname = 'public'
        AND c.relname = 'aether_head_frontiers'
)
SELECT
    'BASE TABLE (public.aether_head_frontiers)' AS relation_type,
    s.schemaname,
    s.relname AS relation_name,
    s.n_live_tup AS live_tuples,
    s.n_dead_tup AS dead_tuples,
    CASE 
        WHEN s.n_live_tup = 0 THEN 0.0
        ELSE round((s.n_dead_tup::numeric / s.n_live_tup::numeric), 4)
    END AS dead_to_live_ratio,
    s.last_autovacuum,
    s.autovacuum_count
FROM
    pg_stat_all_tables s
JOIN
    base_info b ON b.base_oid = s.relid

UNION ALL

SELECT
    'ASSOCIATED TOAST RELATION' AS relation_type,
    s.schemaname,
    s.relname AS relation_name,
    s.n_live_tup AS live_tuples,
    s.n_dead_tup AS dead_tuples,
    CASE 
        WHEN s.n_live_tup = 0 THEN 0.0
        ELSE round((s.n_dead_tup::numeric / s.n_live_tup::numeric), 4)
    END AS dead_to_live_ratio,
    s.last_autovacuum,
    s.autovacuum_count
FROM
    pg_stat_all_tables s
JOIN
    base_info b ON b.toast_oid = s.relid;

\echo '=== [3/3] Physical Sizing Breakdown (Table, Index, TOAST, Total) ==='
SELECT
    relname AS table_name,
    pg_size_pretty(pg_relation_size(relid)) AS table_size,
    pg_size_pretty(pg_indexes_size(relid)) AS index_size,
    pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid)) AS toast_size,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM
    pg_stat_user_tables
WHERE
    relname = 'aether_head_frontiers';
