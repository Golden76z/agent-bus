import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.mkdir(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_name_is_agent_at_branch(self):
        _, name = bus.identity("claude", self.repo)
        self.assertEqual(name, "claude@main")

    def test_worktrees_of_same_repo_share_channel(self):
        wt = os.path.join(self.tmp.name, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "feat/export", wt)
        ch_main, _ = bus.identity("claude", self.repo)
        ch_wt, name_wt = bus.identity("codex", wt)
        self.assertEqual(ch_main, ch_wt)
        self.assertEqual(name_wt, "codex@feat/export")

    def test_repo_without_commits_uses_branch_name(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        git(empty, "init", "-q", "-b", "dev")
        self.assertEqual(bus.identity("claude", empty)[1], "claude@dev")

    def test_non_git_dir_falls_back_to_dir(self):
        plain = os.path.join(self.tmp.name, "plain")
        os.mkdir(plain)
        channel, name = bus.identity("claude", plain)
        self.assertEqual(channel, plain)
        self.assertEqual(name, "claude@plain")


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bus.connect(os.path.join(self.tmp.name, "bus.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_direct_message_is_delivered_once(self):
        bus.send(self.db, "ch", "claude@a", "codex@b", "hi")
        msgs = bus.unread(self.db, "ch", "codex@b")
        self.assertEqual([(m["sender"], m["body"]) for m in msgs], [("claude@a", "hi")])
        self.assertEqual(bus.unread(self.db, "ch", "codex@b"), [])

    def test_message_not_delivered_to_other_agent_or_channel(self):
        bus.send(self.db, "ch", "claude@a", "codex@b", "private")
        self.assertEqual(bus.unread(self.db, "ch", "claude@c"), [])
        self.assertEqual(bus.unread(self.db, "other", "codex@b"), [])

    def test_broadcast_reaches_others_but_not_sender(self):
        bus.touch(self.db, "ch", "codex@b")
        bus.send(self.db, "ch", "claude@a", "*", "all")
        self.assertEqual(len(bus.unread(self.db, "ch", "codex@b")), 1)
        self.assertEqual(bus.unread(self.db, "ch", "claude@a"), [])

    def test_new_agent_does_not_get_backlog_of_broadcasts(self):
        bus.send(self.db, "ch", "claude@a", "*", "old")
        self.assertEqual(bus.unread(self.db, "ch", "codex@new"), [])
        bus.send(self.db, "ch", "claude@a", "*", "recent")
        self.assertEqual([m["body"] for m in bus.unread(self.db, "ch", "codex@new")], ["recent"])

    def test_direct_message_to_not_yet_seen_agent_is_kept(self):
        bus.send(self.db, "ch", "claude@a", "codex@later", "for later")
        self.assertEqual([m["body"] for m in bus.unread(self.db, "ch", "codex@later")], ["for later"])

    def test_board_shows_status_and_presence(self):
        bus.set_status(self.db, "ch", "claude@a", "login", "running tests")
        bus.touch(self.db, "ch", "codex@b")
        board = {r["name"]: r for r in bus.board(self.db, "ch")}
        self.assertEqual(board["claude@a"]["feature"], "login")
        self.assertEqual(board["claude@a"]["state"], "running tests")
        self.assertIn("codex@b", board)
        self.assertEqual(bus.board(self.db, "other"), [])


    def test_board_drops_agents_idle_for_more_than_a_week(self):
        bus.touch(self.db, "ch", "claude@old")
        bus.touch(self.db, "ch", "codex@fresh")
        self.db.execute("UPDATE agents SET last_seen = '2000-01-01 00:00' WHERE name = 'claude@old'")
        self.assertEqual([r["name"] for r in bus.board(self.db, "ch")], ["codex@fresh"])

    def test_claim_reports_overlap_with_other_agents_only(self):
        self.assertEqual(bus.claim(self.db, "ch", "claude@a", ["src"]), [])
        self.assertEqual(bus.claim(self.db, "ch", "claude@a", ["src/app.py"]), [])
        self.assertEqual(bus.claim(self.db, "ch", "codex@b", ["src/app.py", "README.md"]), [("src/app.py", "claude@a")])
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "src/other.py"), ["claude@a"])
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "srcx.py"), [])

    def test_claiming_the_repo_root_overlaps_everything(self):
        bus.claim(self.db, "ch", "claude@a", ["."])
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "src/a.py"), ["claude@a"])

    def test_release_frees_claims_and_board_lists_them(self):
        bus.claim(self.db, "ch", "claude@a", ["a.py", "b.py"])
        self.assertEqual({r["name"]: r["claims"] for r in bus.board(self.db, "ch")}, {"claude@a": ["a.py", "b.py"]})
        bus.release(self.db, "ch", "claude@a", ["a.py"])
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "a.py"), [])
        bus.release(self.db, "ch", "claude@a")
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "b.py"), [])

    def test_pruned_agent_loses_its_claims(self):
        bus.claim(self.db, "ch", "claude@old", ["a.py"])
        self.db.execute("UPDATE agents SET last_seen = '2000-01-01 00:00'")
        bus.board(self.db, "ch")
        self.assertEqual(bus.claimed_by_others(self.db, "ch", "codex@b", "a.py"), [])


class RepoTest(unittest.TestCase):
    """Features that depend on a real git repo: pruning by branch, hook, CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.mkdir(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.db = bus.connect(os.path.join(self.tmp.name, "bus.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def hook(self, agent, **data):
        return bus.handle_hook(self.db, agent, {"cwd": self.repo, **data})

    def test_board_drops_agents_whose_branch_was_deleted(self):
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        for name in ("claude@main", "codex@feat/merged", f"codex@{sha}"):
            bus.touch(self.db, self.repo, name)
        self.assertEqual(sorted(r["name"] for r in bus.board(self.db, self.repo)), sorted(["claude@main", f"codex@{sha}"]))

    def test_successful_push_or_merge_is_broadcast(self):
        bus.touch(self.db, self.repo, "codex@other")
        for cmd in ("git add . && git push origin main", "gh pr merge 12 --squash"):
            self.hook("claude", hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": cmd})
        bodies = [m["body"] for m in bus.unread(self.db, self.repo, "codex@other")]
        self.assertEqual(len(bodies), 2)
        self.assertIn("claude@main", bodies[0])
        self.assertIn("git push origin main", bodies[0])
        self.assertIn("gh pr merge 12", bodies[1])

    def test_other_commands_failures_and_dry_runs_are_not_broadcast(self):
        bus.touch(self.db, self.repo, "codex@other")
        for cmd, resp in (("git status", {}), ("git push --dry-run", {}), ("git push", {"exit_code": 1})):
            self.hook("claude", hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": cmd}, tool_response=resp)
        self.hook("claude", hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "git push"})
        self.assertEqual(bus.unread(self.db, self.repo, "codex@other"), [])

    def test_edit_of_file_claimed_by_another_agent_warns(self):
        bus.claim(self.db, self.repo, "codex@feat/x", ["src"])
        out = self.hook("claude", hook_event_name="PreToolUse", tool_name="Edit",
                        tool_input={"file_path": os.path.join(self.repo, "src", "app.py")})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("src/app.py", ctx)
        self.assertIn("codex@feat/x", ctx)
        self.assertIsNone(self.hook("claude", hook_event_name="PreToolUse", tool_name="Edit",
                                    tool_input={"file_path": os.path.join(self.repo, "README.md")}))

    def test_push_mentioned_in_quotes_is_not_broadcast_but_chained_push_is(self):
        bus.touch(self.db, self.repo, "codex@other")
        for cmd in ('git commit -m "fix before git push"', 'gh pr create --body "gh pr merge later"'):
            self.hook("claude", hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": cmd})
        self.assertEqual(bus.unread(self.db, self.repo, "codex@other"), [])
        self.hook("claude", hook_event_name="PostToolUse", tool_name="Bash",
                  tool_input={"command": "npm publish --dry-run && git push"})
        self.assertEqual(len(bus.unread(self.db, self.repo, "codex@other")), 1)

    def test_credentials_in_pushed_url_are_redacted(self):
        bus.touch(self.db, self.repo, "codex@other")
        self.hook("claude", hook_event_name="PostToolUse", tool_name="Bash",
                  tool_input={"command": "git push https://ghp_secret@github.com/o/r main"})
        body = bus.unread(self.db, self.repo, "codex@other")[0]["body"]
        self.assertNotIn("ghp_secret", body)
        self.assertIn("github.com/o/r", body)

    def test_claim_paths_are_relative_to_repo_root_even_from_a_subdirectory(self):
        sub = os.path.join(self.repo, "web")
        os.mkdir(sub)
        self.assertEqual(bus.rel_path(sub, "src/app.py"), "src/app.py")
        self.assertEqual(bus.rel_path(sub, os.path.join(self.repo, "web", "x.py")), "web/x.py")

    def test_cli_lets_a_human_send_read_and_see_the_board(self):
        env = {**os.environ, "XDG_DATA_HOME": os.path.join(self.tmp.name, "data")}
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus.py")

        def cli(*args):
            return subprocess.run([sys.executable, script, *args], cwd=self.repo, env=env,
                                  capture_output=True, text=True, check=True).stdout

        self.assertIn("Sent to codex@main", cli("send", "codex@main", "please rebase"))
        self.assertIn("human@main", cli("board"))
        inbox = cli("inbox", "--agent", "codex")
        self.assertIn("human@main", inbox)
        self.assertIn("please rebase", inbox)
        cli("claim", "src", "docs")
        self.assertIn("claims: docs, src", cli("board"))
        cli("release", "docs")
        self.assertIn("claims: src", cli("board"))

    def test_cli_rejects_bad_arguments_and_serve_without_agent(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus.py")
        env = {**os.environ, "XDG_DATA_HOME": os.path.join(self.tmp.name, "data")}
        for args in (["board", "extra"], ["send", "only-recipient"], ["hook"], ["serve"]):
            r = subprocess.run([sys.executable, script, *args], cwd=self.repo, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 2, args)
            self.assertNotIn("Traceback", r.stderr)


class NotifyTest(unittest.TestCase):
    def test_desktop_notification_only_for_direct_messages_to_claude_or_human(self):
        calls = []
        run = lambda cmd: calls.append(cmd) or True  # noqa: E731
        for to in ("claude@a", "human@main", "codex@b", "*"):
            bus.notify("codex@x", to, "hello", run=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "notify-send")
        self.assertIn("claude@a", " ".join(calls[0]))
        self.assertIn("hello", " ".join(calls[0]))


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bus.connect(os.path.join(self.tmp.name, "bus.db"))
        self.calls = []

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def runner(self, ok=True):
        def run(cmd):
            self.calls.append(cmd)
            return ok
        return run

    def test_message_to_codex_with_known_session_is_queued_and_marked_read(self):
        bus.touch(self.db, "ch", "codex@b", session_id="thread-1")
        msg_id = bus.send(self.db, "ch", "claude@a", "codex@b", "wake up")
        self.assertTrue(bus.wake(self.db, "ch", msg_id, run=self.runner()))
        self.assertEqual(self.calls[0][:4], ["codex", "queue", "--thread", "thread-1"])
        self.assertIn("claude@a", self.calls[0][-1])
        self.assertIn("wake up", self.calls[0][-1])
        self.assertEqual(bus.unread(self.db, "ch", "codex@b"), [])

    def test_no_wake_without_session_or_for_claude_or_broadcast(self):
        bus.touch(self.db, "ch", "claude@c", session_id="s-claude")
        for to in ("codex@unknown", "claude@c", "*"):
            self.assertFalse(bus.wake(self.db, "ch", bus.send(self.db, "ch", "codex@x", to, "m"), run=self.runner()))
        self.assertEqual(self.calls, [])

    def test_failed_queue_keeps_message_for_hook(self):
        bus.touch(self.db, "ch", "codex@b", session_id="thread-1")
        msg_id = bus.send(self.db, "ch", "claude@a", "codex@b", "via hook")
        self.assertFalse(bus.wake(self.db, "ch", msg_id, run=self.runner(ok=False)))
        self.assertEqual([m["body"] for m in bus.unread(self.db, "ch", "codex@b")], ["via hook"])

    def test_schema_upgrade_adds_session_column_to_existing_db(self):
        path = os.path.join(self.tmp.name, "old.db")
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE agents (channel TEXT NOT NULL, name TEXT NOT NULL, feature TEXT, state TEXT,"
                    " last_seen TEXT NOT NULL, broadcast_cursor INTEGER NOT NULL, PRIMARY KEY (channel, name))")
        old.close()
        conn = bus.connect(path)
        bus.touch(conn, "ch", "codex@b", session_id="t")
        self.assertEqual(conn.execute("SELECT session_id FROM agents WHERE name = 'codex@b'").fetchone()[0], "t")
        conn.close()

    def test_message_already_delivered_by_hook_is_not_queued_again(self):
        bus.touch(self.db, "ch", "codex@b", session_id="thread-1")
        msg_id = bus.send(self.db, "ch", "claude@a", "codex@b", "only once")
        bus.unread(self.db, "ch", "codex@b")
        self.assertFalse(bus.wake(self.db, "ch", msg_id, run=self.runner()))
        self.assertEqual(self.calls, [])

    def test_message_is_claimed_before_queue_runs(self):
        bus.touch(self.db, "ch", "codex@b", session_id="thread-1")
        msg_id = bus.send(self.db, "ch", "claude@a", "codex@b", "during queue")
        seen_by_hook = []
        def run(cmd):
            seen_by_hook.extend(bus.unread(self.db, "ch", "codex@b"))
            return True
        self.assertTrue(bus.wake(self.db, "ch", msg_id, run=run))
        self.assertEqual(seen_by_hook, [])


class HookTest(unittest.TestCase):
    def test_no_output_without_messages(self):
        self.assertIsNone(bus.hook_output("PostToolUse", []))

    def test_output_carries_messages_as_additional_context(self):
        out = json.loads(bus.hook_output("UserPromptSubmit", [{"sender": "codex@b", "body": "done", "ts": "12:00"}]))
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "UserPromptSubmit")
        self.assertIn("codex@b", spec["additionalContext"])
        self.assertIn("done", spec["additionalContext"])


if __name__ == "__main__":
    unittest.main()
