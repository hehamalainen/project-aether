#!/usr/bin/env bash
# ============================================================================
# Native Rust Soak Driver
#
# Exercises the full Rust native promotion path under sustained concurrent load:
#   POST /v1/repos/{id}/proposals   → Rust submit (intake + trust)
#   POST /v1/repos/{id}/promotions  → Rust native promotion (orchestrate +
#                                     gates + trust + CAS + head advance)
#
# No Python in the critical path. Every promotion flows through Rust.
#
# Usage:
#   ./scripts/soak_native_rust.sh                    # canary (5 agents, 2 min)
#   SOAK_AGENTS=20 SOAK_RUN_SECONDS=600 ./scripts/soak_native_rust.sh  # medium
#   SOAK_AGENTS=50 SOAK_RUN_SECONDS=3600 ./scripts/soak_native_rust.sh # baseline
#
# Prerequisites:
#   - aether-rs release binary built (cargo build --release)
#   - sqlite3 available on PATH
#   - curl available on PATH
#   - jq available on PATH
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configurable parameters ─────────────────────────────────────────────────
SOAK_AGENTS="${SOAK_AGENTS:-5}"
SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-120}"
SOAK_INTERVAL_MS="${SOAK_INTERVAL_MS:-1000}"
SOAK_WITH_GATES="${SOAK_WITH_GATES:-1}"
SOAK_HOME="${AETHER_SOAK_HOME:-$PROJECT_DIR/.soak_workspace}"
BASE_URL="http://127.0.0.1:8100"
SERVER_BINARY="$PROJECT_DIR/aether-rs/target/release/server"

# ── Derived paths ───────────────────────────────────────────────────────────
SOAK_DB="$SOAK_HOME/.aether/aether.db"
SOAK_DB_URL="sqlite://$SOAK_DB"
SOAK_STORAGE="$SOAK_HOME/.aether/aether_storage"
SERVER_LOG="$SOAK_HOME/server.log"
RESULTS_DIR="$SOAK_HOME/results"

# ── State ───────────────────────────────────────────────────────────────────
SERVER_PID=""
AGENT_PIDS=()
REPO_ID=""

# Counters (written to temp files by agents, aggregated at end)
COUNTER_DIR=""

# ── Cleanup ─────────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."

    # Kill agent loops
    if [[ ${#AGENT_PIDS[@]} -gt 0 ]]; then
        for pid in "${AGENT_PIDS[@]}"; do
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        done
    fi

    # Kill server
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ── Preflight checks ───────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║           Aether Native Rust Soak Driver                       ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Agents:       $SOAK_AGENTS"
echo "  Duration:     ${SOAK_RUN_SECONDS}s"
echo "  Interval:     ${SOAK_INTERVAL_MS}ms per agent"
echo "  Gates:        $([ "$SOAK_WITH_GATES" = "1" ] && echo "enabled" || echo "disabled")"
echo "  Soak home:    $SOAK_HOME"
echo ""

if [[ ! -f "$SERVER_BINARY" ]]; then
    echo "❌ Release binary not found at $SERVER_BINARY"
    echo "   Run: cargo build --manifest-path aether-rs/Cargo.toml --release"
    exit 1
fi

for cmd in sqlite3 curl jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required command '$cmd' not found on PATH"
        exit 1
    fi
done

# ── Setup isolated workspace ───────────────────────────────────────────────
echo "📁 Setting up isolated workspace at $SOAK_HOME"
rm -rf "$SOAK_HOME"
mkdir -p "$SOAK_HOME/.aether" "$RESULTS_DIR"

COUNTER_DIR="$(mktemp -d "$SOAK_HOME/counters.XXXXXX")"

# ── Start Rust server with isolated HOME ────────────────────────────────────
echo "🚀 Starting Rust server (release build, isolated HOME)..."

HOME="$SOAK_HOME" \
AETHER_DB_PATH="$SOAK_DB_URL" \
RUST_LOG="server=info" \
"$SERVER_BINARY" > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "   PID: $SERVER_PID"
echo "   DB:  $SOAK_DB"
echo "   Log: $SERVER_LOG"

# Wait for health check
echo -n "   Waiting for health..."
for i in $(seq 1 30); do
    if curl -sf "$BASE_URL/v1/health" >/dev/null 2>&1; then
        echo " ✅ healthy (${i}s)"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo " ❌ server exited prematurely"
        echo "   Server log (last 20 lines):"
        tail -20 "$SERVER_LOG" 2>/dev/null || true
        exit 1
    fi
    sleep 1
    echo -n "."
done

if ! curl -sf "$BASE_URL/v1/health" >/dev/null 2>&1; then
    echo " ❌ health check failed after 30s"
    tail -20 "$SERVER_LOG" 2>/dev/null || true
    exit 1
fi

# ── Create repo ─────────────────────────────────────────────────────────────
echo "📦 Creating soak repository..."
CREATE_RESPONSE=$(curl -sf -X POST "$BASE_URL/v1/repos" \
    -H "Content-Type: application/json" \
    -d '{"name": "native-soak-test"}')
REPO_ID=$(echo "$CREATE_RESPONSE" | jq -r '.id')

if [[ -z "$REPO_ID" || "$REPO_ID" == "null" ]]; then
    echo "❌ Failed to create repo"
    echo "   Response: $CREATE_RESPONSE"
    exit 1
fi
echo "   Repo ID: $REPO_ID"

# ── Bake gate_config into the repo ──────────────────────────────────────────
if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    echo "🔒 Setting gate_config on repo..."
    GATE_CONFIG='[{"name":"soak_gate","command":"echo SOAK_GATE_OK","required":true,"timeout_seconds":10}]'
    sqlite3 "$SOAK_DB" "UPDATE aether_repositories SET gate_config = '$GATE_CONFIG' WHERE id = '$REPO_ID';"
    VERIFY_GATE=$(sqlite3 "$SOAK_DB" "SELECT gate_config FROM aether_repositories WHERE id = '$REPO_ID';")
    if [[ -z "$VERIFY_GATE" || "$VERIFY_GATE" == "" ]]; then
        echo "❌ gate_config not set"
        exit 1
    fi
    echo "   Gate config: $VERIFY_GATE"
fi

# ── Agent loop function ────────────────────────────────────────────────────
run_agent() {
    local agent_id="$1"
    local agent_counter_dir="$2"
    local interval_s
    interval_s=$(echo "scale=3; $SOAK_INTERVAL_MS / 1000" | bc)

    local submit_ok=0
    local submit_fail=0
    local promote_ok=0
    local promote_contention=0
    local promote_rejected=0
    local promote_5xx=0
    local seq=0

    local end_time=$((SECONDS + SOAK_RUN_SECONDS))

    while [[ $SECONDS -lt $end_time ]]; do
        seq=$((seq + 1))
        local file_path="src/agent_${agent_id}_file.py"
        local content="\"\"\"Soak module: ${file_path}\"\"\"
# agent ${agent_id} tick ${seq}
value_${seq} = ${seq}
"

        # ── Submit proposal ────────────────────────────────────────────
        local submit_response
        local submit_http
        submit_response=$(curl -s -w "\n%{http_code}" -X POST \
            "$BASE_URL/v1/repos/$REPO_ID/proposals" \
            -H "Content-Type: application/json" \
            -d "{
                \"agent_id\": \"soak_agent_${agent_id}\",
                \"task_id\": \"soak-${agent_id}-${seq}\",
                \"source\": \"native_soak\",
                \"intent_type\": \"soak_update\",
                \"change_type\": \"full_file\",
                \"change_content\": $(echo "$content" | jq -Rs .),
                \"affected_paths\": [\"${file_path}\"],
                \"confidence_score\": 0.85,
                \"rationale\": \"native soak tick ${seq}\"
            }" 2>/dev/null) || true

        submit_http=$(echo "$submit_response" | tail -1)
        local submit_body
        submit_body=$(echo "$submit_response" | sed '$d')

        if [[ "$submit_http" == "200" ]]; then
            submit_ok=$((submit_ok + 1))
        else
            submit_fail=$((submit_fail + 1))
            sleep "$interval_s"
            continue
        fi

        local prop_id
        prop_id=$(echo "$submit_body" | jq -r '.proposal_id // empty' 2>/dev/null) || true
        if [[ -z "$prop_id" ]]; then
            submit_fail=$((submit_fail + 1))
            sleep "$interval_s"
            continue
        fi

        # ── Promote proposal (native Rust path) ───────────────────────
        local promote_response
        local promote_http
        promote_response=$(curl -s -w "\n%{http_code}" -X POST \
            "$BASE_URL/v1/repos/$REPO_ID/promotions" \
            -H "Content-Type: application/json" \
            -d "{\"proposal_ids\": [\"${prop_id}\"]}" 2>/dev/null) || true

        promote_http=$(echo "$promote_response" | tail -1)
        local promote_body
        promote_body=$(echo "$promote_response" | sed '$d')

        if [[ "$promote_http" == "200" ]]; then
            local success
            success=$(echo "$promote_body" | jq -r '.success // false' 2>/dev/null) || true
            if [[ "$success" == "true" ]]; then
                promote_ok=$((promote_ok + 1))
            else
                # 200 but success:false means gates failed or policy rejected
                promote_rejected=$((promote_rejected + 1))
            fi
        elif [[ "$promote_http" == "409" ]]; then
            # Expected CAS contention under concurrent load — not a bug
            promote_contention=$((promote_contention + 1))
        elif [[ "$promote_http" -ge 500 ]]; then
            # Hard failure
            promote_5xx=$((promote_5xx + 1))
        else
            # 400-level errors other than 409
            promote_rejected=$((promote_rejected + 1))
        fi

        sleep "$interval_s"
    done

    # Write counters to files for aggregation
    echo "$submit_ok" > "$agent_counter_dir/submit_ok"
    echo "$submit_fail" > "$agent_counter_dir/submit_fail"
    echo "$promote_ok" > "$agent_counter_dir/promote_ok"
    echo "$promote_contention" > "$agent_counter_dir/promote_contention"
    echo "$promote_rejected" > "$agent_counter_dir/promote_rejected"
    echo "$promote_5xx" > "$agent_counter_dir/promote_5xx"
}

# ── Launch agent loops ──────────────────────────────────────────────────────
echo ""
echo "🏃 Launching $SOAK_AGENTS concurrent agent loops for ${SOAK_RUN_SECONDS}s..."
echo ""

START_EPOCH=$(date +%s)

for i in $(seq 1 "$SOAK_AGENTS"); do
    agent_dir="$COUNTER_DIR/agent_$i"
    mkdir -p "$agent_dir"
    run_agent "$i" "$agent_dir" &
    AGENT_PIDS+=($!)
done

# ── Wait for agents with progress ──────────────────────────────────────────
echo -n "   Running"
while true; do
    alive=0
    for pid in "${AGENT_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            alive=$((alive + 1))
        fi
    done
    if [[ $alive -eq 0 ]]; then
        break
    fi
    elapsed=$(($(date +%s) - START_EPOCH))
    echo -ne "\r   Running... ${elapsed}s / ${SOAK_RUN_SECONDS}s (${alive} agents alive)  "
    sleep 5
done
echo ""
echo "   ✅ All agents completed"

# Wait for all to finish
for pid in "${AGENT_PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
done
AGENT_PIDS=()

END_EPOCH=$(date +%s)
ELAPSED=$((END_EPOCH - START_EPOCH))

# ── Aggregate counters ──────────────────────────────────────────────────────
echo ""
echo "📊 Aggregating results..."

total_submit_ok=0
total_submit_fail=0
total_promote_ok=0
total_promote_contention=0
total_promote_rejected=0
total_promote_5xx=0

for i in $(seq 1 "$SOAK_AGENTS"); do
    d="$COUNTER_DIR/agent_$i"
    total_submit_ok=$((total_submit_ok + $(cat "$d/submit_ok" 2>/dev/null || echo 0)))
    total_submit_fail=$((total_submit_fail + $(cat "$d/submit_fail" 2>/dev/null || echo 0)))
    total_promote_ok=$((total_promote_ok + $(cat "$d/promote_ok" 2>/dev/null || echo 0)))
    total_promote_contention=$((total_promote_contention + $(cat "$d/promote_contention" 2>/dev/null || echo 0)))
    total_promote_rejected=$((total_promote_rejected + $(cat "$d/promote_rejected" 2>/dev/null || echo 0)))
    total_promote_5xx=$((total_promote_5xx + $(cat "$d/promote_5xx" 2>/dev/null || echo 0)))
done

# ── Check server is still alive ─────────────────────────────────────────────
server_alive="yes"
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    server_alive="no"
fi

# ── Post-run DB audit queries ───────────────────────────────────────────────
echo ""
echo "🔍 Post-run audit..."

head_seq=$(sqlite3 "$SOAK_DB" "SELECT head_sequence FROM aether_repositories WHERE id = '$REPO_ID';" 2>/dev/null || echo "0")
promo_count=$(sqlite3 "$SOAK_DB" "SELECT COUNT(*) FROM aether_promotions;" 2>/dev/null || echo "0")
snapshot_count=$(sqlite3 "$SOAK_DB" "SELECT COUNT(*) FROM aether_snapshots;" 2>/dev/null || echo "0")
proposal_status=$(sqlite3 "$SOAK_DB" "SELECT status, COUNT(*) FROM aether_proposals GROUP BY status;" 2>/dev/null || echo "unknown")
trust_count=$(sqlite3 "$SOAK_DB" "SELECT COUNT(*) FROM aether_agent_metrics;" 2>/dev/null || echo "0")
trust_avg=$(sqlite3 "$SOAK_DB" "SELECT printf('alpha=%.2f beta=%.2f', AVG(trust_alpha), AVG(trust_beta)) FROM aether_agent_metrics;" 2>/dev/null || echo "n/a")

# Tight gate-result audit: every promoted snapshot must have at least one gate row
if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    promotions_missing_gates=$(sqlite3 "$SOAK_DB" "
        SELECT COUNT(*) FROM aether_promotions p
        JOIN aether_heads h ON h.promotion_id = p.id
        LEFT JOIN aether_gate_results g ON g.snapshot_id = h.snapshot_id
        WHERE g.id IS NULL;
    " 2>/dev/null || echo "-1")
else
    promotions_missing_gates="0"
fi

# Snapshot/head integrity
head_integrity=$(sqlite3 "$SOAK_DB" "
    SELECT CASE
        WHEN r.head_id IS NULL THEN 'no_head'
        WHEN h.id IS NULL THEN 'HEAD_MISSING'
        WHEN s.id IS NULL THEN 'SNAPSHOT_MISSING'
        ELSE 'ok'
    END
    FROM aether_repositories r
    LEFT JOIN aether_heads h ON h.id = r.head_id
    LEFT JOIN aether_snapshots s ON s.id = h.snapshot_id
    WHERE r.id = '$REPO_ID';
" 2>/dev/null || echo "query_failed")

# Orphaned snapshots on disk vs DB
disk_snapshots=$(find "$SOAK_STORAGE" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | wc -l | tr -d ' ')
db_snapshots=$(sqlite3 "$SOAK_DB" "SELECT COUNT(*) FROM aether_snapshots;" 2>/dev/null || echo "0")

# ── Print summary ───────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║                    SOAK RESULTS SUMMARY                        ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Duration:              ${ELAPSED}s (target: ${SOAK_RUN_SECONDS}s)"
echo "  Agents:                $SOAK_AGENTS"
echo "  Server alive:          $server_alive"
echo ""
echo "  ── Submit ─────────────────────────────────────"
echo "  Submit OK:             $total_submit_ok"
echo "  Submit Fail:           $total_submit_fail"
echo "  Submit TPS:            $(echo "scale=2; $total_submit_ok / $ELAPSED" | bc 2>/dev/null || echo "n/a")"
echo ""
echo "  ── Promote (native /promotions) ──────────────"
echo "  Promote OK:            $total_promote_ok"
echo "  Promote Contention:    $total_promote_contention (409, expected under concurrency)"
echo "  Promote Rejected:      $total_promote_rejected (policy/gate reject)"
echo "  Promote 5xx:           $total_promote_5xx"
echo "  Promote TPS:           $(echo "scale=2; $total_promote_ok / $ELAPSED" | bc 2>/dev/null || echo "n/a")"
echo ""
echo "  ── Database Audit ────────────────────────────"
echo "  Head sequence:         $head_seq"
echo "  Promotions (DB):       $promo_count"
echo "  Snapshots (DB):        $snapshot_count"
echo "  Snapshots (disk):      $disk_snapshots"
echo "  Trust agents:          $trust_count ($trust_avg)"
echo "  Proposals by status:   $proposal_status"
echo "  Head integrity:        $head_integrity"
if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    echo "  Promotions w/o gates:  $promotions_missing_gates"
fi
echo ""

# ── Pass/Fail verdict ───────────────────────────────────────────────────────
FAILURES=0

check() {
    local label="$1" condition="$2"
    if eval "$condition"; then
        echo "  ✅ $label"
    else
        echo "  ❌ $label"
        FAILURES=$((FAILURES + 1))
    fi
}

# Compute failure rates for threshold-based checks
total_submit=$((total_submit_ok + total_submit_fail))
if [[ $total_submit -gt 0 ]]; then
    submit_fail_rate=$(echo "scale=4; $total_submit_fail / $total_submit" | bc 2>/dev/null || echo "1")
else
    submit_fail_rate="1"
fi
total_promote_attempts=$((total_promote_ok + total_promote_contention + total_promote_rejected + total_promote_5xx))
if [[ $total_promote_attempts -gt 0 ]]; then
    promote_5xx_rate=$(echo "scale=4; $total_promote_5xx / $total_promote_attempts" | bc 2>/dev/null || echo "1")
else
    promote_5xx_rate="1"
fi

echo "  ── Verdict ─────────────────────────────────────"
check "Server alive at end"           '[[ "$server_alive" == "yes" ]]'
check "Head sequence > 0"             '[[ "$head_seq" -gt 0 ]]'
check "Promotions created"            '[[ "$promo_count" -gt 0 ]]'
check "Submit failure rate < 20%"     '(( $(echo "$submit_fail_rate < 0.20" | bc -l) ))'
check "Promote 5xx rate < 10%"        '(( $(echo "$promote_5xx_rate < 0.10" | bc -l) ))'
check "Trust metrics recorded"        '[[ "$trust_count" -gt 0 ]]'
check "Head/snapshot integrity"       '[[ "$head_integrity" == "ok" ]]'
check "No orphaned disk snapshots"    '[[ "$disk_snapshots" -le "$db_snapshots" ]]'

if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    check "All promotions have gate results" '[[ "$promotions_missing_gates" -eq 0 ]]'
fi

echo ""
if [[ "$FAILURES" -gt 0 ]]; then
    echo "  ❌ SOAK FAILED ($FAILURES check(s) failed)"
    echo ""
    echo "  Server log tail:"
    tail -30 "$SERVER_LOG" 2>/dev/null || true
    exit 1
else
    echo "  ✅ SOAK PASSED"
    exit 0
fi
