import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import make_repo
from tests.test_herdr import logged_calls, write_scenario
from cos_lib import ledger, paths, spawn

CREATED = {"result": {"type": "worktree_created",
                      "workspace": {"workspace_id": "w9"},
                      "root_pane": {"pane_id": "w9:p1", "terminal_id": "term_abc", "cwd": "/x"},
                      "worktree": {"branch": "gabriel/plt-1-fix"}}}


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name, "proj")
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text("# PLT-1\nGOAL: fix it\n")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def scenario(self, extra=None):
        write_scenario(self.tmp.name, (extra or []) + [{"match": ["worktree", "create"], "stdout": CREATED}])

    def run_spawn(self, **kw):
        args = dict(repo=self.repo, branch="gabriel/plt-1-fix", name="plt-1", brief_path=self.brief,
                    goal="PLT-1 is done when the test passes.", linear="PLT-1", title="fix it")
        args.update(kw)
        return spawn.spawn(**args)

    def test_spawn_calls_in_order(self):
        self.scenario()
        result = self.run_spawn()
        calls = logged_calls(self.tmp.name)
        verbs = [c[:2] for c in calls]
        self.assertEqual(verbs, [["worktree", "create"], ["agent", "start"], ["agent", "prompt"],
                                 ["agent", "wait"], ["workspace", "report-metadata"]])
        create = calls[0]
        wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")
        self.assertIn(str(wt), create)
        self.assertEqual(create[create.index("--base") + 1], "origin/main")
        self.assertEqual(create[create.index("--label") + 1], "plt-1 fix it")
        start = calls[1]
        self.assertEqual(start[:3], ["agent", "start", "plt-1"])
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[claude.index("--name") + 1], "plt-1")
        self.assertTrue(claude[claude.index("--append-system-prompt-file") + 1].endswith("worker.md"))
        settings = json.loads(Path(claude[claude.index("--settings") + 1]).read_text())
        self.assertEqual(settings["env"]["COS_WORKER"], "plt-1")
        self.assertEqual(settings["crossSessionInbound"], "accept")
        self.assertFalse(settings["enabledPlugins"]["keepwarm@keepwarm"])
        prompt = calls[2]
        self.assertTrue(prompt[3].startswith("/goal PLT-1 is done when the test passes."))
        self.assertIn(str(paths.state_dir() / "briefs" / "plt-1.md"), prompt[3])
        self.assertNotIn("\n", prompt[3])
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["state"], w["pane"], w["terminal"], w["workspace"]), ("live", "w9:p1", "term_abc", "w9"))
        self.assertFalse(paths.claim_file(str(wt)).exists())
        self.assertEqual(result["name"], "plt-1")

    def test_spawn_refuses_over_cap(self):
        self.scenario()
        os.environ["COS_MAX_WORKERS"] = "2"
        ledger.append("spawn", "a", pane="w1:p1")
        ledger.append("spawn", "b", pane="w2:p1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("2 workers are live", str(ctx.exception))
        self.assertEqual(logged_calls(self.tmp.name), [])

    def test_hard_cap_ignores_force(self):
        self.scenario()
        for i in range(5):
            ledger.append("spawn", f"w{i}", pane=f"w{i}:p1")
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(force=True)

    def test_spawn_refuses_when_quota_high(self):
        self.scenario()
        with open(os.environ["COS_QUOTA_FILE"], "w") as f:
            json.dump({"fetched_at_unix": time.time(), "windows": [{"kind": "five_hour", "used_percent": 91}]}, f)
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("quota", str(ctx.exception))

    def test_spawn_refuses_dirty_main(self):
        self.scenario()
        (self.repo / "README.md").write_text("dirty\n")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("modified tracked files", str(ctx.exception))

    def test_spawn_failure_removes_claim(self):
        err = {"error": {"code": "agent_not_ready", "message": "blocked during startup"}}
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("agent_not_ready", str(ctx.exception))
        wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")
        self.assertFalse(paths.claim_file(str(wt)).exists())
        w = ledger.workers()["plt-1"]
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["pane"], "w9:p1")
        self.assertEqual(ledger.live(), [])

    def test_claim_written_before_worktree_create(self):
        wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")
        wt.parent.mkdir(parents=True, exist_ok=True)
        claim = paths.claim_file(str(wt))
        seen = Path(self.tmp.name) / "seen"
        # The fake herdr cannot see the claim, so check it from the scenario hook: use a create that fails,
        # then assert the claim existed when create ran by inspecting the failure path ordering.
        self.scenario([{"match": ["worktree", "create"], "stdout": "", "stderr": json.dumps({"error": {"code": "x", "message": "boom"}}), "exit": 1}])
        original = spawn._create_worktree

        def spy(*a, **k):
            seen.write_text("yes" if claim.exists() else "no")
            return original(*a, **k)

        spawn._create_worktree = spy
        try:
            with self.assertRaises(spawn.SpawnError):
                self.run_spawn()
        finally:
            spawn._create_worktree = original
        self.assertEqual(seen.read_text(), "yes")
        self.assertFalse(claim.exists())

    def test_rejects_bad_name_and_live_duplicate(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(name="PLT-1")
        ledger.append("spawn", "plt-1", pane="w1:p1")
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()


if __name__ == "__main__":
    unittest.main()
