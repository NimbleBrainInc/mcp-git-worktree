"""Deterministic, rule-based risk classification for worktree diffs.

Pure function, no git or IO operations. Evaluates a diff against a declared
scope and returns a risk assessment.

Rules from spec Section 16.7.1:
- LOW when ALL: no conflicts, all files in scope, all ops match scope, <500 lines, no binaries
- HIGH when ANY: conflicts, out-of-scope files, unexpected ops, >500 lines, no scope declared
"""

from __future__ import annotations

from mcp_git_worktree.api_models import (
    DiffFileEntry,
    RiskAssessment,
    RiskLevel,
    ScopeDeclaration,
)

MAX_LINES_THRESHOLD = 500


def classify_risk(
    files: list[DiffFileEntry],
    scope: ScopeDeclaration,
    has_conflicts: bool = False,
    has_binary_changes: bool = False,
) -> RiskAssessment:
    """Classify the risk level of a worktree diff.

    Args:
        files: List of file changes in the diff.
        scope: The declared scope (directories and operations).
        has_conflicts: Whether merge conflicts were detected.
        has_binary_changes: Whether any binary files were changed.

    Returns:
        A RiskAssessment with the computed level and reasons.
    """
    reasons: list[str] = []
    scope_violations: list[str] = []
    conflict_files: list[str] = []

    # Rule: no scope declared -> HIGH
    if not scope.directories and not scope.operations:
        reasons.append("No scope was declared")
        return RiskAssessment(
            level=RiskLevel.high,
            reasons=reasons,
            scope_violations=scope_violations,
            conflict_files=conflict_files,
        )

    # Rule: conflicts -> HIGH
    if has_conflicts:
        reasons.append("Merge conflicts detected")

    # Rule: binary changes -> HIGH
    if has_binary_changes:
        reasons.append("Binary files changed")

    # Rule: scope directory violations
    if scope.directories:
        for f in files:
            if not _file_in_scope_dirs(f.path, scope.directories):
                scope_violations.append(f.path)
        if scope_violations:
            reasons.append(f"Files changed outside declared scope: {', '.join(scope_violations)}")

    # Rule: operation type mismatches
    if scope.operations:
        _STATUS_TO_OP = {
            "added": "add",
            "modified": "modify",
            "deleted": "delete",
            "renamed": "rename",
        }
        unexpected_ops: list[str] = []
        for f in files:
            op = _STATUS_TO_OP.get(f.status)
            if op and op not in scope.operations:
                unexpected_ops.append(f"{f.path} ({f.status})")
        if unexpected_ops:
            reasons.append(f"Operations don't match declared scope: {', '.join(unexpected_ops)}")

    # Rule: diff exceeds line threshold
    total_lines = sum(f.lines_added + f.lines_removed for f in files)
    if total_lines > MAX_LINES_THRESHOLD:
        reasons.append(f"Diff exceeds {MAX_LINES_THRESHOLD} lines ({total_lines} total)")

    level = RiskLevel.high if reasons else RiskLevel.low
    return RiskAssessment(
        level=level,
        reasons=reasons,
        scope_violations=scope_violations,
        conflict_files=conflict_files,
    )


def _file_in_scope_dirs(file_path: str, scope_dirs: list[str]) -> bool:
    """Check if a file path falls within any of the declared scope directories."""
    for scope_dir in scope_dirs:
        # Normalize: ensure scope dir ends with /
        normalized = scope_dir if scope_dir.endswith("/") else scope_dir + "/"
        if file_path.startswith(normalized) or file_path == scope_dir.rstrip("/"):
            return True
    return False
