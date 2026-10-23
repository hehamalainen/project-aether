"""
RGA (Replicated Growable Array) CRDT implementation for text.

RGA provides conflict-free concurrent text editing where:
- Each character has a unique ID (replica_id, sequence)
- Insertions reference a position (after which element)
- Deletions mark elements as tombstones
- All replicas converge to the same state

This implementation is optimized for clarity over performance in Phase 0.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import hashlib
import json
from collections import defaultdict


class OperationType(str, Enum):
    """Types of CRDT operations."""
    INSERT = "INSERT"
    DELETE = "DELETE"


@dataclass(frozen=True)
class ElementId:
    """Unique identifier for an RGA element."""
    replica_id: str
    sequence: int

    def __lt__(self, other: "ElementId") -> bool:
        """Total ordering: by sequence, then by replica_id."""
        if self.sequence != other.sequence:
            return self.sequence < other.sequence
        return self.replica_id < other.replica_id

    def __str__(self) -> str:
        return f"{self.replica_id}:{self.sequence}"

    def to_dict(self) -> dict:
        return {"replica_id": self.replica_id, "sequence": self.sequence}

    @classmethod
    def from_dict(cls, d: dict) -> "ElementId":
        return cls(replica_id=d["replica_id"], sequence=d["sequence"])


# Special ID representing the start of the document
ROOT_ID = ElementId("_root", 0)


@dataclass
class RGAElement:
    """An element in the RGA sequence."""
    id: ElementId
    value: str  # Single character or empty for tombstone
    deleted: bool = False
    # Reference to the element this was inserted after
    after_id: Optional[ElementId] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id.to_dict(),
            "value": self.value,
            "deleted": self.deleted,
            "after_id": self.after_id.to_dict() if self.after_id else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RGAElement":
        return cls(
            id=ElementId.from_dict(d["id"]),
            value=d["value"],
            deleted=d["deleted"],
            after_id=ElementId.from_dict(d["after_id"]) if d["after_id"] else None,
        )


@dataclass
class RGAOperation:
    """A CRDT operation that can be applied to an RGA document."""
    op_type: OperationType
    element_id: ElementId
    # For INSERT: the character to insert
    value: Optional[str] = None
    # For INSERT: insert after this element
    after_id: Optional[ElementId] = None
    # Timestamp for ordering
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "op_type": self.op_type.value,
            "element_id": self.element_id.to_dict(),
            "value": self.value,
            "after_id": self.after_id.to_dict() if self.after_id else None,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RGAOperation":
        return cls(
            op_type=OperationType(d["op_type"]),
            element_id=ElementId.from_dict(d["element_id"]),
            value=d.get("value"),
            after_id=ElementId.from_dict(d["after_id"]) if d.get("after_id") else None,
            timestamp=d.get("timestamp", 0.0),
        )

    def serialize(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def deserialize(cls, s: str) -> "RGAOperation":
        return cls.from_dict(json.loads(s))


@dataclass
class RGADocument:
    """
    An RGA CRDT document representing a text file.

    Operations:
    - local_insert(position, char): Insert at local position
    - local_delete(position): Delete at local position
    - apply_operation(op): Apply a remote operation
    - get_text(): Get current document text
    """

    replica_id: str
    # Elements indexed by their ID
    elements: dict[str, RGAElement] = field(default_factory=dict)
    # Ordered list of element IDs (including tombstones for position tracking)
    order: list[ElementId] = field(default_factory=list)
    # Local sequence counter
    sequence: int = 0
    # Operation log for sync
    operations: list[RGAOperation] = field(default_factory=list)
    # Inserts waiting for their parent element to arrive
    _pending_inserts: dict[str, list[RGAOperation]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self):
        """Initialize with root element if empty."""
        if not self.elements:
            root = RGAElement(id=ROOT_ID, value="", after_id=None)
            self.elements[str(ROOT_ID)] = root

    def _next_id(self) -> ElementId:
        """Generate next unique element ID."""
        self.sequence += 1
        return ElementId(self.replica_id, self.sequence)

    def _find_position(self, index: int) -> tuple[int, Optional[ElementId]]:
        """
        Find the element at the given visible position.
        Returns (order_index, element_id).
        """
        visible_count = 0
        for i, eid in enumerate(self.order):
            elem = self.elements.get(str(eid))
            if elem and not elem.deleted:
                if visible_count == index:
                    return i, eid
                visible_count += 1
        # Position is at the end
        return len(self.order), None

    def _rebuild_order(self) -> None:
        """Rebuild linear order deterministically from parent-child links."""
        children: dict[ElementId, list[ElementId]] = defaultdict(list)
        for eid_str, elem in self.elements.items():
            if eid_str == str(ROOT_ID):
                continue
            parent = elem.after_id or ROOT_ID
            children[parent].append(elem.id)

        for parent in children:
            children[parent].sort()

        ordered: list[ElementId] = []
        seen: set[str] = set()
        stack: list[ElementId] = [ROOT_ID]

        while stack:
            parent = stack.pop()
            siblings = children.get(parent, [])
            visit_children: list[ElementId] = []

            # Emit all siblings first, then process their subtrees in order.
            for sibling in siblings:
                sibling_key = str(sibling)
                if sibling_key in seen:
                    continue
                ordered.append(sibling)
                seen.add(sibling_key)
                visit_children.append(sibling)

            for sibling in reversed(visit_children):
                stack.append(sibling)

        # Recover deterministically if malformed links create unreachable nodes.
        if len(ordered) < len(self.elements) - 1:
            remaining = [
                elem.id
                for eid_str, elem in self.elements.items()
                if eid_str != str(ROOT_ID) and eid_str not in seen
            ]
            remaining.sort()
            ordered.extend(remaining)

        self.order = ordered

    def _queue_pending_insert(self, op: RGAOperation) -> None:
        """Queue an insert until its referenced parent exists."""
        if not op.after_id:
            return
        key = str(op.after_id)
        pending = self._pending_inserts[key]
        if any(existing.element_id == op.element_id for existing in pending):
            return
        pending.append(op)

    def _drain_pending_inserts(self, parent_id: ElementId) -> None:
        """Apply any queued inserts that were waiting on parent_id. (Deprecated: handled iteratively in apply_operation)"""
        pass

    def local_insert(self, position: int, char: str) -> RGAOperation:
        """
        Insert a character at the given visible position.
        Returns the operation for replication.
        """
        import time

        # Find what element we're inserting after
        if position == 0:
            after_id = ROOT_ID
        else:
            _, after_id = self._find_position(position - 1)
            if after_id is None:
                # Inserting after the last element
                if self.order:
                    after_id = self.order[-1]
                else:
                    after_id = ROOT_ID

        new_id = self._next_id()
        op = RGAOperation(
            op_type=OperationType.INSERT,
            element_id=new_id,
            value=char,
            after_id=after_id,
            timestamp=time.time(),
        )

        self.apply_operation(op)
        return op

    def local_delete(self, position: int) -> Optional[RGAOperation]:
        """
        Delete the character at the given visible position.
        Returns the operation for replication, or None if position invalid.
        """
        import time

        _, eid = self._find_position(position)
        if eid is None:
            return None

        op = RGAOperation(
            op_type=OperationType.DELETE,
            element_id=eid,
            timestamp=time.time(),
        )

        self.apply_operation(op)
        return op

    def apply_operation(self, op: RGAOperation) -> bool:
        """
        Apply an operation (local or remote).
        Returns True if applied, False if already applied.
        """
        eid_str = str(op.element_id)

        if op.op_type == OperationType.INSERT:
            if eid_str in self.elements:
                # Already have this element
                return False

            if (
                op.after_id
                and op.after_id != ROOT_ID
                and str(op.after_id) not in self.elements
            ):
                self._queue_pending_insert(op)
                return False

            elem = RGAElement(
                id=op.element_id,
                value=op.value or "",
                deleted=False,
                after_id=op.after_id,
            )
            self.elements[eid_str] = elem
            self._rebuild_order()

            # Update sequence if needed
            if op.element_id.replica_id == self.replica_id:
                self.sequence = max(self.sequence, op.element_id.sequence)
            self.operations.append(op)

            # Iteratively process any pending inserts that were waiting for this element
            queue = [op.element_id]
            while queue:
                curr_parent_id = queue.pop(0)
                key = str(curr_parent_id)
                pending = list(self._pending_inserts.pop(key, []))
                for pending_op in pending:
                    pending_eid_str = str(pending_op.element_id)
                    if pending_eid_str in self.elements:
                        continue

                    pending_elem = RGAElement(
                        id=pending_op.element_id,
                        value=pending_op.value or "",
                        deleted=False,
                        after_id=pending_op.after_id,
                    )
                    self.elements[pending_eid_str] = pending_elem
                    self._rebuild_order()

                    # Update sequence if needed
                    if pending_op.element_id.replica_id == self.replica_id:
                        self.sequence = max(self.sequence, pending_op.element_id.sequence)
                    self.operations.append(pending_op)

                    # Queue the newly applied element's ID to check for its pending children
                    queue.append(pending_op.element_id)

            return True

        elif op.op_type == OperationType.DELETE:
            if eid_str not in self.elements:
                # Element doesn't exist (yet) - store op for later?
                # For simplicity, we'll create a tombstone
                return False

            self.elements[eid_str].deleted = True
            self.operations.append(op)
            return True

        return False

    def apply_operations(self, ops: list[RGAOperation]) -> int:
        """Apply multiple operations. Returns count of newly applied ops."""
        applied = 0
        for op in ops:
            if self.apply_operation(op):
                applied += 1
        return applied

    def get_text(self) -> str:
        """Get the current document text (excluding tombstones)."""
        result = []
        for eid in self.order:
            elem = self.elements.get(str(eid))
            if elem and not elem.deleted and elem.value:
                result.append(elem.value)
        return "".join(result)

    def get_length(self) -> int:
        """Get visible character count."""
        count = 0
        for eid in self.order:
            elem = self.elements.get(str(eid))
            if elem and not elem.deleted:
                count += 1
        return count

    def replace_text(self, text: str) -> list[RGAOperation]:
        """
        Replace document content in O(n).

        This is used for initialization/full-file replacement paths where we already
        have canonical content and do not need to replay incremental inserts.
        """
        root = RGAElement(id=ROOT_ID, value="", after_id=None)
        elements: dict[str, RGAElement] = {str(ROOT_ID): root}
        order: list[ElementId] = []
        ops: list[RGAOperation] = []

        prev_id = ROOT_ID
        seq = self.sequence
        for char in text:
            seq += 1
            eid = ElementId(self.replica_id, seq)
            elements[str(eid)] = RGAElement(
                id=eid,
                value=char,
                deleted=False,
                after_id=prev_id,
            )
            order.append(eid)
            ops.append(
                RGAOperation(
                    op_type=OperationType.INSERT,
                    element_id=eid,
                    value=char,
                    after_id=prev_id,
                )
            )
            prev_id = eid

        self.elements = elements
        self.order = order
        self.sequence = seq
        self.operations = ops
        self._pending_inserts.clear()
        return ops

    def set_text(self, text: str) -> list[RGAOperation]:
        """
        Set document to given text (for initialization).
        Returns operations generated.
        """
        return self.replace_text(text)

    def get_state_hash(self) -> str:
        """Get a hash of the current document state."""
        text = self.get_text()
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def get_operations_since(self, since_index: int = 0) -> list[RGAOperation]:
        """Get operations since a given index."""
        return self.operations[since_index:]

    def to_dict(self) -> dict:
        """Serialize document state."""
        return {
            "replica_id": self.replica_id,
            "sequence": self.sequence,
            "elements": {k: v.to_dict() for k, v in self.elements.items()},
            "order": [{"replica_id": eid.replica_id, "sequence": eid.sequence} for eid in self.order],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RGADocument":
        """Deserialize document state."""
        doc = cls(replica_id=d["replica_id"])
        doc.sequence = d["sequence"]
        doc.elements = {k: RGAElement.from_dict(v) for k, v in d["elements"].items()}
        doc.order = [ElementId(eid["replica_id"], eid["sequence"]) for eid in d["order"]]
        return doc

    def merge(self, other: "RGADocument") -> int:
        """
        Merge another document's state into this one.
        Returns number of new operations applied.
        """
        applied = 0
        for eid_str, elem in other.elements.items():
            if eid_str not in self.elements:
                # Reconstruct the insert operation
                op = RGAOperation(
                    op_type=OperationType.INSERT,
                    element_id=elem.id,
                    value=elem.value,
                    after_id=elem.after_id,
                )
                if self.apply_operation(op):
                    applied += 1

            # Check for deletions
            if elem.deleted and eid_str in self.elements:
                if not self.elements[eid_str].deleted:
                    op = RGAOperation(
                        op_type=OperationType.DELETE,
                        element_id=elem.id,
                    )
                    if self.apply_operation(op):
                        applied += 1

        return applied
