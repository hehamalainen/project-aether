#!/usr/bin/env bash
# ============================================================================
# Native Rust Soak Driver — PostgreSQL Backend
#
# Exercises the full PG-compiled Rust native promotion path under sustained
# concurrent load. Every operation flows through the Rust PG binary:
#   POST /v1/repos/{id}/proposals   → Rust PG submit (intake + trust)
#   POST /v1/repos/{id}/promotions  → Rust PG native promotion (orchestrate +
#                                     gates + trust + CAS + head advance)
#
# Provisioning model follows scripts/test_pg_e2e.sh:
#   1. Create a fresh test database
#   2. Build and copy the PG release binary
#   3. Start server, run soak, audit
#   4. Tear down database and server on exit
#
# Usage:
#   ./scripts/soak_native_rust_pg.sh                     # canary
#   SOAK_AGENTS=20 SOAK_RUN_SECONDS=600 ./scripts/soak_native_rust_pg.sh
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configurable parameters ─────────────────────────────────────────────────
SOAK_AGENTS="${SOAK_AGENTS:-5}"
SOAK_REPOS="${SOAK_REPOS:-1}"
SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-120}"
SOAK_INTERVAL_MS="${SOAK_INTERVAL_MS:-1000}"
SOAK_WITH_GATES="${SOAK_WITH_GATES:-1}"
SOAK_DRAIN_SECONDS="${SOAK_DRAIN_SECONDS:-30}"
SOAK_DRAIN_POLL_INTERVAL="${SOAK_DRAIN_POLL_INTERVAL:-3}"
SOAK_HOME="${AETHER_SOAK_HOME:-$HOME/.aether/.soak_workspace_pg}"
BASE_URL="http://127.0.0.1:8100"

# PostgreSQL connection
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-$(whoami)}"
PG_SUPERDB="${PG_SUPERDB:-postgres}"
TEST_DB="aether_soak_pg_$$"
DATABASE_URL="postgres://${PG_USER}@${PG_HOST}:${PG_PORT}/${TEST_DB}"
START_TIME_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# ── Derived paths ───────────────────────────────────────────────────────────
SOAK_STORAGE="$SOAK_HOME/.aether/aether_storage"
SERVER_LOG="$SOAK_HOME/server.log"
SERVER_BINARY="$SOAK_HOME/server-pg"

# ── State ───────────────────────────────────────────────────────────────────
SERVER_PID=""
AGENT_PIDS=()
VACUUM_DAEMON_PID=""
REPO_IDS=()
COUNTER_DIR=""

# ── Cleanup ─────────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."

    # Kill vacuum daemon
    if [[ -f "${SOAK_HOME:-}/vacuum_running" ]]; then
        rm -f "${SOAK_HOME:-}/vacuum_running"
    fi
    if [[ -n "${VACUUM_DAEMON_PID:-}" ]]; then
        kill "$VACUUM_DAEMON_PID" 2>/dev/null || true
        wait "$VACUUM_DAEMON_PID" 2>/dev/null || true
    fi

    # Kill poller
    if [[ -n "${PG_POLLER_PID:-}" ]]; then
        kill "$PG_POLLER_PID" 2>/dev/null || true
        wait "$PG_POLLER_PID" 2>/dev/null || true
    fi
    if [[ -n "${PENDING_POLLER_PID:-}" ]]; then
        kill "$PENDING_POLLER_PID" 2>/dev/null || true
        wait "$PENDING_POLLER_PID" 2>/dev/null || true
    fi
    if [[ -n "${CAPACITY_POLLER_PID:-}" ]]; then
        kill "$CAPACITY_POLLER_PID" 2>/dev/null || true
        wait "$CAPACITY_POLLER_PID" 2>/dev/null || true
    fi

    # Kill agent loops first
    if [[ ${#AGENT_PIDS[@]} -gt 0 ]]; then
        for pid in "${AGENT_PIDS[@]}"; do
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        done
    fi

    # Kill server before dropping DB
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    sleep 1

    # Terminate lingering backends then conditionally drop DB
    local db_preserved="true"
    local cleanup_mode="preserved"

    if [ -f "${SOAK_HOME:-}/.aborted" ] || [ "${AETHER_PRESERVE_DB:-0}" = "1" ]; then
        cleanup_mode="aborted-preserved"
        db_preserved="true"
    elif [ "${AETHER_CLEANUP_DB:-0}" = "1" ]; then
        cleanup_mode="dropped"
        db_preserved="false"
    else
        cleanup_mode="preserved"
        db_preserved="true"
    fi

    if [ "$db_preserved" = "true" ]; then
        echo "🚨 Preserving database $TEST_DB for post-run forensics (Mode: $cleanup_mode)!"
    else
        echo "   Dropping test database $TEST_DB..."
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$TEST_DB' AND pid <> pg_backend_pid();" \
            2>/dev/null || true
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
            -c "DROP DATABASE IF EXISTS $TEST_DB;" \
            2>/dev/null || true
    fi

    # Emit manifest
    local end_time_iso
    end_time_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    if [ -d "${SOAK_HOME:-}" ]; then
        jq -n \
            --arg db_name "$TEST_DB" \
            --arg agents "$SOAK_AGENTS" \
            --arg repos "$SOAK_REPOS" \
            --arg run_seconds "$SOAK_RUN_SECONDS" \
            --arg interval_ms "$SOAK_INTERVAL_MS" \
            --arg with_gates "$SOAK_WITH_GATES" \
            --arg drain_seconds "$SOAK_DRAIN_SECONDS" \
            --arg start_time "${START_TIME_ISO:-}" \
            --arg end_time "$end_time_iso" \
            --arg soak_home "$SOAK_HOME" \
            --arg cleanup_mode "$cleanup_mode" \
            --argjson db_preserved "$db_preserved" \
            '{
                db_name: $db_name,
                soak_config: {
                    agents: ($agents | tonumber),
                    repos: ($repos | tonumber),
                    run_seconds: ($run_seconds | tonumber),
                    interval_ms: ($interval_ms | tonumber),
                    with_gates: ($with_gates | tonumber),
                    drain_seconds: ($drain_seconds | tonumber)
                },
                start_time: $start_time,
                end_time: $end_time,
                soak_home: $soak_home,
                cleanup_mode: $cleanup_mode,
                db_preserved: $db_preserved
            }' > "$SOAK_HOME/soak_manifest.json"
        echo "📝 Soak manifest written to $SOAK_HOME/soak_manifest.json"
    fi
}
trap cleanup EXIT INT TERM

# ── Preflight ───────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║       Aether Native Rust Soak Driver — PostgreSQL              ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Agents:       $SOAK_AGENTS"
echo "  Repos:        $SOAK_REPOS"
echo "  Duration:     ${SOAK_RUN_SECONDS}s + ${SOAK_DRAIN_SECONDS}s drain"
echo "  Interval:     ${SOAK_INTERVAL_MS}ms per agent"
echo "  Gates:        $([ "$SOAK_WITH_GATES" = "1" ] && echo "enabled" || echo "disabled")"
echo "  Soak home:    $SOAK_HOME"
echo "  PG:           $PG_USER@$PG_HOST:$PG_PORT/$TEST_DB"
echo ""

for cmd in psql curl jq bc pg_isready; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required command '$cmd' not found on PATH"; exit 1
    fi
done

if [[ "${AETHER_ALLOW_NON_DIVISIBLE_TOPOLOGY:-0}" != "1" ]]; then
    if (( SOAK_AGENTS % SOAK_REPOS != 0 )); then
        echo "❌ Topology Sanity Failure: SOAK_AGENTS ($SOAK_AGENTS) must be evenly divisible by SOAK_REPOS ($SOAK_REPOS)"
        exit 1
    fi
fi

if ! pg_isready -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
    echo "❌ PostgreSQL not accepting connections at $PG_HOST:$PG_PORT"; exit 1
fi

# ── Build PG binary and isolate artifact ────────────────────────────────────
echo "🔨 Building aether-rs (db-postgres, release)..."
(cd "$PROJECT_DIR" && cargo build --manifest-path aether-rs/Cargo.toml \
    --release --no-default-features --features db-postgres --quiet 2>&1)
echo "   ✅ Build succeeded"

if [ -d "$SOAK_HOME" ]; then
    PREV_DB="unknown"
    if [ -f "$SOAK_HOME/soak_manifest.json" ]; then
        PREV_DB=$(jq -r '.db_name // "unknown"' "$SOAK_HOME/soak_manifest.json" 2>/dev/null || echo "unknown")
    fi
    BACKUP_DIR="${SOAK_HOME}_backups/run_${PREV_DB}_$(date -u +%Y%m%d_%H%M%S)"
    echo "📦 Archiving previous run workspace to $BACKUP_DIR..."
    mkdir -p "$(dirname "$BACKUP_DIR")"
    mv "$SOAK_HOME" "$BACKUP_DIR"
fi
mkdir -p "$SOAK_HOME/.aether"

# Copy binary to isolated path so future builds don't clobber it
echo "   ✅ Copying binary..."; cp "$PROJECT_DIR/aether-rs/target/release/server" "$SERVER_BINARY"
echo "   Binary: $SERVER_BINARY"

# ── Provision PG database ──────────────────────────────────────────────────
echo "📦 Creating test database $TEST_DB..."
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
    -c "CREATE DATABASE $TEST_DB;" 2>&1 | grep -v "^$"
echo "   ✅ Database created"

# ── Start PG server with isolated HOME ──────────────────────────────────────
echo "🚀 Starting Rust server (PostgreSQL, release)..."
CRDT_MODE=""
if [[ "${SOAK_CRDT_ENABLED:-0}" == "3" ]]; then
    CRDT_MODE="token"
elif [[ "${SOAK_CRDT_ENABLED:-0}" == "2" ]]; then
    CRDT_MODE="frontier"
elif [[ "${SOAK_CRDT_ENABLED:-0}" == "1" ]]; then
    CRDT_MODE="advisory"
fi

HOME="$SOAK_HOME" \
DATABASE_URL="$DATABASE_URL" \
RUST_LOG="info" \
AETHER_SOAK_TELEMETRY="1" \
AETHER_SOAK_TELEMETRY_LOG="$SOAK_HOME/pool_telemetry.log" \
AETHER_CRDT_MODE="$CRDT_MODE" \
DATABASE_MAX_CONNECTIONS="${DATABASE_MAX_CONNECTIONS:-}" \
AETHER_MAX_COHERENCE_BACKLOG="${AETHER_MAX_COHERENCE_BACKLOG:-}" \
AETHER_MAX_PATH_ACTIVE_PROPOSALS="${AETHER_MAX_PATH_ACTIVE_PROPOSALS:-}" \
"$SERVER_BINARY" > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "   PID: $SERVER_PID"
echo "   Log: $SERVER_LOG"

# Wait for health + assert it's actually PG
echo -n "   Waiting for health..."
for i in $(seq 1 30); do
    if curl -sf "$BASE_URL/v1/health" >/dev/null 2>&1; then
        echo " ✅ healthy (${i}s)"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo " ❌ server exited prematurely"
        tail -20 "$SERVER_LOG" 2>/dev/null || true; exit 1
    fi
    sleep 1; echo -n "."
done
if ! curl -sf "$BASE_URL/v1/health" >/dev/null 2>&1; then
    echo " ❌ health check failed after 30s"
    tail -20 "$SERVER_LOG" 2>/dev/null || true; exit 1
fi

# Assert PG backend via exact listener line (db_postgres.rs:93-96)
sleep 2
if ! grep -q "Aether-RS Server (PostgreSQL) listening on" "$SERVER_LOG"; then
    echo "❌ Server log missing exact PG listener line — wrong binary!"
    echo "   Expected: 'Aether-RS Server (PostgreSQL) listening on'"
    tail -10 "$SERVER_LOG"; exit 1
fi
echo "   ✅ Confirmed PostgreSQL backend (exact listener match)"

# ── Create repos ─────────────────────────────────────────────────────────────
echo "📦 Creating $SOAK_REPOS soak repos..."
for r in $(seq 1 "$SOAK_REPOS"); do
    CREATE_RESPONSE=$(curl -sf -X POST "$BASE_URL/v1/repos" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"pg-native-soak-repo-$r\"}")
    R_ID=$(echo "$CREATE_RESPONSE" | jq -r '.id')
    if [[ -z "$R_ID" || "$R_ID" == "null" ]]; then
        echo "❌ Failed to create repo $r"; echo "   Response: $CREATE_RESPONSE"; exit 1
    fi
    REPO_IDS+=("$R_ID")

    if [[ "$SOAK_WITH_GATES" == "1" ]]; then
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -c \
            "UPDATE aether_repositories SET gate_config = \$gc\$[{\"name\":\"soak_gate\",\"command\":\"echo SOAK_GATE_OK\",\"required\":true,\"timeout_seconds\":10}]\$gc\$ WHERE id = '$R_ID';" >/dev/null
    fi

    if [[ "${SOAK_CRDT_ENABLED:-0}" -ge "1" ]]; then
        echo "   [CRDT] Enabling crdt_enabled for repo $R_ID (value: $SOAK_CRDT_ENABLED)..."
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -c \
            "UPDATE aether_repositories SET crdt_enabled = true WHERE id = '$R_ID';" >/dev/null
    fi
done
echo "   ✅ Created ${#REPO_IDS[@]} repos"

if [[ "${AETHER_APPLY_TOAST_TUNING:-0}" == "1" ]]; then
    echo "   [TUNING] Applying aggressive TOAST and autovacuum overrides to public.aether_head_frontiers..."
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -f scripts/apply_toast_tuning.sql >/dev/null
fi

if [[ "${AETHER_RUN_FORCED_VACUUM:-0}" == "1" ]]; then
    echo "   [VACUUM] Starting background forced vacuum daemon..."
    touch "$SOAK_HOME/vacuum_running"
    (
        set -e
        interval=5
        log_file="$SOAK_HOME/forced_vacuum.log"
        echo "Forced Vacuum Daemon started for DB $TEST_DB" > "$log_file"
        tick=0
        while [ -f "$SOAK_HOME/vacuum_running" ]; do
            tick=$((tick + 1))
            start_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
            start_sec=$SECONDS
            
            echo "------------------------------------------------------------" >> "$log_file"
            echo "[$start_time] [Tick $tick] Starting Telemetry Collection" >> "$log_file"
            echo "------------------------------------------------------------" >> "$log_file"
            
            # Log oldest active transactions blocking cleanup
            echo "[$start_time] [Tick $tick] [TELEMETRY] Oldest Active Transactions (backend_xmin & xact_start):" >> "$log_file"
            psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$TEST_DB" -c "
                SELECT pid, age(backend_xmin) AS xmin_age, age(xact_start) AS xact_age, xact_start, state, substring(query from 1 for 80) AS query_summary
                FROM pg_stat_activity 
                WHERE backend_xmin IS NOT NULL OR state != 'idle'
                ORDER BY COALESCE(age(backend_xmin), 0) DESC, xact_start ASC 
                LIMIT 5;
            " >> "$log_file" 2>&1

            # Log vacuum progress if any vacuum is currently running
            echo "[$start_time] [Tick $tick] [TELEMETRY] Vacuum Progress (pg_stat_progress_vacuum):" >> "$log_file"
            psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$TEST_DB" -c "
                SELECT v.pid, age(a.backend_xmin) as xmin_age, v.phase, v.heap_blks_total, v.heap_blks_scanned, v.heap_blks_vacuumed, v.index_vacuum_count, v.num_dead_tuples
                FROM pg_stat_progress_vacuum v
                JOIN pg_stat_activity a ON a.pid = v.pid;
            " >> "$log_file" 2>&1

            echo "[$start_time] [Tick $tick] Attempting VACUUM VERBOSE public.aether_head_frontiers..." >> "$log_file"
            
            # Execute vacuum verbose with timeout set via PGOPTIONS to avoid transaction block error
            if PGOPTIONS="-c statement_timeout=4000" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$TEST_DB" \
                -c "VACUUM VERBOSE public.aether_head_frontiers;" >> "$log_file" 2>&1; then
                end_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
                duration=$((SECONDS - start_sec))
                completed_before="Yes"
                if [ "$duration" -ge "$interval" ]; then
                    completed_before="No"
                fi
                echo "[$end_time] [Tick $tick] SUCCESS. Duration: ${duration}s. Completed before next: $completed_before" >> "$log_file"
            else
                end_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
                duration=$((SECONDS - start_sec))
                echo "[$end_time] [Tick $tick] FAILURE. Duration: ${duration}s." >> "$log_file"
            fi
            
            elapsed=$((SECONDS - start_sec))
            sleep_time=$((interval - elapsed))
            if [ "$sleep_time" -lt 0 ]; then
                sleep_time=0
            fi
            sleep "$sleep_time"
        done
        echo "Forced Vacuum Daemon stopped." >> "$log_file"
    ) >/dev/null 2>&1 &
    VACUUM_DAEMON_PID=$!
fi

COUNTER_DIR="$(mktemp -d "$SOAK_HOME/counters.XXXXXX")"

# ── Agent loop function ────────────────────────────────────────────────────
run_agent() {
    local agent_id="$1" agent_counter_dir="$2" repo_id="$3"
    local interval_s
    interval_s=$(echo "scale=3; $SOAK_INTERVAL_MS / 1000" | bc)
    local submit_ok=0 submit_fail=0
    local promote_ok=0 promote_contention=0 promote_rejected=0 promote_5xx=0
    local seq=0
    local end_time=$((SECONDS + SOAK_RUN_SECONDS))

    while [[ $SECONDS -lt $end_time ]]; do
        seq=$((seq + 1))
        local file_path="src/agent_${agent_id}_file.py"
        local content="\"\"\"Soak module: ${file_path}\"\"\"
# agent ${agent_id} tick ${seq}
value_${seq} = ${seq}
"
        # Submit
        local submit_response submit_http
        submit_response=$(curl --connect-timeout 5 --max-time 45 -s -w "\n%{http_code}" -X POST \
            "$BASE_URL/v1/repos/$repo_id/proposals" \
            -H "Content-Type: application/json" \
            -d "{
                \"agent_id\": \"soak_agent_${agent_id}\",
                \"task_id\": \"soak-${agent_id}-${seq}\",
                \"source\": \"native_soak_pg\",
                \"intent_type\": \"soak_update\",
                \"change_type\": \"full_file\",
                \"change_content\": $(echo "$content" | jq -Rs .),
                \"affected_paths\": [\"${file_path}\"],
                \"confidence_score\": 0.85,
                \"rationale\": \"pg soak tick ${seq}\"
            }" 2>/dev/null) || true

        submit_http=$(echo "$submit_response" | tail -1)
        local submit_body
        submit_body=$(echo "$submit_response" | sed '$d')

        if [[ "$submit_http" == "200" || "$submit_http" == "201" ]]; then
            submit_ok=$((submit_ok + 1))
        else
            submit_fail=$((submit_fail + 1)); sleep "$interval_s"; continue
        fi

        local prop_id
        prop_id=$(echo "$submit_body" | jq -r '.proposal_id // empty' 2>/dev/null) || true
        if [[ -z "$prop_id" ]]; then
            submit_fail=$((submit_fail + 1)); sleep "$interval_s"; continue
        fi

        # Promote (native PG path)
        local promote_response promote_http
        promote_response=$(curl --connect-timeout 5 --max-time 45 -s -w "\n%{http_code}" -X POST \
            "$BASE_URL/v1/repos/$repo_id/promotions" \
            -H "Content-Type: application/json" \
            -d "{\"proposal_ids\": [\"${prop_id}\"]}" 2>/dev/null) || true

        promote_http=$(echo "$promote_response" | tail -1)
        local promote_body
        promote_body=$(echo "$promote_response" | sed '$d')

        local promote_body_jsonl
        promote_body_jsonl=$(echo "$promote_body" | jq -c '.' 2>/dev/null) || true
        if [[ -n "$promote_body_jsonl" && "$promote_body_jsonl" != "null" ]]; then
            echo "$promote_body_jsonl" >> "$agent_counter_dir/promotions.jsonl"
        fi

        if [[ "$promote_http" == "200" ]]; then
            local success
            success=$(echo "$promote_body" | jq -r '.success // false' 2>/dev/null) || true
            if [[ "$success" == "true" ]]; then
                promote_ok=$((promote_ok + 1))
            else
                promote_rejected=$((promote_rejected + 1))
            fi
        elif [[ "$promote_http" == "409" ]]; then
            promote_contention=$((promote_contention + 1))
        elif [[ "$promote_http" -ge 500 ]]; then
            promote_5xx=$((promote_5xx + 1))
        else
            promote_rejected=$((promote_rejected + 1))
        fi
        sleep "$interval_s"
    done

    echo "$submit_ok" > "$agent_counter_dir/submit_ok"
    echo "$submit_fail" > "$agent_counter_dir/submit_fail"
    echo "$promote_ok" > "$agent_counter_dir/promote_ok"
    echo "$promote_contention" > "$agent_counter_dir/promote_contention"
    echo "$promote_rejected" > "$agent_counter_dir/promote_rejected"
    echo "$promote_5xx" > "$agent_counter_dir/promote_5xx"
}

# ── Launch poller ──────────────────────────────────────────────────────────
echo "📊 Launching pg_stat_activity poller..."
(
    while true; do
        TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" \
            -t -A -c "SELECT '$TS', state, wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE datname = '$TEST_DB' GROUP BY state, wait_event_type, wait_event;" \
            >> "$SOAK_HOME/pg_stats.log" 2>/dev/null || true
        sleep 5
    done
) >/dev/null 2>&1 &
PG_POLLER_PID=$!

echo "📊 Launching PENDING backlog poller (15s interval, persistent connection)..."
(
    while true; do
        echo "SELECT EXTRACT(EPOCH FROM now()) as ts, COUNT(*) as c FROM aether_proposals WHERE status = 'PENDING';"
        sleep 15
    done | psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -t -A >> "$SOAK_HOME/pending_log.txt" 2>/dev/null
) >/dev/null 2>&1 &
PENDING_POLLER_PID=$!

echo "📊 Launching host-capacity safety poller (15s interval)..."
(
    # Create capacity log header if empty
    if [ ! -f "$SOAK_HOME/capacity_telemetry.log" ]; then
        echo "timestamp|db_size_mb|free_space_mb" > "$SOAK_HOME/capacity_telemetry.log"
    fi

    while true; do
        TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        
        # 1. Query database size in megabytes
        DB_BYTES=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_SUPERDB" -t -A -c "SELECT pg_database_size('$TEST_DB');" 2>/dev/null || echo "0")
        if [[ ! "$DB_BYTES" =~ ^[0-9]+$ ]]; then
            DB_BYTES=0
        fi
        DB_SIZE_MB=$(( DB_BYTES / 1024 / 1024 ))
        
        # 2. Query available host free space in megabytes
        FREE_SPACE_MB=$(df -m "$SOAK_HOME" | tail -n 1 | awk '{print $4}')
        if [[ ! "$FREE_SPACE_MB" =~ ^[0-9]+$ ]]; then
            FREE_SPACE_MB=0
        fi
        
        # Append telemetry
        echo "$TS|$DB_SIZE_MB|$FREE_SPACE_MB" >> "$SOAK_HOME/capacity_telemetry.log"
        
        # Assert thresholds:
        #   - Max DB size safety budget: AETHER_DB_SIZE_LIMIT_MB (default: 1000 MB / 1 GB)
        #   - Min host free space floor: 5120 MB (5 GB)
        DB_LIMIT_MB="${AETHER_DB_SIZE_LIMIT_MB:-1000}"
        if [ "$DB_SIZE_MB" -gt "$DB_LIMIT_MB" ]; then
            echo "⚠️ HARNESS CAPACITY ALARM: Dynamic PostgreSQL database $TEST_DB has grown to $DB_SIZE_MB MB, exceeding the $DB_LIMIT_MB MB safety budget!" | tee -a "$SOAK_HOME/server.log"
            echo "TRIGGERING IMMEDIATE AUTONOMOUS ABORT TO PREVENT HOST RESOURCE EXHAUSTION..." | tee -a "$SOAK_HOME/server.log"
            touch "$SOAK_HOME/.aborted"
            kill -s TERM "$$"
            exit 88
        fi
        
        MIN_FREE_SPACE_MB="${AETHER_MIN_FREE_SPACE_MB:-5120}"
        if [ "$FREE_SPACE_MB" -lt "$MIN_FREE_SPACE_MB" ]; then
            echo "⚠️ HARNESS CAPACITY ALARM: Host available free space has dropped to $FREE_SPACE_MB MB, below the $MIN_FREE_SPACE_MB MB safety floor!" | tee -a "$SOAK_HOME/server.log"
            echo "TRIGGERING IMMEDIATE AUTONOMOUS ABORT TO PREVENT HOST CRASH!" | tee -a "$SOAK_HOME/server.log"
            touch "$SOAK_HOME/.aborted"
            kill -s TERM "$$"
            exit 88
        fi
        
        sleep 15
    done
) >/dev/null 2>&1 &
CAPACITY_POLLER_PID=$!

# ── Launch agent loops ──────────────────────────────────────────────────────
echo ""
echo "🏃 Launching $SOAK_AGENTS concurrent agent loops for ${SOAK_RUN_SECONDS}s..."
echo ""
START_EPOCH=$(date +%s)
GLOBAL_END_TIME=$(( START_EPOCH + SOAK_RUN_SECONDS ))

PYTHON_BIN="python3"
if [ -f "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
elif [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
fi

CONTAMINATED=0

for i in $(seq 1 "$SOAK_AGENTS"); do
    agent_dir="$COUNTER_DIR/agent_$i"
    mkdir -p "$agent_dir"
    if [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "skew" ]]; then
        # Hot-repo skew: first 62 agents target repo index 0, remaining 18 target repo index 1..9
        if [ "$i" -le 62 ]; then
            assigned_repo="${REPO_IDS[0]}"
        else
            assigned_repo="${REPO_IDS[$(( 1 + (i - 63) % 9 ))]}"
        fi
    elif [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "moderate_skew" ]]; then
        # moderate_skew: distribute 160 agents across 10 repos
        if [ "$i" -le 40 ]; then
            assigned_repo="${REPO_IDS[0]}"
        elif [ "$i" -le 65 ]; then
            assigned_repo="${REPO_IDS[1]}"
        elif [ "$i" -le 90 ]; then
            assigned_repo="${REPO_IDS[2]}"
        elif [ "$i" -le 105 ]; then
            assigned_repo="${REPO_IDS[3]}"
        elif [ "$i" -le 120 ]; then
            assigned_repo="${REPO_IDS[4]}"
        elif [ "$i" -le 128 ]; then
            assigned_repo="${REPO_IDS[5]}"
        elif [ "$i" -le 136 ]; then
            assigned_repo="${REPO_IDS[6]}"
        elif [ "$i" -le 144 ]; then
            assigned_repo="${REPO_IDS[7]}"
        elif [ "$i" -le 152 ]; then
            assigned_repo="${REPO_IDS[8]}"
        else
            assigned_repo="${REPO_IDS[9]}"
        fi
    elif [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "100repos" ]]; then
        # 100repos mode: agent i gets repo index i-1 (80 agents target the first 80 repos of 100)
        assigned_repo="${REPO_IDS[$(( i - 1 ))]}"
    else
        assigned_repo="${REPO_IDS[$(( (i - 1) % SOAK_REPOS ))]}"
    fi

    # Distribute agents across 5 target files per repository
    file_idx=$(( (i - 1) % 5 ))
    file_path="src/module_${file_idx}.py"

    "$PYTHON_BIN" "$PROJECT_DIR/scripts/agent_client.py" \
        --repo-id "$assigned_repo" \
        --agent-id "soak_agent_${i}" \
        --end-time "$GLOBAL_END_TIME" \
        --interval-ms "$SOAK_INTERVAL_MS" \
        --base-url "$BASE_URL" \
        --soak-home "$SOAK_HOME" \
        --agent-dir "$agent_dir" \
        --file-path "$file_path" > "$agent_dir/agent.log" 2>&1 &
    AGENT_PIDS+=($!)
done

# ── Wait for agents with progress ──────────────────────────────────────────
echo -n "   Running"
while true; do
    if [[ "${AETHER_RUN_FORCED_VACUUM:-0}" == "1" ]]; then
        if ! kill -0 "$VACUUM_DAEMON_PID" 2>/dev/null; then
            echo ""
            echo "❌ ERROR: Background forced vacuum daemon crashed/exited unexpectedly!"
            exit 1
        fi
    fi

    alive=0
    for pid in "${AGENT_PIDS[@]}"; do
        kill -0 "$pid" 2>/dev/null && alive=$((alive + 1))
    done
    [[ $alive -eq 0 ]] && break
    elapsed=$(($(date +%s) - START_EPOCH))
    echo -ne "\r   Running... ${elapsed}s / ${SOAK_RUN_SECONDS}s (${alive} agents alive)  "
    
    # Safety Overrun Check: if agents overrun the duration by more than 45s grace, terminate them and mark contaminated
    if [[ $elapsed -gt $((SOAK_RUN_SECONDS + 45)) ]]; then
        echo ""
        echo "⚠️ WARNING: SAFETY OVERRUN DETECTED! Terminating remaining agents..."
        CONTAMINATED=1
        for pid in "${AGENT_PIDS[@]}"; do
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        done
        break
    fi
    
    sleep 5
done
echo ""
echo "   ✅ All agents completed"

for pid in "${AGENT_PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
AGENT_PIDS=()

# Drain phase: poll until PENDING stabilizes.
# NOTE: Proposals whose promotions were contended (409) or rejected stay PENDING
# permanently since there is no auto-promoter. We check that PENDING stopped
# growing (stabilized), not that it reached zero.
echo ""
echo "⏳ Drain phase: polling PENDING for up to ${SOAK_DRAIN_SECONDS}s..."
drain_deadline=$((SECONDS + SOAK_DRAIN_SECONDS))
pending_at_drain=999999
prev_pending=999999
stable_count=0

while [[ $SECONDS -lt $drain_deadline ]]; do
    pending_at_drain=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -t -A \
        -c "SELECT COUNT(*) FROM aether_proposals WHERE status = 'PENDING';" 2>/dev/null) || pending_at_drain="999999"
    echo "   PENDING=$pending_at_drain (${SECONDS}s)"
    if [[ "$pending_at_drain" -eq "$prev_pending" ]]; then
        stable_count=$((stable_count + 1))
        if [[ $stable_count -ge 3 ]]; then
            echo "   ✅ Backlog stabilized at $pending_at_drain"
            break
        fi
    else
        stable_count=0
    fi
    prev_pending=$pending_at_drain
    sleep "$SOAK_DRAIN_POLL_INTERVAL"
done

END_EPOCH=$(date +%s)
ELAPSED=$((END_EPOCH - START_EPOCH))

# ── Aggregate counters ──────────────────────────────────────────────────────
echo ""
echo "📊 Aggregating results..."

total_submit_ok=0; total_submit_fail=0; total_submit_429=0
total_promote_ok=0; total_promote_contention=0
total_promote_rejected=0; total_promote_5xx=0; total_success_after_409=0
total_conflicted_ticks=0

for r in $(seq 0 $((SOAK_REPOS-1))); do
    repo_submit_ok[$r]=0; repo_promote_ok[$r]=0; repo_promote_409[$r]=0
    repo_promote_rej[$r]=0; repo_promote_5xx[$r]=0; repo_submit_fail[$r]=0
    repo_submit_429[$r]=0; repo_success_after_409[$r]=0; repo_conflicted_ticks[$r]=0
done

for i in $(seq 1 "$SOAK_AGENTS"); do
    if [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "skew" ]]; then
        if [ "$i" -le 62 ]; then
            r_idx=0
        else
            r_idx=$(( 1 + (i - 63) % 9 ))
        fi
    elif [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "moderate_skew" ]]; then
        if [ "$i" -le 40 ]; then r_idx=0
        elif [ "$i" -le 65 ]; then r_idx=1
        elif [ "$i" -le 90 ]; then r_idx=2
        elif [ "$i" -le 105 ]; then r_idx=3
        elif [ "$i" -le 120 ]; then r_idx=4
        elif [ "$i" -le 128 ]; then r_idx=5
        elif [ "$i" -le 136 ]; then r_idx=6
        elif [ "$i" -le 144 ]; then r_idx=7
        elif [ "$i" -le 152 ]; then r_idx=8
        else r_idx=9; fi
    elif [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "100repos" ]]; then
        r_idx=$(( i - 1 ))
    else
        r_idx=$(( (i - 1) % SOAK_REPOS ))
    fi
    d="$COUNTER_DIR/agent_$i"
    
    s_ok=$(cat "$d/submit_ok" 2>/dev/null || echo 0)
    s_429=$(cat "$d/submit_429" 2>/dev/null || echo 0)
    s_fail=$(cat "$d/submit_fail" 2>/dev/null || echo 0)
    p_ok=$(cat "$d/promote_ok" 2>/dev/null || echo 0)
    p_409=$(cat "$d/promote_409" 2>/dev/null || echo 0)
    p_rej=$(cat "$d/promote_rejected" 2>/dev/null || echo 0)
    p_5xx=$(cat "$d/promote_5xx" 2>/dev/null || echo 0)
    p_succ_after_409=$(cat "$d/success_after_409" 2>/dev/null || echo 0)
    c_ticks=$(cat "$d/conflicted_ticks" 2>/dev/null || echo 0)

    total_submit_ok=$((total_submit_ok + s_ok))
    total_submit_429=$((total_submit_429 + s_429))
    total_submit_fail=$((total_submit_fail + s_fail))
    total_promote_ok=$((total_promote_ok + p_ok))
    total_promote_contention=$((total_promote_contention + p_409))
    total_promote_rejected=$((total_promote_rejected + p_rej))
    total_promote_5xx=$((total_promote_5xx + p_5xx))
    total_success_after_409=$((total_success_after_409 + p_succ_after_409))
    total_conflicted_ticks=$((total_conflicted_ticks + c_ticks))
    
    repo_submit_ok[$r_idx]=$((repo_submit_ok[$r_idx] + s_ok))
    repo_submit_429[$r_idx]=$((repo_submit_429[$r_idx] + s_429))
    repo_submit_fail[$r_idx]=$((repo_submit_fail[$r_idx] + s_fail))
    repo_promote_ok[$r_idx]=$((repo_promote_ok[$r_idx] + p_ok))
    repo_promote_409[$r_idx]=$((repo_promote_409[$r_idx] + p_409))
    repo_promote_rej[$r_idx]=$((repo_promote_rej[$r_idx] + p_rej))
    repo_promote_5xx[$r_idx]=$((repo_promote_5xx[$r_idx] + p_5xx))
    repo_success_after_409[$r_idx]=$((repo_success_after_409[$r_idx] + p_succ_after_409))
    repo_conflicted_ticks[$r_idx]=$((repo_conflicted_ticks[$r_idx] + c_ticks))
done

# ── Server alive check ─────────────────────────────────────────────────────
server_alive="yes"
kill -0 "$SERVER_PID" 2>/dev/null || server_alive="no"

# ── Post-run DB audit queries (global across repos) ──────────────────────────
echo ""
echo "🔍 Post-run audit (across $SOAK_REPOS repos)..."

PQ() { local r; r=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$TEST_DB" -t -A -c "$1" 2>/dev/null) || r="-1"; echo "$r"; }

head_seq=$(PQ "SELECT COALESCE(SUM(head_sequence), 0) FROM aether_repositories;")
promo_count=$(PQ "SELECT COUNT(*) FROM aether_promotions;")
snapshot_count_db=$(PQ "SELECT COUNT(*) FROM aether_snapshots;")
trust_agent_count=$(PQ "SELECT COUNT(DISTINCT agent_id) FROM aether_agent_metrics WHERE agent_id LIKE 'soak_agent_%';")
trust_avg=$(PQ "SELECT 'alpha=' || ROUND(AVG(trust_alpha)::numeric, 2) || ' beta=' || ROUND(AVG(trust_beta)::numeric, 2) FROM aether_agent_metrics WHERE agent_id LIKE 'soak_agent_%';")
proposal_status=$(PQ "SELECT status || '|' || COUNT(*) FROM aether_proposals GROUP BY status;")
timings_db_count=$(PQ "SELECT COUNT(*) FROM aether_promotions WHERE evidence LIKE '%_timings_ms%';")

# Per-repo performance violations
# Extract T (median latency) from structured server logs
T_SOURCE="measured"
T_ms=$(grep "latency_ms=" "$SERVER_LOG" 2>/dev/null | awk -F'latency_ms=' '{print $2}' | awk '{print $1}' | sed 's/[^0-9.]//g' | sort -n | awk ' { a[i++]=$1; } END { print a[int(i/2)]; }' || true)
if [[ -z "$T_ms" || "$T_ms" == "" ]]; then
    T_SOURCE="fallback"
    T="0.175"
else
    T=$(echo "scale=4; $T_ms / 1000" | bc 2>/dev/null || echo "0.175")
fi

if [ "$SOAK_REPOS" -gt 0 ]; then
    agents_per_repo=$((SOAK_AGENTS / SOAK_REPOS))
    if [ "$agents_per_repo" -eq 0 ]; then
        agents_per_repo=1
    fi
else
    agents_per_repo=1
fi
interval_s=$(echo "scale=4; $SOAK_INTERVAL_MS / 1000" | bc)
max_409_rate=$(echo "scale=4; 1 - e( -($agents_per_repo - 1) * $T / $interval_s ) + 0.10" | bc -l)

echo "  ── Performance Gate Math ──────────────────────"
echo "  T Source:              $T_SOURCE"
echo "  T (median latency):    ${T}s"
echo "  Agents/Repo (N):       $agents_per_repo"
echo "  Interval (I):          ${interval_s}s"
echo "  Max 409 limit:         $max_409_rate"
echo "  ───────────────────────────────────────────────"

if [[ "$T_SOURCE" == "fallback" ]]; then
    echo "  [WARNING] Using empirical fallback latency for Gate! This blocks strict confidence."
    echo "  ───────────────────────────────────────────────"
fi
echo ""

per_repo_perf_violations=0
per_repo_diagnostics=""
for r in $(seq 0 $((SOAK_REPOS-1))); do
    s_ok=${repo_submit_ok[$r]}
    s_fail=${repo_submit_fail[$r]}
    p_ok=${repo_promote_ok[$r]}
    p_409=${repo_promote_409[$r]}
    p_rej=${repo_promote_rej[$r]}
    p_5xx=${repo_promote_5xx[$r]}
    
    repo_violation=0
    
    if [[ $p_5xx -gt 0 || $p_rej -gt 0 || $s_fail -gt 0 ]]; then
        per_repo_perf_violations=$((per_repo_perf_violations + 1))
        repo_violation=1
    fi
    
    actual_superseded=$(PQ "SELECT COUNT(*) FROM aether_proposals WHERE status = 'SUPERSEDED' AND repo_id = '${REPO_IDS[$r]}';")
    expected_pending=$((s_ok - p_ok - actual_superseded))
    actual_pending=$(PQ "SELECT COUNT(*) FROM aether_proposals WHERE status = 'PENDING' AND repo_id = '${REPO_IDS[$r]}';")
    
    delta=$((actual_pending - expected_pending))
    abs_delta=$delta
    if [[ $abs_delta -lt 0 ]]; then abs_delta=$((-abs_delta)); fi
    tolerance="${AETHER_SOAK_BACKLOG_TOLERANCE:-0}"
    if [[ $abs_delta -gt $tolerance ]]; then
        per_repo_perf_violations=$((per_repo_perf_violations + 1))
        repo_violation=1
    fi
    
    agents_r=$agents_per_repo
    if [[ "${AETHER_TOPOLOGY_MODE:-uniform}" == "moderate_skew" ]]; then
        if [ "$r" -eq 0 ]; then agents_r=40
        elif [ "$r" -eq 1 ] || [ "$r" -eq 2 ]; then agents_r=25
        elif [ "$r" -eq 3 ] || [ "$r" -eq 4 ]; then agents_r=15
        else agents_r=8; fi
    fi
    max_409_r=$(echo "scale=4; 1 - e( -($agents_r - 1) * $T / $interval_s ) + 0.10" | bc -l 2>/dev/null || echo "1.0")

    rate="0"
    total_p=$((p_ok + p_409 + p_rej + p_5xx))
    if [[ $total_p -gt 0 ]]; then
        rate=$(echo "scale=4; $p_409 / $total_p" | bc || echo "0")
        if (( $(echo "$rate > $max_409_r" | bc -l) )); then
            per_repo_perf_violations=$((per_repo_perf_violations + 1))
            repo_violation=1
        fi
    fi
    
    per_repo_diagnostics="${per_repo_diagnostics}\n    Repo $r: s_ok=$s_ok s_fail=$s_fail p_ok=$p_ok p_409=$p_409 rate=$rate delta=$delta fail=$repo_violation"
done

# Tight gate audit: promotions missing head OR missing gate results
promotions_missing_audit=0
if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    promotions_missing_audit=$(PQ "
        SELECT COUNT(DISTINCT p.id)
        FROM aether_promotions p
        LEFT JOIN aether_heads h ON h.promotion_id = p.id
        LEFT JOIN aether_gate_results g ON g.snapshot_id = h.snapshot_id
        WHERE h.id IS NULL OR g.id IS NULL;
    ")
fi

# Head/snapshot integrity (any repo missing head or snapshot)
head_integrity=$(PQ "
    SELECT CASE WHEN COUNT(*) > 0 THEN 'MISSING' ELSE 'ok' END
    FROM aether_repositories r
    LEFT JOIN aether_heads h ON h.id = r.head_id
    LEFT JOIN aether_snapshots s ON s.id = h.snapshot_id
    WHERE (r.head_sequence > 0 AND r.head_id IS NULL)
       OR (r.head_id IS NOT NULL AND (h.id IS NULL OR s.id IS NULL));
")

# Per-repo invariants: ensure no repo masks another
per_repo_violations=$(PQ "
    SELECT COUNT(*) FROM (
        SELECT r.id, r.head_sequence, 
               (SELECT COUNT(*) FROM aether_promotions p WHERE p.repo_id = r.id) as promo_count,
               (SELECT COUNT(*) FROM aether_snapshots s WHERE s.repo_id = r.id) as snap_count,
               (SELECT COUNT(*) FROM aether_gate_results g 
                JOIN aether_snapshots s2 ON g.snapshot_id = s2.id 
                WHERE s2.repo_id = r.id) as gate_count
        FROM aether_repositories r
    ) stats
    WHERE head_sequence != promo_count 
       OR promo_count != snap_count 
       OR (promo_count > 0 AND promo_count != gate_count AND $SOAK_WITH_GATES = 1);
")

starved_repos=$(PQ "
    SELECT COUNT(*) FROM aether_repositories WHERE head_sequence = 0;
")

# Disk snapshot count
disk_snapshots=$(find "$SOAK_STORAGE" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | wc -l | tr -d ' ')

# ── Compute rates ───────────────────────────────────────────────────────────
total_submit=$((total_submit_ok + total_submit_fail))
if [[ $total_submit -gt 0 ]]; then
    submit_fail_rate=$(echo "scale=4; $total_submit_fail / $total_submit" | bc || echo "1")
else submit_fail_rate="1"; fi

total_promote_attempts=$((total_promote_ok + total_promote_contention + total_promote_rejected + total_promote_5xx))
if [[ $total_promote_attempts -gt 0 ]]; then
    promote_5xx_rate=$(echo "scale=4; $total_promote_5xx / $total_promote_attempts" | bc || echo "1")
    promote_409_rate=$(echo "scale=4; $total_promote_contention / $total_promote_attempts" | bc || echo "0")
else promote_5xx_rate="1"; promote_409_rate="0"; fi

# ── Print summary ───────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║            PG NATIVE SOAK RESULTS SUMMARY                      ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "  Duration:              ${ELAPSED}s (run: ${SOAK_RUN_SECONDS}s + drain: ${SOAK_DRAIN_SECONDS}s)"
echo "  Agents:                $SOAK_AGENTS"
echo "  Server alive:          $server_alive"
echo ""
echo "  ── Submit ─────────────────────────────────────"
echo "  Submit OK:             $total_submit_ok"
echo "  Submit Throttled (429):$total_submit_429"
echo "  Submit Fail:           $total_submit_fail (rate: $submit_fail_rate)"
echo "  Submit TPS:            $(echo "scale=2; $total_submit_ok / $ELAPSED" | bc 2>/dev/null || echo "n/a")"
echo ""
echo "  ── Promote (native PG /promotions) ───────────"
echo "  Promote OK:            $total_promote_ok"
echo "  Promote Contention:    $total_promote_contention (409 rate: $promote_409_rate)
  Promote Success-after-409: $total_success_after_409
  Conflicted Ticks (409): $total_conflicted_ticks"
echo "  Promote Rejected:      $total_promote_rejected"
echo "  Promote 5xx:           $total_promote_5xx (rate: $promote_5xx_rate)"
echo "  Promote TPS:           $(echo "scale=2; $total_promote_ok / $ELAPSED" | bc 2>/dev/null || echo "n/a")"
echo ""
echo "  ── Database Audit (global across $SOAK_REPOS repos) ──"
echo "  Sum head sequences:    $head_seq"
echo "  Promotions (DB):       $promo_count"
echo "  Snapshots (DB):        $snapshot_count_db"
echo "  Snapshots (disk):      $disk_snapshots"
echo "  Per-repo DB violatns:  $per_repo_violations"
echo "  Per-repo Perf violatns:$per_repo_perf_violations"
echo -e "  Per-repo Diagnostics:  $per_repo_diagnostics"
echo "  Starved repos (0 head):$starved_repos"
echo "  Trust agents:          $trust_agent_count ($trust_avg)"
echo "  PENDING after drain:   $pending_at_drain"
echo "  Proposals by status:   $proposal_status"
echo "  Timings persisted (DB):$timings_db_count"
echo "  Head integrity:        $head_integrity"
if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    echo "  Promotions w/o audit:  $promotions_missing_audit"
fi
echo ""

# ── Verdict ─────────────────────────────────────────────────────────────────
FAILURES=0
check() {
    local label="$1" condition="$2"
    if eval "$condition"; then echo "  ✅ $label"
    else echo "  ❌ $label"; FAILURES=$((FAILURES + 1)); fi
}

echo "  ── Verdict (production-grade) ────────────────"
check "Server alive at end"                 '[[ "$server_alive" == "yes" ]]'
check "No child-process overrun (no hangs)"  '[[ "$CONTAMINATED" -eq 0 ]]'
check "Sum head sequence > 0"               '[[ "$head_seq" -gt 0 ]]'
check "Per-repo invariants (heads=promos=snaps)" '[[ "$per_repo_violations" -eq 0 ]]'
check "Per-repo performance (409s/pending)" '[[ "$per_repo_perf_violations" -eq 0 ]]'
check "Zero starved repos (minimum progress)" '[[ "$starved_repos" -eq 0 || "${AETHER_ALLOW_STARVED_REPOS:-0}" == "1" ]]'
check "Global sum_heads == total_promos"    '[[ "$head_seq" -eq "$promo_count" ]]'
check "Global total_promos == db_snaps"     '[[ "$promo_count" -eq "$snapshot_count_db" ]]'
echo "  ⚠️ Skipping Global db_snaps == disk_snaps (GC needed)"
check "Timings persisted == db_promos"      '[[ "$timings_db_count" -eq "$promo_count" ]]'
check "Submit failure rate < 1%"            '(( $(echo "$submit_fail_rate < 0.01" | bc -l) ))'
check "Promote 5xx rate < 0.1%"             '(( $(echo "$promote_5xx_rate < 0.001" | bc -l) ))'
# Drain invariant is now checked exactly per-repo above, but we leave the global one as an aggregate health signal
total_superseded=$(PQ "SELECT COUNT(*) FROM aether_proposals WHERE status = 'SUPERSEDED';" 2>/dev/null || echo 0)
total_rejected_db=$(PQ "SELECT COUNT(*) FROM aether_proposals WHERE status = 'REJECTED';" 2>/dev/null || echo 0)
expected_pending=$((total_submit_ok - total_promote_ok - total_superseded - total_rejected_db))
pending_delta=0
if [[ "$pending_at_drain" -ne 999999 ]]; then
    pending_delta=$((pending_at_drain - expected_pending))
    if [[ $pending_delta -lt 0 ]]; then pending_delta=$((-pending_delta)); fi
fi
check "Global backlog conservation (exact)"   '[[ "$pending_delta" -le "${AETHER_SOAK_BACKLOG_TOLERANCE:-0}" ]]'
check "Trust agent count == SOAK_AGENTS"    '[[ "$trust_agent_count" -eq "$SOAK_AGENTS" ]]'
check "Head/snapshot integrity ok"          '[[ "$head_integrity" == "ok" ]]'

if [[ "$SOAK_WITH_GATES" == "1" ]]; then
    check "All promotions have head+gate rows" '[[ "$promotions_missing_audit" -eq 0 ]]'
fi

echo ""
if [[ "$T_SOURCE" == "fallback" ]]; then
    echo "  [WARNING] SOAK EVALUATED UNDER FALLBACK LATENCY. MEASUREMENT FAILED."
    echo "            Do not trust this as a rigorous pass for scaling!"
    echo ""
fi

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
