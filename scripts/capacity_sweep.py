#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import statistics

SOAK_HOME = os.path.expanduser("~/.aether/.soak_workspace_pg")
SERVER_LOG = os.path.join(SOAK_HOME, "server.log")
PENDING_LOG = os.path.join(SOAK_HOME, "pending_log.txt")

def run_tier(agents: int, duration_seconds: int = 300) -> dict:
    repos = 40
    density = agents / repos
    print(f"\n=======================================================")
    print(f"🧹 Running capacity sweep tier: {agents} Agents | {repos} Repos | {density:.1f} Agents/Repo")
    print(f"=======================================================")
    
    env = os.environ.copy()
    env["SOAK_AGENTS"] = str(agents)
    env["SOAK_REPOS"] = str(repos)
    env["SOAK_RUN_SECONDS"] = str(duration_seconds)
    env["AETHER_SOAK_HOME"] = SOAK_HOME
    
    cmd = ["bash", "scripts/soak_native_rust_pg.sh"]
    process = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = process.stdout
    
    passed_invariants = (process.returncode == 0)
    if not passed_invariants:
        print(f"❌ Soak failed for {agents} agents. See output:")
        print("\n".join(output.splitlines()[-30:]))
        
    return analyze_results(agents, output, passed_invariants)

def check_pool_pressure() -> str:
    # Run the actual pool telemetry script
    cmd = ["python3", "scripts/analyze_pool_telemetry.py", "--soak-home", SOAK_HOME]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        out = proc.stdout
        if "Pattern A (Pool Saturation)              : YES (Dominant)" in out:
            return "COLLAPSED"
        if "Pattern A (Pool Saturation)              : Present, but not sustained" in out:
            return "MODERATE"
        return "OK"
    except Exception as e:
        return "ERROR"

def analyze_results(agents: int, driver_output: str, passed_invariants: bool) -> dict:
    submit_tps = 0.0
    promote_tps = 0.0
    promote_409_rate = 0.0
    for line in driver_output.splitlines():
        if line.strip().startswith("Submit TPS:"):
            try: submit_tps = float(line.split(":")[-1].strip())
            except: pass
        if line.strip().startswith("Promote TPS:"):
            try: promote_tps = float(line.split(":")[-1].strip())
            except: pass
        if line.strip().startswith("Promote Contention:"):
            try: promote_409_rate = float(line.split("rate:")[-1].strip().replace(")", ""))
            except: pass

    rho = submit_tps / promote_tps if promote_tps > 0 else 999.9

    latencies = []
    if os.path.exists(SERVER_LOG):
        with open(SERVER_LOG, "r") as f:
            for line in f:
                if "latency_ms=" in line:
                    parts = line.split("latency_ms=")
                    if len(parts) > 1:
                        try:
                            l_str = ''.join(c for c in parts[1].split()[0] if c.isdigit() or c == '.')
                            latencies.append(float(l_str))
                        except: pass
    
    p95 = statistics.quantiles(latencies, n=100)[94] if len(latencies) >= 100 else 0.0
    p99 = statistics.quantiles(latencies, n=100)[98] if len(latencies) >= 100 else 0.0

    slope = 0.0
    if os.path.exists(PENDING_LOG):
        with open(PENDING_LOG, "r") as f:
            data = []
            for line in f:
                parts = line.strip().split('|')
                if len(parts) == 2:
                    try: data.append((float(parts[0]), int(parts[1])))
                    except: pass
            
            if data:
                start_epoch = data[0][0]
                steady_data = [(t, c) for t, c in data if t - start_epoch >= 60]
                if len(steady_data) > 1:
                    t_start, c_start = steady_data[0]
                    t_end, c_end = steady_data[-1]
                    delta_t = t_end - t_start
                    if delta_t > 0:
                        slope = (c_end - c_start) / delta_t
    
    pool_pressure = check_pool_pressure()

    # Pass/fail overload logic (strict)
    is_safe = (rho <= 1.05) and (slope <= 1.0) and passed_invariants

    return {
        "agents": agents,
        "submit_tps": submit_tps,
        "promote_tps": promote_tps,
        "rho": rho,
        "409_rate": promote_409_rate,
        "p95": p95,
        "p99": p99,
        "backlog_slope": slope,
        "pool": pool_pressure,
        "invariants": passed_invariants,
        "is_safe": is_safe
    }

def print_table(results):
    print("\n\n")
    print("=======================================================================================================================")
    print(f"| Agents | Submit TPS | Promote TPS | Rho   | Backlog Slope (TPS) | P95 (ms) | P99 (ms) | 409 Rate | Pool     | Inv. |")
    print("=======================================================================================================================")
    for r in results:
        inv = "✅" if r['invariants'] else "❌"
        slope_flag = "⚠️" if r['backlog_slope'] > 1.0 else " "
        rho_flag = "⚠️" if r['rho'] > 1.05 else " "
        safe_str = "🟢" if r['is_safe'] else "🔴"
        print(f"| {safe_str} {r['agents']:>3} | {r['submit_tps']:>10.2f} | {r['promote_tps']:>11.2f} | {r['rho']:>4.2f}{rho_flag}| {r['backlog_slope']:>18.2f} {slope_flag}| {r['p95']:>8.1f} | {r['p99']:>8.1f} | {r['409_rate']:>8.2f} | {r['pool']:>8} | {inv:>4} |")
    print("=======================================================================================================================\n")

def main():
    print("Starting A1 Capacity Sweep (Density Sweep)")
    sys.stdout.flush()
    results = []
    
    # 40 repos fixed. Sweeping [1, 2, 3, 4, 5] agents per repo
    coarse_tiers = [40, 80, 120, 160, 200]
    
    overloaded_tier = None
    for agents in coarse_tiers:
        res = run_tier(agents)
        results.append(res)
        print_table(results)
        sys.stdout.flush()
        
        if not res['is_safe']:
            overloaded_tier = agents
            print(f"\n🚨 Overload detected at {agents} agents (rho={res['rho']:.2f}, slope={res['backlog_slope']:.2f}, invariants={res['invariants']}). Stopping coarse sweep.")
            break
            
    if overloaded_tier is None:
        print("Never reached overload in the coarse tier. Capacity exceeds 200 agents!")
        return

    if overloaded_tier == coarse_tiers[0]:
        print("Lowest tier overloaded. The system cannot sustain even 1 agent per repo at 40 width.")
        return
        
    idx = coarse_tiers.index(overloaded_tier)
    safe_tier = coarse_tiers[idx - 1]
    
    print(f"\nStarting Pass 2: Fine Sweep between {safe_tier} and {overloaded_tier} (Holding REPOS=40)")
    sys.stdout.flush()
    fine_tiers = list(range(safe_tier + 10, overloaded_tier, 10))
    
    for agents in fine_tiers:
        res = run_tier(agents)
        results.append(res)
        print_table(results)
        sys.stdout.flush()
        if not res['is_safe']:
            print(f"\n🚨 Fine overload detected at {agents} agents.")
            break

    results.sort(key=lambda x: x['agents'])
    print("\n🏁 FINAL CAPACITY SWEEP RESULTS")
    print_table(results)
    
    # Identify boundaries
    safe_results = [r for r in results if r['is_safe']]
    unsafe_results = [r for r in results if not r['is_safe']]
    
    if not safe_results:
        print("❌ No safe tier identified.")
        return
        
    best_safe = max(safe_results, key=lambda x: x['agents'])
    lowest_unsafe = min(unsafe_results, key=lambda x: x['agents']) if unsafe_results else None
    
    print(f"🎯 RECOMMENDED KNEE (BALANCE POINT): {best_safe['agents']} agents")
    
    if lowest_unsafe:
        print(f"\n♻️ Starting Pass 3: Boundary Validation (Repeating {best_safe['agents']} and {lowest_unsafe['agents']})")
        sys.stdout.flush()
        val_safe = run_tier(best_safe['agents'])
        val_unsafe = run_tier(lowest_unsafe['agents'])
        
        print("\n🏁 VALIDATION RESULTS")
        print_table([val_safe, val_unsafe])
        
        if val_safe['is_safe'] and not val_unsafe['is_safe']:
            print(f"✅ Boundary fully confirmed. The exact sustainable capacity is {best_safe['agents']} agents.")
        else:
            print(f"❌ Boundary validation diverged! The knee is noisy. Operator review required.")
    else:
        print(f"✅ Boundary confirmed. The exact sustainable capacity is {best_safe['agents']} agents.")

if __name__ == "__main__":
    main()
