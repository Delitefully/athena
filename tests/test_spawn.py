import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.gitrepo import make_repo
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import brief as briefs, config, handoff, ledger, paths, spawn, status

CREATED = {"result": {"type": "worktree_created",
                      "workspace": {"workspace_id": "w9"},
                      "root_pane": {"pane_id": "w9:p1", "terminal_id": "term_abc", "cwd": "/x"},
                      "worktree": {"branch": "you/abc-1-fix"}}}


class SpawnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name, "proj")
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text(helpers.BRIEF)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def scenario(self, extra=None):
        write_scenario(self.tmp.name, (extra or []) + [{"match": ["worktree", "create"], "stdout": CREATED}])

    def run_spawn(self, **kw):
        args = dict(repo=self.repo, branch="you/abc-1-fix", name="abc-1", brief_path=self.brief,
                    goal="ABC-1 is done when the test passes.", linear="ABC-1", title="fix it")
        args.update(kw)
        return spawn.spawn(**args)


class SpawnTest(SpawnBase):
    def test_a_skipped_brief_check_is_recorded_on_the_spawn(self):
        self.scenario()
        self.brief.write_text(helpers.BRIEF.replace("GOAL: The parser", "GOAL: Ship the parser"))
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()
        self.run_spawn(brief_check=False)
        record = ledger.workers()["abc-1"]
        self.assertEqual(len(record["brief_check_skipped"]), 1)
        self.assertIn("GOAL (outcome)", record["brief_check_skipped"][0])

    def test_a_passing_brief_records_no_skip(self):
        self.scenario()
        self.run_spawn()
        self.assertNotIn("brief_check_skipped", ledger.workers()["abc-1"])

    def test_a_respawn_starts_with_no_status_from_the_old_life(self):
        """Its old workers/<name>.json (a DONE report, a block count) would read as the new worker's until the new
        Claude's first hook event; it is set aside before that Claude starts."""
        self.scenario()
        old = paths.ensure("workers") / "abc-1.json"
        old.write_text(json.dumps({"name": "abc-1", "state": "done", "report": {"status": "DONE"}, "stop_blocks": 3}))
        self.run_spawn()
        self.assertFalse(old.exists())
        self.assertEqual(json.loads((old.parent / "abc-1.json.prev").read_text())["state"], "done")

    def test_the_preferences_file_is_pasted_into_every_brief(self):
        self.scenario()
        prefs = "# Preferences\n\n## Build\n- No feature flags unless asked.\n"
        (paths.ensure() / "preferences.md").write_text(prefs)
        self.run_spawn()
        copy = (paths.state_dir() / "briefs" / "abc-1.md").read_text()
        self.assertTrue(copy.startswith(helpers.BRIEF))
        self.assertIn("\nPREFERENCES (the human's standing preferences, from " + str(paths.state_dir() / "preferences.md"),
                      copy)
        self.assertIn("- No feature flags unless asked.", copy)
        self.assertNotIn("# Preferences", copy)  # its title, not its rules

    def test_no_preferences_file_leaves_the_brief_as_written(self):
        self.scenario()
        self.run_spawn()
        self.assertEqual((paths.state_dir() / "briefs" / "abc-1.md").read_text(), helpers.BRIEF)

    def test_a_brief_with_its_own_preferences_keeps_them(self):
        self.scenario()
        (paths.ensure() / "preferences.md").write_text("- No feature flags unless asked.\n")
        self.brief.write_text(helpers.BRIEF + "PREFERENCES: HQ pasted them already.\n")
        self.run_spawn()
        copy = (paths.state_dir() / "briefs" / "abc-1.md").read_text()
        self.assertEqual(copy.count("PREFERENCES"), 1)

    def test_spawn_calls_in_order(self):
        self.scenario([{"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_session": {"value": "s"}}}}}])
        result = self.run_spawn()
        calls = logged_calls(self.tmp.name)
        verbs = [c[:2] for c in calls]
        self.assertEqual(verbs, [["worktree", "create"], ["agent", "start"], ["agent", "get"],
                                 ["pane", "report-agent-session"], ["agent", "prompt"], ["agent", "wait"],
                                 ["workspace", "report-metadata"]])
        create = calls[0]
        wt = paths.worktree_path(self.repo, "you/abc-1-fix")
        self.assertIn(str(wt), create)
        self.assertEqual(create[create.index("--base") + 1], "origin/main")
        self.assertEqual(create[create.index("--label") + 1], "fix it")
        start = calls[1]
        self.assertEqual(start[:3], ["agent", "start", "abc-1"])
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[claude.index("--name") + 1], "abc-1")
        self.assertTrue(claude[claude.index("--append-system-prompt-file") + 1].endswith("worker.md"))
        settings = json.loads(Path(claude[claude.index("--settings") + 1]).read_text())
        self.assertEqual(settings["env"]["ATHENA_WORKER"], "abc-1")
        self.assertEqual(settings["crossSessionInbound"], "accept")
        self.assertFalse(settings["enabledPlugins"]["keepwarm@keepwarm"])
        # Remote Control is HQ's alone: --settings outranks the user's remoteControlAtStartup: true.
        self.assertIs(settings["remoteControlAtStartup"], False)
        prompt = calls[4]
        self.assertTrue(prompt[3].startswith("/goal ABC-1 is done when the test passes."))
        self.assertIn(str(paths.state_dir() / "briefs" / "abc-1.md"), prompt[3])
        self.assertNotIn("\n", prompt[3])
        w = ledger.workers()["abc-1"]
        self.assertEqual((w["state"], w["pane"], w["terminal"], w["workspace"]), ("live", "w9:p1", "term_abc", "w9"))
        self.assertFalse(paths.claim_file(str(wt)).exists())
        self.assertEqual(result["name"], "abc-1")

    def test_spawn_registers_the_full_resume_command_with_herdr(self):
        # herdr resumes agents after a restart with a bare `claude --resume <id>` unless told the whole command.
        self.scenario([{"match": ["agent", "get", "w9:p1"], "stdout": {"result": {"agent": {
            "name": "abc-1", "pane_id": "w9:p1", "agent_session": {"kind": "id", "value": "sid-1"}}}}}])
        self.run_spawn()
        calls = logged_calls(self.tmp.name)
        start = next(c for c in calls if c[:2] == ["agent", "start"])
        args = start[start.index("--") + 1:]
        report = next(c for c in calls if c[:2] == ["pane", "report-agent-session"])
        self.assertEqual(report[2], "w9:p1")
        self.assertEqual(report[report.index("--source") + 1], "athena")
        self.assertEqual(report[report.index("--agent") + 1], "claude")
        self.assertEqual(report[report.index("--agent-session-id") + 1], "sid-1")
        self.assertTrue(report[report.index("--seq") + 1].isdigit())
        self.assertEqual(report[report.index("--") + 1:], ["claude", "--resume", "sid-1", *args])
        self.assertLess(calls.index(start), calls.index(report))
        w = ledger.workers()["abc-1"]
        self.assertEqual((w["claude_args"], w["session"], w["resume"]), (args, "sid-1", "registered"))

    def test_waits_for_herdr_to_learn_the_session_id(self):
        # herdr's own Claude hook reports the session; it may land just after `agent start` returns.
        self.scenario([
            {"match": ["agent", "get", "w9:p1"], "stdout": {"result": {"agent": {"name": "abc-1"}}}, "times": 2},
            {"match": ["agent", "get", "w9:p1"], "stdout": {"result": {"agent": {"agent_session": {"value": "sid-9"}}}}},
        ])
        self.run_spawn()
        calls = logged_calls(self.tmp.name)
        self.assertEqual(len([c for c in calls if c[:2] == ["agent", "get"]]), 3)
        self.assertEqual(ledger.workers()["abc-1"]["session"], "sid-9")

    def test_a_refused_resume_registration_does_not_fail_the_spawn(self):
        err = {"error": {"code": "invalid_resume_argv", "message": "resume_argv must not contain apostrophes"}}
        self.scenario([
            {"match": ["agent", "get", "w9:p1"], "stdout": {"result": {"agent": {"agent_session": {"value": "sid-1"}}}}},
            {"match": ["pane", "report-agent-session"], "stdout": "", "stderr": json.dumps(err), "exit": 1},
        ])
        self.assertEqual(self.run_spawn()["name"], "abc-1")
        w = ledger.workers()["abc-1"]
        self.assertEqual((w["state"], w["resume"]), ("live", "failed: invalid_resume_argv"))
        self.assertIn(["agent", "prompt"], [c[:2] for c in logged_calls(self.tmp.name)])

    def test_no_session_id_yet_skips_the_resume_registration(self):
        self.scenario()
        self.run_spawn()
        self.assertNotIn(["pane", "report-agent-session"], [c[:2] for c in logged_calls(self.tmp.name)])
        self.assertIsNone(ledger.workers()["abc-1"].get("resume"))
        self.assertTrue(ledger.workers()["abc-1"]["claude_args"])

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
        for i in range(8):
            ledger.append("spawn", f"w{i}", pane=f"w{i}:p1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn(force=True)
        self.assertIn("hard cap is 8", str(ctx.exception))

    def test_force_allows_an_eighth_worker(self):
        self.scenario()
        for i in range(7):
            ledger.append("spawn", f"w{i}", pane=f"w{i}:p1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("the cap is 4 (use --force to go up to 8)", str(ctx.exception))
        self.assertEqual(self.run_spawn(force=True)["name"], "abc-1")

    def test_the_configured_cap_never_passes_the_hard_cap(self):
        self.assertEqual(paths.max_workers(), 4)
        os.environ["ATHENA_MAX_WORKERS"] = "7"
        self.assertEqual(paths.max_workers(), 7)
        os.environ["ATHENA_MAX_WORKERS"] = "12"
        self.assertEqual(paths.max_workers(), 8)
        self.assertEqual(spawn.HARD_CAP, paths.HARD_CAP)

    def test_spawn_ignores_quota_state(self):
        # An install from before the quota gate was removed: its config.json still names the
        # herdr-agent-quota state file, and that file says the 5-hour window is 99% used.
        self.scenario()
        home = Path(self.tmp.name) / "home"
        self.env.set("HOME", str(home))
        state = home / ".local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"fetched_at_unix": time.time(),
                                     "windows": [{"kind": "five_hour", "used_percent": 99}]}))
        config.config_file().parent.mkdir(parents=True, exist_ok=True)
        config.config_file().write_text(json.dumps({"version": 1, "quota_file": str(state)}))
        self.assertEqual(self.run_spawn()["name"], "abc-1")

    def test_prompt_failure_leaves_worker_tracked(self):
        err = {"error": {"code": "agent_not_found", "message": "gone"}}
        self.scenario([{"match": ["agent", "prompt"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn()
        self.assertIn("athena resume abc-1", str(ctx.exception))
        w = ledger.workers()["abc-1"]
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
        wt = paths.worktree_path(self.repo, "you/abc-1-fix")
        self.assertFalse(paths.claim_file(str(wt)).exists())
        w = ledger.workers()["abc-1"]
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["pane"], "w9:p1")
        self.assertEqual(ledger.live(), [])

    def test_startup_prompt_is_not_a_failure(self):
        err = {"error": {"code": "agent_not_ready", "message": "blocked during startup"}}
        self.scenario([{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        result = self.run_spawn()
        self.assertTrue(result["pending"])
        wt = paths.worktree_path(self.repo, "you/abc-1-fix")
        self.assertFalse(paths.claim_file(str(wt)).exists())
        self.assertEqual([w["name"] for w in ledger.live()], ["abc-1"])

    def test_claim_written_before_worktree_create(self):
        wt = paths.worktree_path(self.repo, "you/abc-1-fix")
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

    def claude_args_passed(self):
        start = [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "start"]][0]
        return start[start.index("--") + 1:]

    def test_model_and_effort_default_to_opus_high(self):
        self.scenario()
        result = self.run_spawn()
        claude = self.claude_args_passed()
        self.assertEqual((claude[claude.index("--model") + 1], claude[claude.index("--effort") + 1]), ("opus", "high"))
        self.assertEqual((result["model"], result["effort"]), ("opus", "high"))

    def test_model_and_effort_come_from_the_config(self):
        config.write({**config.DEFAULTS, "model": "sonnet", "effort": "medium"})
        self.scenario()
        self.run_spawn()
        claude = self.claude_args_passed()
        self.assertEqual((claude[claude.index("--model") + 1], claude[claude.index("--effort") + 1]),
                         ("sonnet", "medium"))

    def test_a_reapproval_repo_tells_its_worker(self):
        config.write({**config.DEFAULTS, "reapproval_repos": [self.repo.name]})
        self.scenario()
        self.run_spawn()
        self.assertEqual(self.settings_passed()["env"]["ATHENA_REAPPROVAL"], "1")

    def test_other_repos_carry_no_reapproval_flag(self):
        config.write({**config.DEFAULTS, "reapproval_repos": ["elsewhere"]})
        self.scenario()
        self.run_spawn()
        self.assertNotIn("ATHENA_REAPPROVAL", self.settings_passed()["env"])

    def test_settings_without_ultracode_are_unchanged(self):
        brief = paths.ensure("briefs") / "abc-1.md"
        text = spawn._write_settings("abc-1", brief).read_text()
        expected = {
            "env": {"ATHENA_WORKER": "abc-1", "ATHENA_HQ": paths.hq_name(), "ATHENA_STATE_DIR": str(paths.state_dir()),
                    "ATHENA_BRIEF": str(brief)},
            "crossSessionInbound": "accept",
            "remoteControlAtStartup": False,
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
        self.assertEqual(settings["env"]["ATHENA_WORKER"], "abc-1")
        w = ledger.workers()["abc-1"]
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
        w = ledger.workers()["abc-1"]
        self.assertTrue(w["ultracode"])
        self.assertNotIn("workflow_size", w)

    def test_no_ultracode_keys_without_the_flag(self):
        self.scenario()
        result = self.run_spawn()
        self.assertNotIn("ultracode", self.settings_passed())
        self.assertNotIn("ultracode", ledger.workers()["abc-1"])
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
            self.run_spawn(name="ABC-1")
        with self.assertRaises(spawn.SpawnError) as ctx:
            self.run_spawn(name="dash-queue")  # athena watch's doorbell line starts with it
        self.assertIn("reserved", str(ctx.exception))
        ledger.append("spawn", "abc-1", pane="w1:p1")
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn()


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name, "proj")
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text(helpers.BRIEF)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([str(helpers.ROOT / "bin" / "athena"), *args], capture_output=True, text=True,
                              env=dict(os.environ))

    def spawn_args(self, *extra):
        return ("spawn", "--repo", str(self.repo), "--branch", "you/abc-1-fix", "--name", "abc-1",
                "--brief", str(self.brief), "--goal", "ABC-1 is done when x.", *extra)


class UltracodeCliTest(CliBase):
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
        ledger.append("spawn", "abc-2", pane="w2:p1")
        ledger.append("spawn", "abc-3", pane="w3:p1", ultracode=True)
        lines = {line.split()[0]: line for line in self.cli("status").stdout.splitlines() if line[:1].strip()}
        self.assertIn("  UC:medium", lines["abc-1"])
        self.assertIn("  UC", lines["abc-3"])
        self.assertNotIn("UC:", lines["abc-3"])
        self.assertNotIn("UC", lines["abc-2"])
        rows = {r["name"]: r for r in json.loads(self.cli("status", "--json").stdout)}
        self.assertEqual((rows["abc-1"]["ultracode"], rows["abc-1"]["workflow_size"]), (True, "medium"))
        self.assertFalse(rows["abc-2"]["ultracode"])


if __name__ == "__main__":
    unittest.main()


class SpaceLabelTest(unittest.TestCase):
    def test_title_alone(self):
        self.assertEqual(spawn.space_label("abc-103", "Settings dialog keeps its scroll position", "ABC-103"),
                         "Settings dialog keeps its scroll position")

    def test_ticket_id_stripped_from_title(self):
        for title in ("ABC-103: Fix the dialog", "abc-103 Fix the dialog", "[ABC-103] Fix the dialog",
                      "Fix the dialog (ABC-103)", "Fix the dialog · abc-103"):
            self.assertEqual(spawn.space_label("abc-103", title, "ABC-103"), "Fix the dialog", title)

    def test_other_hyphenated_words_kept(self):
        self.assertEqual(spawn.space_label("abc-104", "Sign-in page re-renders twice", "ABC-104"),
                         "Sign-in page re-renders twice")

    def test_branch_slug_without_title(self):
        self.assertEqual(spawn.space_label("abc-103", None, "ABC-103",
                                           "you/abc-103-settings-scroll-reset"), "settings scroll reset")

    def test_name_is_last_resort(self):
        self.assertEqual(spawn.space_label("abc-1", "ABC-1", "ABC-1", "abc-1"), "abc-1")

    def test_truncated(self):
        self.assertEqual(len(spawn.space_label("abc-1", "x" * 80)), 48)


class SetupEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_setup_script_runs_without_force_hyperlink(self):
        # HQ's settings set FORCE_HYPERLINK for Claude Code; a setup script's tools must not print OSC 8 into its log.
        from unittest import mock
        root = Path(self.tmp.name) / "plugin"
        (root / "repos").mkdir(parents=True)
        (root / "repos" / "app.setup").write_text('echo "force=${FORCE_HYPERLINK-unset} repo=$ATHENA_REPO"\n')
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        with mock.patch.object(paths, "PLUGIN_ROOT", root), mock.patch.dict(os.environ, {"FORCE_HYPERLINK": "1"}):
            log = spawn._run_setup("/src/app", wt, "w1")
        self.assertEqual(Path(log).read_text().strip(), "force=unset repo=/src/app")

    def test_the_users_own_setup_script_wins_over_the_plugins(self):
        from unittest import mock
        root = Path(self.tmp.name) / "plugin"
        (root / "repos").mkdir(parents=True)
        (root / "repos" / "app.setup").write_text("echo plugin\n")
        mine = config.config_dir() / "repos"
        mine.mkdir(parents=True)
        (mine / "app.setup").write_text("echo mine\n")
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            log = spawn._run_setup("/src/app", wt, "w1")
            self.assertEqual(Path(log).read_text().strip(), "mine")
            self.assertIsNone(spawn._run_setup("/src/other", wt, "w2"))

    def test_the_plugin_ships_only_the_example_setup(self):
        self.assertEqual(sorted(p.name for p in (paths.PLUGIN_ROOT / "repos").iterdir()), ["example.setup"])


class BuildGoalTest(unittest.TestCase):
    def setUp(self):
        self.env = helpers.EnvPatch({"ATHENA_HQ": "athena"})

    def tearDown(self):
        self.env.restore()

    def test_the_suffix_is_short_and_any_report_status_meets_the_goal(self):
        # 70 of 204 workers lost their /goal once this suffix grew: it leaves the condition room under GOAL_LIMIT.
        brief = "/Users/someone/.local/state/athena/briefs/abc-1234.md"
        goal = spawn.build_goal("Open the PR", "abc-1234", brief)
        suffix = goal[len("/goal Open the PR"):]
        self.assertIn(brief, suffix)
        self.assertLessEqual(len(suffix), 210)
        # A BLOCKED report with a question ends the turn: without this clause the worker loops until someone clears it.
        # These are the words the evaluator has called met on a BLOCKED report; nothing stops a not-met loop.
        self.assertIn("The goal is met once an ATHENA-REPORT line is written, whatever its status.", suffix)
        self.assertIn("Out of scope: merging, approvals, undrafting, unfixable checks.", suffix)

    def test_a_long_goal_is_built_whole_so_the_length_check_sees_it(self):
        goal = spawn.build_goal("x" * 5000, "abc-1", "/briefs/abc-1.md")
        self.assertIn("x" * 5000, goal)
        self.assertTrue(goal.endswith("unfixable checks."))

    def test_nothing_still_says_the_goal_names_hq(self):
        # The suffix no longer carries HQ's name: a worker finds it in its environment.
        worker = (paths.PLUGIN_ROOT / "worker.md").read_text()
        self.assertNotIn("named in your goal", worker)
        self.assertIn("ATHENA_HQ", worker)


class GoalTemplateTest(unittest.TestCase):
    """The goal HQ is told to write has to arrive as a /goal, whatever worker it is for."""

    def condition(self, name="ABC-1234"):
        skill = (paths.PLUGIN_ROOT / "skills" / "athena" / "SKILL.md").read_text()
        template = re.search(r'--goal "(<NAME> is done when[^"]+)"', skill).group(1)
        return template.replace("<NAME>", name).replace("<TIMEBOX>", "2 hours or 150 turns")

    def test_a_goal_from_the_template_comes_under_the_limit(self):
        brief = "/Users/someone/.local/state/athena/briefs/abc-1234.md"
        self.assertLessEqual(len(spawn.build_goal(self.condition(), "abc-1234", brief)), spawn.GOAL_LIMIT)

    def test_it_stays_under_with_the_longest_worker_name(self):
        name = "a" * 32
        brief = f"/Users/someone/.local/state/athena/briefs/{name}.md"
        self.assertLessEqual(len(spawn.build_goal(self.condition(name), name, brief)), spawn.GOAL_LIMIT)

    def test_the_template_leaves_room_for_a_line_of_its_own(self):
        brief = "/Users/someone/.local/state/athena/briefs/abc-1234.md"
        room = spawn.GOAL_LIMIT - len(spawn.build_goal(self.condition(), "abc-1234", brief))
        self.assertGreaterEqual(room, 150)

    def test_the_template_passes_the_goal_check(self):
        self.assertEqual(briefs.check_goal(self.condition()), [])

    def test_a_resend_is_not_sent_to_nudge(self):
        # A nudge drops what spawn adds and has no limit, so a long one pastes again.
        skill = (paths.PLUGIN_ROOT / "skills" / "athena" / "SKILL.md").read_text()
        limit = skill[skill.index("The whole `/goal` has to fit"):]
        self.assertIn("athena resume", limit[:limit.index("\n")])

    def test_the_docs_state_the_limit(self):
        for doc in ("skills/athena/SKILL.md", "docs/commands.md"):
            self.assertIn(f"{spawn.GOAL_LIMIT} characters", (paths.PLUGIN_ROOT / doc).read_text(), doc)


class GoalLimitTest(SpawnBase):
    """Over the limit Claude Code takes the text as a paste: /goal never runs, and the worker just gets text."""

    def goal_of_length(self, length):
        overhead = len(spawn.build_goal("", "abc-1", paths.state_dir() / "briefs" / "abc-1.md"))
        return "x" * (length - overhead)

    def test_a_goal_over_the_limit_is_refused_naming_both_lengths(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError) as caught:
            self.run_spawn(goal=self.goal_of_length(spawn.GOAL_LIMIT + 1))
        self.assertIn(str(spawn.GOAL_LIMIT + 1), str(caught.exception))
        self.assertIn(str(spawn.GOAL_LIMIT), str(caught.exception).replace(str(spawn.GOAL_LIMIT + 1), ""))

    def test_the_refusal_comes_before_anything_is_made(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(goal=self.goal_of_length(spawn.GOAL_LIMIT + 1))
        self.assertEqual(logged_calls(self.tmp.name), [])
        self.assertFalse(paths.worktree_path(self.repo, "you/abc-1-fix").exists())
        self.assertNotIn("abc-1", ledger.workers())

    def test_skipping_the_brief_check_does_not_skip_the_limit(self):
        self.scenario()
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(goal=self.goal_of_length(spawn.GOAL_LIMIT + 1), brief_check=False)

    def test_a_goal_at_the_limit_is_sent_whole(self):
        self.scenario()
        self.run_spawn(goal=self.goal_of_length(spawn.GOAL_LIMIT))
        prompt = next(c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"])
        self.assertEqual(len(prompt[3]), spawn.GOAL_LIMIT)


GOAL_STATUS = {"type": "attachment", "attachment": {"type": "goal_status", "met": False, "condition": "x"}}
GOAL_SET = {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Goal set: x</local-command-stdout>"}}
# How a goal that was typed too long arrives, and a brief read later: both name the markers, neither is a goal.
PASTED = {"type": "user", "message": {"role": "user", "content": "<pasted_content>/goal x; Goal set: goal_status</pasted_content>"}}
TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "content": "<local-command-stdout>Goal set: {\"type\": \"goal_status\"}"}]}}


class GoalConfirmTest(SpawnBase):
    """herdr raising no error and Claude starting to work say nothing: a pasted prompt starts Claude working too."""

    SESSION = {"match": ["agent", "get"], "stdout": {"result": {"agent": {"agent_session": {"value": "sess-1"}}}}}

    def transcript(self, *records, session="sess-1"):
        path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-some-worktree" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def spawned(self):
        return ledger.workers()["abc-1"]

    def hook(self, session):
        (paths.ensure("workers") / "abc-1.json").write_text(json.dumps({"name": "abc-1", "state": "working", "session_id": session}))

    def ready(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {
            "name": "abc-1", "pane_id": "w9:p1", "agent_status": "idle"}}}}])

    def pending_worker(self, **fields):
        ledger.append("spawn", "abc-1", pane="w9:p1", goal_pending="/goal x", **fields)
        self.ready()

    def failed_spawn(self, code, name="abc-1"):
        """A spawn whose goal send got this error from herdr, so spawn raised after the worker was tracked."""
        self.scenario([self.SESSION, {"match": ["agent", "prompt"], "stdout": "", "exit": 1, "times": 1,
                                      "stderr": json.dumps({"error": {"code": code, "message": "x"}})}])
        with self.assertRaises(spawn.SpawnError):
            self.run_spawn(name=name)

    def test_a_goal_status_record_confirms_the_goal(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED, GOAL_STATUS)
        result = self.run_spawn()
        self.assertEqual(result["goal_state"], "confirmed")
        self.assertEqual(self.spawned()["goal_state"], "confirmed")

    def test_a_goal_set_record_confirms_the_goal(self):
        self.scenario([self.SESSION])
        self.transcript(GOAL_SET)
        self.assertEqual(self.run_spawn()["goal_state"], "confirmed")

    def test_a_pasted_goal_is_unconfirmed(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED, TOOL_RESULT)
        self.assertEqual(self.run_spawn()["goal_state"], "unconfirmed")
        self.assertEqual(self.spawned()["goal_state"], "unconfirmed")

    def test_no_transcript_is_unconfirmed(self):
        self.scenario([self.SESSION])
        self.assertEqual(self.run_spawn()["goal_state"], "unconfirmed")

    def test_no_session_id_from_herdr_falls_back_to_the_hook(self):
        self.scenario()
        self.transcript(GOAL_STATUS, session="sess-hook")
        hook = paths.ensure("workers") / "abc-1.json"
        real = spawn._set_aside_status  # a hook event that lands after the new Claude starts
        def and_the_hook_reports(name):
            real(name)
            hook.write_text(json.dumps({"name": "abc-1", "state": "working", "session_id": "sess-hook"}))
        with mock.patch.object(spawn, "_set_aside_status", and_the_hook_reports):
            self.assertEqual(self.run_spawn()["goal_state"], "confirmed")

    def test_it_waits_for_the_goal_to_land(self):
        self.scenario([self.SESSION])
        self.env.set("ATHENA_GOAL_WAIT", "5")
        calls = []
        real = status.goal_took
        def lands_on_the_third_look(name, session=None):
            calls.append(name)
            if len(calls) == 3:
                self.transcript(GOAL_STATUS)
            return real(name, session)
        with mock.patch.object(status, "goal_took", lands_on_the_third_look), mock.patch.object(spawn.time, "sleep"):
            self.assertEqual(self.run_spawn()["goal_state"], "confirmed")
        self.assertEqual(len(calls), 3)

    def test_status_says_goal_unconfirmed(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.assertTrue(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_status_clears_once_the_goal_shows_up(self):
        # Someone pressed enter on a send left in the input box, or HQ sent a shorter goal.
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.transcript(PASTED, GOAL_STATUS)
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_status_says_nothing_of_a_confirmed_goal(self):
        self.scenario([self.SESSION])
        self.transcript(GOAL_STATUS)
        self.run_spawn()
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_a_worker_from_before_this_check_is_not_flagged(self):
        ledger.append("spawn", "old-1", pane="w2:p1", session="sess-1")
        self.transcript(PASTED)
        self.assertFalse(status.collect(ledger.workers()["old-1"], with_herdr=False)["goal_unconfirmed"])

    def test_a_goal_sent_after_a_startup_prompt_is_checked_too(self):
        ledger.append("spawn", "abc-1", pane="w9:p1", goal_pending="/goal x", session="sess-1")
        write_scenario(self.tmp.name, [{"match": ["agent", "get"], "stdout": {"result": {"agent": {
            "name": "abc-1", "pane_id": "w9:p1", "agent_status": "idle"}}}}])
        self.assertEqual(spawn.deliver("abc-1")["outcome"], "sent")
        self.assertTrue(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])
        self.transcript(GOAL_STATUS)
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])


    def test_a_goal_seen_once_stays_confirmed_after_a_clear(self):
        # A /clear moves the worker to a new transcript, which holds no goal record.
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.transcript(PASTED, GOAL_STATUS)
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])
        self.assertEqual(self.spawned()["goal_state"], "confirmed")
        self.hook("sess-after-clear")
        self.transcript(PASTED, session="sess-after-clear")
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_a_pending_path_worker_is_not_flagged_after_a_clear_nobody_looked_before(self):
        # It reaches the ledger with no session: delivery keeps the one its goal went to.
        self.pending_worker()
        self.hook("sess-1")
        self.transcript(GOAL_STATUS)
        spawn.deliver("abc-1")
        self.hook("sess-after-clear")
        self.transcript(PASTED, session="sess-after-clear")
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_a_goal_that_never_ran_stays_flagged_after_a_clear(self):
        self.pending_worker()
        self.hook("sess-1")
        self.transcript(PASTED)
        spawn.deliver("abc-1")
        self.hook("sess-after-clear")
        self.transcript(PASTED, session="sess-after-clear")
        self.assertTrue(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_resume_resends_the_checked_text_of_an_unconfirmed_goal(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        sent = next(c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"])[3]
        self.ready()
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")
        prompts = [c[3] for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"]]
        self.assertEqual(prompts, [sent, sent])
        self.assertLessEqual(len(sent), spawn.GOAL_LIMIT)

    def test_resume_resends_a_goal_whose_send_failed(self):
        self.scenario([self.SESSION, {"match": ["agent", "prompt"], "stdout": "", "exit": 1, "times": 1,
                                      "stderr": json.dumps({"error": {"code": "timeout", "message": "x"}})}])
        with self.assertRaises(spawn.SpawnError) as caught:
            self.run_spawn()
        self.assertIn("athena resume abc-1", str(caught.exception))
        self.assertNotIn("athena nudge", str(caught.exception))
        self.ready()
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")

    def test_resume_does_not_resend_a_goal_just_sent(self):
        # It lands a few seconds after the send: a second resume in that time would type it twice.
        self.pending_worker()
        ledger.append("update", "abc-1", goal_text="/goal x")
        self.env.set("ATHENA_GOAL_WAIT", "60")
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")

    def test_resume_refuses_a_goal_that_took(self):
        self.scenario([self.SESSION])
        self.transcript(GOAL_STATUS)
        self.run_spawn()
        self.ready()
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")

    def test_resume_does_not_resend_a_goal_that_showed_up_since(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.transcript(PASTED, GOAL_STATUS)
        self.ready()
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")

    def refused_prompt(self, code):
        """The worker reads as ready, and herdr answers its next prompt with this error."""
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get"], "stdout": {"result": {"agent": {"name": "abc-1", "pane_id": "w9:p1", "agent_status": "idle"}}}},
            {"match": ["agent", "prompt"], "stdout": "", "exit": 1, "times": 1,
             "stderr": json.dumps({"error": {"code": code, "message": "x"}})}])

    def prompts(self):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"]]

    def test_a_refused_resume_never_leaves_a_goal_for_the_watch_to_send(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.refused_prompt("agent_not_ready")
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")
        self.assertFalse(self.spawned().get("goal_pending"))
        before = len(self.prompts())
        self.assertEqual(spawn.deliver_pending(), [])
        self.assertEqual(len(self.prompts()), before)

    def test_a_refused_resume_can_be_retried_at_once(self):
        # herdr refused it before typing anything, so nothing is on its way.
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.refused_prompt("agent_not_ready")
        self.env.set("ATHENA_GOAL_WAIT", "60")
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")

    def test_a_resend_herdr_may_have_typed_is_not_sent_again_at_once(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.refused_prompt("timeout")
        self.env.set("ATHENA_GOAL_WAIT", "60")
        self.assertEqual(spawn.resume("abc-1")["outcome"], "unconfirmed")
        before = len(self.prompts())
        with self.assertRaises(spawn.SpawnError):
            spawn.resume("abc-1")
        self.assertEqual(len(self.prompts()), before)

    def test_a_pending_send_that_times_out_is_flagged_and_can_be_resent(self):
        self.pending_worker(goal_text="/goal x")
        self.refused_prompt("timeout")
        self.assertEqual(spawn.deliver("abc-1")["outcome"], "unconfirmed")
        self.assertEqual(self.spawned()["goal_state"], "unconfirmed")
        self.assertTrue(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])
        self.ready()
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")
        self.assertEqual(self.prompts()[-1][3], "/goal x")

    def test_a_send_that_failed_at_spawn_is_flagged(self):
        self.failed_spawn("timeout")
        self.assertEqual(self.spawned()["goal_state"], "unconfirmed")
        self.assertTrue(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def bad_resume_then_a_good_one(self, code):
        self.failed_spawn("timeout")
        self.refused_prompt(code)
        try:
            spawn.resume("abc-1")
        except spawn.SpawnError:
            pass
        self.assertEqual(self.spawned()["goal_state"], "unconfirmed")
        self.ready()
        self.assertEqual(spawn.resume("abc-1")["outcome"], "sent")

    def test_a_resume_that_times_out_does_not_close_resend(self):
        self.bad_resume_then_a_good_one("timeout")

    def test_a_resume_herdr_refused_does_not_close_resend(self):
        self.bad_resume_then_a_good_one("agent_not_ready")

    def test_a_first_send_herdr_refused_is_pending_not_unconfirmed(self):
        # Nothing was typed, so the goal is simply still waiting.
        self.pending_worker(goal_text="/goal x")
        self.refused_prompt("agent_not_ready")
        self.assertEqual(spawn.deliver("abc-1")["outcome"], "waiting")
        self.assertEqual(self.spawned()["goal_pending"], "/goal x")
        self.assertFalse(self.spawned().get("goal_state"))
        self.assertFalse(status.collect(self.spawned(), with_herdr=False)["goal_unconfirmed"])

    def test_a_dry_run_handoff_writes_nothing_when_a_goal_has_shown_up(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.transcript(PASTED, GOAL_STATUS)
        before = list(ledger.events())
        handoff.build(dry_run=True)
        self.assertEqual(list(ledger.events()), before)
        self.assertEqual(self.spawned()["goal_state"], "unconfirmed")

    def test_the_watch_never_resends_on_its_own(self):
        self.scenario([self.SESSION])
        self.transcript(PASTED)
        self.run_spawn()
        self.ready()
        self.assertEqual(spawn.deliver_pending(), [])
        self.assertEqual(spawn.deliver("abc-1")["outcome"], "not-pending")


class GoalUnconfirmedCliTest(CliBase):
    def test_status_prints_goal_unconfirmed(self):
        ledger.append("spawn", "abc-2", pane="w2:p1", goal_state="unconfirmed")
        ledger.append("spawn", "abc-3", pane="w3:p1", goal_state="confirmed")
        lines = {line.split()[0]: line for line in self.cli("status").stdout.splitlines() if line[:1].strip()}
        self.assertIn("goal unconfirmed", lines["abc-2"])
        self.assertNotIn("goal unconfirmed", lines["abc-3"])
        rows = {r["name"]: r for r in json.loads(self.cli("status", "--json").stdout)}
        self.assertTrue(rows["abc-2"]["goal_unconfirmed"])
        self.assertFalse(rows["abc-3"]["goal_unconfirmed"])

    def test_status_of_one_worker_says_goal_unconfirmed(self):
        ledger.append("spawn", "abc-2", pane="w2:p1", goal_state="unconfirmed")
        ledger.append("spawn", "abc-3", pane="w3:p1", goal_state="confirmed")
        self.assertIn("goal unconfirmed", self.cli("status", "abc-2").stdout)
        self.assertNotIn("goal unconfirmed", self.cli("status", "abc-3").stdout)

    def test_spawn_warns_when_its_goal_is_unconfirmed(self):
        write_scenario(self.tmp.name, [{"match": ["worktree", "create"], "stdout": CREATED}])
        out = self.cli(*self.spawn_args())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["goal_state"], "unconfirmed")
        self.assertIn("goal unconfirmed", out.stderr)

    def test_spawn_refuses_an_over_long_goal_with_a_nonzero_exit(self):
        args = list(self.spawn_args())
        args[args.index("--goal") + 1] = "x" * 2000
        out = self.cli(*args)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(str(spawn.GOAL_LIMIT), out.stderr)
