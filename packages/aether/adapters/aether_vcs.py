"""
Aether VCS Adapter for Project Aether.

This module implements the VCSAdapter interface for submitting proposals
to the Aether version control system.
"""
import httpx
from typing import Optional, Dict, Any
import logging

from .vcs_adapter import VCSAdapter, VCSConfig, SubmitResult, SubmitStatus

logger = logging.getLogger(__name__)


class AetherVCS(VCSAdapter):
    """
    VCS adapter for submitting proposals to Aether.
    
    Implements the VCSAdapter interface to translate MX Swarm worker
    outputs into Aether proposal submissions.
    """
    
    def __init__(self, config: VCSConfig):
        """Initialize the Aether adapter with configuration."""
        super().__init__(config)
        if not config.aether_base_url:
            raise ValueError("aether_base_url is required for AetherVCS")
        if not config.aether_repo_id:
            raise ValueError("aether_repo_id is required for AetherVCS")
        
        base_url = config.aether_base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        self.base_url = base_url
        self.repo_id = config.aether_repo_id
        self.api_key = config.aether_api_key
    
    def _get_headers(self) -> Dict[str, str]:
        """Build request headers with optional auth."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    async def submit_change(
        self,
        diff: str,
        confidence: float,
        agent_id: str,
        task_id: str,
        paths: list[str],
        metadata: Optional[Dict[str, Any]] = None
    ) -> SubmitResult:
        """
        Submit a code change as an Aether proposal.
        
        Args:
            diff: Unified diff of the changes
            confidence: Agent's confidence score [0, 1]
            agent_id: Identifier of the submitting agent
            task_id: Task/PR reference
            paths: List of affected file paths
            metadata: Optional additional metadata
            
        Returns:
            SubmitResult with proposal_id and status
        """
        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals"
        
        import os
        contention_mode = False
        contention_group_id = None
        contention_policy = "exactly_one_winner"
        
        if metadata:
            contention_mode = metadata.get("contention_mode", False)
            contention_group_id = metadata.get("contention_group_id")
            contention_policy = metadata.get("contention_policy", "exactly_one_winner")
            
        if not contention_mode:
            contention_mode = os.getenv("AETHER_CONTENTION_MODE", "false").lower() in ("true", "1", "yes", "on")
        if not contention_group_id:
            contention_group_id = os.getenv("AETHER_CONTENTION_GROUP_ID")
        if contention_mode and not contention_group_id and paths:
            run_ref = metadata.get("run_id") or metadata.get("source_run_id") or os.getenv("MX_RUN_ID") or "unknown" if metadata else (os.getenv("MX_RUN_ID") or "unknown")
            paths_key = ":".join(sorted(paths))
            contention_group_id = f"demo_b:{run_ref}:{paths_key}"
            
        # Build payload matching ProposalCreate schema
        payload = {
            "agent_id": agent_id,
            "task_id": task_id,
            "change_type": "unified_diff",
            "change_content": diff,
            "affected_paths": paths,
            "risk_tier": 1,
            "confidence_score": confidence,
            "model_id": metadata.get("model", "unknown") if metadata else "unknown",
            "calibration_profile": "mx-swarm-v0",
            "dependencies": metadata.get("dependencies", []) if metadata else []
        }
        
        if contention_mode:
            payload["contention_mode"] = True
            payload["contention_group_id"] = contention_group_id
            payload["contention_policy"] = contention_policy
            payload["target_paths"] = paths
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                
                if response.status_code in (200, 201):
                    data = response.json()
                    return SubmitResult(
                        success=True,
                        proposal_id=data.get("proposal_id"),
                        status=SubmitStatus.PENDING,
                        message="Proposal submitted successfully",
                        metadata=data
                    )
                else:
                    logger.error(f"Aether proposal submission failed: {response.status_code} - {response.text}")
                    return SubmitResult(
                        success=False,
                        status=SubmitStatus.ERROR,
                        message=f"Submission failed: {response.status_code} - {response.text}"
                    )
        except Exception as e:
            logger.exception("Error submitting proposal to Aether")
            return SubmitResult(
                success=False,
                status=SubmitStatus.ERROR,
                message=f"Connection error: {str(e)}"
            )
    
    async def get_status(self, proposal_id: str) -> SubmitResult:
        """
        Get the status of an Aether proposal.
        
        Args:
            proposal_id: ID of the proposal to check
            
        Returns:
            SubmitResult with current status
        """
        url = f"{self.base_url}/v1/repos/{self.repo_id}/proposals/{proposal_id}"
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self._get_headers())
                
                if response.status_code == 200:
                    data = response.json()
                    status_map = {
                        "INGESTED": SubmitStatus.PENDING,
                        "PENDING": SubmitStatus.PENDING,
                        "EVALUATING": SubmitStatus.PENDING,
                        "PROMOTED": SubmitStatus.ACCEPTED,
                        "REJECTED": SubmitStatus.REJECTED,
                        "QUARANTINED": SubmitStatus.REJECTED
                    }
                    return SubmitResult(
                        success=True,
                        proposal_id=proposal_id,
                        status=status_map.get(data.get("status", "PENDING"), SubmitStatus.PENDING),
                        metadata=data
                    )
                else:
                    return SubmitResult(
                        success=False,
                        proposal_id=proposal_id,
                        status=SubmitStatus.ERROR,
                        message=f"Failed to get status: {response.status_code}"
                    )
        except Exception as e:
            return SubmitResult(
                success=False,
                proposal_id=proposal_id,
                status=SubmitStatus.ERROR,
                message=f"Connection error: {str(e)}"
            )
    
    async def health_check(self) -> bool:
        """
        Check if Aether server is reachable.
        
        Returns:
            True if server responds to health endpoint
        """
        url = f"{self.base_url}/v1/health"
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                return response.status_code == 200
        except Exception:
            return False
