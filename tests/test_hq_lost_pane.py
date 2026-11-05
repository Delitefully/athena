"""HQ's recorded pane is gone while its space lives (a herdr restart can do that): every route gives HQ a working pane."""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from tests.test_hq import GONE, HQ_SPACE, alive
from athena_lib import board, cli, herdr, hq, paths


def err(code):
    return json.dumps({"error": {"code": code, "message": "x"}})


def fails(match, code, **rule):
    return {"match": match, "stdout": "", "stderr": err(code), "exit": 1, **rule}


def pane(pane_id, label=None):
    return {"match": ["pane", "get", pane_id], "stdout": {"result": {"pane": {"pane_id": pane_id, "label": label}}}}


def listing(*ids):
    return {"match": ["pane", "list", "--workspace", "w8"],
            "stdout": {"result": {"panes": [{"pane_id": i, "terminal_id": "t"} for i in ids]}}}


LOST = fails(["pane", "get", "w8:p1"], "pane_not_found")
# What herdr answers when anything still starts HQ in the recorded pane.
OLD_PANE = fails(["agent", "start", "athena", "--kind", "claude", "--pane", "w8:p1"], "agent_pane_not_found")
TILES = [listing("w8:p2", "w8:p3"), pane("w8:p2", "board:abc-1"), pane("w8:p3", "board:abc-2")]
MIXED = [listing("w8:p2", "w8:p4"), pane("w8:p2", "board:abc-1"), pane("w8:p4", "notes")]
SPLIT = {"match": ["pane", "split"], "stdout": {"result": {"pane": {"pane_id": "w8:p9", "terminal_id": "term_n"}}}}
CREATED = {"match": ["workspace", "create"],
           "stdout": {"result": {"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1", "terminal_id": "term_c"}}}}
SPACE_CLOSED = fails(["workspace", "get", "w8"], "workspace_not_found", after=["workspace", "close", "w8"])
START_FAILS = fails(["agent", "start"], "agent_exited", times=1)
CLOSES = (["pane", "close"], ["workspace", "close"])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.env.set("ATHENA_HQ_SESSION_WAIT", "0")
        self.env.set("ATHENA_HERDR", f"{sys.executable} {helpers.ROOT / 'tests' / 'fake_herdr.py'}")  # no shim: quick
        self.cwd = Path(self.tmp.name) / "dev"
        self.cwd.mkdir()
        self.record = {"pane": "w8:p1", "workspace": "w8", "session": "sess-9", "cwd": str(self.cwd)}
        self.write_record()
        for p in (mock.patch.object(hq.time, "sleep"), mock.patch.object(hq, "_say"),
                  mock.patch.object(hq.board, "start_watcher"), mock.patch.object(hq.dash, "start")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def write_record(self):
        (paths.ensure() / "hq.json").write_text(json.dumps(self.record))

    def fresh_run(self):
        """A new try from the start: hq.json as it was, and no earlier `agent start` for the fake to go by."""
        self.write_record()
        Path(self.tmp.name, "herdr.log").unlink(missing_ok=True)

    def saved(self):
        return json.loads((paths.state_dir() / "hq.json").read_text())

    def scenario(self, *rules, up="fresh-1"):
        """HQ's pane is lost; `rules` are herdr's answers. `up`: the conversation HQ's Claude reports once `agent start`
        has run, if it comes up."""
        started = [alive(up, after=["agent", "start"])] if up else []
        write_scenario(self.tmp.name, [*rules, *started, GONE, LOST, HQ_SPACE, OLD_PANE])

    def calls(self, *verbs):
        calls = logged_calls(self.tmp.name)
        return [c for c in calls if c[:2] in [list(v) for v in verbs]] if verbs else calls

    def starts(self):
        return self.calls(["agent", "start"])

    def assert_started_in(self, pane_id, *claude):
        start = self.starts()[-1]
        self.assertEqual(start[start.index("--pane") + 1], pane_id)
        args = start[start.index("--") + 1:]
        self.assertEqual(args[:len(claude)], list(claude))
        return args

    def assert_nothing_moved(self, before):
        moved = self.calls(*CLOSES, ["pane", "split"], ["workspace", "create"], ["agent", "start"])
        self.assertEqual(moved, [])
        self.assertEqual((paths.state_dir() / "hq.json").read_text(), before)


class HqTest(Fixture):
    def test_hq_starts_in_a_new_pane_and_resumes_the_recorded_conversation(self):
        self.scenario(*MIXED, SPLIT, up="sess-9")
        result = hq.hq(start_board=False, focus=False)
        self.assert_started_in("w8:p9", "--resume", "sess-9")
        self.assertEqual(self.saved()["pane"], "w8:p9")
        self.assertEqual(self.saved()["terminal"], "term_n")
        self.assertEqual(self.saved()["session"], "sess-9")
        self.assertEqual((result["pane"], result["created"], result["resumed"]), ("w8:p9", False, True))
        self.assertIn(["pane", "rename", "w8:p9", "athena"], self.calls())
        self.assertEqual(self.calls(["agent", "prompt"])[-1][3], hq.RESTART_ROUTINE)

    def test_hq_opens_the_space_it_replaced_when_only_tiles_were_left(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED)
        result = hq.hq(start_board=False, focus=False)
        self.assert_started_in("w5:p1", "--resume", "sess-9")
        self.assertEqual((self.saved()["pane"], self.saved()["workspace"]), ("w5:p1", "w5"))
        self.assertTrue(result["created"])

    def test_with_no_conversation_doctor_closes_nothing(self):
        self.record.pop("session")
        self.write_record()
        self.scenario(*TILES)
        self.assertTrue(hq.hq(start_board=False, fresh=False)["missing"])
        self.assertEqual(self.calls(*CLOSES), [])

    def test_the_space_gone_too_is_the_create_path_with_nothing_closed(self):
        self.scenario(fails(["workspace", "get", "w8"], "workspace_not_found"), CREATED)
        hq.hq(start_board=False, focus=False)
        self.assert_started_in("w5:p1", "--resume", "sess-9")
        self.assertEqual(self.calls(*CLOSES), [])

    def test_a_space_that_is_no_longer_hqs_is_never_touched(self):
        self.scenario({"match": ["workspace", "get", "w8"], "stdout": {"result": {"workspace": {"label": "other"}}}},
                      *TILES, CREATED)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(*CLOSES), [])
        self.assertEqual(self.calls(["pane", "list"]), [])
        self.assert_started_in("w5:p1", "--resume", "sess-9")


class RestartTest(Fixture):
    def test_restart_resumes_in_a_new_pane(self):
        self.scenario(*MIXED, SPLIT)
        result = hq.restart_now(update=False)
        self.assert_started_in("w8:p9", "--resume", "sess-9")
        self.assertEqual(self.saved()["pane"], "w8:p9")
        self.assertEqual((result["pane"], result["resumed"]), ("w8:p9", True))
        self.assertEqual(self.calls(["agent", "prompt"])[-1][3], hq.RESTART_ROUTINE)
        self.assertFalse(hq.lock_file().exists())

    def test_restart_with_only_tiles_left_resumes_in_a_new_space(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED)
        result = hq.restart_now(update=False)
        self.assert_started_in("w5:p1", "--resume", "sess-9")
        self.assertEqual(result["pane"], "w5:p1")
        self.assertEqual((self.saved()["pane"], self.saved()["workspace"]), ("w5:p1", "w5"))

    def test_herdr_giving_up_while_the_claude_came_up_keeps_the_new_space(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED, fails(["agent", "start"], "agent_start_timeout", times=1))
        hq.restart_now(update=False)
        self.assertEqual(self.calls(["workspace", "close"]), [["workspace", "close", "w8"]])
        self.assertEqual(self.saved()["pane"], "w5:p1")


class RotateTest(Fixture):
    def assert_fresh(self, pane_id):
        args = self.assert_started_in(pane_id)
        self.assertNotIn("--resume", args)
        self.assertEqual(self.saved()["pane"], pane_id)
        self.assertEqual(self.saved()["previous_session"], "sess-9")
        self.assertEqual(self.saved()["session"], "fresh-1")
        self.assertEqual(self.calls(["agent", "prompt"])[-1][3], hq._rotate_prompt())

    def test_rotate_starts_its_fresh_conversation_in_a_new_pane(self):
        self.scenario(*MIXED, SPLIT)
        result = hq.rotate_now()
        self.assert_fresh("w8:p9")
        self.assertEqual(result["pane"], "w8:p9")
        report = self.calls(["pane", "report-agent-session"])[-1]
        self.assertEqual(report[2], "w8:p9")

    def test_rotate_with_only_tiles_left_starts_fresh_in_a_new_space(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED)
        result = hq.rotate_now()
        self.assert_fresh("w5:p1")
        self.assertEqual(result["pane"], "w5:p1")

    def test_a_failed_fresh_start_gives_the_old_conversation_back(self):
        self.scenario(*MIXED, SPLIT, START_FAILS, up=None)
        with self.assertRaises(hq.RestartError):
            hq.rotate_now()
        self.assertEqual((self.saved()["pane"], self.saved()["session"]), ("w8:p9", "sess-9"))


class OnlyTilesTest(Fixture):
    def test_closes_the_tiles_and_the_space_and_nothing_else(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(*CLOSES), [["pane", "close", "w8:p2"], ["pane", "close", "w8:p3"],
                                               ["workspace", "close", "w8"]])
        # Exactly one athena space afterwards: the old one closed before the only new one opened.
        (create,) = self.calls(["workspace", "create"])
        self.assertEqual(create[create.index("--label") + 1], "athena")
        calls = self.calls()
        self.assertLess(calls.index(["workspace", "close", "w8"]), calls.index(create))
        self.assertEqual(self.calls(["pane", "split"]), [])

    def test_a_space_that_closed_with_its_last_tile_is_fine(self):
        self.scenario(fails(["workspace", "close", "w8"], "workspace_not_found"), *TILES, CREATED)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(len(self.calls(["workspace", "create"])), 1)

    def test_a_tile_already_gone_is_fine(self):
        self.scenario(fails(["pane", "close", "w8:p2"], "pane_not_found"), *TILES, CREATED)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(self.saved()["pane"], "w5:p1")

    def test_a_space_that_will_not_close_opens_no_second_one(self):
        self.scenario(fails(["workspace", "close", "w8"], "cli_timeout"), *TILES, CREATED)
        with self.assertRaises(herdr.HerdrError):
            hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(["workspace", "create"]), [])
        self.assertEqual(self.saved()["pane"], "w8:p1")


class OtherPanesTest(Fixture):
    def test_leaves_the_pane_and_the_space_and_splits_in_hqs_cwd(self):
        self.scenario(*MIXED, SPLIT)
        hq.hq(cwd="/somewhere/else", start_board=False, focus=False)
        self.assertEqual(self.calls(*CLOSES), [["pane", "close", "w8:p2"]])
        (split,) = self.calls(["pane", "split"])
        self.assertEqual(split[split.index("--cwd") + 1], os.path.realpath(self.cwd))
        self.assertEqual(split[2], "w8:p4")
        self.assertIn("--no-focus", split)
        self.assertEqual(self.calls(["workspace", "create"]), [])
        touched = [c for c in self.calls() if "w8:p4" in c and c[:2] not in (["pane", "get"], ["pane", "split"])]
        self.assertEqual(touched, [])
        self.assert_started_in("w8:p9", "--resume", "sess-9")

    def test_a_pane_with_no_label_is_not_a_tile(self):
        self.scenario(listing("w8:p4"), pane("w8:p4"), SPLIT)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(*CLOSES), [])
        self.assert_started_in("w8:p9", "--resume", "sess-9")

    def test_auto_claude_is_kept_off_the_new_pane_while_it_starts(self):
        self.scenario(*MIXED, SPLIT)
        claimed = []
        real = herdr.call

        def spy(*args, **kw):
            if args[:2] in (("pane", "split"), ("agent", "start")):
                claimed.append(paths.claim_file(str(self.cwd)).exists())
            return real(*args, **kw)
        with mock.patch.object(hq.herdr, "call", side_effect=spy):
            hq.hq(start_board=False, focus=False)
        self.assertEqual(claimed, [True, True])
        self.assertFalse(paths.claim_file(str(self.cwd)).exists())

    def test_tiles_saved_or_not_are_closed_so_none_is_orphaned(self):
        # board.json names one tile; the other only its label says is a tile. The saved HQ pane is the lost one.
        board.save({"hq": "w8:p1", "key": "k", "columns": [{"pane": "w8:p2", "name": "abc-1", "terminal": "t"}]})
        self.scenario(listing("w8:p2", "w8:p3", "w8:p4"), pane("w8:p2", "board:abc-1"), pane("w8:p3", "board:gone"),
                      pane("w8:p4", "notes"), SPLIT)
        hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(["pane", "close"]), [["pane", "close", "w8:p2"], ["pane", "close", "w8:p3"]])
        self.assertEqual(self.saved()["pane"], "w8:p9")


class HqJsonFirstTest(Fixture):
    def pane_at_start(self, run):
        seen = []
        real = herdr.call

        def spy(*args, **kw):
            if args[:2] == ("agent", "start"):
                seen.append(hq.load().get("pane"))
            return real(*args, **kw)
        with mock.patch.object(hq.herdr, "call", side_effect=spy):
            run()
        return seen

    def test_a_split_pane_is_recorded_before_the_start(self):
        for name, run in (("hq", lambda: hq.hq(start_board=False, focus=False)),
                          ("restart", lambda: hq.restart_now(update=False)), ("rotate", hq.rotate_now)):
            with self.subTest(name):
                self.fresh_run()
                self.scenario(*MIXED, SPLIT)
                self.assertEqual(self.pane_at_start(run)[-1], "w8:p9")

    def test_a_new_space_is_recorded_before_the_start(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED)
        self.assertEqual(self.pane_at_start(lambda: hq.hq(start_board=False, focus=False)), ["w5:p1"])

    def test_a_failed_start_then_a_retry_makes_one_new_pane(self):
        self.scenario(pane("w8:p9"), *MIXED, SPLIT, START_FAILS, up=None)
        with self.assertRaises(hq.ResumeError):
            hq.hq(start_board=False, focus=False)
        self.assertEqual(self.saved()["pane"], "w8:p9")
        result = hq.hq(start_board=False, focus=False)
        self.assertEqual(len(self.calls(["pane", "split"])), 1)
        self.assertEqual(len(self.calls(["pane", "close"])), 1)
        self.assertEqual(len(self.starts()), 2)
        self.assert_started_in("w8:p9", "--resume", "sess-9")
        self.assertEqual(result["pane"], "w8:p9")

    def test_a_failed_restart_then_a_retry_makes_one_new_pane(self):
        self.scenario(pane("w8:p9"), *MIXED, SPLIT, START_FAILS, up=None)
        with self.assertRaises(hq.RestartError):
            hq.restart_now(update=False)
        hq.restart_now(update=False)
        self.assertEqual(len(self.calls(["pane", "split"])), 1)
        self.assert_started_in("w8:p9", "--resume", "sess-9")

    def test_a_failed_start_in_a_new_space_leaves_no_space_and_a_retry_opens_one(self):
        self.scenario(SPACE_CLOSED, *TILES, CREATED, START_FAILS, up=None)
        with self.assertRaises(hq.ResumeError):
            hq.hq(start_board=False, focus=False)
        self.assertEqual(self.calls(["workspace", "close"]), [["workspace", "close", "w8"], ["workspace", "close", "w5"]])
        self.assertEqual((self.saved()["pane"], self.saved()["workspace"]), ("w8:p1", "w8"))
        self.assertEqual(hq.hq(start_board=False, focus=False)["pane"], "w5:p1")
        self.assertEqual(len(self.calls(["workspace", "create"])), 2)  # one closed above, one open now
        self.assertEqual(len(self.calls(["workspace", "close"])), 2)


class SlowHerdrTest(Fixture):
    """Only herdr's pane_not_found says HQ's pane is gone: a slow herdr must never close tiles or replace a living HQ."""
    ROUTES = (("hq", lambda: hq.hq(start_board=False, focus=False), herdr.HerdrError),
              ("restart", lambda: hq.restart_now(update=False), hq.RestartError),
              ("rotate", hq.rotate_now, hq.RestartError))

    def check(self, *rules):
        before = (paths.state_dir() / "hq.json").read_text()
        for name, run, error in self.ROUTES:
            with self.subTest(name):
                self.scenario(*rules, *TILES, SPLIT, CREATED)
                with self.assertRaises(error):
                    run()
                self.assert_nothing_moved(before)

    def test_a_timeout_on_the_recorded_pane_moves_nothing(self):
        self.check(fails(["pane", "get", "w8:p1"], "cli_timeout"))

    def test_a_herdr_too_slow_for_anything_moves_nothing(self):
        before = (paths.state_dir() / "hq.json").read_text()
        write_scenario(self.tmp.name, [fails([], "cli_timeout")])
        with self.assertRaises(herdr.HerdrError):
            hq.hq(start_board=False, focus=False)
        self.assert_nothing_moved(before)

    def test_a_timeout_on_the_space_opens_no_second_one(self):
        self.check(fails(["workspace", "get", "w8"], "cli_timeout"))

    def test_a_timeout_listing_the_space_closes_nothing(self):
        self.check(fails(["pane", "list"], "cli_timeout"))

    def test_a_timeout_reading_a_pane_of_the_space_closes_nothing(self):
        self.check(fails(["pane", "get", "w8:p2"], "cli_timeout"))

    def test_a_timeout_message_names_no_route(self):
        self.scenario(fails(["pane", "get", "w8:p1"], "cli_timeout"))
        for run in (lambda: hq.restart_now(update=False), hq.rotate_now):
            with self.assertRaises(hq.RestartError) as caught:
                run()
            self.assertNotIn("`athena", str(caught.exception))
            self.assertIn("nothing", str(caught.exception))


class WaysOutTest(Fixture):
    """Every route a failure on this path names has to work in the state it failed in."""
    ROUTES = {"athena hq": lambda: hq.hq(start_board=False, focus=False),
              "athena hq rotate": hq.rotate_now,
              "athena doctor": lambda: hq.hq(focus=False, start_board=False, start_dash=False, fresh=False)}

    def test_each_route_a_failed_start_names_works(self):
        for name, run, error in (("hq", self.ROUTES["athena hq"], hq.ResumeError),
                                 ("restart", lambda: hq.restart_now(update=False), hq.RestartError),
                                 ("rotate", hq.rotate_now, hq.RestartError)):
            self.fresh_run()
            self.scenario(*MIXED, SPLIT, START_FAILS, up=None)
            with self.assertRaises(error) as caught:
                run()
            text = str(caught.exception)
            self.assertNotIn("agent_pane_not_found", text)
            named = re.findall(r"`(athena[^`]*)`", text)
            self.assertTrue(named, text)
            for route in named:
                with self.subTest(failed=name, route=route):
                    self.fresh_run()
                    self.scenario(*MIXED, SPLIT)
                    self.ROUTES[route]()
                    self.assertEqual(hq.load()["pane"], "w8:p9")


class CommandTest(Fixture):
    """`athena hq restart` from a terminal tells the terminal; from HQ's own pane it must stay detached."""

    def run_cli(self):
        out, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stderr):
            code = cli.main(["hq", "restart", "--no-update"])
        return code, out.getvalue(), stderr.getvalue()

    def test_outside_hqs_pane_a_failed_start_reaches_the_terminal(self):
        self.env.set("HERDR_PANE_ID", "w9:p5")
        self.scenario(*MIXED, SPLIT, fails(["agent", "start"], "agent_exited"), up=None)
        code, _, stderr = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertIn("FAILED", stderr)
        self.assertIn("resuming conversation sess-9 failed", stderr)
        self.assertNotIn("agent_pane_not_found", stderr)

    def test_with_no_herdr_pane_at_all_a_failed_start_reaches_the_terminal(self):
        self.scenario(*MIXED, SPLIT, fails(["agent", "start"], "agent_exited"), up=None)
        code, _, stderr = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertIn("FAILED", stderr)

    def test_outside_hqs_pane_a_restart_that_works_exits_zero(self):
        self.env.set("HERDR_PANE_ID", "w9:p5")
        self.scenario(*MIXED, SPLIT)
        code, _, stderr = self.run_cli()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.saved()["pane"], "w8:p9")

    def test_with_no_hq_recorded_the_terminal_hears_so(self):
        (paths.state_dir() / "hq.json").unlink()
        code, _, stderr = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertIn("there is no HQ to restart", stderr)

    def test_only_this_restarts_log_lines_are_relayed(self):
        hq._log_file().write_text("FAILED: an older restart\n")
        self.env.set("HERDR_PANE_ID", "w9:p5")
        self.scenario(*MIXED, SPLIT)
        _, _, stderr = self.run_cli()
        self.assertNotIn("an older restart", stderr)

    def test_ctrl_c_only_stops_the_copying(self):
        self.env.set("HERDR_PANE_ID", "w9:p5")
        with mock.patch.object(hq.subprocess, "Popen") as popen:
            popen.return_value.pid = 7
            popen.return_value.poll.side_effect = KeyboardInterrupt
            code, _, stderr = self.run_cli()
        self.assertEqual(code, 130)
        self.assertIn("the restart carries on", stderr)
        popen.return_value.terminate.assert_not_called()
        popen.return_value.kill.assert_not_called()

    def test_from_hqs_own_pane_it_stays_detached(self):
        self.env.set("HERDR_PANE_ID", "w8:p1")
        with mock.patch.object(hq.subprocess, "Popen") as popen:
            popen.return_value.pid = 7
            code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("restarting HQ; log at", out)
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.return_value.method_calls, [])  # no wait, no poll: it may outlive this process
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
