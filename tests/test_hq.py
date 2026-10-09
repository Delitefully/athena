import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import dash, hq, paths

CREATED = {"result": {"workspace": {"workspace_id": "w8"}, "root_pane": {"pane_id": "w8:p1", "terminal_id": "term_h"}}}


class HqTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.cwd = Path(self.tmp.name) / "dev"
        self.cwd.mkdir()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_starts_the_board_and_the_dashboard_after_athena(self):
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        with mock.patch.object(hq.board, "start_watcher") as board, mock.patch.object(hq.dash, "start") as start:
            hq.hq(cwd=str(self.cwd))
        board.assert_called_once_with()
        start.assert_called_once_with()

    def test_a_dashboard_failure_does_not_stop_hq(self):
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        with mock.patch.object(hq.board, "start_watcher"), \
                mock.patch.object(hq.dash, "start", side_effect=dash.DashError("no go")), \
                mock.patch("sys.stderr"):
            self.assertTrue(hq.hq(cwd=str(self.cwd), start_board=True)["created"])

    def test_no_board_means_no_dashboard_unless_asked(self):
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        with mock.patch.object(hq.board, "start_watcher") as board, mock.patch.object(hq.dash, "start") as start:
            hq.hq(cwd=str(self.cwd), start_board=False)
        board.assert_not_called()
        start.assert_not_called()

    def test_creates_space_and_starts_athena(self):
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        result = hq.hq(cwd=str(self.cwd), start_board=False)
        self.assertTrue(result["created"])
        calls = logged_calls(self.tmp.name)
        verbs = [c[:2] for c in calls]
        self.assertEqual(verbs[:2], [["workspace", "create"], ["agent", "start"]])
        self.assertEqual(verbs[-1], ["agent", "prompt"])
        self.assertIn(["pane", "rename", "w8:p1", "athena"], calls)
        start = calls[1]
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[claude.index("--name") + 1], "athena")
        self.assertTrue(claude[claude.index("--append-system-prompt-file") + 1].endswith("hq.md"))
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["pane"], "w8:p1")
        self.assertFalse(paths.claim_file(str(self.cwd)).exists())

    def test_focuses_existing(self):
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8"}))
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent": "claude", "name": "athena"}}}}])
        result = hq.hq(start_board=False)
        self.assertFalse(result["created"])
        self.assertIn(["workspace", "focus", "w8"], logged_calls(self.tmp.name))

    def test_restarts_claude_in_surviving_hq_space(self):
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8"}))
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1},
            {"match": ["workspace", "get", "w8"], "stdout": {"result": {"workspace": {"label": "athena"}}}},
        ])
        result = hq.hq(start_board=False)
        self.assertTrue(result["restarted"])
        verbs = [c[:2] for c in logged_calls(self.tmp.name)]
        self.assertNotIn(["workspace", "create"], verbs)
        self.assertIn(["agent", "start"], verbs)

    def test_keep_label_renames_drifted_space_and_pane(self):
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8"}))
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent": "claude", "name": "athena"}}}},
            {"match": ["workspace", "get", "w8"], "stdout": {"result": {"workspace": {"label": "athena-hq-status-watch"}}}},
            {"match": ["pane", "get", "w8:p1"], "stdout": {"result": {"pane": {"pane_id": "w8:p1"}}}},
        ])
        hq.keep_label()
        calls = logged_calls(self.tmp.name)
        self.assertIn(["workspace", "rename", "w8", "athena"], calls)
        self.assertIn(["pane", "rename", "w8:p1", "athena"], calls)

    def test_keep_label_leaves_a_pane_it_does_not_own(self):
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8"}))
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"name": "other"}}}}])
        hq.keep_label()
        self.assertEqual([c[:2] for c in logged_calls(self.tmp.name)], [["agent", "get"]])

    def test_claim_removed_when_start_fails(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "trust dialog"}})
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED},
                                       {"match": ["agent", "start"], "stdout": "", "stderr": err, "exit": 1}])
        with self.assertRaises(Exception):
            hq.hq(cwd=str(self.cwd), start_board=False)
        self.assertFalse(paths.claim_file(str(self.cwd)).exists())


if __name__ == "__main__":
    unittest.main()
