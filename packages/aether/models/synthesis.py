"""Pydantic model definitions for SynthesisRequestV1 and SynthesisResponseV1."""

from pydantic import BaseModel, Field
from typing import Optional, List

class ContractConstraintsV1(BaseModel):
    must_preserve_signature: bool = True
    must_preserve_return_annotation: bool = True
    must_not_modify_imports: bool = False
    must_not_modify_tests: bool = True
    must_not_modify_manifest: bool = True

class VerificationContextV1(BaseModel):
    gate_test_selection: str
    test_targets: List[str]
    lint_targets: List[str]
    typecheck_targets: List[str]

class GenerationPolicyV1(BaseModel):
    max_candidates: int = 3
    timeout_seconds: int = 120
    max_prompt_tokens: int = 24000
    max_completion_tokens: int = 6000
    temperature: float = 0.2
    seed: int = 12345
    output_format: str = "replacement_node"

class ModelPolicyV1(BaseModel):
    provider: str
    model: str
    allow_remote_model: bool = False
    prompt_template_version: str = "phase4-synthesis-v1"

class RequestProvenanceV1(BaseModel):
    request_created_at: str
    request_creator: str = "aether-promotion-engine"
    policy_version: str = "phase4-track3-v1"
    request_hash: str

class SynthesisRequestV1(BaseModel):
    schema_version: int = 1
    job_id: str
    repo_id: str
    language: str = "python"
    file_path: str
    base_head_id: str
    current_head_id: str
    conflict_class: str = "class2_isomorphic_mutation"
    symbol_id: str
    parent_symbol_id: str
    base_node_hash: str
    left_proposal_id: str
    right_proposal_id: str
    base_node_text: str
    left_node_text: str
    right_node_text: str
    left_diff: str
    right_diff: str
    allowed_paths: List[str]
    protected_paths: List[str]
    contract_constraints: ContractConstraintsV1
    verification_context: VerificationContextV1
    generation_policy: GenerationPolicyV1
    model_policy: ModelPolicyV1
    provenance: RequestProvenanceV1

class CandidateV1(BaseModel):
    candidate_id: str
    output_format: str = "replacement_node"
    file_path: str
    symbol_id: str
    candidate_text: str
    candidate_hash: str
    declared_touched_symbols: List[str]
    declared_changed_paths: List[str]
    model_rationale: Optional[str] = None

class WorkerVerdictV1(BaseModel):
    declared_conflict: bool = False
    reason: Optional[str] = None
    retryable: bool = False

class TokenUsageV1(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ResponseProvenanceV1(BaseModel):
    worker_id: str
    worker_version: str = "phase4-track3-v1"
    started_at: str
    finished_at: str
    elapsed_ms: int
    provider: str
    model: str
    model_version: Optional[str] = None
    seed: int
    temperature: float
    max_prompt_tokens: int
    max_completion_tokens: int
    prompt_template_version: str
    prompt_hash: str
    raw_model_output_hash: str
    response_hash: str
    token_usage: TokenUsageV1

class ResponseArtifactsV1(BaseModel):
    request_artifact_hash: str
    response_artifact_hash: str
    sanitized_transcript_hash: str

class SynthesisResponseV1(BaseModel):
    schema_version: int = 1
    job_id: str
    status: str
    candidates: List[CandidateV1] = Field(default_factory=list)
    worker_verdict: WorkerVerdictV1
    provenance: ResponseProvenanceV1
    artifacts: ResponseArtifactsV1
