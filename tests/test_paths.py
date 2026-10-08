import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401  (sets sys.path)
from athena_lib import paths


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_slug_strips_user_prefix(self):
        self.assertEqual(paths.slug("gabriel/plt-4512-SSO Fix"), "plt-4512-sso-fix")
        self.assertEqual(paths.slug("plt-1--x__y"), "plt-1-x-y")
        self.assertEqual(paths.slug("a/b/c-d"), "b-c-d")

    def test_worktree_path(self):
        repo = Path("/Users/someone/Developer/platform")
        expected = Path(self.tmp.name) / "worktrees" / "platform" / "plt-4512-sso-fix"
        self.assertEqual(paths.worktree_path(repo, "gabriel/plt-4512-sso-fix"), expected)

    def test_claim_file_matches_shasum(self):
        target = Path(self.tmp.name)
        out = subprocess.run(
            ["sh", "-c", "printf '%s' \"$1\" | shasum | cut -c 1-16", "sh", os.path.realpath(target)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(paths.claim_file(str(target)).name, out)
        self.assertEqual(paths.claim_file(str(target)).parent, Path(self.tmp.name) / "state" / "claims")

    def test_valid_name(self):
        self.assertTrue(paths.valid_name("plt-4512"))
        self.assertTrue(paths.valid_name("athena"))
        self.assertFalse(paths.valid_name("PLT-4512"))
        self.assertFalse(paths.valid_name("4512"))
        self.assertFalse(paths.valid_name("a" * 33))
        self.assertFalse(paths.valid_name("a b"))


if __name__ == "__main__":
    unittest.main()
