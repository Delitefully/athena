import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from tests.test_retire import DIALOG
from athena_lib import cli, dash, hq, paths

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
        env = json.loads(Path(claude[claude.index("--settings") + 1]).read_text())["env"]
        self.assertEqual(env["ATHENA_HQ"], "athena")  # so `athena hq restart` run inside HQ targets this HQ
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


def alive(session="sess-1", after=None):
    rule = {"match": ["agent", "get", "athena"],
            "stdout": {"result": {"agent": {"name": "athena", "agent_session": {"kind": "id", "value": session}}}}}
    return {**rule, "after": after} if after else rule


GONE = {"match": ["agent", "get"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1}
HQ_SPACE = {"match": ["workspace", "get", "w8"], "stdout": {"result": {"workspace": {"label": "athena"}}}}
# HQ normally has watches running, so /exit brings up the background-work dialog.
ON_EXIT = {"match": ["agent", "read", "athena"], "stdout": DIALOG}


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8", "session": "old"}))
        self.patches = [mock.patch.object(hq.time, "sleep"), mock.patch.object(hq, "_say")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.restore()
        self.tmp.cleanup()

    def calls(self):
        return logged_calls(self.tmp.name)

    def starts(self):
        return [c for c in self.calls() if c[:2] == ["agent", "start"]]

    def test_exits_then_resumes_the_same_conversation_and_runs_the_routine(self):
        write_scenario(self.tmp.name, [alive("sess-1", after=["agent", "start"]),
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("sess-1"), HQ_SPACE, ON_EXIT])
        result = hq.restart_now(update=False)
        self.assertTrue(result["resumed"])
        calls = self.calls()
        exit_at = calls.index(["agent", "prompt", "athena", "/exit"])
        after_exit = [c for c in calls[exit_at + 1:] if c[:2] not in (["agent", "get"], ["agent", "read"])]
        self.assertEqual(after_exit[0], ["agent", "send-keys", "athena", "enter"])
        (start,) = self.starts()
        self.assertGreater(calls.index(start), exit_at)
        self.assertEqual(start[start.index("--pane") + 1], "w8:p1")
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[:2], ["--resume", "sess-1"])
        self.assertEqual(claude[claude.index("--name") + 1], "athena")
        self.assertTrue(claude[claude.index("--append-system-prompt-file") + 1].endswith("hq.md"))
        routine = [c for c in calls if c[:3] == ["agent", "prompt", "athena"] and c[3] != "/exit"]
        self.assertEqual(len(routine), 1)
        self.assertGreater(calls.index(routine[0]), calls.index(start))
        self.assertIn("athena status --pr", routine[0][3])
        self.assertIn("needs-you.md", routine[0][3])
        self.assertNotIn("ultracode", routine[0][3].lower())
        self.assertNotIn(["workspace", "create"], [c[:2] for c in calls])
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "sess-1")
        self.assertFalse(hq.lock_file().exists())

    def test_waits_for_hq_to_finish_its_turn_before_exit(self):
        write_scenario(self.tmp.name, [{**GONE, "after": ["agent", "send-keys"]}, alive("sess-1"), HQ_SPACE, ON_EXIT])
        hq.restart_now(update=False)
        calls = self.calls()
        wait = next(c for c in calls if c[:2] == ["agent", "wait"])
        self.assertLess(calls.index(wait), calls.index(["agent", "prompt", "athena", "/exit"]))
        self.assertNotIn("blocked", wait)

    def test_uses_the_recorded_session_when_hq_already_exited(self):
        write_scenario(self.tmp.name, [alive("sess-1", after=["agent", "start"]), GONE, HQ_SPACE])
        hq.restart_now(update=False)
        self.assertNotIn(["agent", "prompt", "athena", "/exit"], self.calls())
        claude = self.starts()[0][self.starts()[0].index("--") + 1:]
        self.assertEqual(claude[:2], ["--resume", "old"])

    def test_exit_timeout_starts_no_second_claude(self):
        write_scenario(self.tmp.name, [alive("sess-1"), HQ_SPACE])
        with self.assertRaises(hq.RestartError) as err:
            hq.restart_now(update=False)
        self.assertIn("did not exit", str(err.exception))
        self.assertEqual(self.starts(), [])
        self.assertIn(["agent", "prompt", "athena", "/exit"], self.calls())
        self.assertTrue(any(c[:2] == ["notification", "show"] for c in self.calls()))
        self.assertFalse(hq.lock_file().exists())

    def test_an_hq_that_never_goes_idle_is_left_alone(self):
        err = json.dumps({"error": {"code": "timeout", "message": "still blocked"}})
        write_scenario(self.tmp.name, [{"match": ["agent", "wait"], "stdout": "", "stderr": err, "exit": 1},
                                       alive("sess-1"), HQ_SPACE])
        with self.assertRaises(hq.RestartError) as raised:
            hq.restart_now(update=False)
        self.assertIn("busy or waiting on you", str(raised.exception))
        self.assertNotIn(["agent", "prompt", "athena", "/exit"], self.calls())
        self.assertNotIn(["agent", "send-keys", "athena", "enter"], self.calls())
        self.assertEqual(self.starts(), [])
        self.assertTrue(any(c[:2] == ["notification", "show"] for c in self.calls()))
        self.assertFalse(hq.lock_file().exists())

    def test_a_failed_resume_falls_back_to_a_fresh_hq_and_says_so(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "no session"}})
        write_scenario(self.tmp.name, [{"match": ["agent", "start"], "stdout": "", "stderr": err, "exit": 1, "times": 1},
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("sess-1"), HQ_SPACE, ON_EXIT])
        result = hq.restart_now(update=False)
        self.assertTrue(result["fallback"])
        first, second = self.starts()
        self.assertIn("--resume", first)
        self.assertNotIn("--resume", second)
        self.assertEqual(second[second.index("--pane") + 1], "w8:p1")
        self.assertTrue(any(c[:2] == ["notification", "show"] for c in self.calls()))
        self.assertNotIn(["workspace", "create"], [c[:2] for c in self.calls()])

    def test_a_resume_that_came_up_late_is_not_started_twice(self):
        err = json.dumps({"error": {"code": "cli_timeout", "message": "slow"}})
        write_scenario(self.tmp.name, [{"match": ["agent", "start"], "stdout": "", "stderr": err, "exit": 1},
                                       alive("sess-1", after=["agent", "start"]),
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("sess-1"), HQ_SPACE, ON_EXIT])
        result = hq.restart_now(update=False)
        self.assertTrue(result["resumed"])
        self.assertEqual(len(self.starts()), 1)

    def test_lock_refuses_a_second_restart(self):
        hq.lock_file().write_text(str(os.getpid()))
        with self.assertRaises(hq.RestartError) as err:
            hq.restart_now(update=False)
        self.assertIn("already", str(err.exception))
        with self.assertRaises(hq.RestartError):
            hq.restart(update=False)
        self.assertEqual(self.calls(), [])
        self.assertEqual(hq.lock_file().read_text(), str(os.getpid()))

    def test_a_stale_lock_is_taken_over(self):
        hq.lock_file().write_text("999999")
        write_scenario(self.tmp.name, [{**GONE, "after": ["agent", "send-keys"]}, alive("sess-1"), HQ_SPACE, ON_EXIT])
        self.assertTrue(hq.restart_now(update=False)["resumed"])
        self.assertFalse(hq.lock_file().exists())

    def test_no_hq_to_restart(self):
        (paths.state_dir() / "hq.json").unlink()
        with self.assertRaises(hq.RestartError):
            hq.restart_now(update=False)

    def test_restart_detaches_and_returns_at_once(self):
        with mock.patch.object(hq.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            result = hq.restart(update=False)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-4:], ["hq", "restart", "--child", "--no-update"])
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], hq.subprocess.DEVNULL)
        self.assertEqual(result["pid"], 4242)
        self.assertTrue(result["log"].endswith("hq-restart.log"))
        self.assertIn("restarting HQ; log at", result["message"])

    def fake_claude(self, fail=(), meet=()):
        """A stand-in for `subprocess.run` of the claude CLI. Commands in `fail` exit 1. Commands in `meet` each wait
        until all of them are running at once, so a test fails if update_claude runs them one after another.
        `overlap["max"]` is the most plugin updates that ran at the same time."""
        plugins = [{"id": "athena@athena", "scope": "user"}, {"id": "linear@x", "scope": "project", "projectPath": "/p"},
                   {"id": "linear@x", "scope": "user"}, {"id": "linear@x", "scope": "user"},
                   {"id": "figma@y", "scope": "user"}]
        markets = [{"name": "athena"}, {"name": "claude-plugins-official"}]
        ran, lock = [], threading.Lock()
        barrier = threading.Barrier(len(meet), timeout=5) if meet else None
        overlap = {"now": 0, "max": 0}

        def run(cmd, **kw):
            args = cmd[1:]
            with lock:
                ran.append(args)
            if tuple(args) in meet:
                barrier.wait()
            if args[:2] == ["plugin", "update"]:
                with lock:
                    overlap["now"] += 1
                    overlap["max"] = max(overlap["max"], overlap["now"])
                threading.Event().wait(0.05)  # time.sleep is patched out in these tests
                with lock:
                    overlap["now"] -= 1
            if tuple(args) in fail:
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            if args[:3] == ["plugin", "list", "--json"]:
                return mock.Mock(returncode=0, stdout=json.dumps(plugins), stderr="")
            if args[:4] == ["plugin", "marketplace", "list", "--json"]:
                return mock.Mock(returncode=0, stdout=json.dumps(markets), stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.overlap = overlap
        return run, ran

    def test_update_runs_claude_update_marketplaces_and_user_plugins_and_survives_failures(self):
        run, ran = self.fake_claude(fail={("update",), ("plugin", "update", "athena@athena", "--scope", "user")})
        with mock.patch.object(hq.subprocess, "run", side_effect=run):
            warnings = hq.update_claude()
        self.assertIn(["update"], ran)
        markets = sorted(c[3] for c in ran if c[:3] == ["plugin", "marketplace", "update"])
        self.assertEqual(markets, ["anthropic-plugin-directory", "athena", "claude-plugins-official"])
        updates = sorted(c for c in ran if c[:2] == ["plugin", "update"])
        self.assertEqual(updates, [["plugin", "update", "athena@athena", "--scope", "user"],
                                   ["plugin", "update", "figma@y", "--scope", "user"],
                                   ["plugin", "update", "linear@x", "--scope", "user"]])
        last_market = max(i for i, c in enumerate(ran) if c[:3] == ["plugin", "marketplace", "update"])
        first_update = min(i for i, c in enumerate(ran) if c[:2] == ["plugin", "update"])
        self.assertLess(last_market, first_update)  # plugins update from refreshed marketplaces
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("claude update failed" in w for w in warnings))
        self.assertTrue(any("athena@athena" in w for w in warnings))

    def test_updates_run_concurrently(self):
        run, ran = self.fake_claude(meet={("update",), ("plugin", "marketplace", "update", "athena"),
                                          ("plugin", "marketplace", "update", "claude-plugins-official"),
                                          ("plugin", "marketplace", "update", "anthropic-plugin-directory")})
        with mock.patch.object(hq.subprocess, "run", side_effect=run):
            self.assertEqual(hq.update_claude(), [])
        # The plugin updates start once the marketplaces are done, without waiting for `claude update`.
        run, ran = self.fake_claude(meet={("update",), ("plugin", "update", "athena@athena", "--scope", "user")})
        with mock.patch.object(hq.subprocess, "run", side_effect=run):
            self.assertEqual(hq.update_claude(), [])

    def test_plugin_updates_never_overlap(self):
        # An update that installs a new version rewrites ~/.claude/plugins/installed_plugins.json.
        run, ran = self.fake_claude()
        with mock.patch.object(hq.subprocess, "run", side_effect=run):
            self.assertEqual(hq.update_claude(), [])
        self.assertEqual(len([c for c in ran if c[:2] == ["plugin", "update"]]), 3)
        self.assertEqual(self.overlap["max"], 1)

    def test_a_failed_marketplace_listing_updates_them_all_in_one_call(self):
        run, ran = self.fake_claude(fail={("plugin", "marketplace", "list", "--json")})
        with mock.patch.object(hq.subprocess, "run", side_effect=run):
            hq.update_claude()
        self.assertIn(["plugin", "marketplace", "update"], ran)

    def test_fresh_hq_records_its_session(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get", "w8:p1"], "stdout": "", "exit": 1},
                                       alive("sess-9", after=["agent", "start"]), HQ_SPACE])
        hq.hq(start_board=False)
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "sess-9")


class HqCliTest(unittest.TestCase):
    def test_plain_hq_and_restart_parse(self):
        p = cli.parser()
        self.assertIsNone(p.parse_args(["hq"]).action)
        self.assertIsNone(p.parse_args(["hq", "--cwd", "/x", "--no-board"]).action)
        a = p.parse_args(["hq", "restart", "--no-update"])
        self.assertEqual((a.action, a.no_update, a.child), ("restart", True, False))
        self.assertFalse(p.parse_args(["hq", "restart"]).no_update)


if __name__ == "__main__":
    unittest.main()
