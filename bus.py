#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""agent-bus: messages and a status board between agents (Claude, Codex) working on the same repo.

  bus.py serve --agent claude   MCP stdio server
  bus.py hook  --agent claude   hook: injects unread messages, broadcasts push/merge, warns on claimed files
  bus.py board | inbox | send <to> <message>   CLI for humans (as human@<branch>, or --agent)
  bus.py board --json | feed [--since N]       JSON for desktop widgets
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

DB_PATH = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "agent-bus", "bus.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL,
  recipient TEXT NOT NULL, body TEXT NOT NULL, ts TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agents (
  channel TEXT NOT NULL, name TEXT NOT NULL, feature TEXT, state TEXT,
  last_seen TEXT NOT NULL, broadcast_cursor INTEGER NOT NULL, session_id TEXT, PRIMARY KEY (channel, name));
CREATE TABLE IF NOT EXISTS claims (
  channel TEXT NOT NULL, path TEXT NOT NULL, owner TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (channel, path, owner));
"""
STALE_DAYS = 7
ANNOUNCED = re.compile(r"^\s*(git\s+push|gh\s+pr\s+merge)\b")


def now():
    return time.strftime("%Y-%m-%d %H:%M")


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if "session_id" not in {r[1] for r in conn.execute("PRAGMA table_info(agents)")}:
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError:  # another process migrated at the same time
            pass
    return conn


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def identity(agent, cwd):
    """(channel, name): channel = the repo (shared by its worktrees), name = agent@branch.

    ponytail: two sessions of the same agent on the same branch share a name (and an inbox);
    designed for one worktree per feature. Add a session suffix if that case becomes common.
    """
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return os.path.abspath(cwd), f"{agent}@{os.path.basename(os.path.abspath(cwd))}"
    # symbolic-ref also works in a repo without commits; detached HEAD → short sha.
    branch = _git(cwd, "symbolic-ref", "--short", "HEAD") or _git(cwd, "rev-parse", "--short", "HEAD") or "detached"
    return os.path.dirname(common) if common.endswith("/.git") else common, f"{agent}@{branch}"


def touch(conn, channel, name, session_id=None):
    # First visit: the cursor starts at the latest message, so the agent doesn't get the broadcast backlog.
    conn.execute(
        "INSERT INTO agents (channel, name, last_seen, broadcast_cursor, session_id) "
        "VALUES (?, ?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages), ?) "
        "ON CONFLICT (channel, name) DO UPDATE SET last_seen = excluded.last_seen, "
        "session_id = COALESCE(excluded.session_id, agents.session_id)",
        (channel, name, now(), session_id),
    )


def send(conn, channel, sender, to, body):
    touch(conn, channel, sender)
    return conn.execute(
        "INSERT INTO messages (channel, sender, recipient, body, ts) VALUES (?, ?, ?, ?, ?)",
        (channel, sender, to, body, now()),
    ).lastrowid


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wake(conn, channel, msg_id, run=_run):
    """Pushes a direct message to a known Codex session via `codex queue` (wakes up an idle session).

    If queueing succeeds, the message is marked read so the hook doesn't deliver it a second time.
    ponytail: "*" broadcasts and Claude recipients are not woken (delivered by the hook on the next prompt/tool).
    """
    m = conn.execute(
        "SELECT m.sender, m.body, a.session_id FROM messages m JOIN agents a "
        "ON a.channel = m.channel AND a.name = m.recipient "
        "WHERE m.id = ? AND m.channel = ? AND m.recipient LIKE 'codex@%' AND a.session_id IS NOT NULL",
        (msg_id, channel),
    ).fetchone()
    if m is None:
        return False
    # Claimed before queueing: a hook running during `codex queue` won't deliver it again.
    if conn.execute("UPDATE messages SET read = 1 WHERE id = ? AND read = 0", (msg_id,)).rowcount == 0:
        return False
    text = f"[agent-bus] Message from {m['sender']} (reply with the agent-bus send tool if needed):\n{m['body']}"
    if not run(["codex", "queue", "--thread", m["session_id"], "--message", text]):
        conn.execute("UPDATE messages SET read = 0 WHERE id = ?", (msg_id,))
        return False
    return True


def unread(conn, channel, name):
    """Unread messages for `name`; marks them as read."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        touch(conn, channel, name)
        cursor = conn.execute(
            "SELECT broadcast_cursor FROM agents WHERE channel = ? AND name = ?", (channel, name)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, sender, recipient, body, ts FROM messages WHERE channel = ? AND ("
            " (recipient = ? AND read = 0) OR (recipient = '*' AND sender != ? AND id > ?)) ORDER BY id",
            (channel, name, name, cursor),
        ).fetchall()
        conn.execute("UPDATE messages SET read = 1 WHERE channel = ? AND recipient = ?", (channel, name))
        conn.execute(
            "UPDATE agents SET broadcast_cursor = (SELECT COALESCE(MAX(id), 0) FROM messages) WHERE channel = ? AND name = ?",
            (channel, name),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return [dict(r) for r in rows]


def set_status(conn, channel, name, feature, state):
    touch(conn, channel, name)
    conn.execute(
        "UPDATE agents SET feature = ?, state = ? WHERE channel = ? AND name = ?", (feature, state, channel, name)
    )


def _branch_alive(channel, name):
    return _git(channel, "rev-parse", "--verify", "--quiet", name.split("@", 1)[1] + "^{commit}") is not None


def prune(conn, channel):
    """Forgets agents idle for more than STALE_DAYS or whose branch was deleted, along with their claims."""
    cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - STALE_DAYS * 86400))
    rows = conn.execute("SELECT name, last_seen FROM agents WHERE channel = ?", (channel,)).fetchall()
    # Branches are only checked in a repo with commits (not a plain dir, not a fresh `git init`).
    check = os.path.isdir(channel) and _git(channel, "rev-parse", "--verify", "--quiet", "HEAD") is not None
    gone = [(channel, r["name"]) for r in rows
            if r["last_seen"] < cutoff or (check and not _branch_alive(channel, r["name"]))]
    conn.executemany("DELETE FROM agents WHERE channel = ? AND name = ?", gone)
    conn.executemany("DELETE FROM claims WHERE channel = ? AND owner = ?", gone)


def board(conn, channel):
    prune(conn, channel)
    claims = {}
    for r in conn.execute("SELECT owner, path FROM claims WHERE channel = ? ORDER BY path", (channel,)):
        claims.setdefault(r["owner"], []).append(r["path"])
    rows = conn.execute(
        "SELECT name, feature, state, last_seen FROM agents WHERE channel = ? ORDER BY last_seen DESC", (channel,)
    )
    return [{**dict(r), "claims": claims.get(r["name"], [])} for r in rows]


def feed(conn, since):
    """Messages after `since` to any human@* or to everyone, across all channels (100 at most); marks the human ones read.

    No touch(): the human's name depends on the branch, and reading the feed must not show them on the board
    nor move the agents' broadcast cursors. `since` None (first run) → only the latest id, no backlog.
    """
    latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    # Past the end: the DB was recreated and ids restarted; resume from its latest id instead of waiting forever.
    if since is None or since > latest:
        return {"last_id": latest, "messages": []}
    rows = [dict(r) for r in conn.execute(
        "SELECT id, channel, ts, sender, recipient, body FROM messages "
        "WHERE id > ? AND (recipient LIKE 'human@%' OR recipient = '*') AND sender NOT LIKE 'human@%' "
        "ORDER BY id LIMIT 100", (since,))]
    conn.executemany("UPDATE messages SET read = 1 WHERE id = ?", [(m["id"],) for m in rows if m["recipient"].startswith("human@")])
    return {"last_id": rows[-1]["id"] if rows else since, "messages": rows}


def _overlaps(a, b):
    return a == b or "." in (a, b) or a.startswith(b + "/") or b.startswith(a + "/")


def claimed_by_others(conn, channel, owner, path):
    rows = conn.execute("SELECT path, owner FROM claims WHERE channel = ? AND owner != ?", (channel, owner))
    return sorted({r["owner"] for r in rows if _overlaps(r["path"], path)})


def claim(conn, channel, owner, paths):
    """Records that `owner` is working on `paths` (files or directories); returns [(path, other owner)] overlaps."""
    touch(conn, channel, owner)
    conn.executemany("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, ?)", [(channel, p, owner, now()) for p in paths])
    return [(p, o) for p in paths for o in claimed_by_others(conn, channel, owner, p)]


def release(conn, channel, owner, paths=None):
    if paths is None:
        conn.execute("DELETE FROM claims WHERE channel = ? AND owner = ?", (channel, owner))
    else:
        conn.executemany("DELETE FROM claims WHERE channel = ? AND owner = ? AND path = ?", [(channel, owner, p) for p in paths])


def rel_path(cwd, path):
    """Path (absolute, or relative to the worktree root) made relative to the worktree root, so claims match
    across worktrees of the same repo."""
    top = os.path.realpath(_git(cwd, "rev-parse", "--show-toplevel") or cwd)
    return os.path.relpath(os.path.realpath(os.path.join(top, path)), top)


def notify(sender, to, body, run=_run):
    """Desktop notification for direct messages to recipients that can't be woken (idle Claude session, human)."""
    if not to.startswith(("claude@", "human@")):
        return False
    return run(["notify-send", "--app-name=agent-bus", f"agent-bus: {sender} → {to}", body])


def format_messages(msgs):
    return "\n".join(f"- [{m['ts']}] {m['sender']}{' (to all)' if m.get('recipient') == '*' else ''}: {m['body']}" for m in msgs)


def hook_output(event, msgs, notes=()):
    parts = list(notes)
    if msgs:
        parts.append("agent-bus messages from other agents (reply with the send tool if needed):\n" + format_messages(msgs))
    if not parts:
        return None
    ctx = "\n\n".join(parts)
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}, ensure_ascii=False)


def announced_command(tool_input, tool_response):
    """The shell command if it is a successful `git push` / `gh pr merge` worth broadcasting, else None."""
    cmd = tool_input.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(cmd)
    if not isinstance(cmd, str):
        return None
    segments = re.split(r"&&|\|\||[;|\n]", re.sub(r"\"[^\"]*\"|'[^']*'", "", cmd))
    if not any(ANNOUNCED.match(seg) and "--dry-run" not in seg for seg in segments):
        return None
    # Claude only fires PostToolUse on success; the exit code check covers other harnesses.
    if isinstance(tool_response, dict) and any(tool_response.get(k) not in (None, 0) for k in ("exit_code", "exitCode", "returncode")):
        return None
    return re.sub(r"://[^/@\s]+@", "://***@", cmd)[:200]


def handle_hook(conn, agent, data):
    cwd = data.get("cwd") or os.getcwd()
    channel, name = identity(agent, cwd)
    touch(conn, channel, name, data.get("session_id"))
    event = data.get("hook_event_name", "PostToolUse")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    notes = []
    if event == "PostToolUse" and (cmd := announced_command(tool_input, data.get("tool_response"))):
        send(conn, channel, name, "*", f"[auto] {name} ran: {cmd}")
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if event == "PreToolUse" and isinstance(path, str):
        rel = rel_path(cwd, path)
        if owners := claimed_by_others(conn, channel, name, rel):
            notes.append(f"agent-bus warning: {rel} is claimed by {', '.join(owners)}. Coordinate with send before editing it.")
    return hook_output(event, unread(conn, channel, name), notes)


def run_hook(agent):
    # A failing hook would show up on every tool call: report on stderr and exit 0.
    try:
        data = json.load(sys.stdin)
        with closing(connect()) as conn:
            out = handle_hook(conn, agent, data)
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f"agent-bus hook: {e}", file=sys.stderr)
        return
    if out:
        print(out)


def do_send(agent, cwd, to, message):
    channel, name = identity(agent, cwd)
    with closing(connect()) as conn:
        known = {r[0] for r in conn.execute("SELECT name FROM agents WHERE channel = ?", (channel,))}
        woke = wake(conn, channel, send(conn, channel, name, to, message))
    if not woke:
        notify(name, to, message)
    warn = "" if to == "*" or to in known else f" (warning: {to} hasn't appeared on the bus yet; known names: {', '.join(sorted(known))})"
    return f"Sent to {to}{' (Codex session woken via codex queue)' if woke else ''}.{warn}"


def do_inbox(agent, cwd):
    channel, name = identity(agent, cwd)
    with closing(connect()) as conn:
        return format_messages(unread(conn, channel, name)) or "No unread messages."


def do_board(agent, cwd, as_json=False):
    channel, name = identity(agent, cwd)
    with closing(connect()) as conn:
        rows = board(conn, channel)
    if as_json:
        # The island is the human's view: sending a reply touch()es human@<branch>, which is not an agent row.
        return json.dumps([r for r in rows if not r["name"].startswith("human@")], ensure_ascii=False)
    return "\n".join(
        f"- {r['name']}{' (you)' if r['name'] == name else ''} | {r['feature'] or '—'} | {r['state'] or '—'} | seen {r['last_seen']}"
        + (f" | claims: {', '.join(r['claims'])}" if r["claims"] else "")
        for r in rows
    ) or "No agents on the bus."


def do_feed(agent, cwd, since=None):
    with closing(connect()) as conn:
        return json.dumps(feed(conn, since), ensure_ascii=False)


def do_claim(agent, cwd, *paths):
    channel, name = identity(agent, cwd)
    rels = [rel_path(cwd, p) for p in paths]
    with closing(connect()) as conn:
        overlaps = claim(conn, channel, name, rels)
    return f"Claimed by {name}: {', '.join(rels)}." + "".join(
        f"\nwarning: {p} is also claimed by {o}; coordinate with send." for p, o in overlaps
    )


def do_release(agent, cwd, *paths):
    channel, name = identity(agent, cwd)
    with closing(connect()) as conn:
        release(conn, channel, name, [rel_path(cwd, p) for p in paths] or None)
    return "Released."


def run_server(agent):
    from mcp.server.mcpserver import MCPServer

    # Identity is recomputed on every call, like the hook: it follows a branch checkout.
    channel, name = identity(agent, os.getcwd())
    with closing(connect()) as conn:
        touch(conn, channel, name)
    mcp = MCPServer(
        "agent-bus",
        instructions=(
            f"You are \"{agent}@<current branch>\" (currently {name}) on this repo's bus. Other agents (Claude, Codex) may be "
            "working on this repo in parallel, in other worktrees. When starting a feature, call set_status and claim the "
            "files or directories you will change; check board to see who is doing what; warn with send before touching "
            "shared code. Successful git push / gh pr merge are announced automatically. Release your claims when done. "
            "Incoming messages are injected into your context automatically."
        ),
    )

    @mcp.tool(name="send")
    def tool_send(to: str, message: str) -> str:
        """Sends a message to an agent of this repo (exact name from board, e.g. codex@feat/x) or to everyone with "*"."""
        return do_send(agent, os.getcwd(), to, message)

    @mcp.tool(name="inbox")
    def tool_inbox() -> str:
        """Reads unread messages (normally injected automatically by the hook)."""
        return do_inbox(agent, os.getcwd())

    @mcp.tool(name="set_status")
    def tool_set_status(feature: str, state: str) -> str:
        """Publishes what you are working on (feature) and where you are (state), visible to the other agents."""
        channel, name = identity(agent, os.getcwd())
        with closing(connect()) as conn:
            set_status(conn, channel, name, feature, state)
        return f"Status of {name} updated."

    @mcp.tool(name="board")
    def tool_board() -> str:
        """Lists this repo's agents: name, feature, state, last activity, claimed paths."""
        return do_board(agent, os.getcwd())

    @mcp.tool(name="claim")
    def tool_claim(paths: list[str]) -> str:
        """Claims files or directories you are about to change (relative to the repo root); other agents get warned."""
        return do_claim(agent, os.getcwd(), *paths)

    @mcp.tool(name="release")
    def tool_release(paths: list[str] | None = None) -> str:
        """Releases claimed paths, or all of your claims when paths is omitted."""
        return do_release(agent, os.getcwd(), *(paths or []))

    mcp.run()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="agent-bus: messages and status board between coding agents of a repo.")
    cli = {"board": do_board, "inbox": do_inbox, "send": do_send, "claim": do_claim, "release": do_release, "feed": do_feed}
    p.add_argument("mode", choices=["serve", "hook", *cli])
    p.add_argument("args", nargs="*", help="send: <to> <message> (use -- before a message starting with -); claim/release: <paths>")
    p.add_argument("--agent", choices=["claude", "codex", "human"], help="required for serve/hook; CLI default: human")
    p.add_argument("--json", action="store_true", help="board: JSON output (feed always prints JSON)")
    p.add_argument("--since", type=int, help="feed: messages after this id; omitted → latest id only, no backlog")
    a = p.parse_args()
    if a.mode in ("serve", "hook") and a.agent in (None, "human"):
        p.error(f"{a.mode} needs --agent claude or --agent codex")
    expected = {"board": 0, "inbox": 0, "send": 2, "serve": 0, "hook": 0, "feed": 0}.get(a.mode)
    if (expected is not None and len(a.args) != expected) or (a.mode == "claim" and not a.args):
        p.error(f"wrong number of arguments for {a.mode}")
    if a.mode == "serve":
        run_server(a.agent)
    elif a.mode == "hook":
        run_hook(a.agent)
    else:
        kw = {"board": {"as_json": a.json}, "feed": {"since": a.since}}.get(a.mode, {})
        print(cli[a.mode](a.agent or "human", os.getcwd(), *a.args, **kw))
