"""Git operations via subprocess.

All git commands run in a specified working directory (the repo).
No global state — every function takes cwd explicitly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class GitResult:
    """Result of a git operation."""
    success: bool
    stdout: str
    stderr: str
    returncode: int


def _run(args: list[str], cwd: str, timeout: int = 30) -> GitResult:
    """Run a git command and return structured result."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return GitResult(
            success=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            returncode=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return GitResult(
            success=False,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            returncode=-1,
        )
    except FileNotFoundError:
        return GitResult(
            success=False,
            stdout="",
            stderr="git not found in PATH",
            returncode=-1,
        )


def status(cwd: str) -> GitResult:
    """git status --porcelain (machine-readable)."""
    return _run(["status", "--porcelain"], cwd)


def diff_staged(cwd: str) -> GitResult:
    """git diff --cached --stat (summary of staged changes)."""
    return _run(["diff", "--cached", "--stat"], cwd)


def add(files: list[str] | None, cwd: str) -> GitResult:
    """Stage files. If files is None, stages all changes (git add -A)."""
    if files:
        return _run(["add"] + files, cwd)
    return _run(["add", "-A"], cwd)


def commit(message: str, cwd: str) -> GitResult:
    """git commit with the given message."""
    return _run(["commit", "-m", message], cwd)


def push(cwd: str, remote: str = "origin", branch: str | None = None) -> GitResult:
    """git push. If branch is None, pushes current branch."""
    args = ["push", remote]
    if branch:
        args.append(branch)
    return _run(args, cwd, timeout=60)


def log(cwd: str, n: int = 10, grep: str | None = None,
        oneline: bool = True) -> GitResult:
    """git log with optional grep filter."""
    args = ["log", f"-{n}"]
    if oneline:
        args.append("--oneline")
    if grep:
        args.extend(["--grep", grep])
    return _run(args, cwd)


def rev_parse_head(cwd: str) -> GitResult:
    """Get current HEAD SHA."""
    return _run(["rev-parse", "HEAD"], cwd)


def current_branch(cwd: str) -> GitResult:
    """Get current branch name."""
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def remote_url(cwd: str, remote: str = "origin") -> GitResult:
    """Get the remote URL for constructing commit links."""
    return _run(["remote", "get-url", remote], cwd)


def has_staged_changes(cwd: str) -> bool:
    """Check if there are staged changes ready to commit."""
    result = _run(["diff", "--cached", "--quiet"], cwd)
    return not result.success  # exit code 1 means there ARE diffs


def has_unstaged_changes(cwd: str) -> bool:
    """Check if there are unstaged changes in tracked files."""
    result = _run(["diff", "--quiet"], cwd)
    return not result.success


def get_repo_root(cwd: str) -> str | None:
    """Get the git repo root directory."""
    result = _run(["rev-parse", "--show-toplevel"], cwd)
    return result.stdout if result.success else None


def diff(cwd: str, staged: bool = False, file_path: str | None = None) -> GitResult:
    """Show diff of changes."""
    args = ["diff"]
    if staged:
        args.append("--cached")
    if file_path:
        args.extend(["--", file_path])
    return _run(args, cwd)


def pull(cwd: str, remote: str = "origin", branch: str | None = None) -> GitResult:
    """Pull from remote."""
    args = ["pull", remote]
    if branch:
        args.append(branch)
    return _run(args, cwd, timeout=60)


def create_branch(cwd: str, name: str, checkout: bool = True) -> GitResult:
    """Create a new branch, optionally checking it out."""
    if checkout:
        return _run(["checkout", "-b", name], cwd)
    return _run(["branch", name], cwd)


def checkout(cwd: str, ref: str) -> GitResult:
    """Checkout a branch or ref."""
    return _run(["checkout", ref], cwd)


def construct_commit_url(cwd: str, sha: str) -> str | None:
    """Construct a GitHub/GitLab commit URL from remote + SHA."""
    result = remote_url(cwd)
    if not result.success:
        return None

    url = result.stdout
    # Normalize git@ and .git URLs to HTTPS
    if url.startswith("git@"):
        # git@github.com:user/repo.git -> https://github.com/user/repo
        url = url.replace(":", "/").replace("git@", "https://")
    if url.endswith(".git"):
        url = url[:-4]

    return f"{url}/commit/{sha}"
