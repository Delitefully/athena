"""athena watch --tagged: which lines wake HQ, the status line, and the stacked-PR watch."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import ledger, paths, watch

WAKE = [
    "plt-1 report DONE",
    "plt-1 report DONE_WITH_CONCERNS",
    "plt-1 report BLOCKED",
    "plt-1 report NEEDS_CONTEXT",
    "plt-1 state working->blocked",
    "plt-1 state idle->blocked (startup prompt)",
    "plt-1 state working->exited",
    "plt-1 pr opened https://github.com/o/r/pull/7",
    "plt-1 pr merged",
    "plt-1 pr closed",
    "plt-1 checks pending->failure",
    "plt-1 review CHANGES_REQUESTED",
    "plt-1 review APPROVED",
    "plt-1 review comments +2",
    "platform main moved 1a2b3c4..4d5e6f7",
    "stack o/platform#5844: OPEN base=a MERGEABLE -> MERGED base=a UNKNOWN",
    "stack o/platform#5856: OPEN base=a MERGEABLE -> OPEN base=a CONFLICTING",
    "plt-1 gone from the ledger",
    "plt-1 new worker (blocked, startup prompt)",
    "plt-1 new worker (exited)",
    "plt-1 goal not sent: herdr has no pane w9:p1",
    "plt-1 goal unconfirmed (timeout): check its pane, do not resend blindly",
    "watch error: gh: rate limited",
]

ROUTINE = [
    "watching: plt-1=working, plt-2=idle",
    "watching: no live workers",
    "plt-1 state working->idle",
    "plt-1 state idle->working",
    "plt-1 state done->idle",
    "plt-1 state working->done",
    "plt-1 state starting->working",
    "plt-1 checks none->pending",
    "plt-1 checks pending->success",
    "plt-1 review REVIEW_REQUIRED",
    "plt-1 new worker (working)",
    "plt-1 new worker (starting)",
    "plt-1 goal sent",
    "stack o/platform#5844: OPEN base=a MERGEABLE -> OPEN base=b MERGEABLE",
    "stack o/platform#5844: OPEN base=a CONFLICTING -> OPEN base=a MERGEABLE",
]


class ClassifyTest(unittest.TestCase):
    def test_every_actionable_kind_wakes(self):
        for line in WAKE:
            with self.subTest(line=line):
                self.assertEqual(watch.classify(line), "wake")

    def test_every_routine_kind_is_status_only(self):
        for line in ROUTINE:
            with self.subTest(line=line):
                self.assertEqual(watch.classify(line), "status")

    def test_gone_after_retire_is_routine(self):
        self.assertEqual(watch.classify("plt-1 gone from the ledger", retired={"plt-1"}), "status")
        self.assertEqual(watch.classify("plt-1 gone from the ledger", retired={"plt-2"}), "wake")

    def test_unknown_lines_wake(self):
        # Something athena watch learns to say later should reach HQ, not vanish into the status line.
        self.assertEqual(watch.classify("plt-1 something new happened"), "wake")


def _w(state, pr=None, report=None):
    return {"state": state, "pr": pr, "report": report}


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_grouped_needs_you_then_working_then_done(self):
        snap = {"workers": {"pr6519": _w("working"), "plt-4041": _w("done"), "plt-9": _w("blocked"),
                            "plt-4042": _w("idle", report={"status": "DONE"}), "plt-7": _w("idle"),
                            "plt-8": _w("working", report=None), "plt-6": _w("idle", report={"status": "NEEDS_CONTEXT"}),
                            "plt-5": _w("exited")}}
        b = watch.board(snap)
        self.assertEqual(b["needs"], ["plt-6 needs context", "plt-9 blocked", "plt-5 exited"])
        self.assertEqual(b["active"], [["working", ["plt-8", "pr6519"]], ["idle", ["plt-7"]]])
        self.assertEqual(b["done"], ["plt-4041", "plt-4042"])
        self.assertEqual(watch.summary(snap), "plt-6 needs context · plt-9 blocked · plt-5 exited · "
                                              "plt-8, pr6519 working · plt-7 idle · plt-4041, plt-4042 done")

    def test_a_blocked_report_is_blocked_whatever_the_hook_state(self):
        b = watch.board({"workers": {"a": _w("idle", report={"status": "BLOCKED"})}})
        self.assertEqual(b["needs"], ["a blocked"])

    def test_prs_that_need_the_human(self):
        def pr(n, **kw):
            return {"state": "OPEN", "draft": False, "review": "", "url": f"https://github.com/o/r/pull/{n}", **kw}
        snap = {"workers": {"a": _w("working", pr(1, draft=True, review="APPROVED")), "b": _w("done", pr(2)),
                            "c": _w("idle", pr(3)), "d": _w("done", pr(4, draft=True)), "e": _w("working", pr(5)),
                            "f": _w("idle", pr(6, state="MERGED"))}}
        self.assertEqual(watch.board(snap)["prs"], ["#1", "#2", "#3"])
        self.assertEqual(watch.summary(snap), "3 PRs need you · a, e working · c, f idle · b, d done")
        one = {"workers": {"a": _w("working", pr(1, review="APPROVED"))}}
        self.assertEqual(watch.summary(one), "1 PR needs you · a working")

    def test_stack_prs_awaiting_the_human(self):
        # The bottom of a chain (based on main, not a draft) awaits a review or merge; a conflicting one too.
        snap = {"workers": {}, "stacks": {"o/r#1": "OPEN base=b CONFLICTING", "o/r#2": "OPEN base=b MERGEABLE",
                                          "o/r#3": "OPEN base=main MERGEABLE", "o/r#4": "OPEN base=main DRAFT MERGEABLE",
                                          "o/r#5": "MERGED base=main UNKNOWN"}}
        self.assertEqual(watch.board(snap)["prs"], ["#1", "#3"])
        self.assertEqual(watch.summary(snap), "2 PRs need you · no live workers")

    def test_prs_named_in_needs_you_and_one_pr_counted_once(self):
        paths.ensure()
        (paths.state_dir() / "needs-you.md").write_text(
            "- Approve and merge platform #6547: https://github.com/o/platform/pull/6547\n"
            "- Approve and merge #6552: https://github.com/o/platform/pull/6552.\n"
            "- File a ticket? nothing to merge\n")
        snap = {"workers": {"plt-4041": _w("idle", {"state": "OPEN", "draft": False, "review": "",
                                                    "url": "https://github.com/o/platform/pull/6552"},
                                           report={"status": "DONE"})},
                "stacks": {"o/platform#5856": "OPEN base=main MERGEABLE", "o/platform#6552": "OPEN base=main MERGEABLE"}}
        self.assertEqual(watch.board(snap)["prs"], ["#5856", "#6547", "#6552"])
        self.assertEqual(watch.summary(snap), "3 PRs need you · plt-4041 done")


class StackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_stack_file_round_trip(self):
        self.assertEqual(watch.stacks(), [])
        watch.set_stack("o/platform", [5844, 5856])
        watch.set_stack("o/other", [12])
        watch.set_stack("o/platform", [5856, 5858])
        self.assertEqual(watch.stacks(), [{"repo": "o/platform", "prs": [5856, 5858]}, {"repo": "o/other", "prs": [12]}])
        self.assertEqual(json.loads((paths.state_dir() / "stack.json").read_text())["stacks"][1]["repo"], "o/other")
        watch.set_stack("o/platform", [])
        self.assertEqual(watch.stacks(), [{"repo": "o/other", "prs": [12]}])
        self.assertEqual([p.name for p in paths.state_dir().iterdir() if p.name.startswith("stack")], ["stack.json"])

    def test_unreadable_stack_file_is_no_stack(self):
        paths.ensure()
        (paths.state_dir() / "stack.json").write_text("not json")
        self.assertEqual(watch.stacks(), [])

    def _gh(self, answers):
        def run(argv, **kw):
            n = argv[3]
            value = answers.get(n)
            if value is None:
                return mock.Mock(returncode=1, stdout="", stderr="no such PR")
            return mock.Mock(returncode=0, stdout=json.dumps(value), stderr="")
        return mock.patch.object(watch.subprocess, "run", side_effect=run)

    def test_stack_snapshot_reads_gh_and_skips_unknown(self):
        watch.set_stack("o/platform", [1, 2, 3])
        answers = {"1": {"state": "OPEN", "baseRefName": "main", "mergeable": "MERGEABLE"},
                   "2": {"state": "OPEN", "baseRefName": "b1", "mergeable": "UNKNOWN"},
                   "3": {"state": "OPEN", "baseRefName": "main", "mergeable": "CONFLICTING", "isDraft": True}}
        with self._gh(answers) as run:
            snap = watch.stack_snapshot({"o/platform#2": "OPEN base=b1 MERGEABLE"})
        self.assertEqual(snap, {"o/platform#1": "OPEN base=main MERGEABLE", "o/platform#2": "OPEN base=b1 MERGEABLE",
                                "o/platform#3": "OPEN base=main DRAFT CONFLICTING"})
        self.assertEqual(watch.classify("stack o/platform#3: OPEN base=main DRAFT MERGEABLE -> OPEN base=main DRAFT CONFLICTING"),
                         "wake")
        self.assertEqual(run.call_args_list[0].args[0][:6], ["gh", "pr", "view", "1", "--repo", "o/platform"])

    def test_stack_lines_in_diff(self):
        prev = {"workers": {}, "mains": {}, "stacks": {"o/p#1": "OPEN base=main MERGEABLE", "o/p#2": "OPEN base=b1 MERGEABLE"}}
        cur = {"workers": {}, "mains": {}, "stacks": {"o/p#1": "MERGED base=main UNKNOWN", "o/p#2": "OPEN base=b1 MERGEABLE",
                                                       "o/p#3": "OPEN base=b2 MERGEABLE"}}
        self.assertEqual(watch.diff(prev, cur), ["stack o/p#1: OPEN base=main MERGEABLE -> MERGED base=main UNKNOWN"])

    def test_step_polls_stacks_only_when_fetching(self):
        watch.set_stack("o/p", [1])
        with self._gh({"1": {"state": "OPEN", "baseRefName": "main", "mergeable": "MERGEABLE"}}) as run, \
                mock.patch.object(watch.spawn, "deliver_pending", return_value=[]):
            cur, _ = watch.step(None, fetch=True, with_pr=False)
            self.assertEqual(cur["stacks"], {"o/p#1": "OPEN base=main MERGEABLE"})
            again, lines = watch.step(cur, fetch=False, with_pr=False)
            self.assertEqual((again["stacks"], lines), (cur["stacks"], []))
            self.assertEqual(run.call_count, 1)


class TaggedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def _emit(self, *args, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            state = watch.emit(*args, **kw)
        return [json.loads(line) for line in out.getvalue().splitlines()], state

    def test_plain_output_is_unchanged(self):
        out = io.StringIO()
        with redirect_stdout(out):
            watch.emit(["a state working->idle", "a report DONE"], {"workers": {}}, tagged=False)
        self.assertEqual(out.getvalue(), "a state working->idle\na report DONE\n")

    def test_tagged_lines_and_status_once_per_change(self):
        snap = {"workers": {"a": _w("working")}}
        rows, last = self._emit(["watching: a=working"], snap, tagged=True)
        board = {"needs": [], "prs": [], "active": [["working", ["a"]]], "done": []}
        self.assertEqual(rows, [{"line": "watching: a=working", "wake": False}, {"status": "a working", "board": board}])
        rows, last = self._emit([], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [])
        snap = {"workers": {"a": _w("done", report={"status": "DONE"})}}
        rows, _ = self._emit(["a state working->done", "a report DONE"], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [{"line": "a state working->done", "wake": False}, {"line": "a report DONE", "wake": True},
                                {"status": "a done", "board": {"needs": [], "prs": [], "active": [], "done": ["a"]}}])

    def test_tagged_gone_after_retire_stays_quiet(self):
        ledger.append("spawn", "a")
        ledger.append("retire", "a")
        ledger.append("spawn", "b")
        rows, _ = self._emit(["a gone from the ledger", "b gone from the ledger"], {"workers": {}}, tagged=True)
        self.assertEqual(rows[:2], [{"line": "a gone from the ledger", "wake": False},
                                    {"line": "b gone from the ledger", "wake": True}])

    def test_tagged_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            watch.report_error(RuntimeError("boom"), tagged=True)
        self.assertEqual(json.loads(out.getvalue()), {"line": "watch error: boom", "wake": True})



class StackCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_set_show_clear(self):
        from athena_lib import cli
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["stack", "set", "o/platform", "5844", "5856"]), 0)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["stack"])
        self.assertEqual(json.loads(out.getvalue()), [{"repo": "o/platform", "prs": [5844, 5856]}])
        with redirect_stdout(io.StringIO()):
            cli.main(["stack", "clear", "o/platform"])
        self.assertEqual(watch.stacks(), [])

    def test_watch_takes_tagged(self):
        from athena_lib import cli
        self.assertTrue(cli.parser().parse_args(["watch", "--tagged"]).tagged)


if __name__ == "__main__":
    unittest.main()
