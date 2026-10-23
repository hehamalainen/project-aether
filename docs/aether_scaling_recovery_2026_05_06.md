# Aether Scaling Sprint: Recovery & Continuation Guide

**Date**: May 6, 2026
**Historical Branch**: `codex/backup-20260504-full-recovery` (pre-release development branch; not present in the single-root public release repository)
**Historical Last Stable Commit**: `f8787e2b54c8` (pre-release development SHA; not present in the single-root public release repository)

This document serves as an emergency recovery state and continuation guide for the Aether Promotion Engine Scaling objective. It ensures that if all agent sessions (Antigravity, Copilot) are lost, the team can immediately resume work without losing context or team dynamics.

---

## 1. Current Architectural State

We have successfully completed the **Python Unblocking Optimization Pass** for the `aether-rs` external promotion architecture.
*   All idle bottlenecks, 409 conflict starvation loops, and blocking IO calls in the Python orchestrator have been resolved.
*   **Hash Semantics**: `DocumentStore.get_content_hash()` was explicitly renamed to `get_state_hash()` to clarify that it operates deterministically on CRDT sequence states.
*   **Mock Gates**: The simulation fast-path is strictly walled behind `AETHER_ENABLE_MOCK_GATE_FAST_PATH=1`.

### The Structural Ceilings (The Final Soak)
A 300-second sustained external APFS benchmark (`swarm-safe-synthetic_benchmark-20260506-081714.json`) proved that the system has hit two hard structural walls:
1.  **The CRDT / Python GIL Wall**: Throughput degrades to `~7.8 proposals/sec` over 5 minutes. CRDT sequence growth causes `apply_diff` to slow linearly. Because of Python's Global Interpreter Lock (GIL), the 4 concurrent workers serialize CPU time, creating an absolute throughput ceiling.
2.  **The SQLite Lock Contention**: `Submit P95` latency fails the `140ms` SLO (measuring `~560ms`) due to the Rust backend waiting on the strict SQLite write-lock held by the Python workers.

---

## 2. Cold-Start Context Reconstruction (Emergency Prompts)

If the IDE or terminal sessions crash and all conversational context is lost, **do not try to explain the project manually**. Instead, paste the following exact prompts to the respective agents. These prompts force the agents to systematically rebuild their mental models by reading the foundational architectural documents before taking any action.

### Prompt for Antigravity (Gemini) - Main Coding Agent
> **Copy and paste this to Antigravity:**
> "System crash recovery initiated. You are Antigravity (Gemini), the Main Coding Agent, Lead Architect, and Systems Engineer for Project Aether. 
> 
> **Immediate Directive:** Do NOT write any code yet. First, systematically rebuild your context by reading the following files in this exact order:
> 1. `AGENTS.md` (Crucial: Read the entire Long-Run Soak Policy, Setup Commands, Architecture, and Key Concepts).
> 2. `README.md` (For high-level project goals).
> 3. `docs/aether_rs_refactor_recovery_2026_05_04.md` (Crucial for understanding the overall Rust backend transition and its current state).
> 4. `docs/aether_scaling_recovery_2026_05_06.md` (To understand the exact state of the Scaling Sprint, the SQLite/GIL ceilings, and the clean git commit `f8787e2b54c8`).
> 5. `packages/aether/services/promotion_engine.py` and `packages/aether/crdt/document_store.py` (To see the latest `get_state_hash` optimizations).
> 
> Once you have read these files, reply with a brief, 3-bullet-point summary confirming your understanding of the current architectural limits (Python GIL vs CRDTs, SQLite write-locks) and state that you are ready to execute the next objective."

### Prompt for GitHub Copilot CLI - Adversarial Review Agent
> **Copy and paste this to Copilot CLI:**
> "System crash recovery initiated. You are GitHub Copilot, operating as the Super-Tight Adversarial Review Agent for Project Aether. 
> 
> **Your Role:** You are NOT the primary feature developer. Your explicit role is to ruthlessly review, verify, and attack the proposals made by Antigravity (the Lead Architect). You must look for race conditions, GIL deadlocks, database lock contention, and verify test coverage.
> 
> **Immediate Directive:** Rebuild your context by reading:
> 1. `AGENTS.md` (Focus on the Architecture and CRDT implementations).
> 2. `docs/aether_rs_refactor_recovery_2026_05_04.md` (To understand the historical context of the Rust migration).
> 3. `docs/aether_scaling_recovery_2026_05_06.md` (Understand the exact boundaries we hit: Python GIL CRDT starvation and SQLite write-lock saturation).
> 
> Do not generate any new features. Acknowledge your role as the Adversarial Review Agent, confirm you understand the structural limits we are currently facing, and state that you are holding the line and waiting to review Antigravity's next proposal."

---

## 3. Next Steps (Continuation)

To scale throughput beyond 40 proposals/sec sustained, the team must execute the following roadmap:

### Phase 1: Break the SQLite Lock (Database Migration)
*   **Objective**: Migrate from SQLite to a multi-writer PostgreSQL backend.
*   **Why**: To prevent `aether-rs` from blocking on `aether_proposals` inserts while Python commits heavy snapshot states.

### Phase 2: Break the Python GIL (Rust CRDTs)
*   **Objective**: Port the O(N) `apply_diff` CRDT logic out of Python and directly into compiled Rust.
*   **Why**: To eliminate Python's GIL serialization, allowing true multi-core processing of conflict resolution and semantic evaluation.

---

## 4. Vital Artifacts
*   **Final Benchmark JSON**: `~/.aether/runtime_artifacts/phase1/swarm-safe-synthetic_benchmark-20260506-081714.json`
*   **Benchmark Command**:
    ```bash
    AETHER_ENABLE_MOCK_GATE_FAST_PATH=1 AETHER_SWARM_RUN_CLASS=synthetic_benchmark ./scripts/run_swarm_harness_safe.sh --run-seconds 300 --submit-worker-threads 32 --target-mode external --external-api-url http://127.0.0.1:8100 --promotion-batch 120
    ```
