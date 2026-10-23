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
