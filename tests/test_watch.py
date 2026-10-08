import tempfile
import unittest

from tests import helpers
from tests.test_herdr import write_scenario
from athena_lib import hookstatus, ledger, pr, status, watch


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


class WatchTest(unittest.TestCase):
    def test_diff_lines(self):
        prev = {"workers": {"a": {"state": "working", "pr": None, "report": None},
                            "b": {"state": "idle", "pr": {"checks": "pending", "comments": 1, "review": "", "state": "OPEN"}}},
                "mains": {"/r/platform": "1a2b3c4"}}
        cur = {"workers": {"a": {"state": "done", "pr": {"checks": "pending", "comments": 0, "review": "", "state": "OPEN", "url": "u"},
                                 "report": {"status": "DONE"}},
                           "b": {"state": "idle", "pr": {"checks": "failure", "comments": 3, "review": "CHANGES_REQUESTED", "state": "OPEN"}},
                           "c": {"state": "working", "pr": None, "report": None}},
               "mains": {"/r/platform": "4d5e6f7"}}
        lines = watch.diff(prev, cur)
        self.assertIn("a state working->done", lines)
        self.assertIn("a report DONE", lines)
        self.assertIn("a pr opened u", lines)
        self.assertIn("b checks pending->failure", lines)
        self.assertIn("b review comments +2", lines)
        self.assertIn("b review CHANGES_REQUESTED", lines)
        self.assertIn("c new worker (working)", lines)
        self.assertIn("platform main moved 1a2b3c4..4d5e6f7", lines)

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

    def test_diff_retired_and_quiet(self):
        prev = {"workers": {"a": {"state": "working", "pr": None, "report": None}}, "mains": {}}
        self.assertEqual(watch.diff(prev, prev), [])
        self.assertEqual(watch.diff(prev, {"workers": {}, "mains": {}}), ["a gone from the ledger"])


if __name__ == "__main__":
    unittest.main()
