"""The release: athena's old name keeps working and says so."""
import json
import os
import shutil
import subprocess
from unittest import mock

from harness import BIN, ROOT, Case, store

from athena.caps import hq
from athena.kernel import caps


class OldName(Case):
    def test_the_old_command_runs_athena_and_says_so_in_one_line(self):
        new = self.cli("doctor", "--json")
        old = subprocess.run([str(BIN.with_name("athena-rex")), "doctor", "--json"], capture_output=True, text=True, timeout=60)
        self.assertEqual(old.returncode, new.returncode, old.stderr)
        self.assertEqual(old.stdout.splitlines()[0], new.stdout.splitlines()[0])
        self.assertEqual(old.stderr.splitlines()[0], "athena-rex is now athena: this alias goes in a later release")
        self.assertNotIn("athena-rex", new.stderr)

    def test_the_old_package_name_is_the_same_modules_not_copies(self):
        import athena.caps.hooks
        import athena.kernel.store
        from athena_rex.caps import hooks
        from athena_rex.kernel import store
        self.assertIs(store, athena.kernel.store, "one copy of each module, and of its state")
        self.assertIs(hooks, athena.caps.hooks)
        self.assertEqual(store.__spec__.name, "athena.kernel.store", "the alias leaves the real module's own spec")


class HqRecord(Case):
    """`athena hq` typed in HQ's own block after a restore: closing that block ends the command, so the new place must be on
    record before it closes, or hq.json names a dead block and nothing can wake HQ."""

    def setUp(self):
        super().setUp()
        self.backend = caps.CTX.backend
        self.old = self.backend.create("athena", str(self.tmp), ["sh"], block_label="athena", window_label="athena")
        hq._save({"space": self.old.space, "window": self.old.window, "block": self.old.block, "session": "conv", "cwd": str(self.tmp)})

    def blocks(self):
        from athena.backends import fake
        return set(fake._load()["spaces"][self.old.space]["blocks"])

    def test_the_new_block_is_on_record_before_the_old_one_closes(self):
        def dies_with_its_block(backend, place):
            if place.block == self.old.block:
                raise KeyboardInterrupt("the block this command runs in closed")
        with mock.patch.object(type(self.backend), "close_block", dies_with_its_block):
            with self.assertRaises(KeyboardInterrupt):
                hq.open_hq(focus=False, routine=False)
        record = hq.load()
        self.assertEqual(record["space"], self.old.space)
        self.assertNotEqual(record["block"], self.old.block, "hq.json names the new block, not the one that closed")
        self.assertIn(record["block"], self.blocks())
        self.assertIn("--resume conv", store.path("run", "athena.sh").read_text())

    def test_the_old_block_closes_once_the_new_one_is_on_record(self):
        out = hq.open_hq(focus=False, routine=False)
        self.assertEqual(out["outcome"], "resumed")
        self.assertEqual(self.blocks(), {hq.load()["block"]}, "the old block is gone, the new one stays")


class _Checkout(Case):
    """A checkout of only the tracked files, so nothing a checkout used for work leaves behind (bytecode, a built app) counts."""

    def setUp(self):
        super().setUp()
        self.local = self.home / ".local"
        os.environ.pop("ATHENA_STATE_DIR", None)
        os.environ["PATH"] = f"{self.local / 'bin'}:{os.environ['PATH']}"
        self.stub("rex", "rex version 1")
        os.environ["ATHENA_REX_BIN"] = str(self.stubs / "rex")
        self.state_dir = self.home / "Library/Application Support/athena"
        self.checkout = self.tmp / "athena"
        for name in subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout.split("\0"):
            if name and (ROOT / name).is_file():
                (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, self.checkout / name)

    def athena(self, *args, command="athena"):
        return subprocess.run([str(self.local / "bin" / command), *args], capture_output=True, text=True, env=os.environ, timeout=120)

    def install(self, *args):
        done = subprocess.run([str(self.checkout / "bin/athena"), "install", *args], capture_output=True, text=True, env=os.environ,
                              timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def snapshot(self) -> dict:
        """Every file install made or touched, by content and mode."""
        out = {}
        for top in (self.local, self.state_dir, self.tmp / "agents"):
            for path in sorted(top.rglob("*")) if top.exists() else []:
                if path.is_symlink():
                    out[str(path)] = ("link", os.readlink(path))
                elif path.is_file() and path.name != ".lock":
                    out[str(path)] = (path.read_bytes(), path.stat().st_mode)
        return out



class InstallFromACleanCheckout(_Checkout):
    """What a new user does: clone, install, use it, install again, uninstall."""

    def test_install_twice_then_uninstall(self):
        first = self.install("--services")
        self.assertIn("Installed athena 1.0.0.", first)
        version = self.athena("--version")
        self.assertEqual((version.returncode, version.stdout.strip()), (0, f"athena {(ROOT / 'VERSION').read_text().strip()}"))
        doctor = self.athena("doctor", "--json")
        self.assertIn("update", json.loads(doctor.stdout)["checks"], doctor.stderr)
        self.assertFalse((self.local / "bin/athena-rex").exists(), "a new user gets no old name")
        before = self.snapshot()
        again = self.install("--services")
        self.assertIn("= release", again)
        after = self.snapshot()
        self.assertEqual(sorted(k for k in after.keys() | before.keys() if after.get(k) != before.get(k)), [], "a second install changes nothing")
        (self.state_dir / "ledger.jsonl").write_text("{}\n")
        gone = self.athena("install", "--uninstall")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertFalse((self.local / "bin/athena").exists())
        self.assertFalse((self.local / "share/athena").exists())
        self.assertEqual(list((self.tmp / "agents").glob("*.plist")), [])
        self.assertEqual((self.state_dir / "ledger.jsonl").read_text(), "{}\n", "the state dir stays")
        self.assertTrue((self.state_dir / "config.json").exists())


class FromAthenaRex(_Checkout):
    """This Mac's way over: an athena-rex install, its launcher, its record and its launchd jobs, become athena's. Its state is
    not new, so install leaves the door as it is: shut here, so the guard-out copy can't reach the real world."""

    def setUp(self):
        super().setUp()
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "ledger.jsonl").write_text("")
        old_root = self.local / "share/athena-rex"
        old_root.mkdir(parents=True)
        (old_root / "install.json").write_text(json.dumps({"version": 1, "release": "0123456789ab-live", "previous": None,
                                                           "source": str(self.checkout), "lab_guard": "out", "ts": 1}))
        (self.local / "bin").mkdir(parents=True)
        self.old_launcher = self.local / "bin/athena-rex"
        self.old_launcher.write_text(f'#!/bin/sh\n# athena-rex launcher, written by `athena-rex install`.\nexec "{old_root}/current/bin/athena-rex" "$@"\n')
        self.old_launcher.chmod(0o755)
        agents = self.tmp / "agents"
        agents.mkdir()
        (agents / "athena-rex.doctor.plist").write_text("<plist/>")

    def test_the_old_name_becomes_an_alias_its_jobs_are_retired_and_the_guard_stays_out(self):
        out = self.install("--services")
        self.assertIn("now the old name's alias", out)
        record = json.loads((self.local / "share/athena/install.json").read_text())
        self.assertEqual(record["lab_guard"], "out", "carried from the athena-rex install's record")
        self.assertTrue(record["current"]["release"].endswith("-live"))
        old = self.athena("doctor", "--json", command="athena-rex")
        new = self.athena("doctor", "--json")
        self.assertEqual(old.returncode, new.returncode, old.stderr)
        self.assertEqual(json.loads(old.stdout)["checks"].keys(), json.loads(new.stdout)["checks"].keys())
        self.assertIn("athena-rex is now athena", old.stderr.splitlines()[0])
        self.assertIn(f"{self.local}/share/athena/current/bin/athena-rex", self.old_launcher.read_text())
        self.assertFalse((self.tmp / "agents/athena-rex.doctor.plist").exists())
        self.assertTrue((self.tmp / "LaunchAgents.athena-rex-old/athena-rex.doctor.plist").exists(), "moved aside, not deleted")
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/athena-rex.doctor", self.world_calls())
        self.assertTrue((self.tmp / "agents/local.athena.doctor.plist").exists())
        self.assertTrue((self.local / "share/athena-rex").is_dir(), "left: running Claudes' hooks name it")
        gone = self.athena("install", "--uninstall")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertFalse(self.old_launcher.exists(), "the alias is ours, so it goes too")
