"""CommandRunner integration tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

from packages.idasdlc_core.models.entities import Task
from packages.idasdlc_core.services.runners import CommandRunner


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def test_command_runner_produces_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    runner = CommandRunner(commands=["echo change >> file.txt"], validators=["tests"])
    task = Task(repo="repo", prompt="update", base_sha="HEAD", tags=[], validators_required=[])

    result = runner.execute(task, repo)

    assert result.validator_names == ["tests"]
    assert result.metrics["tool_calls"] == 1
    assert result.metrics["diff_size"] > 0
    assert result.diffs and "change" in result.diffs[0]