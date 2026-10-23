#!/usr/bin/env python3
"""
Aether Ticket 1 Design Spike — Causal Frontiers Access-Pattern Simulation.
This script acts strictly as a physical microbenchmark to compare physical storage size,
index footprint, dead-tuple TOAST accumulation, and write transaction latencies between:
  1. UPDATE-in-place (simulating current head frontier updates)
  2. INSERT-append (simulating the proposed revision generation log)

WARNING: This is a microbenchmark only. It does not validate overall correctness,
arbitration logic, CRDT convergence, or full Aether server runtime behavior.
"""

import os
import sys
import json
import time
import argparse
import subprocess
import threading
from queue import Queue

PG_HOST = os.environ.get("PGHOST", "localhost")
PG_PORT = os.environ.get("PGPORT", "5432")
PG_USER = os.environ.get("PGUSER") or os.environ.get("USER") or "postgres"
PG_SUPERDB = os.environ.get("PGDATABASE", "postgres")
SIM_DB = "aether_sim_access_pattern"

# Construct a representative version vector JSON (~5 KB) to trigger TOAST relation storage
def generate_representative_vv(size_nodes=80):
    vv = {}
    for i in range(size_nodes):
        vv[f"agent_{i:03d}_uuid_node_key_prefix_long_str"] = 1000 + i
    return json.dumps(vv)

def run_psql(db_name, query):
    cmd = ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", db_name, "-t", "-A", "-c", query]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error running psql query: {query}\nStderr: {e.stderr}", file=sys.stderr)
        return None

def setup_database():
    print(f"🧹 Recreating simulation database: {SIM_DB}...")
    run_psql(PG_SUPERDB, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{SIM_DB}' AND pid <> pg_backend_pid();")
    run_psql(PG_SUPERDB, f"DROP DATABASE IF EXISTS {SIM_DB};")
    run_psql(PG_SUPERDB, f"CREATE DATABASE {SIM_DB};")
    
    print("🏗️ Creating simulation tables...")
    
    # 1. In-Place table
    inplace_table_sql = """
    CREATE TABLE sim_inplace_frontiers (
        repo_id VARCHAR(255) PRIMARY KEY,
        frontier_json TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    INSERT INTO sim_inplace_frontiers (repo_id, frontier_json) VALUES ('repo_1', '{"init": 0}');
    """
    run_psql(SIM_DB, inplace_table_sql)
    
    # 2. Append-Only table
    append_table_sql = """
    CREATE TABLE sim_append_frontiers (
        id BIGSERIAL PRIMARY KEY,
        repo_id VARCHAR(255) NOT NULL,
        repo_revision_id BIGINT NOT NULL,
        frontier_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX idx_sim_append_repo_rev ON sim_append_frontiers(repo_id, repo_revision_id DESC);
    INSERT INTO sim_append_frontiers (repo_id, repo_revision_id, frontier_json) VALUES ('repo_1', 1, '{"init": 0}');
    """
    run_psql(SIM_DB, append_table_sql)
    
    # Force initial analyze to prime catalog
    run_psql(SIM_DB, "ANALYZE sim_inplace_frontiers; ANALYZE sim_append_frontiers;")
    print("✅ Setup complete.")

def worker_inplace(iterations, vv_data, latencies_list):
    for i in range(iterations):
        start = time.perf_counter()
        q = f"UPDATE sim_inplace_frontiers SET frontier_json = '{vv_data}', updated_at = NOW() WHERE repo_id = 'repo_1';"
        run_psql(SIM_DB, q)
        elapsed = (time.perf_counter() - start) * 1000.0 # ms
        latencies_list.append(elapsed)

def worker_append(iterations, vv_data, latencies_list, thread_id):
    for i in range(iterations):
        start = time.perf_counter()
        # Ensure monotonic sequence increment per thread operation
        rev_id = (thread_id * 1000) + i + 2
        q = f"INSERT INTO sim_append_frontiers (repo_id, repo_revision_id, frontier_json) VALUES ('repo_1', {rev_id}, '{vv_data}');"
        run_psql(SIM_DB, q)
        elapsed = (time.perf_counter() - start) * 1000.0 # ms
        latencies_list.append(elapsed)

def percentile(data, percent):
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * percent
    f = int(k)
    c = f + 1
    if c < len(s):
        return s[f] + (s[c] - s[f]) * (k - f)
    else:
        return s[f]

def collect_size_metrics(table_name):
    # Retrieve base table OID and TOAST OID
    base_oid = run_psql(SIM_DB, f"SELECT oid FROM pg_class WHERE relname='{table_name}';")
    toast_oid = run_psql(SIM_DB, f"SELECT reltoastrelid FROM pg_class WHERE relname='{table_name}';")
    
    # Size in MB
    base_bytes = run_psql(SIM_DB, f"SELECT pg_relation_size({base_oid});")
    index_bytes = run_psql(SIM_DB, f"SELECT pg_indexes_size({base_oid});")
    total_bytes = run_psql(SIM_DB, f"SELECT pg_total_relation_size({base_oid});")
    
    base_mb = float(base_bytes or 0) / (1024.0 * 1024.0)
    index_mb = float(index_bytes or 0) / (1024.0 * 1024.0)
    total_mb = float(total_bytes or 0) / (1024.0 * 1024.0)
    toast_mb = max(0.0, total_mb - base_mb - index_mb)
    
    # Dead/Live tuples
    live_tup = run_psql(SIM_DB, f"SELECT n_live_tup FROM pg_stat_all_tables WHERE relname='{table_name}';")
    dead_tup = run_psql(SIM_DB, f"SELECT n_dead_tup FROM pg_stat_all_tables WHERE relname='{table_name}';")
    
    live_tup_count = int(live_tup or 0)
    dead_tup_count = int(dead_tup or 0)
    
    # TOAST tuples if applicable
    toast_live_count = 0
    toast_dead_count = 0
    if toast_oid and toast_oid != '0':
        toast_live = run_psql(SIM_DB, f"SELECT n_live_tup FROM pg_stat_all_tables WHERE relid='{toast_oid}';")
        toast_dead = run_psql(SIM_DB, f"SELECT n_dead_tup FROM pg_stat_all_tables WHERE relid='{toast_oid}';")
        toast_live_count = int(toast_live or 0)
        toast_dead_count = int(toast_dead or 0)
        
    return {
        "base_mb": base_mb,
        "toast_mb": toast_mb,
        "index_mb": index_mb,
        "total_mb": total_mb,
        "live_tuples": live_tup_count,
        "dead_tuples": dead_tup_count,
        "toast_live_tuples": toast_live_count,
        "toast_dead_tuples": toast_dead_count
    }

def main():
    parser = argparse.ArgumentParser(description="Aether Causal Frontiers Access-Pattern Simulation")
    parser.path_desc = "scratch/simulate_append_only.py"
    parser.add_argument("--concurrency", type=int, default=20, help="Number of concurrent worker threads")
    parser.add_argument("--iterations", type=int, default=10, help="Write transactions per thread")
    args = parser.parse_args()
    
    concurrency = args.concurrency
    iterations = args.iterations
    
    print("=" * 90)
    print("                    AETHER TICKET 1 ACCESS-PATTERN MICROBENCHMARK")
    print("=" * 90)
    print(f"Parameters: Concurrency = {concurrency} threads, Iterations = {iterations} writes/thread")
    print("-" * 90)
    
    setup_database()
    vv_data = generate_representative_vv()
    print(f"Generated version vector payload: ~{len(vv_data) / 1024.0:.3f} KB")
    
    # --------------------------------------------------------------------------
    # 1. RUN IN-PLACE UPDATE SWEEP
    # --------------------------------------------------------------------------
    print("\n🏃 Sweep 1/2: Simulating In-Place UPDATEs...")
    inplace_latencies = []
    threads = []
    start_time = time.perf_counter()
    
    for t_id in range(concurrency):
        t = threading.Thread(target=worker_inplace, args=(iterations, vv_data, inplace_latencies))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    inplace_duration = time.perf_counter() - start_time
    print(f"   Finished in {inplace_duration:.2f} seconds.")
    
    # --------------------------------------------------------------------------
    # 2. RUN APPEND-ONLY INSERT SWEEP
    # --------------------------------------------------------------------------
    print("\n🏃 Sweep 2/2: Simulating Append-Only INSERTs...")
    append_latencies = []
    threads = []
    start_time = time.perf_counter()
    
    for t_id in range(concurrency):
        t = threading.Thread(target=worker_append, args=(iterations, vv_data, append_latencies, t_id))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    append_duration = time.perf_counter() - start_time
    print(f"   Finished in {append_duration:.2f} seconds.")
    
    # Force vacuum analyze to update pg_stat tables for correct sizing metrics
    print("\n🧹 Analyzing tables to flush database catalog stats...")
    run_psql(SIM_DB, "VACUUM ANALYZE sim_inplace_frontiers;")
    run_psql(SIM_DB, "VACUUM ANALYZE sim_append_frontiers;")
    
    # --------------------------------------------------------------------------
    # 3. COLLECT AND PRINT RESULTS
    # --------------------------------------------------------------------------
    inplace_metrics = collect_size_metrics("sim_inplace_frontiers")
    append_metrics = collect_size_metrics("sim_append_frontiers")
    
    print("\n" + "=" * 90)
    print("                                SIMULATION METRICS MATRIX")
    print("=" * 90)
    print(f"{'Metric':<30} | {'Sweep 1: UPDATE-in-place':<26} | {'Sweep 2: INSERT-append':<26}")
    print("-" * 90)
    
    print(f"{'Committed Transactions':<30} | {len(inplace_latencies):<26} | {len(append_latencies):<26}")
    print(f"{'Total Duration (s)':<30} | {inplace_duration:<26.2f} | {append_duration:<26.2f}")
    print("-" * 90)
    print(f"{'Base Table Size (MB)':<30} | {inplace_metrics['base_mb']:<26.4f} | {append_metrics['base_mb']:<26.4f}")
    print(f"{'TOAST Relation Size (MB)':<30} | {inplace_metrics['toast_mb']:<26.4f} | {append_metrics['toast_mb']:<26.4f}")
    print(f"{'Total Relation Size (MB)':<30} | {inplace_metrics['total_mb']:<26.4f} | {append_metrics['total_mb']:<26.4f}")
    print(f"{'Total Index Size (MB)':<30} | {inplace_metrics['index_mb']:<26.4f} | {append_metrics['index_mb']:<26.4f}")
    print("-" * 90)
    print(f"{'Base Live / Dead Tuples':<30} | {inplace_metrics['live_tuples']:d} / {inplace_metrics['dead_tuples']:d} | {append_metrics['live_tuples']:d} / {append_metrics['dead_tuples']:d}")
    print(f"{'TOAST Live / Dead Tuples':<30} | {inplace_metrics['toast_live_tuples']:d} / {inplace_metrics['toast_dead_tuples']:d} | {append_metrics['toast_live_tuples']:d} / {append_metrics['toast_dead_tuples']:d}")
    print("-" * 90)
    
    p50_in, p95_in, p99_in = percentile(inplace_latencies, 0.50), percentile(inplace_latencies, 0.95), percentile(inplace_latencies, 0.99)
    p50_ap, p95_ap, p99_ap = percentile(append_latencies, 0.50), percentile(append_latencies, 0.95), percentile(append_latencies, 0.99)
    
    print(f"{'Write Latency P50 (ms)':<30} | {p50_in:<26.2f} | {p50_ap:<26.2f}")
    print(f"{'Write Latency P95 (ms)':<30} | {p95_in:<26.2f} | {p95_ap:<26.2f}")
    print(f"{'Write Latency P99 (ms)':<30} | {p99_in:<26.2f} | {p99_ap:<26.2f}")
    print("=" * 90)
    
    print("\n🧹 Dropping post-mortem database...")
    run_psql(PG_SUPERDB, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{SIM_DB}' AND pid <> pg_backend_pid();")
    run_psql(PG_SUPERDB, f"DROP DATABASE IF EXISTS {SIM_DB};")
    print("✅ Completed.")

if __name__ == "__main__":
    main()
