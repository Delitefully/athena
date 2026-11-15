"""Login services. Each capability lists what it runs in `services(ctx)`; this runs them all under one launchd job,
`local.athena`, whose program is `athena services run`: the supervisor (kernel/supervisor.py), which starts each service
as its child, restarts one that crashes and runs each `every` on its timer. One job is one background item, so macOS
shows the human one notice where five jobs showed five.

Writes only local.athena jobs, so the old athena's jobs stay as they are. In the lab, --dry-run is all it will do.
`install` also moves an install of the earlier layout, one job per service (`local.athena.<name>`), to the one job: each
old job is booted out and its file removed, the old doctor's last, so a move cut short is finished by its next run.

`stop` is the menu bar's Quit athena. It writes the stopped mark, and the supervisor, which polls it, stops what the mark
names: the watchdog (doctor) first, so nothing brings the rest back, then athena's other services. The mark stays across
a login, and `start` takes it away; the supervisor starts them again with the watchdog last. Rex runs the sessions, HQ's
and the workers', so Quit never stops the Rex server or any Claude. A module's services keep running, and so does the menu
bar app, which shows Start athena. Once the watch is down, each capability's and module's optional `on_quit(ctx)` runs,
so nothing it drives (a module's card) is left showing a state that has stopped changing.

This CLI reaches the supervisor only through launchctl (install, restart) or a file it polls (the stopped mark, an ask to
restart one child now). It never signals it.
"""
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from athena.cli import UserError
from athena.kernel import caps, config, lab, store, world

SUMMARY = "the background services of every enabled capability, run by one launchd agent"
PREFIX = "local.athena"  # not "athena.": that is the herdr athena's, whose jobs this must never take for its own
LABEL = PREFIX  # the one job; `local.athena.<name>` were the jobs of the layout before it
WATCHDOG = "doctor"
MENUBAR = "menubar"
STOPPED = "services-stopped.json"
CARRIED = ("ATHENA_STATE_DIR", "ATHENA_CONFIG", "ATHENA_HQ", "ATHENA_DASH_PORT", "ATHENA_SERVER", "ATHENA_BINARY")
AWAIT = 60  # how long stop and start wait for the supervisor to act
MOVE_LOCK = "locks/services-move.lock"  # held by `services install` for as long as it moves the earlier layout
MOVE = "services-move.json"  # the move's record: {"state": "running" | "failed", "since", "failed": what the last failure said}
MOVE_START = 30  # a try doctor started may take this long to take MOVE_LOCK


def agents_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("ATHENA_LAUNCH_AGENTS") or "~/Library/LaunchAgents"))


def wanted(ctx) -> list:
    out = []
    for module in caps.loaded():
        finder = getattr(module, "services", None)
        if finder:
            out.extend(finder(ctx) or [])
    return out


def _owned(ctx) -> list:
    """[(service, from a module)]: the modules loader marks a module's capability with MODULE."""
    out = []
    for module in caps.loaded():
        finder = getattr(module, "services", None)
        out.extend((s, bool(getattr(module, "MODULE", None))) for s in (finder(ctx) or [] if finder else []))
    return out


def athenas(ctx) -> list:
    """What Quit stops, Start starts and the doctor looks after: every service but a module's and the menu bar's, the
    watchdog first."""
    jobs = [s for s, module in _owned(ctx) if not module and s.name != MENUBAR]
    return [s for s in jobs if s.name == WATCHDOG] + [s for s in jobs if s.name != WATCHDOG]


def is_stopped() -> bool:
    return store.path(STOPPED).exists()


def label(service) -> str:
    """A service's name in what this says: its job's label in the layout before the one job."""
    return f"{PREFIX}.{service.name}"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def old_plists() -> list:
    """The earlier layout's job files, one per service, the old doctor's last: it finishes a move cut short."""
    found = sorted(agents_dir().glob(f"{PREFIX}.*.plist"))
    return [p for p in found if p.stem != f"{PREFIX}.{WATCHDOG}"] + [p for p in found if p.stem == f"{PREFIX}.{WATCHDOG}"]


def installed_plists() -> list:
    """Every athena job file here, the one job's and any left from the earlier layout."""
    return ([plist_path()] if plist_path().exists() else []) + old_plists()


def _old_layout() -> bool:
    return not plist_path().exists() and bool(old_plists())


# the supervisor, as seen from outside it

def _run_dir() -> Path:
    return store.path("run")


def supervisor_runs() -> bool:
    """Whether a supervisor runs on this state dir: it holds run/services.lock for as long as it lives."""
    try:
        fd = os.open(_run_dir() / "services.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # closing lets go of a lock this probe took
    return False


def children() -> dict:
    """{name: row} as the running supervisor last said; {} when none runs, whatever a file left behind says."""
    if not supervisor_runs():
        return {}
    rows = store.read_dict(_run_dir() / "services.json").get("children")
    return rows if isinstance(rows, dict) else {}


def supervises(name) -> bool:
    return name in children()


def ask_restart(name) -> bool:
    """Asks the supervisor to restart that child now and clear its backoff: doctor's repair of the watch or the dash. False
    when no supervisor runs one by that name, so the caller starts its own copy as before."""
    if not supervises(name):
        return False
    store.ensure("run", "services-ask")
    store.write_text(_run_dir() / "services-ask" / name, f"{time.time()}\n")
    return True


def _await(names, held) -> str:
    """Until the supervisor has stopped (held) or started again (not held) each of `names` it runs; "" once it has."""
    deadline = time.monotonic() + AWAIT
    while True:
        rows = children()
        if not rows:
            return ""
        late = [n for n in names if n in rows and bool(rows[n].get("held")) != held]
        if not late:
            return ""
        if time.monotonic() >= deadline:
            return f"the supervisor has not {'stopped' if held else 'started'} {', '.join(late)} yet: `athena services status` says where it stands"
        time.sleep(0.1)


def run(ctx) -> int:
    """`athena services run`: the one job's program."""
    from athena.kernel import supervisor
    store.ensure("logs")
    held = supervisor.Supervisor(wanted(ctx), [store.ROOT / "bin" / "athena"], store.ensure("run"), store.path("logs"),
                                 store.path(STOPPED), background=sys.platform == "darwin")
    return held.main()


# the plist

def _defaults() -> dict:
    """What each carried variable means unset: one set to this says nothing, so it is left out of the plist."""
    return {"ATHENA_HQ": "athena", "ATHENA_DASH_PORT": str(config.REX_DEFAULTS["dash_port"]),
            "ATHENA_BINARY": str(config.REX_DEFAULTS["binary"]), "ATHENA_CONFIG": str(store.state_dir() / "config.json")}


def _env_of(target) -> dict:
    try:
        env = plistlib.loads(target.read_bytes()).get("EnvironmentVariables")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    return env if isinstance(env, dict) else {}


def written_env() -> dict:
    """The environment the job files here carry: the one job's, else the earlier layout's, so a move keeps it."""
    for target in installed_plists():
        env = _env_of(target)
        if env:
            return env
    return {}


def plist() -> dict:
    """The same bytes whoever runs install: each write shows the human a Background Activity notice. So nothing that varies by caller:
    a carried variable at its default is left out, one the caller hasn't set keeps the value already written, and so does PATH.
    Interactive, as the menu bar app is one of its children; the supervisor puts each other child under darwin's
    background policy, as ProcessType Background did for its own job."""
    binary = store.ROOT / "bin" / "athena"
    log = store.path("logs", "services.log")
    written = written_env()
    defaults = _defaults()
    env = {}
    for key in CARRIED:
        value = os.environ.get(key) or written.get(key)
        if value and value != defaults.get(key):
            env[key] = value
    env["ATHENA_STATE_DIR"] = str(store.state_dir())
    env["PATH"] = written.get("PATH") or os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return {"Label": LABEL, "ProgramArguments": [str(binary), "services", "run"], "EnvironmentVariables": env,
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "StandardOutPath": str(log),
            "StandardErrorPath": str(log), "ProcessType": "Interactive"}


NOT_LOADED = (0, 3, 113)  # launchctl's words for a job that wasn't loaded, as a bootout of one is no failure


def _launchctl(*args):
    """A launchctl that hangs past world.run's timeout is a failed call, said like any other, never a traceback."""
    try:
        return world.run("launchctl", *args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UserError(f"launchctl {' '.join(args[:2])} did not answer: {exc}")


def _checked(*args, ok=(0,)):
    done = _launchctl(*args)
    if done.returncode not in ok:
        raise UserError(f"launchctl {' '.join(args)} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")


BOOTSTRAP_TRIES = 4
LOADED_ALREADY = (5, 37)  # launchctl's bootstrap of a job still loaded: a bootout not yet finished, or one never booted out


def _bootstrap(domain, target, name) -> str:
    """A job still loaded is no failure: a bootout launchd hasn't finished is waited for, then a job loaded all the same is kept."""
    for attempt in range(BOOTSTRAP_TRIES):
        done = _launchctl("bootstrap", domain, str(target))
        if done.returncode == 0:
            return ""
        if done.returncode not in LOADED_ALREADY:
            raise UserError(f"launchctl bootstrap failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")
        if attempt < BOOTSTRAP_TRIES - 1:
            time.sleep(float(os.environ.get("ATHENA_BOOTSTRAP_WAIT") or 0.5) * (attempt + 1))
    if _launchctl("print", f"{domain}/{name}").returncode == 0:
        return f"{name} was still loaded, so it was kept: `athena services restart` restarts it"
    raise UserError(f"launchctl bootstrap failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _remove_old(target, dry_run, actions):
    actions.append(f"remove {target}, a job of the layout before the one job")
    if dry_run:
        return
    try:
        _checked("bootout", f"{_domain()}/{target.stem}", ok=NOT_LOADED)  # a job file left behind says it may still run
        target.unlink()
    except (UserError, OSError) as exc:
        actions.append(f"failed {target.stem}: {exc}")


def _loaded(domain) -> bool:
    try:
        return _launchctl("print", f"{domain}/{LABEL}").returncode == 0
    except UserError:
        return False


def install(ctx, dry_run=False) -> list:
    """`_install`. While it moves the earlier layout it holds MOVE_LOCK and keeps the move's record (MOVE): running from
    the moment it holds the lock, failed with what went wrong when it raises or leaves old jobs behind, and gone only once
    the move is done. A try killed outright leaves its record running with the lock free, which `move_state` reads as
    failed. Doctor's row is derived from these two alone, in `move_state`."""
    if dry_run or not old_plists():
        return _install(ctx, dry_run)
    with store.locked(MOVE_LOCK):
        _record("running", store.read_dict(store.path(MOVE)).get("failed"))
        try:
            actions = _install(ctx, dry_run)
        except BaseException as exc:
            _record("failed", str(exc) or type(exc).__name__)
            raise
        if old_plists():
            failed = [a for a in actions if a.startswith("failed ")]
            _record("failed", (failed or ["old jobs are left"])[0])
        else:
            store.path(MOVE).unlink(missing_ok=True)
    return actions


def _record(state, failed):
    store.write_json(store.path(MOVE), {"state": state, "since": time.time(), "failed": failed})


def move_state(now=None) -> tuple:
    """(state, what the last failure said) of the move from the earlier layout, from the lock and the record alone:

        not started   no record, and no install holds the lock
        under way     an install holds the lock (or doctor started one moments ago), and no try has failed yet
        retrying      the same, after a try that failed
        failed        no install holds the lock, and the record says failed, or running (a try killed outright)
    """
    now = time.time() if now is None else now
    record = store.read_dict(store.path(MOVE))
    with store.locked(MOVE_LOCK, blocking=False) as free:
        held = not free
    failed = record.get("failed")
    starting = record.get("state") == "running" and now - float(record.get("since") or 0) < MOVE_START
    if held or starting:
        return ("retrying", failed) if failed else ("under way", None)
    if not record:
        return "not started", None
    if record.get("state") == "running":
        return "failed", "a try was cut off before it finished" + (f", after one that said: {failed}" if failed else "")
    return "failed", failed or "a try failed"


def _install(ctx, dry_run=False) -> list:
    """The one job, and none of the earlier layout's: those go first, but the old doctor's goes last, and only once
    launchd says the one job is loaded, so a move cut short or a bootstrap that failed is finished by that doctor's next
    run. A plist written before but not loaded (a bootstrap that failed) is loaded now. Its services changed (a module
    added or removed) with the plist the same: the supervisor is restarted, as it reads them when it starts."""
    if not dry_run:
        world.resolve("launchctl")
    actions = []
    domain = _domain()
    menubar = ctx.cap(MENUBAR)
    if menubar:
        built = menubar.ensure_built(dry_run)  # before any job changes, so a failed build leaves launchd as it was
        if built:
            actions.append(built)
    data = plist()
    target = plist_path()
    text = plistlib.dumps(data)  # made before an old job's file goes: it carries that job's environment
    old = old_plists()
    names = [s.name for s in wanted(ctx)]
    ran = store.read_dict(store.path("service-rex.json")).get("jobs")
    for path in old:
        if path.stem != f"{PREFIX}.{WATCHDOG}":
            _remove_old(path, dry_run, actions)
    if target.exists() and target.read_bytes() == text:
        actions.append(f"unchanged {target}")
        if not dry_run and not _loaded(domain):
            try:
                actions.append(_bootstrap(domain, target, LABEL) or f"started {LABEL}, which was not loaded")
            except UserError as exc:
                actions.append(f"failed {LABEL}: {exc}")
        elif ran is not None and sorted(ran) != sorted(names) and not dry_run:
            done = _launchctl("kickstart", "-k", f"{domain}/{LABEL}")
            actions.append(f"restarted {LABEL}: its services changed" if done.returncode == 0 else
                           f"failed {LABEL}: launchctl kickstart failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")
    else:
        actions.append(f"write {target}\n{text.decode()}" if dry_run else f"wrote {target}")
        if dry_run:
            actions.append(f"launchctl bootstrap {domain} {target}")
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                _checked("bootout", f"{domain}/{LABEL}", ok=NOT_LOADED)
                target.write_bytes(text)
                kept = _bootstrap(domain, target, LABEL)
                if kept:
                    actions.append(kept)
            except (UserError, OSError) as exc:
                actions.append(f"failed {LABEL}: {exc}")
    for path in old:
        if path.stem != f"{PREFIX}.{WATCHDOG}":
            continue
        if dry_run or _loaded(domain):
            _remove_old(path, dry_run, actions)
        elif not any(a.startswith(f"failed {LABEL}") for a in actions):
            actions.append(f"failed {LABEL}: launchd does not have it loaded, so the old doctor job stays to try again")
    if not dry_run:
        store.write_json(store.path("service-rex.json"), {"jobs": names})
    return actions


def uninstall(ctx, dry_run=False) -> list:
    if not dry_run:
        world.resolve("launchctl")
    actions = []
    for target in installed_plists():
        actions.append(f"remove {target}")
        if not dry_run:
            _checked("bootout", f"{_domain()}/{target.stem}", ok=NOT_LOADED)  # a job file left behind says it may still run
            target.unlink()
    return actions


def stop(ctx) -> list:
    """Quit athena. The mark goes down first, so a stop cut short still reads as stopped and Start finishes it. The
    supervisor stops what it names; this waits for it, then stops a stray watch and runs each on_quit. With no
    supervisor running there is nothing to wait for, and the mark keeps the services stopped when one starts."""
    jobs = athenas(ctx)
    store.write_json(store.path(STOPPED), {"at": time.time(), "jobs": [s.name for s in jobs]})
    old = _old_layout()
    if old:
        world.resolve("launchctl")
    late = "" if old else _await([s.name for s in jobs], held=True)
    actions = []
    for service in jobs:
        if old:  # the earlier layout, until doctor moves it to the one job: each job disabled and booted out, as it was
            _checked("disable", f"{_domain()}/{label(service)}")
            _checked("bootout", f"{_domain()}/{label(service)}", ok=NOT_LOADED)
        actions.append(f"stopped {label(service)}")
        if service.name == "watch":
            actions.extend(_stray_watch() + _quiet(ctx))
    if not any(s.name == "watch" for s in jobs):
        actions.extend(_stray_watch() + _quiet(ctx))
    if late:
        actions.append(late)
    kept = [s.name for s, module in _owned(ctx) if module]
    if kept:
        actions.append(f"left running: {', '.join(kept)}")
    return actions


def _stray_watch() -> list:
    """A watch doctor's repair started outside the supervisor (when none ran) outlives Quit and keeps ticking while stopped. It
    goes too, before any on_quit: watch.stop_running stops only the pid it recorded, over 1, while it holds the lock and says
    it is a watch."""
    module = caps.get("watch")
    if not module:
        return []
    try:
        return ["stopped the watch doctor started outside launchd"] if module.stop_running() else []
    except UserError as exc:
        return [f"watch: {exc}"]


def _quiet(ctx) -> list:
    """Each capability's on_quit, once the watch that ticks them is down. One that fails is said, and Quit goes on."""
    said = []
    for module in caps.loaded():
        hook = getattr(module, "on_quit", None)
        if hook is None:
            continue
        try:
            line = hook(ctx)
        except Exception as exc:  # Quit still stops everything else
            line = f"{getattr(module, '__name__', '?').rsplit('.', 1)[-1]}: on_quit failed: {type(exc).__name__}: {exc}"
        if line:
            said.append(line)
    return said


def start(ctx) -> list:
    """Start athena: the mark goes, and the supervisor starts each service again, the watchdog last so what it looks
    after is already coming up. With no supervisor running, the one job is loaded, and starts them as it comes up."""
    jobs = athenas(ctx)
    order = jobs[1:] + jobs[:1] if jobs and jobs[0].name == WATCHDOG else jobs
    if _old_layout():
        return _start_old(order)
    store.path(STOPPED).unlink(missing_ok=True)
    if supervisor_runs():
        late = _await([s.name for s in order], held=False)
        return [f"started {label(s)}" for s in order] + ([late] if late else [])
    if not plist_path().exists():
        return [f"not installed: {LABEL}; `athena services install` writes it"]
    world.resolve("launchctl")
    _checked("enable", f"{_domain()}/{LABEL}")
    _checked("bootout", f"{_domain()}/{LABEL}", ok=NOT_LOADED)
    kept = _bootstrap(_domain(), plist_path(), LABEL)
    return [kept] if kept else [f"started {label(s)}" for s in order]


def _start_old(order) -> list:
    """The earlier layout, until doctor moves it to the one job: each job enabled and loaded again, as it was."""
    world.resolve("launchctl")
    actions = []
    for service in order:
        _checked("enable", f"{_domain()}/{label(service)}")
        target = agents_dir() / f"{label(service)}.plist"
        if not target.exists():
            actions.append(f"not installed: {label(service)}; `athena services install` writes it")
            continue
        _checked("bootout", f"{_domain()}/{label(service)}", ok=NOT_LOADED)
        kept = _bootstrap(_domain(), target, label(service))
        actions.append(kept or f"started {label(service)}")
    store.path(STOPPED).unlink(missing_ok=True)
    return actions


def restart(ctx) -> list:
    """The supervisor and every service, onto the code `current` names now: launchd's kickstart of the one job. Run by a
    child of the supervisor (doctor, the menu bar), it ends that caller too, after launchd has it. While athena is stopped
    it still restarts, so the menu bar and modules run the new code; the stopped mark keeps the rest stopped."""
    if _old_layout():
        return _restart_old(ctx)
    if not plist_path().exists():
        raise UserError(f"not installed: {LABEL}; `athena services install` writes it")
    world.resolve("launchctl")
    domain = _domain()
    stays = "; athena stays stopped until Start" if is_stopped() else ""
    built = _menubar_built(ctx)  # first: a release arrives without its app built, and the restart runs that release's
    done = _launchctl("kickstart", "-k", f"{domain}/{LABEL}")
    if done.returncode == 0:
        return built + [f"restarted {LABEL}{stays}"]
    if done.returncode not in NOT_LOADED:
        return built + [f"failed {LABEL}: launchctl kickstart failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}"]
    try:
        return built + [(_bootstrap(domain, plist_path(), LABEL) or f"started {LABEL}, which was not loaded") + stays]
    except UserError as exc:
        return built + [f"failed {LABEL}: {exc}"]


def _menubar_built(ctx) -> list:
    """The menu bar app of the release that runs this, built if it is missing or stale: a release is copied without its
    build, and the supervisor restarted onto it runs its `menubar run`, which exits clean (and stays down) without one. A
    failed build is said; the restart goes on, as everything else still runs."""
    menubar = ctx.cap(MENUBAR)
    if not menubar:
        return []
    try:
        built = menubar.ensure_built()
    except UserError as exc:
        return [f"menubar: {exc}"]
    return [built] if built else []


def _restart_old(ctx) -> list:
    """The earlier layout's jobs, each kickstarted as before: an update to this release restarts them so, and doctor moves
    them to the one job only once the update's health check has passed."""
    if is_stopped():
        raise UserError("athena is stopped: Start athena, or `athena services start`, brings it back")
    world.resolve("launchctl")
    domain = _domain()
    actions = []
    for service in athenas(ctx):
        name = label(service)
        try:
            done = _launchctl("kickstart", "-k", f"{domain}/{name}")
            if done.returncode == 0:
                actions.append(f"restarted {name}")
                continue
            target = agents_dir() / f"{name}.plist"
            if done.returncode not in NOT_LOADED or not target.exists():
                raise UserError(f"launchctl kickstart failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}"
                                if done.returncode not in NOT_LOADED else "not installed: `athena services install` writes it")
            actions.append(_bootstrap(domain, target, name) or f"started {name}, which was not loaded")
        except UserError as exc:
            actions.append(f"failed {name}: {exc}")
    return actions


def _launchd_jobs():
    """{label: (pid or None, last exit)} from one `launchctl list`, or None when nobody can say."""
    try:
        done = _launchctl("list")
    except (world.Off, OSError, UserError):
        return None
    if done.returncode != 0:
        return None
    jobs = {}
    for line in done.stdout.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) == 3 and (parts[2] == LABEL or parts[2].startswith(PREFIX + ".")):
            pid, status, name = parts
            jobs[name] = (int(pid) if pid.isdigit() else None, int(status) if status.lstrip("-").isdigit() else 0)
    return jobs


def _state_of(row) -> tuple:
    """A supervisor's child as the menu bar reads a launchd job: (state, pid)."""
    state, pid, last = row.get("state"), row.get("pid"), row.get("last_exit")
    if state == "running" and pid:
        return "running", pid
    if state == "backing off" or (state in ("stopped", "waiting") and last):
        return f"exited {last}", None
    return "loaded", None  # starting, waiting for its next run, or a clean exit: a job between its runs


def status(ctx) -> dict:
    """What the menu bar shows: each service's state from the supervisor (or launchd, for the earlier layout), and whether
    athena is stopped."""
    one = plist_path().exists()
    rows_now = children() if one else {}
    jobs = None if rows_now else _launchd_jobs()
    stopped = is_stopped()
    held = {s.name for s in athenas(ctx)} if stopped else set()
    rows = []
    for service, module in _owned(ctx):
        installed = one or (agents_dir() / f"{label(service)}.plist").exists()
        pid = None
        if not installed:
            state = "not installed"
        elif one and rows_now:
            row = rows_now.get(service.name)
            if row is None:
                state = "not loaded"  # added since the supervisor started: the next restart or install runs it
            elif row.get("held"):
                state = "stopped"
            else:
                state, pid = _state_of(row)
        elif one:  # no supervisor runs
            if service.name in held:
                state = "stopped"
            elif jobs is None:
                state = "unknown"
            elif LABEL not in jobs:
                state = "not loaded"
            else:
                last = jobs[LABEL][1]
                state = f"exited {last}" if last else "unknown"
        else:
            name = label(service)
            pid, last = (jobs or {}).get(name, (None, None))
            if service.name in held and name not in (jobs or {}):
                state = "stopped"
            elif jobs is None:
                state = "unknown"
            elif name not in jobs:
                state = "not loaded"
            elif pid:
                state = "running"
            else:
                state = "loaded" if not last else f"exited {last}"
        rows.append({"name": service.name, "state": state, "pid": pid, "every": service.every, "installed": installed,
                     "module": module})
    return {"stopped": stopped, "services": rows}


def checks(ctx):
    """Doctor's `services` row, which moves an install of the earlier layout to the one job: the command that migrates is
    this check's repair, `athena services install` of the release `current` names, in a session of its own as it boots out
    the old doctor job running it. An update to this release runs the old release's updater, which only restarts the old
    jobs (onto this code) and asks this doctor for its verdict; so this waits while an update holds the install lock or has
    a switch on record, and moves nothing until the update's health check has passed and been saved.

    The move is upkeep the human asked for fewer notices about. Its row follows `move_state`: not started is a `quiet`
    repair (no notification when it goes through); under way is ok, however long the menu bar app takes to build; failed
    is an ordinary problem, told once and hourly after, its repair tried at doctor's usual pace; retrying stays failing
    and starts no second try. Done (no old jobs) has no row."""
    old = old_plists()
    if not old:
        store.path(MOVE).unlink(missing_ok=True)  # done: whatever a try left on record is over
        return []
    install = ctx.cap("install")
    releases = getattr(install, "releases", None)
    if releases is None or (lab.active() and not lab.scratch_place(releases.home())):
        return [caps.Check("services", True, f"{len(old)} job(s) of the earlier layout: `athena services install` moves them")]
    record = releases.load()
    if record["switch"]:
        return [caps.Check("services", True, "an update is under way: the move to the one job waits for its health check")]
    with releases.locked() as got:
        if not got:
            return [caps.Check("services", True, "an update is under way: the move to the one job waits for its health check")]
    state, failed = move_state()
    if state == "under way":
        return [caps.Check("services", True, "the move to the one job is under way", short="moving")]
    if state == "retrying":  # still failing until a try goes through, and no second try beside this one
        return [caps.Check("services", False, f"the move to the one job failed: {failed}; a new try is under way", short="move failed")]
    if state == "failed":  # an ordinary problem: told once and hourly after, its repair tried at doctor's usual pace
        return [caps.Check("services", False, f"the move to the one job failed: {failed}; the repair tries again",
                           repair=lambda: _move(releases), grace=0, short="move failed")]
    return [caps.Check("services", False, f"{len(old)} job(s) of the earlier layout, one per service: the repair moves them to the one job",
                       repair=lambda: _move(releases), grace=0, short="earlier layout", quiet=True)]


def _move(releases):
    """Started, not waited for: the install may first build the menu bar app, longer than a doctor pass may last, and it
    ends the old doctor job running this. In a session of its own, it outlives both; what it says goes to its log."""
    with releases.locked() as got:
        if not got or releases.load()["switch"]:
            raise UserError("an update is under way: the move waits for it")
        installed = releases.load()["current"]
    _record("running", store.read_dict(store.path(MOVE)).get("failed"))  # under way from now, before it holds the lock
    binary = releases.root() / "current/bin/athena" if installed else store.ROOT / "bin" / "athena"  # else run from a checkout
    with open(store.ensure("logs") / "services-move.log", "ab") as log:
        subprocess.Popen([str(binary), "services", "install"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)


def _configure(p):
    p.add_argument("action", choices=["install", "uninstall", "status", "stop", "start", "restart", "run"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true", help="status: as the menu bar reads it")


def _run(args):
    ctx = caps.CTX
    if args.action == "run":
        return run(ctx)
    if args.action == "status" and args.json:
        print(json.dumps(status(ctx), indent=1))
        return 0
    if args.action == "status":
        one = plist_path().exists()
        for service in wanted(ctx):
            installed = one or (agents_dir() / f"{label(service)}.plist").exists()
            every = f"every {service.every}s" if service.every else "kept alive"
            print(f"{service.name:<10} {'installed' if installed else 'not installed':<14} {every}")
        if is_stopped():
            print("athena is stopped: `athena services start` brings it back")
        return 0
    if args.action in ("stop", "start", "restart") and args.dry_run:
        raise UserError(f"services {args.action} has no --dry-run")
    try:
        if args.action in ("stop", "start", "restart"):
            actions = {"stop": stop, "start": start, "restart": restart}[args.action](ctx)
        else:
            actions = install(ctx, args.dry_run) if args.action == "install" else uninstall(ctx, args.dry_run)
    except world.Off as exc:
        raise UserError(f"{exc}; use --dry-run" if args.action in ("install", "uninstall") else str(exc))
    for action in actions:
        print(action)
    return 1 if any(action.startswith("failed ") for action in actions) else 0


def commands():
    return {"services": caps.Command(SUMMARY, _configure, _run)}
