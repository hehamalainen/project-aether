# Aether Public Release Security Review

## Scope and reviewed revision

This is a narrow follow-up review of the three previously reported release blockers: proposal-controlled filesystem paths, CLI snapshot archive extraction, and SQLite promotion atomicity. It is not a production security certification or a review of every code path.

- Branch: `main`
- Reviewed HEAD: `db6eedc870d403719b871e90b3eb6bb82e8d4db3`
- Reviewed tree: `728b5f0f19933769e0058657adf8f5341969c530`

## Findings

No confirmed exploitable instance of the three previously reported blockers was established in the reviewed revision.

The Python path-safety helpers reject traversal, absolute paths, symlinks, and reserved `.git` aliases, and snapshot materialization rechecks containment. Rust validates proposal paths at intake and evaluation boundaries and validates containment again during materialization. The CLI export path uses a custom archive extractor that rejects traversal and link/special-file entries rather than relying on `tarfile.extractall`. SQLite promotion persistence uses flushes and deferred subordinate commits, with rollback on pre-commit failure and success events published after commit.

## Nonblocking verification notes

1. `tests/aether/test_release_security_and_atomicity.py` contains an AIP path-safety fixture that appears malformed: the operation name and required fields do not form a valid control payload. That case may fail schema validation before reaching path-safety validation.
2. The SQLite atomicity regression checks rollback of head, promotion, and operation-node records and verifies no success events plus retry behavior. It does not inject failure at the final commit or after conflict/trust mutations, and it does not assert proposal status immediately after rollback.
3. Python 3.11 runtime behavior and native Windows filesystem behavior were not executed in this follow-up. The reported local probes covered Python 3.13 and 3.14; those probe results were not independently rerun as part of this report.
4. The full test-count claims supplied in the accompanying work summary were not independently reproduced for this follow-up.

## Verdict

**No blocker remains established for the three reviewed findings.** Approval is limited to the narrow code-inspection scope above; the test gaps and unexecuted platform/runtime checks remain.
