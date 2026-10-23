-- Force aggressive autovacuum tuning on the specific public.aether_head_frontiers base table
ALTER TABLE public.aether_head_frontiers SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_vacuum_threshold = 50,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_analyze_threshold = 20
);

-- Force aggressive autovacuum tuning specifically on its associated TOAST relation table
ALTER TABLE public.aether_head_frontiers SET (
    toast.autovacuum_vacuum_scale_factor = 0.05,
    toast.autovacuum_vacuum_threshold = 50
);

\echo '✅ Aggressive autovacuum parameters successfully applied to public.aether_head_frontiers and its associated TOAST relation!'
