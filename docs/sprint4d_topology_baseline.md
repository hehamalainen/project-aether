# Sprint 4D Topology Baseline Report

**Execution Provenance (Historical Pre-Release Run):**
- **Historical Branch**: `soak/sprint-a-20260616` (pre-release development branch; not a ref in the single-root public release repository)
- **Historical Commit**: `ab03e58a8faf1c16a993d1ce3fd74c6a28b26fdf` (pre-release development SHA; not a reachable object in the single-root public release repository)
- **Command**: `./scripts/run_topology_baseline.sh`
- **Sweep Duration**: 300 seconds

> [!NOTE]
> **Confirmatory Long-Run Soak Validation Notice:**
> The benchmarks compiled in this report represent 300-second soaks. These serve as confirmatory baseline validation of topology sizing trends and autovacuum metrics under config-level database variations.

---

## 1. Comparative Sizing & Performance Metrics Matrix

| Benchmark Topology | Total Agents ($N$) | Total Repos | Topology Shape | Tuned? | Committed Proms | TOAST Size (MB) | TOAST MB / 1k Proms | Base Dead/Live | TOAST Dead/Live | Base Vac Count | Base Last Vac | TOAST Vac Count | TOAST Last Vac | P95 Checkout | P99 Checkout | P95 Lock Wait | P99 Lock Wait | P95 Post-Lock Hold | P99 Post-Lock Hold | P95 Occupancy | P99 Occupancy |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1_Repo_Baseline | 80 | 1 | uniform | No | 158 | 0.031 MB | 0.196 MB | 7.0000 | 0.0 | 0 | never | 0 | never | 152.716 ms | 394.670 ms | 3573.825 ms | 4369.849 ms | 612.588 ms | 974.736 ms | 3695.967 ms | 4444.541 ms |
| 10_Repos_Uniform | 80 | 10 | uniform | No | 1323 | 0.039 MB | 0.029 MB | 4.5000 | 0.0 | 1 | 20:20:00 | 0 | never | 271.248 ms | 489.457 ms | 284.484 ms | 946.582 ms | 1409.330 ms | 2296.793 ms | 1654.181 ms | 2659.461 ms |
| 100_Repos_Uniform | 80 | 100 | 100repos | No | 7576 | 0.039 MB | 0.005 MB | 0.5125 | 0.0 | 5 | 20:29:22 | 0 | never | 202.044 ms | 331.626 ms | 68.529 ms | 153.964 ms | 860.726 ms | 1523.732 ms | 893.910 ms | 1576.478 ms |
| Hot_Repo_Skew | 80 | 10 | skew | No | 1362 | 0.039 MB | 0.028 MB | 0.4000 | 0.0 | 3 | 20:34:58 | 0 | never | 343.373 ms | 633.795 ms | 528.961 ms | 3265.895 ms | 798.401 ms | 1415.236 ms | 1169.862 ms | 3600.640 ms |
| 1_Repo_Max_Stress | 160 | 1 | uniform | No | 80 | 0.031 MB | 0.387 MB | 13.0000 | 0.0 | 0 | never | 0 | never | 602.208 ms | 703.598 ms | 2609.792 ms | 3230.579 ms | 205.788 ms | 441.906 ms | 2713.142 ms | 3302.877 ms |
| Hot_Repo_Skew_Max_Stress | 160 | 10 | skew | No | 960 | 0.039 MB | 0.040 MB | 5.8000 | 0.0 | 2 | 20:44:43 | 0 | never | 695.427 ms | 1019.748 ms | 1310.908 ms | 3804.507 ms | 747.546 ms | 1256.130 ms | 1707.994 ms | 4114.195 ms |
