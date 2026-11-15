#!/usr/bin/python3 -I
"""From the herdr athena to athena, as docs/upgrade-from-herdr.md walks it: scripts/upgrade-from-herdr.py run [--dry-run], or one step at a
time: check|snapshot|stop-old|move|launcher|install|plugin|verify, and compare|rollback|restore-snapshot to look back or go back.

Each step looks first, says what it does, and can be run again: a second run changes nothing. `run` goes through them all in order and stops at
the first that can't go on, with what to do; --dry-run says what each would do and changes nothing. Everything is under $HOME, so a rehearsal
with an isolated HOME is the same code. While a worker of athena's own lab runs it (ATHENA_WORKER=rex-*) it refuses any HOME but a marked
scratch one.

What the herdr athena left, and where each goes:
    ~/.local/state/athena            renamed to ~/Library/Application Support/athena; the old path becomes a link to it
    athena.doctor, .dash, .board     stopped, their job files kept in ~/Library/LaunchAgents.athena-old
    athena.herdr                     left: it is the herdr server's, which may serve more than athena
    the old HQ's herdr workspace     closed, so herdr's session restore has no pane to resume the old HQ in
    ~/.local/bin/athena              herdr athena's link to its checkout, moved aside to ~/.local/bin/athena-herdr
    the athena@athena Claude plugin  turned off in ~/.claude/settings.json (hooks, skill, watch mod), the file kept in the snapshot
    athena                           installed with its one launchd job, local.athena, which runs every service
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from athena.kernel import lab, world  # noqa: E402

HOME = os.path.expanduser("~")
OLD = os.path.join(HOME, ".local/state/athena")
NEW = os.path.join(HOME, "Library/Application Support/athena")
RECORD = os.path.join(HOME, ".local/state/athena-upgrade.json")
AGENTS = os.path.join(HOME, "Library/LaunchAgents")
OFF = os.path.join(HOME, "Library/LaunchAgents.athena-old")
OLD_JOBS = ("athena.doctor", "athena.dash", "athena.board", "athena-lights")  # doctor first, or it brings the others back
NEW_JOBS = ("local.athena",)  # one job: its supervisor runs the doctor, the watch, the dash and the menu bar
KEPT = "athena.herdr"
CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(HOME, ".local/bin/athena")
HERDR_LAUNCHER = os.path.join(HOME, ".local/bin/athena-herdr")
LAUNCHER_MARK = "# athena launcher, written by `athena install`."
CLAUDE_SETTINGS = os.path.join(HOME, ".claude/settings.json")
OLD_PLUGIN = "athena@athena"
HQ_LABEL = "athena"  # the label the herdr athena kept on HQ's workspace, and went by
SPACE_NOT_FOUND = ("workspace_not_found", "not_found")
DASH_PORT_DEFAULT = 2843
GROWS_ONLY = ("ledger.jsonl", "history.jsonl", "requests.jsonl", "actions.jsonl", "items-archive.jsonl")
COMES_AND_GOES = ("run/", "logs/", "locks/", "workers/")
WAIT_FOR_JOBS = float(os.environ.get("ATHENA_UPGRADE_WAIT") or 60)


DRY = False  # --dry-run: every step says what it would do, and does nothing


def say(mark, text):
    print(f"  {mark} {text}".replace(HOME, "~"), flush=True)


def would(mark, text) -> bool:
    """Says `text`; True when it is to be done, False on a dry run, where it is only said."""
    say(mark, f"would: {text}" if DRY else text)
    return not DRY


def stop(text):
    print(f"upgrade: {text}".replace(HOME, "~"), file=sys.stderr)
    sys.exit(1)


def record() -> dict:
    try:
        with open(RECORD) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember(**fields):
    data = {**record(), **fields}
    tmp = f"{RECORD}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, RECORD)


def fingerprint(root) -> dict:
    """{relative path: {size, sha}} of every file, and each link's target."""
    out = {}
    for folder, dirs, names in os.walk(root):
        for name in names + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                out[rel] = {"link": os.readlink(path)}
                continue
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        digest.update(chunk)
                out[rel] = {"size": os.path.getsize(path), "sha": digest.hexdigest()}
            except OSError as exc:
                out[rel] = {"unreadable": str(exc)}
    return out


def _is_prefix(path, size, sha) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        digest.update(f.read(size))
    return digest.hexdigest() == sha


def _same_json(root_a, root_b, rel) -> bool:
    try:
        with open(os.path.join(root_a, rel)) as a, open(os.path.join(root_b, rel)) as b:
            return json.load(a) == json.load(b)
    except (OSError, ValueError):
        return False


def compare_trees(baseline, snapshot, now_root) -> dict:
    """What happened to each file of `baseline`, a manifest of `snapshot`. Only `changed` and `gone` are a loss to explain."""
    now = fingerprint(now_root)
    out = {"same": [], "same_json": [], "grew": [], "changed": [], "gone": [], "added": sorted(set(now) - set(baseline))}
    for rel, was in baseline.items():
        found = now.get(rel)
        if found is None:
            out["gone"].append(rel)
        elif found == was:
            out["same"].append(rel)
        elif was.get("sha") and found.get("size", 0) > was["size"] and _is_prefix(os.path.join(now_root, rel), was["size"], was["sha"]):
            out["grew"].append(rel)
        elif snapshot and _same_json(snapshot, now_root, rel):
            out["same_json"].append(rel)
        else:
            out["changed"].append(rel)
    return out


def _labels_in(folder, names):
    return [n for n in names if os.path.exists(os.path.join(folder, f"{n}.plist"))]


def _launchctl(*args):
    """In the lab only a marked stub: main() refuses without one, so no path here reaches the real launchd from a worker of this rewrite."""
    program = world.resolve("launchctl") if lab.active() else "launchctl"
    try:
        return subprocess.run([program, *args], capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _loaded(label):
    """True, False, or None when launchctl can't say. Only `print` says: a job file gone or a bootout's exit code says nothing of a running job."""
    code = _launchctl("print", f"{_domain()}/{label}")
    return True if code == 0 else False if code in (3, 113) else None


def _gone_within(label, seconds) -> bool:
    deadline = time.monotonic() + seconds
    while _loaded(label) is not False:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def _processes():
    """[(pid, command)] of everything running, or None if nobody can say. In the lab the Mac's real processes aren't a scratch state's,
    so only a stand-in table is looked at, as hq.py's own scan does."""
    if lab.active() and not os.environ.get("ATHENA_PS"):
        return []
    try:
        out = subprocess.run([os.environ.get("ATHENA_PS") or "ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    rows = (line.strip().split(None, 1) for line in out.stdout.splitlines())
    return [(row[0], row[1]) for row in rows if len(row) == 2]


def _old_hq_and_watch(rows) -> list:
    """Claudes named as HQ, whatever a pid file says, and the old watch: the athena-watch mod runs `athena watch --tagged`, with no --wake."""
    name = re.escape(os.environ.get("ATHENA_HQ") or "athena")
    found = []
    for pid, command in rows:
        if re.search(rf"(?:^|\s)--name\s+{name}(?:\s|$)", command) and "claude" in command:
            found.append(f"HQ's Claude at pid {pid}")
        elif re.search(r"\bathena watch --tagged\s*$", command):
            found.append(f"the old watch at pid {pid} (after /exit it only dies at its next print: `kill {pid}`, that process only)")
    return found


def _old_workers(state) -> list:
    """Live workers the old athena started: no `backend` on their record."""
    return [n for n, w in _workers(state).items() if w.get("state") == "live" and not w.get("backend")]


def _new_workers(state) -> list:
    """Live workers athena on Rex started: a `backend` on their record."""
    return [n for n, w in _workers(state).items() if w.get("state") == "live" and w.get("backend")]


def _workers(state) -> dict:
    out = {}
    try:
        with open(os.path.join(state, "ledger.jsonl"), errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        name = event.get("name") if isinstance(event, dict) else None
        if not isinstance(name, str):
            continue
        kind = event.get("event")
        base = {} if kind == "spawn" else out.get(name, {})
        merged = {**base, **{k: v for k, v in event.items() if k not in ("event", "ts")}}
        merged["state"] = {"spawn": "live", "retire": "retired", "failed": "failed"}.get(kind, base.get("state", "damaged"))
        out[name] = merged
    return out


def _state_now():
    return NEW if os.path.isdir(NEW) else OLD


def _auto_items(state) -> int:
    try:
        with open(os.path.join(state, "needs-you.json")) as f:
            items = json.load(f).get("items") or []
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for i in items if isinstance(i, dict) and i.get("auto") and i.get("rec") and not i.get("resolved") and not i.get("stakes") and not i.get("applied"))


def cmd_check(args):
    """Look only: what stops the upgrade, each with what clears it."""
    problems = 0
    def row(ok, text, hard=True):
        nonlocal problems
        say("✓" if ok else ("✗" if hard else "!"), text)
        problems += 0 if ok or not hard else 1
    moved = os.path.islink(OLD) and os.path.isdir(NEW)
    row(moved or (os.path.isdir(OLD) and not os.path.islink(OLD)), f"state at {OLD}" + (" (already moved)" if moved else ""))
    row(moved or not os.path.exists(NEW), f"nothing at {NEW} yet" if moved or not os.path.exists(NEW) else
        f"{NEW} exists already, so the move would not be a rename: look at what is there")
    parent = os.path.dirname(NEW)
    if not moved and os.path.isdir(parent) and os.path.isdir(OLD):
        row(os.stat(OLD).st_dev == os.stat(parent).st_dev, "both paths on one volume, so the move is a rename")
    free = shutil.disk_usage(HOME).free // (1 << 30)
    row(free >= 10, f"{free} GB free")
    row(not os.environ.get("ATHENA_STATE_DIR"), "no ATHENA_STATE_DIR in this shell" if not os.environ.get("ATHENA_STATE_DIR") else
        f"this shell sets ATHENA_STATE_DIR={os.environ['ATHENA_STATE_DIR']}: open a new one without it, or install writes in the old folder")
    old = _old_workers(_state_now())
    row(not old, "no worker runs on the herdr athena" if not old else
        f"these still run on the herdr athena, which the upgrade takes away: {', '.join(old)}. Let each finish, then `athena retire <name>`")
    if not moved:
        rows = _processes()
        running = _old_hq_and_watch(rows) if rows is not None else ["nobody can say what is running: ps did not answer"]
        row(not running, "the herdr athena's HQ and watch are gone" if not running else
            f"exit the herdr athena's HQ first (/exit in its pane, then close the pane): {'; '.join(running)}")
    say("·", f"{_auto_items(_state_now())} open items with `auto` take their recommendation together, 12 hours after the new watch first looks")
    say("·", f"old jobs to stop: {', '.join(_labels_in(AGENTS, OLD_JOBS)) or 'none'}; {KEPT} stays")
    print("ready" if not problems else f"{problems} to clear first")
    return 1 if problems else 0


def cmd_snapshot(args):
    """Before anything moves. On APFS a clone: instant, and it costs no room until files change."""
    if record().get("snapshot") and os.path.isdir(record()["snapshot"]) and not getattr(args, "again", False) and not record().get("rolled_back"):
        say("=", f"snapshot already taken: {record()['snapshot']} (--again takes another)")
        return 0
    if os.path.islink(OLD) and os.path.isdir(NEW):
        say("=", "the state has moved already, so there is nothing left to snapshot before it")
        return 0
    if not os.path.isdir(OLD) or os.path.islink(OLD):
        stop(f"{OLD} isn't the state folder any more, so a snapshot from here would be of the wrong thing")
    target = f"{OLD}.bak-upgrade-{time.strftime('%Y%m%d-%H%M%S')}"
    if not would("+", f"snapshot {target}, with Claude's settings and the launchd jobs beside it"):
        return 0
    while os.path.exists(target):  # cp would copy into it
        target += "x"
    done = subprocess.run(["cp", "-cRp", OLD, target], capture_output=True, text=True)
    if done.returncode:
        shutil.rmtree(target, ignore_errors=True)
        subprocess.run(["cp", "-Rp", OLD, target], check=True)
    manifest = f"{target}.manifest.json"
    with open(manifest, "w") as f:
        json.dump(fingerprint(target), f)
    extras = f"{target}.outside"
    os.makedirs(extras, mode=0o700)
    for source, name in ((os.path.join(HOME, ".claude/settings.json"), "claude-settings.json"), (os.path.join(HOME, ".claude.json"), "claude.json")):
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(extras, name))
    for label in _labels_in(AGENTS, (*OLD_JOBS, KEPT)):
        shutil.copy2(os.path.join(AGENTS, f"{label}.plist"), extras)
    remember(snapshot=target, manifest=manifest, outside=extras, taken=time.time(), rolled_back=False)
    say("+", f"snapshot {target}")
    say("+", f"{len(json.load(open(manifest)))} files fingerprinted in {manifest}")
    say("+", f"Claude's settings and the launchd jobs copied to {extras}")
    return 0


def cmd_stop_old(args):
    """The old jobs stop and stay stopped, kept aside so rollback can bring them back. Each is asked to go, and looked at: a doctor that survived
    could restart the old HQ. One that won't go stops this with its job file where it was. herdr's own server job stays."""
    for label in OLD_JOBS:
        target = os.path.join(AGENTS, f"{label}.plist")
        loaded = _loaded(label)
        if loaded is False and not os.path.exists(target):
            say("=", f"{label} already off")
            continue
        if not would("-", f"stop {label} and keep its job in {OFF}"):
            continue
        os.makedirs(OFF, exist_ok=True)
        if loaded is not False:
            _launchctl("bootout", f"{_domain()}/{label}")
        if not _gone_within(label, 10):
            stop(f"{label} is still loaded, or launchctl can't say, so this stops here. Its job file wasn't moved. Look: launchctl print {_domain()}/{label}")
        if os.path.exists(target):
            os.replace(target, os.path.join(OFF, f"{label}.plist"))
    say("=", f"{KEPT} left running: it is herdr's server")
    return 0


def _herdr():
    """herdr's command, or None. In the lab only ATHENA_HERDR, a stand-in: the real one would reach the human's herdr server."""
    if lab.active():
        return os.environ.get("ATHENA_HERDR")
    return os.environ.get("ATHENA_HERDR") or shutil.which("herdr")


def _herdr_call(herdr, *args):
    """(result, error code) from herdr's CLI, read as the herdr athena read it: an error is JSON with a code, on either stream."""
    try:
        done = subprocess.run([herdr, *args], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"no answer ({type(exc).__name__})"
    for text in (done.stderr.strip(), done.stdout.strip()):
        try:
            error = json.loads(text).get("error") if text else None
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, dict):
            return None, str(error.get("code") or "error")
    if done.returncode:
        return None, f"exit_{done.returncode}"
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        data = {}
    return (data.get("result", data) if isinstance(data, dict) else {}), None


def cmd_old_hq(args):
    """The herdr athena's HQ workspace closed. herdr resumes the Claude it was told of in each pane it restores, so an old HQ
    pane left open comes back as a second HQ on the same state after any herdr restart, however it was exited. herdr reuses
    ids after a restart, so the id on record is closed only while herdr still labels it `athena`, as the herdr athena checked.
    Nothing is remembered: herdr is asked each time, so a re-run, or an upgrade again after a rollback, goes by what is open."""
    workspace = _read(os.path.join(_state_now(), "hq.json")).get("workspace")
    if not workspace:
        say("=", "no herdr workspace of the old HQ on record")
        return 0
    if os.environ.get("HERDR_PANE_ID") or os.environ.get("HERDR_ENV"):
        why = "this runs inside herdr, maybe in the very workspace it closes: run it from a terminal outside herdr"
        if DRY:
            say("!", f"would stop here: {why}")
            return 0
        stop(why)
    moved = os.path.islink(OLD) and os.path.isdir(NEW)
    if not moved and not DRY:  # once the state has moved, a Claude named athena is the new HQ, which this never touches
        rows = _processes()
        running = _old_hq_and_watch(rows) if rows is not None else ["nobody can say what is running: ps did not answer"]
        if running:
            stop(f"exit the herdr athena's HQ first (/exit in its pane), so closing its workspace ends nothing mid-turn: {'; '.join(running)}")
    herdr = _herdr()
    if not herdr:
        say("!", "herdr isn't here: in herdr, close the workspace labelled athena yourself, or a herdr restart resumes the old HQ")
        return 0
    found, error = _herdr_call(herdr, "workspace", "get", workspace)
    if error in SPACE_NOT_FOUND:
        say("=", f"the old HQ's herdr workspace {workspace} is closed")
        return 0
    if error:
        stop(f"herdr can't say what workspace {workspace} is ({error}): nothing was closed. Run this again once herdr answers")
    label = (found.get("workspace") or {}).get("label")
    if label != HQ_LABEL:
        say("!", f"herdr's workspace {workspace} is now {label!r}, not the old HQ's (herdr reuses ids after a restart): nothing "
                 "closed. If a workspace labelled athena is still open in herdr, close it yourself")
        return 0
    if not would("-", f"close the old HQ's herdr workspace {workspace}, so a herdr restart can't resume it"):
        return 0
    _, error = _herdr_call(herdr, "workspace", "close", workspace)
    if error and error not in SPACE_NOT_FOUND:
        stop(f"herdr would not close the old HQ's workspace {workspace} ({error}): close it in herdr, then run this again")
    return 0


def _merge_raced(raced, into):
    """A hook recreated the path in the instant between a rename and its link. What it wrote is newer than what moved: a whole file by its age,
    and a log's lines added on, as replacing one would lose all but its last line."""
    for folder, _, names in os.walk(raced):
        for name in names:
            source = os.path.join(folder, name)
            target = os.path.join(into, os.path.relpath(source, raced))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if name.endswith(".jsonl") and os.path.exists(target):
                with open(source, "rb") as new, open(target, "ab") as log:
                    log.write(new.read())
            elif not os.path.exists(target) or os.path.getmtime(source) >= os.path.getmtime(target):
                os.replace(source, target)
    shutil.rmtree(raced, ignore_errors=True)


def _link_old_path():
    try:
        os.symlink(NEW, OLD)
    except FileExistsError:
        raced = f"{OLD}.raced-{os.getpid()}"
        os.rename(OLD, raced)
        os.symlink(NEW, OLD)
        _merge_raced(raced, NEW)
        say("!", "a hook wrote to the old path during the move; what it wrote was merged in")


def cmd_move(args):
    """A rename, then a link from the old path: the old athena's hooks, jobs and the clamshell daemon all still name the old path."""
    if os.path.islink(OLD) and os.path.isdir(NEW):
        say("=", f"already moved: {OLD} -> {NEW}")
        return 0
    if not os.path.lexists(OLD) and os.path.isdir(NEW):
        if not would("+", f"finish a move killed halfway: link {OLD} to the moved state"):
            return 0
        _link_old_path()
        say("+", f"a move killed halfway is finished: {OLD} links to the moved state")
        return 0
    rows = _processes()
    running = _old_hq_and_watch(rows) if rows is not None else ["nobody can say what is running: ps did not answer"]
    blocked = ([f"the old HQ has to be gone first, nothing was moved: {'; '.join(running)}"] if running else []) + \
        ([f"{NEW} exists, so this would not be a rename. Look at what is there; nothing was changed"] if os.path.exists(NEW) else [])
    if blocked and DRY:  # a dry run says where the real one would stop, and goes on to show the rest
        say("!", f"would stop here: {'; '.join(blocked)}")
        return 0
    if blocked:
        stop(blocked[0])
    if not DRY and (not record().get("snapshot") or record().get("rolled_back")):
        stop("no snapshot since the last rollback: run the snapshot step first")
    if not would("~", f"rename {OLD} to {NEW}, and link the old path to it"):
        return 0
    os.makedirs(os.path.dirname(NEW), exist_ok=True)
    if os.stat(OLD).st_dev != os.stat(os.path.dirname(NEW)).st_dev:
        stop("the two paths are on different volumes; nothing was changed")
    os.rename(OLD, NEW)
    _link_old_path()
    say("~", f"state is at {NEW}")
    say("+", f"{OLD} is a link to it")
    return 0


def _installed():
    """athena's own launcher by its path, and only athena's: before the launcher step ~/.local/bin/athena is the herdr athena's."""
    try:
        with open(LAUNCHER) as f:
            return LAUNCHER if LAUNCHER_MARK in f.read() else None
    except (OSError, UnicodeDecodeError):
        return None


def cmd_launcher(args):
    """The herdr athena's command, a link into its checkout, moves aside to athena-herdr, so install can write athena's."""
    if _installed() or not os.path.lexists(LAUNCHER):
        say("=", f"{LAUNCHER} is free for athena's launcher" if not _installed() else f"{LAUNCHER} is athena's")
        return 0
    aside = HERDR_LAUNCHER if not os.path.lexists(HERDR_LAUNCHER) else f"{HERDR_LAUNCHER}.{int(time.time())}"
    if would("~", f"move the herdr athena's {LAUNCHER} to {aside}"):
        os.replace(LAUNCHER, aside)
        remember(launcher_aside=aside)
    return 0


def cmd_install(args):
    """athena itself, from this checkout, with its launchd jobs, opening the door for the moved state as the herdr athena had it open."""
    argv = [os.path.join(CHECKOUT, "bin/athena"), "install", "--services", "--real-world"]
    if DRY:
        argv.insert(3, "--dry-run")
        argv.remove("--services")
        argv.remove("--real-world")
    env = {k: v for k, v in os.environ.items() if k != "ATHENA_STATE_DIR"}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        stop(f"install did not run: {exc}")
    print("\n".join(f"    {line}" for line in done.stdout.rstrip().splitlines()).replace(HOME, "~"))
    if done.returncode:
        stop(f"install failed ({done.returncode}): {(done.stderr or done.stdout).strip()[-300:]}")
    return 0


def cmd_plugin(args):
    """The herdr athena's Claude plugin off, for every Claude: its hooks, skill and watch mod would act on a state and a herdr that are gone.
    Only that key changes, written atomically; the file as it was is in the snapshot's outside folder."""
    data = _read(CLAUDE_SETTINGS)
    if (data.get("enabledPlugins") or {}).get(OLD_PLUGIN) is not True:
        say("=", f"the {OLD_PLUGIN} plugin is not on")
        return 0
    if not would("-", f"turn the {OLD_PLUGIN} plugin off in {CLAUDE_SETTINGS}"):
        return 0
    _write_json(CLAUDE_SETTINGS, {**data, "enabledPlugins": {**data["enabledPlugins"], OLD_PLUGIN: False}}, backup=True)
    remember(plugin_was_on=True)
    return 0


def _ran(*argv, timeout=60):
    """athena's own command (only ever athena's launcher, never the herdr athena's), pinned to where the state is: run before the move, a
    lock file would make the new path by asking."""
    if argv[0] == "athena":
        if not _installed():
            return subprocess.CompletedProcess(argv, 127, "", "athena is not installed yet")
        argv = (_installed(), *argv[1:])
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env={**os.environ, "ATHENA_STATE_DIR": _state_now()},
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _dash_port() -> int:
    try:
        with open(os.path.join(NEW, "config.json")) as f:
            return int((json.load(f).get("rex") or {}).get("dash_port") or DASH_PORT_DEFAULT)
    except (OSError, ValueError):
        return DASH_PORT_DEFAULT


def _dash_serves() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_dash_port()}/healthz", timeout=3) as response:
            answer = json.load(response)
            return answer.get("state_dir") or ("old" if answer.get("state") else "")
    except (OSError, ValueError, urllib.error.URLError):
        return ""


def _doctor_job_ran() -> bool:
    """health.json newer than the job's file, and recent. Loaded says nothing of a doctor that crashes on every run. Read before verify's own doctor."""
    job = os.path.join(AGENTS, "local.athena.plist")
    deadline = time.monotonic() + WAIT_FOR_JOBS
    while True:
        ts = float(_read(os.path.join(NEW, "health.json")).get("ts") or 0)
        if os.path.exists(job) and ts >= os.path.getmtime(job) - 1 and time.time() - ts < 180:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def cmd_verify(args):
    failed = 0
    doctor_ran = _doctor_job_ran() if args.services else None
    def row(ok, text):
        nonlocal failed
        say("✓" if ok else "✗", text)
        failed += 0 if ok else 1
    row(os.path.islink(OLD) and os.path.realpath(OLD) == os.path.realpath(NEW), f"{OLD} leads to {NEW}")
    row(os.path.isfile(os.path.join(NEW, "ledger.jsonl")), "the ledger is at the new path")
    try:
        with open(os.path.join(NEW, "config.json")) as f:
            opened = (json.load(f).get("rex") or {}).get("real_world") is True
    except (OSError, ValueError):
        opened = False
    row(opened, "rex.real_world is set")
    done = _ran("athena", "install", "--check")
    row(done.returncode == 0, "athena install --check")
    done = _ran("athena", "status")
    row(done.returncode == 0, "athena status answers")
    print("\n".join(f"      {line}" for line in done.stdout.splitlines()).replace(HOME, "~"))
    done = _ran("athena", "doctor", "--json", timeout=300)  # a pass may take up to doctor's own limit, 240s
    try:
        answer = json.loads(done.stdout)
        problems = [p["check"] for p in answer.get("problems") or []]
    except (ValueError, AttributeError):
        answer, problems = {}, ["doctor did not answer"]
    if answer.get("busy"):  # the job's own doctor pass holds the lock: a pass is running, not a doctor that failed
        say("!", "doctor: another doctor pass is running, so this one did not look; `athena doctor` again in a minute")
    else:
        say("✓" if not problems else "!", f"doctor: {', '.join(problems) or 'all ok'}")
    for label in OLD_JOBS:
        row(_loaded(label) is False and not _labels_in(AGENTS, (label,)), f"{label} is off")
    rows = _processes()
    old = [found for found in _old_hq_and_watch(rows or []) if "old watch" in found]
    row(rows is not None and not old, "no old watch is running" if not old else f"{'; '.join(old)} is still running")
    if args.services:
        for job in NEW_JOBS:
            row(_loaded(job) is True, f"{job} is loaded")
        row(doctor_ran, "doctor has run and written health.json, so the clamshell daemon and the dash have a heartbeat")
        row(_watch_came_round(), "the watch has come round, so HQ gets its wakes")
        serves = _dash_serves()
        row(bool(serves) and os.path.realpath(serves) == os.path.realpath(NEW), f"the dash on port {_dash_port()} serves this state" if serves != "old" else f"an old dash holds port {_dash_port()}: `~/.local/bin/athena-herdr dash off`, the herdr athena's command")
    print("verified" if not failed else f"{failed} failed: see docs/upgrade-from-herdr.md, Going back")
    return 1 if failed else 0


def _watch_came_round() -> bool:
    deadline = time.monotonic() + WAIT_FOR_JOBS
    while True:
        try:
            with open(os.path.join(NEW, "run/watch-state.json")) as f:
                if time.time() - float(json.load(f).get("beat") or 0) < 300:
                    return True
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def cmd_compare(args):
    """What is worth stopping for is apart from what running things change. A log that was rewritten, or a file gone that nothing removes by itself."""
    data = record()
    if not data.get("manifest"):
        print("no snapshot was taken, so nothing has moved and there is nothing to compare")
        return 0
    with open(data["manifest"]) as f:
        baseline = json.load(f)
    found = compare_trees(baseline, data["snapshot"], os.path.realpath(_state_now()))
    print(f"against {os.path.basename(data['snapshot'])}, {len(baseline)} files:")
    print(f"  {len(found['same'])} byte for byte the same, {len(found['same_json'])} the same as JSON, {len(found['grew'])} only appended to")
    if found["grew"]:
        print("  appended to: " + ", ".join(sorted(found["grew"])[:40]))
    unexpected = [f for f in found["changed"] if f in GROWS_ONLY] + [f for f in found["gone"] if not f.startswith(COMES_AND_GOES) and not f.endswith((".lock", ".pid"))]
    usual = sorted((set(found["changed"]) | set(found["gone"])) - set(unexpected))
    if usual:
        print("  changed by things running, as usual: " + ", ".join(usual[:40]))
    print(f"  added: {len(found['added'])}" + (": " + ", ".join(found["added"][:40]) if found["added"] else ""))
    print("nothing unexpected" if not unexpected else f"UNEXPECTED, look before going on: {', '.join(sorted(unexpected))}")
    return 1 if unexpected else 0


def _hq_state() -> str:
    """up, gone, or unknown: `hq status` read as it says. As with the move, a state dir doesn't go out from under what may be a live HQ."""
    if not _installed():
        return "gone"
    try:
        alive = json.loads(_ran("athena", "hq", "status").stdout).get("alive")
    except (ValueError, AttributeError):
        return "unknown"
    return "up" if alive is True else "gone" if alive is False else "unknown"


def _hq_still_up(hq_gone=False) -> bool:
    """`--hq-gone` lifts only "can't say": an HQ `hq status` reads as alive always stops it (check with `pgrep -fl 'claude.*--name athena'`)."""
    state = _hq_state()
    return state == "up" or state == "unknown" and not hq_gone


def _stop_new(hq_gone=False):
    """The jobs first, or doctor brings things back, then what doctor may have started by itself. Anything still running stops this with nothing
    moved: its hooks and files name the new path, and would make it again."""
    if not _installed():
        return
    done = _ran("athena", "services", "uninstall")
    jobs = _labels_in(AGENTS, NEW_JOBS)
    if jobs:
        stop(f"these jobs didn't go, so nothing moved: {', '.join(jobs)}. {(done.stderr or done.stdout).strip()[:200]}")
    for job in NEW_JOBS:
        if not _gone_within(job, 10):
            stop(f"{job} is still loaded, or launchctl can't say, so nothing moved. Look: launchctl print {_domain()}/{job}")
    say("-", "the new job is gone")
    for argv, what in ((("watch", "--stop"), "a watch doctor started"), (("dash", "off"), "the dash")):
        done = _ran("athena", *argv)
        if done.returncode:
            stop(f"{what} wouldn't stop, so nothing moved: {(done.stderr or done.stdout).strip()[-200:]}")
    say("-", "no watch or dash is left")
    done = _ran("athena", "hq", "stop")
    if done.returncode:
        say("!", "HQ didn't exit by itself, so its session is closed. Its conversation stays on record")
        done = _ran("athena", "hq", "stop", "--force")
    if _hq_still_up(hq_gone):
        stop(f"HQ's Claude is still running, or `athena hq status` can't say, so nothing moved. Close it in its session, then run this again; if you have looked and it's gone, --hq-gone. {(done.stderr or done.stdout).strip()[-200:]}")
    say("-", "the new HQ is gone")


def _restore_hq_record():
    """The new HQ wrote its own command and session place here, naming the new path. The old athena's goes back, keeping the conversation it has
    moved on to, as that is what `athena hq` should resume. Nothing to do if the new version never started an HQ."""
    target = os.path.join(OLD, "hq.json")
    current = _read(target)
    if not current.get("backend"):
        return
    kept = _read(os.path.join(record().get("snapshot") or "", "hq.json"))
    session = _read(os.path.join(OLD, "hq-start.json")).get("session_id") or current.get("session") or current.get("previous_session") or kept.get("session")
    restored = {**kept, "session": session} if kept else {k: v for k, v in current.items() if k not in ("backend", "space", "block", "claude_args", "previous_session")}
    _write_json(target, restored)
    prev = os.path.join(OLD, "hq.json.prev")
    if os.path.exists(prev):
        os.unlink(prev)
    say("~", "hq.json is the old athena's again, on the conversation it has now")


def _read(path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data, backup=False):
    """Atomically, and through a link: a settings file kept in a dotfiles repo stays a link to it. With `backup`, the file as it
    was is kept once beside it, as <file>.before-athena-upgrade."""
    target = os.path.realpath(path)
    if backup and os.path.isfile(target) and not os.path.exists(f"{target}.before-athena-upgrade"):
        shutil.copy2(target, f"{target}.before-athena-upgrade")
    tmp = f"{target}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, target)


def _move_back():
    if os.path.islink(OLD):
        os.unlink(OLD)
    if os.path.isdir(NEW):
        if os.path.exists(OLD):
            raced = f"{OLD}.raced-{os.getpid()}"
            os.rename(OLD, raced)
            os.rename(NEW, OLD)
            _merge_raced(raced, OLD)
            say("!", "something wrote to the old path during the move back; what it wrote was merged in")
        else:
            os.rename(NEW, OLD)
        say("~", f"state is back at {OLD}")
    elif os.path.isdir(OLD):
        say("=", f"state is already at {OLD}")
    else:
        stop(f"no state at {OLD} or {NEW}. Restore the snapshot: {record().get('snapshot')}")


def cmd_rollback(args):
    """From any step, back to the old athena on the old path. The state goes back with what was written since: the new version only added."""
    state = NEW if os.path.isdir(NEW) else OLD
    left = _new_workers(state)
    if left and not args.leave_workers:
        stop(f"these run on the new version, and the old one can't see them: {', '.join(left)}. Retire each with `athena retire <name>` first, or pass --leave-workers")
    _stop_new(args.hq_gone)
    _move_back()
    _restore_hq_record()
    config = os.path.join(OLD, "config.json")
    data = _read(config)
    if isinstance(data.get("rex"), dict) and data["rex"].pop("real_world", None) is not None:
        _write_json(config, data)
        say("-", "rex.real_world taken out")
    armed = os.path.join(OLD, "run/defaults-armed.json")
    if os.path.exists(armed):
        os.unlink(armed)  # a later upgrade gives the queue its whole wait again
    aside = record().get("launcher_aside")
    if aside and os.path.lexists(aside):
        os.replace(aside, LAUNCHER)
        say("~", f"{LAUNCHER} is the herdr athena's again")
    if record().get("plugin_was_on"):
        data = _read(CLAUDE_SETTINGS)
        _write_json(CLAUDE_SETTINGS, {**data, "enabledPlugins": {**(data.get("enabledPlugins") or {}), OLD_PLUGIN: True}}, backup=True)
        say("+", f"the {OLD_PLUGIN} plugin is on again")
    remember(rolled_back=True, launcher_aside=None, plugin_was_on=False)
    failed = []
    for label in OLD_JOBS:
        kept, target = os.path.join(OFF, f"{label}.plist"), os.path.join(AGENTS, f"{label}.plist")
        if os.path.exists(kept):
            os.replace(kept, target)
        if os.path.exists(target) and _loaded(label) is not True:
            _launchctl("bootstrap", _domain(), target)
            if _loaded(label) is True:
                say("+", f"{label} back")
            else:
                failed.append(label)
    if os.path.isdir(OFF) and not os.listdir(OFF):
        os.rmdir(OFF)
    if failed:
        print(f"the state is back, but these jobs didn't load: {', '.join(failed)}. Run this again, or: launchctl bootstrap {_domain()} ~/Library/LaunchAgents/<job>.plist", file=sys.stderr)
        return 1
    print("rolled back. Start the old HQ with: athena hq")
    return 0


def cmd_restore(args):
    """The last resort, after a rollback. Into a temporary name, then one rename, so a path that comes back meanwhile can't get the snapshot put
    inside it, and is never overwritten."""
    snapshot = record().get("snapshot")
    if not snapshot or not os.path.isdir(snapshot):
        stop("no snapshot on record")
    if os.path.islink(OLD) or os.path.exists(NEW):
        stop("the state is still moved: run the rollback first")
    copy = f"{OLD}.restoring-{os.getpid()}"
    done = subprocess.run(["cp", "-cRp", snapshot, copy], capture_output=True, text=True)
    if done.returncode:
        shutil.rmtree(copy, ignore_errors=True)
        subprocess.run(["cp", "-Rp", snapshot, copy], check=True)
    aside = None
    if os.path.lexists(OLD):
        aside = f"{OLD}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(OLD, aside)
    try:
        os.rename(copy, OLD)
    except OSError:
        stop(f"{OLD} came back in the meantime, so nothing was put over it. The copy is at {copy}{f', the damaged state at {aside}' if aside else ''}")
    say("~", f"{OLD} is the snapshot again" + (f"; the damaged state is at {aside}" if aside else ""))
    return 0


STEPS = (("check", cmd_check), ("snapshot", cmd_snapshot), ("stop-old", cmd_stop_old), ("old-hq", cmd_old_hq), ("move", cmd_move),
         ("launcher", cmd_launcher),
         ("install", cmd_install), ("plugin", cmd_plugin), ("verify", cmd_verify))


def cmd_run(args):
    """Every step in order. The check's crosses stop it before anything changes; each later step stops it where it is, with what to do."""
    for name, step in STEPS:
        print(f"{name}{' (a dry run: nothing changes)' if DRY else ''}", flush=True)
        if name == "verify" and DRY:
            say("·", "would: verify the moved state, the new jobs, doctor, the watch and the dash")
            continue
        code = step(args)
        if code and not (DRY and name == "check"):
            print(f"stopped at {name}: clear what it says, then run this again. Every step can be run twice.")
            return code
    print("dry run: nothing was changed" if DRY else "upgraded. Open HQ with: athena hq")
    return 0


def main():
    global DRY
    if lab.active() and not lab.scratch_place(HOME):
        print(f"upgrade: refusing {HOME}: ATHENA_WORKER={os.environ['ATHENA_WORKER']} may only rehearse in a marked scratch HOME", file=sys.stderr)
        return 2
    if lab.active() and not world.stubbed("launchctl"):
        print("upgrade: refusing: in the lab launchd is a marked stub or nothing (ATHENA_WORLD_STUBS), and none resolves", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog="upgrade-from-herdr.py", description="From the herdr athena to athena: docs/upgrade-from-herdr.md")
    sub = p.add_subparsers(dest="step", required=True)
    run = sub.add_parser("run", help="every step in order")
    run.add_argument("--dry-run", action="store_true", help="say what each step would do, and change nothing")
    sub.add_parser("check")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--again", action="store_true")
    for name in ("stop-old", "old-hq", "move", "launcher", "install", "plugin", "compare", "restore-snapshot"):
        sub.add_parser(name)
    verify = sub.add_parser("verify")
    verify.add_argument("--no-services", dest="services", action="store_false", help="don't look at the launchd jobs")
    back = sub.add_parser("rollback")
    back.add_argument("--leave-workers", action="store_true")
    back.add_argument("--hq-gone", action="store_true", help="you have looked: HQ's Claude isn't running, though `hq status` can't say")
    args = p.parse_args()
    DRY = getattr(args, "dry_run", False)
    if args.step == "run":
        args.services, args.again = True, False
    return {"run": cmd_run, "compare": cmd_compare, "rollback": cmd_rollback, "restore-snapshot": cmd_restore, **dict(STEPS)}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
