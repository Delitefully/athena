"""The review channel's record (config `open_prs_channel`): the PRs posted there for review, as PR keys and ts in
open-prs-posted.json (athena_lib/slack.py), and `athena open-prs show|add|check` around a post."""
import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from tests import helpers
from athena_lib import cli, config, paths, pr, slack

CHAN = "C0OPENPR1"
DAY = 24 * 3600


def url(n, repo="acme/web"):
    return f"https://github.com/{repo}/pull/{n}"


class PrKeysTest(unittest.TestCase):
    def test_slack_link_formats_and_bare_links(self):
        self.assertEqual(slack.pr_keys(f"<{url(6978)}|acme/web#6978> - a warning on Mac"), ["acme/web#6978"])
        self.assertEqual(slack.pr_keys(f"<{url(6980)}> - something"), ["acme/web#6980"])
        self.assertEqual(slack.pr_keys(f"see {url(7)}/files and <{url(8)}|this>, again {url(7)}."),
                         ["acme/web#7", "acme/web#8"])
        self.assertEqual(slack.pr_key(url(12, "Acme/Web")), "acme/web#12")
        self.assertEqual(slack.pr_key("Acme/Web#12"), "acme/web#12")

    def test_other_links_are_not_prs(self):
        for text in ("https://github.com/acme/web/issues/6978", "https://github.com/acme/web/pulls",
                     "https://gitlab.com/acme/web/pull/1", "https://github.com/acme/web/pull/0", "no link", None, 42):
            self.assertEqual(slack.pr_keys(text), [], text)


class PostedRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_a_re_post_keeps_its_newer_ts_and_old_posts_age_out(self):
        now = 2_000_000_000.0
        slack.record_posted({"acme/web#1": f"{now - 100:.6f}", "acme/web#2": f"{now - 95 * DAY:.6f}"}, now=now)
        self.assertEqual(list(slack.posted()), ["acme/web#1"])
        slack.record_posted({"acme/web#1": f"{now - 200:.6f}"}, now=now)
        self.assertEqual(slack.posted()["acme/web#1"], f"{now - 100:.6f}")
        slack.record_posted({"acme/web#1": f"{now:.6f}"}, now=now)
        self.assertEqual(slack.posted()["acme/web#1"], f"{now:.6f}")

    def test_a_broken_file_reads_as_empty(self):
        paths.ensure()
        slack.posted_file().write_text("{not json")
        self.assertEqual(slack.posted(), {})
        slack.posted_file().write_text(json.dumps({"Acme/Web#3": 1999999999, "bad": "1", "acme/web#4": True}))
        self.assertEqual(slack.posted(), {"acme/web#3": "1999999999"})


class OpenPrsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        paths.ensure()
        config.config_file().write_text(json.dumps({"open_prs_channel": CHAN}))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_records_a_key_or_a_url(self):
        code, out, _ = self.run_cli("open-prs", "add", "acme/web#6978", url(6980, "Acme/Web"))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(slack.posted()), ["acme/web#6978", "acme/web#6980"])
        self.assertEqual(sorted(json.loads(out)), ["acme/web#6978", "acme/web#6980"])

    def test_add_refuses_anything_else(self):
        code, _, err = self.run_cli("open-prs", "add", "https://github.com/a/b/issues/1")
        self.assertEqual(code, 2)
        self.assertIn("not a PR", err)
        self.assertEqual(slack.posted(), {})

    def test_add_needs_the_channel_configured(self):
        config.config_file().write_text(json.dumps({}))
        code, _, err = self.run_cli("open-prs", "add", "acme/web#5")
        self.assertEqual(code, 2)
        self.assertIn("open_prs_channel", err)
        self.assertFalse(slack.posted_file().exists())

    def check(self, key, reviewed_at):
        with mock.patch.object(pr, "batch", return_value={"acme/web#6978": {
                "teammate_reviewed_at": reviewed_at}} if reviewed_at is not None else None) as batch:
            got = self.run_cli("open-prs", "check", key)
        return got, batch

    def test_check_says_whether_the_post_still_counts(self):
        key = "acme/web#6978"
        (code, out, _), batch = self.check(key, "")
        self.assertEqual((code, json.loads(out)["counts"]), (1, False))  # never posted: post it
        batch.assert_called_once_with([("acme/web", 6978)])
        slack.record_posted({key: "1792000000.000100"})  # 2026-10-14T17:46:40Z
        (code, out, _), _ = self.check(key, "")
        self.assertEqual((code, json.loads(out)["counts"]), (0, True))  # posted, no review since: post nothing
        (code, out, _), _ = self.check(url(6978), "2026-10-14T01:00:00Z")
        self.assertEqual(code, 0)  # reviewed before the post
        (code, out, _), _ = self.check(key, "2026-10-14T20:00:00Z")
        self.assertEqual((code, json.loads(out)["counts"]), (1, False))  # reviewed since: a fresh post is wanted
        (code, _, err), _ = self.check(key, None)
        self.assertEqual(code, 2)  # GitHub unreadable: HQ asks the human
        self.assertIn("cannot tell", err)

    def test_any_error_in_check_is_exit_2_never_1(self):
        # Exit 1 tells HQ to post: an error must never read as that.
        slack.record_posted({"acme/web#6978": "1792000000.000100"})
        for boom in (RuntimeError("boom"), KeyError("x"), ValueError("bad"), TypeError("t")):
            with mock.patch.object(pr, "batch", side_effect=boom):
                code, _, err = self.run_cli("open-prs", "check", "acme/web#6978")
            self.assertEqual(code, 2, boom)
            self.assertIn("cannot tell", err)
        with mock.patch.object(pr, "batch", return_value={"acme/web#6978": {"teammate_reviewed_at": "not a time"}}):
            self.assertEqual(self.run_cli("open-prs", "check", "acme/web#6978")[0], 2)
        with mock.patch.object(slack, "posted", side_effect=OSError("disk")), \
                mock.patch.object(pr, "batch", return_value={"acme/web#6978": {"teammate_reviewed_at": ""}}):
            self.assertEqual(self.run_cli("open-prs", "check", "acme/web#6978")[0], 2)

    def test_a_click_after_a_review_makes_exactly_one_post(self):
        """HQ's steps in SKILL.md section 5 for a PR to post: check, post when it says so, add."""
        key, posts = "acme/web#6978", []
        slack.record_posted({key: "1792000000.000100"})
        review = "2026-10-14T20:00:00Z"  # a teammate's review after that post: the row is back in Needs you

        def hq_takes_the_click():
            (code, _, _), _ = self.check(key, review)
            if code == 1:
                posts.append(key)  # the one Slack post
                self.assertEqual(self.run_cli("open-prs", "add", key)[0], 0)

        hq_takes_the_click()
        hq_takes_the_click()  # a second click, or a retry: the first post counts now
        self.assertEqual(posts, [key])
        self.assertGreater(float(slack.posted()[key]), 1792000000.0001)

    def test_show_lists_the_keys(self):
        slack.record_posted({"acme/web#3": "1999999999.000000"})
        code, out, _ = self.run_cli("open-prs")
        self.assertEqual((code, json.loads(out)), (0, {"acme/web#3": "1999999999.000000"}))


class TeammateReviewTest(unittest.TestCase):
    """A posted PR leaves Awaiting review once a teammate reviews it after the post: the dash reads
    teammate_reviewed_at, the latest review by a person who is not the PR's author. Bots and apps never count."""

    def test_only_teammates_reviews_count(self):
        r = pr.reduce({"author": {"login": "you-dev"}, "latestReviews": [
            {"author": {"login": "github-actions"}, "authorAssociation": "NONE", "state": "COMMENTED",
             "submittedAt": "2026-10-14T21:00:00Z"},
            {"author": {"login": "you-dev"}, "authorAssociation": "MEMBER", "state": "COMMENTED",
             "submittedAt": "2026-10-14T22:00:00Z"},
            {"author": {"login": "dependabot[bot]"}, "authorAssociation": "MEMBER", "state": "COMMENTED",
             "submittedAt": "2026-10-14T22:30:00Z"},
            {"author": {"login": "github-advanced-security"}, "state": "COMMENTED", "submittedAt": "2026-10-14T23:00:00Z"},
            {"author": {"__typename": "Bot", "login": "some-app"}, "authorAssociation": "CONTRIBUTOR",
             "state": "COMMENTED", "submittedAt": "2026-10-14T23:30:00Z"},
            {"author": {"login": "kim"}, "authorAssociation": "MEMBER", "state": "COMMENTED",
             "submittedAt": "2026-10-14T20:00:00Z"},
            {"author": {"login": "bo"}, "authorAssociation": "MEMBER", "state": "APPROVED",
             "submittedAt": "2026-10-14T19:00:00Z"}]})
        self.assertEqual(r["teammate_reviewed_at"], "2026-10-14T20:00:00Z")

    def test_bots_and_the_author_alone_are_no_review(self):
        r = pr.reduce({"author": {"login": "you-dev"}, "latestReviews": [
            {"author": {"login": "github-actions"}, "authorAssociation": "NONE", "state": "COMMENTED",
             "submittedAt": "2026-10-14T21:00:00Z"},
            {"author": {"login": "you-dev"}, "authorAssociation": "OWNER", "state": "COMMENTED",
             "submittedAt": "2026-10-14T22:00:00Z"}]})
        self.assertEqual(r["teammate_reviewed_at"], "")
        self.assertEqual(pr.reduce({})["teammate_reviewed_at"], "")

    def test_the_watchs_bot_list_is_the_one_used(self):
        r = pr.reduce({"author": {"login": "you-dev"}, "latestReviews": [
            {"author": {"login": login}, "authorAssociation": "MEMBER", "state": "COMMENTED",
             "submittedAt": "2026-10-14T21:00:00Z"}
            for login in ("linear-code", "copilot-pull-request-reviewer", "copilot-swe-agent")]})
        self.assertEqual(r["teammate_reviewed_at"], "")
        self.assertTrue(all(pr.is_bot({"login": b}) for b in pr.BOTS))

    def test_both_fetches_ask_for_what_it_needs(self):
        self.assertIn("author", pr.FIELDS.split(","))
        for part in ("author { login }", "submittedAt", "authorAssociation", "__typename"):
            self.assertIn(part, pr._NODE)


class OpenPrsConfigTest(unittest.TestCase):
    def test_off_by_default_and_written_only_when_set(self):
        self.assertIsNone(config.DEFAULTS["open_prs_channel"])  # no workspace's ids in a public repo
        self.assertIn("open_prs_channel", config.OPTIONAL_KEYS)




if __name__ == "__main__":
    unittest.main()
