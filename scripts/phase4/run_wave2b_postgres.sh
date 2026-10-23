#!/bin/bash
set -e

# Activate venv if exists
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

# Setup PYTHONPATH
export PYTHONPATH="$(pwd):$PYTHONPATH"

OUT_DIR=".pytest-artifacts/wave2b"
mkdir -p "$OUT_DIR"

# Database drop/create bootstrap helper
run_psql_cmd() {
    local cmd="$1"
    if psql -h localhost -d postgres -c "$cmd" >/dev/null 2>&1; then
        return 0
    elif psql -h localhost -U $(whoami) -d postgres -c "$cmd" >/dev/null 2>&1; then
        return 0
    elif psql -h localhost -U postgres -d postgres -c "$cmd" >/dev/null 2>&1; then
        return 0
    else
        # Run directly so output/errors propagate to terminal if everything failed
        psql -h localhost -d postgres -c "$cmd"
    fi
}

echo "Bootstrapping PostgreSQL test database..."
run_psql_cmd "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'aether_test' AND pid <> pg_backend_pid();" || true
run_psql_cmd "DROP DATABASE IF EXISTS aether_test;"
run_psql_cmd "CREATE DATABASE aether_test;"

export DATABASE_URL="postgresql://localhost/aether_test"
export AETHER_SEMANTIC_MERGE_ENABLED=1
export AETHER_SYNTHESIS_WORKER_ENABLED=1

AGENTS=(3 5 10 20)
SCENARIOS=("mixed" "hotspot" "worker_failure" "manifest_failure")

echo "Starting Wave 2B: PostgreSQL Concurrency Confirmations..."

for scenario in "${SCENARIOS[@]}"; do
    for n in "${AGENTS[@]}"; do
        echo "--------------------------------------------------------"
        echo "Running scenario: $scenario with $n agents (PostgreSQL)"
        run_dir="$OUT_DIR/${scenario}_${n}"
        
        # Re-bootstrap db for each run to keep it clean
        run_psql_cmd "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'aether_test' AND pid <> pg_backend_pid();" || true
        run_psql_cmd "DROP DATABASE IF EXISTS aether_test;"
        run_psql_cmd "CREATE DATABASE aether_test;"
        
        python -m scripts.phase4.run_semantic_cascade_load \
            --agents "$n" \
            --scenario "$scenario" \
            --worker-mode fake \
            --db-profile postgres \
            --max-local-workers 4 \
            --duration-seconds 30 \
            --repo-fixture "$scenario" \
            --output-dir "$run_dir" || {
                echo "❌ Failed: $scenario with $n agents"
                exit 1
            }
            
        # check semantic oracle result
        RESULT=$(python -c "import json; print(json.load(open('$run_dir/semantic_oracle_result.json')).get('classification'))")
        if [ "$RESULT" = "PASS" ] || [ "$RESULT" = "PASS_WITH_QUEUEING" ] || [ "$RESULT" = "PASS_WITH_RESOURCE_WARNING" ]; then
            echo "✅ Passed: $scenario ($n agents) -> $RESULT"
        else
            echo "❌ Failed: $scenario ($n agents) -> $RESULT"
            exit 1
        fi
    done
done

echo "Wave 2B Completed Successfully!"
