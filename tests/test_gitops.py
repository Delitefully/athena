import tempfile
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import git, make_repo, push_from_clone
from cos_lib import gitops


class GitopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, self.origin = make_repo(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_preflight_ok_fast_forwards(self):
        head = push_from_clone(self.tmp.name, self.origin, "new.txt")
        result = gitops.preflight(self.repo)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_preflight_refuses_feature_branch(self):
        git(self.repo, "checkout", "-q", "-b", "gabriel/plt-1-x")
        result = gitops.preflight(self.repo)
        self.assertFalse(result["ok"])
        self.assertIn("on branch gabriel/plt-1-x, not main", " ".join(result["problems"]))

    def test_preflight_refuses_dirty_tracked(self):
        (self.repo / "README.md").write_text("changed\n")
        result = gitops.preflight(self.repo)
        self.assertFalse(result["ok"])
        self.assertEqual(result["dirty"], ["README.md"])

    def test_preflight_ignores_untracked(self):
        (self.repo / "scratch.txt").write_text("x\n")
        self.assertTrue(gitops.preflight(self.repo)["ok"])

    def test_preflight_reports_diverged(self):
        push_from_clone(self.tmp.name, self.origin, "remote.txt")
        (self.repo / "local.txt").write_text("x\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-q", "-m", "local")
        result = gitops.preflight(self.repo)
        self.assertFalse(result["ok"])
        self.assertIn("cannot fast-forward", " ".join(result["problems"]))

    def test_choose_base_local_remote_default(self):
        git(self.repo, "branch", "gabriel/local-only")
        self.assertEqual(gitops.choose_base(self.repo, "gabriel/local-only"), [])
        push_from_clone(self.tmp.name, self.origin, "f.txt", branch="gabriel/remote-only")
        git(self.repo, "fetch", "-q", "origin")
        self.assertEqual(gitops.choose_base(self.repo, "gabriel/remote-only"), ["--base", "origin/gabriel/remote-only"])
        self.assertEqual(gitops.choose_base(self.repo, "gabriel/new"), ["--base", "origin/main"])

    def test_unpushed_counts(self):
        wt = Path(self.tmp.name) / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "gabriel/plt-2", str(wt), "main")
        state = gitops.unpushed(wt)
        self.assertEqual(state, {"dirty": [], "ahead": 0, "has_upstream": False, "branch": "gabriel/plt-2"})
        (wt / "a.txt").write_text("a\n")
        self.assertEqual(gitops.unpushed(wt)["dirty"], ["a.txt"])
        git(wt, "add", "a.txt")
        git(wt, "commit", "-q", "-m", "a")
        state = gitops.unpushed(wt)
        self.assertEqual(state["ahead"], 1)
        git(wt, "push", "-q", "-u", "origin", "gabriel/plt-2")
        state = gitops.unpushed(wt)
        self.assertEqual((state["ahead"], state["has_upstream"]), (0, True))

    def test_backup_ref(self):
        sha = git(self.repo, "rev-parse", "HEAD")
        ref = gitops.backup_ref(self.repo, "plt-9", sha)
        self.assertTrue(ref.startswith("refs/cos-backup/plt-9/"))
        self.assertEqual(git(self.repo, "rev-parse", ref), sha)


if __name__ == "__main__":
    unittest.main()
