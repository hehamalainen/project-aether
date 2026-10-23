"""Tests for gate_manifest.py."""

import json
from pathlib import Path
import pytest

from packages.aether.services.gate_manifest import (
    load_gate_manifest,
    _file_based_fallback,
    ManifestEntry,
)


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Fixture creating a temporary repository layout."""
    (tmp_path / "packages/aether/services").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/aether").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aether").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_manifest_absent_returns_file_fallback(temp_repo: Path):
    # Setup test file under tests/ to match stem of source file
    test_file = temp_repo / "tests/aether/test_promotion_engine.py"
    test_file.write_text("# dummy test", encoding="utf-8")

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "file_fallback"
    assert res.test_targets == ["tests/aether/test_promotion_engine.py"]
    assert res.lint_targets == ["packages/aether/services/promotion_engine.py"]
    assert res.typecheck_targets == ["packages/aether/services/promotion_engine.py"]
    assert res.error is None


def test_manifest_absent_and_fallback_resolves_zero_fails_closed(temp_repo: Path):
    # No test files exist, manifest is absent
    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "fail_closed"
    assert "file_fallback_resolved_zero_targets" in res.error
    assert not res.test_targets


def test_manifest_malformed_json_fails_closed(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    manifest.write_text("invalid json {", encoding="utf-8")

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "fail_closed"
    assert "manifest_malformed" in res.error
    assert "json" in res.error


def test_manifest_unknown_schema_version_fails_closed(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 99, "entries": []}), encoding="utf-8"
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "fail_closed"
    assert "manifest_unknown_schema_version" in res.error


def test_manifest_missing_required_field_fails_closed(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    # missing test_targets in entry
    entry = {"path_matcher": "packages/aether/services/*.py"}
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": [entry]}), encoding="utf-8"
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "fail_closed"
    assert "test_targets_missing_or_empty" in res.error


def test_manifest_loads_and_matches_changed_path(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    entry = {
        "path_matcher": "packages/aether/services/promotion_engine.py",
        "test_targets": ["tests/aether/test_promotion_engine.py"],
    }
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": [entry]}), encoding="utf-8"
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "manifest"
    assert res.test_targets == ["tests/aether/test_promotion_engine.py"]
    assert res.lint_targets == ["packages/aether/services/promotion_engine.py"]


def test_manifest_glob_matcher_matches_subdirectory(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    entry = {
        "path_matcher": "packages/aether/services/*.py",
        "test_targets": ["tests/aether/test_services.py"],
    }
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": [entry]}), encoding="utf-8"
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "manifest"
    assert res.test_targets == ["tests/aether/test_services.py"]


def test_manifest_multiple_entries_unions_test_targets(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    entry1 = {
        "path_matcher": "packages/aether/services/promotion_engine.py",
        "test_targets": ["tests/aether/test_promotion_engine.py"],
    }
    entry2 = {
        "path_matcher": "*.py",
        "test_targets": ["tests/aether/test_generic.py"],
    }
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": [entry1, entry2]}),
        encoding="utf-8",
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "manifest"
    assert res.test_targets == [
        "tests/aether/test_generic.py",
        "tests/aether/test_promotion_engine.py",
    ]


def test_manifest_valid_but_no_match_fails_closed(temp_repo: Path):
    manifest = temp_repo / ".aether/test_manifest.json"
    entry = {
        "path_matcher": "packages/aether/other/*.py",
        "test_targets": ["tests/aether/test_other.py"],
    }
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": [entry]}), encoding="utf-8"
    )

    res = load_gate_manifest(temp_repo, ["packages/aether/services/promotion_engine.py"])
    assert res.selection_source == "fail_closed"
    assert "manifest_no_entries_matched_changed_paths" in res.error
