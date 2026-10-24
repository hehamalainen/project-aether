# Aether Public Release Security Review

## Scope and reviewed revision

This is a narrow follow-up review of the three previously reported release blockers: proposal-controlled filesystem paths, CLI snapshot archive extraction, and SQLite promotion atomicity. It is not a production security certification or a review of every code path.

- Branch: `main`
- Reviewed HEAD: `db6eedc870d403719b871e90b3eb6bb82e8d4db3`
- Reviewed tree: `728b5f0f19933769e0058657adf8f5341969c530`

## Findings

No confirmed exploitable instance of the three previously reported blockers was established in the reviewed revision.

The Python path-safety helpers reject traversal, absolute paths, symlinks, and reserved `.git` aliases, and snapshot materialization rechecks containment. Rust validates proposal paths at intake and evaluation boundaries and validates containment again during materialization. The CLI export path uses a custom archive extractor that rejects traversal and link/special-file entries rather than relying on `tarfile.extractall`. SQLite promotion persistence uses flushes and deferred subordinate commits, with rollback on pre-commit failure and success events published after commit.

## Verification notes

1. `tests/aether/test_release_security_and_atomicity.py` now validates the AIP v0 path-safety guard against a positive control payload (`schema: "aip.v0"`, `op: "update_node"`, `node_id: "func:x"`, `content: ...`) that succeeds (`200 OK`) when `path="safe.py"` and fails specifically in `validate_repo_relative_path` (`400 Bad Request`, `"Path traversal segment is not allowed"`) when `path="../outside_victim.py"`.
2. `TestSQLitePromotionAtomicity` in `tests/aether/test_release_security_and_atomicity.py` now injects failure both via SQLite triggers on `aether_agent_metrics` (after `Promotion`, `AetherHead`, `OperationGraphNode`, `ProposalStatus.PROMOTED`, `ConflictSet.resolved`, and `ProposalStatus.SUPERSEDED` are flushed) and by directly raising at the final `session.commit()` boundary (`phase == "trust_commit"`), verifying that proposal statuses (`EVALUATING` / `PENDING`), conflict sets, head pointers, and metrics all roll back cleanly with zero promotion/conflict/head events emitted.
3. Python 3.11 runtime behavior and native Windows filesystem behavior were not executed locally on this macOS host; CI executes on Ubuntu and local verification executed on macOS (Python 3.14, Rust 1.94.1).

## Verdict

**No blocker remains established for the three reviewed findings, and the two follow-up regression test gaps have been closed.**
