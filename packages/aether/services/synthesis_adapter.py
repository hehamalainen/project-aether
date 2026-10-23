"""Synthesis Adapter to invoke standalone synthesis worker CLI."""

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple
import structlog

from ..models.synthesis import SynthesisRequestV1, SynthesisResponseV1

logger = structlog.get_logger()

class SynthesisAdapter:
    """Invokes the standalone synthesis worker process via CLI contract."""

    def __init__(self, python_executable: Optional[str] = None):
        self.python_executable = python_executable or sys.executable

    def _compute_sha256(self, content: str) -> str:
        """Compute SHA256 of text content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def invoke_worker(
        self,
        request: SynthesisRequestV1,
        timeout_seconds: int = 120,
    ) -> Tuple[str, Optional[SynthesisResponseV1]]:
        """
        Serialize request, invoke worker process, and parse response.
        Returns (verdict_status, response_obj).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            req_file = tmppath / "request.json"
            resp_file = tmppath / "response.json"

            # Serialize request
            req_json = request.model_dump_json(indent=2)
            req_file.write_text(req_json, encoding="utf-8")

            # Subprocess command: python -m packages.aether.synthesis_worker.main once ...
            cmd = [
                self.python_executable,
                "-m",
                "packages.aether.synthesis_worker.main",
                "once",
                "--request",
                str(req_file),
                "--response",
                str(resp_file),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                    logger.error("synthesis_worker_timeout_expired", timeout=timeout_seconds)
                    return "failed_timeout", None

                if proc.returncode != 0:
                    stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
                    logger.error("synthesis_worker_failed", exit_code=proc.returncode, stderr=stderr_str)
                    return "failed_model_error", None
            except Exception as e:
                logger.error("synthesis_worker_crash", error=str(e))
                return "failed_model_error", None

            if not resp_file.exists():
                logger.error("synthesis_worker_output_missing")
                return "failed_model_error", None

            try:
                resp_json = resp_file.read_text(encoding="utf-8")
                resp = SynthesisResponseV1.model_validate_json(resp_json)
            except Exception as e:
                logger.error("synthesis_response_parse_failed", error=str(e))
                return "failed_model_error", None

            # Cryptographic provenance check: verify request hash matches
            expected_req_hash = self._compute_sha256(req_json)
            if resp.artifacts.request_artifact_hash != expected_req_hash:
                logger.error("synthesis_response_provenance_invalid", expected=expected_req_hash, actual=resp.artifacts.request_artifact_hash)
                return "failed_policy_violation", None

            return resp.status, resp
