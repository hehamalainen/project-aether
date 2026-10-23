#!/usr/bin/env bash
# ============================================================================
# Phase 1A: Rust External Compatibility Smoke Test
#
# Validates that the Python swarm harness works correctly against the Rust
# server via the /commit_promotion external path under sustained load.
#
# Architecture:
#   - Python harness bootstraps repo/head locally via SQLModel services
#   - Proposal submits go over HTTP → Rust POST /v1/repos/{id}/proposals
#   - Promotion commits go over HTTP → Rust POST /v1/repos/{id}/commit_promotion
#   - Both processes share the same DB via HOME=$TMP_HOME
#
# Prerequisites:
#   - aether-rs workspace compiled (cargo build --workspace)
#   - Python venv with aether installed (pip install -e ".[dev]")
#   - No other process on port 8100
#
# Usage:
#   ./scripts/test_rust_compatibility_smoke.sh
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AETHER_RS_DIR="$PROJECT_DIR/aether-rs"
PYTHON="$PROJECT_DIR/.venv/bin/python"
BASE_URL="http://127.0.0.1:8100"
SERVER_PID=""
TMP_HOME=""
REPORT_JSON=""

# --- Tunables ---
RUN_SECONDS="${SMOKE_RUN_SECONDS:-60}"
NUM_AGENTS="${SMOKE_NUM_AGENTS:-10}"
PROMOTION_BATCH="${SMOKE_PROMOTION_BATCH:-30}"
MIN_SUBMIT_THROUGHPUT="${SMOKE_MIN_SUBMIT_THROUGHPUT:-1.0}"
MIN_PROMOTION_THROUGHPUT="${SMOKE_MIN_PROMOTION_THROUGHPUT:-0.1}"

cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        echo "  Stopped Rust server (PID: $SERVER_PID)"
    fi
    if [[ -n "$TMP_HOME" && -d "$TMP_HOME" ]]; then
        rm -rf "$TMP_HOME"
        echo "  Removed temp HOME: $TMP_HOME"
    fi
}
trap cleanup EXIT

echo "============================================"
echo "Phase 1A: Rust External Compatibility Smoke"
echo "============================================"
echo ""
echo "Config:"
echo "  Run seconds:     $RUN_SECONDS"
echo "  Agents:          $NUM_AGENTS"
echo "  Promotion batch: $PROMOTION_BATCH"
echo ""

# --- 0. Verify venv ---
if [[ ! -x "$PYTHON" ]]; then
    echo "❌ Python venv not found at $PYTHON"
    echo "   Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
    exit 1
fi
echo "🐍 Using Python: $PYTHON"
echo ""

# --- 1. Build Rust server ---
echo "🔨 Building aether-rs..."
cd "$AETHER_RS_DIR"
cargo build --workspace --quiet 2>&1 || { echo "❌ Rust build failed"; exit 1; }
echo "  ✅ Build succeeded"
echo ""

# --- 2. Create isolated HOME ---
TMP_HOME=$(mktemp -d)
mkdir -p "$TMP_HOME/.aether"
echo "📁 Isolated HOME: $TMP_HOME"
echo ""

# --- 3. Start Rust server with shared HOME ---
# The Rust server resolves DB path as:
#   AETHER_DB_PATH || $HOME/.aether/aether.db  (main.rs:41-44)
# By setting HOME=$TMP_HOME, both Rust and Python use the same DB.
echo "🚀 Starting Rust server..."
HOME="$TMP_HOME" "$AETHER_RS_DIR/target/debug/server" > "$TMP_HOME/rust_server.log" 2>&1 &
SERVER_PID=$!

# Wait for server to be ready
MAX_WAIT=10
for i in $(seq 1 $MAX_WAIT); do
    if curl -sf "$BASE_URL/v1/health" > /dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "❌ Rust server crashed on startup. Log:"
        cat "$TMP_HOME/rust_server.log" | tail -20
        exit 1
    fi
    if [[ $i -eq $MAX_WAIT ]]; then
        echo "❌ Rust server did not become healthy in ${MAX_WAIT}s"
        cat "$TMP_HOME/rust_server.log" | tail -20
        exit 1
    fi
    sleep 1
done

echo "  ✅ Server healthy (PID: $SERVER_PID)"
echo ""

# --- 4. Run Python harness in external mode ---
# The Python harness CLI resolves DB path as:
#   db_path = Path.home() / ".aether" / "aether.db"  (main.py:995)
# Path.home() reads $HOME, so setting HOME=$TMP_HOME aligns it.
REPORT_JSON="$TMP_HOME/smoke_report.json"

echo "🏃 Running swarm harness (${RUN_SECONDS}s, ${NUM_AGENTS} agents)..."
cd "$PROJECT_DIR"

HOME="$TMP_HOME" "$PYTHON" -u -m apps.aether_cli.main swarm-harness \
    --agents "$NUM_AGENTS" \
    --files 20 \
    --run-seconds "$RUN_SECONDS" \
    --drain-seconds 15 \
    --contention low \
    --min-interval-ms 800 \
    --max-interval-ms 2000 \
    --inflight 50 \
    --promotion-batch "$PROMOTION_BATCH" \
    --promotion-interval-ms 200 \
    --target-mode external \
    --external-api-url "$BASE_URL" \
    --traffic-mix full_file \
    --submit-worker-threads 2 \
    --min-submit-throughput "$MIN_SUBMIT_THROUGHPUT" \
    --min-promotion-throughput "$MIN_PROMOTION_THROUGHPUT" \
    --max-submit-p95-ms 2000 \
    --max-backlog-end 5000 \
    --max-submit-failure-rate 0.10 \
    --output-json "$REPORT_JSON" \
    --quiet \
    --no-strict \
    2>&1 | tee "$TMP_HOME/harness.log"

echo ""

# --- 5. Copy report before cleanup can delete temp dir ---
LOCAL_REPORT="$PROJECT_DIR/smoke_report_latest.json"
LOCAL_SERVER_LOG="$PROJECT_DIR/smoke_server_latest.log"
if [[ -f "$REPORT_JSON" ]]; then
    cp "$REPORT_JSON" "$LOCAL_REPORT"
    echo "📄 Report copied to: $LOCAL_REPORT"
else
    echo "⚠️  Report JSON not found at $REPORT_JSON"
fi
if [[ -f "$TMP_HOME/rust_server.log" ]]; then
    cp "$TMP_HOME/rust_server.log" "$LOCAL_SERVER_LOG"
fi

# --- 6. Assert SLOs ---
echo ""
echo "📊 Checking SLOs..."
PASS=0
FAIL=0
TOTAL=0

if [[ ! -f "$LOCAL_REPORT" ]]; then
    echo "❌ Report JSON not available"
    exit 1
fi

# Extract metrics via Python with proper quoting
read_metric() {
    "$PYTHON" -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d['metrics'][sys.argv[2]])
" "$LOCAL_REPORT" "$1" 2>/dev/null || echo "0"
}

SUBMITTED_SUCCESS=$(read_metric submitted_success)
PROMOTIONS_CREATED=$(read_metric promotions_created)
SUBMIT_THROUGHPUT=$(read_metric submit_throughput)
PROMOTION_THROUGHPUT=$(read_metric promotion_throughput)
SUBMIT_FAILURES=$(read_metric submit_failures)

assert_gt() {
    local label="$1" actual="$2" threshold="$3"
    TOTAL=$((TOTAL + 1))
    if "$PYTHON" -c "import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)" "$actual" "$threshold" 2>/dev/null; then
        echo "  ✅ $label: $actual > $threshold"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label: $actual <= $threshold"
        FAIL=$((FAIL + 1))
    fi
}

assert_gt "submitted_success > 0" "$SUBMITTED_SUCCESS" "0"
assert_gt "promotions_created > 0" "$PROMOTIONS_CREATED" "0"
assert_gt "submit_throughput > $MIN_SUBMIT_THROUGHPUT" "$SUBMIT_THROUGHPUT" "$MIN_SUBMIT_THROUGHPUT"
assert_gt "promotion_throughput > $MIN_PROMOTION_THROUGHPUT" "$PROMOTION_THROUGHPUT" "$MIN_PROMOTION_THROUGHPUT"

# Check Rust server log for 500 errors
RUST_500_COUNT=0
if [[ -f "$LOCAL_SERVER_LOG" ]]; then
    RUST_500_COUNT=$(grep -cE "INTERNAL_SERVER_ERROR" "$LOCAL_SERVER_LOG" 2>/dev/null || true)
    RUST_500_COUNT=$(echo "$RUST_500_COUNT" | tr -d '[:space:]')
    RUST_500_COUNT=${RUST_500_COUNT:-0}
fi
TOTAL=$((TOTAL + 1))
if [[ "$RUST_500_COUNT" -eq 0 ]]; then
    echo "  ✅ No INTERNAL_SERVER_ERROR in Rust server log"
    PASS=$((PASS + 1))
else
    echo "  ❌ Found $RUST_500_COUNT INTERNAL_SERVER_ERROR entries in Rust server log"
    FAIL=$((FAIL + 1))
fi

# Verify both processes used the same DB
DB_PATH="$TMP_HOME/.aether/aether.db"
TOTAL=$((TOTAL + 1))
if [[ -f "$DB_PATH" ]]; then
    REPO_COUNT=$("$PYTHON" -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
count = conn.execute('SELECT COUNT(*) FROM aether_repositories').fetchone()[0]
print(count)
conn.close()
" "$DB_PATH" 2>/dev/null || echo "0")
    if [[ "$REPO_COUNT" -gt 0 ]]; then
        echo "  ✅ Shared DB verified: $DB_PATH ($REPO_COUNT repos)"
        PASS=$((PASS + 1))
    else
        echo "  ❌ Shared DB has 0 repos — processes may not have used the same DB"
        FAIL=$((FAIL + 1))
    fi
else
    echo "  ❌ Shared DB not found at $DB_PATH"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "============================================"
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
echo "============================================"
echo ""
echo "Key metrics:"
echo "  Submitted:   $SUBMITTED_SUCCESS"
echo "  Promoted:    $PROMOTIONS_CREATED"
echo "  Submit TPS:  $SUBMIT_THROUGHPUT"
echo "  Promote TPS: $PROMOTION_THROUGHPUT"
echo "  Failures:    $SUBMIT_FAILURES"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    echo "❌ COMPATIBILITY SMOKE FAILED"
    echo ""
    if [[ -f "$LOCAL_SERVER_LOG" ]]; then
        echo "Rust server log (last 30 lines):"
        tail -30 "$LOCAL_SERVER_LOG"
    fi
    exit 1
else
    echo "✅ COMPATIBILITY SMOKE PASSED"
    exit 0
fi

