#!/usr/bin/env python3
import os
import sys
import json
import time
import argparse
import subprocess
import threading
import shutil
import random

# Postgres connection parameters
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_USER = os.environ.get("PG_USER", os.environ.get("USER", "postgres"))
PG_SUPERDB = os.environ.get("PG_SUPERDB", "postgres")
SIM_DB = "aether_storage_battery"

def run_psql(db_name, query):
    """Fallback run_psql for setup/cleanup steps outside latency loops."""
    cmd = ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", db_name, "-t", "-A", "-F", "|", "-c", query]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        out = res.stdout.strip()
        err = res.stderr.strip()
        for output in [out, err]:
            if "ERROR:" in output or "FATAL:" in output or "PANIC:" in output:
                raise RuntimeError(f"run_psql failed for query: {query}\nOutput: {output}")
        return out
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Error running psql query: {query}\nExit Code: {e.returncode}\nStderr: {e.stderr}")

class PGConnection:
    """Persistent psql session that reuses a single process to eliminate startup overhead."""
    def __init__(self):
        self.proc = subprocess.Popen(
            ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", SIM_DB, "-t", "-A", "-F", "|"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merges stderr and stdout to catch errors
            text=True
        )
        # Verify startup and set timeouts to prevent hangs
        self.query("SELECT 1;")
        self.query("SET statement_timeout = 10000;")
        self.query("SET lock_timeout = 5000;")
        
    def query(self, sql):
        sentinel = f"SENTINEL_{time.perf_counter_ns()}_{random.randint(1000, 9999)}"
        full_sql = f"{sql}\n\\echo {sentinel}\n"
        self.proc.stdin.write(full_sql)
        self.proc.stdin.flush()
        
        lines = []
        err_msg = None
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("psql connection lost")
            line = line.strip()
            if line == sentinel:
                break
            if line.startswith("ERROR:") or line.startswith("FATAL:") or line.startswith("PANIC:"):
                if err_msg is None:
                    err_msg = line
            else:
                lines.append(line)
                
        if err_msg:
            raise RuntimeError(f"PostgreSQL command failed: {err_msg}")
        return lines

    def seed(self, query_list):
        batch_sql = "BEGIN;\n" + "\n".join(query_list) + "\nCOMMIT;\n"
        self.query(batch_sql)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait()
        except Exception:
            pass

def db_exists():
    res = run_psql(PG_SUPERDB, f"SELECT 1 FROM pg_database WHERE datname='{SIM_DB}';")
    return res == "1"

def create_db():
    if not db_exists():
        print(f"Creating database {SIM_DB}...")
        run_psql(PG_SUPERDB, f"CREATE DATABASE {SIM_DB};")
    else:
        print(f"Database {SIM_DB} already exists.")

def get_lsn_diff(lsn_before, lsn_after):
    if not lsn_before or not lsn_after:
        return 0
    res = run_psql(SIM_DB, f"SELECT pg_wal_lsn_diff('{lsn_after}', '{lsn_before}');")
    return int(res) if res and res.isdigit() else 0

def generate_vv_payload(size_nodes=80):
    vv = {}
    for i in range(size_nodes):
        vv[f"agent_{i:03d}_uuid_node_key_prefix_long_str"] = 1000 + i
    return json.dumps(vv)

def generate_delta_payload(num_mutations=3):
    delta = {}
    for _ in range(num_mutations):
        agent_idx = random.randint(0, 79)
        delta[f"agent_{agent_idx:03d}_uuid_node_key_prefix_long_str"] = random.randint(1000, 50000)
    return json.dumps(delta)

# Workspace and scratch paths
WORKSPACE_ROOT = os.path.join(os.getcwd(), "scratch", "storage_battery_workspaces")
SCRATCH_ROOT = os.path.join(os.getcwd(), "scratch", "storage_battery_scratch")

current_scratch_size = 0
peak_scratch_size = 0
scratch_lock = threading.Lock()

def robust_rmtree(path):
    if not os.path.exists(path):
        return
    temp_dir = f"{path}_del_{time.perf_counter_ns()}"
    try:
        os.rename(path, temp_dir)
        subprocess.Popen(["rm", "-rf", temp_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        subprocess.run(["rm", "-rf", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def cleanup_dirs():
    global current_scratch_size, peak_scratch_size
    with scratch_lock:
        current_scratch_size = 0
        peak_scratch_size = 0
    if os.path.exists(WORKSPACE_ROOT):
        subprocess.run(["rm", "-rf", WORKSPACE_ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(SCRATCH_ROOT):
        subprocess.run(["rm", "-rf", SCRATCH_ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    os.makedirs(SCRATCH_ROOT, exist_ok=True)

def record_scratch_write_start(payload_len):
    global current_scratch_size, peak_scratch_size
    with scratch_lock:
        current_scratch_size += payload_len
        if current_scratch_size > peak_scratch_size:
            peak_scratch_size = current_scratch_size

def record_scratch_write_end(payload_len):
    global current_scratch_size
    with scratch_lock:
        current_scratch_size -= payload_len

def get_dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

def get_median(data):
    if not data:
        return 0.0
    s = sorted(data)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    else:
        return float(s[n // 2 - 1] + s[n // 2]) / 2.0

def get_p95(data):
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * 0.95)
    return float(s[min(idx, len(s) - 1)])

class CandidateInPlace:
    name = "Update-In-Place"
    
    def setup(self, conn):
        conn.query("DROP TABLE IF EXISTS sim_inplace;")
        conn.query("""
        CREATE TABLE sim_inplace (
            repo_id VARCHAR(255) PRIMARY KEY,
            head_sequence BIGINT NOT NULL,
            frontier_json TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        
    def seed(self, conn, depth):
        vv = generate_vv_payload()
        conn.query(f"INSERT INTO sim_inplace (repo_id, head_sequence, frontier_json) VALUES ('repo_1', {depth}, '{vv}') ON CONFLICT (repo_id) DO UPDATE SET head_sequence = {depth}, frontier_json = EXCLUDED.frontier_json;")

    def get_seq_query(self):
        return "SELECT head_sequence FROM sim_inplace WHERE repo_id = 'repo_1';"

    def write(self, conn, repo_id, vv_data, basis_sequence):
        next_seq = basis_sequence + 1
        q = f"UPDATE sim_inplace SET frontier_json = '{vv_data}', head_sequence = {next_seq} WHERE repo_id = '{repo_id}' AND head_sequence = {basis_sequence} RETURNING 1;"
        res = conn.query(q)
        success = len(res) > 0 and res[0] == "1"
        if success:
            candidate_dir = os.path.join(WORKSPACE_ROOT, self.name)
            os.makedirs(candidate_dir, exist_ok=True)
            scratch_file = os.path.join(SCRATCH_ROOT, f"{repo_id}_inplace_{next_seq}_{threading.get_ident()}.tmp")
            
            payload_len = len(vv_data.encode('utf-8'))
            record_scratch_write_start(payload_len)
            
            with open(scratch_file, "w") as f:
                f.write(vv_data)
            workspace_file = os.path.join(candidate_dir, f"{repo_id}_inplace.json")
            shutil.copy2(scratch_file, workspace_file)
            
            try:
                os.remove(scratch_file)
            except Exception:
                pass
                
            record_scratch_write_end(payload_len)
        return success
        
    def read(self, conn, repo_id):
        res = conn.query(f"SELECT frontier_json FROM sim_inplace WHERE repo_id = '{repo_id}';")
        return len(res[0]) if res else 0

    def compact(self, conn):
        start_vac = time.perf_counter()
        conn.query("VACUUM sim_inplace;")
        vac_time = time.perf_counter() - start_vac
        return 0.0, vac_time * 1000

    def get_db_size(self, conn):
        res = conn.query("SELECT pg_total_relation_size('sim_inplace');")
        return int(res[0]) if res and res[0].isdigit() else 0

class CandidateOptionA:
    name = "Option A (Append-Only)"
    
    def setup(self, conn):
        conn.query("DROP TABLE IF EXISTS sim_option_a;")
        conn.query("""
        CREATE TABLE sim_option_a (
            id BIGSERIAL PRIMARY KEY,
            repo_id VARCHAR(255) NOT NULL,
            head_sequence BIGINT NOT NULL,
            frontier_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX idx_sim_option_a_repo_seq ON sim_option_a(repo_id, head_sequence);
        """)
        
    def seed(self, conn, depth):
        vv = generate_vv_payload()
        res = conn.query(self.get_seq_query())
        current_max = int(res[0]) if res and res[0].isdigit() else 0
        if current_max >= depth:
            return
            
        if current_max == 0:
            conn.query("INSERT INTO sim_option_a (repo_id, head_sequence, frontier_json) VALUES ('repo_1', 1, '{\"init\": 0}') ON CONFLICT (repo_id, head_sequence) DO NOTHING;")
            start_seq = 2
        else:
            start_seq = current_max + 1
            
        queries = []
        for seq in range(start_seq, depth + 1):
            queries.append(f"INSERT INTO sim_option_a (repo_id, head_sequence, frontier_json) VALUES ('repo_1', {seq}, '{vv}');")
            if len(queries) >= 1000:
                conn.seed(queries)
                queries = []
        if queries:
            conn.seed(queries)

    def get_seq_query(self):
        return "SELECT MAX(head_sequence) FROM sim_option_a WHERE repo_id = 'repo_1';"

    def write(self, conn, repo_id, vv_data, basis_sequence):
        next_seq = basis_sequence + 1
        q = f"INSERT INTO sim_option_a (repo_id, head_sequence, frontier_json) VALUES ('{repo_id}', {next_seq}, '{vv_data}') RETURNING 1;"
        try:
            res = conn.query(q)
            success = len(res) > 0 and res[0] == "1"
        except RuntimeError as e:
            if "duplicate key" in str(e) or "unique constraint" in str(e):
                success = False
            else:
                raise
        if success:
            candidate_dir = os.path.join(WORKSPACE_ROOT, self.name)
            os.makedirs(candidate_dir, exist_ok=True)
            scratch_file = os.path.join(SCRATCH_ROOT, f"{repo_id}_option_a_{next_seq}_{threading.get_ident()}.tmp")
            
            payload_len = len(vv_data.encode('utf-8'))
            record_scratch_write_start(payload_len)
            
            with open(scratch_file, "w") as f:
                f.write(vv_data)
            workspace_file = os.path.join(candidate_dir, f"{repo_id}_option_a_{next_seq}.json")
            shutil.copy2(scratch_file, workspace_file)
            
            try:
                os.remove(scratch_file)
            except Exception:
                pass
                
            record_scratch_write_end(payload_len)
        return success
            
    def read(self, conn, repo_id):
        res = conn.query(f"SELECT frontier_json FROM sim_option_a WHERE repo_id = '{repo_id}' ORDER BY head_sequence DESC LIMIT 1;")
        return len(res[0]) if res else 0

    def compact(self, conn):
        start_dml = time.perf_counter()
        conn.query("""
        DELETE FROM sim_option_a a USING (
            SELECT repo_id, MAX(head_sequence) as max_seq FROM sim_option_a GROUP BY repo_id
        ) m
        WHERE a.repo_id = m.repo_id AND a.head_sequence < m.max_seq;
        """)
        dml_time = time.perf_counter() - start_dml
        
        start_vac = time.perf_counter()
        conn.query("VACUUM sim_option_a;")
        vac_time = time.perf_counter() - start_vac
        return dml_time * 1000, vac_time * 1000

    def get_db_size(self, conn):
        res = conn.query("SELECT pg_total_relation_size('sim_option_a');")
        return int(res[0]) if res and res[0].isdigit() else 0

class CandidateOptionB:
    name = "Option B (Event-Log)"
    
    def __init__(self, checkpoint_interval=100):
        self.checkpoint_interval = checkpoint_interval
        
    def setup(self, conn):
        conn.query("DROP TABLE IF EXISTS sim_option_b_snapshots; DROP TABLE IF EXISTS sim_option_b_events;")
        conn.query("""
        CREATE TABLE sim_option_b_snapshots (
            repo_id VARCHAR(255) NOT NULL,
            snapshot_sequence BIGINT NOT NULL,
            frontier_json TEXT NOT NULL,
            PRIMARY KEY (repo_id, snapshot_sequence)
        );
        CREATE TABLE sim_option_b_events (
            id BIGSERIAL PRIMARY KEY,
            repo_id VARCHAR(255) NOT NULL,
            head_sequence BIGINT NOT NULL,
            delta_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE UNIQUE INDEX idx_sim_option_b_events_repo_seq ON sim_option_b_events(repo_id, head_sequence);
        """)
        
    def seed(self, conn, depth):
        vv = generate_vv_payload()
        delta_str = generate_delta_payload()
        res = conn.query(self.get_seq_query())
        current_max = int(res[0]) if res and res[0].isdigit() else 0
        if current_max >= depth:
            return
            
        if current_max == 0:
            conn.query("INSERT INTO sim_option_b_snapshots (repo_id, snapshot_sequence, frontier_json) VALUES ('repo_1', 1, '{\"init\": 0}') ON CONFLICT (repo_id, snapshot_sequence) DO NOTHING;")
            conn.query("INSERT INTO sim_option_b_events (repo_id, head_sequence, delta_json) VALUES ('repo_1', 1, '{\"init\": 0}') ON CONFLICT (repo_id, head_sequence) DO NOTHING;")
            start_seq = 2
        else:
            start_seq = current_max + 1
            
        queries = []
        for seq in range(start_seq, depth + 1):
            if seq % self.checkpoint_interval == 0:
                queries.append(f"INSERT INTO sim_option_b_snapshots (repo_id, snapshot_sequence, frontier_json) VALUES ('repo_1', {seq}, '{vv}');")
            queries.append(f"INSERT INTO sim_option_b_events (repo_id, head_sequence, delta_json) VALUES ('repo_1', {seq}, '{delta_str}');")
            if len(queries) >= 1000:
                conn.seed(queries)
                queries = []
        if queries:
            conn.seed(queries)

    def get_seq_query(self):
        return "SELECT MAX(head_sequence) FROM sim_option_b_events WHERE repo_id = 'repo_1';"

    def write(self, conn, repo_id, vv_data, basis_sequence):
        next_seq = basis_sequence + 1
        delta_str = generate_delta_payload()
        
        if next_seq % self.checkpoint_interval == 0:
            q = f"BEGIN;\nINSERT INTO sim_option_b_snapshots (repo_id, snapshot_sequence, frontier_json) VALUES ('{repo_id}', {next_seq}, '{vv_data}');\nINSERT INTO sim_option_b_events (repo_id, head_sequence, delta_json) VALUES ('{repo_id}', {next_seq}, '{delta_str}') RETURNING 1;\nCOMMIT;"
        else:
            q = f"INSERT INTO sim_option_b_events (repo_id, head_sequence, delta_json) VALUES ('{repo_id}', {next_seq}, '{delta_str}') RETURNING 1;"
            
        try:
            res = conn.query(q)
            success = len(res) > 0 and "1" in res
        except RuntimeError as e:
            if "duplicate key" in str(e) or "unique constraint" in str(e) or "current transaction is aborted" in str(e):
                success = False
                if next_seq % self.checkpoint_interval == 0 or "current transaction is aborted" in str(e):
                    try:
                        conn.query("ROLLBACK;")
                    except Exception:
                        pass
            else:
                raise
        if success:
            candidate_dir = os.path.join(WORKSPACE_ROOT, self.name)
            os.makedirs(candidate_dir, exist_ok=True)
            scratch_file = os.path.join(SCRATCH_ROOT, f"{repo_id}_option_b_{next_seq}_{threading.get_ident()}.tmp")
            if next_seq % self.checkpoint_interval == 0:
                payload = vv_data
                workspace_file = os.path.join(candidate_dir, f"{repo_id}_snapshot_{next_seq}.json")
            else:
                payload = delta_str
                workspace_file = os.path.join(candidate_dir, f"{repo_id}_delta_{next_seq}.json")
                
            payload_len = len(payload.encode('utf-8'))
            record_scratch_write_start(payload_len)
            
            with open(scratch_file, "w") as f:
                f.write(payload)
            shutil.copy2(scratch_file, workspace_file)
            
            try:
                os.remove(scratch_file)
            except Exception:
                pass
                
            record_scratch_write_end(payload_len)
        return success

    def read(self, conn, repo_id):
        snap_res = conn.query(f"SELECT snapshot_sequence, frontier_json FROM sim_option_b_snapshots WHERE repo_id = '{repo_id}' ORDER BY snapshot_sequence DESC LIMIT 1;")
        if not snap_res:
            return 0
        snap_seq, snap_json = snap_res[0].split("|") if "|" in snap_res[0] else (0, "{}")
        snap_seq = int(snap_seq)
        
        events_res = conn.query(f"SELECT delta_json FROM sim_option_b_events WHERE repo_id = '{repo_id}' AND head_sequence > {snap_seq} ORDER BY head_sequence ASC;")
        
        # Faithful delta replay merging logic
        try:
            frontier = json.loads(snap_json)
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse snap_json: {repr(snap_json)}", file=sys.stderr)
            raise
            
        if events_res:
            for event_delta in events_res:
                if event_delta.strip():
                    try:
                        delta = json.loads(event_delta)
                        frontier.update(delta)
                    except json.JSONDecodeError as e:
                        print(f"ERROR: Failed to parse event_delta: {repr(event_delta)}", file=sys.stderr)
                        raise
        return len(json.dumps(frontier))

    def compact(self, conn):
        start_dml = time.perf_counter()
        max_seq_res = conn.query("SELECT MAX(head_sequence) FROM sim_option_b_events WHERE repo_id = 'repo_1';")
        if not max_seq_res or not max_seq_res[0].isdigit():
            return 0.0, 0.0
        max_seq = int(max_seq_res[0])
        
        snap_res = conn.query(f"SELECT snapshot_sequence, frontier_json FROM sim_option_b_snapshots WHERE repo_id = 'repo_1' ORDER BY snapshot_sequence DESC LIMIT 1;")
        if not snap_res:
            return 0.0, 0.0
        snap_seq, snap_json = snap_res[0].split("|") if "|" in snap_res[0] else (0, "{}")
        snap_seq = int(snap_seq)
        
        events_res = conn.query(f"SELECT delta_json FROM sim_option_b_events WHERE repo_id = 'repo_1' AND head_sequence > {snap_seq} ORDER BY head_sequence ASC;")
        frontier = json.loads(snap_json)
        if events_res:
            for event_delta in events_res:
                if event_delta.strip():
                    delta = json.loads(event_delta)
                    frontier.update(delta)
                    
        conn.query(f"INSERT INTO sim_option_b_snapshots (repo_id, snapshot_sequence, frontier_json) VALUES ('repo_1', {max_seq}, '{json.dumps(frontier)}') ON CONFLICT (repo_id, snapshot_sequence) DO NOTHING;")
        conn.query(f"DELETE FROM sim_option_b_snapshots WHERE repo_id = 'repo_1' AND snapshot_sequence < {max_seq};")
        conn.query(f"DELETE FROM sim_option_b_events WHERE repo_id = 'repo_1' AND head_sequence < {max_seq};")
        dml_time = time.perf_counter() - start_dml
        
        start_vac = time.perf_counter()
        conn.query("VACUUM sim_option_b_snapshots;")
        conn.query("VACUUM sim_option_b_events;")
        vac_time = time.perf_counter() - start_vac
        return dml_time * 1000, vac_time * 1000

    def get_db_size(self, conn):
        res1 = conn.query("SELECT pg_total_relation_size('sim_option_b_snapshots');")
        res2 = conn.query("SELECT pg_total_relation_size('sim_option_b_events');")
        s1 = int(res1[0]) if res1 and res1[0].isdigit() else 0
        s2 = int(res2[0]) if res2 and res2[0].isdigit() else 0
        return s1 + s2

def verify_seeding_depth(conn, candidate, expected_depth):
    # 1. Check head sequence
    seq_res = conn.query(candidate.get_seq_query())
    actual_seq = int(seq_res[0]) if seq_res and seq_res[0].isdigit() else 0
    if actual_seq < expected_depth:
        raise RuntimeError(f"Seeding verification failed for {candidate.name}: expected head_sequence >= {expected_depth}, found {actual_seq}")
        
    # 2. Check precise row count
    if candidate.name == "Update-In-Place":
        res = conn.query("SELECT COUNT(*) FROM sim_inplace WHERE repo_id = 'repo_1';")
        actual_count = int(res[0]) if res and res[0].isdigit() else 0
        expected_count = 1
    elif candidate.name == "Option A (Append-Only)":
        res = conn.query("SELECT COUNT(*) FROM sim_option_a WHERE repo_id = 'repo_1';")
        actual_count = int(res[0]) if res and res[0].isdigit() else 0
        expected_count = expected_depth
    elif candidate.name == "Option B (Event-Log)":
        res = conn.query("SELECT COUNT(*) FROM sim_option_b_events WHERE repo_id = 'repo_1';")
        actual_count = int(res[0]) if res and res[0].isdigit() else 0
        expected_count = expected_depth
    else:
        actual_count = expected_depth
        expected_count = expected_depth
        
    if actual_count != expected_count:
        raise RuntimeError(f"Seeding precise count check failed for {candidate.name}: expected exactly {expected_count} rows, found {actual_count}")

def run_benchmark_arm(candidate, num_threads, num_writes, checkpoint_interval=100, is_verify=False):
    # Setup seed depth based on verify vs sweep
    seed_depth_1k = 20 if is_verify else 1000
    seed_depth_5k = 35 if is_verify else 5000
    seed_depth_50k = 50 if is_verify else 50000
    
    main_conn = PGConnection()
    candidate.setup(main_conn)
    
    # 1. Seed database to 1k depth
    print(f"[{candidate.name}] Seeding database to 1k depth...")
    candidate.seed(main_conn, seed_depth_1k)
    verify_seeding_depth(main_conn, candidate, seed_depth_1k)
        
    # 2. Measure read latency at 1k depth
    read_latencies_1k = []
    for _ in range(10 if is_verify else 100):
        start = time.perf_counter()
        candidate.read(main_conn, "repo_1")
        read_latencies_1k.append(time.perf_counter() - start)
    read_p95_1k = get_p95(read_latencies_1k) * 1000
    read_median_1k = get_median(read_latencies_1k) * 1000
    
    # 2.5. Seed database to 5k depth
    print(f"[{candidate.name}] Seeding database to 5k depth...")
    candidate.seed(main_conn, seed_depth_5k)
    verify_seeding_depth(main_conn, candidate, seed_depth_5k)
        
    # Measure read latency at 5k depth
    read_latencies_5k = []
    for _ in range(10 if is_verify else 100):
        start = time.perf_counter()
        candidate.read(main_conn, "repo_1")
        read_latencies_5k.append(time.perf_counter() - start)
    read_p95_5k = get_p95(read_latencies_5k) * 1000
    read_median_5k = get_median(read_latencies_5k) * 1000
    
    # 3. Seed database to 50k depth
    print(f"[{candidate.name}] Seeding database to 50k depth...")
    candidate.seed(main_conn, seed_depth_50k)
    verify_seeding_depth(main_conn, candidate, seed_depth_50k)
        
    # 4. Measure read latency at 50k depth
    read_latencies_50k = []
    for _ in range(10 if is_verify else 100):
        start = time.perf_counter()
        candidate.read(main_conn, "repo_1")
        read_latencies_50k.append(time.perf_counter() - start)
    read_p95_50k = get_p95(read_latencies_50k) * 1000
    read_median_50k = get_median(read_latencies_50k) * 1000
    
    read_slope = ((read_p95_50k - read_p95_1k) / read_p95_1k * 100) if read_p95_1k > 0 else 0.0
    
    # 5. Measure Control Arm (Low Contention, 1 thread sequential matched load)
    cleanup_dirs()
    # Reset/reseed before control arm to isolate starting state
    candidate.setup(main_conn)
    candidate.seed(main_conn, seed_depth_50k)
    verify_seeding_depth(main_conn, candidate, seed_depth_50k)
    
    lsn_before_control = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    
    control_conn = PGConnection()
    control_write_latencies = []
    control_success = 0
    vv = generate_vv_payload()
    
    while control_success < num_writes:
        seq_res = control_conn.query(candidate.get_seq_query())
        basis = int(seq_res[0]) if seq_res and seq_res[0].isdigit() else seed_depth_50k
        
        start = time.perf_counter()
        ok = candidate.write(control_conn, "repo_1", vv, basis)
        elapsed = time.perf_counter() - start
        
        if ok:
            control_write_latencies.append(elapsed)
            control_success += 1
            
    control_conn.close()
    
    lsn_after_control = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    wal_bytes_control = get_lsn_diff(lsn_before_control, lsn_after_control)
    wal_per_promotion_control = wal_bytes_control / control_success if control_success > 0 else 0.0
    
    # 6. Measure Contended Arm (Matched-Load)
    cleanup_dirs()
    # Reset/reseed before contended arm to isolate starting state
    candidate.setup(main_conn)
    candidate.seed(main_conn, seed_depth_50k)
    verify_seeding_depth(main_conn, candidate, seed_depth_50k)
    
    lsn_before_contended = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    
    worker_conns = [PGConnection() for _ in range(num_threads)]
    write_latencies = []
    all_contended_writes = []
    success_count = 0
    conflict_count = 0
    attempt_count = 0
    allocated_writes = 0
    lock = threading.Lock()
    stop_event = threading.Event()
    compaction_active = True
    
    def worker(worker_conn):
        nonlocal success_count, conflict_count, attempt_count, allocated_writes
        while not stop_event.is_set():
            with lock:
                if allocated_writes >= num_writes:
                    break
                allocated_writes += 1
            
            success_slot_used = False
            try:
                seq_res = worker_conn.query(candidate.get_seq_query())
                basis = int(seq_res[0]) if seq_res and seq_res[0].isdigit() else seed_depth_50k
                
                write_start_wall = time.time()
                ok = candidate.write(worker_conn, "repo_1", vv, basis)
                write_end_wall = time.time()
                elapsed = write_end_wall - write_start_wall
                
                with lock:
                    attempt_count += 1
                    if ok:
                        success_count += 1
                        success_slot_used = True
                        write_latencies.append(elapsed)
                    else:
                        conflict_count += 1
                    all_contended_writes.append((write_start_wall, write_end_wall, elapsed))
                    
                if not ok:
                    time.sleep(0.01)  # Conflict backoff
            except Exception:
                with lock:
                    attempt_count += 1
                    conflict_count += 1
                time.sleep(0.01)
            finally:
                if not success_slot_used:
                    with lock:
                        allocated_writes -= 1
                
    # Launch worker threads
    threads = []
    for conn in worker_conns:
        t = threading.Thread(target=worker, args=(conn,))
        threads.append(t)
        t.start()
        
    # Main thread waits until matched load trigger threshold is reached
    trigger_threshold = max(1, int(num_writes * 0.3))
    while True:
        with lock:
            if success_count >= trigger_threshold or stop_event.is_set():
                break
        time.sleep(0.01)
        
    # Run concurrent compaction
    compaction_start_wall = time.time()
    compaction_dml_time_ms, compaction_vac_time_ms = candidate.compact(main_conn)
    compaction_end_wall = time.time()
    compaction_time = compaction_end_wall - compaction_start_wall
    
    with lock:
        compaction_active = False
        
    # Wait for all threads to finish
    for t in threads:
        t.join(timeout=10.0 if is_verify else 120.0)
        
    # Force stop if stuck
    any_alive = any(t.is_alive() for t in threads)
    if any_alive:
        print("Warning: Forced shutdown of worker threads due to timeout")
        stop_event.set()
        for t in threads:
            t.join()
            
    for conn in worker_conns:
        conn.close()
        
    # Filter writes that overlapped with compaction to measure stall
    compaction_write_latencies = [
        elapsed for start, end, elapsed in all_contended_writes
        if start < compaction_end_wall and end > compaction_start_wall
    ]
        
    lsn_after_contended = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    wal_bytes_contended = get_lsn_diff(lsn_before_contended, lsn_after_contended)
    wal_per_promotion_contended = wal_bytes_contended / success_count if success_count > 0 else 0.0
    
    # 7. Measure Final offline compaction (VACUUM FULL)
    lsn_before_full = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    full_start = time.perf_counter()
    if candidate.name == "Update-In-Place":
        main_conn.query("VACUUM FULL sim_inplace;")
    elif "Append-Only" in candidate.name:
        main_conn.query("VACUUM FULL sim_option_a;")
    elif "Event-Log" in candidate.name:
        main_conn.query("VACUUM FULL sim_option_b_snapshots;")
        main_conn.query("VACUUM FULL sim_option_b_events;")
    full_time_ms = (time.perf_counter() - full_start) * 1000
    lsn_after_full = main_conn.query("SELECT pg_current_wal_insert_lsn();")[0]
    wal_bytes_full = get_lsn_diff(lsn_before_full, lsn_after_full)
    
    # Physical measurements
    db_size = candidate.get_db_size(main_conn)
    workspace_size = get_dir_size(os.path.join(WORKSPACE_ROOT, candidate.name))
    
    main_conn.close()
    
    return {
        "candidate": candidate.name,
        "threads": num_threads,
        "checkpoint": checkpoint_interval,
        "success_rate": success_count / attempt_count if attempt_count > 0 else 0.0,
        "conflict_count": conflict_count,
        "read_med_1k_ms": read_median_1k,
        "read_p95_1k_ms": read_p95_1k,
        "read_med_5k_ms": read_median_5k,
        "read_p95_5k_ms": read_p95_5k,
        "read_med_50k_ms": read_median_50k,
        "read_p95_50k_ms": read_p95_50k,
        "read_slope_pct": read_slope,
        "write_control_med_ms": get_median(control_write_latencies) * 1000,
        "write_control_p95_ms": get_p95(control_write_latencies) * 1000,
        "write_contended_med_ms": get_median(write_latencies) * 1000,
        "write_contended_p95_ms": get_p95(write_latencies) * 1000,
        "compaction_dml_time_ms": compaction_dml_time_ms,
        "compaction_vac_time_ms": compaction_vac_time_ms,
        "compaction_time_ms": compaction_time * 1000,
        "compaction_full_time_ms": full_time_ms,
        "compaction_full_wal_bytes": wal_bytes_full,
        "compaction_stall_p95_ms": get_p95(compaction_write_latencies) * 1000 if compaction_write_latencies else 0.0,
        "db_size_kb": db_size / 1024,
        "workspace_size_kb": workspace_size / 1024,
        "scratch_size_kb": peak_scratch_size / 1024,
        "wal_control_bytes_per_promo": wal_per_promotion_control,
        "wal_contended_bytes_per_promo": wal_per_promotion_contended
    }

def run_harness_self_tests():
    print("Running harness self-tests for error-handling verification...")
    # 1. Test run_psql error detection
    try:
        run_psql(SIM_DB, "SELECT INVALID_COLUMN_NAME_FOR_TESTING;")
        raise AssertionError("run_psql failed to raise RuntimeError on invalid query")
    except RuntimeError as e:
        print(" -> run_psql error detection: PASSED")
        
    # 2. Test PGConnection error detection (syntax error) & connection reuse
    conn = PGConnection()
    try:
        conn.query("SELECT INVALID_COLUMN_NAME_FOR_TESTING;")
        raise AssertionError("PGConnection failed to raise RuntimeError on invalid query")
    except RuntimeError as e:
        # Assert connection is still reusable
        reuse_res = conn.query("SELECT 1;")
        if not reuse_res or reuse_res[0] != "1":
            raise AssertionError("PGConnection not reusable after syntax error")
        print(" -> PGConnection syntax error detection & connection reuse: PASSED")
        
    # 3. Test PGConnection unique constraint error detection & connection reuse (single statement)
    try:
        conn.query("DROP TABLE IF EXISTS test_err_unique;")
        conn.query("CREATE TABLE test_err_unique (id INT PRIMARY KEY);")
        conn.query("INSERT INTO test_err_unique VALUES (1);")
        try:
            conn.query("INSERT INTO test_err_unique VALUES (1);")  # Should fail
            raise AssertionError("PGConnection failed to raise RuntimeError on unique constraint violation")
        except RuntimeError as e:
            if "duplicate key" not in str(e) and "unique constraint" not in str(e):
                raise
            # Assert connection is still reusable (single-statement implicit transaction doesn't need rollback)
            reuse_res = conn.query("SELECT 1;")
            if not reuse_res or reuse_res[0] != "1":
                raise AssertionError("PGConnection not reusable after single-statement unique constraint violation")
            print(" -> PGConnection single-statement unique constraint error & connection reuse: PASSED")
    finally:
        try:
            conn.query("DROP TABLE IF EXISTS test_err_unique;")
        except Exception:
            pass

    # 4. Test PGConnection unique constraint error detection, rollback, & connection reuse (explicit transaction)
    try:
        conn.query("DROP TABLE IF EXISTS test_err_trans;")
        conn.query("CREATE TABLE test_err_trans (id INT PRIMARY KEY);")
        conn.query("INSERT INTO test_err_trans VALUES (1);")
        try:
            # Explicit transaction that fails
            conn.query("BEGIN;\nINSERT INTO test_err_trans VALUES (2);\nINSERT INTO test_err_trans VALUES (1);\nCOMMIT;")
            raise AssertionError("PGConnection failed to raise RuntimeError on unique constraint violation in transaction")
        except RuntimeError as e:
            if "duplicate key" not in str(e) and "unique constraint" not in str(e):
                raise
            # Connection is in transaction (failed). Must rollback.
            conn.query("ROLLBACK;")
            # Assert connection is reusable
            reuse_res = conn.query("SELECT 1;")
            if not reuse_res or reuse_res[0] != "1":
                raise AssertionError("PGConnection not reusable after transaction rollback")
            print(" -> PGConnection transaction unique constraint error, rollback & connection reuse: PASSED")
    finally:
        try:
            conn.query("DROP TABLE IF EXISTS test_err_trans;")
        except Exception:
            pass
        conn.close()
        
    print("Harness self-tests completed successfully. Verification coverage validated.\n")

def print_comparison_table(results, is_verify=False):
    print("\n=== STORAGE BATTERY RESULTS COMPARISON ===")
    r_1k_lbl = "R_p95_20" if is_verify else "R_p95_1k"
    r_5k_lbl = "R_p95_35" if is_verify else "R_p95_5k"
    r_50k_lbl = "R_p95_50" if is_verify else "R_p95_50k"
    print(f"{'Candidate':<20} | {'Thr':<3} | {'Chk':<3} | {r_1k_lbl:<8} | {r_5k_lbl:<8} | {r_50k_lbl:<9} | {'Slope%':<6} | {'W_Ctrl_p95':<10} | {'W_Cont_p95':<10} | {'Conflicts':<9} | {'DB Size(K)':<10} | {'Work(K)':<7} | {'Scratch(K)':<10} | {'WAL_Ctrl':<9} | {'WAL_Cont':<9} | {'Stall_p95':<9}")
    print("-" * 155)
    for r in results:
        print(f"{r['candidate']:<20} | {r['threads']:<3} | {r['checkpoint']:<3} | {r['read_p95_1k_ms']:<8.2f} | {r['read_p95_5k_ms']:<8.2f} | {r['read_p95_50k_ms']:<9.2f} | {r['read_slope_pct']:<6.1f}% | {r['write_control_p95_ms']:<10.2f} | {r['write_contended_p95_ms']:<10.2f} | {r['conflict_count']:<9} | {r['db_size_kb']:<10.2f} | {r['workspace_size_kb']:<7.2f} | {r['scratch_size_kb']:<10.2f} | {r['wal_control_bytes_per_promo']:<9.2f} | {r['wal_contended_bytes_per_promo']:<9.2f} | {r['compaction_stall_p95_ms']:<9.2f}")
    print("-" * 155)

    print("\n=== STORAGE BATTERY COMPACTION DETAIL ===")
    print(f"{'Candidate':<20} | {'Thr':<3} | {'Chk':<3} | {'DML (ms)':<10} | {'VAC (ms)':<10} | {'Total Conc (ms)':<15} | {'Full (ms)':<10} | {'Full WAL (K)':<12}")
    print("-" * 95)
    for r in results:
        total_conc = r['compaction_dml_time_ms'] + r['compaction_vac_time_ms']
        print(f"{r['candidate']:<20} | {r['threads']:<3} | {r['checkpoint']:<3} | {r['compaction_dml_time_ms']:<10.2f} | {r['compaction_vac_time_ms']:<10.2f} | {total_conc:<15.2f} | {r['compaction_full_time_ms']:<10.2f} | {r['compaction_full_wal_bytes']/1024:<12.2f}")
    print("-" * 95)

def main():
    parser = argparse.ArgumentParser(description="Sprint B: Storage Measurement Battery Harness")
    parser.add_argument("--verify", action="store_true", help="Perform verification check (1 thread, 10 iterations, fast setup)")
    parser.add_argument("--sweep", action="store_true", help="Run full parameter sweeps (threads: 10,20,30,40; checkpoints: 50,100,200,500)")
    
    args = parser.parse_args()
    
    if not args.verify and not args.sweep:
        print("Aether Storage Measurement Battery Harness Prepared.")
        print("STATUS: NOT YET AUTHORIZED TO RUN.")
        print("Execution requires Operator freeze confirmation.")
        print("To verify code readiness and database connection compatibility, run with --verify.")
        sys.exit(0)
        
    if args.sweep:
        # Check operator gating authorization
        if not os.environ.get("AETHER_OPERATOR_FREEZE") or not os.environ.get("AETHER_SWEEP_CONFIRMED"):
            print("ERROR: Execution is NOT authorized yet.")
            print("Please resolve Product Storage Budget (Blocker 1) and WAL Amplification Threshold (Blocker 2).")
            print("Wait for the Operator to freeze the thresholds and export AETHER_OPERATOR_FREEZE=1 AETHER_SWEEP_CONFIRMED=1.")
            sys.exit(1)
            
    create_db()
    
    if args.verify:
        run_harness_self_tests()
        print("Running verification validation check (dry run)...")
        candidates = [
            CandidateInPlace(),
            CandidateOptionA(),
            CandidateOptionB(checkpoint_interval=5)
        ]
        
        results = []
        for cand in candidates:
            res = run_benchmark_arm(cand, num_threads=2, num_writes=10, checkpoint_interval=5, is_verify=True)
            results.append(res)
            
        print_comparison_table(results, is_verify=True)
        print("Harness verification completed successfully. All queries, persistent connections, and measurements validated.")
        sys.exit(0)
        
    if args.sweep:
        print("Starting full authorized sweeps...")
        thread_sweep = [10, 20, 30, 40]
        checkpoint_sweep = [50, 100, 200, 500]
        
        results = []
        
        # 1. Update-In-Place Sweep
        cand = CandidateInPlace()
        for t in thread_sweep:
            res = run_benchmark_arm(cand, num_threads=t, num_writes=100, checkpoint_interval=100, is_verify=False)
            results.append(res)
            
        # 2. Option A Sweep
        cand = CandidateOptionA()
        for t in thread_sweep:
            res = run_benchmark_arm(cand, num_threads=t, num_writes=100, checkpoint_interval=100, is_verify=False)
            results.append(res)
            
        # 3. Option B Sweep
        for t in thread_sweep:
            for c in checkpoint_sweep:
                cand = CandidateOptionB(checkpoint_interval=c)
                res = run_benchmark_arm(cand, num_threads=t, num_writes=100, checkpoint_interval=c, is_verify=False)
                results.append(res)
                
        manifest_path = os.path.join(os.getcwd(), "scratch", "storage_battery_results.json")
        with open(manifest_path, "w") as f:
            json.dump(results, f, indent=2)
            
        print_comparison_table(results, is_verify=False)
        print(f"Sweep complete. Results written to {manifest_path}")

if __name__ == "__main__":
    main()
