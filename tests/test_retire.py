import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import ledger, retire

# Screens as `herdr agent read --source visible` showed them in the lab (Claude Code 2.1.296).
DIALOG = """⏺ ok
✻ Cooked for 3s · done 6:38 PM · 1 shell still running
❯ /exit
────────────────────────────────────────
  Background work is running
  The following will stop when you exit:
  shell · sleep 600
  ❯ 1. Exit and stop tasks
    2. Move to background and exit
    3. Stay
  Enter to confirm · Esc to cancel
"""
MENU = """❯ /exit
────────────────────────────────────────
  ❯ /exit                                                Exit the CLI
    /context                                             Visualize current context usage as a colored grid
"""
PERMISSION = """❯ /exit
────────────────────────────────────────
 Bash command
   rm -rf build
 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)
"""
EXITED = DIALOG + """Resume this session with:
claude --resume 3e29d369-c9ad-4aad-b983-7c6e4ec01c63
gabriel-mbp-dev:.athena-lab-sandbox gabriel$
"""


class RetireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "plt-1"
        git(self.repo, "worktree", "add", "-q", "-b", "gabriel/plt-1", str(self.wt), "main")
        ledger.append("spawn", "plt-1", repo=str(self.repo), path=os.path.realpath(self.wt), branch="gabriel/plt-1",
                      workspace="w5", pane="w5:p1", terminal="term_1")
        self.owned = [
            {"match": ["agent", "get", "plt-1"], "stdout": {"result": {"agent": {"name": "plt-1"}}}},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(self.wt)}}}}},
        ]
        write_scenario(self.tmp.name, self.owned)
        os.environ["ATHENA_EXIT_GRACE"] = "0"

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_retire_refuses_unpushed(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        with self.assertRaises(retire.RetireError) as ctx:
            retire.retire("plt-1")
        self.assertIn("1 unpushed commit", str(ctx.exception))
        self.assertEqual(ledger.workers()["plt-1"]["state"], "live")
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["worktree", "remove"]], [])

    def test_retire_refuses_dirty(self):
        (self.wt / "b.txt").write_text("b\n")
        with self.assertRaises(retire.RetireError) as ctx:
            retire.retire("plt-1")
        self.assertIn("uncommitted", str(ctx.exception))

    def test_retire_writes_backup_ref_first(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        head = git(self.wt, "rev-parse", "HEAD")
        with self.assertRaises(retire.RetireError):
            retire.retire("plt-1")
        refs = git(self.repo, "for-each-ref", "--format=%(objectname)", "refs/athena-backup/plt-1/")
        self.assertEqual(refs.splitlines(), [head])

    def test_retire_clean_pushed(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        git(self.wt, "push", "-q", "-u", "origin", "gabriel/plt-1")
        result = retire.retire("plt-1")
        calls = logged_calls(self.tmp.name)
        self.assertIn(["agent", "prompt", "plt-1", "/exit"], calls)
        self.assertIn(["worktree", "remove", "--workspace", "w5"], calls)
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")
        self.assertTrue(result["backup"].startswith("refs/athena-backup/plt-1/"))

    def test_retire_force_passes_force(self):
        (self.wt / "b.txt").write_text("b\n")
        retire.retire("plt-1", force=True)
        self.assertIn(["worktree", "remove", "--workspace", "w5", "--force"], logged_calls(self.tmp.name))

    def test_retire_force_backs_up_uncommitted(self):
        (self.wt / "README.md").write_text("changed\n")
        result = retire.retire("plt-1", force=True)
        self.assertTrue(result["wip_backup"].endswith("-wip"))
        self.assertIn("changed", git(self.repo, "show", result["wip_backup"] + ":README.md"))

    def test_retire_never_touches_reused_ids(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        git(self.wt, "push", "-q", "-u", "origin", "gabriel/plt-1")
        # After a herdr restart, w5 is someone else's space and plt-1 is not a herdr agent.
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": "/Users/x/other"}}}}},
        ])
        result = retire.retire("plt-1")
        calls = logged_calls(self.tmp.name)
        self.assertEqual([c for c in calls if c[:2] in (["agent", "prompt"], ["worktree", "remove"])], [])
        self.assertTrue(result["removed"])
        self.assertFalse(self.wt.exists())

    def gone_after(self, n):
        """agent get finds plt-1 for the first n calls, then herdr reports no such agent."""
        return [{"match": ["agent", "get", "plt-1"], "stdout": {"result": {"agent": {"name": "plt-1"}}}, "times": n},
                {"match": ["agent", "get", "plt-1"], "stdout": "",
                 "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1}] + self.owned[1:]

    def test_retire_exit_submitted_by_the_prompt_sends_nothing_more(self):
        write_scenario(self.tmp.name, self.gone_after(1))
        retire.retire("plt-1")
        verbs = [c[:2] for c in logged_calls(self.tmp.name)]
        self.assertIn(["agent", "prompt"], verbs)
        self.assertNotIn(["agent", "send-keys"], verbs)
        self.assertNotIn(["pane", "close"], verbs)

    def test_retire_sends_enter_when_exit_sits_in_the_menu(self):
        # /exit typed but not submitted: the agent is still there after the prompt, gone after one more Enter.
        write_scenario(self.tmp.name, [{"match": ["agent", "read", "plt-1"], "stdout": MENU}] + self.gone_after(2))
        retire.retire("plt-1")
        calls = logged_calls(self.tmp.name)
        self.assertIn(["agent", "send-keys", "plt-1", "enter"], calls)
        self.assertNotIn(["pane", "close"], [c[:2] for c in calls])
        self.assertLess([c[:2] for c in calls].index(["agent", "send-keys"]),
                        [c[:2] for c in calls].index(["worktree", "remove"]))

    def test_retire_closes_its_pane_when_claude_will_not_exit(self):
        retire.retire("plt-1")
        calls = logged_calls(self.tmp.name)
        verbs = [c[:2] for c in calls]
        self.assertIn(["pane", "close", "w5:p1"], calls)
        self.assertLess(verbs.index(["pane", "close"]), verbs.index(["worktree", "remove"]))

    def test_retire_refuses_when_claude_will_not_exit_and_pane_is_not_ours(self):
        write_scenario(self.tmp.name, [self.owned[0]])
        with self.assertRaises(retire.RetireError):
            retire.retire("plt-1")
        self.assertEqual(ledger.workers()["plt-1"]["state"], "live")
        self.assertNotIn(["worktree", "remove"], [c[:2] for c in logged_calls(self.tmp.name)])

    def test_retire_orphaned_checkout_needs_force_then_deletes_it(self):
        git(self.repo, "worktree", "remove", "--force", str(self.wt))
        self.wt.mkdir(parents=True)
        (self.wt / "leftover.txt").write_text("x\n")
        with self.assertRaises(retire.RetireError) as ctx:
            retire.retire("plt-1")
        self.assertIn("no longer a git worktree", str(ctx.exception))
        self.assertTrue(self.wt.exists())
        result = retire.retire("plt-1", force=True)
        self.assertTrue(result["removed"])
        self.assertFalse(self.wt.exists())
        self.assertIn(["workspace", "close", "w5"], logged_calls(self.tmp.name))
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")

    def test_retire_unknown(self):
        with self.assertRaises(retire.RetireError):
            retire.retire("nope")


GONE = {"match": ["agent", "get"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}',
        "exit": 1}


class ExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.owned = {"match": ["agent", "get", "w"], "stdout": {"result": {"agent": {"name": "w"}}}}

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def presses(self):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "send-keys"]]

    def test_the_background_work_dialog_is_confirmed_with_one_enter(self):
        write_scenario(self.tmp.name, [{**GONE, "after": ["agent", "send-keys"]}, self.owned,
                                       {"match": ["agent", "read", "w"], "stdout": DIALOG}])
        self.assertTrue(retire.exit_claude("w", 5))
        self.assertEqual(self.presses(), [["agent", "send-keys", "w", "enter"]])
        calls = logged_calls(self.tmp.name)
        self.assertLess(calls.index(["agent", "prompt", "w", "/exit"]), calls.index(self.presses()[0]))

    def test_any_other_dialog_gets_no_key(self):
        write_scenario(self.tmp.name, [self.owned, {"match": ["agent", "read", "w"], "stdout": PERMISSION}])
        self.assertFalse(retire.exit_claude("w", 0.3))
        self.assertEqual(self.presses(), [])

    def test_an_unreadable_screen_gets_no_key(self):
        write_scenario(self.tmp.name, [self.owned, {"match": ["agent", "read"], "stdout": "", "exit": 1}])
        self.assertFalse(retire.exit_claude("w", 0.3))
        self.assertEqual(self.presses(), [])

    def test_a_claude_on_its_way_out_gets_no_more_keys(self):
        # The dialog stays in the scrollback after Claude exits; herdr notices a moment later.
        write_scenario(self.tmp.name, [{**self.owned, "times": 3}, GONE,
                                       {"match": ["agent", "read", "w"], "stdout": EXITED}])
        self.assertTrue(retire.exit_claude("w", 5))
        self.assertEqual(self.presses(), [])

    def test_exit_screen_reads_the_bottom_of_the_screen(self):
        self.assertEqual(retire.exit_screen(DIALOG), "confirm")
        self.assertEqual(retire.exit_screen(MENU), "submit")
        self.assertEqual(retire.exit_screen(EXITED), "exited")
        self.assertIsNone(retire.exit_screen(PERMISSION))
        self.assertIsNone(retire.exit_screen(""))
        # An earlier exit's lines above a new Claude's dialog are history, not the current state.
        self.assertEqual(retire.exit_screen("Resume this session with:\nclaude --resume x\n$ claude\n" + DIALOG), "confirm")
        # "Move to background and exit" selected is not the choice athena makes.
        self.assertIsNone(retire.exit_screen(DIALOG.replace("  ❯ 1.", "    1.").replace("    2. Move", "  ❯ 2. Move")))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_cli_status_json(self):
        ledger.append("spawn", "a", pane="w2:p1", branch="gabriel/a")
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_status": "idle"}}}}])
        out = subprocess.run([str(helpers.ROOT / "bin" / "athena"), "status", "--json"], capture_output=True, text=True,
                             env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data[0]["name"], "a")
        self.assertEqual(data[0]["state"], "idle")

    def test_cli_status_table_empty(self):
        out = subprocess.run([str(helpers.ROOT / "bin" / "athena"), "status"], capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("no live workers", out.stdout)

    def test_cli_nudge_submits_a_bare_slash_command(self):
        from athena_lib import cli
        ledger.append("spawn", "plt-9", pane="w9:p1")
        cli.main(["nudge", "plt-9", "/exit"])
        cli.main(["nudge", "plt-9", "/goal", "clear"])
        calls = logged_calls(self.tmp.name)
        self.assertEqual(calls.count(["agent", "send-keys", "plt-9", "enter"]), 1)

    def test_cli_nudge_only_workers(self):
        out = subprocess.run([str(helpers.ROOT / "bin" / "athena"), "nudge", "claude-w2g", "hi"], capture_output=True,
                             text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 2)
        self.assertIn("not a live athena worker", out.stderr)

    def test_cli_spawn_error_exit_code(self):
        out = subprocess.run([str(helpers.ROOT / "bin" / "athena"), "spawn", "--repo", self.tmp.name, "--branch", "x",
                              "--name", "BAD", "--brief", "/dev/null", "--goal", "g"], capture_output=True, text=True,
                             env=dict(os.environ))
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid worker name", out.stderr)


if __name__ == "__main__":
    unittest.main()
