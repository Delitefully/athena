"""athena watch --tagged: which lines wake HQ, the status line, and the stacked-PR watch."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import asks, dashstate, ledger, paths, watch

WAKE = [
    "abc-1 report DONE",
    "abc-1 report DONE_WITH_CONCERNS",
    "abc-1 report BLOCKED",
    "abc-1 report NEEDS_CONTEXT",
    "abc-1 state working->blocked",
    "abc-1 state idle->blocked (startup prompt)",
    "abc-1 state working->exited",
    "abc-1 pr opened https://github.com/o/r/pull/7",
    "abc-1 pr merged",
    "abc-1 pr closed",
    "abc-1 checks pending->failure",
    "abc-1 review CHANGES_REQUESTED",
    "abc-1 review APPROVED",
    "abc-1 review comments +2",
    "web main moved 1a2b3c4..4d5e6f7",
    "stack o/web#41: OPEN base=a MERGEABLE -> MERGED base=a UNKNOWN",
    "stack o/web#42: OPEN base=a MERGEABLE -> OPEN base=a CONFLICTING",
    "abc-1 gone from the ledger",
    "abc-1 new worker (blocked, startup prompt)",
    "abc-1 new worker (exited)",
    "abc-1 goal not sent: herdr has no pane w9:p1",
    "abc-1 goal unconfirmed (timeout): check its pane, do not resend blindly",
    "watch error: gh: rate limited",
    "dash-queue 41",
]

ROUTINE = [
    "watching: abc-1=working, abc-2=idle",
    "watching: no live workers",
    "abc-1 state working->idle",
    "abc-1 state idle->working",
    "abc-1 state done->idle",
    "abc-1 state working->done",
    "abc-1 state starting->working",
    "abc-1 checks none->pending",
    "abc-1 checks pending->success",
    "abc-1 review REVIEW_REQUIRED",
    "abc-1 new worker (working)",
    "abc-1 new worker (starting)",
    "abc-1 goal sent",
    "stack o/web#41: OPEN base=a MERGEABLE -> OPEN base=b MERGEABLE",
    "stack o/web#41: OPEN base=a CONFLICTING -> OPEN base=a MERGEABLE",
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
        self.assertEqual(watch.classify("abc-1 gone from the ledger", retired={"abc-1"}), "status")
        self.assertEqual(watch.classify("abc-1 gone from the ledger", retired={"abc-2"}), "wake")

    def test_unknown_lines_wake(self):
        # Something athena watch learns to say later should reach HQ, not vanish into the status line.
        self.assertEqual(watch.classify("abc-1 something new happened"), "wake")


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
        snap = {"workers": {"pr47": _w("working"), "abc-101": _w("done"), "abc-9": _w("blocked"),
                            "abc-102": _w("idle", report={"status": "DONE"}), "abc-7": _w("idle"),
                            "abc-8": _w("working", report=None), "abc-6": _w("idle", report={"status": "NEEDS_CONTEXT"}),
                            "abc-5": _w("exited")}}
        b = watch.board(snap)
        self.assertEqual(b["needs"], ["abc-6 needs context", "abc-9 blocked", "abc-5 exited"])
        self.assertEqual(b["active"], [["working", ["abc-8", "pr47"]], ["idle", ["abc-7"]]])
        self.assertEqual(b["done"], ["abc-101", "abc-102"])
        self.assertEqual(watch.summary(snap), "abc-6 needs context · abc-9 blocked · abc-5 exited · "
                                              "abc-8, pr47 working · abc-7 idle · abc-101, abc-102 done")

    def test_a_blocked_report_is_blocked_whatever_the_hook_state(self):
        b = watch.board({"workers": {"a": _w("idle", report={"status": "BLOCKED"})}})
        self.assertEqual(b["needs"], ["a blocked"])

    def test_prs_that_need_the_human(self):
        # Only a PR whose dash row is in Needs you counts: never one that waits on a teammate's review.
        def pr(n, review="REVIEW_REQUIRED", requested=(), **kw):
            p = {"state": "OPEN", "draft": False, "review": review, "checks": "success", "requested": list(requested),
                 "reviews": {}, "auto_merge": False, "queued": False, "url": f"https://github.com/o/r/pull/{n}", **kw}
            p["move"] = watch.pr.move(p, live=True, human_merge=False)
            return p
        done = {"status": "DONE"}
        snap = {"workers": {"a": _w("idle", pr(1, review="APPROVED"), report=done),  # approved, worker done: the human's go
                            "b": _w("done", pr(2)),  # no reviewer requested
                            "c": _w("idle", pr(3, requested=["alex"])),  # a teammate's move
                            "d": _w("done", pr(4, draft=True)),
                            "e": _w("working", pr(5)),  # still working: its worker's move
                            "f": _w("idle", pr(6, state="MERGED")),
                            "g": _w("blocked", pr(7)),  # counted as blocked, not again as a PR
                            "h": _w("idle", pr(8, review="APPROVED", auto_merge=True)),
                            "i": _w("idle", pr(9, review="CHANGES_REQUESTED", reviews={"x": "CHANGES_REQUESTED"})),
                            "j": _w("idle", {"state": "OPEN", "draft": False, "review": "",
                                             "url": "https://github.com/o/r/pull/10"}),  # no review data yet
                            "k": {**_w("idle", pr(11, review="")), "last_report": done},  # no review rule: the human's go
                            "l": _w("working", pr(12, review="")),  # ... once its worker is done
                            "m": _w("idle", pr(13, review="")),  # idle with no DONE report: not done
                            "n": _w("exited", pr(14, review="APPROVED")),  # exited mid-task
                            "o": _w("working", pr(15, review="APPROVED"))}}  # approved, still working
        self.assertEqual(watch.board(snap)["prs"], ["#1", "#2", "#11"])
        self.assertEqual(watch.summary(snap), "g blocked · n exited · 3 PRs need you · e, l, o working · "
                                              "c, f, h, i, j, k, m idle · a, b, d done")

    def test_one_pr_per_repo_and_number(self):
        def followed(n, repo):
            return {"state": "OPEN", "number": n, "move": {"label": "no reviewer requested", "needs": True}}
        snap = {"workers": {}, "prs": {"o/web#7": followed(7, "o/web"), "o/api#7": followed(7, "o/api")},
                "review_requests": [{"repo": "O/Web", "number": 7, "title": "t"}]}
        self.assertEqual(watch.board(snap)["prs"], ["#7", "#7"])

    def test_followed_prs_and_review_requests_count_by_their_row(self):
        def followed(n, label, needs):
            return {"state": "OPEN", "move": {"label": label, "needs": needs, "kind": "x"}, "number": n}
        snap = {"workers": {}, "prs": {"o/r#148": followed(148, "awaiting review · alex", False),
                                       "o/r#149": followed(149, "no reviewer requested", True),
                                       "o/r#150": followed(150, "checks failing", True),
                                       "o/r#151": {"state": "MERGED", "move": None, "number": 151}},
                "review_requests": [{"repo": "o/r", "number": 7, "title": "t"}, {"repo": "o/x", "number": 149, "title": "u"}]}
        self.assertEqual(watch.board(snap)["prs"], ["#7", "#149", "#149", "#150"])  # o/r#149 and o/x#149
        self.assertEqual(watch.summary(snap), "4 PRs need you · no live workers")

    def test_asks_never_add_prs(self):
        # needs-you.json is for decisions: a PR an ask is about counts only when its own row is in Needs you.
        paths.ensure()
        asks.add("Merge web #43? https://github.com/o/web/pull/43", pr="o/web#43")
        self.assertEqual(watch.board({"workers": {}})["prs"], [])


class BoardLinksTest(unittest.TestCase):
    """What the mod links: each PR to its repo from the ledger (a worker's origin) or stack.json, and the dash."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_links_and_repos_come_from_the_worker_repo_and_the_stack(self):
        def pr(n, repo="delitefully/ATHENA"):
            return {"state": "OPEN", "draft": False, "review": "APPROVED", "number": n,
                    "url": f"https://github.com/{repo}/pull/{n}", "move": {"label": "approved", "needs": True}}
        snap = {"workers": {"a": {**_w("idle", pr(12)), "slug": "Delitefully/athena"},
                            "b": {**_w("idle", pr(13)), "slug": None},
                            "c": _w("working")},
                "stacks": {"o/stack#42": "OPEN base=main MERGEABLE"},
                "prs": {"o/stack#42": {"state": "OPEN", "number": 42, "slug": "o/stack",
                                       "move": {"label": "no reviewer requested", "needs": True}},
                        "o/old#44": {"state": "OPEN", "number": 44, "slug": "o/old", "move": None}},
                "review_requests": [{"repo": "o/team", "number": 46, "title": "t"}]}
        b = watch.board(snap)
        self.assertEqual(b["prs"], ["#12", "#13", "#42", "#46"])
        self.assertEqual(b["links"], {"12": "Delitefully/athena", "42": "o/stack", "44": "o/old", "46": "o/team"})
        self.assertEqual(b["repos"], {"a": "Delitefully/athena"})
        self.assertNotIn("dash", b)

    def test_a_pr_in_another_repo_than_the_origin_draws_plain(self):
        # A fork, or a PR opened elsewhere: the origin is not where the PR lives, so neither #n nor the wake row links.
        pr = {"state": "OPEN", "draft": False, "review": "APPROVED", "number": 12,
              "url": "https://github.com/upstream/athena/pull/12", "move": {"label": "approved", "needs": True}}
        b = watch.board({"workers": {"a": {**_w("idle", pr), "slug": "me/athena"}}})
        self.assertEqual((b["prs"], b["links"], b["repos"]), (["#12"], {}, {}))

    def test_a_slug_with_a_trailing_newline_is_no_slug(self):
        snap = {"workers": {"a": {**_w("idle", {"state": "OPEN", "number": 7}), "slug": "o/r\n"}},
                "stacks": {"o/r\n#8": "OPEN base=main MERGEABLE"}}
        b = watch.board(snap)
        self.assertEqual((b["links"], b["repos"]), ({}, {}))

    def test_a_number_two_repos_share_links_nowhere(self):
        snap = {"workers": {"a": {**_w("idle", {"state": "OPEN", "number": 7}), "slug": "o/one"}},
                "stacks": {"o/two#7": "OPEN base=main MERGEABLE"}}
        self.assertEqual(watch.board(snap)["links"], {})

    def test_the_dash_url_while_it_runs(self):
        b = watch.board({"workers": {}, "dash": "http://127.0.0.1:28431/"})
        self.assertEqual(b["dash"], "http://127.0.0.1:28431/")

    def test_step_records_the_running_dash_and_nothing_once_it_stops(self):
        with mock.patch.object(watch.dash, "running", return_value={"url": "http://127.0.0.1:2843/", "pid": 1}), \
                mock.patch.object(watch, "snapshot", return_value={"workers": {}, "mains": {}}), \
                mock.patch.object(watch.spawn, "deliver_pending", return_value=[]):
            cur, _ = watch.step(None)
            self.assertEqual(cur["dash"], "http://127.0.0.1:2843/")
            watch.dash.running.return_value = None
            cur, _ = watch.step(None)
            self.assertIsNone(cur["dash"])

    def test_origin_slug_of_a_repo(self):
        repo = helpers_repo(self.tmp.name, "git@github.com:Delitefully/athena.git")
        self.assertEqual(watch.repo_slug(repo), "Delitefully/athena")
        other = helpers_repo(self.tmp.name + "/x", "/private/tmp/lab/origin.git")
        self.assertIsNone(watch.repo_slug(other))
        self.assertIsNone(watch.repo_slug(self.tmp.name + "/missing"))


def helpers_repo(path, remote):
    import os
    import subprocess
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "remote", "add", "origin", remote], check=True)
    return path


class StackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_stack_file_round_trip(self):
        self.assertEqual(watch.stacks(), [])
        watch.set_stack("o/web", [41, 42])
        watch.set_stack("o/other", [12])
        watch.set_stack("o/web", [42, 44])
        self.assertEqual(watch.stacks(), [{"repo": "o/web", "prs": [42, 44]}, {"repo": "o/other", "prs": [12]}])
        self.assertEqual(json.loads((paths.state_dir() / "stack.json").read_text())["stacks"][1]["repo"], "o/other")
        watch.set_stack("o/web", [])
        self.assertEqual(watch.stacks(), [{"repo": "o/other", "prs": [12]}])
        self.assertEqual([p.name for p in paths.state_dir().iterdir() if p.name.startswith("stack")], ["stack.json"])

    def test_unreadable_stack_file_is_no_stack(self):
        paths.ensure()
        (paths.state_dir() / "stack.json").write_text("not json")
        self.assertEqual(watch.stacks(), [])

    def _batch(self, answers):
        """pr.batch answering from `answers` (`repo#n` -> reduced PR), recording each call's keys."""
        calls = []

        def batch(keys):
            calls.append(list(keys))
            return {f"{r}#{n}": answers[f"{r}#{n}"] for r, n in keys if f"{r}#{n}" in answers}
        return mock.patch.object(watch.pr, "batch", side_effect=batch), calls

    def test_stack_snapshot_reads_one_batch_and_skips_unknown(self):
        watch.set_stack("o/web", [1, 2, 3])
        answers = {"o/web#1": {"state": "OPEN", "base": "main", "mergeable": "MERGEABLE"},
                   "o/web#2": {"state": "OPEN", "base": "b1", "mergeable": "UNKNOWN"},
                   "o/web#3": {"state": "OPEN", "base": "main", "mergeable": "CONFLICTING", "draft": True}}
        patch, calls = self._batch(answers)
        with patch:
            snap = watch.stack_snapshot({"o/web#2": "OPEN base=b1 MERGEABLE"})
        self.assertEqual(snap, {"o/web#1": "OPEN base=main MERGEABLE", "o/web#2": "OPEN base=b1 MERGEABLE",
                                "o/web#3": "OPEN base=main DRAFT CONFLICTING"})
        self.assertEqual(watch.classify("stack o/web#3: OPEN base=main DRAFT MERGEABLE -> OPEN base=main DRAFT CONFLICTING"),
                         "wake")
        self.assertEqual(calls, [[("o/web", 1), ("o/web", 2), ("o/web", 3)]])

    def test_a_failed_batch_keeps_the_last_stack_state(self):
        watch.set_stack("o/web", [1])
        with mock.patch.object(watch.pr, "batch", return_value=None):
            self.assertEqual(watch.stack_snapshot({"o/web#1": "OPEN base=main MERGEABLE"}),
                             {"o/web#1": "OPEN base=main MERGEABLE"})

    def test_stack_lines_in_diff(self):
        prev = {"workers": {}, "mains": {}, "stacks": {"o/p#1": "OPEN base=main MERGEABLE", "o/p#2": "OPEN base=b1 MERGEABLE"}}
        cur = {"workers": {}, "mains": {}, "stacks": {"o/p#1": "MERGED base=main UNKNOWN", "o/p#2": "OPEN base=b1 MERGEABLE",
                                                       "o/p#3": "OPEN base=b2 MERGEABLE"}}
        self.assertEqual(watch.diff(prev, cur), ["stack o/p#1: OPEN base=main MERGEABLE -> MERGED base=main UNKNOWN"])

    def test_step_polls_stacks_only_when_fetching(self):
        watch.set_stack("o/p", [1])
        patch, calls = self._batch({"o/p#1": {"state": "OPEN", "base": "main", "mergeable": "MERGEABLE"}})
        with patch, mock.patch.object(watch.spawn, "deliver_pending", return_value=[]), \
                mock.patch.object(watch.pr, "review_requested", return_value=[]) as search:
            cur, _ = watch.step(None, fetch=True, with_pr=False)
            self.assertEqual(cur["stacks"], {"o/p#1": "OPEN base=main MERGEABLE"})
            again, lines = watch.step(cur, fetch=False, with_pr=False)
            self.assertEqual((again["stacks"], lines), (cur["stacks"], []))
            self.assertEqual((len(calls), search.call_count), (1, 1))


def _reviewed(n, **kw):
    """A reduced PR as pr.batch or pr.summary gives it, with review data."""
    return {"number": n, "url": f"https://github.com/o/web/pull/{n}", "title": f"change {n}", "state": "OPEN",
            "draft": False, "review": "REVIEW_REQUIRED", "checks": "success", "base": "main", "mergeable": "MERGEABLE",
            "requested": [], "reviews": {}, "auto_merge": False, "queued": False, **kw}


class CITest(unittest.TestCase):
    """Whether a repo runs CI: asked once a day, and only for a PR with no checks yet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        watch._CI.clear()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_cached_for_a_day_and_kept_when_gh_fails(self):
        with mock.patch.object(watch.pr, "has_ci", return_value=False) as ask, \
                mock.patch.object(watch.time, "time", return_value=1000.0):
            self.assertFalse(watch.has_ci("o/web"))
            self.assertFalse(watch.has_ci("o/web"))
            self.assertEqual(ask.call_count, 1)
        with mock.patch.object(watch.pr, "has_ci", return_value=None) as ask, \
                mock.patch.object(watch.time, "time", return_value=1000.0 + 86400 + 1):
            self.assertFalse(watch.has_ci("o/web"))  # gh failed: the last answer stands
            self.assertEqual(ask.call_count, 1)
        with mock.patch.object(watch.pr, "has_ci", return_value=None):
            self.assertIsNone(watch.has_ci("o/new"))  # never known: treated as a repo with CI

    def test_none_is_green_only_where_the_repo_has_no_ci(self):
        p = _reviewed(5, review="", checks="none")
        with mock.patch.object(watch.pr, "has_ci", side_effect=lambda slug: slug == "o/ci"):
            self.assertEqual(watch._move(p, False, "o/ci", "o/ci")["label"], "checks starting")
            self.assertEqual(watch._move(p, False, "o/web", "o/web")["label"], "your go to merge")
        with mock.patch.object(watch.pr, "has_ci") as ask:
            self.assertEqual(watch._move(_reviewed(6, review=""), False, "o/x", "o/x")["label"], "your go to merge")
            self.assertEqual(ask.call_count, 0)  # checks reported: no need to ask


class FollowedTest(unittest.TestCase):
    """Every PR athena follows gets one row: a live worker's, a stacked one, and a retired worker's while open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo = helpers_repo(self.tmp.name + "/web", "git@github.com:o/web.git")
        watch._SLUGS.clear()
        paths.ensure()
        (paths.state_dir() / "config.json").write_text(json.dumps({"human_merge_repos": ["web"]}))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def _history(self, name, pr, age=3600):
        with open(paths.state_dir() / "history.jsonl", "a") as f:
            f.write(json.dumps({"ts": watch.time.time() - age, "name": name, "repo": self.repo, "pr": pr}) + "\n")

    def test_one_batch_for_stacked_and_retired_prs_and_one_row_each(self):
        ledger.append("spawn", "abc-452", repo=self.repo, branch="you/abc-452")
        ledger.append("spawn", "abc-300", repo=self.repo, branch="you/abc-300")
        ledger.append("retire", "abc-300")
        ledger.append("spawn", "abc-301", repo=self.repo, branch="you/abc-301")
        ledger.append("retire", "abc-301")
        self._history("abc-300", {"state": "OPEN", "url": "https://github.com/o/web/pull/300"})
        self._history("abc-301", {"state": "MERGED", "url": "https://github.com/o/web/pull/301"})
        self._history("abc-299", {"state": "OPEN", "url": "https://github.com/o/web/pull/299"}, age=15 * 24 * 3600)
        watch.set_stack("o/web", [148, 452])
        live_pr = _reviewed(452, requested=["alex"], auto_merge=True)
        answers = {"o/web#148": _reviewed(148, requested=["alex"], auto_merge=True),
                   "o/web#452": live_pr, "o/web#300": _reviewed(300, review="APPROVED")}
        calls = []

        def batch(keys):
            calls.append(sorted(keys))
            return {f"{r}#{n}": answers[f"{r}#{n}"] for r, n in keys if f"{r}#{n}" in answers}
        mine = [{"repo": "o/web", "number": 77, "title": "Teammate change", "author": "kim",
                 "url": "https://github.com/o/web/pull/77"}]
        with mock.patch.object(watch.pr, "batch", side_effect=batch), \
                mock.patch.object(watch.pr, "review_requested", return_value=mine) as search, \
                mock.patch.object(watch.spawn, "deliver_pending", return_value=[]), \
                mock.patch.object(watch.status, "collect", return_value={
                    "state": "idle", "pr": dict(live_pr), "report": None, "detail": None}):
            cur, _ = watch.step(None, fetch=True)
        self.assertEqual(calls, [[("o/web", 148), ("o/web", 300), ("o/web", 452)]])
        self.assertEqual(search.call_args.args[0], ["o/web"])
        self.assertEqual(cur["workers"]["abc-452"]["pr"]["move"]["label"], "awaiting review · alex")
        self.assertEqual(sorted(cur["prs"]), ["o/web#148", "o/web#300"])  # #452 is its live worker's row
        stacked, retired = cur["prs"]["o/web#148"], cur["prs"]["o/web#300"]
        self.assertEqual((stacked["owner"], stacked["slug"], stacked["move"]["label"]),
                         (None, "o/web", "awaiting review · alex"))
        self.assertEqual((retired["owner"], retired["slug"], retired["move"]["label"], retired["move"]["needs"]),
                         ("abc-300", "o/web", "approved · yours to merge", True))
        self.assertEqual(cur["review_requests"], mine)
        self.assertEqual(watch.board(cur)["prs"], ["#77", "#300"])
        dashstate.save_watch(cur, now=1.0)
        saved = dashstate.load_watch()
        self.assertEqual((sorted(saved["prs"]), saved["review_requests"]), (["o/web#148", "o/web#300"], mine))

    def test_between_fetches_the_last_followed_prs_carry_over(self):
        prev = {"workers": {}, "mains": {}, "stacks": {}, "prs": {"o/web#1": _reviewed(1)},
                "review_requests": [{"repo": "o/web", "number": 2, "title": "t"}]}
        with mock.patch.object(watch.pr, "batch") as batch, mock.patch.object(watch.pr, "review_requested") as search, \
                mock.patch.object(watch.spawn, "deliver_pending", return_value=[]):
            cur, _ = watch.step(prev, fetch=False, with_pr=False)
        self.assertEqual((cur["prs"], cur["review_requests"]), (prev["prs"], prev["review_requests"]))
        self.assertEqual((batch.call_count, search.call_count), (0, 0))

    def test_a_review_request_athena_already_follows_gets_no_second_row(self):
        watch.set_stack("o/web", [148])
        with mock.patch.object(watch.pr, "batch", return_value={"o/web#148": _reviewed(148)}), \
                mock.patch.object(watch.pr, "review_requested", return_value=[
                    {"repo": "o/web", "number": 148, "title": "t", "author": "", "url": "u"}]), \
                mock.patch.object(watch.spawn, "deliver_pending", return_value=[]):
            cur, _ = watch.step(None, fetch=True, with_pr=False)
        self.assertEqual(cur["review_requests"], [])


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
        board = {"needs": [], "prs": [], "active": [["working", ["a"]]], "done": [], "links": {}, "repos": {}}
        self.assertEqual(rows, [{"line": "watching: a=working", "wake": False}, {"status": "a working", "board": board}])
        rows, last = self._emit([], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [])
        snap = {"workers": {"a": _w("done", report={"status": "DONE"})}}
        rows, _ = self._emit(["a state working->done", "a report DONE"], snap, tagged=True, last_status=last)
        self.assertEqual(rows, [{"line": "a state working->done", "wake": False}, {"line": "a report DONE", "wake": True},
                                {"status": "a done", "board": {"needs": [], "prs": [], "active": [], "done": ["a"],
                                                                "links": {}, "repos": {}}}])

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

    def test_set_refuses_a_repo_that_is_not_owner_name(self):
        from athena_lib import cli
        for bad in ("o/r\n", "evil.com/o/r", "o", "o/r?x"):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(["stack", "set", bad, "1"]), 2)
        self.assertEqual(watch.stacks(), [])

    def test_watch_runs_without_force_hyperlink(self):
        # HQ's settings set it for Claude Code; the watch and the gh, git and herdr it runs must not print OSC 8.
        from athena_lib import cli
        seen = {}
        with mock.patch.dict("os.environ", {"FORCE_HYPERLINK": "1"}), \
                mock.patch.object(watch, "run", side_effect=lambda **kw: seen.update(env="FORCE_HYPERLINK" in __import__("os").environ)):
            cli.main(["watch", "--tagged"])
        self.assertEqual(seen, {"env": False})

    def test_set_show_clear(self):
        from athena_lib import cli
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["stack", "set", "o/web", "41", "42"]), 0)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["stack"])
        self.assertEqual(json.loads(out.getvalue()), [{"repo": "o/web", "prs": [41, 42]}])
        with redirect_stdout(io.StringIO()):
            cli.main(["stack", "clear", "o/web"])
        self.assertEqual(watch.stacks(), [])

    def test_watch_takes_tagged(self):
        from athena_lib import cli
        self.assertTrue(cli.parser().parse_args(["watch", "--tagged"]).tagged)


if __name__ == "__main__":
    unittest.main()
