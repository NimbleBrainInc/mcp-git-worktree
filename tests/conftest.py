"""Shared fixtures for git-worktree tests."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from mcp_git_worktree.config import ServerConfig


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> pygit2.Repository:
    """Create a temporary git repository with an initial commit on 'main'."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Set default branch to main before init
    repo = pygit2.init_repository(str(repo_path), bare=False, initial_head="main")

    # Configure git identity
    repo.config["user.name"] = "Test User"
    repo.config["user.email"] = "test@example.com"

    # Create initial commit with a README
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")

    repo.index.add("README.md")
    repo.index.write()
    tree_oid = repo.index.write_tree()

    sig = pygit2.Signature("Test User", "test@example.com")
    repo.create_commit("refs/heads/main", sig, sig, "Initial commit", tree_oid, [])

    # Set HEAD to point to main
    repo.set_head("refs/heads/main")

    return repo


@pytest.fixture
def server_config(tmp_git_repo: pygit2.Repository, tmp_path: Path) -> ServerConfig:
    """Create a ServerConfig pointing to the temp git repo."""
    workspaces_path = tmp_path / "workspaces"
    workspaces_path.mkdir()

    return ServerConfig(
        repo_path=str(tmp_git_repo.workdir).rstrip("/"),
        workspaces_path=str(workspaces_path),
        branch_prefix="ws/",
        manifest_dir="system/workstreams",
        max_worktrees=3,
        author_name="Test Author",
        author_email="test@localhost",
    )
