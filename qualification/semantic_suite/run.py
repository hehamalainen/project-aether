import os
import sys
import json
import hashlib
import time
import argparse
import asyncio
import shutil
import tempfile
import subprocess
from pathlib import Path
from sqlmodel import Session, create_engine, SQLModel, select

from packages.aether.models.entities import (
    Repository, Proposal, ProposalStatus, Promotion, AetherHead,
    ProposalContention, SystemMetricEvent, SystemMetricEventType,
    ContentionWinner, ConflictSet
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine


def make_diff(filename: str, old_start: int, old_count: int, new_start: int, new_count: int, lines: list[str]) -> str:
    diff_text = f"diff --git a/{filename} b/{filename}\n"
    diff_text += f"--- a/{filename}\n"
    diff_text += f"+++ b/{filename}\n"
    diff_text += f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"
    for line in lines:
        diff_text += f"{line}\n"
    return diff_text


REPO_ROOT = Path(__file__).resolve().parents[2]


def get_git_info():
    git_dir = os.environ.get("AETHER_GIT_DIR", str(REPO_ROOT))
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_dir).decode().strip()
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_dir).decode().strip()
        return branch, sha
    except Exception as e:
        raise RuntimeError(f"Failed to dynamically query Git identity: {e}")


def get_postgres_engine():
    current_user = os.environ.get("PGUSER") or os.environ.get("USER") or "postgres"
    urls = [
        os.environ.get("DATABASE_URL"),
        "postgresql://localhost/postgres",
        f"postgresql://{current_user}@localhost/postgres",
    ]
    urls = [u for u in urls if u]
    
    last_err = None
    for url in urls:
        try:
            engine = create_engine(url)
            with engine.connect() as conn:
                pass
            return engine
        except Exception as e:
            last_err = e
            continue
            
    print(f"Error: Failed to connect to any PostgreSQL database. Urls tried: {urls}. Detail: {last_err}", file=sys.stderr)
    sys.exit(1)


# Setup local temp directory inside Aether project for isolated runs
QUAL_DIR = Path(__file__).resolve().parent
QUAL_DIR.mkdir(parents=True, exist_ok=True)


async def run_case_1(session, storage_path):
    """
    Shape 1: class_member_collisions (disjoint methods added to the same class).
    Must resolve via structural_merge without synthesis fallback.
    """
    eb = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, eb, {})
    snapshot_service = SnapshotService(session, eb, storage_path)
    gate_runner = GateRunner(session, eb)
    promotion_engine = PromotionEngine(
        session, eb, proposal_service, snapshot_service, gate_runner, storage_path,
        gate_execution_mode="sequential"
    )
    promotion_engine.semantic_merge_enabled = True
    promotion_engine.synthesis_worker_enabled = False # Ensure no synthesis fallback is possible

    # Configure repository with gate config running echo 'ok'
    gate_config_json = json.dumps({
        "gates": [
            {"name": "test_gate", "command": "echo 'ok'", "required": True, "timeout_seconds": 60, "order": 0}
        ]
    })
    repo = Repository(
        id="repo-1",
        name="test-repo-1",
        workspace_path=str(storage_path / "repo-1-work"),
        gate_config=gate_config_json,
        max_rebase_attempts=2
    )
    session.add(repo)
    session.commit()

    # Initial file setup
    doc_store = DocumentStore(storage_path)
    base_file = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = 1\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    session.add(repo)
    session.commit()

    # Proposal 1 adds get_name (higher score/priority)
    diff_a = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_name(self):",
            "         return 'Alice'",
        ]
    )
    p1 = Proposal(
        id="p1_1",
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )

    # Proposal 2 adds get_email (lower score)
    diff_b = make_diff(
        "user.py", 2, 2, 2, 6,
        [
            "     def __init__(self):",
            "         self.id = 1",
            "",
            "     def get_email(self):",
            "         return 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        id="p1_2",
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_email"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.8
    )

    # Contention metadata to opt into semantic_merge
    contention_1 = ProposalContention(
        proposal_id=p1.id,
        contention_group_id="class_member_collision_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )
    contention_2 = ProposalContention(
        proposal_id=p2.id,
        contention_group_id="class_member_collision_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )

    session.add(p1)
    session.add(p2)
    session.add(contention_1)
    session.add(contention_2)
    session.commit()

    # Process Proposal 1
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p1)
    session.refresh(p2)
    assert p1.status == ProposalStatus.PROMOTED, f"Proposal 1 should be promoted, got {p1.status}"

    # Process Proposal 2 (conflict on base, resolves structurally)
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p2)
    assert p2.status == ProposalStatus.PROMOTED, f"Proposal 2 should be promoted, got {p2.status}"

    # Verify provenance
    prov = proposal_service.get_proposal_provenance(p2.id)
    assert prov is not None, "Provenance for Proposal 2 should exist"
    assert prov.event_type == "structural_merge", f"Expected structural_merge, got {prov.event_type}"

    # Verify merged document content
    session.refresh(repo)
    head_record = session.get(AetherHead, repo.head_id)
    latest_snap = snapshot_service.get_snapshot(head_record.snapshot_id)
    merged_content = snapshot_service.get_file_content(latest_snap.id, "user.py")
    assert "def get_name(self):" in merged_content, "get_name missing from merged file"
    assert "def get_email(self):" in merged_content, "get_email missing from merged file"

    return [p1, p2]


async def run_case_2(session, storage_path):
    """
    Shape 2: ast_edit_block_overlaps (modifying disjoint methods in the same class/file block).
    Must resolve via structural_merge.
    """
    eb = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, eb, {})
    snapshot_service = SnapshotService(session, eb, storage_path)
    gate_runner = GateRunner(session, eb)
    promotion_engine = PromotionEngine(
        session, eb, proposal_service, snapshot_service, gate_runner, storage_path,
        gate_execution_mode="sequential"
    )
    promotion_engine.semantic_merge_enabled = True
    promotion_engine.synthesis_worker_enabled = False

    # Configure repository with gate config running echo 'ok'
    gate_config_json = json.dumps({
        "gates": [
            {"name": "test_gate", "command": "echo 'ok'", "required": True, "timeout_seconds": 60, "order": 0}
        ]
    })
    repo = Repository(
        id="repo-2",
        name="test-repo-2",
        workspace_path=str(storage_path / "repo-2-work"),
        gate_config=gate_config_json,
        max_rebase_attempts=2
    )
    session.add(repo)
    session.commit()

    # Initial file setup with disjoint methods
    doc_store = DocumentStore(storage_path)
    base_file = (
        "class User:\n"
        "    def get_name(self):\n"
        "        return 'Guest'\n"
        "\n"
        "    def get_email(self):\n"
        "        return 'guest@example.com'\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    session.add(repo)
    session.commit()

    # Proposal 1 modifies get_name
    diff_a = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def get_name(self):",
            "-        return 'Guest'",
            "+        return 'Alice'",
        ]
    )
    p1 = Proposal(
        id="p2_1",
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_name"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )

    # Proposal 2 modifies get_email
    diff_b = make_diff(
        "user.py", 5, 2, 5, 2,
        [
            "     def get_email(self):",
            "-        return 'guest@example.com'",
            "+        return 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        id="p2_2",
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:get_email"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.8
    )

    # Contention metadata to opt into semantic_merge
    contention_1 = ProposalContention(
        proposal_id=p1.id,
        contention_group_id="ast_edit_block_overlap_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )
    contention_2 = ProposalContention(
        proposal_id=p2.id,
        contention_group_id="ast_edit_block_overlap_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )

    session.add(p1)
    session.add(p2)
    session.add(contention_1)
    session.add(contention_2)
    session.commit()

    # Process Proposal 1
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p1)
    session.refresh(p2)
    assert p1.status == ProposalStatus.PROMOTED, f"Proposal 1 should be promoted, got {p1.status}"

    # Process Proposal 2
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p2)
    assert p2.status == ProposalStatus.PROMOTED, f"Proposal 2 should be promoted, got {p2.status}"

    # Verify provenance
    prov = proposal_service.get_proposal_provenance(p2.id)
    assert prov is not None, "Provenance for Proposal 2 should exist"
    assert prov.event_type == "structural_merge", f"Expected structural_merge, got {prov.event_type}"

    # Verify merged document content
    session.refresh(repo)
    head_record = session.get(AetherHead, repo.head_id)
    latest_snap = snapshot_service.get_snapshot(head_record.snapshot_id)
    merged_content = snapshot_service.get_file_content(latest_snap.id, "user.py")
    assert "return 'Alice'" in merged_content, "get_name modifications missing from merged file"
    assert "return 'alice@example.com'" in merged_content, "get_email modifications missing from merged file"

    return [p1, p2]


async def run_case_3(session, storage_path):
    """
    Shape 3: synthesis_fallback_merges (overlapping changes on the same method requiring LLM reconciliation).
    Must resolve via synthesis_merge.
    """
    eb = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, eb, {})
    snapshot_service = SnapshotService(session, eb, storage_path)
    gate_runner = GateRunner(session, eb)
    promotion_engine = PromotionEngine(
        session, eb, proposal_service, snapshot_service, gate_runner, storage_path,
        gate_execution_mode="sequential"
    )
    promotion_engine.semantic_merge_enabled = True
    promotion_engine.synthesis_worker_enabled = True

    # Configure repository with gate config running echo 'ok'
    gate_config_json = json.dumps({
        "gates": [
            {"name": "test_gate", "command": "echo 'ok'", "required": True, "timeout_seconds": 60, "order": 0}
        ]
    })
    repo = Repository(
        id="repo-3",
        name="test-repo-3",
        workspace_path=str(storage_path / "repo-3-work"),
        gate_config=gate_config_json,
        max_rebase_attempts=2
    )
    session.add(repo)
    session.commit()

    # Initial file setup
    doc_store = DocumentStore(storage_path)
    base_file = (
        "class User:\n"
        "    def update_profile(self):\n"
        "        pass\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    session.add(repo)
    session.commit()

    # Proposal 1 adds self.name = 'Alice' in update_profile
    diff_a = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.name = 'Alice'",
        ]
    )
    p1 = Proposal(
        id="p3_1",
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )

    # Proposal 2 adds self.email = 'alice@example.com' in update_profile
    diff_b = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.email = 'alice@example.com'",
        ]
    )
    p2 = Proposal(
        id="p3_2",
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.8
    )

    # Contention metadata to opt into semantic_merge
    contention_1 = ProposalContention(
        proposal_id=p1.id,
        contention_group_id="synthesis_fallback_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )
    contention_2 = ProposalContention(
        proposal_id=p2.id,
        contention_group_id="synthesis_fallback_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )

    session.add(p1)
    session.add(p2)
    session.add(contention_1)
    session.add(contention_2)
    session.commit()

    # Process Proposal 1
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p1)
    session.refresh(p2)
    assert p1.status == ProposalStatus.PROMOTED, f"Proposal 1 should be promoted, got {p1.status}"

    # Process Proposal 2
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=1)
    session.refresh(p2)
    assert p2.status == ProposalStatus.PROMOTED, f"Proposal 2 should be promoted, got {p2.status}"

    # Verify provenance shows synthesis_merge
    prov = proposal_service.get_proposal_provenance(p2.id)
    assert prov is not None, "Provenance for Proposal 2 should exist"
    assert prov.event_type == "synthesis_merge", f"Expected synthesis_merge, got {prov.event_type}"

    # Verify merged document content has BOTH statements
    session.refresh(repo)
    head_record = session.get(AetherHead, repo.head_id)
    latest_snap = snapshot_service.get_snapshot(head_record.snapshot_id)
    merged_content = snapshot_service.get_file_content(latest_snap.id, "user.py")
    assert "self.name = 'Alice'" in merged_content, "self.name missing from merged file"
    assert "self.email = 'alice@example.com'" in merged_content, "self.email missing from merged file"

    return [p1, p2]


async def run_case_4(session, storage_path):
    """
    Shape 4: negative_control (mutually exclusive contradictory edits on the same variable, e.g. self.name = 'Alice' vs self.name = 'Bob').
    Must end in SUPERSEDED status with a rebase_budget_exhaustion reason.
    """
    eb = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, eb, {})
    snapshot_service = SnapshotService(session, eb, storage_path)
    gate_runner = GateRunner(session, eb)
    promotion_engine = PromotionEngine(
        session, eb, proposal_service, snapshot_service, gate_runner, storage_path,
        gate_execution_mode="sequential"
    )
    promotion_engine.semantic_merge_enabled = True
    promotion_engine.synthesis_worker_enabled = True

    # Configure repository with gate config running echo 'ok' and max_rebase_attempts = 0
    gate_config_json = json.dumps({
        "gates": [
            {"name": "test_gate", "command": "echo 'ok'", "required": True, "timeout_seconds": 60, "order": 0}
        ]
    })
    repo = Repository(
        id="repo-4",
        name="test-repo-4",
        workspace_path=str(storage_path / "repo-4-work"),
        gate_config=gate_config_json,
        max_rebase_attempts=0  # Zero rebase budget!
    )
    session.add(repo)
    session.commit()

    # Initial file setup
    doc_store = DocumentStore(storage_path)
    base_file = (
        "class User:\n"
        "    def update_profile(self):\n"
        "        pass\n"
    )
    doc_store.apply_full_file("user.py", base_file, "init")
    
    snap = await snapshot_service.create_snapshot(repo.id, doc_store, [])
    repo.head_id = snap.id
    session.add(repo)
    session.commit()

    # Proposal 1 sets self.name = 'Alice'
    diff_a = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.name = 'Alice'",
        ]
    )
    p1 = Proposal(
        id="p4_1",
        repo_id=repo.id,
        agent_id="agent-1",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_a,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.9
    )

    # Proposal 2 sets self.name = 'Bob'
    diff_b = make_diff(
        "user.py", 2, 2, 2, 2,
        [
            "     def update_profile(self):",
            "-        pass",
            "+        self.name = 'Bob'",
        ]
    )
    # Set rebase_attempts initially to 1 to exceed the max_rebase_attempts = 0
    p2 = Proposal(
        id="p4_2",
        repo_id=repo.id,
        agent_id="agent-2",
        base_head_id=snap.id,
        base_head_sequence=0,
        change_type="unified_diff",
        change_content=diff_b,
        affected_paths=json.dumps(["user.py"]),
        affected_symbols=json.dumps(["user.py::CLASS:User", "user.py::CLASS:User::FUNC:update_profile"]),
        status=ProposalStatus.PENDING,
        confidence_score=0.8,
        rebase_attempts=1
    )

    # Let's put them in a ConflictSet together
    cs = ConflictSet(
        id="cs_4",
        repo_id=repo.id,
        path="user.py",
        proposal_ids=json.dumps([p1.id, p2.id]),
        resolved=False,
    )
    p1.conflict_set_id = cs.id
    p2.conflict_set_id = cs.id

    # Contention metadata to opt into semantic_merge
    contention_1 = ProposalContention(
        proposal_id=p1.id,
        contention_group_id="negative_control_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )
    contention_2 = ProposalContention(
        proposal_id=p2.id,
        contention_group_id="negative_control_group",
        contention_policy="semantic_merge",
        target_paths=json.dumps(["user.py"]),
    )

    session.add(p1)
    session.add(p2)
    session.add(cs)
    session.add(contention_1)
    session.add(contention_2)
    session.commit()

    # Process pending queue: the conflict set resolution picks Proposal 1 (higher score) as the winner,
    # and processes Proposal 2 via resolve_conflict_set, updating it since 1 > max_rebase (0).
    await promotion_engine.process_pending_proposals(repo.id, doc_store, batch_size=2)
    session.refresh(p1)
    session.refresh(p2)

    assert p1.status == ProposalStatus.PROMOTED, f"Proposal 1 should be promoted, got {p1.status}"
    assert p2.status == ProposalStatus.SUPERSEDED, f"Proposal 2 should be superseded, got {p2.status}"

    # Verify provenance for Proposal 2
    prov = proposal_service.get_proposal_provenance(p2.id)
    assert prov is not None, "Provenance for Proposal 2 should exist"
    assert prov.event_reason == "rebase_budget_exhaustion", f"Expected rebase_budget_exhaustion reason, got {prov.event_reason}"

    return [p1, p2]


async def run_suite(engine, db_profile):
    results = []
    
    cases = [
        ("class_member_collisions", run_case_1),
        ("ast_edit_block_overlaps", run_case_2),
        ("synthesis_fallback_merges", run_case_3),
        ("negative_control", run_case_4),
    ]

    for name, run_fn in cases:
        print(f"\n--- Running case: {name} ---")
        # Ensure fresh state at start of each case
        SQLModel.metadata.drop_all(engine)
        SQLModel.metadata.create_all(engine)
        
        start_time = time.perf_counter()
        status = "pass"
        proposals_data = []
        
        # Isolated storage path for this case
        storage_path = QUAL_DIR / f"storage_{name}"
        if storage_path.exists():
            shutil.rmtree(storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)
        
        try:
            with Session(engine) as session:
                proposals = await run_fn(session, storage_path)
                
                # Fetch fresh provenance and state for report
                proposal_service = ProposalService(session, EventBus(), {})
                for p in proposals:
                    session.refresh(p)
                    prov = proposal_service.get_proposal_provenance(p.id)
                    proposals_data.append({
                        "proposal_id": p.id,
                        "final_status": p.status.value if p.status else None,
                        "provenance_event_type": prov.event_type if prov else None,
                        "provenance_event_reason": prov.event_reason if prov else None,
                        "rebase_attempts": p.rebase_attempts or 0
                    })
            print(f"Case {name} PASSED successfully.")
        except Exception as e:
            print(f"Case {name} FAILED with exception: {e}")
            import traceback
            traceback.print_exc()
            status = "fail"
        finally:
            if storage_path.exists():
                shutil.rmtree(storage_path)
                
        duration = time.perf_counter() - start_time
        results.append({
            "case_name": name,
            "status": status,
            "duration_seconds": duration,
            "proposals": proposals_data
        })
        
    return results


approved_env_allowlist = ["AETHER_SEMANTIC_MERGE_ENABLED", "AETHER_SYNTHESIS_WORKER_ENABLED", "AETHER_SERVER_PROFILE"]

def compute_config_fingerprint(argv, env):
    allowlist = sorted(approved_env_allowlist)
    env_data = {k: env.get(k, "") for k in allowlist}
    
    with open(__file__, "rb") as f:
        run_py_content = f.read()
        
    h = hashlib.sha256()
    h.update(run_py_content)
    h.update(json.dumps(argv, sort_keys=True).encode("utf-8"))
    h.update(json.dumps(env_data, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def main():
    # 1. Compute clean_at_start before any writes, and fail closed if Git status fails
    try:
        status_res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
        clean_at_start = (status_res.stdout.strip() == "")
    except Exception as e:
        print(f"Error checking git status: {e}", file=sys.stderr)
        sys.exit(1)

    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("--db-profile", choices=["sqlite", "postgres"], required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    print(f"Initializing qualification suite with profile: {args.db_profile}")
    
    if args.db_profile == "sqlite":
        engine = create_engine("sqlite:///:memory:")
    else:
        engine = get_postgres_engine()
        
    branch, sha = get_git_info()
    print(f"Pinned Git Branch: {branch}")
    print(f"Pinned HEAD Commit SHA: {sha}")
    
    # Run tests
    results = asyncio.run(run_suite(engine, args.db_profile))
    
    ended_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = time.time() - start_time
    
    # Build env_data
    env_data = {k: os.environ.get(k, "") for k in approved_env_allowlist}

    report = {
        "db_profile": args.db_profile,
        "model_configuration": "simulated-synthesis-worker",
        "git_branch": branch,
        "git_commit_sha": sha,
        "command": {
            "interpreter": sys.executable,
            "script_path": str(Path(__file__).resolve()),
            "arguments": sys.argv,
            "cwd": os.getcwd()
        },
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "runtime_info": {
            "python_version": sys.version,
            "platform": sys.platform,
            "env_vars": env_data
        },
        "clean_at_start": clean_at_start,
        "cases": results
    }
    
    # Compute runner config fingerprint
    runner_config_fingerprint = compute_config_fingerprint(sys.argv, os.environ)
    report["runner_config_fingerprint"] = runner_config_fingerprint
    
    # Write to versioned output filename to preserve original report files
    output_dir = Path(args.output_dir) if args.output_dir else QUAL_DIR
    report_file = output_dir / f"report_{args.db_profile}_v2_{sha}.json"
    
    report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWritten suite execution report to: {report_file}")
    
    # Exit code based on overall status
    if any(r["status"] == "fail" for r in results):
        print("Qualification suite FAILED.")
        sys.exit(1)
    else:
        print("Qualification suite PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    main()
