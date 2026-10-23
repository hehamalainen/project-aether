#!/bin/bash


echo "Setting up Postgres Python Validation..."

export AETHER_MAX_PATH_ACTIVE_PROPOSALS=5
export DATABASE_MAX_CONNECTIONS=20
export DATABASE_URL="postgresql://$(whoami)@localhost/postgres"
export AETHER_SERVER_PROFILE=mx_proof

# Kill any existing server
pkill -f "apps.aether_server.main" || true
sleep 1

# Reset database
echo "Resetting database..."
psql -h localhost -U $(whoami) postgres -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

echo "Starting server..."
.venv/bin/python -m apps.aether_server.main > server_postgres_val.log 2>&1 &
SERVER_PID=$!
sleep 5

echo "Running Baseline (uniform/full_file)..."
.venv/bin/python -m apps.aether_cli.main swarm-harness \
  --target-mode external \
  --external-api-url http://localhost:8100 \
  --traffic-mix full_file \
  --submit-worker-threads 20 \
  --agents 1000 \
  --run-seconds 60 \
  --output-json baseline_pg.json

echo "Resetting database..."
pkill -P $SERVER_PID || true
kill $SERVER_PID || true
sleep 3
psql -h localhost -U $(whoami) postgres -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

echo "Starting server..."
.venv/bin/python -m apps.aether_server.main >> server_postgres_val.log 2>&1 &
SERVER_PID=$!
sleep 5

echo "Running Realistic Skew (batched/mixed)..."
.venv/bin/python -m apps.aether_cli.main swarm-harness \
  --target-mode external \
  --external-api-url http://localhost:8100 \
  --traffic-mix mixed \
  --submit-worker-threads 20 \
  --agents 1000 \
  --run-seconds 60 \
  --output-json skew_pg.json

pkill -P $SERVER_PID || true
kill $SERVER_PID || true
echo "Done."
