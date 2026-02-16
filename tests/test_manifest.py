"""Tests for the manifest manager."""

from __future__ import annotations

import pytest

from mcp_git_worktree.api_models import ManifestStatus, ScopeDeclaration
from mcp_git_worktree.config import ServerConfig
from mcp_git_worktree.manifest import (
    ManifestNotFoundError,
    create_manifest,
    generate_worktree_id,
    read_all_manifests,
    read_manifest,
    update_manifest,
)


class TestGenerateWorktreeId:
    def test_format(self) -> None:
        wt_id = generate_worktree_id()
        assert wt_id.startswith("wt_")
        assert len(wt_id) > 5

    def test_unique(self) -> None:
        ids = {generate_worktree_id() for _ in range(100)}
        assert len(ids) == 100


class TestCreateManifest:
    def test_creates_manifest_file(self, server_config: ServerConfig) -> None:
        manifest = create_manifest(
            config=server_config,
            worktree_id="wt_TEST001",
            description="Test worktree",
            base_branch="main",
            base_commit="abc123",
            worktree_path="/workspaces/wt_TEST001",
        )

        assert manifest.id == "wt_TEST001"
        assert manifest.status == ManifestStatus.active
        assert manifest.description == "Test worktree"
        assert manifest.branch == "ws/wt_TEST001"
        assert manifest.base_branch == "main"
        assert manifest.base_commit == "abc123"

    def test_with_scope_and_metadata(self, server_config: ServerConfig) -> None:
        scope = ScopeDeclaration(
            directories=["knowledge/competitors/"],
            operations=["add", "modify"],
        )
        manifest = create_manifest(
            config=server_config,
            worktree_id="wt_TEST002",
            description="Scoped worktree",
            base_branch="main",
            base_commit="def456",
            worktree_path="/workspaces/wt_TEST002",
            scope=scope,
            metadata={"nb_task_id": "task_001"},
        )

        assert manifest.scope.directories == ["knowledge/competitors/"]
        assert manifest.scope.operations == ["add", "modify"]
        assert manifest.metadata["nb_task_id"] == "task_001"


class TestReadManifest:
    def test_read_existing(self, server_config: ServerConfig) -> None:
        create_manifest(
            config=server_config,
            worktree_id="wt_READ001",
            description="Readable",
            base_branch="main",
            base_commit="aaa",
            worktree_path="/workspaces/wt_READ001",
        )

        loaded = read_manifest(server_config, "wt_READ001")
        assert loaded.id == "wt_READ001"
        assert loaded.description == "Readable"

    def test_missing_manifest(self, server_config: ServerConfig) -> None:
        with pytest.raises(ManifestNotFoundError):
            read_manifest(server_config, "wt_NONEXISTENT")


class TestReadAllManifests:
    def test_empty_directory(self, server_config: ServerConfig) -> None:
        manifests = read_all_manifests(server_config)
        assert manifests == []

    def test_multiple_manifests(self, server_config: ServerConfig) -> None:
        for i in range(3):
            create_manifest(
                config=server_config,
                worktree_id=f"wt_ALL{i:03d}",
                description=f"Worktree {i}",
                base_branch="main",
                base_commit="abc",
                worktree_path=f"/workspaces/wt_ALL{i:03d}",
            )

        manifests = read_all_manifests(server_config)
        assert len(manifests) == 3

    def test_status_filter(self, server_config: ServerConfig) -> None:
        create_manifest(
            config=server_config,
            worktree_id="wt_FILT001",
            description="Active one",
            base_branch="main",
            base_commit="abc",
            worktree_path="/workspaces/wt_FILT001",
        )

        m2 = create_manifest(
            config=server_config,
            worktree_id="wt_FILT002",
            description="To be merged",
            base_branch="main",
            base_commit="def",
            worktree_path="/workspaces/wt_FILT002",
        )
        m2.status = ManifestStatus.merged
        update_manifest(server_config, m2)

        active = read_all_manifests(server_config, status_filter=ManifestStatus.active)
        assert len(active) == 1
        assert active[0].id == "wt_FILT001"

        merged = read_all_manifests(server_config, status_filter=ManifestStatus.merged)
        assert len(merged) == 1
        assert merged[0].id == "wt_FILT002"


class TestUpdateManifest:
    def test_updates_status(self, server_config: ServerConfig) -> None:
        manifest = create_manifest(
            config=server_config,
            worktree_id="wt_UPD001",
            description="Updatable",
            base_branch="main",
            base_commit="abc",
            worktree_path="/workspaces/wt_UPD001",
        )
        original_updated_at = manifest.updated_at

        manifest.status = ManifestStatus.completed
        update_manifest(server_config, manifest)

        loaded = read_manifest(server_config, "wt_UPD001")
        assert loaded.status == ManifestStatus.completed
        assert loaded.updated_at >= original_updated_at

    def test_updates_result(self, server_config: ServerConfig) -> None:
        manifest = create_manifest(
            config=server_config,
            worktree_id="wt_UPD002",
            description="With result",
            base_branch="main",
            base_commit="abc",
            worktree_path="/workspaces/wt_UPD002",
        )

        manifest.result.head_commit = "new_commit_oid"
        manifest.result.files_added = 5
        manifest.result.lines_added = 120
        update_manifest(server_config, manifest)

        loaded = read_manifest(server_config, "wt_UPD002")
        assert loaded.result.head_commit == "new_commit_oid"
        assert loaded.result.files_added == 5
        assert loaded.result.lines_added == 120
