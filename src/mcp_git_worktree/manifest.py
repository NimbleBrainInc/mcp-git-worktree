"""Manifest manager for worktree lifecycle tracking.

YAML read/write for worktree manifest files stored in the git repository.
Filesystem-only, no git operations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from ulid import ULID

from mcp_git_worktree.api_models import (
    ManifestStatus,
    ScopeDeclaration,
    WorktreeManifest,
)
from mcp_git_worktree.config import ServerConfig


class ManifestNotFoundError(Exception):
    """Raised when a manifest file cannot be found."""

    def __init__(self, worktree_id: str) -> None:
        self.worktree_id = worktree_id
        super().__init__(f"Manifest not found: {worktree_id}")


def _manifest_path(config: ServerConfig, worktree_id: str) -> Path:
    """Resolve the filesystem path for a manifest file."""
    return Path(config.repo_path) / config.manifest_dir / f"{worktree_id}.yaml"


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def generate_worktree_id() -> str:
    """Generate a new worktree ID using ULID."""
    return f"wt_{ULID()}"


def create_manifest(
    config: ServerConfig,
    worktree_id: str,
    description: str,
    base_branch: str,
    base_commit: str,
    worktree_path: str,
    scope: ScopeDeclaration | None = None,
    metadata: dict[str, object] | None = None,
) -> WorktreeManifest:
    """Create a new manifest and write it to disk.

    Args:
        config: Server configuration.
        worktree_id: Unique worktree identifier.
        description: Human-readable description of the worktree's purpose.
        base_branch: The branch the worktree was forked from.
        base_commit: The commit OID the worktree was created from.
        worktree_path: Filesystem path to the worktree checkout.
        scope: Optional scope declaration for risk classification.
        metadata: Optional arbitrary key-value pairs from the caller.

    Returns:
        The created WorktreeManifest.
    """
    now = _now_iso()
    branch = f"{config.branch_prefix}{worktree_id}"

    manifest = WorktreeManifest(
        id=worktree_id,
        status=ManifestStatus.active,
        created_at=now,
        updated_at=now,
        branch=branch,
        base_branch=base_branch,
        base_commit=base_commit,
        worktree_path=worktree_path,
        description=description,
        metadata=metadata or {},
        scope=scope or ScopeDeclaration(),
    )

    _write_manifest(config, manifest)
    return manifest


def read_manifest(config: ServerConfig, worktree_id: str) -> WorktreeManifest:
    """Read a manifest from disk.

    Args:
        config: Server configuration.
        worktree_id: The worktree identifier.

    Returns:
        The parsed WorktreeManifest.

    Raises:
        ManifestNotFoundError: If the manifest file does not exist.
    """
    path = _manifest_path(config, worktree_id)
    if not path.is_file():
        raise ManifestNotFoundError(worktree_id)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return WorktreeManifest.model_validate(data)


def read_all_manifests(
    config: ServerConfig,
    status_filter: ManifestStatus | None = None,
) -> list[WorktreeManifest]:
    """Read all manifest files from the manifest directory.

    Args:
        config: Server configuration.
        status_filter: Optional filter to return only manifests with this status.

    Returns:
        List of WorktreeManifest objects, sorted by created_at.
    """
    manifest_dir = Path(config.repo_path) / config.manifest_dir
    if not manifest_dir.is_dir():
        return []

    manifests: list[WorktreeManifest] = []
    for yaml_file in sorted(manifest_dir.glob("*.yaml")):
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        manifest = WorktreeManifest.model_validate(data)
        if status_filter is None or manifest.status == status_filter:
            manifests.append(manifest)

    return manifests


def update_manifest(config: ServerConfig, manifest: WorktreeManifest) -> None:
    """Write an updated manifest back to disk.

    Automatically updates the `updated_at` timestamp.

    Args:
        config: Server configuration.
        manifest: The manifest to persist.
    """
    manifest.updated_at = _now_iso()
    _write_manifest(config, manifest)


def _write_manifest(config: ServerConfig, manifest: WorktreeManifest) -> None:
    """Write a manifest to its YAML file."""
    path = _manifest_path(config, manifest.id)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = manifest.model_dump(mode="json")
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
