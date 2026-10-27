"""athena as login services: herdr's server, the dashboard and the board watcher kept running, and `athena doctor`
every minute. launchd user agents on macOS, systemd user units on Linux.

`athena service install` writes the job files and loads them; run again, it rewrites only what changed and loads
only what is not loaded. `uninstall` unloads and removes them. Both take `--dry-run`, which prints what they would
write and run, and runs nothing. Each job carries the athena environment of the shell that installed it (the state
dir, the herdr command and session, the config), so a second install with another state dir and herdr session runs
beside the first under `--instance`. service.json in the state dir records what is installed, for `athena doctor`.

herdr's job restarts the server only after a crash (`herdr server stop` stays stopped), and at most every 30 s. A
loaded herdr job is never reloaded by install, since that stops every agent in it: a changed herdr job takes effect
when the server next starts.
"""
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from athena_lib import herdr, paths

JOBS = ("herdr", "dash", "board", "doctor")
# Carried into every job when set in the installing shell: what makes a job talk to this state dir and herdr.
CARRIED_ENV = ("ATHENA_STATE_DIR", "ATHENA_HERDR", "ATHENA_CONFIG", "ATHENA_HQ", "ATHENA_DASH_PORT",
               "ATHENA_WORKTREE_ROOT", "ATHENA_HQ_CWD", "ATHENA_PLUGIN_DIR", "ATHENA_PYTHON", "XDG_CONFIG_HOME",
               "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR", "LANG")
DESCRIPTIONS = {"herdr": "herdr server", "dash": "dashboard", "board": "board watcher",
                "doctor": "doctor, every minute"}
INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class ServiceError(Exception):
    pass


def platform(name=None) -> str:
    name = name or ("launchd" if sys.platform == "darwin" else "systemd" if sys.platform.startswith("linux") else "")
    if name not in ("launchd", "systemd"):
        raise ServiceError(f"no service manager athena knows on {sys.platform}: launchd (macOS) or systemd (Linux)")
    return name


def label(job, instance=None) -> str:
    """launchd's label (and systemd's unit name, dashed): athena.<job>, or athena.<instance>.<job>."""
    return ".".join(["athena", *([instance] if instance else []), job])


def unit(job, instance=None) -> str:
    return label(job, instance).replace(".", "-")


def launch_agents_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("ATHENA_LAUNCH_AGENTS") or "~/Library/LaunchAgents"))


def systemd_dir() -> Path:
    default = Path(os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config")) / "systemd" / "user"
    return Path(os.path.expanduser(os.environ.get("ATHENA_SYSTEMD_USER_DIR") or default))


def _tool(var, name):
    return shlex.split(os.environ.get(var) or name)


def manifest_file() -> Path:
    return paths.state_dir() / "service.json"


def installed() -> dict:
    """What `athena service install` installed for this state dir, or {}."""
    try:
        data = json.loads(manifest_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _which(cmd):
    """An absolute path for argv[0], kept as found on PATH (a symlink such as Homebrew's, not the versioned
    path behind it, so an upgrade keeps working)."""
    if os.path.isabs(cmd):
        return cmd
    found = shutil.which(cmd)
    if not found:
        raise ServiceError(f"{cmd} is not on PATH")
    return os.path.abspath(found)


def python() -> str:
    return os.environ.get("ATHENA_PYTHON") or sys.executable


def athena_bin() -> str:
    return str(paths.PLUGIN_ROOT / "bin" / "athena")


def programs() -> dict:
    """Each job's argv. The board runs as `athena board on` starts it, so `board.running_pid` knows it."""
    h = herdr.argv("server")
    return {
        "herdr": [_which(h[0]), *h[1:]],
        "dash": [python(), athena_bin(), "dash", "run"],
        "board": [python(), "-m", "athena_lib.cli", "board", "watch"],
        "doctor": [python(), athena_bin(), "doctor", "--fix", "--quiet"],
    }


HERDR_ENV = ("LANG", "XDG_CONFIG_HOME", "XDG_STATE_HOME")  # herdr's panes inherit its environment: nothing athena's
STANDARD_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _path() -> str:
    """The folders of the tools athena runs, as this shell finds them, then the standard ones: never the whole
    shell PATH, which carries per-shell entries that are gone by the next login."""
    found = [shutil.which(t) for t in ("herdr", "claude", "gh", "git", "go")] + [python()]
    seen = []
    for d in [os.path.dirname(os.path.abspath(f)) for f in found if f] + list(STANDARD_PATH):
        if d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def environment(job=None) -> dict:
    keep = HERDR_ENV if job == "herdr" else CARRIED_ENV
    env = {k: os.environ[k] for k in keep if os.environ.get(k)}
    env["PATH"] = _path()
    if job != "herdr":
        env["PYTHONPATH"] = str(paths.PLUGIN_ROOT)
    return env


def log_file(job) -> Path:
    return paths.state_dir() / "logs" / f"service-{job}.log"


def _workdir(job) -> str:
    return os.path.expanduser("~") if job == "herdr" else str(paths.PLUGIN_ROOT)


def plist(job, instance=None, argv=None, env=None) -> bytes:
    argv = argv or programs()[job]
    data = {"Label": label(job, instance), "ProgramArguments": argv, "EnvironmentVariables": env or environment(job),
            "WorkingDirectory": _workdir(job), "StandardOutPath": str(log_file(job)),
            "StandardErrorPath": str(log_file(job)), "RunAtLoad": True}
    if job == "doctor":
        data["StartInterval"] = 60
        # What doctor starts outlives it: a detached HQ restart, a dashboard or board it started by hand, caffeinate.
        # launchd would otherwise kill whatever is left in the job's process group when doctor exits.
        data["AbandonProcessGroup"] = True
    else:
        # Restarted after a crash, never after a clean stop (`herdr server stop`, a second watcher leaving).
        data["KeepAlive"] = {"SuccessfulExit": False}
        data["ThrottleInterval"] = 30 if job == "herdr" else 10
    if job == "herdr":
        data["ProcessType"] = "Interactive"  # it hosts the agents the human works with
    return plistlib.dumps(data, sort_keys=True)


def _systemd_quote(arg) -> str:
    arg = str(arg).replace("%", "%%").replace("$", "$$")
    if arg and not re.search(r'[\s"\'\\;]', arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def units(job, instance=None, argv=None, env=None) -> dict:
    """The systemd unit files for `job`, by file name: a service, and for doctor its timer too."""
    argv = argv or programs()[job]
    env = env or environment(job)
    lines = ["[Unit]", f"Description=athena {DESCRIPTIONS[job]}", "", "[Service]",
             "ExecStart=" + " ".join(_systemd_quote(a) for a in argv),
             "WorkingDirectory=" + _systemd_quote(_workdir(job))]
    lines += ["Environment=" + _systemd_quote(f"{k}={v}") for k, v in sorted(env.items())]
    lines += [f"StandardOutput=append:{log_file(job)}", f"StandardError=append:{log_file(job)}"]
    name = unit(job, instance)
    if job == "doctor":
        # KillMode=process: when doctor exits, systemd stops doctor alone, not the detached HQ restart, dashboard,
        # board or caffeinate it started, which share its cgroup whatever their session.
        lines += ["Type=oneshot", "KillMode=process"]
        timer = ["[Unit]", f"Description=athena {DESCRIPTIONS[job]}", "", "[Timer]", "OnBootSec=60",
                 "OnUnitActiveSec=60", "AccuracySec=5", f"Unit={name}.service", "", "[Install]",
                 "WantedBy=timers.target"]
        return {f"{name}.service": "\n".join(lines) + "\n", f"{name}.timer": "\n".join(timer) + "\n"}
    lines += ["Restart=on-failure", f"RestartSec={30 if job == 'herdr' else 10}", "", "[Install]",
              "WantedBy=default.target"]
    return {f"{name}.service": "\n".join(lines) + "\n"}


def files(kind, instance=None) -> dict:
    """Every job file, path -> content (bytes), for `kind` launchd or systemd."""
    progs = programs()
    out = {}
    for job in JOBS:
        if kind == "launchd":
            out[launch_agents_dir() / f"{label(job, instance)}.plist"] = plist(job, instance, progs[job], environment(job))
        else:
            for name, text in units(job, instance, progs[job], environment(job)).items():
                out[systemd_dir() / name] = text.encode()
    return out


def _run(argv, check=False):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise ServiceError(f"{' '.join(argv)} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc


def _domain():
    return f"gui/{os.getuid()}"


def loaded(kind, job, instance=None) -> bool:
    if kind == "launchd":
        return _run(_tool("ATHENA_LAUNCHCTL", "launchctl") + ["print", f"{_domain()}/{label(job, instance)}"]).returncode == 0
    target = unit(job, instance) + (".timer" if job == "doctor" else ".service")
    return _run(_tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user", "is-enabled", target]).stdout.strip() == "enabled"


def _commands(kind, job, instance, state):
    """The commands that bring `job` from `state` (its file "new", "changed" or "unchanged", and whether it is
    loaded) to installed and running."""
    is_loaded, change = state
    if kind == "launchd":
        launchctl = _tool("ATHENA_LAUNCHCTL", "launchctl")
        target, path = f"{_domain()}/{label(job, instance)}", str(launch_agents_dir() / f"{label(job, instance)}.plist")
        if not is_loaded:
            return [launchctl + ["bootstrap", _domain(), path]]
        if change != "unchanged" and job != "herdr":
            return [launchctl + ["bootout", target], launchctl + ["bootstrap", _domain(), path]]
        return []
    systemctl = _tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user"]
    name = unit(job, instance) + (".timer" if job == "doctor" else ".service")
    if not is_loaded:
        return [systemctl + ["enable", "--now", name]]
    if change != "unchanged" and job not in ("herdr", "doctor"):
        return [systemctl + ["restart", name]]
    return []


def _by_job(kind, instance, wanted):
    out = {}
    for job in JOBS:
        name = label(job, instance) if kind == "launchd" else unit(job, instance)
        out[job] = [p for p in wanted if p.name.rsplit(".", 1)[0] == name]
    return out


def install(kind=None, instance=None, dry_run=False, out=print) -> dict:
    """Write the job files that differ, then load what is not loaded and reload what changed (never herdr's).
    Dry run: print each file it would write in full and each command, read nothing but the files, run nothing."""
    kind = platform(kind)
    if instance is not None and not INSTANCE_RE.match(instance):
        raise ServiceError(f"--instance takes lowercase letters, digits and dashes, not {instance!r}")
    wanted = files(kind, instance)
    changes = {}
    for path, content in wanted.items():
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        changes[path] = "new" if current is None else "unchanged" if current == content else "changed"
    plan = []
    for job, job_paths in _by_job(kind, instance, wanted).items():
        change = "unchanged" if all(changes[p] == "unchanged" for p in job_paths) else \
            "new" if all(changes[p] == "new" for p in job_paths) else "changed"
        plan.append((job, change))
    if dry_run:
        out(f"athena service install ({kind}{', instance ' + instance if instance else ''}): dry run, nothing written or run")
        for path, content in wanted.items():
            if changes[path] == "unchanged":
                out(f"\n--- {path} (unchanged)")
            else:
                out(f"\n--- {path} ({changes[path]}: would write)\n{content.decode()}")
        out("\n--- would run, for each job not loaded yet (a loaded job whose file changed is reloaded, but herdr's):")
        if kind == "systemd":
            out("  " + " ".join(_tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user", "daemon-reload"]) +
                "   (when a unit file changed)")
        for job, change in plan:
            for cmd in _commands(kind, job, instance, (False, change)):
                out("  " + " ".join(shlex.quote(c) for c in cmd))
        return {"dry_run": True, "files": {str(p): c for p, c in changes.items()}}
    paths.ensure("logs")
    for path, content in wanted.items():
        if changes[path] != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)
    if kind == "systemd" and any(c != "unchanged" for c in changes.values()):
        _run(_tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user", "daemon-reload"], check=True)
    ran, notes = [], []
    for job, change in plan:
        is_loaded = loaded(kind, job, instance)
        for cmd in _commands(kind, job, instance, (is_loaded, change)):
            _run(cmd, check=True)
            ran.append(cmd)
        if job == "herdr" and is_loaded and change != "unchanged":
            notes.append("herdr's job changed; it takes effect when its server next starts (a reload now would stop "
                         "every agent in it)")
    manifest = {"platform": kind, "instance": instance, "state_dir": str(paths.state_dir()),
                "jobs": {job: {"label": label(job, instance), "unit": unit(job, instance)} for job in JOBS},
                "files": [str(p) for p in wanted]}
    manifest_file().write_text(json.dumps(manifest, indent=2) + "\n")
    return {"files": {str(p): c for p, c in changes.items()}, "ran": [" ".join(c) for c in ran], "notes": notes}


def uninstall(kind=None, instance=None, dry_run=False, out=print) -> dict:
    """Unload every job and remove its files; what is already gone is left alone. Stopping herdr's job stops its
    server and the agents in it (herdr resumes them on its next start)."""
    kind = platform(kind)
    wanted = files(kind, instance)
    present = [p for p in wanted if p.exists()]
    if kind == "launchd":
        launchctl = _tool("ATHENA_LAUNCHCTL", "launchctl")
        cmds = [launchctl + ["bootout", f"{_domain()}/{label(job, instance)}"] for job in JOBS]
    else:
        systemctl = _tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user"]
        cmds = [systemctl + ["disable", "--now", unit(job, instance) + (".timer" if job == "doctor" else ".service")]
                for job in JOBS]
    if dry_run:
        out(f"athena service uninstall ({kind}{', instance ' + instance if instance else ''}): dry run, nothing removed or run")
        out("--- would run, for each job that is loaded (herdr's stops its server and every agent in it):")
        for cmd in cmds:
            out("  " + " ".join(shlex.quote(c) for c in cmd))
        out("--- would remove:" if present else "--- no job files to remove")
        for p in present:
            out(f"  {p}")
        return {"dry_run": True, "remove": [str(p) for p in present]}
    ran = []
    for job, cmd in zip(JOBS, cmds):
        if loaded(kind, job, instance):
            _run(cmd)
            ran.append(" ".join(cmd))
    for p in present:
        p.unlink(missing_ok=True)
    if kind == "systemd" and present:
        _run(_tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user", "daemon-reload"])
    if installed().get("instance") == instance:
        manifest_file().unlink(missing_ok=True)
    return {"removed": [str(p) for p in present], "ran": ran}


def status(kind=None, instance=None) -> list:
    kind = platform(kind)
    rows = []
    for job in JOBS:
        name = label(job, instance) if kind == "launchd" else unit(job, instance)
        path = (launch_agents_dir() / f"{name}.plist") if kind == "launchd" else \
            systemd_dir() / (name + (".timer" if job == "doctor" else ".service"))
        rows.append({"job": job, "name": name, "file": str(path), "installed": path.exists(),
                     "loaded": loaded(kind, job, instance)})
    return rows


def start(job) -> bool:
    """Ask the service manager to start an installed job now (doctor's repair). False when athena's service is not
    installed for this state dir, or the manager refused."""
    record = installed()
    if not record or job not in (record.get("jobs") or {}):
        return False
    names = record["jobs"][job]
    if record.get("platform") == "launchd":
        cmd = _tool("ATHENA_LAUNCHCTL", "launchctl") + ["kickstart", f"{_domain()}/{names['label']}"]
    elif record.get("platform") == "systemd":
        cmd = _tool("ATHENA_SYSTEMCTL", "systemctl") + ["--user", "start", names["unit"] + ".service"]
    else:
        return False
    try:
        return _run(cmd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
