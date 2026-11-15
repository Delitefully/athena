"""The release: HQ's record across a restore, and a clean install."""
import json
import os
import shutil
import subprocess
from unittest import mock

from harness import ROOT, Case, store

from athena.caps import hq
from athena.kernel import caps


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
        os.environ["ATHENA_BINARY"] = str(self.stubs / "rex")
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
