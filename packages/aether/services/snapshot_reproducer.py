"""Focused reproducer for sustained snapshot creation failures."""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from ..crdt.document_store import DocumentStore
from ..models.entities import GateConfig, RepoConfig
from .event_bus import EventBus
from .repository_service import RepositoryService
from .snapshot_service import SnapshotService


@dataclass(frozen=True)
class SnapshotReproducerConfig:
    iterations: int = 2000
    num_files: int = 64
    file_lines: int = 48
    seed: int = 2026


@dataclass
class SnapshotReproducerReport:
    config: SnapshotReproducerConfig
    total_iterations: int
    first_failure_iteration: int | None
    failing_phase: str | None
    exception_type: str | None
    exception_message: str | None
    last_create_metrics: dict[str, Any]
    max_identity_map_size: float
    passed: bool
    started_at: float
    completed_at: float
    storage_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "total_iterations": self.total_iterations,
            "first_failure_iteration": self.first_failure_iteration,
            "failing_phase": self.failing_phase,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "last_create_metrics": self.last_create_metrics,
            "max_identity_map_size": self.max_identity_map_size,
            "passed": self.passed,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "storage_path": self.storage_path,
        }


def _build_file_content(path: str, *, lines: int, marker: str) -> str:
    output = [f'"""Snapshot repro: {path}"""', f"# marker {marker}", ""]
    for index in range(lines):
        output.append(f"# line {index}")
    return "\n".join(output) + "\n"


async def run_snapshot_reproducer(
    config: SnapshotReproducerConfig,
) -> SnapshotReproducerReport:
    started_at = time.time()
    storage_path = Path(tempfile.mkdtemp(prefix="aether_snapshot_repro_"))
    db_path = storage_path / "snapshot-repro.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA busy_timeout=30000;"))
        conn.execute(text("PRAGMA synchronous=NORMAL;"))
    SQLModel.metadata.create_all(engine)

    rng = random.Random(config.seed)
    with Session(engine) as session:
        event_bus = EventBus()
        repo_service = RepositoryService(session, storage_path)
        snapshot_service = SnapshotService(session, event_bus, storage_path)
        repo = repo_service.create_repository(
            name="snapshot-repro",
            config=RepoConfig(
                gates=[
                    GateConfig(name="syntax", command="true", required=True, timeout_seconds=1, order=0),
                ],
                auto_promote=True,
            ),
        )
        store = DocumentStore(repo.id)
        files = [f"src/module_{index:03d}.py" for index in range(config.num_files)]
        for path in files:
            store.apply_full_file(path, _build_file_content(path, lines=config.file_lines, marker="base"), "init")

        base_snapshot = await snapshot_service.create_snapshot(repo.id, store, [], changed_paths_override=set(files))
        repo_service.update_head(repo.id, base_snapshot.id)

        last_metrics = snapshot_service.get_last_create_metrics()
        max_identity_map_size = max(
            float(last_metrics.get("subphase_repo_lookup_identity_map_size", 0.0) or 0.0),
            *(
                float(phase.get("identity_map_size", 0.0) or 0.0)
                for phase in last_metrics.get("sub_phases", [])
            ),
        )

        for iteration in range(1, config.iterations + 1):
            target_path = files[rng.randrange(len(files))]
            next_store = store.clone()
            next_store.apply_full_file(
                target_path,
                _build_file_content(target_path, lines=config.file_lines, marker=f"iter-{iteration}"),
                f"repro-{iteration}",
            )
            try:
                snapshot = await snapshot_service.create_snapshot(
                    repo.id,
                    next_store,
                    [f"repro-{iteration}"],
                    changed_paths_override={target_path},
                    created_by="snapshot-repro",
                )
            except Exception as exc:
                last_metrics = snapshot_service.get_last_create_metrics()
                max_identity_map_size = max(
                    max_identity_map_size,
                    *(
                        float(phase.get("identity_map_size", 0.0) or 0.0)
                        for phase in last_metrics.get("sub_phases", [])
                    ),
                )
                return SnapshotReproducerReport(
                    config=config,
                    total_iterations=iteration - 1,
                    first_failure_iteration=iteration,
                    failing_phase="create_snapshot",
                    exception_type=exc.__class__.__name__,
                    exception_message=str(exc),
                    last_create_metrics=last_metrics,
                    max_identity_map_size=max_identity_map_size,
                    passed=False,
                    started_at=started_at,
                    completed_at=time.time(),
                    storage_path=str(storage_path),
                )

            try:
                repo_service.update_head(repo.id, snapshot.id)
            except Exception as exc:
                last_metrics = snapshot_service.get_last_create_metrics()
                return SnapshotReproducerReport(
                    config=config,
                    total_iterations=iteration - 1,
                    first_failure_iteration=iteration,
                    failing_phase="update_head",
                    exception_type=exc.__class__.__name__,
                    exception_message=str(exc),
                    last_create_metrics=last_metrics,
                    max_identity_map_size=max_identity_map_size,
                    passed=False,
                    started_at=started_at,
                    completed_at=time.time(),
                    storage_path=str(storage_path),
                )
            store = next_store
            last_metrics = snapshot_service.get_last_create_metrics()
            if last_metrics.get("sub_phases"):
                max_identity_map_size = max(
                    max_identity_map_size,
                    *(
                        float(phase.get("identity_map_size", 0.0) or 0.0)
                        for phase in last_metrics.get("sub_phases", [])
                    ),
                )

        return SnapshotReproducerReport(
            config=config,
            total_iterations=config.iterations,
            first_failure_iteration=None,
            failing_phase=None,
            exception_type=None,
            exception_message=None,
            last_create_metrics=last_metrics,
            max_identity_map_size=max_identity_map_size,
            passed=True,
            started_at=started_at,
            completed_at=time.time(),
            storage_path=str(storage_path),
        )


def run_snapshot_reproducer_sync(
    config: SnapshotReproducerConfig,
) -> SnapshotReproducerReport:
    return asyncio.run(run_snapshot_reproducer(config))
