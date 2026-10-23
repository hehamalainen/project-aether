"""Regression tests for public release hygiene, license presence, and artifact exclusion."""

from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]


def _get_tracked_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        text=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_root_license_and_third_party_notices_present() -> None:
    """Release root must contain a valid MIT LICENSE and THIRD_PARTY_NOTICES.md."""
    license_path = REPO_ROOT / "LICENSE"
    notices_path = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    assert license_path.is_file(), "Root LICENSE file is missing"
    assert notices_path.is_file(), "Root THIRD_PARTY_NOTICES.md file is missing"
    license_text = license_path.read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Permission is hereby granted, free of charge" in license_text


def test_no_tracked_runtime_databases_or_scratch_logs() -> None:
    """Tracked git tree must not contain runtime SQLite databases, logs, or scratch debug files."""
    tracked = set(_get_tracked_files())
    forbidden_exact = {
        "idasdlc.db",
        ".idasdlc/logs/idasdlc.log",
        "aether.db",
        "apps/aether_server/aether.db",
        "test_physics.json",
        "scratch/cp_ps.txt",
        "scratch/git_fsck.txt",
        "test.rs",
        "test_diffy.rs",
        "aether-rs/diff_test.rs",
        "patch_request_promotion.py",
    }
    assert forbidden_exact.isdisjoint(tracked), (
        f"Forbidden runtime/scratch artifacts still tracked: {sorted(forbidden_exact & tracked)}"
    )

    for rel_path in tracked:
        assert not rel_path.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")), (
            f"Database artifact should not be tracked in git: {rel_path}"
        )
        if rel_path.endswith(".log"):
            assert rel_path.startswith(("docs/evidence/", "peer-review-artifacts/")), (
                f"Unexpected tracked log file outside evidence directories: {rel_path}"
            )


def test_no_hardcoded_author_home_paths_in_executable_code() -> None:
    """All tracked files (code, docs, evidence, and reports) must be free of local home/iCloud paths."""
    import re

    tracked = _get_tracked_files()
    private_path_re = re.compile(
        r"(\x2fUsers\x2f[A-Za-z0-9_.-]+|com\x7eapple|Mobile(?:\s|%20)Documents|\.gemini\x2fantigravity)",
        re.IGNORECASE,
    )

    offenders: list[str] = []
    for rel_path in tracked:
        try:
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if private_path_re.search(content):
            offenders.append(rel_path)

    assert not offenders, f"Private local or iCloud paths found in tracked files: {offenders}"


def test_pyproject_packaging_metadata_and_imports() -> None:
    """pyproject.toml must declare Python >=3.11 and include all Aether subpackages."""
    import importlib
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.11"
    declared_packages = set(pyproject["tool"]["setuptools"]["packages"])
    required_packages = {
        "packages.aether",
        "packages.aether.adapters",
        "packages.aether.api",
        "packages.aether.crdt",
        "packages.aether.git",
        "packages.aether.models",
        "packages.aether.services",
        "packages.aether.synthesis_worker",
    }
    missing = required_packages - declared_packages
    assert not missing, f"Missing packages in pyproject.toml: {sorted(missing)}"

    for mod_name in (
        "packages.aether.git",
        "packages.aether.adapters",
        "packages.aether.synthesis_worker",
    ):
        mod = importlib.import_module(mod_name)
        assert mod is not None


def test_artifact_index_committed_evidence_files_exist_and_historical_qualified() -> None:
    """Committed evidence files referenced in docs/aether_artifact_index_2026_04_13.md must exist."""
    index_path = REPO_ROOT / "docs" / "aether_artifact_index_2026_04_13.md"
    assert index_path.is_file()
    index_text = index_path.read_text(encoding="utf-8")

    # Historical .pytest-artifacts references must be explicitly qualified as git-ignored/excluded
    assert "historical local soak reports" in index_text.lower()
    assert "excluded from git" in index_text.lower()

    committed_evidence = [
        "examples/reproducible_gate_verification_demo.py",
        "qualification/semantic_suite/report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json",
        "qualification/semantic_suite/report_postgres_v2_024163b8a3ebf951923a8e6899051af2793f1748.json",
        "peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json",
        "peer-review-artifacts/20260304/phase1/phase1-60m-preflight-safe-20260304-133325.heartbeat.log",
        "docs/evidence/gate_provenance_table.md",
        "docs/evidence/phase4/provenance_contract.md",
        "docs/evidence/phase4d/pytest_green.log",
        "docs/aether_stage3_pg_baseline_closeout_2026_05_10.md",
        "docs/sprint4d_topology_baseline.md",
    ]
    for rel_path in committed_evidence:
        assert (REPO_ROOT / rel_path).is_file(), f"Committed evidence file missing: {rel_path}"


def test_git_checkout_is_complete_fsck_clean_and_history_secret_free() -> None:
    """Release checkout and CI workflow must use full history, pass strict git fsck, and have zero secret matches."""
    import re

    ci_workflow = (REPO_ROOT / ".github" / "workflows" / "aether-ci.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in ci_workflow, (
        "aether-ci.yml must configure fetch-depth: 0 so full-history release hygiene checks pass in CI"
    )

    is_shallow = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--is-shallow-repository"],
        text=True,
    ).strip()
    assert is_shallow == "false", "Release repository must not be a shallow clone"

    commit_count = int(
        subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-list", "--count", "--all"],
            text=True,
        ).strip()
    )
    assert commit_count >= 1, "Release repository must have at least one reachable commit"

    fsck = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "fsck", "--full", "--strict"],
        capture_output=True,
        text=True,
        check=False,
    )
    combined_fsck = fsck.stdout + fsck.stderr
    assert fsck.returncode == 0, f"git fsck --full --strict failed:\n{combined_fsck}"
    assert "missing " not in combined_fsck, f"git fsck reported missing objects:\n{combined_fsck}"
    assert "broken link" not in combined_fsck, f"git fsck reported broken links:\n{combined_fsck}"

    # Scan all unique historical blobs across all refs (--all) for credential patterns
    objs = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-list", "--objects", "--all"],
        text=True,
    ).splitlines()
    shas = [line.split(" ", 1)[0] for line in objs if line.strip()]
    path_by_sha = {
        line.split(" ", 1)[0]: line.split(" ", 1)[1]
        for line in objs
        if " " in line
    }

    secret_re = re.compile(
        rb"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}"
        rb"|github_pat_[A-Za-z0-9_]{50,}|sk-[A-Za-z0-9]{32,}|sk-proj-[A-Za-z0-9_-]{32,}"
        rb"|AIza[0-9A-Za-z\-_]{35}|xox[baprs]-[0-9A-Za-z\-]{10,}"
        rb"|-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----)"
    )
    private_blob_re = re.compile(
        rb"(\x2fUsers\x2f[A-Za-z0-9_.-]+|com\x7eapple|Mobile(?:\s|%20)Documents|\.gemini\x2fantigravity)",
        re.IGNORECASE,
    )

    forbidden_historical_paths = {"idasdlc.db", ".idasdlc/logs/idasdlc.log", "scratch/cp_ps.txt"}
    present_forbidden_paths = forbidden_historical_paths & set(path_by_sha.values())
    assert not present_forbidden_paths, (
        f"Deleted runtime artifacts still present in git history: {sorted(present_forbidden_paths)}"
    )

    proc = subprocess.Popen(
        ["git", "-C", str(REPO_ROOT), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    out_bytes, _ = proc.communicate(("\n".join(shas) + "\n").encode("ascii"))

    matches: list[tuple[str, str]] = []
    private_matches: list[tuple[str, str]] = []
    idx = 0
    total_len = len(out_bytes)
    blob_count = 0
    while idx < total_len:
        nl = out_bytes.find(b"\n", idx)
        if nl == -1:
            break
        header = out_bytes[idx:nl].decode("ascii", errors="replace").split()
        sha, obj_type, size = header[0], header[1], int(header[2])
        body = out_bytes[nl + 1 : nl + 1 + size]
        idx = nl + 1 + size + 1
        if obj_type == "blob":
            blob_count += 1
            if secret_re.search(body):
                matches.append((sha, path_by_sha.get(sha, "")))
            if private_blob_re.search(body):
                private_matches.append((sha, path_by_sha.get(sha, "")))

    tracked_count = len(_get_tracked_files())
    assert blob_count >= 350 and blob_count >= int(tracked_count * 0.8), (
        f"Expected full reachable blob scan (>=350 blobs across {tracked_count} tracked files), got {blob_count}"
    )
    assert not matches, f"Credential pattern found in historical git blobs: {matches}"
    assert not private_matches, f"Private path pattern found in historical git blobs: {private_matches}"

    log_meta = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "log", "--all", "--format=%an <%ae> | %cn <%ce>%n%B"],
        text=True,
    )
    private_meta_re = re.compile(
        r"(\x2fUsers\x2f[A-Za-z0-9_.-]+|\x40Mac\.home|MacBook-[A-Za-z0-9_.-]+\.local)",
        re.IGNORECASE,
    )
    assert not private_meta_re.search(log_meta), (
        "Commit metadata or messages still expose local machine hostnames or user paths"
    )


def test_public_claims_and_evidence_documentation_integrity() -> None:
    """Public docs must align claims with committed evidence, backend boundaries, and prototype status."""
    import json

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    agents_doc = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claims_matrix = (REPO_ROOT / "docs" / "aether_claims_matrix_2026_04_13.md").read_text(encoding="utf-8")
    artifact_index = (REPO_ROOT / "docs" / "aether_artifact_index_2026_04_13.md").read_text(encoding="utf-8")
    readiness_doc = (REPO_ROOT / "docs" / "aether_readiness_package_2026_04_11.md").read_text(encoding="utf-8")
    stage3_doc = (REPO_ROOT / "docs" / "aether_stage3_pg_baseline_closeout_2026_05_10.md").read_text(encoding="utf-8")
    cas_doc = (REPO_ROOT / "docs" / "architecture" / "aether_cas_correctness_proof.md").read_text(encoding="utf-8")
    human_doc = (REPO_ROOT / "HUMAN.md").read_text(encoding="utf-8")
    exec_summary = (REPO_ROOT / "EXECUTIVE_SUMMARY.md").read_text(encoding="utf-8")
    prd_review = (REPO_ROOT / "PRD_REVIEW.md").read_text(encoding="utf-8")
    release_notes = (REPO_ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    qual_run = (REPO_ROOT / "qualification" / "semantic_suite" / "run.py").read_text(encoding="utf-8")

    # 1. Research prototype opening & security/backend scoping
    assert (
        "Aether is an experimental repository control plane for concurrent coding agents. "
        "It implements proposal intake, CRDT-based document operations, selected structural "
        "merge workflows, snapshots, and configurable promotion gates."
    ) in readme
    assert "AETHER_ALLOW_CUSTOM_GATE_COMMANDS" in readme
    assert "/v1/repos/:id/commit_promotion" in readme
    assert "commit_promotion" in readme
    assert "Claims-to-Evidence Table" in readme

    # 2. Cross-check committed 1000-agent preflight JSON against README, Claims Matrix, and Artifact Index
    preflight_path = (
        REPO_ROOT / "peer-review-artifacts" / "20260304" / "phase1" / "phase1-60m-preflight-20260304-121522.json"
    )
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert preflight["passed"] is False
    assert preflight["checks"]["submit_p95_ok"] is False
    assert round(preflight["metrics"]["p95_submit_latency_ms"], 2) == 5759.84
    assert preflight["metrics"]["submitted_total"] == 1334063
    assert preflight["metrics"]["submit_throttled"] == 1328003
    assert preflight["metrics"]["submitted_success"] == 5637
    assert round(preflight["metrics"]["promotion_throughput"], 2) == 1.15

    for doc_name, text in (
        ("README.md", readme),
        ("docs/aether_claims_matrix_2026_04_13.md", claims_matrix),
        ("docs/aether_artifact_index_2026_04_13.md", artifact_index),
        ("docs/aether_readiness_package_2026_04_11.md", readiness_doc),
    ):
        assert "5,759.84" in text or "5759.84" in text, f"{doc_name} must disclose p95_submit_latency_ms=5759.84"
        assert "1,328,003" in text or "99.55%" in text, f"{doc_name} must disclose 99.55% intake throttling"
        assert (
            "passed=false" in text
            or "passed: false" in text
            or "`passed`: `false`" in text
            or '"passed": false' in text
        ), f"{doc_name} must explicitly state the committed 60m preflight failed (passed=false)"
        assert "proposals promoted/" in text, f"{doc_name} must label promotion_throughput as proposals promoted/s"
        assert "0.16" in text, f"{doc_name} must disclose 0.16 promotion batches/s for the 684 batches"
        assert "1.15` promotions/s" not in text and "1.15 promotions/s" not in text
        assert "0.8037` promotions/s" not in text and "0.8037 promotions/s" not in text

    # 2b. Verify advertised runtime toolchain minimums in README.md and AGENTS.md
    assert "1.88" in readme and "20.9.0" in readme
    assert "1.88" in agents_doc and "20.9.0" in agents_doc
    assert "Rust `1.70+`" not in readme and "Rust 1.70+" not in agents_doc
    assert "Node.js `18+`" not in readme

    # 3. Cross-check semantic qualification reports (simulated synthesis worker & echo 'ok' gate)
    qual_report = json.loads(
        (
            REPO_ROOT
            / "qualification"
            / "semantic_suite"
            / "report_sqlite_v2_024163b8a3ebf951923a8e6899051af2793f1748.json"
        ).read_text(encoding="utf-8")
    )
    assert qual_report["model_configuration"] == "simulated-synthesis-worker"
    assert '"command": "echo \'ok\'"' in qual_run
    assert "simulated-synthesis-worker" in readme
    assert "simulated-synthesis-worker" in claims_matrix
    assert "1.0-fake" in readme and "1.0-fake" in claims_matrix
    assert "requiring LLM reconciliation" not in qual_run

    # 4. Stage 3 PG closeout & CAS design argument scoping
    assert "53.96%" in stage3_doc
    assert "175ms" in stage3_doc
    assert "echo SOAK_GATE_OK" in stage3_doc
    assert "complex CRDT promotions" not in stage3_doc
    assert "Conditional OCC & Advisory-Locking Safety Argument" in cas_doc
    assert "commit_promotion" in cas_doc

    # 5. Historical predecessor-project banners and removal of unsupported hype phrases
    for pred_doc in (exec_summary, prd_review, release_notes):
        assert "Historical Predecessor-Project Document (Not Project Aether)" in pred_doc
    assert "Historical Session Note (January 27, 2026)" in human_doc
    for checked_text in (readme, claims_matrix, stage3_doc, cas_doc, human_doc, exec_summary):
        assert "world's first" not in checked_text.lower()
        assert "world’s first" not in checked_text.lower()

