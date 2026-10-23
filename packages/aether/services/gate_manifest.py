"""
gate_manifest.py — Test-manifest loader for the Tripartite Verifier Gate (Track 2).

Schema version: 1
Manifest location: <repo_root>/.aether/test_manifest.json

Selection rules (authoritative):
  1. Manifest present and parses cleanly → manifest is the authority.
  2. Manifest absent                     → deterministic file-based fallback.
  3. Manifest present but malformed      → fail closed (do NOT fall back).
  4. File-based fallback resolves zero   → fail closed.

Always sets ManifestLoadResult.selection_source to one of:
  "manifest"      — manifest authority used
  "file_fallback" — manifest absent, fallback used
  "fail_closed"   — manifest malformed OR fallback resolved zero targets

Council guardrails honoured:
  Guardrail 1: No external tree-sitter CLI binary. This module uses stdlib only.
  Guardrail 2: Empty fallback → fail_closed (not ceremonial pass).
  Guardrail 3: No retry/requeue logic. Timeout semantics are out of scope.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
import structlog

logger = structlog.get_logger()

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_PATH = ".aether/test_manifest.json"


@dataclass
class ManifestEntry:
    """A single path-matcher → test/lint/typecheck mapping entry."""
    path_matcher: str
    test_targets: list[str]
    lint_targets: list[str] = field(default_factory=list)       # empty → use changed files
    typecheck_targets: list[str] = field(default_factory=list)  # empty → use changed files


@dataclass
class ManifestLoadResult:
    """
    Result of load_gate_manifest().

    selection_source values:
      "manifest"      — manifest present and authoritative
      "file_fallback" — manifest absent, file-based fallback used
      "fail_closed"   — malformed manifest OR empty fallback; gate must FAIL immediately
    """
    entries: list[ManifestEntry]
    selection_source: Literal["manifest", "file_fallback", "fail_closed"]
    test_targets: list[str]
    lint_targets: list[str]
    typecheck_targets: list[str]
    error: Optional[str] = None  # set when selection_source == "fail_closed"


def _fail_closed(error: str) -> ManifestLoadResult:
    return ManifestLoadResult(
        entries=[],
        selection_source="fail_closed",
        test_targets=[],
        lint_targets=[],
        typecheck_targets=[],
        error=error,
    )


def _parse_manifest(manifest_path: Path) -> tuple[list[ManifestEntry], Optional[str]]:
    """
    Parse and validate the manifest JSON.

    Returns (entries, error_message). If error_message is set, entries is [].
    """
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"manifest_read_error: {exc}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"manifest_json_invalid: {exc}"

    if not isinstance(data, dict):
        return [], "manifest_root_not_object"

    version = data.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        return [], f"manifest_unknown_schema_version: {version!r} (expected {MANIFEST_SCHEMA_VERSION})"

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return [], "manifest_entries_not_array"

    entries: list[ManifestEntry] = []
    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            return [], f"manifest_entry[{i}]_not_object"

        path_matcher = entry.get("path_matcher")
        if not isinstance(path_matcher, str) or not path_matcher:
            return [], f"manifest_entry[{i}].path_matcher_missing_or_invalid"

        test_targets = entry.get("test_targets")
        if not isinstance(test_targets, list) or not test_targets:
            return [], f"manifest_entry[{i}].test_targets_missing_or_empty"
        if not all(isinstance(t, str) and t for t in test_targets):
            return [], f"manifest_entry[{i}].test_targets_contains_non_string"

        lint_targets = entry.get("lint_targets", [])
        if not isinstance(lint_targets, list):
            return [], f"manifest_entry[{i}].lint_targets_not_array"
        if not all(isinstance(t, str) for t in lint_targets):
            return [], f"manifest_entry[{i}].lint_targets_contains_non_string"

        typecheck_targets = entry.get("typecheck_targets", [])
        if not isinstance(typecheck_targets, list):
            return [], f"manifest_entry[{i}].typecheck_targets_not_array"
        if not all(isinstance(t, str) for t in typecheck_targets):
            return [], f"manifest_entry[{i}].typecheck_targets_contains_non_string"

        entries.append(ManifestEntry(
            path_matcher=path_matcher,
            test_targets=list(test_targets),
            lint_targets=list(lint_targets),
            typecheck_targets=list(typecheck_targets),
        ))

    return entries, None


def _match_entries(entries: list[ManifestEntry], changed_paths: list[str]) -> list[ManifestEntry]:
    """Return all entries whose path_matcher matches at least one changed path."""
    matched: list[ManifestEntry] = []
    for entry in entries:
        for path in changed_paths:
            if fnmatch.fnmatch(path, entry.path_matcher) or fnmatch.fnmatch(
                path, f"*/{entry.path_matcher}"
            ):
                matched.append(entry)
                break
            # Also match if the entry matcher is a prefix (exact path == matcher)
            if path == entry.path_matcher:
                matched.append(entry)
                break
    return matched


def _file_based_fallback(changed_paths: list[str], repo_root: Path) -> list[str]:
    """
    Deterministic file-based fallback: find test files under tests/ whose
    name contains any stem of a changed source file.

    Strategy:
      For each changed path like "packages/aether/services/foo.py":
        - stem = "foo"
        - Look for tests/**/*foo*.py under repo_root/tests/
      Union all matches, sorted for determinism.
    """
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        return []

    candidates: set[str] = set()
    for changed in changed_paths:
        stem = Path(changed).stem
        if not stem:
            continue
        for test_file in tests_root.rglob(f"*{stem}*.py"):
            # Return paths relative to repo_root
            try:
                rel = str(test_file.relative_to(repo_root))
                candidates.add(rel)
            except ValueError:
                candidates.add(str(test_file))

    return sorted(candidates)


def load_gate_manifest(
    repo_root: Path,
    changed_paths: list[str],
) -> ManifestLoadResult:
    """
    Load .aether/test_manifest.json and resolve gate targets for changed paths.

    Applies the four selection rules and returns ManifestLoadResult with
    selection_source set to "manifest", "file_fallback", or "fail_closed".
    """
    manifest_path = repo_root / MANIFEST_PATH

    # Rule 2: manifest absent → file-based fallback
    if not manifest_path.exists():
        logger.warning(
            "gate_manifest_absent",
            manifest_path=str(manifest_path),
            fallback="file_based",
        )
        fallback_tests = _file_based_fallback(changed_paths, repo_root)

        # Guardrail 2: empty fallback fails closed
        if not fallback_tests:
            return _fail_closed("file_fallback_resolved_zero_targets")

        # Fallback lint/typecheck default to changed files
        return ManifestLoadResult(
            entries=[],
            selection_source="file_fallback",
            test_targets=fallback_tests,
            lint_targets=sorted(set(changed_paths)),
            typecheck_targets=sorted(set(changed_paths)),
        )

    # Rule 3: manifest present but malformed → fail closed
    entries, parse_error = _parse_manifest(manifest_path)
    if parse_error is not None:
        logger.error(
            "gate_manifest_malformed",
            manifest_path=str(manifest_path),
            error=parse_error,
        )
        return _fail_closed(f"manifest_malformed: {parse_error}")

    # Rule 1: manifest present and valid → authority
    matched = _match_entries(entries, changed_paths)

    # Union all test_targets from matched entries
    test_targets: set[str] = set()
    lint_targets: set[str] = set()
    typecheck_targets: set[str] = set()

    for entry in matched:
        test_targets.update(entry.test_targets)
        if entry.lint_targets:
            lint_targets.update(entry.lint_targets)
        else:
            # Default: use the changed files that matched this entry
            for path in changed_paths:
                if fnmatch.fnmatch(path, entry.path_matcher) or path == entry.path_matcher:
                    lint_targets.add(path)
        if entry.typecheck_targets:
            typecheck_targets.update(entry.typecheck_targets)
        else:
            for path in changed_paths:
                if fnmatch.fnmatch(path, entry.path_matcher) or path == entry.path_matcher:
                    typecheck_targets.add(path)

    # If manifest matched nothing, fall back to changed-files for lint/typecheck
    # but test_targets remains empty → fail closed (no tests to run is not a pass)
    if not test_targets:
        # Manifest is valid but no entry matched the changed paths → fail closed
        return _fail_closed(
            f"manifest_no_entries_matched_changed_paths: {sorted(changed_paths)}"
        )

    return ManifestLoadResult(
        entries=matched,
        selection_source="manifest",
        test_targets=sorted(test_targets),
        lint_targets=sorted(lint_targets) or sorted(set(changed_paths)),
        typecheck_targets=sorted(typecheck_targets) or sorted(set(changed_paths)),
    )
