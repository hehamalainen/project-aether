# TECHNICAL MEMO: Atomic VCS & Token-Level CRDT Architecture (Revised)

**Prepared by:** Antigravity (IDE Architect)  
**Date:** May 25, 2026  
**Context:** Conceptual analysis of Atomic VCS (atomic.dev) token-level CRDT and AST-level merging compared to local Project Aether's current architecture.
**Status:** Conceptual Background Reference (Non-Decision-Grade / Analytical Memo).

---

## 1. Executive Summary

This memo provides an analytical review of **Atomic VCS**'s conceptual "Trunk-Branch-Leaf" token-level CRDT architecture. This is a conceptual reference designed to guide future design, **not** promotion-ready proof or decision-grade evidence. 

The primary value of this memo is to understand how token-level RGA CRDTs operate in theory, and explicitly map Aether's current implementation limits against these conceptual end-states.

---

## 2. Tokenization & Boundary Mapping: Conceptual Inferences

Based on Atomic VCS's published document models, we infer the following conceptual mechanics:

### A. Hierarchical Token-Level RGA (Conceptual)
*   **Structure**: Atomic VCS models code as **Trunk (Files) → Branch (Lines) → Leaf (Tokens)**. Instead of tracking individual characters (as in standard character-level CRDTs like Yjs), the RGA sequence elements are discrete lexical tokens (e.g. keywords, identifiers, operators).
*   **Expected Benefits**: Reduces metadata overhead compared to character-level CRDTs, and conceptually allows non-conflicting concurrent edits on different tokens within the same line.
*   **Aether Current Limits**: Aether **does not** implement a hierarchical token-level RGA in production. Aether's active head is represented as a linear sequence of heads and promotions. Our recent Rust prototype (`replace_tokens`) is a local, isolated helper module and has **no** integration with the database, promotion engine, or API endpoints.

### B. Character-to-Token Diff Mapping (Conceptual)
*   **Protocol**: Translating raw character edits (from editors or LLMs) to token boundaries requires coordinate translation by walking visible RGA elements and accumulating token lengths. 
*   **Splitting Nodes**: If an edit occurs *inside* a token, the token must be split. This conceptually involves creating a deletion (tombstone) operation for the old token and inserting new sub-token leaves linked sequentially in the RGA.
*   **Aether Current Limits**: Aether's prototype **does not** implement or test split-token boundaries (Case B inside-token splitting) or coordinate diff translations, meaning this mapping protocol remains entirely unproven and hypothetical.

---

## 3. Tree-Sitter & AST Hashing: Conceptual Inferences

*   **Dual-Layer Architecture**: A Graph Layer (representing content-addressed AST nodes like classes and methods) mapped by Tree-sitter to a Semantic Layer (hierarchical CRDT).
*   **Incremental Re-parsing**: Conceptual use of incremental Tree-sitter parsers to re-process only edited AST blocks.
*   **AST Hashing**: Hashing functions and classes based on structural AST shape rather than text layout, allowing history tracking across file moves or renames.
*   **Aether Current Limits**: Aether's production database maintains standard, linear metadata records. Our Rust structured document prototype (`StructuredDocument`) decomposes Python files using `tree-sitter-python` into static logic nodes, but Aether **does not** maintain a global knowledge graph, content-addressed AST hashing, or queryable history.

---

## 4. Parser Version Drift & Syntax Fallbacks: Conceptual Inferences

*   **Syntax Error Fallback**: If an agent writes syntactically invalid code, AST parsing fails. Conceptual VCS designs fallback to lexical scanners to maintain convergence of raw bytes while marked as "stale" AST nodes.
*   **Tokenizer Drift**: OS, library, or compiler version changes can cause different clients to tokenize the identical file differently, resulting in RGA desynchronization. Conceptual mitigations include WebAssembly (WASM) parser pinning and migration re-tokenization patches.
*   **Aether Current Limits**: Aether's Rust structured doc prototype implements a basic `split_inclusive` fallback when syntax errors are encountered, but we have **no** empirical test data proving convergence safety under active concurrent edits while in a fallback state. Aether does **not** package WASM parsers, strictly pin compiler versions, or implement re-tokenization migration patches.

---

## 5. Industrial & Academic Citations

1.  **CoAST (Conflict-free Replicated Abstract Syntax Tree):**  
    *   *Source:* Published in *ICSOFT 2022*. Explores how operation-based CRDTs with logical clocks can modify AST structures collaboratively while preserving syntactic correctness.
2.  **Atomic VCS Repository & Documentation:**  
    *   *Reference:* [github.com/atomicdotdev/atomic](https://github.com/atomicdotdev/atomic). Outlines high-level concepts of trunk-branch-leaf structures and validation engines.
3.  **Aura VCS (AST Versioning Engine):**  
    *   *Reference:* [auravcs.com](https://auravcs.com). A git-native AST hashing tool that rejects real-time CRDTs in favor of AST logic-node hashing and intent verification.
