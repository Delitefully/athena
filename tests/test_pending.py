"""A worker whose Claude stops at a startup prompt stays tracked, and its goal is sent once it is ready."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import logged_calls, write_scenario
from tests.test_spawn import CREATED
from athena_lib import ledger, paths, retire, spawn, status, watch

NOT_READY = {"error": {"code": "agent_not_ready",
                       "message": "agent plt-1 is blocked during startup and is not ready for prompts"}}
TRUST_SCREEN = "Quick safety check: Is this a project you created or one you trust?\n❯ 1. Yes, I trust this folder\n  2. No, exit\n"


def err(code, message="x"):
    return json.dumps({"error": {"code": code, "message": message}})


def agent(status_):
    return {"result": {"agent": {"name": "plt-1", "agent_status": status_}}}


class PendingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name, "proj")
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text("# PLT-1\nGOAL: fix it\n")
        self.wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def scenario(self, rules):
        write_scenario(self.tmp.name, rules + [{"match": ["worktree", "create"], "stdout": CREATED}])

    def run_spawn(self):
        return spawn.spawn(repo=self.repo, branch="gabriel/plt-1-fix", name="plt-1", brief_path=self.brief,
                           goal="PLT-1 is done when the test passes.", linear="PLT-1", title="fix it")

    def spawn_blocked(self):
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(NOT_READY), "exit": 1},
                       {"match": ["agent", "read"], "stdout": TRUST_SCREEN}])
        return self.run_spawn()

    def set_agent(self, status_, extra=None):
        """What herdr reports from now on, as the human answers the prompt."""
        write_scenario(self.tmp.name, (extra or []) + [{"match": ["agent", "get"], "stdout": agent(status_)}])

    def prompts(self):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"]]


class SpawnBlockedTest(PendingBase):
    def test_blocked_at_startup_stays_live_and_pending(self):
        result = self.spawn_blocked()
        w = ledger.workers()["plt-1"]
        self.assertEqual(w["state"], "live")
        self.assertEqual((w["workspace"], w["pane"], w["terminal"]), ("w9", "w9:p1", "term_abc"))
        self.assertEqual(w["path"], os.path.realpath(self.wt))
        self.assertTrue(w["goal_pending"].startswith("/goal PLT-1 is done when the test passes."))
        self.assertIn(str(paths.state_dir() / "briefs" / "plt-1.md"), w["goal_pending"])
        self.assertIn("agent_not_ready", w["pending_reason"])
        self.assertEqual(self.prompts(), [])
        self.assertIn(["workspace", "report-metadata", "w9", "--source", "athena", "--token", "athena=worker",
                       "--token", "linear=PLT-1"], logged_calls(self.tmp.name))
        self.assertFalse(paths.claim_file(str(self.wt)).exists())
        self.assertTrue(result["pending"])
        self.assertIn('"fix it"', result["message"])
        self.assertIn("trust", result["message"])
        self.assertIn("athena resume plt-1", result["message"])
        self.assertEqual([x["name"] for x in ledger.live()], ["plt-1"])

    def test_other_start_error_with_blocked_agent_is_pending(self):
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": err("timeout"), "exit": 1},
                       {"match": ["agent", "get"], "stdout": agent("blocked")}])
        result = self.run_spawn()
        self.assertTrue(result["pending"])
        self.assertTrue(ledger.workers()["plt-1"]["goal_pending"])

    def test_normal_spawn_sends_goal_immediately(self):
        self.scenario([])
        result = self.run_spawn()
        self.assertFalse(result.get("pending"))
        self.assertEqual(len(self.prompts()), 1)
        self.assertFalse(ledger.workers()["plt-1"].get("goal_pending"))


class DeliverTest(PendingBase):
    def test_not_sent_while_blocked(self):
        self.spawn_blocked()
        self.set_agent("blocked")
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "waiting")
        self.set_agent("unknown")
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "waiting")
        self.assertEqual(self.prompts(), [])
        self.assertTrue(ledger.workers()["plt-1"]["goal_pending"])

    def test_sent_once_when_ready(self):
        self.spawn_blocked()
        goal = ledger.workers()["plt-1"]["goal_pending"]
        self.set_agent("idle")
        outcomes = [spawn.deliver("plt-1")["outcome"] for _ in range(3)]
        self.assertEqual(outcomes, ["sent", "not-pending", "not-pending"])
        self.assertEqual(self.prompts(), [["agent", "prompt", "plt-1", goal]])
        w = ledger.workers()["plt-1"]
        self.assertFalse(w.get("goal_pending"))
        self.assertEqual((w["state"], w["prompt"]), ("live", "sent"))

    def test_rejected_as_blocked_stays_pending(self):
        self.spawn_blocked()
        self.set_agent("idle", [{"match": ["agent", "prompt"], "stdout": "", "stderr": err("agent_blocked"),
                                 "exit": 1, "times": 1}])
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "waiting")
        self.assertTrue(ledger.workers()["plt-1"]["goal_pending"])
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "sent")
        self.assertEqual(len(self.prompts()), 2)

    def test_ambiguous_failure_is_never_resent(self):
        self.spawn_blocked()
        self.set_agent("idle", [{"match": ["agent", "prompt"], "stdout": "", "stderr": err("timeout"), "exit": 1}])
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "unconfirmed")
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "not-pending")
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(ledger.workers()["plt-1"]["prompt"], "unconfirmed: timeout")

    def test_resume(self):
        self.spawn_blocked()
        self.set_agent("blocked")
        with self.assertRaises(spawn.SpawnError) as ctx:
            spawn.resume("plt-1")
        self.assertIn("answer", str(ctx.exception))
        self.set_agent("idle")
        self.assertEqual(spawn.resume("plt-1")["outcome"], "sent")
        with self.assertRaises(spawn.SpawnError) as ctx:
            spawn.resume("plt-1")
        self.assertIn("no pending goal", str(ctx.exception))
        self.assertEqual(len(self.prompts()), 1)


class WatchPendingTest(PendingBase):
    def test_watch_lines_and_single_send(self):
        self.spawn_blocked()
        self.set_agent("unknown")
        prev, lines = watch.step(None, with_pr=False)
        self.assertEqual(prev["workers"]["plt-1"]["state"], "starting")
        self.set_agent("blocked")
        prev, lines = watch.step(prev, with_pr=False)
        self.assertEqual(lines, ["plt-1 state starting->blocked (startup prompt)"])
        self.set_agent("idle")
        prev, lines = watch.step(prev, with_pr=False)
        self.assertIn("plt-1 goal sent", lines)
        for _ in range(3):
            prev, lines = watch.step(prev, with_pr=False)
            self.assertNotIn("plt-1 goal sent", lines)
        self.assertEqual(len(self.prompts()), 1)

    def test_status_and_board_state(self):
        self.spawn_blocked()
        self.set_agent("blocked")
        w = ledger.workers()["plt-1"]
        s = status.collect(w)
        self.assertEqual((s["state"], s["detail"], s["pending"]), ("blocked", "startup prompt", True))
        self.assertEqual(status.quick_state(w), "blocked")
        self.assertEqual(status.urgency(status.quick_state(w)), 0)


class RetirePendingTest(PendingBase):
    def test_retire_pending_never_types_into_it(self):
        self.spawn_blocked()
        git(self.repo, "worktree", "add", "-q", "-b", "gabriel/plt-1-fix", str(self.wt), "main")
        self.set_agent("blocked", [{"match": ["workspace", "get", "w9"],
                                    "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(self.wt)}}}}}])
        result = retire.retire("plt-1")
        self.assertEqual(self.prompts(), [])
        self.assertIn(["worktree", "remove", "--workspace", "w9"], logged_calls(self.tmp.name))
        self.assertTrue(result["removed"])
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")
        self.assertEqual(spawn.deliver("plt-1")["outcome"], "not-pending")

    def test_retire_pending_keep_worktree_closes_pane(self):
        self.spawn_blocked()
        self.set_agent("blocked", [{"match": ["workspace", "get", "w9"],
                                    "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(self.wt)}}}}}])
        retire.retire("plt-1", keep_worktree=True)
        calls = logged_calls(self.tmp.name)
        self.assertEqual(self.prompts(), [])
        self.assertIn(["pane", "close", "w9:p1"], calls)


class HardFailureTest(PendingBase):
    def owned(self):
        return {"match": ["workspace", "get", "w9"],
                "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(self.wt)}}}}}

    def test_hard_failure_after_create_cleans_up(self):
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": err("pane_not_found"), "exit": 1},
                       {"match": ["agent", "get"], "stdout": "", "stderr": err("agent_not_found"), "exit": 1},
                       self.owned()])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("pane_not_found", str(ctx.exception))
        calls = logged_calls(self.tmp.name)
        self.assertIn(["worktree", "remove", "--workspace", "w9", "--force"], calls)
        self.assertEqual(self.prompts(), [])
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["state"], w["cleaned"]), ("failed", True))
        self.assertEqual(ledger.live(), [])
        self.assertEqual(ledger.leftovers(), [])
        self.assertFalse(paths.claim_file(str(self.wt)).exists())

    def test_failed_cleanup_is_listed_and_retireable(self):
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": err("pane_not_found"), "exit": 1},
                       {"match": ["agent", "get"], "stdout": "", "stderr": err("agent_not_found"), "exit": 1},
                       {"match": ["worktree", "remove"], "stdout": "", "stderr": err("busy"), "exit": 1, "times": 1},
                       self.owned()])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("athena retire plt-1", str(ctx.exception))
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["state"], w["cleaned"]), ("failed", False))
        self.assertEqual([x["name"] for x in ledger.leftovers()], ["plt-1"])
        result = retire.retire("plt-1")
        self.assertTrue(result["removed"])
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")
        self.assertEqual(ledger.leftovers(), [])

    def test_create_failure_leaves_nothing(self):
        self.scenario([{"match": ["worktree", "create"], "stdout": "", "stderr": err("git_failed"), "exit": 1}])
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["state"], w["cleaned"]), ("failed", True))
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["worktree", "remove"]], [])
        self.assertEqual(ledger.leftovers(), [])


class CliPendingTest(PendingBase):
    def cli(self, *args):
        return subprocess.run([str(helpers.ROOT / "bin" / "athena"), *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def test_spawn_pending_exit_code_and_message(self):
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(NOT_READY), "exit": 1},
                       {"match": ["agent", "read"], "stdout": TRUST_SCREEN}])
        out = self.cli("spawn", "--repo", str(self.repo), "--branch", "gabriel/plt-1-fix", "--name", "plt-1",
                       "--brief", str(self.brief), "--goal", "PLT-1 is done when x.", "--title", "fix it")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn("startup prompt", out.stderr)
        self.assertIn("athena resume plt-1", out.stderr)
        self.assertTrue(json.loads(out.stdout)["pending"])

    def test_status_nudge_and_resume(self):
        self.spawn_blocked()
        self.set_agent("blocked")
        out = self.cli("status")
        self.assertIn("goal pending (startup prompt)", out.stdout)
        out = self.cli("nudge", "plt-1", "hello")
        self.assertEqual(out.returncode, 2)
        self.assertIn("athena resume plt-1", out.stderr)
        out = self.cli("resume", "plt-1")
        self.assertEqual(out.returncode, 2)
        self.set_agent("idle")
        out = self.cli("resume", "plt-1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.prompts()), 1)

    def test_status_lists_leftovers(self):
        ledger.append("failed", "plt-2", reason="boom", cleaned=False, workspace="w3", path="/x")
        out = self.cli("status")
        self.assertIn("plt-2", out.stdout)
        self.assertIn("athena retire plt-2", out.stdout)


if __name__ == "__main__":
    unittest.main()
