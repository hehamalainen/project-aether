"""
Abstract VCS Adapter Interface for Project Aether.

This module defines the abstract base class for version control system adapters,
enabling pluggable backends (Aether, GitHub, etc.) for code submission.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any


class SubmitStatus(Enum):
    """Status of a code submission."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class SubmitResult:
    """Result of a code submission to the VCS backend."""
    success: bool
    proposal_id: Optional[str] = None
    status: SubmitStatus = SubmitStatus.PENDING
    message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class VCSConfig:
    """Configuration for VCS adapter."""
    provider: str  # "aether" or "github"
    
    # Aether-specific settings
    aether_base_url: Optional[str] = None
    aether_repo_id: Optional[str] = None
    aether_api_key: Optional[str] = None
    
    # GitHub-specific settings
    github_token: Optional[str] = None
    github_repo: Optional[str] = None  # "owner/repo"
    github_base_branch: str = "main"


class VCSAdapter(ABC):
    """
    Abstract base class for Version Control System adapters.
    
    Enables pluggable VCS backends for the MX Swarm worker to submit
    code changes to different systems (Aether, GitHub, etc.).
    """
    
    def __init__(self, config: VCSConfig):
        """Initialize the adapter with configuration."""
        self.config = config
    
    @abstractmethod
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
        Submit a code change to the VCS backend.
        
        Args:
            diff: Unified diff of the changes
            confidence: Agent's confidence score [0, 1]
            agent_id: Identifier of the submitting agent
            task_id: Task/PR reference
            paths: List of affected file paths
            metadata: Optional additional metadata
            
        Returns:
            SubmitResult with status and reference ID
        """
        pass
    
    @abstractmethod
    async def get_status(self, proposal_id: str) -> SubmitResult:
        """
        Get the status of a previously submitted proposal.
        
        Args:
            proposal_id: ID returned from submit_change
            
        Returns:
            SubmitResult with current status
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the VCS backend is reachable and healthy.
        
        Returns:
            True if backend is healthy, False otherwise
        """
        pass
