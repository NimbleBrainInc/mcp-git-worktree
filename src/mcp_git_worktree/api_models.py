"""Pydantic models for Git Worktree MCP Server responses."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ManifestStatus(StrEnum):
    """Lifecycle status of a worktree."""

    active = "active"
    completed = "completed"
    merged = "merged"
    discarded = "discarded"
    failed = "failed"


class RiskLevel(StrEnum):
    """Risk classification for a worktree diff."""

    low = "low"
    high = "high"


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------


class ScopeDeclaration(BaseModel):
    """Declares the expected scope of changes for a worktree."""

    directories: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)


class WorktreeResult(BaseModel):
    """Result data populated when a worktree reaches a terminal state."""

    head_commit: str | None = None
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    risk_level: RiskLevel | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    merge_commit: str | None = None
    discard_reason: str | None = None


class WorktreeManifest(BaseModel):
    """Full manifest schema for a worktree (stored as YAML in the repo)."""

    id: str
    status: ManifestStatus = ManifestStatus.active
    created_at: str
    updated_at: str

    branch: str
    base_branch: str
    base_commit: str
    worktree_path: str

    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    scope: ScopeDeclaration = Field(default_factory=ScopeDeclaration)
    result: WorktreeResult = Field(default_factory=WorktreeResult)


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------


class RiskAssessment(BaseModel):
    """Result of risk classification on a worktree diff."""

    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    conflict_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Diff models
# ---------------------------------------------------------------------------


class DiffFileEntry(BaseModel):
    """A single file change in a worktree diff."""

    path: str
    status: str  # added | modified | deleted | renamed
    lines_added: int = 0
    lines_removed: int = 0
    in_scope: bool = True


class DiffStats(BaseModel):
    """Aggregate diff statistics."""

    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    lines_added: int = 0
    lines_removed: int = 0


# ---------------------------------------------------------------------------
# Tool response models
# ---------------------------------------------------------------------------


class WorktreeCreateResponse(BaseModel):
    """Response from worktree_create."""

    id: str
    branch: str
    worktree_path: str
    base_commit: str


class WorktreeListEntry(BaseModel):
    """A single entry in the worktree list."""

    id: str
    status: ManifestStatus
    description: str
    branch: str
    base_branch: str
    created_at: str
    commits_ahead: int = 0


class WorktreeListResponse(BaseModel):
    """Response from worktree_list."""

    worktrees: list[WorktreeListEntry] = Field(default_factory=list)


class WorktreeStatusResponse(BaseModel):
    """Response from worktree_status."""

    manifest: WorktreeManifest
    commits_ahead: int = 0
    uncommitted_changes: bool = False
    diff_stats: DiffStats = Field(default_factory=DiffStats)
    can_fast_forward: bool = True


class WorktreeDiffResponse(BaseModel):
    """Response from worktree_diff."""

    base_commit: str
    head_commit: str
    risk_level: RiskLevel
    risk_reasons: list[str] = Field(default_factory=list)
    files: list[DiffFileEntry] = Field(default_factory=list)


class WorktreeMergeResponse(BaseModel):
    """Response from worktree_merge."""

    merge_commit: str
    strategy: str  # fast_forward | three_way
    files_merged: int
    risk_level: RiskLevel


class WorktreeDiscardResponse(BaseModel):
    """Response from worktree_discard."""

    status: str = "discarded"


class WorktreeCommitResponse(BaseModel):
    """Response from worktree_commit."""

    commit: str
    files_committed: int
