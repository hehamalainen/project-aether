"""
VCS Adapter Factory for Project Aether.

This module provides a factory function to create appropriate VCS adapters
based on configuration settings.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vcs_adapter import VCSAdapter, VCSConfig


def create_vcs_adapter(config: "VCSConfig") -> "VCSAdapter":
    """
    Factory function to create a VCS adapter based on configuration.
    
    Args:
        config: VCS configuration specifying provider and endpoints
        
    Returns:
        VCSAdapter instance configured for the specified provider
        
    Raises:
        ValueError: If provider is not supported
    """
    from .vcs_adapter import VCSConfig
    from .aether_vcs import AetherVCS
    from .github_vcs import GitHubVCS
    
    if config.provider == "aether":
        return AetherVCS(config)
    elif config.provider == "github":
        return GitHubVCS(config)
    else:
        raise ValueError(f"Unsupported VCS provider: {config.provider}")


__all__ = ["create_vcs_adapter"]
