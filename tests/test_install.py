"""The installer end to end in an isolated HOME: real copies, real links, the command really run; launchd is a stub."""
import json
import os
import shutil
import subprocess

from harness import BIN, Case, ROOT

from athena_rex.caps import install, profiles, spawn
from athena_rex.kernel import caps, config, lab


class Install(Case):
    def setUp(self):
        super().setUp()
        self.local = self.home / ".local"
        self.bin = self.local / "bin" / "athena-rex"
        os.environ.pop("ATHENA_STATE_DIR", None)
        os.environ["PATH"] = f"{self.local / 'bin'}:{os.environ['PATH']}"
        self.state_dir = self.home / "Library/Application Support/athena"
        self.stub("rex", "rex version 1")
        os.environ["ATHENA_REX_BIN"] = str(self.stubs / "rex")

    def run_install(self, *args):
        done = self.cli("install", *args)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def installed(self, *args):
        return subprocess.run([str(self.bin), *args], capture_output=True, text=True, env=os.environ, timeout=60)

    def test_fresh_install_runs_from_anywhere_and_writes_nothing_in_claude(self):
        out = self.run_install()
        self.assertIn("Installed.", out)
        done = subprocess.run([str(self.bin), "--help"], capture_output=True, text=True, cwd=self.tmp, env=os.environ)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Commands:", done.stdout)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / "Library/LaunchAgents").exists())
        self.assertEqual(self.installed("install", "--check").returncode, 0)

    def test_a_fresh_install_opens_the_door_for_its_own_state_only(self):
        self.run_install()
        self.assertIs(json.loads((self.state_dir / "config.json").read_text())["rex"]["real_world"], True)

    def test_paths_it_hands_out_go_through_current_and_outlive_the_release(self):
        self.run_install()
        code = "from athena_rex.kernel import store; from athena_rex.caps import hooks, spawn; print(store.ROOT); print(hooks.settings_block()['Stop'][0]['hooks'][0]['command']); print(spawn.PROMPTS)"
        done = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", f"import sys; sys.path.insert(0, {str(self.local / 'share/athena-rex/current')!r})\n{code}"],
                              capture_output=True, text=True, env=os.environ)
        self.assertEqual(done.returncode, 0, done.stderr)
        for line in done.stdout.splitlines():
            self.assertIn("/current", line)
            self.assertNotIn("/releases/", line)

    def test_every_plugin_dir_it_passes_is_in_the_release(self):
        """HQ's skill and the workers' band load from --plugin-dir: a folder the release left out loads nothing, and says nothing."""
        self.run_install()
        current = self.local / "share/athena-rex/current"
        self.assertTrue((current / "plugin/skills/athena-rex/SKILL.md").is_file())
        code = ("import json; from athena_rex.caps import hq, spawn\n"
                "args = hq.claude_args() + spawn.claude_args('w', 'opus', 'high', 'settings.json')\n"
                "print(json.dumps([args[i + 1] for i, a in enumerate(args) if a == '--plugin-dir']))")
        done = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", f"import sys; sys.path.insert(0, {str(current)!r})\n{code}"],
                              capture_output=True, text=True, env={**os.environ, "ATHENA_STATE_DIR": str(self.state_dir)})
        self.assertEqual(done.returncode, 0, done.stderr)
        dirs = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual(len(dirs), 2, f"HQ's and a worker's: {dirs}")
        for folder in dirs:
            self.assertIn("/current/", folder)
            self.assertTrue(os.path.isdir(folder), f"{folder} is not in the release")
            self.assertTrue(any(os.scandir(folder)), f"{folder} is empty")

    def test_install_over_the_top_of_itself_changes_nothing(self):
        self.run_install()
        current = os.readlink(self.local / "share/athena-rex/current")
        config_before = (self.state_dir / "config.json").read_bytes()
        out = self.run_install()
        self.assertEqual(os.readlink(self.local / "share/athena-rex/current"), current)
        self.assertEqual((self.state_dir / "config.json").read_bytes(), config_before)
        self.assertIn(f"= release {os.path.basename(current)}", out)

    def test_an_update_swaps_the_link_and_keeps_one_to_go_back_to(self):
        first_source = self.tmp / "first"
        shutil.copytree(ROOT, first_source, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__"))
        (first_source / "docs" / "new.md").write_text("changed\n")
        first = subprocess.run([str(first_source / "bin/athena-rex"), "install"], capture_output=True, text=True, env=os.environ)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        one = os.readlink(self.local / "share/athena-rex/current")
        self.run_install()
        two = os.readlink(self.local / "share/athena-rex/current")
        self.assertNotEqual(one, two)
        self.assertEqual(self.installed("install", "--rollback").returncode, 0)
        self.assertEqual(os.readlink(self.local / "share/athena-rex/current"), one)
        self.assertEqual(self.installed("--help").returncode, 0)

    def test_an_update_shows_what_changed_in_a_profile(self):
        self.run_install()
        settings = json.loads((self.state_dir / "config.json").read_text())
        settings["rex"]["grants"] = {"worker": ["Bash(make lint:*)"]}
        (self.state_dir / "config.json").write_text(json.dumps(settings))
        out = self.run_install()
        self.assertIn("permission profile worker changed", out)
        self.assertIn('+  "Bash(make lint:*)"', out.replace('+ "', '+  "').replace("+  \"", "+  \""))

    def test_someone_elses_launcher_stops_it_and_force_keeps_a_copy(self):
        self.bin.parent.mkdir(parents=True)
        self.bin.write_text("#!/bin/sh\necho mine\n")
        done = self.cli("install")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("isn't ours", done.stderr)
        self.assertEqual(self.bin.read_text(), "#!/bin/sh\necho mine\n")
        self.assertFalse((self.local / "share/athena-rex").exists())
        self.run_install("--force")
        kept = list(self.bin.parent.glob("athena-rex.before-install.*"))
        self.assertEqual(kept[0].read_text(), "#!/bin/sh\necho mine\n")

    def test_state_that_lives_elsewhere_is_left_alone(self):
        old = self.home / ".local/state/athena"
        old.mkdir(parents=True)
        (old / "ledger.jsonl").write_text("")
        out = self.run_install()
        self.assertIn("Move it here", out)
        self.assertFalse(self.state_dir.exists())

    def test_uninstall_removes_what_it_made_and_leaves_state(self):
        self.run_install()
        (self.state_dir / "ledger.jsonl").write_text("{}\n")
        done = self.installed("install", "--uninstall")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.local / "share/athena-rex").exists())
        self.assertEqual((self.state_dir / "ledger.jsonl").read_text(), "{}\n")
        self.assertIn("State stays", done.stdout)

    def test_services_go_through_launchctl_and_uninstall_takes_them_out(self):
        out = self.run_install("--services")
        self.assertIn("wrote", out)
        agents = self.tmp / "agents"
        jobs = {p.name for p in agents.iterdir()}
        self.assertEqual(jobs, {"athena-rex.dash.plist", "athena-rex.doctor.plist", "athena-rex.menubar.plist", "athena-rex.watch.plist"})
        self.assertIn("/current/bin/athena-rex", (agents / "athena-rex.doctor.plist").read_text())
        self.assertTrue(any("launchctl bootstrap" in c for c in self.world_calls()))
        self.assertEqual(self.installed("install", "--uninstall").returncode, 0)
        self.assertEqual(list(agents.iterdir()), [])

    def test_an_unmarked_home_is_refused_while_a_dry_run_only_looks(self):
        (self.home / lab.SCRATCH_MARK).unlink()
        done = self.cli("install")
        self.assertEqual(done.returncode, 2)
        self.assertIn("scratch HOME", done.stderr)
        self.assertFalse(self.bin.exists())
        looked = self.cli("install", "--dry-run")
        self.assertEqual(looked.returncode, 0, looked.stderr)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.state_dir.exists())

    def test_the_guard_comes_out_of_the_installed_copy_only(self):
        before = (ROOT / "athena_rex/kernel/lab.py").read_bytes(), BIN.read_bytes()
        self.run_install("--lab-guard-out")
        release = (self.local / "share/athena-rex/current").resolve()
        self.assertTrue(release.name.endswith("-live"))
        self.assertEqual(((ROOT / "athena_rex/kernel/lab.py").read_bytes(), BIN.read_bytes()), before)
        self.assertNotIn("ATHENA_WORKER", (release / "bin/athena-rex").read_text())
        probe = "from athena_rex.kernel import lab; print(lab.active(), lab.LIVE_PATHS)"
        done = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", f"import sys; sys.path.insert(0, {str(release)!r})\n{probe}"], capture_output=True, text=True,
                              env={k: v for k, v in os.environ.items() if k != "ATHENA_CANARY"})
        self.assertEqual(done.stdout.strip(), "False ()", done.stderr)
        again = self.run_install()
        self.assertIn("-live", again, "an update keeps it out")

    def test_taking_the_guard_out_needs_stubs_in_the_lab(self):
        (self.stubs / "launchctl").unlink()
        done = self.cli("install", "--lab-guard-out", "--services")
        self.assertEqual(done.returncode, 2)
        self.assertIn("stubs", done.stderr)
        self.assertFalse((self.local / "share/athena-rex").exists())

    def test_a_flag_that_would_do_nothing_is_refused(self):
        self.run_install()
        current = os.readlink(self.local / "share/athena-rex/current")
        for args in (("--rollback", "--dry-run"), ("--check", "--services"), ("--uninstall", "--real-world"), ("--rollback", "--force"), ("--check", "--uninstall")):
            done = self.cli("install", *args)
            self.assertNotEqual(done.returncode, 0, args)
        self.assertEqual(os.readlink(self.local / "share/athena-rex/current"), current)
        self.assertTrue(self.bin.exists())

    def test_uninstall_stops_at_what_still_runs_on_this_install(self):
        self.run_install()
        with open(self.state_dir / "ledger.jsonl", "w") as f:
            f.write(json.dumps({"event": "spawn", "name": "w", "ts": 1, "backend": "fake", "space": "s", "block": "b", "path": str(self.tmp), "repo": str(self.tmp)}) + "\n")
        done = self.installed("install", "--uninstall")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("still running", done.stderr)
        self.assertTrue(self.bin.exists())
        self.assertEqual(self.installed("install", "--uninstall", "--force").returncode, 0)
        self.assertFalse(self.bin.exists())

    def test_jobs_it_could_not_remove_stop_the_uninstall(self):
        self.run_install("--services")
        (self.stubs / "launchctl").unlink()
        done = self.installed("install", "--uninstall")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("launchd jobs", done.stderr)
        self.assertTrue(self.bin.exists())

    def test_the_guard_stays_in_a_checkout(self):
        with self.assertRaisesRegex(Exception, "checkout"):
            (self.tmp / "co" / ".git").mkdir(parents=True)
            install.take_the_guard_out(self.tmp / "co")


class Profiles(Case):
    def test_no_role_has_a_deny_list_as_in_the_old_athena(self):
        worker = json.loads(spawn.settings_for("w", self.tmp / "b.md").read_text())
        self.assertEqual(worker["permissions"]["deny"], [])
        self.write_config(hq_skip_permissions=True, hq_allow=["Bash(ls:*)"])
        hq = caps.get("hq")
        args = hq.claude_args()
        self.assertIn("--dangerously-skip-permissions", args)
        settings = json.loads(open(args[args.index("--settings") + 1]).read())
        self.assertEqual(settings["permissions"]["deny"], [])
        self.assertIn("Bash(ls:*)", settings["permissions"]["allow"])

    def test_a_grant_is_a_rule_on_record(self):
        profiles.write_all()
        profiles.grant("worker", "Bash(aws s3 ls:*)")
        config.reset()
        self.assertIn("Bash(aws s3 ls:*)", profiles.permissions("worker")["allow"])
