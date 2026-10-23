"""Standalone Synthesis Worker CLI."""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from ..models.synthesis import (
    SynthesisRequestV1,
    SynthesisResponseV1,
    CandidateV1,
    WorkerVerdictV1,
    TokenUsageV1,
    ResponseProvenanceV1,
    ResponseArtifactsV1,
)

def sha256_hash(text: str) -> str:
    """Return SHA256 hash of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def combine_node_texts(base: str, left: str, right: str) -> Optional[str]:
    """Deterministically combine left and right edits into base for testing."""
    base_lines = [l.strip() for l in base.splitlines() if l.strip()]
    left_lines = [l.strip() for l in left.splitlines() if l.strip()]
    right_lines = [l.strip() for l in right.splitlines() if l.strip()]

    # Check for direct conflicts (e.g. self.var = X vs self.var = Y)
    left_assigns = {}
    right_assigns = {}
    for stmt in left_lines:
        m = re.match(r"^self\.([a-zA-Z0-9_]+)\s*=\s*(.+)$", stmt)
        if m:
            left_assigns[m.group(1)] = m.group(2).strip()
    for stmt in right_lines:
        m = re.match(r"^self\.([a-zA-Z0-9_]+)\s*=\s*(.+)$", stmt)
        if m:
            right_assigns[m.group(1)] = m.group(2).strip()

    for var, val in left_assigns.items():
        if var in right_assigns and right_assigns[var] != val:
            # Conflict detected
            return None

    # Collect unique statement lines from left and right that are new
    new_statements = []
    for l in left_lines + right_lines:
        if l not in base_lines and l not in new_statements:
            if l.startswith("def ") or l == "pass":
                continue
            new_statements.append(l)

    # Rebuild from first line of left text (the signature)
    sig = left.splitlines()[0] if left.splitlines() else "def dummy(self):"
    indent_count = len(sig) - len(sig.lstrip())
    body_indent = " " * (indent_count + 4)
    body = "\n".join(f"{body_indent}{stmt}" for stmt in new_statements)
    if not body.strip():
        body = f"{body_indent}pass"
    return f"{sig}\n{body}\n"

def run_once(request_path: Path, response_path: Path) -> int:
    """Run a single synthesis job once."""
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start_perf = time.perf_counter()

    if not request_path.exists():
        print(f"Request file not found: {request_path}", file=sys.stderr)
        return 1

    # Read and parse request
    try:
        request_text = request_path.read_text(encoding="utf-8")
        req = SynthesisRequestV1.model_validate_json(request_text)
    except ValidationError as ve:
        print(f"Schema validation error: {ve}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Failed to read request: {e}", file=sys.stderr)
        return 2

    # Simulated model policy / provider
    # Reject remote model by default unless allow_remote_model is True
    if req.model_policy.provider != "configured_provider" and not req.model_policy.allow_remote_model:
        # Policy violation
        verdict = WorkerVerdictV1(declared_conflict=False, reason="policy_violation", retryable=False)
        status = "failed_policy_violation"
        candidates = []
    else:
        # Attempt merge logic
        merged_text = combine_node_texts(req.base_node_text, req.left_node_text, req.right_node_text)
        if merged_text is None:
            # Declared conflict
            verdict = WorkerVerdictV1(declared_conflict=True, reason="synthesis_declared_conflict", retryable=False)
            status = "declared_conflict"
            candidates = []
        else:
            # Successful candidate ready
            cand_id = f"cand_{hashlib.md5(merged_text.encode('utf-8')).hexdigest()[:12]}"
            cand = CandidateV1(
                candidate_id=cand_id,
                output_format="replacement_node",
                file_path=req.file_path,
                symbol_id=req.symbol_id,
                candidate_text=merged_text,
                candidate_hash=sha256_hash(merged_text),
                declared_touched_symbols=[req.symbol_id],
                declared_changed_paths=[req.file_path],
                model_rationale="Simulated deterministic merge candidate."
            )
            verdict = WorkerVerdictV1(declared_conflict=False, reason=None, retryable=False)
            status = "candidate_ready"
            candidates = [cand]

    # Calculate token usage and timing
    elapsed_ms = int((time.perf_counter() - start_perf) * 1000.0)
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    token_usage = TokenUsageV1(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150
    )

    prov = ResponseProvenanceV1(
        worker_id="aether-synthesis-worker-local",
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        provider=req.model_policy.provider,
        model=req.model_policy.model,
        model_version="1.0-fake",
        seed=req.generation_policy.seed,
        temperature=req.generation_policy.temperature,
        max_prompt_tokens=req.generation_policy.max_prompt_tokens,
        max_completion_tokens=req.generation_policy.max_completion_tokens,
        prompt_template_version=req.model_policy.prompt_template_version,
        prompt_hash=sha256_hash(req.base_node_text),
        raw_model_output_hash=sha256_hash(candidates[0].candidate_text) if candidates else "",
        response_hash="",
        token_usage=token_usage
    )

    req_hash = sha256_hash(request_text)
    if req.provenance.request_hash == "trigger_mismatch":
        req_hash = "mismatched_hash_value"

    artifacts = ResponseArtifactsV1(
        request_artifact_hash=req_hash,
        response_artifact_hash="",
        sanitized_transcript_hash=sha256_hash("transcript-sanitized")
    )

    resp = SynthesisResponseV1(
        job_id=req.job_id,
        status=status,
        candidates=candidates,
        worker_verdict=verdict,
        provenance=prov,
        artifacts=artifacts
    )

    # Compute response hashes and update fields
    resp_json = resp.model_dump_json(indent=2)
    resp.provenance.response_hash = sha256_hash(resp_json)
    resp.artifacts.response_artifact_hash = sha256_hash(resp_json)

    # Write output
    try:
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(resp.model_dump_json(indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Failed to write response: {e}", file=sys.stderr)
        return 3

    return 0

def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Aether Standalone Synthesis Worker CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    once_parser = subparsers.add_parser("once", help="Run once mode")
    once_parser.add_argument("--request", required=True, help="Path to request JSON file")
    once_parser.add_argument("--response", required=True, help="Path to write response JSON file")

    args = parser.parse_args()

    if args.command == "once":
        sys.exit(run_once(Path(args.request), Path(args.response)))

if __name__ == "__main__":
    main()
