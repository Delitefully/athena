"""One stateless pass over everything athena needs. What to look at comes from each capability's `checks`; this owns only
the rules: a grace before acting, at most three repairs an hour, backing off, and health.json for whoever reads it.

Its memory lives under the added `rex` key of doctor.json, so the old athena's keys there stay untouched.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from athena_rex.kernel import agents, budget, caps, config, ledger, store, world
from athena_rex.kernel.backend import BackendError

SUMMARY = "check everything athena needs; --fix repairs what it safely can"
REPAIRS_PER_HOUR = 3
BACKOFF = (120, 240)
DISK_FLOOR_GB = 10
MEMORY_FLOOR = 10
GRACE = float(os.environ.get("ATHENA_DOCTOR_GRACE") or 90)
HELLO_WITHIN = 180
TELL_AGAIN = 3600
DOCTOR_STALE = 300
DOCTOR_LIMIT = 240  # a pass past this ends itself, so its lock is free for the next; ATHENA_DOCTOR_LIMIT for tests
BACKSTOP = 30  # past the limit by this, a pass the alarm could not reach exits outright


class GaveUp(BaseException):
    """Not an Exception, so the `except Exception` around each check and repair can't swallow it."""


def _workers_hooks(ctx) -> list:
    """Rex runs the sessions: doctor never brings a worker back. It only looks that a running one's hooks said hello."""
    workers = [w for w in ledger.live() if ledger.managed(w) and w.get("block") and not ledger.is_retiring(w)]
    running = agents.running([w["name"] for w in workers])
    return [check for w in workers if w["name"] in running for check in _said_hello(w)]


def _said_hello(worker) -> list:
    """A running Claude whose hooks never fired has no gate and no status, and otherwise looks fine."""
    name = worker["name"]
    began = float(worker.get("revived") or worker.get("spawned") or 0)
    if worker.get("goal_pending") or time.time() - began < HELLO_WITHIN or store.path("workers", f"{name}.json").exists():
        return []
    return [caps.Check(f"hooks:{name}", False, f"no SessionStart in {HELLO_WITHIN}s: its hooks aren't running, so it has no report gate and no status")]


def checks(ctx):
    out = []
    backend = ctx.backend
    try:
        up = backend.up()
        out.append(caps.Check("server", up, f"{backend.name} {'running' if up else backend.why_down()}: Rex runs it, so doctor only says so"))
        if up:
            out.extend(_workers_hooks(ctx))
    except BackendError as exc:
        out.append(caps.Check("server", False, f"{backend.name} did not answer in time ({exc}): slow isn't dead, so nothing is touched"))
    r = budget.readings()
    out.append(caps.Check("disk", r["disk_gb"] >= DISK_FLOOR_GB, f"{r['disk_gb']} GB free" + ("" if r["disk_gb"] >= DISK_FLOOR_GB else ": `athena-rex disk` lists the caches that can go, `athena-rex workspaces` the worktrees")))
    enough = (r["memory"] or 100) >= MEMORY_FLOOR
    big = None if enough else agents.largest([w["name"] for w in ledger.live()])
    out.append(caps.Check("memory", enough, f"{r['memory']}% free" + (f"; the biggest worker is {big[0]}, {big[1]} MB" if big else "")))
    out.append(_ledger_check())
    config.load()
    newer = config.newer_than_known()
    trouble = "config.json does not parse; using the last good copy" if config.damaged else (
        f"config.json is version {newer}, newer than this build knows" if newer else "")
    out.append(caps.Check("config", not trouble, trouble or "ok"))
    if not config.damaged:
        try:
            config.remember_good()
        except OSError:
            pass
    return out


def _ledger_check():
    """A lost ledger comes back from the snapshot, and until then launches stop. Damage is told once, not on every pass for ever."""
    lost = ledger.lost()
    if lost:
        return caps.Check("ledger", False, f"lost {', '.join(sorted(lost))}: the ledger was deleted or cut short", ledger.restore)
    damaged = [str(w.get("name")) for w in ledger.damaged()]
    count, new = ledger.unreadable()
    try:
        ledger.remember()
    except OSError:
        pass
    if damaged:
        return caps.Check("ledger", False, f"no spawn line for {', '.join(damaged)}")
    return caps.Check("ledger", not new, f"{count} unreadable lines" if count else "ok")


def _load() -> dict:
    data = store.read_dict(store.path("doctor.json"))
    return data.get("rex") or {}


def _save(memory: dict):
    data = store.read_dict(store.path("doctor.json"))
    data["rex"] = memory
    try:
        store.write_json(store.path("doctor.json"), data, indent=1)
    except OSError:
        pass


def notify(problems, memory, now=None):
    """One notification per new problem, and again each hour it stays down. `notified` is kept apart from `down`, so a hand run
    never swallows it."""
    now = time.time() if now is None else now
    told = memory.setdefault("notified", {})
    new = [p for p in problems if p["check"] not in told or now - float(told[p["check"]]) >= TELL_AGAIN]
    for gone in set(told) - {p["check"] for p in problems}:
        told.pop(gone)
    if not new or not world.available("osascript"):
        return
    body = "; ".join(f"{p['check']}: {p['detail']}" for p in new)[:200].replace('"', "'").replace("\\", "")
    try:
        world.run("osascript", "-e", f'display notification "{body}" with title "athena doctor"', timeout=10)
        told.update({p["check"]: now for p in new})
    except (OSError, subprocess.SubprocessError):
        pass


def may_repair(memory, subject, now) -> tuple:
    tried = [t for t in memory.get("repairs", {}).get(subject, []) if now - t < 3600]
    if len(tried) >= REPAIRS_PER_HOUR:
        return False, f"{REPAIRS_PER_HOUR} repairs in the last hour"
    if tried:
        wait = BACKOFF[min(len(tried) - 1, len(BACKOFF) - 1)]
        if now - tried[-1] < wait:
            return False, f"backing off for {int(wait - (now - tried[-1]))}s"
    return True, ""


def run(ctx, fix=True, now=None) -> dict:
    now = time.time() if now is None else now
    store.sweep_temps()
    memory = _load()
    down = memory.setdefault("down", {})
    results, problems, actions = {}, [], []
    gathered = []
    strained = None
    for module in caps.loaded():
        finder = getattr(module, "checks", None)
        if not finder:
            continue
        try:
            gathered.extend(finder(ctx) or [])
        except Exception as exc:
            gathered.append(caps.Check(module.__name__.rsplit(".", 1)[-1], False, f"{type(exc).__name__}: {exc}"))
    for check in gathered:
        results[check.name] = {"ok": check.ok, "detail": check.detail}
        if check.ok:
            down.pop(check.name, None)
            continue
        since = float(down.setdefault(check.name, now))
        detail = check.detail
        if fix and check.repair and now - since >= check.grace:
            allowed, why = may_repair(memory, check.name, now)
            strained = budget.strained() if strained is None else strained
            if allowed and strained:
                allowed, why = False, f"holding repairs: {strained}"
            if allowed:
                memory.setdefault("repairs", {}).setdefault(check.name, []).append(now)
                try:
                    check.repair()
                    actions.append(f"repaired {check.name}")
                except Exception as exc:
                    detail += f"; repair failed: {exc}"
            else:
                detail += f"; {why}"
        elif fix and check.repair:
            detail += f"; waiting out a {int(check.grace)}s grace"
        problems.append({"check": check.name, "detail": detail, "since": since})
    for vanished in set(down) - set(results):  # a worker respawned later starts a new grace
        down.pop(vanished)
    memory["repairs"] = {k: [t for t in v if now - t < 3600] for k, v in (memory.get("repairs") or {}).items()}
    if fix:
        notify(problems, memory, now)
    _save(memory)
    hq = ctx.cap("hq")
    try:
        hq_alive = bool(hq and hq.alive())
    except agents.Unknown:  # true only when HQ is confirmed alive: the clamshell daemon keeps the Mac awake on it
        hq_alive = False
    health = {"ts": time.time(), "ok": not problems, "problems": problems, "checks": results, "actions": actions,
              "hq_alive": hq_alive}
    try:
        store.write_json(store.path("health.json"), health)
    except OSError as exc:
        print(f"health.json not written: {exc}", file=sys.stderr)
    return health


def tick(ctx, now):
    """The dead-man check for doctor lives in the other supervised process: the watch raises it, once, until doctor is back."""
    from athena_rex.caps.watch import Line
    health = store.read_json(store.path("health.json"), None)
    if not isinstance(health, dict):
        return []
    told = store.path("run", "doctor-dead.json")
    age = now - float(health.get("ts") or 0)
    if age < DOCTOR_STALE:
        told.unlink(missing_ok=True)
        return []
    if told.exists():
        return []
    store.write_json(told, {"ts": now})
    return [Line(f"doctor has not reported for {int(age // 60)} min", "wake")]


def last(now=None) -> dict:
    """The menu bar's contract, from the last pass's health.json, running nothing:
    {"ran", "ts", "age", "ok", "checks": [{"name", "ok", "detail"}]}, a failing check's detail with what its repair said."""
    health = store.read_json(store.path("health.json"), None)
    if not isinstance(health, dict) or not isinstance(health.get("checks"), dict):
        return {"ran": False, "ts": None, "age": None, "ok": None, "checks": []}
    said = {p.get("check"): p.get("detail") for p in health.get("problems") or [] if isinstance(p, dict)}
    rows = [{"name": name, "ok": bool(row.get("ok")), "detail": str(said.get(name) or row.get("detail") or "")}
            for name, row in sorted(health["checks"].items()) if isinstance(row, dict)]
    ts = float(health.get("ts") or 0)
    now = time.time() if now is None else now
    return {"ran": True, "ts": ts, "age": max(0, int(now - ts)), "ok": all(r["ok"] for r in rows), "checks": rows}


def _configure(p):
    p.add_argument("--fix", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--last", action="store_true", help="the last pass's result, running nothing (with --json: the menu bar's)")


def _run(args):
    if args.last:
        out = last()
        if args.json:
            print(json.dumps(out, indent=1))
        elif not out["ran"]:
            print("doctor has not run on this state dir yet")
        else:
            print(f"last ran {out['age']}s ago")
            for row in out["checks"]:
                print(f"{'ok ' if row['ok'] else 'BAD'} {row['name']:<28} {row['detail']}")
        return 0
    limit = _limit()
    try:
        with _time_limit(limit), store.locked("doctor.lock", blocking=False, full_disk_ok=True) as got:
            if not got:
                if args.json:  # scripts read this, and an empty answer looked like a doctor that died
                    print(json.dumps({"busy": True, "detail": "another doctor is running"}))
                else:
                    print("another doctor is running", file=sys.stderr)
                return 0
            health = run(caps.CTX, fix=args.fix and got is not store.UNHELD)
    except GaveUp:
        print(f"doctor gave up after {limit:g}s: a check ran past the limit; its lock is free for the next pass", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(health, indent=1))
    elif not args.quiet:
        for name, row in sorted(health["checks"].items()):
            print(f"{'ok ' if row['ok'] else 'BAD'} {name:<28} {row['detail']}")
        for action in health["actions"]:
            print(f"did {action}")
    elif health["problems"]:
        for problem in health["problems"]:
            print(f"{problem['check']}: {problem['detail']}", file=sys.stderr)
    return 0 if health["ok"] else 1


def _limit() -> float:
    try:
        return max(float(os.environ.get("ATHENA_DOCTOR_LIMIT") or DOCTOR_LIMIT), 0.1)
    except ValueError:
        return DOCTOR_LIMIT


class _time_limit:
    """A pass that hangs held the lock, and every later pass said "another doctor is running" for as long as it hung. Past the limit
    an alarm raises GaveUp in this thread; the lock, an flock, goes as the `with` unwinds. Should the alarm not get through, the
    backstop exits this process, which frees the lock all the same. Doctor ends only itself. Armed only here, at the command, never
    in a pass HQ runs in-process, and disarmed on the way out."""

    def __init__(self, limit):
        self.limit = limit
        self.armed = threading.current_thread() is threading.main_thread()

    def __enter__(self):
        if self.armed:
            self.previous = signal.signal(signal.SIGALRM, self._alarm)
            signal.setitimer(signal.ITIMER_REAL, self.limit)
            self.backstop = threading.Timer(self.limit + BACKSTOP, self._exit)
            self.backstop.daemon = True
            self.backstop.start()
        return self

    def __exit__(self, *exc):
        if self.armed:
            self.armed = False
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous)
            self.backstop.cancel()
        return False

    def _alarm(self, signum, frame):
        if self.armed:
            raise GaveUp()

    def _exit(self):
        sys.stderr.write(f"doctor gave up after {self.limit + BACKSTOP:g}s and exits: a check did not let go\n")
        sys.stderr.flush()
        os._exit(1)


def services(ctx):
    return [caps.Service("doctor", ["doctor", "--fix", "--quiet"], every=60, keep_alive=False)]


def commands():
    return {"doctor": caps.Command(SUMMARY, _configure, _run)}
