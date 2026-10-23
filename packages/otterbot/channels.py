"""Minimal channel adapters for OtterBot v1 scope."""

from __future__ import annotations


class ChannelAdapter:
    """Base interface for channel request validation."""

    allowed_origins: tuple[str, ...] = ()

    def validate_origin(self, origin: str) -> bool:
        return any(origin.startswith(prefix) for prefix in self.allowed_origins)


class TeamsAdapter(ChannelAdapter):
    """Teams channel adapter constrained to trusted origins."""

    allowed_origins = ("https://smba.trafficmanager.net", "https://teams.microsoft.com")


class SlackAdapter(ChannelAdapter):
    """Slack channel adapter constrained to trusted origins."""

    allowed_origins = ("https://slack.com", "https://hooks.slack.com")
