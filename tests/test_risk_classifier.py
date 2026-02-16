"""Tests for the risk classifier."""

from __future__ import annotations

from mcp_git_worktree.api_models import DiffFileEntry, RiskLevel, ScopeDeclaration
from mcp_git_worktree.risk_classifier import classify_risk


def _file(
    path: str = "knowledge/doc.md",
    status: str = "added",
    lines_added: int = 10,
    lines_removed: int = 0,
) -> DiffFileEntry:
    """Helper to create a DiffFileEntry."""
    return DiffFileEntry(
        path=path,
        status=status,
        lines_added=lines_added,
        lines_removed=lines_removed,
    )


def _scope(
    directories: list[str] | None = None,
    operations: list[str] | None = None,
) -> ScopeDeclaration:
    """Helper to create a ScopeDeclaration."""
    return ScopeDeclaration(
        directories=directories or [],
        operations=operations or [],
    )


class TestLowRisk:
    def test_all_in_scope(self) -> None:
        files = [_file("knowledge/competitors/acme.md", "added", 50, 0)]
        scope = _scope(["knowledge/competitors/"], ["add"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.low
        assert result.reasons == []

    def test_multiple_files_in_scope(self) -> None:
        files = [
            _file("docs/a.md", "added", 20, 0),
            _file("docs/b.md", "modified", 10, 5),
        ]
        scope = _scope(["docs/"], ["add", "modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.low

    def test_under_line_threshold(self) -> None:
        files = [_file("src/main.py", "modified", 200, 100)]
        scope = _scope(["src/"], ["modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.low


class TestHighRiskNoScope:
    def test_empty_scope(self) -> None:
        files = [_file()]
        scope = _scope()

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high
        assert "No scope was declared" in result.reasons[0]


class TestHighRiskConflicts:
    def test_conflicts_detected(self) -> None:
        files = [_file("knowledge/doc.md", "modified", 10, 5)]
        scope = _scope(["knowledge/"], ["modify"])

        result = classify_risk(files, scope, has_conflicts=True)
        assert result.level == RiskLevel.high
        assert any("conflict" in r.lower() for r in result.reasons)


class TestHighRiskBinary:
    def test_binary_changes(self) -> None:
        files = [_file("assets/logo.png", "added", 0, 0)]
        scope = _scope(["assets/"], ["add"])

        result = classify_risk(files, scope, has_binary_changes=True)
        assert result.level == RiskLevel.high
        assert any("binary" in r.lower() for r in result.reasons)


class TestHighRiskScopeViolation:
    def test_file_outside_scope(self) -> None:
        files = [
            _file("knowledge/doc.md", "added", 10, 0),
            _file("system/config.yaml", "modified", 5, 2),
        ]
        scope = _scope(["knowledge/"], ["add", "modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high
        assert "system/config.yaml" in result.scope_violations

    def test_multiple_violations(self) -> None:
        files = [
            _file("knowledge/doc.md", "added", 10, 0),
            _file("brand/logo.svg", "modified", 5, 2),
            _file("system/queue.json", "modified", 1, 1),
        ]
        scope = _scope(["knowledge/"], ["add", "modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high
        assert len(result.scope_violations) == 2


class TestHighRiskOperationMismatch:
    def test_unexpected_delete(self) -> None:
        files = [
            _file("knowledge/old.md", "deleted", 0, 50),
        ]
        scope = _scope(["knowledge/"], ["add", "modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high
        assert any("don't match" in r.lower() for r in result.reasons)

    def test_unexpected_rename(self) -> None:
        files = [_file("docs/renamed.md", "renamed", 0, 0)]
        scope = _scope(["docs/"], ["add"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high


class TestHighRiskLargeDiff:
    def test_exceeds_threshold(self) -> None:
        files = [_file("src/big.py", "modified", 300, 250)]
        scope = _scope(["src/"], ["modify"])

        result = classify_risk(files, scope)
        assert result.level == RiskLevel.high
        assert any("500" in r for r in result.reasons)


class TestMultipleReasons:
    def test_accumulates_reasons(self) -> None:
        files = [
            _file("knowledge/doc.md", "added", 300, 0),
            _file("system/secret.yaml", "deleted", 0, 250),
        ]
        scope = _scope(["knowledge/"], ["add"])

        result = classify_risk(files, scope, has_conflicts=True, has_binary_changes=True)
        assert result.level == RiskLevel.high
        # Should have multiple reasons: conflicts, binary, scope violation,
        # op mismatch, and large diff
        assert len(result.reasons) >= 3
