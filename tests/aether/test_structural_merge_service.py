"""Tests for structural_merge.py."""

import pytest
from packages.aether.services.structural_merge import attempt_structural_merge


def test_commutative_merge_two_methods_same_class():
    base = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = None\n"
    )

    # A adds get_name
    content_a = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = None\n"
        "\n"
        "    def get_name(self):\n"
        "        return 'Alice'\n"
    )

    # B adds get_email
    content_b = (
        "class User:\n"
        "    def __init__(self):\n"
        "        self.id = None\n"
        "\n"
        "    def get_email(self):\n"
        "        return 'alice@example.com'\n"
    )

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    assert merged is not None
    assert reason is None
    assert "def get_name(self):" in merged
    assert "def get_email(self):" in merged
    # Order should be sorted alphabetically by method node ID: get_email then get_name
    idx_email = merged.index("get_email")
    idx_name = merged.index("get_name")
    assert idx_email < idx_name


def test_non_commutative_merge_same_method_rejects():
    base = (
        "class User:\n"
        "    def get_name(self):\n"
        "        return 'Base'\n"
    )

    content_a = (
        "class User:\n"
        "    def get_name(self):\n"
        "        return 'Alice'\n"
    )

    content_b = (
        "class User:\n"
        "    def get_name(self):\n"
        "        return 'Bob'\n"
    )

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    # Direct method overlap conflict should return None
    assert merged is None
    assert reason == "WRITE_WRITE_COLLISION"


def test_non_whitelisted_global_function_modified_rejects():
    base = (
        "def helper():\n"
        "    pass\n"
        "\n"
        "class User:\n"
        "    pass\n"
    )

    content_a = (
        "def helper():\n"
        "    print('a')\n"
        "\n"
        "class User:\n"
        "    pass\n"
    )

    content_b = (
        "class User:\n"
        "    def get_email(self):\n"
        "        return 'b'\n"
    )

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    # helper is a global function, not in a class, so not whitelisted
    assert merged is None
    assert reason == "NON_WHITELISTED_MODIFICATION"


def test_invalid_syntax_in_input_returns_none():
    base = "class User:"
    content_a = "class User:\n  def get_name("  # syntax error
    content_b = "class User:\n  pass"

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    assert merged is None
    assert reason == "PARSE_FAILURE"


def test_structural_merge_preserves_module_docstring_on_line_1_and_untouched_order():
    import ast

    header = (
        '"""Runtime orchestration primitives."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import asyncio\n"
        "import dataclasses\n"
        "from typing import Any\n"
        "\n"
        "DEFAULT_WORKERS = 4\n"
        "\n"
        "\n"
    )
    footer = (
        "\n"
        "\n"
        '__all__ = ["Runtime"]\n'
    )
    base = (
        header
        + "class Runtime:\n"
        + '    """Runtime service."""\n'
        + "\n"
        + "    def __init__(self) -> None:\n"
        + "        self.workers = DEFAULT_WORKERS\n"
        + footer
    )
    content_a = (
        header
        + "class Runtime:\n"
        + '    """Runtime service."""\n'
        + "\n"
        + "    def __init__(self) -> None:\n"
        + "        self.workers = DEFAULT_WORKERS\n"
        + "\n"
        + "    def status(self) -> dict[str, Any]:\n"
        + '        return {"workers": self.workers}\n'
        + footer
    )
    content_b = (
        header
        + "class Runtime:\n"
        + '    """Runtime service."""\n'
        + "\n"
        + "    def __init__(self) -> None:\n"
        + "        self.workers = DEFAULT_WORKERS\n"
        + "\n"
        + "    async def ping(self) -> bool:\n"
        + "        await asyncio.sleep(0)\n"
        + "        return True\n"
        + footer
    )

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    assert reason is None
    assert merged is not None

    # Module docstring must remain on line 1 (not moved below imports), preserving __doc__.
    assert merged.startswith(header)
    assert merged.endswith(footer)
    assert merged.splitlines()[0] == '"""Runtime orchestration primitives."""'

    namespace: dict[str, object] = {}
    exec(compile(merged, "runtime.py", "exec"), namespace)
    assert namespace["__doc__"] == "Runtime orchestration primitives."

    # Order-sensitive AST comparison without stripping the module docstring:
    # removing only the two added methods must reproduce the exact base AST.
    base_tree = ast.parse(base)
    merged_tree = ast.parse(merged)
    for node in merged_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Runtime":
            node.body = [
                member
                for member in node.body
                if not (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name in {"status", "ping"}
                )
            ]
    assert ast.dump(merged_tree) == ast.dump(base_tree)


def test_structural_merge_preserves_untouched_method_and_decorator_order():
    base = (
        '"""Runtime module."""\n'
        "import os\n\n"
        "class Runtime:\n"
        "    def z_decorator(fn):\n"
        "        return fn\n\n"
        "    @z_decorator\n"
        "    def alpha(self):\n"
        "        return 1\n"
    )
    content_a = base.rstrip("\n") + '\n\n    def get_pid(self):\n        return os.getpid()\n'
    content_b = base.rstrip("\n") + '\n\n    def get_name(self):\n        return "runtime"\n'

    merged_ab, reason_ab = attempt_structural_merge(base, content_a, content_b)
    merged_ba, reason_ba = attempt_structural_merge(base, content_b, content_a)
    assert reason_ab is None
    assert reason_ba is None
    assert merged_ab is not None
    assert merged_ab == merged_ba

    namespace: dict[str, object] = {}
    exec(compile(merged_ab, "runtime.py", "exec"), namespace)
    assert namespace["__doc__"] == "Runtime module."
    runtime_cls = namespace["Runtime"]
    instance = runtime_cls()
    assert instance.alpha() == 1
    assert instance.get_name() == "runtime"


def test_structural_merge_handles_missing_eof_newlines_across_base_and_proposals():
    base = (
        '"""Runtime module."""\n'
        "import os\n\n"
        "class Runtime:\n"
        "    def __init__(self):\n"
        "        self.ready = True"
    )
    content_a = base + '\n\n    def get_name(self):\n        return "runtime"'
    content_b = base + "\n\n    def get_pid(self):\n        return os.getpid()"

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    assert reason is None
    assert merged is not None

    namespace: dict[str, object] = {}
    exec(compile(merged, "runtime.py", "exec"), namespace)
    assert namespace["__doc__"] == "Runtime module."
    instance = namespace["Runtime"]()
    assert instance.ready is True
    assert instance.get_name() == "runtime"
    assert isinstance(instance.get_pid(), int)


def test_structural_merge_syntax_validation_rejects_mismatched_method_indentation():
    base = "class Runtime:\n  def __init__(self): pass\n"
    content_a = "class Runtime:\n  def __init__(self): pass\n  def get_name(self): return 1\n"
    content_b = "class Runtime:\n    def __init__(self): pass\n    def get_pid(self): return 2\n"

    merged, reason = attempt_structural_merge(base, content_a, content_b)
    assert merged is None
    assert reason == "RECONSTRUCTION_FAILURE"
