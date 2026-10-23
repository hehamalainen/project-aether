"""Git exporter for Aether.

Exports Aether state back to Git:
1. Export snapshot as a git commit
2. Export promotion history as git log
3. Create deterministic git bundles for distribution
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from datetime import datetime
import structlog

from ..crdt.document_store import DocumentStore
from ..models.entities import Snapshot, Promotion

logger = structlog.get_logger()


class GitExporter:
    """Export Aether state to Git repositories."""
    
    def __init__(self, storage_path: Path):
        """Initialize exporter.
        
        Args:
            storage_path: Path for temporary exports
        """
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
    
    def export_snapshot_to_git(
        self,
        document_store: DocumentStore,
        output_path: Path,
        commit_message: str = "Export from Aether",
        author_name: str = "Aether",
        author_email: str = "aether@localhost",
        init_if_needed: bool = True,
    ) -> ExportResult:
        """Export current DocumentStore state as a git commit.
        
        Args:
            document_store: The DocumentStore to export
            output_path: Path to git repository (will create if needed)
            commit_message: Git commit message
            author_name: Git author name
            author_email: Git author email
            init_if_needed: Initialize git repo if not exists
            
        Returns:
            ExportResult with commit info
        """
        output_path = Path(output_path).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize git if needed
        git_dir = output_path / ".git"
        if not git_dir.exists():
            if init_if_needed:
                self._run_git(output_path, ["init"])
                logger.info("initialized_git_repo", path=str(output_path))
            else:
                raise ValueError(f"Not a git repository: {output_path}")
        
        # Write all files from document store
        files_written = 0
        for file_path in document_store.list_files():
            content = document_store.get_file(file_path)
            if content is not None:
                full_path = output_path / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                files_written += 1
        
        # Stage all changes
        self._run_git(output_path, ["add", "-A"])
        
        # Check if there are changes to commit
        status = self._run_git(output_path, ["status", "--porcelain"])
        if not status.strip():
            return ExportResult(
                output_path=output_path,
                commit_sha=None,
                files_written=files_written,
                message="No changes to commit",
            )
        
        # Commit with author info
        env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        self._run_git(output_path, ["commit", "-m", commit_message], env=env)
        
        # Get commit SHA
        commit_sha = self._run_git(output_path, ["rev-parse", "HEAD"]).strip()
        
        logger.info(
            "exported_to_git",
            path=str(output_path),
            commit=commit_sha[:8],
            files=files_written,
        )
        
        return ExportResult(
            output_path=output_path,
            commit_sha=commit_sha,
            files_written=files_written,
            message=f"Committed {files_written} files",
        )
    
    def create_bundle(
        self,
        document_store: DocumentStore,
        bundle_name: str,
    ) -> Path:
        """Create a git bundle from DocumentStore state.
        
        Args:
            document_store: The DocumentStore to export
            bundle_name: Name for the bundle file
            
        Returns:
            Path to the created bundle file
        """
        # Create temp git repo
        temp_dir = Path(tempfile.mkdtemp(prefix="aether-bundle-"))
        
        # Export to temp repo
        self.export_snapshot_to_git(
            document_store,
            temp_dir,
            commit_message=f"Aether bundle: {bundle_name}",
        )
        
        # Create bundle
        bundle_path = self.storage_path / "bundles" / f"{bundle_name}.bundle"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._run_git(temp_dir, ["bundle", "create", str(bundle_path), "HEAD"])
        
        # Cleanup temp dir
        import shutil
        shutil.rmtree(temp_dir)
        
        logger.info("created_bundle", path=str(bundle_path))
        
        return bundle_path
    
    def export_promotions_as_commits(
        self,
        promotions: list[Promotion],
        document_stores: dict[str, DocumentStore],
        output_path: Path,
    ) -> ExportResult:
        """Export a series of promotions as git commits.
        
        Creates one commit per promotion, preserving the timeline.
        
        Args:
            promotions: List of Promotion records (oldest first)
            document_stores: Map of snapshot_id -> DocumentStore state
            output_path: Output git repository path
            
        Returns:
            ExportResult with final commit info
        """
        output_path = Path(output_path).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize git
        if not (output_path / ".git").exists():
            self._run_git(output_path, ["init"])
        
        commits_created = 0
        
        for promotion in promotions:
            if promotion.snapshot_id not in document_stores:
                logger.warning("missing_snapshot", snapshot_id=promotion.snapshot_id)
                continue
            
            doc_store = document_stores[promotion.snapshot_id]
            
            # Write files
            for file_path in doc_store.list_files():
                content = doc_store.get_file(file_path)
                if content is not None:
                    full_path = output_path / file_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")
            
            # Stage and commit
            self._run_git(output_path, ["add", "-A"])
            
            status = self._run_git(output_path, ["status", "--porcelain"])
            if not status.strip():
                continue
            
            commit_msg = (
                f"Promotion {promotion.id}\n\n"
                f"Score: {promotion.final_score:.2f}\n"
                f"Triggered by: {promotion.triggered_by}"
            )
            
            # Set commit timestamp to promotion time
            env = {
                "GIT_AUTHOR_DATE": promotion.created_at.isoformat(),
                "GIT_COMMITTER_DATE": promotion.created_at.isoformat(),
                "GIT_AUTHOR_NAME": "Aether",
                "GIT_AUTHOR_EMAIL": "aether@localhost",
                "GIT_COMMITTER_NAME": "Aether",
                "GIT_COMMITTER_EMAIL": "aether@localhost",
            }
            
            self._run_git(output_path, ["commit", "-m", commit_msg], env=env)
            commits_created += 1
        
        # Get final commit
        commit_sha = self._run_git(output_path, ["rev-parse", "HEAD"]).strip() if commits_created > 0 else None
        
        return ExportResult(
            output_path=output_path,
            commit_sha=commit_sha,
            files_written=0,  # Not tracked per-commit
            message=f"Created {commits_created} commits from promotions",
        )
    
    def _run_git(self, cwd: Path, args: list[str], env: dict = None) -> str:
        """Run a git command."""
        import os
        
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            env=full_env,
        )
        return result.stdout


class ExportResult:
    """Result of a Git export operation."""
    
    def __init__(
        self,
        output_path: Path,
        commit_sha: Optional[str],
        files_written: int,
        message: str,
    ):
        self.output_path = output_path
        self.commit_sha = commit_sha
        self.files_written = files_written
        self.message = message
    
    def summary(self) -> str:
        """Get a human-readable summary."""
        if self.commit_sha:
            return f"{self.message}\nCommit: {self.commit_sha[:8]}\nPath: {self.output_path}"
        return f"{self.message}\nPath: {self.output_path}"
