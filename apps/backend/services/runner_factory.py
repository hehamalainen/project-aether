"""Factory for backend agent runners."""
from __future__ import annotations

from fastapi import HTTPException, status

from packages.idasdlc_core.services.runners import CommandRunner, StaticPlanRunner


def build_runner(
    agent: str,
    *,
    commands: list[str] | None = None,
    validators: list[str] | None = None,
):
    if agent == "static":
        return StaticPlanRunner()
    if agent == "command":
        if not commands:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Command runner requires --commands")
        return CommandRunner(commands=commands, validators=validators)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown agent '{agent}'")
