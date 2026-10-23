#!/usr/bin/env bash
# ============================================================================
# Step 7: Aether-RS Native Path Integration Test
#
# Primary gate for Rust API parity. Tests the full lifecycle:
#   health → create repo → submit proposal → promote → verify head
#
# Usage:
#   ./scripts/test_rust_native_parity.sh
#
# Prerequisites:
#   - aether-rs server compiled and available
#   - No other process on port 8100
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AETHER_RS_DIR="$PROJECT_DIR/aether-rs"
BASE_URL="http://127.0.0.1:8100"
SERVER_PID=""
TEST_DB=""
PASS=0
FAIL=0
TOTAL=0

cleanup() {
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [[ -n "$TEST_DB" && -f "$TEST_DB" ]]; then
        rm -f "$TEST_DB" "${TEST_DB}-wal" "${TEST_DB}-shm" 2>/dev/null || true
    fi
}
trap cleanup EXIT

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    TOTAL=$((TOTAL + 1))
    if [[ "$expected" == "$actual" ]]; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label: expected '$expected', got '$actual'"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$haystack" | grep -q "$needle"; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label: expected to contain '$needle'"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_empty() {
    local label="$1" value="$2"
    TOTAL=$((TOTAL + 1))
    if [[ -n "$value" && "$value" != "null" && "$value" != '""' ]]; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label: value is empty or null"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "Aether-RS Native Path Integration Test"
echo "============================================"
echo ""

# --- Build ---
echo "🔨 Building aether-rs..."
cd "$AETHER_RS_DIR"
cargo build --workspace --quiet 2>&1 || { echo "❌ Build failed"; exit 1; }
echo "  ✅ Build succeeded"
echo ""

# --- Start server with isolated DB ---
# Use /tmp for the test DB to avoid spaces in iCloud-synced project paths
# that break sqlite:// URLs. The DB is ephemeral and cleaned up on exit.
TEST_DB="/tmp/aether_test_native_$$.db"
rm -f "$TEST_DB" "${TEST_DB}-wal" "${TEST_DB}-shm"
export AETHER_DB_PATH="sqlite://${TEST_DB}"

echo "🚀 Starting aether-rs server (DB: $TEST_DB)..."
"$AETHER_RS_DIR/target/debug/server" > /tmp/aether_native_test.log 2>&1 &
SERVER_PID=$!
sleep 2

# Verify server is up
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "❌ Server failed to start"
    exit 1
fi
echo "  ✅ Server started (PID: $SERVER_PID)"
echo ""

# ============================================================================
# Test 1: Health & Root endpoints
# ============================================================================
echo "📋 Test 1: Health & Root API Parity"

ROOT_RESPONSE=$(curl -s "$BASE_URL/")
assert_contains "GET / returns service name" '"aether-rs"' "$ROOT_RESPONSE"

HEALTH_RESPONSE=$(curl -s "$BASE_URL/v1/health")
assert_contains "GET /v1/health returns healthy" '"healthy"' "$HEALTH_RESPONSE"
assert_contains "GET /v1/health returns service" '"aether"' "$HEALTH_RESPONSE"
echo ""

# ============================================================================
# Test 2: Create Repository
# ============================================================================
echo "📋 Test 2: Create Repository"

REPO_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos" \
    -H "Content-Type: application/json" \
    -d '{"name": "test-native-parity", "description": "Integration test repo"}')

REPO_ID=$(echo "$REPO_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
assert_not_empty "Repo created with ID" "$REPO_ID"
assert_contains "Repo has correct name" '"test-native-parity"' "$REPO_RESPONSE"
echo "  → Repo ID: $REPO_ID"
echo ""

# ============================================================================
# Test 3: Submit Proposal (verify PENDING status, provenance fill-in)
# ============================================================================
echo "📋 Test 3: Submit Proposal"

PROPOSAL_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-001\",
        \"task_id\": \"task-integration-test\",
        \"confidence_score\": 0.85,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"def hello():\\n    return 'Hello from Aether-RS native path'\",
        \"affected_paths\": [\"src/hello.py\"],
        \"source\": \"manual\"
    }")

PROP_ID=$(echo "$PROPOSAL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")
PROP_STATUS=$(echo "$PROPOSAL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "")

assert_not_empty "Proposal created with ID" "$PROP_ID"
assert_eq "Proposal status is PENDING (not INGESTED)" "PENDING" "$PROP_STATUS"
assert_contains "Response message says accepted" "accepted" "$PROPOSAL_RESPONSE"
echo "  → Proposal ID: $PROP_ID"
echo ""

# Verify provenance was filled in by checking DB directly
PROP_DETAIL=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/proposals" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for p in data.get('proposals', []):
    if p['id'] == '$PROP_ID':
        print(json.dumps(p))
        break
" 2>/dev/null || echo "")

echo "📋 Test 3b: Verify Provenance Persistence"
assert_contains "Proposal has agent_id" '"test-agent-001"' "$PROP_DETAIL"
assert_contains "Proposal has task_id" '"task-integration-test"' "$PROP_DETAIL"
assert_contains "Proposal has change_type" '"full_file"' "$PROP_DETAIL"
echo ""

# ============================================================================
# Test 4: Promote Proposal (native path)
# ============================================================================
echo "📋 Test 4: Native Promotion"

PROMOTE_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$PROP_ID\"]}")

PROM_SUCCESS=$(echo "$PROMOTE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
NEW_HEAD_ID=$(echo "$PROMOTE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_head_id', ''))" 2>/dev/null || echo "")
NEW_SEQ=$(echo "$PROMOTE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_head_sequence', 0))" 2>/dev/null || echo "")

assert_eq "Promotion succeeded" "True" "$PROM_SUCCESS"
assert_not_empty "New head ID assigned" "$NEW_HEAD_ID"
assert_eq "Head sequence advanced to 1" "1" "$NEW_SEQ"
echo "  → New head: $NEW_HEAD_ID (seq: $NEW_SEQ)"
echo ""

# ============================================================================
# Test 5: Verify Head State
# ============================================================================
echo "📋 Test 5: Verify Head State"

HEAD_RESPONSE=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/head")
HEAD_ID=$(echo "$HEAD_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('head_id', ''))" 2>/dev/null || echo "")
HEAD_SEQ=$(echo "$HEAD_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sequence', 0))" 2>/dev/null || echo "")
SNAP_ID=$(echo "$HEAD_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_id', ''))" 2>/dev/null || echo "")
CONTENT_HASH=$(echo "$HEAD_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content_hash', ''))" 2>/dev/null || echo "")

assert_eq "Head matches promotion result" "$NEW_HEAD_ID" "$HEAD_ID"
assert_eq "Head sequence is 1" "1" "$HEAD_SEQ"
assert_not_empty "Snapshot ID present" "$SNAP_ID"
assert_not_empty "Content hash present" "$CONTENT_HASH"
echo "  → Snapshot: $SNAP_ID"
echo "  → Hash: $CONTENT_HASH"
echo ""

# ============================================================================
# Test 6: Verify Snapshot Files on Disk
# ============================================================================
echo "📋 Test 6: Verify Snapshot Materialization on Disk"

HOME_DIR="${HOME:-.}"
SNAP_DIR="$HOME_DIR/.aether/aether_storage/$REPO_ID/snapshots/$SNAP_ID"

TOTAL=$((TOTAL + 1))
if [[ -d "$SNAP_DIR" ]]; then
    echo "  ✅ Snapshot directory exists: $SNAP_DIR"
    PASS=$((PASS + 1))
else
    echo "  ❌ Snapshot directory missing: $SNAP_DIR"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
if [[ -f "$SNAP_DIR/.aether_snapshot_manifest.json" ]]; then
    echo "  ✅ Manifest file exists"
    PASS=$((PASS + 1))
    MANIFEST_MODE=$(python3 -c "import json; print(json.load(open('$SNAP_DIR/.aether_snapshot_manifest.json'))['mode'])" 2>/dev/null || echo "")
    assert_eq "Manifest mode is 'full'" "full" "$MANIFEST_MODE"
else
    echo "  ❌ Manifest file missing"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
if [[ -f "$SNAP_DIR/src/hello.py" ]]; then
    echo "  ✅ Materialized file exists: src/hello.py"
    PASS=$((PASS + 1))
else
    echo "  ❌ Materialized file missing: src/hello.py"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Test 7: Verify Proposal Status After Promotion
# ============================================================================
echo "📋 Test 7: Post-Promotion Proposal Status"

POST_PROM_DETAIL=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/proposals" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for p in data.get('proposals', []):
    if p['id'] == '$PROP_ID':
        print(p.get('status', ''))
        break
" 2>/dev/null || echo "")

assert_eq "Proposal status is PROMOTED" "PROMOTED" "$POST_PROM_DETAIL"
echo ""

# ============================================================================
# Test 8: Second Promotion — No Double-Apply of History
# This is the critical regression test: submit a v2 change to the same file,
# promote it, then verify the snapshot only contains v2 content (not v1+v2).
# ============================================================================
echo "📋 Test 8: Second Promotion — No Double-Apply"

PROPOSAL2_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-002\",
        \"task_id\": \"task-v2-update\",
        \"confidence_score\": 0.90,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"def hello():\\n    return 'v2 only'\",
        \"affected_paths\": [\"src/hello.py\"],
        \"source\": \"manual\"
    }")

PROP2_ID=$(echo "$PROPOSAL2_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")
assert_not_empty "Second proposal created" "$PROP2_ID"

PROMOTE2_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$PROP2_ID\"]}")

PROM2_SUCCESS=$(echo "$PROMOTE2_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
assert_eq "Second promotion succeeded" "True" "$PROM2_SUCCESS"

NEW_SEQ2=$(echo "$PROMOTE2_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_head_sequence', 0))" 2>/dev/null || echo "")
assert_eq "Head sequence advanced to 2" "2" "$NEW_SEQ2"

# Get the new snapshot and verify file content on disk
HEAD2_RESPONSE=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/head")
SNAP2_ID=$(echo "$HEAD2_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_id', ''))" 2>/dev/null || echo "")
assert_not_empty "Second snapshot ID present" "$SNAP2_ID"

HOME_DIR="${HOME:-.}"
SNAP2_DIR="$HOME_DIR/.aether/aether_storage/$REPO_ID/snapshots/$SNAP2_ID"
SNAP2_FILE="$SNAP2_DIR/src/hello.py"

TOTAL=$((TOTAL + 1))
if [[ -f "$SNAP2_FILE" ]]; then
    echo "  ✅ Second snapshot file exists"
    PASS=$((PASS + 1))
else
    echo "  ❌ Second snapshot file missing: $SNAP2_FILE"
    FAIL=$((FAIL + 1))
fi

# Critical assertion: file must contain ONLY v2, not v1+v2
SNAP2_CONTENT=$(cat "$SNAP2_FILE" 2>/dev/null || echo "")
TOTAL=$((TOTAL + 1))
if echo "$SNAP2_CONTENT" | grep -q "v2 only"; then
    echo "  ✅ Snapshot contains v2 content"
    PASS=$((PASS + 1))
else
    echo "  ❌ Snapshot missing v2 content"
    FAIL=$((FAIL + 1))
fi

# Must NOT contain v1 content (the double-apply bug)
TOTAL=$((TOTAL + 1))
if echo "$SNAP2_CONTENT" | grep -q "Hello from Aether-RS"; then
    echo "  ❌ DOUBLE-APPLY BUG: snapshot still contains v1 content"
    echo "     File content:"
    echo "$SNAP2_CONTENT" | head -10
    FAIL=$((FAIL + 1))
else
    echo "  ✅ Snapshot does NOT contain old v1 content (no double-apply)"
    PASS=$((PASS + 1))
fi

# Count 'def hello()' — must appear exactly once
DEF_COUNT=$(grep -c "def hello" "$SNAP2_FILE" 2>/dev/null || echo "0")
TOTAL=$((TOTAL + 1))
if [[ "$DEF_COUNT" -eq 1 ]]; then
    echo "  ✅ 'def hello()' appears exactly once (no duplicate)"
    PASS=$((PASS + 1))
else
    echo "  ❌ DUPLICATE CONTENT: 'def hello()' appears $DEF_COUNT times"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Test 9: Partial base_head — Independent Field Fill-in
# Submit a proposal with base_head_id but NO base_head_sequence.
# Verify that base_head_sequence is filled from the current repo head (2),
# not defaulted to 0.
# ============================================================================
echo "📋 Test 9: Partial base_head — Independent Fill-in"

# Get the current head_id to use as partial base
CURRENT_HEAD_ID=$(echo "$HEAD2_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('head_id', ''))" 2>/dev/null || echo "")

PARTIAL_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-003\",
        \"confidence_score\": 0.75,
        \"intent_type\": \"fix\",
        \"change_type\": \"full_file\",
        \"change_content\": \"# partial test\",
        \"affected_paths\": [\"src/partial.py\"],
        \"base_head_id\": \"$CURRENT_HEAD_ID\",
        \"source\": \"manual\"
    }")

PARTIAL_PROP_ID=$(echo "$PARTIAL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")
assert_not_empty "Partial base_head proposal created" "$PARTIAL_PROP_ID"

# Read back the proposal and check base_head_sequence
PARTIAL_DETAIL=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/proposals" | python3 -c "
import sys,json
data = json.load(sys.stdin)
for p in data.get('proposals', []):
    if p['id'] == '$PARTIAL_PROP_ID':
        print(json.dumps(p))
        break
" 2>/dev/null || echo "")

PARTIAL_BASE_SEQ=$(echo "$PARTIAL_DETAIL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_head_sequence', -1))" 2>/dev/null || echo "-1")
assert_eq "base_head_sequence filled to current head (2), not 0" "2" "$PARTIAL_BASE_SEQ"

PARTIAL_BASE_ID=$(echo "$PARTIAL_DETAIL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_head_id', ''))" 2>/dev/null || echo "")
assert_eq "base_head_id preserved from client" "$CURRENT_HEAD_ID" "$PARTIAL_BASE_ID"
echo ""

# ============================================================================
# Test 10: Multi-file full_file proposal
# Submit a single full_file proposal with TWO paths in affected_paths.
# ============================================================================
echo "📋 Test 10: Multi-file full_file Application"

MULTI_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-004\",
        \"confidence_score\": 0.95,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"multi file test\",
        \"affected_paths\": [\"src/file1.txt\", \"src/file2.txt\"],
        \"source\": \"manual\"
    }")

MULTI_PROP_ID=$(echo "$MULTI_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")

MULTI_PROMOTE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$MULTI_PROP_ID\"]}")

MULTI_SUCCESS=$(echo "$MULTI_PROMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
assert_eq "Multi-file promotion succeeded" "True" "$MULTI_SUCCESS"

HEAD_MULTI=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/head")
SNAP_MULTI_ID=$(echo "$HEAD_MULTI" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_id', ''))" 2>/dev/null || echo "")
SNAP_MULTI_DIR="$HOME_DIR/.aether/aether_storage/$REPO_ID/snapshots/$SNAP_MULTI_ID"

TOTAL=$((TOTAL + 2))
if [[ -f "$SNAP_MULTI_DIR/src/file1.txt" ]]; then
    echo "  ✅ Multi-file path 1 materialized"
    PASS=$((PASS + 1))
else
    echo "  ❌ Multi-file path 1 missing"
    FAIL=$((FAIL + 1))
fi

if [[ -f "$SNAP_MULTI_DIR/src/file2.txt" ]]; then
    echo "  ✅ Multi-file path 2 materialized"
    PASS=$((PASS + 1))
else
    echo "  ❌ Multi-file path 2 missing"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Test 11: Gate Rejection
# Set a required gate in repo config, submit a proposal without gate results,
# verify it gets rejected with success=false.
# ============================================================================
echo "📋 Test 11: Gate Rejection"

sqlite3 "$TEST_DB" "UPDATE aether_repositories SET gate_config = '[{\"name\":\"strict_gate\",\"command\":\"exit 1\",\"required\":true}]' WHERE id = '$REPO_ID';"

FAIL_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-005\",
        \"confidence_score\": 0.99,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"gate fail test\",
        \"affected_paths\": [\"src/gate.txt\"],
        \"source\": \"manual\"
    }")

FAIL_PROP_ID=$(echo "$FAIL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")

FAIL_PROMOTE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$FAIL_PROP_ID\"]}")

FAIL_SUCCESS=$(echo "$FAIL_PROMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
assert_eq "Promotion rejected due to missing required gate" "False" "$FAIL_SUCCESS"

GATE_ROW_COUNT=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM aether_gate_results WHERE status='FAIL' AND exit_code=1;")
assert_eq "GateResult row created with FAIL and exit_code=1" "1" "$GATE_ROW_COUNT"

HARD_FAIL_COUNT=$(sqlite3 "$TEST_DB" "SELECT hard_fail_count FROM aether_agent_metrics WHERE agent_id='test-agent-005';")
assert_eq "Single-proposal gate rejection increments hard_fail_count" "1" "$HARD_FAIL_COUNT"

TRUST_BETA=$(sqlite3 "$TEST_DB" "SELECT trust_beta FROM aether_agent_metrics WHERE agent_id='test-agent-005';")
assert_eq "Single-proposal gate rejection increments trust_beta" "6.0" "$TRUST_BETA"

# Reset repo gate config to empty for next tests
sqlite3 "$TEST_DB" "UPDATE aether_repositories SET gate_config = '[]' WHERE id = '$REPO_ID';"
echo ""

# ============================================================================
# Test 12: Conflict Set Batch Rejection
# Submit two proposals with the same conflict_set_id and try to promote together.
# ============================================================================
echo "📋 Test 12: Conflict Set Batch Rejection"

sqlite3 "$TEST_DB" "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, conflict_set_id, submitted_at, source) VALUES ('prop_conflict1', '$REPO_ID', 'PENDING', 'agt1', 'full_file', 'v1', '[\"file.txt\"]', 1.0, 'cs_123', '2026-05-09T00:00:00Z', 'manual');"
sqlite3 "$TEST_DB" "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, conflict_set_id, submitted_at, source) VALUES ('prop_conflict2', '$REPO_ID', 'PENDING', 'agt2', 'full_file', 'v2', '[\"file.txt\"]', 1.0, 'cs_123', '2026-05-09T00:00:00Z', 'manual');"

CONFLICT_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"prop_conflict1\", \"prop_conflict2\"]}")

if [[ "$CONFLICT_HTTP" == "400" ]]; then
    TOTAL=$((TOTAL + 1))
    echo "  ✅ Batch rejected due to conflict set collision (HTTP 400)"
    PASS=$((PASS + 1))
else
    TOTAL=$((TOTAL + 1))
    echo "  ❌ Batch rejected due to conflict set collision (got HTTP $CONFLICT_HTTP, expected 400)"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Test 12.5: Batch Parity Semantic Check
# ============================================================================
echo "📋 Test 12.5: Batch Parity Semantic Check & Gate Proof"

sqlite3 "$TEST_DB" "UPDATE aether_repositories SET gate_config = '[{\"name\":\"batch_gate\",\"command\":\"cat src/b1.txt\",\"required\":true}]' WHERE id = '$REPO_ID';"
sqlite3 "$TEST_DB" "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, submitted_at, source) VALUES ('prop_batch1', '$REPO_ID', 'PENDING', 'batch-agt1', 'full_file', 'b1_content_marker', '[\"src/b1.txt\"]', 0.8, '2026-05-09T00:00:00Z', 'manual');"
sqlite3 "$TEST_DB" "INSERT INTO aether_proposals (id, repo_id, status, agent_id, change_type, change_content, affected_paths, confidence_score, submitted_at, source) VALUES ('prop_batch2', '$REPO_ID', 'PENDING', 'batch-agt2', 'full_file', 'b2_content_marker', '[\"src/b2.txt\"]', 0.9, '2026-05-09T00:00:00Z', 'manual');"

BATCH_PROMOTE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"prop_batch1\", \"prop_batch2\"]}")

BATCH_SUCCESS=$(echo "$BATCH_PROMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
assert_eq "Batch semantic promotion succeeded" "True" "$BATCH_SUCCESS"

BATCH_NULL_PROPOSAL_COUNT=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM aether_gate_results WHERE gate_name='batch_gate' AND proposal_id IS NULL;")
assert_eq "Batch gate results explicitly have proposal_id = NULL" "1" "$BATCH_NULL_PROPOSAL_COUNT"

BATCH_GATE_STDOUT=$(sqlite3 "$TEST_DB" "SELECT stdout FROM aether_gate_results WHERE gate_name='batch_gate' AND proposal_id IS NULL;" | tr -d '\n' | tr -d '\r')
assert_eq "Gate output proves it ran in candidate workspace" "b1_content_marker" "$BATCH_GATE_STDOUT"

# We should also check stored confidence score on this promotion record reflects the raw score
PROM_ID=$(echo "$BATCH_PROMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('promotion_id', ''))" 2>/dev/null || echo "")
CONFIDENCE_STORED=$(sqlite3 "$TEST_DB" "SELECT confidence_score FROM aether_promotions WHERE id='$PROM_ID';")
# The average of 0.8 and 0.9 is 0.85. The compute_score applies:
# (0.3 * 0.85) + (0.5 * 0.5) + (0.2 * 0.5) = 0.255 + 0.25 + 0.1 = 0.605
assert_eq "Stored promotion confidence reflects the raw_score" "0.605" "$CONFIDENCE_STORED"

sqlite3 "$TEST_DB" "UPDATE aether_repositories SET gate_config = '[]' WHERE id = '$REPO_ID';"
echo ""

# ============================================================================
# Test 13: Delta Snapshot Loading
# Create a manual delta snapshot head, promote a new change, and verify
# the old files carry over + new file exists.
# ============================================================================
echo "📋 Test 13: Delta Snapshot Loading"

DELTA_HEAD_ID="head_delta123"
DELTA_SNAP_ID="snap_delta123"
DELTA_DIR="$HOME_DIR/.aether/aether_storage/$REPO_ID/snapshots/$DELTA_SNAP_ID"
mkdir -p "$DELTA_DIR/src"
echo '{"mode":"delta","base_snapshot_id":"'"$SNAP_MULTI_ID"'","changed_paths":["src/delta.txt"]}' > "$DELTA_DIR/.aether_snapshot_manifest.json"
echo "delta content" > "$DELTA_DIR/src/delta.txt"

# Update DB to point to this manual delta head
sqlite3 "$TEST_DB" "INSERT INTO aether_snapshots (id, repo_id, head_sequence, storage_path, created_at, content_hash, file_count, total_bytes) VALUES ('$DELTA_SNAP_ID', '$REPO_ID', 10, '$DELTA_DIR', '2026-05-09T00:00:00Z', 'hash123', 1, 10);"
sqlite3 "$TEST_DB" "INSERT INTO aether_heads (id, repo_id, sequence, snapshot_id, is_canonical, created_at) VALUES ('$DELTA_HEAD_ID', '$REPO_ID', 10, '$DELTA_SNAP_ID', 1, '2026-05-09');"
sqlite3 "$TEST_DB" "UPDATE aether_repositories SET head_id = '$DELTA_HEAD_ID', head_sequence = 10 WHERE id = '$REPO_ID';"

DELTA_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-delta\",
        \"confidence_score\": 0.95,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"post delta\",
        \"affected_paths\": [\"src/post_delta.txt\"],
        \"source\": \"manual\"
    }")

DELTA_PROP_ID=$(echo "$DELTA_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")

DELTA_PROMOTE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$DELTA_PROP_ID\"]}")

DELTA_SUCCESS=$(echo "$DELTA_PROMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || echo "")
assert_eq "Post-delta promotion succeeded" "True" "$DELTA_SUCCESS"

HEAD_FINAL=$(curl -s "$BASE_URL/v1/repos/$REPO_ID/head")
SNAP_FINAL_ID=$(echo "$HEAD_FINAL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_id', ''))" 2>/dev/null || echo "")
SNAP_FINAL_DIR="$HOME_DIR/.aether/aether_storage/$REPO_ID/snapshots/$SNAP_FINAL_ID"

TOTAL=$((TOTAL + 3))
if [[ -f "$SNAP_FINAL_DIR/src/file1.txt" ]] && grep -q "multi file test" "$SNAP_FINAL_DIR/src/file1.txt"; then
    echo "  ✅ Base file carried over (delta resolution worked)"
    PASS=$((PASS + 1))
else
    echo "  ❌ Base file missing or wrong content! Delta resolution failed"
    FAIL=$((FAIL + 1))
fi

if [[ -f "$SNAP_FINAL_DIR/src/delta.txt" ]] && grep -q "delta content" "$SNAP_FINAL_DIR/src/delta.txt"; then
    echo "  ✅ Intermediate delta file carried over"
    PASS=$((PASS + 1))
else
    echo "  ❌ Intermediate delta file missing or wrong content!"
    FAIL=$((FAIL + 1))
fi

if [[ -f "$SNAP_FINAL_DIR/src/post_delta.txt" ]] && grep -q "post delta" "$SNAP_FINAL_DIR/src/post_delta.txt"; then
    echo "  ✅ New file materialized"
    PASS=$((PASS + 1))
else
    echo "  ❌ New file missing or wrong content!"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Test 14: No-op full_file
# Submit a full_file proposal with affected_paths = [] and ensure 400 Bad Request
# ============================================================================
echo "📋 Test 14: No-op full_file rejection"

NOOP_RESPONSE=$(curl -s -X POST "$BASE_URL/v1/repos/$REPO_ID/proposals" \
    -H "Content-Type: application/json" \
    -d "{
        \"agent_id\": \"test-agent-noop\",
        \"confidence_score\": 0.95,
        \"intent_type\": \"feature\",
        \"change_type\": \"full_file\",
        \"change_content\": \"noop\",
        \"affected_paths\": [],
        \"source\": \"manual\"
    }")

NOOP_PROP_ID=$(echo "$NOOP_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['proposal_id'])" 2>/dev/null || echo "")

NOOP_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/repos/$REPO_ID/promotions" \
    -H "Content-Type: application/json" \
    -d "{\"proposal_ids\": [\"$NOOP_PROP_ID\"]}")

if [[ "$NOOP_HTTP" == "400" ]]; then
    TOTAL=$((TOTAL + 1))
    echo "  ✅ No-op full_file rejected (HTTP 400)"
    PASS=$((PASS + 1))
else
    TOTAL=$((TOTAL + 1))
    echo "  ❌ No-op full_file rejected (got HTTP $NOOP_HTTP, expected 400)"
    FAIL=$((FAIL + 1))
fi
echo ""

# ============================================================================
# Summary
# ============================================================================
echo "============================================"
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
echo "============================================"

if [[ "$FAIL" -gt 0 ]]; then
    echo "❌ NATIVE PARITY TEST FAILED"
    exit 1
else
    echo "✅ NATIVE PARITY TEST PASSED"
    exit 0
fi
