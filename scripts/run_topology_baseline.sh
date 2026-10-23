#!/usr/bin/env bash
# Aether Ticket 0.5 Topology Baseline Benchmark Harness
#
# Compiles normalized base/TOAST sizes, dead/live ratios, autovacuum counts, and P95/P99 latency percentiles.

set -euo pipefail

PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5432}"
PG_USER="${PGUSER:-${USER:-postgres}}"
PG_SUPERDB="${PGDATABASE:-postgres}"

export SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-60}"
export SOAK_INTERVAL_MS=1000
export SOAK_CRDT_ENABLED=3
export AETHER_ALLOW_NON_DIVISIBLE_TOPOLOGY=1
export AETHER_ALLOW_STARVED_REPOS=1
export AETHER_SOAK_BACKLOG_TOLERANCE=5

REPORT_FILE="docs/sprint4d_topology_baseline.md"
mkdir -p docs

echo "=========================================================================="
echo "               AETHER TICKET 0.5 TOPOLOGY HARNESS STARTING"
echo "=========================================================================="
echo "Duration per sweep: $SOAK_RUN_SECONDS seconds"
echo "Results will be compiled in: $REPORT_FILE"
echo "=========================================================================="

# Define the 6 core topologies to run
# Format: Name | SOAK_AGENTS | SOAK_REPOS | TOPOLOGY_MODE | TUNING_ENABLED
topologies=(
    "1_Repo_Baseline|80|1|uniform|0"
    "10_Repos_Uniform|80|10|uniform|0"
    "100_Repos_Uniform|80|100|100repos|0"
    "Hot_Repo_Skew|80|10|skew|0"
    "1_Repo_Max_Stress|160|1|uniform|0"
    "Hot_Repo_Skew_Max_Stress|160|10|skew|0"
)

# Generate dynamic validation notice based on run duration
if [ "$SOAK_RUN_SECONDS" -ge 300 ]; then
    validation_notice="> [!NOTE]
> **Confirmatory Long-Run Soak Validation Notice:**
> The benchmarks compiled in this report represent ${SOAK_RUN_SECONDS}-second soaks. These serve as confirmatory baseline validation of topology sizing trends and autovacuum metrics under config-level database variations."
else
    validation_notice="> [!NOTE]
> **Directional / Short-Run Validation Notice:**
> The benchmarks compiled in this report represent ${SOAK_RUN_SECONDS}-second sweeps. These serve strictly as directional and short-run validations of topology sizing trends and autovacuum metrics under config-level database variations. They do not constitute equivalent long-run soak evidence to the original 5-minute baselines, but provide rapid mechanism verification under code freeze."
fi

# Prepare markdown table report header
cat << EOF > "$REPORT_FILE"
# Sprint 4D Topology Baseline Report

**Execution Provenance:**
- **Branch**: $(git rev-parse --abbrev-ref HEAD)
- **Commit**: $(git rev-parse HEAD)
- **Command**: $0 $@
- **Sweep Duration**: $SOAK_RUN_SECONDS seconds

$validation_notice
EOF

cat << 'EOF' >> "$REPORT_FILE"

---

## 1. Comparative Sizing & Performance Metrics Matrix

| Benchmark Topology | Total Agents ($N$) | Total Repos | Topology Shape | Tuned? | Committed Proms | TOAST Size (MB) | TOAST MB / 1k Proms | Base Dead/Live | TOAST Dead/Live | Base Vac Count | Base Last Vac | TOAST Vac Count | TOAST Last Vac | P95 Checkout | P99 Checkout | P95 Lock Wait | P99 Lock Wait | P95 Post-Lock Hold | P99 Post-Lock Hold | P95 Occupancy | P99 Occupancy |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
EOF

# Strict Metric Validation function
validate_metric() {
    local m_name="$1"
    local m_val="$2"
    if [ -z "$m_val" ]; then
        echo "❌ Error: Extracted metric '$m_name' is empty or missing!"
        exit 1
    fi
}

for topo in "${topologies[@]}"; do
    IFS='|' read -r name agents repos mode tuning <<< "$topo"
    
    echo ""
    echo "--------------------------------------------------------------------------"
    echo "🏃 RUNNING TOPOLOGY: $name (Agents: $agents, Repos: $repos, Mode: $mode, Tuning: $tuning)"
    echo "--------------------------------------------------------------------------"
    
    export SOAK_AGENTS="$agents"
    export SOAK_REPOS="$repos"
    export AETHER_TOPOLOGY_MODE="$mode"
    export AETHER_APPLY_TOAST_TUNING="$tuning"
    
    # Run the PG soak harness in the background and capture output
    run_log=$(mktemp)
    harness_status=0
    ./scripts/soak_native_rust_pg.sh > "$run_log" 2>&1 || harness_status=$?
    
    # Extract the test database name from the log
    db_name=""
    if grep -q "TEST_DB=" "$run_log"; then
        db_name=$(grep "TEST_DB=" "$run_log" | head -1 | cut -d'=' -f2 | tr -d '"')
    elif grep -q "aether_soak_pg_" "$run_log"; then
        db_name=$(grep "aether_soak_pg_" "$run_log" | head -1 | grep -o 'aether_soak_pg_[0-9]*')
    fi
    
    # If the harness failed, trigger forensic preservation immediately
    if [ "$harness_status" -ne 0 ]; then
        echo "❌ Harness execution failed with status $harness_status!"
        if [ -n "$db_name" ]; then
            db_exists=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" -t -A -c "SELECT 1 FROM pg_database WHERE datname='$db_name';")
            if [ "$db_exists" = "1" ]; then
                echo "🚨 Preserving failed database $db_name for forensics!"
            fi
        else
            echo "⚠️ Could not resolve database name from failed run log."
        fi
        cat "$run_log"
        rm -f "$run_log"
        exit "$harness_status"
    fi
    
    # Assert database name was resolved for successful runs
    if [ -z "$db_name" ]; then
        echo "❌ Error: Could not resolve database name for run $name from log."
        cat "$run_log"
        rm -f "$run_log"
        exit 1
    fi
    
    echo "   ✅ Found Database: $db_name"
    
    # Check if database is populated/exists
    db_exists=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" -t -A -c "SELECT 1 FROM pg_database WHERE datname='$db_name';")
    if [ "$db_exists" != "1" ]; then
        echo "❌ Error: Database $db_name does not exist. Run failed."
        tail -30 "$run_log"
        rm -f "$run_log"
        exit 1
    fi
    
    # Run the SQL diagnostic script to inspect raw autovacuum/TOAST metrics
    echo "   📊 Running diagnostics_toast.sql..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -f scripts/diagnostics_toast.sql >/dev/null 2>&1 || { echo "❌ diagnostics_toast.sql failed!"; exit 1; }
    
    # 1. Fetch committed promotions count
    prom_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "SELECT count(*) FROM aether_promotions;")
    validate_metric "prom_count" "$prom_count"
    echo "   ✅ Committed Promotions: $prom_count"
    
    # 2. Extract database sizing & TOAST info
    toast_mb=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT round((pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid)) / (1024.0 * 1024.0), 3) 
        FROM pg_stat_user_tables WHERE relname='aether_head_frontiers';")
    validate_metric "toast_mb" "$toast_mb"
    if [[ "$toast_mb" == .* ]]; then
        toast_mb="0$toast_mb"
    fi
    
    # Calculate TOAST MB per 1k promotions
    if [ "$prom_count" -gt 0 ]; then
        toast_mb_per_1k=$(echo "scale=3; $toast_mb / ($prom_count / 1000)" | bc)
        validate_metric "toast_mb_per_1k" "$toast_mb_per_1k"
        if [[ "$toast_mb_per_1k" == .* ]]; then
            toast_mb_per_1k="0$toast_mb_per_1k"
        fi
    else
        toast_mb_per_1k="0.000"
    fi
    
    # 3. Pull dead/live tuple ratios
    base_dead_live=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT CASE WHEN n_live_tup = 0 THEN 0.0 ELSE round((n_dead_tup::numeric / n_live_tup::numeric), 4) END 
        FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
    validate_metric "base_dead_live" "$base_dead_live"
    if [[ "$base_dead_live" == .* ]]; then
        base_dead_live="0$base_dead_live"
    fi
        
    toast_oid=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT reltoastrelid FROM pg_class WHERE relname='aether_head_frontiers';")
    validate_metric "toast_oid" "$toast_oid"
        
    toast_dead_live=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT CASE WHEN n_live_tup = 0 THEN 0.0 ELSE round((n_dead_tup::numeric / n_live_tup::numeric), 4) END 
        FROM pg_stat_all_tables WHERE relid='$toast_oid';")
    validate_metric "toast_dead_live" "$toast_dead_live"
    if [[ "$toast_dead_live" == .* ]]; then
        toast_dead_live="0$toast_dead_live"
    fi
        
    # 3.5. Extract autovacuum cadence metrics
    base_vac_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT autovacuum_count FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
    validate_metric "base_vac_count" "$base_vac_count"
    
    base_vac_time=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(to_char(last_autovacuum, 'HH24:MI:SS'), 'never') FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
    validate_metric "base_vac_time" "$base_vac_time"
        
    toast_vac_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT autovacuum_count FROM pg_stat_all_tables WHERE relid='$toast_oid';")
    validate_metric "toast_vac_count" "$toast_vac_count"
    
    toast_vac_time=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(to_char(last_autovacuum, 'HH24:MI:SS'), 'never') FROM pg_stat_all_tables WHERE relid='$toast_oid';")
    validate_metric "toast_vac_time" "$toast_vac_time"
        
    # 4. Execute latency decomposition script to fetch P95 & P99 percentiles
    latency_output=$(DATABASE_URL="postgresql://$PG_HOST:$PG_PORT/$db_name" python3 scripts/diagnostics_latency.py) || { echo "❌ diagnostics_latency.py failed!"; exit 1; }
    
    p95_acquire=$(echo "$latency_output" | grep "t_pool_acquire_ms" | awk -F'|' '{print $3}' | tr -d ' ')
    validate_metric "p95_acquire" "$p95_acquire"
    p99_acquire=$(echo "$latency_output" | grep "t_pool_acquire_ms" | awk -F'|' '{print $4}' | tr -d ' ')
    validate_metric "p99_acquire" "$p99_acquire"
    
    p95_wait=$(echo "$latency_output" | grep "t_lock_wait" | awk -F'|' '{print $3}' | tr -d ' ')
    validate_metric "p95_wait" "$p95_wait"
    p99_wait=$(echo "$latency_output" | grep "t_lock_wait" | awk -F'|' '{print $4}' | tr -d ' ')
    validate_metric "p99_wait" "$p99_wait"
    
    p95_hold=$(echo "$latency_output" | grep "t_lock_hold" | awk -F'|' '{print $3}' | tr -d ' ')
    validate_metric "p95_hold" "$p95_hold"
    p99_hold=$(echo "$latency_output" | grep "t_lock_hold" | awk -F'|' '{print $4}' | tr -d ' ')
    validate_metric "p99_hold" "$p99_hold"
    
    p95_occupancy=$(echo "$latency_output" | grep "Connection Occupancy" | awk -F'|' '{print $3}' | tr -d ' ')
    validate_metric "p95_occupancy" "$p95_occupancy"
    p99_occupancy=$(echo "$latency_output" | grep "Connection Occupancy" | awk -F'|' '{print $4}' | tr -d ' ')
    validate_metric "p99_occupancy" "$p99_occupancy"
    
    # Log to reports file
    tuned_str="No"
    if [ "$tuning" = "1" ]; then
        tuned_str="Yes"
    fi
    
    echo "| $name | $agents | $repos | $mode | $tuned_str | $prom_count | ${toast_mb} MB | ${toast_mb_per_1k} MB | $base_dead_live | $toast_dead_live | $base_vac_count | $base_vac_time | $toast_vac_count | $toast_vac_time | ${p95_acquire} ms | ${p99_acquire} ms | ${p95_wait} ms | ${p99_wait} ms | ${p95_hold} ms | ${p99_hold} ms | ${p95_occupancy} ms | ${p99_occupancy} ms |" >> "$REPORT_FILE"
    
    # Print inline summary
    echo "=========================================================================="
    echo "SWEEP RESULT: $name"
    echo "  - Committed Promotions:        $prom_count"
    echo "  - TOAST Size:                 $toast_mb MB"
    echo "  - TOAST MB / 1k Proms:        $toast_mb_per_1k MB"
    echo "  - Base Dead/Live Tuple Ratio: $base_dead_live"
    echo "  - TOAST Dead/Live Tuple Ratio: $toast_dead_live"
    echo "  - Base Vac Count / Last Vac:  $base_vac_count / $base_vac_time"
    echo "  - TOAST Vac Count / Last Vac: $toast_vac_count / $toast_vac_time"
    echo "  - P95 / P99 Checkout Wait:    $p95_acquire ms / $p99_acquire ms"
    echo "  - P95 / P99 Lock Wait:        $p95_wait ms / $p99_wait ms"
    echo "  - P95 / P99 Post-Lock Hold:   $p95_hold ms / $p99_hold ms"
    echo "  - P95 / P99 Conn Occupancy:   $p95_occupancy ms / $p99_occupancy ms"
    echo "=========================================================================="
    
    # Check if this run is an outlier or if DB preservation is explicitly requested
    is_outlier=0
    p99_wait_int=$(echo "$p99_wait" | cut -d'.' -f1 | tr -d ' ms')
    if [[ "$p99_wait_int" =~ ^[0-9]+$ ]] && [ "$p99_wait_int" -gt 1000 ]; then
        is_outlier=1
    fi
    
    preserve_db=0
    if [ "${AETHER_PRESERVE_DB:-0}" = "1" ] || [ "$is_outlier" -eq 1 ]; then
        preserve_db=1
    fi
    
    rm -f "$run_log"
    if [ "$preserve_db" -eq 1 ]; then
        reason="requested via AETHER_PRESERVE_DB"
        if [ "$is_outlier" -eq 1 ]; then
            reason="detected P99 Lock Wait outlier (${p99_wait} ms)"
        fi
        echo "🚨 Preserving database $db_name for post-run forensics (Reason: $reason)!"
    else
        echo "🧹 Dropping post-mortem database $db_name..."
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db_name' AND pid <> pg_backend_pid();" >/dev/null
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" \
            -c "DROP DATABASE IF EXISTS $db_name;" >/dev/null
        echo "   ✅ Dropped."
    fi

    # Force a checkpoint to reclaim WAL disk space immediately
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" -c "CHECKPOINT;" >/dev/null
    echo "   ✅ Checkpoint completed (WAL reclaimed)."
done

echo ""
echo "=========================================================================="
echo "          AETHER TICKET 0.5 TOPOLOGY SWEEP COMPLETED SUCCESSFULLY!"
echo "=========================================================================="
echo "Report is compiled in: $REPORT_FILE"
echo "=========================================================================="
