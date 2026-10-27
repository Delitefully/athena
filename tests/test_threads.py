"""athena threads: a PR's unresolved review threads, so HQ can check a DONE has none open and work through the AI
reviewer's (answer or fix, then resolve) before they hold an approved PR out of the merge queue."""
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import cli, ledger, pr


def _thread(tid, login, resolved=False, outdated=False, typename="User", body="This leaks the token.", line=12):
    return {"id": tid, "isResolved": resolved, "isOutdated": outdated, "path": "app/auth.py", "line": line,
            "comments": {"nodes": [{"author": {"__typename": typename, "login": login}, "body": body,
                                    "url": f"https://github.com/o/web/pull/7#discussion_r{tid}"}]}}


def _answer(*threads):
    data = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(threads)}}}}}
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr="")


class ThreadsTest(unittest.TestCase):
    def test_only_unresolved_threads_with_who_and_where(self):
        answer = _answer(_thread("T1", "copilot-pull-request-reviewer", typename="Bot"),
                         _thread("T2", "teammate", resolved=True), _thread("T3", "teammate", outdated=True))
        with mock.patch.object(pr.subprocess, "run", return_value=answer) as run:
            found = pr.threads("o/web", 7)
        self.assertEqual([t["id"] for t in found], ["T1", "T3"])
        self.assertEqual(found[0], {"id": "T1", "author": "copilot-pull-request-reviewer", "bot": True,
                                    "path": "app/auth.py", "line": 12, "outdated": False,
                                    "body": "This leaks the token.",
                                    "url": "https://github.com/o/web/pull/7#discussion_rT1"})
        self.assertFalse(found[1]["bot"])
        args = run.call_args[0][0]
        self.assertIn("owner=o", args)
        self.assertIn("name=web", args)
        self.assertIn("number=7", args)

    def test_a_long_body_is_cut(self):
        with mock.patch.object(pr.subprocess, "run", return_value=_answer(_thread("T1", "x", body="y" * 900))):
            self.assertEqual(len(pr.threads("o/web", 7)[0]["body"]), pr.THREAD_BODY + 1)

    def test_gh_failing_is_none_never_no_threads(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502")
        with mock.patch.object(pr.subprocess, "run", return_value=failed):
            self.assertIsNone(pr.threads("o/web", 7))
        self.assertIsNone(pr.threads("not a slug", 7))


class ThreadsCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        ledger.append("spawn", "abc-7", repo="/src/web", branch="you/abc-7")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def athena(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_by_worker_name_or_pr(self):
        summary = {"number": 7, "url": "https://github.com/o/web/pull/7"}
        with mock.patch.object(cli.pr, "summary", return_value=summary), \
                mock.patch.object(cli.pr, "threads", return_value=[{"id": "T1"}]) as threads:
            code, out, _ = self.athena("threads", "abc-7")
        self.assertEqual((code, json.loads(out)), (0, [{"id": "T1"}]))
        threads.assert_called_once_with("o/web", 7)
        for given in ("o/web#7", "https://github.com/o/web/pull/7"):
            with mock.patch.object(cli.pr, "threads", return_value=[]) as threads:
                self.assertEqual(self.athena("threads", given)[0], 0)
            threads.assert_called_once_with("o/web", 7)

    def test_unknown_or_unreadable_is_exit_2(self):
        self.assertEqual(self.athena("threads", "nobody")[0], 2)
        with mock.patch.object(cli.pr, "threads", return_value=None):
            code, _, err = self.athena("threads", "o/web#7")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
