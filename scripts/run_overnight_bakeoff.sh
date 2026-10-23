#!/usr/bin/env bash
# ============================================================================
# Overnight 20-Hour Bake-off Capacity Sweep Runner (Tiers 1-5)
# ============================================================================
# Runs 20 tiers of the Postgres CAS vs Advisory-Lock vs Phase 3 vs Phase 4 sweep.
# Keeps the MacBook awake using caffeinate.
# Saves isolated results and runs analyze_soak_timings.py to create
# docs/sprint4d_bakeoff_results.md.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Global configurations
SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-3600}" # 1 hour per run (3600s)
SOAK_DRAIN_SECONDS="${SOAK_DRAIN_SECONDS:-60}"
SOAK_REPOS=10

# ── Blocker 1: New Isolated Sprint 4D Summary Report Path ─────────────────────
REPORT_FILE="$REPO_ROOT/docs/sprint4d_bakeoff_results.md"

# New Isolated Sprint 4D Workspace Root
WORKSPACES_ROOT="$HOME/.aether/soak_pg_sprint4d_overnight"
mkdir -p "$WORKSPACES_ROOT"

# ── Blocker 2: Hard-Enforced File Descriptor limits pre-flight ────────────────
echo "⚙️ Running host pre-flight verification..."

# A. Enforce process file descriptor limits (ulimit)
echo "   Verifying file descriptor limits..."
CURRENT_FD_LIMIT=$(ulimit -n || echo "0")

if [ "$CURRENT_FD_LIMIT" = "unlimited" ]; then
    echo "   ✅ File descriptor limit is unlimited."
elif [ "$CURRENT_FD_LIMIT" = "0" ]; then
    echo "❌ PREFLIGHT ERROR: Failed to query current file descriptor limits."
    exit 1
elif [ "$CURRENT_FD_LIMIT" -lt 65536 ]; then
    echo "   Current limit ($CURRENT_FD_LIMIT) is less than 65536. Attempting to set limit to 65536..."
    ulimit -n 65536 || {
        echo "❌ PREFLIGHT ERROR: Failed to set file descriptor limit (ulimit -n 65536)."
        echo "   High-density sweeps (up to 160 concurrent agents) require at least 65536 descriptors."
        exit 1
    }
    echo "   ✅ File descriptor limit successfully set to 65536."
else
    echo "   ✅ File descriptor limit is $CURRENT_FD_LIMIT (>= 65536)."
fi

# B. Enforce pre-flight host free-space threshold (at least 15 GB)
echo "   Verifying host disk space..."
FREE_SPACE=$(df -m "$WORKSPACES_ROOT" | tail -n 1 | awk '{print $4}')
if [[ ! "$FREE_SPACE" =~ ^[0-9]+$ ]]; then
    FREE_SPACE=0
fi
if [ "$FREE_SPACE" -lt 15360 ]; then
    echo "❌ PREFLIGHT ERROR: Host available free space ($FREE_SPACE MB) is less than 15360 MB (15 GB)."
    echo "   High-density sequential sweeps require at least 15 GB of free space."
    exit 1
fi
echo "   ✅ Host free space is $FREE_SPACE MB (>= 15 GB)."

# C. PostgreSQL Connection parameters
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"

# C. Query and assert PostgreSQL max_connections
echo "   Verifying PostgreSQL max_connections configuration..."
if ! command -v psql &> /dev/null; then
    echo "❌ PREFLIGHT ERROR: 'psql' utility not found on PATH. Cannot verify PostgreSQL connection."
    exit 1
fi

MAX_CONN=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -t -A -c "SHOW max_connections;" 2>/dev/null || echo "0")

if [ "$MAX_CONN" -eq 0 ]; then
    echo "❌ PREFLIGHT ERROR: Failed to connect to PostgreSQL on $PG_HOST:$PG_PORT. Is the server running?"
    exit 1
fi

echo "   PostgreSQL max_connections detected: $MAX_CONN"
if [ "$MAX_CONN" -lt 250 ]; then
    echo "❌ PREFLIGHT ERROR: Postgres max_connections ($MAX_CONN) is less than 250."
    echo "   Running high-density Tier 4/5 (up to 160 agents) requires at least 250 connections to prevent pool starvation."
    echo "   Please adjust max_connections in postgresql.conf and restart Postgres."
    exit 1
fi
echo "   ✅ Pre-flight checks passed."

# Ensure caffeinate is running to keep the Mac awake
echo "☕ Starting caffeinate to prevent sleep during the 20-hour sweep..."
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

# Define the runs (Lane, Tier, Agents, CRDT)
# Run formats: LANE_NAME|TIER_NAME|AGENTS|CRDT_ENABLED
RUNS=(
    "control|tier1|10|0"
    "control|tier2|20|0"
    "control|tier3|40|0"
    "control|tier4|80|0"
    "control|tier5|160|0"
    "advisory_lock|tier1|10|1"
    "advisory_lock|tier2|20|1"
    "advisory_lock|tier3|40|1"
    "advisory_lock|tier4|80|1"
    "advisory_lock|tier5|160|1"
    "phase3_frontiers|tier1|10|2"
    "phase3_frontiers|tier2|20|2"
    "phase3_frontiers|tier3|40|2"
    "phase3_frontiers|tier4|80|2"
    "phase3_frontiers|tier5|160|2"
    "phase4_token|tier1|10|3"
    "phase4_token|tier2|20|3"
    "phase4_token|tier3|40|3"
    "phase4_token|tier4|80|3"
    "phase4_token|tier5|160|3"
)

echo "📊 Preparing consolidated report file: $REPORT_FILE"
mkdir -p "$(dirname "$REPORT_FILE")"

SWEEP_TITLE="Sprint 4D Comparative 4-Lane Bake-off Capacity Sweep Results"
if [ "${SOAK_RUN_SECONDS}" -lt 600 ]; then
    SWEEP_TITLE="Sprint 4D Comparative 4-Lane Pre-Flight Shakeout Sweep Results"
fi

cat << EOF > "$REPORT_FILE"
# ${SWEEP_TITLE}

**Start Time:** $(date)  
**Matrix:** 10 Repositories, ${SOAK_RUN_SECONDS} seconds run duration per tier.  
**System Configuration:** ulimit -n $(ulimit -n), max_connections = $MAX_CONN.

---

## Run Configurations & Status

| Run | Lane | Tier | Agents | CRDT | Status |
| :--- | :--- | :--- | :---: | :---: | :---: |
EOF

for run in "${RUNS[@]}"; do
    # Manual splitting compatible with all bash versions
    lane=$(echo "$run" | cut -d'|' -f1)
    tier=$(echo "$run" | cut -d'|' -f2)
    agents=$(echo "$run" | cut -d'|' -f3)
    crdt=$(echo "$run" | cut -d'|' -f4)
    
    run_name="${lane}_${tier}"
    workspace_path="$WORKSPACES_ROOT/${run_name}"
    
    echo "=========================================================================="
    echo "🚀 STARTING OVERNIGHT RUN: Lane=$lane, Tier=$tier, Agents=$agents, CRDT=$crdt"
    echo "📂 Workspace: $workspace_path"
    echo "⏱️ Duration: $SOAK_RUN_SECONDS seconds (+ $SOAK_DRAIN_SECONDS seconds drain)"
    echo "=========================================================================="
    
    # Run the soak
    export AETHER_SOAK_HOME="$workspace_path"
    export SOAK_AGENTS="$agents"
    export SOAK_REPOS="$SOAK_REPOS"
    export SOAK_RUN_SECONDS="$SOAK_RUN_SECONDS"
    export SOAK_DRAIN_SECONDS="$SOAK_DRAIN_SECONDS"
    export SOAK_CRDT_ENABLED="$crdt"
    
    # Create the workspace folder beforehand
    mkdir -p "$workspace_path"
    
    # Run soak script and don't exit if it fails (it's expected to fail Control under high loads due to 409s)
    set +e
    "$SCRIPT_DIR/soak_native_rust_pg.sh" 2>&1 | tee "$workspace_path/run_execution.log"
    rc=$?
    set -e
    
    if [ $rc -eq 0 ]; then
        status_text="PASSED ✅"
    else
        status_text="FAILED ❌ (Exit Code $rc)"
    fi
    
    # Persist status in the workspace
    echo "$status_text" > "$workspace_path/status.txt"
    
    echo "| $run_name | $lane | $tier | $agents | $crdt | $status_text |" >> "$REPORT_FILE"
    echo "=========================================================================="
    echo "🏁 FINISHED OVERNIGHT RUN: Lane=$lane, Tier=$tier (Status: $status_text)"
    echo "=========================================================================="
    sleep 10
done

echo "⚙️ Running Timing Analysis for all runs..."
echo -e "\n## Detailed Telemetry & Metrics Analysis\n" >> "$REPORT_FILE"

for run in "${RUNS[@]}"; do
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

echo "✨ Sweep completed! Consolidated report compiled at: $REPORT_FILE"
