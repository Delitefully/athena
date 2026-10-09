import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import write_scenario
from athena_lib import dashstate, ledger, paths, retire, watch


class WatchFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_save_and_load_round_trip(self):
        snap = {"workers": {"a": {"state": "working", "pr": {"url": "u", "checks": "pending"}, "report": None, "detail": None}},
                "mains": {"/r": "abc"}, "missing": []}
        dashstate.save_watch(snap, now=100.0)
        saved = json.loads((paths.state_dir() / "watch.json").read_text())
        self.assertEqual(saved, {"ts": 100.0, "workers": snap["workers"]})
        self.assertEqual(dashstate.load_watch()["workers"]["a"]["pr"]["url"], "u")
        self.assertEqual(list(paths.state_dir().glob("watch.json.*")), [], "no temp file left behind")

    def test_load_survives_missing_and_broken_files(self):
        self.assertEqual(dashstate.load_watch(), {})
        paths.ensure().joinpath("watch.json").write_text('{"ts": 1, "work')
        self.assertEqual(dashstate.load_watch(), {})

    def test_watch_tick_writes_the_snapshot(self):
        ledger.append("spawn", "a", pane="w2:p1")
        with mock.patch.object(watch.status, "collect", return_value={
                "state": "working", "pr": {"url": "u"}, "report": None, "detail": None}):
            prev, _ = watch.tick(None)
        saved = dashstate.load_watch()
        self.assertEqual(saved["workers"], prev["workers"])
        self.assertEqual(saved["workers"]["a"]["pr"], {"url": "u"})


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        os.environ["ATHENA_EXIT_GRACE"] = "0"
        self.repo, _ = make_repo(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "plt-1"
        git(self.repo, "worktree", "add", "-q", "-b", "you/abc-1", str(self.wt), "main")
        ledger.append("spawn", "abc-1", repo=str(self.repo), path=os.path.realpath(self.wt), branch="you/abc-1",
                      linear="ABC-1", title="A title", workspace="w5", pane="w5:p1", terminal="term_1")
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get", "abc-1"], "stdout": {"result": {"agent": {"name": "abc-1"}}}},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(self.wt)}}}}},
        ])
        paths.ensure("workers").joinpath("abc-1.json").write_text(json.dumps(
            {"state": "done", "report": None, "last_report": {"status": "DONE", "pr": "https://github.com/acme/web/pull/9"}}))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def history(self):
        return [json.loads(line) for line in (paths.state_dir() / "history.jsonl").read_text().splitlines()]

    def test_retire_records_the_pr_from_gh(self):
        merged = {"number": 9, "url": "https://github.com/acme/web/pull/9", "state": "MERGED", "draft": False}
        with mock.patch.object(dashstate.pr, "summary", return_value=merged) as summary:
            retire.retire("abc-1", keep_worktree=True)
        summary.assert_called_once_with(str(self.repo), "you/abc-1")
        [entry] = self.history()
        self.assertEqual(entry["pr"], merged)
        self.assertEqual((entry["name"], entry["linear"], entry["title"], entry["status"]), ("abc-1", "ABC-1", "A title", "DONE"))
        retired = ledger.workers()["abc-1"]
        self.assertEqual(retired["state"], "retired")
        self.assertLessEqual(entry["ts"], retired["updated"])

    def test_retire_falls_back_to_watch_then_to_the_report(self):
        dashstate.save_watch({"workers": {"abc-1": {"pr": {"url": "https://github.com/acme/web/pull/9", "state": "OPEN"}}}})
        with mock.patch.object(dashstate.pr, "summary", return_value=None):
            dashstate.record_retire(ledger.workers()["abc-1"])
            (paths.state_dir() / "watch.json").unlink()
            dashstate.record_retire(ledger.workers()["abc-1"])
        first, second = self.history()
        self.assertEqual(first["pr"]["state"], "OPEN")
        self.assertEqual(second["pr"], {"url": "https://github.com/acme/web/pull/9"})

    def test_history_never_blocks_a_retire(self):
        with mock.patch.object(dashstate.pr, "summary", side_effect=RuntimeError("gh broke")):
            retire.retire("abc-1", keep_worktree=True)
        self.assertEqual(ledger.workers()["abc-1"]["state"], "retired")


if __name__ == "__main__":
    unittest.main()
