"""Git integration module for Aether.

Provides import/export capabilities between Git and Aether:
- Import: Convert Git repository to Aether CRDT state
- Export: Export Aether snapshots as Git commits
"""

from .importer import GitImporter
from .exporter import GitExporter

__all__ = ["GitImporter", "GitExporter"]
