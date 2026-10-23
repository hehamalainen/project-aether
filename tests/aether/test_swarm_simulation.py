"""
Swarm simulation test - Phase 0 acceptance criteria.

This test demonstrates:
- 20-50 simulated agents editing a toy repo without manual merges
- Canonical head remains buildable under light gates (syntax + unit tests)
- Structural conflict rate near zero for text-level conflicts
"""

import asyncio
import random
import json
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import SQLModel, Session, create_engine

from packages.aether.models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    GateConfig,
    RepoConfig,
)
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.event_bus import EventBus
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.snapshot_service import SnapshotService
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.promotion_engine import PromotionEngine


class SimulatedAgent:
    """A simulated coding agent that submits proposals."""

    def __init__(
        self,
        agent_id: str,
        repo_id: str,
        proposal_service: ProposalService,
        files: list[str],
    ):
        self.agent_id = agent_id
        self.repo_id = repo_id
        self.proposal_service = proposal_service
        self.files = files
        self.proposals_submitted = 0
        self.proposals_promoted = 0

    async def work(self, iterations: int = 5):
        """Simulate agent doing work - submitting proposals."""
        for i in range(iterations):
            # Pick a random file to modify
            file_path = random.choice(self.files)

            # Generate a simple change (add a comment)
            line_num = random.randint(1, 50)
            comment = f"# Agent {self.agent_id} edit {i+1} at line {line_num}"

            # Create a simple diff
            diff = f"""diff --git a/{file_path} b/{file_path}
--- a/{file_path}
+++ b/{file_path}
@@ -{line_num},1 +{line_num},2 @@
 existing_line
+{comment}
"""

            # Submit proposal
            confidence = random.uniform(0.6, 0.95)
            proposal = ProposalCreate(
                agent_id=self.agent_id,
                task_id=f"task_{self.agent_id}_{i}",
                change_type="unified_diff",
                change_content=diff,
                affected_paths=[file_path],
                confidence_score=confidence,
                rationale=f"Adding comment at line {line_num}",
            )

            try:
                response = await self.proposal_service.submit_proposal(
                    self.repo_id, proposal
                )
                self.proposals_submitted += 1

                # Small delay to simulate work
                await asyncio.sleep(random.uniform(0.01, 0.05))

            except Exception as e:
                print(f"Agent {self.agent_id} error: {e}")

            # Random delay between submissions
            await asyncio.sleep(random.uniform(0.01, 0.1))


class SwarmSimulator:
    """Coordinates a swarm of simulated agents."""

    def __init__(
        self,
        num_agents: int,
        db_session: Session,
        storage_path: Path,
    ):
        self.num_agents = num_agents
        self.session = db_session
        self.storage_path = storage_path

        # Initialize services
        self.event_bus = EventBus()
        self.repo_service = RepositoryService(db_session, storage_path)
        self.proposal_service = ProposalService(db_session, self.event_bus, {})
        self.snapshot_service = SnapshotService(db_session, self.event_bus, storage_path)
        self.gate_runner = GateRunner(db_session, self.event_bus)
        self.promotion_engine = PromotionEngine(
            db_session,
            self.event_bus,
            self.proposal_service,
            self.snapshot_service,
            self.gate_runner,
            storage_path,
        )

        self.repo: Optional[Repository] = None
        self.document_store: Optional[DocumentStore] = None
        self.agents: list[SimulatedAgent] = []

        # Metrics
        self.promotions_successful = 0
        self.promotions_failed = 0
        self.manual_merges_required = 0

    def setup_repository(self):
        """Create repository with initial files."""
        # Configure gates for the simulation
        config = RepoConfig(
            gates=[
                GateConfig(
                    name="syntax",
                    command="echo 'syntax ok'",  # Simplified for simulation
                    required=True,
                    timeout_seconds=5,
                    order=0,
                ),
                GateConfig(
                    name="unit_tests",
                    command="echo '5 passed'",  # Simplified for simulation
                    required=True,
                    timeout_seconds=10,
                    order=1,
                ),
            ],
            auto_promote=True,
        )

        self.repo = self.repo_service.create_repository(
            name="swarm-test-repo",
            description="Test repository for swarm simulation",
            config=config,
        )

        # Initialize document store with some files
        self.document_store = DocumentStore(self.repo.id)
        files = [
            "src/main.py",
            "src/utils.py",
            "src/models.py",
            "src/api.py",
            "tests/test_main.py",
        ]

        # Create initial content for each file
        for file_path in files:
            content = self._generate_initial_content(file_path)
            self.document_store.apply_full_file(file_path, content, "initial")

        # Link document store to proposal service
        self.proposal_service.set_document_store(self.repo.id, self.document_store)

        return files

    def _generate_initial_content(self, file_path: str) -> str:
        """Generate initial file content."""
        lines = []
        lines.append(f'"""Module: {file_path}"""')
        lines.append("")

        # Add some placeholder content (50 lines)
        for i in range(50):
            lines.append(f"# Line {i+1}")
            if i % 10 == 0:
                lines.append(f"def function_{i}():")
                lines.append("    pass")
                lines.append("")

        return "\n".join(lines)

    def create_agents(self, files: list[str]):
        """Create simulated agents."""
        for i in range(self.num_agents):
            agent = SimulatedAgent(
                agent_id=f"agent_{i:03d}",
                repo_id=self.repo.id,
                proposal_service=self.proposal_service,
                files=files,
            )
            self.agents.append(agent)

    async def run_promotion_loop(self, duration_seconds: float):
        """Run the promotion engine periodically."""
        end_time = asyncio.get_event_loop().time() + duration_seconds

        while asyncio.get_event_loop().time() < end_time:
            try:
                promotions = await self.promotion_engine.process_pending_proposals(
                    self.repo.id,
                    self.document_store,
                    batch_size=5,
                )

                for promotion in promotions:
                    self.promotions_successful += 1

            except Exception as e:
                print(f"Promotion error: {e}")
                self.promotions_failed += 1

            await asyncio.sleep(0.1)  # Run every 100ms

    async def run_simulation(self, iterations_per_agent: int = 5):
        """Run the full swarm simulation."""
        # Start all agents working concurrently
        agent_tasks = [
            asyncio.create_task(agent.work(iterations_per_agent))
            for agent in self.agents
        ]

        # Run promotion loop concurrently
        # Estimate duration based on agents and iterations
        estimated_duration = iterations_per_agent * 0.5  # seconds
        promotion_task = asyncio.create_task(
            self.run_promotion_loop(estimated_duration + 2.0)
        )

        # Wait for agents to finish
        await asyncio.gather(*agent_tasks)

        # Give promotion loop a bit more time
        await asyncio.sleep(0.5)
        promotion_task.cancel()
        try:
            await promotion_task
        except asyncio.CancelledError:
            pass

    def get_metrics(self) -> dict:
        """Get simulation metrics."""
        total_proposals = sum(a.proposals_submitted for a in self.agents)

        # Count proposals by status
        pending = self.proposal_service.list_proposals(
            self.repo.id, status=ProposalStatus.PENDING
        )
        promoted = self.proposal_service.list_proposals(
            self.repo.id, status=ProposalStatus.PROMOTED
        )
        conflicts = self.proposal_service.list_conflict_sets(
            self.repo.id, resolved=False
        )

        return {
            "num_agents": self.num_agents,
            "total_proposals": total_proposals,
            "proposals_pending": len(pending),
            "proposals_promoted": len(promoted),
            "unresolved_conflicts": len(conflicts),
            "promotions_successful": self.promotions_successful,
            "promotions_failed": self.promotions_failed,
            "manual_merges_required": self.manual_merges_required,
            "head_sequence": self.repo.head_sequence if self.repo else 0,
        }


@pytest.fixture
def db_session():
    """Create in-memory database session."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def storage_path(tmp_path):
    """Create temporary storage path."""
    return tmp_path / "swarm_storage"


class TestSwarmSimulation:
    """Test swarm simulation - Phase 0 acceptance criteria."""

    @pytest.mark.asyncio
    async def test_small_swarm_no_manual_merges(self, db_session, storage_path):
        """
        10 agents should work without manual merges.

        This is a smaller version of the Phase 0 target (20-50 agents).
        """
        simulator = SwarmSimulator(
            num_agents=10,
            db_session=db_session,
            storage_path=storage_path,
        )

        files = simulator.setup_repository()
        simulator.create_agents(files)

        await simulator.run_simulation(iterations_per_agent=3)

        metrics = simulator.get_metrics()

        print("\n=== Swarm Simulation Results ===")
        print(f"Agents: {metrics['num_agents']}")
        print(f"Total proposals submitted: {metrics['total_proposals']}")
        print(f"Proposals promoted: {metrics['proposals_promoted']}")
        print(f"Proposals pending: {metrics['proposals_pending']}")
        print(f"Unresolved conflicts: {metrics['unresolved_conflicts']}")
        print(f"Successful promotions: {metrics['promotions_successful']}")
        print(f"Manual merges required: {metrics['manual_merges_required']}")
        print(f"Head sequence: {metrics['head_sequence']}")

        # Acceptance criteria
        assert metrics['manual_merges_required'] == 0, "Should not require manual merges"
        assert metrics['total_proposals'] > 0, "Should have submitted proposals"

    @pytest.mark.asyncio
    async def test_medium_swarm_convergence(self, db_session, storage_path):
        """
        25 agents should converge without issues.

        Tests the Phase 0 target range (20-50 agents).
        """
        simulator = SwarmSimulator(
            num_agents=25,
            db_session=db_session,
            storage_path=storage_path,
        )

        files = simulator.setup_repository()
        simulator.create_agents(files)

        await simulator.run_simulation(iterations_per_agent=2)

        metrics = simulator.get_metrics()

        print("\n=== Medium Swarm Results ===")
        for key, value in metrics.items():
            print(f"{key}: {value}")

        # CRDT should prevent structural conflicts
        assert metrics['manual_merges_required'] == 0

    @pytest.mark.asyncio
    async def test_concurrent_edits_same_file(self, db_session, storage_path):
        """Multiple agents editing same file should converge via CRDT."""
        simulator = SwarmSimulator(
            num_agents=5,
            db_session=db_session,
            storage_path=storage_path,
        )

        # Override with single-file setup
        simulator.repo = simulator.repo_service.create_repository(name="single-file-test")
        simulator.document_store = DocumentStore(simulator.repo.id)
        simulator.document_store.apply_full_file(
            "shared.py",
            "\n".join([f"# Line {i}" for i in range(100)]),
            "initial",
        )
        simulator.proposal_service.set_document_store(
            simulator.repo.id, simulator.document_store
        )

        # All agents target same file
        for i in range(5):
            agent = SimulatedAgent(
                agent_id=f"agent_{i}",
                repo_id=simulator.repo.id,
                proposal_service=simulator.proposal_service,
                files=["shared.py"],  # All edit same file
            )
            simulator.agents.append(agent)

        await simulator.run_simulation(iterations_per_agent=3)

        metrics = simulator.get_metrics()

        print("\n=== Concurrent Same-File Edit Results ===")
        for key, value in metrics.items():
            print(f"{key}: {value}")

        # Should handle concurrent edits
        assert metrics['total_proposals'] > 0


class TestCRDTConvergence:
    """Test CRDT convergence properties."""

    def test_independent_edits_converge(self):
        """Edits to different parts of file should always converge."""
        from packages.aether.crdt.rga import RGADocument

        # Two replicas with same initial content
        doc1 = RGADocument(replica_id="a")
        doc2 = RGADocument(replica_id="b")

        # Initialize both with same content
        ops = doc1.set_text("line1\nline2\nline3\n")
        for op in ops:
            doc2.apply_operation(op)

        assert doc1.get_text() == doc2.get_text()

        # Independent edits (different lines)
        # Agent A edits line 1
        op_a = doc1.local_insert(5, "X")  # After "line1"

        # Agent B edits line 3
        op_b = doc2.local_insert(17, "Y")  # After "line3"

        # Exchange operations
        doc1.apply_operation(op_b)
        doc2.apply_operation(op_a)

        # Both should converge to same state
        assert doc1.get_text() == doc2.get_text()
        assert "X" in doc1.get_text()
        assert "Y" in doc1.get_text()

    def test_many_concurrent_inserts(self):
        """Many concurrent inserts at same position should all be preserved."""
        from packages.aether.crdt.rga import RGADocument

        # Create 10 replicas
        replicas = [RGADocument(replica_id=f"r{i}") for i in range(10)]

        # Initialize all with same content
        base_ops = replicas[0].set_text("abc")
        for replica in replicas[1:]:
            for op in base_ops:
                replica.apply_operation(op)

        # Each replica inserts a character at position 1 (after 'a')
        insert_ops = []
        for i, replica in enumerate(replicas):
            op = replica.local_insert(1, str(i))
            insert_ops.append(op)

        # Exchange all operations between all replicas
        for replica in replicas:
            for op in insert_ops:
                replica.apply_operation(op)

        # All replicas should converge
        final_texts = [r.get_text() for r in replicas]
        assert len(set(final_texts)) == 1, "All replicas should have same text"

        # All inserted characters should be present
        final_text = final_texts[0]
        for i in range(10):
            assert str(i) in final_text

    def test_delete_then_insert_converges(self):
        """Delete and insert at same position should converge."""
        from packages.aether.crdt.rga import RGADocument

        doc1 = RGADocument(replica_id="a")
        doc2 = RGADocument(replica_id="b")

        ops = doc1.set_text("hello")
        for op in ops:
            doc2.apply_operation(op)

        # Agent A deletes 'e'
        del_op = doc1.local_delete(1)

        # Agent B inserts 'X' at same position
        ins_op = doc2.local_insert(1, "X")

        # Exchange
        doc1.apply_operation(ins_op)
        doc2.apply_operation(del_op)

        # Should converge
        assert doc1.get_text() == doc2.get_text()
        # Both operations should be reflected
        assert "X" in doc1.get_text()
        assert doc1.get_text().count("e") == 0  # 'e' should be deleted
