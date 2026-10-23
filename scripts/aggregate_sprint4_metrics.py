#!/usr/bin/env python3
import json
import argparse
import os
import statistics

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--density", required=True)
    return parser.parse_args()

def main():
    args = parse_args()
    
    # 1. Parse run_summary.json
    summary_path = os.path.join(args.home, "run_summary.json")
    try:
        with open(summary_path, "r") as f:
            summary = json.load(f)
    except Exception as e:
        print(f"| {args.lane} | {args.density} | ERROR: {str(e)} |")
        return

    # 2. Parse promotions.jsonl for timings
    timings_path = os.path.join(args.home, "promotions.jsonl")
    latencies = []
    t_lock_waits = []
    t_lock_holds = []
    
    try:
        with open(timings_path, "r") as f:
            for line in f:
                if not line.strip(): continue
                data = json.loads(line)
                if "evidence" in data and "_timings_ms" in data["evidence"]:
                    t = data["evidence"]["_timings_ms"]
                    if "t_total" in t: latencies.append(t["t_total"])
                    if "t_lock_wait" in t: t_lock_waits.append(t["t_lock_wait"])
                    if "t_lock_hold" in t: t_lock_holds.append(t["t_lock_hold"])
    except FileNotFoundError:
        pass

    def p99(data):
        if not data: return 0.0
        return sorted(data)[int(len(data) * 0.99)] if len(data) > 0 else 0.0

    p99_lat = p99(latencies)
    p99_wait = p99(t_lock_waits)
    p99_hold = p99(t_lock_holds)

    # 3. Parse pg_stats.log
    pg_stats_path = os.path.join(args.home, "pg_stats.log")
    max_idle_in_tx = 0
    try:
        with open(pg_stats_path, "r") as f:
            for line in f:
                if "idle in transaction" in line:
                    parts = line.strip().split('|')
                    if len(parts) >= 5:
                        max_idle_in_tx = max(max_idle_in_tx, int(parts[4]))
    except FileNotFoundError:
        pass

    # 4. Parse queue_depths.log (coarse corroboration)
    queue_path = os.path.join(args.home, "queue_depths.log")
    max_queue_depth = 0
    try:
        with open(queue_path, "r") as f:
            for line in f:
                if line.strip().isdigit():
                    max_queue_depth = max(max_queue_depth, int(line.strip()))
    except FileNotFoundError:
        pass

    # Compute metrics
    # Using ELAPSED from bash run would be best, but we'll approximate based on expected RUN_SECONDS if not provided,
    # actually let's assume 1800s unless we know otherwise. Or we can just output raw counts.
    # We will output raw counts, rate of 409s, P99 latency, wait/hold, max_queue, max_idle
    total_promos = summary.get("total_promote_ok", 0) + summary.get("promote_409", 0) + summary.get("promote_gate_rejects", 0) + summary.get("promote_invalid_state_rejects", 0) + summary.get("promote_5xx", 0)
    rate_409 = (summary.get("promote_409", 0) / total_promos * 100) if total_promos > 0 else 0
    
    # Initialize header if it's the first run
    # (The bash script can just handle this, but we'll just print rows)
    row = f"| {args.lane} | {args.density} | {summary.get('total_submit_ok',0)} | {summary.get('total_promote_ok',0)} | {rate_409:.1f}% | {summary.get('promote_invalid_state_rejects',0)} | {p99_lat:.1f}ms | {p99_wait:.1f}ms / {p99_hold:.1f}ms | {max_queue_depth} | {max_idle_in_tx} | {summary.get('per_repo_perf_violations', 0)} |"
    print(row)

if __name__ == "__main__":
    main()
