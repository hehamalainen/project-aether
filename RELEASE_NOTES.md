# Historical Predecessor Release Notes (`Evolution-Coding` / `IDASDLC` — Early Prototype)

> [!WARNING]
> **Historical Predecessor-Project Document (Not Project Aether):** These test notes record a 14-test regression run from the earlier `Evolution-Coding` / `IDASDLC` prototype prior to the creation of Project Aether. For Project Aether's current verification commands and test suites (`python -m pytest tests/aether/ -v` and `cargo test --workspace`), see [`README.md`](README.md) and [`docs/aether_claims_matrix_2026_04_13.md`](docs/aether_claims_matrix_2026_04_13.md).

## Historical Regression Verification (Predecessor Workspace)

### Targeted Suites
- Command: `PYTHONPATH=/workspaces/Evolution-Coding:/workspaces/Evolution-Coding/packages:/workspaces/Evolution-Coding/apps .venv/bin/python -m pytest tests/test_cli_run_detail.py tests/test_backend_runs_api.py -q`
```
.....                                                                                         [100%]
5 passed in 1.00s
```

### Full Test Suite
- Command: `PYTHONPATH=/workspaces/Evolution-Coding:/workspaces/Evolution-Coding/packages:/workspaces/Evolution-Coding/apps .venv/bin/python -m pytest -q`
```
..............                                                                                [100%]
14 passed in 1.60s
```

## Static Analysis

### Lint (Ruff)
- Command: `.venv/bin/ruff check`
```
All checks passed!
```

### Typing (mypy)
- Command: `PYTHONPATH=/workspaces/Evolution-Coding:/workspaces/Evolution-Coding/packages:/workspaces/Evolution-Coding/apps .venv/bin/mypy --explicit-package-bases apps packages`
```
Success: no issues found in 43 source files
```
