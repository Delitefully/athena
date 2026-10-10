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
    "abc-102 report DONE",
    "abc-102 report DONE_WITH_CONCERNS",
    "abc-102 report BLOCKED",
    "abc-102 report NEEDS_CONTEXT",
    "abc-102 state working->blocked",
    "abc-102 state idle->blocked (startup prompt)",
    "abc-102 state working->exited",
    "abc-102 pr opened https://github.com/o/r/pull/7",
    "abc-102 pr merged",
    "abc-102 pr closed",
    "abc-102 checks pending->failure",
    "abc-102 review CHANGES_REQUESTED",
    "abc-102 review APPROVED",
    "abc-102 review comments +2",
    "web main moved 1a2b3c4..4d5e6f7",
    "stack o/web#901: OPEN base=a MERGEABLE -> MERGED base=a UNKNOWN",
    "stack o/web#902: OPEN base=a MERGEABLE -> OPEN base=a CONFLICTING",
    "abc-102 gone from the ledger",
    "abc-102 new worker (blocked, startup prompt)",
    "abc-102 new worker (exited)",
    "abc-102 goal not sent: herdr has no pane w9:p1",
    "abc-102 goal unconfirmed (timeout): check its pane, do not resend blindly",
    "watch error: gh: rate limited",
]

ROUTINE = [
    "watching: abc-102=working, abc-103=idle",
    "watching: no live workers",
    "abc-102 state working->idle",
    "abc-102 state idle->working",
    "abc-102 state done->idle",
    "abc-102 state working->done",
    "abc-102 state starting->working",
    "abc-102 checks none->pending",
    "abc-102 checks pending->success",
    "abc-102 review REVIEW_REQUIRED",
    "abc-102 new worker (working)",
    "abc-102 new worker (starting)",
    "abc-102 goal sent",
    "stack o/web#901: OPEN base=a MERGEABLE -> OPEN base=b MERGEABLE",
    "stack o/web#901: OPEN base=a CONFLICTING -> OPEN base=a MERGEABLE",
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
        self.assertEqual(watch.classify("abc-102 gone from the ledger", retired={"abc-102"}), "status")
        self.assertEqual(watch.classify("abc-102 gone from the ledger", retired={"abc-103"}), "wake")

    def test_unknown_lines_wake(self):
        # Something athena watch learns to say later should reach HQ, not vanish into the status line.
        self.assertEqual(watch.classify("abc-102 something new happened"), "wake")


def _w(state, pr=None, report=None):
    return {"state": state, "pr": pr, "report": report}


class SummaryTest(unittest.TestCase):
    def test_last_state_per_worker_most_urgent_first(self):
        snap = {"workers": {"pr908": _w("working"), "abc-113": _w("done"), "abc-104": _w("blocked")}}
        self.assertEqual(watch.summary(snap), "abc-104 blocked · abc-113 done · pr908 working")

    def test_prs_that_need_the_human(self):
        approved = {"state": "OPEN", "draft": True, "review": "APPROVED"}
        ready = {"state": "OPEN", "draft": False, "review": ""}
        draft = {"state": "OPEN", "draft": True, "review": ""}
        snap = {"workers": {"a": _w("working", approved), "b": _w("done", ready), "c": _w("idle", ready),
                            "d": _w("done", draft), "e": _w("working", ready)}}
        self.assertTrue(watch.summary(snap).endswith(" · 3 PRs need you"))
        one = {"workers": {"a": _w("working", approved)}}
        self.assertTrue(watch.summary(one).endswith(" · 1 PR needs you"))

    def test_conflicting_stack_needs_the_human(self):
        snap = {"workers": {}, "stacks": {"o/r#1": "OPEN base=a CONFLICTING", "o/r#2": "OPEN base=a MERGEABLE"}}
        self.assertEqual(watch.summary(snap), "no live workers · 1 PR needs you")


class StackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_stack_file_round_trip(self):
        self.assertEqual(watch.stacks(), [])
        watch.set_stack("o/web", [901, 902])
        watch.set_stack("o/other", [12])
        watch.set_stack("o/web", [902, 903])
        self.assertEqual(watch.stacks(), [{"repo": "o/web", "prs": [902, 903]}, {"repo": "o/other", "prs": [12]}])
        self.assertEqual(json.loads((paths.state_dir() / "stack.json").read_text())["stacks"][1]["repo"], "o/other")
        watch.set_stack("o/web", [])
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
        watch.set_stack("o/web", [1, 2, 3])
        answers = {"1": {"state": "OPEN", "baseRefName": "main", "mergeable": "MERGEABLE"},
                   "2": {"state": "OPEN", "baseRefName": "b1", "mergeable": "UNKNOWN"}}
        with self._gh(answers) as run:
            snap = watch.stack_snapshot({"o/web#2": "OPEN base=b1 MERGEABLE"})
        self.assertEqual(snap, {"o/web#1": "OPEN base=main MERGEABLE", "o/web#2": "OPEN base=b1 MERGEABLE"})
        self.assertEqual(run.call_args_list[0].args[0][:6], ["gh", "pr", "view", "1", "--repo", "o/web"])

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
        self.assertEqual(rows, [{"line": "watching: a=working", "wake": False}, {"status": "a working"}])
        rows, last = self._emit([], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [])
        snap = {"workers": {"a": _w("done", report={"status": "DONE"})}}
        rows, _ = self._emit(["a state working->done", "a report DONE"], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [{"line": "a state working->done", "wake": False}, {"line": "a report DONE", "wake": True},
                                {"status": "a done"}])

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
            self.assertEqual(cli.main(["stack", "set", "o/web", "901", "902"]), 0)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["stack"])
        self.assertEqual(json.loads(out.getvalue()), [{"repo": "o/web", "prs": [901, 902]}])
        with redirect_stdout(io.StringIO()):
            cli.main(["stack", "clear", "o/web"])
        self.assertEqual(watch.stacks(), [])

    def test_watch_takes_tagged(self):
        from athena_lib import cli
        self.assertTrue(cli.parser().parse_args(["watch", "--tagged"]).tagged)


if __name__ == "__main__":
    unittest.main()
