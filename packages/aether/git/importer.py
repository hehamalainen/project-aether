"""Git importer for Aether.

Converts a Git repository into Aether CRDT state:
1. Clone or read existing Git repo
2. Extract file tree at HEAD
3. Initialize DocumentStore with file contents
4. Optionally import commit history as proposals
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from datetime import datetime
import structlog

from ..crdt.document_store import DocumentStore
from ..crdt.rga import RGADocument

logger = structlog.get_logger()


class GitImporter:
    """Import Git repositories into Aether."""
    
    def __init__(self, storage_path: Path):
        """Initialize importer.
        
        Args:
            storage_path: Path for temporary clones and storage
        """
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
    
    def import_from_path(
        self,
        git_path: Path,
        repo_id: str,
        include_history: bool = False,
        max_commits: int = 100,
    ) -> ImportResult:
        """Import a local Git repository.
        
        Args:
            git_path: Path to local git repository
            repo_id: Aether repository ID to create
            include_history: Whether to import commit history as proposals
            max_commits: Maximum commits to import if include_history
            
        Returns:
            ImportResult with DocumentStore and metadata
        """
        git_path = Path(git_path).resolve()
        
        if not (git_path / ".git").exists():
            raise ValueError(f"Not a git repository: {git_path}")
        
        logger.info("importing_git_repo", path=str(git_path), repo_id=repo_id)
        
        # Get current HEAD
        head_sha = self._run_git(git_path, ["rev-parse", "HEAD"]).strip()
        
        # Get tracked files
        files = self._get_tracked_files(git_path)
        
        # Create DocumentStore and initialize with file contents
        document_store = DocumentStore(replica_id=f"import_{repo_id}")
        
        imported_files = 0
        skipped_files = 0
        
        for file_path in files:
            full_path = git_path / file_path
            
            # Skip binary files and large files
            if self._is_binary(full_path) or self._is_too_large(full_path):
                skipped_files += 1
                continue
            
            try:
                content = full_path.read_text(encoding="utf-8")
                document_store.set_file(file_path, content)
                imported_files += 1
            except Exception as e:
                logger.warning("skip_file", path=file_path, error=str(e))
                skipped_files += 1
        
        # Get branch info
        try:
            branch = self._run_git(git_path, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except Exception:
            branch = "main"
        
        result = ImportResult(
            repo_id=repo_id,
            document_store=document_store,
            source_path=git_path,
            head_sha=head_sha,
            branch=branch,
            imported_files=imported_files,
            skipped_files=skipped_files,
            commit_history=[],
        )
        
        # Optionally import history
        if include_history:
            result.commit_history = self._import_history(git_path, max_commits)
        
        logger.info(
            "import_complete",
            repo_id=repo_id,
            files=imported_files,
            skipped=skipped_files,
            head=head_sha[:8],
        )
        
        return result
    
    def import_from_url(
        self,
        git_url: str,
        repo_id: str,
        branch: str = "main",
        include_history: bool = False,
    ) -> ImportResult:
        """Clone and import a remote Git repository.
        
        Args:
            git_url: Git clone URL
            repo_id: Aether repository ID
            branch: Branch to import
            include_history: Whether to import commit history
            
        Returns:
            ImportResult with DocumentStore and metadata
        """
        # Create temp directory for clone
        clone_dir = self.storage_path / "clones" / repo_id
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        
        if clone_dir.exists():
            # Pull latest
            logger.info("pulling_existing_clone", path=str(clone_dir))
            self._run_git(clone_dir, ["fetch", "origin"])
            self._run_git(clone_dir, ["checkout", branch])
            self._run_git(clone_dir, ["pull", "origin", branch])
        else:
            # Fresh clone
            logger.info("cloning_repo", url=git_url, branch=branch)
            subprocess.run(
                ["git", "clone", "--branch", branch, "--single-branch", git_url, str(clone_dir)],
                check=True,
                capture_output=True,
            )
        
        return self.import_from_path(
            clone_dir,
            repo_id,
            include_history=include_history,
        )
    
    def _get_tracked_files(self, git_path: Path) -> list[str]:
        """Get list of tracked files in the repo."""
        output = self._run_git(git_path, ["ls-files"])
        return [f for f in output.strip().split("\n") if f]
    
    def _import_history(self, git_path: Path, max_commits: int) -> list[dict]:
        """Import commit history as proposal metadata."""
        output = self._run_git(
            git_path,
            ["log", f"-{max_commits}", "--format=%H|%an|%ae|%at|%s"]
        )
        
        commits = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) >= 5:
                commits.append({
                    "sha": parts[0],
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "timestamp": datetime.fromtimestamp(int(parts[3])),
                    "message": parts[4],
                })
        
        return commits
    
    def _run_git(self, cwd: Path, args: list[str]) -> str:
        """Run a git command."""
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    
    def _is_binary(self, path: Path) -> bool:
        """Check if file is binary."""
        if not path.exists():
            return False
        
        # Check by extension
        binary_exts = {
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
            ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll",
            ".so", ".dylib", ".pyc", ".pyo", ".whl",
            ".mp3", ".mp4", ".wav", ".avi", ".mov",
            ".ttf", ".woff", ".woff2", ".eot",
        }
        if path.suffix.lower() in binary_exts:
            return True
        
        # Check file content
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except Exception:
            return True
    
    def _is_too_large(self, path: Path, max_size: int = 1_000_000) -> bool:
        """Check if file is too large (default 1MB)."""
        try:
            return path.stat().st_size > max_size
        except Exception:
            return True


class ImportResult:
    """Result of a Git import operation."""
    
    def __init__(
        self,
        repo_id: str,
        document_store: DocumentStore,
        source_path: Path,
        head_sha: str,
        branch: str,
        imported_files: int,
        skipped_files: int,
        commit_history: list[dict],
    ):
        self.repo_id = repo_id
        self.document_store = document_store
        self.source_path = source_path
        self.head_sha = head_sha
        self.branch = branch
        self.imported_files = imported_files
        self.skipped_files = skipped_files
        self.commit_history = commit_history
    
    def summary(self) -> str:
        """Get a human-readable summary."""
        return (
            f"Imported {self.imported_files} files from {self.source_path}\n"
            f"Branch: {self.branch} @ {self.head_sha[:8]}\n"
            f"Skipped: {self.skipped_files} files (binary/large)"
        )
