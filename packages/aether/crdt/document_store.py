"""
Document store for managing CRDT documents per file.

Handles:
- Creating/loading documents for each file path
- Applying unified diffs to documents
- Materializing snapshots
- Conflict detection at the text range level
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
from typing import Optional
import json
import time

from .rga import RGADocument, RGAOperation
from .structured_doc import StructuredDocument
from ..models.aip import AIPMutation, parse_aip_mutation_envelope


@dataclass
class FileDocument:
    """A file represented as a CRDT document."""
    path: str
    document: RGADocument
    # Optional structured representation for semantic merging
    structured: Optional[StructuredDocument] = None
    # Track which proposals have modified this file
    proposal_history: list[str] = field(default_factory=list)
    # Track which semantic nodes were modified by which proposal (proposal_id -> set[node_id])
    node_modifications: dict[str, set[str]] = field(default_factory=dict)
    # Last known hash for change detection
    last_hash: str = ""

    def get_content(self) -> str:
        if self.structured:
            # Keep byte-exact file content in the raw document representation.
            text = self.document.get_text()
            if text:
                return text
            return self.structured.get_full_text()
        return self.document.get_text()

    def get_hash(self) -> str:
        return self.document.get_state_hash()

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "document": self.document.to_dict(),
            "proposal_history": self.proposal_history,
            "last_hash": self.last_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileDocument":
        return cls(
            path=d["path"],
            document=RGADocument.from_dict(d["document"]),
            proposal_history=d.get("proposal_history", []),
            last_hash=d.get("last_hash", ""),
        )


@dataclass
class DiffHunk:
    """A single hunk from a unified diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # Lines with +/- prefix


def parse_unified_diff(diff_text: str) -> dict[str, list[DiffHunk]]:
    """
    Parse a unified diff into per-file hunks.
    Returns {file_path: [DiffHunk, ...]}
    """
    files: dict[str, list[DiffHunk]] = {}
    current_file: Optional[str] = None
    current_hunks: list[DiffHunk] = []

    if diff_text.endswith("\n"):
        diff_text = diff_text[:-1]
    if diff_text.endswith("\r"):
        diff_text = diff_text[:-1]
    lines = diff_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # New file header
        if line.startswith("diff --git"):
            if current_file and current_hunks:
                files[current_file] = current_hunks
            current_hunks = []
            # Extract filename from "diff --git a/path b/path"
            match = re.match(r"diff --git a/(.+) b/(.+)", line)
            if match:
                current_file = match.group(2)
            i += 1
            continue

        # --- and +++ headers
        if line.startswith("---") or line.startswith("+++"):
            # Could extract filename from here too if needed
            i += 1
            continue

        # Hunk header
        if line.startswith("@@"):
            # Parse @@ -old_start,old_count +new_start,new_count @@
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise ValueError(f"Malformed hunk header: {line}")
            try:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or 1)
                new_start = int(match.group(3))
                new_count = int(match.group(4) or 1)
            except ValueError as ve:
                raise ValueError(f"Invalid integer in hunk header {line}: {ve}")

            # Collect hunk lines
            hunk_lines = []
            i += 1
            while i < len(lines):
                hunk_line = lines[i]
                if hunk_line.startswith(("diff --git", "@@", "---", "+++")):
                    break
                if hunk_line.startswith(("+", "-", " ")) or hunk_line == "":
                    hunk_lines.append(hunk_line)
                i += 1

            current_hunks.append(DiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=hunk_lines,
            ))
            continue

        i += 1

    if current_file and current_hunks:
        files[current_file] = current_hunks

    return files


def apply_hunk_to_lines(lines: list[str], hunk: DiffHunk) -> list[str]:
    """Apply a single hunk to a list of lines."""
    # Convert to 0-indexed
    start_idx = hunk.old_start - 1
    if start_idx < 0:
        start_idx = 0

    # Build the new section
    new_section = []
    for hunk_line in hunk.lines:
        if hunk_line.startswith("+"):
            new_section.append(hunk_line[1:])
        elif hunk_line.startswith(" "):
            new_section.append(hunk_line[1:])
        elif hunk_line == "":
            # Empty context line
            new_section.append("")
        # Lines starting with "-" are removed (not added to new_section)

    # Calculate where to splice
    end_idx = start_idx + hunk.old_count

    # Ensure we don't go past the end
    if end_idx > len(lines):
        end_idx = len(lines)

    # Apply the splice
    result = lines[:start_idx] + new_section + lines[end_idx:]
    return result


class DocumentStore:
    """
    Manages CRDT documents for a repository.

    Responsibilities:
    - Load/store documents from/to disk or memory
    - Apply diffs to documents
    - Detect conflicts between proposals
    - Materialize snapshots
    """

    def __init__(self, repo_id: str, base_path: Optional[str] = None):
        self.repo_id = repo_id
        self.base_path = Path(base_path) if base_path else None
        self.documents: dict[str, FileDocument] = {}
        self._shared_paths: set[str] = set()
        self._replica_counter = 0
        self._last_clone_metrics: dict[str, float] = {}
        self._last_hash_metrics: dict[str, float] = {}
        self._last_materialize_metrics: dict[str, float] = {}

    def _get_replica_id(self) -> str:
        """Generate a unique replica ID for a new document."""
        self._replica_counter += 1
        return f"{self.repo_id}_{self._replica_counter}"

    def _replace_document_text(self, file_doc: FileDocument, text: str) -> None:
        """Replace a file's raw document text while preserving replica identity."""
        new_doc = RGADocument(replica_id=file_doc.document.replica_id)
        new_doc.sequence = file_doc.document.sequence
        new_doc.set_text(text)
        file_doc.document = new_doc

    def _clone_structured_document(self, structured: StructuredDocument) -> StructuredDocument:
        """Clone structured document state without reparsing source text."""
        cloned = StructuredDocument(replica_id=structured.replica_id)
        cloned.node_order = list(structured.node_order)
        cloned.nodes = {
            node_id: RGADocument.from_dict(node_doc.to_dict())
            for node_id, node_doc in structured.nodes.items()
        }
        return cloned

    def _clone_file_document(self, file_doc: FileDocument) -> FileDocument:
        """Create a deep clone of one file document."""
        new_doc = RGADocument.from_dict(file_doc.document.to_dict())
        new_structured = None
        if file_doc.structured:
            new_structured = self._clone_structured_document(file_doc.structured)

        return FileDocument(
            path=file_doc.path,
            document=new_doc,
            structured=new_structured,
            proposal_history=list(file_doc.proposal_history),
            node_modifications={k: set(v) for k, v in file_doc.node_modifications.items()},
            last_hash=file_doc.last_hash,
        )

    def _ensure_writable_document(self, path: str) -> FileDocument:
        """Detach a shared file document before mutation (copy-on-write)."""
        if path in self._shared_paths and path in self.documents:
            self.documents[path] = self._clone_file_document(self.documents[path])
            self._shared_paths.discard(path)
        return self.documents[path]

    def get_document(self, path: str) -> FileDocument:
        """Get or create a document for a file path."""
        if path not in self.documents:
            replica_id = self._get_replica_id()
            doc = RGADocument(replica_id=replica_id)
            
            structured = None
            if path.endswith(".py"):
                structured = StructuredDocument(replica_id=replica_id)
                
            self.documents[path] = FileDocument(path=path, document=doc, structured=structured)
            self._shared_paths.discard(path)
            return self.documents[path]
        return self._ensure_writable_document(path)

    def has_document(self, path: str) -> bool:
        """Check if a document exists."""
        return path in self.documents

    def load_from_disk(self, relative_path: str) -> FileDocument:
        """Load a file from disk into a CRDT document."""
        if not self.base_path:
            raise ValueError("No base path configured")

        full_path = self.base_path / relative_path
        if not full_path.exists():
            # Return empty document
            return self.get_document(relative_path)

        content = full_path.read_text(encoding="utf-8", errors="replace")
        file_doc = self.get_document(relative_path)
        if file_doc.structured:
            file_doc.structured.set_content(content)
        self._replace_document_text(file_doc, content)
        file_doc.last_hash = file_doc.get_hash()
        return file_doc

    def load_directory(self, extensions: Optional[list[str]] = None) -> int:
        """Load all files from base_path into documents."""
        if not self.base_path or not self.base_path.exists():
            return 0

        EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
        EXCLUDED_FILES = {".DS_Store", "package-lock.json", "yarn.lock"}

        count = 0
        for root, dirs, files in os.walk(self.base_path):
            # Skip hidden and excluded directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDED_DIRS]

            for fname in files:
                if fname.startswith(".") or fname in EXCLUDED_FILES:
                    continue

                if extensions:
                    if not any(fname.endswith(ext) for ext in extensions):
                        continue

                full_path = Path(root) / fname
                rel_path = str(full_path.relative_to(self.base_path))

                self.load_from_disk(rel_path)
                count += 1

        return count

    def apply_diff(
        self, diff_text: str, proposal_id: str
    ) -> tuple[list[str], list[tuple[str, int, int]]]:
        """
        Apply a unified diff to the document store.

        Returns:
            (affected_paths, conflict_regions)
            - affected_paths: list of file paths that were modified
            - conflict_regions: list of (path, start_line, end_line) for overlap detection
        """
        parsed = parse_unified_diff(diff_text)
        affected_paths = []
        conflict_regions = []

        for file_path, hunks in parsed.items():
            file_doc = self.get_document(file_path)
            affected_paths.append(file_path)

            # Get current content as lines
            current_text = file_doc.get_content()
            lines = current_text.split("\n") if current_text else []

            # Track regions being modified for conflict detection
            for hunk in hunks:
                conflict_regions.append((file_path, hunk.old_start, hunk.old_start + hunk.old_count))

            # Apply hunks in reverse order to preserve line numbers
            for hunk in reversed(hunks):
                lines = apply_hunk_to_lines(lines, hunk)
                # If file was empty and we're inserting from /dev/null, treat as full content for tracking
                if (
                    not lines
                    and all(l.startswith("+") for l in hunk.lines if l)
                    and hunk.old_start == 0
                    and hunk.old_count == 0
                ):
                    lines = [l[1:] for l in hunk.lines if l.startswith("+")]

            # Convert back to CRDT operations
            new_text = "\n".join(lines)

            if file_doc.structured:
                # Track specifically which semantic nodes are targeted by these hunks
                modified_nodes = set()
                for hunk in hunks:
                    node_id = file_doc.structured.get_node_at_line(hunk.old_start)
                    if node_id:
                        modified_nodes.add(node_id)
                    elif hunk.old_start == 0 and hunk.old_count == 0:
                        # New file: track all nodes after set_content
                        modified_nodes = set()
                        break

                file_doc.structured.set_content(new_text)
                self._replace_document_text(file_doc, new_text)

                if not modified_nodes:
                    modified_nodes = set(file_doc.structured.nodes.keys())

                file_doc.node_modifications[proposal_id] = modified_nodes
            else:
                # Clear and re-set (simple approach for Phase 0)
                self._replace_document_text(file_doc, new_text)
                
            file_doc.proposal_history.append(proposal_id)
            file_doc.last_hash = file_doc.get_hash()

        return affected_paths, conflict_regions

    def apply_diff_batch(
        self, batched_diffs: list[tuple[str, str]]
    ) -> tuple[list[str], list[tuple[str, int, int]]]:
        """
        Apply a batch of unified diffs to the document store using 
        Aggregated Descending Line-Order Splicing.

        Args:
            batched_diffs: list of (diff_text, proposal_id)

        Returns:
            (affected_paths, conflict_regions)
            - affected_paths: list of file paths that were modified
            - conflict_regions: list of (path, start_line, end_line)
        """
        # Group all hunks by file path
        hunks_by_file = defaultdict(list)
        for diff_text, proposal_id in batched_diffs:
            parsed = parse_unified_diff(diff_text)
            for path, hunks in parsed.items():
                for hunk in hunks:
                    hunks_by_file[path].append((hunk, proposal_id))

        # Verify disjointness per file
        for path, hunk_list in hunks_by_file.items():
            # Sort by old_start to check overlap easily
            hunk_list.sort(key=lambda x: x[0].old_start)
            for idx in range(len(hunk_list) - 1):
                h1, prop1 = hunk_list[idx]
                h2, prop2 = hunk_list[idx + 1]
                h1_end = h1.old_start + max(1, h1.old_count) - 1
                h2_start = h2.old_start
                if h1_end >= h2_start:
                    raise ValueError(
                        f"Spatial overlap detected between proposal {prop1} and {prop2} on file {path}"
                    )

        affected_paths = []
        conflict_regions = []

        for path, hunk_list in hunks_by_file.items():
            file_doc = self.get_document(path)
            affected_paths.append(path)

            current_text = file_doc.get_content()
            lines = current_text.split("\n") if current_text else []

            for hunk, prop_id in hunk_list:
                conflict_regions.append((path, hunk.old_start, hunk.old_start + hunk.old_count))

            # Apply hunks in descending order of old_start
            sorted_hunks = sorted(hunk_list, key=lambda x: x[0].old_start, reverse=True)
            for hunk, prop_id in sorted_hunks:
                lines = apply_hunk_to_lines(lines, hunk)
                if (
                    not lines
                    and all(l.startswith("+") for l in hunk.lines if l)
                    and hunk.old_start == 0
                    and hunk.old_count == 0
                ):
                    lines = [l[1:] for l in hunk.lines if l.startswith("+")]

            new_text = "\n".join(lines)

            # Update document content and metadata
            if file_doc.structured:
                # Track specifically which semantic nodes are targeted by these hunks
                prop_modified_nodes = defaultdict(set)
                for hunk, prop_id in hunk_list:
                    node_id = file_doc.structured.get_node_at_line(hunk.old_start)
                    if node_id:
                        prop_modified_nodes[prop_id].add(node_id)

                file_doc.structured.set_content(new_text)
                self._replace_document_text(file_doc, new_text)

                all_nodes = set(file_doc.structured.nodes.keys())
                for prop_id in {p for _, p in hunk_list}:
                    modified = prop_modified_nodes[prop_id]
                    if not modified:
                        modified = all_nodes
                    file_doc.node_modifications[prop_id] = modified
            else:
                self._replace_document_text(file_doc, new_text)

            for _, prop_id in hunk_list:
                if prop_id not in file_doc.proposal_history:
                    file_doc.proposal_history.append(prop_id)

            file_doc.last_hash = file_doc.get_hash()

        return affected_paths, conflict_regions

    def apply_full_file(self, file_path: str, content: str, proposal_id: str) -> None:
        """Apply a full file replacement."""
        file_doc = self.get_document(file_path)
        
        if file_doc.structured:
            # Decompose new content
            # For Phase 0 integration: we just re-decompose the whole file
            # and track which nodes changed.
            file_doc.structured.set_content(content)
            new_nodes = set(file_doc.structured.nodes.keys())
            self._replace_document_text(file_doc, content)
            
            # Simple heuristic for modified nodes: any node in new content
            # In a more advanced version, we'd compare content per node
            file_doc.node_modifications[proposal_id] = new_nodes
        else:
            self._replace_document_text(file_doc, content)
            
        file_doc.proposal_history.append(proposal_id)
        file_doc.last_hash = file_doc.get_hash()

    @staticmethod
    def _resolve_unique_node_id(structured: StructuredDocument, preferred_node_id: str) -> str:
        """Ensure inserted node IDs are unique while keeping readable suffixes."""
        if preferred_node_id not in structured.nodes:
            return preferred_node_id
        suffix = 2
        while True:
            candidate = f"{preferred_node_id}#{suffix}"
            if candidate not in structured.nodes:
                return candidate
            suffix += 1

    @staticmethod
    def _apply_aip_mutation_to_structured(
        structured: StructuredDocument,
        mutation: AIPMutation,
    ) -> str:
        """Apply one AIP mutation to a structured document and return touched node ID."""
        if mutation.op == "update_node":
            structured.update_node(mutation.node_id, mutation.content or "")
            return mutation.node_id

        if mutation.op == "insert_node":
            node_id = DocumentStore._resolve_unique_node_id(structured, mutation.node_id)
            node_doc = RGADocument(structured.replica_id)
            node_doc.set_text(mutation.content or "")
            structured.nodes[node_id] = node_doc
            if mutation.after_node_id and mutation.after_node_id in structured.node_order:
                insert_at = structured.node_order.index(mutation.after_node_id) + 1
            else:
                insert_at = len(structured.node_order)
            structured.node_order.insert(insert_at, node_id)
            return node_id

        if mutation.op == "delete_node":
            if mutation.node_id in structured.nodes:
                del structured.nodes[mutation.node_id]
            if mutation.node_id in structured.node_order:
                structured.node_order.remove(mutation.node_id)
            return mutation.node_id

        if mutation.op == "move_node":
            if mutation.node_id not in structured.node_order:
                raise ValueError(f"move_node target not found: {mutation.node_id}")
            structured.node_order.remove(mutation.node_id)
            if mutation.after_node_id and mutation.after_node_id in structured.node_order:
                insert_at = structured.node_order.index(mutation.after_node_id) + 1
            else:
                insert_at = len(structured.node_order)
            structured.node_order.insert(insert_at, mutation.node_id)
            return mutation.node_id

        raise ValueError(f"Unsupported AIP mutation op: {mutation.op}")

    def apply_aip_mutations(
        self,
        payload_text: str,
        proposal_id: str,
    ) -> tuple[list[str], list[str]]:
        """
        Apply AIP v0 mutations to the document store.

        Returns:
            (affected_paths, touched_symbols)
        """
        envelope = parse_aip_mutation_envelope(payload_text)
        mutations_by_path: dict[str, list[AIPMutation]] = defaultdict(list)
        for mutation in envelope.mutations:
            mutations_by_path[mutation.path].append(mutation)

        affected_paths: list[str] = []
        touched_symbols: list[str] = []
        for path, mutations in mutations_by_path.items():
            file_doc = self.get_document(path)
            if not file_doc.structured:
                raise ValueError(
                    f"AIP v0 currently requires structured document support (.py) for path: {path}"
                )

            modified_nodes: set[str] = set()
            for mutation in mutations:
                touched_node = self._apply_aip_mutation_to_structured(file_doc.structured, mutation)
                modified_nodes.add(touched_node)
                touched_symbols.append(f"{path}::{touched_node}")

            new_text = file_doc.structured.get_full_text()
            self._replace_document_text(file_doc, new_text)
            file_doc.node_modifications[proposal_id] = modified_nodes
            file_doc.proposal_history.append(proposal_id)
            file_doc.last_hash = file_doc.get_hash()
            affected_paths.append(path)

        return affected_paths, touched_symbols

    def materialize_snapshot(self, output_path: Path) -> tuple[int, int]:
        """
        Write all documents to disk at the given path.

        Returns: (file_count, total_bytes)
        """
        started = time.perf_counter()
        output_path.mkdir(parents=True, exist_ok=True)

        file_count = 0
        total_bytes = 0

        for rel_path, file_doc in self.documents.items():
            content = file_doc.get_content()
            full_path = output_path / rel_path

            # Create parent directories
            full_path.parent.mkdir(parents=True, exist_ok=True)

            full_path.write_text(content, encoding="utf-8")
            file_count += 1
            total_bytes += len(content.encode("utf-8"))

        self._last_materialize_metrics = {
            "total_ms": (time.perf_counter() - started) * 1000.0,
            "file_count": float(file_count),
            "total_bytes": float(total_bytes),
        }
        return file_count, total_bytes

    def get_state_hash(self) -> str:
        """
        Get a hash of all document states.
        
        NOTE: This hashes the deterministic per-file `last_hash` state 
        (which is based on CRDT structural sequence) rather than reconstructing 
        and hashing raw UTF-8 file bytes. This is optimized for rapid deterministic 
        comparison of CRDT states during promotion batching.
        """
        started = time.perf_counter()
        hasher = hashlib.sha256()
        file_count = 0

        # Sort paths for determinism
        for path in sorted(self.documents.keys()):
            file_doc = self.documents[path]
            if not file_doc.last_hash:
                file_doc.last_hash = file_doc.get_hash()
            
            hasher.update(path.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(file_doc.last_hash.encode("utf-8"))
            hasher.update(b"\x00")
            file_count += 1

        self._last_hash_metrics = {
            "total_ms": (time.perf_counter() - started) * 1000.0,
            "file_count": float(file_count),
            "total_bytes": 0.0,
        }
        return hasher.hexdigest()

    def find_overlapping_proposals(
        self, path: str, start_line: int, end_line: int
    ) -> list[str]:
        """Find proposals that have modified an overlapping region."""
        if path not in self.documents:
            return []

        # For Phase 0, just return all proposals that modified this file
        # In Phase 2, we'd track line-level modifications
        return list(self.documents[path].proposal_history)

    def clone(self) -> "DocumentStore":
        """Create a copy-on-write clone of this document store."""
        started = time.perf_counter()
        new_store = DocumentStore(self.repo_id, str(self.base_path) if self.base_path else None)
        new_store._replica_counter = self._replica_counter
        new_store.documents = dict(self.documents)
        new_store._shared_paths = set(self.documents.keys())

        structured_count = sum(
            1
            for file_doc in self.documents.values()
            if file_doc.structured is not None
        )
        total_bytes = float(self._last_hash_metrics.get("total_bytes", 0.0))

        self._last_clone_metrics = {
            "total_ms": (time.perf_counter() - started) * 1000.0,
            "file_count": float(len(self.documents)),
            "structured_file_count": float(structured_count),
            "total_bytes": float(total_bytes),
        }
        return new_store

    def get_last_clone_metrics(self) -> dict[str, float]:
        """Get timing/size metrics from the most recent clone() call."""
        return dict(self._last_clone_metrics)

    def get_last_hash_metrics(self) -> dict[str, float]:
        """Get timing/size metrics from the most recent get_state_hash() call."""
        return dict(self._last_hash_metrics)

    def get_last_materialize_metrics(self) -> dict[str, float]:
        """Get timing/size metrics from the most recent materialize_snapshot() call."""
        return dict(self._last_materialize_metrics)

    def to_dict(self) -> dict:
        """Serialize the document store."""
        return {
            "repo_id": self.repo_id,
            "base_path": str(self.base_path) if self.base_path else None,
            "replica_counter": self._replica_counter,
            "documents": {k: v.to_dict() for k, v in self.documents.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentStore":
        """Deserialize a document store."""
        store = cls(d["repo_id"], d.get("base_path"))
        store._replica_counter = d.get("replica_counter", 0)
        store.documents = {k: FileDocument.from_dict(v) for k, v in d.get("documents", {}).items()}
        store._shared_paths = set()
        return store

    def get_file_list(self) -> list[str]:
        """Get list of all file paths."""
        return list(self.documents.keys())

    def get_file_content(self, path: str) -> Optional[str]:
        """Get content of a specific file."""
        if path in self.documents:
            return self.documents[path].get_content()
        return None
