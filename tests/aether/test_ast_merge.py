"""
AST-Level Structured Merge Tests.

Verifies:
- Semantic decomposition of Python files
- Conflict detection at the node level
- Auto-merge of disjoint changes in the same file
"""

import pytest
from packages.aether.crdt.structured_doc import StructuredDocument
from packages.aether.crdt.document_store import DocumentStore
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.event_bus import EventBus
from packages.aether.models.entities import ProposalCreate, Repository
from sqlmodel import SQLModel, Session, create_engine

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

@pytest.fixture
def doc_store():
    return DocumentStore(repo_id="test-repo")

@pytest.fixture
def proposal_service(db_session, doc_store):
    bus = EventBus()
    svc = ProposalService(db_session, bus, {"test-repo": doc_store})
    return svc

def test_round_trip_preservation():
    """Ensure that decomposition and recomposition preserves everything."""
    source = """
import os
import sys

# Global constant
MAX_VALUE = 100

def func1(a, b):
    \"\"\"Docstring for func1.\"\"\"
    return a + b

class MyClass:
    def method1(self):
        pass
        
    def method2(self):
        return True
"""
    doc = StructuredDocument("replica-1")
    doc.set_content(source)
    
    assert doc.get_full_text() == source
    assert "FUNC:func1" in doc.list_nodes()
    assert any(node.startswith("CLASS:MyClass") for node in doc.list_nodes())


def test_round_trip_preserves_module_docstring_before_imports_and_interleaved_globals():
    """Module docstrings, future imports, and interleaved globals must keep exact source order."""
    import ast

    source = (
        '"""Runtime module docstring on line 1."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "import sys\n"
        "\n"
        "DEFAULT_LIMIT = 64\n"
        "\n"
        "\n"
        "class Runtime:\n"
        '    """Runtime class."""\n'
        "\n"
        "    def __init__(self) -> None:\n"
        "        self.limit = DEFAULT_LIMIT\n"
        "\n"
        "\n"
        '__all__ = ["Runtime"]'
    )
    doc = StructuredDocument("replica-docstring")
    doc.set_content(source)

    reconstructed = doc.get_full_text()
    assert reconstructed == source
    assert doc.get_node_at_line(1) == "__docstring__"
    assert doc.list_nodes()[:3] == ["__docstring__", "__imports__", "__globals__"]
    assert doc.list_nodes()[-1] == "__globals__#2"
    assert ast.get_docstring(ast.parse(reconstructed)) == "Runtime module docstring on line 1."

def test_disjoint_edits_no_conflict(proposal_service, doc_store, db_session):
    """Edits to different functions in the same file should not conflict."""
    from packages.aether.services.promotion_engine import PromotionEngine
    from packages.aether.services.snapshot_service import SnapshotService
    from packages.aether.services.gate_runner import GateRunner
    from pathlib import Path

    # Setup repo
    repo = Repository(name="test-repo", id="test-repo", head_id="head-1", head_sequence=1)
    db_session.add(repo)
    db_session.commit()
    
    source = "def func1():\n    pass\n\ndef func2():\n    pass\n"
    doc_store.get_document("main.py").structured.set_content(source)
    
    # Proposal 1: Edit func1
    diff1 = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1,2 +1,2 @@
 def func1():
-    pass
+    print("hello")
"""
    p1_data = ProposalCreate(
        agent_id="agent1",
        task_id="task1",
        change_type="unified_diff",
        change_content=diff1,
        affected_paths=["main.py"]
    )
    
    # Proposal 2: Edit func2
    diff2 = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -4,2 +4,2 @@
 def func2():
-    pass
+    print("world")
"""
    p2_data = ProposalCreate(
        agent_id="agent2",
        task_id="task2",
        change_type="unified_diff",
        change_content=diff2,
        affected_paths=["main.py"]
    )
    
    import asyncio
    # Submit both
    asyncio.run(proposal_service.submit_proposal("test-repo", p1_data))
    asyncio.run(proposal_service.submit_proposal("test-repo", p2_data))
    
    # Setup PromotionEngine
    bus = EventBus()
    storage = Path("/tmp/aether_test_storage")
    storage.mkdir(exist_ok=True)
    snapshot_svc = SnapshotService(db_session, bus, storage)
    gate_runner = GateRunner(db_session, bus)
    
    engine = PromotionEngine(
        db_session, bus, proposal_service, snapshot_svc, gate_runner, storage
    )
    
    # Run promotion loop
    # This should evaluate [p1, p2] as a batch because they don't conflict
    promotions = asyncio.run(engine.process_pending_proposals("test-repo", doc_store))
    
    assert len(promotions) > 0
    final_content = doc_store.get_file_content("main.py")
    assert 'print("hello")' in final_content
    assert 'print("world")' in final_content

def test_same_node_edits_conflict(proposal_service, doc_store, db_session):
    """Edits to the same function in the same file should conflict."""
    # Setup repo
    repo = Repository(name="test-repo", id="test-repo", head_id="head-1", head_sequence=1)
    db_session.add(repo)
    db_session.commit()
    
    source = "def func1():\n    pass\n"
    doc_store.get_document("main.py").structured.set_content(source)
    
    # Proposal 1: Edit func1
    diff1 = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1,2 +1,2 @@
 def func1():
-    pass
+    return 1
"""
    p1_data = ProposalCreate(
        agent_id="agent1",
        task_id="task1",
        change_type="unified_diff",
        change_content=diff1,
        affected_paths=["main.py"]
    )
    
    # Proposal 2: Edit same func1
    diff2 = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -1,2 +1,2 @@
 def func1():
-    pass
+    return 2
"""
    p2_data = ProposalCreate(
        agent_id="agent2",
        task_id="task2",
        change_type="unified_diff",
        change_content=diff2,
        affected_paths=["main.py"]
    )
    
    import asyncio
    asyncio.run(proposal_service.submit_proposal("test-repo", p1_data))
    res2 = asyncio.run(proposal_service.submit_proposal("test-repo", p2_data))
    
    # Verify conflict set created
    assert res2.conflict_set_id is not None


def test_disjoint_methods_same_class_no_conflict(proposal_service, doc_store, db_session):
    """Edits to different methods in the same class should not conflict."""
    from packages.aether.services.promotion_engine import PromotionEngine
    from packages.aether.services.snapshot_service import SnapshotService
    from packages.aether.services.gate_runner import GateRunner
    from pathlib import Path
    import asyncio

    repo = Repository(name="test-repo", id="test-repo", head_id="head-1", head_sequence=1)
    db_session.add(repo)
    db_session.commit()

    source = (
        "class Worker:\n"
        "    def alpha(self):\n"
        "        return 1\n"
        "\n"
        "    def beta(self):\n"
        "        return 2\n"
    )
    doc_store.get_document("main.py").structured.set_content(source)
    doc_store.apply_full_file("main.py", source, "initial")

    diff_alpha = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -2,2 +2,2 @@
     def alpha(self):
-        return 1
+        return 10
"""
    diff_beta = """diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -5,2 +5,2 @@
     def beta(self):
-        return 2
+        return 20
"""

    p1 = ProposalCreate(
        agent_id="agent-alpha",
        task_id="task-alpha",
        change_type="unified_diff",
        change_content=diff_alpha,
        affected_paths=["main.py"],
    )
    p2 = ProposalCreate(
        agent_id="agent-beta",
        task_id="task-beta",
        change_type="unified_diff",
        change_content=diff_beta,
        affected_paths=["main.py"],
    )

    res1 = asyncio.run(proposal_service.submit_proposal("test-repo", p1))
    res2 = asyncio.run(proposal_service.submit_proposal("test-repo", p2))

    assert res1.conflict_set_id is None
    assert res2.conflict_set_id is None

    bus = EventBus()
    storage = Path("/tmp/aether_test_storage_methods")
    storage.mkdir(exist_ok=True)
    snapshot_svc = SnapshotService(db_session, bus, storage)
    gate_runner = GateRunner(db_session, bus)
    engine = PromotionEngine(
        db_session, bus, proposal_service, snapshot_svc, gate_runner, storage
    )

    promotions = asyncio.run(engine.process_pending_proposals("test-repo", doc_store))
    assert len(promotions) > 0

    final_content = doc_store.get_file_content("main.py")
    assert "return 10" in final_content
    assert "return 20" in final_content
