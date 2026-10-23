"""Tests for the Tripartite Verifier Gate (Track 2)."""

import json
from pathlib import Path
import pytest
import sys

from packages.aether.models.entities import (
    Repository,
    Snapshot,
    GateStatus,
    GateConfig,
)
from packages.aether.services.event_bus import EventBus
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.gate_runner import GateRunner, TripartiteGateLayer


@pytest.fixture
def db_session():
    """Create in-memory database session."""
    from sqlmodel import SQLModel, Session, create_engine
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def event_bus():
    """Create event bus."""
    return EventBus()


@pytest.fixture
def gate_runner(db_session, event_bus):
    """Create gate runner with trusted custom command execution enabled for tripartite checks."""
    return GateRunner(db_session, event_bus, allow_custom_commands=True)


@pytest.fixture
def repo_and_snapshot(db_session):
    """Create repo and snapshot for testing."""
    # Create repository record
    repo = Repository(id="repo_test", name="test")
    db_session.add(repo)

    # Create snapshot manually for this test
    snapshot = Snapshot(
        id="snap_test",
        repo_id=repo.id,
        head_sequence=0,
        content_hash="test_hash",
        file_count=1,
        total_bytes=14,
    )
    db_session.add(snapshot)
    db_session.commit()

    return repo, snapshot


@pytest.mark.asyncio
async def test_parser_gate_rejects_syntax_error_output(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write a python file with syntax error in tmp_path
    py_file = tmp_path / "foo.py"
    py_file.write_text("def foo(\n  print('missing close parenthesis')", encoding="utf-8")

    # Write dummy test to avoid empty fallback fail closed
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/test_foo.py").write_text("# dummy", encoding="utf-8")

    # Build semantic merge gates
    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["foo.py"], repo_root=tmp_path
    )

    # Parser gate should be the first gate in list
    assert gates[0].name == TripartiteGateLayer.PARSER.value

    # Run parser gate
    result = await gate_runner.run_gate(
        snapshot.id,
        gates[0],
        tmp_path,
    )

    assert result.status == GateStatus.FAIL
    assert result.exit_code != 0
    assert "Parser fail" in result.stderr or "SyntaxError" in result.stderr


@pytest.mark.asyncio
async def test_parser_gate_passes_valid_syntax(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write a valid python file
    py_file = tmp_path / "foo.py"
    py_file.write_text("def foo():\n    pass\n", encoding="utf-8")

    # Write dummy test to avoid empty fallback fail closed
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/test_foo.py").write_text("# dummy", encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["foo.py"], repo_root=tmp_path
    )

    result = await gate_runner.run_gate(
        snapshot.id,
        gates[0],
        tmp_path,
    )

    assert result.status == GateStatus.PASS
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_static_gate_rejects_dangling_reference(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write a python file with type/lint issue
    # Mypy will catch undefined name if typechecking is strict, or ruff will catch it
    py_file = tmp_path / "foo.py"
    py_file.write_text("def foo() -> int:\n    return \"not an int\"\n", encoding="utf-8")

    # Write dummy test to avoid empty fallback fail closed
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/test_foo.py").write_text("# dummy", encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["foo.py"], repo_root=tmp_path
    )

    # Static gate is second
    assert gates[1].name == TripartiteGateLayer.STATIC.value

    result = await gate_runner.run_gate(
        snapshot.id,
        gates[1],
        tmp_path,
    )

    # Mypy should exit non-zero on type mismatch
    assert result.status == GateStatus.FAIL
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_impacted_baseline_gate_uses_manifest_test_targets(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write source and test files
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    src_file = tmp_path / "src/foo.py"
    src_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    test_file = tmp_path / "tests/test_foo.py"
    test_file.write_text("def test_add():\n    from src.foo import add\n    assert add(1, 2) == 3\n", encoding="utf-8")

    # Create manifest
    manifest_dir = tmp_path / ".aether"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_data = {
        "schema_version": 1,
        "entries": [
            {
                "path_matcher": "src/foo.py",
                "test_targets": ["tests/test_foo.py"],
                "lint_targets": ["src/foo.py"],
                "typecheck_targets": ["src/foo.py"]
            }
        ]
    }
    (manifest_dir / "test_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["src/foo.py"], repo_root=tmp_path
    )

    assert manifest_res.selection_source == "manifest"
    assert manifest_res.test_targets == ["tests/test_foo.py"]

    # Run impacted baseline gate
    result = await gate_runner.run_gate(
        snapshot.id,
        gates[2],
        tmp_path,
    )

    assert result.status == GateStatus.PASS
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_impacted_baseline_gate_file_fallback_when_manifest_absent(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # No manifest exists
    # Write source and test files
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    src_file = tmp_path / "src/foo.py"
    src_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    test_file = tmp_path / "tests/test_foo.py"
    test_file.write_text("def test_add():\n    from src.foo import add\n    assert add(1, 2) == 3\n", encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["src/foo.py"], repo_root=tmp_path
    )

    assert manifest_res.selection_source == "file_fallback"
    assert "tests/test_foo.py" in manifest_res.test_targets

    result = await gate_runner.run_gate(
        snapshot.id,
        gates[2],
        tmp_path,
    )

    assert result.status == GateStatus.PASS
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_manifest_malformed_json_fails_closed(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write malformed manifest
    manifest_dir = tmp_path / ".aether"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "test_manifest.json").write_text("malformed {", encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["foo.py"], repo_root=tmp_path
    )

    assert manifest_res.selection_source == "fail_closed"
    assert len(gates) == 1
    assert gates[0].name == "fail_closed"

    result = await gate_runner.run_gate(
        snapshot.id,
        gates[0],
        tmp_path,
    )

    assert result.status == GateStatus.FAIL
    assert "manifest_malformed" in result.stderr


@pytest.mark.asyncio
async def test_gate_sequence_stops_at_first_failure(
    gate_runner, repo_and_snapshot, tmp_path
):
    repo, snapshot = repo_and_snapshot

    # Write files that fail parser gate
    py_file = tmp_path / "foo.py"
    py_file.write_text("def invalid_syntax(\n", encoding="utf-8")

    # Write dummy test to avoid empty fallback fail closed
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/test_foo.py").write_text("# dummy", encoding="utf-8")

    gates, manifest_res = gate_runner.build_semantic_merge_gates(
        changed_paths=["foo.py"], repo_root=tmp_path
    )

    # Run all gates with stop_on_failure=True
    results = await gate_runner.run_all_gates(
        snapshot.id,
        gates,
        tmp_path,
        stop_on_failure=True,
    )

    # Should only run parser gate and fail immediately
    assert len(results) == 1
    assert results[0].gate_name == TripartiteGateLayer.PARSER.value
    assert results[0].status == GateStatus.FAIL
