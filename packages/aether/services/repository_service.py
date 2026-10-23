"""
Repository service for managing Aether repositories.

Handles:
- Repository creation and configuration
- Loading/storing repository state
- Managing canonical head
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
import structlog

from sqlmodel import Session, select

from ..models.entities import (
    Repository,
    AetherHead,
    Snapshot,
    GateConfig,
    RepoConfig,
    generate_id,
)
from ..crdt.document_store import DocumentStore

logger = structlog.get_logger()


SNAPSHOT_MANIFEST_FILENAME = ".aether_snapshot_manifest.json"


class RepositoryService:
    """Service for managing Aether repositories."""

    def __init__(self, session: Session, storage_path: Path):
        self.session = session
        self.storage_path = storage_path
        self._document_stores: dict[str, DocumentStore] = {}

    def _read_snapshot_manifest(self, snapshot_path: Path) -> Optional[dict]:
        """Read manifest metadata for delta snapshots."""
        manifest_path = snapshot_path / SNAPSHOT_MANIFEST_FILENAME
        if not manifest_path.exists() or not manifest_path.is_file():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _iter_snapshot_files(self, snapshot_path: Path) -> dict[str, Path]:
        """List concrete files stored in a snapshot directory."""
        files: dict[str, Path] = {}
        for file_path in snapshot_path.rglob("*"):
            if not file_path.is_file():
                continue
            rel_path = str(file_path.relative_to(snapshot_path))
            if rel_path == SNAPSHOT_MANIFEST_FILENAME:
                continue
            files[rel_path] = file_path
        return files

    def _resolve_snapshot_files(
        self,
        snapshot_id: str,
    ) -> dict[str, Path]:
        """Resolve full snapshot file map, following delta manifests when present."""
        chain: list[tuple[Path, Optional[dict[str, Any]]]] = []
        seen: set[str] = set()
        current_snapshot_id: Optional[str] = snapshot_id

        while current_snapshot_id:
            if current_snapshot_id in seen:
                logger.warning("snapshot_manifest_cycle_detected", snapshot_id=current_snapshot_id)
                return {}
            seen.add(current_snapshot_id)

            snapshot = self.session.get(Snapshot, current_snapshot_id)
            if not snapshot or not snapshot.storage_path:
                return {}
            snapshot_path = Path(snapshot.storage_path)
            if not snapshot_path.exists() or not snapshot_path.is_dir():
                return {}

            manifest = self._read_snapshot_manifest(snapshot_path)
            chain.append((snapshot_path, manifest))
            if not manifest or manifest.get("mode") != "delta":
                break

            changed_paths = manifest.get("changed_paths", [])
            deleted_paths = manifest.get("deleted_paths", [])
            base_snapshot_id = manifest.get("base_snapshot_id")
            if (
                not isinstance(base_snapshot_id, str)
                or not base_snapshot_id
                or not isinstance(changed_paths, list)
                or not isinstance(deleted_paths, list)
            ):
                break
            current_snapshot_id = base_snapshot_id

        files: dict[str, Path] = {}
        for snapshot_path, manifest in reversed(chain):
            if not manifest or manifest.get("mode") != "delta":
                files = self._iter_snapshot_files(snapshot_path)
                continue

            for rel_path in manifest.get("deleted_paths", []):
                if isinstance(rel_path, str):
                    files.pop(rel_path, None)

            for rel_path in manifest.get("changed_paths", []):
                if not isinstance(rel_path, str):
                    continue
                file_path = snapshot_path / rel_path
                if file_path.exists() and file_path.is_file():
                    files[rel_path] = file_path

        return files

    def create_repository(
        self,
        name: str,
        description: Optional[str] = None,
        config: Optional[RepoConfig] = None,
    ) -> Repository:
        """Create a new repository."""
        repo_id = generate_id("repo")

        # Create repo directory
        repo_path = self.storage_path / repo_id
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / "snapshots").mkdir(exist_ok=True)
        (repo_path / "workspace").mkdir(exist_ok=True)

        cfg = config or RepoConfig()
        repo = Repository(
            id=repo_id,
            name=name,
            description=description,
            gate_config=json.dumps(cfg.model_dump()),
            auto_promote=cfg.auto_promote,
            max_alternatives_per_conflict=cfg.max_alternatives_per_conflict,
        )
        self.session.add(repo)
        self.session.commit()
        self.session.refresh(repo)

        # Initialize document store
        workspace_path = repo_path / "workspace"
        self._document_stores[repo_id] = DocumentStore(repo_id, str(workspace_path))

        logger.info("repository_created", repo_id=repo_id, name=name)
        return repo

    def get_repository(self, repo_id: str) -> Optional[Repository]:
        """Get a repository by ID."""
        return self.session.get(Repository, repo_id)

    def list_repositories(self) -> list[Repository]:
        """List all repositories."""
        statement = select(Repository).order_by(Repository.created_at.desc())
        return list(self.session.exec(statement))

    def delete_repository(self, repo_id: str) -> bool:
        """Delete a repository."""
        repo = self.get_repository(repo_id)
        if not repo:
            return False

        # Clean up document store
        if repo_id in self._document_stores:
            del self._document_stores[repo_id]

        # Delete from database
        self.session.delete(repo)
        self.session.commit()

        # Optionally clean up files (could be dangerous, leave for now)
        logger.info("repository_deleted", repo_id=repo_id)
        return True

    def get_document_store(self, repo_id: str) -> Optional[DocumentStore]:
        """Get the document store for a repository, initialized from HEAD snapshot."""
        if repo_id not in self._document_stores:
            repo = self.get_repository(repo_id)
            if not repo:
                return None

            workspace_path = self.storage_path / repo_id / "workspace"
            doc_store = DocumentStore(repo_id, str(workspace_path))
            
            # Load content from HEAD snapshot if it exists
            if repo.head_id:
                head = self.get_head(repo_id)
                if head and head.snapshot_id:
                    files = self._resolve_snapshot_files(head.snapshot_id)
                    # Load all logical snapshot files into the document store
                    for rel_path, file_path in files.items():
                        try:
                            content = file_path.read_text(encoding="utf-8", errors="replace")
                            file_doc = doc_store.get_document(rel_path)
                            if file_doc.structured:
                                file_doc.structured.set_content(content)
                            file_doc.document.set_text(content)
                            file_doc.last_hash = file_doc.get_hash()
                        except Exception as e:
                            logger.warning(
                                "failed_to_load_file_from_snapshot",
                                file_path=rel_path,
                                error=str(e),
                            )
                    logger.info(
                        "document_store_loaded_from_snapshot",
                        repo_id=repo_id,
                        snapshot_id=head.snapshot_id,
                        file_count=len(files),
                    )
            
            self._document_stores[repo_id] = doc_store

        return self._document_stores[repo_id]

    def get_repo_config(self, repo_id: str) -> Optional[RepoConfig]:
        """Get repository configuration."""
        repo = self.get_repository(repo_id)
        if not repo or not repo.gate_config:
            return RepoConfig()

        try:
            return RepoConfig(**json.loads(repo.gate_config))
        except Exception:
            return RepoConfig()

    def update_repo_config(self, repo_id: str, config: RepoConfig) -> bool:
        """Update repository configuration."""
        repo = self.get_repository(repo_id)
        if not repo:
            return False

        repo.gate_config = json.dumps(config.model_dump())
        repo.updated_at = datetime.now(UTC)
        self.session.add(repo)
        self.session.commit()

        logger.info("repository_config_updated", repo_id=repo_id)
        return True

    def get_head(self, repo_id: str) -> Optional[AetherHead]:
        """Get the current canonical head for a repository."""
        repo = self.get_repository(repo_id)
        if not repo or not repo.head_id:
            return None

        return self.session.get(AetherHead, repo.head_id)

    def get_head_history(self, repo_id: str, limit: int = 50) -> list[AetherHead]:
        """Get history of head states."""
        statement = (
            select(AetherHead)
            .where(AetherHead.repo_id == repo_id)
            .order_by(AetherHead.sequence.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement))

    def update_head(
        self,
        repo_id: str,
        snapshot_id: str,
        promotion_id: Optional[str] = None,
        commit: bool = True,
    ) -> AetherHead:
        """
        Update the canonical head to a new snapshot.

        Creates a new AetherHead record and updates the repository.
        """
        repo = self.get_repository(repo_id)
        if not repo:
            raise ValueError(f"Repository not found: {repo_id}")

        # Create new head record
        new_sequence = repo.head_sequence + 1
        head = AetherHead(
            id=generate_id("head"),
            repo_id=repo_id,
            sequence=new_sequence,
            snapshot_id=snapshot_id,
            promotion_id=promotion_id,
        )
        self.session.add(head)

        # Mark old head as non-canonical
        if repo.head_id:
            old_head = self.session.get(AetherHead, repo.head_id)
            if old_head:
                old_head.is_canonical = False
                self.session.add(old_head)

        # Update repository
        repo.head_id = head.id
        repo.head_sequence = new_sequence
        repo.updated_at = datetime.now(UTC)
        self.session.add(repo)

        if commit:
            self.session.commit()
        else:
            self.session.flush()

        logger.info(
            "head_updated",
            repo_id=repo_id,
            head_id=head.id,
            sequence=new_sequence,
            snapshot_id=snapshot_id,
        )

        return head

    def import_from_directory(self, repo_id: str, source_path: Path, extensions: Optional[list[str]] = None) -> int:
        """
        Import files from a directory into the repository.

        Returns number of files imported.
        """
        doc_store = self.get_document_store(repo_id)
        if not doc_store:
            raise ValueError(f"Repository not found: {repo_id}")

        # Temporarily set base path to source
        old_base = doc_store.base_path
        doc_store.base_path = source_path

        count = doc_store.load_directory(extensions)

        # Restore base path
        doc_store.base_path = old_base

        logger.info("files_imported", repo_id=repo_id, count=count, source=str(source_path))
        return count

    def get_snapshot_path(self, repo_id: str, snapshot_id: str) -> Path:
        """Get the filesystem path for a snapshot."""
        return self.storage_path / repo_id / "snapshots" / snapshot_id

    def get_workspace_path(self, repo_id: str) -> Path:
        """Get the workspace path for a repository."""
        return self.storage_path / repo_id / "workspace"
