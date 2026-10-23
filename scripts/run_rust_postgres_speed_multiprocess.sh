#!/bin/bash
set -e

echo "Setting up Postgres Rust Multiprocess Validation..."

export AETHER_MAX_PATH_ACTIVE_PROPOSALS=0
export DATABASE_MAX_CONNECTIONS=200
export TEST_DB="aether_rust_bench_mp"
export DATABASE_URL="postgresql://$(whoami)@localhost/$TEST_DB"
export PORT=8100
export AETHER_MAX_COHERENCE_BACKLOG=5000

echo "Building Rust server in release mode with db-postgres feature..."
cargo build --manifest-path aether-rs/Cargo.toml --release --no-default-features --features db-postgres

echo "Resetting test database $TEST_DB..."
psql -h localhost -U $(whoami) postgres -c "DROP DATABASE IF EXISTS $TEST_DB;"
psql -h localhost -U $(whoami) postgres -c "CREATE DATABASE $TEST_DB;"
psql -h localhost -U $(whoami) postgres -c "ALTER DATABASE $TEST_DB SET synchronous_commit = on;"

echo "Starting Rust PostgreSQL Server..."
RUST_LOG=info ./aether-rs/target/release/server > server_postgres_rust_mp.log 2>&1 &
SERVER_PID=$!

echo -n "Waiting for health check..."
for i in {1..30}; do
  if curl -sf http://localhost:8100/v1/health >/dev/null 2>&1; then
    echo " healthy!"
    break
  fi
  sleep 1
  echo -n "."
done

echo "Running Rust PG Baseline (uniform/full_file) - Multi-Process..."
../.venv/bin/python3 -m apps.aether_cli.main swarm-harness-mp \
  --processes 4 \
  --target-mode external \
  --external-api-url http://localhost:8100 \
  --traffic-mix full_file \
  --submit-worker-threads 150 \
  --inflight 500 \
  --agents 2000 \
  --run-seconds 100 \
  --output-json baseline_pg_rust_mp.json || true

echo "Resetting test database $TEST_DB..."
pkill -P $SERVER_PID || true
kill $SERVER_PID || true
psql -h localhost -U $(whoami) postgres -c "DROP DATABASE IF EXISTS $TEST_DB;"
psql -h localhost -U $(whoami) postgres -c "CREATE DATABASE $TEST_DB;"
psql -h localhost -U $(whoami) postgres -c "ALTER DATABASE $TEST_DB SET synchronous_commit = on;"

echo "Starting Rust PostgreSQL Server..."
RUST_LOG=info ./aether-rs/target/release/server >> server_postgres_rust_mp.log 2>&1 &
SERVER_PID=$!

echo -n "Waiting for health check..."
for i in {1..30}; do
  if curl -sf http://localhost:8100/v1/health >/dev/null 2>&1; then
    echo " healthy!"
    break
  fi
  sleep 1
  echo -n "."
done

echo "Running Rust PG Realistic Skew (batched/mixed) - Multi-Process..."
../.venv/bin/python3 -m apps.aether_cli.main swarm-harness-mp \
  --processes 4 \
  --target-mode external \
  --external-api-url http://localhost:8100 \
  --traffic-mix mixed \
  --submit-worker-threads 150 \
  --inflight 500 \
  --agents 2000 \
  --run-seconds 100 \
  --output-json skew_pg_rust_mp.json || true

pkill -P $SERVER_PID || true
kill $SERVER_PID || true
echo "Done."
