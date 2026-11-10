"""Login services. It knows launchd and nothing about what runs: each capability lists its own in `services(ctx)`.

Writes only athena-rex.* jobs, so the old athena's jobs stay as they are. In the lab, --dry-run is all it will do.

`stop` is the menu bar's Quit athena: the watchdog (doctor) first, so nothing brings the rest back, then athena's other jobs.
Each is disabled as well as booted out, so launchd won't load it at login either, and it stays stopped until `start`, which
loads them again with the watchdog last. Rex runs the sessions, HQ's and the workers', so Quit never stops the Rex server or
any Claude. A module's jobs keep running, and so does the menu bar app, which shows Start athena.
Once the watch is down, each capability's and module's optional `on_quit(ctx)` runs, so nothing it drives (a card a
module drives) is left showing a state that has stopped changing.
"""
import json
import os
import plistlib
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, store, world

SUMMARY = "the background jobs of every enabled capability, as launchd agents"
PREFIX = "athena-rex"
WATCHDOG = "doctor"
MENUBAR = "menubar"
STOPPED = "services-stopped.json"
CARRIED = ("ATHENA_STATE_DIR", "ATHENA_CONFIG", "ATHENA_HQ", "ATHENA_DASH_PORT", "ATHENA_REX_SERVER", "ATHENA_REX_BIN")


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
    """What Quit stops, Start loads and Restart restarts: every job but a module's and the menu bar's, the watchdog first."""
    jobs = [s for s, module in _owned(ctx) if not module and s.name != MENUBAR]
    return [s for s in jobs if s.name == WATCHDOG] + [s for s in jobs if s.name != WATCHDOG]


def is_stopped() -> bool:
    return store.path(STOPPED).exists()


def label(service) -> str:
    return f"{PREFIX}.{service.name}"


def _defaults() -> dict:
    """What each carried variable means unset: one set to this says nothing, so it is left out of the plist."""
    return {"ATHENA_HQ": "athena", "ATHENA_DASH_PORT": str(config.REX_DEFAULTS["dash_port"]),
            "ATHENA_REX_BIN": str(config.REX_DEFAULTS["binary"]), "ATHENA_CONFIG": str(store.state_dir() / "config.json")}


def _written_env(target) -> dict:
    try:
        env = plistlib.loads(target.read_bytes()).get("EnvironmentVariables")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    return env if isinstance(env, dict) else {}


def plist_for(service) -> dict:
    """The same bytes whoever runs install: each write shows the human a Background Activity notice. So nothing that varies by caller:
    a carried variable at its default is left out, one the caller hasn't set keeps the value already written, and so does PATH."""
    binary = store.ROOT / "bin" / "athena-rex"
    log = store.path("logs", f"service-{service.name}.log")
    written = _written_env(agents_dir() / f"{PREFIX}.{service.name}.plist")
    defaults = _defaults()
    env = {}
    for key in CARRIED:
        value = os.environ.get(key) or written.get(key)
        if value and value != defaults.get(key):
            env[key] = value
    env["ATHENA_STATE_DIR"] = str(store.state_dir())
    env["PATH"] = written.get("PATH") or os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    plist = {"Label": f"{PREFIX}.{service.name}", "ProgramArguments": [str(binary), *service.argv],
             "EnvironmentVariables": env, "RunAtLoad": True, "StandardOutPath": str(log), "StandardErrorPath": str(log),
             "ProcessType": "Interactive" if service.interactive else "Background"}
    if service.every:
        plist["StartInterval"] = service.every
    if service.keep_alive:
        plist["KeepAlive"] = {"SuccessfulExit": False}
        plist["ThrottleInterval"] = 10
    return plist


NOT_LOADED = (0, 3, 113)  # launchctl's words for a job that wasn't loaded, as a bootout of one is no failure


def _launchctl(*args):
    return world.run("launchctl", *args)


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
        return f"{name} was still loaded, so it was kept: `athena-rex services restart` restarts it"
    raise UserError(f"launchctl bootstrap failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")


def install(ctx, dry_run=False) -> list:
    """Each job on its own: one that fails is said ("failed <label>: ...") and the others still go in."""
    if not dry_run:
        world.resolve("launchctl")
    actions = []
    domain = f"gui/{os.getuid()}"
    held = {s.name for s in athenas(ctx)} if is_stopped() else set()
    menubar = ctx.cap(MENUBAR)
    if menubar:
        built = menubar.ensure_built(dry_run)  # before any job changes, so a failed build leaves launchd as it was
        if built:
            actions.append(built)
    for service in wanted(ctx):
        plist = plist_for(service)
        target = agents_dir() / f"{plist['Label']}.plist"
        text = plistlib.dumps(plist)
        if target.exists() and target.read_bytes() == text:
            actions.append(f"unchanged {target}")
            continue
        actions.append(f"write {target}\n{text.decode()}" if dry_run else f"wrote {target}")
        if service.name in held:
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text)
            actions.append(f"kept stopped: {plist['Label']}, until `athena-rex services start`")
            continue
        if dry_run:
            actions.append(f"launchctl bootstrap {domain} {target}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _checked("bootout", f"{domain}/{plist['Label']}", ok=NOT_LOADED)
            target.write_bytes(text)
            kept = _bootstrap(domain, target, plist["Label"])
            if kept:
                actions.append(kept)
        except (UserError, OSError) as exc:
            actions.append(f"failed {plist['Label']}: {exc}")
    store.write_json(store.path("service-rex.json"), {"jobs": [s.name for s in wanted(ctx)]})
    return actions


def uninstall(ctx, dry_run=False) -> list:
    if not dry_run:
        world.resolve("launchctl")
    actions = []
    domain = f"gui/{os.getuid()}"
    for target in sorted(agents_dir().glob(f"{PREFIX}.*.plist")):
        actions.append(f"remove {target}")
        if not dry_run:
            _checked("bootout", f"{domain}/{target.stem}", ok=NOT_LOADED)  # a job file left behind says it may still run
            target.unlink()
    return actions


def stop(ctx) -> list:
    """Quit athena. The mark goes down first, so a stop cut short still reads as stopped and Start finishes it."""
    world.resolve("launchctl")
    domain = f"gui/{os.getuid()}"
    jobs = athenas(ctx)
    store.write_json(store.path(STOPPED), {"at": time.time(), "jobs": [s.name for s in jobs]})
    actions = []
    for service in jobs:
        _checked("disable", f"{domain}/{label(service)}")
        _checked("bootout", f"{domain}/{label(service)}", ok=NOT_LOADED)
        actions.append(f"stopped {label(service)}")
        if service.name == "watch":
            actions.extend(_stray_watch() + _quiet(ctx))
    if not any(s.name == "watch" for s in jobs):
        actions.extend(_stray_watch() + _quiet(ctx))
    kept = [s.name for s, module in _owned(ctx) if module]
    if kept:
        actions.append(f"left running: {', '.join(kept)}")
    return actions


def _stray_watch() -> list:
    """A watch doctor's repair started outside launchd outlives the bootout and keeps ticking while stopped. It goes too, before any
    on_quit: watch.stop_running stops only the pid it recorded, over 1, while it holds the lock and says it is a watch."""
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
    """Start athena: each job enabled and loaded again, the watchdog last so what it looks after is already coming up."""
    world.resolve("launchctl")
    domain = f"gui/{os.getuid()}"
    jobs = athenas(ctx)
    actions = []
    for service in jobs[1:] + jobs[:1] if jobs and jobs[0].name == WATCHDOG else jobs:
        _checked("enable", f"{domain}/{label(service)}")
        target = agents_dir() / f"{label(service)}.plist"
        if not target.exists():
            actions.append(f"not installed: {label(service)}; `athena-rex services install` writes it")
            continue
        _checked("bootout", f"{domain}/{label(service)}", ok=NOT_LOADED)
        kept = _bootstrap(domain, target, label(service))
        actions.append(kept or f"started {label(service)}")
    store.path(STOPPED).unlink(missing_ok=True)
    return actions


def restart(ctx) -> list:
    if is_stopped():
        raise UserError("athena is stopped: Start athena, or `athena-rex services start`, brings it back")
    world.resolve("launchctl")
    domain = f"gui/{os.getuid()}"
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
                                if done.returncode not in NOT_LOADED else "not installed: `athena-rex services install` writes it")
            actions.append(_bootstrap(domain, target, name) or f"started {name}, which was not loaded")
        except UserError as exc:
            actions.append(f"failed {name}: {exc}")
    return actions


def _launchd_jobs():
    """{label: (pid or None, last exit)} from one `launchctl list`, or None when nobody can say."""
    try:
        done = _launchctl("list")
    except (world.Off, OSError):
        return None
    if done.returncode != 0:
        return None
    jobs = {}
    for line in done.stdout.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) == 3 and parts[2].startswith(PREFIX + "."):
            pid, status, name = parts
            jobs[name] = (int(pid) if pid.isdigit() else None, int(status) if status.lstrip("-").isdigit() else 0)
    return jobs


def status(ctx) -> dict:
    """What the menu bar shows: each job's state from launchd, and whether athena is stopped."""
    jobs = _launchd_jobs()
    stopped = is_stopped()
    held = {s.name for s in athenas(ctx)} if stopped else set()
    rows = []
    for service, module in _owned(ctx):
        installed = (agents_dir() / f"{label(service)}.plist").exists()
        pid, last = (jobs or {}).get(label(service), (None, None))
        if not installed:
            state = "not installed"
        elif service.name in held and label(service) not in (jobs or {}):
            state = "stopped"
        elif jobs is None:
            state = "unknown"
        elif label(service) not in jobs:
            state = "not loaded"
        elif pid:
            state = "running"
        else:
            state = "loaded" if not last else f"exited {last}"
        rows.append({"name": service.name, "state": state, "pid": pid, "every": service.every, "installed": installed,
                     "module": module})
    return {"stopped": stopped, "services": rows}


def _configure(p):
    p.add_argument("action", choices=["install", "uninstall", "status", "stop", "start", "restart"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true", help="status: as the menu bar reads it")


def _run(args):
    ctx = caps.CTX
    if args.action == "status" and args.json:
        print(json.dumps(status(ctx), indent=1))
        return 0
    if args.action == "status":
        for service in wanted(ctx):
            target = agents_dir() / f"{PREFIX}.{service.name}.plist"
            every = f"every {service.every}s" if service.every else "kept alive"
            print(f"{service.name:<10} {'installed' if target.exists() else 'not installed':<14} {every}")
        if is_stopped():
            print("athena is stopped: `athena-rex services start` brings it back")
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
