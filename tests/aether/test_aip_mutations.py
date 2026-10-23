"""Tests for AIP v0 mutation schema/validation."""

import pytest

from packages.aether.models.aip import (
    extract_aip_paths_and_symbols,
    parse_aip_mutation_envelope,
)


def test_parse_aip_mutation_envelope_valid_payload():
    payload = """
    {
      "schema": "aip.v0",
      "mutations": [
        {
          "op": "update_node",
          "path": "src/main.py",
          "node_id": "FUNC:alpha",
          "content": "def alpha():\\n    return 2\\n"
        }
      ]
    }
    """
    envelope = parse_aip_mutation_envelope(payload)
    assert envelope.schema_version == "aip.v0"
    assert len(envelope.mutations) == 1
    assert envelope.mutations[0].op == "update_node"


def test_parse_aip_mutation_envelope_rejects_invalid_payload():
    payload = """
    {
      "schema": "aip.v0",
      "mutations": [
        {
          "op": "update_node",
          "path": "src/main.py",
          "node_id": "FUNC:alpha"
        }
      ]
    }
    """
    with pytest.raises(ValueError):
        parse_aip_mutation_envelope(payload)


def test_extract_aip_paths_and_symbols_collects_move_dependencies():
    payload = """
    {
      "schema": "aip.v0",
      "mutations": [
        {
          "op": "move_node",
          "path": "src/main.py",
          "node_id": "FUNC:alpha",
          "after_node_id": "FUNC:beta"
        }
      ]
    }
    """
    paths, symbols = extract_aip_paths_and_symbols(payload)
    assert paths == {"src/main.py"}
    assert "src/main.py::FUNC:alpha" in symbols
    assert "src/main.py::FUNC:beta" in symbols
