"""Git + Plane MCP Server.

Schema-enforced commits with automatic Plane issue tracking.
This is the ONLY way agents should interact with git when working on tracked issues.

Tools:
    commit      - Validate message, commit, sync to Plane (atomic)
    push        - Push to remote
    status      - Git status + staged changes summary
    log         - Git log with optional issue filtering
    claim_issue - Transition issue to In Progress + comment
    close_issue - Transition issue to Done with summary + commit links
    create_issue - Create a new Plane work item
    get_my_work  - List Todo/In Progress items for a project
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from .git_ops import (
    add,
    commit as git_commit,
    construct_commit_url,
    current_branch,
    diff_staged,
    get_repo_root,
    has_staged_changes,
    log as git_log,
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


def _resolve_cwd() -> str:
    """Get the working directory — from env or current directory."""
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
        comment = f"<p>Work started. Commit: <code>{commit_sha[:8] if commit_sha else 'N/A'}</code></p>"
        if commit_message:
            comment = f"<p>Work started: {commit_message}</p>"
        plane.add_comment(project_id, work_item_id, comment)
        results.append("Commented")

    elif action == "update":
        # Comment only, no state change
        comment = f"<p>Progress: <code>{commit_sha[:8] if commit_sha else 'N/A'}</code>"
        if commit_message:
            comment = f"<p>{commit_message}</p>"
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
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool
def commit(
    message: str,
    action: str = "none",
    issue: str | None = None,
    files: list[str] | None = None,
    stage_all: bool = False,
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
        files: Specific files to stage. If None, only commits already-staged files.
        stage_all: If True, stages all changes (git add -A) before committing.

    Returns:
        Success message with commit SHA and Plane actions taken, or error message.
    """
    cwd = _resolve_cwd()

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
    resolved_issues: list[tuple[str, str, dict, str, int]] = []
    for ia in parsed.issue_actions:
        resolved = _resolve_issue(ia.project_prefix, ia.sequence_id)
        if not resolved:
            return (
                f"REJECTED: Issue {ia.issue_ref} not found in Plane. "
                f"Check that project '{ia.project_prefix}' exists and issue "
                f"#{ia.sequence_id} is valid."
            )
        project_id, work_item_id, state_map = resolved
        resolved_issues.append((project_id, work_item_id, state_map,
                                ia.action, ia.sequence_id))

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
    for project_id, work_item_id, state_map, act, seq_id in resolved_issues:
        try:
            pr = _execute_plane_action(
                action=act,
                project_id=project_id,
                work_item_id=work_item_id,
                state_map=state_map,
                commit_sha=commit_sha,
                commit_url=commit_url,
                commit_message=parsed.description,
            )
            plane_results.append(f"  {parsed.issue_actions[0].issue_ref}: {pr}")
        except Exception as e:
            plane_results.append(f"  Plane sync failed (non-fatal): {e}")

    # 8. Build response
    lines = [f"Committed: {commit_sha[:8]} {parsed.description}"]
    if plane_results:
        lines.append("Plane updates:")
        lines.extend(plane_results)

    return "\n".join(lines)


@mcp.tool
def push(remote: str = "origin", branch: str | None = None) -> str:
    """Push commits to remote.

    Args:
        remote: Remote name (default: 'origin')
        branch: Branch to push (default: current branch)

    Returns:
        Success or error message.
    """
    cwd = _resolve_cwd()
    result = git_push(cwd, remote, branch)
    if result.success:
        br = current_branch(cwd)
        return f"Pushed to {remote}/{br.stdout if br.success else 'unknown'}"
    return f"FAILED: {result.stderr}"


@mcp.tool
def status() -> str:
    """Show git status: staged changes, unstaged changes, untracked files.

    Returns:
        Formatted status summary.
    """
    cwd = _resolve_cwd()

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
def log(n: int = 10, issue: str | None = None) -> str:
    """Show recent git log, optionally filtered by issue reference.

    Args:
        n: Number of commits to show (default: 10)
        issue: Filter to commits referencing this issue (e.g., 'QFP-15')

    Returns:
        Git log output.
    """
    cwd = _resolve_cwd()
    result = git_log(cwd, n=n, grep=issue)
    if result.success:
        return result.stdout or "No commits found."
    return f"FAILED: {result.stderr}"


@mcp.tool
def claim_issue(issue: str, plan: str | None = None) -> str:
    """Claim a Plane issue by transitioning it to In Progress.

    Args:
        issue: Issue reference like 'QFP-15'
        plan: Optional brief plan of how you'll approach this work

    Returns:
        Success or error message.
    """
    import re
    match = re.match(r"([A-Z]+)-(\d+)", issue)
    if not match:
        return f"REJECTED: Invalid issue format '{issue}'. Expected: PROJ-123"

    prefix, seq = match.group(1), int(match.group(2))
    resolved = _resolve_issue(prefix, seq)
    if not resolved:
        return f"REJECTED: Issue {issue} not found in Plane."

    project_id, work_item_id, state_map = resolved
    plane = _get_plane()

    # Transition to In Progress
    in_progress_id = state_map.get("In Progress")
    if in_progress_id:
        plane.update_work_item(project_id, work_item_id, state=in_progress_id)

    # Comment
    comment = f"<p>Claiming this issue. "
    if plan:
        comment += f"Plan: {plan}"
    comment += "</p>"
    plane.add_comment(project_id, work_item_id, comment)

    return f"Claimed {issue} -> In Progress"


@mcp.tool
def close_issue(issue: str, summary: str) -> str:
    """Close a Plane issue by transitioning it to Done with a summary.

    Call this AFTER committing and pushing. The commit should already
    reference this issue via the commit tool.

    Args:
        issue: Issue reference like 'QFP-15'
        summary: Summary of what was done (displayed in Plane comment)

    Returns:
        Success or error message.
    """
    import re
    match = re.match(r"([A-Z]+)-(\d+)", issue)
    if not match:
        return f"REJECTED: Invalid issue format '{issue}'. Expected: PROJ-123"

    prefix, seq = match.group(1), int(match.group(2))
    resolved = _resolve_issue(prefix, seq)
    if not resolved:
        return f"REJECTED: Issue {issue} not found in Plane."

    project_id, work_item_id, state_map = resolved
    plane = _get_plane()

    # Comment with summary
    comment = f"<p>Completed: {summary}</p>"
    plane.add_comment(project_id, work_item_id, comment)

    # Transition to Done
    done_id = state_map.get("Done")
    if done_id:
        plane.update_work_item(project_id, work_item_id, state=done_id)

    return f"Closed {issue} -> Done"


@mcp.tool
def create_issue(
    project: str,
    name: str,
    priority: str = "medium",
    description: str | None = None,
) -> str:
    """Create a new Plane work item in a project.

    Args:
        project: Project identifier like 'QFP', 'QFM', 'EXE', etc.
        name: Work item title (concise, imperative)
        priority: One of 'urgent', 'high', 'medium', 'low', 'none'
        description: Optional description (plain text, will be wrapped in HTML)

    Returns:
        The created issue reference (e.g., 'QFP-16') or error message.
    """
    plane = _get_plane()
    proj = plane.get_project_by_identifier(project)
    if not proj:
        return f"REJECTED: Project '{project}' not found in Plane."

    project_id = proj["id"]

    # Get the Todo state as default
    state_map = plane.get_state_map(project_id)
    todo_id = state_map.get("Todo")

    fields: dict = {"priority": priority}
    if todo_id:
        fields["state"] = todo_id
    if description:
        fields["description_html"] = f"<p>{description}</p>"

    item = plane.create_work_item(project_id, name, **fields)
    seq = item.get("sequence_id", "?")
    identifier = proj.get("identifier", project)

    return f"Created {identifier}-{seq}: {name}"


@mcp.tool
def get_my_work(project: str, include_backlog: bool = False) -> str:
    """List open work items (Todo + In Progress) for a project.

    Args:
        project: Project identifier like 'QFP', 'QFM', 'EXE', etc.
        include_backlog: Also show Backlog items (default: False)

    Returns:
        Formatted list of open work items with priority and state.
    """
    plane = _get_plane()
    proj = plane.get_project_by_identifier(project)
    if not proj:
        return f"REJECTED: Project '{project}' not found in Plane."

    project_id = proj["id"]
    identifier = proj.get("identifier", project)
    states = plane.list_states(project_id)
    state_id_to_name = {s["id"]: s["name"] for s in states}

    target_groups = {"unstarted", "started"}
    if include_backlog:
        target_groups.add("backlog")
    target_state_ids = {
        s["id"] for s in states if s.get("group") in target_groups
    }

    items = plane.list_work_items(project_id, per_page=100)
    filtered = [i for i in items if i.get("state") in target_state_ids]

    if not filtered:
        return f"No open work items in {identifier}."

    # Sort by priority (urgent first) then by created_at
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    filtered.sort(key=lambda i: (
        priority_order.get(i.get("priority", "none"), 5),
        i.get("created_at", ""),
    ))

    lines = [f"Open work in {identifier} ({len(filtered)} items):"]
    for item in filtered:
        ref = f"{identifier}-{item['sequence_id']}"
        state = state_id_to_name.get(item.get("state", ""), "?")
        priority = item.get("priority", "none")
        name = item.get("name", "Untitled")
        lines.append(f"  [{priority:>6}] {ref} ({state}): {name}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server in stdio mode."""
    mcp.run()


if __name__ == "__main__":
    main()
