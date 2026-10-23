# Phase 4 Reconnaissance Findings & Go/No-Go Recommendation (Revised)

**Prepared by:** Antigravity (IDE Architect)  
**Date:** May 25, 2026  
**Context:** Gate 0A (Phase 4 Reconnaissance) evaluation for Token-Level RGA CRDT feasibility and risk assessment.
**Status:** Completed (Reconnaissance Spike Completed, Strategic Optionality Maintained).

---

## 1. Executive Summary & Strategic Position

We have completed the **Gate 0A Phase 4 Reconnaissance Spike**. We researched Atomic VCS's Trunk-Branch-Leaf CRDT model, audited the Python symbol-mapping logic (`symbol_adapters.py`), extended the Rust backend's parsing footholds (`structured_doc.rs`), and added initial unit tests on token-level RGA convergence and stability under simple spacing shifts.

### Strategic Recommendation: **De-risking Ongoing (Strategic Optionality Maintained)**
While our initial Rust prototyping shows that the RGA CRDT structure is polymorphic and can store token strings, the overall strategic conclusion is that **Phase 4 is not ready to become the priority build lane**. Significant unresolved research risks remain.

Rather than committing prematurely to a full-scale Phase 4 implementation, we recommend **maintaining both Phase 3 and Phase 4 as open strategic options**. We should use our newly verified unit test prototypes as conceptual de-risking spikes while keeping the main Aether control plane cleanly aligned with the current production-hardened advisory-lock architecture.

---

## 2. Prototyping Outcomes & Limitations

### A. Extended Rust RGA Helper
*   **Prototype**: Added `RGADocument::replace_tokens` inside [rga.rs](aether-rs/crates/crdt/src/rga.rs) to allow inserting a flat slice of string tokens.
*   **Parsing**: Added `tokenize_source` inside [structured_doc.rs](aether-rs/crates/crdt/src/structured_doc.rs). It walks AST leaf nodes, interleaving spacing gaps as separate tokens.
*   **Verification Status**: Unit tests successfully verified that syntactically valid code blocks round-trip to original strings under quiet conditions.
*   **Critical Limitation**: This remains an isolated helper utility. It has **no production integration** with Aether's actual promotion server, API endpoints, or database structures.

### B. Spacing Drift Prototyping
*   **Prototype**: Added `test_token_mapping_stability_under_shifts` to check token boundaries under simple statement formatting additions.
*   **Critical Limitation**: This test only checks that the tail text of an unrelated line remains identical under a single whitespace edit. It does **not** prove stable token identities, robust long-term anchoring, or safety during complex, multi-agent structural refactors.

### C. Concurrency Merge Testing
*   **Prototype**: Added `test_token_rga_merge_concurrency` to assert that two replica RGA documents containing tokens converge under concurrent edits.
*   **Critical Limitation**: The test covers only a simple, non-conflicting concurrent insertion of comments. It does **not** address real-world collaborative conflicts, overlapping token updates, or complex edits.

---

## 3. Unresolved Strategic Gaps & Research Risks

Before either track can be considered for full production development, the following strategic research risks must be addressed:

1.  **Tokenizer/Parser Version Drift**: If different agents utilize different versions of a parser grammar (e.g. Tree-sitter updates), they may parse identical files into differing token boundaries, causing RGA desynchronization. While WebAssembly (WASM) parser packaging is a conceptual solution, we have not built or validated any WASM-based parser execution harness.
2.  **Syntax Error AST Convergence**: When an agent writes half-complete code, compile-time syntax is broken, causing Tree-sitter AST parsing to fail. Our prototype falls back to a whitespace-preserving lexical scanner (`split_inclusive`), but we have **no** test evidence proving that concurrent edits merge safely or preserve code correctness while in this fallback state.
3.  **Inside-Token Splitting (Coordinate Translation)**: We have not implemented or tested split-token boundaries (i.e. modifying part of a token, requiring a delete-and-split sequence in RGA). The RGA diff coordinate translation for active splits remains entirely unproven.
4.  **Language Portability**: The prototype is strictly limited to Python. We have no empirical data regarding Tree-sitter grammar stability, performance, or convergence behaviors in non-Python environments (e.g. JS/TS, Rust, Go).
5.  **symbol_adapters.py Limitations**: `symbol_adapters.py` remains a best-effort, regex-backed symbol boundary locator. It is not designed to serve as a token-level CRDT and does not prove parser-drift immunity or token stability.

---

## 4. Strategic Optionality Framework

To maintain a disciplined engineering posture, we revise our Gate 1 decision paths:

*   **Option A (Hard-Focus on Advisory-Locks)**: The Sprint 4B sweep proved that advisory-locking handles extreme contention with exactly 0.0% conflicts. Defer both Phase 3 and Phase 4 indefinitely, focusing entirely on production-grade hardening.
*   **Option B (Phase 3 Controlled Spike)**: Use Phase 3 (causal head frontiers serialized as JSON arrays) as a simpler, known-solvable intermediate metadata lane, keeping its integration isolated behind database mocks.
*   **Option C (Phase 4 Feasibility)**: Maintain Phase 4 strictly as an open research spike, using our Rust tree-sitter prototype to run offline de-risking experiments without affecting the production server.
