"""What the supervisor's tests share: fake children (tests/fixtures/fake_child.py), a supervisor run in this process or as
the real `athena services run` with only fixture services under it, and the check, at each test's end, that every fake
child is gone. A child is told gone by taking the lock it held for its whole life, never by signalling a pid."""
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness import BIN, Case, ROOT, store

from athena.kernel import caps, supervisor

CHILD = ROOT / "tests" / "fixtures" / "fake_child.py"
FIXTURE_MODULES = ROOT / "tests" / "fixtures" / "modules"


class Supervised(Case):
    def setUp(self):
        super().setUp()
        self.kids = self.tmp / "fake"
        (self.kids / "alive").mkdir(parents=True)
        os.environ["ATHENA_FAKE_CHILDREN"] = str(self.kids)
        self.spawned = []
        self.addCleanup(self.assert_children_gone)  # first in, so it runs last: after each supervisor and process is ended

    # fake children

    def mode(self, tag, text):
        (self.kids / f"mode-{tag}").write_text(text)

    def release(self, tag_or_pid):
        (self.kids / f"release-{tag_or_pid}").write_text("")

    def starts(self, tag=None) -> list:
        """[(tag, pid, pgid, ppid)] each start noted, in order; once each child a supervisor here started has noted its own."""
        path = self.kids / "starts.log"
        deadline = time.monotonic() + 15
        while True:
            rows = [line.split() for line in path.read_text().splitlines()] if path.exists() else []
            if set(self.spawned) <= {int(r[1]) for r in rows} or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        return [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in rows if tag is None or r[0] == tag]

    def stops(self) -> list:
        path = self.kids / "stops.log"
        return path.read_text().split() if path.exists() else []

    def alive(self, tag=None) -> list:
        """The pids of fake children alive now, by the lock each holds while it lives."""
        out = []
        for lock in sorted((self.kids / "alive").glob("*.lock")):
            name, pid = lock.stem.rsplit("-", 1)
            if tag is not None and name != tag:
                continue
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                out.append(int(pid))
            finally:
                os.close(fd)
        return out

    def wait_for(self, what, timeout=15.0, message=""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = what()
            if got:
                return got
            time.sleep(0.05)
        self.fail(message or "waited in vain")

    def assert_children_gone(self):
        left = self.alive()
        deadline = time.monotonic() + 10
        while left and time.monotonic() < deadline:
            time.sleep(0.1)
            left = self.alive()
        self.assertEqual(left, [], "a fake child outlived its test")

    # a supervisor in this process

    def supervisor(self, services, lock=False) -> supervisor.Supervisor:
        """Its children are fake, run as `python fake_child.py <argv>`; each test's cleanup stops every one, through the
        handles it holds."""
        held = supervisor.Supervisor(services, [sys.executable, CHILD], store.ensure("run"), store.path("logs"),
                                     store.path("services-stopped.json"), env=dict(os.environ))
        self.addCleanup(held.stop_all)
        real = held._start

        def start(child, now):
            real(child, now)
            if child.proc is not None and child.proc.pid not in self.spawned:
                self.spawned.append(child.proc.pid)
        held._start = start
        if lock:
            fd = held.lock()
            self.assertIsNotNone(fd)
            self.addCleanup(os.close, fd)
        return held

    def stepping(self, held, every=0.05):
        """Its loop on a thread of this process, as `services run` runs it, until the test ends."""
        done = threading.Event()

        def loop():
            while not done.is_set():
                held.step()
                done.wait(every)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()

        def end():
            done.set()
            thread.join(30)
        self.addCleanup(end)
        return thread

    def exit_of(self, held, name, timeout=10):
        child = next(c for c in held.children if c.name == name)
        if child.proc is not None:
            child.proc.wait(timeout)
        return child

    # the real `athena services run`, with fixture services only

    def services_run(self, listed) -> subprocess.Popen:
        """`athena services run` as launchd runs it, in this test's process group, with only fake services: no capability
        but `services` loads, so no real dash, watch or menu bar can be its child."""
        self.write_config(rex={"caps": ["services"], "modules": ["fake-services"], "modules_dir": str(FIXTURE_MODULES)})
        env = {**os.environ, "ATHENA_FAKE_SERVICES": json.dumps(listed)}
        log = open(self.tmp / "services-run.log", "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen([str(BIN), "services", "run"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)

        def end():
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.addCleanup(end)
        return proc

    def run_log(self) -> str:
        path = self.tmp / "services-run.log"
        return path.read_text(errors="replace") if path.exists() else ""


def fake(name, every=None, keep_alive=True, interactive=False):
    return caps.Service(name, [name], every, keep_alive, interactive)


__all__ = ["Supervised", "fake", "CHILD", "FIXTURE_MODULES"]
