import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import logged_calls, write_scenario
from cos_lib import ledger, retire


class RetireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "plt-1"
        git(self.repo, "worktree", "add", "-q", "-b", "gabriel/plt-1", str(self.wt), "main")
        ledger.append("spawn", "plt-1", repo=str(self.repo), path=os.path.realpath(self.wt), branch="gabriel/plt-1",
                      workspace="w5", pane="w5:p1", terminal="term_1")
        write_scenario(self.tmp.name, [])

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_retire_refuses_unpushed(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        with self.assertRaises(retire.RetireError) as ctx:
            retire.retire("plt-1")
        self.assertIn("1 unpushed commit", str(ctx.exception))
        self.assertEqual(ledger.workers()["plt-1"]["state"], "live")
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["worktree", "remove"]], [])

    def test_retire_refuses_dirty(self):
        (self.wt / "b.txt").write_text("b\n")
        with self.assertRaises(retire.RetireError) as ctx:
            retire.retire("plt-1")
        self.assertIn("uncommitted", str(ctx.exception))

    def test_retire_writes_backup_ref_first(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        head = git(self.wt, "rev-parse", "HEAD")
        with self.assertRaises(retire.RetireError):
            retire.retire("plt-1")
        refs = git(self.repo, "for-each-ref", "--format=%(objectname)", "refs/cos-backup/plt-1/")
        self.assertEqual(refs.splitlines(), [head])

    def test_retire_clean_pushed(self):
        (self.wt / "a.txt").write_text("a\n")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "a")
        git(self.wt, "push", "-q", "-u", "origin", "gabriel/plt-1")
        result = retire.retire("plt-1")
        calls = logged_calls(self.tmp.name)
        self.assertIn(["agent", "prompt", "w5:p1", "/exit"], calls)
        self.assertIn(["worktree", "remove", "--workspace", "w5"], calls)
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")
        self.assertTrue(result["backup"].startswith("refs/cos-backup/plt-1/"))

    def test_retire_force_passes_force(self):
        (self.wt / "b.txt").write_text("b\n")
        retire.retire("plt-1", force=True)
        self.assertIn(["worktree", "remove", "--workspace", "w5", "--force"], logged_calls(self.tmp.name))

    def test_retire_unknown(self):
        with self.assertRaises(retire.RetireError):
            retire.retire("nope")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_cli_status_json(self):
        ledger.append("spawn", "a", pane="w2:p1", branch="gabriel/a")
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_status": "idle"}}}}])
        out = subprocess.run([str(helpers.ROOT / "bin" / "cos"), "status", "--json"], capture_output=True, text=True,
                             env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data[0]["name"], "a")
        self.assertEqual(data[0]["state"], "idle")

    def test_cli_status_table_empty(self):
        out = subprocess.run([str(helpers.ROOT / "bin" / "cos"), "status"], capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("no live workers", out.stdout)

    def test_cli_spawn_error_exit_code(self):
        out = subprocess.run([str(helpers.ROOT / "bin" / "cos"), "spawn", "--repo", self.tmp.name, "--branch", "x",
                              "--name", "BAD", "--brief", "/dev/null", "--goal", "g"], capture_output=True, text=True,
                             env=dict(os.environ))
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid worker name", out.stderr)


if __name__ == "__main__":
    unittest.main()
