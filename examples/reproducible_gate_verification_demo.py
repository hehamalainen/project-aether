#!/usr/bin/env python3
"""
Reproducible Aether gate verification example.

Demonstrates:
1. Default repositories use explicit `SIMULATED:` no-op gates (`_verification_mode == "SIMULATED"`),
   making clear that default promotions simulate gate workflow rather than compiling/testing code.
2. When configured with real verification gates (`python -m py_compile` and `pytest`) in a trusted
   local environment:
   - A valid initial proposal passes gates and advances canonical head (`head_sequence == 1`).
   - A broken proposal (introducing a regression that fails unit tests) is rejected by the
     verification gate (`GateStatus.FAIL`), leaving the canonical head unchanged (`head_sequence == 1`)
     and marking the proposal `REJECTED`.
   - A subsequent valid proposal passes the real gates and advances canonical head (`head_sequence == 2`)
     with `_verification_mode == "EXECUTED"` recorded in promotion evidence.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine

from packages.aether.crdt.document_store import DocumentStore
from packages.aether.models.entities import (
    GateConfig,
    GateStatus,
    ProposalCreate,
    ProposalStatus,
    RepoConfig,
)
from packages.aether.services.event_bus import EventBus
from packages.aether.services.gate_runner import DEFAULT_GATES, GateRunner
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService


async def run_reproducible_demo(work_dir: Path | None = None) -> dict[str, Any]:
    """Execute the end-to-end verification gate demonstration and return structured results."""
    own_temp = work_dir is None
    temp_ctx = tempfile.TemporaryDirectory(prefix="aether_gate_demo_") if own_temp else None
    base_dir = Path(temp_ctx.name) if temp_ctx is not None else Path(work_dir)  # type: ignore[arg-type]

    try:
        db_path = base_dir / "demo.db"
        storage_path = base_dir / "storage"
        storage_path.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            event_bus = EventBus()
            document_stores: dict[str, DocumentStore] = {}
            repo_service = RepositoryService(session, storage_path)
            proposal_service = ProposalService(session, event_bus, document_stores)
            snapshot_service = SnapshotService(session, event_bus, storage_path)

            # Part 1: Inspect explicit simulation defaults
            default_commands = [g.command for g in DEFAULT_GATES]
            assert all(cmd.startswith("SIMULATED:") for cmd in default_commands)

            # Part 2: Real gate enforcement in a trusted local runner
            gate_runner = GateRunner(session, event_bus, allow_custom_commands=True)
            promotion_engine = PromotionEngine(
                session,
                event_bus,
                proposal_service,
                snapshot_service,
                gate_runner,
                storage_path,
                gate_execution_mode="sequential",
            )

            real_gates = [
                GateConfig(
                    name="syntax",
                    command=f"{sys.executable} -m py_compile calculator.py test_calculator.py",
                    required=True,
                    timeout_seconds=30,
                    order=0,
                ),
                GateConfig(
                    name="unit_tests",
                    command=f"{sys.executable} -m pytest -q test_calculator.py",
                    required=True,
                    timeout_seconds=60,
                    order=1,
                ),
            ]

            repo = repo_service.create_repository(
                name="verified-calculator-demo",
                description="Demonstrates real verification gates rejecting a broken proposal",
                config=RepoConfig(gates=real_gates),
            )
            store = repo_service.get_document_store(repo.id)
            proposal_service.set_document_store(repo.id, store)

            # Seed initial test suite into the repository baseline
            test_code = (
                "from calculator import add\n\n"
                "def test_add():\n"
                "    assert add(2, 2) == 4\n"
                "    assert add(-1, 5) == 4\n"
            )
            store.apply_full_file("test_calculator.py", test_code, "seed_tests")

            # Step A: Submit and promote a valid baseline implementation
            valid_v1 = await proposal_service.submit_proposal(
                repo.id,
                ProposalCreate(
                    agent_id="agent-alpha",
                    task_id="task-init",
                    change_type="full_file",
                    change_content="def add(a: int, b: int) -> int:\n    return a + b\n",
                    affected_paths=["calculator.py"],
                    confidence_score=0.95,
                ),
            )
            promotions_v1 = await promotion_engine.process_pending_proposals(repo.id, store)
            assert len(promotions_v1) == 1, "Valid v1 proposal should promote"
            promo_v1 = promotions_v1[0]
            head_after_v1 = repo_service.get_head(repo.id)
            assert head_after_v1 is not None
            assert head_after_v1.sequence == 1
            v1_evidence = json.loads(promo_v1.evidence)
            assert v1_evidence.get("_verification_mode") == "EXECUTED"

            # Step B: Submit a broken proposal that introduces a bug (a - b instead of a + b)
            broken_v2 = await proposal_service.submit_proposal(
                repo.id,
                ProposalCreate(
                    agent_id="agent-buggy",
                    task_id="task-regressed",
                    base_head_id=head_after_v1.id,
                    change_type="full_file",
                    change_content="def add(a: int, b: int) -> int:\n    return a - b  # regression!\n",
                    affected_paths=["calculator.py"],
                    confidence_score=0.99,  # High agent confidence, but code is broken
                ),
            )

            promotions_broken = await promotion_engine.process_pending_proposals(repo.id, store)
            assert len(promotions_broken) == 0, "Broken proposal must be rejected by real unit_tests gate"

            rejected_proposal = proposal_service.get_proposal(broken_v2.proposal_id)
            assert rejected_proposal is not None
            assert rejected_proposal.status == ProposalStatus.REJECTED

            head_after_broken = repo_service.get_head(repo.id)
            assert head_after_broken is not None
            assert head_after_broken.id == head_after_v1.id
            assert head_after_broken.sequence == 1

            # Inspect the failed gate result recorded in the database
            latest_unit_gates = gate_runner.get_latest_results(repo.id, gate_name="unit_tests", limit=5)
            failed_gate = next(r for r in latest_unit_gates if r.status == GateStatus.FAIL)
            assert failed_gate.status == GateStatus.FAIL
            assert failed_gate.exit_code != 0

            # Step C: Submit a valid enhancement proposal and verify canonical head advances
            valid_v3 = await proposal_service.submit_proposal(
                repo.id,
                ProposalCreate(
                    agent_id="agent-beta",
                    task_id="task-enhance",
                    base_head_id=head_after_v1.id,
                    change_type="full_file",
                    change_content=(
                        "def add(a: int, b: int) -> int:\n"
                        "    return int(a) + int(b)\n"
                    ),
                    affected_paths=["calculator.py"],
                    confidence_score=0.92,
                ),
            )
            promotions_v3 = await promotion_engine.process_pending_proposals(repo.id, store)
            assert len(promotions_v3) == 1, "Fixed v3 proposal should promote"
            head_after_v3 = repo_service.get_head(repo.id)
            assert head_after_v3 is not None
            assert head_after_v3.sequence == 2

            summary = {
                "default_gates_commands": default_commands,
                "repo_id": repo.id,
                "v1_promoted_head_sequence": head_after_v1.sequence,
                "v1_verification_mode": v1_evidence.get("_verification_mode"),
                "broken_proposal_id": broken_v2.proposal_id,
                "broken_proposal_status": rejected_proposal.status.value,
                "broken_gate_status": failed_gate.status.value,
                "broken_gate_exit_code": failed_gate.exit_code,
                "head_sequence_after_broken": head_after_broken.sequence,
                "v3_promoted_head_sequence": head_after_v3.sequence,
                "canonical_calculator_source": snapshot_service.get_file_content(
                    head_after_v3.snapshot_id, "calculator.py"
                ),
            }
            return summary
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


def main() -> None:
    summary = asyncio.run(run_reproducible_demo())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
