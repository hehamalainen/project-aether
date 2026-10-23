#!/usr/bin/env python3
"""
Aether Ticket 0 CRDT Diagnostics Telemetry & Storage Compiler.
Analyzes filesystem projected snapshot footprints, reads database promotion evidence columns,
and aggregates connection/alignment timing metrics.
"""

import os
import sys
import json
import subprocess

def analyze_storage(storage_dir):
    snapshot_dirs_count = 0
    total_files = 0
    total_size_bytes = 0
    
    if not os.path.exists(storage_dir):
        return 0, 0, 0.0
        
    for root, dirs, files in os.walk(storage_dir):
        if os.path.basename(root) == 'snapshots':
            snapshot_dirs_count += len(dirs)
        
        for file in files:
            if file.startswith('.'):
                continue
            file_path = os.path.join(root, file)
            try:
                total_size_bytes += os.path.getsize(file_path)
                total_files += 1
            except OSError:
                pass
                
    return snapshot_dirs_count, total_files, total_size_bytes / (1024.0 * 1024.0)

def fetch_promotions(db_url):
    psql_cmd = ["psql"]
    if db_url and (db_url.startswith("postgres://") or db_url.startswith("postgresql://")):
        psql_cmd.append(db_url)
    else:
        psql_cmd.extend(["-d", "aether"])
        
    psql_cmd.extend([
        "-t", "-A", "-c", 
        "SELECT id, evidence, to_head_id, to_head_sequence, policy_score, final_score FROM aether_promotions ORDER BY created_at ASC;"
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
                to_head_id = parts[2] if len(parts) > 2 else ""
                to_head_seq = parts[3] if len(parts) > 3 else "0"
                policy_score = parts[4] if len(parts) > 4 else "0"
                final_score = parts[5] if len(parts) > 5 else "0"
                
                try:
                    evidence = json.loads(evidence_str) if evidence_str else {}
                except json.JSONDecodeError:
                    evidence = {}
                    
                promotions.append({
                    "id": prom_id,
                    "evidence": evidence,
                    "to_head_id": to_head_id,
                    "to_head_sequence": int(to_head_seq) if to_head_seq.isdigit() else 0,
                    "policy_score": float(policy_score) if policy_score else 0.0,
                    "final_score": float(final_score) if final_score else 0.0
                })
        return promotions
    except Exception as e:
        print(f"Note: database query returned empty or failed: {e}")
        return []

def main():
    home = os.path.expanduser("~")
    
    # Robust path resolution that respects SOAK_HOME / AETHER_SOAK_HOME
    soak_home_env = os.getenv("AETHER_SOAK_HOME") or os.getenv("SOAK_HOME")
    if soak_home_env:
        default_storage = os.path.join(soak_home_env, ".aether", "aether_storage")
    else:
        soak_storage = os.path.join(home, ".aether", ".soak_workspace_pg", ".aether", "aether_storage")
        default_storage = soak_storage if os.path.exists(soak_storage) else os.path.join(home, ".aether", "aether_storage")
        
    storage_dir = os.getenv("AETHER_STORAGE_ROOT", default_storage)
    db_url = os.getenv("DATABASE_URL")

    
    print("=" * 115)
    print("                                AETHER TICKET 0 DIAGNOSTICS REPORT")
    print("=" * 115)
    
    # 1. Disk footprint
    snap_count, file_count, total_mb = analyze_storage(storage_dir)
    print("Filesystem Snapshot Footprint:")
    print(f"  - Storage Location:             {storage_dir}")
    print(f"  - Total Snapshots Materialized: {snap_count}")
    print(f"  - Total Materialized Files:     {file_count}")
    print(f"  - Total Filesystem Size (MB):   {total_mb:.3f} MB")
    print("-" * 115)
    
    # 2. Database Promotions
    promotions = fetch_promotions(db_url)
    print(f"PostgreSQL Promotions Count: {len(promotions)}")
    print("-" * 115)
    
    if promotions:
        headers = ["Prom ID", "Seq", "Visible", "Tombstones", "Empty", "Ops", "Acquire(ms)", "Eval(ms)", "Hold(ms)", "Align(ms)", "Frontier W"]
        print(f"{headers[0]:<10} | {headers[1]:<4} | {headers[2]:<8} | {headers[3]:<10} | {headers[4]:<6} | {headers[5]:<6} | {headers[6]:<11} | {headers[7]:<9} | {headers[8]:<9} | {headers[9]:<9} | {headers[10]:<10}")
        print("-" * 115)
        
        for p in promotions:
            ev = p["evidence"]
            timings = {}
            if "_timings_ms" in ev:
                try:
                    timings = json.loads(ev["_timings_ms"]) if isinstance(ev["_timings_ms"], str) else ev["_timings_ms"]
                except Exception:
                    pass
            
            visible = ev.get("crdt_visible_elements", "N/A")
            tombstones = ev.get("crdt_tombstones", "N/A")
            empty = ev.get("crdt_empty_non_root", "N/A")
            ops = ev.get("crdt_operations", "N/A")
            
            t_acquire = timings.get("t_pool_acquire_ms", timings.get("t_db_tx_start", 0.0))
            t_eval = timings.get("t_crdt_eval", 0.0)
            t_hold = timings.get("t_lock_hold", 0.0)
            t_align = timings.get("t_rga_alignment_ms", 0.0)
            f_width = timings.get("frontier_width", 0.0)
            
            p_id = p["id"]
            if len(p_id) > 10:
                p_id = p_id[:10]
            seq = p["to_head_sequence"]
            
            print(f"{p_id:<10} | {seq:<4} | {visible:<8} | {tombstones:<10} | {empty:<6} | {ops:<6} | {t_acquire:<11.2f} | {t_eval:<9.2f} | {t_hold:<9.2f} | {t_align:<9.2f} | {f_width:<10.0f}")
        print("-" * 115)
    
    # 3. Read system physics endpoint to show live global hit/miss rates
    # We use urllib to avoid importing third-party packages if not needed, falling back gracefully
    import urllib.request
    try:
        port = os.getenv("PORT", "8100")
        with urllib.request.urlopen(f"http://localhost:{port}/v1/system/physics", timeout=2.0) as req:
            physics = json.loads(req.read().decode())
            hits = physics.get("projected_cache_hits", 0)
            misses = physics.get("projected_cache_misses", 0)
            total = hits + misses
            rate = (hits / total * 100.0) if total > 0 else 0.0
            print("Global Projected Cache Performance (/v1/system/physics):")
            print(f"  - Cache Hits:   {hits}")
            print(f"  - Cache Misses: {misses}")
            print(f"  - Hit Rate:     {rate:.2f}%")
            print("-" * 115)
    except Exception as e:
        print("Note: Could not query /v1/system/physics endpoint (server not running or offline).")
        print("-" * 115)

if __name__ == "__main__":
    main()
