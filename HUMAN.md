# Project Aether: Early Session Deep-Dive (Historical Phase 1–2 Note — January 27, 2026)

> [!NOTE]
> **Historical Session Note (January 27, 2026):** This file is an informal engineering session summary from the initial Phase 1–2 Python prototype build. For the current research-prototype capability matrix, security boundaries, and verified evidence, see [`README.md`](README.md) and [`docs/aether_claims_matrix_2026_04_13.md`](docs/aether_claims_matrix_2026_04_13.md).

## 🎤 The Big Idea
Today we laid the foundation for **Aether**, an experimental **Agent-Native Version Control System** research prototype.

Conventional Git was built for humans who move slowly and communicate in PRs. Aether explores a different workflow: where multiple AI agents propose concurrent edits to the same repository and coordinate through CRDT state and gated promotions.

---

## 🏗️ Technical Pillar 1: Semantic Convergence (Phase 2 Python Prototype)
The primary technical milestone in this session was **AST-Level Structured Merges** for Python files (`packages/aether/crdt/structured_doc.py`).
- **The Problem**: If two agents edit different functions in the same Python file, line-based diffing often flags a conflict when line numbers shift.
- **The Prototype Approach**: We integrated `libcst` to decompose Python source files into top-level semantic nodes (`FUNC:name`, `CLASS:name`).
- **Observed Scope**: In our test cases for disjoint top-level function and method edits in valid `.py` files (`tests/aether/test_structured_doc.py`, `qualification/semantic_suite/run.py`), `StructuredDocument` merges disjoint AST nodes without line-drift conflicts, while overlapping edits to the same node or unsupported syntax fall back to character-level RGA or conflict reporting.

## 🛡️ Technical Pillar 2: The Trust Ecosystem
We implemented a **Bayesian Agent Trust Calibration** model (`AgentTrust` in `packages/aether/models/entities.py`) in the Python prototype.
- **Bayesian Beta Model**: Uses an Alpha/Beta distribution to track agent promotion outcomes (`alpha` incremented on promotion, `beta` incremented on gate failure).
- **Logit-Combined Scoring**: Combines proposal confidence with the agent's historical trust score in logit space during conflict-set ranking in the Python `PromotionEngine`.
- **Failure Taxonomy**: Distinguishes between gate failures (`REJECTED`) and losing a conflict race to a higher-ranked proposal (`SUPERSEDED`).

## 🚀 Technical Pillar 3: Early In-Process Concurrency Micro-Test
We ran an initial in-process synthetic test (`tests/aether/test_swarm_scale.py`) with **100 simulated agents**:
- **Observed In-Process Micro-Test Stats (January 27, 2026)**:
  - **Throughput**: ~`32.3` proposals per second in the in-process synthetic test loop.
  - **Latency**: ~`3.2ms` (P50) for in-memory CRDT operations.
- **Scope Note**: These numbers came from an early in-process Python micro-test without external subprocess gates or multi-hour HTTP route backpressure. For full HTTP/SQLite/PostgreSQL qualification and soak measurements (including where 1,000-agent and high-contention runs hit throttling and lock limits), see [`README.md`](README.md) and [`docs/aether_claims_matrix_2026_04_13.md`](docs/aether_claims_matrix_2026_04_13.md).

## 🖥️ The Interface: From Mock to Live
We connected an experimental dashboard (`apps/aether_ui`):
- **The Dashboard**: A Next.js interface for inspecting repositories, proposals, promotions, and queue physics telemetry from the Python FastAPI backend (`apps/aether_server`).

---

## 🛠️ The Session Timeline
- **Step 1**: Minimal UI framework (Dashboard, Gates, Proposals).
- **Step 2**: Git Lifecycle (`git-import` and `git-export` helpers in Python).
- **Step 3**: Initial 100-agent in-process CRDT test (`tests/aether/test_swarm_scale.py`).
- **Step 4**: Adding Python AST semantic merges (`libcst`).
- **Step 5**: Adding the Bayesian Trust & Calibration scoring loop in Python.

---

## 🧩 Key Terminology
- **Agent-Native VCS**: A version-control prototype designed around machine-submitted proposals and automated gate evaluation.
- **Semantic Node Tracking**: Using Python AST node keys (`FUNC:*`, `CLASS:*`) in `StructuredDocument` to merge disjoint top-level edits.
- **Bayesian Trust**: An Alpha/Beta reliability prior used by the Python `PromotionEngine`.
- **Neutral Loss (`SUPERSEDED`)**: When a proposal in a conflict set is not selected because a competing proposal was promoted first.

---
*Historical Session Note — January 27, 2026*
