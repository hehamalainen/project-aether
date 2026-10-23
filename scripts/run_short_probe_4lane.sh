#!/usr/bin/env bash
# ============================================================================
# Short 15-20 Minute 4-Lane Pre-Flight Bake-off Probe (Tonight's Action Script)
# ============================================================================
# Runs a short 4-lane Tier 1 sweep to verify dynamic projected snapshot merges,
# token-mode RGA convergence, and anti-bias timing telemetry in PostgreSQL.
# Keeps the MacBook awake using caffeinate and compiles timings results.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Global configurations for the short probe
export SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-120}" # 2 minutes per run (fast probe)
export SOAK_DRAIN_SECONDS="${SOAK_DRAIN_SECONDS:-15}" # 15 seconds drain
export SOAK_REPOS=10

# Summary Report Path
REPORT_FILE="$REPO_ROOT/docs/sprint4d_bakeoff_results.md"

# Isolated Workspace Root
WORKSPACES_ROOT="$HOME/.aether/soak_pg_sprint4d_probe"
rm -rf "$WORKSPACES_ROOT"
mkdir -p "$WORKSPACES_ROOT"

echo "⚙️ Running host pre-flight verification..."

# A. Verify file descriptor limits
CURRENT_FD_LIMIT=$(ulimit -n || echo "0")
if [ "$CURRENT_FD_LIMIT" = "unlimited" ]; then
    echo "   ✅ File descriptor limit is unlimited."
elif [ "$CURRENT_FD_LIMIT" -lt 65536 ]; then
    ulimit -n 65536 || {
        echo "⚠️ WARNING: Failed to set ulimit to 65536. Proceeding anyway for light Tier 1 probe..."
    }
else
    echo "   ✅ File descriptor limit is $CURRENT_FD_LIMIT."
fi

# B. Verify host free-space
FREE_SPACE=$(df -m "$REPO_ROOT" | tail -n 1 | awk '{print $4}')
if [[ ! "$FREE_SPACE" =~ ^[0-9]+$ ]]; then
    FREE_SPACE=0
fi
if [ "$FREE_SPACE" -lt 15360 ]; then
    echo "⚠️ WARNING: Host free space is $FREE_SPACE MB (< 15 GB). Proceeding since this is a short, light probe..."
else
    echo "   ✅ Host free space is $FREE_SPACE MB."
fi

# C. PostgreSQL Connection parameters
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"

# D. Query max_connections
MAX_CONN=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -t -A -c "SHOW max_connections;" 2>/dev/null || echo "0")
if [ "$MAX_CONN" -eq 0 ]; then
    echo "❌ PREFLIGHT ERROR: Failed to connect to PostgreSQL on $PG_HOST:$PG_PORT."
    exit 1
fi
echo "   ✅ Connected to PostgreSQL (max_connections: $MAX_CONN)."

# Ensure caffeinate keeps Mac awake
echo "☕ Starting caffeinate..."
/usr/bin/caffeinate -dimsu &
CAFFEINATE_PID=$!

cleanup() {
    echo "🧹 Cleaning up probe runner..."
    if kill -0 "$CAFFEINATE_PID" >/dev/null 2>&1; then
        kill "$CAFFEINATE_PID" >/dev/null 2>&1 || true
        wait "$CAFFEINATE_PID" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

# Define the 4 lanes to probe at Tier 1 (10 agents, 10 repos)
RUNS=(
    "control|tier1|10|0"
    "advisory_lock|tier1|10|1"
    "phase3_frontiers|tier1|10|2"
    "phase4_token|tier1|10|3"
)

echo "📊 Preparing consolidated report file: $REPORT_FILE"
mkdir -p "$(dirname "$REPORT_FILE")"

cat << EOF > "$REPORT_FILE"
# Sprint 4D Comparative 4-Lane Pre-Flight Bake-off Probe Results

**Start Time:** $(date)  
**Matrix:** 10 Repositories, ${SOAK_RUN_SECONDS} seconds run duration per lane (Tier 1).  
**System Configuration:** ulimit -n $(ulimit -n), max_connections = $MAX_CONN.

---

## Run Configurations & Status

| Run | Lane | Tier | Agents | CRDT | Status |
| :--- | :--- | :--- | :---: | :---: | :---: |
EOF

# Pre-compile the PG release binary first so it is fast and isolated
echo "🔨 Compiling server binary in release mode..."
(cd "$REPO_ROOT/aether-rs" && cargo build --release --no-default-features --features db-postgres --quiet)
echo "   ✅ Pre-compilation completed."

for run in "${RUNS[@]}"; do
    lane=$(echo "$run" | cut -d'|' -f1)
    tier=$(echo "$run" | cut -d'|' -f2)
    agents=$(echo "$run" | cut -d'|' -f3)
    crdt=$(echo "$run" | cut -d'|' -f4)
    
    run_name="${lane}_${tier}"
    workspace_path="$WORKSPACES_ROOT/${run_name}"
    
    echo "=========================================================================="
    echo "🚀 STARTING PROBE RUN: Lane=$lane, Tier=$tier, Agents=$agents, CRDT=$crdt"
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
    echo "🏁 FINISHED PROBE RUN: Lane=$lane, Tier=$tier (Status: $status_text)"
    echo "=========================================================================="
    sleep 5
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

echo "✨ Probe sweep completed successfully! Results written to: $REPORT_FILE"
