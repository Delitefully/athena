import io
import subprocess
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout

from tests import helpers
from tests.test_herdr import write_scenario
from athena_lib import cli, gitops, hookstatus, ledger, pr, status, watch


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_urgency_order(self):
        order = sorted(["working", "done", "blocked", "idle", "unknown"], key=status.urgency)
        self.assertEqual(order[:3], ["blocked", "done", "idle"])
        self.assertLess(status.urgency("idle-unreported"), status.urgency("idle"))  # HQ acts on it, as on a done

    def test_status_shows_a_worker_the_gate_gave_way_on(self):
        ledger.append("spawn", "abc-1", branch="you/abc-1")
        for _ in range(hookstatus.MAX_STOP_BLOCKS + 1):
            hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": "pushed"}, "abc-1", gated=True)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        self.assertRegex(out.getvalue(), r"(?m)^abc-1\s+idle-unreported\s+herdr=")

    def test_report_beats_herdr_status(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_status": "working"}}}}])
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": 'ATHENA-REPORT {"status":"DONE"}'}, "a")
        s = status.collect({"name": "a", "pane": "w2:p1"})
        self.assertEqual((s["state"], s["herdr"], s["state_source"]), ("done", "working", "hook"))

    def test_herdr_fills_unknown(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_status": "blocked"}}}}])
        s = status.collect({"name": "a", "pane": "w2:p1"})
        self.assertEqual((s["state"], s["state_source"]), ("blocked", "herdr"))

    def test_pr_summary_parses_checks(self):
        data = {"number": 7, "url": "u", "state": "OPEN", "isDraft": True, "reviewDecision": "CHANGES_REQUESTED",
                "headRefOid": "abcdef1234567890", "statusCheckRollup": [
                    {"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS", "conclusion": ""}],
                "comments": [{}, {}], "reviews": [{"body": "fix"}, {"body": ""}]}
        r = pr.reduce(data)
        self.assertEqual((r["checks"], r["comments"], r["head"], r["draft"]), ("pending", 3, "abcdef123456", True))
        self.assertEqual(pr.reduce_checks([{"conclusion": "FAILURE"}, {"conclusion": "SUCCESS"}]), "failure")
        self.assertEqual(pr.reduce_checks([{"conclusion": "SUCCESS"}]), "success")
        self.assertEqual(pr.reduce_checks([]), "none")


class GoalWakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def goal_ticks(self, *flags):
        """The watch's lines for a worker whose goal_unconfirmed reads these on successive ticks."""
        ledger.append("spawn", "abc-1", pane="w1:p1", repo=None)
        prev, told = None, []
        for flag in flags:
            row = {"name": "abc-1", "state": "working", "pr": None, "report": None, "detail": None,
                   "goal_unconfirmed": flag}
            with mock.patch.object(status, "collect", return_value=row), mock.patch.object(watch.sockets, "hq_missing", return_value=None):
                cur = watch.snapshot(with_pr=False, prev=prev)
            told += watch.diff(prev, cur) if prev else []
            prev = cur
        return [line for line in told if "goal unconfirmed" in line]

    def test_an_unconfirmed_goal_wakes_hq_on_the_second_tick_only(self):
        lines = self.goal_ticks(True, True, True, True)
        self.assertEqual(lines, ["abc-1 goal unconfirmed (no goal record in its transcript): check its pane, do not resend blindly"])
        self.assertEqual(watch.classify(lines[0]), "wake")

    def test_a_goal_that_shows_up_by_the_second_tick_wakes_nobody(self):
        self.assertEqual(self.goal_ticks(True, False, False), [])

    def test_a_flag_that_clears_and_comes_back_is_told_again(self):
        self.assertEqual(len(self.goal_ticks(True, True, False, True, True)), 2)


class WatchTest(unittest.TestCase):
    def test_diff_lines(self):
        prev = {"workers": {"a": {"state": "working", "pr": None, "report": None},
                            "b": {"state": "idle", "pr": {"checks": "pending", "comments": 1, "review": "", "state": "OPEN"}}},
                "mains": {"/r/web": "1a2b3c4"}}
        cur = {"workers": {"a": {"state": "done", "pr": {"checks": "pending", "comments": 0, "review": "", "state": "OPEN", "url": "u"},
                                 "report": {"status": "DONE"}},
                           "b": {"state": "idle", "pr": {"checks": "failure", "comments": 3, "review": "CHANGES_REQUESTED", "state": "OPEN"}},
                           "c": {"state": "working", "pr": None, "report": None}},
               "mains": {"/r/web": "4d5e6f7"}}
        lines = watch.diff(prev, cur)
        self.assertIn("a state working->done", lines)
        self.assertIn("a report DONE", lines)
        self.assertIn("a pr opened u", lines)
        self.assertIn("b checks pending->failure", lines)
        self.assertIn("b review comments +2", lines)
        self.assertIn("b review CHANGES_REQUESTED", lines)
        self.assertIn("c new worker (working)", lines)
        self.assertIn("web main moved 1a2b3c4..4d5e6f7", lines)

    def test_snapshot_keeps_pr_when_gh_fails(self):
        import tempfile as _t
        tmp = _t.TemporaryDirectory()
        env = helpers.isolated_env(tmp.name)
        try:
            ledger.append("spawn", "a", pane="w2:p1")
            prev = {"workers": {"a": {"state": "working", "pr": {"checks": "pending", "url": "u"}, "report": None}}}
            cur = watch.snapshot(with_pr=False, prev=prev)
            self.assertEqual(cur["workers"]["a"]["pr"]["url"], "u")
            self.assertNotIn("a pr opened u", watch.diff(prev, cur))
        finally:
            env.restore()
            tmp.cleanup()

    def test_a_fetch_that_times_out_does_not_crash_the_tick(self):
        tmp = tempfile.TemporaryDirectory()
        env = helpers.isolated_env(tmp.name)
        calls = []

        def git(repo, *args, check=True, timeout=None):
            calls.append((args, timeout))
            if args[0] == "fetch":
                raise subprocess.TimeoutExpired(["git", *args], timeout)
            return "abc1234"
        try:
            ledger.append("spawn", "a", pane="w2:p1", repo="/r/web")
            with mock.patch.object(watch.gitops, "git", side_effect=git), \
                    mock.patch.object(watch.gitops, "default_branch", return_value="main"):
                cur = watch.snapshot(fetch=True, with_pr=False)
            self.assertIn((("fetch", "--quiet", "origin"), watch.FETCH_TIMEOUT), calls)
            self.assertEqual(watch.FETCH_TIMEOUT, 60)
            self.assertEqual(cur["mains"], {"/r/web": "abc1234"})  # read from what origin/main was before
        finally:
            env.restore()
            tmp.cleanup()

    def test_diff_retired_and_quiet(self):
        prev = {"workers": {"a": {"state": "working", "pr": None, "report": None}}, "mains": {}}
        self.assertEqual(watch.diff(prev, prev), [])
        self.assertEqual(watch.diff(prev, {"workers": {}, "mains": {}}), ["a gone from the ledger"])


if __name__ == "__main__":
    unittest.main()
