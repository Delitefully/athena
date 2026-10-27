import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import board, herdr, hookstatus, ledger, paths


def W(name, urgency=3, spawned=0):
    return {"name": name, "urgency": urgency, "spawned": spawned}


class PlanTest(unittest.TestCase):
    def test_plan_columns_when_wide(self):
        p = board.plan(360, 70, [W("a", 3, 1), W("b", 0, 2), W("c", 3, 3), W("d", 1, 4)])
        self.assertEqual(p["mode"], "columns")
        self.assertEqual(p["rows"], [["a", "b", "c", "d"]])
        self.assertAlmostEqual(p["hq_ratio"], 1 / 5)  # an equal share with the four columns
        self.assertEqual(board.plan(330, 70, [W("a"), W("b"), W("c"), W("d")])["mode"], "rows")

    def test_plan_rows_when_medium(self):
        p = board.plan(230, 70, [W("a", spawned=1), W("b", spawned=2), W("c", spawned=3), W("d", spawned=4)])
        self.assertEqual(p["mode"], "rows")
        self.assertEqual(p["rows"], [["a", "b"], ["c", "d"]])

    def test_plan_subset_when_narrow(self):
        ws = [W("a", 3, 1), W("b", 0, 2), W("c", 3, 3), W("d", 1, 4), W("e", 2, 5)]
        p = board.plan(180, 70, ws)
        self.assertEqual(p["mode"], "subset")
        self.assertEqual(p["rows"], [["b"]])
        p = board.plan(250, 70, ws)
        self.assertEqual(p["mode"], "subset")
        self.assertEqual(p["rows"], [["b", "d"]])  # most urgent two, in spawn order
        self.assertEqual(p["hidden"], ["a", "c", "e"])

    def test_plan_hq_only_when_tiny(self):
        p = board.plan(111, 70, [W("a")])
        self.assertEqual(p["mode"], "hq-only")
        self.assertEqual(p["rows"], [])
        self.assertEqual(board.plan(300, 70, [])["mode"], "empty")

    def test_hq_ratio_equal_share(self):
        self.assertAlmostEqual(board.plan(150, 70, [W("a")])["hq_ratio"], 0.5)
        self.assertAlmostEqual(board.plan(1000, 70, [W("a")])["hq_ratio"], 0.5)
        self.assertAlmostEqual(board.plan(554, 70, [W("a"), W("b"), W("c")])["hq_ratio"], 0.25)
        rows = board.plan(230, 70, [W("a"), W("b"), W("c"), W("d")])
        self.assertAlmostEqual(rows["hq_ratio"], 1 / 3)  # beside two columns of two rows
        self.assertAlmostEqual(board.plan(180, 70, [W("a"), W("b"), W("c")])["hq_ratio"], 0.5)  # subset of one

    def test_ops_equal_ratios(self):
        p = board.plan(400, 70, [W("a", spawned=1), W("b", spawned=2), W("c", spawned=3), W("d", spawned=4)])
        ops = board.ops(p, "w1:p1")
        splits = [o for o in ops if o[0] == "split"]
        self.assertEqual(splits[0][1:4], ("w1:p1", "right", 0.2))
        self.assertEqual([round(o[3], 4) for o in splits[1:]], [0.25, 0.3333, 0.5])
        self.assertEqual([o[1] for o in ops if o[0] == "attach"], ["c0", "c1", "c2", "c3"])
        self.assertEqual([o[2] for o in ops if o[0] == "attach"], ["a", "b", "c", "d"])

    def test_ops_rows(self):
        p = board.plan(230, 70, [W("a", spawned=1), W("b", spawned=2), W("c", spawned=3)])
        ops = board.ops(p, "hq")
        splits = [o[:4] for o in ops if o[0] == "split"]
        self.assertEqual(splits[0], ("split", "hq", "right", round(1 / 3, 4)))
        self.assertEqual(splits[1][2:], ("down", 0.5))
        attaches = {o[2]: o[1] for o in ops if o[0] == "attach"}
        self.assertEqual(sorted(attaches), ["a", "b", "c"])


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.env.set("ATHENA_HQ_PANE", "w1:p1")
        ledger.append("spawn", "a", pane="w2:p1", terminal="term_a", workspace="w2")
        ledger.append("spawn", "b", pane="w3:p1", terminal="term_b", workspace="w3")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def scenario(self, width, extra=None):
        rules = (extra or []) + [
            {"match": ["pane", "layout"], "stdout": {"result": {"layout": {"area": {"width": width, "height": 60}}}}},
            {"match": ["pane", "split"], "stdout": {"result": {"pane": {"pane_id": "w1:p7"}}}, "times": 1},
            {"match": ["pane", "split"], "stdout": {"result": {"pane": {"pane_id": "w1:p8"}}}, "times": 1},
            {"match": ["pane", "split"], "stdout": {"result": {"pane": {"pane_id": "w1:p9"}}}},
            {"match": ["pane", "get"], "stdout": {"result": {"pane": {"pane_id": "x", "focused": False}}}},
        ]
        write_scenario(self.tmp.name, rules)

    def test_sync_builds_columns(self):
        self.scenario(330)
        result = board.sync()
        self.assertEqual(result["mode"], "columns")
        calls = logged_calls(self.tmp.name)
        runs = [c for c in calls if c[:2] == ["pane", "run"]]
        self.assertEqual(len(runs), 2)
        self.assertIn("terminal attach term_a --takeover", runs[0][3])
        self.assertIn("terminal attach term_b --takeover", runs[1][3])
        saved = json.loads((paths.state_dir() / "board.json").read_text())
        self.assertEqual([c["name"] for c in saved["columns"]], ["a", "b"])
        self.assertEqual([c["pane"] for c in saved["columns"]], ["w1:p7", "w1:p8"])

    def test_sync_noop_when_plan_unchanged(self):
        self.scenario(330)
        board.sync()
        log = Path(self.tmp.name) / "herdr.log"
        log.write_text("")
        self.scenario(330)
        result = board.sync()
        self.assertEqual(result["changed"], False)
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] in (["pane", "split"], ["pane", "close"])], [])

    def test_sync_postpones_when_column_focused(self):
        self.scenario(330)
        board.sync()
        ledger.append("spawn", "c", pane="w4:p1", terminal="term_c", workspace="w4")
        self.scenario(330, [{"match": ["pane", "get", "w1:p8"], "stdout": {"result": {"pane": {"pane_id": "w1:p8", "focused": True}}}}])
        result = board.sync()
        self.assertTrue(result["postponed"])
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "close"]], [])
        result = board.sync(force=True)
        self.assertEqual(result["mode"], "columns")

    def test_sync_rebuild_closes_old_columns(self):
        self.scenario(330)
        board.sync()
        ledger.append("retire", "b")
        self.scenario(330, [
            {"match": ["pane", "get", "w1:p7"], "stdout": {"result": {"pane": {"pane_id": "w1:p7", "label": "board:a"}}}},
            {"match": ["pane", "get", "w1:p8"], "stdout": {"result": {"pane": {"pane_id": "w1:p8", "label": "board:b"}}}},
        ])
        board.sync()
        closes = [c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "close"]]
        self.assertEqual(sorted(c[2] for c in closes), ["w1:p7", "w1:p8"])

    def test_never_closes_a_pane_that_is_not_ours(self):
        self.scenario(330)
        board.sync()
        ledger.append("retire", "b")
        # After a herdr restart, w1:p7 now belongs to one of the user's own panes.
        self.scenario(330, [
            {"match": ["pane", "get", "w1:p7"], "stdout": {"result": {"pane": {"pane_id": "w1:p7", "label": "my editor"}}}},
            {"match": ["pane", "get", "w1:p8"], "stdout": {"result": {"pane": {"pane_id": "w1:p8"}}}},
        ])
        board.sync()
        board.clear()
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "close"]], [])

    def test_split_failure_rolls_back(self):
        err = '{"error":{"code":"split_failed","message":"no room"}}'
        write_scenario(self.tmp.name, [
            {"match": ["pane", "layout"], "stdout": {"result": {"layout": {"area": {"width": 330}}}}},
            {"match": ["pane", "split"], "stdout": {"result": {"pane": {"pane_id": "w1:p7"}}}, "times": 1},
            {"match": ["pane", "split"], "stdout": "", "stderr": err, "exit": 1},
        ])
        with self.assertRaises(RuntimeError):
            board.sync()
        self.assertIn(["pane", "close", "w1:p7"], logged_calls(self.tmp.name))
        self.assertEqual(board.load()["columns"], [])

    def test_stale_hq_refused(self):
        self.env.restore()
        self.env = helpers.isolated_env(self.tmp.name)
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w1:p1"}))
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"name": "claude-w1"}}}}])
        with self.assertRaises(RuntimeError) as ctx:
            board.sync()
        self.assertIn("no longer runs", str(ctx.exception))
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "split"]], [])

    def test_sync_skips_when_locked(self):
        import fcntl
        with open(paths.ensure() / "board.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.scenario(330)
            self.assertIn("skipped", board.sync())

    def agents(self, *agents):
        return [{"match": ["agent", "list"], "stdout": {"result": {"agents": list(agents)}}}]

    def attached(self):
        runs = [c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "run"]]
        return [r[3].split("terminal attach ")[1].split()[0] for r in runs]

    def test_attaches_the_live_terminal_herdr_names_not_the_stale_ledger_id(self):
        # herdr gives every pane a new terminal id when it restarts; the ledger keeps the old one.
        self.scenario(330, self.agents({"name": "a", "pane_id": "w2:p1", "terminal_id": "term_a2"},
                                       {"name": "b", "pane_id": "w3:p1", "terminal_id": "term_b2"}))
        board.sync()
        self.assertEqual(self.attached(), ["term_a2", "term_b2"])
        saved = json.loads((paths.state_dir() / "board.json").read_text())
        self.assertEqual([c["terminal"] for c in saved["columns"]], ["term_a2", "term_b2"])

    def test_a_worker_with_no_agent_listed_is_found_by_its_pane(self):
        # `pane list` carries every pane's terminal, a pane whose Claude exited included.
        self.scenario(330, self.agents({"name": "claude", "pane_id": "w9:p1", "terminal_id": "term_x"}) + [
            {"match": ["pane", "list"], "stdout": {"result": {"panes": [{"pane_id": "w2:p1", "terminal_id": "term_a2"}]}}}])
        board.sync()
        self.assertEqual(self.attached(), ["term_a2", "term_b"])

    def test_the_agent_name_wins_over_the_pane(self):
        self.scenario(330, self.agents({"name": "a", "pane_id": "w2:p1", "terminal_id": "term_a2"}) + [
            {"match": ["pane", "list"], "stdout": {"result": {"panes": [{"pane_id": "w2:p1", "terminal_id": "term_old"}]}}}])
        board.sync()
        self.assertEqual(self.attached()[0], "term_a2")

    def test_a_herdr_that_cannot_list_moves_nothing(self):
        # After a restart the ledger ids are dead: attaching to them, then to the live ones, would rebuild twice.
        self.scenario(330)
        board.sync()
        (Path(self.tmp.name) / "herdr.log").write_text("")
        for failing in (["agent", "list"], ["pane", "list"]):
            self.scenario(330, [{"match": failing, "stdout": "", "stderr": "down", "exit": 1}])
            with self.assertRaises(herdr.HerdrError):
                board.sync()
            moved = [c for c in logged_calls(self.tmp.name) if c[:2] in (["pane", "split"], ["pane", "close"], ["pane", "run"])]
            self.assertEqual(moved, [])
            self.assertEqual([c["name"] for c in board.load()["columns"]], ["a", "b"])

    def test_a_new_terminal_rebuilds_an_unchanged_layout(self):
        self.scenario(330)
        board.sync()
        (Path(self.tmp.name) / "herdr.log").write_text("")
        self.scenario(330, self.agents({"name": "a", "pane_id": "w2:p1", "terminal_id": "term_a2"}))
        self.assertTrue(board.sync()["changed"])
        self.assertEqual(self.attached(), ["term_a2", "term_b"])

    def test_sync_uses_urgency_when_narrow(self):
        ledger.append("spawn", "c", pane="w4:p1", terminal="term_c", workspace="w4")
        hookstatus.apply({"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "x"}, "b")
        self.scenario(150)
        result = board.sync()
        self.assertEqual(result["mode"], "subset")
        self.assertEqual(result["rows"], [["b"]])


if __name__ == "__main__":
    unittest.main()


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_a_foreground_watcher_records_itself(self):
        with mock.patch.object(board, "running_pid", return_value=None):
            self.assertTrue(board.claim())
        self.assertEqual((paths.state_dir() / "board.pid").read_text(), str(os.getpid()))

    def test_a_second_watcher_leaves_at_once(self):
        with mock.patch.object(board, "running_pid", return_value=4242), mock.patch.object(board, "sync") as sync, \
                mock.patch("sys.stderr"):
            board.watch()
        sync.assert_not_called()

    def test_the_watcher_athena_board_on_started_is_itself(self):
        with mock.patch.object(board, "running_pid", return_value=os.getpid()):
            self.assertTrue(board.claim())
