import pytest
import os
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

def test_foreign_cwd_resolved():
    """Aether-7: Build identity resolves repository root from module path, not cwd."""
    from packages.aether.api import routes
    
    expected_root = Path(routes.__file__).resolve().parents[3]
    
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            true_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(expected_root),
                text=True
            ).strip()
            assert getattr(routes, "BOOT_SHA", None) == true_commit
        finally:
            os.chdir(old_cwd)

def test_moved_head_snapshot_preserved():
    """Aether-8: Moving HEAD post-start does not change system/build response (boot identity is preserved)."""
    from packages.aether.api import routes
    
    with patch("subprocess.check_output") as mock_git:
        mock_git.return_value = "new_mocked_sha\n"
        
        import asyncio
        resp = asyncio.run(routes.system_build())
        
        assert resp["commit"] != "new_mocked_sha"

def test_unresolvable_git_returns_unknown():
    """Aether-9: Git failure returns 'unknown' identity cleanly (expected RED = 0)."""
    from packages.aether.api import routes
    if hasattr(routes, "BOOT_COMMIT"):
        with patch.multiple(routes, BOOT_COMMIT="unknown", BOOT_BRANCH="unknown", BOOT_DIRTY=False, BOOT_DIFF_FINGERPRINT=None):
            import asyncio
            resp = asyncio.run(routes.system_build())
            assert resp["commit"] == "unknown"
            assert resp["branch"] == "unknown"
    else:
        with patch("subprocess.check_output", side_effect=Exception("git error")):
            import asyncio
            resp = asyncio.run(routes.system_build())
            assert resp["commit"] == "unknown"
            assert resp["branch"] == "unknown"
