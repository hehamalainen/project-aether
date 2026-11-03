"""
structural_merge.py — Python Class 1 Structural AST Merger (Track 1).

Whitelist-bound: Only class_definition child nodes (methods) are eligible.
No DB schema changes.
No LLM calls.
No external binary dependencies.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

import libcst as cst
import structlog

from packages.aether.crdt.structured_doc import StructuredDocument
from packages.aether.crdt.rga import RGADocument

logger = structlog.get_logger()


def is_class_member_node(node_id: str) -> bool:
    """
    Return True if the node is a whitelisted class member (method).
    Format: CLASS:<class_name>::FUNC:<method_name> or CLASS:<class_name>::ASYNCFUNC:<method_name>
    """
    return bool(re.match(r"^CLASS:[^:]+::(?:FUNC|ASYNCFUNC):[^:]+$", node_id))


def get_modified_nodes(base_doc: StructuredDocument, doc: StructuredDocument) -> set[str]:
    """Find all node IDs that differ in text content from base_doc, are new, or were deleted."""
    modified = set()
    for node_id in doc.list_nodes():
        if node_id not in base_doc.nodes:
            modified.add(node_id)
            continue
        text_a = doc.nodes[node_id].get_text().strip()
        text_b = base_doc.nodes[node_id].get_text().strip()
        if text_a != text_b:
            modified.add(node_id)
    for node_id in base_doc.list_nodes():
        if node_id not in doc.nodes:
            modified.add(node_id)
    return modified


def _extract_new_member_runs_by_anchor(
    base_doc: StructuredDocument,
    replica_doc: StructuredDocument,
    added_ids: set[str],
    class_prefix: str,
) -> dict[str, list[list[str]]]:
    """
    Group newly added class members from replica_doc into contiguous runs keyed by
    the nearest preceding base node of the same class (or head_id if inserted at top).
    Preserves untouched base member order as well as intra-proposal declaration order.
    """
    head_id = f"{class_prefix}::__head__"
    default_anchor = head_id if head_id in base_doc.nodes else class_prefix
    current_anchor = default_anchor
    runs_by_anchor: dict[str, list[list[str]]] = {}
    active_run: list[str] = []
    active_anchor = current_anchor

    for nid in replica_doc.list_nodes():
        if nid != class_prefix and not nid.startswith(f"{class_prefix}::"):
            continue
        if nid in base_doc.nodes:
            if active_run:
                runs_by_anchor.setdefault(active_anchor, []).append(active_run)
                active_run = []
            current_anchor = nid
        elif nid in added_ids:
            if not active_run:
                active_anchor = current_anchor
            active_run.append(nid)

    if active_run:
        runs_by_anchor.setdefault(active_anchor, []).append(active_run)

    return runs_by_anchor


def splice_added_class_members(
    merged_doc: StructuredDocument,
    base_doc: StructuredDocument,
    doc_a: StructuredDocument,
    added_a: set[str],
    doc_b: StructuredDocument,
    added_b: set[str],
) -> None:
    """
    Insert newly added class members into merged_doc after their respective base anchors
    without reordering any existing base members. Runs targeting the same anchor are
    sorted by their leading node ID for commutativity.
    """
    all_added = added_a | added_b
    if not all_added:
        return

    for nid in added_a:
        doc = RGADocument("merged")
        doc.set_text(doc_a.nodes[nid].get_text())
        merged_doc.nodes[nid] = doc
    for nid in added_b:
        doc = RGADocument("merged")
        doc.set_text(doc_b.nodes[nid].get_text())
        merged_doc.nodes[nid] = doc

    class_prefixes = sorted({nid.split("::", 1)[0] for nid in all_added})
    for class_prefix in class_prefixes:
        runs_a = _extract_new_member_runs_by_anchor(base_doc, doc_a, added_a, class_prefix)
        runs_b = _extract_new_member_runs_by_anchor(base_doc, doc_b, added_b, class_prefix)
        anchors = set(runs_a.keys()) | set(runs_b.keys())

        ordered_anchors = [nid for nid in merged_doc.node_order if nid in anchors]
        missing_anchors = sorted(anchors - set(ordered_anchors))

        for anchor in ordered_anchors:
            combined_runs = runs_a.get(anchor, []) + runs_b.get(anchor, [])
            combined_runs.sort(key=lambda run: run[0])
            insert_pos = merged_doc.node_order.index(anchor) + 1
            for run in combined_runs:
                for nid in run:
                    if nid not in merged_doc.node_order:
                        merged_doc.node_order.insert(insert_pos, nid)
                        insert_pos += 1

        if missing_anchors:
            class_indices = [
                i
                for i, nid in enumerate(merged_doc.node_order)
                if nid == class_prefix or nid.startswith(f"{class_prefix}::")
            ]
            fallback_pos = (max(class_indices) + 1) if class_indices else len(merged_doc.node_order)
            for anchor in missing_anchors:
                combined_runs = runs_a.get(anchor, []) + runs_b.get(anchor, [])
                combined_runs.sort(key=lambda run: run[0])
                for run in combined_runs:
                    for nid in run:
                        if nid not in merged_doc.node_order:
                            merged_doc.node_order.insert(fallback_pos, nid)
                            fallback_pos += 1


def ensure_valid_node_boundaries(merged_doc: StructuredDocument) -> None:
    """
    Ensure every non-final node in merged_doc ends with a valid newline separator so
    nodes originating from inputs without a trailing EOF newline cannot concatenate
    onto the first line of the following node.
    """
    total = len(merged_doc.node_order)
    for idx, node_id in enumerate(merged_doc.node_order):
        if idx + 1 >= total:
            continue
        node_doc = merged_doc.nodes.get(node_id)
        if node_doc is None:
            continue
        text = node_doc.get_text()
        if not text or text.endswith("\n"):
            continue
        next_id = merged_doc.node_order[idx + 1]
        same_class_followup = (
            node_id.startswith("CLASS:")
            and next_id.startswith("CLASS:")
            and node_id.split("::", 1)[0] == next_id.split("::", 1)[0]
        )
        separator = "\n\n" if (same_class_followup and is_class_member_node(node_id)) else "\n"
        node_doc.set_text(text + separator)


def validate_reconstructed_python(merged_text: str) -> bool:
    """Verify reconstructed Python parses and compiles cleanly without executing it."""
    try:
        ast.parse(merged_text)
        cst.parse_module(merged_text)
        compile(merged_text, "<aether-merge>", "exec", dont_inherit=True)
        return True
    except Exception as exc:
        logger.error("structural_merge_syntax_validation_failed", error=str(exc))
        return False


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
        return (base_content, None) if validate_reconstructed_python(base_content) else (None, "RECONSTRUCTION_FAILURE")
    if not mod_a:
        return (content_b, None) if validate_reconstructed_python(content_b) else (None, "RECONSTRUCTION_FAILURE")
    if not mod_b:
        return (content_a, None) if validate_reconstructed_python(content_a) else (None, "RECONSTRUCTION_FAILURE")

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

    # 5. Build merged StructuredDocument preserving exact base member order
    merged_doc = StructuredDocument("merged")
    for node_id in base_doc.list_nodes():
        doc = RGADocument("merged")
        doc.set_text(base_doc.nodes[node_id].get_text())
        merged_doc.nodes[node_id] = doc
        merged_doc.node_order.append(node_id)

    added_a: set[str] = set()
    deleted_ids: list[str] = []
    for node_id in mod_a:
        if node_id not in doc_a.nodes:
            deleted_ids.append(node_id)
        elif node_id in merged_doc.nodes:
            merged_doc.update_node(node_id, doc_a.nodes[node_id].get_text())
        else:
            added_a.add(node_id)

    added_b: set[str] = set()
    for node_id in mod_b:
        if node_id not in doc_b.nodes:
            deleted_ids.append(node_id)
        elif node_id in merged_doc.nodes:
            merged_doc.update_node(node_id, doc_b.nodes[node_id].get_text())
        else:
            added_b.add(node_id)

    # Splice additions before removing deleted anchors so anchor positions stay exact
    splice_added_class_members(merged_doc, base_doc, doc_a, added_a, doc_b, added_b)

    for node_id in deleted_ids:
        merged_doc.nodes.pop(node_id, None)
        if node_id in merged_doc.node_order:
            merged_doc.node_order.remove(node_id)

    ensure_valid_node_boundaries(merged_doc)

    # 6. Reconstruct and syntax-validate merged content
    try:
        merged_text = merged_doc.get_full_text()
    except Exception as e:
        logger.error("structural_merge_reconstruction_error", error=str(e))
        return None, "RECONSTRUCTION_FAILURE"

    if not validate_reconstructed_python(merged_text):
        return None, "RECONSTRUCTION_FAILURE"

    return merged_text, None
