"""`athena update`, end to end in an isolated HOME: a published repo of real releases, the installed copy really run, launchd a stub.
Every outcome is read from install.json, the explicit record, and from the `current` link it names."""
import json
import os
import shutil
import signal
import subprocess
import time
from unittest import mock

from harness import Case, ROOT, git

from athena.caps.install import releases


class Update(Case):
    def setUp(self):
        super().setUp()
        self.local = self.home / ".local"
        os.environ.pop("ATHENA_STATE_DIR", None)
        os.environ["PATH"] = f"{self.local / 'bin'}:{os.environ['PATH']}"
        os.environ["ATHENA_UPDATE_WAIT"] = "2"
        self.stub("rex", "rex version 1")
        os.environ["ATHENA_REX_BIN"] = str(self.stubs / "rex")
        self.published = self.tmp / "published"
        shutil.copytree(ROOT, self.published, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "build"))
        git(self.published.parent, "init", "-q", "-b", "main", str(self.published))
        self.release("1.0.0")
        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "-q", "--branch", "v1.0.0", str(self.published), str(checkout))
        done = subprocess.run([str(checkout / "bin/athena"), "install"], capture_output=True, text=True, env=os.environ, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["current"]["version"], "1.0.0")

    def release(self, version, change=None):
        """A commit on the published repo's main, tagged v<version>, whose VERSION says so."""
        (self.published / "VERSION").write_text(f"{version}\n")
        if change:
            change(self.published)
        git(self.published, "add", "-A")
        git(self.published, "commit", "-q", "-m", f"athena {version}")
        git(self.published, "tag", f"v{version}")

    def record(self) -> dict:
        return json.loads((self.local / "share/athena/install.json").read_text())

    def running(self) -> str:
        return os.path.basename(os.readlink(self.local / "share/athena/current"))

    def athena(self, *args, timeout=180):
        return subprocess.run([str(self.local / "bin/athena"), *args], capture_output=True, text=True, env=os.environ, timeout=timeout)

    def update(self, *args):
        return self.athena("update", "--repo", str(self.published), *args)

    def test_a_newer_release_installs_switches_and_passes_its_health_check(self):
        self.release("1.1.0")
        before = self.record()["current"]
        done = self.update()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        record = self.record()
        self.assertEqual(record["current"]["version"], "1.1.0")
        self.assertEqual(record["current"]["commit"], git(self.published, "rev-parse", "v1.1.0"))
        self.assertEqual(record["previous"], before, "the release it replaced is on record, to go back to")
        self.assertEqual(record["channel"], "release")
        self.assertEqual(record["repo"], str(self.published), "kept for next time")
        self.assertIsNone(record["switch"])
        self.assertEqual(record["last_update"]["result"], "updated")
        self.assertEqual(self.running(), record["current"]["release"])
        self.assertTrue((self.local / "share/athena/releases" / before["release"]).is_dir(), "the old release stays on disk")
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.1.0")
        self.assertEqual(self.athena("install", "--rollback").returncode, 0)
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.0.0")

    def test_a_failed_health_check_rolls_back_to_the_running_release(self):
        def break_a_check(tree):
            doctor = tree / "athena/caps/doctor.py"
            doctor.write_text(doctor.read_text().replace("def checks(ctx):\n", "def checks(ctx):\n    return [caps.Check('disk', False, 'broken in 1.1.0')]\n", 1))
        self.assertTrue(json.loads(self.athena("doctor", "--json").stdout)["checks"]["disk"]["ok"], "the check passes before")
        self.release("1.1.0", break_a_check)
        before = self.record()
        done = self.update()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("1.1.0 failed its health check: it fails what passed before: disk", done.stdout)
        record = self.record()
        self.assertEqual(record["current"], before["current"])
        self.assertEqual(record["previous"], before["previous"])
        self.assertIsNone(record["switch"])
        self.assertEqual(record["last_update"]["result"], "rolled back")
        self.assertEqual(self.running(), before["current"]["release"])
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.0.0")

    def test_already_the_latest_changes_nothing(self):
        before = self.record()
        done = self.update()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("athena 1.0.0 is the latest on release (v1.0.0)", done.stdout)
        record = self.record()
        self.assertEqual((record["current"], record["previous"]), (before["current"], before["previous"]))
        self.assertEqual(record["latest"]["version"], "1.0.0", "what it found is on record")
        self.assertEqual(self.running(), before["current"]["release"])
        self.assertEqual(sorted(p.name for p in (self.local / "share/athena/releases").iterdir()), [before["current"]["release"]])

    def test_an_older_version_is_never_installed_even_when_tagged_last(self):
        self.release("0.9.0")  # the newest commit and tag, holding an older version
        done = self.update()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("athena 1.0.0 is the latest on release (v1.0.0)", done.stdout)
        self.assertEqual(self.record()["current"]["version"], "1.0.0")

    def test_install_from_the_installed_copy_keeps_what_an_update_brought(self):
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        done = self.athena("install")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("the one running", done.stdout)
        self.assertEqual(self.record()["current"]["version"], "1.1.0", "never the older checkout it was first installed from")
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.1.0")

    def test_a_rollback_restarts_the_jobs_onto_the_release_it_names(self):
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        agents = self.tmp / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "local.athena.watch.plist").write_text("x")
        done = self.athena("install", "--rollback")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("no launchd jobs to restart", done.stdout)
        self.assertTrue(any(c.startswith("launchctl ") for c in self.world_calls()), "services restart reached launchd")

    def test_a_recovery_cut_off_by_its_own_restart_has_already_saved(self):
        """From the doctor job, the restart kills the doctor that runs the repair: the record must say so first."""
        from athena.caps.install import update
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        record = self.record()
        old, new = record["previous"], record["current"]
        (self.local / "share/athena/install.json").write_text(json.dumps(
            {**record, "current": old, "previous": None, "switch": {"from": old, "to": new, "since": time.time()}}))
        with mock.patch.object(releases, "restart_services", side_effect=SystemExit("killed by its own kickstart")):
            with self.assertRaises(SystemExit):
                update.recover(releases.load(), out=open(os.devnull, "w"))
        self.assertIsNone(self.record()["switch"], "no repair loop: the switch is off the record before the restart")
        self.assertEqual(self.running(), old["release"])

    def test_doctor_says_plainly_when_nothing_is_published_and_when_main_moved(self):
        from athena.caps.install import update
        current = {"release": "1.0.0-x", "version": "1.0.0", "commit": "a" * 40}
        line = update._latest_line({"current": current, "channel": "release", "latest": {"version": None, "ref": None, "checked": 1}})
        self.assertEqual(line, "athena 1.0.0; no release is published yet")
        line = update._latest_line({"current": {**current, "commit": None}, "channel": "main",
                                    "latest": {"version": None, "ref": "main", "commit": "b" * 40}})
        self.assertEqual(line, "athena 1.0.0; no commit on record for what runs, so main can't be compared: install from a clean checkout")
        line = update._latest_line({"current": current, "channel": "main", "latest": {"version": None, "ref": "main", "commit": "b" * 40}})
        self.assertEqual(line, f"athena 1.0.0; main is at {'b' * 10}, not fetched yet: `athena update --check` compares",
                         "a commit the clone lacks is unknown, never 'the latest'")

    def test_doctor_says_when_main_has_moved_ahead(self):
        head = self.commit_on_main("1.0.1")
        self.assertEqual(self.update("--channel", "main", "--check").returncode, 0)
        doctor = json.loads(self.athena("doctor", "--json").stdout)
        self.assertEqual(doctor["checks"]["update"]["detail"], f"athena 1.0.0; main at {head[:10]} is out: `athena update`")

    def commit_on_main(self, version):
        (self.published / "VERSION").write_text(f"{version}\n")
        git(self.published, "commit", "-q", "--allow-empty", "-am", f"main at {version}")
        return git(self.published, "rev-parse", "HEAD")

    def test_on_main_a_commit_ahead_on_the_same_line_installs(self):
        self.assertTrue(self.record()["current"]["commit"], "installed from a clean checkout: its commit is on record")
        head = self.commit_on_main("1.0.1")
        done = self.update("--channel", "main")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.record()["current"]["version"], self.record()["current"]["commit"]), ("1.0.1", head))

    def test_on_main_a_mac_ahead_of_the_public_main_is_never_taken_back(self):
        checkout = self.tmp / "ahead"
        git(self.tmp, "clone", "-q", str(self.published), str(checkout))
        (checkout / "VERSION").write_text("1.0.0\n")
        (checkout / "docs" / "local.md").write_text("merged here, not published\n")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "local work")
        done = subprocess.run([str(checkout / "bin/athena"), "install"], capture_output=True, text=True, env=os.environ, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        running = self.record()["current"]
        self.commit_on_main("1.0.0")  # public main moved on another line
        done = self.update("--channel", "main")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["current"], running, "public main is not ahead of what runs: no downgrade")
        self.assertIn("this runs commits main doesn't have", done.stdout)

    def test_on_main_a_lower_version_is_never_installed(self):
        before = self.record()["current"]
        self.commit_on_main("0.9.0")
        done = self.update("--channel", "main")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["current"], before)

    def test_check_says_what_is_out_and_installs_nothing(self):
        self.release("1.2.0")
        before = self.running()
        done = self.update("--check")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("athena 1.2.0 (v1.2.0) is out", done.stdout)
        self.assertEqual(self.running(), before)
        self.assertEqual(self.record()["latest"]["version"], "1.2.0")
        doctor = json.loads(self.athena("doctor", "--json").stdout)
        self.assertEqual(doctor["checks"]["update"], {"ok": True, "detail": "athena 1.0.0; 1.2.0 is out: `athena update`"})

    def test_an_update_interrupted_mid_switch_leaves_the_old_release_running(self):
        def hang_its_doctor(tree):
            doctor = tree / "athena/caps/doctor.py"
            doctor.write_text(doctor.read_text().replace("def _run(args):\n", "def _run(args):\n    time.sleep(120)\n", 1))
        self.release("1.1.0", hang_its_doctor)
        before = self.record()["current"]
        os.environ["ATHENA_UPDATE_WAIT"] = "120"
        proc = subprocess.Popen([str(self.local / "bin/athena"), "update", "--repo", str(self.published)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=os.environ)
        deadline = time.monotonic() + 120
        while not self.record().get("switch") and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIsNotNone(self.record()["switch"], "it reached the switch")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the update was interrupted", out)
        record = self.record()
        self.assertEqual(record["current"], before)
        self.assertIsNone(record["switch"])
        self.assertEqual(self.running(), before["release"])

    def test_a_switch_left_by_a_killed_update_is_rolled_back_by_doctor(self):
        """SIGKILL leaves no chance to undo: the switch stays on record, and doctor's repair finishes it the safe way."""
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        record = self.record()
        old, new = record["previous"], record["current"]
        killed = {**record, "current": old, "previous": None, "switch": {"from": old, "to": new, "since": time.time()}}
        (self.local / "share/athena/install.json").write_text(json.dumps(killed))
        self.assertEqual(self.running(), new["release"], "killed after the link swapped, before its verdict")
        doctor = self.athena("doctor", "--fix", "--json")
        self.assertIn("repaired update", json.loads(doctor.stdout)["actions"], doctor.stderr)
        record = self.record()
        self.assertEqual(record["current"], old)
        self.assertIsNone(record["switch"])
        self.assertEqual(record["last_update"]["result"], "rolled back")
        self.assertEqual(self.running(), old["release"])

    def test_the_version_is_the_release_file(self):
        done = self.athena("--version")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "athena 1.0.0"))
        self.assertEqual(releases.version_of(ROOT), (ROOT / "VERSION").read_text().strip())

    def test_the_jobs_restart_in_a_session_of_their_own_so_doctors_own_restart_cannot_cut_it_short(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "restarted\n", "")) as ran:
            releases.restart_services(["local.athena.doctor"])
        self.assertEqual(ran.call_args.args[0][1:], ["services", "restart"])
        self.assertTrue(ran.call_args.kwargs["start_new_session"])

    def test_a_cut_off_switch_whose_release_is_gone_keeps_what_runs_and_says_so(self):
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        record = self.record()
        old, new = record["previous"], record["current"]
        shutil.rmtree(self.local / "share/athena/releases" / old["release"])
        (self.local / "share/athena/install.json").write_text(json.dumps(
            {**record, "current": old, "previous": None, "switch": {"from": old, "to": new, "since": time.time()}}))
        done = self.update()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("the release the cut-off update came from is gone", done.stdout, "update reaches the recovery first")
        self.assertIsNone(self.record()["switch"])
        self.assertEqual(self.record()["current"], new, "the link names it, and nothing else is left")
        self.assertEqual(self.record()["last_update"]["result"], "kept")

    def test_an_install_that_recovers_a_cut_off_update_never_imports_the_other_releases_code(self):
        """A killed update leaves `current` on the new release, so `athena install` runs the new code. Its recovery points `current`
        back, and a module imported after that would come from the old release. Each of 1.0.0's modules says who imports it."""
        self.release("1.1.0")
        self.assertEqual(self.update().returncode, 0)
        record = self.record()
        old, new = record["previous"], record["current"]
        seen = self.tmp / "imported-by"
        for module in (self.local / "share/athena/releases" / old["release"] / "athena").rglob("*.py"):
            module.write_text(f"import sys as _s\nopen({str(seen)!r}, 'a').write(' '.join(_s.argv[1:3]) + ' {module.stem}\\n')\n" + module.read_text())
        (self.local / "share/athena/install.json").write_text(json.dumps(
            {**record, "current": old, "previous": None, "switch": {"from": old, "to": new, "since": time.time()}}))
        self.assertEqual(self.running(), new["release"], "killed after the link swapped, before its verdict")
        done = self.athena("install")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.running(), old["release"], "the install rolled the cut-off update back")
        lines = seen.read_text().splitlines() if seen.exists() else []
        self.assertFalse([line for line in lines if line.startswith("install")], f"the 1.1.0 install imported 1.0.0's code: {lines}")

    def test_no_uninstall_while_an_update_holds_the_lock(self):
        with releases.locked() as got:
            self.assertTrue(got)
            done = self.athena("install", "--uninstall")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("an install or update is running", done.stderr)
        self.assertTrue((self.local / "share/athena/current").is_symlink(), "nothing was removed")

    def test_a_lab_doctor_on_an_unmarked_home_never_looks_at_its_install(self):
        from athena.caps.install import update
        from athena.kernel import lab
        self.assertTrue(update.check(None), "on this marked scratch HOME it looks")
        (self.home / lab.SCRATCH_MARK).unlink()
        self.assertEqual(update.check(None), [], "a lab doctor with the real HOME must not roll back the live install's switch")

    def test_an_update_never_imports_the_new_releases_code_into_itself(self):
        """The running process puts the `current` link on its path: after the swap, an import of a module it had not loaded yet
        would come from the new release. 1.1.0's services module says which processes import it: its own commands may."""
        seen = self.tmp / "imported-by"

        def tell_who_imports(tree):
            services = tree / "athena/caps/services.py"
            services.write_text(f"import sys as _s\nopen({str(seen)!r}, 'a').write(' '.join(_s.argv[1:3]) + '\\n')\n" + services.read_text())
        self.release("1.1.0", tell_who_imports)
        agents = self.tmp / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "local.athena.watch.plist").write_text("x")  # a job to restart
        done = self.update()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = seen.read_text().splitlines() if seen.exists() else []
        self.assertIn("services restart", lines, "1.1.0's own command restarted the jobs")
        self.assertFalse([line for line in lines if line.startswith("update")], f"the 1.0.0 update process imported 1.1.0's: {lines}")
