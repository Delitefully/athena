"""Posting ready PRs for review, automatically: config-driven, at most once per PR, the bottom of a stack only, and
nothing at all when no channel is configured. The poster here is a fake: no test ever reaches Slack."""
import json
import tempfile
import unittest
from unittest import mock

from tests import helpers
from athena_lib import config, ledger, paths, reviewpost, slack, watch

HEAD = "4f2c1ab9d0e3aa"
CHANNEL = "C0123ABCDE"


def _pr(number=7, **kw):
    """A live worker's PR, ready for review: open, green, not a draft, a review required and none given."""
    return {"number": number, "url": f"https://github.com/o/web/pull/{number}", "title": "ABC-7: a change",
            "state": "OPEN", "draft": False, "review": "REVIEW_REQUIRED", "checks": "success", "base": "main",
            "branch": f"you/abc-{number}", "head": HEAD, "requested": [], "reviews": {}, **kw}


def _snap(**workers):
    return {"workers": {name: {"state": "done", "pr": p, "slug": "o/web"} for name, p in workers.items()},
            "repos": {name: "/src/web" for name in workers}}


class FakePoster:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, worker, key, p, reminder):
        self.calls.append((worker, key, p["number"], reminder))
        if self.fail:
            raise RuntimeError("the channel refused it")
        return f"{worker} post for review #{p['number']}"


class ReviewPostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        config.write({**config.DEFAULTS, "open_prs_channel": CHANNEL})
        ledger.append("spawn", "abc-7", repo="/src/web", branch="you/abc-7")
        ledger.append("spawn", "abc-8", repo="/src/web", branch="you/abc-8")
        self.passed("abc-7")
        self.now = 1_000_000.0

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def passed(self, name, head=HEAD, verdict="pass"):
        ledger.review(name, verdict, head)

    def run_post(self, snap, poster, now=None):
        with mock.patch.object(reviewpost.gitops, "default_branch", return_value="main"):
            return reviewpost.run(snap, poster, now=self.now if now is None else now)

    def test_a_ready_pr_is_posted_once(self):
        poster = FakePoster()
        lines = self.run_post(_snap(**{"abc-7": _pr()}), poster)
        self.assertEqual(lines, ["abc-7 post for review #7"])
        self.assertEqual(poster.calls, [("abc-7", "o/web#7", 7, False)])
        self.assertEqual(self.run_post(_snap(**{"abc-7": _pr()}), poster, now=self.now + 60), [])
        self.assertEqual(len(poster.calls), 1)

    def test_at_most_once_across_a_restarted_watch(self):
        self.run_post(_snap(**{"abc-7": _pr()}), FakePoster())
        later = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr()}), later, now=self.now + 60)
        self.assertEqual(later.calls, [])

    def test_nothing_without_a_configured_channel(self):
        for value in (None, "", "not a channel id"):
            config.write({**config.DEFAULTS, "open_prs_channel": value})
            poster = FakePoster()
            self.assertEqual(self.run_post(_snap(**{"abc-7": _pr()}), poster), [])
            self.assertEqual(poster.calls, [])
        self.assertFalse((paths.state_dir() / reviewpost.STATE_FILE).exists())

    def test_not_ready_is_not_posted(self):
        cases = {
            "draft": _pr(draft=True),
            "checks running": _pr(checks="pending"),
            "checks failing": _pr(checks="failure"),
            "approved": _pr(review="APPROVED"),
            "changes requested": _pr(review="CHANGES_REQUESTED"),
            "closed": _pr(state="MERGED"),
            "head moved since the review": _pr(head="9999999aaaaaaa"),
        }
        for why, p in cases.items():
            poster = FakePoster()
            self.assertEqual(self.run_post(_snap(**{"abc-7": p}), poster), [], why)
            self.assertEqual(poster.calls, [], why)

    def test_no_review_pass_at_its_head_is_not_posted(self):
        poster = FakePoster()
        self.assertEqual(self.run_post(_snap(**{"abc-8": _pr(8)}), poster), [])  # never reviewed
        self.passed("abc-8", verdict="fail")
        self.assertEqual(self.run_post(_snap(**{"abc-8": _pr(8)}), poster), [])
        self.passed("abc-8", verdict="start")
        self.assertEqual(self.run_post(_snap(**{"abc-8": _pr(8)}), poster), [])
        self.assertEqual(poster.calls, [])

    def test_a_reviewer_already_requested_is_still_posted(self):
        poster = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr(requested=["web-team"])}), poster)
        self.assertEqual(len(poster.calls), 1)

    def test_only_the_bottom_of_a_stack(self):
        self.passed("abc-8")
        top = _pr(8, base="you/abc-7")  # stacked on abc-7's branch
        poster = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr(), "abc-8": top}), poster)
        self.assertEqual([c[1] for c in poster.calls], ["o/web#7"])

    def test_later_in_a_registered_chain_is_not_the_bottom(self):
        self.passed("abc-8")
        watch.set_stack("o/web", [7, 8])
        poster = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr(), "abc-8": _pr(8)}), poster)  # both on main, but 8 is second
        self.assertEqual([c[1] for c in poster.calls], ["o/web#7"])

    def test_once_the_bottom_merged_the_next_in_the_chain_is_the_bottom(self):
        self.passed("abc-8")
        watch.set_stack("o/web", [7, 8])
        snap = _snap(**{"abc-8": _pr(8)})  # retargeted to main after #7 merged
        snap["stacks"] = {"o/web#7": "MERGED base=main UNKNOWN", "o/web#8": "OPEN base=main MERGEABLE"}
        poster = FakePoster()
        self.run_post(snap, poster)
        self.assertEqual([c[1] for c in poster.calls], ["o/web#8"])

    def test_already_posted_is_never_posted(self):
        slack.record_posted({"o/web#7": f"{self.now - 10:.6f}"}, now=self.now)
        poster = FakePoster()
        self.assertEqual(self.run_post(_snap(**{"abc-7": _pr()}), poster), [])
        self.assertEqual(poster.calls, [])

    def test_a_post_never_recorded_comes_back_once_after_a_while(self):
        """HQ's post can fail (a refused connector call): the request rings once more, then never again."""
        poster = FakePoster()
        snap = _snap(**{"abc-7": _pr()})
        self.run_post(snap, poster)
        self.assertEqual(self.run_post(snap, poster, now=self.now + reviewpost.REMIND_AFTER - 60), [])
        lines = self.run_post(snap, poster, now=self.now + reviewpost.REMIND_AFTER + 1)
        self.assertEqual(lines, ["abc-7 post for review #7"])
        self.assertEqual(self.run_post(snap, poster, now=self.now + 10 * reviewpost.REMIND_AFTER), [])
        self.assertEqual([c[3] for c in poster.calls], [False, True])

    def test_a_post_recorded_after_the_request_ends_it(self):
        poster = FakePoster()
        snap = _snap(**{"abc-7": _pr()})
        self.run_post(snap, poster)
        slack.record_posted({"o/web#7": f"{self.now + 30:.6f}"}, now=self.now + 30)  # athena open-prs add
        self.assertEqual(self.run_post(snap, poster, now=self.now + 2 * reviewpost.REMIND_AFTER), [])
        self.assertEqual(len(poster.calls), 1)

    def test_a_pr_no_longer_ready_gets_no_reminder(self):
        poster = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr()}), poster)
        for p in (_pr(draft=True), _pr(review="APPROVED"), _pr(head="9999999aaaaaaa")):
            self.assertEqual(self.run_post(_snap(**{"abc-7": p}), poster, now=self.now + 2 * reviewpost.REMIND_AFTER), [])
        self.assertEqual(len(poster.calls), 1)
        self.run_post(_snap(**{"abc-7": _pr()}), poster, now=self.now + 3 * reviewpost.REMIND_AFTER)
        self.assertEqual([c[3] for c in poster.calls], [False, True])  # ready again: the one reminder

    def test_a_pr_gone_from_the_fleet_gets_no_reminder(self):
        poster = FakePoster()
        self.run_post(_snap(**{"abc-7": _pr()}), poster)
        self.assertEqual(self.run_post(_snap(), poster, now=self.now + 2 * reviewpost.REMIND_AFTER), [])
        self.assertEqual(len(poster.calls), 1)

    def test_a_failing_poster_never_posts_twice_in_a_row(self):
        failing = FakePoster(fail=True)
        lines = self.run_post(_snap(**{"abc-7": _pr()}), failing)
        self.assertEqual(lines, ["abc-7 post for review failed"])
        again = FakePoster()
        self.assertEqual(self.run_post(_snap(**{"abc-7": _pr()}), again, now=self.now + 60), [])
        self.assertEqual(again.calls, [])

    def test_the_real_poster_rings_hq_and_never_reaches_slack(self):
        with mock.patch.object(slack.urllib.request, "urlopen") as urlopen:
            line = reviewpost.ring_hq("abc-7", "o/web#7", _pr(), False)
        self.assertEqual(line, "abc-7 post for review #7")
        urlopen.assert_not_called()


class WatchStepTest(unittest.TestCase):
    """Through athena watch's own tick, with the stack states the watch keeps, not a snapshot built by hand."""

    setUp, tearDown, passed = ReviewPostTest.setUp, ReviewPostTest.tearDown, ReviewPostTest.passed

    def step(self, prev, workers, fetch=False, fetched=None):
        snap = {"workers": workers, "mains": {}, "repos": {name: "/src/web" for name in workers}}
        with mock.patch.object(watch.spawn, "deliver_pending", return_value=[]), \
                mock.patch.object(watch, "snapshot", return_value=snap), \
                mock.patch.object(watch, "_dash_url", return_value=None), \
                mock.patch.object(watch, "follow", side_effect=lambda cur, prev: cur.update(fetched or {})), \
                mock.patch.object(watch, "ci_failures", return_value=[]), \
                mock.patch.object(reviewpost.gitops, "default_branch", return_value="main"):
            return watch.step(prev, fetch=fetch)

    def test_after_the_bottom_merged_the_watch_posts_the_next_in_the_chain(self):
        self.passed("abc-8")
        watch.set_stack("o/web", [7, 8])
        workers = {"abc-8": {"state": "done", "pr": _pr(8), "slug": "o/web"}}  # retargeted to main
        stacks = {"o/web#7": "MERGED base=main UNKNOWN", "o/web#8": "OPEN base=main MERGEABLE"}
        _, lines = self.step({"workers": {}, "stacks": stacks}, workers)  # a tick between fetches keeps them
        self.assertIn("abc-8 post for review #8", lines)

    def test_a_fetch_tick_uses_the_states_it_just_read(self):
        self.passed("abc-8")
        watch.set_stack("o/web", [7, 8])
        workers = {"abc-8": {"state": "done", "pr": _pr(8), "slug": "o/web"}}
        _, lines = self.step({"workers": {}}, workers, fetch=True,
                             fetched={"stacks": {"o/web#7": "MERGED base=main UNKNOWN"}, "prs": {}})
        self.assertIn("abc-8 post for review #8", lines)

    def test_while_the_bottom_is_open_the_next_waits(self):
        self.passed("abc-8")
        watch.set_stack("o/web", [7, 8])
        workers = {"abc-8": {"state": "done", "pr": _pr(8), "slug": "o/web"}}
        _, lines = self.step({"workers": {}, "stacks": {"o/web#7": "OPEN base=main MERGEABLE"}}, workers)
        self.assertNotIn("abc-8 post for review #8", lines)


class WatchPostsTest(unittest.TestCase):
    def test_a_watch_tick_asks_the_lane_with_the_hq_poster(self):
        with mock.patch.object(watch.spawn, "deliver_pending", return_value=[]), \
                mock.patch.object(watch, "snapshot", return_value={"workers": {}, "mains": {}, "repos": {}}), \
                mock.patch.object(watch.reviewpost, "run", return_value=["abc-7 post for review #7"]) as run:
            _, lines = watch.step({"workers": {}}, fetch=False)
        self.assertIn("abc-7 post for review #7", lines)
        self.assertIs(run.call_args[0][1], watch.reviewpost.ring_hq)

    def test_the_line_wakes_hq(self):
        self.assertEqual(watch.classify("abc-7 post for review #7"), "wake")
        self.assertEqual(watch.classify("abc-7 post for review failed"), "wake")


if __name__ == "__main__":
    unittest.main()
