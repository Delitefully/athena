import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests import helpers
from tests.gitrepo import make_repo
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import ledger, paths, spawn

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
        self.assertEqual(create[create.index("--label") + 1], "fix it")
        start = calls[1]
        self.assertEqual(start[:3], ["agent", "start", "plt-1"])
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[claude.index("--name") + 1], "plt-1")
        self.assertTrue(claude[claude.index("--append-system-prompt-file") + 1].endswith("worker.md"))
        settings = json.loads(Path(claude[claude.index("--settings") + 1]).read_text())
        self.assertEqual(settings["env"]["ATHENA_WORKER"], "plt-1")
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
        os.environ["ATHENA_MAX_WORKERS"] = "2"
        ledger.append("spawn", "a", pane="w1:p1")
        ledger.append("spawn", "b", pane="w2:p1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("2 workers are live", str(ctx.exception))
        self.assertEqual(logged_calls(self.tmp.name), [])

    def test_hard_cap_ignores_force(self):
        self.scenario()
        for i in range(6):
            ledger.append("spawn", f"w{i}", pane=f"w{i}:p1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn(force=True)
        self.assertIn("hard cap is 6", str(ctx.exception))

    def test_force_allows_a_sixth_worker(self):
        self.scenario()
        for i in range(5):
            ledger.append("spawn", f"w{i}", pane=f"w{i}:p1")
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()
        self.assertEqual(self.run_spawn(force=True)["name"], "plt-1")

    def test_spawn_refuses_when_quota_high(self):
        self.scenario()
        with open(os.environ["ATHENA_QUOTA_FILE"], "w") as f:
            json.dump({"fetched_at_unix": time.time(), "windows": [{"kind": "five_hour", "used_percent": 91}]}, f)
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("quota", str(ctx.exception))
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(force=True)
        self.assertEqual(self.run_spawn(ignore_quota=True)["name"], "plt-1")

    def test_prompt_failure_leaves_worker_tracked(self):
        err = {"error": {"code": "agent_not_found", "message": "gone"}}
        self.scenario([{"match": ["agent", "prompt"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("athena nudge plt-1", str(ctx.exception))
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["state"], w["prompt"]), ("live", "failed: agent_not_found"))

    def test_spawn_refuses_dirty_main(self):
        self.scenario()
        (self.repo / "README.md").write_text("dirty\n")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("modified tracked files", str(ctx.exception))

    def test_spawn_failure_removes_claim(self):
        err = {"error": {"code": "pane_not_found", "message": "gone"}}
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("pane_not_found", str(ctx.exception))
        wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")
        self.assertFalse(paths.claim_file(str(wt)).exists())
        w = ledger.workers()["plt-1"]
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["pane"], "w9:p1")
        self.assertEqual(ledger.live(), [])

    def test_startup_prompt_is_not_a_failure(self):
        err = {"error": {"code": "agent_not_ready", "message": "blocked during startup"}}
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        result = self.run_spawn()
        self.assertTrue(result["pending"])
        wt = paths.worktree_path(self.repo, "gabriel/plt-1-fix")
        self.assertFalse(paths.claim_file(str(wt)).exists())
        self.assertEqual([w["name"] for w in ledger.live()], ["plt-1"])

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

    def test_settings_without_ultracode_are_unchanged(self):
        brief = paths.ensure("briefs") / "plt-1.md"
        text = spawn._write_settings("plt-1", brief).read_text()
        expected = {
            "env": {"ATHENA_WORKER": "plt-1", "ATHENA_HQ": paths.hq_name(), "ATHENA_STATE_DIR": str(paths.state_dir()),
                    "ATHENA_BRIEF": str(brief)},
            "crossSessionInbound": "accept",
            "enabledPlugins": {"keepwarm@keepwarm": False, "keepwarm-bundle@keepwarm": False},
        }
        self.assertEqual(text, json.dumps(expected, indent=2) + "\n")
        self.assertNotIn("ultracode", text)
        self.assertNotIn("workflowSizeGuideline", text)

    def settings_passed(self):
        start = [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "start"]][0]
        claude = start[start.index("--") + 1:]
        return json.loads(Path(claude[claude.index("--settings") + 1]).read_text())

    def test_ultracode_turns_on_the_setting_and_is_recorded(self):
        self.scenario()
        result = self.run_spawn(ultracode=True, workflow_size="large")
        settings = self.settings_passed()
        self.assertIs(settings["ultracode"], True)
        self.assertEqual(settings["workflowSizeGuideline"], "large")
        self.assertEqual(settings["env"]["ATHENA_WORKER"], "plt-1")
        w = ledger.workers()["plt-1"]
        self.assertEqual((w["ultracode"], w["workflow_size"]), (True, "large"))
        self.assertEqual((result["ultracode"], result["workflow_size"]), (True, "large"))
        prompt = [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"]][0]
        self.assertNotIn("ultracode", prompt[3].lower())  # the setting turns it on, never the goal text

    def test_ultracode_without_size_leaves_the_guideline_alone(self):
        self.scenario()
        self.run_spawn(ultracode=True)
        settings = self.settings_passed()
        self.assertIs(settings["ultracode"], True)
        self.assertNotIn("workflowSizeGuideline", settings)
        w = ledger.workers()["plt-1"]
        self.assertTrue(w["ultracode"])
        self.assertNotIn("workflow_size", w)

    def test_no_ultracode_keys_without_the_flag(self):
        self.scenario()
        result = self.run_spawn()
        self.assertNotIn("ultracode", self.settings_passed())
        self.assertNotIn("ultracode", ledger.workers()["plt-1"])
        self.assertNotIn("ultracode", result)

    def test_workflow_size_needs_ultracode(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn(workflow_size="large")
        self.assertIn("--ultracode", str(ctx.exception))
        self.assertEqual(logged_calls(self.tmp.name), [])

    def test_rejects_bad_name_and_live_duplicate(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(name="PLT-1")
        ledger.append("spawn", "plt-1", pane="w1:p1")
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()


class UltracodeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name, "proj")
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text("# PLT-1\nGOAL: fix it\n")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([str(helpers.ROOT / "bin" / "athena"), *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def spawn_args(self, *extra):
        return ("spawn", "--repo", str(self.repo), "--branch", "gabriel/plt-1-fix", "--name", "plt-1",
                "--brief", str(self.brief), "--goal", "PLT-1 is done when x.", *extra)

    def test_workflow_size_without_ultracode_is_rejected(self):
        out = self.cli(*self.spawn_args("--workflow-size", "large"))
        self.assertEqual(out.returncode, 2)
        self.assertIn("--workflow-size needs --ultracode", out.stderr)
        self.assertEqual(logged_calls(self.tmp.name), [])
        self.assertEqual(ledger.workers(), {})

    def test_workflow_size_choices(self):
        out = self.cli(*self.spawn_args("--ultracode", "--workflow-size", "huge"))
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid choice", out.stderr)

    def test_spawn_output_and_status_show_ultracode(self):
        write_scenario(self.tmp.name, [{"match": ["worktree", "create"], "stdout": CREATED}])
        out = self.cli(*self.spawn_args("--ultracode", "--workflow-size", "medium"))
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual((result["ultracode"], result["workflow_size"]), (True, "medium"))
        ledger.append("spawn", "plt-2", pane="w2:p1")
        ledger.append("spawn", "plt-3", pane="w3:p1", ultracode=True)
        lines = {line.split()[0]: line for line in self.cli("status").stdout.splitlines() if line[:1].strip()}
        self.assertIn("  UC:medium", lines["plt-1"])
        self.assertIn("  UC", lines["plt-3"])
        self.assertNotIn("UC:", lines["plt-3"])
        self.assertNotIn("UC", lines["plt-2"])
        rows = {r["name"]: r for r in json.loads(self.cli("status", "--json").stdout)}
        self.assertEqual((rows["plt-1"]["ultracode"], rows["plt-1"]["workflow_size"]), (True, "medium"))
        self.assertFalse(rows["plt-2"]["ultracode"])


if __name__ == "__main__":
    unittest.main()


class SpaceLabelTest(unittest.TestCase):
    def test_title_alone(self):
        self.assertEqual(spawn.space_label("plt-4365", "Edit-group dialog keeps member identity", "PLT-4365"),
                         "Edit-group dialog keeps member identity")

    def test_ticket_id_stripped_from_title(self):
        for title in ("PLT-4365: Fix the dialog", "plt-4365 Fix the dialog", "[PLT-4365] Fix the dialog",
                      "Fix the dialog (PLT-4365)", "Fix the dialog · plt-4365"):
            self.assertEqual(spawn.space_label("plt-4365", title, "PLT-4365"), "Fix the dialog", title)

    def test_other_hyphenated_words_kept(self):
        self.assertEqual(spawn.space_label("plt-4366", "macOS 27 browser-ext reads 0", "PLT-4366"),
                         "macOS 27 browser-ext reads 0")

    def test_branch_slug_without_title(self):
        self.assertEqual(spawn.space_label("plt-4365", None, "PLT-4365",
                                           "gabriel/plt-4365-endpoints-tab-editing"), "endpoints tab editing")

    def test_name_is_last_resort(self):
        self.assertEqual(spawn.space_label("plt-1", "PLT-1", "PLT-1", "plt-1"), "plt-1")

    def test_truncated(self):
        self.assertEqual(len(spawn.space_label("plt-1", "x" * 80)), 48)
