"""
structural_merge.py — Python Class 1 Structural AST Merger (Track 1).

Whitelist-bound: Only class_definition child nodes (methods) are eligible.
No DB schema changes.
No LLM calls.
No external binary dependencies.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from packages.aether.crdt.structured_doc import StructuredDocument
from packages.aether.crdt.rga import RGADocument

logger = logging.getLogger(__name__)


def is_class_member_node(node_id: str) -> bool:
    """
    Return True if the node is a whitelisted class member (method).
    Format: CLASS:<class_name>::FUNC:<method_name> or CLASS:<class_name>::ASYNCFUNC:<method_name>
    """
    return bool(re.match(r"^CLASS:[^:]+::(?:FUNC|ASYNCFUNC):[^:]+$", node_id))


def get_modified_nodes(base_doc: StructuredDocument, doc: StructuredDocument) -> set[str]:
    """Find all node IDs that differ in text content from base_doc, or are new."""
    modified = set()
    for node_id in doc.list_nodes():
        if node_id not in base_doc.nodes:
            modified.add(node_id)
            continue
        text_a = doc.nodes[node_id].get_text().strip()
        text_b = base_doc.nodes[node_id].get_text().strip()
        if text_a != text_b:
            modified.add(node_id)
    return modified


def attempt_structural_merge(
    base_content: str,
    content_a: str,
    content_b: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Attempt to merge changes from content_a and content_b into base_content structurally.

    Eligible ONLY if:
      1. Both parse cleanly.
      2. All modified nodes are whitelisted class members (class_definition methods).
      3. Modified node sets do not intersect (no write/write collision on same method).

    Returns tuple: (merged file content string, None) if successful, or (None, failure_reason) if ineligible.
    """
    # 1. Parse all inputs
    base_doc = StructuredDocument("base")
    base_doc.set_content(base_content)

    doc_a = StructuredDocument("replica_a")
    doc_a.set_content(content_a)

    doc_b = StructuredDocument("replica_b")
    doc_b.set_content(content_b)

    # If any file failed parsing (fell back to single raw node), fail merge
    if "__raw__" in base_doc.nodes or "__raw__" in doc_a.nodes or "__raw__" in doc_b.nodes:
        logger.info("structural_merge_ineligible: parse_failed_or_raw_fallback")
        return None, "PARSE_FAILURE"

    # 2. Extract modified nodes
    mod_a = get_modified_nodes(base_doc, doc_a)
    mod_b = get_modified_nodes(base_doc, doc_b)

    if not mod_a and not mod_b:
        return base_content, None
    if not mod_a:
        return content_b, None
    if not mod_b:
        return content_a, None

    # 3. Whitelist check: modified nodes must be class member methods only
    for node_id in mod_a:
        if not is_class_member_node(node_id):
            logger.info("structural_merge_ineligible: non_whitelisted_node_modified_in_a", node_id=node_id)
            return None, "NON_WHITELISTED_MODIFICATION"
    for node_id in mod_b:
        if not is_class_member_node(node_id):
            logger.info("structural_merge_ineligible: non_whitelisted_node_modified_in_b", node_id=node_id)
            return None, "NON_WHITELISTED_MODIFICATION"

    # 4. Write/write collision check: no overlap on the exact same method
    overlap = mod_a.intersection(mod_b)
    if overlap:
        logger.info("structural_merge_ineligible: direct_method_overlap_collision", overlap=list(overlap))
        return None, "WRITE_WRITE_COLLISION"

    # 5. Build merged StructuredDocument
    merged_doc = StructuredDocument("merged")
    
    # Initialize with base nodes
    for node_id in base_doc.list_nodes():
        doc = RGADocument("merged")
        doc.set_text(base_doc.nodes[node_id].get_text())
        merged_doc.nodes[node_id] = doc
        merged_doc.node_order.append(node_id)

    # Helper to insert a new class member node deterministically sorted
    def insert_class_member(node_id: str, content: str):
        # Extract class name: CLASS:<class_name>::...
        parts = node_id.split("::")
        class_prefix = parts[0]  # "CLASS:<class_name>"
        head_id = f"{class_prefix}::__head__"

        # Find position to insert. We place it after the class head node.
        # To make it deterministic, we collect all methods of this class, sort them alphabetically, and place them.
        class_methods = []
        for existing_id in merged_doc.node_order:
            if existing_id.startswith(f"{class_prefix}::") and existing_id != head_id:
                class_methods.append(existing_id)
        
        # Add the new node to the list and sort
        if node_id not in class_methods:
            class_methods.append(node_id)
        class_methods.sort()

        # Find where to splice them.
        # Locate the head_id or class_prefix node in the order
        start_idx = -1
        if head_id in merged_doc.node_order:
            start_idx = merged_doc.node_order.index(head_id)
        elif class_prefix in merged_doc.node_order:
            start_idx = merged_doc.node_order.index(class_prefix)
        
        if start_idx == -1:
            # Fallback: append at end if class definition not found
            merged_doc.update_node(node_id, content)
            return

        # Remove existing methods from the order list, and re-insert all sorted methods
        # to ensure deterministic order
        for m_id in class_methods:
            if m_id in merged_doc.node_order:
                merged_doc.node_order.remove(m_id)
        
        # Insert them right after the class header/prefix
        for i, m_id in enumerate(class_methods):
            merged_doc.node_order.insert(start_idx + 1 + i, m_id)
            
        merged_doc.update_node(node_id, content)

    # Apply A's changes
    for node_id in mod_a:
        content = doc_a.nodes[node_id].get_text()
        if node_id in merged_doc.nodes:
            merged_doc.update_node(node_id, content)
        else:
            insert_class_member(node_id, content)

    # Apply B's changes
    for node_id in mod_b:
        content = doc_b.nodes[node_id].get_text()
        if node_id in merged_doc.nodes:
            merged_doc.update_node(node_id, content)
        else:
            insert_class_member(node_id, content)

    # 6. Reconstruct merged content
    try:
        merged_text = merged_doc.get_full_text()
        return merged_text, None
    except Exception as e:
        logger.error("structural_merge_reconstruction_error", error=str(e))
        return None, "RECONSTRUCTION_FAILURE"
