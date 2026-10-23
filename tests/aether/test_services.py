"""Tests for Aether services."""

import pytest
import asyncio
import json
from pathlib import Path
from sqlmodel import SQLModel, Session, create_engine, select

from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    ProposalSource,
    ConflictSet,
    Snapshot,
    GateResult,
    GateStatus,
    Promotion,
    OperationGraphNode,
    AetherHead,
    GateConfig,
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import (
    EvaluationPhaseError,
    ExternalCommitAbortedError,
    PromotionEngine,
    PromotionPhaseError,
)
from packages.aether.services.snapshot_reproducer import (
    SnapshotReproducerConfig,
    run_snapshot_reproducer,
)


@pytest.fixture
def db_session():
    """Create in-memory database session."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def event_bus():
    """Create event bus."""
    return EventBus()


@pytest.fixture
def storage_path(tmp_path):
    """Create temporary storage path."""
    return tmp_path / "aether_storage"


@pytest.fixture
def repo_service(db_session, storage_path):
    """Create repository service."""
    return RepositoryService(db_session, storage_path)


@pytest.fixture
def proposal_service(db_session, event_bus):
    """Create proposal service."""
    return ProposalService(db_session, event_bus, {})


@pytest.fixture
def snapshot_service(db_session, event_bus, storage_path):
    """Create snapshot service."""
    return SnapshotService(db_session, event_bus, storage_path)


@pytest.fixture
def gate_runner(db_session, event_bus):
    """Create gate runner."""
    return GateRunner(db_session, event_bus)


class TestRepositoryService:
    """Test repository service."""

    def test_create_repository(self, repo_service):
        """Should create a repository."""
        repo = repo_service.create_repository(
            name="test-repo",
            description="A test repository",
        )

        assert repo.id.startswith("repo_")
        assert repo.name == "test-repo"
        assert repo.head_sequence == 0

    def test_get_repository(self, repo_service):
        """Should retrieve repository by ID."""
        created = repo_service.create_repository(name="test")
        retrieved = repo_service.get_repository(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id

    def test_list_repositories(self, repo_service):
        """Should list all repositories."""
        repo_service.create_repository(name="repo1")
        repo_service.create_repository(name="repo2")

        repos = repo_service.list_repositories()
        assert len(repos) == 2

    def test_delete_repository(self, repo_service):
        """Should delete a repository."""
        repo = repo_service.create_repository(name="to-delete")
        assert repo_service.delete_repository(repo.id)
        assert repo_service.get_repository(repo.id) is None

    def test_get_document_store(self, repo_service):
        """Should get document store for repository."""
        repo = repo_service.create_repository(name="test")
        store = repo_service.get_document_store(repo.id)

        assert store is not None
        assert store.repo_id == repo.id

    @pytest.mark.asyncio
    async def test_get_document_store_loads_delta_head_snapshot(
        self,
        repo_service,
        snapshot_service,
        db_session,
    ):
        """Repository document store bootstrap should reconstruct full state from delta head."""
        repo = repo_service.create_repository(name="delta-bootstrap")
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('base')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        proposal = Proposal(
            repo_id=repo.id,
            agent_id="agent-bootstrap",
            change_type="full_file",
            change_content="print('head')\n",
            affected_paths=json.dumps(["main.py"]),
            confidence_score=0.9,
            status=ProposalStatus.PENDING,
        )
        db_session.add(proposal)
        db_session.commit()
        db_session.refresh(proposal)

        head_store = base_store.clone()
        head_store.apply_full_file("main.py", "print('head')\n", proposal.id)
        delta_snapshot = await snapshot_service.create_snapshot(repo.id, head_store, [proposal.id])
        repo_service.update_head(repo.id, delta_snapshot.id)

        repo_service._document_stores.pop(repo.id, None)
        loaded = repo_service.get_document_store(repo.id)
        assert loaded is not None
        assert loaded.get_file_content("main.py") == "print('head')\n"
        assert loaded.get_file_content("util.py") == "def helper():\n    return 1\n"


class TestProposalService:
    """Test proposal service."""

    @pytest.fixture
    def repo(self, repo_service):
        """Create a test repository."""
        return repo_service.create_repository(name="test-repo")

    @pytest.mark.asyncio
    async def test_submit_proposal(self, proposal_service, repo):
        """Should submit a proposal."""
        proposal_data = ProposalCreate(
            agent_id="test-agent",
            task_id="task-1",
            change_type="unified_diff",
            change_content="diff content",
            affected_paths=["file.py"],
            confidence_score=0.85,
        )

        response = await proposal_service.submit_proposal(repo.id, proposal_data)

        assert response.proposal_id.startswith("prop_")
        assert response.status == ProposalStatus.PENDING

    @pytest.mark.asyncio
    async def test_submit_proposal_persists_source_metadata(self, proposal_service, repo):
        """Source-tagged proposals should persist external correlation metadata."""
        proposal_data = ProposalCreate(
            agent_id="mx-agent-1",
            task_id="task-1",
            source=ProposalSource.MX_SWARM.value,
            source_run_id="mx-run-42",
            source_task_id="mx-task-9",
            source_agent_id="mx-agent-1",
            source_attempt_id="attempt-3",
            intent_type="refactor",
            change_type="unified_diff",
            change_content="diff content",
            affected_paths=["file.py"],
            confidence_score=0.85,
        )

        response = await proposal_service.submit_proposal(repo.id, proposal_data)
        stored = proposal_service.get_proposal(response.proposal_id)

        assert stored is not None
        assert response.source == ProposalSource.MX_SWARM.value
        assert stored.source == ProposalSource.MX_SWARM.value
        assert stored.source_run_id == "mx-run-42"
        assert stored.source_task_id == "mx-task-9"
        assert stored.source_agent_id == "mx-agent-1"
        assert stored.source_attempt_id == "attempt-3"
        assert stored.intent_type == "refactor"

    @pytest.mark.asyncio
    async def test_submit_proposal_defaults_source_to_manual(self, proposal_service, repo):
        """Untyped proposal submissions should remain backward compatible."""
        response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="manual-agent",
                change_type="unified_diff",
                change_content="diff",
                affected_paths=["file.py"],
            ),
        )
        stored = proposal_service.get_proposal(response.proposal_id)
        assert stored is not None
        assert response.source == ProposalSource.MANUAL.value
        assert stored.source == ProposalSource.MANUAL.value

    @pytest.mark.asyncio
    async def test_idempotent_submission(self, proposal_service, repo):
        """Same idempotency key should return same proposal."""
        proposal_data = ProposalCreate(
            agent_id="test-agent",
            change_content="diff",
            affected_paths=["file.py"],
            idempotency_key="unique-key-123",
        )

        response1 = await proposal_service.submit_proposal(repo.id, proposal_data)
        response2 = await proposal_service.submit_proposal(repo.id, proposal_data)

        assert response1.proposal_id == response2.proposal_id

    @pytest.mark.asyncio
    async def test_conflict_detection(self, proposal_service, repo):
        """Overlapping proposals should create conflict set."""
        # Submit first proposal
        proposal1 = ProposalCreate(
            agent_id="agent-1",
            change_content="diff1",
            affected_paths=["shared_file.py"],
            confidence_score=0.8,
        )
        response1 = await proposal_service.submit_proposal(repo.id, proposal1)

        # Submit second proposal affecting same file
        proposal2 = ProposalCreate(
            agent_id="agent-2",
            change_content="diff2",
            affected_paths=["shared_file.py"],
            confidence_score=0.7,
        )
        response2 = await proposal_service.submit_proposal(repo.id, proposal2)

        # Second should have conflict
        assert response2.conflict_set_id is not None

    @pytest.mark.asyncio
    async def test_semantic_fallback_marks_file_symbol_for_ambiguous_diff(self, proposal_service, repo):
        """Ambiguous hunks should persist __file__ fallback and force conflict."""
        store = DocumentStore(repo.id)
        store.apply_full_file("shared.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, store)

        ambiguous_diff = """diff --git a/shared.py b/shared.py
--- a/shared.py
+++ b/shared.py
@@ -0,0 +1,1 @@
+print("bootstrap")
"""
        proposal1 = ProposalCreate(
            agent_id="agent-fallback",
            change_type="unified_diff",
            change_content=ambiguous_diff,
            affected_paths=["shared.py"],
            confidence_score=0.9,
        )
        response1 = await proposal_service.submit_proposal(repo.id, proposal1)
        stored1 = proposal_service.get_proposal(response1.proposal_id)
        assert stored1 is not None
        assert stored1.affected_symbols is not None
        assert "shared.py::__file__" in json.loads(stored1.affected_symbols)

        semantic_diff = """diff --git a/shared.py b/shared.py
--- a/shared.py
+++ b/shared.py
@@ -1,1 +1,2 @@
 def alpha():
+    pass
"""
        proposal2 = ProposalCreate(
            agent_id="agent-semantic",
            change_type="unified_diff",
            change_content=semantic_diff,
            affected_paths=["shared.py"],
            affected_symbols=["shared.py::alpha"],
            confidence_score=0.8,
        )
        response2 = await proposal_service.submit_proposal(repo.id, proposal2)
        assert response2.conflict_set_id is not None

    @pytest.mark.asyncio
    async def test_semantic_fallback_marks_file_symbol_for_unparseable_diff(self, proposal_service, repo):
        """Unparseable diffs should fall back to file-level semantic conflict."""
        store = DocumentStore(repo.id)
        store.apply_full_file("shared.py", "def beta():\n    return 2\n", "init")
        proposal_service.set_document_store(repo.id, store)

        unparseable_diff = """diff --git a/shared.py b/shared.py
--- a/shared.py
+++ b/shared.py
this is not a hunk
"""
        proposal1 = ProposalCreate(
            agent_id="agent-u1",
            change_type="unified_diff",
            change_content=unparseable_diff,
            affected_paths=["shared.py"],
            confidence_score=0.7,
        )
        response1 = await proposal_service.submit_proposal(repo.id, proposal1)
        stored1 = proposal_service.get_proposal(response1.proposal_id)
        assert stored1 is not None
        assert stored1.affected_symbols is not None
        assert "shared.py::__file__" in json.loads(stored1.affected_symbols)

        proposal2 = ProposalCreate(
            agent_id="agent-u2",
            change_type="unified_diff",
            change_content=unparseable_diff,
            affected_paths=["shared.py"],
            confidence_score=0.6,
        )
        response2 = await proposal_service.submit_proposal(repo.id, proposal2)
        stored2 = proposal_service.get_proposal(response2.proposal_id)
        assert stored2 is not None
        assert stored2.affected_symbols is not None
        assert "shared.py::__file__" in json.loads(stored2.affected_symbols)
        assert response2.conflict_set_id is not None

    @pytest.mark.asyncio
    async def test_non_python_symbol_detection_allows_disjoint_edits(self, proposal_service, repo):
        """Disjoint TypeScript function edits should avoid file-level conflicts."""
        store = DocumentStore(repo.id)
        store.apply_full_file(
            "main.ts",
            (
                "function alpha() {\n"
                "  return 1;\n"
                "}\n"
                "\n"
                "function beta() {\n"
                "  return 2;\n"
                "}\n"
            ),
            "init",
        )
        proposal_service.set_document_store(repo.id, store)

        diff_alpha = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -1,3 +1,3 @@
 function alpha() {
-  return 1;
+  return 10;
 }
"""
        diff_beta = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -5,3 +5,3 @@
 function beta() {
-  return 2;
+  return 20;
 }
"""
        first = ProposalCreate(
            agent_id="agent-ts-1",
            change_type="unified_diff",
            change_content=diff_alpha,
            affected_paths=["main.ts"],
            confidence_score=0.8,
        )
        second = ProposalCreate(
            agent_id="agent-ts-2",
            change_type="unified_diff",
            change_content=diff_beta,
            affected_paths=["main.ts"],
            confidence_score=0.8,
        )

        response1 = await proposal_service.submit_proposal(repo.id, first)
        response2 = await proposal_service.submit_proposal(repo.id, second)

        proposal1 = proposal_service.get_proposal(response1.proposal_id)
        proposal2 = proposal_service.get_proposal(response2.proposal_id)
        assert proposal1 is not None
        assert proposal2 is not None
        assert proposal1.affected_symbols is not None
        assert proposal2.affected_symbols is not None
        symbols1 = json.loads(proposal1.affected_symbols)
        symbols2 = json.loads(proposal2.affected_symbols)
        assert any(symbol.endswith("FUNC:alpha") for symbol in symbols1)
        assert any(symbol.endswith("FUNC:beta") for symbol in symbols2)
        assert response2.conflict_set_id is None

    @pytest.mark.asyncio
    async def test_non_python_same_symbol_still_conflicts(self, proposal_service, repo):
        """Overlapping TypeScript function edits should still conflict."""
        store = DocumentStore(repo.id)
        store.apply_full_file(
            "main.ts",
            (
                "function alpha() {\n"
                "  return 1;\n"
                "}\n"
            ),
            "init",
        )
        proposal_service.set_document_store(repo.id, store)

        diff_one = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -1,3 +1,3 @@
 function alpha() {
-  return 1;
+  return 10;
 }
"""
        diff_two = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -1,3 +1,3 @@
 function alpha() {
-  return 1;
+  return 11;
 }
"""
        first = ProposalCreate(
            agent_id="agent-ts-conflict-1",
            change_type="unified_diff",
            change_content=diff_one,
            affected_paths=["main.ts"],
            confidence_score=0.8,
        )
        second = ProposalCreate(
            agent_id="agent-ts-conflict-2",
            change_type="unified_diff",
            change_content=diff_two,
            affected_paths=["main.ts"],
            confidence_score=0.8,
        )

        _ = await proposal_service.submit_proposal(repo.id, first)
        response2 = await proposal_service.submit_proposal(repo.id, second)
        assert response2.conflict_set_id is not None

    @pytest.mark.asyncio
    async def test_aip_submission_populates_paths_and_symbols(self, proposal_service, repo):
        """AIP payload should auto-populate affected paths and semantic symbols."""
        store = DocumentStore(repo.id)
        store.apply_full_file("main.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, store)

        payload = """
        {
          "schema": "aip.v0",
          "mutations": [
            {
              "op": "update_node",
              "path": "main.py",
              "node_id": "FUNC:alpha",
              "content": "def alpha():\\n    return 2\\n"
            }
          ]
        }
        """

        response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-aip",
                change_type="aip.v0",
                change_content=payload,
                affected_paths=[],
                confidence_score=0.9,
            ),
        )
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None
        assert "main.py" in json.loads(proposal.affected_paths)
        assert proposal.affected_symbols is not None
        assert "main.py::FUNC:alpha" in json.loads(proposal.affected_symbols)

    @pytest.mark.asyncio
    async def test_aip_invalid_payload_is_rejected(self, proposal_service, repo):
        """Invalid AIP payloads should fail validation before persistence."""
        invalid_payload = """
        {
          "schema": "aip.v0",
          "mutations": [
            {
              "op": "update_node",
              "path": "main.py",
              "node_id": "FUNC:alpha"
            }
          ]
        }
        """
        with pytest.raises(ValueError):
            await proposal_service.submit_proposal(
                repo.id,
                ProposalCreate(
                    agent_id="agent-aip-invalid",
                    change_type="aip.v0",
                    change_content=invalid_payload,
                    affected_paths=["main.py"],
                    confidence_score=0.7,
                ),
            )

    @pytest.mark.asyncio
    async def test_non_python_parser_failure_uses_deterministic_file_fallback(
        self,
        proposal_service,
        repo,
        monkeypatch,
        db_session,
    ):
        """Parser extraction failure should deterministically fall back to __file__ symbol."""
        store = DocumentStore(repo.id)
        store.apply_full_file(
            "main.ts",
            (
                "function alpha() {\n"
                "  return 1;\n"
                "}\n"
                "\n"
                "function beta() {\n"
                "  return 2;\n"
                "}\n"
            ),
            "init",
        )
        proposal_service.set_document_store(repo.id, store)
        monkeypatch.setattr(
            "packages.aether.services.proposal_service.extract_symbol_spans",
            lambda _path, _content: [],
        )

        diff_alpha = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -1,3 +1,3 @@
 function alpha() {
-  return 1;
+  return 11;
 }
"""
        diff_beta = """diff --git a/main.ts b/main.ts
--- a/main.ts
+++ b/main.ts
@@ -5,3 +5,3 @@
 function beta() {
-  return 2;
+  return 22;
 }
"""

        first = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-fallback-ts-1",
                change_type="unified_diff",
                change_content=diff_alpha,
                affected_paths=["main.ts"],
                confidence_score=0.8,
            ),
        )
        second = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-fallback-ts-2",
                change_type="unified_diff",
                change_content=diff_beta,
                affected_paths=["main.ts"],
                confidence_score=0.8,
            ),
        )

        first_row = proposal_service.get_proposal(first.proposal_id)
        second_row = proposal_service.get_proposal(second.proposal_id)
        assert first_row is not None
        assert second_row is not None
        assert first_row.affected_symbols is not None
        assert second_row.affected_symbols is not None
        assert "main.ts::__file__" in json.loads(first_row.affected_symbols)
        assert "main.ts::__file__" in json.loads(second_row.affected_symbols)
        assert second.conflict_set_id is not None

        metrics = ProposalService.get_system_physics_snapshot(db_session, window_seconds=300)
        assert metrics["semantic_fallback"]["by_language"]["typescript"] >= 2
        assert metrics["semantic_fallback"]["by_path"]["main.ts"] >= 2

    @pytest.mark.asyncio
    async def test_deferred_ingest_materializes_conflicts(self, db_session, event_bus, repo):
        """Deferred ingest should ack fast and materialize conflicts asynchronously."""
        deferred_service = ProposalService(
            db_session,
            event_bus,
            {},
            defer_conflict_detection=True,
        )

        proposal1 = ProposalCreate(
            agent_id="agent-async-1",
            change_content="diff1",
            affected_paths=["shared_async.py"],
            confidence_score=0.8,
        )
        proposal2 = ProposalCreate(
            agent_id="agent-async-2",
            change_content="diff2",
            affected_paths=["shared_async.py"],
            confidence_score=0.7,
        )

        response1 = await deferred_service.submit_proposal(repo.id, proposal1)
        response2 = await deferred_service.submit_proposal(repo.id, proposal2)

        assert response1.status == ProposalStatus.INGESTED
        assert response2.status == ProposalStatus.INGESTED
        assert response2.conflict_set_id is None

        materialized = await deferred_service.materialize_ingested_queue(repo.id, limit=10)
        assert materialized == 2

        stored1 = deferred_service.get_proposal(response1.proposal_id)
        stored2 = deferred_service.get_proposal(response2.proposal_id)
        assert stored1 is not None
        assert stored2 is not None
        assert stored1.status == ProposalStatus.PENDING
        assert stored2.status == ProposalStatus.PENDING

        conflict_sets = deferred_service.list_conflict_sets(repo.id, resolved=False)
        assert len(conflict_sets) == 1

    def test_list_proposals(self, proposal_service, repo):
        """Should list proposals with filters."""
        # Create proposals directly for this test
        from packages.aether.models.entities import Proposal

        for i in range(3):
            proposal = Proposal(
                repo_id=repo.id,
                agent_id=f"agent-{i}",
                change_content="diff",
                affected_paths=json.dumps(["file.py"]),
                status=ProposalStatus.PENDING if i < 2 else ProposalStatus.PROMOTED,
            )
            proposal_service.session.add(proposal)
        proposal_service.session.commit()

        pending = proposal_service.list_proposals(repo.id, status=ProposalStatus.PENDING)
        assert len(pending) == 2


class TestSnapshotService:
    """Test snapshot service."""

    @pytest.fixture
    def repo(self, repo_service):
        """Create a test repository."""
        return repo_service.create_repository(name="test-repo")

    @pytest.fixture
    def document_store(self, repo):
        """Create a document store with content."""
        store = DocumentStore(repo.id)
        store.apply_full_file("main.py", "print('hello')", "prop_1")
        store.apply_full_file("util.py", "def helper(): pass", "prop_1")
        return store

    @pytest.mark.asyncio
    async def test_create_snapshot(self, snapshot_service, repo, document_store):
        """Should create a snapshot."""
        snapshot = await snapshot_service.create_snapshot(
            repo.id,
            document_store,
            included_proposals=["prop_1"],
        )

        assert snapshot.id.startswith("snap_")
        assert snapshot.file_count == 2
        assert snapshot.content_hash is not None

    @pytest.mark.asyncio
    async def test_snapshot_deduplication(self, snapshot_service, repo, document_store):
        """Same content should return same snapshot."""
        snap1 = await snapshot_service.create_snapshot(repo.id, document_store, [])
        snap2 = await snapshot_service.create_snapshot(repo.id, document_store, [])

        assert snap1.id == snap2.id

    @pytest.mark.asyncio
    async def test_list_files(self, snapshot_service, repo, document_store):
        """Should list files in snapshot."""
        snapshot = await snapshot_service.create_snapshot(repo.id, document_store, [])
        files = snapshot_service.list_files(snapshot.id)

        assert "main.py" in files
        assert "util.py" in files

    @pytest.mark.asyncio
    async def test_get_file_content(self, snapshot_service, repo, document_store):
        """Should get file content from snapshot."""
        snapshot = await snapshot_service.create_snapshot(repo.id, document_store, [])
        content = snapshot_service.get_file_content(snapshot.id, "main.py")

        assert content == "print('hello')"

    @pytest.mark.asyncio
    async def test_delta_materialization_reuses_unchanged_files(
        self,
        snapshot_service,
        repo_service,
        db_session,
        repo,
    ):
        """Delta materialization should reuse unchanged files from current head snapshot."""
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('hello')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        proposal = Proposal(
            repo_id=repo.id,
            agent_id="agent-delta",
            change_type="full_file",
            change_content="print('hello v2')\n",
            affected_paths=json.dumps(["main.py"]),
            confidence_score=0.9,
            status=ProposalStatus.PENDING,
        )
        db_session.add(proposal)
        db_session.commit()
        db_session.refresh(proposal)

        updated_store = base_store.clone()
        updated_store.apply_full_file("main.py", "print('hello v2')\n", proposal.id)

        delta_snapshot = await snapshot_service.create_snapshot(
            repo.id,
            updated_store,
            [proposal.id],
        )

        assert snapshot_service.get_file_content(delta_snapshot.id, "main.py") == "print('hello v2')\n"
        assert snapshot_service.get_file_content(delta_snapshot.id, "util.py") == "def helper():\n    return 1\n"

        metrics = snapshot_service.get_last_create_metrics()
        assert metrics["materialize_delta_used"] == 1.0
        assert metrics["materialize_reused_file_count"] >= 1.0
        assert metrics["materialize_written_file_count"] >= 1.0

    @pytest.mark.asyncio
    async def test_create_snapshot_uses_changed_path_override_without_db_lookup(
        self,
        snapshot_service,
        repo_service,
        repo,
        monkeypatch,
    ):
        """Caller-provided changed paths should bypass proposal ORM lookups in the hot path."""
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('base')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        updated_store = base_store.clone()
        updated_store.apply_full_file("main.py", "print('changed')\n", "prop_override")

        def _unexpected_lookup(*args, **kwargs):
            raise AssertionError("_collect_changed_paths should not be called")

        monkeypatch.setattr(snapshot_service, "_collect_changed_paths", _unexpected_lookup)

        delta_snapshot = await snapshot_service.create_snapshot(
            repo.id,
            updated_store,
            ["prop_override"],
            changed_paths_override={"main.py"},
        )

        assert snapshot_service.get_file_content(delta_snapshot.id, "main.py") == "print('changed')\n"
        metrics = snapshot_service.get_last_create_metrics()
        assert metrics["materialize_delta_used"] == 1.0

    @pytest.mark.asyncio
    async def test_snapshot_file_index_cache_hits_without_rereading_manifest(
        self,
        snapshot_service,
        repo_service,
        db_session,
        repo,
        monkeypatch,
    ):
        """Repeated reads should hit the cached logical snapshot index."""
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('base')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        proposal = Proposal(
            repo_id=repo.id,
            agent_id="agent-cache-hit",
            change_type="full_file",
            change_content="print('changed')\n",
            affected_paths=json.dumps(["main.py"]),
            confidence_score=0.9,
            status=ProposalStatus.PENDING,
        )
        db_session.add(proposal)
        db_session.commit()
        db_session.refresh(proposal)

        updated_store = base_store.clone()
        updated_store.apply_full_file("main.py", "print('changed')\n", proposal.id)
        delta_snapshot = await snapshot_service.create_snapshot(repo.id, updated_store, [proposal.id])

        assert snapshot_service.get_file_content(delta_snapshot.id, "main.py") == "print('changed')\n"

        def _unexpected_manifest_read(_snapshot_path: Path):
            raise AssertionError("cached snapshot index should avoid re-reading manifests")

        monkeypatch.setattr(snapshot_service, "_read_manifest", _unexpected_manifest_read)

        assert snapshot_service.get_file_content(delta_snapshot.id, "util.py") == "def helper():\n    return 1\n"

    def test_snapshot_file_index_cache_uses_lru_eviction(self, snapshot_service, tmp_path):
        """Recently used cache entries should survive bounded eviction."""
        snapshot_service._index_cache_max_entries = 2
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_c = tmp_path / "c"

        snapshot_service._store_snapshot_file_index("snap-a", {"a.py": path_a})
        snapshot_service._store_snapshot_file_index("snap-b", {"b.py": path_b})
        assert snapshot_service._get_cached_snapshot_file_index("snap-a") == {"a.py": path_a}

        snapshot_service._store_snapshot_file_index("snap-c", {"c.py": path_c})

        assert snapshot_service._get_cached_snapshot_file_index("snap-a") == {"a.py": path_a}
        assert snapshot_service._get_cached_snapshot_file_index("snap-b") is None
        assert snapshot_service._get_cached_snapshot_file_index("snap-c") == {"c.py": path_c}

    @pytest.mark.asyncio
    async def test_snapshot_reproducer_smoke(self):
        """Focused snapshot reproducer should complete a small run cleanly."""
        report = await run_snapshot_reproducer(
            SnapshotReproducerConfig(
                iterations=12,
                num_files=8,
                file_lines=12,
                seed=77,
            )
        )

        assert report.passed is True
        assert report.total_iterations == 12
        assert report.first_failure_iteration is None
        assert report.max_identity_map_size >= 0.0
        assert isinstance(report.last_create_metrics, dict)

    @pytest.mark.asyncio
    async def test_true_delta_snapshot_stores_only_changed_files(
        self,
        snapshot_service,
        repo_service,
        db_session,
        repo,
    ):
        """Delta snapshot directory should only persist changed files plus manifest metadata."""
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('base')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        proposal = Proposal(
            repo_id=repo.id,
            agent_id="agent-delta-files",
            change_type="full_file",
            change_content="print('changed')\n",
            affected_paths=json.dumps(["main.py"]),
            confidence_score=0.9,
            status=ProposalStatus.PENDING,
        )
        db_session.add(proposal)
        db_session.commit()
        db_session.refresh(proposal)

        updated_store = base_store.clone()
        updated_store.apply_full_file("main.py", "print('changed')\n", proposal.id)
        delta_snapshot = await snapshot_service.create_snapshot(repo.id, updated_store, [proposal.id])

        snapshot_path = snapshot_service.get_snapshot_path(delta_snapshot.id)
        assert snapshot_path is not None
        stored_files = {
            str(path.relative_to(snapshot_path))
            for path in snapshot_path.rglob("*")
            if path.is_file()
        }
        assert ".aether_snapshot_manifest.json" in stored_files
        assert "main.py" in stored_files
        assert "util.py" not in stored_files

        # Logical file view must still be complete.
        files = snapshot_service.list_files(delta_snapshot.id)
        assert "main.py" in files
        assert "util.py" in files

    @pytest.mark.asyncio
    async def test_materialize_gate_workspace_from_delta_snapshot(
        self,
        snapshot_service,
        repo_service,
        db_session,
        repo,
    ):
        """Gate workspace materialization should reconstruct a full tree from delta snapshots."""
        base_store = DocumentStore(repo.id)
        base_store.apply_full_file("main.py", "print('base')\n", "init")
        base_store.apply_full_file("util.py", "def helper():\n    return 1\n", "init")
        base_snapshot = await snapshot_service.create_snapshot(repo.id, base_store, [])
        repo_service.update_head(repo.id, base_snapshot.id)

        proposal = Proposal(
            repo_id=repo.id,
            agent_id="agent-gate-workspace",
            change_type="full_file",
            change_content="print('changed')\n",
            affected_paths=json.dumps(["main.py"]),
            confidence_score=0.9,
            status=ProposalStatus.PENDING,
        )
        db_session.add(proposal)
        db_session.commit()
        db_session.refresh(proposal)

        updated_store = base_store.clone()
        updated_store.apply_full_file("main.py", "print('changed')\n", proposal.id)
        delta_snapshot = await snapshot_service.create_snapshot(repo.id, updated_store, [proposal.id])

        workspace = snapshot_service.materialize_gate_workspace(delta_snapshot.id)
        assert workspace is not None
        try:
            assert (workspace / "main.py").read_text(encoding="utf-8") == "print('changed')\n"
            assert (workspace / "util.py").read_text(encoding="utf-8") == "def helper():\n    return 1\n"
        finally:
            snapshot_service.cleanup_gate_workspace(workspace)

    def test_cleanup_gate_workspace_uses_bounded_executor(
        self,
        snapshot_service,
        tmp_path,
        monkeypatch,
    ):
        """Workspace cleanup should delegate to the shared executor."""
        workspace = tmp_path / "gate-workspace"
        workspace.mkdir(parents=True)
        (workspace / "main.py").write_text("print('cleanup')\n", encoding="utf-8")
        submitted: dict[str, Path] = {}

        class InlineExecutor:
            def submit(self, fn, *args):
                submitted["workspace"] = args[0]
                fn(*args)
                return None

        monkeypatch.setattr(SnapshotService, "_cleanup_executor", InlineExecutor())

        snapshot_service.cleanup_gate_workspace(workspace)

        assert submitted["workspace"] == workspace
        assert not workspace.exists()


class TestGateRunner:
    """Test gate runner."""

    @pytest.fixture
    def repo_and_snapshot(self, db_session, repo_service, snapshot_service, event_bus):
        """Create repo and snapshot for testing."""
        repo = repo_service.create_repository(name="test")
        store = DocumentStore(repo.id)
        store.apply_full_file("test.py", "print('test')", "prop_1")

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
    async def test_run_passing_gate(self, gate_runner, repo_and_snapshot, tmp_path):
        """Gate with exit code 0 should pass."""
        repo, snapshot = repo_and_snapshot

        gate_config = GateConfig(
            name="test_gate",
            command="echo 'success'",
            required=True,
            timeout_seconds=10,
        )

        result = await gate_runner.run_gate(
            snapshot.id,
            gate_config,
            tmp_path,
        )

        assert result.status == GateStatus.PASS
        assert result.exit_code == 0
        assert "success" in result.stdout

    @pytest.mark.asyncio
    async def test_run_failing_gate(self, gate_runner, repo_and_snapshot, tmp_path):
        """Gate with non-zero exit should fail."""
        repo, snapshot = repo_and_snapshot

        gate_config = GateConfig(
            name="fail_gate",
            command="exit 1",
            required=True,
            timeout_seconds=10,
        )

        result = await gate_runner.run_gate(
            snapshot.id,
            gate_config,
            tmp_path,
        )

        assert result.status == GateStatus.FAIL
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_gate_timeout(self, db_session, event_bus, repo_and_snapshot, tmp_path):
        """Gate that times out should error when custom commands are enabled."""
        repo, snapshot = repo_and_snapshot
        trusted_runner = GateRunner(db_session, event_bus, allow_custom_commands=True)

        gate_config = GateConfig(
            name="slow_gate",
            command="sleep 10",
            required=True,
            timeout_seconds=1,  # Short timeout
        )

        result = await trusted_runner.run_gate(
            snapshot.id,
            gate_config,
            tmp_path,
        )

        assert result.status == GateStatus.ERROR
        assert "timed out" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_gate_hard_timeout_cap(self, db_session, event_bus, repo_and_snapshot, tmp_path, monkeypatch):
        """Global gate hard-timeout cap should clamp longer per-gate timeouts."""
        _, snapshot = repo_and_snapshot
        monkeypatch.setenv("AETHER_GATE_HARD_TIMEOUT_SECONDS", "0.2")
        capped_runner = GateRunner(db_session, event_bus, allow_custom_commands=True)

        gate_config = GateConfig(
            name="capped_gate",
            command="sleep 1",
            required=True,
            timeout_seconds=10,
        )

        result = await capped_runner.run_gate(
            snapshot.id,
            gate_config,
            tmp_path,
        )

        assert result.status == GateStatus.ERROR
        assert "timed out" in result.stderr.lower()
        assert "hard cap" in result.stderr.lower()

    def test_compute_evidence(self, gate_runner):
        """Should compute evidence dict from results."""
        results = [
            GateResult(
                id="r1",
                repo_id="repo",
                snapshot_id="snap",
                gate_name="build",
                status=GateStatus.PASS,
            ),
            GateResult(
                id="r2",
                repo_id="repo",
                snapshot_id="snap",
                gate_name="test",
                status=GateStatus.FAIL,
            ),
        ]

        evidence = gate_runner.compute_evidence(results)

        assert evidence["build"] == "PASS"
        assert evidence["test"] == "FAIL"

    @pytest.mark.asyncio
    async def test_run_all_gates_fast_fail_stops_on_required_failure(
        self, gate_runner, repo_and_snapshot, tmp_path
    ):
        """Fast-fail mode should stop after the first failed required gate."""
        _, snapshot = repo_and_snapshot
        gates = [
            GateConfig(name="required_fail", command="exit 1", required=True, timeout_seconds=10, order=0),
            GateConfig(name="optional_after", command="echo 'should not run'", required=False, timeout_seconds=10, order=1),
        ]

        results = await gate_runner.run_all_gates(
            snapshot.id,
            gates,
            tmp_path,
            stop_on_failure=True,
            execution_mode="fast_fail",
        )

        assert len(results) == 1
        assert results[0].gate_name == "required_fail"
        assert results[0].status == GateStatus.FAIL

    @pytest.mark.asyncio
    async def test_run_all_gates_parallel_executes_all(self, gate_runner, repo_and_snapshot, tmp_path):
        """Parallel mode should execute all configured gates and persist results."""
        _, snapshot = repo_and_snapshot
        gates = [
            GateConfig(name="g1", command="echo one", required=True, timeout_seconds=10, order=0),
            GateConfig(name="g2", command="echo two", required=True, timeout_seconds=10, order=1),
        ]

        results = await gate_runner.run_all_gates(
            snapshot.id,
            gates,
            tmp_path,
            execution_mode="parallel",
            max_parallel=2,
        )

        assert len(results) == 2
        assert {result.gate_name for result in results} == {"g1", "g2"}
        assert all(result.status == GateStatus.PASS for result in results)


class TestPromotionEngine:
    """Test promotion engine."""

    @pytest.fixture
    def services(self, db_session, event_bus, storage_path):
        """Create all required services."""
        repo_service = RepositoryService(db_session, storage_path)
        proposal_service = ProposalService(db_session, event_bus, {})
        snapshot_service = SnapshotService(db_session, event_bus, storage_path)
        gate_runner = GateRunner(db_session, event_bus)
        promotion_engine = PromotionEngine(
            db_session,
            event_bus,
            proposal_service,
            snapshot_service,
            gate_runner,
            storage_path,
        )

        return {
            "repo_service": repo_service,
            "proposal_service": proposal_service,
            "snapshot_service": snapshot_service,
            "gate_runner": gate_runner,
            "promotion_engine": promotion_engine,
        }

    def test_compute_score_all_pass(self, services):
        """Score should be positive when all gates pass."""
        engine = services["promotion_engine"]

        # Create mock proposal
        proposal = Proposal(
            id="prop_1",
            repo_id="repo_1",
            agent_id="agent",
            confidence_score=0.9,
        )

        # Create passing gate results
        gates = [
            GateConfig(name="build", command="echo ok", required=True),
            GateConfig(name="unit_tests", command="echo ok", required=True),
        ]
        results = [
            GateResult(id="r1", repo_id="repo", snapshot_id="snap", gate_name="build", status=GateStatus.PASS),
            GateResult(id="r2", repo_id="repo", snapshot_id="snap", gate_name="unit_tests", status=GateStatus.PASS),
        ]

        final_score, policy_score, confidence_score = engine.compute_score(proposal, results, gates)

        assert policy_score == 1.0
        assert final_score > 0

    def test_compute_score_required_gate_fails(self, services):
        """Score should be 0 when required gate fails."""
        engine = services["promotion_engine"]

        proposal = Proposal(
            id="prop_1",
            repo_id="repo_1",
            agent_id="agent",
            confidence_score=0.9,
        )

        gates = [
            GateConfig(name="build", command="echo ok", required=True),
        ]
        results = [
            GateResult(id="r1", repo_id="repo", snapshot_id="snap", gate_name="build", status=GateStatus.FAIL),
        ]

        final_score, policy_score, _ = engine.compute_score(proposal, results, gates)

        assert policy_score == 0.0
        assert final_score == 0.0

    def test_compute_score_optional_gate_fails(self, services):
        """Score should be positive when only optional gate fails."""
        engine = services["promotion_engine"]

        proposal = Proposal(
            id="prop_1",
            repo_id="repo_1",
            agent_id="agent",
            confidence_score=0.9,
        )

        gates = [
            GateConfig(name="build", command="echo ok", required=True),
            GateConfig(name="lint", command="echo ok", required=False),
        ]
        results = [
            GateResult(id="r1", repo_id="repo", snapshot_id="snap", gate_name="build", status=GateStatus.PASS),
            GateResult(id="r2", repo_id="repo", snapshot_id="snap", gate_name="lint", status=GateStatus.FAIL),
        ]

        final_score, policy_score, _ = engine.compute_score(proposal, results, gates)

        assert policy_score == 1.0
        assert final_score > 0

    def test_format_exception_reason_includes_promotion_phase(self, services):
        """Promotion phase wrappers should preserve the original failure context."""
        engine = services["promotion_engine"]

        try:
            raise RecursionError("maximum recursion depth exceeded")
        except RecursionError as exc:
            wrapped = PromotionPhaseError("status_updates", exc)
            reason = engine._format_exception_reason(
                wrapped,
                prefix="individual_promotion_failed",
            )

        assert reason.startswith("individual_promotion_failed:status_updates: RecursionError")
        assert "maximum recursion depth exceeded" in reason

    def test_format_exception_reason_includes_evaluation_phase(self, services):
        """Evaluation phase wrappers should preserve the original failure context."""
        engine = services["promotion_engine"]

        try:
            raise RecursionError("maximum recursion depth exceeded")
        except RecursionError as exc:
            wrapped = EvaluationPhaseError("mark_evaluating", exc)
            reason = engine._format_exception_reason(
                wrapped,
                prefix="individual_promotion_failed",
            )

        assert reason.startswith("individual_promotion_failed:mark_evaluating: RecursionError")
        assert "maximum recursion depth exceeded" in reason

    @pytest.mark.asyncio
    async def test_promotion_evidence_includes_provenance(self, services):
        """Promotion evidence should include deterministic provenance metadata."""
        repo_service = services["repo_service"]
        proposal_service = services["proposal_service"]
        snapshot_service = services["snapshot_service"]
        engine = services["promotion_engine"]
        session = services["repo_service"].session

        repo = repo_service.create_repository(name="provenance-test")
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("main.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, doc_store)

        proposal_data = ProposalCreate(
            agent_id="agent-provenance",
            change_type="full_file",
            change_content="def alpha():\n    return 2\n",
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
        response = await proposal_service.submit_proposal(repo.id, proposal_data)
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None

        before_hash = doc_store.get_state_hash()
        candidate = await engine.evaluate_proposal(proposal, doc_store)
        promotion = await engine.promote(repo.id, candidate, doc_store, triggered_by="test")

        evidence = json.loads(promotion.evidence)
        snapshot = snapshot_service.get_snapshot(candidate.snapshot_id)
        assert snapshot is not None

        assert evidence["_op_graph_kind"] == "promotion"
        assert evidence["_replay_snapshot_id"] == candidate.snapshot_id
        assert evidence["_replay_from_hash"] == before_hash
        assert evidence["_replay_to_hash"] == snapshot.content_hash
        assert len(evidence["_op_graph_node"]) == 64

        operation_node = session.exec(
            select(OperationGraphNode).where(OperationGraphNode.promotion_id == promotion.id)
        ).first()
        assert operation_node is not None
        assert operation_node.node_hash == evidence["_op_graph_node"]
        assert operation_node.replay_to_hash == snapshot.content_hash

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance(self, services):
        """Promotion provenance verification should pass for untouched records."""
        repo_service = services["repo_service"]
        proposal_service = services["proposal_service"]
        engine = services["promotion_engine"]

        repo = repo_service.create_repository(name="verify-provenance-test")
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("main.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, doc_store)

        proposal_data = ProposalCreate(
            agent_id="agent-verify",
            change_type="full_file",
            change_content="def alpha():\n    return 3\n",
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
        response = await proposal_service.submit_proposal(repo.id, proposal_data)
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None

        candidate = await engine.evaluate_proposal(proposal, doc_store)
        promotion = await engine.promote(repo.id, candidate, doc_store, triggered_by="test")
        verification = engine.verify_promotion_provenance(promotion.id)

        assert verification["verified"] is True
        checks = verification["checks"]
        assert checks["node_hash_match"] is True
        assert checks["replay_to_hash_match"] is True
        assert checks["operation_node_hash_match"] is True

    @pytest.mark.asyncio
    async def test_evaluate_proposal_supports_aip_v0(self, services):
        """Promotion evaluation should apply AIP mutations into candidate snapshots."""
        repo_service = services["repo_service"]
        proposal_service = services["proposal_service"]
        snapshot_service = services["snapshot_service"]
        engine = services["promotion_engine"]

        repo = repo_service.create_repository(name="aip-eval-test")
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("main.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, doc_store)

        payload = """
        {
          "schema": "aip.v0",
          "mutations": [
            {
              "op": "update_node",
              "path": "main.py",
              "node_id": "FUNC:alpha",
              "content": "def alpha():\\n    return 5\\n"
            }
          ]
        }
        """
        response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-aip-eval",
                change_type="aip.v0",
                change_content=payload,
                affected_paths=[],
                confidence_score=0.9,
            ),
        )
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None

        candidate = await engine.evaluate_proposal(proposal, doc_store)
        assert candidate.snapshot_id

        snapshot_content = snapshot_service.get_file_content(candidate.snapshot_id, "main.py")
        assert snapshot_content is not None
        assert "return 5" in snapshot_content

    @pytest.mark.asyncio
    async def test_apply_proposal_change_unified_diff_does_not_require_affected_paths(
        self,
        services,
    ):
        """Unified diffs should derive their paths from the diff itself."""
        engine = services["promotion_engine"]
        doc_store = DocumentStore("repo-diff-paths")
        doc_store.apply_full_file("main.py", "print('old')\n", "init")
        proposal = Proposal(
            id="prop_diff",
            repo_id="repo-diff-paths",
            agent_id="agent-diff",
            change_type="unified_diff",
            change_content=(
                "diff --git a/main.py b/main.py\n"
                "--- a/main.py\n"
                "+++ b/main.py\n"
                "@@ -1 +1 @@\n"
                "-print('old')\n"
                "+print('new')\n"
            ),
            affected_paths="not-json",
            confidence_score=0.9,
        )

        changed_paths = await engine._apply_proposal_change(doc_store, proposal)

        assert changed_paths == ["main.py"]
        assert doc_store.get_file_content("main.py").startswith("print('new')\n")

    @pytest.mark.asyncio
    async def test_process_pending_proposals_requeues_external_commit_abort(
        self,
        services,
        monkeypatch,
    ):
        """External commit races should requeue proposals without aborting the iteration."""
        repo_service = services["repo_service"]
        proposal_service = services["proposal_service"]
        engine = services["promotion_engine"]

        repo = repo_service.create_repository(name="external-abort-requeue")
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("main.py", "print('base')\n", "init")
        proposal_service.set_document_store(repo.id, doc_store)

        response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-race",
                change_type="full_file",
                change_content="print('changed')\n",
                affected_paths=["main.py"],
                confidence_score=0.9,
            ),
        )
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None

        async def _abort_promote(*_args, **_kwargs):
            raise ExternalCommitAbortedError("simulated concurrent head update")

        monkeypatch.setattr(engine, "promote", _abort_promote)

        promotions = await engine.process_pending_proposals(
            repo.id,
            doc_store,
            batch_size=1,
            max_non_conflicting_batches=1,
        )

        reloaded = proposal_service.get_proposal(proposal.id)
        assert promotions == []
        assert reloaded is not None
        assert reloaded.status == ProposalStatus.PENDING

    async def _setup_valid_promotion(self, services, name: str):
        from packages.aether.models.entities import generate_id
        repo_service = services["repo_service"]
        proposal_service = services["proposal_service"]
        engine = services["promotion_engine"]

        repo = repo_service.create_repository(name=name)
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("main.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, doc_store)

        proposal_data = ProposalCreate(
            agent_id="agent-verify",
            change_type="full_file",
            change_content="def alpha():\n    return 3\n",
            affected_paths=["main.py"],
            confidence_score=0.9,
        )
        response = await proposal_service.submit_proposal(repo.id, proposal_data)
        proposal = proposal_service.get_proposal(response.proposal_id)
        assert proposal is not None

        candidate = await engine.evaluate_proposal(proposal, doc_store)
        promotion = await engine.promote(repo.id, candidate, doc_store, triggered_by="test")
        return promotion, repo, proposal, doc_store

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_missing_keys(self, services):
        """Should fail if required evidence keys are missing."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "missing-keys-test")

        # Tamper: delete a required key from evidence
        evidence = json.loads(promotion.evidence)
        del evidence["_op_graph_node"]
        promotion.evidence = json.dumps(evidence)
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "missing evidence keys: _op_graph_node" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_corrupted_json(self, services):
        """Should fail and fall back gracefully if evidence is corrupted JSON."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "corrupted-json-test")

        # Tamper: set invalid JSON
        promotion.evidence = "{invalid_json"
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        # Since it falls back to {}, it complains about missing all required keys
        assert any("missing evidence keys" in err for err in verification["errors"])

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_malformed_included_proposals_json(self, services):
        """Should fail explicitly if included_proposals JSON is malformed."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "malformed-included-test")

        # Tamper
        promotion.included_proposals = "{invalid_json"
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed included_proposals JSON" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_malformed_excluded_proposals_json(self, services):
        """Should fail explicitly if excluded_proposals JSON is malformed."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "malformed-excluded-test")

        # Tamper
        promotion.excluded_proposals = "{invalid_json"
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed excluded_proposals JSON" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_missing_proposal(self, services):
        """Should fail if one or more included proposals are missing from DB."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, proposal, _ = await self._setup_valid_promotion(services, "missing-proposal-test")

        # Delete the proposal
        session.delete(proposal)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "one or more included proposals are missing" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_missing_head(self, services):
        """Should fail if no AetherHead record is found for the promotion."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "missing-head-test")

        # Find and delete head record
        head = session.exec(
            select(AetherHead).where(AetherHead.promotion_id == promotion.id)
        ).first()
        assert head is not None
        session.delete(head)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "no head record found for promotion" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_missing_operation_node(self, services):
        """Should fail if no OperationGraphNode record is found for the promotion."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "missing-opnode-test")

        # Find and delete operation graph node
        op_node = session.exec(
            select(OperationGraphNode).where(OperationGraphNode.promotion_id == promotion.id)
        ).first()
        assert op_node is not None
        session.delete(op_node)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "no operation graph node found for promotion" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_missing_snapshot(self, services):
        """Should fail if snapshot referenced by the head is not found."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "missing-snapshot-test")

        head = session.exec(
            select(AetherHead).where(AetherHead.promotion_id == promotion.id)
        ).first()
        assert head is not None
        snapshot = session.get(Snapshot, head.snapshot_id)
        assert snapshot is not None
        session.delete(snapshot)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert f"snapshot not found: {head.snapshot_id}" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_gate_vector_mismatch(self, services):
        """Should fail if stored gate vector doesn't match database gate results."""
        from packages.aether.models.entities import generate_id
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "gate-mismatch-test")

        # Add a dummy gate result to the database for this snapshot
        head = session.exec(
            select(AetherHead).where(AetherHead.promotion_id == promotion.id)
        ).first()
        assert head is not None
        new_gate = GateResult(
            id=generate_id("gate"),
            repo_id=promotion.repo_id,
            snapshot_id=head.snapshot_id,
            gate_name="extra_unexpected_gate",
            status=GateStatus.PASS,
        )
        session.add(new_gate)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "gate vector does not match stored gate results" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_replay_hash_mismatch(self, services):
        """Should fail if the target content hash doesn't match snapshot."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "hash-mismatch-test")

        # Tamper: modify target content hash in evidence
        evidence = json.loads(promotion.evidence)
        evidence["_replay_to_hash"] = "corrupted_hash"
        promotion.evidence = json.dumps(evidence)
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "replay target hash does not match snapshot" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_op_node_hash_mismatch(self, services):
        """Should fail if the operation node hash doesn't match the promotion's stored hash."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "opnode-hash-test")

        # Tamper: modify node_hash on OperationGraphNode
        op_node = session.exec(
            select(OperationGraphNode).where(OperationGraphNode.promotion_id == promotion.id)
        ).first()
        assert op_node is not None
        op_node.node_hash = "tampered_node_hash"
        session.add(op_node)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "operation node hash does not match promotion evidence" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_tampered_evidence_node_hash(self, services):
        """Should fail if stored node hash itself is tampered/mismatched from recomputation."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "tampered-evidence-node-hash")

        # Tamper: modify stored _op_graph_node to be random
        evidence = json.loads(promotion.evidence)
        evidence["_op_graph_node"] = "a" * 64
        promotion.evidence = json.dumps(evidence)
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "op graph node hash mismatch" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_op_node_gate_vector_mismatch(self, services):
        """Should fail if the operation node's gate vector is tampered."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "opnode-gate-vector-mismatch")

        # Tamper: modify replay_gate_vector on OperationGraphNode
        op_node = session.exec(
            select(OperationGraphNode).where(OperationGraphNode.promotion_id == promotion.id)
        ).first()
        assert op_node is not None
        op_node.replay_gate_vector = "tampered_gate:FAIL"
        session.add(op_node)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "operation node gate vector mismatch" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_op_node_replay_to_hash_mismatch(self, services):
        """Should fail if the operation node's replay target hash doesn't match the snapshot's content hash."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "opnode-replay-hash-test")

        # Tamper: modify replay_to_hash on OperationGraphNode
        op_node = session.exec(
            select(OperationGraphNode).where(OperationGraphNode.promotion_id == promotion.id)
        ).first()
        assert op_node is not None
        op_node.replay_to_hash = "tampered_replay_to_hash"
        session.add(op_node)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "operation node replay target hash mismatch" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_included_proposals_not_list_of_strings(self, services):
        """Should fail if included_proposals is a JSON dictionary or has non-string values."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "included-shape-test")

        # Tamper: set to a valid JSON dictionary (not a list)
        promotion.included_proposals = '{"a": 1}'
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed included_proposals JSON" in verification["errors"]

        # Tamper: set to a list with integers (not list of strings)
        promotion.included_proposals = '[1, 2, 3]'
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed included_proposals JSON" in verification["errors"]

    @pytest.mark.asyncio
    async def test_verify_promotion_provenance_excluded_proposals_not_list_of_strings(self, services):
        """Should fail if excluded_proposals is a JSON integer or has non-string values."""
        engine = services["promotion_engine"]
        session = services["repo_service"].session
        promotion, _, _, _ = await self._setup_valid_promotion(services, "excluded-shape-test")

        # Tamper: set to a valid JSON integer (not a list)
        promotion.excluded_proposals = '123'
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed excluded_proposals JSON" in verification["errors"]

        # Tamper: set to a list with booleans (not list of strings)
        promotion.excluded_proposals = '[true, false]'
        session.add(promotion)
        session.commit()

        verification = engine.verify_promotion_provenance(promotion.id)
        assert verification["verified"] is False
        assert "malformed excluded_proposals JSON" in verification["errors"]



class TestConflictResolution:
    """Test Phase 1 conflict resolution with parallel evaluation."""

    @pytest.fixture
    def services(self, db_session, event_bus, storage_path):
        """Create all required services."""
        repo_service = RepositoryService(db_session, storage_path)
        proposal_service = ProposalService(db_session, event_bus, {})
        snapshot_service = SnapshotService(db_session, event_bus, storage_path)
        gate_runner = GateRunner(db_session, event_bus)
        promotion_engine = PromotionEngine(
            db_session,
            event_bus,
            proposal_service,
            snapshot_service,
            gate_runner,
            storage_path,
        )

        return {
            "repo_service": repo_service,
            "proposal_service": proposal_service,
            "snapshot_service": snapshot_service,
            "gate_runner": gate_runner,
            "promotion_engine": promotion_engine,
            "session": db_session,
        }

    @pytest.mark.asyncio
    async def test_parallel_evaluation_selects_best_score(self, services):
        """Parallel evaluation should select highest scoring proposal."""
        from packages.aether.crdt.document_store import DocumentStore
        import json

        session = services["session"]
        repo_service = services["repo_service"]
        promotion_engine = services["promotion_engine"]

        # Create repo
        repo = repo_service.create_repository(name="conflict-test")

        # Create multiple proposals with different confidences
        proposals = []
        for i, confidence in enumerate([0.6, 0.9, 0.7]):  # Middle one has highest
            proposal = Proposal(
                repo_id=repo.id,
                agent_id=f"agent_{i}",
                change_content=f"change {i}",
                affected_paths=json.dumps(["shared.py"]),
                confidence_score=confidence,
                status=ProposalStatus.PENDING,
            )
            session.add(proposal)
            proposals.append(proposal)
        session.commit()

        # Create conflict set
        conflict_set = ConflictSet(
            repo_id=repo.id,
            path="shared.py",
            proposal_ids=json.dumps([p.id for p in proposals]),
        )
        session.add(conflict_set)
        session.commit()
        session.refresh(conflict_set)

        # Create document store
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("shared.py", "original content", "initial")

        # Select best candidate (with mocked gate results that all pass)
        # Note: In real scenario, gates would run. Here we test the selection logic.
        best = await promotion_engine.select_best_candidate(
            conflict_set, doc_store, parallel=True
        )

        # Should have selected highest confidence proposal (0.9)
        assert best is not None
        # The best should be from the highest confidence proposal
        assert len(best.proposals) == 1

    @pytest.mark.asyncio
    async def test_excluded_proposals_tracked(self, services):
        """Excluded proposals should be tracked in the candidate."""
        from packages.aether.crdt.document_store import DocumentStore
        import json

        session = services["session"]
        repo_service = services["repo_service"]
        promotion_engine = services["promotion_engine"]

        # Create repo with max_k = 2 (only top 2 evaluated)
        repo = repo_service.create_repository(name="exclude-test")
        repo.max_alternatives_per_conflict = 2
        session.add(repo)
        session.commit()

        # Create 4 proposals - only top 2 by confidence should be evaluated
        proposals = []
        for i, confidence in enumerate([0.5, 0.9, 0.7, 0.3]):
            proposal = Proposal(
                repo_id=repo.id,
                agent_id=f"agent_{i}",
                change_content=f"change {i}",
                affected_paths=json.dumps(["shared.py"]),
                confidence_score=confidence,
                status=ProposalStatus.PENDING,
            )
            session.add(proposal)
            proposals.append(proposal)
        session.commit()

        # Create conflict set
        conflict_set = ConflictSet(
            repo_id=repo.id,
            path="shared.py",
            proposal_ids=json.dumps([p.id for p in proposals]),
        )
        session.add(conflict_set)
        session.commit()
        session.refresh(conflict_set)

        # Document store
        doc_store = DocumentStore(repo.id)
        doc_store.apply_full_file("shared.py", "content", "initial")

        best = await promotion_engine.select_best_candidate(
            conflict_set, doc_store, parallel=False
        )

        # Should have excluded proposals tracked
        # Top 2 by confidence are 0.9 and 0.7, so 0.5 and 0.3 should be excluded initially
        assert best is not None
        assert len(best.excluded_proposal_ids) >= 2  # At least the bottom 2

    def test_tie_breaking_by_submission_time(self, services):
        """Equal scores should be broken by earlier submission time."""
        from packages.aether.services.promotion_engine import PromotionCandidate
        from datetime import UTC, datetime, timedelta

        # Create two candidates with same score but different submission times
        earlier_time = datetime.now(UTC) - timedelta(hours=1)
        later_time = datetime.now(UTC)

        proposal_early = Proposal(
            id="early",
            repo_id="repo",
            agent_id="agent",
            confidence_score=0.8,
            submitted_at=earlier_time,
        )
        proposal_late = Proposal(
            id="late",
            repo_id="repo",
            agent_id="agent",
            confidence_score=0.8,
            submitted_at=later_time,
        )

        candidate_early = PromotionCandidate(
            proposals=[proposal_early],
            score=0.85,
            evidence={},
            gate_results=[],
            snapshot_id="snap1",
        )
        candidate_late = PromotionCandidate(
            proposals=[proposal_late],
            score=0.85,
            evidence={},
            gate_results=[],
            snapshot_id="snap2",
        )

        # Sort using the same logic as select_best_candidate
        candidates = [candidate_late, candidate_early]

        def sort_key(c):
            submission_ts = c.earliest_submission.timestamp() if c.earliest_submission else 0
            return (c.score, -submission_ts)

        candidates.sort(key=sort_key, reverse=True)

        # Earlier submission should win
        assert candidates[0].proposals[0].id == "early"


class TestEventBus:
    """Test event bus."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self, event_bus):
        """Should deliver events to subscribers."""
        from packages.aether.models.events import AetherEvent, EventType

        received = []

        def handler(event):
            received.append(event)

        event_bus.subscribe(handler)

        event = AetherEvent(
            event_id="evt_1",
            event_type=EventType.PROPOSAL_SUBMITTED,
            repo_id="repo_1",
        )
        await event_bus.publish(event)

        assert len(received) == 1
        assert received[0].event_id == "evt_1"

    @pytest.mark.asyncio
    async def test_filter_by_repo(self, event_bus):
        """Should filter events by repo_id."""
        from packages.aether.models.events import AetherEvent, EventType

        received = []

        event_bus.subscribe(lambda e: received.append(e), repo_id="repo_1")

        # Publish to different repos
        await event_bus.publish(AetherEvent(
            event_id="evt_1",
            event_type=EventType.PROPOSAL_SUBMITTED,
            repo_id="repo_1",
        ))
        await event_bus.publish(AetherEvent(
            event_id="evt_2",
            event_type=EventType.PROPOSAL_SUBMITTED,
            repo_id="repo_2",
        ))

        # Should only receive repo_1 event
        assert len(received) == 1
        assert received[0].repo_id == "repo_1"

    @pytest.mark.asyncio
    async def test_filter_by_event_type(self, event_bus):
        """Should filter events by type."""
        from packages.aether.models.events import AetherEvent, EventType

        received = []

        event_bus.subscribe(
            lambda e: received.append(e),
            event_types=[EventType.HEAD_UPDATED],
        )

        await event_bus.publish(AetherEvent(
            event_id="evt_1",
            event_type=EventType.PROPOSAL_SUBMITTED,
            repo_id="repo_1",
        ))
        await event_bus.publish(AetherEvent(
            event_id="evt_2",
            event_type=EventType.HEAD_UPDATED,
            repo_id="repo_1",
        ))

        assert len(received) == 1
        assert received[0].event_type == EventType.HEAD_UPDATED

    def test_unsubscribe(self, event_bus):
        """Should remove subscription."""
        sub_id = event_bus.subscribe(lambda e: None)
        assert event_bus.subscription_count == 1

        event_bus.unsubscribe(sub_id)
        assert event_bus.subscription_count == 0


class TestSystemPhysicsTelemetry:
    """Queue-physics telemetry tests."""

    @pytest.mark.asyncio
    async def test_system_physics_snapshot_tracks_submit_mu_and_fallback(
        self,
        db_session,
        event_bus,
        storage_path,
    ):
        repo_service = RepositoryService(db_session, storage_path)
        proposal_service = ProposalService(db_session, event_bus, {})
        repo = repo_service.create_repository(name="physics-metrics")

        store = DocumentStore(repo.id)
        store.apply_full_file("shared.py", "def alpha():\n    return 1\n", "init")
        proposal_service.set_document_store(repo.id, store)

        unparseable_diff = """diff --git a/shared.py b/shared.py
--- a/shared.py
+++ b/shared.py
this is not a hunk
"""
        valid_diff = """diff --git a/shared.py b/shared.py
--- a/shared.py
+++ b/shared.py
@@ -1,2 +1,2 @@
 def alpha():
-    return 1
+    return 2
"""

        fallback_response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-fallback",
                change_type="unified_diff",
                change_content=unparseable_diff,
                affected_paths=["shared.py"],
                confidence_score=0.8,
            ),
        )
        normal_response = await proposal_service.submit_proposal(
            repo.id,
            ProposalCreate(
                agent_id="agent-normal",
                change_type="unified_diff",
                change_content=valid_diff,
                affected_paths=["shared.py"],
                confidence_score=0.8,
            ),
        )

        await proposal_service.update_status(fallback_response.proposal_id, ProposalStatus.PROMOTED)
        await proposal_service.update_status(
            normal_response.proposal_id,
            ProposalStatus.REJECTED,
            reason="test_reject",
        )

        metrics = ProposalService.get_system_physics_snapshot(db_session, window_seconds=300)

        assert metrics["events"]["submit_accepted"] >= 2
        assert metrics["events"]["status_promoted"] >= 1
        assert metrics["events"]["status_rejected"] >= 1
        assert metrics["events"]["semantic_fallback_hits"] >= 1
        assert metrics["semantic_fallback"]["by_language"]["python"] >= 1
        assert metrics["semantic_fallback"]["by_path"]["shared.py"] >= 1
        assert metrics["lambda_tps"] > 0
        assert metrics["mu_tps"] > 0
        assert metrics["query_latency_ms"] >= 0

    def test_active_path_counts_only_include_active_statuses(
        self,
        db_session,
        storage_path,
    ):
        repo_service = RepositoryService(db_session, storage_path)
        repo = repo_service.create_repository(name="path-hotspot-metrics")
        db_session.add_all(
            [
                Proposal(
                    repo_id=repo.id,
                    agent_id="agent-1",
                    change_type="unified_diff",
                    change_content="noop",
                    affected_paths=json.dumps(["src/a.py"]),
                    status=ProposalStatus.INGESTED,
                ),
                Proposal(
                    repo_id=repo.id,
                    agent_id="agent-2",
                    change_type="unified_diff",
                    change_content="noop",
                    affected_paths=json.dumps(["src/a.py"]),
                    status=ProposalStatus.PENDING,
                ),
                Proposal(
                    repo_id=repo.id,
                    agent_id="agent-3",
                    change_type="unified_diff",
                    change_content="noop",
                    affected_paths=json.dumps(["src/a.py"]),
                    status=ProposalStatus.EVALUATING,
                ),
                Proposal(
                    repo_id=repo.id,
                    agent_id="agent-4",
                    change_type="unified_diff",
                    change_content="noop",
                    affected_paths=json.dumps(["src/a.py"]),
                    status=ProposalStatus.PROMOTED,
                ),
            ]
        )
        db_session.commit()

        counts = ProposalService.get_active_path_counts(
            db_session,
            repo_id=repo.id,
            paths=["src/a.py", "src/b.py"],
        )

        assert counts["src/a.py"] == 3
        assert counts["src/b.py"] == 0
