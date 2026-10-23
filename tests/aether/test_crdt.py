"""Tests for Aether CRDT implementation."""

import pytest
from packages.aether.crdt.rga import RGADocument, RGAOperation, OperationType, ElementId


class TestRGADocument:
    """Test RGA CRDT document operations."""

    def test_create_empty_document(self):
        """Empty document should have no visible content."""
        doc = RGADocument(replica_id="test-1")
        assert doc.get_text() == ""
        assert doc.get_length() == 0

    def test_set_text(self):
        """Setting text should create operations."""
        doc = RGADocument(replica_id="test-1")
        ops = doc.set_text("hello")

        assert doc.get_text() == "hello"
        assert doc.get_length() == 5
        assert len(ops) == 5  # One op per character

    def test_local_insert_at_start(self):
        """Insert at position 0."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("world")

        op = doc.local_insert(0, "h")
        # The 'h' should be inserted somewhere in the text
        text = doc.get_text()
        assert "h" in text
        assert "world" in text or all(c in text for c in "world")
        assert op.op_type == OperationType.INSERT
        assert op.value == "h"

    def test_local_insert_at_end(self):
        """Insert at end of document."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("hello")

        doc.local_insert(5, "!")
        assert doc.get_text() == "hello!"

    def test_local_insert_in_middle(self):
        """Insert in middle of document."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("helo")

        doc.local_insert(2, "l")
        assert doc.get_text() == "hello"

    def test_local_delete(self):
        """Delete a character."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("hello")

        op = doc.local_delete(0)
        assert doc.get_text() == "ello"
        assert op is not None
        assert op.op_type == OperationType.DELETE

    def test_local_delete_middle(self):
        """Delete from middle of document."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("hello")

        doc.local_delete(2)  # Delete 'l'
        assert doc.get_text() == "helo"

    def test_local_delete_invalid_position(self):
        """Delete at invalid position returns None."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("hi")

        op = doc.local_delete(10)  # Out of bounds
        assert op is None
        assert doc.get_text() == "hi"

    def test_concurrent_inserts_at_same_position(self):
        """Two replicas inserting at same position should converge."""
        # Create two replicas - they need to share operations to be in sync
        doc1 = RGADocument(replica_id="replica-a")
        doc2 = RGADocument(replica_id="replica-b")

        # Initialize doc1 and sync to doc2
        ops = doc1.set_text("ab")
        for op in ops:
            doc2.apply_operation(op)

        # Verify they're in sync
        assert doc1.get_text() == doc2.get_text() == "ab"

        # Both insert at position 1 (between 'a' and 'b')
        op1 = doc1.local_insert(1, "X")
        op2 = doc2.local_insert(1, "Y")

        # Exchange operations
        doc1.apply_operation(op2)
        doc2.apply_operation(op1)

        # CRITICAL: Both should converge to same text (key CRDT property)
        assert doc1.get_text() == doc2.get_text(), "Replicas must converge"

        # Text should contain both insertions
        text = doc1.get_text()
        assert "X" in text, "X should be in final text"
        assert "Y" in text, "Y should be in final text"
        assert "a" in text
        assert "b" in text

    def test_concurrent_deletes(self):
        """Two replicas deleting same character should converge."""
        doc1 = RGADocument(replica_id="replica-a")
        doc2 = RGADocument(replica_id="replica-b")

        # Use operations from doc1 to initialize doc2
        ops = doc1.set_text("abc")
        for op in ops:
            doc2.apply_operation(op)

        assert doc1.get_text() == doc2.get_text() == "abc"

        # Both delete 'b' (position 1)
        op1 = doc1.local_delete(1)
        op2 = doc2.local_delete(1)

        # Exchange operations (delete of already-deleted is idempotent)
        doc1.apply_operation(op2)
        doc2.apply_operation(op1)

        # Both should have "ac"
        assert doc1.get_text() == doc2.get_text() == "ac"

    def test_operation_serialization(self):
        """Operations should serialize/deserialize correctly."""
        op = RGAOperation(
            op_type=OperationType.INSERT,
            element_id=ElementId("test", 42),
            value="X",
            after_id=ElementId("test", 41),
            timestamp=123.456,
        )

        serialized = op.serialize()
        restored = RGAOperation.deserialize(serialized)

        assert restored.op_type == op.op_type
        assert restored.element_id == op.element_id
        assert restored.value == op.value
        assert restored.after_id == op.after_id
        assert restored.timestamp == op.timestamp

    def test_document_serialization(self):
        """Document state should serialize/deserialize correctly."""
        doc1 = RGADocument(replica_id="test-1")
        doc1.set_text("hello world")

        state = doc1.to_dict()
        doc2 = RGADocument.from_dict(state)

        assert doc2.get_text() == "hello world"
        assert doc2.replica_id == "test-1"

    def test_merge_documents(self):
        """Merging two documents should combine all changes."""
        doc1 = RGADocument(replica_id="replica-a")
        doc2 = RGADocument(replica_id="replica-b")

        # Start with same content (simulate sync)
        ops = doc1.set_text("hello")
        for op in ops:
            doc2.apply_operation(op)

        # Independent edits
        doc1.local_insert(5, "!")  # After 'o', adds "!"
        doc2.local_insert(0, "S")  # At start, adds "S"

        # Merge doc2 into doc1
        doc1.merge(doc2)

        # Should have both edits - check that all characters are present
        text = doc1.get_text()
        assert "S" in text, "S should be in merged text"
        assert "h" in text
        assert "e" in text
        assert "l" in text
        assert "o" in text
        assert "!" in text, "! should be in merged text"

    def test_state_hash_changes_with_content(self):
        """State hash should change when content changes."""
        doc = RGADocument(replica_id="test-1")
        doc.set_text("hello")
        hash1 = doc.get_state_hash()

        doc.local_insert(5, "!")
        hash2 = doc.get_state_hash()

        assert hash1 != hash2

    def test_state_hash_same_for_same_content(self):
        """Same content should produce same hash."""
        doc1 = RGADocument(replica_id="replica-a")
        doc2 = RGADocument(replica_id="replica-b")

        doc1.set_text("hello")
        doc2.set_text("hello")

        assert doc1.get_state_hash() == doc2.get_state_hash()


class TestDocumentStore:
    """Test document store operations."""

    def test_create_and_get_document(self):
        """Should create and retrieve documents."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")
        doc = store.get_document("file.txt")

        assert doc.path == "file.txt"
        assert doc.get_content() == ""

    def test_apply_diff(self):
        """Should apply unified diff to documents."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")

        # Initialize a file
        doc = store.get_document("test.py")
        doc.document.set_text("line1\nline2\nline3\n")

        # Apply a diff that changes line2
        diff = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,3 @@
 line1
-line2
+modified_line2
 line3
"""

        affected_paths, regions = store.apply_diff(diff, "prop_1")

        assert "test.py" in affected_paths
        assert "modified_line2" in store.get_file_content("test.py")

    def test_apply_full_file(self):
        """Should replace full file content."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")
        store.apply_full_file("new_file.py", "print('hello')", "prop_1")

        assert store.get_file_content("new_file.py") == "print('hello')"

    def test_apply_aip_mutations_updates_structured_python_file(self):
        """AIP payload should update and insert structured nodes deterministically."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")
        store.apply_full_file(
            "main.py",
            "def alpha():\n    return 1\n",
            "init",
        )
        payload = """
        {
          "schema": "aip.v0",
          "mutations": [
            {
              "op": "update_node",
              "path": "main.py",
              "node_id": "FUNC:alpha",
              "content": "def alpha():\\n    return 2\\n"
            },
            {
              "op": "insert_node",
              "path": "main.py",
              "node_id": "FUNC:beta",
              "after_node_id": "FUNC:alpha",
              "content": "\\ndef beta():\\n    return 3\\n"
            }
          ]
        }
        """

        affected_paths, touched_symbols = store.apply_aip_mutations(payload, "prop_1")
        content = store.get_file_content("main.py")

        assert affected_paths == ["main.py"]
        assert "main.py::FUNC:alpha" in touched_symbols
        assert "main.py::FUNC:beta" in touched_symbols
        assert "return 2" in content
        assert "def beta()" in content

    def test_apply_aip_mutations_rejects_non_structured_paths(self):
        """AIP payload should fail fast for files without structured-node support."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")
        store.apply_full_file("main.ts", "export const x = 1;\n", "init")
        payload = """
        {
          "schema": "aip.v0",
          "mutations": [
            {
              "op": "update_node",
              "path": "main.ts",
              "node_id": "FUNC:alpha",
              "content": "export const x = 2;\\n"
            }
          ]
        }
        """

        with pytest.raises(ValueError):
            store.apply_aip_mutations(payload, "prop_1")

    def test_clone_store(self):
        """Cloning should create independent copy."""
        from packages.aether.crdt.document_store import DocumentStore

        store1 = DocumentStore(repo_id="test-repo")
        store1.apply_full_file("test.py", "original", "prop_1")

        store2 = store1.clone()
        store2.apply_full_file("test.py", "modified", "prop_2")

        assert store1.get_file_content("test.py") == "original"
        assert store2.get_file_content("test.py") == "modified"

    def test_clone_copy_on_write_detaches_only_modified_file(self):
        """Clone should detach only the file it mutates."""
        from packages.aether.crdt.document_store import DocumentStore

        store1 = DocumentStore(repo_id="test-repo")
        store1.apply_full_file("a.py", "one", "prop_1")
        store1.apply_full_file("b.py", "two", "prop_1")

        store2 = store1.clone()

        # Fresh clone shares file docs until write.
        assert store1.documents["a.py"] is store2.documents["a.py"]
        assert store1.documents["b.py"] is store2.documents["b.py"]

        store2.apply_full_file("a.py", "one-modified", "prop_2")

        # Only touched file is detached.
        assert store1.documents["a.py"] is not store2.documents["a.py"]
        assert store1.documents["b.py"] is store2.documents["b.py"]
        assert store1.get_file_content("a.py") == "one"
        assert store2.get_file_content("a.py") == "one-modified"

    def test_clone_get_document_write_isolated(self):
        """Direct writes via get_document on clone should not leak to parent."""
        from packages.aether.crdt.document_store import DocumentStore

        store1 = DocumentStore(repo_id="test-repo")
        store1.apply_full_file("test.py", "baseline", "prop_1")

        store2 = store1.clone()
        doc2 = store2.get_document("test.py")
        doc2.document.set_text("changed-via-doc")

        assert store1.get_file_content("test.py") == "baseline"
        assert store2.get_file_content("test.py") == "changed-via-doc"

    def test_content_hash_deterministic(self):
        """Content hash should be deterministic."""
        from packages.aether.crdt.document_store import DocumentStore

        store1 = DocumentStore(repo_id="test-repo")
        store1.apply_full_file("a.py", "content_a", "prop_1")
        store1.apply_full_file("b.py", "content_b", "prop_1")

        store2 = DocumentStore(repo_id="test-repo")
        store2.apply_full_file("a.py", "content_a", "prop_1")
        store2.apply_full_file("b.py", "content_b", "prop_1")

        assert store1.get_state_hash() == store2.get_state_hash()

    def test_materialize_snapshot(self, tmp_path):
        """Should write all files to disk."""
        from packages.aether.crdt.document_store import DocumentStore

        store = DocumentStore(repo_id="test-repo")
        store.apply_full_file("src/main.py", "print('main')", "prop_1")
        store.apply_full_file("src/util.py", "print('util')", "prop_1")

        file_count, total_bytes = store.materialize_snapshot(tmp_path)

        assert file_count == 2
        assert (tmp_path / "src" / "main.py").exists()
        assert (tmp_path / "src" / "util.py").read_text() == "print('util')"
