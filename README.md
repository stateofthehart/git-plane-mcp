# mcp-git-plane

MCP server that wraps git operations with commit message schema enforcement and automatic [Plane](https://plane.so) issue tracking.

Agents use this instead of raw `git commit` / `git push`. Every commit is validated against the schema, and referenced Plane issues are automatically updated (commented, state-transitioned, commit-linked).

## Tools

| Tool | Purpose |
|------|---------|
| `commit` | Validate schema → stage files → git commit → sync to Plane |
| `push` | Push to remote |
| `pull` | Pull from remote |
| `status` | Staged/unstaged/untracked summary |
| `diff` | View changes (staged or unstaged) |
| `log` | Recent commits, filterable by issue reference |
| `branch` | Show current branch or create a new one |

## Commit Schema

```
<type>(<scope>): <description> [<action> <ISSUE-REF>]
```

- **Types**: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `wip`
- **Actions**: `progress`, `update`, `close`, `cancel`, `ref`, `none`
- **Issue ref**: `PROJ-123` format (validated against Plane)

Invalid commits are rejected before reaching git.

## Install

```bash
git clone https://github.com/stateofthehart/git-plane-mcp.git
cd git-plane-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

## Configure

### Claude Code

```bash
claude mcp add -s project mcp-git-plane /path/to/.venv/bin/mcp-git-plane \
  -e PLANE_API_KEY=your_key \
  -e PLANE_WORKSPACE_SLUG=your_workspace \
  -e PLANE_BASE_URL=http://your-plane-instance:8585
```

### OpenCode (opencode.json)

```json
{
  "git-plane": {
    "type": "local",
    "command": ["/path/to/.venv/bin/mcp-git-plane"],
    "env": {
      "PLANE_API_KEY": "your_key",
      "PLANE_WORKSPACE_SLUG": "your_workspace",
      "PLANE_BASE_URL": "http://your-plane-instance:8585"
    }
  }
}
```

## Environment Variables

| Var | Required | Description |
|-----|----------|-------------|
| `PLANE_API_KEY` | Yes | Plane API token |
| `PLANE_WORKSPACE_SLUG` | Yes | Plane workspace slug |
| `PLANE_BASE_URL` | Yes | Plane instance URL |
| `GIT_WORK_DIR` | No | Override working directory (defaults to cwd) |

## License

MIT
