"""Commit message schema parser and validator.

Format: <type>(<scope>): <description> [<action> <ISSUE-REF>]

Examples:
    feat(providers): add Kalshi normalizer [close QFP-15]
    fix(store): handle null timestamps [update QFM-42]
    docs: update AGENTS.md [close EXE-8]
    chore: remove unused import
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


VALID_TYPES = frozenset({
    "feat",      # New functionality
    "fix",       # Bug fix
    "refactor",  # Code restructuring, no behavior change
    "docs",      # Documentation only
    "test",      # Tests only
    "chore",     # Maintenance, deps, config
    "wip",       # Work in progress (partial, NOT complete)
})

VALID_ACTIONS = frozenset({
    "progress",  # Transition to In Progress + comment
    "update",    # Comment only (no state change)
    "close",     # Comment + link + transition to Done
    "cancel",    # Comment + transition to Cancelled
    "ref",       # Link commit only (tangential reference)
})

# Matches: [action PROJ-123] or [action PROJ-123 PROJ-456]
_ACTION_PATTERN = re.compile(
    r"\[(\w+)\s+((?:[A-Z]+-\d+\s*)+)\]"
)

# Matches the full commit message
_COMMIT_PATTERN = re.compile(
    r"^(\w+)(?:\(([^)]+)\))?:\s+(.+?)(?:\s+\[.+\])*$"
)

# Matches a single issue reference like QFP-15
_ISSUE_REF_PATTERN = re.compile(r"[A-Z]+-\d+")


@dataclass
class IssueAction:
    """A parsed action-issue pair from a commit message."""
    action: str          # "close", "update", "progress", etc.
    issue_ref: str       # "QFP-15"
    project_prefix: str  # "QFP"
    sequence_id: int     # 15


@dataclass
class ParsedCommit:
    """Result of parsing a commit message."""
    raw: str
    commit_type: str | None = None
    scope: str | None = None
    description: str | None = None
    issue_actions: list[IssueAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def has_issue_refs(self) -> bool:
        return len(self.issue_actions) > 0

    def format_message(self) -> str:
        """Reconstruct the canonical commit message."""
        parts = [self.commit_type]
        if self.scope:
            parts = [f"{self.commit_type}({self.scope})"]
        msg = f"{''.join(parts)}: {self.description}"
        for ia in self.issue_actions:
            msg += f" [{ia.action} {ia.issue_ref}]"
        return msg


def parse_commit_message(message: str) -> ParsedCommit:
    """Parse and validate a commit message against the schema.

    Returns a ParsedCommit with errors populated if validation fails.
    """
    result = ParsedCommit(raw=message.strip())

    if not result.raw:
        result.errors.append("Commit message is empty")
        return result

    # Extract action blocks first (they're at the end)
    action_blocks = _ACTION_PATTERN.findall(result.raw)
    # Strip action blocks from message for type/scope/description parsing
    clean_msg = _ACTION_PATTERN.sub("", result.raw).strip()

    # Parse type(scope): description
    match = _COMMIT_PATTERN.match(clean_msg) if clean_msg else None
    if not match and not _COMMIT_PATTERN.match(result.raw):
        # Try matching the full message (action blocks included)
        match = _COMMIT_PATTERN.match(result.raw)

    if not match:
        # Try a simpler parse — maybe no scope
        simple = re.match(r"^(\w+):\s+(.+)$", clean_msg)
        if simple:
            result.commit_type = simple.group(1).lower()
            result.description = simple.group(2).strip()
        else:
            result.errors.append(
                f"Invalid format. Expected: <type>(<scope>): <description> [<action> <ISSUE>]. "
                f"Got: {result.raw[:80]}"
            )
            return result
    else:
        result.commit_type = match.group(1).lower()
        result.scope = match.group(2)
        result.description = match.group(3).strip()

    # Validate type
    if result.commit_type and result.commit_type not in VALID_TYPES:
        result.errors.append(
            f"Invalid type '{result.commit_type}'. "
            f"Valid types: {', '.join(sorted(VALID_TYPES))}"
        )

    # Validate description
    if result.description:
        if result.description[0].isupper():
            result.errors.append(
                "Description should start with lowercase (imperative mood)"
            )
        if result.description.endswith("."):
            result.errors.append("Description should not end with a period")

    # Parse action blocks
    for action_str, refs_str in action_blocks:
        action = action_str.lower()
        if action not in VALID_ACTIONS:
            result.errors.append(
                f"Invalid action '{action}'. "
                f"Valid actions: {', '.join(sorted(VALID_ACTIONS))}"
            )
            continue

        refs = _ISSUE_REF_PATTERN.findall(refs_str)
        for ref in refs:
            parts = ref.split("-")
            result.issue_actions.append(IssueAction(
                action=action,
                issue_ref=ref,
                project_prefix=parts[0],
                sequence_id=int(parts[1]),
            ))

    # WIP commits cannot close issues
    if result.commit_type == "wip":
        for ia in result.issue_actions:
            if ia.action == "close":
                result.errors.append(
                    f"WIP commits cannot close issues. "
                    f"Use [update {ia.issue_ref}] instead of [close {ia.issue_ref}]"
                )

    return result


def validate_commit_message(message: str) -> tuple[bool, str]:
    """Convenience function: returns (is_valid, error_message_or_empty)."""
    parsed = parse_commit_message(message)
    if parsed.is_valid:
        return True, ""
    return False, "; ".join(parsed.errors)
