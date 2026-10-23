# Release Notes

## Regression Verification

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
