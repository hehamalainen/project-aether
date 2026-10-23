#!/usr/bin/env python3
"""
Aether Ticket 0.5 Latency Decomposition & Pool Diagnostics.
Parses promotion telemetry records and computes P50, P95, and P99 latency splits
to isolate connection checkout wait, lock delay, and post-lock transaction hold.
"""

import os
import sys
import json
import subprocess

def percentile(data, percent):
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * percent
    f = int(k)
    c = f + 1
    if c < len(sorted_data):
        return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)
    else:
        return sorted_data[f]

def fetch_promotions(db_url):
    psql_cmd = ["psql"]
    if db_url and (db_url.startswith("postgres://") or db_url.startswith("postgresql://")):
        psql_cmd.append(db_url)
    else:
        psql_cmd.extend(["-d", "aether"])
        
    psql_cmd.extend([
        "-t", "-A", "-c", 
        "SELECT id, evidence FROM aether_promotions ORDER BY created_at ASC;"
    ])
    
    try:
        res = subprocess.run(psql_cmd, capture_output=True, text=True, check=True)
        lines = res.stdout.strip().split("\n")
        promotions = []
        for line in lines:
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 2:
                prom_id = parts[0]
                evidence_str = parts[1]
                
                try:
                    evidence = json.loads(evidence_str) if evidence_str else {}
                except json.JSONDecodeError:
                    evidence = {}
                    
                promotions.append({
                    "id": prom_id,
                    "evidence": evidence
                })
        return promotions
    except Exception as e:
        print(f"Error fetching promotions from database: {e}")
        return []

def main():
    db_url = os.getenv("DATABASE_URL")
    promotions = fetch_promotions(db_url)
    
    if not promotions:
        print("No promotions telemetry found. Ensure DATABASE_URL is set and server has run soaks.")
        sys.exit(0)
        
    t_pool_acquires = []
    t_lock_waits = []
    t_lock_holds = []
    t_connection_occupancies = [] # t_lock_wait + t_lock_hold
    
    for p in promotions:
        ev = p["evidence"]
        timings = {}
        if "_timings_ms" in ev:
            try:
                timings = json.loads(ev["_timings_ms"]) if isinstance(ev["_timings_ms"], str) else ev["_timings_ms"]
            except Exception:
                pass
        
        t_acquire = timings.get("t_pool_acquire_ms", timings.get("t_db_tx_start", 0.0))
        t_wait = timings.get("t_lock_wait", 0.0)
        t_hold = timings.get("t_lock_hold", 0.0)
        
        t_pool_acquires.append(t_acquire)
        t_lock_waits.append(t_wait)
        t_lock_holds.append(t_hold)
        t_connection_occupancies.append(t_wait + t_hold)
        
    count = len(promotions)
    
    print("=" * 90)
    print("                      AETHER TICKET 0.5 LATENCY DECOMPOSITION REPORT")
    print("=" * 90)
    print(f"Total Telemetry Samples: {count}")
    print("-" * 90)
    print("Connection Pool Configuration:")
    print("  - PostgreSQL Server max_connections:  250 (soak limit)")
    print("  - SQLx App Max Connections Pool Size:  50 (client app cap)")
    print("-" * 90)
    print(f"{'Timing Stage (ms)':<40} | {'P50 (Median)':<12} | {'P95':<12} | {'P99':<12}")
    print("-" * 90)
    
    metrics = [
        ("t_pool_acquire_ms (Checkout Wait)", t_pool_acquires),
        ("t_lock_wait (Lock Scheduling Wait)", t_lock_waits),
        ("t_lock_hold (Post-Lock Tx Execution)", t_lock_holds),
        ("Connection Occupancy (Wait + Hold)", t_connection_occupancies)
    ]
    
    for label, data in metrics:
        p50 = percentile(data, 0.50)
        p95 = percentile(data, 0.95)
        p99 = percentile(data, 0.99)
        print(f"{label:<40} | {p50:<12.3f} | {p95:<12.3f} | {p99:<12.3f}")
    print("=" * 90)

if __name__ == "__main__":
    main()
