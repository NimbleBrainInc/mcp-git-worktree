"""Git Worktree MCP Server - FastMCP Implementation."""

from __future__ import annotations

import logging
import sys
from typing import Any

from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .api_models import ManifestStatus
from .config import ServerConfig
from .worktree_manager import (
    MaxWorktreesError,
    MergeBlockedError,
    MergeConflictError,
    SecretDetectedError,
    WorktreeError,
    WorktreeManager,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_git_worktree")
logger.info("Git Worktree server module loading...")

load_dotenv()

mcp = FastMCP("git-worktree")

# Lazy-initialized manager
_manager: WorktreeManager | None = None


def _get_manager() -> WorktreeManager:
    """Get or create the WorktreeManager singleton."""
    global _manager
    if _manager is None:
        config = ServerConfig.from_env()
        logger.info(
            "Initializing WorktreeManager: repo=%s workspaces=%s",
            config.repo_path,
            config.workspaces_path,
        )
        _manager = WorktreeManager(config)
    return _manager


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for monitoring."""
    return JSONResponse({"status": "healthy", "service": "mcp-git-worktree"})


@mcp.tool()
async def worktree_create(
    description: str,
    base_branch: str = "main",
    scope_directories: list[str] | None = None,
    scope_operations: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a new isolated worktree with its own branch.

    Args:
        description: Human-readable description of the worktree's purpose.
        base_branch: Branch to fork from (default: "main").
        scope_directories: Expected directories to be modified.
        scope_operations: Expected operations: add, modify, delete, rename.
        metadata: Arbitrary caller metadata.
    """
    if ctx:
        await ctx.info(f"Creating worktree: {description}")

    try:
        result = _get_manager().create(
            description=description,
            base_branch=base_branch,
            scope_dirs=scope_directories,
            scope_ops=scope_operations,
            metadata=metadata,
        )
        return result.model_dump()
    except MaxWorktreesError as e:
        return {"error": str(e)}
    except WorktreeError as e:
        return {"error": str(e)}


@mcp.tool()
async def worktree_list(
    status: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List all tracked worktrees and their status.

    Args:
        status: Optional filter by status (active, completed, merged, discarded, failed).
    """
    status_filter = ManifestStatus(status) if status else None
    result = _get_manager().list_worktrees(status_filter=status_filter)
    return result.model_dump()


@mcp.tool()
async def worktree_status(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Get detailed status of a single worktree including diff stats.

    Args:
        id: Worktree ID.
    """
    try:
        result = _get_manager().get_status(id)
        return result.model_dump()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def worktree_diff(
    id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Get the full diff between a worktree branch and its base with risk classification.

    Args:
        id: Worktree ID.
    """
    try:
        result = _get_manager().get_diff(id)
        return result.model_dump()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def worktree_merge(
    id: str,
    message: str | None = None,
    force: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Merge a worktree branch into its base branch and clean up.

    Args:
        id: Worktree ID.
        message: Optional merge commit message.
        force: Skip risk check (default: false). Use after user approval.
    """
    if ctx:
        await ctx.info(f"Merging worktree: {id} (force={force})")

    try:
        result = _get_manager().merge(id, message=message, force=force)
        return result.model_dump()
    except MergeBlockedError as e:
        return {"error": "merge_blocked", "risk_level": e.risk_level, "reasons": e.reasons}
    except MergeConflictError as e:
        return {"error": "merge_conflict", "conflict_files": e.conflict_files}
    except WorktreeError as e:
        return {"error": str(e)}


@mcp.tool()
async def worktree_discard(
    id: str,
    reason: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Discard a worktree and its branch without merging.

    Args:
        id: Worktree ID.
        reason: Why the worktree is being discarded.
    """
    if ctx:
        await ctx.info(f"Discarding worktree: {id}")

    try:
        result = _get_manager().discard(id, reason=reason)
        return result.model_dump()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def worktree_commit(
    id: str,
    message: str,
    paths: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Commit changes in a specific worktree.

    Args:
        id: Worktree ID.
        message: Commit message.
        paths: Files to stage. If empty, stages all changes.
    """
    if ctx:
        await ctx.info(f"Committing in worktree {id}: {message[:50]}")

    try:
        result = _get_manager().commit_in_worktree(id, message=message, paths=paths)
        return result.model_dump()
    except SecretDetectedError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


# Create ASGI application for HTTP deployment
app = mcp.http_app()

# Stdio entrypoint for mpak
if __name__ == "__main__":
    mcp.run()
