"""athena watch's quiet rules: the notices it keeps out of HQ, and the log that keeps them."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import config, ledger, paths, pr, status, watch


def _pr(**kw):
    return {"state": "OPEN", "review": "", "checks": "success", "comments": 0, "comments_all": 0, **kw}


def _snap(workers=None, repos=None):
    return {"workers": workers or {}, "repos": repos or {}, "mains": {}}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def verdict(self, line, prev=None, cur=None):
        return watch.verdict(line, ctx=watch.Context(prev or _snap(), cur or _snap()))


class BotCommentsTest(unittest.TestCase):
    def test_bots_by_login_suffix_type_and_flag(self):
        for author in ({"login": "github-actions"}, {"login": "GitHub-Advanced-Security"}, {"login": "Copilot"},
                       {"login": "copilot-pull-request-reviewer"}, {"login": "renovate[bot]"},
                       {"login": "linear-code"}, {"login": "sun-merge-queue"}, {"login": "Sun-Merge-Queue"},
                       {"login": "someapp", "type": "Bot"}, {"login": "someapp", "is_bot": True},
                       {"login": "someapp", "__typename": "Bot"}):
            with self.subTest(author=author):
                self.assertTrue(pr.is_bot(author))
        for author in ({"login": "alice"}, {"login": "copilotfan"}, {"login": "robot"}):
            with self.subTest(author=author):
                self.assertFalse(pr.is_bot(author))
        self.assertTrue(pr.is_bot({"login": "x"}, association="BOT"))
        self.assertFalse(pr.is_bot({"login": "x"}, association="NONE"))  # an outside human is NONE too

    def test_reduce_counts_only_humans_who_are_not_the_author(self):
        data = {"author": {"login": "me", "is_bot": False},
                "comments": [{"author": {"login": "github-actions"}, "authorAssociation": "NONE"},
                             {"author": {"login": "me"}, "authorAssociation": "OWNER"},
                             {"author": {"login": "alice"}, "authorAssociation": "MEMBER"},
                             {"author": {"login": "renovate[bot]"}}],
                "reviews": [{"author": {"login": "copilot-pull-request-reviewer"}, "body": "nit"},
                            {"author": {"login": "bob"}, "body": "looks off"},
                            {"author": {"login": "carol"}, "body": ""}]}
        r = pr.reduce(data)
        self.assertEqual((r["comments"], r["comments_all"], r["author"]), (2, 6, "me"))

    def test_platform_bots_never_count(self):
        data = {"author": {"login": "me"},
                "comments": [{"author": {"login": "linear-code"}, "authorAssociation": "NONE"},
                             {"author": {"login": "sun-merge-queue"}, "authorAssociation": "MEMBER"}]}
        self.assertEqual((pr.reduce(data)["comments"], pr.reduce(data)["comments_all"]), (0, 2))


class CommentsRuleTest(Isolated):
    def test_a_bot_only_burst_is_logged_not_told(self):
        prev = _snap({"a": {"state": "idle", "pr": _pr(comments=1, comments_all=1)}})
        cur = _snap({"a": {"state": "idle", "pr": _pr(comments=1, comments_all=4)}})
        lines = watch.diff(prev, cur)
        self.assertEqual(lines, ["a review comments +3"])
        self.assertEqual(self.verdict(lines[0], prev, cur), ("quiet", "bot or author comments only"))

    def test_a_mixed_burst_tells_the_human_count(self):
        prev = _snap({"a": {"state": "idle", "pr": _pr(comments=1, comments_all=1)}})
        cur = _snap({"a": {"state": "idle", "pr": _pr(comments=3, comments_all=6)}})
        lines = watch.diff(prev, cur)
        self.assertEqual(lines, ["a review comments +2"])
        self.assertEqual(self.verdict(lines[0], prev, cur), ("wake", None))

    def test_no_comment_line_when_nothing_was_added(self):
        snap = _snap({"a": {"state": "idle", "pr": _pr(comments=2, comments_all=5)}})
        self.assertEqual(watch.diff(snap, snap), [])


class MainMovedTest(Isolated):
    """The old modes, each set in config.json, keep their words."""
    LINE = "platform main moved 1a2b3c4..4d5e6f7"

    def setUp(self):
        super().setUp()
        config.write({**config.DEFAULTS, "watch_main_moved": "active"})

    def _cur(self, **worker):
        return _snap({"a": {"state": "idle", "pr": None, "report": None, **worker}}, {"a": "/src/platform"})

    def test_no_active_worker_is_logged(self):
        self.assertEqual(self.verdict(self.LINE, cur=_snap()), ("quiet", "no active worker or open PR in platform"))
        self.assertEqual(self.verdict(self.LINE, cur=self._cur())[0], "quiet")

    def test_a_working_or_blocked_worker_is_told(self):
        for worker in ({"state": "working"}, {"state": "blocked"}, {"state": "idle", "report": {"status": "BLOCKED"}}):
            with self.subTest(worker=worker):
                self.assertEqual(self.verdict(self.LINE, cur=self._cur(**worker)), ("wake", None))

    def test_a_worker_in_another_repo_does_not_count(self):
        cur = _snap({"a": {"state": "working"}}, {"a": "/src/web"})
        self.assertEqual(self.verdict(self.LINE, cur=cur)[0], "quiet")

    def test_an_unapproved_open_pr_is_told_an_approved_one_is_not(self):
        self.assertEqual(self.verdict(self.LINE, cur=self._cur(state="done", pr=_pr(review="REVIEW_REQUIRED")))[0], "wake")
        self.assertEqual(self.verdict(self.LINE, cur=self._cur(state="done", pr=_pr(review="APPROVED")))[0], "quiet")
        self.assertEqual(self.verdict(self.LINE, cur=self._cur(state="done", pr=_pr(state="MERGED")))[0], "quiet")

    def test_configurable(self):
        config.write({**config.DEFAULTS, "watch_main_moved": "always"})
        self.assertEqual(self.verdict(self.LINE, cur=_snap()), ("wake", None))
        config.write({**config.DEFAULTS, "watch_main_moved": "never"})
        self.assertEqual(self.verdict(self.LINE, cur=self._cur(state="working")), ("quiet", "watch_main_moved is never"))
        config.write({**config.DEFAULTS, "watch_main_moved": "sometimes"})  # unknown: the default, conflicts
        self.assertEqual(self.verdict(self.LINE, cur=self._cur(state="working")),
                         ("quiet", "watch_main_moved is conflicts"))

    def test_active_is_still_written_down_now_that_it_is_not_the_default(self):
        self.assertEqual(json.loads(config.config_file().read_text())["watch_main_moved"], "active")

    def test_snapshot_records_each_workers_repo_but_watch_json_does_not(self):
        ledger.append("spawn", "a", repo="/src/platform")
        ledger.append("spawn", "b")
        snap = watch.snapshot(with_pr=False)
        self.assertEqual(snap["repos"], {"a": "/src/platform"})
        from athena_lib import dashstate
        dashstate.save_watch(snap)
        self.assertNotIn("repos", json.loads((paths.state_dir() / "watch.json").read_text()))


class ConflictsModeTest(Isolated):
    """The default: `main moved` is logged whatever the repo's workers do; HQ hears `pr conflicts` instead."""
    LINE = "platform main moved 1a2b3c4..4d5e6f7"

    def test_the_default_is_conflicts_and_written_only_when_set(self):
        self.assertEqual(config.DEFAULTS["watch_main_moved"], "conflicts")
        config.write(dict(config.DEFAULTS))
        self.assertNotIn("watch_main_moved", json.loads(config.config_file().read_text()))
        self.assertEqual(config.get("watch_main_moved"), "conflicts")

    def test_a_config_file_without_the_key_picks_up_the_default(self):
        paths.ensure()
        config.config_file().write_text(json.dumps({"version": 1, "projects_dir": "~"}))
        self.assertEqual(config.get("watch_main_moved"), "conflicts")
        self.assertEqual(config.source("watch_main_moved"), "default")

    def test_main_moved_is_logged_with_its_reason_even_with_an_active_worker(self):
        cur = _snap({"a": {"state": "working", "pr": _pr(review="REVIEW_REQUIRED")}}, {"a": "/src/platform"})
        for snap in (_snap(), cur):
            with self.subTest(snap=snap):
                self.assertEqual(self.verdict(self.LINE, cur=snap), ("quiet", "watch_main_moved is conflicts"))

    def test_main_moved_under_conflicts_goes_to_the_log_not_hq(self):
        cur = _snap({"a": {"state": "working", "pr": _pr()}}, {"a": "/src/platform"})
        out = io.StringIO()
        with redirect_stdout(out):
            watch.emit([self.LINE], cur, tagged=False, prev=cur)
        self.assertEqual(out.getvalue(), "")
        log = (paths.state_dir() / "logs" / "watch-suppressed.log").read_text()
        self.assertIn(" platform main moved 1a2b3c4..4d5e6f7 (watch_main_moved is conflicts)", log)


def _open(n=7, mergeable="MERGEABLE", **kw):
    return _pr(number=n, url=f"https://github.com/o/platform/pull/{n}", mergeable=mergeable, **kw)


class PrConflictsTest(Isolated):
    """`<w> pr conflicts #<n>`: a live worker's open PR turning CONFLICTING, told once per turn."""

    def _ticks(self, *values, n=7):
        """Each value is what gh says of the worker's PR's mergeable on one tick; the lines of every tick after the
        first, as athena watch would print them."""
        ledger.append("spawn", "a")
        prev, lines = None, []
        for value in values:
            p = None if value is None else _open(n, value) if isinstance(value, str) else value
            with mock.patch.object(status, "collect", return_value={
                    "state": "idle", "pr": p, "report": None, "detail": None}):
                cur = watch.snapshot(prev=prev)
            if prev is not None:
                lines += [line for line in watch.diff(prev, cur) if " pr " in line]
            prev = cur
        return lines

    def test_mergeable_to_conflicting_wakes_once(self):
        lines = self._ticks("MERGEABLE", "CONFLICTING", "CONFLICTING", "CONFLICTING")
        self.assertEqual(lines, ["a pr conflicts #7"])
        self.assertEqual(self.verdict(lines[0]), ("wake", None))
        self.assertEqual(watch.classify(lines[0]), "wake")

    def test_unknown_in_between_does_not_flap(self):
        self.assertEqual(self._ticks("MERGEABLE", "UNKNOWN", "UNKNOWN", "MERGEABLE"), [])
        self.assertEqual(self._ticks("CONFLICTING", "UNKNOWN", "CONFLICTING", "UNKNOWN", "CONFLICTING"), [])
        self.assertEqual(self._ticks("MERGEABLE", "UNKNOWN", "CONFLICTING", "UNKNOWN", "CONFLICTING"),
                         ["a pr conflicts #7"])

    def test_unknown_then_conflicting_is_a_turn(self):
        self.assertEqual(self._ticks("UNKNOWN", "CONFLICTING"), ["a pr conflicts #7"])

    def test_conflicting_to_mergeable_is_status_only(self):
        lines = self._ticks("MERGEABLE", "CONFLICTING", "UNKNOWN", "MERGEABLE")
        self.assertEqual(lines, ["a pr conflicts #7", "a pr mergeable #7"])
        self.assertEqual(self.verdict("a pr mergeable #7"), ("status", None))
        self.assertEqual(watch.classify("a pr mergeable #7"), "status")

    def test_a_second_conflict_after_a_fix_is_told_again(self):
        self.assertEqual(self._ticks("MERGEABLE", "CONFLICTING", "MERGEABLE", "CONFLICTING"),
                         ["a pr conflicts #7", "a pr mergeable #7", "a pr conflicts #7"])

    def test_a_pr_gh_cannot_read_keeps_its_last_value(self):
        self.assertEqual(self._ticks("CONFLICTING", None, "CONFLICTING"), [])

    def test_only_an_open_pr_conflicts(self):
        self.assertEqual(self._ticks("MERGEABLE", _open(mergeable="CONFLICTING", state="CLOSED")), ["a pr closed"])

    def test_a_new_pr_does_not_inherit_the_old_ones_value(self):
        lines = self._ticks(_open(7, "CONFLICTING"), _open(8, "UNKNOWN"), _open(8, "CONFLICTING"))
        self.assertEqual(lines, ["a pr conflicts #8"])

    def test_every_mode_tells_a_conflict(self):
        for mode in ("conflicts", "active", "always", "never"):
            with self.subTest(mode=mode):
                config.write({**config.DEFAULTS, "watch_main_moved": mode})
                self.assertEqual(self.verdict("a pr conflicts #7"), ("wake", None))

    def test_a_retired_workers_pr_is_ignored(self):
        ledger.append("spawn", "a")
        ledger.append("spawn", "b")
        before = {"state": "idle", "pr": None, "report": None, "detail": None}
        with mock.patch.object(status, "collect", return_value={**before, "pr": _open(7, "MERGEABLE")}):
            prev = watch.snapshot()
        ledger.append("retire", "a")
        with mock.patch.object(status, "collect", return_value={**before, "pr": _open(7, "CONFLICTING")}):
            cur = watch.snapshot(prev=prev)
        self.assertNotIn("a", cur["workers"])
        self.assertEqual([line for line in watch.diff(prev, cur) if line.startswith("a ")], ["a gone from the ledger"])
        self.assertIn("b pr conflicts #7", watch.diff(prev, cur))

    def test_a_followed_pr_of_a_retired_worker_says_nothing(self):
        prev = {"workers": {}, "prs": {"o/platform#7": _open(7, "MERGEABLE")}}
        cur = {"workers": {}, "prs": {"o/platform#7": _open(7, "CONFLICTING")}}
        self.assertEqual(watch.diff(prev, cur), [])

    def test_a_worker_being_retired_is_logged(self):
        ledger.append("spawn", "a")
        ledger.append("update", "a", retiring=time.time())
        self.assertEqual(self.verdict("a pr conflicts #7"), ("quiet", "retired via athena retire"))


class RetiredExitTest(Isolated):
    def test_an_exit_while_retiring_is_logged(self):
        ledger.append("spawn", "a")
        ledger.append("update", "a", retiring=time.time())
        self.assertEqual(self.verdict("a state done->exited"), ("quiet", "retired via athena retire"))
        self.assertEqual(self.verdict("a state done->exited (gone)")[0], "quiet")

    def test_an_exit_nobody_asked_for_is_told(self):
        ledger.append("spawn", "a")
        self.assertEqual(self.verdict("a state working->exited"), ("wake", None))

    def test_a_failed_retire_or_an_old_mark_no_longer_hides_an_exit(self):
        ledger.append("spawn", "a")
        ledger.append("update", "a", retiring=time.time())
        ledger.append("update", "a", retiring=None, cleanup_error="claude did not exit")
        self.assertEqual(self.verdict("a state done->exited")[0], "wake")
        ledger.append("spawn", "b")
        ledger.append("update", "b", retiring=time.time() - 2 * 3600)
        self.assertEqual(self.verdict("b state done->exited")[0], "wake")

    def test_gone_from_the_ledger_after_retire_is_logged(self):
        ledger.append("spawn", "a")
        ledger.append("retire", "a")
        ledger.append("spawn", "b")
        self.assertEqual(self.verdict("a gone from the ledger"), ("quiet", "retired via athena retire"))
        self.assertEqual(self.verdict("b gone from the ledger")[0], "wake")


class DuplicateReportTest(Isolated):
    def _ticks(self, last, now):
        prev = _snap({"a": {"state": "working", "report": None, "last_report": last}})
        cur = _snap({"a": {"state": "done", "report": now, "last_report": now}})
        return prev, cur, watch.diff(prev, cur)

    def test_the_same_report_again_is_logged(self):
        done = {"status": "DONE", "head": "abc1234"}
        prev, cur, lines = self._ticks(done, dict(done))
        self.assertIn("a report DONE", lines)
        self.assertEqual(self.verdict("a report DONE", prev, cur), ("quiet", "same report as last told"))

    def test_a_done_at_a_new_head_is_told(self):
        prev, cur, lines = self._ticks({"status": "DONE", "head": "abc1234"}, {"status": "DONE", "head": "def5678"})
        self.assertEqual(self.verdict("a report DONE", prev, cur), ("wake", None))

    def test_a_new_head_with_no_clear_between_ticks_is_a_line(self):
        prev = _snap({"a": {"state": "done", "report": {"status": "DONE", "head": "abc1234"}}})
        cur = _snap({"a": {"state": "done", "report": {"status": "DONE", "head": "def5678"}}})
        self.assertEqual(watch.diff(prev, cur), ["a report DONE"])
        self.assertEqual(self.verdict("a report DONE", prev, cur)[0], "wake")
        self.assertEqual(watch.diff(prev, prev), [])

    def test_another_status_is_told(self):
        prev, cur, _ = self._ticks({"status": "DONE", "head": "abc1234"},
                                   {"status": "DONE_WITH_CONCERNS", "head": "abc1234"})
        self.assertEqual(self.verdict("a report DONE_WITH_CONCERNS", prev, cur)[0], "wake")

    def test_blocked_and_needs_context_are_always_told(self):
        for status in ("BLOCKED", "NEEDS_CONTEXT"):
            with self.subTest(status=status):
                report = {"status": status, "head": "abc1234"}
                prev, cur, _ = self._ticks(report, dict(report))
                self.assertEqual(self.verdict(f"a report {status}", prev, cur), ("wake", None))


class AlwaysToldTest(Isolated):
    def test_what_hq_acts_on_is_never_quiet(self):
        prev = _snap({"a": {"state": "working", "pr": _pr(checks="pending")}})
        cur = _snap({"a": {"state": "blocked", "pr": _pr(checks="failure", review="CHANGES_REQUESTED")}})
        for line in ("a state working->blocked", "a report BLOCKED", "a report NEEDS_CONTEXT", "a report DONE",
                     "a checks pending->failure", "a review CHANGES_REQUESTED", "a pr opened https://github.com/o/r/pull/7",
                     "web main ci failed CI 123", "slack-new D0123ABCD 2 dm", "dash-queue 4", "watch error: boom"):
            with self.subTest(line=line):
                self.assertEqual(self.verdict(line, prev, cur), ("wake", None))

    def test_a_dm_the_human_is_active_in_is_quiet_with_or_without_a_context(self):
        self.assertEqual(watch.verdict("slack-new D0123ABCD 2 dm active"), ("quiet", "human active in DM"))
        self.assertEqual(self.verdict("slack-new D0123ABCD 2 dm active", _snap(), _snap()),
                         ("quiet", "human active in DM"))
        self.assertEqual(watch.verdict("slack-new C0123ABCD 2 active"), ("wake", None))  # only a DM is held

    def test_classify_keeps_its_words(self):
        ctx = watch.Context(_snap(), _snap())
        self.assertEqual(watch.classify("platform main moved a..b", ctx=ctx), "quiet")
        self.assertEqual(watch.classify("platform main moved a..b"), "wake")  # no context: nothing is hidden
        self.assertEqual(watch.classify("a checks none->pending", ctx=ctx), "status")


class EmitTest(Isolated):
    def _log(self):
        try:
            return (paths.state_dir() / "logs" / "watch-suppressed.log").read_text().splitlines()
        except FileNotFoundError:
            return []

    def _emit(self, lines, cur, prev, tagged):
        out = io.StringIO()
        with redirect_stdout(out):
            watch.emit(lines, cur, tagged=tagged, prev=prev)
        return out.getvalue().splitlines()

    def test_quiet_lines_reach_neither_hq_nor_the_band_only_the_log(self):
        prev = _snap({"a": {"state": "idle", "pr": _pr(comments=0, comments_all=0)}})
        cur = _snap({"a": {"state": "idle", "pr": _pr(comments=0, comments_all=2)}})
        lines = ["platform main moved 1a2b3c4..4d5e6f7", "a review comments +2", "a checks pending->failure"]
        rows = [json.loads(r) for r in self._emit(lines, cur, prev, tagged=True)]
        self.assertEqual([r for r in rows if "line" in r], [{"line": "a checks pending->failure", "wake": True}])
        self.assertEqual(self._emit(lines, cur, prev, tagged=False), ["a checks pending->failure"])
        log = self._log()
        self.assertEqual(len(log), 4)  # two quiet lines, once per emit
        self.assertRegex(log[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d platform main moved 1a2b3c4\.\.4d5e6f7 "
                                 r"\(watch_main_moved is conflicts\)$")
        self.assertTrue(log[1].endswith(" a review comments +2 (bot or author comments only)"))

    def test_the_log_rotates_at_its_cap_and_keeps_one(self):
        logs = paths.ensure("logs")
        (logs / "watch-suppressed.log").write_text("x" * watch.SUPPRESSED_MAX)
        (logs / "watch-suppressed.log.1").write_text("oldest\n")
        watch.suppress("a report DONE", "same report as last told")
        self.assertEqual((logs / "watch-suppressed.log.1").read_text(), "x" * watch.SUPPRESSED_MAX)
        self.assertEqual(len(self._log()), 1)
        self.assertEqual(sorted(os.listdir(logs)), ["watch-suppressed.log", "watch-suppressed.log.1"])

    def test_one_line_per_notice_whatever_it_holds(self):
        watch.suppress("a  odd\nline\r", "why")
        self.assertEqual(len(self._log()), 1)
        self.assertTrue(self._log()[0].endswith(" a odd line (why)"))

    def test_an_unwritable_log_never_stops_the_watch(self):
        paths.ensure()
        (paths.state_dir() / "logs").write_text("not a directory")
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            watch.suppress("a report DONE", "same report as last told")
        self.assertIn("watch-suppressed.log", err.getvalue())


if __name__ == "__main__":
    unittest.main()
