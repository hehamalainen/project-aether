"""Regression tests for release security boundaries and SQLite promotion atomicity."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlmodel import SQLModel, Session, create_engine, select, text
from typer.testing import CliRunner

from apps.aether_cli.main import app as cli_app
from packages.aether.api import routes
from packages.aether.crdt.document_store import (
    DocumentStore,
    parse_unified_diff,
    resolve_contained_path,
    safe_extract_tarball,
    validate_repo_relative_path,
)
from packages.aether.git.exporter import GitExporter
from packages.aether.models.entities import (
    AetherHead,
    AgentMetric,
    ConflictSet,
    GateConfig,
    OperationGraphNode,
    Promotion,
    Proposal,
    ProposalCreate,
    ProposalStatus,
)
from packages.aether.models.events import AetherEvent, EventType
from packages.aether.services.event_bus import EventBus
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine, PromotionPhaseError
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService


@pytest.fixture
def api_env(tmp_path: Path):
    """Create a file-backed SQLite FastAPI test environment."""
    prev_engine = routes._engine
    prev_event_bus = routes._event_bus
    prev_storage_path = routes._storage_path
    db_file = tmp_path / "test_release_security.db"
    storage_dir = tmp_path / "aether_storage"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    event_bus = EventBus()
    routes.set_services(engine, event_bus, storage_dir)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield {
            "client": client,
            "engine": engine,
            "event_bus": event_bus,
            "storage_dir": storage_dir,
            "tmp_path": tmp_path,
        }
    finally:
        routes.set_services(prev_engine, prev_event_bus, prev_storage_path)


class TestProposalPathContainment:
    """Blocker 1: Proposal paths must be validated across all formats and contained on write."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            "",
            "/tmp/outside.py",
            "../outside.py",
            "sub/../../outside.py",
            "./inside.py",
            "sub/./inside.py",
            "sub//inside.py",
            "sub\\inside.py",
            "~/outside.py",
            ".git/config",
            ".GIT/config",
            ".Git/hooks/pre-commit",
            "sub/.GIT/config",
            ".git./config",
            "git~1/config",
            "sub/.git/hooks/pre-commit",
            ".aether_snapshot_manifest.json",
            ".AETHER_SNAPSHOT_MANIFEST.JSON",
            "sub/.aether_snapshot_manifest.json",
            ".aether_crdt_state.json",
            ".AETHER_CRDT_STATE.JSON",
            "C:outside.py",
        ],
    )
    def test_validate_repo_relative_path_rejects_unsafe_paths(self, bad_path: str) -> None:
        with pytest.raises(ValueError):
            validate_repo_relative_path(bad_path)

    def test_validate_repo_relative_path_allows_normal_git_named_package_dir(self) -> None:
        assert (
            validate_repo_relative_path("packages/aether/git/exporter.py")
            == "packages/aether/git/exporter.py"
        )

    def test_api_rejects_traversal_and_absolute_paths_across_all_change_types(
        self, api_env
    ) -> None:
        client: TestClient = api_env["client"]
        tmp_path: Path = api_env["tmp_path"]
        outside_target = tmp_path / "outside_victim.py"
        outside_target.write_text("CANARY_INTACT\n", encoding="utf-8")

        repo_resp = client.post(
            "/v1/repos",
            json={
                "name": "path-containment-repo",
                "config": {"gates": []},
            },
        )
        assert repo_resp.status_code == 200
        repo_id = repo_resp.json()["id"]

        # 1. Traversal in affected_paths
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "full_file",
                "change_content": "OVERWRITTEN\n",
                "affected_paths": ["../outside_victim.py"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        # 2. Absolute path in affected_paths
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "full_file",
                "change_content": "OVERWRITTEN\n",
                "affected_paths": [str(outside_target)],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        # 3. Benign affected_paths=["safe.py"] with traversal in unified_diff headers
        traversal_diff = (
            "diff --git a/../outside_victim.py b/../outside_victim.py\n"
            "--- a/../outside_victim.py\n"
            "+++ b/../outside_victim.py\n"
            "@@ -1 +1 @@\n"
            "-CANARY_INTACT\n"
            "+OVERWRITTEN\n"
        )
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "unified_diff",
                "change_content": traversal_diff,
                "affected_paths": ["safe.py"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        # 4. Benign affected_paths=["safe.py"] with absolute path in unified_diff headers
        abs_diff = (
            f"diff --git a/{outside_target} b/{outside_target}\n"
            f"--- a/{outside_target}\n"
            f"+++ b/{outside_target}\n"
            "@@ -1 +1 @@\n"
            "-CANARY_INTACT\n"
            "+OVERWRITTEN\n"
        )
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "unified_diff",
                "change_content": abs_diff,
                "affected_paths": ["safe.py"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        # 5. Benign diff --git header paired with malicious +++ header
        mismatched_diff = (
            "diff --git a/safe.py b/safe.py\n"
            "--- a/safe.py\n"
            "+++ b/../outside_victim.py\n"
            "@@ -0,0 +1 @@\n"
            "+OVERWRITTEN\n"
        )
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "unified_diff",
                "change_content": mismatched_diff,
                "affected_paths": ["safe.py"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        # 6. Benign affected_paths=["safe.py"] with traversal in a valid aip.v0 mutation schema
        # First verify the exact same AIP v0 mutation structure is valid when path="safe.py"
        valid_aip_control = json.dumps(
            {
                "schema": "aip.v0",
                "mutations": [
                    {
                        "op": "update_node",
                        "path": "safe.py",
                        "node_id": "func:x",
                        "content": "def x():\n    return 1\n",
                    }
                ],
            }
        )
        control_resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "benign-control",
                "change_type": "aip.v0",
                "change_content": valid_aip_control,
                "affected_paths": ["safe.py"],
                "confidence_score": 0.9,
            },
        )
        assert control_resp.status_code == 200, control_resp.text

        # Now substitute only the path with "../outside_victim.py" and verify the path guard rejects it
        aip_payload = json.dumps(
            {
                "schema": "aip.v0",
                "mutations": [
                    {
                        "op": "update_node",
                        "path": "../outside_victim.py",
                        "node_id": "func:x",
                        "content": "def x():\n    return 1\n",
                    }
                ],
            }
        )
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "aip.v0",
                "change_content": aip_payload,
                "affected_paths": ["safe.py"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400
        assert "Path traversal segment is not allowed" in resp.json()["detail"]

        # 7. Case-insensitive .GIT/config alias via API intake
        resp = client.post(
            f"/v1/repos/{repo_id}/proposals",
            json={
                "agent_id": "attacker",
                "change_type": "full_file",
                "change_content": "[core]\n\tfsmonitor = false\n",
                "affected_paths": [".GIT/config"],
                "confidence_score": 0.9,
            },
        )
        assert resp.status_code == 400

        assert outside_target.read_text(encoding="utf-8") == "CANARY_INTACT\n"

    def test_filesystem_materialization_blocks_symlink_escapes(self, tmp_path: Path) -> None:
        outside_dir = tmp_path / "outside_secret_dir"
        outside_dir.mkdir()
        outside_victim = outside_dir / "escape.py"
        outside_victim.write_text("ORIGINAL_SECRET\n", encoding="utf-8")

        store = DocumentStore("repo-symlink-test")
        store.apply_full_file("sub/escape.py", "PWNED\n", "agent-1")

        snapshot_dest = tmp_path / "snapshot_dest"
        snapshot_dest.mkdir()
        (snapshot_dest / "sub").symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(ValueError, match="Symlink escape detected"):
            store.materialize_snapshot(snapshot_dest)

        assert outside_victim.read_text(encoding="utf-8") == "ORIGINAL_SECRET\n"

        # Also verify resolve_contained_path directly rejects symlinked root or final symlink
        symlink_root = tmp_path / "symlink_root"
        symlink_root.symlink_to(outside_dir, target_is_directory=True)
        with pytest.raises(ValueError, match="(?i)symlink"):
            resolve_contained_path(symlink_root, "escape.py")

    def test_snapshot_clone_gate_workspace_and_git_exporter_reject_symlink_escape(
        self, api_env
    ) -> None:
        tmp_path: Path = api_env["tmp_path"]
        outside_dir = tmp_path / "outside_export_target"
        outside_dir.mkdir()
        outside_file = outside_dir / "app.py"
        outside_file.write_text("UNTOUCHED\n", encoding="utf-8")

        with Session(api_env["engine"]) as session:
            repo_service = RepositoryService(session, api_env["storage_dir"])
            snapshot_service = SnapshotService(session, api_env["event_bus"], api_env["storage_dir"])
            repo = repo_service.create_repository(name="symlink-export-repo")
            doc_store = repo_service.get_document_store(repo.id)
            assert doc_store is not None
            doc_store.apply_full_file("pkg/app.py", "print('inside')\n", "init")
            snap = asyncio_run(
                snapshot_service.create_snapshot(repo.id, doc_store, included_proposals=[])
            )

            clone_dest = tmp_path / "clone_dest"
            clone_dest.mkdir()
            (clone_dest / "pkg").symlink_to(outside_dir, target_is_directory=True)
            with pytest.raises(ValueError, match="(?i)symlink"):
                snapshot_service.clone_to_directory(snap.id, clone_dest)

            gate_dest = tmp_path / "gate_dest"
            gate_dest.mkdir()
            with pytest.raises(ValueError, match="(?i)symlink"):
                snap_workspace = gate_dest / snap.id
                snap_workspace.mkdir()
                (snap_workspace / "pkg").symlink_to(outside_dir, target_is_directory=True)
                snapshot_service._materialize_delta_snapshot(
                    document_store=doc_store,
                    snapshot_path=snap_workspace,
                    base_snapshot_id="snap-base",
                    base_paths={"pkg/app.py"},
                    changed_paths={"pkg/app.py"},
                    total_bytes=16,
                )

            export_dest = tmp_path / "export_dest"
            export_dest.mkdir()
            (export_dest / ".git").mkdir()
            (export_dest / "pkg").symlink_to(outside_dir, target_is_directory=True)
            exporter = GitExporter(tmp_path / "exporter_storage")
            with pytest.raises(ValueError, match="(?i)symlink"):
                exporter.export_snapshot_to_git(
                    doc_store,
                    output_path=export_dest,
                    commit_message="attempt symlink escape",
                    init_if_needed=False,
                )

        assert outside_file.read_text(encoding="utf-8") == "UNTOUCHED\n"


class TestSnapshotTarballExtractionSafety:
    """Blocker 2: Tarball extraction must reject traversal, absolute paths, links, and special files."""

    @staticmethod
    def _build_tar_gz(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for info, payload in members:
                if payload is not None:
                    info.size = len(payload)
                    tf.addfile(info, io.BytesIO(payload))
                else:
                    tf.addfile(info)
        return buf.getvalue()

    def test_safe_extract_tarball_rejects_parent_traversal(self, tmp_path: Path) -> None:
        outside_victim = tmp_path / "outside.txt"
        dest_dir = tmp_path / "extract_here"
        dest_dir.mkdir()

        info = tarfile.TarInfo(name="../outside.txt")
        archive_bytes = self._build_tar_gz([(info, b"escaped\n")])

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Path traversal segment is not allowed"):
                safe_extract_tarball(tf, dest_dir)

        assert not outside_victim.exists()

    def test_safe_extract_tarball_rejects_absolute_path(self, tmp_path: Path) -> None:
        outside_victim = tmp_path / "abs_outside.txt"
        dest_dir = tmp_path / "extract_abs"
        dest_dir.mkdir()

        info = tarfile.TarInfo(name=str(outside_victim))
        archive_bytes = self._build_tar_gz([(info, b"escaped\n")])

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Path must be a relative repository path"):
                safe_extract_tarball(tf, dest_dir)

        assert not outside_victim.exists()

    def test_safe_extract_tarball_rejects_symlink_and_hardlink_escapes(
        self, tmp_path: Path
    ) -> None:
        outside_dir = tmp_path / "outside_link_target"
        outside_dir.mkdir()
        outside_victim = outside_dir / "pwn.txt"
        dest_dir = tmp_path / "extract_links"
        dest_dir.mkdir()

        sym_info = tarfile.TarInfo(name="sub")
        sym_info.type = tarfile.SYMTYPE
        sym_info.linkname = str(outside_dir)

        file_info = tarfile.TarInfo(name="sub/pwn.txt")
        archive_bytes = self._build_tar_gz([(sym_info, None), (file_info, b"pwned\n")])

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Unsafe or unsupported tar member type"):
                safe_extract_tarball(tf, dest_dir)

        assert not outside_victim.exists()

        hard_info = tarfile.TarInfo(name="hardlink.txt")
        hard_info.type = tarfile.LNKTYPE
        hard_info.linkname = "../outside.txt"
        hard_archive = self._build_tar_gz([(hard_info, None)])

        with tarfile.open(fileobj=io.BytesIO(hard_archive), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Unsafe or unsupported tar member type"):
                safe_extract_tarball(tf, dest_dir)

    def test_safe_extract_tarball_rejects_special_fifo_member(self, tmp_path: Path) -> None:
        dest_dir = tmp_path / "extract_special"
        dest_dir.mkdir()

        fifo_info = tarfile.TarInfo(name="named_pipe")
        fifo_info.type = tarfile.FIFOTYPE
        archive_bytes = self._build_tar_gz([(fifo_info, None)])

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Unsafe or unsupported tar member type"):
                safe_extract_tarball(tf, dest_dir)

    @pytest.mark.parametrize(
        "git_alias_member",
        [
            ".GIT/config",
            ".Git/hooks/pre-commit",
            "sub/.GIT/config",
            ".git./config",
            "git~1/config",
        ],
    )
    def test_safe_extract_tarball_rejects_case_insensitive_git_aliases(
        self, tmp_path: Path, git_alias_member: str
    ) -> None:
        dest_dir = tmp_path / "extract_git_alias"
        dest_dir.mkdir()

        info = tarfile.TarInfo(name=git_alias_member)
        archive_bytes = self._build_tar_gz([(info, b"[core]\n\tfsmonitor = true\n")])

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            with pytest.raises(ValueError, match="Reserved path segment"):
                safe_extract_tarball(tf, dest_dir)

    def test_cli_git_export_rejects_malicious_tarball_from_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        outside_victim = tmp_path / "cli_outside_escape.txt"
        marker_file = tmp_path / "fsmonitor_executed.marker"
        export_target_traversal = tmp_path / "git_export_out_traversal"
        export_target_git_alias = tmp_path / "git_export_out_git_alias"
        export_target_benign = tmp_path / "git_export_out_benign"

        traversal_info = tarfile.TarInfo(name="../cli_outside_escape.txt")
        traversal_tar = self._build_tar_gz([(traversal_info, b"cli_escaped\n")])

        git_alias_info = tarfile.TarInfo(name=".GIT/config")
        git_alias_payload = (
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tfilemode = true\n"
            "\tbare = false\n"
            f"\tfsmonitor = touch {marker_file}\n"
        ).encode("utf-8")
        git_alias_tar = self._build_tar_gz([(git_alias_info, git_alias_payload)])

        benign_info = tarfile.TarInfo(name="src/main.py")
        benign_tar = self._build_tar_gz([(benign_info, b"print('ok')\n")])

        requested_urls: list[str] = []
        active_archive = {"bytes": traversal_tar}

        class _FakeResponse:
            def __init__(self, url: str, content: bytes):
                self.url = url
                self.status_code = 200
                self.content = content

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"snapshot_id": "snap-test", "head_sequence": 1}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

            def get(self, url: str):
                requested_urls.append(url)
                return _FakeResponse(url, active_archive["bytes"])

        import httpx

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        runner = CliRunner()

        # 1. Parent traversal archive reaches download/extraction and fails closed
        requested_urls.clear()
        active_archive["bytes"] = traversal_tar
        result_traversal = runner.invoke(
            cli_app,
            ["git-export", "--repo", "repo-123", "--output", str(export_target_traversal)],
        )
        assert result_traversal.exit_code == 1
        assert "/v1/repos/repo-123/head" in requested_urls
        assert "/v1/repos/repo-123/snapshots/snap-test/download" in requested_urls
        assert "Export failed:" in result_traversal.stdout
        assert "Path traversal segment is not allowed" in result_traversal.stdout
        assert not outside_victim.exists()

        # 2. Case-insensitive .GIT/config archive reaches download/extraction and fails closed
        requested_urls.clear()
        active_archive["bytes"] = git_alias_tar
        result_git_alias = runner.invoke(
            cli_app,
            ["git-export", "--repo", "repo-123", "--output", str(export_target_git_alias)],
        )
        assert result_git_alias.exit_code == 1
        assert "/v1/repos/repo-123/head" in requested_urls
        assert "/v1/repos/repo-123/snapshots/snap-test/download" in requested_urls
        assert "Export failed:" in result_git_alias.stdout
        assert "Reserved path segment '.GIT' is not allowed" in result_git_alias.stdout
        assert not marker_file.exists()
        assert not export_target_git_alias.exists()

        # 3. Benign archive with the exact same CLI flags succeeds and commits cleanly
        requested_urls.clear()
        active_archive["bytes"] = benign_tar
        result_benign = runner.invoke(
            cli_app,
            ["git-export", "--repo", "repo-123", "--output", str(export_target_benign)],
        )
        assert result_benign.exit_code == 0, result_benign.stdout
        assert "/v1/repos/repo-123/head" in requested_urls
        assert "/v1/repos/repo-123/snapshots/snap-test/download" in requested_urls
        assert "Export successful!" in result_benign.stdout
        assert (export_target_benign / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"


class TestSQLitePromotionAtomicity:
    """Blocker 3: SQLite promotion must commit canonical state in one transaction and defer events."""

    def test_sqlite_trigger_abort_on_promoted_status_rolls_back_all_canonical_state(
        self, api_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AETHER_DEFER_CONFLICT_DETECTION", "false")
        client: TestClient = api_env["client"]
        engine = api_env["engine"]
        event_bus: EventBus = api_env["event_bus"]

        published_events: list[AetherEvent] = []
        sub_id = event_bus.subscribe(lambda evt: published_events.append(evt))

        try:
            repo_resp = client.post(
                "/v1/repos",
                json={
                    "name": "atomic-sqlite-repo",
                    "config": {
                        "gates": [
                            {
                                "name": "build",
                                "command": "echo 'Build OK'",
                                "required": True,
                                "timeout_seconds": 10,
                            }
                        ]
                    },
                },
            )
            assert repo_resp.status_code == 200
            repo_id = repo_resp.json()["id"]

            # Submit two conflicting proposals so ConflictSet resolution is also exercised
            prop1_resp = client.post(
                f"/v1/repos/{repo_id}/proposals",
                json={
                    "agent_id": "agent-atomic-1",
                    "change_type": "full_file",
                    "change_content": "VALUE = 1\n",
                    "affected_paths": ["atomic.py"],
                    "confidence_score": 0.95,
                },
            )
            assert prop1_resp.status_code == 200
            prop1_id = prop1_resp.json()["proposal_id"]

            prop2_resp = client.post(
                f"/v1/repos/{repo_id}/proposals",
                json={
                    "agent_id": "agent-atomic-2",
                    "change_type": "full_file",
                    "change_content": "VALUE = 2\n",
                    "affected_paths": ["atomic.py"],
                    "confidence_score": 0.60,
                },
            )
            assert prop2_resp.status_code == 200
            prop2_id = prop2_resp.json()["proposal_id"]

            # Clear submission events before testing promotion
            published_events.clear()

            # Inject SQLite triggers at two points:
            # (a) during status_updates (when Proposal.status becomes PROMOTED), and
            # (b) at final trust_commit flush (when AgentMetric.promoted_count is incremented after
            #     Promotion, AetherHead, OperationGraphNode, ProposalStatus.PROMOTED,
            #     ConflictSet.resolved, and loser ProposalStatus.SUPERSEDED have all been flushed).
            with Session(engine) as session:
                session.exec(
                    text(
                        """
                        CREATE TRIGGER reject_promoted_status
                        BEFORE UPDATE ON aether_proposals
                        WHEN NEW.status = 'PROMOTED'
                        BEGIN
                            SELECT RAISE(ABORT, 'injected_promoted_status_failure');
                        END;
                        """
                    )
                )
                session.exec(
                    text(
                        """
                        CREATE TRIGGER reject_final_trust_commit_update
                        BEFORE UPDATE ON aether_agent_metrics
                        WHEN NEW.promoted_count > 0
                        BEGIN
                            SELECT RAISE(ABORT, 'injected_final_trust_commit_failure');
                        END;
                        """
                    )
                )
                session.exec(
                    text(
                        """
                        CREATE TRIGGER reject_final_trust_commit_insert
                        BEFORE INSERT ON aether_agent_metrics
                        WHEN NEW.promoted_count > 0
                        BEGIN
                            SELECT RAISE(ABORT, 'injected_final_trust_commit_failure');
                        END;
                        """
                    )
                )
                session.commit()

            promote_resp = client.post(
                f"/v1/repos/{repo_id}/promotions",
                json={"proposal_ids": [prop1_id]},
            )
            assert promote_resp.status_code == 500

            # Drop the mid-transaction status trigger so the second attempt reaches final trust_commit
            # after flushing Promotion, AetherHead, OperationGraphNode, PROMOTED, ConflictSet, and SUPERSEDED.
            with Session(engine) as session:
                session.exec(text("DROP TRIGGER reject_promoted_status;"))
                session.commit()

            promote_final_abort_resp = client.post(
                f"/v1/repos/{repo_id}/promotions",
                json={"proposal_ids": [prop1_id]},
            )
            assert promote_final_abort_resp.status_code == 500

            # Verify no canonical promotion state was left committed after final-commit abort
            with Session(engine) as session:
                repo = session.get(routes.Repository, repo_id)
                assert repo is not None
                assert repo.head_sequence == 0
                assert repo.head_id is None

                promotions = session.exec(
                    select(Promotion).where(Promotion.repo_id == repo_id)
                ).all()
                assert promotions == []

                heads = session.exec(
                    select(AetherHead).where(AetherHead.repo_id == repo_id)
                ).all()
                assert heads == []

                op_nodes = session.exec(
                    select(OperationGraphNode).where(OperationGraphNode.repo_id == repo_id)
                ).all()
                assert op_nodes == []

                prop1_after_rollback = session.get(Proposal, prop1_id)
                assert prop1_after_rollback is not None
                assert prop1_after_rollback.status != ProposalStatus.PROMOTED
                assert prop1_after_rollback.promoted_at is None

                prop2_after_rollback = session.get(Proposal, prop2_id)
                assert prop2_after_rollback is not None
                assert prop2_after_rollback.status == ProposalStatus.PENDING

                conflict_sets = session.exec(
                    select(ConflictSet).where(ConflictSet.repo_id == repo_id)
                ).all()
                assert len(conflict_sets) == 1
                assert conflict_sets[0].resolved is False
                assert conflict_sets[0].resolved_at is None
                assert conflict_sets[0].active_choice_id is None

                metric = session.get(AgentMetric, ("agent-atomic-1", "global"))
                assert metric is None or metric.promoted_count == 0

            # Verify no promotion/conflict/head events were published when commit failed
            forbidden_event_types = {
                EventType.PROPOSAL_PROMOTED,
                EventType.PROPOSAL_REJECTED,
                EventType.CONFLICT_RESOLVED,
                EventType.HEAD_UPDATED,
            }
            published_types = {evt.event_type for evt in published_events}
            assert forbidden_event_types.isdisjoint(published_types), (
                f"Events published despite transaction rollback: {published_types}"
            )

            # Now remove the final-commit triggers and verify promotion succeeds cleanly in one transaction
            with Session(engine) as session:
                session.exec(text("DROP TRIGGER reject_final_trust_commit_update;"))
                session.exec(text("DROP TRIGGER reject_final_trust_commit_insert;"))
                session.commit()

            published_events.clear()
            retry_resp = client.post(
                f"/v1/repos/{repo_id}/promotions",
                json={"proposal_ids": [prop1_id]},
            )
            assert retry_resp.status_code == 200
            retry_body = retry_resp.json()
            assert retry_body["success"] is True
            assert retry_body["new_head_sequence"] == 1

            with Session(engine) as session:
                repo = session.get(routes.Repository, repo_id)
                assert repo is not None
                assert repo.head_sequence == 1
                assert repo.head_id is not None

                promotions = session.exec(
                    select(Promotion).where(Promotion.repo_id == repo_id)
                ).all()
                assert len(promotions) == 1

                prop1 = session.get(Proposal, prop1_id)
                assert prop1 is not None
                assert prop1.status == ProposalStatus.PROMOTED

                conflict_sets = session.exec(
                    select(ConflictSet).where(ConflictSet.repo_id == repo_id)
                ).all()
                assert len(conflict_sets) == 1
                assert conflict_sets[0].resolved is True
                assert conflict_sets[0].resolved_at is not None
                assert conflict_sets[0].active_choice_id == prop1_id

            retry_event_types = [evt.event_type for evt in published_events]
            assert EventType.PROPOSAL_PROMOTED in retry_event_types
            assert EventType.CONFLICT_RESOLVED in retry_event_types
            assert EventType.HEAD_UPDATED in retry_event_types
        finally:
            event_bus.unsubscribe(sub_id)

    def test_sqlite_final_session_commit_failure_rolls_back_all_canonical_state(
        self, api_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directly fail PromotionEngine's final session.commit() after conflict/trust mutations."""
        monkeypatch.setenv("AETHER_DEFER_CONFLICT_DETECTION", "false")
        engine = api_env["engine"]
        event_bus: EventBus = api_env["event_bus"]
        storage_dir: Path = api_env["storage_dir"]

        published_events: list[AetherEvent] = []
        sub_id = event_bus.subscribe(lambda evt: published_events.append(evt))

        try:
            with Session(engine) as session:
                repo_service = RepositoryService(session, storage_dir)
                proposal_service = ProposalService(session, event_bus, repo_service)
                snapshot_service = SnapshotService(session, event_bus, storage_dir)
                gate_runner = GateRunner(session, event_bus)
                promotion_engine = PromotionEngine(
                    session,
                    event_bus,
                    proposal_service,
                    snapshot_service,
                    gate_runner,
                    storage_dir,
                )

                repo = repo_service.create_repository(
                    name="final-commit-abort-repo",
                    config=routes.RepoConfig(
                        gates=[
                            GateConfig(
                                name="build",
                                command="echo 'Build OK'",
                                required=True,
                                timeout_seconds=10,
                            )
                        ]
                    ),
                )
                doc_store = repo_service.get_document_store(repo.id)
                assert doc_store is not None

                prop1_resp = asyncio_run(
                    proposal_service.submit_proposal(
                        repo.id,
                        ProposalCreate(
                            agent_id="agent-final-1",
                            change_type="full_file",
                            change_content="FINAL = 1\n",
                            affected_paths=["final.py"],
                            confidence_score=0.95,
                        ),
                    )
                )
                prop2_resp = asyncio_run(
                    proposal_service.submit_proposal(
                        repo.id,
                        ProposalCreate(
                            agent_id="agent-final-2",
                            change_type="full_file",
                            change_content="FINAL = 2\n",
                            affected_paths=["final.py"],
                            confidence_score=0.55,
                        ),
                    )
                )
                prop1 = proposal_service.get_proposal(prop1_resp.proposal_id)
                prop2 = proposal_service.get_proposal(prop2_resp.proposal_id)
                assert prop1 is not None and prop2 is not None
                repo_id = repo.id
                prop1_id = prop1.id
                prop2_id = prop2.id

                candidate = asyncio_run(promotion_engine.evaluate_proposal(prop1, doc_store))
                published_events.clear()

                real_commit = session.commit

                def _fail_final_commit():
                    # Flush all pending changes (including conflict set resolution and trust metric)
                    # into the SQLite transaction, then raise right at the final commit boundary.
                    session.flush()
                    raise RuntimeError("simulated_final_commit_failure")

                monkeypatch.setattr(session, "commit", _fail_final_commit)
                with pytest.raises(RuntimeError, match="simulated_final_commit_failure"):
                    asyncio_run(promotion_engine.promote(repo_id, candidate, doc_store))
                monkeypatch.setattr(session, "commit", real_commit)

            with Session(engine) as verify_session:
                repo_row = verify_session.get(routes.Repository, repo_id)
                assert repo_row is not None
                assert repo_row.head_sequence == 0
                assert repo_row.head_id is None

                assert (
                    verify_session.exec(
                        select(Promotion).where(Promotion.repo_id == repo_id)
                    ).all()
                    == []
                )
                assert (
                    verify_session.exec(
                        select(AetherHead).where(AetherHead.repo_id == repo_id)
                    ).all()
                    == []
                )
                assert (
                    verify_session.exec(
                        select(OperationGraphNode).where(OperationGraphNode.repo_id == repo_id)
                    ).all()
                    == []
                )

                p1_row = verify_session.get(Proposal, prop1_id)
                assert p1_row is not None
                assert p1_row.status == ProposalStatus.EVALUATING
                assert p1_row.promoted_at is None

                p2_row = verify_session.get(Proposal, prop2_id)
                assert p2_row is not None
                assert p2_row.status == ProposalStatus.PENDING

                cs_rows = verify_session.exec(
                    select(ConflictSet).where(ConflictSet.repo_id == repo_id)
                ).all()
                assert len(cs_rows) == 1
                assert cs_rows[0].resolved is False
                assert cs_rows[0].resolved_at is None
                assert cs_rows[0].active_choice_id is None

                metric_row = verify_session.get(AgentMetric, ("agent-final-1", "global"))
                assert metric_row is None or metric_row.promoted_count == 0

            assert published_events == []
        finally:
            event_bus.unsubscribe(sub_id)


def test_cli_promote_help_does_not_claim_force_bypasses_gates() -> None:
    """Minor note: CLI --force help text must reflect that required gates remain fail-closed."""
    runner = CliRunner()
    result = runner.invoke(cli_app, ["promote", "--help"])
    assert result.exit_code == 0
    assert "even if gates fail" not in result.stdout.lower()
    assert "fail-closed" in result.stdout.lower()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
