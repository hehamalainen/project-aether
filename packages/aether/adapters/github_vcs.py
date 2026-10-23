"""
GitHub VCS Adapter for Project Aether.

This module implements the VCSAdapter interface for creating pull requests
on GitHub, providing backward compatibility with traditional Git workflows.
"""
import httpx
from typing import Optional, Dict, Any
import logging
import base64

from .vcs_adapter import VCSAdapter, VCSConfig, SubmitResult, SubmitStatus

logger = logging.getLogger(__name__)


class GitHubVCS(VCSAdapter):
    """
    VCS adapter for creating pull requests on GitHub.
    
    Provides backward compatibility for teams that want to continue
    using GitHub while transitioning to Aether.
    """
    
    GITHUB_API_URL = "https://api.github.com"
    
    def __init__(self, config: VCSConfig):
        """Initialize the GitHub adapter with configuration."""
        super().__init__(config)
        if not config.github_token:
            raise ValueError("github_token is required for GitHubVCS")
        if not config.github_repo:
            raise ValueError("github_repo is required for GitHubVCS")
        
        self.token = config.github_token
        self.repo = config.github_repo  # "owner/repo"
        self.base_branch = config.github_base_branch
    
    def _get_headers(self) -> Dict[str, str]:
        """Build request headers with GitHub auth."""
        return {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {self.token}",
            "X-GitHub-Api-Version": "2022-11-28"
        }
    
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
        Submit a code change as a GitHub Pull Request.
        
        Note: This is a simplified implementation. In production,
        you would need to:
        1. Create a new branch
        2. Apply the diff to files
        3. Commit the changes
        4. Create the PR
        
        Args:
            diff: Unified diff of the changes
            confidence: Agent's confidence score [0, 1]
            agent_id: Identifier of the submitting agent
            task_id: Task/PR reference
            paths: List of affected file paths
            metadata: Optional additional metadata
            
        Returns:
            SubmitResult with PR number and status
        """
        branch_name = f"aether/{task_id}/{agent_id[:8]}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Get the base branch SHA
                ref_url = f"{self.GITHUB_API_URL}/repos/{self.repo}/git/refs/heads/{self.base_branch}"
                ref_response = await client.get(ref_url, headers=self._get_headers())
                
                if ref_response.status_code != 200:
                    return SubmitResult(
                        success=False,
                        status=SubmitStatus.ERROR,
                        message=f"Failed to get base branch: {ref_response.text}"
                    )
                
                base_sha = ref_response.json()["object"]["sha"]
                
                # Create a new branch
                create_ref_url = f"{self.GITHUB_API_URL}/repos/{self.repo}/git/refs"
                create_ref_response = await client.post(
                    create_ref_url,
                    headers=self._get_headers(),
                    json={
                        "ref": f"refs/heads/{branch_name}",
                        "sha": base_sha
                    }
                )
                
                if create_ref_response.status_code not in (200, 201, 422):  # 422 = already exists
                    return SubmitResult(
                        success=False,
                        status=SubmitStatus.ERROR,
                        message=f"Failed to create branch: {create_ref_response.text}"
                    )
                
                # Create the PR
                pr_url = f"{self.GITHUB_API_URL}/repos/{self.repo}/pulls"
                pr_body = f"""## AI Agent Submission

**Agent ID:** `{agent_id}`
**Task ID:** `{task_id}`
**Confidence:** {confidence:.2%}

### Affected Files
{chr(10).join(f'- `{p}`' for p in paths)}

### Diff
```diff
{diff[:2000]}{'...(truncated)' if len(diff) > 2000 else ''}
```

---
*Submitted via Aether VCS Adapter*
"""
                pr_response = await client.post(
                    pr_url,
                    headers=self._get_headers(),
                    json={
                        "title": f"[Aether] {task_id}: Agent {agent_id[:8]} submission",
                        "head": branch_name,
                        "base": self.base_branch,
                        "body": pr_body,
                        "draft": confidence < 0.7  # Draft if low confidence
                    }
                )
                
                if pr_response.status_code in (200, 201):
                    data = pr_response.json()
                    return SubmitResult(
                        success=True,
                        proposal_id=str(data["number"]),
                        status=SubmitStatus.PENDING,
                        message=f"PR #{data['number']} created",
                        metadata={"pr_url": data["html_url"], "pr_number": data["number"]}
                    )
                else:
                    return SubmitResult(
                        success=False,
                        status=SubmitStatus.ERROR,
                        message=f"Failed to create PR: {pr_response.text}"
                    )
                    
        except Exception as e:
            logger.exception("Error creating GitHub PR")
            return SubmitResult(
                success=False,
                status=SubmitStatus.ERROR,
                message=f"Connection error: {str(e)}"
            )
    
    async def get_status(self, proposal_id: str) -> SubmitResult:
        """
        Get the status of a GitHub Pull Request.
        
        Args:
            proposal_id: PR number
            
        Returns:
            SubmitResult with current PR status
        """
        url = f"{self.GITHUB_API_URL}/repos/{self.repo}/pulls/{proposal_id}"
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self._get_headers())
                
                if response.status_code == 200:
                    data = response.json()
                    state = data.get("state", "open")
                    merged = data.get("merged", False)
                    
                    if merged:
                        status = SubmitStatus.ACCEPTED
                    elif state == "closed":
                        status = SubmitStatus.REJECTED
                    else:
                        status = SubmitStatus.PENDING
                    
                    return SubmitResult(
                        success=True,
                        proposal_id=proposal_id,
                        status=status,
                        metadata={"state": state, "merged": merged}
                    )
                else:
                    return SubmitResult(
                        success=False,
                        proposal_id=proposal_id,
                        status=SubmitStatus.ERROR,
                        message=f"Failed to get PR status: {response.status_code}"
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
        Check if GitHub API is reachable.
        
        Returns:
            True if API responds successfully
        """
        url = f"{self.GITHUB_API_URL}/rate_limit"
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=self._get_headers())
                return response.status_code == 200
        except Exception:
            return False
