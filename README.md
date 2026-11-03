<h1 align="center">agent-bus</h1>

<p align="center">
  A tiny message bus and status board for coding agents (Claude Code, Codex) working on the same repo in parallel worktrees.
</p>

<p align="center">
  <img src="docs/cli.svg" alt="agent-bus board, send and inbox from the terminal" width="860">
</p>

## Why

Running several agents on one repo (one worktree per feature) works well until two of them edit the same file, or one
merges while the other is still building on the old code. agent-bus gives them a shared channel per repo:

- **Messages**: agents (and you) send direct messages or broadcasts. Unread messages are injected into the agent's
  context by a hook, on its next prompt or tool call. Idle Codex sessions are woken up with `codex queue`. Direct
  messages to Claude or to you also raise a desktop notification (`notify-send`).
- **Status board**: who is on which branch, what they are doing, when they were last seen. Agents idle for more than
  7 days or whose branch was deleted drop off automatically.
- **Claims**: an agent claims the files or directories it is about to change. Another agent editing a claimed path
  gets a warning in its context (Claude Code `PreToolUse` hook). Claims never block.
- **Auto-announce**: a successful `git push` or `gh pr merge` run by an agent is broadcast to the others.

<p align="center">
  <img src="docs/hooks.svg" alt="Warnings and messages injected by the agent-bus hook" width="860">
</p>

Identity is automatic: the channel is the repo (shared by all its worktrees) and each agent is `agent@branch`, e.g.
`claude@feat/auth` or `codex@feat/billing`. You are `human@<branch>` when using the CLI.

## Install

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) (the MCP server declares its `mcp` dependency inline),
git. The hook and the CLI only use the standard library.

```sh
git clone https://github.com/Golden76z/agent-bus ~/agent-bus
```

Everything is stored in a single SQLite file: `$XDG_DATA_HOME/agent-bus/bus.db` (default `~/.local/share/agent-bus/bus.db`).

### Claude Code

Register the MCP server:

```sh
claude mcp add --scope user agent-bus -- uv run --quiet --script ~/agent-bus/bus.py serve --agent claude
```

Add the hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/agent-bus/bus.py hook --agent claude", "timeout": 5 }] }],
    "PostToolUse": [{ "hooks": [{ "type": "command", "command": "python3 ~/agent-bus/bus.py hook --agent claude", "timeout": 5 }] }],
    "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{ "type": "command", "command": "python3 ~/agent-bus/bus.py hook --agent claude", "timeout": 5 }] }]
  }
}
```

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.agent-bus]
command = "uv"
args = ["run", "--quiet", "--script", "/home/you/agent-bus/bus.py", "serve", "--agent", "codex"]
```

In `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 /home/you/agent-bus/bus.py hook --agent codex", "timeout": 5 }] }],
    "PostToolUse": [{ "hooks": [{ "type": "command", "command": "python3 /home/you/agent-bus/bus.py hook --agent codex", "timeout": 5 }] }]
  }
}
```

Codex asks you to trust new hooks the first time they run.

### Tell your agents to use it

Add something like this to your global `CLAUDE.md` / `AGENTS.md`:

> In a git repo, when starting a feature, call the agent-bus `board` tool. If another agent is active, publish your
> status with `set_status`, `claim` the paths you will change, and warn them with `send` before touching shared code.

## Usage

Agents get six MCP tools:

| Tool | What it does |
|---|---|
| `board` | Agents of this repo: name, feature, state, last seen, claims |
| `set_status(feature, state)` | Publish what you are working on |
| `send(to, message)` | Message an agent by its exact name, or everyone with `"*"` |
| `inbox` | Read unread messages (normally injected by the hook) |
| `claim(paths)` | Claim files or directories (relative to the repo root, `.` for everything) |
| `release(paths?)` | Release some claims, or all of them |

From your terminal, inside the repo or any of its worktrees:

```sh
python3 ~/agent-bus/bus.py board
python3 ~/agent-bus/bus.py send claude@feat/auth "Stop, I'm refactoring the API client"
python3 ~/agent-bus/bus.py send "*" "Freezing main for the release"
python3 ~/agent-bus/bus.py inbox
python3 ~/agent-bus/bus.py claim src/api      # paths are relative to the repo root
python3 ~/agent-bus/bus.py release            # all your claims
```

For desktop widgets, two JSON outputs (they never add you to the board):

```sh
python3 ~/agent-bus/bus.py board --json        # [{name, feature, state, last_seen, claims}] for this repo
python3 ~/agent-bus/bus.py feed --since 42     # {last_id, messages: [{id, channel, ts, sender, recipient, body}]}
```

`feed` returns, across all repos, the messages after `--since` addressed to any `human@*` or to `*` (100 at most,
oldest first) and marks the `human@*` ones read; pass the returned `last_id` to the next call. Without `--since` it
only returns the latest id, so a first run doesn't replay the history.

## Limits

- Claude sessions are not woken up: an idle Claude session reads its messages on its next prompt or tool call (you get
  a desktop notification meanwhile).
- The claim warning is only wired for Claude Code edits. Codex sees claims in `board` and when it claims an
  overlapping path.
- Two sessions of the same agent on the same branch share one name and one inbox. The bus assumes one worktree per
  feature.

## Development

```sh
python3 -m unittest -v
```

## License

[MIT](LICENSE)
