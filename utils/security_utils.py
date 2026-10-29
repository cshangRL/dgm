"""Small, dependency-free helpers for host/container trust boundaries.

The harness treats container output, dataset fields, and archive members as
untrusted. These helpers keep that policy in one place: validate identifiers,
cap reads, redact URLs, and extract tar members without following links.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

# Defaults are deliberately finite; override with env vars if a legitimate
# patch/log is larger.
MAX_ARCHIVE_BYTES = int(os.getenv("DGM_MAX_ARCHIVE_BYTES", str(64 * 1024 * 1024)))
MAX_PATCH_BYTES = int(os.getenv("DGM_MAX_PATCH_BYTES", str(16 * 1024 * 1024)))
MAX_LOG_BYTES = int(os.getenv("DGM_MAX_LOG_BYTES", str(1024 * 1024)))
MAX_TAR_MEMBERS = int(os.getenv("DGM_MAX_TAR_MEMBERS", "10000"))

INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def validate_instance_id(instance_id: str) -> str:
    """Validate a SWE-bench/polyglot instance id before using it in host paths."""
    if not isinstance(instance_id, str) or not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError(f"Invalid instance_id: {instance_id!r}")
    if ".." in instance_id or instance_id.startswith((".", "/")):
        raise ValueError(f"Unsafe instance_id: {instance_id!r}")
    return instance_id


def validate_git_commit(commit: str) -> str:
    """Validate a git commit hash before interpolating it into a shell command."""
    if not isinstance(commit, str) or not GIT_COMMIT_RE.fullmatch(commit):
        raise ValueError(f"Invalid git commit: {commit!r}")
    return commit


def redact_url(url: str) -> str:
    """Strip credentials, query strings, and fragments from a URL for logs."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<redacted-url>"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urllib.parse.urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))


def _validate_member_name(name: str) -> None:
    if not name:
        raise ValueError("Tar member has an empty name")
    if name.startswith(("/", "\\")):
        raise ValueError(f"Absolute tar member path: {name!r}")
    if re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"Drive-qualified tar member path: {name!r}")
    if ".." in Path(name).parts:
        raise ValueError(f"Parent traversal in tar member path: {name!r}")


def _validate_member(member: tarfile.TarInfo) -> None:
    _validate_member_name(member.name)
    if member.issym() or member.islnk():
        raise ValueError(f"Link tar member rejected: {member.name!r}")
    if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
        raise ValueError(f"Special tar member rejected: {member.name!r}")
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"Unsupported tar member type: {member.name!r}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_member(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path, remaining: int) -> int:
    source = tar.extractfile(member)
    if source is None:
        raise ValueError(f"Cannot read tar member: {member.name!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".dgm-extract-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            written = 0
            while True:
                chunk = source.read(min(65536, remaining - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining:
                    raise ValueError(f"Tar member exceeds size cap: {member.name!r}")
                out.write(chunk)
        os.replace(tmp_name, target)
        return written
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def safe_extract_tar(archive_data: bytes, dest_root: Path, max_bytes: int = MAX_ARCHIVE_BYTES) -> Path:
    """Extract a tar into dest_root, rejecting links/traversal and capping bytes."""
    if len(archive_data) > max_bytes:
        raise ValueError(f"Archive exceeds size cap ({len(archive_data)} > {max_bytes})")
    dest_root = Path(dest_root).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_TAR_MEMBERS:
            raise ValueError(f"Archive has too many members ({len(members)})")
        total = 0
        for member in members:
            _validate_member(member)
            target = (dest_root / member.name).resolve()
            if not _is_within(target, dest_root):
                raise ValueError(f"Tar member escapes destination: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > max_bytes:
                raise ValueError("Archive exceeds uncompressed size cap")
            _write_member(tar, member, target, max_bytes - total + member.size)
    return dest_root


def safe_extract_single_file(
    archive_data: bytes,
    dest_path: Path,
    expected_name: Optional[str] = None,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> Path:
    """Extract exactly one regular file from a tar into dest_path."""
    if len(archive_data) > max_bytes:
        raise ValueError(f"Archive exceeds size cap ({len(archive_data)} > {max_bytes})")
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:*") as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
        if len(files) != 1:
            raise ValueError(f"Expected one regular file in archive, got {len(files)}")
        member = files[0]
        _validate_member(member)
        if expected_name is not None and Path(member.name).name != expected_name:
            raise ValueError(
                f"Unexpected archive member {member.name!r}; expected {expected_name!r}"
            )
        _write_member(tar, member, dest_path, max_bytes)
    return dest_path


def truncate_bytes(data: bytes, limit: int = MAX_LOG_BYTES) -> bytes:
    """Bound a byte string before logging or decoding it."""
    if len(data) <= limit:
        return data
    return data[:limit] + b"\n...[truncated by dgm security limit]...\n"
