#!/usr/bin/env python3
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

# Aether imports
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.models.entities import ProposalCreate
from packages.aether.services.event_bus import EventBus
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.models.events import EventType

logger = logging.getLogger("semantic_cascade_load")
logging.basicConfig(level=logging.INFO)

def setup_argparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 4 Semantic Promotion Cascade Load Harness")
    parser.add_argument("--agents", type=int, required=True, help="Number of concurrent agents")
    parser.add_argument("--scenario", type=str, required=True, choices=[
        "class1", "class2", "class3", "class2_subtle_reject", "class2_obvious_reject",
        "oracle_false_green", "mixed", "hotspot", "adversarial_suite", 
        "manifest_failure", "worker_failure"
    ], help="Testing scenario to execute")
    parser.add_argument("--worker-mode", type=str, required=True, choices=["fake", "local", "cloud"], help="Synthesis worker execution mode")
    parser.add_argument("--db-profile", type=str, required=True, choices=["sqlite", "postgres"], help="Database backend profile")
    parser.add_argument("--max-local-workers", type=int, default=4, help="Maximum concurrent synthesis worker subprocesses")
    parser.add_argument("--max-promotions", type=int, default=0, help="Maximum number of successful promotions before stopping (0 for unlimited)")
    parser.add_argument("--duration-seconds", type=int, default=60, help="Test duration in seconds")
    parser.add_argument("--repo-fixture", type=str, required=True, help="Name of the base repository fixture to load")
    parser.add_argument("--output-dir", type=str, required=True, help="Directory to output standard artifact bundle")
    parser.add_argument("--cloud-concurrency", type=int, default=10, help="Max concurrency for cloud inference")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic runs")
    parser.add_argument("--distributed-runners", type=int, default=1, help="Number of remote runners for distributed scale tests")
    parser.add_argument("--agents-per-runner", type=int, default=0, help="Agents per remote runner")
    return parser

async def run_harness(args, oracle: Dict[str, Any], engine, storage_path: Path, out_dir: Path):
    start_perf = time.perf_counter()
    event_bus = EventBus()
    
    # Event Collectors
    agent_events = []
    proposal_events = []
    promotion_events = []
    worker_events = []
    gate_results = []
    
    async def collect_events(event):
        if event.event_type in (EventType.PROPOSAL_SUBMITTED, EventType.PROPOSAL_EVALUATING, EventType.PROPOSAL_PROMOTED, EventType.PROPOSAL_REJECTED, EventType.PROPOSAL_QUARANTINED):
            proposal_events.append(event)
            agent_events.append(event)
        elif event.event_type in (EventType.GATE_STARTED, EventType.GATE_COMPLETED, EventType.GATE_FAILED):
            gate_results.append(event)
        elif event.event_type in (EventType.HEAD_UPDATED, EventType.HEAD_ROLLBACK, EventType.CONFLICT_DETECTED, EventType.CONFLICT_RESOLVED, EventType.PROMOTION_STARTED, EventType.PROMOTION_COMPLETED, EventType.PROMOTION_FAILED, EventType.SNAPSHOT_CREATED):
            worker_events.append(event)
        
        # Keep promotion_events strictly for PROPOSAL_PROMOTED and PROPOSAL_REJECTED
        if event.event_type in (EventType.PROPOSAL_PROMOTED, EventType.PROPOSAL_REJECTED):
            promotion_events.append(event)
    
    event_bus.subscribe(collect_events, is_async=True)

    with Session(engine) as session:
        repo_service = RepositoryService(session, storage_path)
        proposal_service = ProposalService(session, event_bus, {})
        snapshot_service = SnapshotService(session, event_bus, storage_path)
        gate_runner = GateRunner(session, event_bus)
        
        promotion_engine = PromotionEngine(
            session=session,
            event_bus=event_bus,
            proposal_service=proposal_service,
            snapshot_service=snapshot_service,
            gate_runner=gate_runner,
            storage_path=storage_path,
        )
        
        # Configure worker
        promotion_engine.synthesis_worker_enabled = True
        
        # 1. Create Repository
        repo = repo_service.create_repository(args.repo_fixture)
        gate_config = dict(oracle["gate_config"]) if "gate_config" in oracle else {}
        if "golden_assertions" in oracle:
            gate_config["golden_assertions"] = oracle["golden_assertions"]
        if gate_config:
            repo.gate_config = json.dumps(gate_config)
            session.add(repo)
            session.commit()
            
        doc_store = DocumentStore(repo.id, str(storage_path))
        
        # Load Base Fixture
        fixture_dir = Path(f"tests/fixtures/phase4/{args.scenario}/base")
        if fixture_dir.exists():
            for filepath in fixture_dir.glob("**/*"):
                if filepath.is_file():
                    rel_path = filepath.relative_to(fixture_dir).as_posix()
                    with open(filepath, "r") as f:
                        doc_store.apply_full_file(rel_path, f.read(), "base_fixture")
        
        # Commit base files
        await snapshot_service.create_snapshot(repo.id, doc_store, "base fixture")
        logger.info(f"Loaded base fixture for {args.scenario} into repo {repo.id}")
        logger.info(f"Gate config for repo {repo.id} is: {repo.gate_config}")
        
        # 2. Submit proposals
        import difflib
        tasks = []
        
        # Check submission mode
        submission_mode = oracle.get("submission_mode", "concurrent")

        agents = oracle.get("agents", [])
        if args.agents > 0 and args.scenario in ("mixed", "hotspot"):
            agents = agents[:args.agents]
            
        # Capture initial base state for temporal conflicts
        initial_base_head_id = repo.head_id
        initial_base_head_sequence = repo.head_sequence
        initial_contents = {}
        for agent_def in agents:
            path = agent_def["proposal"]["path"]
            if path not in initial_contents:
                initial_contents[path] = doc_store.get_document(path).get_content() if doc_store.has_document(path) else ""

        # Submit proposals
        for agent_def in agents:
            path = agent_def["proposal"]["path"]
            base_content = initial_contents[path]
            new_content = agent_def["proposal"]["content"]
            
            diff_lines = list(difflib.unified_diff(
                base_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3
            ))
            
            # DocumentStore's parser requires the "diff --git" header
            diff_str = f"diff --git a/{path} b/{path}\n" + "".join(diff_lines)
            
            prop_data = ProposalCreate(
                repo_id=repo.id,
                agent_id=agent_def["id"],
                task_id=f"task_{agent_def['id']}",
                confidence_score=0.9,
                affected_paths=[path],
                affected_symbols=agent_def["proposal"]["affected_symbols"],
                dependencies=[],
                change_type="unified_diff",
                change_content=diff_str,
                base_head_id=initial_base_head_id,
                base_head_sequence=initial_base_head_sequence
            )
            tasks.append(proposal_service.submit_proposal(repo.id, prop_data))

            if submission_mode == "sequential":
                await asyncio.gather(*tasks)
                tasks = []
                logger.info("Processing pending proposals sequentially...")
                while True:
                    batch = await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=10)
                    if not batch:
                        break
        
        if tasks:
            logger.info(f"Submitting {len(tasks)} proposals concurrently...")
            await asyncio.gather(*tasks)
            
            # 3. Process promotions until no pending proposals
            logger.info("Processing pending proposals...")
            while True:
                batch = await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=10)
                if not batch:
                    break

        # 4. Write artifacts
        with open(out_dir / "proposal_events.jsonl", "w") as f:
            for ev in proposal_events:
                f.write(ev.model_dump_json() + "\n")
        
        with open(out_dir / "agent_events.jsonl", "w") as f:
            for ev in agent_events:
                f.write(ev.model_dump_json() + "\n")
        
        with open(out_dir / "promotion_events.jsonl", "w") as f:
            for ev in promotion_events:
                f.write(ev.model_dump_json() + "\n")
                
        with open(out_dir / "worker_events.jsonl", "w") as f:
            for ev in worker_events:
                f.write(ev.model_dump_json() + "\n")
                
        with open(out_dir / "gate_results.jsonl", "w") as f:
            for ev in gate_results:
                f.write(ev.model_dump_json() + "\n")
                
        with open(out_dir / "expected_outcome.json", "w") as f:
            json.dump({
                "scenario": oracle["scenario"],
                "expected_outcome": oracle["expected_outcome"],
                "expected_reason": oracle["expected_reason"]
            }, f, indent=2)

        with open(out_dir / "golden_assertions.json", "w") as f:
            json.dump(oracle.get("golden_assertions", {}), f, indent=2)

        # Check expected_terminal_counts is present
        if "expected_terminal_counts" not in oracle:
            raise ValueError(f"expected_terminal_counts is missing in oracle.json for scenario {args.scenario}")
        expected_terminal_counts = oracle["expected_terminal_counts"]

        # Compute actual terminal counts
        from packages.aether.models.entities import Proposal, ProposalStatus
        from sqlmodel import select
        actual_counts = {}
        for status_enum in ProposalStatus:
            actual_counts[status_enum.value] = 0
        proposals_in_db = session.exec(select(Proposal).where(Proposal.repo_id == repo.id)).all()
        for p in proposals_in_db:
            status_str = p.status.value
            actual_counts[status_str] = actual_counts.get(status_str, 0) + 1
        
        # Clean actual counts (only non-zero values)
        actual_terminal_counts = {k: v for k, v in actual_counts.items() if v > 0}
        vector_match = (actual_terminal_counts == expected_terminal_counts)

        # 5. Semantic Oracle Result
        semantic_oracle_result = {
            "passed": vector_match,
            "classification": "PASS",
            "details": [],
            "expected_terminal_counts": expected_terminal_counts,
            "actual_terminal_counts": actual_terminal_counts,
            "vector_match": vector_match
        }
        if not vector_match:
            semantic_oracle_result["details"].append(
                f"Terminal counts mismatch: expected {expected_terminal_counts}, got {actual_terminal_counts}"
            )

        promoted_count = actual_counts.get(ProposalStatus.PROMOTED.value, 0)

        # Check assertions against DocumentStore
        golden = oracle.get("golden_assertions", {})
        for path, assertions in golden.items():
            try:
                content = doc_store.get_document(path).get_content()
                for expected in assertions.get("must_contain", []):
                    if expected not in content:
                        semantic_oracle_result["passed"] = False
                        semantic_oracle_result["details"].append(f"Missing expected content in {path}: {expected}")
                for unexpected in assertions.get("must_not_contain", []):
                    if unexpected in content:
                        semantic_oracle_result["passed"] = False
                        semantic_oracle_result["details"].append(f"Found forbidden content in {path}: {unexpected}")
            except Exception as e:
                semantic_oracle_result["passed"] = False
                semantic_oracle_result["details"].append(f"Error reading {path}: {str(e)}")

        # Check outcome alignment
        actual_promotions = [e for e in promotion_events if e.event_type == EventType.PROPOSAL_PROMOTED]
        
        from packages.aether.models.entities import Proposal, ProposalStatus
        superseded_or_rejected = 0
        for prop in session.query(Proposal).filter_by(repo_id=repo.id).all():
            if prop.status in (ProposalStatus.SUPERSEDED, ProposalStatus.REJECTED):
                superseded_or_rejected += 1

        expected_outcome = oracle.get("expected_outcome", "PROMOTED")

        # Check if any proposal was quarantined due to oracle insufficiency
        has_oracle_quarantine = False
        for prop in session.query(Proposal).filter_by(repo_id=repo.id).all():
            if prop.status == ProposalStatus.QUARANTINED:
                has_oracle_quarantine = True
                break

        if expected_outcome == "PROMOTED":
            if has_oracle_quarantine:
                semantic_oracle_result["passed"] = False
                semantic_oracle_result["classification"] = "FAIL_ORACLE_SUFFICIENCY"
                semantic_oracle_result["details"].append("Engine native quarantine: oracle_insufficiency_escalated")
            elif not actual_promotions:
                semantic_oracle_result["passed"] = False
                semantic_oracle_result["classification"] = "FAIL_CORRECTNESS"
                semantic_oracle_result["details"].append("Expected promotion but none succeeded")
            elif not semantic_oracle_result["passed"]:
                semantic_oracle_result["classification"] = "FAIL_ORACLE_SUFFICIENCY"
            else:
                semantic_oracle_result["classification"] = "PASS"
        else:
            # Expected outcome is REJECTED or QUARANTINED
            if actual_promotions:
                semantic_oracle_result["passed"] = False
                semantic_oracle_result["classification"] = "FAIL_GATE_INTEGRITY"
                semantic_oracle_result["details"].append(f"Expected {expected_outcome} but proposal was promoted")
            else:
                # Actual matches expected (rejected/quarantined)
                if not semantic_oracle_result["passed"]:
                    semantic_oracle_result["classification"] = "FAIL_ORACLE_SUFFICIENCY"
                else:
                    semantic_oracle_result["classification"] = "PASS"
            
            # Special override for multi-agent conflict resolution
            is_conflict_rejection = len(oracle.get("agents", [])) > 1 and superseded_or_rejected > 0
            if is_conflict_rejection and expected_outcome != "PROMOTED":
                # In conflict rejection scenarios, it's correct for Aether to resolve it by rejecting at least one,
                # even if the golden assertions failed (e.g. because we promoted one, which is not rejected).
                semantic_oracle_result["details"].append("Conflict resolved by superseding/rejecting at least one proposal")

        # Force passed to False if terminal counts mismatch
        if not vector_match:
            semantic_oracle_result["passed"] = False

        # 6. Write remaining output contracts (resource_metrics, summary, final_repo_snapshot)
        elapsed_sec = time.perf_counter() - start_perf
        
        db_size = 0
        db_file = out_dir / "aether.db"
        if db_file.exists():
            db_size = db_file.stat().st_size
            
        resource_metrics = [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cpu_percent": 0.0,
                "memory_bytes": 0,
                "db_size_bytes": db_size,
                "promoted_count": promoted_count,
                "superseded_or_rejected": superseded_or_rejected
            }
        ]
        with open(out_dir / "resource_metrics.jsonl", "w") as f:
            for met in resource_metrics:
                f.write(json.dumps(met) + "\n")
                
        with open(out_dir / "summary.json", "w") as f:
            json.dump({
                "scenario": oracle["scenario"],
                "passed": semantic_oracle_result["passed"],
                "classification": semantic_oracle_result["classification"],
                "total_proposals": len(oracle.get("agents", [])),
                "promoted_proposals": promoted_count,
                "rejected_proposals": superseded_or_rejected,
                "duration_seconds": elapsed_sec
            }, f, indent=2)

        # Write final_repo_snapshot
        final_snapshot_dir = out_dir / "final_repo_snapshot"
        final_snapshot_dir.mkdir(parents=True, exist_ok=True)
        for rel_path in doc_store.documents.keys():
            content = doc_store.get_document(rel_path).get_content()
            dest = final_snapshot_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w") as f:
                f.write(content)

        with open(out_dir / "semantic_oracle_result.json", "w") as f:
            json.dump(semantic_oracle_result, f, indent=2)
            
        logger.info(f"Cascade load finished. Outcome classification: {semantic_oracle_result['classification']}")
        
def main():
    parser = setup_argparse()
    args = parser.parse_args()
    
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    with open(out_dir / "run_config.json", "w") as f:
        json.dump(vars(args), f, indent=2)
        
    oracle_path = Path(f"tests/fixtures/phase4/{args.scenario}/oracle.json")
    if not oracle_path.exists():
        logger.error(f"Oracle fixture not found at {oracle_path}")
        sys.exit(1)
        
    with open(oracle_path) as f:
        oracle = json.load(f)
        
    storage_path = out_dir / "aether_storage"
    storage_path.mkdir(exist_ok=True)
    
    db_path = out_dir / "aether.db"
    
    if args.db_profile == "sqlite":
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
    else:
        # Postgres mode
        db_url = os.environ.get("DATABASE_URL", "postgres://localhost/aether_test")
        engine = create_engine(db_url)
        
    SQLModel.metadata.create_all(engine)
    
    asyncio.run(run_harness(args, oracle, engine, storage_path, out_dir))

if __name__ == "__main__":
    main()
