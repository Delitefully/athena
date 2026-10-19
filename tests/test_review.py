"""athena review: HQ's own review verdict per live worker, and the in-review stage it gives the worker's PR."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import cli, config, ledger, watch

HEAD = "4f2c1ab9d0e3"


def _pr(**kw):
    """A reduced PR as pr.summary gives it: green, on a branch with no review rule."""
    return {"number": 7, "url": "https://github.com/o/web/pull/7", "title": "change", "state": "OPEN", "draft": False,
            "review": "", "checks": "success", "base": "main", "mergeable": "MERGEABLE", "head": HEAD,
            "requested": [], "reviews": {}, "auto_merge": False, "queued": False, **kw}


class ReviewCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        config.write(dict(config.DEFAULTS))
        ledger.append("spawn", "abc-7", repo="/src/web", branch="you/abc-7")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def athena(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_start_records_the_prs_current_head(self):
        with mock.patch.object(cli.pr, "summary", return_value=_pr()) as summary, \
                mock.patch.object(ledger.time, "time", return_value=1000.0):
            code, out, err = self.athena("review", "abc-7", "start")
        self.assertEqual(code, 0, err)
        summary.assert_called_once_with("/src/web", "you/abc-7")
        record = {"verdict": "start", "head": HEAD, "at": 1000.0}
        self.assertEqual(ledger.workers()["abc-7"]["review"], record)
        self.assertEqual(json.loads(out)["review"], record)

    def test_pass_and_fail_take_the_head_they_reviewed(self):
        with mock.patch.object(cli.pr, "summary") as summary:
            self.assertEqual(self.athena("review", "abc-7", "pass", "--head", "4F2C1AB")[0], 0)
            self.assertEqual(ledger.workers()["abc-7"]["review"]["verdict"], "pass")
            self.assertEqual(ledger.workers()["abc-7"]["review"]["head"], "4f2c1ab")
            self.assertEqual(self.athena("review", "abc-7", "fail", "--head", "9" * 40)[0], 0)
        self.assertEqual(summary.call_count, 0)  # a head given is the one reviewed: no need to ask gh
        self.assertEqual((ledger.workers()["abc-7"]["review"]["verdict"], ledger.workers()["abc-7"]["review"]["head"]),
                         ("fail", "9" * 40))

    def test_refusals_record_nothing(self):
        with mock.patch.object(cli.pr, "summary", return_value=None):
            code, _, err = self.athena("review", "abc-7", "pass")
        self.assertEqual(code, 2)
        self.assertIn("--head", err)  # gh could not say the PR's head
        self.assertEqual(self.athena("review", "abc-7", "pass", "--head", "main")[0], 2)  # not a sha
        self.assertEqual(self.athena("review", "abc-7", "pass", "--head", "4f2c1")[0], 2)  # too short to name one
        self.assertEqual(self.athena("review", "abc-9", "pass", "--head", HEAD)[0], 2)  # no such live worker
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.main(["review", "abc-7", "approve"])
        self.assertNotIn("review", ledger.workers()["abc-7"])

    def test_status_shows_the_verdict_and_whether_its_head_is_stale(self):
        self.athena("review", "abc-7", "pass", "--head", HEAD)
        self.assertIn("review=pass@4f2c1ab", self.athena("status")[1])
        with mock.patch.object(cli.status.pr, "summary", return_value=_pr()):
            self.assertNotIn("(stale)", self.athena("status", "--pr")[1])
        with mock.patch.object(cli.status.pr, "summary", return_value=_pr(head="9a8b7c6d5e4f")):
            self.assertIn("review=pass@4f2c1ab (stale)", self.athena("status", "--pr")[1])

    def test_a_respawn_starts_with_no_verdict(self):
        self.athena("review", "abc-7", "pass", "--head", HEAD)
        ledger.append("retire", "abc-7")
        ledger.append("spawn", "abc-7", repo="/src/web", branch="you/abc-7")
        self.assertNotIn("review", ledger.workers()["abc-7"])


class WatchReviewTest(unittest.TestCase):
    """athena watch gives each live worker's PR its move with the worker's verdict; the status line says in review."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        config.write(dict(config.DEFAULTS))
        ledger.append("spawn", "abc-7", repo="/src/web", branch="you/abc-7")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def snap(self, state="done", report=None):
        collected = {"state": state, "pr": _pr(), "report": report, "detail": None}
        with mock.patch.object(watch.status, "collect", return_value=collected), \
                mock.patch.object(watch, "repo_slug", return_value="o/web"), \
                mock.patch.object(watch.gitops, "default_branch", return_value="main"), \
                mock.patch.object(watch.gitops, "git", return_value=""):
            return watch.snapshot()

    def test_a_done_worker_is_in_review_until_its_review_passes_at_the_head(self):
        cur = self.snap(report={"status": "DONE"})
        self.assertEqual(cur["workers"]["abc-7"]["pr"]["move"]["label"], "in review · athena")
        board = watch.board(cur)
        self.assertEqual((board["active"], board["done"], board["prs"]), ([["in review", ["abc-7"]]], [], []))
        self.assertEqual(watch.summary(cur), "abc-7 in review")
        ledger.append("review", "abc-7", review={"verdict": "pass", "head": HEAD, "at": 1.0})
        cur = self.snap(report={"status": "DONE"})
        self.assertEqual(cur["workers"]["abc-7"]["pr"]["move"]["label"], "your go to merge")
        board = watch.board(cur)
        self.assertEqual((board["active"], board["done"], board["prs"]), ([], ["abc-7"], ["#7"]))

    def test_a_failed_review_is_the_workers_move(self):
        ledger.append("review", "abc-7", review={"verdict": "fail", "head": HEAD, "at": 1.0})
        cur = self.snap(state="working")
        self.assertEqual(cur["workers"]["abc-7"]["pr"]["move"]["label"], "review findings · worker fixing")
        self.assertEqual(watch.board(cur)["active"], [["working", ["abc-7"]]])

    def test_a_worker_still_working_stays_working(self):
        self.assertEqual(watch.board(self.snap(state="working"))["active"], [["working", ["abc-7"]]])


if __name__ == "__main__":
    unittest.main()
