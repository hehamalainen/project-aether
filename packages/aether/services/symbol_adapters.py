"""
Symbol extraction adapters for non-Python source files.

Primary mode uses tree-sitter when available. If tree-sitter bindings are not
installed, adapters fall back to lightweight regex extraction so conflict
detection can still operate on symbol-level units.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


@dataclass(frozen=True)
class SymbolSpan:
    """A semantic symbol span in source text."""

    symbol_id: str
    start_line: int
    end_line: int


_JS_TS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_GO_EXTENSIONS = {".go"}
_JAVA_EXTENSIONS = {".java"}
_RUST_EXTENSIONS = {".rs"}


def _file_extension(path: str) -> str:
    dot = path.rfind(".")
    if dot < 0:
        return ""
    return path[dot:].lower()


def _tree_sitter_language_for_extension(ext: str) -> Optional[str]:
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if ext in {".ts", ".tsx"}:
        return "typescript"
    if ext == ".go":
        return "go"
    if ext == ".java":
        return "java"
    if ext == ".rs":
        return "rust"
    return None


def _tree_sitter_extract(path: str, source_code: str) -> list[SymbolSpan]:
    ext = _file_extension(path)
    language_name = _tree_sitter_language_for_extension(ext)
    if not language_name:
        return []

    try:
        from tree_sitter import Parser
        from tree_sitter_languages import get_language
    except Exception:
        return []

    try:
        language = get_language(language_name)
        parser = Parser()
        parser.set_language(language)
        tree = parser.parse(source_code.encode("utf-8"))
    except Exception:
        return []

    spans: list[SymbolSpan] = []

    def add_node_symbol(node, symbol_kind: str, name_node) -> None:
        if not name_node:
            return
        name = source_code[name_node.start_byte:name_node.end_byte].strip()
        if not name:
            return
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if end < start:
            end = start
        spans.append(SymbolSpan(symbol_id=f"{symbol_kind}:{name}", start_line=start, end_line=end))

    def visit(node) -> None:
        node_type = getattr(node, "type", "")

        if language_name in {"javascript", "typescript"}:
            if node_type == "function_declaration":
                add_node_symbol(node, "FUNC", node.child_by_field_name("name"))
            elif node_type == "class_declaration":
                add_node_symbol(node, "CLASS", node.child_by_field_name("name"))
            elif node_type == "method_definition":
                add_node_symbol(node, "METHOD", node.child_by_field_name("name"))
        elif language_name == "go":
            if node_type == "function_declaration":
                add_node_symbol(node, "FUNC", node.child_by_field_name("name"))
            elif node_type == "method_declaration":
                add_node_symbol(node, "METHOD", node.child_by_field_name("name"))
            elif node_type == "type_declaration":
                add_node_symbol(node, "TYPE", node.child_by_field_name("name"))
        elif language_name == "java":
            if node_type in {"class_declaration", "interface_declaration"}:
                add_node_symbol(node, "CLASS", node.child_by_field_name("name"))
            elif node_type == "method_declaration":
                add_node_symbol(node, "METHOD", node.child_by_field_name("name"))
        elif language_name == "rust":
            if node_type == "function_item":
                add_node_symbol(node, "FUNC", node.child_by_field_name("name"))
            elif node_type in {"struct_item", "enum_item", "trait_item"}:
                add_node_symbol(node, "TYPE", node.child_by_field_name("name"))

        for child in getattr(node, "children", []):
            visit(child)

    visit(tree.root_node)
    return _dedupe_and_order_spans(spans)


def _dedupe_and_order_spans(spans: list[SymbolSpan]) -> list[SymbolSpan]:
    if not spans:
        return []

    spans = sorted(spans, key=lambda s: (s.start_line, s.end_line, s.symbol_id))
    result: list[SymbolSpan] = []
    seen: dict[str, int] = {}
    for span in spans:
        count = seen.get(span.symbol_id, 0) + 1
        seen[span.symbol_id] = count
        symbol_id = span.symbol_id if count == 1 else f"{span.symbol_id}#{count}"
        result.append(SymbolSpan(symbol_id=symbol_id, start_line=span.start_line, end_line=span.end_line))
    return result


def _regex_extract_js_ts(source_code: str) -> list[SymbolSpan]:
    lines = source_code.splitlines()
    if not lines:
        return []

    fn_pattern = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    )
    class_pattern = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)\b")
    const_fn_pattern = re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>"
    )

    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        match = fn_pattern.match(line)
        if match:
            starts.append((i, f"FUNC:{match.group(1)}"))
            continue

        match = const_fn_pattern.match(line)
        if match:
            starts.append((i, f"FUNC:{match.group(1)}"))
            continue

        match = class_pattern.match(line)
        if match:
            starts.append((i, f"CLASS:{match.group(1)}"))

    if not starts:
        return []

    return _spans_from_starts(lines, starts)


def _spans_from_starts(lines: list[str], starts: list[tuple[int, str]]) -> list[SymbolSpan]:
    """Build contiguous line spans from declaration start lines."""
    if not starts:
        return []
    spans: list[SymbolSpan] = []
    for idx, (start_line, symbol_id) in enumerate(starts):
        if idx + 1 < len(starts):
            end_line = starts[idx + 1][0] - 1
        else:
            end_line = len(lines)
        if end_line < start_line:
            end_line = start_line
        spans.append(SymbolSpan(symbol_id=symbol_id, start_line=start_line, end_line=end_line))

    return _dedupe_and_order_spans(spans)


def _regex_extract_go(source_code: str) -> list[SymbolSpan]:
    lines = source_code.splitlines()
    if not lines:
        return []

    fn_pattern = re.compile(
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("
    )
    type_pattern = re.compile(
        r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface|\w+)"
    )
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        match = fn_pattern.match(line)
        if match:
            starts.append((i, f"FUNC:{match.group(1)}"))
            continue
        match = type_pattern.match(line)
        if match:
            starts.append((i, f"TYPE:{match.group(1)}"))
    return _spans_from_starts(lines, starts)


def _regex_extract_java(source_code: str) -> list[SymbolSpan]:
    lines = source_code.splitlines()
    if not lines:
        return []

    type_pattern = re.compile(
        r"^\s*(?:public|private|protected)?\s*(?:abstract|final)?\s*"
        r"(class|interface|enum)\s+([A-Za-z_]\w*)\b"
    )
    method_pattern = re.compile(
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?"
        r"(?:[A-Za-z_][\w<>,\[\]]*\s+)+([A-Za-z_]\w*)\s*\("
    )
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        match = type_pattern.match(line)
        if match:
            starts.append((i, f"CLASS:{match.group(2)}"))
            continue
        match = method_pattern.match(line)
        if match and match.group(1) not in {"if", "for", "while", "switch", "catch"}:
            starts.append((i, f"METHOD:{match.group(1)}"))
    return _spans_from_starts(lines, starts)


def _regex_extract_rust(source_code: str) -> list[SymbolSpan]:
    lines = source_code.splitlines()
    if not lines:
        return []

    fn_pattern = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(")
    type_pattern = re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)\b")
    impl_pattern = re.compile(r"^\s*impl\s+([A-Za-z_]\w*)\b")
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        match = fn_pattern.match(line)
        if match:
            starts.append((i, f"FUNC:{match.group(1)}"))
            continue
        match = type_pattern.match(line)
        if match:
            starts.append((i, f"TYPE:{match.group(1)}"))
            continue
        match = impl_pattern.match(line)
        if match:
            starts.append((i, f"TYPE:{match.group(1)}"))
    return _spans_from_starts(lines, starts)


def extract_symbol_spans(path: str, source_code: str) -> list[SymbolSpan]:
    """
    Extract semantic symbol spans for a file.

    For supported languages, tries tree-sitter first and falls back to regex.
    """
    ext = _file_extension(path)
    if ext in _JS_TS_EXTENSIONS:
        spans = _tree_sitter_extract(path, source_code)
        if spans:
            return spans
        return _regex_extract_js_ts(source_code)

    if ext in _GO_EXTENSIONS:
        spans = _tree_sitter_extract(path, source_code)
        if spans:
            return spans
        return _regex_extract_go(source_code)

    if ext in _JAVA_EXTENSIONS:
        spans = _tree_sitter_extract(path, source_code)
        if spans:
            return spans
        return _regex_extract_java(source_code)

    if ext in _RUST_EXTENSIONS:
        spans = _tree_sitter_extract(path, source_code)
        if spans:
            return spans
        return _regex_extract_rust(source_code)

    spans = _tree_sitter_extract(path, source_code)
    if spans:
        return spans

    return []


def find_symbol_at_line(spans: list[SymbolSpan], line_num: int) -> Optional[str]:
    """Find the most specific symbol that covers a given 1-based line number."""
    candidates = [
        span
        for span in spans
        if span.start_line <= line_num <= span.end_line
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda span: (span.end_line - span.start_line, span.start_line))
    return candidates[0].symbol_id
