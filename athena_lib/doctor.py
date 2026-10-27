"""athena doctor: one look at everything athena needs running, a repair for what it safely can, and health.json.

Run every minute by the doctor service (`athena service install`), and by a bare `athena`. Each run is a stateless
tick over files and herdr; doctor.json in the state dir keeps only what spans ticks: when a part was first seen
down, the repairs tried (so a repair that keeps failing backs off and then stops, flagged), and what was sent.

What it checks, and what `--fix` does about each:
- herdr's server: started (the herdr service's job when installed, else `herdr server` detached).
- the dashboard and the board watcher: started the same way, the dashboard bound to no HQ.
- HQ: a Claude gone from HQ's pane is brought back on its saved conversation (`--resume`), never a fresh one, once
  it has been gone for a grace period (herdr resumes its own agents after a restart; doctor does not race it).
- the watch (run by the athena-watch mod in HQ): watch.json not written for WATCH_STALE seconds while HQ runs means
  the mod's loop is gone; HQ is restarted on the same conversation, which starts the mod again.
- messaging sockets: a live session whose socket file is gone is restarted on the same conversation (HQ through
  `athena hq restart`, a worker in place) once it is idle, so SendMessage reaches it again.
- a resumed HQ that got no start routine gets it; a worker herdr resumed in the middle of a turn is told to go on.
- HQ's context past ROTATE_PERCENT: HQ is asked, once, to write its in-flight threads and rotate.
- free disk and memory: flagged, never fixed.
- while a worker is mid-turn on AC power (macOS): a short `caffeinate -i`, renewed each tick, so a dead doctor never
  leaves the Mac awake.
Nothing is typed into a pane that is busy or whose input box holds unsent text. Hook state alone never counts: a
worker's state is checked against herdr before anything is done to it.
"""
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time

from athena_lib import board, composer, dash, handoff, herdr, hookstatus, hq, launch, ledger, paths, retire, service, \
    sockets

WATCH_STALE = 5 * 60  # watch.json ticks every 20 s
ROTATE_PERCENT = handoff.HINT_PERCENT
ROTATE_ASK_EVERY = 2 * 3600
RESUMED_FRESH = 30 * 60  # a resume older than this is no longer "just resumed"
ATTEMPTS_MAX = 3  # repairs of one part per ATTEMPT_WINDOW before doctor stops and flags it
ATTEMPT_WINDOW = 3600
DISK_MIN_GB = 10
MEMORY_MIN_PERCENT = 10
CAFFEINATE_SECONDS = 180

ROTATE_ASK = ("athena doctor: your context is {ctx}. Write your in-flight threads (what you promised, what you wait "
              "on, what is half done) to {threads}, then run `athena hq rotate`. It rotates once you are idle, never "
              "while the human is typing.")
NO_SESSION = ("HQ's conversation id is not known, and doctor never starts a fresh HQ: restart it by hand "
              "(`athena hq restart`)")
CONTINUE = ("athena doctor: herdr restarted and resumed this session in the middle of your turn. Carry on from where "
            "you were, by your brief.")


def _grace() -> float:
    return float(os.environ.get("ATHENA_DOCTOR_GRACE") or 90)


def _state_file():
    return paths.ensure() / "doctor.json"


def health_file():
    return paths.ensure() / "health.json"


def _load(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path, data):
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    f = open(paths.ensure() / "doctor.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        yield False
        return
    try:
        yield True
    finally:
        f.close()


class Doctor:
    def __init__(self, fix=True, now=None):
        self.fix = fix
        self.now = time.time() if now is None else now
        self.state = _load(_state_file())
        self.checks = {}
        self.actions = []

    # -- bookkeeping

    def record(self, name, ok, detail="", fixed=None):
        self.checks[name] = {"ok": bool(ok), "detail": detail, **({"fixed": fixed} if fixed is not None else {})}

    def act(self, text):
        self.actions.append(text)

    def down_for(self, subject) -> float:
        """Seconds since `subject` was first seen down this stretch; marks it down now."""
        since = self.state.setdefault("down", {}).setdefault(subject, self.now)
        return self.now - since

    def up(self, subject):
        self.state.setdefault("down", {}).pop(subject, None)

    def may_repair(self, subject):
        """None when a repair may run now, else why not: backing off after a failed one, or the cap is reached."""
        tries = [t for t in self.state.setdefault("attempts", {}).get(subject, []) if self.now - t < ATTEMPT_WINDOW]
        self.state["attempts"][subject] = tries
        if len(tries) >= ATTEMPTS_MAX:
            return f"gave up after {len(tries)} repairs in the last hour; fix it by hand"
        if tries and self.now - tries[-1] < 120 * 2 ** (len(tries) - 1):  # 2 min, then 4
            return f"backing off after {len(tries)} repair{'s' if len(tries) > 1 else ''}"
        return None

    def tried(self, subject):
        self.state.setdefault("attempts", {}).setdefault(subject, []).append(self.now)

    def repaired(self, subject):
        """`subject` is up. Its repairs stay counted for ATTEMPT_WINDOW: a part that fails again right after each
        repair (a watch that stalls every few minutes) is still held to ATTEMPTS_MAX an hour, then flagged."""
        attempts = self.state.setdefault("attempts", {})
        tries = [t for t in attempts.get(subject, []) if self.now - t < ATTEMPT_WINDOW]
        if tries:
            attempts[subject] = tries
        else:
            attempts.pop(subject, None)
        self.up(subject)

    def repair(self, name, subject, fn, what):
        """Run a repair under the backoff. Records the check as fixed, or why not."""
        if not self.fix:
            self.record(name, False, what + " (run with --fix to repair)")
            return False
        why = self.may_repair(subject)
        if why:
            self.record(name, False, f"{what}: {why}")
            return False
        self.tried(subject)
        try:
            detail = fn()
        except Exception as exc:  # a repair that fails is a finding, never a crash
            self.record(name, False, f"{what}; repair failed: {exc}")
            return False
        self.act(f"{name}: {detail}")
        self.record(name, True, detail, fixed=True)
        return True

    # -- the checks

    def check_herdr(self) -> bool:
        if launch.server_running():
            self.repaired("herdr")
            self.record("herdr", True, "server running")
            return True

        def start():
            if service.start("herdr"):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not launch.server_running():
                    time.sleep(0.5)
                if not launch.server_running():
                    raise RuntimeError("the herdr service did not bring the server up")
                return "started the herdr service"
            launch.start_server()
            return "started herdr's server"
        if self.repair("herdr", "herdr", start, "herdr's server is not running"):
            self.state["herdr_started"] = self.now
            return True
        return False

    def check_dash(self):
        rec = dash.running()
        if rec and dash._health(rec.get("port") or dash.port()):
            self.repaired("dash")
            self.record("dash", True, rec.get("url") or "running")
            return

        def start():
            if service.start("dash"):
                return "started the dashboard service"
            return f"started the dashboard at {dash.start(hq=0)['url']}"
        self.repair("dash", "dash", start, "the dashboard is not running")

    def check_board(self):
        pid = board.running_pid()
        if pid:
            self.repaired("board")
            self.record("board", True, f"watcher pid {pid}")
            return

        def start():
            if service.start("board"):
                return "started the board service"
            return f"started the board watcher (pid {board.start_watcher()})"
        self.repair("board", "board", start, "the board watcher is not running")

    def hq_record(self):
        return hq.load()

    def hq_agent(self):
        """HQ's agent as herdr sees it (name, agent_status), or None when its pane runs no HQ."""
        pane = self.hq_record().get("pane")
        if not pane:
            return None
        try:
            agent = herdr.call("agent", "get", pane, timeout=10).get("agent", {})
        except herdr.HerdrError:
            return None
        return agent if agent.get("name") == paths.hq_name() else None

    def check_hq(self):
        record = self.hq_record()
        if not record.get("pane") and not record.get("session"):
            self.record("hq", True, "no HQ yet (athena hq starts one)")
            return None
        agent = self.hq_agent()
        if agent:
            live = agent.get("agent_session") or {}
            if live.get("kind") == "id" and hq.sync_session(live.get("value")):
                self.act(f"hq: recorded HQ's conversation {live['value']} (it changed while HQ ran, as after /clear)")
            self.repaired("hq")
            self.record("hq", True, f"{agent.get('agent_status') or 'running'} in pane {record.get('pane')}")
            return agent
        if not hq.own_session(record):
            self.record("hq", False, "HQ's Claude is gone and no conversation of it is known; start one with athena hq")
            return None
        gone = self.down_for("hq")
        if gone < _grace():
            self.record("hq", False, f"HQ's Claude is gone ({int(gone)}s); waiting {int(_grace())}s for herdr to "
                                     "resume it before doctor does")
            return None

        def resume():
            result = hq.hq(focus=False, start_board=False, start_dash=False, fresh=False)
            if result.get("missing"):
                raise RuntimeError("no conversation to resume")
            return f"resumed HQ's conversation {hq.load().get('session') or hq.own_session(record)} in pane {result.get('pane')}"
        self.repair("hq", "hq", resume, "HQ's Claude is gone")
        return None

    def idle(self, agent) -> bool:
        return (agent or {}).get("agent_status") in ("idle", "done")

    def quiet_pane(self, name, agent) -> bool:
        """Idle, and nothing typed in its input box: the only state anything is typed into."""
        return self.idle(agent) and not composer.typing(name)

    def check_watch(self, agent):
        if not agent:
            return
        try:
            ts = float(json.loads((paths.state_dir() / "watch.json").read_text()).get("ts") or 0)
        except (OSError, ValueError, TypeError):
            ts = 0
        age = self.now - ts if ts else None
        if age is not None and age < WATCH_STALE:
            self.repaired("watch")
            self.record("watch", True, f"ticked {int(age)}s ago")
            return
        what = f"the watch has not ticked for {int(age // 60)} min" if age is not None else "the watch has never ticked"
        if self.down_for("watch") < _grace():
            self.record("watch", False, what)
            return
        if not self.hq_session():
            self.record("watch", False, f"{what}; {NO_SESSION}")
            return
        if not self.idle(agent):
            self.record("watch", False, f"{what}; HQ is restarted at its next idle")
            return
        self.repair("watch", "watch", lambda: self._restart_hq("restarting HQ on its conversation, which starts the "
                                                             "athena-watch mod again"), what)

    def hq_session(self):
        """HQ's conversation id as athena trusts it (`hq.trusted_session`); None when none is known."""
        return hq.trusted_session(self.hq_record())

    def _restart_hq(self, why):
        # Detached; it waits for idle and an empty input box itself. Unattended: with no known conversation, or with
        # background work running in HQ, it leaves HQ as it is (never a fresh HQ, never "Exit and stop tasks").
        result = hq.restart(update=False, unattended=True)
        return f"{why} (pid {result['pid']})"

    def check_sockets(self, agent):
        if agent:
            path = sockets.hq_missing()
            if path:
                if self.down_for("socket:hq") >= _grace() and not self.hq_session():
                    self.record("socket:hq", False, f"HQ's messaging socket is gone ({path}); {NO_SESSION}")
                elif self.down_for("socket:hq") >= _grace() and not self.idle(agent):
                    self.record("socket:hq", False, f"HQ's messaging socket is gone ({path}); HQ is restarted at its "
                                                    "next idle")
                elif self.down_for("socket:hq") >= _grace():
                    self.repair("socket:hq", "socket:hq", lambda: self._restart_hq("restarting HQ on its conversation "
                                                                                   "for a new socket"),
                                f"HQ's messaging socket is gone ({path})")
                else:
                    self.record("socket:hq", False, f"HQ's messaging socket is gone ({path})")
            else:
                self.repaired("socket:hq")
        for w in ledger.live():
            name = w["name"]
            path = sockets.worker_missing(name)
            if not path:
                self.repaired(f"socket:{name}")
                continue
            what = f"{name}'s messaging socket is gone ({path})"
            if self.down_for(f"socket:{name}") < _grace():
                self.record(f"socket:{name}", False, what)
                continue
            agent_w = self.worker_agent(w)
            if not agent_w:
                self.record(f"socket:{name}", False, f"{what}; herdr does not run {name}")
                continue
            if not self.quiet_pane(name, agent_w):
                self.record(f"socket:{name}", False, f"{what}; restarted once it is idle with an empty input box")
                continue
            self.repair(f"socket:{name}", f"socket:{name}", lambda w=w: self._restart_worker(w), what)

    def worker_agent(self, w):
        try:
            agent = herdr.call("agent", "get", w["name"], timeout=10).get("agent", {})
        except herdr.HerdrError:
            return None
        return agent if agent.get("name") == w["name"] else None

    def _restart_worker(self, w):
        name = w["name"]
        session = (hookstatus.read(name) or {}).get("session_id")
        args = hookstatus._resume_args(name, w)
        if not session or not args or not w.get("pane"):
            raise RuntimeError(f"{name}'s conversation or start command is not known")
        try:
            exited = retire.exit_claude(name, float(os.environ.get("ATHENA_EXIT_GRACE") or 20), stop_tasks=False)
        except retire.BackgroundWork as exc:
            raise RuntimeError(f"{exc}; tried again later") from exc
        if not exited:
            raise RuntimeError(f"{name}'s Claude did not exit")
        herdr.call("agent", "start", name, "--kind", "claude", "--pane", w["pane"], "--timeout", "120000", "--",
                   "--resume", session, *args, timeout=200)
        return f"restarted {name} on its conversation {session} for a new socket"

    def check_routine(self, agent):
        """A resumed HQ that got no start routine (herdr resumed it, or a resume outside athena) gets it."""
        if not agent:
            return
        start = _load(paths.state_dir() / "hq-start.json")
        record = self.hq_record()
        ts = float(start.get("ts") or 0)
        if start.get("source") != "resume" or ts <= float(record.get("routine_at") or 0):
            return
        if self.now - ts > RESUMED_FRESH or not self.fix:
            return
        if not self.quiet_pane(paths.hq_name(), agent):
            return
        hq._routine(hq.RESTART_ROUTINE)
        self.act("hq: sent the start routine to the resumed HQ")

    def check_resumed_workers(self):
        """A worker herdr resumed while it was mid-turn is told once to carry on; never a BLOCKED or DONE one."""
        told = self.state.setdefault("continued", {})
        for w in ledger.live():
            name = w["name"]
            hook = hookstatus.read(name) or {}
            if hook.get("event") != "SessionStart" or hook.get("source") != "resume" or \
                    hook.get("resumed_from") != "working":
                continue
            ts = float(hook.get("ts") or 0)
            if told.get(name) == ts or self.now - ts > RESUMED_FRESH or not self.fix:
                continue
            agent = self.worker_agent(w)
            if not agent or not self.quiet_pane(name, agent):
                continue
            herdr.call("agent", "prompt", name, CONTINUE, timeout=30)
            told[name] = ts
            self.act(f"{name}: resumed mid-turn; told it to carry on")

    def check_context(self, agent):
        try:
            ctx = handoff.hq_context()
        except Exception:
            ctx = None
        if not ctx:
            return
        if ctx["percent"] < ROTATE_PERCENT:
            self.record("hq context", True, handoff.describe(ctx))
            return
        self.record("hq context", True, f"{handoff.describe(ctx)}: past {ROTATE_PERCENT}%, rotation asked for")
        last = float(self.state.get("rotate_asked") or 0)
        if not self.fix or not agent or self.now - last < ROTATE_ASK_EVERY or not self.quiet_pane(paths.hq_name(), agent):
            return
        herdr.call("agent", "prompt", paths.hq_name(),
                   ROTATE_ASK.format(ctx=handoff.describe(ctx), threads=handoff.threads_file()), timeout=30)
        self.state["rotate_asked"] = self.now
        self.act(f"hq: context {handoff.describe(ctx)}; asked HQ to write its threads and rotate")

    def check_disk(self):
        try:
            usage = shutil.disk_usage(os.path.expanduser("~"))
        except OSError:
            return
        free = usage.free / 1e9
        self.record("disk", free >= DISK_MIN_GB, f"{free:.0f} GB free" + ("" if free >= DISK_MIN_GB else
                    f", under {DISK_MIN_GB} GB: clean up (worktrees of retired workers, caches)"))

    def check_memory(self):
        level = memory_free_percent()
        if level is None:
            return
        self.record("memory", level >= MEMORY_MIN_PERCENT, f"{level}% free" + ("" if level >= MEMORY_MIN_PERCENT else
                    f", under {MEMORY_MIN_PERCENT}%: retire parked workers"))

    def caffeinate(self):
        if sys.platform != "darwin" or not self.fix:
            return
        busy = [w["name"] for w in ledger.live() if (hookstatus.read(w["name"]) or {}).get("state") == "working"]
        if not busy or not on_ac_power():
            return
        pid = self.state.get("caffeinate_pid")
        if pid and _alive(pid):
            return
        pid = spawn_caffeinate()
        if pid:
            self.state["caffeinate_pid"] = pid

    # -- the tick

    def guard(self, name, fn, *args):
        """Run one check: whatever it raises (a herdr call that failed, say) is that check's finding, and the
        rest still run, so health.json is always written."""
        try:
            return fn(*args)
        except Exception as exc:
            self.record(name, False, f"the check failed: {exc}")
            return None

    def run(self) -> dict:
        if self.guard("herdr", self.check_herdr):
            agent = self.guard("hq", self.check_hq)
            self.guard("watch", self.check_watch, agent)
            self.guard("sockets", self.check_sockets, agent)
            self.guard("hq routine", self.check_routine, agent)
            self.guard("resumed workers", self.check_resumed_workers)
            self.guard("hq context", self.check_context, agent)
        self.guard("dash", self.check_dash)
        self.guard("board", self.check_board)
        self.guard("disk", self.check_disk)
        self.guard("memory", self.check_memory)
        self.guard("caffeinate", self.caffeinate)
        return self.finish()

    def finish(self) -> dict:
        before = {p["check"]: p for p in _load(health_file()).get("problems", []) if isinstance(p, dict)}
        problems = []
        for name, c in self.checks.items():
            if not c["ok"]:
                since = (before.get(name) or {}).get("since") or self.now
                problems.append({"check": name, "detail": c["detail"], "since": since})
        health = {"ts": self.now, "ok": not problems, "problems": problems, "checks": self.checks,
                  "actions": self.actions}
        _save(health_file(), health)
        _save(_state_file(), self.state)
        new = [p for p in problems if p["check"] not in before]
        if new and self.fix:
            notify(new)
        return health


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def spawn_caffeinate():
    """`caffeinate -i` for CAFFEINATE_SECONDS, detached: it ends on its own, so nothing is left holding the Mac awake
    when doctor stops renewing it. Its pid, or None."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-t", str(CAFFEINATE_SECONDS)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid
    except OSError:
        return None


def on_ac_power() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "AC Power" in out


def memory_free_percent():
    """Free memory as a percentage: macOS's memorystatus level, Linux's MemAvailable. None when unknown."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            return int(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo").read().splitlines() if ":" in line)
        total, avail = int(info["MemTotal"].split()[0]), int(info["MemAvailable"].split()[0])
        return round(100 * avail / total)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def notify(problems):
    """One herdr notification for problems doctor has just found; nothing while herdr itself is down."""
    body = "; ".join(f"{p['check']}: {p['detail']}" for p in problems)[:300]
    try:
        herdr.call("notification", "show", "athena doctor", "--body", body, "--sound", "request", timeout=10)
    except herdr.HerdrError:
        pass


def run(fix=True) -> dict:
    with _lock() as held:
        if not held:
            return {"skipped": "another athena doctor is running"}
        return Doctor(fix=fix).run()


def render(health, quiet=False) -> list:
    """Lines for a person: every check, or with `quiet` only what doctor did and what is wrong."""
    if health.get("skipped"):
        return [] if quiet else [health["skipped"]]
    lines = [f"did: {a}" for a in health.get("actions", [])]
    for name, c in health.get("checks", {}).items():
        if c["ok"] and quiet:
            continue
        lines.append(f"{'ok ' if c['ok'] else 'BAD'} {name:<14} {c['detail']}")
    if quiet and lines:
        lines.insert(0, time.strftime("%Y-%m-%d %H:%M:%S"))
    return lines
