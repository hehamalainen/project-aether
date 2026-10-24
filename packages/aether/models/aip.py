"""AIP (Agent Interoperability Protocol) v0 mutation schema and parsing helpers."""

from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..crdt.path_safety import validate_repo_relative_path


AIP_SCHEMA_VERSION = "aip.v0"


class AIPMutation(BaseModel):
    """One mutation operation in the AIP v0 payload."""

    op: Literal["insert_node", "move_node", "delete_node", "update_node"]
    path: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    content: Optional[str] = None
    after_node_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_mutation(self) -> "AIPMutation":
        path = validate_repo_relative_path(self.path)
        node_id = self.node_id.strip()
        if not node_id:
            raise ValueError("node_id must not be empty")

        if self.op in {"insert_node", "update_node"}:
            if self.content is None:
                raise ValueError(f"{self.op} requires content")
        else:
            if self.content is not None:
                raise ValueError(f"{self.op} must not include content")

        if self.op == "move_node" and self.after_node_id == self.node_id:
            raise ValueError("move_node cannot target the same node as after_node_id")

        self.path = path
        self.node_id = node_id
        if self.after_node_id is not None:
            self.after_node_id = self.after_node_id.strip()
            if not self.after_node_id:
                self.after_node_id = None
        return self


class AIPMutationEnvelope(BaseModel):
    """Top-level AIP mutation envelope."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[AIP_SCHEMA_VERSION] = Field(alias="schema")
    mutations: list[AIPMutation] = Field(min_length=1)


def parse_aip_mutation_envelope(raw_payload: str) -> AIPMutationEnvelope:
    """Parse and validate an AIP v0 JSON mutation payload."""

    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AIP JSON payload: {exc.msg}") from exc

    try:
        return AIPMutationEnvelope.model_validate(parsed)
    except ValidationError as exc:
        first_error = exc.errors()[0] if exc.errors() else {"msg": "unknown validation error"}
        raise ValueError(f"Invalid AIP payload: {first_error['msg']}") from exc


def extract_aip_paths_and_symbols(raw_payload: str) -> tuple[set[str], set[str]]:
    """Extract affected file paths and semantic symbols from AIP payload."""

    envelope = parse_aip_mutation_envelope(raw_payload)
    paths: set[str] = set()
    symbols: set[str] = set()
    for mutation in envelope.mutations:
        paths.add(mutation.path)
        symbols.add(f"{mutation.path}::{mutation.node_id}")
        if mutation.op == "move_node" and mutation.after_node_id:
            symbols.add(f"{mutation.path}::{mutation.after_node_id}")
    return paths, symbols
