"""
AST-Level Structured Document for Aether.

Decomposes Python source code into semantic nodes using libcst.
Each node is managed by an independent RGADocument for convergence.
"""

import ast
import libcst as cst
from typing import Dict, List, Optional
import structlog
from .rga import RGADocument

logger = structlog.get_logger()


class StructuredDocument:
    """A document decomposed into semantic nodes for structured merging."""

    def __init__(self, replica_id: str):
        self.replica_id = replica_id
        # node_id -> RGADocument
        self.nodes: Dict[str, RGADocument] = {}
        # Order of node IDs to preserve file structure
        self.node_order: List[str] = []
        
    def set_content(self, source_code: str):
        """Initialize or reset document from source code."""
        try:
            tree = cst.parse_module(source_code)
            self._decompose_tree(tree, source_code)
        except Exception as e:
            logger.error("parse_failed", error=str(e))
            # Fallback to single raw node if parsing fails
            self._set_raw_node(source_code)

    def _set_raw_node(self, content: str):
        """Fallback for non-Python or invalid Python."""
        node_id = "__raw__"
        doc = RGADocument(self.replica_id)
        doc.set_text(content)
        self.nodes = {node_id: doc}
        self.node_order = [node_id]

    @staticmethod
    def _class_member_start_line(node: ast.stmt) -> int:
        """Best-effort starting line for class member, including decorators."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorator_lines = [d.lineno for d in node.decorator_list if hasattr(d, "lineno")]
            return min([node.lineno] + decorator_lines) if decorator_lines else node.lineno
        return node.lineno if hasattr(node, "lineno") else 1

    def _decompose_class_by_methods(
        self,
        class_name: str,
        class_node: Optional[ast.ClassDef],
        full_class_text: str,
        source_lines: list[str],
    ) -> None:
        """
        Decompose class into header + per-method nodes when possible.

        This allows independent method edits to merge without treating the whole class
        as a single conflict region.
        """
        if not class_node:
            self._add_node(f"CLASS:{class_name}::__head__", full_class_text)
            return

        class_start = getattr(class_node, "lineno", None)
        class_end = getattr(class_node, "end_lineno", None)
        if class_start is None or class_end is None:
            self._add_node(f"CLASS:{class_name}::__head__", full_class_text)
            return

        raw_class_slice = "".join(source_lines[class_start - 1 : class_end])
        leading_prefix = ""
        if raw_class_slice and full_class_text.endswith(raw_class_slice):
            leading_prefix = full_class_text[: len(full_class_text) - len(raw_class_slice)]

        methods = [
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if not methods:
            self._add_node(f"CLASS:{class_name}::__head__", full_class_text)
            return

        methods.sort(key=self._class_member_start_line)

        first_start = self._class_member_start_line(methods[0])
        if first_start > class_start:
            header_text = "".join(source_lines[class_start - 1 : first_start - 1])
            header_text = leading_prefix + header_text
            if header_text:
                self._add_node(f"CLASS:{class_name}::__head__", header_text)

        for idx, method in enumerate(methods):
            start_line = self._class_member_start_line(method)
            if idx + 1 < len(methods):
                end_line = self._class_member_start_line(methods[idx + 1]) - 1
            else:
                end_line = class_end
            if end_line < start_line:
                continue

            method_kind = "ASYNCFUNC" if isinstance(method, ast.AsyncFunctionDef) else "FUNC"
            method_text = "".join(source_lines[start_line - 1 : end_line])
            if not method_text:
                continue

            method_id = f"CLASS:{class_name}::{method_kind}:{method.name}"
            self._add_node(method_id, method_text)

    def _decompose_tree(self, tree: cst.Module, source_code: str):
        """Decompose LibCST module into semantic nodes."""
        self.nodes = {}
        self.node_order = []

        source_lines = source_code.splitlines(keepends=True)
        class_nodes: list[ast.ClassDef] = []
        try:
            py_tree = ast.parse(source_code)
            class_nodes = [node for node in py_tree.body if isinstance(node, ast.ClassDef)]
        except Exception:
            class_nodes = []
        class_index = 0
        
        # 1. Handle Module Header (whitespace/comments before imports)
        if tree.header:
            header_code = "".join(tree.code_for_node(line) for line in tree.header)
            if header_code:
                self._add_node("__header__", header_code)
        
        # 2. Group Imports
        imports: List[cst.CSTNode] = []
        body_remain: List[cst.CSTNode] = []
        
        for stmt in tree.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                is_import = any(isinstance(b, (cst.Import, cst.ImportFrom)) for b in stmt.body)
                if is_import:
                    imports.append(stmt)
                    continue
            body_remain.append(stmt)
            
        if imports:
            node_id = "__imports__"
            import_code = "".join(tree.code_for_node(i) for i in imports)
            self._add_node(node_id, import_code)
            
        # 3. Process remaining body
        for stmt in body_remain:
            if isinstance(stmt, cst.FunctionDef):
                node_id = f"FUNC:{stmt.name.value}"
                self._add_node(node_id, tree.code_for_node(stmt))
            elif isinstance(stmt, cst.ClassDef):
                full_class_text = tree.code_for_node(stmt)
                class_node = class_nodes[class_index] if class_index < len(class_nodes) else None
                if class_index < len(class_nodes):
                    class_index += 1
                self._decompose_class_by_methods(
                    class_name=stmt.name.value,
                    class_node=class_node,
                    full_class_text=full_class_text,
                    source_lines=source_lines,
                )
            else:
                # Group other statements into globals
                node_id = "__globals__"
                content = tree.code_for_node(stmt)
                if node_id in self.nodes:
                    # Append text to existing globals node text
                    current_text = self.nodes[node_id].get_text()
                    self.nodes[node_id].set_text(current_text + content)
                else:
                    self._add_node(node_id, content)

        # 4. Handle Module Footer
        if tree.footer:
            footer_code = "".join(tree.code_for_node(line) for line in tree.footer)
            if footer_code:
                self._add_node("__footer__", footer_code)

    def _add_node(self, node_id: str, content: str):
        base_id = node_id
        suffix = 2
        while node_id in self.nodes:
            node_id = f"{base_id}#{suffix}"
            suffix += 1

        doc = RGADocument(self.replica_id)
        doc.set_text(content)
        self.nodes[node_id] = doc
        if node_id not in self.node_order:
            self.node_order.append(node_id)

    def get_full_text(self) -> str:
        """Stitch all nodes back into a single file."""
        return "".join(self.nodes[nid].get_text() for nid in self.node_order)

    def list_nodes(self) -> List[str]:
        return self.node_order

    def get_node_at_line(self, line_num: int) -> Optional[str]:
        """Find which semantic node contains the given line number (1-based)."""
        current_line = 1
        for node_id in self.node_order:
            node_text = self.nodes[node_id].get_text()
            node_lines = node_text.count("\n") + (1 if node_text and not node_text.endswith("\n") else 0)
            if current_line <= line_num < current_line + node_lines:
                return node_id
            current_line += node_lines
        return None

    def update_node(self, node_id: str, new_content: str):
        """Update a specific node with new content."""
        if node_id in self.nodes:
            self.nodes[node_id].set_text(new_content)
        else:
            self._add_node(node_id, new_content)
