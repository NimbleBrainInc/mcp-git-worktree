"""Core worktree lifecycle manager using pygit2.

Handles creation, listing, status, diff, merge, discard, and commit operations
for git worktrees. All state is persisted in manifest YAML files committed to
the base branch.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import pygit2

from mcp_git_worktree.api_models import (
    DiffFileEntry,
    DiffStats,
    ManifestStatus,
    RiskLevel,
    ScopeDeclaration,
    WorktreeCommitResponse,
    WorktreeCreateResponse,
    WorktreeDiffResponse,
    WorktreeDiscardResponse,
    WorktreeListEntry,
    WorktreeListResponse,
    WorktreeMergeResponse,
    WorktreeStatusResponse,
)
from mcp_git_worktree.config import ServerConfig
from mcp_git_worktree.manifest import (
    ManifestNotFoundError,
    create_manifest,
    generate_worktree_id,
    read_all_manifests,
    read_manifest,
    update_manifest,
)
from mcp_git_worktree.risk_classifier import classify_risk

logger = logging.getLogger(__name__)

# Secret scanning patterns (subset from agent's git_ops.py)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS Access Key ID", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub Token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}")),
    ("Slack Token", re.compile(r"xox[bprs]-[A-Za-z0-9\-]{10,}")),
]


class WorktreeError(Exception):
    """Base error for worktree operations."""


class MaxWorktreesError(WorktreeError):
    """Raised when the maximum number of active worktrees is reached."""


class MergeBlockedError(WorktreeError):
    """Raised when a merge is blocked by high risk assessment."""

    def __init__(self, risk_level: RiskLevel, reasons: list[str]) -> None:
        self.risk_level = risk_level
        self.reasons = reasons
        super().__init__(f"Merge blocked by {risk_level} risk: {'; '.join(reasons)}")


class MergeConflictError(WorktreeError):
    """Raised when a merge has conflicts."""

    def __init__(self, conflict_files: list[str]) -> None:
        self.conflict_files = conflict_files
        super().__init__(f"Merge conflicts in: {', '.join(conflict_files)}")


class SecretDetectedError(WorktreeError):
    """Raised when secrets are detected in staged changes."""


class WorktreeManager:
    """Manages the full lifecycle of git worktrees.

    Args:
        config: Server configuration with paths and limits.
    """

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._repo = pygit2.Repository(config.repo_path)

    def _signature(self) -> pygit2.Signature:
        return pygit2.Signature(self._config.author_name, self._config.author_email)

    def _commit_on_base(self, message: str) -> str:
        """Create a commit on the current branch (base) with all staged changes."""
        self._repo.index.read()
        tree_oid = self._repo.index.write_tree()
        sig = self._signature()

        try:
            head = self._repo.head.peel(pygit2.Commit)
            parents = [head.id]
        except pygit2.GitError:
            parents = []

        oid = self._repo.create_commit("HEAD", sig, sig, message, tree_oid, parents)
        return str(oid)

    def _stage_manifest(self, worktree_id: str) -> None:
        """Stage the manifest file for the given worktree ID."""
        rel_path = f"{self._config.manifest_dir}/{worktree_id}.yaml"
        self._repo.index.read()
        self._repo.index.add(rel_path)
        self._repo.index.write()

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    def create(
        self,
        description: str,
        base_branch: str = "main",
        scope_dirs: list[str] | None = None,
        scope_ops: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorktreeCreateResponse:
        """Create a new worktree with its own branch.

        Args:
            description: Human-readable description.
            base_branch: Branch to fork from.
            scope_dirs: Expected directories to be modified.
            scope_ops: Expected operation types.
            metadata: Arbitrary caller metadata.

        Returns:
            WorktreeCreateResponse with the new worktree details.

        Raises:
            MaxWorktreesError: If the active worktree limit is reached.
        """
        # Check limit
        active = read_all_manifests(self._config, status_filter=ManifestStatus.active)
        if len(active) >= self._config.max_worktrees:
            raise MaxWorktreesError(
                f"Maximum {self._config.max_worktrees} active worktrees reached"
            )

        worktree_id = generate_worktree_id()
        branch_name = f"{self._config.branch_prefix}{worktree_id}"
        worktree_path = str(Path(self._config.workspaces_path) / worktree_id)

        # Resolve base branch to commit
        base_ref = self._repo.branches.get(base_branch)
        if base_ref is None:
            raise WorktreeError(f"Base branch not found: {base_branch}")
        base_commit = base_ref.peel(pygit2.Commit)
        base_commit_hex = str(base_commit.id)

        # Create branch from base
        self._repo.branches.create(branch_name, base_commit)

        # Create worktree
        self._repo.add_worktree(worktree_id, worktree_path, self._repo.branches[branch_name])

        # Write and commit manifest
        scope = ScopeDeclaration(
            directories=scope_dirs or [],
            operations=scope_ops or [],
        )
        create_manifest(
            config=self._config,
            worktree_id=worktree_id,
            description=description,
            base_branch=base_branch,
            base_commit=base_commit_hex,
            worktree_path=worktree_path,
            scope=scope,
            metadata=metadata,
        )

        self._stage_manifest(worktree_id)
        self._commit_on_base(f"worktree: create {worktree_id}")

        return WorktreeCreateResponse(
            id=worktree_id,
            branch=branch_name,
            worktree_path=worktree_path,
            base_commit=base_commit_hex,
        )

    # -----------------------------------------------------------------------
    # List
    # -----------------------------------------------------------------------

    def list_worktrees(self, status_filter: ManifestStatus | None = None) -> WorktreeListResponse:
        """List all tracked worktrees."""
        manifests = read_all_manifests(self._config, status_filter=status_filter)
        entries: list[WorktreeListEntry] = []

        for m in manifests:
            commits_ahead = self._count_commits_ahead(m.branch, m.base_branch)
            entries.append(
                WorktreeListEntry(
                    id=m.id,
                    status=m.status,
                    description=m.description,
                    branch=m.branch,
                    base_branch=m.base_branch,
                    created_at=m.created_at,
                    commits_ahead=commits_ahead,
                )
            )

        return WorktreeListResponse(worktrees=entries)

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def get_status(self, worktree_id: str) -> WorktreeStatusResponse:
        """Get detailed status of a single worktree."""
        manifest = read_manifest(self._config, worktree_id)

        commits_ahead = self._count_commits_ahead(manifest.branch, manifest.base_branch)
        uncommitted = self._has_uncommitted_changes(manifest.worktree_path)
        diff_stats = self._compute_diff_stats(manifest.branch, manifest.base_branch)
        can_ff = self._can_fast_forward(manifest.branch, manifest.base_branch)

        return WorktreeStatusResponse(
            manifest=manifest,
            commits_ahead=commits_ahead,
            uncommitted_changes=uncommitted,
            diff_stats=diff_stats,
            can_fast_forward=can_ff,
        )

    # -----------------------------------------------------------------------
    # Diff
    # -----------------------------------------------------------------------

    def get_diff(self, worktree_id: str) -> WorktreeDiffResponse:
        """Get the full diff with risk classification."""
        manifest = read_manifest(self._config, worktree_id)

        base_commit = self._resolve_to_commit(manifest.base_branch)
        head_commit = self._resolve_to_commit(manifest.branch)

        diff_obj = base_commit.tree.diff_to_tree(head_commit.tree)

        # Manifest dir prefix to filter out server-managed files from risk assessment
        manifest_prefix = self._config.manifest_dir.rstrip("/") + "/"

        files: list[DiffFileEntry] = []
        has_binary = False
        for patch in diff_obj:
            if patch is None:
                continue
            delta = patch.delta

            file_path = delta.new_file.path if delta.new_file.path else delta.old_file.path

            # Skip manifest files managed by this server
            if file_path.startswith(manifest_prefix):
                continue

            status = self._delta_status_name(delta.status)

            if delta.flags & pygit2.GIT_DIFF_FLAG_BINARY:
                has_binary = True

            lines_added = 0
            lines_removed = 0
            for hunk in patch.hunks:
                for line in hunk.lines:
                    if line.origin == "+":
                        lines_added += 1
                    elif line.origin == "-":
                        lines_removed += 1

            in_scope = self._file_in_scope(file_path, manifest.scope)

            files.append(
                DiffFileEntry(
                    path=file_path,
                    status=status,
                    lines_added=lines_added,
                    lines_removed=lines_removed,
                    in_scope=in_scope,
                )
            )

        # Check for conflicts
        has_conflicts, conflict_files = self._check_conflicts(manifest.branch, manifest.base_branch)

        risk = classify_risk(
            files=files,
            scope=manifest.scope,
            has_conflicts=has_conflicts,
            has_binary_changes=has_binary,
        )
        risk.conflict_files = conflict_files

        return WorktreeDiffResponse(
            base_commit=str(base_commit.id),
            head_commit=str(head_commit.id),
            risk_level=risk.level,
            risk_reasons=risk.reasons,
            files=files,
        )

    # -----------------------------------------------------------------------
    # Merge
    # -----------------------------------------------------------------------

    def merge(
        self,
        worktree_id: str,
        message: str | None = None,
        force: bool = False,
    ) -> WorktreeMergeResponse:
        """Merge the worktree branch into its base branch and clean up.

        Args:
            worktree_id: The worktree to merge.
            message: Optional merge commit message.
            force: Skip risk check if True.

        Raises:
            MergeBlockedError: If risk is HIGH and force is False.
            MergeConflictError: If the merge has conflicts.
        """
        manifest = read_manifest(self._config, worktree_id)

        # Risk check unless forced
        if not force:
            diff_resp = self.get_diff(worktree_id)
            if diff_resp.risk_level == RiskLevel.high:
                raise MergeBlockedError(diff_resp.risk_level, diff_resp.risk_reasons)

        branch_name = manifest.branch
        base_branch = manifest.base_branch

        # Checkout base branch
        base_ref = self._repo.branches.get(base_branch)
        if base_ref is None:
            raise WorktreeError(f"Base branch not found: {base_branch}")
        self._repo.checkout(self._repo.lookup_reference(base_ref.name))

        source_commit = self._resolve_to_commit(branch_name)
        analysis, _ = self._repo.merge_analysis(source_commit.id)

        merge_commit_hex: str
        strategy: str

        if analysis & pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE:
            merge_commit_hex = str(self._repo.head.peel(pygit2.Commit).id)
            strategy = "fast_forward"

        elif analysis & pygit2.GIT_MERGE_ANALYSIS_FASTFORWARD:
            current_ref = self._repo.head.name
            self._repo.references.create(current_ref, source_commit.id, force=True)
            self._repo.checkout_head(  # type: ignore[no-untyped-call]
                strategy=pygit2.GIT_CHECKOUT_FORCE | pygit2.GIT_CHECKOUT_RECREATE_MISSING
            )
            merge_commit_hex = str(source_commit.id)
            strategy = "fast_forward"

        else:
            # Three-way merge
            self._repo.merge(source_commit.id)

            if self._repo.index.conflicts:
                conflict_paths: list[str] = []
                for conflict in self._repo.index.conflicts:
                    for entry in conflict:
                        if entry is not None:
                            conflict_paths.append(entry.path)
                conflict_paths = sorted(set(conflict_paths))
                self._repo.state_cleanup()
                raise MergeConflictError(conflict_paths)

            tree_oid = self._repo.index.write_tree()
            self._repo.index.write()

            sig = self._signature()
            head_commit = self._repo.head.peel(pygit2.Commit)

            if message is None:
                message = f"merge(ws/{worktree_id}): {manifest.description}"

            oid = self._repo.create_commit(
                "HEAD",
                sig,
                sig,
                message,
                tree_oid,
                [head_commit.id, source_commit.id],
            )
            self._repo.state_cleanup()
            merge_commit_hex = str(oid)
            strategy = "three_way"

        # Compute diff stats for result before cleanup
        diff_stats = self._compute_diff_stats(branch_name, base_branch)
        files_merged = diff_stats.files_added + diff_stats.files_modified + diff_stats.files_deleted

        # Update manifest to merged state
        manifest.status = ManifestStatus.merged
        manifest.result.merge_commit = merge_commit_hex
        manifest.result.head_commit = str(source_commit.id)
        manifest.result.files_added = diff_stats.files_added
        manifest.result.files_modified = diff_stats.files_modified
        manifest.result.files_deleted = diff_stats.files_deleted
        manifest.result.lines_added = diff_stats.lines_added
        manifest.result.lines_removed = diff_stats.lines_removed
        manifest.result.risk_level = RiskLevel.low if not force else RiskLevel.high
        update_manifest(self._config, manifest)

        self._stage_manifest(worktree_id)
        self._commit_on_base(f"worktree: merge {worktree_id}")

        # Cleanup worktree and branch
        self._cleanup_worktree(worktree_id, branch_name)

        return WorktreeMergeResponse(
            merge_commit=merge_commit_hex,
            strategy=strategy,
            files_merged=files_merged,
            risk_level=manifest.result.risk_level,
        )

    # -----------------------------------------------------------------------
    # Discard
    # -----------------------------------------------------------------------

    def discard(self, worktree_id: str, reason: str) -> WorktreeDiscardResponse:
        """Discard a worktree and its branch without merging."""
        manifest = read_manifest(self._config, worktree_id)

        manifest.status = ManifestStatus.discarded
        manifest.result.discard_reason = reason
        update_manifest(self._config, manifest)

        self._stage_manifest(worktree_id)
        self._commit_on_base(f"worktree: discard {worktree_id}")

        self._cleanup_worktree(worktree_id, manifest.branch)

        return WorktreeDiscardResponse(status="discarded")

    # -----------------------------------------------------------------------
    # Commit in worktree
    # -----------------------------------------------------------------------

    def commit_in_worktree(
        self,
        worktree_id: str,
        message: str,
        paths: list[str] | None = None,
    ) -> WorktreeCommitResponse:
        """Commit changes in a specific worktree.

        Args:
            worktree_id: The worktree to commit in.
            message: Commit message.
            paths: Files to stage. If empty/None, stages all changes.

        Raises:
            SecretDetectedError: If secrets are found in staged changes.
        """
        manifest = read_manifest(self._config, worktree_id)
        wt_repo = pygit2.Repository(manifest.worktree_path)

        wt_repo.index.read()

        if paths:
            for p in paths:
                wt_repo.index.add(p)
        else:
            wt_repo.index.add_all()

        wt_repo.index.write()

        # Secret scan
        self._scan_secrets_in_repo(wt_repo)

        tree_oid = wt_repo.index.write_tree()
        sig = self._signature()

        try:
            head = wt_repo.head.peel(pygit2.Commit)
            parents = [head.id]
        except pygit2.GitError:
            parents = []

        oid = wt_repo.create_commit("HEAD", sig, sig, message, tree_oid, parents)

        # Count committed files
        if parents:
            parent_commit = wt_repo.get(parents[0])
            if parent_commit is not None:
                parent_tree = parent_commit.peel(pygit2.Tree)
                new_tree = wt_repo.get(tree_oid)
                diff = parent_tree.diff_to_tree(new_tree)  # type: ignore[arg-type]
                files_committed = len([p for p in diff if p is not None])
            else:
                files_committed = 0
        else:
            files_committed = len(list(wt_repo.index))

        return WorktreeCommitResponse(
            commit=str(oid),
            files_committed=files_committed,
        )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _resolve_to_commit(self, ref: str) -> pygit2.Commit:
        """Resolve a branch name or OID to a commit."""
        branch = self._repo.branches.get(ref)
        if branch is not None:
            return branch.peel(pygit2.Commit)
        try:
            obj = self._repo.get(pygit2.Oid(hex=ref))
            if obj is not None:
                if isinstance(obj, pygit2.Commit):
                    return obj
                return obj.peel(pygit2.Commit)
        except (ValueError, pygit2.GitError):
            pass
        raise WorktreeError(f"Cannot resolve ref: {ref}")

    def _count_commits_ahead(self, branch: str, base_branch: str) -> int:
        """Count commits on branch that are not on base_branch."""
        try:
            branch_commit = self._resolve_to_commit(branch)
            base_commit = self._resolve_to_commit(base_branch)
        except (WorktreeError, ManifestNotFoundError):
            return 0

        count = 0
        for commit in self._repo.walk(branch_commit.id, pygit2.GIT_SORT_TIME):  # type: ignore[arg-type]
            if commit.id == base_commit.id:
                break
            count += 1
        return count

    def _has_uncommitted_changes(self, worktree_path: str) -> bool:
        """Check if a worktree has uncommitted changes."""
        if not Path(worktree_path).exists():
            return False
        try:
            wt_repo = pygit2.Repository(worktree_path)
            status = wt_repo.status()
            for _path, flags in status.items():
                if flags not in (pygit2.GIT_STATUS_IGNORED, pygit2.GIT_STATUS_CURRENT):
                    return True
        except pygit2.GitError:
            return False
        return False

    def _compute_diff_stats(self, branch: str, base_branch: str) -> DiffStats:
        """Compute aggregate diff statistics between two branches."""
        try:
            base_commit = self._resolve_to_commit(base_branch)
            head_commit = self._resolve_to_commit(branch)
        except WorktreeError:
            return DiffStats()

        diff_obj = base_commit.tree.diff_to_tree(head_commit.tree)
        manifest_prefix = self._config.manifest_dir.rstrip("/") + "/"

        stats = DiffStats()
        for patch in diff_obj:
            if patch is None:
                continue
            delta = patch.delta
            file_path = delta.new_file.path if delta.new_file.path else delta.old_file.path
            if file_path.startswith(manifest_prefix):
                continue
            if delta.status == pygit2.GIT_DELTA_ADDED:
                stats.files_added += 1
            elif delta.status == pygit2.GIT_DELTA_MODIFIED:
                stats.files_modified += 1
            elif delta.status == pygit2.GIT_DELTA_DELETED:
                stats.files_deleted += 1

            for hunk in patch.hunks:
                for line in hunk.lines:
                    if line.origin == "+":
                        stats.lines_added += 1
                    elif line.origin == "-":
                        stats.lines_removed += 1

        return stats

    def _can_fast_forward(self, branch: str, base_branch: str) -> bool:
        """Check if a fast-forward merge is possible."""
        try:
            source_commit = self._resolve_to_commit(branch)
            analysis, _ = self._repo.merge_analysis(source_commit.id)
            return bool(
                analysis & pygit2.GIT_MERGE_ANALYSIS_FASTFORWARD
                or analysis & pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE
            )
        except (WorktreeError, pygit2.GitError):
            return False

    def _check_conflicts(self, branch: str, base_branch: str) -> tuple[bool, list[str]]:
        """Check if merging branch into base_branch would cause conflicts.

        Does not actually perform the merge. Uses merge_analysis and a dry-run.
        """
        try:
            source_commit = self._resolve_to_commit(branch)
            base_commit = self._resolve_to_commit(base_branch)
            analysis, _ = self._repo.merge_analysis(source_commit.id)

            if analysis & (
                pygit2.GIT_MERGE_ANALYSIS_FASTFORWARD | pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE
            ):
                return False, []

            # Perform merge in-memory to check for conflicts
            merge_index = self._repo.merge_commits(base_commit, source_commit)
            if merge_index.conflicts:
                conflict_paths: list[str] = []
                for conflict in merge_index.conflicts:
                    for entry in conflict:
                        if entry is not None:
                            conflict_paths.append(entry.path)
                return True, sorted(set(conflict_paths))
            return False, []
        except (WorktreeError, pygit2.GitError):
            return False, []

    def _file_in_scope(self, file_path: str, scope: ScopeDeclaration) -> bool:
        """Check if a file is within the declared scope directories."""
        if not scope.directories:
            return True  # No scope = everything is "in scope" for display
        for scope_dir in scope.directories:
            normalized = scope_dir if scope_dir.endswith("/") else scope_dir + "/"
            if file_path.startswith(normalized):
                return True
        return False

    @staticmethod
    def _delta_status_name(status: int) -> str:
        """Map pygit2 delta status to human-readable name."""
        return {
            pygit2.GIT_DELTA_ADDED: "added",
            pygit2.GIT_DELTA_DELETED: "deleted",
            pygit2.GIT_DELTA_MODIFIED: "modified",
            pygit2.GIT_DELTA_RENAMED: "renamed",
        }.get(status, "modified")

    def _cleanup_worktree(self, worktree_id: str, branch_name: str) -> None:
        """Remove the worktree directory and delete the branch."""
        worktree_path = Path(self._config.workspaces_path) / worktree_id

        # Prune the worktree from git
        try:
            wt = self._repo.lookup_worktree(worktree_id)
            if wt is not None:
                wt.prune(True)
        except (pygit2.GitError, AttributeError):
            pass

        # Remove the worktree directory
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

        # Delete the branch
        branch = self._repo.branches.get(branch_name)
        if branch is not None and not branch.is_checked_out():
            branch.delete()

    def _scan_secrets_in_repo(self, repo: pygit2.Repository) -> None:
        """Scan staged changes for secrets. Raises SecretDetectedError if found."""
        try:
            head_tree = repo.head.peel(pygit2.Tree)
        except pygit2.GitError:
            head_tree = None

        repo.index.read()
        if head_tree is not None:
            diff = repo.index.diff_to_tree(head_tree)
        else:
            diff = repo.index.diff_to_tree()

        for patch in diff:
            file_path = patch.delta.new_file.path
            if patch.delta.status == pygit2.GIT_DELTA_DELETED:
                continue

            try:
                full_path = Path(repo.workdir) / file_path
                content = full_path.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError):
                continue

            for pattern_name, regex in _SECRET_PATTERNS:
                if regex.search(content):
                    raise SecretDetectedError(f"Secret detected ({pattern_name}) in {file_path}")
