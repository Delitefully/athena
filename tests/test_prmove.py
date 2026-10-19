"""Whose move a PR waits on: the review data `athena watch` fetches, and the one label and section per PR."""
import json
import unittest
from pathlib import Path
from unittest import mock

from athena_lib import pr


HEAD = "8d1c2b3a4f5e"
PASSED = {"verdict": "pass", "head": HEAD}  # HQ's review passed at the PR's head


def _pr(**kw):
    base = {"state": "OPEN", "draft": False, "review": "REVIEW_REQUIRED", "checks": "success", "requested": [],
            "reviews": {}, "auto_merge": False, "queued": False, "head": HEAD}
    return {**base, **kw}


class ReduceTest(unittest.TestCase):
    def test_review_requests_latest_reviews_auto_merge_and_queue(self):
        data = {"number": 907, "url": "https://github.com/acme/web/pull/907", "state": "OPEN", "isDraft": False,
                "title": "Search keeps filters", "reviewDecision": "REVIEW_REQUIRED",
                "reviewRequests": [{"__typename": "User", "login": "alex", "name": ""},
                                   {"__typename": "User", "login": "sam", "name": "Sam Lee"},
                                   {"__typename": "Team", "slug": "web-core", "name": "Web Core"},
                                   {"__typename": "Team", "slug": "infra", "name": ""}],
                "latestReviews": [{"author": {"login": "kim"}, "state": "COMMENTED"},
                                  {"author": {"login": "bo"}, "state": "CHANGES_REQUESTED"}],
                "autoMergeRequest": {"enabledAt": "2026-10-11T10:00:00Z"}, "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [{"name": "build", "conclusion": "SUCCESS"},
                                      {"__typename": "StatusContext", "context": "merge-queue", "state": "PENDING"}]}
        r = pr.reduce(data)
        self.assertEqual(r["requested"], ["alex", "Sam Lee", "Web Core", "infra"])
        self.assertEqual(r["reviews"], {"kim": "COMMENTED", "bo": "CHANGES_REQUESTED"})
        self.assertEqual((r["auto_merge"], r["queued"], r["merge_state"], r["title"]), (True, True, "BLOCKED", "Search keeps filters"))

    def test_no_queue_without_a_pending_merge_queue_check(self):
        for rollup in ([{"name": "merge-queue", "conclusion": "FAILURE"}], [{"name": "build", "status": "IN_PROGRESS"}], []):
            self.assertFalse(pr.reduce({"statusCheckRollup": rollup})["queued"], rollup)
        self.assertTrue(pr.reduce({"isInMergeQueue": True})["queued"])
        self.assertTrue(pr.reduce({"statusCheckRollup": [{"name": "Merge Queue", "status": "QUEUED"}]})["queued"])

    def test_auto_merge_off_and_no_requests(self):
        r = pr.reduce({"autoMergeRequest": None, "reviewRequests": [], "latestReviews": None})
        self.assertEqual((r["auto_merge"], r["requested"], r["reviews"]), (False, [], {}))


class MoveTest(unittest.TestCase):
    """The table: whose move it is, its label, and whether its row is in Needs you."""

    def check(self, p, live, human_merge, label, needs, review=PASSED):
        m = pr.move(p, live=live, human_merge=human_merge, review=review)
        self.assertEqual((m["label"], m["needs"]), (label, needs), p)

    def test_a_teammate_reviews(self):
        self.check(_pr(requested=["alex"]), True, False, "awaiting review · alex", False)
        self.check(_pr(requested=["alex"], auto_merge=True), False, False, "awaiting review · alex", False)
        self.check(_pr(requested=["alex", "Sam Lee", "Web Core", "kim"]), True, False,
                   "awaiting review · alex, Sam Lee, Web Core +1", False)
        self.check(_pr(review="", requested=["alex"]), True, False, "awaiting review · alex", False)

    def test_it_says_who_merges(self):
        # The dashboard offers Merge (a go for athena) only where athena merges: never in human_merge_repos.
        for human_merge in (True, False):
            for p in (_pr(review="APPROVED"), _pr(review=""), _pr(), _pr(checks="failure")):
                self.assertIs(pr.move(p, live=False, human_merge=human_merge)["human_merge"], human_merge, p)

    def test_nobody_yet(self):
        self.check(_pr(), True, False, "no reviewer requested", True)
        self.check(_pr(auto_merge=True), False, False, "no reviewer requested", True)

    def test_a_repo_that_requires_no_review(self):
        # reviewDecision empty: branch protection has no review rule, so a green PR is the human's to merge.
        self.check(_pr(review=""), True, False, "your go to merge", True)
        self.check(_pr(review=""), False, True, "yours to merge", True)
        self.check(_pr(review="", auto_merge=True), True, False, "auto-merge on", False)
        self.check(_pr(review="", queued=True), True, True, "in merge queue", False)
        self.check(_pr(review="", checks="failure"), True, False, "checks failing", False)
        # Its worker still at work keeps the row (`working`): not a move that wins over working.
        self.assertEqual(pr.move(_pr(review=""), live=True, human_merge=False, review=PASSED)["kind"], "mergeable")

    def test_a_merge_waits_for_green_checks(self):
        # Nothing is the human's to merge while CI still runs: "checks running", In progress.
        self.check(_pr(review="", checks="pending"), True, False, "checks running", False)
        self.check(_pr(review="", checks="pending"), False, True, "checks running", False)
        self.check(_pr(review="APPROVED", checks="pending"), True, True, "checks running", False)
        self.check(_pr(review="APPROVED", checks="pending"), False, False, "checks running", False)
        # Auto-merge or the queue merges once checks pass: still nobody's move.
        self.check(_pr(review="APPROVED", checks="pending", auto_merge=True), True, False, "approved · auto-merge on", False)
        self.check(_pr(review="", checks="pending", queued=True), True, False, "in merge queue", False)
        # No checks yet: a repo with no CI has nothing to wait for; in one with CI (or not known to have none),
        # the workflows have not registered yet after a push.
        for ci in (True, None):
            self.assertEqual(pr.move(_pr(review="", checks="none"), live=False, human_merge=False, ci=ci)["label"],
                             "checks starting")
        self.assertEqual(pr.move(_pr(review="APPROVED", checks="none"), live=False, human_merge=True)["label"],
                         "checks starting")
        m = pr.move(_pr(review="", checks="none"), live=True, human_merge=False, ci=False, review=PASSED)
        self.assertEqual((m["label"], m["needs"]), ("your go to merge", True))
        m = pr.move(_pr(review="APPROVED", checks="none"), live=False, human_merge=True, ci=False)
        self.assertEqual((m["label"], m["needs"]), ("approved · yours to merge", True))
        # A review still to come waits on the reviewer whatever CI does.
        self.check(_pr(checks="pending", requested=["alex"]), True, False, "awaiting review · alex", False)
        self.assertEqual(pr.move(_pr(review="", checks="pending"), live=True, human_merge=False)["kind"], "pending")

    def test_approved(self):
        self.check(_pr(review="APPROVED"), True, True, "approved · yours to merge", True)
        self.check(_pr(review="APPROVED", queued=True), True, True, "approved · in merge queue", False)
        self.check(_pr(review="APPROVED", auto_merge=True), False, True, "approved · auto-merge on", False)
        self.check(_pr(review="APPROVED", queued=True, auto_merge=True), True, False, "approved · in merge queue", False)
        # Not a repo the human merges: athena merges once the human says go.
        self.check(_pr(review="APPROVED"), True, False, "approved · your go to merge", True)

    def test_changes_requested(self):
        self.check(_pr(review="CHANGES_REQUESTED", reviews={"alex": "CHANGES_REQUESTED"}), True, False,
                   "changes requested · worker fixing", False)
        self.check(_pr(review="CHANGES_REQUESTED", reviews={"alex": "CHANGES_REQUESTED"}), False, False,
                   "changes requested", True)
        # Every reviewer who asked for changes is asked again: their move.
        self.check(_pr(review="CHANGES_REQUESTED", reviews={"alex": "CHANGES_REQUESTED", "kim": "APPROVED"},
                       requested=["alex"]), True, False, "awaiting review · alex", False)
        # Someone else is asked, but alex's changes still stand: the worker's move.
        self.check(_pr(review="CHANGES_REQUESTED", reviews={"alex": "CHANGES_REQUESTED"}, requested=["sam"]),
                   True, False, "changes requested · worker fixing", False)

    def test_checks_failing(self):
        self.check(_pr(checks="failure", requested=["alex"]), True, False, "checks failing", False)
        self.check(_pr(checks="failure", review="APPROVED", auto_merge=True), False, False, "checks failing", True)

    def test_drafts_and_closed(self):
        self.check(_pr(draft=True), False, False, "draft", False)
        self.assertIsNone(pr.move(_pr(state="MERGED"), live=False, human_merge=False))
        self.assertIsNone(pr.move(_pr(state="CLOSED"), live=True, human_merge=False))

    def test_no_review_data_makes_no_move(self):
        # watch.json from before review data was fetched, or a bare PR from a report url: no guess.
        self.assertIsNone(pr.move({"state": "OPEN", "draft": False, "review": "", "checks": "success"}, live=True,
                                  human_merge=False))

    def test_kinds(self):
        self.assertEqual(pr.move(_pr(requested=["a"]), live=True, human_merge=False)["kind"], "awaiting")
        self.assertEqual(pr.move(_pr(review="APPROVED"), live=True, human_merge=True, review=PASSED)["kind"], "approved")
        self.assertEqual(pr.move(_pr(review="CHANGES_REQUESTED"), live=True, human_merge=False)["kind"], "changes")


class ReviewGateTest(unittest.TestCase):
    """A live worker's PR is the human's to merge only once HQ's review passed at the PR's current head."""

    def move(self, p, review, live=True, human_merge=False):
        m = pr.move(p, live=live, human_merge=human_merge, review=review)
        return m["label"], m["kind"], m["needs"]

    def test_done_with_no_verdict_is_in_review(self):
        self.assertEqual(self.move(_pr(review=""), None), ("in review · athena", "reviewing", False))
        self.assertEqual(self.move(_pr(review=""), {}), ("in review · athena", "reviewing", False))
        self.assertEqual(self.move(_pr(review=""), {"verdict": "start", "head": HEAD}),
                         ("in review · athena", "reviewing", False))

    def test_a_pass_at_the_current_head_is_the_merge(self):
        self.assertEqual(self.move(_pr(review=""), PASSED), ("your go to merge", "mergeable", True))
        self.assertEqual(self.move(_pr(review=""), PASSED, human_merge=True), ("yours to merge", "mergeable", True))
        # A full sha against the 12 characters gh's reduce keeps, and a short one: the same commit.
        self.assertEqual(self.move(_pr(review=""), {"verdict": "pass", "head": HEAD + "0123456789abcdef0123"})[1],
                         "mergeable")
        self.assertEqual(self.move(_pr(review=""), {"verdict": "pass", "head": HEAD[:7].upper()})[1], "mergeable")

    def test_a_pass_at_an_old_head_is_in_review_again(self):
        old = {"verdict": "pass", "head": "1111111aaaaa"}
        self.assertEqual(self.move(_pr(review=""), old), ("in review · athena", "reviewing", False))
        self.assertEqual(self.move(_pr(review=""), {"verdict": "pass", "head": ""})[1], "reviewing")
        self.assertEqual(self.move(_pr(review=""), {"verdict": "pass", "head": "8d1"})[1], "reviewing")  # too short
        self.assertEqual(self.move(_pr(review="", head=""), PASSED)[1], "reviewing")  # no head to match

    def test_a_fail_is_the_workers_move(self):
        failed = {"verdict": "fail", "head": HEAD}
        self.assertEqual(self.move(_pr(review=""), failed), ("review findings · worker fixing", "changes", False))
        self.assertEqual(self.move(_pr(review="APPROVED"), failed),
                         ("review findings · worker fixing", "changes", False))
        # The worker pushed its fixes: the failed verdict is void, so the new head waits on a new review.
        self.assertEqual(self.move(_pr(review=""), {"verdict": "fail", "head": "1111111aaaaa"})[1], "reviewing")

    def test_an_approved_pr_waits_on_athenas_review_too(self):
        self.assertEqual(self.move(_pr(review="APPROVED"), None, human_merge=True),
                         ("in review · athena", "reviewing", False))
        self.assertEqual(self.move(_pr(review="APPROVED"), PASSED, human_merge=True),
                         ("approved · yours to merge", "approved", True))

    def test_no_reviewer_requested_waits_on_athenas_review_first(self):
        # A review is required and nobody is asked: HQ reviews before the human is asked to request one.
        self.assertEqual(self.move(_pr(), None), ("in review · athena", "reviewing", False))
        self.assertEqual(self.move(_pr(), {"verdict": "fail", "head": HEAD})[0], "review findings · worker fixing")
        self.assertEqual(self.move(_pr(), PASSED), ("no reviewer requested", "unrequested", True))
        self.assertEqual(self.move(_pr(), None, live=False), ("no reviewer requested", "unrequested", True))

    def test_earlier_moves_come_first(self):
        # Draft, failing checks, changes requested, a reviewer asked, CI still running: as before, verdict or not.
        for review in (None, {"verdict": "fail", "head": HEAD}):
            self.assertEqual(self.move(_pr(draft=True), review)[0], "draft")
            self.assertEqual(self.move(_pr(review="", checks="failure"), review)[0], "checks failing")
            self.assertEqual(self.move(_pr(review="CHANGES_REQUESTED", reviews={"alex": "CHANGES_REQUESTED"}), review)[0],
                             "changes requested · worker fixing")
            self.assertEqual(self.move(_pr(requested=["alex"]), review)[0], "awaiting review · alex")
            self.assertEqual(self.move(_pr(review="", checks="pending"), review)[0], "checks running")
            self.assertEqual(self.move(_pr(review="APPROVED", auto_merge=True), review)[0], "approved · auto-merge on")

    def test_a_pr_with_no_live_worker_is_unchanged(self):
        for review in (None, {"verdict": "fail", "head": HEAD}, {"verdict": "start", "head": HEAD}):
            self.assertEqual(self.move(_pr(review=""), review, live=False), ("your go to merge", "mergeable", True))
            self.assertEqual(self.move(_pr(review="APPROVED"), review, live=False, human_merge=True),
                             ("approved · yours to merge", "approved", True))


class DashFixtureTest(unittest.TestCase):
    def test_the_dash_fixture_moves_are_what_pr_move_says(self):
        # The dash renders the move athena watch stored: its fixture must hold what pr.move gives (web is human-merged).
        fixture = json.loads((Path(__file__).resolve().parent.parent / "dash/testdata/state/watch.json").read_text())
        state = Path(__file__).resolve().parent.parent / "dash/testdata/state"
        verdicts = {}  # HQ's review per live worker, as the fixture ledger records it
        for line in (state / "ledger.jsonl").read_text().splitlines():
            ev = json.loads(line)
            if ev.get("review"):
                verdicts[ev["name"]] = ev["review"]
        stored = [(p, True, verdicts.get(name)) for name, p in ((n, w.get("pr")) for n, w in fixture["workers"].items())
                  if p and "move" in p]
        stored += [(p, False, None) for p in fixture["prs"].values()]
        self.assertGreaterEqual(len(stored), 4)
        for p, live, review in stored:
            human = "/web/" in p["url"]
            self.assertEqual(p["move"], pr.move(p, live=live, human_merge=human, review=review), p["url"])


def _graphql(nodes):
    return mock.Mock(returncode=0, stdout=json.dumps({"data": nodes}), stderr="")


class BatchTest(unittest.TestCase):
    def test_one_graphql_call_for_every_pr(self):
        node = {"number": 906, "url": "https://github.com/acme/web/pull/906", "title": "Stacked: step 2",
                "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "you/abc-147-step-2", "mergeable": "MERGEABLE",
                "reviewDecision": "REVIEW_REQUIRED", "headRefOid": "abcdef1234567890", "isInMergeQueue": False,
                "mergeStateStatus": "BLOCKED", "autoMergeRequest": {"enabledAt": "x"},
                "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "alex", "name": None}}]},
                "latestReviews": {"nodes": [{"author": {"login": "kim"}, "state": "COMMENTED"}]},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]}}
        answer = {"r0": {"p0": node, "p1": None}, "r1": {"p0": dict(node, number=9, state="MERGED")}}
        with mock.patch.object(pr.subprocess, "run", return_value=_graphql(answer)) as run:
            got = pr.batch([("acme/web", 906), ("acme/web", 6150), ("acme/api", 9)])
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "api", "graphql"])
        self.assertIn("o0=acme", argv)
        self.assertIn("n1=api", argv)
        self.assertEqual(set(got), {"acme/web#906", "acme/api#9"})
        web = got["acme/web#906"]
        self.assertEqual((web["requested"], web["auto_merge"], web["checks"], web["base"], web["branch"], web["title"]),
                         (["alex"], True, "success", "main", "you/abc-147-step-2", "Stacked: step 2"))
        self.assertIn("headRefName", " ".join(argv))  # the dash stacks a PR on the one whose head branch is its base
        self.assertEqual(got["acme/api#9"]["state"], "MERGED")

    def test_nothing_to_fetch_makes_no_call_and_a_failure_is_no_data(self):
        with mock.patch.object(pr.subprocess, "run") as run:
            self.assertEqual(pr.batch([]), {})
            self.assertEqual(run.call_count, 0)
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="rate limited")
            self.assertIsNone(pr.batch([("acme/web", 1)]))

    def test_bad_slugs_and_numbers_never_reach_the_query(self):
        with mock.patch.object(pr.subprocess, "run", return_value=_graphql({})) as run:
            pr.batch([('acme/web"){x}', 1), ("acme/web", "1 }"), ("acme/web", 2)])
        query = " ".join(run.call_args.args[0])
        self.assertNotIn("{x}", query)
        self.assertIn("pullRequest(number: 2)", query)
        self.assertNotIn("1 }", query)


class HasCITest(unittest.TestCase):
    def test_a_workflows_directory_means_ci(self):
        def answer(code, stderr=""):
            return mock.patch.object(pr.subprocess, "run", return_value=mock.Mock(returncode=code, stdout="[]", stderr=stderr))
        with answer(0) as run:
            self.assertTrue(pr.has_ci("acme/web"))
        self.assertEqual(run.call_args.args[0][:3], ["gh", "api", "repos/acme/web/contents/.github/workflows"])
        with answer(1, "gh: Not Found (HTTP 404)"):
            self.assertFalse(pr.has_ci("acme/web"))
        with answer(1, "gh: API rate limit exceeded (HTTP 403)"):
            self.assertIsNone(pr.has_ci("acme/web"))
        with mock.patch.object(pr.subprocess, "run") as run:
            self.assertIsNone(pr.has_ci("not a slug"))
            self.assertEqual(run.call_count, 0)


class ReviewRequestedTest(unittest.TestCase):
    def test_a_failed_search_keeps_that_repos_last_list(self):
        last = [{"repo": "acme/web", "number": 4, "title": "t", "author": "kim", "url": "https://github.com/acme/web/pull/4"},
                {"repo": "acme/api", "number": 5, "title": "u", "author": "kim", "url": "https://github.com/acme/api/pull/5"}]

        def run(argv, **kw):
            if argv[argv.index("--repo") + 1] == "acme/web":
                return mock.Mock(returncode=1, stdout="", stderr="rate limited")
            return mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(pr.subprocess, "run", side_effect=run):
            self.assertEqual(pr.review_requested(["acme/web", "acme/api"], last=last), last[:1])

    def test_one_search_per_repo_capped_and_linked_by_the_searched_repo(self):
        def run(argv, **kw):
            repo = argv[argv.index("--repo") + 1]
            items = [{"number": n, "title": f"{repo} change {n}", "url": f"https://evil.example/{n}",
                      "author": {"login": "kim"}} for n in range(1, 8)]
            return mock.Mock(returncode=0, stdout=json.dumps(items), stderr="")
        with mock.patch.object(pr.subprocess, "run", side_effect=run) as called:
            got = pr.review_requested(["acme/web", "acme/api", "not a slug"], cap=10)
        self.assertEqual(called.call_count, 2)
        argv = called.call_args_list[0].args[0]
        self.assertEqual(argv[:6], ["gh", "search", "prs", "user-review-requested:@me", "draft:false", "--state=open"])
        self.assertEqual(len(got), 10)
        self.assertEqual(got[0], {"repo": "acme/web", "number": 1, "title": "acme/web change 1", "author": "kim",
                                  "url": "https://github.com/acme/web/pull/1"})
        self.assertEqual(got[-1]["repo"], "acme/api")

    def test_only_a_non_draft_pr_requesting_the_human_by_name_becomes_a_row(self):
        prs = [{"number": 1, "title": "team only", "draft": False, "users": [], "teams": ["devs"]},
               {"number": 2, "title": "a draft", "draft": True, "users": ["me"], "teams": []},
               {"number": 3, "title": "direct", "draft": False, "users": ["me"], "teams": ["devs"]}]

        def search(argv, **kw):
            # GitHub's search: review-requested matches the human or one of their teams (devs),
            # user-review-requested only the human by name, and draft:false drops drafts.
            hits = [p for p in prs if ("user-review-requested:@me" in argv and "me" in p["users"])
                    or ("--review-requested=@me" in argv and ("me" in p["users"] or "devs" in p["teams"]))]
            hits = [p for p in hits if not ("draft:false" in argv and p["draft"])]
            items = [{"number": p["number"], "title": p["title"], "author": {"login": "kim"}, "isDraft": p["draft"]}
                     for p in hits]
            return mock.Mock(returncode=0, stdout=json.dumps(items), stderr="")
        with mock.patch.object(pr.subprocess, "run", side_effect=search):
            got = pr.review_requested(["acme/web"])
        self.assertEqual([r["number"] for r in got], [3])

    def test_a_draft_the_search_returns_anyway_is_dropped(self):
        items = [{"number": 2, "title": "a draft", "author": {"login": "kim"}, "isDraft": True},
                 {"number": 3, "title": "direct", "author": {"login": "kim"}}]
        with mock.patch.object(pr.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(items), stderr="")):
            self.assertEqual([r["number"] for r in pr.review_requested(["acme/web"])], [3])

    def test_a_failed_search_is_skipped(self):
        with mock.patch.object(pr.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="x")):
            self.assertEqual(pr.review_requested(["acme/web"]), [])


if __name__ == "__main__":
    unittest.main()
