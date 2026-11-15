"""scripts/upgrade-from-herdr.py, on isolated HOMEs holding what the herdr athena left: its state, jobs, launcher and plugin."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from harness import Case, ROOT

from athena.caps import install
from athena.kernel import config

SCRIPT = str(ROOT / "scripts/upgrade-from-herdr.py")


class _HerdrHome(Case):
    """A HOME holding what the herdr athena left, launchd a stub that keeps what is loaded."""

    def setUp(self):
        super().setUp()
        self.old = self.home / ".local/state/athena"
        self.new = self.home / "Library/Application Support/athena"
        self.old.mkdir(parents=True)
        (self.old / "ledger.jsonl").write_text('{"event": "spawn", "name": "o", "ts": 1}\n')
        (self.old / "config.json").write_text(json.dumps({"model": "opus"}))
        os.environ["ATHENA_STATE_DIR"] = str(self.old)
        config.reset()
        install.set_real_world()  # as step 6 does; with ATHENA_WORKER=rex-harness the door stays shut
        os.environ.pop("ATHENA_STATE_DIR")
        config.reset()
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.loaded = self.tmp / "loaded"
        self.loaded.mkdir()
        for job in ("athena.dash", "athena.doctor", "athena.board", "athena.herdr", "athena-lights"):
            (self.agents / f"{job}.plist").write_text(job)
            (self.loaded / job).write_text("")
        self.fails = self.tmp / "launchctl-fails"
        launchctl = self.stubs / "launchctl"
        launchctl.write_text(f"""#!/bin/sh
echo "launchctl $*" >> "$ATHENA_WORLD_CALLS"
case "$1" in
bootout|print) label=${{2##*/}} ;;
bootstrap) label=$(basename "$3" .plist) ;;
*) exit 0 ;;
esac
grep -qx "$1 $label" "{self.fails}" 2>/dev/null && exit 5
case "$1" in
bootout) [ -e "{self.loaded}/$label" ] || exit 113; rm -f "{self.loaded}/$label" ;;
bootstrap) touch "{self.loaded}/$label" ;;
print) [ -e "{self.loaded}/$label" ] || exit 113 ;;
esac
""")
        launchctl.chmod(0o755)
        table = self.stubs / "no-old-hq"
        table.write_text('#!/bin/sh\n[ -f "$ATHENA_PROCESSES" ] && cat "$ATHENA_PROCESSES"\nexit 0\n')
        table.chmod(0o755)
        os.environ.update(ATHENA_PS=str(table), ATHENA_PROCESSES=str(self.tmp / "processes"), ATHENA_UPGRADE_WAIT="0")

    script = SCRIPT

    def run_step(self, *args):
        return subprocess.run(["/usr/bin/python3", "-I", self.script, *args], capture_output=True, text=True, env=os.environ, timeout=60)

    def ok(self, *args):
        done = self.run_step(*args)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def _bare_install(self, script):
        installed = self.home / ".local/bin"
        installed.mkdir(parents=True, exist_ok=True)
        fake = installed / "athena"
        fake.write_text("#!/bin/sh\n# athena launcher, written by `athena install`.\n" + script)
        fake.chmod(0o755)
        os.environ["PATH"] = f"{installed}:{os.environ['PATH']}"



class Upgrade(_HerdrHome):
    def test_it_refuses_an_unmarked_home_while_a_lab_worker_runs_it(self):
        (self.home / ".athena-scratch").unlink()
        done = self.run_step("move")
        self.assertEqual(done.returncode, 2)
        self.assertTrue(self.old.is_dir() and not self.old.is_symlink())

    def test_no_move_without_a_snapshot(self):
        done = self.run_step("move")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.old.is_symlink())

    def test_a_folder_at_the_new_path_stops_it_with_nothing_changed(self):
        self.ok("snapshot")
        self.new.mkdir(parents=True)
        done = self.run_step("move")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(self.old.is_symlink())

    def test_check_stops_on_an_inherited_state_dir(self):
        os.environ["ATHENA_STATE_DIR"] = str(self.old)
        done = self.run_step("check")
        self.assertEqual(done.returncode, 1)
        self.assertIn("ATHENA_STATE_DIR", done.stdout)

    def test_the_move_is_a_rename_and_a_link_and_can_run_twice(self):
        self.ok("snapshot")
        self.ok("snapshot")
        self.ok("move")
        self.ok("move")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual((self.old / "ledger.jsonl").read_text(), (self.new / "ledger.jsonl").read_text())

    def test_a_hook_that_recreates_the_old_path_mid_move_loses_nothing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("upgrade", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        os.rename(self.old, self.new.parent.mkdir(parents=True, exist_ok=True) or self.new)
        (self.old / "workers").mkdir(parents=True)
        (self.old / "workers/o.json").write_text('{"state": "working"}')
        script._link_old_path()
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(json.loads((self.new / "workers/o.json").read_text())["state"], "working")
        self.assertTrue((self.new / "ledger.jsonl").exists())

    def test_stop_old_leaves_herdr_and_rollback_brings_all_back(self):
        self.ok("snapshot")
        self.ok("stop-old")
        self.assertEqual(sorted(p.stem for p in self.agents.iterdir()), ["athena.herdr"])
        self.ok("move")
        self.ok("rollback")
        self.assertTrue(self.old.is_dir() and not self.old.is_symlink())
        self.assertFalse(self.new.exists())
        self.assertEqual(len(list(self.agents.iterdir())), 5)
        self.assertNotIn("real_world", json.loads((self.old / "config.json").read_text())["rex"])
        self.assertTrue(any("bootstrap" in c and "athena.doctor" in c for c in self.world_calls()))
        self.ok("rollback")

    def test_a_worker_on_the_new_version_stops_the_rollback(self):
        with open(self.old / "ledger.jsonl", "a") as f:
            f.write(json.dumps({"event": "spawn", "name": "newer", "backend": "rex", "space": "s", "ts": 2}) + "\n")
        self.ok("snapshot")
        self.ok("move")
        done = self.run_step("rollback")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("newer", done.stderr)
        self.assertTrue(self.old.is_symlink())
        self.ok("rollback", "--leave-workers")

    def test_compare_tells_an_append_from_a_loss(self):
        (self.old / "notes.md").write_text("mine")
        self.ok("snapshot")
        with open(self.old / "ledger.jsonl", "a") as f:
            f.write('{"event": "update", "name": "o"}\n')
        self.assertIn("appended to: ledger.jsonl", self.ok("compare"))
        (self.old / "notes.md").write_text("changed by someone, as notes are")
        self.assertEqual(self.run_step("compare").returncode, 0, "not worth a rollback")
        (self.old / "notes.md").unlink()
        done = self.run_step("compare")
        self.assertEqual(done.returncode, 1)
        self.assertIn("UNEXPECTED", done.stdout)
        self.assertIn("notes.md", done.stdout)
        (self.old / "notes.md").write_text("mine")
        with open(self.old / "ledger.jsonl", "w") as f:
            f.write("rewritten\n")
        self.assertEqual(self.run_step("compare").returncode, 1, "the ledger may only grow")

    def test_a_move_killed_between_rename_and_link_is_finished_by_running_it_again(self):
        self.ok("snapshot")
        self.new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.old, self.new)
        self.ok("move")
        self.assertTrue(self.old.is_symlink())

    def test_a_move_killed_between_rename_and_link_goes_back_too(self):
        self.ok("snapshot")
        self.new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.old, self.new)
        out = self.ok("rollback")
        self.assertIn("state is back", out)
        self.assertTrue((self.old / "ledger.jsonl").exists())
        self.assertFalse(self.new.exists())

    def test_a_raced_log_adds_its_lines_and_loses_none(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("upgrade", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        self.new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self.old, self.new)
        self.old.mkdir()
        (self.old / "ledger.jsonl").write_text('{"event": "update", "name": "o"}\n')
        script._link_old_path()
        self.assertEqual(len((self.new / "ledger.jsonl").read_text().splitlines()), 2)

    def test_a_second_upgrade_after_a_rollback_takes_a_new_snapshot_first(self):
        self.ok("snapshot")
        self.ok("move")
        self.ok("rollback")
        self.assertNotEqual(self.run_step("move").returncode, 0)
        self.assertFalse(self.old.is_symlink())
        out = self.ok("snapshot")
        self.assertIn("+ snapshot", out)
        self.ok("move")

    def test_the_rollback_forgets_when_defaults_were_armed(self):
        self.ok("snapshot")
        (self.old / "run").mkdir()
        (self.old / "run/defaults-armed.json").write_text('{"since": 1}')
        self.ok("move")
        self.ok("rollback")
        self.assertFalse((self.old / "run/defaults-armed.json").exists())

    def test_it_stops_before_moving_if_hq_will_not_go(self):
        self.ok("snapshot")
        self.ok("move")
        installed = self.home / ".local/bin"
        installed.mkdir(parents=True)
        fake = installed / "athena"
        fake.write_text('#!/bin/sh\n# athena launcher, written by `athena install`.\ncase "$*" in "hq status") echo \'{"alive": true}\' ;; "hq stop") exit 1 ;; "hq stop --force") exit 1 ;; esac\nexit 0\n')
        fake.chmod(0o755)
        os.environ["PATH"] = f"{installed}:{os.environ['PATH']}"
        done = self.run_step("rollback")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("still running", done.stderr)
        self.assertTrue(self.old.is_symlink(), "nothing moved")

    def test_a_bootout_that_fails_stops_step_4_with_its_file_in_place(self):
        self.fails.write_text("bootout athena.doctor\n")
        done = self.run_step("stop-old")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("athena.doctor", done.stderr)
        self.assertTrue((self.agents / "athena.doctor.plist").exists())

    def test_the_move_refuses_while_the_old_hq_or_its_watch_runs(self):
        self.ok("snapshot")
        for row in ("4242 /x/claude --name athena --settings y", "4243 /usr/bin/python3 /x/bin/athena watch --tagged"):
            Path(os.environ["ATHENA_PROCESSES"]).write_text(row + "\n")
            done = self.run_step("move")
            self.assertNotEqual(done.returncode, 0, row)
            self.assertFalse(self.old.is_symlink())
        Path(os.environ["ATHENA_PROCESSES"]).write_text("4242 /x/claude --name a-worker\n4243 /x/python /y/athena/current watch --tagged --wake\n")
        self.ok("move")

    def test_a_ps_that_cannot_answer_refuses_the_move(self):
        self.ok("snapshot")
        os.environ["ATHENA_PS"] = str(self.tmp / "no-such-ps")
        self.assertNotEqual(self.run_step("move").returncode, 0)
        self.assertFalse(self.old.is_symlink())

    def test_verify_goes_by_launchd_not_by_job_files(self):
        self.ok("snapshot")
        self.ok("stop-old")
        self.ok("move")
        self._bare_install("exit 0\n")
        (self.loaded / "athena.board").write_text("")
        done = self.run_step("verify")
        self.assertIn("✗ athena.board is off", done.stdout)
        (self.loaded / "athena.board").unlink()
        done = self.run_step("verify")
        for job in ("doctor", "watch", "dash"):
            self.assertIn(f"✗ local.athena.{job} is loaded", done.stdout)
        self.assertIn("✗ the watch has come round", done.stdout)
        self.assertNotEqual(done.returncode, 0)

    def test_a_new_job_that_wont_go_stops_the_rollback_with_nothing_moved(self):
        self.ok("snapshot")
        self.ok("move")
        self._bare_install("exit 0\n")
        (self.loaded / "local.athena.doctor").write_text("")
        self.fails.write_text("print local.athena.doctor\n")
        done = self.run_step("rollback")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("rolled back", done.stdout)
        self.assertTrue(self.old.is_symlink())

    def test_an_old_job_that_wont_load_says_so_and_running_it_again_finishes(self):
        self.ok("snapshot")
        self.ok("stop-old")
        self.ok("move")
        self.fails.write_text("bootstrap athena.doctor\n")
        done = self.run_step("rollback")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("athena.doctor", done.stderr)
        self.assertFalse(self.old.is_symlink())
        self.fails.write_text("")
        self.ok("rollback")
        self.assertTrue((self.loaded / "athena.doctor").exists())

    def test_the_rollback_puts_the_old_hq_record_back_on_the_newest_conversation(self):
        old = {"pane": "p0", "workspace": "w0", "session": "first", "cwd": "/c", "claude_args": ["--settings", f"{self.old}/settings/hq.json"]}
        (self.old / "hq.json").write_text(json.dumps(old))
        self.ok("snapshot")
        self.ok("move")
        (self.new / "hq.json").write_text(json.dumps({**old, "backend": "rex", "space": "s", "block": "b", "session": "second", "claude_args": ["--settings", f"{self.new}/settings/hq-rex.json"]}))
        (self.new / "hq.json.prev").write_text(json.dumps({"claude_args": [str(self.new)]}))
        self.ok("rollback")
        restored = json.loads((self.old / "hq.json").read_text())
        self.assertEqual(restored, {**old, "session": "second"})
        self.assertFalse((self.old / "hq.json.prev").exists())

    def test_a_rollback_before_the_move_makes_no_new_path_by_asking(self):
        made = self.new / "locks"
        self._bare_install(f'[ "$ATHENA_STATE_DIR" = "{self.old}" ] || mkdir -p "{made}"\ncase "$*" in "hq status") echo \'{{"alive": false}}\' ;; esac\nexit 0\n')
        self.ok("snapshot")
        self.ok("rollback")
        self.assertFalse(self.new.exists())

    def test_check_stops_on_a_worker_still_on_the_herdr_athena_and_counts_the_auto_items(self):
        (self.old / "needs-you.json").write_text(json.dumps({"next": 2, "items": [{"id": "a1", "text": "x", "auto": True, "rec": "y"}]}))
        done = self.run_step("check")
        self.assertIn("1 open items", done.stdout)
        self.assertEqual(done.returncode, 1)
        self.assertIn("these still run on the herdr athena, which the upgrade takes away: o", done.stdout)
        with open(self.old / "ledger.jsonl", "a") as f:
            f.write(json.dumps({"event": "retire", "name": "o", "ts": 2}) + "\n")
        self.assertEqual(self.run_step("check").returncode, 0)

    def test_restoring_a_snapshot_never_lands_inside_a_path_that_came_back(self):
        (self.old / "notes.md").write_text("mine")
        self.ok("snapshot")
        (self.old / "notes.md").write_text("damaged")
        self.ok("restore-snapshot")
        self.assertEqual((self.old / "notes.md").read_text(), "mine")
        self.assertEqual(sorted(p.name for p in self.old.parent.glob("athena.damaged-*"))[:1] != [], True)
        self.assertFalse((self.old / "athena").exists())

    def test_restoring_refuses_while_the_state_is_moved(self):
        self.ok("snapshot")
        self.ok("move")
        self.assertNotEqual(self.run_step("restore-snapshot").returncode, 0)

    def test_in_the_lab_launchd_is_a_marked_stub_or_nothing(self):
        del os.environ["ATHENA_WORLD_STUBS"]
        real = self.tmp / "real-launchctl-was-called"
        fake = self.tmp / "onpath"
        fake.mkdir()
        (fake / "launchctl").write_text(f'#!/bin/sh\ntouch "{real}"\n')
        (fake / "launchctl").chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
        for step in ("stop-old", "check", "rollback", "verify"):
            done = self.run_step(step)
            self.assertEqual(done.returncode, 2, step)
            self.assertIn("stub", done.stderr)
        self.assertFalse(real.exists())
        self.assertTrue((self.agents / "athena.doctor.plist").exists())

    def test_doctor_goes_before_what_it_would_bring_back(self):
        self.ok("snapshot")
        self.ok("stop-old")
        bootouts = [c for c in self.world_calls() if " bootout " in f" {c} "]
        self.assertIn("athena.doctor", bootouts[0])

    def test_verify_wants_the_doctor_job_to_have_run_itself(self):
        self.ok("snapshot")
        self.ok("stop-old")
        self.ok("move")
        self._bare_install("exit 0\n")
        job = self.agents / "local.athena.doctor.plist"
        job.write_text("x")
        (self.loaded / "local.athena.doctor").write_text("")
        (self.new / "health.json").write_text(json.dumps({"ts": os.path.getmtime(job) - 3600}))
        self.assertIn("✗ the doctor job has run", self.run_step("verify").stdout)
        (self.new / "health.json").write_text(json.dumps({"ts": os.path.getmtime(job) + 1}))
        self.assertIn("✓ the doctor job has run", self.run_step("verify").stdout)

    def test_verify_reads_a_busy_doctor_as_busy(self):
        self.ok("snapshot")
        self.ok("stop-old")
        self.ok("move")
        self._bare_install('case "$*" in "doctor --json") echo \'{"busy": true, "detail": "another doctor is running"}\' ;; esac\nexit 0\n')
        out = self.run_step("verify").stdout
        self.assertIn("! doctor: another doctor pass is running", out)
        self.assertNotIn("did not answer", out)
        self._bare_install("exit 0\n")
        self.assertIn("! doctor: doctor did not answer", self.run_step("verify").stdout)

    def test_an_hq_status_nobody_can_read_stops_the_rollback(self):
        self.ok("snapshot")
        self.ok("move")
        self._bare_install('case "$*" in "hq status") echo not json ;; esac\nexit 0\n')
        done = self.run_step("rollback")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("can't say", done.stderr)
        self.assertTrue(self.old.is_symlink())
        self.ok("rollback", "--hq-gone")
        self.assertFalse(self.old.is_symlink())

    def test_hq_gone_never_lifts_an_hq_that_reads_alive(self):
        self.ok("snapshot")
        self.ok("move")
        self._bare_install('case "$*" in "hq status") echo \'{"alive": true}\' ;; esac\nexit 0\n')
        done = self.run_step("rollback", "--hq-gone")
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(self.old.is_symlink(), "nothing moved under a live HQ")

    def test_a_watch_or_dash_that_wont_stop_stops_the_rollback(self):
        self.ok("snapshot")
        self.ok("move")
        for script in ('case "$*" in "watch --stop") echo stuck; exit 1 ;; esac\nexit 0\n', 'case "$*" in "dash off") echo stuck; exit 1 ;; esac\nexit 0\n'):
            self._bare_install(script)
            done = self.run_step("rollback")
            self.assertNotEqual(done.returncode, 0, script)
            self.assertIn("nothing moved", done.stderr)
            self.assertTrue(self.old.is_symlink())

    def test_the_error_names_the_pid_to_kill_for_the_old_watch(self):
        self.ok("snapshot")
        Path(os.environ["ATHENA_PROCESSES"]).write_text("4243 /usr/bin/python3 /x/bin/athena watch --tagged\n")
        self.assertIn("kill 4243", self.run_step("move").stderr)


class WholeRun(_HerdrHome):
    """`run` on a fixture of what the herdr athena leaves: its state, its jobs, its launcher (a link into its checkout) and its plugin."""

    def setUp(self):
        super().setUp()
        with open(self.old / "ledger.jsonl", "a") as f:
            f.write(json.dumps({"event": "retire", "name": "o", "ts": 2}) + "\n")
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "cwd": str(self.tmp)}))
        clone = self.home / "Developer/athena/bin"
        clone.mkdir(parents=True)
        (clone / "athena").write_text("#!/bin/sh\necho herdr athena\n")
        (clone / "athena").chmod(0o755)
        (self.home / ".local/bin").mkdir(parents=True)
        os.symlink(clone / "athena", self.home / ".local/bin/athena")
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude/settings.json"
        self.settings.write_text(json.dumps({"enabledPlugins": {"athena@athena": True, "other@x": True}, "model": "opus"}))
        self.stub("rex", "rex version 1")
        self.script = self.checkout()

    def checkout(self) -> str:
        """The script in a checkout whose bin/athena stands in for install: athena itself can't run on this HOME's moved state in
        the lab, whose guard holds that path live (test_install and test_release install it for real). It writes what install
        writes: athena's launcher, answering as an installed athena would, and its jobs, loaded."""
        root = self.tmp / "checkout"
        (root / "scripts").mkdir(parents=True)
        (root / "bin").mkdir()
        shutil.copy2(SCRIPT, root / "scripts")
        os.symlink(ROOT / "athena", root / "athena")
        launcher = self.home / ".local/bin/athena"
        answers = ('case "$*" in --version) echo "athena 1.0.0" ;; "hq status") echo \'{"alive": false}\' ;; '
                   '"doctor --json") echo \'{"ok": true, "problems": [], "checks": {}}\' ;; esac')
        stand_in = root / "bin/athena"
        stand_in.write_text(f"""#!/bin/sh
echo "install $*" >> "$ATHENA_WORLD_CALLS"
case "$*" in *--dry-run*) echo "  + release 1.0.0"; exit 0 ;; esac
if grep -q "athena launcher" "{launcher}" 2>/dev/null; then echo "  = release 1.0.0"; exit 0; fi
printf '#!/bin/sh\\n# athena launcher, written by `athena install`.\\n%s\\nexit 0\\n' '{answers}' > "{launcher}"
chmod 755 "{launcher}"
for job in doctor watch dash; do echo x > "{self.agents}/local.athena.$job.plist"; touch "{self.loaded}/local.athena.$job"; done
echo "  + release 1.0.0"
""")
        stand_in.chmod(0o755)
        return str(root / "scripts/upgrade-from-herdr.py")

    def tree(self) -> dict:
        out = {}
        for path in sorted(self.home.rglob("*")):
            if path.is_symlink():
                out[str(path)] = os.readlink(path)
            elif path.is_file() and path.name != ".lock":
                out[str(path)] = path.read_bytes()
        return out

    def test_a_dry_run_says_every_step_and_changes_nothing(self):
        before = self.tree()
        out = self.ok("run", "--dry-run")
        for line in ("would: snapshot", "would: stop athena.doctor", "would: rename", "would: move the herdr athena's",
                     "would: turn the athena@athena plugin off", "dry run: nothing was changed"):
            self.assertIn(line, out)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.world_calls().count("launchctl bootout"), 0)

    def test_a_dry_run_with_the_old_hq_up_says_where_it_would_stop_and_shows_the_rest(self):
        Path(os.environ["ATHENA_PROCESSES"]).write_text("4242 /x/claude --name athena --settings y\n")
        before = self.tree()
        out = self.ok("run", "--dry-run")
        self.assertIn("would stop here: the old HQ has to be gone first", out)
        self.assertIn("would: turn the athena@athena plugin off", out, "the steps after the move are shown too")
        self.assertEqual(self.tree(), before)

    def herdr_stub(self, get='{"result": {"workspace": {"workspace_id": "w7", "label": "athena"}}}'):
        """A herdr that records its calls, answers `workspace get` with `get` until the workspace is closed, then not found."""
        herdr = self.stubs / "herdr"
        closed = self.tmp / "herdr-closed"
        herdr.write_text(f"""#!/bin/sh
echo "herdr $*" >> "$ATHENA_WORLD_CALLS"
case "$1 $2" in
"workspace get") if [ -e "{closed}" ]; then echo '{{"error": {{"code": "workspace_not_found", "message": "gone"}}}}' >&2; exit 1; fi
	echo '{get}' ;;
"workspace close") touch "{closed}" ;;
esac
exit 0
""")
        herdr.chmod(0o755)
        os.environ["ATHENA_HERDR"] = str(herdr)
        return closed

    def test_the_old_hqs_herdr_workspace_closes_once_so_a_herdr_restart_cannot_resume_it(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7", "pane": "p3"}))
        closed = self.herdr_stub()
        self.assertIn("would: close the old HQ's herdr workspace w7", self.ok("run", "--dry-run"))
        self.assertNotIn("herdr workspace close w7", self.world_calls(), "a dry run closes nothing")
        self.ok("old-hq")
        self.assertEqual(self.world_calls().count("herdr workspace close w7"), 1)
        self.assertIn("= the old HQ's herdr workspace w7 is closed", self.ok("old-hq"))
        self.assertEqual(self.world_calls().count("herdr workspace close w7"), 1, "a second run changes nothing")
        closed.unlink()  # after a rollback the herdr athena's HQ opens a workspace again, and herdr may give it the same id
        self.ok("old-hq")
        self.assertEqual(self.world_calls().count("herdr workspace close w7"), 2, "nothing remembered: what is open is what counts")

    def test_an_id_herdr_has_given_to_another_workspace_is_left_open(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        self.herdr_stub('{"result": {"workspace": {"workspace_id": "w7", "label": "my-project"}}}')
        out = self.ok("old-hq")
        self.assertIn("now 'my-project', not the old HQ's", out)
        self.assertNotIn("herdr workspace close w7", self.world_calls())

    def test_a_workspace_already_gone_is_done_and_a_rerun_says_so(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        self.herdr_stub()
        (self.stubs / "herdr").write_text('#!/bin/sh\necho "herdr $*" >> "$ATHENA_WORLD_CALLS"\n'
                                          'echo \'{"error": {"code": "workspace_not_found", "message": "no workspace w7"}}\' >&2\nexit 1\n')
        self.assertIn("is closed", self.ok("old-hq"))
        self.assertNotIn("herdr workspace close w7", self.world_calls())

    def test_once_moved_the_new_hq_running_is_not_taken_for_the_old_one(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        self.herdr_stub()
        self.ok("snapshot")
        self.ok("move")
        Path(os.environ["ATHENA_PROCESSES"]).write_text("4242 /x/claude --name athena --plugin-dir /y/plugin\n")
        self.ok("old-hq")
        self.assertEqual(self.world_calls().count("herdr workspace close w7"), 1)

    def test_it_will_not_run_inside_herdr(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        self.herdr_stub()
        done = subprocess.run(["/usr/bin/python3", "-I", self.script, "old-hq"], capture_output=True, text=True,
                              env={**os.environ, "HERDR_PANE_ID": "p9"}, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("outside herdr", done.stderr)
        self.assertNotIn("herdr workspace close w7", self.world_calls())

    def test_without_herdr_it_says_what_to_close_by_hand(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        os.environ.pop("ATHENA_HERDR", None)
        self.assertIn("close the workspace labelled athena yourself", self.ok("old-hq"))

    def test_the_old_hq_running_stops_the_close(self):
        (self.old / "hq.json").write_text(json.dumps({"session": "conv", "workspace": "w7"}))
        self.herdr_stub()
        Path(os.environ["ATHENA_PROCESSES"]).write_text("4242 /x/claude --name athena --settings y\n")
        done = self.run_step("old-hq")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("herdr workspace close w7", self.world_calls())

    def test_run_upgrades_and_a_second_run_changes_nothing(self):
        done = self.run_step("run")
        self.assertIn("✓ the ledger is at the new path", done.stdout, done.stdout + done.stderr)
        launchd_only = {"the doctor job has run", "the watch has come round", "the dash on port"}
        failed = [line for line in done.stdout.splitlines() if line.strip().startswith("✗")]
        self.assertTrue(all(any(part in line for part in launchd_only) for line in failed), failed)
        self.assertTrue(self.old.is_symlink() and (self.new / "ledger.jsonl").is_file(), "state moved, old path a link")
        self.assertTrue(json.loads((self.new / "config.json").read_text())["rex"]["real_world"])
        self.assertEqual(json.loads((self.new / "hq.json").read_text())["session"], "conv", "HQ resumes its conversation")
        for job in ("athena.doctor", "athena.dash", "athena.board"):
            self.assertFalse((self.agents / f"{job}.plist").exists())
            self.assertTrue((self.home / "Library/LaunchAgents.athena-old" / f"{job}.plist").exists())
        self.assertTrue((self.agents / "athena.herdr.plist").exists(), "herdr's server job stays")
        for job in ("doctor", "watch", "dash"):
            self.assertTrue((self.agents / f"local.athena.{job}.plist").exists())
        self.assertIn("athena launcher", (self.home / ".local/bin/athena").read_text())
        self.assertEqual(os.readlink(self.home / ".local/bin/athena-herdr"), str(self.home / "Developer/athena/bin/athena"))
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["enabledPlugins"], {"athena@athena": False, "other@x": True})
        self.assertEqual(settings["model"], "opus")
        self.assertIn("install install --services --real-world", self.world_calls())

        before = self.tree()
        again = self.run_step("run")
        for line in ("= snapshot already taken", "= athena.doctor already off", "= already moved", "is athena's",
                     "= release", "= the athena@athena plugin is not on"):
            self.assertIn(line, again.stdout)
        changed = sorted(k for k in self.tree().keys() | before.keys() if self.tree().get(k) != before.get(k))
        self.assertEqual([k for k in changed if "/Library/Application Support/athena/" not in k], [], "nothing outside the state changed")

    def test_a_settings_file_that_is_a_link_stays_one_and_keeps_a_backup(self):
        dotfiles = self.home / "dotfiles"
        dotfiles.mkdir()
        kept = dotfiles / "claude-settings.json"
        os.replace(self.settings, kept)
        os.symlink(kept, self.settings)
        before = kept.read_text()
        self.ok("plugin")
        self.assertTrue(self.settings.is_symlink(), "the link to the dotfiles repo stays")
        self.assertIs(json.loads(kept.read_text())["enabledPlugins"]["athena@athena"], False)
        self.assertEqual((dotfiles / "claude-settings.json.before-athena-upgrade").read_text(), before)

    def test_rollback_puts_the_herdr_launcher_and_plugin_back(self):
        self.run_step("run")
        self._bare_install(f'case "$*" in "hq status") echo \'{{"alive": false}}\' ;; "services uninstall") rm -f "{self.agents}"/local.athena.*.plist '
                           f'"{self.loaded}"/local.athena.* ;; esac\nexit 0\n')
        self.ok("rollback")
        self.assertEqual(os.readlink(self.home / ".local/bin/athena"), str(self.home / "Developer/athena/bin/athena"))
        self.assertTrue(json.loads(self.settings.read_text())["enabledPlugins"]["athena@athena"])
        self.assertTrue(self.old.is_dir() and not self.old.is_symlink())
        self.assertTrue((self.agents / "athena.doctor.plist").exists())
