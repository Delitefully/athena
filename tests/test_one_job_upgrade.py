"""An install of the earlier layout (one launchd job per service) taking this release, against two scratch releases: v1.0.0
as it was tagged, the last release with a job per service, and this tree. launchd is the fake that remembers
(test_menubar.FAKE_LAUNCHCTL). v1.0.0 runs its own `update`: it only restarts its jobs onto the new code and asks the new
doctor for a verdict, so the move to the one job is the new doctor's `services` repair, once the verdict is saved."""
import io
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from harness import Case, ROOT, git

from athena.caps.install import releases

OLD = "v1.0.0"
OLD_JOBS = {"local.athena.doctor", "local.athena.watch", "local.athena.dash", "local.athena.menubar"}


class OneJobUpgrade(Case):
    def setUp(self):
        super().setUp()
        from test_menubar import FAKE_LAUNCHCTL
        script = self.stubs / "launchctl"
        script.write_text(FAKE_LAUNCHCTL)
        script.chmod(0o755)
        self.local = self.home / ".local"
        os.environ.pop("ATHENA_STATE_DIR", None)
        os.environ["PATH"] = f"{self.local / 'bin'}:{os.environ['PATH']}"
        os.environ["ATHENA_UPDATE_WAIT"] = "2"
        os.environ["ATHENA_HQ"] = "hq-of-this-mac"  # carried by every plist, whichever layout writes it
        self.stub("rex", "rex version 1")
        os.environ["ATHENA_BINARY"] = str(self.stubs / "rex")
        self.published = self.tmp / "published"
        self.published.mkdir()
        tagged = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", OLD], capture_output=True)
        self.assertEqual(tagged.returncode, 0, f"{OLD} is the release this tests against: {tagged.stderr.decode()}")
        with tarfile.open(fileobj=io.BytesIO(tagged.stdout)) as archive:
            archive.extractall(self.published)
        shutil.rmtree(self.published / "tests")
        self.assertFalse(releases.has_supervisor(self.published), f"{OLD} runs a job per service")
        git(self.published.parent, "init", "-q", "-b", "main", str(self.published))
        self.release("1.0.0")
        checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "-q", "--branch", "v1.0.0", str(self.published), str(checkout))
        done = subprocess.run([str(checkout / "bin/athena"), "install", "--services"], capture_output=True, text=True,
                              env=os.environ, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.plists(), sorted(f"{j}.plist" for j in OLD_JOBS))
        self.assertEqual(self.loaded(), OLD_JOBS)

    def release(self, version, tree=None):
        if tree is not None:
            for entry in self.published.iterdir():
                if entry.name != ".git":
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            shutil.copytree(tree, self.published, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "build"))
        (self.published / "VERSION").write_text(f"{version}\n")
        git(self.published, "add", "-A")
        git(self.published, "commit", "-q", "-m", f"athena {version}")
        git(self.published, "tag", f"v{version}")

    def plists(self) -> list:
        agents = self.tmp / "agents"
        return sorted(p.name for p in agents.glob("*.plist")) if agents.is_dir() else []

    def loaded(self) -> set:
        path = Path(f"{self.calls}.launchd")
        return set(path.read_text().split()) if path.exists() else set()

    def launchctl_calls(self) -> list:
        return [c[len("launchctl "):] for c in self.world_calls() if c.startswith("launchctl ")]

    def record(self) -> dict:
        return json.loads((self.local / "share/athena/install.json").read_text())

    def athena(self, *args, timeout=240):
        return subprocess.run([str(self.local / "bin/athena"), *args], capture_output=True, text=True, env=os.environ, timeout=timeout)

    def update_to_this_release(self):
        self.release("1.1.0", ROOT)
        self.calls.write_text("")
        done = self.athena("update", "--repo", str(self.published))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["current"]["version"], "1.1.0")
        self.assertIsNone(self.record()["switch"])
        self.assertTrue(releases.has_supervisor(self.local / "share/athena/current"))
        return done

    def moved(self, done):
        """doctor's repair starts `services install` in a session of its own and does not wait for it: this does."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and (self.plists() != ["local.athena.plist"] or self.loaded() != {"local.athena"}):
            time.sleep(0.1)
        log = self.home / "Library/Application Support/athena/logs/services-move.log"
        said = log.read_text() if log.exists() else ""
        self.assertEqual(self.plists(), ["local.athena.plist"], done.stdout + done.stderr + said)
        self.assertEqual(self.loaded(), {"local.athena"})

    def env_of(self, name) -> dict:
        return plistlib.loads((self.tmp / "agents" / name).read_bytes())["EnvironmentVariables"]

    def test_an_update_restarts_the_old_jobs_and_the_next_doctor_moves_them_to_the_one_job(self):
        self.update_to_this_release()
        self.assertEqual(self.plists(), sorted(f"{j}.plist" for j in OLD_JOBS), "nothing moved before the verdict was saved")
        self.assertEqual(self.loaded(), OLD_JOBS)
        restarted = {c.split("/")[-1] for c in self.launchctl_calls() if c.startswith("kickstart -k ")}
        self.assertEqual(restarted, OLD_JOBS - {"local.athena.menubar"}, "onto the new code, as each was")
        self.calls.write_text("")
        done = self.athena("doctor", "--fix", "--quiet")  # the old doctor job's next run, on the new code
        self.moved(done)
        health = json.loads((self.home / "Library/Application Support/athena/health.json").read_text())
        self.assertIn("repaired services", health["actions"])
        self.assertNotIn("services", [p["check"] for p in health["problems"]], "upkeep, not a problem: no notification for it")
        self.assertEqual(self.loaded(), {"local.athena"})
        self.assertEqual(self.env_of("local.athena.plist")["ATHENA_HQ"], "hq-of-this-mac")
        self.calls.write_text("")
        self.athena("doctor", "--fix", "--quiet")
        self.assertFalse(any(c.split()[0] in ("bootstrap", "bootout", "kickstart") for c in self.launchctl_calls()),
                         "moved once: the next pass has nothing to do")
        self.assertEqual(self.athena("services", "restart").stdout.strip(), "restarted local.athena")

    def test_nothing_moves_while_an_update_holds_the_install(self):
        self.update_to_this_release()
        with releases.locked() as got:
            self.assertTrue(got)
            health = json.loads(self.athena("doctor", "--fix", "--json").stdout)
        self.assertTrue(health["checks"]["services"]["ok"])
        self.assertIn("an update is under way", health["checks"]["services"]["detail"])
        self.assertEqual(self.loaded(), OLD_JOBS)

    def test_a_failed_health_check_rolls_back_onto_the_old_jobs_as_they_were(self):
        def break_a_check(tree):
            doctor = tree / "athena/caps/doctor.py"
            doctor.write_text(doctor.read_text().replace("def checks(ctx):\n", "def checks(ctx):\n    return [caps.Check('disk', False, 'broken in 1.1.0')]\n", 1))
        staged = self.tmp / "this-release"
        shutil.copytree(ROOT, staged, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "build"))
        break_a_check(staged)
        self.release("1.1.0", staged)
        self.calls.write_text("")
        done = self.athena("update", "--repo", str(self.published))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("failed its health check", done.stdout)
        self.assertEqual(self.record()["current"]["version"], "1.0.0")
        self.assertEqual(self.plists(), sorted(f"{j}.plist" for j in OLD_JOBS))
        self.assertEqual(self.loaded(), OLD_JOBS, "doctor, watch, dash and the menu bar, as 1.0.0 runs them")
        self.athena("doctor", "--fix", "--quiet")
        self.assertEqual(self.loaded(), OLD_JOBS, "1.0.0's doctor moves nothing")

    def test_a_rollback_to_a_release_without_the_supervisor_gets_its_jobs_back(self):
        self.update_to_this_release()
        self.moved(self.athena("doctor", "--fix", "--quiet"))
        self.calls.write_text("")
        done = self.athena("install", "--rollback")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("back to one launchd job per service", done.stdout)
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.0.0")
        self.assertEqual(self.plists(), sorted(f"{j}.plist" for j in OLD_JOBS))
        self.assertEqual(self.loaded(), OLD_JOBS, "doctor, watch, dash and the menu bar, as 1.0.0 runs them")
        self.assertEqual(self.env_of("local.athena.doctor.plist")["ATHENA_HQ"], "hq-of-this-mac", "the one job's environment kept")
        calls = self.launchctl_calls()
        self.assertEqual(calls[-1], f"bootout gui/{os.getuid()}/local.athena", "the one job goes last: a caller under it ends with it")
        self.calls.write_text("")
        again = self.athena("update", "--repo", str(self.published))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.loaded(), OLD_JOBS, "and forward again by update, which moves nothing before doctor does")
        self.assertEqual(self.athena("services", "install").returncode, 0)  # doctor's repair, run here past its hourly backoff
        self.assertEqual(self.loaded(), {"local.athena"})

    def cut_off(self, back_to):
        """A switch on record, as an update killed between its swap and its verdict leaves it: from `back_to` to what runs."""
        record = releases.load()
        releases.save({**record, "switch": {"from": back_to, "to": record["current"], "since": time.time()}})

    def test_recover_from_a_cut_off_update_lands_on_the_layout_of_the_release_it_goes_back_to(self):
        self.update_to_this_release()
        self.moved(self.athena("doctor", "--fix", "--quiet"))
        self.cut_off(self.record()["previous"])  # back to 1.0.0, which runs a job per service
        self.calls.write_text("")
        self.athena("doctor", "--fix", "--quiet")  # doctor's `update` repair recovers the switch
        record = self.record()
        self.assertIsNone(record["switch"])
        self.assertEqual(record["current"]["version"], "1.0.0")
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.0.0")
        self.assertEqual(self.plists(), sorted(f"{j}.plist" for j in OLD_JOBS))
        self.assertEqual(self.loaded(), OLD_JOBS, "doctor, watch, dash and the menu bar, as 1.0.0 runs them")

    def test_recover_between_two_supervisor_releases_restarts_the_one_job_onto_the_code_it_goes_back_to(self):
        self.update_to_this_release()
        self.moved(self.athena("doctor", "--fix", "--quiet"))
        self.release("1.2.0", ROOT)
        done = self.athena("update", "--repo", str(self.published))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["current"]["version"], "1.2.0")
        self.cut_off(self.record()["previous"])  # back to 1.1.0, which has the supervisor too
        self.calls.write_text("")
        done = self.athena("install", "--rollback")  # an install or rollback recovers a switch on record first
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.athena("--version").stdout.strip(), "athena 1.1.0")
        self.assertEqual(self.launchctl_calls(), [f"kickstart -k gui/{os.getuid()}/local.athena"],
                         "the supervisor and every child restart onto 1.1.0's code")
        self.assertEqual(self.plists(), ["local.athena.plist"])
        self.assertEqual(self.loaded(), {"local.athena"})
