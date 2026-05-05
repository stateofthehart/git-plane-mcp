"""Git + Plane MCP Server.

Schema-enforced commits with automatic Plane issue tracking.
This server provides git operations ONLY. For project management (creating
issues, managing sprints, labels, etc.), use the Plane MCP directly.

Tools:
    commit  - Validate message schema, stage files, git commit, sync to Plane
    push    - Push to remote
    status  - Git status summary
    log     - Git log with optional issue filtering

Why these tools exist:
    - commit: The core value — enforces commit message schema at the tool
      boundary and atomically syncs git commits to Plane issues. Neither
      raw git nor the Plane MCP can do this alone.
    - push/status/log: Standard git operations that agents need. No Plane
      overlap. Provided here so agents have one MCP for all code workflow.

What this server does NOT do (use Plane MCP instead):
    - Create/update/delete issues (use create_work_item)
    - Manage projects, cycles, modules, labels, states
    - Search work items, read comments, check activities
    - Any project management that isn't tied to a git commit
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from .git_ops import (
    add,
    checkout as git_checkout,
    commit as git_commit,
    construct_commit_url,
    create_branch as git_create_branch,
    current_branch,
    diff as git_diff,
    diff_staged,
    has_staged_changes,
    log as git_log,
    pull as git_pull,
    push as git_push,
    rev_parse_head,
    status as git_status,
)
from .plane_ops import PlaneClient
from .schema import parse_commit_message

logger = logging.getLogger(__name__)

mcp = FastMCP("mcp-git-plane")

# ---------------------------------------------------------------------------
# Plane client (initialized lazily on first use)
# ---------------------------------------------------------------------------

_plane: PlaneClient | None = None


def _get_plane() -> PlaneClient:
    global _plane
    if _plane is None:
        _plane = PlaneClient()
    return _plane


def _resolve_cwd(override: str | None = None) -> str:
    """Resolve the working directory for git operations.

    Precedence (highest to lowest):
    1. ``override`` — the per-call ``repo_path`` argument from a tool. Used
       when the server runs as an HTTP daemon serving multiple repos: the
       caller (or an upstream gateway-proxy injecting defaults via scope
       rules) names which repo each call targets.
    2. ``GIT_WORK_DIR`` env var — single-repo daemon mode (rare).
    3. ``os.getcwd()`` — stdio per-session mode (the original behavior).
    """
    if override:
        return override
    return os.environ.get("GIT_WORK_DIR", os.getcwd())


def _resolve_issue(project_prefix: str, sequence_id: int) -> tuple[str, str, dict] | None:
    """Resolve an issue ref like QFP-15 to (project_id, work_item_id, state_map).

    Returns None if the project or issue doesn't exist.
    """
    plane = _get_plane()
    project = plane.get_project_by_identifier(project_prefix)
    if not project:
        return None

    project_id = project["id"]
    item = plane.get_work_item_by_identifier(project_id, sequence_id)
    if not item:
        return None

    state_map = plane.get_state_map(project_id)
    return project_id, item["id"], state_map


def _execute_plane_action(
    action: str,
    project_id: str,
    work_item_id: str,
    state_map: dict[str, str],
    commit_sha: str | None = None,
    commit_url: str | None = None,
    commit_message: str | None = None,
) -> str:
    """Execute a Plane action (progress, update, close, cancel, ref).

    Returns a human-readable summary of what was done.
    """
    plane = _get_plane()
    results = []

    if action == "progress":
        # Transition to In Progress + comment
        in_progress_id = state_map.get("In Progress")
        if in_progress_id:
            plane.update_work_item(project_id, work_item_id, state=in_progress_id)
            results.append("State -> In Progress")
        comment = f"<p>Work started: {commit_message or 'N/A'}</p>"
        if commit_sha:
            comment += f"<p>Commit: <code>{commit_sha[:8]}</code></p>"
        plane.add_comment(project_id, work_item_id, comment)
        results.append("Commented")

    elif action == "update":
        # Comment only, no state change
        comment = f"<p>Progress: {commit_message or 'N/A'}</p>"
        if commit_sha:
            comment += f"<p>Commit: <code>{commit_sha[:8]}</code></p>"
        plane.add_comment(project_id, work_item_id, comment)
        results.append("Commented")

    elif action == "close":
        # Comment + link + transition to Done
        done_id = state_map.get("Done")
        comment = f"<p>Completed: {commit_message or 'N/A'}</p>"
        if commit_sha:
            comment += f"<p>Commit: <code>{commit_sha[:8]}</code></p>"
        plane.add_comment(project_id, work_item_id, comment)
        results.append("Commented")

        if commit_url:
            try:
                plane.add_link(project_id, work_item_id, commit_url)
                results.append(f"Linked: {commit_url}")
            except Exception:
                results.append("Link failed (non-fatal)")

        if done_id:
            plane.update_work_item(project_id, work_item_id, state=done_id)
            results.append("State -> Done")

    elif action == "cancel":
        # Comment + transition to Cancelled
        cancelled_id = state_map.get("Cancelled")
        comment = f"<p>Cancelled: {commit_message or 'No reason given'}</p>"
        plane.add_comment(project_id, work_item_id, comment)
        results.append("Commented")
        if cancelled_id:
            plane.update_work_item(project_id, work_item_id, state=cancelled_id)
            results.append("State -> Cancelled")

    elif action == "ref":
        # Link only, no comment, no state change
        if commit_url:
            try:
                plane.add_link(project_id, work_item_id, commit_url)
                results.append(f"Linked: {commit_url}")
            except Exception:
                results.append("Link failed (non-fatal)")

    return "; ".join(results) if results else "No action taken"


# ---------------------------------------------------------------------------
# MCP Tools — git operations only
# ---------------------------------------------------------------------------


@mcp.tool
def commit(
    message: str,
    action: str = "none",
    issue: str | None = None,
    files: list[str] | None = None,
    stage_all: bool = False,
    repo_path: str | None = None,
) -> str:
    """Validate commit message, stage files, commit, and sync to Plane.

    This is the primary tool for making commits. It enforces the commit message
    schema and automatically updates linked Plane issues.

    Args:
        message: Commit message in format '<type>(<scope>): <description>'.
            Do NOT include the [action ISSUE] suffix — use the action and issue
            params instead. The tool constructs the full message.
        action: Plane action to take. One of:
            'progress' - transition issue to In Progress + comment
            'update'   - comment on issue (no state change)
            'close'    - comment + link commit + transition to Done
            'cancel'   - comment + transition to Cancelled
            'ref'      - link commit to issue only
            'none'     - no Plane interaction (default)
        issue: Issue reference like 'QFP-15'. Required unless action is 'none'.
        files: Specific files to stage before committing. If None, only
            commits already-staged files (unless stage_all is True).
        stage_all: If True, stages all changes (git add -A) before committing.
        repo_path: Absolute path to the repo this commit targets. When the
            server runs as a shared HTTP daemon, callers (or the upstream
            gateway-proxy injecting defaults) must name the repo here.
            When unset, falls back to GIT_WORK_DIR / os.getcwd() for the
            stdio per-session case.

    Returns:
        Success message with commit SHA and Plane actions taken, or
        REJECTED/FAILED with explanation.
    """
    cwd = _resolve_cwd(repo_path)

    # Validate action requires issue
    if action != "none" and not issue:
        return "REJECTED: action requires an issue reference (e.g., issue='QFP-15')"

    # Build the full commit message
    if action != "none" and issue:
        full_message = f"{message} [{action} {issue}]"
    else:
        full_message = message

    # 1. Validate schema
    parsed = parse_commit_message(full_message)
    if not parsed.is_valid:
        return f"REJECTED: {'; '.join(parsed.errors)}"

    # 2. Validate issue exists in Plane (if referenced)
    resolved_issues = []
    for ia in parsed.issue_actions:
        resolved = _resolve_issue(ia.project_prefix, ia.sequence_id)
        if not resolved:
            return (
                f"REJECTED: Issue {ia.issue_ref} not found in Plane. "
                f"Check that project '{ia.project_prefix}' exists and issue "
                f"#{ia.sequence_id} is valid."
            )
        project_id, work_item_id, state_map = resolved
        resolved_issues.append((project_id, work_item_id, state_map, ia))

    # 3. Stage files if requested
    if files:
        result = add(files, cwd)
        if not result.success:
            return f"FAILED: Could not stage files: {result.stderr}"
    elif stage_all:
        result = add(None, cwd)
        if not result.success:
            return f"FAILED: Could not stage files: {result.stderr}"

    # 4. Check there are staged changes
    if not has_staged_changes(cwd):
        return (
            "REJECTED: No staged changes to commit. "
            "Either pass files=['file1.py', ...] or stage_all=True, "
            "or stage files manually before calling commit."
        )

    # 5. Commit
    result = git_commit(full_message, cwd)
    if not result.success:
        return f"FAILED: git commit failed: {result.stderr}"

    # 6. Get commit SHA and URL
    sha_result = rev_parse_head(cwd)
    commit_sha = sha_result.stdout if sha_result.success else "unknown"
    commit_url = construct_commit_url(cwd, commit_sha)

    # 7. Execute Plane actions
    plane_results = []
    for project_id, work_item_id, state_map, ia in resolved_issues:
        try:
            pr = _execute_plane_action(
                action=ia.action,
                project_id=project_id,
                work_item_id=work_item_id,
                state_map=state_map,
                commit_sha=commit_sha,
                commit_url=commit_url,
                commit_message=parsed.description,
            )
            plane_results.append(f"  {ia.issue_ref}: {pr}")
        except Exception as e:
            plane_results.append(f"  {ia.issue_ref}: Plane sync failed (non-fatal): {e}")

    # 8. Build response
    lines = [f"Committed: {commit_sha[:8]} {parsed.description}"]
    if plane_results:
        lines.append("Plane updates:")
        lines.extend(plane_results)

    return "\n".join(lines)


@mcp.tool
def push(
    remote: str = "origin",
    branch: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Push commits to remote.

    Args:
        remote: Remote name (default: 'origin')
        branch: Branch to push (default: current branch)
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Success or error message.
    """
    cwd = _resolve_cwd(repo_path)
    result = git_push(cwd, remote, branch)
    if result.success:
        br = current_branch(cwd)
        return f"Pushed to {remote}/{br.stdout if br.success else 'unknown'}"
    return f"FAILED: {result.stderr}"


@mcp.tool
def status(repo_path: str | None = None) -> str:
    """Show git status: staged changes, unstaged changes, untracked files.

    Args:
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Formatted status summary.
    """
    cwd = _resolve_cwd(repo_path)

    stat = git_status(cwd)
    if not stat.success:
        return f"FAILED: {stat.stderr}"

    if not stat.stdout:
        return "Working tree clean. Nothing to commit."

    staged = diff_staged(cwd)

    lines = ["Git Status:"]
    if staged.stdout:
        lines.append(f"\nStaged changes:\n{staged.stdout}")

    lines.append(f"\nAll changes:\n{stat.stdout}")
    return "\n".join(lines)


@mcp.tool
def log(
    n: int = 10,
    issue: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Show recent git log, optionally filtered by issue reference.

    Args:
        n: Number of commits to show (default: 10)
        issue: Filter to commits referencing this issue (e.g., 'QFP-15')
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Git log output.
    """
    cwd = _resolve_cwd(repo_path)
    result = git_log(cwd, n=n, grep=issue)
    if result.success:
        return result.stdout or "No commits found."
    return f"FAILED: {result.stderr}"


@mcp.tool
def diff(
    staged: bool = False,
    file_path: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Show diff of changes in the working tree.

    Args:
        staged: If True, show only staged changes. If False, show unstaged changes.
        file_path: Optional specific file to diff.
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Diff output or 'No changes' message.
    """
    cwd = _resolve_cwd(repo_path)
    result = git_diff(cwd, staged=staged, file_path=file_path)
    if result.success:
        return result.stdout or "No changes."
    return f"FAILED: {result.stderr}"


@mcp.tool
def pull(remote: str = "origin", repo_path: str | None = None) -> str:
    """Pull latest changes from remote. Run this before pushing to avoid conflicts.

    Args:
        remote: Remote name (default: 'origin')
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Success or error message.
    """
    cwd = _resolve_cwd(repo_path)
    result = git_pull(cwd, remote)
    if result.success:
        return result.stdout or "Already up to date."
    return f"FAILED: {result.stderr}"


@mcp.tool
def branch(name: str | None = None, repo_path: str | None = None) -> str:
    """Show current branch, or create and switch to a new branch.

    Only create branches when the human explicitly requests one.
    Default workflow is direct push to the current branch.

    Args:
        name: If provided, create and switch to this branch.
              If None, show the current branch name.
        repo_path: Absolute path to the repo (HTTP-daemon mode). See ``commit``.

    Returns:
        Current branch name, or confirmation of new branch creation.
    """
    cwd = _resolve_cwd(repo_path)
    if name:
        result = git_create_branch(cwd, name)
        if result.success:
            return f"Created and switched to branch: {name}"
        return f"FAILED: {result.stderr}"
    else:
        result = current_branch(cwd)
        if result.success:
            return f"Current branch: {result.stdout}"
        return f"FAILED: {result.stderr}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server in stdio mode."""
    mcp.run()


if __name__ == "__main__":
    main()
