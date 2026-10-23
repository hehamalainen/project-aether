# Aether CRDT DocumentStore Port Inventory

This document maps the API surface, caller chains, and architectural responsibilities of the `DocumentStore` to prepare for its migration from Python (`packages/aether/crdt/document_store.py`) to Rust (`aether-rs/crates/crdt/src/document_store.rs`).

## 1. Architectural Role
The `DocumentStore` sits between the orchestration layer (`PromotionEngine`) and the low-level data structures (`RGADocument`, `StructuredDocument`).
- **Input**: Unified diffs, AIP mutations, raw file content.
- **State**: In-memory map of `FileDocument`s (path -> RGA/Structured trees).
- **Output**: Materialized file systems (snapshots), conflict sets, state hashes.

## 2. Caller Chains: PromotionEngine → DocumentStore
The `PromotionEngine` invokes `DocumentStore` in the following critical paths:

| PromotionEngine Method | DocumentStore Method Invoked | Purpose |
|------------------------|------------------------------|---------|
| `_apply_proposal_change` | `apply_diff` / `apply_aip_mutations` / `apply_full_file` | Applies a single proposal's intent to the CRDT state. Returns modified paths. |
| `evaluate_proposal` | `clone()` | Creates an isolated fork of the document store for candidate evaluation. |
| `evaluate_proposal` | `get_state_hash()` | Extracts deterministic hash for collision detection after applying changes. |
| `_evaluate_conflict_batch` | `find_overlapping_proposals` | Computes geometric/AST intersections between concurrent proposals. |
| `materialize_candidate` | `materialize_snapshot` | Flushes the CRDT tree back to a flat filesystem directory. |

## 3. Python vs Rust API Mapping

### Low-Level Primitives (Already Ported)
| Python Component | Rust Equivalent | Status |
|------------------|-----------------|--------|
| `RGADocument` | `crdt::rga::RGADocument` | ✅ Fully Ported |
| `RGAOperation` | `crdt::rga::RGAOperation` | ✅ Fully Ported |
| `StructuredDocument` | `crdt::structured_doc::StructuredDocument` | 🚧 Partial (`new`, `set_content`, `get_full_text`) |

### `DocumentStore` Public API (To Be Ported)

| Python Method | Rust Target Signature | Complexity |
|---------------|-----------------------|------------|
| `load_directory(extensions)` | `pub fn load_directory(&mut self, path: &Path)` | Low (FS traversal) |
| `apply_diff(diff_text, prop_id)` | `pub fn apply_diff(&mut self, diff: &str, prop_id: &str) -> Result<Vec<String>>` | High (Diff parsing & RGA mapping) |
| `apply_aip_mutations(...)` | `pub fn apply_aip_mutations(...)` | High (AST matching) |
| `get_state_hash()` | `pub fn get_state_hash(&self) -> String` | Medium (Deterministic serialization) |
| `clone()` | `impl Clone for DocumentStore` | Low (Rust derives) |
| `find_overlapping_proposals()` | `pub fn find_overlapping_proposals(...)` | High (Conflict geometry) |
| `materialize_snapshot(path)` | `pub fn materialize_snapshot(&self, path: &Path)` | Medium (FS writes) |

## 4. `apply_diff` Performance Profile
Based on previous benchmark artifacts (300s synthetic runs):
- **Python**: `apply_diff` is a primary bottleneck. Python's diff parsing (`unidiff` overhead) and geometric mapping to RGA sequences degrades heavily as `O(N * M)` where N=file size, M=concurrent proposals.
- **Rust Opportunity**: Rust's `similar` / `diffy` crates combined with native contiguous array operations will reduce `apply_diff` overhead by 10-50x.
- **Critical Path**: `apply_diff` must parse the unified diff, translate line numbers to RGA element IDs, and dispatch `local_insert`/`local_delete` without panicking on offset drifts.

## 5. Porting Strategy
1. **Model Layer**: Define `FileDocument` and `DocumentStore` structs in `crates/crdt/src/document_store.rs`.
2. **Diff Engine**: Implement `apply_diff` using the `diffy` crate (already in Cargo.toml).
3. **AST Engine**: Complete `StructuredDocument` (Tree-sitter integration exists, needs mutation API).
4. **Integration**: Expose `DocumentStore` to `services::promotion_engine`.
