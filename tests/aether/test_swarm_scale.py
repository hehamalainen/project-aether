"""
Enhanced Swarm Testing - Step 3 Scalability Tests.

Tests:
- 50-100 concurrent agents
- Metrics collection (throughput, latency, conflicts)
- Stress test scenarios (high contention, wide spread, burst traffic)
"""

import asyncio
import os
import random
import time
import statistics
from dataclasses import dataclass, field
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


@dataclass
class SwarmMetrics:
    """Collected metrics from swarm simulation."""
    
    # Counts
    total_proposals: int = 0
    proposals_promoted: int = 0
    proposals_rejected: int = 0
    proposals_pending: int = 0
    conflicts_detected: int = 0
    conflicts_resolved: int = 0
    
    # Timing (in milliseconds)
    proposal_latencies: list = field(default_factory=list)
    promotion_latencies: list = field(default_factory=list)
    
    # Rates
    start_time: float = 0
    end_time: float = 0
    
    @property
    def duration_seconds(self) -> float:
        return self.end_time - self.start_time
    
    @property
    def throughput_proposals_per_sec(self) -> float:
        if self.duration_seconds > 0:
            return self.total_proposals / self.duration_seconds
        return 0
    
    @property
    def promotion_success_rate(self) -> float:
        total = self.proposals_promoted + self.proposals_rejected
        if total > 0:
            return self.proposals_promoted / total
        return 0
    
    @property
    def conflict_rate(self) -> float:
        if self.total_proposals > 0:
            return self.conflicts_detected / self.total_proposals
        return 0
    
    @property
    def p50_latency(self) -> float:
        if self.proposal_latencies:
            return statistics.median(self.proposal_latencies)
        return 0
    
    @property
    def p95_latency(self) -> float:
        if len(self.proposal_latencies) >= 20:
            sorted_latencies = sorted(self.proposal_latencies)
            idx = int(len(sorted_latencies) * 0.95)
            return sorted_latencies[idx]
        return max(self.proposal_latencies) if self.proposal_latencies else 0
    
    @property
    def p99_latency(self) -> float:
        if len(self.proposal_latencies) >= 100:
            sorted_latencies = sorted(self.proposal_latencies)
            idx = int(len(sorted_latencies) * 0.99)
            return sorted_latencies[idx]
        return max(self.proposal_latencies) if self.proposal_latencies else 0
    
    def summary(self) -> str:
        return f"""
=== Swarm Simulation Metrics ===
Duration:             {self.duration_seconds:.2f}s
Total Proposals:      {self.total_proposals}
Throughput:           {self.throughput_proposals_per_sec:.1f} proposals/sec

Proposals Promoted:   {self.proposals_promoted}
Proposals Rejected:   {self.proposals_rejected}
Proposals Pending:    {self.proposals_pending}
Promotion Success:    {self.promotion_success_rate:.1%}

Conflicts Detected:   {self.conflicts_detected}
Conflicts Resolved:   {self.conflicts_resolved}
Conflict Rate:        {self.conflict_rate:.1%}

Latency (proposal submission):
  P50:                {self.p50_latency:.1f}ms
  P95:                {self.p95_latency:.1f}ms
  P99:                {self.p99_latency:.1f}ms
"""


class ScalableAgent:
    """A simulated agent with timing metrics."""
    
    def __init__(
        self,
        agent_id: str,
        repo_id: str,
        proposal_service: ProposalService,
        files: list[str],
        metrics: SwarmMetrics,
    ):
        self.agent_id = agent_id
        self.repo_id = repo_id
        self.proposal_service = proposal_service
        self.files = files
        self.metrics = metrics
    
    async def work(
        self,
        iterations: int = 5,
        delay_range: tuple = (0.01, 0.05),
        burst_mode: bool = False,
    ):
        """Simulate agent work with optional burst mode."""
        for i in range(iterations):
            file_path = random.choice(self.files)
            line_num = random.randint(1, 100)
            
            # Generate change
            change_type = random.choice(["comment", "function", "docstring"])
            if change_type == "comment":
                new_content = f"# Agent {self.agent_id} edit {i+1}"
            elif change_type == "function":
                new_content = f"def added_by_{self.agent_id}_{i}():\n    pass"
            else:
                new_content = f'"""Added by {self.agent_id}"""'
            
            diff = f"""diff --git a/{file_path} b/{file_path}
--- a/{file_path}
+++ b/{file_path}
@@ -{line_num},1 +{line_num},3 @@
 existing_line
+{new_content}
"""
            
            proposal = ProposalCreate(
                agent_id=self.agent_id,
                task_id=f"task_{self.agent_id}_{i}",
                change_type="unified_diff",
                change_content=diff,
                affected_paths=[file_path],
                confidence_score=random.uniform(0.7, 0.98),
                rationale=f"Edit at line {line_num}",
            )
            
            # Time the submission
            start = time.perf_counter()
            try:
                await self.proposal_service.submit_proposal(self.repo_id, proposal)
                latency_ms = (time.perf_counter() - start) * 1000
                self.metrics.proposal_latencies.append(latency_ms)
                self.metrics.total_proposals += 1
            except Exception as e:
                if "conflict" in str(e).lower():
                    self.metrics.conflicts_detected += 1
            
            # Delay between proposals
            if not burst_mode:
                await asyncio.sleep(random.uniform(*delay_range))


class ScalableSwarmSimulator:
    """Enhanced simulator for 50-100+ agents."""
    
    def __init__(
        self,
        num_agents: int,
        num_files: int,
        db_session: Session,
        storage_path: Path,
    ):
        self.num_agents = num_agents
        self.num_files = num_files
        self.session = db_session
        self.storage_path = storage_path
        
        # Services
        self.event_bus = EventBus()
        self.repo_service = RepositoryService(db_session, storage_path)
        self.proposal_service = ProposalService(db_session, self.event_bus, {})
        self.snapshot_service = SnapshotService(db_session, self.event_bus, storage_path)
        self.gate_runner = GateRunner(db_session, self.event_bus)
        self.promotion_engine = PromotionEngine(
            db_session, self.event_bus, self.proposal_service,
            self.snapshot_service, self.gate_runner, storage_path,
        )
        
        self.repo: Optional[Repository] = None
        self.document_store: Optional[DocumentStore] = None
        self.agents: list[ScalableAgent] = []
        self.metrics = SwarmMetrics()
    
    def setup(self, contention_level: str = "normal"):
        """
        Setup repository.
        
        Args:
            contention_level: "low", "normal", or "high"
                - low: Many files, few overlaps
                - normal: Balanced
                - high: Few files, many overlaps
        """
        # Fast gates for simulation
        config = RepoConfig(
            gates=[
                GateConfig(name="syntax", command="true", required=True, timeout_seconds=1, order=0),
                GateConfig(name="tests", command="true", required=True, timeout_seconds=1, order=1),
            ],
            auto_promote=True,
        )
        
        self.repo = self.repo_service.create_repository(
            name=f"swarm-test-{self.num_agents}",
            config=config,
        )
        
        # Adjust file count based on contention
        if contention_level == "high":
            actual_files = max(1, self.num_files // 5)
        elif contention_level == "low":
            actual_files = self.num_files * 2
        else:
            actual_files = self.num_files
        
        self.document_store = DocumentStore(self.repo.id)
        
        # Create files
        files = []
        for i in range(actual_files):
            file_path = f"src/module_{i:03d}.py"
            content = self._generate_content(file_path, 100)
            self.document_store.apply_full_file(file_path, content, "init")
            files.append(file_path)
        
        self.proposal_service.set_document_store(self.repo.id, self.document_store)
        
        # Create agents
        for i in range(self.num_agents):
            agent = ScalableAgent(
                agent_id=f"agent_{i:04d}",
                repo_id=self.repo.id,
                proposal_service=self.proposal_service,
                files=files,
                metrics=self.metrics,
            )
            self.agents.append(agent)
        
        return files
    
    def _generate_content(self, path: str, lines: int) -> str:
        content = [f'"""Module: {path}"""', ""]
        for i in range(lines):
            if i % 15 == 0:
                content.append(f"def function_{i}():")
                content.append("    pass")
                content.append("")
            else:
                content.append(f"# Line {i}")
        return "\n".join(content)
    
    async def run(
        self,
        iterations_per_agent: int = 5,
        burst_mode: bool = False,
    ):
        """Run the simulation."""
        self.metrics.start_time = time.perf_counter()
        
        # Start agents
        agent_tasks = [
            asyncio.create_task(
                agent.work(iterations_per_agent, burst_mode=burst_mode)
            )
            for agent in self.agents
        ]
        
        # Run promotion loop
        duration = iterations_per_agent * 0.3 * (1 if burst_mode else 1)
        promotion_task = asyncio.create_task(self._promotion_loop(duration + 2))
        
        # Wait for agents
        await asyncio.gather(*agent_tasks, return_exceptions=True)
        
        # Finish promotions
        await asyncio.sleep(0.5)
        promotion_task.cancel()
        try:
            await promotion_task
        except asyncio.CancelledError:
            pass
        
        self.metrics.end_time = time.perf_counter()
        
        # Collect final counts
        pending = self.proposal_service.list_proposals(
            self.repo.id, status=ProposalStatus.PENDING
        )
        promoted = self.proposal_service.list_proposals(
            self.repo.id, status=ProposalStatus.PROMOTED
        )
        rejected = self.proposal_service.list_proposals(
            self.repo.id, status=ProposalStatus.REJECTED
        )
        conflicts = self.proposal_service.list_conflict_sets(self.repo.id, resolved=False)
        
        self.metrics.proposals_pending = len(pending)
        self.metrics.proposals_promoted = len(promoted)
        self.metrics.proposals_rejected = len(rejected)
        self.metrics.conflicts_detected = len(conflicts)
    
    async def _promotion_loop(self, duration: float):
        """Run promotions continuously."""
        end = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < end:
            try:
                start = time.perf_counter()
                promotions = await self.promotion_engine.process_pending_proposals(
                    self.repo.id, self.document_store, batch_size=10
                )
                if promotions:
                    latency_ms = (time.perf_counter() - start) * 1000
                    self.metrics.promotion_latencies.append(latency_ms)
            except Exception:
                pass
            await asyncio.sleep(0.05)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def storage_path(tmp_path):
    return tmp_path / "swarm_storage"


# =============================================================================
# Tests
# =============================================================================

class TestSwarmScale:
    """Test swarm at 50-100 agent scale."""
    
    @pytest.mark.asyncio
    async def test_50_agents_normal_contention(self, db_session, storage_path):
        """50 agents with normal file distribution."""
        sim = ScalableSwarmSimulator(
            num_agents=50,
            num_files=10,
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup(contention_level="normal")
        
        await sim.run(iterations_per_agent=3)
        
        print(sim.metrics.summary())
        
        # Assertions
        assert sim.metrics.total_proposals >= 50, "Should submit proposals"
        assert sim.metrics.throughput_proposals_per_sec > 5, "Should achieve > 5 proposals/sec"
    
    @pytest.mark.asyncio
    async def test_100_agents_low_contention(self, db_session, storage_path):
        """100 agents with low contention (many files)."""
        sim = ScalableSwarmSimulator(
            num_agents=100,
            num_files=50,
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup(contention_level="low")
        
        await sim.run(iterations_per_agent=2)
        
        print(sim.metrics.summary())
        
        assert sim.metrics.total_proposals >= 100
        assert sim.metrics.conflict_rate < 0.5, "Low contention should have few conflicts"
    
    @pytest.mark.asyncio
    async def test_50_agents_high_contention(self, db_session, storage_path):
        """50 agents all editing same few files."""
        sim = ScalableSwarmSimulator(
            num_agents=50,
            num_files=3,  # Only 3 files for high contention
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup(contention_level="high")
        
        await sim.run(iterations_per_agent=3)
        
        print(sim.metrics.summary())
        
        # Should still work despite high contention
        assert sim.metrics.total_proposals >= 50
    
    @pytest.mark.asyncio
    async def test_burst_traffic_50_agents(self, db_session, storage_path):
        """50 agents submitting as fast as possible (no delay)."""
        sim = ScalableSwarmSimulator(
            num_agents=50,
            num_files=10,
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup()
        
        await sim.run(iterations_per_agent=5, burst_mode=True)
        
        print(sim.metrics.summary())
        
        # Should handle burst traffic
        assert sim.metrics.throughput_proposals_per_sec > 20, "Burst should be fast"


class TestSwarmMetrics:
    """Test metrics collection accuracy."""
    
    @pytest.mark.asyncio
    async def test_latency_collection(self, db_session, storage_path):
        """Verify latency metrics are collected."""
        sim = ScalableSwarmSimulator(
            num_agents=10,
            num_files=5,
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup()
        
        await sim.run(iterations_per_agent=5)
        
        # Should have collected latencies
        assert len(sim.metrics.proposal_latencies) > 0
        assert sim.metrics.p50_latency > 0
        assert sim.metrics.p50_latency < 1000, "P50 should be < 1 second"


class TestCRDTScalability:
    """Test CRDT performance at scale."""
    
    def test_100_concurrent_operations(self):
        """100 concurrent operations should converge."""
        from packages.aether.crdt.rga import RGADocument
        
        # Create 100 replicas
        replicas = [RGADocument(replica_id=f"r{i:03d}") for i in range(100)]
        
        # Initialize with base text
        base_ops = replicas[0].set_text("base content\n" * 10)
        for r in replicas[1:]:
            for op in base_ops:
                r.apply_operation(op)
        
        # Each replica makes a unique insert
        all_ops = []
        for i, r in enumerate(replicas):
            pos = random.randint(0, 50)
            op = r.local_insert(pos, f"[{i}]")
            all_ops.append(op)
        
        # Exchange all operations
        for r in replicas:
            for op in all_ops:
                r.apply_operation(op)
        
        # All should converge
        texts = [r.get_text() for r in replicas]
        assert len(set(texts)) == 1, "All replicas must converge"
        
        # All edits present
        final = texts[0]
        for i in range(100):
            assert f"[{i}]" in final, f"Edit {i} missing"


@pytest.mark.skipif(
    os.getenv("AETHER_ENABLE_SOAK_TESTS") != "1",
    reason="Set AETHER_ENABLE_SOAK_TESTS=1 to run 500-agent soak profile",
)
class TestSwarmSoakProfile:
    """Optional high-scale soak profile for CI/perf environments."""

    @pytest.mark.asyncio
    async def test_500_agents_low_contention_soak(self, db_session, storage_path):
        sim = ScalableSwarmSimulator(
            num_agents=500,
            num_files=250,
            db_session=db_session,
            storage_path=storage_path,
        )
        sim.setup(contention_level="low")

        # Single iteration keeps runtime bounded while still exercising 500-agent fan-in.
        await sim.run(iterations_per_agent=1, burst_mode=True)

        print(sim.metrics.summary())

        assert sim.metrics.total_proposals >= 500
        assert sim.metrics.conflict_rate < 0.35
        assert sim.metrics.p95_latency < 500
