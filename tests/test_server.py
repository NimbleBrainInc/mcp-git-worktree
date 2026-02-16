"""Tests for the FastMCP server tools."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
from fastmcp import Client


@pytest.fixture(autouse=True)
def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up environment variables for the server config and reset the manager singleton."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    workspaces_path = tmp_path / "workspaces"
    workspaces_path.mkdir()

    # Init a repo with main branch
    repo = pygit2.init_repository(str(repo_path), bare=False, initial_head="main")
    repo.config["user.name"] = "Test User"
    repo.config["user.email"] = "test@test.com"

    readme = repo_path / "README.md"
    readme.write_text("# Test\n", encoding="utf-8")
    repo.index.add("README.md")
    repo.index.write()
    tree_oid = repo.index.write_tree()
    sig = pygit2.Signature("Test User", "test@test.com")
    repo.create_commit("refs/heads/main", sig, sig, "Initial commit", tree_oid, [])
    repo.set_head("refs/heads/main")

    monkeypatch.setenv("GIT_REPO_PATH", str(repo_path))
    monkeypatch.setenv("GIT_WORKSPACES_PATH", str(workspaces_path))
    monkeypatch.setenv("GIT_MANIFEST_DIR", "system/workstreams")
    monkeypatch.setenv("GIT_MAX_WORKTREES", "5")
    monkeypatch.setenv("GIT_BRANCH_PREFIX", "ws/")

    # Reset the lazy singleton so each test gets a fresh manager
    import mcp_git_worktree.server as server_mod

    server_mod._manager = None


@pytest.fixture
def mcp_server():
    """Return the MCP server instance."""
    from mcp_git_worktree.server import mcp

    return mcp


@pytest.mark.asyncio
async def test_tools_list(mcp_server) -> None:
    """Test that all 7 tools are registered."""
    async with Client(mcp_server) as client:
        tools = await client.list_tools()
        tool_names = sorted(t.name for t in tools)
        expected = sorted(
            [
                "worktree_create",
                "worktree_list",
                "worktree_status",
                "worktree_diff",
                "worktree_merge",
                "worktree_discard",
                "worktree_commit",
            ]
        )
        assert tool_names == expected


@pytest.mark.asyncio
async def test_full_lifecycle(mcp_server, tmp_path: Path) -> None:
    """Test create -> commit -> diff -> merge lifecycle."""
    async with Client(mcp_server) as client:
        # Create
        result = await client.call_tool(
            "worktree_create",
            {
                "description": "Lifecycle test",
                "scope_directories": ["docs/"],
                "scope_operations": ["add"],
            },
        )
        data = result.structured_content
        assert "id" in data
        wt_id = data["id"]
        wt_path = data["worktree_path"]

        # Write a file in the worktree
        docs_dir = Path(wt_path) / "docs"
        docs_dir.mkdir(exist_ok=True)
        (docs_dir / "new.md").write_text("# New\n", encoding="utf-8")

        # Commit in worktree
        commit_result = await client.call_tool(
            "worktree_commit",
            {
                "id": wt_id,
                "message": "Add doc",
            },
        )
        commit_data = commit_result.structured_content
        assert "commit" in commit_data

        # Diff
        diff_result = await client.call_tool("worktree_diff", {"id": wt_id})
        diff_data = diff_result.structured_content
        assert diff_data["risk_level"] == "low"

        # Merge
        merge_result = await client.call_tool("worktree_merge", {"id": wt_id})
        merge_data = merge_result.structured_content
        assert "merge_commit" in merge_data


@pytest.mark.asyncio
async def test_discard_flow(mcp_server) -> None:
    """Test create -> discard flow."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "worktree_create",
            {
                "description": "Discard test",
            },
        )
        wt_id = result.structured_content["id"]

        discard_result = await client.call_tool(
            "worktree_discard",
            {
                "id": wt_id,
                "reason": "No longer needed",
            },
        )
        assert discard_result.structured_content["status"] == "discarded"

        # Verify it appears as discarded in list
        list_result = await client.call_tool("worktree_list", {"status": "discarded"})
        assert len(list_result.structured_content["worktrees"]) == 1


@pytest.mark.asyncio
async def test_max_worktrees_error(mcp_server) -> None:
    """Test that exceeding max worktrees returns an error."""
    async with Client(mcp_server) as client:
        for i in range(5):
            await client.call_tool(
                "worktree_create",
                {
                    "description": f"Worktree {i}",
                },
            )

        result = await client.call_tool(
            "worktree_create",
            {
                "description": "One too many",
            },
        )
        assert "error" in result.structured_content


@pytest.mark.asyncio
async def test_missing_worktree_status(mcp_server) -> None:
    """Test status of non-existent worktree."""
    async with Client(mcp_server) as client:
        result = await client.call_tool("worktree_status", {"id": "wt_NONEXISTENT"})
        assert "error" in result.structured_content
