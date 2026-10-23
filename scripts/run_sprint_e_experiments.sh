#!/usr/bin/env bash
# Aether Ticket 0.5 Sprint E Experiment Harness
#
# Runs a dedicated set of experiments under the extreme Hot_Repo_Skew_Max_Stress topology
# to evaluate autovacuum starvation.
# Outputs to docs/sprint4e_mitigation_experiments.md.

set -euo pipefail

PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5432}"
PG_USER="${PGUSER:-${USER:-postgres}}"
PG_SUPERDB="${PGDATABASE:-postgres}"

export SOAK_RUN_SECONDS=60
export SOAK_INTERVAL_MS=1000
export SOAK_CRDT_ENABLED=3
export AETHER_ALLOW_NON_DIVISIBLE_TOPOLOGY=1
export AETHER_PRESERVE_DB=${AETHER_PRESERVE_DB:-1}
export AETHER_MIN_FREE_SPACE_MB=512

REPORT_FILE="docs/sprint4e_mitigation_experiments.md"
LOG_DIR="docs/sprint4e_logs"

mkdir -p docs
mkdir -p "$LOG_DIR"

# Clean up any old report and logs
rm -f "$REPORT_FILE"
rm -rf "${LOG_DIR:?}"/*

# Query Git and directory details dynamically for Provenance tracking
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
CANONICAL_TREE=$(pwd)
COMMAND="./scripts/run_sprint_e_experiments.sh"

echo "=========================================================================="
# Title
echo "             AETHER TICKET 0.5 SPRINT E EXPERIMENT HARNESS (RERUN)"
echo "=========================================================================="
echo "Sweep duration: $SOAK_RUN_SECONDS seconds"
echo "Provenance Tree: $CANONICAL_TREE"
echo "Git Branch:      $BRANCH"
echo "Git Commit SHA:  $SHA"
echo "Results will be compiled in: $REPORT_FILE"
echo "=========================================================================="

# Define the Experiment Matrix
# Format: Name | Tuning? | MaxConns | ForcedVac? | BacklogVal | PathVal
experiments=(
    "Control_Tuned_Mitigation|1|50|0||"
    "Exp1_Forced_Vacuum|1|50|1||"
    "Exp2a_Pool_20|1|20|0||"
    "Exp2b_Pool_100|1|100|0||"
    "Exp3a_Backlog_Valve_20|1|50|0|20|"
    "Exp3b_Path_Valve_5|1|50|0||5"
)

# Write report header with strict dynamic provenance metadata
cat << EOF > "$REPORT_FILE"
# Sprint 4E Autovacuum Starvation Mitigation & Analysis Report

## 1. Execution Provenance
- **Canonical Run Tree**: \`${CANONICAL_TREE}\` (Local, non-iCloud)
- **Execution Branch**: \`${BRANCH}\`
- **Execution Commit SHA**: \`${SHA}\`
- **Execution Command**: \`${COMMAND}\`

> [!NOTE]
> **Directional / Short-Run Validation Notice:**
> The benchmarks compiled in this report represent 60-second sweeps on the \`Hot_Repo_Skew_Max_Stress\` topology (160 agents, 10 repos, skew shape). These serve to test whether autovacuum starvation is a tunable configuration issue or an architectural lock-acquisition problem.

---

## 2. Environment & Knobs Configuration Matrix

| Experiment Name | TOAST Tuning | Max Connections | Forced Vacuum? | Backlog Limit | Path Limit |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Control_Tuned_Mitigation | Yes | 50 | No | Disabled | Disabled |
| Exp1_Forced_Vacuum | Yes | 50 | Yes (5s cycle) | Disabled | Disabled |
| Exp2a_Pool_20 | Yes | 20 | No | Disabled | Disabled |
| Exp2b_Pool_100 | Yes | 100 | No | Disabled | Disabled |
| Exp3a_Backlog_Valve_20 | Yes | 50 | No | 20 | Disabled |
| Exp3b_Path_Valve_5 | Yes | 50 | No | Disabled | 5 |

---

## 3. Comparative Performance & Database Sizing Metrics

| Experiment Name | Committed Proms | Submit OK | Submit Fail | Promote OK | Promote 409 | Promote Rej | Promote 5xx | TOAST Size (MB) | TOAST MB / 1k Proms | Base Dead/Live | TOAST Dead/Live | Base Vac Count | Base Last Vac | TOAST Vac Count | TOAST Last Vac | P95 Checkout | P99 Checkout | P95 Lock Wait | P99 Lock Wait | P95 Post-Lock Hold | P99 Post-Lock Hold | P95 Occupancy | P99 Occupancy |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
EOF

forced_vacuum_log_path=""

for exp in "${experiments[@]}"; do
    IFS='|' read -r name tuning max_conns forced_vac backlog_val path_val <<< "$exp"
    
    echo ""
    echo "--------------------------------------------------------------------------"
    echo "🏃 RUNNING EXPERIMENT: $name"
    echo "--------------------------------------------------------------------------"
    
    # Configure workload topology: Hot_Repo_Skew_Max_Stress
    export SOAK_AGENTS=160
    export SOAK_REPOS=10
    export AETHER_TOPOLOGY_MODE="skew"
    export AETHER_APPLY_TOAST_TUNING="$tuning"
    export DATABASE_MAX_CONNECTIONS="$max_conns"
    export AETHER_RUN_FORCED_VACUUM="$forced_vac"
    
    if [ -n "$backlog_val" ]; then
        export AETHER_MAX_COHERENCE_BACKLOG="$backlog_val"
    else
        unset AETHER_MAX_COHERENCE_BACKLOG
    fi
    
    if [ -n "$path_val" ]; then
        export AETHER_MAX_PATH_ACTIVE_PROPOSALS="$path_val"
    else
        unset AETHER_MAX_PATH_ACTIVE_PROPOSALS
    fi
    
    # Run the PG soak harness in the background and capture output
    run_log=$(mktemp)
    soak_status=0
    ./scripts/soak_native_rust_pg.sh > "$run_log" 2>&1 || soak_status=$?
    
    # Extract the test database name from the log
    db_name=$(grep "TEST_DB=" "$run_log" | head -1 | cut -d'=' -f2 | tr -d '"' || true)
    if [ -z "$db_name" ]; then
        db_name=$(grep "aether_soak_pg_" "$run_log" | head -1 | grep -o 'aether_soak_pg_[0-9]*' || true)
    fi
    
    # Resolve soak home directory
    soak_home=$(grep "Soak home:" "$run_log" | head -1 | awk '{print $3}' || echo "")
    if [ -z "$soak_home" ]; then
        soak_home="$HOME/.aether/.soak_workspace_pg"
    fi
    
    # Copy server log to dedicated self-contained folder
    if [ -f "$soak_home/server.log" ]; then
        cp "$soak_home/server.log" "$LOG_DIR/${name}_server.log"
    fi
    
    # Store forced vacuum log path if applicable
    if [ "$forced_vac" = "1" ] && [ -f "$soak_home/forced_vacuum.log" ]; then
        cp "$soak_home/forced_vacuum.log" "$LOG_DIR/forced_vacuum_sprint_e.log"
        forced_vacuum_log_path="$LOG_DIR/forced_vacuum_sprint_e.log"
    fi

    # Fail-fast if the soak script aborted early or crashed
    if ! grep -q -E "SOAK (PASSED|FAILED)" "$run_log"; then
        echo "❌ Error: soak_native_rust_pg.sh did not run to completion during $name (exit code: $soak_status)."
        if [ -f "$soak_home/forced_vacuum.log" ]; then
            echo "--- Last 50 lines of forced_vacuum.log ---"
            tail -50 "$soak_home/forced_vacuum.log"
        fi
        echo "--- Last 100 lines of run output log ---"
        tail -100 "$run_log"
        rm -f "$run_log"
        exit 1
    fi

    # Fail-fast: check if server died or crashed
    if ! grep -q "Server alive:          yes" "$run_log"; then
        echo "❌ Error: Server died during run $name"
        tail -100 "$run_log"
        rm -f "$run_log"
        exit 1
    fi
    
    # Check if database is populated/exists
    if [ -z "$db_name" ]; then
        echo "❌ Error: Could not extract database name for run $name"
        tail -100 "$run_log"
        rm -f "$run_log"
        exit 1
    fi
    db_exists=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" -t -A -c "SELECT 1 FROM pg_database WHERE datname='$db_name';" || echo "0")
    if [ "$db_exists" != "1" ]; then
        echo "❌ Error: Database $db_name does not exist. Run failed."
        tail -100 "$run_log"
        rm -f "$run_log"
        exit 1
    fi
    
    # Run the SQL diagnostic script to inspect raw autovacuum/TOAST metrics
    echo "   📊 Running diagnostics_toast.sql..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -f scripts/diagnostics_toast.sql || {
        echo "❌ Error: diagnostics_toast.sql failed for $name."
        rm -f "$run_log"
        exit 1
    }
    
    # 1. Fetch committed promotions count
    prom_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "SELECT count(*) FROM aether_promotions;")
    echo "   ✅ Committed Promotions: $prom_count"
    
    # Extract HTTP Outcome counts directly from log
    submit_ok=$(grep "Submit OK:" "$run_log" | awk '{print $3}' || echo "0")
    submit_fail=$(grep "Submit Fail:" "$run_log" | awk '{print $3}' || echo "0")
    promote_ok=$(grep "Promote OK:" "$run_log" | awk '{print $3}' || echo "0")
    promote_409=$(grep "Promote Contention:" "$run_log" | awk '{print $3}' || echo "0")
    promote_rej=$(grep "Promote Rejected:" "$run_log" | awk '{print $3}' || echo "0")
    promote_5xx=$(grep "Promote 5xx:" "$run_log" | awk '{print $3}' || echo "0")

    # Fallback to zero if empty
    submit_ok=${submit_ok:-0}
    submit_fail=${submit_fail:-0}
    promote_ok=${promote_ok:-0}
    promote_409=${promote_409:-0}
    promote_rej=${promote_rej:-0}
    promote_5xx=${promote_5xx:-0}

    # 2. Extract database sizing & TOAST info
    toast_mb=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT round((pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid)) / (1024.0 * 1024.0), 3) 
        FROM pg_stat_user_tables WHERE relname='aether_head_frontiers';")
    
    # Calculate TOAST MB per 1k promotions
    if [ "$prom_count" -gt 0 ]; then
        toast_mb_per_1k=$(echo "scale=3; $toast_mb / ($prom_count / 1000)" | bc 2>/dev/null || echo "0.000")
    else
        toast_mb_per_1k="0.000"
    fi
    
    # 3. Pull dead/live tuple ratios
    base_dead_live=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT CASE WHEN n_live_tup = 0 THEN 0.0 ELSE round((n_dead_tup::numeric / n_live_tup::numeric), 4) END 
        FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
        
    toast_oid=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT reltoastrelid FROM pg_class WHERE relname='aether_head_frontiers';")
        
    toast_dead_live=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT CASE WHEN n_live_tup = 0 THEN 0.0 ELSE round((n_dead_tup::numeric / n_live_tup::numeric), 4) END 
        FROM pg_stat_all_tables WHERE relid='$toast_oid';")
        
    # 3.5. Extract autovacuum cadence metrics
    base_vac_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(autovacuum_count, 0) FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
    base_vac_time=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(to_char(last_autovacuum, 'HH24:MI:SS'), 'never') FROM pg_stat_all_tables WHERE relname='aether_head_frontiers';")
        
    toast_vac_count=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(autovacuum_count, 0) FROM pg_stat_all_tables WHERE relid='$toast_oid';")
    toast_vac_time=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db_name" -t -A -c "
        SELECT COALESCE(to_char(last_autovacuum, 'HH24:MI:SS'), 'never') FROM pg_stat_all_tables WHERE relid='$toast_oid';")
        
    # 4. Execute latency decomposition script to fetch P95 & P99 percentiles
    latency_output=$(DATABASE_URL="postgresql://$PG_HOST:$PG_PORT/$db_name" .venv/bin/python3 scripts/diagnostics_latency.py) || {
        echo "❌ Error: diagnostics_latency.py failed for $name."
        rm -f "$run_log"
        exit 1
    }
    
    p95_acquire=$(echo "$latency_output" | grep "t_pool_acquire_ms" | awk -F'|' '{print $3}' | tr -d ' ' || echo "")
    p99_acquire=$(echo "$latency_output" | grep "t_pool_acquire_ms" | awk -F'|' '{print $4}' | tr -d ' ' || echo "")
    
    p95_wait=$(echo "$latency_output" | grep "t_lock_wait" | awk -F'|' '{print $3}' | tr -d ' ' || echo "")
    p99_wait=$(echo "$latency_output" | grep "t_lock_wait" | awk -F'|' '{print $4}' | tr -d ' ' || echo "")
    
    p95_hold=$(echo "$latency_output" | grep "t_lock_hold" | awk -F'|' '{print $3}' | tr -d ' ' || echo "")
    p99_hold=$(echo "$latency_output" | grep "t_lock_hold" | awk -F'|' '{print $4}' | tr -d ' ' || echo "")
    
    p95_occupancy=$(echo "$latency_output" | grep "Connection Occupancy" | awk -F'|' '{print $3}' | tr -d ' ' || echo "")
    p99_occupancy=$(echo "$latency_output" | grep "Connection Occupancy" | awk -F'|' '{print $4}' | tr -d ' ' || echo "")

    if [ "$prom_count" -gt 0 ]; then
        if [ -z "$p95_acquire" ] || [ -z "$p99_acquire" ] || [ -z "$p95_wait" ] || [ -z "$p99_wait" ] || [ -z "$p95_hold" ] || [ -z "$p99_hold" ] || [ -z "$p95_occupancy" ] || [ -z "$p99_occupancy" ]; then
            echo "❌ Error: Missing expected latency metrics in latency_output for $name"
            echo "Latency Output:"
            echo "$latency_output"
            exit 1
        fi
    else
        # If no promotions, default to 0.000
        p95_acquire=${p95_acquire:-0.000}
        p99_acquire=${p99_acquire:-0.000}
        p95_wait=${p95_wait:-0.000}
        p99_wait=${p99_wait:-0.000}
        p95_hold=${p95_hold:-0.000}
        p99_hold=${p99_hold:-0.000}
        p95_occupancy=${p95_occupancy:-0.000}
        p99_occupancy=${p99_occupancy:-0.000}
    fi
    
    # Log to reports file
    echo "| $name | $prom_count | $submit_ok | $submit_fail | $promote_ok | $promote_409 | $promote_rej | $promote_5xx | ${toast_mb} MB | ${toast_mb_per_1k} MB | $base_dead_live | $toast_dead_live | $base_vac_count | $base_vac_time | $toast_vac_count | $toast_vac_time | ${p95_acquire} ms | ${p99_acquire} ms | ${p95_wait} ms | ${p99_wait} ms | ${p95_hold} ms | ${p99_hold} ms | ${p95_occupancy} ms | ${p99_occupancy} ms |" >> "$REPORT_FILE"
    
    # Print inline summary
    echo "=========================================================================="
    echo "EXPERIMENT RESULT: $name"
    echo "  - Committed Promotions:        $prom_count"
    echo "  - Submits:                     OK=$submit_ok, Fail=$submit_fail"
    echo "  - Promotes:                    OK=$promote_ok, 409=$promote_409, Rej=$promote_rej, 5xx=$promote_5xx"
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
    
    rm -f "$run_log"

    # Clean up or preserve the DB according to AETHER_PRESERVE_DB variable
    if [[ "${AETHER_PRESERVE_DB:-0}" == "1" ]]; then
        echo "   📌 Preserving database $db_name for forensics."
    else
        echo "   🧹 Dropping post-mortem database $db_name..."
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db_name' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_SUPERDB" \
            -c "DROP DATABASE IF EXISTS $db_name;" >/dev/null 2>&1 || true
        echo "   ✅ Dropped."
    fi
done

# ── Parse and compile Forced Vacuum execution logs ──────────────────────────
cat << 'EOF' >> "$REPORT_FILE"

---

## 4. Experiment 1: Forced Vacuum Execution Detail & Diagnostics

This section compiles the telemetry captured by the background forced vacuum daemon during `Exp1_Forced_Vacuum`. This distinguishes between "vacuum was blocked by contention" vs. "vacuum successfully ran but failed to prevent the performance collapse."

| Log Path | Total Attempts | Successes | Failures/Timeouts | Success % | Max Execution Duration | Completed Before Next Cycle? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
EOF

log_path="$forced_vacuum_log_path"
if [ -n "$log_path" ] && [ -f "$log_path" ]; then
    echo "   📊 Parsing forced vacuum log at $log_path..."
    
    # Calculate statistics
    total_attempts=$(grep -c "Attempting VACUUM VERBOSE" "$log_path" || true)
    success_count=$(grep -c "SUCCESS" "$log_path" || true)
    failure_count=$(grep -c "FAILURE" "$log_path" || true)
    
    success_pct="0.0%"
    if [ "$total_attempts" -gt 0 ]; then
        success_pct=$(echo "scale=2; ($success_count * 100) / $total_attempts" | bc 2>/dev/null || echo "0.0")"%"
    fi
    
    max_duration=$(grep -o "Duration: [0-9]*s" "$log_path" | awk '{print $2}' | tr -d 's' | sort -rn | head -1 || echo "0")
    
    any_failed_next=$(grep -c "Completed before next: No" "$log_path" || true)
    completed_next="Yes"
    if [ "$any_failed_next" -gt 0 ]; then
        completed_next="No"
    fi
    
    # Write to report with relative workspace path for portable clean tree provenance
    echo "| \`$log_path\` | $total_attempts | $success_count | $failure_count | $success_pct | ${max_duration}s | $completed_next |" >> "$REPORT_FILE"
    
    # Print raw log trace inside a details block for forensic auditability
    cat << EOF >> "$REPORT_FILE"

<details>
<summary>Raw Daemon Execution Trace Log (Exp1_Forced_Vacuum)</summary>

\`\`\`text
$(cat "$log_path")
\`\`\`

</details>
EOF
else
    echo "| \`$log_path\` | N/A | N/A | N/A | N/A | N/A | N/A |" >> "$REPORT_FILE"
fi

# ── Append Evaluation against Dual Kill Criterion ────────────────────────────
cat << 'EOF' >> "$REPORT_FILE"

---

## 5. Evaluation of the Dual Kill Criterion

Recall the **Dual Kill Criterion**:
> If tuned runs (with aggressive autovacuum, forced vacuum interventions, and pool sizing adjustments) *still* show autovacuum starvation **AND** near-collapse progress (< 1 TPS) under the skew/max stress topology, Sprint E concludes **contention-avoidance is required**. It will NOT keep spinning on un-tunable autovacuum parameters.

### 4.1. Core Findings Analysis
*(To be completed after running the experiments)*

### 4.2. Final Decision & Recommendation
*(To be completed after running the experiments)*
EOF

echo ""
echo "=========================================================================="
echo "          AETHER TICKET 0.5 SPRINT E EXPERIMENTS COMPLETED!"
echo "=========================================================================="
echo "Report is compiled in: $REPORT_FILE"
echo "=========================================================================="
