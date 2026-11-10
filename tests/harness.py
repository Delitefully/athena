"""Every test runs with an isolated HOME, a scratch state dir and the fake backend, with four separate walls:

1. An OS sandbox (scripts/test.sh) for this process and every child, Python or not. Importing this outside it stops the run.
2. world.py is shut: a fresh state dir's config never sets rex.real_world, and no environment variable can. Each test ends
   by checking that it still is.
3. The named tools resolve to stubs that record their calls, and those stubs come first on PATH. The card is a fake folder.
4. A canary, an audit hook in this process and in bin/athena-rex children, stops and records any reach for a real tool, a
   guarded path or the network. A test whose log has anything in it fails, whatever the test itself says.
"""
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from athena_rex.kernel import canary, caps, config, lab, store, world  # noqa: E402

BIN = ROOT / "bin" / "athena-rex"
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@localhost", "-c", "protocol.file.allow=always"]
canary.install()


def _require_the_sandbox():
    """Asks the sandbox itself, by a write only it forbids. Only its own answer, EPERM on the expected kind of path, counts:
    a missing folder or an unwritable one says nothing about a sandbox, and an environment variable could name either."""
    base = f"/private/tmp/claude-{os.getuid()}"
    probe = os.environ.get("ATHENA_SANDBOX_PROBE_WRITE") or ""
    if os.path.dirname(probe) == base and os.path.basename(probe).startswith(".ar-sandbox-probe."):
        try:
            with open(probe, "w"):
                pass
            os.unlink(probe)
        except OSError as exc:
            if exc.errno == errno.EPERM:
                return
    sys.exit("tests run only through scripts/test.sh: no sandbox around this run")


_require_the_sandbox()
SANDBOX = {k: os.environ.get(f"ATHENA_SANDBOX_{k}") for k in ("PROBE_WRITE", "PROBE_EXEC", "OUTSIDER", "PROFILE", "SOCKETS")}


def guarded() -> list:
    """Its own list: an installed copy can take lab.LIVE_PATHS out, and this must not go with it."""
    home = lab.real_home()
    return [os.path.join(home, ".local/state/athena"),
            os.path.join(home, "Library/Application Support/athena"), "/Volumes", os.path.join(home, ".claude"),
            os.path.join(home, ".claude.json"), os.path.join(home, ".claude.json.lock"),
            os.path.join(home, "Library/LaunchAgents")]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *GIT_ID, *args], capture_output=True, text=True, check=True).stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Case(unittest.TestCase):
    extra_guarded = ()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-", dir=os.environ.get("TMPDIR"))).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.stop_own_dashes)  # cleanups run last first: this one before its tmp, and the pid files in it, go
        self.home = self.tmp / "home"
        self.state = self.tmp / "state"
        self.stubs = self.tmp / "stubs"
        self.home.mkdir()
        self.state.mkdir()
        self.stubs.mkdir()
        self.calls = self.tmp / "world-calls.log"
        self.canary_log = self.tmp / "canary.log"
        (self.stubs / world.STUB_MARK).write_text("")
        for tool in world.TOOLS:
            self.stub(tool)
        app = self.tmp / "menubar" / "AthenaRexMenu.app" / "Contents" / "MacOS" / "AthenaRexMenu"
        app.parent.mkdir(parents=True)
        app.write_text("#!/bin/sh\nexit 0\n")  # a built menu bar app, so `services install` has nothing to compile
        app.chmod(0o755)
        no_processes = self.stubs / "no-processes"
        no_processes.write_text('#!/bin/sh\necho "ps $*" >> "$ATHENA_WORLD_CALLS.ps"\nexit 1\n')
        no_processes.chmod(0o755)
        loads = self.stubs / "sandbox-exec"
        loads.write_text('#!/bin/sh\necho "sandbox-exec $*" >> "$ATHENA_WORLD_CALLS.sandbox-exec"\nexit 0\n')
        loads.chmod(0o755)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in list(os.environ):
            if key.startswith(("ATHENA_", "REX_", "HERDR_", "CLAUDE_")):
                del os.environ[key]
        for place in (self.home, self.state):
            (place / lab.SCRATCH_MARK).write_text("")
        (self.tmp / "cards").mkdir()
        (self.tmp / "pads").mkdir()
        os.environ.update(
            ATHENA_WORKER="rex-harness", HOME=str(self.home), ATHENA_STATE_DIR=str(self.state), ATHENA_BACKEND="fake",
            ATHENA_FAKE_BACKEND=str(self.tmp / "fake.json"), ATHENA_CLAUDE="true", ATHENA_BUDGET_FAKE="80,500,1.0",
            ATHENA_WORLD_STUBS=str(self.stubs), ATHENA_WORLD_CALLS=str(self.calls), ATHENA_CARD_DIR=str(self.tmp / "cards"),
            ATHENA_SCRATCHPADS=str(self.tmp / "pads"), ATHENA_DASH_PORT=str(_free_port()),
            ATHENA_LAUNCH_AGENTS=str(self.tmp / "agents"), ATHENA_PS=str(self.stubs / "no-processes"),
            ATHENA_MENUBAR_APP=str(self.tmp / "menubar" / "AthenaRexMenu.app"),
            ATHENA_SIGNAL_SANDBOX_PROBE=str(loads),
            PATH=f"{self.stubs}:{os.environ['PATH']}")
        tmp_root = f"/private/tmp/claude-{os.getuid()}"
        canary.configure(str(self.canary_log), [*guarded(), *self.extra_guarded], [tmp_root], world.TOOLS, str(self.stubs),
                         [str(ROOT), str(self.tmp), *([SANDBOX["SOCKETS"]] if SANDBOX["SOCKETS"] else [])])
        os.environ.update(canary.env_for_children())
        self.addCleanup(self._check_door_and_canary)
        self.addCleanup(self._join_fetches)
        self.assertFalse(lab.is_live(self.state))
        self.assertNotEqual(str(self.home), lab.real_home())
        config.reset()
        config.damaged = False
        caps.CTX._backend = None
        self.assertFalse(world.enabled())

    def stop_own_dashes(self):
        """A dash a test started (doctor --fix's dashboard repair, `hq`'s, dash on) runs in a session of its own, and its pid file
        goes with this test's tmp, so scripts/test.sh's end-of-run sweep would find nothing: each is stopped here. As dash.off()
        does, only what the dash itself vouches for: its /healthz, on the port in its pid file, names that same pid and a state
        dir inside this test's tmp. A pid file alone may name a reused pid. One pid over 1, never a group; anything else is left."""
        for found in self.tmp.rglob("run/dash.json"):
            try:
                recorded = json.loads(found.read_text())
                pid, port = int(recorded["pid"]), int(recorded["port"])
                if pid <= 1 or not 0 < port < 65536:
                    continue
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    said = json.load(response)
                if said.get("pid") != pid or not str(said.get("state_dir") or "").startswith(str(self.tmp) + os.sep):
                    continue
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
                continue

    def _join_fetches(self):
        """GitHub fetches run on threads of their own; one must end inside the test that started it, never in the next one's world."""
        from athena_rex.caps import follow, prs
        for thread in (follow._fetcher, prs._fetcher):
            if thread is not None and thread.is_alive():
                thread.join(60)

    def _check_door_and_canary(self):
        try:
            self.assertFalse(world.enabled(), "this test left the door to the real world open")
        except canary.CanaryError:
            pass
        tripped = self.canary_log.read_text() if self.canary_log.exists() else ""
        if tripped:
            self.fail(f"the canary tripped: this test reached the real world:\n{tripped}")

    def stub(self, tool, output=""):
        """A stand-in that records its call, and prints `output` if told to."""
        script = self.stubs / tool
        out = f"cat <<'EOF'\n{output}\nEOF\n" if output else ""
        script.write_text(f'#!/bin/sh\necho "{tool} $*" >> "$ATHENA_WORLD_CALLS"\n{out}exit 0\n')
        script.chmod(0o755)

    def ps_calls(self) -> list:
        path = Path(f"{self.calls}.ps")
        return path.read_text().splitlines() if path.exists() else []

    def world_calls(self) -> list:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def write_config(self, **fields):
        (self.state / "config.json").write_text(json.dumps(fields))
        config.reset()

    def repo(self, name="proj") -> Path:
        """A repo with an origin, so pushed and unpushed mean something."""
        root = self.tmp / "repos" / name
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "origin.git")], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root / "main")], check=True)
        git(root / "main", "commit", "-q", "--allow-empty", "-m", "one")
        git(root / "main", "remote", "add", "origin", str(root / "origin.git"))
        git(root / "main", "push", "-q", "-u", "origin", "main")
        return root / "main"

    def worktree(self, repo, branch="t/w", dirname="w") -> Path:
        path = self.home / "worktrees" / repo.parent.name / dirname
        path.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
        git(path, "push", "-q", "-u", "origin", branch)
        return path

    def cli(self, *args, env=None):
        full = {**os.environ, **(env or {})}
        return subprocess.run([str(BIN), *args], capture_output=True, text=True, env=full, timeout=60)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def spawn_record(name, path, repo, block=True, **fields):
    from athena_rex.kernel import ledger
    record = {"repo": str(repo), "branch": "t/w", "path": str(path), "backend": "fake", **fields}
    if block:
        record.update(space="s1", block="b1")
    ledger.append("spawn", name, **record)


FIXTURE_MODULES = ROOT / "tests" / "fixtures" / "modules"


def card_writer(case):
    """The fixture module that writes a card through the door, loaded from the config list as any module is."""
    case.write_config(rex={"modules": ["card-writer"], "modules_dir": str(FIXTURE_MODULES)})
    return caps.get("card-writer")


__all__ = ["Case", "SANDBOX", "git", "dead_pid", "spawn_record", "store", "BIN", "ROOT", "card_writer"]
