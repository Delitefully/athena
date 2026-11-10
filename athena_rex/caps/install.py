"""Install, update and remove athena-rex, one command each way.

    ~/.local/share/athena-rex/releases/<id>   a whole copy, never changed once made
    ~/.local/share/athena-rex/current         a link, swapped atomically, so running Claudes' hooks never see a half-copied tree
    ~/.local/bin/athena-rex                   a two-line launcher
    <state>/profiles/<role>.json              permission profiles, diffed on update

It writes nothing in ~/.claude: hooks and permissions go in each session's own --settings. State is never moved or removed here: moving
it is the human's step. In the lab it refuses any HOME but a marked scratch one, and only --dry-run may look at the real one.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, lab, ledger, store, world

SUMMARY = "install, update, roll back or remove athena-rex"
SHIP = ("bin", "athena_rex", "prompts", "scripts", "docs", "menubar", "plugin", "worker-plugin", "ARCHITECTURE.md")
MARK = "# athena-rex launcher, written by `athena-rex install`."
KEEP_RELEASES = 2
MIN_DISK_GB = 1
_GUARD_SHELL = re.compile(r'^# The guard runs here too.*?^esac\n', re.M | re.S)
_GUARD_ACTIVE = 'return (os.environ.get("ATHENA_WORKER") or "").startswith("rex-")'
_GUARD_PATHS = re.compile(r"^LIVE_PATHS = \(.*?^\)\n", re.M | re.S)


def home() -> Path:
    return Path(os.path.expanduser("~"))


def root() -> Path:
    return home() / ".local/share/athena-rex"


def launcher() -> Path:
    return home() / ".local/bin/athena-rex"


def _record():
    return root() / "install.json"


def _short(path) -> str:
    text, here = str(path), str(home())
    return "~" + text[len(here):] if text.startswith(here + os.sep) else text


def _refuse_in_the_lab(what):
    if lab.active() and not lab.scratch_place(home()):
        raise lab.Refused(f"refusing to {what} under {home()}: ATHENA_WORKER={os.environ['ATHENA_WORKER']} may only install into a marked "
                          f"scratch HOME ({lab.SCRATCH_MARK}). --dry-run looks and changes nothing.")


def _probe(*argv) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or done.stderr).strip().splitlines()[0] if done.returncode == 0 and (done.stdout or done.stderr).strip() else ""


def preflight() -> list:
    """(mark, what, detail). A cross stops the install; a warning means a part stays off and says which."""
    rows = []
    version = ".".join(map(str, sys.version_info[:3]))
    rows.append(("ok" if sys.version_info >= (3, 9) else "stop", "python", version))
    found = _probe("git", "--version")
    rows.append(("ok" if found else "stop", "git", found or "not found: install Xcode's command line tools"))
    disk = shutil.disk_usage(home()).free // (1 << 30)
    rows.append(("ok" if disk >= MIN_DISK_GB else "stop", "disk", f"{disk} GB free"))
    binary = str(config.rex("binary"))
    rex = _probe(binary, "--autostart=false", "version") if os.path.exists(binary) else ""
    rows.append(("ok" if rex else "warn", "rex", rex or f"not at {binary}: spawn and HQ need it (config rex.binary)"))
    claude = str(config.rex("claude")).split()[0]
    found = _probe(claude, "--version")
    rows.append(("ok" if found else "warn", "claude", found or f"{claude} not found: spawn and HQ need it"))
    rows.append(("ok" if shutil.which("gh") else "warn", "gh", "found" if shutil.which("gh") else "not found: PRs stay unread"))
    return rows


def _files(source) -> list:
    out = []
    for entry in SHIP:
        place = Path(source, entry)
        if place.is_file():
            out.append(place)
        for folder, dirs, names in os.walk(place):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            out.extend(Path(folder, n) for n in sorted(names) if not n.endswith((".pyc", ".DS_Store")))
    return sorted(out)


def release_id(source, guard_out) -> str:
    digest = hashlib.sha256()
    for path in _files(source):
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12] + ("-live" if guard_out else "")


def take_the_guard_out(release):
    """The lab's guards leave this copy only, never the tree it came from, which keeps them for whoever works on athena-rex there."""
    release = Path(release)
    if (release / ".git").exists():
        raise UserError(f"{release} is a checkout, not a release; the guard stays there")
    lab_file, command = release / "athena_rex/kernel/lab.py", release / "bin/athena-rex"
    text, shell = lab_file.read_text(), command.read_text()
    if _GUARD_ACTIVE not in text or not _GUARD_PATHS.search(text) or not _GUARD_SHELL.search(shell):
        raise UserError("the lab guard is not where this build expects it; nothing was changed")
    lab_file.write_text(_GUARD_PATHS.sub("LIVE_PATHS = ()\n", text).replace(_GUARD_ACTIVE, "return False"))
    command.write_text(_GUARD_SHELL.sub("", shell))


def _make_release(source, name, guard_out) -> Path:
    target = root() / "releases" / name
    if target.is_dir():
        return target
    partial = target.with_name(f".{name}.{os.getpid()}")
    for path in _files(source):
        copy = partial / path.relative_to(source)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    if guard_out:
        take_the_guard_out(partial)
    os.replace(partial, target)
    return target


def _point_current(name):
    link = root() / "current"
    fresh = link.with_name(f".current.{os.getpid()}")
    fresh.unlink(missing_ok=True)
    os.symlink(Path("releases") / name, fresh)
    os.replace(fresh, link)


def _current() -> str:
    link = root() / "current"
    return os.path.basename(os.readlink(link)) if link.is_symlink() else ""


def _prune(keep):
    folder = root() / "releases"
    for entry in sorted(folder.iterdir() if folder.is_dir() else []):
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def _launcher_text() -> str:
    return f'#!/bin/sh\n{MARK}\nexec "{root()}/current/bin/athena-rex" "$@"\n'


def _launcher_is_ours() -> bool:
    try:
        return MARK in launcher().read_text()
    except (OSError, UnicodeDecodeError):
        return False


def _write_launcher(force) -> str:
    target = launcher()
    wanted = _launcher_text()
    if target.exists() or target.is_symlink():
        if target.is_file() and not target.is_symlink() and target.read_text(errors="replace") == wanted:
            return "unchanged"
        if not _launcher_is_ours():
            if not force:
                raise UserError(f"{_short(target)} exists and isn't ours. Look at it; --force moves it aside and goes on.")
            aside = target.with_name(f"{target.name}.before-install.{int(time.time())}")
            os.replace(target, aside)
            print(f"  moved aside {_short(aside)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(target, wanted, mode=0o755)
    return "written"


def _on_path() -> bool:
    return str(launcher().parent) in os.environ.get("PATH", "").split(os.pathsep)


def set_real_world():
    """Opens the door to the real world for this state dir. Its own config, and only this key."""
    data = store.read_dict(store.path("config.json"))
    data.setdefault("rex", {})["real_world"] = True
    store.write_json(store.path("config.json"), data, indent=2)
    config.reset()


def _state_step(dry_run, real_world) -> list:
    """Lines to say. A state dir that lives elsewhere is left alone: moving it is the human's step, and the install doesn't guess."""
    elsewhere = store.state_elsewhere()
    if elsewhere:
        return [f"= state lives at {_short(elsewhere)}: nothing written. Move it here (or set ATHENA_STATE_DIR to it), then `athena-rex install --real-world` finishes"]
    folder = store.state_dir()
    lines = []
    fresh = not (folder / "config.json").exists() and not (folder / "ledger.jsonl").exists()
    if fresh or real_world:
        if not dry_run:
            set_real_world()
        lines.append(f"+ rex.real_world in {_short(folder / 'config.json')}")
    profiles = caps.get("profiles")
    if profiles and dry_run:
        lines.append(f"+ permission profiles in {_short(folder / 'profiles')}")
    elif profiles:
        for role, changed in profiles.write_all().items():
            if changed is None:
                lines.append(f"+ permission profile {role}")
            elif changed:
                lines.append(f"~ permission profile {role} changed:")
                lines.extend(f"    {line}" for line in changed)
            else:
                lines.append(f"= permission profile {role}")
    return lines


def _installed_command(*argv):
    return subprocess.run([str(root() / "current/bin/athena-rex"), *argv], capture_output=True, text=True, timeout=120)


def _services(action) -> str:
    done = _installed_command("services", action)
    failed = [line for line in done.stdout.splitlines() if line.startswith("failed ")]
    if done.returncode and not failed:
        return f"launchd jobs: skipped ({(done.stderr or done.stdout).strip()})"
    return "\n".join(f"  {line}" for line in done.stdout.splitlines()
                     if line.startswith(("wrote", "unchanged", "remove", "failed ")) or " was still loaded" in line) or "  none"


def _source() -> Path:
    """This tree, unless this is an installed copy: then the checkout it came from."""
    here = store.ROOT
    if not lab.inside(here, [root()]):
        return here
    named = store.read_dict(_record()).get("source")
    if named and os.path.isdir(named):
        return Path(named)
    raise UserError(f"this is an installed copy and its checkout is gone. Run install from a checkout: <checkout>/bin/athena-rex install")


def install(dry_run=False, force=False, services=False, real_world=False, guard_out=False) -> int:
    if not dry_run:
        _refuse_in_the_lab("install")
    if lab.active() and guard_out and not world.stubbed("launchctl"):
        raise lab.Refused("--lab-guard-out in the lab needs stubs for the real tools: the copy it makes runs with no guard, and would reach real launchd")
    print(f"athena-rex install{' (a dry run: nothing changes)' if dry_run else ''}\n")
    stopped = False
    for mark, what, detail in preflight():
        print(f"  {dict(ok='✓', warn='!', stop='✗')[mark]} {what:<7} {detail}")
        stopped = stopped or mark == "stop"
    if stopped:
        print("\nNothing was changed: fix what the crosses say, then run this again.")
        return 1
    print()
    source = _source()
    before = store.read_dict(_record())
    guard_out = guard_out or before.get("lab_guard") == "out"
    name = release_id(source, guard_out)
    was = _current()
    if dry_run:
        print(f"  {'=' if was == name else '+'} release {name} in {_short(root() / 'releases')}{f' (now {was})' if was and was != name else ''}")
        print(f"  {'=' if _launcher_is_ours() else '+'} {_short(launcher())}")
        for line in _state_step(True, real_world):
            print(f"  {line}")
        return 0
    if launcher().exists() and not _launcher_is_ours() and not force:
        raise UserError(f"{_short(launcher())} exists and isn't ours. Look at it; --force moves it aside and goes on.")
    _make_release(source, name, guard_out)
    _point_current(name)
    print(f"  {'=' if was == name else '~' if was else '+'} release {name}" + (f", was {was}" if was and was != name else ""))
    print(f"  {'=' if _write_launcher(force) == 'unchanged' else '+'} {_short(launcher())}")
    store.write_text(_record(), json.dumps({"version": 1, "release": name, "previous": was if was != name else before.get("previous"),
                                            "source": str(source), "lab_guard": "out" if guard_out else "on", "ts": time.time()}, indent=1) + "\n")
    _prune({name, was if was != name else before.get("previous")})
    for line in _state_step(False, real_world):
        print(f"  {line}")
    if services:
        print(f"  launchd jobs:\n{_services('install')}")
    else:
        print("  · launchd jobs not touched: `athena-rex install --services` starts them at login")
    print()
    if not _on_path():
        print(f"{_short(launcher().parent)} is not on your PATH. Add this to your shell's profile:\n  export PATH=\"$HOME/.local/bin:$PATH\"\n")
    done = _installed_command("--help")
    if done.returncode:
        print(f"The new copy did not run: {done.stderr.strip()}\nBack to the last one: athena-rex install --rollback")
        return 1
    print("Installed. `athena-rex install --check` looks it over; `athena-rex doctor` looks at everything else.")
    return 0


def check() -> int:
    """Every part, each with what clears it."""
    rows = []
    current = _current()
    rows.append((bool(current) and (root() / "current/bin/athena-rex").is_file(), "release", current or "none: run install"))
    rows.append((_launcher_is_ours(), "launcher", _short(launcher())))
    rows.append((_on_path(), "PATH", "has it" if _on_path() else f"add {_short(launcher().parent)} to PATH"))
    if current:
        done = _installed_command("--help")
        rows.append((done.returncode == 0, "runs", "--help works" if done.returncode == 0 else done.stderr.strip()[:200]))
    profiles = caps.get("profiles")
    for role in (profiles.ROLES if profiles else ()):
        written = store.path("profiles", f"{role}.json").exists()
        rows.append((written, f"profile {role}", "written" if written else "not yet: state hasn't moved here, or run install"))
    for ok, what, detail in rows:
        print(f"  {'✓' if ok else '✗'} {what:<16} {detail}")
    return 0 if all(ok for ok, what, _ in rows if what != "PATH") else 1


def rollback() -> int:
    _refuse_in_the_lab("roll back")
    record = store.read_dict(_record())
    previous = record.get("previous")
    if not previous or not (root() / "releases" / previous).is_dir():
        raise UserError("no earlier release is kept here")
    _point_current(previous)
    store.write_text(_record(), json.dumps({**record, "release": previous, "previous": record.get("release"), "ts": time.time()}, indent=1) + "\n")
    print(f"  ~ release {previous}, was {record.get('release')}")
    return 0


def _still_running() -> list:
    """Claudes whose hooks name this install: taking it away would silently stop their report gate, their rules and their status."""
    names = [w["name"] for w in ledger.live() if ledger.managed(w)]
    hq = caps.get("hq")
    try:
        if hq and hq.alive():
            names.append("HQ")
    except Exception:
        names.append("HQ, which nobody can say is gone")
    return names


def _jobs_left() -> list:
    services = caps.get("services")
    return sorted(p.name for p in services.agents_dir().glob(f"{services.PREFIX}.*.plist")) if services else []


def uninstall(dry_run=False, force=False) -> int:
    """Everything install made, and nothing else. State stays, and the last line says where."""
    if not dry_run:
        _refuse_in_the_lab("uninstall")
    left = _still_running()
    if left and not force and not dry_run:
        raise UserError(f"still running on this install: {', '.join(left)}. Retire them, or `hq stop`, first: their hooks would stop silently. --force goes past this.")
    verb = "would remove" if dry_run else "removed"
    if (root() / "current/bin/athena-rex").exists() and not dry_run:
        print(_services("uninstall"))
        jobs = _jobs_left()
        if jobs:
            raise UserError(f"these launchd jobs are still there, and would crash-loop on a missing program: {', '.join(jobs)}. Nothing else was removed.")
    if _launcher_is_ours():
        if not dry_run:
            launcher().unlink()
        print(f"  {verb} {_short(launcher())}")
    elif launcher().exists():
        print(f"  left {_short(launcher())}: it isn't ours")
    if root().is_dir():
        if not dry_run:
            shutil.rmtree(root())
        print(f"  {verb} {_short(root())}")
    print(f"\nState stays at {_short(store.state_dir())}. Nothing in ~/.claude was ever written, so nothing there needs undoing.")
    return 0


def _configure(p):
    p.add_argument("--dry-run", action="store_true", help="say what would change, and change nothing")
    p.add_argument("--check", action="store_true", help="look over what is installed")
    p.add_argument("--rollback", action="store_true", help="back to the release before this one")
    p.add_argument("--uninstall", action="store_true", help="remove what install made; state stays")
    p.add_argument("--force", action="store_true", help="move aside a launcher that isn't ours; with --uninstall, go past what is still running")
    p.add_argument("--services", action="store_true", help="also start the background jobs at login")
    p.add_argument("--real-world", action="store_true", help="set rex.real_world in this state dir's config")
    p.add_argument("--lab-guard-out", action="store_true", help="take the lab's development guards out of the installed copy")


def _run(args):
    modes = [name for name in ("check", "rollback", "uninstall") if getattr(args, name)]
    if len(modes) > 1:
        raise UserError(f"one at a time: {', '.join('--' + m for m in modes)}")
    extra = [flag for flag, on in (("--services", args.services), ("--real-world", args.real_world), ("--lab-guard-out", args.lab_guard_out)) if on]
    if modes and extra:
        raise UserError(f"{', '.join(extra)} only go with a plain install, not --{modes[0]}")
    if args.dry_run and modes[:1] in (["check"], ["rollback"]):
        raise UserError(f"--dry-run doesn't go with --{modes[0]}: it would do it anyway")
    if args.force and modes[:1] in (["check"], ["rollback"]):
        raise UserError(f"--force doesn't go with --{modes[0]}")
    if args.check:
        return check()
    if args.rollback:
        return rollback()
    if args.uninstall:
        return uninstall(args.dry_run, args.force)
    return install(args.dry_run, args.force, args.services, args.real_world, args.lab_guard_out)


def commands():
    return {"install": caps.Command(SUMMARY, _configure, _run)}
