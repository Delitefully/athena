"""Install, update and remove athena, one command each way.

    ~/.local/share/athena/      the release store (releases.py): whole copies, the `current` link, install.json
    ~/.local/bin/athena         a two-line launcher through `current`
    <state>/profiles/<role>.json permission profiles, diffed on each install

It writes nothing in ~/.claude: hooks and permissions go in each session's own --settings. State is never moved or removed here:
that is docs/upgrade-from-herdr.md's job. In the lab it refuses any HOME but a marked scratch one, and only --dry-run may look at
the real one. `update` (update.py) brings the newest release from the repo and keeps it only if it is healthy.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from athena.cli import UserError
from athena.caps.install import releases
from athena.kernel import caps, config, lab, ledger, store, world

SUMMARY = "install, update, roll back or remove athena"
MARK = "# athena launcher, written by `athena install`."
MIN_DISK_GB = 1


def launcher() -> Path:
    return releases.home() / ".local/bin/athena"


def refuse_in_the_lab(what):
    home = releases.home()
    if lab.active() and not lab.scratch_place(home):
        raise lab.Refused(f"refusing to {what} under {home}: ATHENA_WORKER={os.environ['ATHENA_WORKER']} may only install into a marked "
                          f"scratch HOME ({lab.SCRATCH_MARK}). --dry-run looks and changes nothing.")


def _probe(*argv) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (done.stdout or done.stderr).strip()
    return out.splitlines()[0] if done.returncode == 0 and out else ""


def preflight() -> list:
    """(mark, what, detail). A cross stops the install; a warning means a part stays off and says which."""
    rows = []
    version = ".".join(map(str, sys.version_info[:3]))
    rows.append(("ok" if sys.version_info >= (3, 9) else "stop", "python", version))
    found = _probe("git", "--version")
    rows.append(("ok" if found else "stop", "git", found or "not found: install Xcode's command line tools (xcode-select --install)"))
    disk = shutil.disk_usage(releases.home()).free // (1 << 30)
    rows.append(("ok" if disk >= MIN_DISK_GB else "stop", "disk", f"{disk} GB free"))
    binary = str(config.rex("binary"))
    rex = _probe(binary, "--autostart=false", "version") if os.path.exists(binary) else ""
    rows.append(("ok" if rex else "warn", "rex", rex or f"not at {binary}: spawn and HQ need the Rex app (config rex.binary)"))
    claude = str(config.rex("claude")).split()[0]
    found = _probe(claude, "--version")
    rows.append(("ok" if found else "warn", "claude", found or f"{claude} not found: spawn and HQ need Claude Code"))
    rows.append(("ok" if shutil.which("gh") else "warn", "gh", "found" if shutil.which("gh") else "not found: PRs stay unread (brew install gh)"))
    return rows


def _marked(path, mark) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and mark in path.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def _launcher_text(command, mark) -> str:
    return f'#!/bin/sh\n{mark}\nexec "{releases.root()}/current/bin/{command}" "$@"\n'


def _write_launcher(force) -> str:
    target = launcher()
    wanted = _launcher_text("athena", MARK)
    if target.exists() or target.is_symlink():
        if _marked(target, MARK) and target.read_text() == wanted:
            return "unchanged"
        if not _marked(target, MARK):
            if not force:
                raise UserError(f"{releases.short(target)} exists and isn't ours. Look at it; --force moves it aside and goes on.")
            aside = target.with_name(f"{target.name}.before-install.{int(time.time())}")
            os.replace(target, aside)
            print(f"  moved aside {releases.short(aside)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(target, wanted, mode=0o755)
    return "written"


def _on_path() -> bool:
    return str(launcher().parent) in os.environ.get("PATH", "").split(os.pathsep)


def set_real_world():
    """Opens the door to the real world for this state dir. Its own config, and only this key."""
    try:
        config.update_rex(lambda rex: rex.__setitem__("real_world", True), store.path("config.json"))
    except ValueError as exc:
        raise UserError(str(exc))


def _state_step(dry_run, real_world, profiles) -> list:
    """Lines to say. A state dir that lives elsewhere is left alone: the upgrade moves it, and the install doesn't guess."""
    elsewhere = store.state_elsewhere()
    if elsewhere:
        return [f"= state lives at {releases.short(elsewhere)}: nothing written. scripts/upgrade-from-herdr.py moves it (docs/upgrade-from-herdr.md)"]
    folder = store.state_dir()
    lines = []
    fresh = not (folder / "config.json").exists() and not (folder / "ledger.jsonl").exists()
    if fresh or real_world:
        if not dry_run:
            set_real_world()
        lines.append(f"+ rex.real_world in {releases.short(folder / 'config.json')}")
    if profiles and dry_run:
        lines.append(f"+ permission profiles in {releases.short(folder / 'profiles')}")
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


def _services(action) -> str:
    done = releases.installed_command("services", action)
    failed = [line for line in done.stdout.splitlines() if line.startswith("failed ")]
    if done.returncode and not failed:
        return f"  launchd jobs: skipped ({(done.stderr or done.stdout).strip()})"
    return "\n".join(f"  {line}" for line in done.stdout.splitlines()
                     if line.startswith(("wrote", "unchanged", "remove", "failed ")) or " was still loaded" in line) or "  none"


def _source():
    """The checkout to install from: this tree, or None when this is an installed copy, which keeps the release that runs (an
    update may have brought a newer one than any checkout). A new release from a checkout is <checkout>/bin/athena install."""
    return None if lab.inside(store.ROOT, [releases.root()]) else store.ROOT


def install(dry_run=False, force=False, services=False, real_world=False, guard_out=False) -> int:
    if not dry_run:
        refuse_in_the_lab("install")
    if lab.active() and guard_out and not world.stubbed("launchctl"):
        raise lab.Refused("--lab-guard-out in the lab needs stubs for the real tools: the copy it makes runs with no guard, and would reach real launchd")
    print(f"athena install{' (a dry run: nothing changes)' if dry_run else ''}\n")
    stopped = False
    for mark, what, detail in preflight():
        print(f"  {dict(ok='✓', warn='!', stop='✗')[mark]} {what:<7} {detail}")
        stopped = stopped or mark == "stop"
    if stopped:
        print("\nNothing was changed: fix what the crosses say, then run this again.")
        return 1
    print()
    if dry_run:
        return _install(True, force, services, real_world, guard_out)
    if launcher().exists() and not _marked(launcher(), MARK) and not force:  # before anything is made
        raise UserError(f"{releases.short(launcher())} exists and isn't ours. Look at it; --force moves it aside and goes on.")
    with releases.locked() as got:
        if not got:
            raise UserError("an update is running: install after it")
        return _install(False, force, services, real_world, guard_out)


def _install(dry_run, force, services, real_world, guard_out) -> int:
    # Loaded before a recovery can move `current`: this process has `current` on its path, so an import after the move would
    # load the other release's code into this one.
    profiles = caps.get("profiles")
    record = releases.load()
    if record["switch"] and not dry_run:  # first: while a switch is on record, `current` is what it came from
        from athena.caps.install import update
        record = update.recover(record)
    source = _source()
    guard_out = guard_out or record["lab_guard"] == "out"
    was = record["current"]
    if source is None and not releases.exists(was):
        raise UserError("this installed copy's release is not on record: run install from a checkout, <checkout>/bin/athena install")
    name = was["release"] if source is None else releases.release_id(source, guard_out)
    same = releases.exists(was) and was["release"] == name
    if dry_run:
        print(f"  {'=' if same else '+'} release {name} in {releases.short(releases.root() / 'releases')}"
              + (f" (now {was['release']})" if was and not same else ""))
        print(f"  {'=' if _marked(launcher(), MARK) else '+'} {releases.short(launcher())}")
        for line in _state_step(True, real_world, profiles):
            if line:
                print(f"  {line}")
        return 0
    made = was if same else {**releases.make(source, guard_out), "commit": releases.checkout_commit(source)}
    releases.point(made)
    print(f"  {'=' if same else '~' if was else '+'} release {name}" + (f", was {was['release']}" if was and not same else "")
          + (", the one running: `athena update` brings a newer one" if source is None else ""))
    print(f"  {'=' if _write_launcher(force) == 'unchanged' else '+'} {releases.short(launcher())}")
    previous = record["previous"] if same else was
    releases.save({**record, "current": made, "previous": previous, "source": str(source) if source else record["source"],
                   "lab_guard": "out" if guard_out else "on"})
    releases.prune(made, previous)
    for line in _state_step(False, real_world, profiles):
        if line:
            print(f"  {line}")
    if services:
        print(f"  launchd jobs:\n{_services('install')}")
    else:
        print("  · launchd jobs not touched: `athena install --services` starts them at login")
    print()
    if not _on_path():
        print(f"{releases.short(launcher().parent)} is not on your PATH. Add this to your shell's profile:\n  export PATH=\"$HOME/.local/bin:$PATH\"\n")
    done = releases.installed_command("--version")
    if done.returncode:
        print(f"The new copy did not run: {done.stderr.strip()}\nBack to the last one: athena install --rollback")
        return 1
    print(f"Installed {done.stdout.strip()}. `athena install --check` looks it over; `athena doctor` looks at everything else.")
    return 0


def check() -> int:
    """Every part, each with what clears it."""
    record = releases.load()
    current = record["current"]
    rows = [(releases.exists(current) and releases.pointed() == current["release"], "release",
             f"{current['version']} ({current['release']})" if current else "none: run install")]
    rows.append((_marked(launcher(), MARK), "launcher", releases.short(launcher())))
    rows.append((_on_path(), "PATH", "has it" if _on_path() else f"add {releases.short(launcher().parent)} to PATH"))
    if current:
        done = releases.installed_command("--version")
        rows.append((done.returncode == 0, "runs", done.stdout.strip() if done.returncode == 0 else done.stderr.strip()[:200]))
    rows.append((True, "updates", f"{record['channel']} from {record['repo']}"))
    profiles = caps.get("profiles")
    for role in (profiles.ROLES if profiles else ()):
        written = store.path("profiles", f"{role}.json").exists()
        rows.append((written, f"profile {role}", "written" if written else "not yet: state hasn't moved here, or run install"))
    for ok, what, detail in rows:
        print(f"  {'✓' if ok else '✗'} {what:<16} {detail}")
    return 0 if all(ok for ok, what, _ in rows if what != "PATH") else 1


def rollback() -> int:
    refuse_in_the_lab("roll back")
    with releases.locked() as got:
        if not got:
            raise UserError("an update is running: it rolls back by itself if its health check fails")
        record = releases.load()
        if record["switch"]:  # an update cut off before its verdict: back to what ran before it, which also restarts the jobs
            from athena.caps.install import update
            update.recover(record)
            return 0
        previous, current = record["previous"], record["current"]
        if not releases.exists(previous):
            raise UserError("no earlier release is kept here")
        jobs = releases.installed_jobs()  # before `current` moves
        releases.point(previous)
        releases.save({**record, "current": previous, "previous": current})
    print(f"  ~ release {previous['release']}, was {current['release']}")
    print(f"  · {releases.restart_services(jobs)}")  # the watch and dash would go on running the other release's code
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
    return sorted(p.name for p in services.installed_plists()) if services else []


def uninstall(dry_run=False, force=False) -> int:
    """Everything install made, and nothing else. State stays, and the last line says where. Not while an update runs: it
    would write its record and link into a folder that is gone."""
    if not dry_run:
        refuse_in_the_lab("uninstall")
    if dry_run or not releases.root().is_dir():
        return _uninstall(dry_run, force)
    with releases.locked() as got:
        if not got:
            raise UserError("an install or update is running: uninstall after it")
        return _uninstall(dry_run, force)


def _uninstall(dry_run, force) -> int:
    left = _still_running()
    if left and not force and not dry_run:
        raise UserError(f"still running on this install: {', '.join(left)}. Retire them, or `hq stop`, first: their hooks would stop silently. --force goes past this.")
    verb = "would remove" if dry_run else "removed"
    if (releases.root() / "current/bin/athena").exists() and not dry_run:
        print(_services("uninstall"))
        jobs = _jobs_left()
        if jobs:
            raise UserError(f"these launchd jobs are still there, and would crash-loop on a missing program: {', '.join(jobs)}. Nothing else was removed.")
    target = launcher()
    if _marked(target, MARK):
        if not dry_run:
            target.unlink()
        print(f"  {verb} {releases.short(target)}")
    elif target.exists():
        print(f"  left {releases.short(target)}: it isn't ours")
    if releases.root().is_dir():
        if not dry_run:
            shutil.rmtree(releases.root())
        print(f"  {verb} {releases.short(releases.root())}")
    print(f"\nState stays at {releases.short(store.state_dir())}. Nothing in ~/.claude was ever written, so nothing there needs undoing.")
    return 0


def _configure(p):
    p.add_argument("--dry-run", action="store_true", help="say what would change, and change nothing")
    p.add_argument("--check", action="store_true", help="look over what is installed")
    p.add_argument("--rollback", action="store_true", help="back to the release before this one")
    p.add_argument("--uninstall", action="store_true", help="remove what install made; state stays")
    p.add_argument("--force", action="store_true", help="move aside a launcher that isn't ours; with --uninstall, go past what is still running")
    p.add_argument("--services", action="store_true", help="also start the background jobs at login")
    p.add_argument("--real-world", action="store_true", help="set rex.real_world in this state dir's config")
    p.add_argument("--lab-guard-out", action="store_true", help="the lab's own installs only: take its guards out of the installed copy")


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


def _configure_update(p):
    p.add_argument("--check", action="store_true", help="say whether a newer release is out, and install nothing")
    p.add_argument("--channel", choices=releases.CHANNELS, help="release: the newest vX.Y.Z tag (the default); main: main's head. Kept for next time")
    p.add_argument("--repo", help="where releases come from (default the public repo). Kept for next time")


def _run_update(args):
    from athena.caps.install import update
    return update.update(args.check, args.channel, args.repo)


def checks(ctx):
    from athena.caps.install import update
    return update.check(ctx)


def commands():
    return {"install": caps.Command(SUMMARY, _configure, _run),
            "update": caps.Command("bring the newest athena release, keep it only if healthy", _configure_update, _run_update)}
