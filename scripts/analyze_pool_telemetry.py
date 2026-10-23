#!/usr/bin/env python3
import os
import sys
import argparse
from collections import defaultdict

def main():
    parser = argparse.ArgumentParser(description="Analyze Aether Sprint 1D Pool Telemetry")
    parser.add_argument("--soak-home", default=".soak_workspace_pg", help="Directory containing soak artifacts")
    args = parser.parse_args()

    pool_log_path = os.path.join(args.soak_home, "pool_telemetry.log")
    pg_log_path = os.path.join(args.soak_home, "pg_stats.log")

    if not os.path.exists(pool_log_path):
        print(f"❌ Pool telemetry log not found: {pool_log_path}")
        return
    if not os.path.exists(pg_log_path):
        print(f"❌ PG stats log not found: {pg_log_path}")
        return

    # 1. Parse Pool Telemetry
    pool_samples = 0
    pool_zero_idle = 0
    pool_has_waiters = 0
    max_waiters = 0

    with open(pool_log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    idle = int(parts[2].strip().split("=")[1])
                    waiters = int(parts[3].strip().split("=")[1])
                    pool_samples += 1
                    if idle == 0:
                        pool_zero_idle += 1
                    if waiters > 0:
                        pool_has_waiters += 1
                    max_waiters = max(max_waiters, waiters)
                except ValueError:
                    pass

    # 2. Parse PG Stats Activity
    pg_samples = 0
    state_occupancy = defaultdict(int)
    active_wait_events = defaultdict(int)

    with open(pg_log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 5:
                # ts|state|wait_event_type|wait_event|count
                state = parts[1].strip()
                wait_type = parts[2].strip()
                wait_event = parts[3].strip()
                try:
                    count = int(parts[4].strip())
                    pg_samples += count
                    if state:
                        state_occupancy[state] += count
                    if state == "active" and wait_type and wait_event:
                        wait_key = f"{wait_type}|{wait_event}"
                        active_wait_events[wait_key] += count
                except ValueError:
                    pass

    print("=============================================")
    print("AETHER POOL & DB TELEMETRY ANALYSIS (Sprint 1D)")
    print("=============================================")
    print(f"Pool Samples: {pool_samples}")
    print(f"PG Connection Samples (Total Activity Rows): {pg_samples}")
    print()

    print("--- Pool State Occupancy ---")
    if pool_samples > 0:
        print(f"Zero Idle Connections:    {pool_zero_idle} / {pool_samples} ({pool_zero_idle/pool_samples*100:.1f}%)")
        print(f"Waiters In Queue (>0):    {pool_has_waiters} / {pool_samples} ({pool_has_waiters/pool_samples*100:.1f}%)")
        print(f"Max App-Side Waiters:     {max_waiters}")
    else:
        print("No pool data.")

    print("\n--- PG State Occupancy ---")
    if pg_samples > 0:
        for state, count in sorted(state_occupancy.items(), key=lambda x: x[1], reverse=True):
            print(f"{state:<25} | {count:6} ({count/pg_samples*100:.1f}%)")
    
    print("\n--- Top Active PG Wait Events ---")
    active_samples = sum(count for state, count in state_occupancy.items() if state == "active")
    if active_samples > 0:
        for event, count in sorted(active_wait_events.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"{event:<25} | {count:6} ({count/active_samples*100:.1f}% of active)")
    else:
        print("No active wait event data.")

    print("\n=============================================")
    print("HYPOTHESIS MATCH SUMMARY")
    print("=============================================")

    # Evaluate matches (tri-state: NO, Present, YES)
    res_a = "NO"
    res_b = "NO"
    res_c = "NO"

    if pool_samples > 0:
        if (pool_zero_idle / pool_samples > 0.8) and (pool_has_waiters / pool_samples > 0.5):
            res_a = "YES (Dominant)"
        elif pool_has_waiters > 0:
            res_a = "Present, but not sustained"

    if active_samples > 0:
        wal_waiters = sum(count for event, count in active_wait_events.items() if "WALWrite" in event or "transactionid" in event)
        if wal_waiters / active_samples > 0.5:
            res_b = "YES (Dominant)"
        elif wal_waiters > 0:
            res_b = "Present, but not dominant"

    if pg_samples > 0:
        idle_in_tx = state_occupancy.get("idle in transaction", 0)
        if idle_in_tx / pg_samples > 0.3:
            res_c = "YES (Dominant)"
        elif idle_in_tx > 0:
            res_c = "Present, but cleanly rejected as dominant"

    print(f"Pattern A (Pool Saturation)              : {res_a}")
    print(f"Pattern B (WAL/Transaction Back-pressure): {res_b}")
    print(f"Pattern C (Transaction Scope Hold)       : {res_c}")

    print("\nNote: Cross-reference with 'analyze_soak_timings.py' to confirm per-query P95/P99 latencies.")

if __name__ == "__main__":
    main()
