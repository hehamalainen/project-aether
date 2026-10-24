"""
Aether CLI - Command line tool for agent interaction with Aether.

Commands:
- aether push: Submit a proposal
- aether status: Get repository status
- aether head: Get current head
- aether watch: Watch for events
- aether repos: Manage repositories
- aether snapshot: Manage snapshots
- aether git-import: Import from Git repository
- aether git-export: Export to Git repository
"""

import asyncio
from datetime import datetime, timedelta
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel

app = typer.Typer(
    name="aether",
    help="Aether CLI - Agent-native version control",
    no_args_is_help=True,
)

console = Console()

# Default server URL
DEFAULT_SERVER = "http://localhost:8100"
DEFAULT_RUNTIME_ARTIFACT_ROOT = Path.home() / ".aether" / "runtime_artifacts"


def get_client() -> httpx.Client:
    """Get HTTP client."""
    server_url = typer.get_app_dir("aether")
    return httpx.Client(base_url=DEFAULT_SERVER, timeout=30.0)


def get_async_client() -> httpx.AsyncClient:
    """Get async HTTP client."""
    return httpx.AsyncClient(base_url=DEFAULT_SERVER, timeout=30.0)


def _runtime_artifact_root() -> Path:
    return Path(os.getenv("AETHER_RUNTIME_ARTIFACT_ROOT", str(DEFAULT_RUNTIME_ARTIFACT_ROOT))).expanduser()


def _candidate_heartbeat_dirs() -> list[Path]:
    return [
        _runtime_artifact_root() / "phase1",
        Path.cwd() / ".pytest-artifacts" / "phase1",
    ]


def _find_latest_heartbeat_path() -> Optional[Path]:
    latest: Optional[Path] = None
    latest_mtime = -1.0
    for directory in _candidate_heartbeat_dirs():
        if not directory.exists():
            continue
        for candidate in directory.glob("*.heartbeat.log"):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest = candidate
                latest_mtime = mtime
    return latest


def _resolve_artifact_path(raw_path: Optional[str], *, cwd: Path) -> Optional[Path]:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (cwd / path).resolve()


def _parse_heartbeat_file(path: Path) -> dict[str, object]:
    metadata: dict[str, str] = {}
    last_elapsed_seconds: Optional[int] = None
    last_rc: Optional[str] = None
    elapsed_pattern = re.compile(r"^t\+(\d+)s .* rc=([A-Za-z0-9_-]+)$")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = elapsed_pattern.match(line)
        if match:
            last_elapsed_seconds = int(match.group(1))
            last_rc = match.group(2)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            metadata[key] = value

    now = datetime.now().astimezone()
    local_tz = now.tzinfo

    def parse_dt(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)

    start_dt = parse_dt(metadata.get("start"))
    end_dt = parse_dt(metadata.get("end"))
    run_seconds = int(float(metadata.get("run_seconds", "0") or 0))
    drain_seconds = int(float(metadata.get("drain_seconds", "0") or 0))
    heartbeat_seconds = int(float(metadata.get("heartbeat_seconds", "0") or 0))
    json_path = _resolve_artifact_path(metadata.get("json_path"), cwd=Path.cwd())
    log_path = path.with_suffix("").with_suffix(".log")
    age_seconds = max(0.0, now.timestamp() - path.stat().st_mtime)

    if end_dt is not None:
        state = "completed"
    elif last_rc == "None" and age_seconds <= max(heartbeat_seconds * 2 + 10, 60):
        state = "running"
    elif last_rc is not None:
        state = "stale"
    else:
        state = "unknown"

    expected_submit_end = start_dt + timedelta(seconds=run_seconds) if start_dt and run_seconds > 0 else None
    expected_finish = (
        start_dt + timedelta(seconds=run_seconds + drain_seconds)
        if start_dt and (run_seconds > 0 or drain_seconds > 0)
        else None
    )

    return {
        "heartbeat_path": str(path.resolve()),
        "log_path": str(log_path.resolve()),
        "json_path": str(json_path) if json_path else None,
        "json_exists": bool(json_path and json_path.exists()),
        "state": state,
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "run_seconds": run_seconds,
        "drain_seconds": drain_seconds,
        "elapsed_seconds": last_elapsed_seconds,
        "heartbeat_age_seconds": round(age_seconds, 1),
        "expected_submit_end": expected_submit_end.isoformat() if expected_submit_end else None,
        "expected_finish": expected_finish.isoformat() if expected_finish else None,
        "pid": metadata.get("pid"),
        "run_class": metadata.get("run_class"),
        "policy_allowed": metadata.get("policy_allowed"),
        "policy_reason": metadata.get("policy_reason"),
        "end_rc": metadata.get("end_rc"),
        "timed_out": metadata.get("timed_out"),
        "post_json_forced_stop": metadata.get("post_json_forced_stop"),
        "last_rc": last_rc,
    }


# ============================================================================
# Repository commands
# ============================================================================

@app.command()
def repos(
    action: str = typer.Argument("list", help="Action: list, create, delete"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Repository name"),
    repo_id: Optional[str] = typer.Option(None, "--repo", "-r", help="Repository ID"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Description"),
):
    """Manage repositories."""
    client = get_client()

    if action == "list":
        response = client.get("/v1/repos")
        response.raise_for_status()
        repos = response.json()

        table = Table(title="Aether Repositories")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Head Seq", justify="right")
        table.add_column("Created")

        for repo in repos:
            table.add_row(
                repo["id"],
                repo["name"],
                str(repo["head_sequence"]),
                repo["created_at"][:19],
            )

        console.print(table)

    elif action == "create":
        if not name:
            console.print("[red]Error: --name is required for create[/red]")
            raise typer.Exit(1)

        response = client.post(
            "/v1/repos",
            json={"name": name, "description": description},
        )
        response.raise_for_status()
        repo = response.json()

        console.print(f"[green]Repository created:[/green] {repo['id']}")
        console.print(f"  Name: {repo['name']}")

    elif action == "delete":
        if not repo_id:
            console.print("[red]Error: --repo is required for delete[/red]")
            raise typer.Exit(1)

        response = client.delete(f"/v1/repos/{repo_id}")
        response.raise_for_status()
        console.print(f"[yellow]Repository deleted:[/yellow] {repo_id}")


# ============================================================================
# Push command (submit proposal)
# ============================================================================

@app.command()
def push(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    diff_file: Optional[Path] = typer.Option(None, "--diff", "-d", help="Diff file path"),
    diff_stdin: bool = typer.Option(False, "--stdin", help="Read diff from stdin"),
    confidence: float = typer.Option(0.8, "--confidence", "-c", help="Confidence score (0-1)"),
    agent_id: str = typer.Option("cli-agent", "--agent", "-a", help="Agent ID"),
    task_id: Optional[str] = typer.Option(None, "--task", "-t", help="Task ID"),
    paths: Optional[str] = typer.Option(None, "--paths", "-p", help="Affected paths (comma-separated)"),
    model_id: Optional[str] = typer.Option(None, "--model", "-m", help="Model ID"),
    rationale: Optional[str] = typer.Option(None, "--rationale", help="Rationale for change"),
    idempotency_key: Optional[str] = typer.Option(None, "--key", "-k", help="Idempotency key"),
):
    """Submit a proposal to Aether."""
    # Get diff content
    if diff_stdin:
        diff_content = sys.stdin.read()
    elif diff_file:
        diff_content = diff_file.read_text()
    else:
        console.print("[red]Error: Provide diff via --diff or --stdin[/red]")
        raise typer.Exit(1)

    # Parse affected paths
    if paths:
        affected_paths = [p.strip() for p in paths.split(",")]
    else:
        # Try to extract from diff
        affected_paths = []
        for line in diff_content.split("\n"):
            if line.startswith("diff --git"):
                parts = line.split()
                if len(parts) >= 4:
                    path = parts[3].lstrip("b/")
                    affected_paths.append(path)

    if not affected_paths:
        console.print("[red]Error: Could not determine affected paths. Use --paths[/red]")
        raise typer.Exit(1)

    # Build proposal
    proposal = {
        "agent_id": agent_id,
        "task_id": task_id,
        "change_type": "unified_diff",
        "change_content": diff_content,
        "affected_paths": affected_paths,
        "confidence_score": confidence,
        "model_id": model_id,
        "rationale": rationale,
        "idempotency_key": idempotency_key,
        "requires_tests": True,
    }

    client = get_client()
    response = client.post(f"/v1/repos/{repo_id}/proposals", json=proposal)

    if response.status_code != 200:
        console.print(f"[red]Error: {response.text}[/red]")
        raise typer.Exit(1)

    result = response.json()

    console.print(f"[green]Proposal submitted:[/green] {result['proposal_id']}")
    console.print(f"  Status: {result['status']}")
    if result.get("conflict_set_id"):
        console.print(f"  [yellow]Conflict detected:[/yellow] {result['conflict_set_id']}")
    console.print(f"  Message: {result['message']}")


# ============================================================================
# Status command
# ============================================================================

@app.command()
def status(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    show_proposals: bool = typer.Option(False, "--proposals", "-p", help="Show pending proposals"),
    show_conflicts: bool = typer.Option(False, "--conflicts", "-c", help="Show conflicts"),
):
    """Get repository status."""
    client = get_client()

    # Get repo info
    response = client.get(f"/v1/repos/{repo_id}")
    if response.status_code == 404:
        console.print(f"[red]Repository not found: {repo_id}[/red]")
        raise typer.Exit(1)
    response.raise_for_status()
    repo = response.json()

    # Get head info
    head_response = client.get(f"/v1/repos/{repo_id}/head")
    head_response.raise_for_status()
    head = head_response.json()

    console.print(Panel(f"[bold]Repository: {repo['name']}[/bold]"))
    console.print(f"  ID: {repo['id']}")
    console.print(f"  Head Sequence: {head['sequence']}")
    console.print(f"  Content Hash: {head['content_hash'][:16]}..." if head['content_hash'] else "  Content Hash: (empty)")

    if show_proposals:
        console.print("\n[bold]Pending Proposals:[/bold]")
        proposals_response = client.get(f"/v1/repos/{repo_id}/proposals", params={"status": "PENDING"})
        proposals_response.raise_for_status()
        proposals = proposals_response.json()

        if proposals["proposals"]:
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Agent")
            table.add_column("Confidence", justify="right")
            table.add_column("Submitted")

            for p in proposals["proposals"]:
                table.add_row(
                    p["id"][:20],
                    p["agent_id"],
                    f"{p['confidence_score']:.2f}",
                    p["submitted_at"][:19],
                )
            console.print(table)
        else:
            console.print("  No pending proposals")

    if show_conflicts:
        console.print("\n[bold]Unresolved Conflicts:[/bold]")
        conflicts_response = client.get(f"/v1/repos/{repo_id}/conflicts", params={"resolved": False})
        conflicts_response.raise_for_status()
        conflicts = conflicts_response.json()

        if conflicts["conflict_sets"]:
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Path")
            table.add_column("Proposals", justify="right")

            for cs in conflicts["conflict_sets"]:
                proposal_ids = json.loads(cs["proposal_ids"])
                table.add_row(
                    cs["id"][:20],
                    cs["path"],
                    str(len(proposal_ids)),
                )
            console.print(table)
        else:
            console.print("  No unresolved conflicts")


# ============================================================================
# Head command
# ============================================================================

@app.command()
def head(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    output_json: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get current canonical head."""
    client = get_client()

    response = client.get(f"/v1/repos/{repo_id}/head")
    if response.status_code == 404:
        console.print(f"[red]Repository not found: {repo_id}[/red]")
        raise typer.Exit(1)
    response.raise_for_status()
    head = response.json()

    if output_json:
        console.print(json.dumps(head, indent=2))
    else:
        console.print(f"[bold]Canonical Head[/bold]")
        console.print(f"  Head ID: {head['head_id'] or '(none)'}")
        console.print(f"  Sequence: {head['sequence']}")
        console.print(f"  Snapshot: {head['snapshot_id'] or '(none)'}")
        console.print(f"  Hash: {head['content_hash'][:16] if head['content_hash'] else '(empty)'}...")


# ============================================================================
# Watch command
# ============================================================================

@app.command()
def watch(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    events: Optional[str] = typer.Option(None, "--events", "-e", help="Event types to watch (comma-separated)"),
):
    """Watch for realtime events."""
    import websockets

    async def _watch():
        event_filter = f"?events={events}" if events else ""
        uri = f"ws://localhost:8100/v1/repos/{repo_id}/events{event_filter}"

        console.print(f"[cyan]Connecting to {uri}...[/cyan]")

        try:
            async with websockets.connect(uri) as websocket:
                console.print("[green]Connected! Watching for events...[/green]")
                console.print("Press Ctrl+C to stop\n")

                async for message in websocket:
                    data = json.loads(message)

                    if data.get("type") == "connected":
                        console.print(f"[dim]Connection ID: {data['connection_id']}[/dim]")
                    elif data.get("type") == "event":
                        event_type = data.get("event_type", "unknown")
                        timestamp = data.get("timestamp", "")[:19]
                        payload = data.get("payload", {})

                        # Color-code by event type
                        if "promoted" in event_type.lower():
                            style = "green"
                        elif "failed" in event_type.lower() or "rejected" in event_type.lower():
                            style = "red"
                        elif "conflict" in event_type.lower():
                            style = "yellow"
                        else:
                            style = "cyan"

                        console.print(f"[{style}]{event_type}[/{style}] [{timestamp}]")
                        for key, value in payload.items():
                            console.print(f"  {key}: {value}")
                        console.print()

        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped watching[/yellow]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    asyncio.run(_watch())


# ============================================================================
# Snapshot command
# ============================================================================

@app.command()
def snapshot(
    action: str = typer.Argument("list", help="Action: list, create, download"),
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    snapshot_id: Optional[str] = typer.Option(None, "--snapshot", "-s", help="Snapshot ID"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output path for download"),
):
    """Manage snapshots."""
    client = get_client()

    if action == "list":
        response = client.get(f"/v1/repos/{repo_id}/snapshots")
        response.raise_for_status()
        data = response.json()

        table = Table(title="Snapshots")
        table.add_column("ID", style="cyan")
        table.add_column("Hash")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Created")

        for s in data["snapshots"]:
            size_kb = s["total_bytes"] / 1024
            table.add_row(
                s["id"][:20],
                s["content_hash"][:12],
                str(s["file_count"]),
                f"{size_kb:.1f} KB",
                s["created_at"][:19],
            )

        console.print(table)

    elif action == "create":
        response = client.post(
            f"/v1/repos/{repo_id}/snapshots",
            json={"selection_type": "head"},
        )
        response.raise_for_status()
        data = response.json()

        console.print(f"[green]Snapshot created:[/green] {data['snapshot_id']}")
        console.print(f"  Hash: {data['content_hash']}")
        console.print(f"  Files: {data['file_count']}")

    elif action == "download":
        if not snapshot_id:
            console.print("[red]Error: --snapshot is required for download[/red]")
            raise typer.Exit(1)

        response = client.get(f"/v1/repos/{repo_id}/snapshots/{snapshot_id}/download")
        response.raise_for_status()

        output_path = output or Path(f"{snapshot_id}.tar.gz")
        output_path.write_bytes(response.content)
        console.print(f"[green]Downloaded to:[/green] {output_path}")


# ============================================================================
# Promote command
# ============================================================================

@app.command()
def promote(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Repository ID"),
    proposal_ids: str = typer.Option(..., "--proposals", "-p", help="Proposal IDs (comma-separated)"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Deprecated compatibility flag (required gates remain fail-closed)",
    ),
):
    """Request promotion of proposals."""
    client = get_client()

    ids = [p.strip() for p in proposal_ids.split(",")]

    response = client.post(
        f"/v1/repos/{repo_id}/promotions",
        json={"proposal_ids": ids, "force": force},
    )

    if response.status_code != 200:
        console.print(f"[red]Error: {response.text}[/red]")
        raise typer.Exit(1)

    result = response.json()

    if result["success"]:
        console.print(f"[green]Promotion successful![/green]")
        console.print(f"  Promotion ID: {result['promotion_id']}")
        console.print(f"  New Head: {result['new_head_id']}")
        console.print(f"  Sequence: {result['new_head_sequence']}")
        console.print(f"  Score: {result['final_score']:.3f}")
    else:
        console.print(f"[red]Promotion failed[/red]")
        console.print(f"  Message: {result['message']}")
        console.print(f"  Evidence: {json.dumps(result['evidence'], indent=2)}")


# ============================================================================
# Git Import command
# ============================================================================

@app.command("git-import")
def git_import(
    source: str = typer.Argument(..., help="Git repository path or URL"),
    name: str = typer.Option(..., "--name", "-n", help="Name for the new Aether repository"),
    branch: str = typer.Option("main", "--branch", "-b", help="Branch to import"),
    include_history: bool = typer.Option(False, "--history", "-H", help="Import commit history"),
    max_commits: int = typer.Option(100, "--max-commits", help="Max commits to import with --history"),
):
    """Import a Git repository into Aether.
    
    Examples:
        aether git-import /path/to/repo --name my-project
        aether git-import https://github.com/user/repo.git --name my-project --history
    """
    from pathlib import Path as PyPath
    import sys
    
    # Add packages to path for local imports
    project_root = PyPath(__file__).parent.parent.parent
    sys.path.insert(0, str(project_root))
    
    try:
        from packages.aether.git.importer import GitImporter
        from packages.aether.crdt.document_store import DocumentStore
    except ImportError as e:
        console.print(f"[red]Import error: {e}[/red]")
        console.print("[yellow]Make sure you're running from the Aether project root[/yellow]")
        raise typer.Exit(1)
    
    storage_path = PyPath.home() / ".aether" / "git-imports"
    importer = GitImporter(storage_path)
    
    console.print(f"[cyan]Importing from:[/cyan] {source}")
    console.print(f"[cyan]Target repo:[/cyan] {name}")
    
    try:
        # Determine if URL or path
        if source.startswith("http://") or source.startswith("https://") or source.startswith("git@"):
            result = importer.import_from_url(
                source,
                repo_id=name,
                branch=branch,
                include_history=include_history,
            )
        else:
            result = importer.import_from_path(
                PyPath(source),
                repo_id=name,
                include_history=include_history,
                max_commits=max_commits,
            )
        
        console.print(f"\n[green]Import successful![/green]")
        console.print(f"  Files imported: {result.imported_files}")
        console.print(f"  Files skipped: {result.skipped_files}")
        console.print(f"  Branch: {result.branch}")
        console.print(f"  Head SHA: {result.head_sha[:12]}")
        
        if result.commit_history:
            console.print(f"  Commits imported: {len(result.commit_history)}")
        
        # Save the document store state
        export_path = storage_path / "states" / name
        export_path.mkdir(parents=True, exist_ok=True)
        
        state_file = export_path / "document_store.json"
        state_data = result.document_store.to_dict()
        
        import json as json_lib
        state_file.write_text(json_lib.dumps(state_data, indent=2))
        console.print(f"\n[dim]State saved to: {state_file}[/dim]")
        
        console.print(f"\n[yellow]Next steps:[/yellow]")
        console.print(f"  1. Start Aether server: python -m apps.aether_server.main")
        console.print(f"  2. Create repo via API: aether repos create --name {name}")
        console.print(f"  3. Initialize from saved state")
        
    except Exception as e:
        console.print(f"[red]Import failed: {e}[/red]")
        raise typer.Exit(1)


# ============================================================================
# Git Export command
# ============================================================================

@app.command("git-export")
def git_export(
    repo_id: str = typer.Option(..., "--repo", "-r", help="Aether repository ID"),
    output: Path = typer.Option(..., "--output", "-o", help="Output git repository path"),
    message: str = typer.Option("Export from Aether", "--message", "-m", help="Commit message"),
    bundle: bool = typer.Option(False, "--bundle", help="Create git bundle instead"),
):
    """Export Aether repository to Git.
    
    Examples:
        aether git-export --repo repo_123 --output /path/to/git-repo
        aether git-export --repo repo_123 --output my-export --bundle
    """
    from pathlib import Path as PyPath
    import sys
    
    project_root = PyPath(__file__).parent.parent.parent
    sys.path.insert(0, str(project_root))
    
    try:
        from packages.aether.git.exporter import GitExporter
        from packages.aether.crdt.document_store import (
            DocumentStore,
            resolve_contained_path,
            safe_extract_tarball,
        )
    except ImportError as e:
        console.print(f"[red]Import error: {e}[/red]")
        raise typer.Exit(1)
    
    # Get document store from server
    client = get_client()
    
    console.print(f"[cyan]Fetching repo:[/cyan] {repo_id}")
    
    try:
        # Get head snapshot
        head_response = client.get(f"/v1/repos/{repo_id}/head")
        if head_response.status_code == 404:
            console.print(f"[red]Repository not found: {repo_id}[/red]")
            raise typer.Exit(1)
        head_response.raise_for_status()
        head = head_response.json()
        
        if not head.get("snapshot_id"):
            console.print("[red]No snapshot available. Create one first with: aether snapshot create[/red]")
            raise typer.Exit(1)
        
        # Download snapshot
        console.print(f"[cyan]Downloading snapshot:[/cyan] {head['snapshot_id']}")
        
        snapshot_response = client.get(f"/v1/repos/{repo_id}/snapshots/{head['snapshot_id']}/download")
        snapshot_response.raise_for_status()
        
        # Extract to temp and export
        import tempfile
        import tarfile
        import io
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Extract tarball safely
            with tarfile.open(fileobj=io.BytesIO(snapshot_response.content), mode="r:gz") as tarball:
                safe_extract_tarball(tarball, temp_dir)
            
            # Create document store from extracted files
            doc_store = DocumentStore(replica_id=f"export_{repo_id}")
            
            temp_path = PyPath(temp_dir)
            for file_path in temp_path.rglob("*"):
                if not file_path.is_symlink() and file_path.is_file():
                    rel_path = file_path.relative_to(temp_path).as_posix()
                    resolve_contained_path(temp_path, rel_path)
                    try:
                        content = file_path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue  # Skip binary files
                    doc_store.set_file(rel_path, content)
            
            # Export
            storage_path = PyPath.home() / ".aether" / "git-exports"
            exporter = GitExporter(storage_path)
            
            if bundle:
                bundle_path = exporter.create_bundle(doc_store, output.stem)
                console.print(f"\n[green]Bundle created:[/green] {bundle_path}")
            else:
                result = exporter.export_snapshot_to_git(
                    doc_store,
                    output,
                    commit_message=message,
                )
                console.print(f"\n[green]Export successful![/green]")
                console.print(f"  Path: {result.output_path}")
                console.print(f"  Commit: {result.commit_sha[:12] if result.commit_sha else 'No changes'}")
                console.print(f"  Files: {result.files_written}")
    
    except Exception as e:
        console.print(f"[red]Export failed: {e}[/red]")
        raise typer.Exit(1)


# ============================================================================
# Swarm harness command
# ============================================================================


@app.command("swarm-run-status")
def swarm_run_status(
    heartbeat: Optional[Path] = typer.Option(
        None,
        "--heartbeat",
        help="Explicit heartbeat log to inspect (defaults to latest known heartbeat)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Report the latest swarm-safe run status from heartbeat artifacts."""
    heartbeat_path = heartbeat.expanduser() if heartbeat else _find_latest_heartbeat_path()
    if heartbeat_path is None or not heartbeat_path.exists():
        console.print("[red]No heartbeat log found.[/red]")
        raise typer.Exit(1)

    status = _parse_heartbeat_file(heartbeat_path)

    if status["json_exists"] and status["json_path"]:
        try:
            with open(status["json_path"], "r") as f:
                report_data = json.load(f)
            metrics = report_data.get("metrics", {})
            status_counts = metrics.get("status_counts", {})
            status["proposals_promoted"] = metrics.get("proposals_promoted", 0)
            status["proposals_rejected"] = status_counts.get("REJECTED", 0)
            status["proposals_superseded"] = status_counts.get("SUPERSEDED", 0)
            status["avg_batch_size_k"] = metrics.get("avg_batch_size_k", 0.0)
            status["batch_gate_failure_rate"] = metrics.get("batch_gate_failure_rate", 0.0)
            status["mu_eff"] = metrics.get("mu_eff", 0.0)
        except Exception:
            pass

    if as_json:
        typer.echo(json.dumps(status, indent=2, sort_keys=True))
        return

    console.print("[bold]Swarm Run Status[/bold]")
    console.print(f"  Heartbeat: {status['heartbeat_path']}")
    console.print(f"  Log: {status['log_path']}")
    console.print(f"  JSON: {status['json_path'] or 'n/a'}")
    console.print(f"  State: {status['state']}")
    console.print(f"  Run class: {status['run_class'] or 'unknown'}")
    console.print(f"  Started: {status['start'] or 'unknown'}")
    console.print(f"  Elapsed seconds: {status['elapsed_seconds'] if status['elapsed_seconds'] is not None else 'unknown'}")
    console.print(f"  Heartbeat age seconds: {status['heartbeat_age_seconds']}")
    console.print(f"  Expected submit end: {status['expected_submit_end'] or 'unknown'}")
    console.print(f"  Expected finish: {status['expected_finish'] or 'unknown'}")
    console.print(f"  JSON exists: {status['json_exists']}")
    if "proposals_promoted" in status:
        console.print("  Report Metrics:")
        console.print(f"    Proposals Promoted: {status['proposals_promoted']}")
        console.print(f"    Proposals Rejected: {status['proposals_rejected']}")
        console.print(f"    Proposals Superseded: {status['proposals_superseded']}")
        console.print(f"    Average Batch Size (K): {status['avg_batch_size_k']:.2f}")
        console.print(f"    Batch Gate Failure Rate: {status['batch_gate_failure_rate']:.2%}")
        console.print(f"    Effective Throughput (mu_eff): {status['mu_eff']:.2f} proposals/sec")
    console.print(f"  Policy: allowed={status['policy_allowed']} reason={status['policy_reason']}")
    if status["end_rc"] is not None:
        console.print(f"  Exit rc: {status['end_rc']}")
        console.print(f"  Timed out: {status['timed_out']}")
        console.print(f"  Post-JSON forced stop: {status['post_json_forced_stop']}")

@app.command("swarm-harness")
def swarm_harness(
    agents: int = typer.Option(1000, "--agents", help="Number of concurrent agents"),
    files: int = typer.Option(500, "--files", help="Number of base files"),
    run_seconds: float = typer.Option(60.0, "--run-seconds", help="Sustained submission window (seconds)"),
    drain_seconds: float = typer.Option(20.0, "--drain-seconds", help="Promotion drain window (seconds)"),
    contention: str = typer.Option("low", "--contention", help="Contention mode: low, normal, high"),
    min_interval_ms: int = typer.Option(400, "--min-interval-ms", help="Per-agent min submit interval (ms)"),
    max_interval_ms: int = typer.Option(1600, "--max-interval-ms", help="Per-agent max submit interval (ms)"),
    inflight: int = typer.Option(300, "--inflight", help="Max in-flight submissions"),
    promotion_batch: int = typer.Option(30, "--promotion-batch", help="Promotion engine batch size"),
    promotion_non_conflicting_batches: int = typer.Option(
        4,
        "--promotion-non-conflicting-batches",
        help="Maximum non-conflicting batches processed per promotion iteration",
    ),
    promotion_max_conflict_sets: int = typer.Option(
        1,
        "--promotion-max-conflict-sets",
        help="Maximum conflict sets processed per promotion iteration",
    ),
    gate_execution_mode: str = typer.Option(
        "parallel",
        "--gate-execution-mode",
        help="Gate execution mode: sequential, fast_fail, parallel",
    ),
    gate_max_parallel: int = typer.Option(
        4,
        "--gate-max-parallel",
        help="Max parallel gate commands per promotion snapshot",
    ),
    promotion_interval_ms: int = typer.Option(40, "--promotion-interval-ms", help="Promotion loop interval (ms)"),
    promotion_iteration_timeout_seconds: float = typer.Option(
        30.0,
        "--promotion-iteration-timeout-seconds",
        help="Max duration for one promotion iteration before skipping",
    ),
    materialization_batch_size: int = typer.Option(
        500,
        "--materialization-batch-size",
        help="Ingest materialization batch size per worker iteration",
    ),
    materialization_interval_ms: int = typer.Option(
        20,
        "--materialization-interval-ms",
        help="Ingest materialization loop interval (ms)",
    ),
    monitor_interval_ms: int = typer.Option(500, "--monitor-interval-ms", help="Backlog monitor interval (ms)"),
    adaptive_intake_throttle: bool = typer.Option(
        True,
        "--adaptive-intake-throttle/--no-adaptive-intake-throttle",
        help="Throttle submit loops when backlog crosses adaptive watermarks",
    ),
    intake_backlog_high_watermark: Optional[int] = typer.Option(
        None,
        "--intake-backlog-high-watermark",
        help="Backlog watermark that enables submit throttling (default: 0.8 * max_backlog_end)",
    ),
    intake_backlog_low_watermark: Optional[int] = typer.Option(
        None,
        "--intake-backlog-low-watermark",
        help="Backlog watermark that disables throttling (default: 0.9 * high watermark)",
    ),
    intake_throttle_check_interval_ms: int = typer.Option(
        120,
        "--intake-throttle-check-interval-ms",
        help="Throttle loop check/sleep interval while backlog is above watermark",
    ),
    physics_window_seconds: int = typer.Option(
        60,
        "--physics-window-seconds",
        help="Queue-physics telemetry window for final lambda/mu snapshot",
    ),
    submit_in_worker: bool = typer.Option(
        False,
        "--submit-in-worker/--submit-inline",
        help="Run proposal intake on a dedicated worker thread pool",
    ),
    target_mode: str = typer.Option(
        "direct",
        "--target-mode",
        help="Target mode: direct, testclient, external",
    ),
    external_api_url: Optional[str] = typer.Option(
        None,
        "--external-api-url",
        help="URL of external API server (e.g. http://localhost:8100)",
    ),
    traffic_mix: str = typer.Option(
        "full_file",
        "--traffic-mix",
        help="Traffic mix: full_file, unified_diff, mixed",
    ),
    submit_worker_threads: int = typer.Option(
        4,
        "--submit-worker-threads",
        help="Worker thread count for threaded proposal intake",
    ),
    defer_conflict_detection: bool = typer.Option(
        True,
        "--defer-conflict-detection/--inline-conflict-detection",
        help="Queue conflict detection off the submit path (INGESTED -> PENDING)",
    ),
    min_submit_throughput: float = typer.Option(40.0, "--min-submit-throughput", help="Min submit throughput SLO"),
    min_promotion_throughput: float = typer.Option(0.2, "--min-promotion-throughput", help="Min promotion throughput SLO"),
    min_mu_eff: float = typer.Option(33.3, "--min-mu-eff", help="Min mu_eff throughput SLO"),
    max_submit_p95_ms: float = typer.Option(140.0, "--max-submit-p95-ms", help="Max p95 submit latency SLO"),
    max_conflict_rate: float = typer.Option(0.35, "--max-conflict-rate", help="Max conflict-rate SLO"),
    max_backlog_end: int = typer.Option(2000, "--max-backlog-end", help="Max pending+evaluating at end"),
    max_submit_failure_rate: float = typer.Option(0.02, "--max-submit-failure-rate", help="Max submit failure-rate SLO"),
    max_window_rho: float = typer.Option(
        1.0,
        "--max-window-rho",
        help="Max allowed rho in final queue-physics window (lambda/mu)",
    ),
    max_fallback_rate: Optional[float] = typer.Option(
        None,
        "--max-fallback-rate",
        help="Optional max semantic fallback rate in final physics window",
    ),
    seed: int = typer.Option(2026, "--seed", help="Random seed"),
    output_json: Optional[Path] = typer.Option(None, "--output-json", help="Write full report JSON"),
    quiet: bool = typer.Option(True, "--quiet/--verbose", help="Suppress per-event logs during run"),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="Exit with code 2 when SLO checks fail"),
):
    """Run sustained swarm flow harness with SLO pass/fail checks."""
    if quiet:
        import logging

        logging.disable(logging.CRITICAL)

    from packages.aether.services.swarm_harness import (
        ContinuousHarnessConfig,
        ContinuousHarnessThresholds,
        run_continuous_swarm_harness_sync,
    )
    import contextlib
    import io

    config = ContinuousHarnessConfig(
        num_agents=agents,
        num_files=files,
        run_seconds=run_seconds,
        drain_seconds=drain_seconds,
        contention_mode=contention,
        agent_interval_min_ms=min_interval_ms,
        agent_interval_max_ms=max_interval_ms,
        max_inflight_submissions=inflight,
        promotion_batch_size=promotion_batch,
        promotion_non_conflicting_batches=promotion_non_conflicting_batches,
        promotion_max_conflict_sets=promotion_max_conflict_sets,
        gate_execution_mode=gate_execution_mode,
        gate_max_parallel=gate_max_parallel,
        promotion_interval_ms=promotion_interval_ms,
        promotion_iteration_timeout_seconds=promotion_iteration_timeout_seconds,
        materialization_batch_size=materialization_batch_size,
        materialization_interval_ms=materialization_interval_ms,
        monitor_interval_ms=monitor_interval_ms,
        submit_in_worker=submit_in_worker,
        target_mode=target_mode,
        external_api_url=external_api_url,
        traffic_mix=traffic_mix,
        submit_worker_threads=submit_worker_threads,
        defer_conflict_detection=defer_conflict_detection,
        adaptive_intake_throttle=adaptive_intake_throttle,
        intake_backlog_high_watermark=intake_backlog_high_watermark,
        intake_backlog_low_watermark=intake_backlog_low_watermark,
        intake_throttle_check_interval_ms=intake_throttle_check_interval_ms,
        physics_window_seconds=physics_window_seconds,
    )
    from packages.aether.models.entities import ProposalStatus

    thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=min_submit_throughput,
        min_promotion_throughput=min_promotion_throughput,
        min_mu_eff=min_mu_eff,
        max_submit_p95_ms=max_submit_p95_ms,
        max_conflict_rate=max_conflict_rate,
        max_backlog_end=max_backlog_end,
        max_submit_failure_rate=max_submit_failure_rate,
        max_window_rho=max_window_rho,
        max_fallback_rate=max_fallback_rate,
    )
    shutdown_event = threading.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def _handle_signal(signum: int, _frame: object) -> None:
        shutdown_event.set()
        if not quiet:
            console.print(f"[yellow]Received signal {signum}; draining and writing report...[/yellow]")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        db_path = None
        storage_path = None
        if target_mode == "external":
            db_path = Path.home() / ".aether" / "aether.db"
            storage_path = Path.home() / ".aether" / "aether_storage"

        if quiet:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                report = run_continuous_swarm_harness_sync(
                    config=config,
                    thresholds=thresholds,
                    seed=seed,
                    shutdown_event=shutdown_event,
                    db_path=db_path,
                    storage_path=storage_path,
                )
        else:
            report = run_continuous_swarm_harness_sync(
                config=config,
                thresholds=thresholds,
                seed=seed,
                shutdown_event=shutdown_event,
                db_path=db_path,
                storage_path=storage_path,
            )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)

    data = report.to_dict()
    metrics = data["metrics"]

    console.print("[bold]Continuous Swarm Harness[/bold]")
    console.print(f"  Passed: {'yes' if report.passed else 'no'}")
    console.print(f"  Agents: {agents}")
    console.print(f"  Submit Throughput: {metrics['submit_throughput']:.2f} proposals/sec")
    console.print(
        f"  Submit Throughput (total phase): {metrics.get('submit_throughput_total_phase', 0.0):.2f} proposals/sec"
    )
    console.print(f"  Promotion Throughput: {metrics['promotion_throughput']:.2f} proposals/sec")
    console.print(f"  Completion Throughput: {metrics.get('completion_throughput', 0.0):.2f} proposals/sec")
    console.print(
        f"  Completion Throughput (submit phase): {metrics.get('completion_throughput_submit_phase', 0.0):.2f} proposals/sec"
    )
    console.print(f"  Config Fingerprint: {data.get('config_fingerprint', '')[:12]}")
    commit_short = data.get("git_commit_short", "unknown")
    dirty = data.get("git_dirty")
    dirty_label = "dirty" if dirty is True else "clean" if dirty is False else "unknown"
    console.print(f"  Git Commit: {commit_short} ({dirty_label})")
    console.print(f"  Interrupted: {'yes' if data.get('interrupted') else 'no'}")
    ratio_total = metrics.get("submit_to_promotion_ratio_total_phase", 0.0)
    if isinstance(ratio_total, (float, int)):
        console.print(f"  Submit/Promote Ratio (total): {float(ratio_total):.2f}x")
    ratio_completion = metrics.get("submit_to_completion_ratio_total_phase", 0.0)
    if isinstance(ratio_completion, (float, int)):
        console.print(f"  Submit/Complete Ratio (total): {float(ratio_completion):.2f}x")
    lambda_window = metrics.get("lambda_tps_window", 0.0)
    mu_window = metrics.get("mu_tps_window", 0.0)
    rho_window = metrics.get("rho_window")
    console.print(
        f"  Physics Window: lambda={float(lambda_window):.2f}/s mu={float(mu_window):.2f}/s "
        f"rho={float(rho_window):.2f}" if rho_window is not None else
        f"  Physics Window: lambda={float(lambda_window):.2f}/s mu={float(mu_window):.2f}/s rho=n/a"
    )
    console.print(
        f"  Physics Events: lambda_events={int(metrics.get('lambda_events_window', 0))} "
        f"mu_events={int(metrics.get('mu_events_window', 0))} "
        f"rho_sample_ok={'yes' if metrics.get('rho_sample_ok') else 'no'} "
        f"({metrics.get('rho_sample_note', 'unknown')})"
    )
    console.print(
        "  Semantic Fallback Window: "
        f"rate={float(metrics.get('fallback_rate_window', 0.0)):.3f} "
        f"hits={int(metrics.get('semantic_fallback_hits_window', 0))}"
    )
    console.print(f"  Submit P95: {metrics['p95_submit_latency_ms']:.2f} ms")
    console.print(
        f"  Enqueue->Ack P95: {metrics.get('p95_enqueue_to_ack_latency_ms', 0.0):.2f} ms"
    )
    console.print(
        f"  Ack->Promote P95: {metrics.get('p95_ack_to_promote_latency_ms', 0.0):.2f} ms"
    )
    console.print(f"  Conflict Rate: {metrics['conflict_rate']:.3f}")
    console.print(f"  Backlog End: {metrics['backlog_end']}")
    if metrics.get("submit_throttled", 0):
        console.print(
            "  Submit Throttled (429): "
            f"{metrics.get('submit_throttled', 0)} "
            f"({metrics.get('submit_throttled_rate', 0.0):.2%} of attempts)"
        )
    console.print(
        f"  Intake Throttle: high={metrics.get('intake_high_watermark', 0)} "
        f"low={metrics.get('intake_low_watermark', 0)} wait={metrics.get('throttle_wait_seconds', 0.0):.1f}s"
    )
    console.print(
        "  Intake Admission: "
        f"avg_rate={metrics.get('intake_admission_rate_avg', 1.0):.3f} "
        f"deferred={int(metrics.get('intake_deferred_submissions', 0))} "
        f"backoff_events={int(metrics.get('intake_backoff_events', 0))} "
        f"queue_peak={int(metrics.get('submit_queue_peak', 0))}"
    )
    status_counts = metrics.get("status_counts", {})
    console.print(f"  Promoted Proposals: {metrics['proposals_promoted']}")
    console.print(f"  Rejected Proposals: {status_counts.get('REJECTED', 0)}")
    console.print(f"  Superseded Proposals: {status_counts.get('SUPERSEDED', 0)}")
    console.print(f"  Promotion Records: {metrics['promotions_created']}")
    console.print(f"  Average Batch Size (K): {metrics.get('avg_batch_size_k', 0.0):.2f}")
    console.print(f"  Batch Gate Failure Rate: {metrics.get('batch_gate_failure_rate', 0.0):.2%}")
    console.print(f"  Effective Throughput (mu_eff): {metrics.get('mu_eff', 0.0):.2f} proposals/sec")
    if metrics.get("create_snapshot_quarantines", 0) or metrics.get("materialize_gate_workspace_quarantines", 0):
        console.print(
            "  Quarantines: "
            f"create_snapshot={int(metrics.get('create_snapshot_quarantines', 0))} "
            f"materialize_gate_workspace={int(metrics.get('materialize_gate_workspace_quarantines', 0))}"
        )
    phase_avg = metrics.get("promotion_phase_avg_ms", {})
    if phase_avg:
        console.print(
            "  Promotion Phases Avg (ms): "
            f"select={phase_avg.get('select_ms', 0.0):.1f}, "
            f"evaluate={phase_avg.get('evaluate_ms', 0.0):.1f}, "
            f"snapshot={phase_avg.get('snapshot_ms', 0.0):.1f}, "
            f"gates={phase_avg.get('gates_ms', 0.0):.1f}, "
            f"commit={phase_avg.get('commit_ms', 0.0):.1f}"
        )
        if any(
            key in phase_avg
            for key in (
                "clone_ms",
                "snapshot_hash_ms",
                "snapshot_materialize_ms",
                "snapshot_db_commit_ms",
                "snapshot_publish_ms",
            )
        ):
            console.print(
                "  Promotion Internals Avg (ms): "
                f"clone={phase_avg.get('clone_ms', 0.0):.1f}, "
                f"snapshot_hash={phase_avg.get('snapshot_hash_ms', 0.0):.1f}, "
                f"snapshot_materialize={phase_avg.get('snapshot_materialize_ms', 0.0):.1f}, "
                f"snapshot_db_commit={phase_avg.get('snapshot_db_commit_ms', 0.0):.1f}, "
                f"snapshot_publish={phase_avg.get('snapshot_publish_ms', 0.0):.1f}"
            )

    check_table = Table(title="SLO Checks")
    check_table.add_column("Check", style="cyan")
    check_table.add_column("Result", style="green")
    for check, passed in data["checks"].items():
        check_table.add_row(check, "PASS" if passed else "FAIL")
    console.print(check_table)

    if output_json:
        output_json.write_text(json.dumps(data, indent=2, sort_keys=True))
        console.print(f"[dim]Wrote report: {output_json}[/dim]")

    if strict and not report.passed:
        raise typer.Exit(2)


@app.command("swarm-harness-mp")
def swarm_harness_mp(
    processes: int = typer.Option(4, "--processes", help="Number of independent OS processes to spawn"),
    agents: int = typer.Option(1000, "--agents", help="Number of concurrent agents"),
    files: int = typer.Option(500, "--files", help="Number of base files"),
    run_seconds: float = typer.Option(60.0, "--run-seconds", help="Sustained submission window (seconds)"),
    drain_seconds: float = typer.Option(20.0, "--drain-seconds", help="Promotion drain window (seconds)"),
    contention: str = typer.Option("low", "--contention", help="Contention mode: low, normal, high"),
    min_interval_ms: int = typer.Option(400, "--min-interval-ms", help="Per-agent min submit interval (ms)"),
    max_interval_ms: int = typer.Option(1600, "--max-interval-ms", help="Per-agent max submit interval (ms)"),
    inflight: int = typer.Option(300, "--inflight", help="Max in-flight submissions"),
    promotion_batch: int = typer.Option(30, "--promotion-batch", help="Promotion engine batch size"),
    promotion_non_conflicting_batches: int = typer.Option(
        4,
        "--promotion-non-conflicting-batches",
        help="Maximum non-conflicting batches processed per promotion iteration",
    ),
    promotion_max_conflict_sets: int = typer.Option(
        1,
        "--promotion-max-conflict-sets",
        help="Maximum conflict sets processed per promotion iteration",
    ),
    gate_execution_mode: str = typer.Option(
        "parallel",
        "--gate-execution-mode",
        help="Gate execution mode: sequential, fast_fail, parallel",
    ),
    gate_max_parallel: int = typer.Option(
        4,
        "--gate-max-parallel",
        help="Max parallel gate commands per promotion snapshot",
    ),
    promotion_interval_ms: int = typer.Option(40, "--promotion-interval-ms", help="Promotion loop interval (ms)"),
    promotion_iteration_timeout_seconds: float = typer.Option(
        30.0,
        "--promotion-iteration-timeout-seconds",
        help="Max duration for one promotion iteration before skipping",
    ),
    materialization_batch_size: int = typer.Option(
        500,
        "--materialization-batch-size",
        help="Ingest materialization batch size per worker iteration",
    ),
    materialization_interval_ms: int = typer.Option(
        20,
        "--materialization-interval-ms",
        help="Ingest materialization loop interval (ms)",
    ),
    monitor_interval_ms: int = typer.Option(500, "--monitor-interval-ms", help="Backlog monitor interval (ms)"),
    adaptive_intake_throttle: bool = typer.Option(
        True,
        "--adaptive-intake-throttle/--no-adaptive-intake-throttle",
        help="Throttle submit loops when backlog crosses adaptive watermarks",
    ),
    intake_backlog_high_watermark: Optional[int] = typer.Option(
        None,
        "--intake-backlog-high-watermark",
        help="Backlog watermark that enables submit throttling (default: 0.8 * max_backlog_end)",
    ),
    intake_backlog_low_watermark: Optional[int] = typer.Option(
        None,
        "--intake-backlog-low-watermark",
        help="Backlog watermark that disables throttling (default: 0.9 * high watermark)",
    ),
    intake_throttle_check_interval_ms: int = typer.Option(
        120,
        "--intake-throttle-check-interval-ms",
        help="Throttle loop check/sleep interval while backlog is above watermark",
    ),
    physics_window_seconds: int = typer.Option(
        60,
        "--physics-window-seconds",
        help="Queue-physics telemetry window for final lambda/mu snapshot",
    ),
    submit_in_worker: bool = typer.Option(
        False,
        "--submit-in-worker/--submit-inline",
        help="Run proposal intake on a dedicated worker thread pool",
    ),
    target_mode: str = typer.Option(
        "direct",
        "--target-mode",
        help="Target mode: direct, testclient, external",
    ),
    external_api_url: Optional[str] = typer.Option(
        None,
        "--external-api-url",
        help="URL of external API server (e.g. http://localhost:8100)",
    ),
    traffic_mix: str = typer.Option(
        "full_file",
        "--traffic-mix",
        help="Traffic mix: full_file, unified_diff, mixed",
    ),
    submit_worker_threads: int = typer.Option(
        4,
        "--submit-worker-threads",
        help="Worker thread count for threaded proposal intake",
    ),
    defer_conflict_detection: bool = typer.Option(
        True,
        "--defer-conflict-detection/--inline-conflict-detection",
        help="Queue conflict detection off the submit path (INGESTED -> PENDING)",
    ),
    min_submit_throughput: float = typer.Option(40.0, "--min-submit-throughput", help="Min submit throughput SLO"),
    min_promotion_throughput: float = typer.Option(0.2, "--min-promotion-throughput", help="Min promotion throughput SLO"),
    min_mu_eff: float = typer.Option(33.3, "--min-mu-eff", help="Min mu_eff throughput SLO"),
    max_submit_p95_ms: float = typer.Option(140.0, "--max-submit-p95-ms", help="Max p95 submit latency SLO"),
    max_conflict_rate: float = typer.Option(0.35, "--max-conflict-rate", help="Max conflict-rate SLO"),
    max_backlog_end: int = typer.Option(2000, "--max-backlog-end", help="Max pending+evaluating at end"),
    max_submit_failure_rate: float = typer.Option(0.02, "--max-submit-failure-rate", help="Max submit failure-rate SLO"),
    max_window_rho: float = typer.Option(
        1.0,
        "--max-window-rho",
        help="Max allowed rho in final queue-physics window (lambda/mu)",
    ),
    max_fallback_rate: Optional[float] = typer.Option(
        None,
        "--max-fallback-rate",
        help="Optional max semantic fallback rate in final physics window",
    ),
    seed: int = typer.Option(2026, "--seed", help="Random seed"),
    output_json: Optional[Path] = typer.Option(None, "--output-json", help="Write full report JSON"),
    quiet: bool = typer.Option(True, "--quiet/--verbose", help="Suppress per-event logs during run"),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="Exit with code 2 when SLO checks fail"),
):
    """Run sustained multi-process swarm flow harness to isolate the GIL."""
    from packages.aether.services.swarm_harness import (
        ContinuousHarnessConfig,
        ContinuousHarnessThresholds,
    )
    from packages.aether.services.multiprocess_harness import MultiprocessHarnessOrchestrator

    config = ContinuousHarnessConfig(
        num_agents=agents,
        num_files=files,
        run_seconds=run_seconds,
        drain_seconds=drain_seconds,
        contention_mode=contention,
        agent_interval_min_ms=min_interval_ms,
        agent_interval_max_ms=max_interval_ms,
        max_inflight_submissions=inflight,
        promotion_batch_size=promotion_batch,
        promotion_non_conflicting_batches=promotion_non_conflicting_batches,
        promotion_max_conflict_sets=promotion_max_conflict_sets,
        gate_execution_mode=gate_execution_mode,
        gate_max_parallel=gate_max_parallel,
        promotion_interval_ms=promotion_interval_ms,
        promotion_iteration_timeout_seconds=promotion_iteration_timeout_seconds,
        materialization_batch_size=materialization_batch_size,
        materialization_interval_ms=materialization_interval_ms,
        monitor_interval_ms=monitor_interval_ms,
        submit_in_worker=submit_in_worker,
        target_mode=target_mode,
        external_api_url=external_api_url,
        traffic_mix=traffic_mix,
        submit_worker_threads=submit_worker_threads,
        defer_conflict_detection=defer_conflict_detection,
        adaptive_intake_throttle=adaptive_intake_throttle,
        intake_backlog_high_watermark=intake_backlog_high_watermark,
        intake_backlog_low_watermark=intake_backlog_low_watermark,
        intake_throttle_check_interval_ms=intake_throttle_check_interval_ms,
        physics_window_seconds=physics_window_seconds,
    )

    thresholds = ContinuousHarnessThresholds(
        min_submit_throughput=min_submit_throughput,
        min_promotion_throughput=min_promotion_throughput,
        min_mu_eff=min_mu_eff,
        max_submit_p95_ms=max_submit_p95_ms,
        max_conflict_rate=max_conflict_rate,
        max_backlog_end=max_backlog_end,
        max_submit_failure_rate=max_submit_failure_rate,
        max_window_rho=max_window_rho,
        max_fallback_rate=max_fallback_rate,
    )

    orchestrator = MultiprocessHarnessOrchestrator(
        config=config,
        thresholds=thresholds,
        num_processes=processes,
        seed=seed,
        output_json=output_json,
    )

    data = orchestrator.run()
    metrics = data["metrics"]

    console.print("[bold]Continuous Swarm Harness (Multi-Process)[/bold]")
    console.print(f"  Passed: {'yes' if data.get('passed') else 'no'}")
    console.print(f"  Processes: {processes}")
    console.print(f"  Agents: {agents} ({agents // processes} per process)")
    console.print(f"  Submit Throughput: {metrics['submit_throughput']:.2f} proposals/sec")
    console.print(f"  Promotion Throughput: {metrics['promotion_throughput']:.2f} proposals/sec")
    console.print(f"  Effective Throughput (mu_eff): {metrics.get('mu_eff', 0.0):.2f} proposals/sec")
    console.print(f"  Sustained Telemetry Promotion Rate: {metrics.get('multiprocess_saturated_promotion_rate', 0.0):.2f} promotions/sec")
    console.print(f"  Queue Saturated: {'yes' if metrics.get('multiprocess_queue_saturated') else 'no'}")
    
    check_table = Table(title="SLO Checks")
    check_table.add_column("Check", style="cyan")
    check_table.add_column("Result", style="green")
    for check, passed in data.get("checks", {}).items():
        check_table.add_row(check, "PASS" if passed else "FAIL")
    console.print(check_table)

    if output_json:
        output_json.write_text(json.dumps(data, indent=2, sort_keys=True))
        console.print(f"[dim]Wrote report: {output_json}[/dim]")

    if strict and not data.get("passed"):
        raise typer.Exit(2)


@app.command("snapshot-repro")
def snapshot_repro(
    iterations: int = typer.Option(2000, "--iterations", help="Maximum snapshot iterations to attempt"),
    files: int = typer.Option(64, "--files", help="Number of files in the synthetic repository"),
    file_lines: int = typer.Option(48, "--file-lines", help="Lines per generated file"),
    seed: int = typer.Option(2026, "--seed", help="Random seed"),
    output_json: Optional[Path] = typer.Option(None, "--output-json", help="Write report JSON"),
):
    """Run the focused snapshot recursion reproducer."""
    from packages.aether.services.snapshot_reproducer import (
        SnapshotReproducerConfig,
        run_snapshot_reproducer_sync,
    )

    config = SnapshotReproducerConfig(
        iterations=iterations,
        num_files=files,
        file_lines=file_lines,
        seed=seed,
    )
    report = run_snapshot_reproducer_sync(config)
    data = report.to_dict()

    console.print("[bold]Snapshot Reproducer[/bold]")
    console.print(f"  Passed: {'yes' if report.passed else 'no'}")
    console.print(f"  Iterations Completed: {report.total_iterations}")
    console.print(f"  First Failure Iteration: {report.first_failure_iteration or '(none)'}")
    console.print(f"  Failing Phase: {report.failing_phase or '(none)'}")
    console.print(f"  Exception: {report.exception_type or '(none)'}")
    console.print(f"  Max Identity Map Size: {report.max_identity_map_size:.1f}")
    console.print(f"  Storage Path: {report.storage_path}")

    if output_json:
        output_json.write_text(json.dumps(data, indent=2, sort_keys=True))
        console.print(f"[dim]Wrote report: {output_json}[/dim]")

    if not report.passed:
        raise typer.Exit(2)


# ============================================================================
# Version command
# ============================================================================

@app.command()
def version():
    """Show version information."""
    console.print("Aether CLI v0.1.0")
    console.print("Project Aether - Agent-Native Version Control")


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
