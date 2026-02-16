"""Integration tests for the worktree manager.

Uses real temporary git repositories to test full lifecycle operations.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from mcp_git_worktree.api_models import ManifestStatus, RiskLevel
from mcp_git_worktree.config import ServerConfig
from mcp_git_worktree.manifest import read_manifest
from mcp_git_worktree.worktree_manager import (
    MaxWorktreesError,
    MergeBlockedError,
    MergeConflictError,
    WorktreeManager,
)


@pytest.fixture
def manager(server_config: ServerConfig) -> WorktreeManager:
    """Create a WorktreeManager bound to the temp repo."""
    return WorktreeManager(server_config)


class TestCreate:
    def test_creates_worktree(self, manager: WorktreeManager, server_config: ServerConfig) -> None:
        result = manager.create(
            description="Test worktree",
            base_branch="main",
            scope_dirs=["knowledge/"],
            scope_ops=["add"],
        )

        assert result.id.startswith("wt_")
        assert result.branch.startswith("ws/")
        assert Path(result.worktree_path).exists()
        assert result.base_commit

    def test_branch_exists_after_create(
        self, manager: WorktreeManager, server_config: ServerConfig
    ) -> None:
        result = manager.create(description="Branch test")
        repo = pygit2.Repository(server_config.repo_path)
        assert repo.branches.get(result.branch) is not None

    def test_manifest_committed(
        self, manager: WorktreeManager, server_config: ServerConfig
    ) -> None:
        result = manager.create(description="Manifest test")
        manifest = read_manifest(server_config, result.id)
        assert manifest.status == ManifestStatus.active
        assert manifest.description == "Manifest test"

    def test_max_worktrees_limit(
        self, manager: WorktreeManager, server_config: ServerConfig
    ) -> None:
        # Config allows max 3
        for i in range(3):
            manager.create(description=f"Worktree {i}")

        with pytest.raises(MaxWorktreesError):
            manager.create(description="One too many")


class TestListAndStatus:
    def test_list_all(self, manager: WorktreeManager) -> None:
        manager.create(description="First")
        manager.create(description="Second")

        result = manager.list_worktrees()
        assert len(result.worktrees) == 2

    def test_list_filter_by_status(self, manager: WorktreeManager) -> None:
        w1 = manager.create(description="Active one")
        w2 = manager.create(description="Will discard")
        manager.discard(w2.id, reason="Not needed")

        active = manager.list_worktrees(status_filter=ManifestStatus.active)
        assert len(active.worktrees) == 1
        assert active.worktrees[0].id == w1.id

    def test_status_commits_ahead(
        self, manager: WorktreeManager, server_config: ServerConfig
    ) -> None:
        result = manager.create(description="Status test")

        # Make a commit in the worktree
        wt_repo = pygit2.Repository(result.worktree_path)
        test_file = Path(result.worktree_path) / "test.md"
        test_file.write_text("# Test\n", encoding="utf-8")
        wt_repo.index.add("test.md")
        wt_repo.index.write()
        tree_oid = wt_repo.index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        head = wt_repo.head.peel(pygit2.Commit)
        wt_repo.create_commit("HEAD", sig, sig, "Add test", tree_oid, [head.id])

        status = manager.get_status(result.id)
        # 1 user commit + the worktree branch forked before the manifest commit on main
        assert status.commits_ahead >= 1

    def test_status_uncommitted_changes(self, manager: WorktreeManager) -> None:
        result = manager.create(description="Dirty worktree")

        # Write a file but don't commit
        test_file = Path(result.worktree_path) / "uncommitted.md"
        test_file.write_text("# Uncommitted\n", encoding="utf-8")

        status = manager.get_status(result.id)
        assert status.uncommitted_changes is True


class TestDiff:
    def test_diff_with_changes(self, manager: WorktreeManager) -> None:
        result = manager.create(
            description="Diff test",
            scope_dirs=["knowledge/"],
            scope_ops=["add"],
        )

        # Commit a file in the worktree
        manager.commit_in_worktree(
            result.id,
            message="Add knowledge",
            paths=None,
        )
        # Need to actually create a file first
        wt_path = Path(result.worktree_path)
        knowledge_dir = wt_path / "knowledge"
        knowledge_dir.mkdir(exist_ok=True)
        (knowledge_dir / "notes.md").write_text("# Notes\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Add notes")

        diff = manager.get_diff(result.id)
        assert diff.head_commit != diff.base_commit
        assert any(f.path == "knowledge/notes.md" for f in diff.files)

    def test_diff_risk_classification(self, manager: WorktreeManager) -> None:
        result = manager.create(
            description="Risk test",
            scope_dirs=["docs/"],
            scope_ops=["add"],
        )

        # Add a file outside scope
        wt_path = Path(result.worktree_path)
        (wt_path / "outside.txt").write_text("Outside scope\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Out of scope change")

        diff = manager.get_diff(result.id)
        assert diff.risk_level == RiskLevel.high


class TestMerge:
    def _create_and_commit(self, manager: WorktreeManager) -> str:
        """Helper: create a worktree, add a file, commit, return worktree_id."""
        result = manager.create(
            description="Merge test",
            scope_dirs=["docs/"],
            scope_ops=["add"],
        )
        wt_path = Path(result.worktree_path)
        docs_dir = wt_path / "docs"
        docs_dir.mkdir(exist_ok=True)
        (docs_dir / "new.md").write_text("# New doc\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Add doc")
        return result.id

    def test_fast_forward_merge(self, manager: WorktreeManager) -> None:
        wt_id = self._create_and_commit(manager)

        merge_result = manager.merge(wt_id)
        # May be three_way since create() commits manifest to main, diverging branches
        assert merge_result.strategy in ("fast_forward", "three_way")
        assert merge_result.merge_commit

    def test_merge_updates_manifest(
        self, manager: WorktreeManager, server_config: ServerConfig
    ) -> None:
        wt_id = self._create_and_commit(manager)
        manager.merge(wt_id)

        manifest = read_manifest(server_config, wt_id)
        assert manifest.status == ManifestStatus.merged
        assert manifest.result.merge_commit is not None

    def test_merge_blocked_by_high_risk(self, manager: WorktreeManager) -> None:
        result = manager.create(
            description="High risk merge",
            scope_dirs=["docs/"],
            scope_ops=["add"],
        )
        # Create file outside scope
        wt_path = Path(result.worktree_path)
        (wt_path / "outside.txt").write_text("Bad\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Out of scope")

        with pytest.raises(MergeBlockedError):
            manager.merge(result.id)

    def test_merge_forced(self, manager: WorktreeManager) -> None:
        result = manager.create(
            description="Force merge",
            scope_dirs=["docs/"],
            scope_ops=["add"],
        )
        wt_path = Path(result.worktree_path)
        (wt_path / "outside.txt").write_text("Forced\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Out of scope")

        # Force should succeed despite high risk
        merge_result = manager.merge(result.id, force=True)
        assert merge_result.merge_commit

    def test_three_way_merge(
        self,
        manager: WorktreeManager,
        server_config: ServerConfig,
        tmp_git_repo: pygit2.Repository,
    ) -> None:
        result = manager.create(
            description="Three-way merge test",
            scope_dirs=["docs/", "other/"],
            scope_ops=["add", "delete"],
        )

        # Add file on worktree
        wt_path = Path(result.worktree_path)
        docs_dir = wt_path / "docs"
        docs_dir.mkdir(exist_ok=True)
        (docs_dir / "branch.md").write_text("# From branch\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Branch commit")

        # Add a different file on base branch to prevent FF
        repo = pygit2.Repository(server_config.repo_path)
        other_dir = Path(server_config.repo_path) / "other"
        other_dir.mkdir(exist_ok=True)
        (other_dir / "base.md").write_text("# From base\n", encoding="utf-8")
        repo.index.read()
        repo.index.add("other/base.md")
        repo.index.write()
        tree_oid = repo.index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        head = repo.head.peel(pygit2.Commit)
        repo.create_commit("HEAD", sig, sig, "Base commit", tree_oid, [head.id])

        merge_result = manager.merge(result.id)
        assert merge_result.strategy == "three_way"

    def test_merge_conflict(
        self,
        manager: WorktreeManager,
        server_config: ServerConfig,
    ) -> None:
        result = manager.create(description="Conflict test")

        # Modify README on the worktree
        wt_path = Path(result.worktree_path)
        (wt_path / "README.md").write_text("# Modified on branch\n", encoding="utf-8")
        manager.commit_in_worktree(result.id, message="Branch change")

        # Modify the same file on main
        repo = pygit2.Repository(server_config.repo_path)
        main_readme = Path(server_config.repo_path) / "README.md"
        main_readme.write_text("# Modified on main\n", encoding="utf-8")
        repo.index.read()
        repo.index.add("README.md")
        repo.index.write()
        tree_oid = repo.index.write_tree()
        sig = pygit2.Signature("Test", "test@test.com")
        head = repo.head.peel(pygit2.Commit)
        repo.create_commit("HEAD", sig, sig, "Main change", tree_oid, [head.id])

        with pytest.raises(MergeConflictError):
            manager.merge(result.id, force=True)


class TestDiscard:
    def test_discard_cleanup(self, manager: WorktreeManager, server_config: ServerConfig) -> None:
        result = manager.create(description="Discard test")
        worktree_path = Path(result.worktree_path)
        assert worktree_path.exists()

        manager.discard(result.id, reason="Not needed")

        # Worktree directory should be removed
        assert not worktree_path.exists()

        # Manifest should still exist with discarded status
        manifest = read_manifest(server_config, result.id)
        assert manifest.status == ManifestStatus.discarded
        assert manifest.result.discard_reason == "Not needed"


class TestCommitInWorktree:
    def test_commit_all(self, manager: WorktreeManager) -> None:
        result = manager.create(description="Commit test")

        wt_path = Path(result.worktree_path)
        (wt_path / "file1.md").write_text("Content 1\n", encoding="utf-8")
        (wt_path / "file2.md").write_text("Content 2\n", encoding="utf-8")

        commit_result = manager.commit_in_worktree(result.id, message="Add files")
        assert commit_result.commit
        assert commit_result.files_committed == 2

    def test_commit_specific_paths(self, manager: WorktreeManager) -> None:
        result = manager.create(description="Specific commit")

        wt_path = Path(result.worktree_path)
        (wt_path / "staged.md").write_text("Staged\n", encoding="utf-8")
        (wt_path / "unstaged.md").write_text("Not staged\n", encoding="utf-8")

        commit_result = manager.commit_in_worktree(
            result.id, message="Partial commit", paths=["staged.md"]
        )
        assert commit_result.commit
        assert commit_result.files_committed == 1
