"""Server configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for the git-worktree MCP server.

    All values are loaded from environment variables with sensible defaults.
    """

    repo_path: str = field(default_factory=lambda: os.environ.get("GIT_REPO_PATH", "/repo"))
    workspaces_path: str = field(
        default_factory=lambda: os.environ.get("GIT_WORKSPACES_PATH", "/workspaces")
    )
    branch_prefix: str = field(default_factory=lambda: os.environ.get("GIT_BRANCH_PREFIX", "ws/"))
    manifest_dir: str = field(
        default_factory=lambda: os.environ.get("GIT_MANIFEST_DIR", ".worktrees")
    )
    max_worktrees: int = field(
        default_factory=lambda: int(os.environ.get("GIT_MAX_WORKTREES", "5"))
    )
    author_name: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_NAME", "git-worktree")
    )
    author_email: str = field(
        default_factory=lambda: os.environ.get("GIT_AUTHOR_EMAIL", "worktree@localhost")
    )

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Create a ServerConfig from current environment variables."""
        return cls()
