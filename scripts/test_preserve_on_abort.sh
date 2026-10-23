#!/usr/bin/env bash
# ============================================================================
# Preserve-on-Abort E2E Test
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"
SOAK_HOME="${AETHER_SOAK_HOME:-$HOME/.aether/.soak_workspace_pg}"

echo "============================================"
echo "Testing Aether Preserve-On-Abort Harness Safeguard"
echo "============================================"

# Test Case 1: AETHER_PRESERVE_DB=1
echo "📋 Test Case 1: AETHER_PRESERVE_DB=1 (Explicit Preservation Override)"
OUTPUT_FILE_1=$(mktemp)

# Run a 2-second soak with preservation enabled
AETHER_PRESERVE_DB=1 SOAK_AGENTS=1 SOAK_RUN_SECONDS=2 SOAK_DRAIN_SECONDS=1 \
  "$PROJECT_DIR/scripts/soak_native_rust_pg.sh" > "$OUTPUT_FILE_1" 2>&1 || true

cat "$OUTPUT_FILE_1"

# Find the TEST_DB from the output
DB_NAME_1=$(grep -o "aether_soak_pg_[0-9]\+" "$OUTPUT_FILE_1" | head -n 1) || true

if [ -z "$DB_NAME_1" ]; then
    echo "❌ Failed to extract database name from soak logs!"
    rm -f "$OUTPUT_FILE_1"
    exit 1
fi

echo "→ Preserved Database Name detected: $DB_NAME_1"

# Check if the database still exists in Postgres
DB_EXISTS_1=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME_1';")

if [ "$DB_EXISTS_1" = "1" ]; then
    echo "✅ Success: Database $DB_NAME_1 was successfully preserved on disk!"
    
    # Clean up the preserved database
    echo "🧹 Cleaning up preserved test database..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME_1' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -c "DROP DATABASE IF EXISTS $DB_NAME_1;" >/dev/null
    echo "✅ Cleaned up successfully."
else
    echo "❌ Fail: Database $DB_NAME_1 was NOT preserved!"
    rm -f "$OUTPUT_FILE_1"
    exit 1
fi

rm -f "$OUTPUT_FILE_1"
echo ""

# Test Case 2: Autonomous Capacity Alarm Trigger (.aborted Sentinel Path)
echo "📋 Test Case 2: Autonomous Capacity Safety Alarm Trigger (.aborted Sentinel Path)"
rm -f "$SOAK_HOME/.aborted"
OUTPUT_FILE_2=$(mktemp)

# Run a soak with database limit set to 0 MB (guaranteed capacity abort on the first size poll)
# We set AETHER_PRESERVE_DB=0 or unset, so it relies entirely on the autonomous .aborted sentinel.
AETHER_PRESERVE_DB=0 AETHER_DB_SIZE_LIMIT_MB=0 SOAK_AGENTS=1 SOAK_RUN_SECONDS=10 SOAK_DRAIN_SECONDS=1 \
  "$PROJECT_DIR/scripts/soak_native_rust_pg.sh" > "$OUTPUT_FILE_2" 2>&1 || true

cat "$OUTPUT_FILE_2"

# A. Verify autonomous capacity abort was triggered in log output
if grep -q "⚠️ HARNESS CAPACITY ALARM" "$OUTPUT_FILE_2"; then
    echo "✅ Success: Harness capacity safety alarm was autonomously triggered!"
else
    echo "❌ Fail: Harness capacity safety alarm was NOT triggered in soak logs!"
    rm -f "$OUTPUT_FILE_2"
    exit 1
fi

# B. Verify .aborted sentinel file was touched
if [ -f "$SOAK_HOME/.aborted" ]; then
    echo "✅ Success: .aborted sentinel file was touched at $SOAK_HOME/.aborted!"
else
    echo "❌ Fail: .aborted sentinel file was NOT touched!"
    rm -f "$OUTPUT_FILE_2"
    exit 1
fi

# C. Find the TEST_DB from the output
DB_NAME_2=$(grep -o "aether_soak_pg_[0-9]\+" "$OUTPUT_FILE_2" | head -n 1) || true

if [ -z "$DB_NAME_2" ]; then
    echo "❌ Failed to extract database name from soak logs!"
    rm -f "$OUTPUT_FILE_2"
    exit 1
fi

echo "→ Preserved Database Name detected: $DB_NAME_2"

# D. Check if the database was successfully preserved because of the sentinel
DB_EXISTS_2=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME_2';")

if [ "$DB_EXISTS_2" = "1" ]; then
    echo "✅ Success: Database $DB_NAME_2 was successfully preserved on disk!"
    
    # Clean up the preserved database
    echo "🧹 Cleaning up preserved test database..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME_2' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -c "DROP DATABASE IF EXISTS $DB_NAME_2;" >/dev/null
    rm -f "$SOAK_HOME/.aborted"
    echo "✅ Cleaned up successfully."
else
    echo "❌ Fail: Database $DB_NAME_2 was NOT preserved on abort!"
    rm -f "$OUTPUT_FILE_2"
    exit 1
fi

rm -f "$OUTPUT_FILE_2"
echo ""

# Test Case 3: Stale Sentinel Clearing at Start-up
echo "📋 Test Case 3: Stale Sentinel Clearing at Start-up"
mkdir -p "$SOAK_HOME"
touch "$SOAK_HOME/.aborted"
OUTPUT_FILE_3=$(mktemp)

# Run a clean short soak run with preserve=0 and a large limit so it does not abort
AETHER_PRESERVE_DB=0 AETHER_DB_SIZE_LIMIT_MB=99999 SOAK_AGENTS=1 SOAK_RUN_SECONDS=2 SOAK_DRAIN_SECONDS=1 \
  "$PROJECT_DIR/scripts/soak_native_rust_pg.sh" > "$OUTPUT_FILE_3" 2>&1 || true

cat "$OUTPUT_FILE_3"

# Find the TEST_DB from the output
DB_NAME_3=$(grep -o "aether_soak_pg_[0-9]\+" "$OUTPUT_FILE_3" | head -n 1) || true

if [ -z "$DB_NAME_3" ]; then
    echo "❌ Failed to extract database name from soak logs!"
    rm -f "$OUTPUT_FILE_3"
    exit 1
fi

echo "→ Database Name detected: $DB_NAME_3"

# Check that the database was NOT preserved (it should be dropped at the end)
DB_EXISTS_3=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME_3';")

if [ "$DB_EXISTS_3" = "1" ]; then
    echo "❌ Fail: Database $DB_NAME_3 was preserved despite clean run! Stale sentinel was not cleared!"
    # Cleanup database just in case
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME_3' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -c "DROP DATABASE IF EXISTS $DB_NAME_3;" >/dev/null
    rm -f "$OUTPUT_FILE_3"
    exit 1
else
    echo "✅ Success: Database $DB_NAME_3 was dropped (stale sentinel successfully cleared and did not trigger false preservation)!"
fi

# Verify the sentinel file is no longer present
if [ -f "$SOAK_HOME/.aborted" ]; then
    echo "❌ Fail: .aborted sentinel file is still present at $SOAK_HOME/.aborted!"
    rm -f "$OUTPUT_FILE_3"
    exit 1
else
    echo "✅ Success: .aborted sentinel file was deleted at start-up!"
fi

rm -f "$OUTPUT_FILE_3"
echo ""

echo "============================================"
echo "🎉 ALL PRESERVE-ON-ABORT HARNESS TESTS PASSED!"
echo "============================================"

