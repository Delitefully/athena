"""One process that runs athena's services as its children: what launchd did for each job, done here for all of them, so
athena is one launchd job (`local.athena`) and macOS shows one background item.

It knows nothing of what a service does: it is handed `Service`s (kernel/caps.py) and runs `program + argv` for each.

    kept alive      started at once; one that exits non-zero or on a signal starts again after a delay that grows from
                    FIRST_DELAY, doubling up to MAX_DELAY, and goes back to FIRST_DELAY once a run has lasted HEALTHY_AFTER.
                    A clean exit stays down until the supervisor next starts, as launchd's KeepAlive{SuccessfulExit: false}.
    every N         started at once and then every N seconds. A run still going when its time comes is not started twice,
                    and after a gap longer than N (the Mac slept) it runs once, not once for each interval it missed.

Each child has one state: starting, running, backing off (crashed, waiting out its delay), waiting (an `every` between
runs, or a copy from an earlier supervisor still alive) or stopped (a clean exit, or held by Quit athena).

What it is asked, it reads from files it polls, so the CLI never signals it:

    <state>/services-stopped.json   Quit athena's mark: each child it names is stopped in its order (the watchdog is
                                    first in it), and started again once the mark is gone, in the same order but with
                                    the watchdog last
    <run>/services-ask/<name>       restart that child now and clear its backoff (doctor's repair of the watch or the dash)

and it says what it did in <run>/services.json. It holds <run>/services.lock while it runs: that lock, never the
state file, is how anyone tells it runs.

It signals only children it started, through their Popen handle, before reaping them. They stay in its process group, so
launchd clears them with the job even when the supervisor itself is killed outright. Each child is handed a lock of its
own (<run>/services/<name>.lock) that it holds for as long as it lives: a supervisor that starts while a child of an
earlier one still runs finds that lock held and waits for it to go, rather than start a second copy.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STARTING, RUNNING, BACKING_OFF, WAITING, STOPPED = "starting", "running", "backing off", "waiting", "stopped"
FIRST_DELAY = 1.0
MAX_DELAY = 60.0
HEALTHY_AFTER = 60.0
STOP_WAIT = 10.0
YOUNG = 10.0  # an ask for a child started less than this ago restarts nothing: it is still coming up
TICK = 0.5
LOCK_TRIES = 60  # a probe of the lock (services.supervisor_runs) holds it for a moment; 3 s of tries outlasts it


class Child:
    def __init__(self, service):
        self.service = service
        self.name = service.name
        self.state = STARTING
        self.proc = None
        self.started = None  # when the run going now, or the last one, began
        self.retry_at = None  # backing off until then
        self.next_run = None  # an `every`'s next time
        self.delay = 0.0  # the last delay waited; 0 once a run has been healthy
        self.last_exit = None
        self.held = False  # by Quit athena
        self.blocked = False  # a copy an earlier supervisor started still holds its lock
        self.note = ""

    def row(self) -> dict:
        return {"state": self.state, "pid": self.proc.pid if self.proc else None, "every": self.service.every,
                "last_exit": self.last_exit, "delay": self.delay, "held": self.held, "note": self.note}


def _background():
    """Darwin's background policy, as launchd gave ProcessType Background: the one job is Interactive for the menu bar."""
    try:
        import ctypes
        ctypes.CDLL(None).setpriority(4, 0, 0x1000)  # PRIO_DARWIN_PROCESS, this process, PRIO_DARWIN_BG
    except Exception:
        pass


class Supervisor:
    def __init__(self, services, program, run_dir, logs_dir, mark, env=None, background=False):
        self.children = [Child(s) for s in services]
        self.program = [str(p) for p in program]
        self.run_dir = Path(run_dir)
        self.logs_dir = Path(logs_dir)
        self.mark = Path(mark)
        self.env = env
        self.background = background
        self.stopping = False
        self._written = None
        self._held_order = []

    # what it is asked

    def held(self) -> list:
        """The names Quit holds, watchdog first; [] when athena runs."""
        try:
            data = json.loads(self.mark.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            data = {}
        names = data.get("jobs") if isinstance(data, dict) else None
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else [c.name for c in self.children]

    def asks(self) -> list:
        folder = self.run_dir / "services-ask"
        out = []
        for entry in sorted(folder.iterdir()) if folder.is_dir() else []:
            out.append(entry.name)
            try:
                entry.unlink()
            except OSError:
                pass
        return out

    # one pass

    def step(self, now=None):
        now = time.time() if now is None else now
        by_name = {c.name: c for c in self.children}
        held = self.held()
        for name in held:  # Quit: in the mark's order, the watchdog first
            child = by_name.get(name)
            if child and not child.held:
                child.held = True
                self._stop(child)
                child.state, child.note = STOPPED, "stopped by Quit athena"
        if held:
            self._held_order = held
        for name in self._held_order[1:] + self._held_order[:1]:  # Start: in the mark's order but the watchdog last
            child = by_name.get(name)
            if child and child.held and name not in held:
                child.held, child.state, child.note = False, STARTING, ""
                self._start(child, now)  # here, so they start in this order
        if not held:
            self._held_order = []
        for name in self.asks():
            child = by_name.get(name)
            if not child or child.held:
                continue
            child.delay = 0.0
            if child.state == RUNNING and now - (child.started or now) < YOUNG:
                continue
            self._stop(child)
            child.state, child.note = STARTING, "restarted when asked"
        for child in self.children:
            self._reap(child, now)
            self._due(child, now)
        self._write()

    def _reap(self, child, now):
        if child.proc is None:
            return
        code = child.proc.poll()
        if code is None:
            if child.started is not None and now - child.started >= HEALTHY_AFTER:
                child.delay = 0.0
            return
        child.proc, child.last_exit = None, code
        if child.started is not None and now - child.started >= HEALTHY_AFTER:
            child.delay = 0.0
        if code != 0 and child.service.keep_alive:
            child.delay = FIRST_DELAY if not child.delay else min(child.delay * 2, MAX_DELAY)
            child.retry_at = now + child.delay
            child.state, child.note = BACKING_OFF, f"exited {code}; again in {child.delay:g}s"
        elif child.service.every:
            child.state, child.note = WAITING, f"exited {code}; next run on its interval"
        else:
            child.state, child.note = STOPPED, f"exited {code}"

    def _due(self, child, now):
        if child.held or child.state == RUNNING:
            every = child.service.every
            if child.state == RUNNING and every and child.next_run is not None and now >= child.next_run:
                while child.next_run <= now:  # still running at its time: this slot is skipped, not queued
                    child.next_run += every
            return
        every = child.service.every
        if every and child.next_run is not None and child.next_run - now > every:
            child.next_run = now + every  # the clock went back
        if child.state == BACKING_OFF and child.retry_at is not None and child.retry_at - now > MAX_DELAY:
            child.retry_at = now + child.delay
        go = (child.state == STARTING or child.blocked
              or (child.state == BACKING_OFF and now >= child.retry_at)
              or (every and child.state in (WAITING, STOPPED, BACKING_OFF) and child.next_run is not None and now >= child.next_run))
        if go:
            self._start(child, now)

    def _start(self, child, now):
        lock = self.run_dir / "services" / f"{child.name}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # a copy an earlier supervisor started still runs: wait for it, never a second one
                child.blocked, child.state, child.note = True, WAITING, "a copy from before this supervisor still runs"
                return
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(self.logs_dir / f"service-{child.name}.log", "ab") as log:
                child.proc = subprocess.Popen(
                    [*self.program, *child.service.argv], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=self.env,
                    pass_fds=(fd,), preexec_fn=_background if self.background and not child.service.interactive else None)
        except OSError as exc:
            child.proc = None
            child.delay = FIRST_DELAY if not child.delay else min(child.delay * 2, MAX_DELAY)
            child.retry_at = now + child.delay
            child.state, child.note = BACKING_OFF, f"did not start: {exc}"
            return
        finally:
            os.close(fd)  # the child holds the lock now, for as long as it lives
        child.started, child.state, child.note, child.blocked = now, RUNNING, "", False
        if child.service.every:
            child.next_run = now + child.service.every

    def _stop(self, child):
        """Only through the handle of a child this supervisor started and has not reaped."""
        proc = child.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        child.proc, child.last_exit = None, proc.returncode

    def stop_all(self):
        """Every child, at once: TERM to each, then a wait, then KILL for any that did not go."""
        running = [c for c in self.children if c.proc is not None and c.proc.poll() is None]
        for child in running:
            child.proc.terminate()
        deadline = time.monotonic() + STOP_WAIT
        for child in running:
            try:
                child.proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.proc.kill()
                child.proc.wait()
        for child in self.children:
            if child.proc is not None:
                child.proc.wait()
                child.last_exit = child.proc.returncode
            child.proc, child.state, child.note = None, STOPPED, "the supervisor stopped"
        self._write()

    def _write(self):
        rows = {c.name: c.row() for c in self.children}
        if rows == self._written:
            return
        self._written = rows
        target = self.run_dir / "services.json"
        partial = target.with_name(f".services.json.{os.getpid()}")
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            partial.write_text(json.dumps({"pid": os.getpid(), "ts": time.time(), "children": rows}, indent=1))
            os.replace(partial, target)
        except OSError:
            pass

    # the process

    def lock(self):
        """The fd that holds <run>/services.lock, or None when another supervisor holds it."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.run_dir / "services.lock", os.O_CREAT | os.O_RDWR, 0o600)
        for attempt in range(LOCK_TRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                time.sleep(0.05)
        os.close(fd)
        return None

    def main(self, tick=TICK) -> int:
        fd = self.lock()
        if fd is None:
            print("services: another supervisor runs on this state dir", file=sys.stderr)
            return 1

        def stop(signum, frame):
            self.stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        try:
            while not self.stopping:
                self.step()
                end = time.monotonic() + tick
                while not self.stopping and time.monotonic() < end:
                    time.sleep(min(0.1, tick))
        finally:
            self.stop_all()
            os.close(fd)
        return 0
