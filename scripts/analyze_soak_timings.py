#!/usr/bin/env python3
import json
import os
import glob
import sys
import argparse
from typing import List, Dict, Any

TRACKED_STAGES = [
    "t_db_read_pre_orch",
    "t_crdt_eval",
    "t_materialize",
    "t_gates",
    "t_trust_math",
    "t_fs_rename",
    "t_db_tx_start",
    "t_db_revalidate",
    "t_db_insert_snapshot_gates",
    "t_db_update_proposals_trust",
    "t_db_insert_promotions_heads_cas",
    "t_db_commit"
]

SUB_STAGES = [
    "t_db_read_repo",
    "t_db_read_proposals",
    "t_db_read_base_snapshot",
    "req_proposal_count",
    "t_db_insert_promotion",
    "t_db_insert_head",
    "t_db_cas_update",
    "t_db_checkout_repo",
    "t_db_exec_repo",
    "t_db_checkout_proposals",
    "t_db_exec_proposals",
    "t_db_checkout_base_snapshot",
    "t_db_exec_base_snapshot",
    "t_db_checkout_tx",
    "t_db_exec_promotion",
    "t_db_exec_head",
    "t_db_exec_cas",
    "t_lock_wait",
    "t_lock_hold"
]

def calculate_percentiles(values: List[float]) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    median = sorted_vals[int(n * 0.50)]
    p95 = sorted_vals[int(n * 0.95) if n > 0 else 0]
    p99 = sorted_vals[int(n * 0.99) if n > 0 else 0]
    return median, p95, p99

def main():
    parser = argparse.ArgumentParser(description="Analyze Aether-RS soak timings")
    parser.add_argument("--soak-home", default=".soak_workspace_pg", help="Path to soak workspace")
    args = parser.parse_args()

    search_pattern = os.path.join(args.soak_home, "counters.*", "agent_*", "promotions.jsonl")
    files = glob.glob(search_pattern)

    if not files:
        print(f"❌ No promotions.jsonl files found matching: {search_pattern}")
        sys.exit(1)

    print(f"🔍 Found {len(files)} agent log files to analyze.")

    rows_analyzed = 0
    missing_timings_blob = 0
    missing_required_keys = 0
    malformed_json = 0
    success_path_count = 0
    reject_path_count = 0

    residuals = []
    stage_data: Dict[str, List[float]] = {k: [] for k in TRACKED_STAGES + SUB_STAGES}
    stage_data["t_total_inner"] = []

    for fpath in files:
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows_analyzed += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    malformed_json += 1
                    continue
                
                success = payload.get("success", False)
                if success:
                    success_path_count += 1
                else:
                    reject_path_count += 1

                evidence = payload.get("evidence", {})
                if not evidence or "_timings_ms" not in evidence:
                    missing_timings_blob += 1
                    continue

                try:
                    if isinstance(evidence["_timings_ms"], str):
                        timings = json.loads(evidence["_timings_ms"])
                    else:
                        timings = evidence["_timings_ms"]
                except Exception:
                    malformed_json += 1
                    continue
                
                if "t_total_inner" not in timings:
                    missing_required_keys += 1
                    continue
                
                t_total_inner = timings["t_total_inner"]
                if t_total_inner <= 0:
                    continue
                
                # Check that ALL standard tracked stages are present
                has_all_keys = True
                for stage in TRACKED_STAGES:
                    if stage not in timings:
                        has_all_keys = False
                        break
                
                if not has_all_keys:
                    missing_required_keys += 1
                    continue
                
                # Now record the timings since we have a complete and valid row
                sum_tracked = 0.0
                for stage in TRACKED_STAGES:
                    val = timings[stage]
                    stage_data[stage].append(val)
                    sum_tracked += val
                
                for stage in SUB_STAGES:
                    if stage in timings:
                        stage_data[stage].append(timings[stage])
                
                stage_data["t_total_inner"].append(t_total_inner)
                residual = t_total_inner - sum_tracked
                residuals.append(residual)

    print("=============================================")
    print("AETHER TIMING ANALYSIS RESULTS")
    print("=============================================")
    print(f"Sample Counts:")
    print(f"  Rows Analyzed:                {rows_analyzed}")
    print(f"  Missing Timing Blob:          {missing_timings_blob}")
    print(f"  Missing Required Stage Keys:  {missing_required_keys}")
    print(f"  Malformed JSON:               {malformed_json}")
    print(f"Path Split:")
    print(f"  Success Path:        {success_path_count}")
    print(f"  Reject Path:         {reject_path_count}")

    total_attempts = success_path_count + reject_path_count
    contention_ratio = reject_path_count / total_attempts if total_attempts > 0 else 0.0
    sum_total_inner = sum(stage_data["t_total_inner"])
    mean_pop_latency = sum_total_inner / total_attempts if total_attempts > 0 else 0.0
    amortized_cost_success = sum_total_inner / success_path_count if success_path_count > 0 else 0.0

    print(f"Anti-Bias Telemetry Normalization (Visibility Phase):")
    print(f"  Total Promotion Attempts:          {total_attempts}")
    print(f"  Contention Waste Ratio:            {contention_ratio * 100:.2f}%")
    print(f"  Sum Total Request Latency:         {sum_total_inner:.2f} ms")
    print(f"  Mean Request-Population Latency:   {mean_pop_latency:.2f} ms (sum latency / total attempts)")
    print(f"  Amortized Cost per Success:        {amortized_cost_success:.2f} ms (sum latency / successful commits)")

    if rows_analyzed == 0:
        print("\n❌ No valid timing metrics were found.")
        sys.exit(1)

    if residuals:
        print("\nResidual Stats (t_total_inner - sum(tracked_stages)):")
        med, p95, p99 = calculate_percentiles(residuals)
        print(f"  Median: {med:7.2f} ms")
        print(f"  p95:    {p95:7.2f} ms")
        print(f"  p99:    {p99:7.2f} ms")

    print("\nIndividual Tracked Stages (ms):")
    print(f"{'Stage':<35} | {'Median':<7} | {'p95':<7} | {'p99':<7}")
    print("-" * 65)
    for stage in TRACKED_STAGES + ["t_total_inner"] + SUB_STAGES:
        if stage == "req_proposal_count":
            continue
        if not stage_data[stage]:
            print(f"{stage:<35} | {'N/A':<7} | {'N/A':<7} | {'N/A':<7}")
        else:
            m, p5, p9 = calculate_percentiles(stage_data[stage])
            print(f"{stage:<35} | {m:7.2f} | {p5:7.2f} | {p9:7.2f}")

    if stage_data.get("req_proposal_count"):
        m, p5, p9 = calculate_percentiles(stage_data["req_proposal_count"])
        print("\nRequest Metadata (count):")
        print(f"{'req_proposal_count':<35} | {m:7.2f} | {p5:7.2f} | {p9:7.2f}")

    print("=============================================")

if __name__ == "__main__":
    main()
