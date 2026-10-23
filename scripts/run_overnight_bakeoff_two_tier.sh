#!/usr/bin/env bash
# ============================================================================
# Directional Overnight Two-Tier Probe (Tiers 3 & 5)
# ============================================================================
# Runs a 1-hour per lane, 4-lane comparative probe at Tier 3 (40 agents)
# and Tier 5 (160 agents), with Tier 5 strictly gated on Tier 3 success.
# Keeps the MacBook awake using caffeinate and compiles timings results.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Global configurations for the overnight probe
export SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-3600}" # 1 hour per run (3600s)
export SOAK_DRAIN_SECONDS="${SOAK_DRAIN_SECONDS:-60}"   # 60s drain
export SOAK_REPOS=10

# Summary Report Path
REPORT_FILE="$REPO_ROOT/docs/sprint4d_bakeoff_results.md"

# Isolated Workspace Root
WORKSPACES_ROOT="$HOME/.aether/soak_pg_sprint4d_overnight"
mkdir -p "$WORKSPACES_ROOT"

echo "⚙️ Running host pre-flight verification..."

# A. Verify and enforce process file descriptor limits (ulimit)
CURRENT_FD_LIMIT=$(ulimit -n || echo "0")
if [ "$CURRENT_FD_LIMIT" = "unlimited" ]; then
    echo "   ✅ File descriptor limit is unlimited."
elif [ "$CURRENT_FD_LIMIT" -lt 65536 ]; then
    echo "   Current limit ($CURRENT_FD_LIMIT) is less than 65536. Attempting to set limit to 65536..."
    ulimit -n 65536 || {
        echo "❌ PREFLIGHT ERROR: Failed to set file descriptor limit (ulimit -n 65536)."
        exit 1
    }
    echo "   ✅ File descriptor limit successfully set to 65536."
else
    echo "   ✅ File descriptor limit is $CURRENT_FD_LIMIT."
fi

# B. Verify host free-space (at least 15 GB)
FREE_SPACE=$(df -m "$REPO_ROOT" | tail -n 1 | awk '{print $4}')
if [[ ! "$FREE_SPACE" =~ ^[0-9]+$ ]]; then
    FREE_SPACE=0
fi
if [ "$FREE_SPACE" -lt 15360 ]; then
    echo "❌ PREFLIGHT ERROR: Host available free space ($FREE_SPACE MB) is less than 15 GB."
    exit 1
else
    echo "   ✅ Host free space is $FREE_SPACE MB."
fi

# C. PostgreSQL Connection parameters
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"

# D. Query max_connections (requires >= 250 for Tier 5)
MAX_CONN=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -t -A -c "SHOW max_connections;" 2>/dev/null || echo "0")
if [ "$MAX_CONN" -eq 0 ]; then
    echo "❌ PREFLIGHT ERROR: Failed to connect to PostgreSQL on $PG_HOST:$PG_PORT."
    exit 1
fi
echo "   PostgreSQL max_connections detected: $MAX_CONN"
if [ "$MAX_CONN" -lt 250 ]; then
    echo "❌ PREFLIGHT ERROR: Postgres max_connections ($MAX_CONN) is less than 250. High-density sweeps require at least 250 connections."
    exit 1
fi
echo "   ✅ Connected to PostgreSQL."

# Ensure caffeinate keeps Mac awake
echo "☕ Starting caffeinate..."
/usr/bin/caffeinate -dimsu &
CAFFEINATE_PID=$!

cleanup() {
    echo "🧹 Cleaning up overnight runner..."
    if kill -0 "$CAFFEINATE_PID" >/dev/null 2>&1; then
        kill "$CAFFEINATE_PID" >/dev/null 2>&1 || true
        wait "$CAFFEINATE_PID" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

# Define the two tiers to run tonight (conditional gating)
TIER3_RUNS=(
    "control|tier3|40|0"
    "advisory_lock|tier3|40|1"
    "phase3_frontiers|tier3|40|2"
    "phase4_token|tier3|40|3"
)

TIER5_RUNS=(
    "control|tier5|160|0"
    "advisory_lock|tier5|160|1"
    "phase3_frontiers|tier5|160|2"
    "phase4_token|tier5|160|3"
)

echo "📊 Preparing consolidated report file: $REPORT_FILE"
mkdir -p "$(dirname "$REPORT_FILE")"

cat << EOF > "$REPORT_FILE"
# Sprint 4D Directional Overnight Two-Tier Probe Results

**Start Time:** $(date)  
**Matrix:** 10 Repositories, ${SOAK_RUN_SECONDS} seconds (1 hour) run duration per tier.  
**System Configuration:** ulimit -n $(ulimit -n), max_connections = $MAX_CONN.

---

## Run Configurations & Status

| Run | Lane | Tier | Agents | CRDT | Status |
| :--- | :--- | :--- | :---: | :---: | :---: |
EOF

# Pre-compile the PG release binary first to isolate compilation
echo "🔨 Compiling server binary in release mode..."
(cd "$REPO_ROOT/aether-rs" && cargo build --release --no-default-features --features db-postgres --quiet)
echo "   ✅ Pre-compilation completed."

# ==========================================
# 1. EXECUTE TIER 3
# ==========================================
echo "🚀 STARTING TIER 3 SWEEP..."
TIER3_SUCCESS=true

for run in "${TIER3_RUNS[@]}"; do
    lane=$(echo "$run" | cut -d'|' -f1)
    tier=$(echo "$run" | cut -d'|' -f2)
    agents=$(echo "$run" | cut -d'|' -f3)
    crdt=$(echo "$run" | cut -d'|' -f4)
    
    run_name="${lane}_${tier}"
    workspace_path="$WORKSPACES_ROOT/${run_name}"
    
    echo "=========================================================================="
    echo "🚀 RUNNING: Lane=$lane, Tier=$tier, Agents=$agents, CRDT=$crdt"
    echo "📂 Workspace: $workspace_path"
    echo "⏱️ Duration: $SOAK_RUN_SECONDS seconds"
    echo "=========================================================================="
    
    export AETHER_SOAK_HOME="$workspace_path"
    export SOAK_AGENTS="$agents"
    export SOAK_REPOS="$SOAK_REPOS"
    export SOAK_RUN_SECONDS="$SOAK_RUN_SECONDS"
    export SOAK_DRAIN_SECONDS="$SOAK_DRAIN_SECONDS"
    export SOAK_CRDT_ENABLED="$crdt"
    
    mkdir -p "$workspace_path"
    
    set +e
    "$SCRIPT_DIR/soak_native_rust_pg.sh" 2>&1 | tee "$workspace_path/run_execution.log"
    rc=$?
    set -e
    
    # Note: Control (crdt=0) is expected to fail under high contention due to unresolved 409s.
    # We only treat a run as a systemic/infrastructure failure if treatments (crdt=1, 2, 3) fail.
    if [ $rc -eq 0 ]; then
        status_text="PASSED ✅"
    else
        status_text="FAILED ❌ (Exit Code $rc)"
        if [ "$crdt" -ne 0 ]; then
            TIER3_SUCCESS=false # Systemic treatment failure blocks Tier 5
        fi
    fi
    
    echo "$status_text" > "$workspace_path/status.txt"
    echo "| $run_name | $lane | $tier | $agents | $crdt | $status_text |" >> "$REPORT_FILE"
    echo "=========================================================================="
    echo "🏁 FINISHED: Lane=$lane, Tier=$tier (Status: $status_text)"
    echo "=========================================================================="
    sleep 10
done

# ==========================================
# 2. CHECK GATING CRITERIA FOR TIER 5
# ==========================================
echo "=========================================================================="
echo "🛡️ EVALUATING GATING CRITERIA FOR TIER 5..."
echo "    Tier 3 Treatment Success Status: $TIER3_SUCCESS"

# Double check that db poller logs exist and DB sizes stayed in check
DB_SIZE_OK=true
for run in "${TIER3_RUNS[@]}"; do
    lane=$(echo "$run" | cut -d'|' -f1)
    tier=$(echo "$run" | cut -d'|' -f2)
    workspace_path="$WORKSPACES_ROOT/${lane}_${tier}"
    if [ -f "$workspace_path/capacity_telemetry.log" ]; then
        # Check if the DB exceeded the safety budget
        DB_LIMIT_MB="${AETHER_DB_SIZE_LIMIT_MB:-1000}"
        PEAK_SIZE=$(cat "$workspace_path/capacity_telemetry.log" | grep -v "timestamp" | awk -F'|' '{print $2}' | sort -rn | head -n 1 || echo "0")
        if [ -n "$PEAK_SIZE" ] && [ "$PEAK_SIZE" -gt "$DB_LIMIT_MB" ]; then
            echo "    ⚠️ Warning: $lane database footprint exceeded $DB_LIMIT_MB MB ($PEAK_SIZE MB)."
            DB_SIZE_OK=false
        fi
    fi
done

if [ "$TIER3_SUCCESS" = true ] && [ "$DB_SIZE_OK" = true ]; then
    echo "    🟢 GATING CRITERIA PASSED. Proceeding to Tier 5 stress test..."
    echo "=========================================================================="
    
    # Run Tier 5
    for run in "${TIER5_RUNS[@]}"; do
        lane=$(echo "$run" | cut -d'|' -f1)
        tier=$(echo "$run" | cut -d'|' -f2)
        agents=$(echo "$run" | cut -d'|' -f3)
        crdt=$(echo "$run" | cut -d'|' -f4)
        
        run_name="${lane}_${tier}"
        workspace_path="$WORKSPACES_ROOT/${run_name}"
        
        echo "=========================================================================="
        echo "🚀 RUNNING: Lane=$lane, Tier=$tier, Agents=$agents, CRDT=$crdt"
        echo "📂 Workspace: $workspace_path"
        echo "⏱️ Duration: $SOAK_RUN_SECONDS seconds"
        echo "=========================================================================="
        
        export AETHER_SOAK_HOME="$workspace_path"
        export SOAK_AGENTS="$agents"
        export SOAK_REPOS="$SOAK_REPOS"
        export SOAK_RUN_SECONDS="$SOAK_RUN_SECONDS"
        export SOAK_DRAIN_SECONDS="$SOAK_DRAIN_SECONDS"
        export SOAK_CRDT_ENABLED="$crdt"
        
        mkdir -p "$workspace_path"
        
        set +e
        "$SCRIPT_DIR/soak_native_rust_pg.sh" 2>&1 | tee "$workspace_path/run_execution.log"
        rc=$?
        set -e
        
        if [ $rc -eq 0 ]; then
            status_text="PASSED ✅"
        else
            status_text="FAILED ❌ (Exit Code $rc)"
        fi
        
        echo "$status_text" > "$workspace_path/status.txt"
        echo "| $run_name | $lane | $tier | $agents | $crdt | $status_text |" >> "$REPORT_FILE"
        echo "=========================================================================="
        echo "🏁 FINISHED: Lane=$lane, Tier=$tier (Status: $status_text)"
        echo "=========================================================================="
        sleep 10
    done
else
    echo "    🔴 GATING CRITERIA FAILED OR DB Safeties Triggered. Skipping Tier 5 stress test..."
    echo "=========================================================================="
    echo -e "\n### Gating Outcome: Tier 5 Skipped\n" >> "$REPORT_FILE"
    echo -e "Tier 5 was gated and was NOT run because Tier 3 treatments experienced failure or exceeded database growth limits.\n" >> "$REPORT_FILE"
fi

# ==========================================
# 3. RUN TIMING ANALYSIS
# ==========================================
echo "⚙️ Running Timing Analysis for executed runs..."
echo -e "\n## Detailed Telemetry & Metrics Analysis\n" >> "$REPORT_FILE"

# Process Tier 3 Runs
for run in "${TIER3_RUNS[@]}"; do
    lane=$(echo "$run" | cut -d'|' -f1)
    tier=$(echo "$run" | cut -d'|' -f2)
    
    run_name="${lane}_${tier}"
    workspace_path="$WORKSPACES_ROOT/${run_name}"
    status_text=$(cat "$workspace_path/status.txt" 2>/dev/null || echo "UNKNOWN ❓")
    
    echo "Analyzing timings for $run_name..."
    echo -e "### Run: $run_name ($lane, $tier)\n" >> "$REPORT_FILE"
    echo -e "**Status:** $status_text\n" >> "$REPORT_FILE"
    echo -e "\`\`\`" >> "$REPORT_FILE"
    
    set +e
    python3 "$SCRIPT_DIR/analyze_soak_timings.py" --soak-home "$workspace_path" >> "$REPORT_FILE" 2>&1
    set -e
    
    echo -e "\`\`\`\n" >> "$REPORT_FILE"
    
    if [ -f "$workspace_path/capacity_telemetry.log" ]; then
        echo -e "**Host Capacity & Disk Telemetry (Sampling):**" >> "$REPORT_FILE"
        echo -e "\`\`\`" >> "$REPORT_FILE"
        cat "$workspace_path/capacity_telemetry.log" | tail -n 10 >> "$REPORT_FILE"
        echo -e "\`\`\`\n" >> "$REPORT_FILE"
    fi
    
    echo -e "---\n" >> "$REPORT_FILE"
done

# Process Tier 5 Runs conditionally
if [ "$TIER3_SUCCESS" = true ] && [ "$DB_SIZE_OK" = true ]; then
    for run in "${TIER5_RUNS[@]}"; do
        lane=$(echo "$run" | cut -d'|' -f1)
        tier=$(echo "$run" | cut -d'|' -f2)
        
        run_name="${lane}_${tier}"
        workspace_path="$WORKSPACES_ROOT/${run_name}"
        status_text=$(cat "$workspace_path/status.txt" 2>/dev/null || echo "UNKNOWN ❓")
        
        echo "Analyzing timings for $run_name..."
        echo -e "### Run: $run_name ($lane, $tier)\n" >> "$REPORT_FILE"
        echo -e "**Status:** $status_text\n" >> "$REPORT_FILE"
        echo -e "\`\`\`" >> "$REPORT_FILE"
        
        set +e
        python3 "$SCRIPT_DIR/analyze_soak_timings.py" --soak-home "$workspace_path" >> "$REPORT_FILE" 2>&1
        set -e
        
        echo -e "\`\`\`\n" >> "$REPORT_FILE"
        
        if [ -f "$workspace_path/capacity_telemetry.log" ]; then
            echo -e "**Host Capacity & Disk Telemetry (Sampling):**" >> "$REPORT_FILE"
            echo -e "\`\`\`" >> "$REPORT_FILE"
            cat "$workspace_path/capacity_telemetry.log" | tail -n 10 >> "$REPORT_FILE"
            echo -e "\`\`\`\n" >> "$REPORT_FILE"
        fi
        
        echo -e "---\n" >> "$REPORT_FILE"
    done
fi

echo "✨ Overnight two-tier probe sweep completed! Results written to: $REPORT_FILE"
