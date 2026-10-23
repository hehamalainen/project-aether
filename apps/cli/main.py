"""Typer CLI for ID-ASDLC."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from packages.idasdlc_core import get_settings, init_db
from packages.idasdlc_core.retrieval import TraceRetrievalService
from packages.idasdlc_core.models.validator_policy import ValidatorPolicy
from packages.idasdlc_core.services import (
    CurationService,
    EvaluationService,
    ExportService,
    OrchestratorService,
    RewardCardService,
    RunService,
    TaskService,
)
from packages.idasdlc_core.services.runners import CommandRunner, StaticPlanRunner
from packages.idasdlc_core.utils import load_task_pack
from packages.idasdlc_core.utils.policy_summary import (
    summarize_auto_remediation,
    summarize_policy_failures,
    summarize_policy_status,
)

app = typer.Typer(help="Iterative Deployment for Agentic SDLC")
task_app = typer.Typer(help="Task management")
run_app = typer.Typer(help="Run agents against tasks")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")

console = Console()


def _init_environment() -> None:
    get_settings()
    init_db()


@app.callback()
def main() -> None:  # pragma: no cover - Typer entry point
    _init_environment()


@app.command()
def init(dirname: Path = typer.Option(Path(".idasdlc"), help="Workspace directory")) -> None:
    """Bootstrap local workspace directories."""
    settings = get_settings()
    settings.ensure_directories()
    dirname.mkdir(parents=True, exist_ok=True)
    console.print(f"Workspace ready at {dirname}", style="green")


@task_app.command("import")
def import_task_pack(path: Path = typer.Argument(..., exists=True, file_okay=True)) -> None:
    """Import tasks from a task pack file."""
    pack = load_task_pack(path)
    tasks = TaskService().import_task_pack(pack)
    console.print(f"Imported {len(tasks)} tasks from {pack.metadata.name}", style="green")


@task_app.command("list")
def list_tasks(holdout: bool | None = typer.Option(None)) -> None:
    service = TaskService()
    tasks = service.list_tasks(holdout=holdout)
    table = Table("ID", "Repo", "Holdout", "Validators")
    for task in tasks:
        table.add_row(str(task.id), task.repo, str(task.holdout), ", ".join(task.validators_required))
    console.print(table)


@run_app.command("task")
def run_single_task(
    task_id: uuid.UUID = typer.Argument(..., help="Task identifier"),
    agent: str = typer.Option("static", help="Agent runner id"),
    model: str = typer.Option("demo", help="Model identifier"),
    repo: Path = typer.Option(Path("."), exists=True, file_okay=False, help="Repository root"),
    generation: str = typer.Option("baseline", help="Generation label"),
    command: list[str] | None = typer.Option(
        None,
        "--command",
        "-c",
        help="Shell command to run (repeat for multiple)",
    ),
    validator: list[str] | None = typer.Option(
        None,
        "--validator",
        help="Validator to request from runner (repeatable)",
    ),
) -> None:
    orchestrator = OrchestratorService()
    runner = build_runner(agent, commands=list(command or []), validators=list(validator or []))
    run_id = orchestrator.run_task(
        task_id=task_id,
        agent_id=agent,
        model_id=model,
        generation_label=generation,
        runner=runner,
        repo_path=repo,
    )
    console.print(f"Run complete: {run_id}", style="green")


@run_app.command("pack")
def run_task_pack(
    path: Path = typer.Argument(..., exists=True),
    agent: str = typer.Option("static"),
    model: str = typer.Option("demo"),
    repo: Path = typer.Option(Path("."), exists=True),
    generation: str = typer.Option("baseline"),
    command: list[str] | None = typer.Option(
        None,
        "--command",
        "-c",
        help="Shell command to run (repeat for multiple)",
    ),
    validator: list[str] | None = typer.Option(
        None,
        "--validator",
        help="Validator to request from runner (repeatable)",
    ),
) -> None:
    pack = load_task_pack(path)
    tasks = TaskService().import_task_pack(pack)
    orchestrator = OrchestratorService()
    runner = build_runner(agent, commands=list(command or []), validators=list(validator or []))
    for task in tasks:
        orchestrator.run_task(
            task_id=task.id,
            agent_id=agent,
            model_id=model,
            generation_label=generation,
            runner=runner,
            repo_path=repo,
        )
    console.print(f"Executed {len(tasks)} tasks", style="green")


@run_app.command("recent", help="Show latest runs with validator policy and auto-remediation summaries")
def run_recent(limit: int = typer.Option(10, min=1, max=100)) -> None:
    service = RunService()
    runs = service.recent_runs(limit=limit)
    if not runs:
        console.print("No runs recorded yet", style="yellow")
        return
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Run ID", overflow="fold", min_width=36)
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Model")
    table.add_column("Generation")
    table.add_column("Created")
    table.add_column("Policy", min_width=12)
    table.add_column("Auto-Remediation", min_width=18)
    table.add_column("Failure Summary", min_width=16)
    summaries: list[tuple[str, str, str, str]] = []
    for run in runs:
        run_metrics = run.metrics or {}
        policy_metrics = run_metrics.get("validator_policy")
        auto_metrics = run_metrics.get("auto_remediation")
        policy_summary = summarize_policy_status(policy_metrics)
        auto_summary = summarize_auto_remediation(auto_metrics)
        failure_summary = summarize_policy_failures(policy_metrics)
        table.add_row(
            str(run.id),
            run.status.value,
            str(run.task_id),
            run.model_id,
            run.generation_label,
            run.created_at.isoformat() if run.created_at else "--",
            policy_summary,
            auto_summary,
            failure_summary,
        )
        summaries.append((str(run.id), policy_summary, auto_summary, failure_summary))
    console.print(table)
    for run_id, policy_summary, auto_summary, failure_summary in summaries:
        console.print(f"Policy summary: {policy_summary}", style="dim")
        if failure_summary:
            console.print(f"Failure Summary: {failure_summary}", style="dim")
        else:
            console.print("Failure Summary: none", style="dim")
        console.print(f"Auto-Remediation: {auto_summary}", style="dim")


@run_app.command("detail")
def run_detail(
    run_id: uuid.UUID = typer.Argument(..., help="Run identifier"),
    output_json: bool = typer.Option(False, "--json", "-j", help="Emit JSON instead of tables"),
) -> None:
    """Show validator outcomes, policy evaluation, and auto-remediation summary for one run."""
    service = RunService()
    details = service.get_run_details(run_id)
    if not details:
        console.print(f"Run not found: {run_id}", style="red")
        raise typer.Exit(code=1)
    run, trace, validations = details
    policy = ValidatorPolicy.from_blob(run.validator_policy_snapshot)
    run_metrics = run.metrics or {}
    policy_metrics = run_metrics.get("validator_policy", {})
    auto_metrics = run_metrics.get("auto_remediation", {})
    if output_json:
        payload = {
            "run": run.model_dump(),
            "trace": trace.model_dump() if trace else None,
            "policy": policy.model_dump() if policy else None,
            "policy_metrics": policy_metrics,
            "auto_remediation": auto_metrics,
            "validations": [item.model_dump() for item in validations],
        }
        console.print(json.dumps(payload, indent=2, default=str))
        return
    console.print(f"Run {run.id} [{run.status.value}]", style="cyan")
    _render_policy(policy, policy_metrics)
    _render_auto_remediation(auto_metrics)
    _render_validations(validations)


@app.command()
def curate(generation: str = typer.Option("baseline"), holdout: bool = typer.Option(False)) -> None:
    tasks = TaskService().list_tasks(holdout=holdout)
    curator = CurationService()
    curated = 0
    for task in tasks:
        result = curator.curate_task(task.id, generation_label=generation)
        if result:
            curated += 1
    console.print(f"Curated {curated} tasks", style="green")


@app.command()
def eval(holdout: bool | None = typer.Option(None)) -> None:
    service = EvaluationService()
    counts = service.solved_counts(holdout=holdout)
    table = Table("Model", "Solved")
    for model_id, solved in counts.items():
        table.add_row(model_id, str(solved))
    console.print(table)


@app.command("eval-solvedk")
def eval_solved_at_k(
    k: int = typer.Option(3, min=1, help="Number of attempts per task"),
    holdout: bool | None = typer.Option(None),
) -> None:
    task_service = TaskService()
    tasks = task_service.list_tasks(holdout=holdout)
    if not tasks:
        console.print("No tasks available for solved@k", style="yellow")
        return
    service = EvaluationService()
    ratio = service.solved_at_k([task.id for task in tasks], k=k)
    solved = int(round(ratio * len(tasks)))
    console.print(
        f"Solved@{k}: {ratio * 100:.1f}% ({solved}/{len(tasks)})",
        style="green",
    )


@app.command()
def export(
    out: Path = typer.Option(Path(".idasdlc/datasets/curated.jsonl")),
    include_trace: bool = typer.Option(True, help="Include trace payloads"),
) -> None:
    exporter = ExportService()
    path = exporter.export_curated(out, include_trace=include_trace)
    console.print(f"Exported dataset to {path}", style="green")


@app.command("reward-card")
def reward_card(generation: str = typer.Argument(...)) -> None:
    service = RewardCardService()
    path = service.generate(generation)
    console.print(f"Reward card saved to {path}", style="green")


@app.command()
def retrieve(query: str = typer.Argument(...), limit: int = typer.Option(5)) -> None:
    service = TraceRetrievalService()
    results = service.search(query, limit=limit)
    table = Table("Task", "Score", "Prompt")
    for item in results:
        table.add_row(str(item.task_id), f"{item.score:.2f}", item.prompt[:80])
    console.print(table)


def build_runner(agent: str, *, commands: list[str], validators: list[str]):
    if agent == "static":
        return StaticPlanRunner()
    if agent == "command":
        if not commands:
            raise typer.BadParameter("--command must be provided for command runner")
        return CommandRunner(commands=commands, validators=validators)
    raise typer.BadParameter(f"Unknown agent '{agent}'")

def _render_policy(policy: ValidatorPolicy | None, metrics: dict[str, Any]) -> None:
    if not policy and not metrics:
        console.print("No validator policy recorded for this run", style="yellow")
        return
    summary_bits: list[str] = []
    if "passed" in metrics:
        summary_bits.append(f"passed={metrics['passed']}")
    if "passed_count" in metrics:
        summary_bits.append(f"passed_count={metrics['passed_count']}")
    if "minimum_pass" in metrics and metrics["minimum_pass"] is not None:
        summary_bits.append(f"minimum_pass={metrics['minimum_pass']}")
    if summary_bits:
        console.print("Policy evaluation: " + ", ".join(summary_bits))
    for label, key, style in (
        ("Required failures", "required_failures", "red"),
        ("Advisory failures", "advisory_failures", "yellow"),
        ("Missing validators", "missing", "magenta"),
    ):
        values = metrics.get(key) or []
        if values:
            console.print(f"{label}: {', '.join(values)}", style=style)
    if policy and policy.rules:
        table = Table("Validator", "Severity", "Must Pass", "Min Score")
        for rule in policy.rules:
            table.add_row(
                rule.name,
                rule.severity.value,
                "yes" if rule.requires_success() else "no",
                _format_optional(rule.min_score),
            )
        console.print(table)


def _render_auto_remediation(metrics: dict[str, Any]) -> None:
    if not metrics:
        console.print("Auto-remediation not attempted", style="yellow")
        return
    status_line: str
    style = "green"
    if metrics.get("applied"):
        status_line = "Auto-remediation applied"
    elif metrics.get("attempted"):
        status_line = "Auto-remediation attempted"
        style = "yellow"
    else:
        console.print("Auto-remediation not attempted", style="yellow")
        return
    console.print(status_line, style=style)
    retries = metrics.get("retries")
    if isinstance(retries, int) and retries > 0:
        console.print(f"Retries: {retries}", style="dim")
    validators = metrics.get("validators") or []
    if validators:
        console.print(f"Targeted validators: {', '.join(validators)}")
    fallbacks = metrics.get("fallbacks") or {}
    if fallbacks:
        pairs = [f"{name} -> {', '.join(names)}" for name, names in fallbacks.items() if names]
        if pairs:
            console.print("Fallbacks: " + "; ".join(pairs), style="dim")


def _render_validations(validations) -> None:
    if not validations:
        console.print("No validator executions recorded", style="yellow")
        return
    table = Table("Validator", "Passed", "Score")
    for item in validations:
        table.add_row(item.validator_name, "PASS" if item.passed else "FAIL", _format_optional(item.score))
    console.print(table)
    for item in validations:
        if item.logs:
            snippet = _truncate(item.logs)
            console.print(f"[{item.validator_name}] logs: {snippet}", style="dim")


def _format_optional(value) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _truncate(text: str, limit: int = 160) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3] + "..."
