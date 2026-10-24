"""
Repository-relative path validation, symlink-safe filesystem containment,
and safe tarball extraction helpers.
"""

from pathlib import Path
import tarfile
from typing import Union


RESERVED_PATH_SEGMENTS = {".git"}
RESERVED_FILENAMES = {".aether_snapshot_manifest.json"}


def validate_repo_relative_path(path: str) -> str:
    """
    Validate and normalize a repository-relative POSIX file path.

    Rejects:
    - Empty strings or non-strings
    - NUL bytes or control newlines
    - Leading/trailing slashes, empty segments ('//'), or tilde prefixes ('~')
    - Backslashes ('\\') or Windows drive/scheme prefixes ('C:', 'file:')
    - '.' or '..' traversal segments
    - Reserved internal segments ('.git') or manifest filenames
    """
    if not isinstance(path, str):
        raise ValueError("Path must be a string")

    cleaned = path.strip()
    if not cleaned:
        raise ValueError("Path must not be empty")

    if "\x00" in cleaned or "\r" in cleaned or "\n" in cleaned:
        raise ValueError(f"Path contains forbidden control characters: {path!r}")

    if "\\" in cleaned:
        raise ValueError(f"Path must use forward slashes only: {path!r}")

    if cleaned.startswith(("/", "~")) or cleaned.endswith("/"):
        raise ValueError(f"Path must be a relative repository path: {path!r}")

    parts = cleaned.split("/")
    if any(part == "" for part in parts):
        raise ValueError(f"Path contains empty segment: {path!r}")

    if ":" in parts[0]:
        raise ValueError(f"Path must not include a drive or scheme prefix: {path!r}")

    for part in parts:
        if part in (".", ".."):
            raise ValueError(f"Path traversal segment is not allowed: {path!r}")
        if part in RESERVED_PATH_SEGMENTS:
            raise ValueError(f"Reserved path segment '{part}' is not allowed: {path!r}")

    if parts[-1] in RESERVED_FILENAMES:
        raise ValueError(f"Reserved filename '{parts[-1]}' is not allowed: {path!r}")

    return "/".join(parts)


def resolve_contained_path(base_dir: Union[Path, str], rel_path: str) -> Path:
    """
    Resolve ``rel_path`` inside ``base_dir`` while enforcing:
    1. ``rel_path`` is a valid repository-relative path.
    2. ``base_dir`` itself is not a symlink.
    3. No existing intermediate directory or target file under ``base_dir`` is a symlink.
    4. The resolved target path remains strictly within ``base_dir.resolve()``.
    """
    normalized = validate_repo_relative_path(rel_path)
    base_path = Path(base_dir)
    if base_path.is_symlink():
        raise ValueError(f"Base directory must not be a symlink: {base_path}")

    base_resolved = base_path.resolve()
    current = base_resolved
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlink escape detected in path: {rel_path!r}")

    resolved_target = current.resolve(strict=False)
    if resolved_target == base_resolved or not resolved_target.is_relative_to(base_resolved):
        raise ValueError(f"Resolved path escapes base directory: {rel_path!r}")

    return current


def safe_extract_tarball(tarball: tarfile.TarFile, dest_dir: Union[Path, str]) -> list[Path]:
    """
    Safely extract a snapshot tarball into ``dest_dir`` across Python >=3.11.

    Validates all archive members before writing any files:
    - Rejects symlinks, hardlinks, FIFOs, character/block devices, and unknown member types.
    - Rejects absolute paths, '..' traversal, drive prefixes, and reserved paths.
    - Enforces symlink-safe containment under ``dest_dir`` for every directory and file.
    """
    dest_path = Path(dest_dir)
    if dest_path.is_symlink():
        raise ValueError(f"Destination directory must not be a symlink: {dest_path}")
    dest_path.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_path.resolve()

    members = tarball.getmembers()
    validated_entries: list[tuple[tarfile.TarInfo, str]] = []

    for member in members:
        if (
            member.issym()
            or member.islnk()
            or member.ischr()
            or member.isblk()
            or member.isfifo()
            or member.isdev()
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"Unsafe or unsupported tar member type: {member.name!r}")

        raw_name = member.name
        while raw_name.startswith("./"):
            raw_name = raw_name[2:]
        if member.isdir():
            raw_name = raw_name.rstrip("/")
            if raw_name in ("", "."):
                continue

        rel_path = validate_repo_relative_path(raw_name)
        resolve_contained_path(dest_resolved, rel_path)
        validated_entries.append((member, rel_path))

    extracted_files: list[Path] = []
    for member, rel_path in validated_entries:
        target = resolve_contained_path(dest_resolved, rel_path)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            resolve_contained_path(dest_resolved, rel_path)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target = resolve_contained_path(dest_resolved, rel_path)
        handle = tarball.extractfile(member)
        if handle is None:
            raise ValueError(f"Unable to read tar member content: {member.name!r}")
        with handle:
            data = handle.read()
        target.write_bytes(data)
        extracted_files.append(target)

    return extracted_files
