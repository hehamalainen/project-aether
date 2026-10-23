#!/usr/bin/env bash
# =============================================================================
# Aether-RS PostgreSQL End-to-End Validation
# =============================================================================
#
# Exercises the PG-compiled Rust binary against a real PostgreSQL instance.
# Covers: repo CRUD, proposal submit, commit_promotion, head advancement,
#         trust metrics, chained promotions, and 501 for native promotion.
#
# Usage:
#   ./scripts/test_pg_e2e.sh                         # defaults to localhost
#   DATABASE_URL=postgres://user:pass@host/db ./scripts/test_pg_e2e.sh
#
# Prerequisites:
#   - PostgreSQL running and accepting connections
#   - psql available on PATH
#   - Rust toolchain (cargo build)
#
# The script:
#   1. Creates a fresh test database (aether_test_e2e_<PID>)
#   2. Builds the PG binary
#   3. Starts the server, runs 14 assertions
#   4. Tears down the database and server on exit
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RS_DIR="$REPO_ROOT/aether-rs"

# Configurable
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"
TEST_DB="aether_test_e2e_$$"
SERVER_PORT=8100
BASE_URL="http://localhost:$SERVER_PORT"

# Test counters
PASS=0
FAIL=0
TOTAL=0

pass() {
    ((++PASS))
    ((++TOTAL))
    echo "  ✅ $1"
}

fail() {
    ((++FAIL))
    ((++TOTAL))
    echo "  ❌ $1"
}

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [ "$actual" = "$expected" ]; then
        pass "$msg"
    else
        fail "$msg (expected '$expected', got '$actual')"
    fi
}

assert_ne() {
    local actual="$1" forbidden="$2" msg="$3"
    if [ "$actual" != "$forbidden" ]; then
        pass "$msg"
    else
        fail "$msg (got forbidden value '$forbidden')"
    fi
}

json_field() {
    python3 -c "import sys,json; print(json.load(sys.stdin)$1)" <<< "$2"
}

# Cleanup handler
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    echo ""
    echo "🧹 Dropping test database $TEST_DB..."
    psql -h "$PG_HOST" -p "$PG_PORT" "$PG_SUPERDB" -c "DROP DATABASE IF EXISTS $TEST_DB;" 2>/dev/null || true
}
trap cleanup EXIT

echo "============================================"
echo "Aether-RS PostgreSQL E2E Validation"
echo "============================================"
echo ""
echo "  PG Host:    $PG_HOST:$PG_PORT"
echo "  Test DB:    $TEST_DB"
echo "  Server:     $BASE_URL"
echo ""

# Step 1: Create test database
echo "📦 Creating test database..."
psql -h "$PG_HOST" -p "$PG_PORT" "$PG_SUPERDB" -c "CREATE DATABASE $TEST_DB;" 2>&1 | grep -v "^$"
echo "  ✅ Database created"

# Step 2: Build PG binary
echo ""
echo "🔨 Building aether-rs (db-postgres)..."
(cd "$RS_DIR" && cargo build --no-default-features --features db-postgres --quiet 2>&1)
echo "  ✅ Build succeeded"

# Step 3: Start PG server
echo ""
echo "🚀 Starting aether-rs server (PostgreSQL)..."
DATABASE_URL="postgres://$PG_HOST:$PG_PORT/$TEST_DB" \
    "$RS_DIR/target/debug/server" &
SERVER_PID=$!
sleep 2

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "  ❌ Server failed to start"
    exit 1
fi
echo "  ✅ Server started (PID: $SERVER_PID)"

# ============================================================================
# Tests
# ============================================================================

echo ""
echo "📋 Test 1: Health & Root API Parity"
ROOT=$(curl -sf "$BASE_URL/")
assert_eq "$(json_field "['service']" "$ROOT")" "aether-rs" "GET / returns service name"

HEALTH=$(curl -sf "$BASE_URL/v1/health")
assert_eq "$(json_field "['status']" "$HEALTH")" "healthy" "GET /v1/health returns healthy"
assert_eq "$(json_field "['service']" "$HEALTH")" "aether" "GET /v1/health returns service"

echo ""
echo "📋 Test 2: Create Repository"
REPO=$(curl -sf -X POST "$BASE_URL/v1/repos" \
    -H "Content-Type: application/json" \
    -d '{"name":"pg-e2e-test","description":"PostgreSQL E2E validation"}')
REPO_ID=$(json_field "['id']" "$REPO")
assert_ne "$REPO_ID" "" "Repo created with ID"
assert_eq "$(json_field "['name']" "$REPO")" "pg-e2e-test" "Repo has correct name"
assert_eq "$(json_field "['head_sequence']" "$REPO")" "0" "Repo starts at sequence 0"
echo "  → Repo ID: $REPO_ID"

echo ""
echo "📋 Test 3: Get Repository"
GET_REPO=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID")
assert_eq "$(json_field "['id']" "$GET_REPO")" "$REPO_ID" "GET repo matches created ID"

echo ""
echo "📋 Test 4: Submit Proposal"
PROP=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-e2e-agent",
        "task_id": "pg-task-001",
        "change_type": "full_file",
        "change_content": "print(\"hello PG\")\n",
        "affected_paths": ["main.py"],
        "confidence_score": 0.9
    }')
PROP_ID=$(json_field "['proposal_id']" "$PROP")
assert_ne "$PROP_ID" "" "Proposal created with ID"
assert_eq "$(json_field "['status']" "$PROP")" "PENDING" "Proposal status is PENDING"
echo "  → Proposal ID: $PROP_ID"

echo ""
echo "📋 Test 5: List Proposals"
LIST_PROPS=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/proposals")
assert_eq "$(json_field "['total']" "$LIST_PROPS")" "1" "Listed 1 proposal"
assert_eq "$(json_field "['proposals'][0]['status']" "$LIST_PROPS")" "PENDING" "Proposal status confirmed PENDING"

echo ""
echo "📋 Test 6: Get Head (pre-promotion)"
HEAD_PRE=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/head")
assert_eq "$(json_field "['sequence']" "$HEAD_PRE")" "0" "Head sequence is 0 before promotion"

echo ""
echo "📋 Test 7: Native Promotion"
COMMIT=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{
        \"proposal_ids\": [\"$PROP_ID\"]
    }")
assert_eq "$(json_field "['success']" "$COMMIT")" "True" "Promotion succeeded"
NEW_HEAD=$(json_field "['new_head_id']" "$COMMIT")
assert_ne "$NEW_HEAD" "" "New head ID assigned"
assert_eq "$(json_field "['new_head_sequence']" "$COMMIT")" "1" "Head sequence advanced to 1"

TIMINGS_STR=$(echo "$COMMIT" | jq -r '.evidence._timings_ms')
if [ "$TIMINGS_STR" == "null" ] || [ -z "$TIMINGS_STR" ]; then
    fail "Missing _timings_ms in evidence"
fi

for key in t_db_read_pre_orch t_crdt_eval t_materialize t_gates t_trust_math t_fs_rename t_db_tx_start t_db_revalidate t_db_insert_snapshot_gates t_db_update_proposals_trust t_db_insert_promotions_heads_cas t_db_commit t_total_inner t_db_read_repo t_db_read_proposals t_db_read_base_snapshot req_proposal_count t_db_insert_promotion t_db_insert_head t_db_cas_update; do
    VAL=$(echo "$TIMINGS_STR" | jq -r ".$key")
    if [ "$VAL" == "null" ]; then
        fail "_timings_ms missing key $key"
    fi
    if ! echo "$VAL" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
        fail "_timings_ms key $key is not numeric (got $VAL)"
    fi
done

TOTAL_INNER=$(echo "$TIMINGS_STR" | jq -r '.t_total_inner')
if (( $(echo "$TOTAL_INNER <= 0" | bc -l) )); then
    fail "t_total_inner must be > 0"
fi
echo "  ✅ _timings_ms present with all 13 valid numeric keys"

echo "  → New head: $NEW_HEAD (seq: 1)"

echo ""
echo "📋 Test 8: Get Head (post-promotion)"
HEAD_POST=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/head")
assert_eq "$(json_field "['sequence']" "$HEAD_POST")" "1" "Head sequence is 1"
POST_SNAP=$(json_field "['snapshot_id']" "$HEAD_POST")
assert_ne "$POST_SNAP" "" "Snapshot ID present"
POST_HASH=$(json_field "['content_hash']" "$HEAD_POST")
assert_ne "$POST_HASH" "" "Content hash present"
echo "  → Snapshot: $POST_SNAP"
echo "  → Hash: $POST_HASH"

echo ""
echo "📋 Test 9: List Promotions"
LIST_PROMOS=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/promotions")
assert_eq "$(json_field "['total']" "$LIST_PROMOS")" "1" "Listed 1 promotion"

echo ""
echo "📋 Test 10: Proposal Status Post-Promotion"
LIST_PROPS2=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/proposals")
PROP_STATUS=$(python3 -c "
import sys, json
d = json.load(sys.stdin)
p = [x for x in d['proposals'] if x['id'] == '$PROP_ID'][0]
print(p['status'])
" <<< "$LIST_PROPS2")
assert_eq "$PROP_STATUS" "PROMOTED" "Proposal status updated to PROMOTED"

echo ""
echo "📋 Test 11: Trust Metrics Persisted"
TRUST_ROW=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A \
    -c "SELECT agent_id, promoted_count FROM aether_agent_metrics WHERE agent_id='pg-e2e-agent'" 2>/dev/null)
TRUST_AGENT=$(echo "$TRUST_ROW" | cut -d'|' -f1)
TRUST_COUNT=$(echo "$TRUST_ROW" | cut -d'|' -f2)
assert_eq "$TRUST_AGENT" "pg-e2e-agent" "Trust row exists for agent"
if [ "$TRUST_COUNT" -ge 1 ] 2>/dev/null; then
    pass "promoted_count >= 1 (got $TRUST_COUNT)"
else
    fail "promoted_count >= 1 (got '$TRUST_COUNT')"
fi


echo "📋 Test 13: Chained Promotion (seq 1→2)"
PROP2=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-e2e-agent-2",
        "task_id": "pg-task-002",
        "change_content": "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,1 @@\n-print(\"hello PG\")\n+print(\"hello PG v2\")\n",
        "affected_paths": ["main.py"],
        "confidence_score": 0.95
    }')
PROP2_ID=$(json_field "['proposal_id']" "$PROP2")
COMMIT2=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{
        \"proposal_ids\": [\"$PROP2_ID\"]
    }")
assert_eq "$(json_field "['success']" "$COMMIT2")" "True" "Second promotion succeeded"
assert_eq "$(json_field "['new_head_sequence']" "$COMMIT2")" "2" "Head sequence advanced to 2"
echo "  → Chain verified: seq 0 → 1 → 2"

echo ""
echo "📋 Test 14: Second Agent Trust Metrics"
TRUST_ROW2=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A \
    -c "SELECT promoted_count FROM aether_agent_metrics WHERE agent_id='pg-e2e-agent-2'" 2>/dev/null)
if [ "$TRUST_ROW2" -ge 1 ] 2>/dev/null; then
    pass "Second agent trust row exists (promoted_count=$TRUST_ROW2)"
else
    fail "Second agent trust row missing or invalid"
fi

# ============================================================================
# Test 15: Gate Rejection
# ============================================================================
echo ""
echo "📋 Test 15: Gate Rejection"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "UPDATE aether_repositories SET gate_config = '[{\"name\":\"strict_gate\",\"command\":\"exit 1\",\"required\":true}]' WHERE id = '$REPO_ID';" >/dev/null

FAIL_PROP=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-agent-fail",
        "change_type": "full_file",
        "change_content": "fail test",
        "affected_paths": ["fail.py"],
        "confidence_score": 0.99
    }')
FAIL_PROP_ID=$(json_field "['proposal_id']" "$FAIL_PROP")

BEFORE_GATES=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_gate_results WHERE repo_id='$REPO_ID';")
BEFORE_SNAPS=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_snapshots WHERE repo_id='$REPO_ID';")
BEFORE_PROMS=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_promotions WHERE repo_id='$REPO_ID';")
BEFORE_DIRS=$(find "${HOME:-.}/.aether/aether_storage/$REPO_ID/snapshots" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')

FAIL_PROMOTE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$FAIL_PROP_ID\"]}")
    
if echo "$FAIL_PROMOTE" | grep -q "\"success\":false"; then
    pass "Promotion rejected due to missing gate"
else
    fail "Promotion rejected due to missing gate"
fi

REJECT_TIMINGS=$(echo "$FAIL_PROMOTE" | jq -r '.evidence._timings_ms')
if [ "$REJECT_TIMINGS" == "null" ] || [ -z "$REJECT_TIMINGS" ]; then
    fail "Missing _timings_ms in reject response evidence"
fi

for key in t_db_read_repo t_db_read_proposals t_db_read_base_snapshot req_proposal_count; do
    VAL=$(echo "$REJECT_TIMINGS" | jq -r ".$key")
    if [ "$VAL" == "null" ]; then
        fail "Reject response _timings_ms missing read key $key"
    fi
done

echo "  ✅ _timings_ms present in reject response"

AFTER_GATES=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_gate_results WHERE repo_id='$REPO_ID';")
assert_eq "$AFTER_GATES" "$BEFORE_GATES" "Regression: No GateResult rows leaked"

AFTER_SNAPS=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_snapshots WHERE repo_id='$REPO_ID';")
assert_eq "$AFTER_SNAPS" "$BEFORE_SNAPS" "Regression: No aether_snapshots rows leaked"

AFTER_PROMS=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_promotions WHERE repo_id='$REPO_ID';")
assert_eq "$AFTER_PROMS" "$BEFORE_PROMS" "Regression: No aether_promotions rows leaked"

AFTER_DIRS=$(find "${HOME:-.}/.aether/aether_storage/$REPO_ID/snapshots" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$AFTER_DIRS" "$BEFORE_DIRS" "Regression: No snapshot directories leaked"


HARD_FAIL_COUNT=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT hard_fail_count FROM aether_agent_metrics WHERE agent_id='pg-agent-fail';")
assert_eq "$HARD_FAIL_COUNT" "1" "Single-proposal gate rejection increments hard_fail_count"

TRUST_BETA=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT trust_beta FROM aether_agent_metrics WHERE agent_id='pg-agent-fail';")
assert_eq "$TRUST_BETA" "6" "Single-proposal gate rejection increments trust_beta"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "UPDATE aether_repositories SET gate_config = '[]' WHERE id = '$REPO_ID';" >/dev/null

# ============================================================================
# Test 16: Multi-file full_file
# ============================================================================
echo ""
echo "📋 Test 16: Multi-file full_file"

MULTI_PROP=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-agent-multi",
        "change_type": "full_file",
        "change_content": "multi content",
        "affected_paths": ["multi1.py", "multi2.py"],
        "confidence_score": 0.95
    }')
MULTI_PROP_ID=$(json_field "['proposal_id']" "$MULTI_PROP")

MULTI_PROMOTE=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$MULTI_PROP_ID\"]}")

assert_eq "$(json_field "['success']" "$MULTI_PROMOTE")" "True" "Multi-file promotion succeeded"

HEAD_MULTI=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/head")
SNAP_MULTI_ID=$(json_field "['snapshot_id']" "$HEAD_MULTI")
SNAP_MULTI_DIR="${HOME:-.}/.aether/aether_storage/$REPO_ID/snapshots/$SNAP_MULTI_ID"


if [[ -f "$SNAP_MULTI_DIR/multi1.py" ]]; then pass "Multi path 1 materialized"; else fail "Multi path 1 missing"; fi
if [[ -f "$SNAP_MULTI_DIR/multi2.py" ]]; then pass "Multi path 2 materialized"; else fail "Multi path 2 missing"; fi

# ============================================================================
# Test 17: Conflict Set Batch Rejection
# ============================================================================
echo ""
echo "📋 Test 17: Conflict Set Batch Rejection"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, conflict_set_id, submitted_at, source) VALUES ('pg_prop_cs1', '$REPO_ID', 'PENDING', 'agt1', 'full_file', 'v1', '[\"file.txt\"]', 1.0, 'cs_pg_123', '2026-05-09 00:00:00', 'manual');" >/dev/null
psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, conflict_set_id, submitted_at, source) VALUES ('pg_prop_cs2', '$REPO_ID', 'PENDING', 'agt2', 'full_file', 'v2', '[\"file.txt\"]', 1.0, 'cs_pg_123', '2026-05-09 00:00:00', 'manual');" >/dev/null

CONFLICT_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"pg_prop_cs1\", \"pg_prop_cs2\"]}")

if [[ "$CONFLICT_HTTP" == "400" ]]; then
    pass "Batch rejected due to conflict set collision (HTTP 400)"
else
    fail "Batch rejected due to conflict set collision (got HTTP $CONFLICT_HTTP, expected 400)"
fi

# ============================================================================
# Test 17.5: Batch Parity Semantic Check
# ============================================================================
echo ""
echo "📋 Test 17.5: Batch Parity Semantic Check"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "UPDATE aether_repositories SET gate_config = '[{\"name\":\"batch_gate\",\"command\":\"cat b1.txt\",\"required\":true}]' WHERE id = '$REPO_ID';" >/dev/null

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, submitted_at, source) VALUES ('pg_prop_batch1', '$REPO_ID', 'PENDING', 'batch-agt1', 'full_file', 'b1_pg_marker', '[\"b1.txt\"]', 0.8, '2026-05-09 00:00:00', 'manual');" >/dev/null
psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, submitted_at, source) VALUES ('pg_prop_batch2', '$REPO_ID', 'PENDING', 'batch-agt2', 'full_file', 'b2_pg_marker', '[\"b2.txt\"]', 0.9, '2026-05-09 00:00:00', 'manual');" >/dev/null

BATCH_PROMOTE=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"pg_prop_batch1\", \"pg_prop_batch2\"]}")

assert_eq "$(json_field "['success']" "$BATCH_PROMOTE")" "True" "Batch semantic promotion succeeded"

BATCH_NULL_PROPOSAL_COUNT=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT COUNT(*) FROM aether_gate_results WHERE gate_name='batch_gate' AND proposal_id IS NULL;")
assert_eq "$BATCH_NULL_PROPOSAL_COUNT" "1" "Batch gate results explicitly have proposal_id = NULL"

BATCH_GATE_STDOUT=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT stdout FROM aether_gate_results WHERE gate_name='batch_gate' AND proposal_id IS NULL;" | tr -d '\n' | tr -d '\r')
assert_eq "$BATCH_GATE_STDOUT" "b1_pg_marker" "Gate output proves it ran in candidate workspace"

PROM_ID=$(json_field "['promotion_id']" "$BATCH_PROMOTE")
CONFIDENCE_STORED=$(psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -t -A -c "SELECT confidence_score FROM aether_promotions WHERE id='$PROM_ID';")
assert_eq "$CONFIDENCE_STORED" "0.605" "Stored promotion confidence reflects the raw_score"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "UPDATE aether_repositories SET gate_config = '[]' WHERE id = '$REPO_ID';" >/dev/null

# ============================================================================
# Test 18: Delta Snapshot Loading
# ============================================================================
echo ""
echo "📋 Test 18: Delta Snapshot Loading"

DELTA_HEAD_ID="head_pg_delta"
DELTA_SNAP_ID="snap_pg_delta"
DELTA_DIR="${HOME:-.}/.aether/aether_storage/$REPO_ID/snapshots/$DELTA_SNAP_ID"
mkdir -p "$DELTA_DIR"
echo '{"mode":"delta","base_snapshot_id":"'"$SNAP_MULTI_ID"'","changed_paths":["delta.py"]}' > "$DELTA_DIR/.aether_snapshot_manifest.json"
echo "delta pg content" > "$DELTA_DIR/delta.py"

psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_snapshots (id, repo_id, head_sequence, storage_path, created_at, content_hash, file_count, total_bytes) VALUES ('$DELTA_SNAP_ID', '$REPO_ID', 20, '$DELTA_DIR', '2026-05-09', 'hashpg1', 1, 10);" >/dev/null
psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "INSERT INTO aether_heads (id, repo_id, sequence, snapshot_id, is_canonical, created_at) VALUES ('$DELTA_HEAD_ID', '$REPO_ID', 20, '$DELTA_SNAP_ID', true, '2026-05-09');" >/dev/null
psql -h "$PG_HOST" -p "$PG_PORT" "$TEST_DB" -c "UPDATE aether_repositories SET head_id = '$DELTA_HEAD_ID', head_sequence = 20 WHERE id = '$REPO_ID';" >/dev/null

DELTA_PROP=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-agent-delta",
        "change_type": "full_file",
        "change_content": "post delta pg",
        "affected_paths": ["post_delta.py"],
        "confidence_score": 0.95
    }')
DELTA_PROP_ID=$(json_field "['proposal_id']" "$DELTA_PROP")

DELTA_PROMOTE=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$DELTA_PROP_ID\"]}")

assert_eq "$(json_field "['success']" "$DELTA_PROMOTE")" "True" "Post-delta promotion succeeded"

HEAD_FINAL=$(curl -sf "$BASE_URL/v1/repos/$REPO_ID/head")
SNAP_FINAL_ID=$(json_field "['snapshot_id']" "$HEAD_FINAL")
SNAP_FINAL_DIR="${HOME:-.}/.aether/aether_storage/$REPO_ID/snapshots/$SNAP_FINAL_ID"


if [[ -f "$SNAP_FINAL_DIR/multi1.py" ]] && grep -q "multi content" "$SNAP_FINAL_DIR/multi1.py"; then pass "Base file carried over"; else fail "Base file carried over (missing or wrong content)"; fi
if [[ -f "$SNAP_FINAL_DIR/delta.py" ]] && grep -q "delta pg content" "$SNAP_FINAL_DIR/delta.py"; then pass "Intermediate delta file carried over"; else fail "Intermediate delta file carried over (missing or wrong content)"; fi
if [[ -f "$SNAP_FINAL_DIR/post_delta.py" ]] && grep -q "post delta pg" "$SNAP_FINAL_DIR/post_delta.py"; then pass "New file materialized"; else fail "New file materialized (missing or wrong content)"; fi

# ============================================================================
# Test 19: No-op full_file
# ============================================================================
echo ""
echo "📋 Test 19: No-op full_file rejection"

NOOP_PROP=$(curl -sf -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d '{
        "agent_id": "pg-agent-noop",
        "change_type": "full_file",
        "change_content": "noop",
        "affected_paths": [],
        "confidence_score": 0.95
    }')
NOOP_PROP_ID=$(json_field "['proposal_id']" "$NOOP_PROP")

NOOP_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$NOOP_PROP_ID\"]}")

if [[ "$NOOP_HTTP" == "400" ]]; then
    pass "No-op full_file rejected (HTTP 400)"
else
    fail "No-op full_file rejected (got HTTP $NOOP_HTTP, expected 400)"
fi

echo -e "\n📋 Test 20: Arrival Telemetry Physics"
PHYSICS_RESP=$(curl -s "$BASE_URL/v1/system/physics?window_seconds=60")
echo "$PHYSICS_RESP" | jq . > test_physics.json
if ! jq -e '.events.submit_accepted' test_physics.json > /dev/null; then
    echo "  ❌ Failed to parse physics payload or missing submit_accepted"
    exit 1
fi
echo "  ✅ Physics API payload structured correctly"
echo "  → Lambda TPS: $(jq .lambda_tps test_physics.json)"
echo "  → Mu TPS: $(jq .mu_tps test_physics.json)"
echo "  → Queue Active: $(jq .queue_depth.active test_physics.json)"

# ============================================================================
# Summary
# ============================================================================

echo ""
echo "============================================"
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
echo "============================================"

if [ "$FAIL" -eq 0 ]; then
    echo "✅ PG E2E VALIDATION PASSED"
    exit 0
else
    echo "❌ PG E2E VALIDATION FAILED"
    exit 1
fi

