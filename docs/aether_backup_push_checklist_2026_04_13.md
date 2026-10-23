# Aether Backup and Push Checklist

## Objective

Secure the current Aether code and documentation to the private GitHub repository before further cleanup or restructuring.

## Phase 1 - Backup first

- Create a fresh backup branch from the current working state.
- Stage current code, tests, and active documentation.
- Exclude local runtime artifacts and unrelated scratch material.
- Commit a single snapshot commit for remote safety.
- Push the backup branch to `origin`.
- Verify the branch exists remotely and can be used as a recovery point.

## Phase 2 - Commit structure cleanup

- Split the working state into a small reviewable stack:
  - runtime and intake hardening
  - soak policy and harness execution
  - tests and regressions
  - readiness and strategy docs
- Keep runtime fixes and strategic docs out of the same commit.
- Keep backup branch history intact even if a cleaner branch is created afterward.

## Phase 3 - Documentation package

- Keep these as first-class documents:
  - readiness package
  - future VCS alignment memo
  - deep research operationalization memo
  - strategic thesis / roadmap
- Add one artifact index that lists:
  - 8h clean pass
  - 10h near-pass
  - 12h threshold pass with surfaced bug
  - 24h endurance-complete throughput miss
- Add one claims matrix:
  - safe claims now
  - unresolved gaps
  - future claims only

## Phase 4 - Verification

- Confirm the backup branch is visible on GitHub.
- Confirm the backup commit includes the latest runtime and doc work.
- Confirm the repo still has a clean recovery path after cleanup work starts.
- Only after backup verification, proceed to stress characterization or the next canonical soak.
