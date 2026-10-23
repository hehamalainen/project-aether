"""
Snapshot service for creating deterministic repository snapshots.

Handles:
- Creating snapshots from current state or specific proposals
- Content-addressed storage
- Snapshot materialization for build/test
"""

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import tarfile
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4
import structlog

from sqlalchemy import text
from sqlmodel import Session, select

from ..models.entities import (
    Snapshot,
    SnapshotCreate,
    SnapshotResponse,
    Proposal,
    generate_id,
)
from ..models.events import EventType
from ..crdt.document_store import DocumentStore
from .event_bus import EventBus

logger = structlog.get_logger()


SNAPSHOT_MANIFEST_FILENAME = ".aether_snapshot_manifest.json"


class SnapshotService:
    """Service for creating and managing snapshots."""

    _cleanup_executor = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="aether-gate-cleanup",
    )

    def __init__(
        self,
        session: Session,
        event_bus: EventBus,
        storage_path: Path,
    ):
        self.session = session
        self.event_bus = event_bus
        self.storage_path = storage_path
        self._last_create_metrics: dict[str, Any] = {}
        self._index_cache: OrderedDict[str, dict[str, Path]] = OrderedDict()
        self._index_cache_max_entries = 2000

    @staticmethod
    def _snapshot_columns() -> tuple[Any, ...]:
        """Return a stable scalar column tuple for snapshot hot-path queries."""
        table = Snapshot.__table__
        return (
            table.c.id,
            table.c.repo_id,
            table.c.head_sequence,
            table.c.included_proposals,
            table.c.content_hash,
            table.c.storage_path,
            table.c.created_at,
            table.c.created_by,
            table.c.file_count,
            table.c.total_bytes,
        )

    @staticmethod
    def _snapshot_from_row(row: Any) -> Optional[Snapshot]:
        """Build a detached Snapshot model from a scalar row."""
        if row is None:
            return None
        values = tuple(row)
        if len(values) != 10:
            return None
        return Snapshot(
            id=values[0],
            repo_id=values[1],
            head_sequence=values[2],
            included_proposals=values[3],
            content_hash=values[4],
            storage_path=values[5],
            created_at=values[6],
            created_by=values[7],
            file_count=values[8],
            total_bytes=values[9],
        )

    def _identity_map_size(self) -> float:
        """Return current session identity-map size for long-run diagnostics."""
        return float(len(self.session.identity_map))

    def _record_subphase_metric(
        self,
        metrics: dict[str, Any],
        sub_phases: list[dict[str, float | str]],
        phase_name: str,
        duration_ms: float,
    ) -> None:
        """Record detailed snapshot sub-phase timings for harness JSON output."""
        identity_map_size = self._identity_map_size()
        metrics[f"subphase_{phase_name}_ms"] = duration_ms
        metrics[f"subphase_{phase_name}_identity_map_size"] = identity_map_size
        sub_phases.append(
            {
                "sub_phase": phase_name,
                "duration_ms": duration_ms,
                "identity_map_size": identity_map_size,
            }
        )

    def _get_snapshot_dir(self, repo_id: str) -> Path:
        """Get the snapshots directory for a repository."""
        path = self.storage_path / repo_id / "snapshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _manifest_path(self, snapshot_path: Path) -> Path:
        """Return the manifest file path for a snapshot directory."""
        return snapshot_path / SNAPSHOT_MANIFEST_FILENAME

    def _read_manifest(self, snapshot_path: Path) -> Optional[dict]:
        """Read snapshot manifest metadata if present."""
        manifest_path = self._manifest_path(snapshot_path)
        if not manifest_path.exists() or not manifest_path.is_file():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _write_manifest(self, snapshot_path: Path, payload: dict) -> None:
        """Persist snapshot manifest metadata."""
        manifest_path = self._manifest_path(snapshot_path)
        manifest_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def _iter_snapshot_files(self, snapshot_path: Path) -> dict[str, Path]:
        """List local files stored in a snapshot directory (excluding metadata)."""
        files: dict[str, Path] = {}
        for file_path in snapshot_path.rglob("*"):
            if not file_path.is_file():
                continue
            rel_path = str(file_path.relative_to(snapshot_path))
            if rel_path == SNAPSHOT_MANIFEST_FILENAME:
                continue
            files[rel_path] = file_path
        return files

    def _resolve_base_snapshot_id(self, head_id: Optional[str]) -> Optional[str]:
        """Resolve current head snapshot id used as delta base."""
        if not head_id:
            return None
        if head_id.startswith("snap_"):
            return head_id
        row = self.session.connection().execute(
            text("SELECT snapshot_id FROM aether_heads WHERE id = :head_id"),
            {"head_id": head_id},
        ).first()
        if not row:
            return None
        return row[0]

    def _get_snapshot_row(self, snapshot_id: str) -> Optional[Any]:
        """Return a stable scalar row for a snapshot lookup."""
        return self.session.connection().execute(
            text(
                """
                SELECT id, repo_id, head_sequence, included_proposals, content_hash,
                       storage_path, created_at, created_by, file_count, total_bytes
                FROM aether_snapshots
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).first()

    def _get_snapshot_storage_row(self, snapshot_id: str) -> Optional[tuple[str, Optional[str]]]:
        """Return only the storage fields needed for file/index resolution."""
        row = self.session.connection().execute(
            text(
                """
                SELECT repo_id, storage_path
                FROM aether_snapshots
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).first()
        if not row:
            return None
        repo_id, storage_path = tuple(row)
        return str(repo_id), storage_path

    def _insert_snapshot_row(
        self,
        *,
        snapshot_id: str,
        repo_id: str,
        head_sequence: int,
        included_proposals: list[str],
        content_hash: str,
        storage_path: Path,
        file_count: int,
        total_bytes: int,
        created_by: str,
        commit: bool = True,
    ) -> Snapshot:
        """Insert a snapshot row directly to avoid ORM graph hydration in the hot path."""
        created_at = datetime.now(UTC)
        self.session.connection().execute(
            text(
                """
                INSERT INTO aether_snapshots (
                    id, repo_id, head_sequence, included_proposals, content_hash,
                    storage_path, created_at, created_by, file_count, total_bytes
                ) VALUES (
                    :id, :repo_id, :head_sequence, :included_proposals, :content_hash,
                    :storage_path, :created_at, :created_by, :file_count, :total_bytes
                )
                """
            ),
            {
                "id": snapshot_id,
                "repo_id": repo_id,
                "head_sequence": head_sequence,
                "included_proposals": json.dumps(included_proposals),
                "content_hash": content_hash,
                "storage_path": str(storage_path),
                "created_at": created_at,
                "created_by": created_by,
                "file_count": file_count,
                "total_bytes": total_bytes,
            },
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise RuntimeError(f"failed to load inserted snapshot {snapshot_id}")
        return snapshot

    @staticmethod
    def _normalize_changed_paths(
        paths: Optional[Iterable[str]],
    ) -> Optional[set[str]]:
        """Normalize pre-resolved changed paths from the caller."""
        if paths is None:
            return None
        changed_paths: set[str] = set()
        for path in paths:
            if not isinstance(path, str) or not path:
                return None
            changed_paths.add(path)
        return changed_paths

    def _resolve_snapshot_file_index(
        self,
        snapshot_id: str,
    ) -> dict[str, Path]:
        """
        Resolve the logical full file map for a snapshot.

        Supports full snapshots and delta snapshots with base pointers.
        """
        snapshot_id = self._resolve_base_snapshot_id(snapshot_id) or snapshot_id
        cached_index = self._get_cached_snapshot_file_index(snapshot_id)
        if cached_index is not None:
            return cached_index

        chain: list[tuple[Path, Optional[dict[str, Any]]]] = []
        seen: set[str] = set()
        current_snapshot_id: Optional[str] = snapshot_id

        # We will track if we hit a cached snapshot in the chain
        cached_base_index: Optional[dict[str, Path]] = None

        while current_snapshot_id:
            cached_base_index = self._get_cached_snapshot_file_index(current_snapshot_id)
            if cached_base_index is not None:
                break

            if current_snapshot_id in seen:
                logger.warning("snapshot_manifest_cycle_detected", snapshot_id=current_snapshot_id)
                return {}
            seen.add(current_snapshot_id)

            snapshot_row = self._get_snapshot_storage_row(current_snapshot_id)
            if not snapshot_row:
                return {}
            _, snapshot_storage_path = snapshot_row
            if not snapshot_storage_path:
                return {}

            snapshot_path = Path(snapshot_storage_path)
            if not snapshot_path.exists() or not snapshot_path.is_dir():
                return {}

            manifest = self._read_manifest(snapshot_path)
            chain.append((snapshot_path, manifest))
            if not manifest or manifest.get("mode") != "delta":
                break

            base_snapshot_id = manifest.get("base_snapshot_id")
            changed_paths = manifest.get("changed_paths", [])
            deleted_paths = manifest.get("deleted_paths", [])
            if (
                not isinstance(base_snapshot_id, str)
                or not base_snapshot_id
                or not isinstance(changed_paths, list)
                or not isinstance(deleted_paths, list)
            ):
                break
            current_snapshot_id = base_snapshot_id

        index: dict[str, Path] = cached_base_index or {}
        for snapshot_path, manifest in reversed(chain):
            if not manifest or manifest.get("mode") != "delta":
                index = self._iter_snapshot_files(snapshot_path)
                continue

            deleted_paths = manifest.get("deleted_paths", [])
            changed_paths = manifest.get("changed_paths", [])
            for rel_path in deleted_paths:
                if isinstance(rel_path, str):
                    index.pop(rel_path, None)

            for rel_path in changed_paths:
                if not isinstance(rel_path, str):
                    continue
                file_path = snapshot_path / rel_path
                if file_path.exists() and file_path.is_file():
                    index[rel_path] = file_path

        # Cache the result before returning
        self._store_snapshot_file_index(snapshot_id, index)
        return index

    def _get_cached_snapshot_file_index(self, snapshot_id: str) -> Optional[dict[str, Path]]:
        """Return a cached snapshot index and update its recency."""
        cached = self._index_cache.get(snapshot_id)
        if cached is None:
            return None
        self._index_cache.move_to_end(snapshot_id)
        return dict(cached)

    def _store_snapshot_file_index(self, snapshot_id: str, index: dict[str, Path]) -> None:
        """Store a snapshot index using LRU eviction semantics."""
        self._index_cache[snapshot_id] = dict(index)
        self._index_cache.move_to_end(snapshot_id)
        while len(self._index_cache) > self._index_cache_max_entries:
            self._index_cache.popitem(last=False)

    def _collect_changed_paths(
        self,
        repo_id: str,
        included_proposals: list[str],
    ) -> Optional[set[str]]:
        """
        Resolve changed file paths from included proposals.

        Returns None when proposal metadata is incomplete or cannot be trusted.
        """
        if not included_proposals:
            return None

        proposal_ids = list(dict.fromkeys(included_proposals))
        statement = select(Proposal).where(
            Proposal.repo_id == repo_id,
            Proposal.id.in_(proposal_ids),
        )
        proposals = list(self.session.exec(statement))
        if len(proposals) != len(proposal_ids):
            return None

        changed_paths: set[str] = set()
        for proposal in proposals:
            try:
                paths = json.loads(proposal.affected_paths)
            except Exception:
                return None
            if not isinstance(paths, list):
                return None
            for path in paths:
                if not isinstance(path, str) or not path:
                    return None
                changed_paths.add(path)
        return changed_paths

    def _materialize_delta_snapshot(
        self,
        document_store: DocumentStore,
        snapshot_path: Path,
        base_snapshot_id: str,
        base_paths: set[str],
        changed_paths: set[str],
        total_bytes: int,
    ) -> tuple[int, int, int, int, int, int]:
        """
        Persist a true delta snapshot (changed files only + manifest base pointer).

        Returns:
            (
                file_count,
                total_bytes,
                reused_file_count,
                written_file_count,
                linked_file_count,
                deleted_file_count,
            )
        """
        snapshot_path.mkdir(parents=True, exist_ok=True)
        current_paths = set(document_store.documents.keys())
        write_paths = sorted(
            rel_path
            for rel_path in current_paths
            if rel_path in changed_paths or rel_path not in base_paths
        )
        deleted_paths = sorted(base_paths - current_paths)

        for rel_path in write_paths:
            file_doc = document_store.documents.get(rel_path)
            if not file_doc:
                continue
            out_path = snapshot_path / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            content = file_doc.get_content()
            out_path.write_text(content, encoding="utf-8")

        changed_manifest_paths = sorted(set(write_paths) | set(deleted_paths))
        self._write_manifest(
            snapshot_path,
            {
                "version": 1,
                "mode": "delta",
                "base_snapshot_id": base_snapshot_id,
                "changed_paths": changed_manifest_paths,
                "deleted_paths": deleted_paths,
            },
        )

        file_count = len(current_paths)
        written_file_count = len(write_paths)
        reused_file_count = max(0, file_count - written_file_count)
        linked_file_count = 0
        deleted_file_count = len(deleted_paths)

        return (
            file_count,
            total_bytes,
            reused_file_count,
            written_file_count,
            linked_file_count,
            deleted_file_count,
        )

    async def create_snapshot(
        self,
        repo_id: str,
        document_store: DocumentStore,
        included_proposals: list[str],
        created_by: Optional[str] = None,
        changed_paths_override: Optional[Iterable[str]] = None,
        commit: bool = True,
    ) -> Snapshot:
        """
        Create a deterministic snapshot from the document store.

        Returns the created Snapshot record.
        """
        create_started = time.perf_counter()
        metrics: dict[str, Any] = {
            "cache_hit": 0.0,
            "content_hash_ms": 0.0,
            "content_hash_file_count": 0.0,
            "content_hash_total_bytes": 0.0,
            "cache_lookup_ms": 0.0,
            "materialize_ms": 0.0,
            "materialize_delta_used": 0.0,
            "materialize_file_count": 0.0,
            "materialize_total_bytes": 0.0,
            "materialize_reused_file_count": 0.0,
            "materialize_written_file_count": 0.0,
            "materialize_linked_file_count": 0.0,
            "materialize_deleted_file_count": 0.0,
            "db_commit_ms": 0.0,
            "publish_ms": 0.0,
            "total_ms": 0.0,
        }
        sub_phases: list[dict[str, float | str]] = []

        repo_lookup_started = time.perf_counter()
        repo_row = self.session.connection().execute(
            text(
                """
                SELECT head_sequence, head_id
                FROM aether_repositories
                WHERE id = :repo_id
                """
            ),
            {"repo_id": repo_id},
        ).first()
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "repo_lookup",
            (time.perf_counter() - repo_lookup_started) * 1000.0,
        )
        if not repo_row:
            raise ValueError(f"Repository not found: {repo_id}")
        repo_head_sequence, repo_head_id = repo_row

        # Get content hash
        hash_started = time.perf_counter()
        content_hash = await asyncio.to_thread(document_store.get_state_hash)
        metrics["content_hash_ms"] = (time.perf_counter() - hash_started) * 1000.0
        hash_metrics = document_store.get_last_hash_metrics()
        metrics["content_hash_file_count"] = float(hash_metrics.get("file_count", 0.0))
        metrics["content_hash_total_bytes"] = float(hash_metrics.get("total_bytes", 0.0))

        # Check if we already have this exact snapshot
        lookup_started = time.perf_counter()
        existing = self._get_by_hash(repo_id, content_hash)
        metrics["cache_lookup_ms"] = (time.perf_counter() - lookup_started) * 1000.0
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "cache_lookup",
            metrics["cache_lookup_ms"],
        )
        if existing:
            metrics["cache_hit"] = 1.0
            metrics["materialize_file_count"] = float(existing.file_count)
            metrics["materialize_total_bytes"] = float(existing.total_bytes)
            metrics["total_ms"] = (time.perf_counter() - create_started) * 1000.0
            metrics["sub_phases"] = sub_phases
            self._last_create_metrics = metrics
            logger.debug(
                "snapshot_cache_hit",
                repo_id=repo_id,
                snapshot_id=existing.id,
                content_hash=content_hash,
            )
            return existing

        # Create snapshot directory
        snapshot_id = generate_id("snap")
        snapshot_path = self._get_snapshot_dir(repo_id) / snapshot_id
        snapshot_path.mkdir(parents=True, exist_ok=True)

        # Materialize files
        materialize_started = time.perf_counter()
        base_resolution_started = time.perf_counter()
        base_snapshot_id = self._resolve_base_snapshot_id(repo_head_id)
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "base_snapshot_resolution",
            (time.perf_counter() - base_resolution_started) * 1000.0,
        )
        changed_path_started = time.perf_counter()
        changed_paths = self._normalize_changed_paths(changed_paths_override)
        if changed_paths is None:
            changed_paths = self._collect_changed_paths(repo_id, included_proposals)
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "changed_path_query",
            (time.perf_counter() - changed_path_started) * 1000.0,
        )
        base_paths: set[str] = set()
        # Logical snapshot size should always describe full repository state.
        file_count = int(hash_metrics.get("file_count", len(document_store.documents)))
        total_bytes = int(hash_metrics.get("total_bytes", 0.0))
        if base_snapshot_id and changed_paths is not None:
            delta_started = time.perf_counter()
            base_paths = set(self._resolve_snapshot_file_index(base_snapshot_id).keys())
            (
                file_count,
                total_bytes,
                reused_file_count,
                written_file_count,
                linked_file_count,
                deleted_file_count,
            ) = await asyncio.to_thread(
                self._materialize_delta_snapshot,
                document_store,
                snapshot_path,
                base_snapshot_id,
                base_paths,
                changed_paths,
                total_bytes,
            )
            self._record_subphase_metric(
                metrics,
                sub_phases,
                "delta_materialization",
                (time.perf_counter() - delta_started) * 1000.0,
            )
            metrics["materialize_delta_used"] = 1.0
            metrics["materialize_reused_file_count"] = float(reused_file_count)
            metrics["materialize_written_file_count"] = float(written_file_count)
            metrics["materialize_linked_file_count"] = float(linked_file_count)
            metrics["materialize_deleted_file_count"] = float(deleted_file_count)
            metrics["materialize_file_count"] = float(file_count)
            metrics["materialize_total_bytes"] = float(total_bytes)
        else:
            full_materialize_started = time.perf_counter()
            file_count_full, total_bytes_full = await asyncio.to_thread(
                document_store.materialize_snapshot, snapshot_path
            )
            self._record_subphase_metric(
                metrics,
                sub_phases,
                "delta_materialization",
                (time.perf_counter() - full_materialize_started) * 1000.0,
            )
            file_count = file_count_full
            total_bytes = total_bytes_full
            metrics["materialize_written_file_count"] = float(file_count_full)
            materialize_metrics = document_store.get_last_materialize_metrics()
            metrics["materialize_file_count"] = float(materialize_metrics.get("file_count", file_count_full))
            metrics["materialize_total_bytes"] = float(materialize_metrics.get("total_bytes", total_bytes_full))
        metrics["materialize_ms"] = (time.perf_counter() - materialize_started) * 1000.0

        commit_started = time.perf_counter()
        snapshot = self._insert_snapshot_row(
            snapshot_id=snapshot_id,
            repo_id=repo_id,
            head_sequence=repo_head_sequence,
            included_proposals=included_proposals,
            content_hash=content_hash,
            storage_path=snapshot_path,
            file_count=file_count,
            total_bytes=total_bytes,
            created_by=created_by or "system",
            commit=commit,
        )
        metrics["db_commit_ms"] = (time.perf_counter() - commit_started) * 1000.0
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "commit",
            metrics["db_commit_ms"],
        )

        # Publish event
        from .event_bus import AetherEvent
        publish_started = time.perf_counter()
        await self.event_bus.publish(
            AetherEvent(
                event_id=generate_id("evt"),
                event_type=EventType.SNAPSHOT_CREATED,
                repo_id=repo_id,
                timestamp=datetime.now(UTC),
                payload={
                    "snapshot_id": snapshot_id,
                    "content_hash": content_hash,
                    "file_count": file_count,
                    "total_bytes": total_bytes,
                    "included_proposals": included_proposals,
                },
            )
        )
        metrics["publish_ms"] = (time.perf_counter() - publish_started) * 1000.0
        self._record_subphase_metric(
            metrics,
            sub_phases,
            "publish",
            metrics["publish_ms"],
        )
        metrics["total_ms"] = (time.perf_counter() - create_started) * 1000.0
        metrics["sub_phases"] = sub_phases
        self._last_create_metrics = metrics

        logger.info(
            "snapshot_created",
            snapshot_id=snapshot_id,
            repo_id=repo_id,
            content_hash=content_hash,
            file_count=file_count,
            total_bytes=total_bytes,
        )

        return snapshot

    def get_last_create_metrics(self) -> dict[str, Any]:
        """Return timing/size metrics from the most recent create_snapshot call."""
        return dict(self._last_create_metrics)

    def _get_by_hash(self, repo_id: str, content_hash: str) -> Optional[Snapshot]:
        """Get snapshot by content hash."""
        row = self.session.connection().execute(
            text(
                """
                SELECT id, repo_id, head_sequence, included_proposals, content_hash,
                       storage_path, created_at, created_by, file_count, total_bytes
                FROM aether_snapshots
                WHERE repo_id = :repo_id AND content_hash = :content_hash
                LIMIT 1
                """
            ),
            {"repo_id": repo_id, "content_hash": content_hash},
        ).first()
        return self._snapshot_from_row(row)

    def get_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        """Get a snapshot by ID."""
        return self._snapshot_from_row(self._get_snapshot_row(snapshot_id))

    def list_snapshots(
        self,
        repo_id: str,
        limit: int = 50,
    ) -> list[Snapshot]:
        """List snapshots for a repository."""
        statement = (
            select(Snapshot)
            .where(Snapshot.repo_id == repo_id)
            .order_by(Snapshot.created_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement))

    def get_snapshot_path(self, snapshot_id: str) -> Optional[Path]:
        """Get the filesystem path for a snapshot."""
        snapshot_id = self._resolve_base_snapshot_id(snapshot_id) or snapshot_id
        snapshot_row = self._get_snapshot_storage_row(snapshot_id)
        if not snapshot_row:
            return None
        _, storage_path = snapshot_row
        if not storage_path:
            return None

        path = Path(storage_path)
        if not path.exists():
            return None

        return path

    async def create_tarball(self, snapshot_id: str) -> Optional[bytes]:
        """
        Create a tarball of the snapshot for download.

        Returns the tarball as bytes.
        """
        file_index = self._resolve_snapshot_file_index(snapshot_id)
        if not file_index:
            return None

        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for rel_path in sorted(file_index.keys()):
                tar.add(file_index[rel_path], arcname=rel_path)

        return buffer.getvalue()

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot."""
        snapshot = self.session.exec(select(Snapshot).where(Snapshot.id == snapshot_id)).first()
        if not snapshot:
            return False

        # Delete files
        if snapshot.storage_path:
            path = Path(snapshot.storage_path)
            if path.exists():
                shutil.rmtree(path)

        # Delete record
        self.session.delete(snapshot)
        self.session.commit()

        logger.info("snapshot_deleted", snapshot_id=snapshot_id)
        return True

    def get_file_content(self, snapshot_id: str, file_path: str) -> Optional[str]:
        """Get content of a specific file from a snapshot."""
        if not file_path or Path(file_path).is_absolute():
            return None

        normalized = str(Path(file_path))
        if normalized.startswith(".."):
            return None

        file_index = self._resolve_snapshot_file_index(snapshot_id)
        full_path = file_index.get(normalized)
        if not full_path:
            return None

        try:
            return full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def list_files(self, snapshot_id: str) -> list[str]:
        """List all files in a snapshot."""
        return sorted(self._resolve_snapshot_file_index(snapshot_id).keys())

    def clone_to_directory(self, snapshot_id: str, target_path: Path) -> bool:
        """Clone a snapshot to a target directory."""
        file_index = self._resolve_snapshot_file_index(snapshot_id)
        if not file_index:
            return False

        target_path.mkdir(parents=True, exist_ok=True)

        for rel_path, source_path in file_index.items():
            dest_path = target_path / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_path)

        return True

    def materialize_gate_workspace(self, snapshot_id: str) -> Optional[Path]:
        """Materialize a full snapshot tree for gate execution outside promotion hot path."""
        snapshot_row = self._get_snapshot_storage_row(snapshot_id)
        if not snapshot_row:
            return None
        repo_id, _ = snapshot_row

        file_index = self._resolve_snapshot_file_index(snapshot_id)
        workspace_root = self.storage_path / repo_id / "gate_workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_path = workspace_root / f"{snapshot_id}_{uuid4().hex[:8]}"
        workspace_path.mkdir(parents=True, exist_ok=True)

        for rel_path, source_path in file_index.items():
            dest_path = workspace_path / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_path, dest_path)
            except OSError:
                shutil.copy2(source_path, dest_path)

        return workspace_path

    def cleanup_gate_workspace(self, workspace_path: Path) -> None:
        """Clean up a materialized gate workspace in the background."""
        if not workspace_path.exists():
            return

        self._cleanup_executor.submit(self._remove_gate_workspace, workspace_path)

    @staticmethod
    def _remove_gate_workspace(workspace_path: Path) -> None:
        """Remove a materialized gate workspace with bounded background concurrency."""
        try:
            shutil.rmtree(workspace_path)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(
                "failed_to_cleanup_gate_workspace",
                workspace_path=str(workspace_path),
                error=str(e),
            )

    def compute_diff(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
    ) -> dict[str, str]:
        """
        Compute diff between two snapshots.

        Returns dict of {path: unified_diff}.
        """
        import difflib

        diffs = {}

        # Get all files from both snapshots
        base_index = self._resolve_snapshot_file_index(base_snapshot_id)
        target_index = self._resolve_snapshot_file_index(target_snapshot_id)
        base_files = set(base_index.keys())
        target_files = set(target_index.keys())

        all_files = base_files | target_files

        for file_path in all_files:
            base_content = ""
            target_content = ""

            if file_path in base_files:
                base_full = base_index[file_path]
                if base_full.exists():
                    base_content = base_full.read_text(encoding="utf-8", errors="replace")

            if file_path in target_files:
                target_full = target_index[file_path]
                if target_full.exists():
                    target_content = target_full.read_text(encoding="utf-8", errors="replace")

            if base_content != target_content:
                diff = difflib.unified_diff(
                    base_content.splitlines(keepends=True),
                    target_content.splitlines(keepends=True),
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                )
                diffs[file_path] = "".join(diff)

        return diffs
