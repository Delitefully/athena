"""A Claude in a place: starting it, what state it is in, typing to it safely, stopping it. Backend-neutral.

Each Claude starts from a launcher that records its pid before exec, so the pid is ours, whichever backend ran it.
Nothing is typed into a Claude that is blocked, not ready, or holds text someone has not sent.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

from athena_rex.kernel import config, store, sandbox
from athena_rex.kernel.backend import BackendError, Place

READY = {"idle", "done"}
STARTUP = re.compile(r"Quick safety check|trust this folder|Do you trust|Enter to confirm|Press Enter to continue")
_CSI = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])")
_ANY_ESC = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_OPTION = re.compile(r"\d+\. ")


class Refused(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def place_of(record) -> Place:
    return Place(record.get("space"), record.get("block"), record.get("window"))


def _ps_program() -> str:
    return os.environ.get("ATHENA_PS") or "ps"


def claude_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def _run_dir() -> Path:
    return store.ensure("run")


def pid_file(name) -> Path:
    return store.path("run", f"{name}.pid")


def launcher(name, cwd, argv, env=None) -> Path:
    """The whole command in a file: nothing is typed, and no argument length limit applies."""
    claude = shlex.split(str(config.rex("claude")))
    words = sandbox.wrap(["env"] + [f"{k}={v}" for k, v in sorted((env or {}).items())] + claude + [str(a) for a in argv])
    path = _run_dir() / f"{name}.sh"
    path.write_text(f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file(name)))}\ncd {shlex.quote(str(cwd))} || exit 111\n"
                    "exec " + " ".join(shlex.quote(w) for w in words) + "\n")
    path.chmod(0o700)
    return path


def start(backend, name, label, cwd, argv, env=None, repo=None) -> Place:
    """Given `repo`, in a window named for it in the repo's session, made with its first worker: Rex's sidebar groups workers by
    repo. Without, in a session of its own."""
    pid_file(name).unlink(missing_ok=True)
    run = ["/bin/sh", str(launcher(name, cwd, argv, env))]
    if not repo:
        return backend.create(label, cwd, run, block_label=name)
    with store.locked("locks/repo-sessions.lock"):
        key = os.path.realpath(str(repo))
        known = store.read_dict(_repo_sessions())
        space = known.get(key)
        if space and space in {s["id"] for s in backend.spaces()}:
            try:
                return backend.new_window(space, cwd, run, window_label=name, block_label=name)
            except BackendError as exc:
                if exc.code != "not_found":
                    raise
        place = backend.create(Path(key).name, cwd, run, block_label=name, window_label=name)
        store.write_json(_repo_sessions(), {**known, key: place.space})
        return place


def _repo_sessions() -> Path:
    return store.path("run", "repo-sessions.json")


def close(backend, record):
    """A worker's window, and its repo's session once no window is left in it. One from before repo sessions: its own session. A
    record with no window in a repo's session closes only its own block: closing the session would take other workers' windows."""
    with store.locked("locks/repo-sessions.lock"):
        shared = record["space"] in store.read_dict(_repo_sessions()).values()
        if not record.get("window") and not shared:
            backend.close_space(record["space"])
            return
        try:
            if record.get("window"):
                backend.close_window(place_of(record))
            elif record.get("block"):
                backend.close_block(place_of(record))
        except BackendError as exc:
            if exc.code != "not_found":
                raise
        try:
            left = backend.windows(record["space"])
        except BackendError as exc:
            if exc.code == "not_found":
                return
            raise
        if not left:
            backend.close_space(record["space"])
            forget_repo_session(record["space"])


def forget_repo_session(space):
    """Under locks/repo-sessions.lock: a closed repo session is made again by the next spawn."""
    known = store.read_dict(_repo_sessions())
    store.write_json(_repo_sessions(), {k: v for k, v in known.items() if v != space})


def restart(backend, record, name, cwd, argv, env=None, label=None) -> Place:
    """In the same space when it still exists, else a new one. Its window and block carry its name."""
    pid_file(name).unlink(missing_ok=True)
    run = ["/bin/sh", str(launcher(name, cwd, argv, env))]
    known = {s["id"] for s in backend.spaces()}
    if record.get("space") in known:
        return backend.replace(place_of(record), cwd, run, block_label=name, window_label=name)
    return backend.create(label or name, cwd, run, block_label=name, window_label=name)


def stat_of(name):
    """The process state of `name`'s own Claude, or None if none runs: its recorded pid, still a Claude, still started
    under that name. A crashed worker's pid can be reused, by another Claude too. Raises Unknown when ps can't answer."""
    return running([name]).get(name)


def running(names) -> dict:
    """{name: stat} for those of `names` whose Claude runs, in one ps."""
    pids = {name: read_pid(name) for name in names}
    found = _ps([p for p in pids.values() if p])
    out = {}
    for name, pid in pids.items():
        row = found.get(pid)
        words = row[1].split() if row else []
        if any(w == "--name" and following == name for w, following in zip(words, words[1:])):
            out[name] = row[0]
    return out


def read_pid(name):
    try:
        return int(pid_file(name).read_text().strip())
    except (OSError, ValueError):
        return None


def _is_claude(command: str) -> bool:
    head = (command or "").split(" ")[0]
    return "claude" in os.path.basename(head) or "/claude/versions/" in head


class Unknown(BackendError):
    """Nobody can say. Never read as gone: anything destructive needs a confirmed absent."""

    def __init__(self, message=""):
        super().__init__("unknown", message)


def _ps(pids) -> dict:
    """{pid: (stat, command)} for those that are a Claude. A `ps` that cannot answer raises Unknown; it never reads as no process."""
    pids = [int(p) for p in pids if p]
    if not pids:
        return {}
    try:
        out = subprocess.run([_ps_program(), "-o", "pid=,stat=,command=", "-p",
                              ",".join(map(str, pids))], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unknown(f"ps did not answer: {type(exc).__name__}")
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and _is_claude(parts[2]):
            found[int(parts[0])] = (parts[1], parts[2])
    return found


def state(backend, record, name=None) -> dict:
    """{state, kind, msg, pid}: working, idle, done, blocked, starting, or None when no Claude runs there."""
    name = name or record.get("name")
    out = {"state": None, "kind": None, "msg": None, "pid": read_pid(name)}
    stat = stat_of(name)
    if not stat:
        return out
    if stat.startswith("T"):
        return {**out, "state": "blocked", "kind": "stopped", "msg": "Claude is stopped and not reading input"}
    place = place_of(record)
    reported = backend.program_status(place)
    if reported and reported.get("state"):
        return {**out, **reported}
    if STARTUP.search(backend.screen(place)):
        return {**out, "state": "blocked", "kind": "startup", "msg": "startup prompt"}
    return {**out, "state": "starting"}


def _faint_after(params: str, faint: bool) -> bool:
    """Whether text is faint or inverse after this SGR. A colour's own numbers are skipped, so a 2 inside one isn't faint."""
    args = iter((params or "0").split(";"))
    for arg in args:
        code = arg.partition(":")[0] or "0"
        if code in ("38", "48", "58"):
            if ":" not in arg:
                for _ in range(4 if next(args, "") == "2" else 1):
                    next(args, None)
        elif code in ("2", "7"):
            faint = True
        elif code in ("0", "22", "27"):
            faint = False
    return faint


def box_text(styled_screen: str):
    """What someone typed and hasn't sent, or None with no box on screen. Claude draws its placeholder and cursor faint or
    inverse, so only plain text counts. Rex draws spaces as cursor moves."""
    prompt = next((r for r in reversed(styled_screen.splitlines()) if _ANY_ESC.sub("", r).lstrip().startswith("❯")), None)
    if prompt is None:
        return None
    typed, faint, at = [], False, 0
    for token in _CSI.finditer(prompt):
        if not faint:
            typed.append(prompt[at:token.start()])
        at = token.end()
        if token.group(2) == "m":
            faint = _faint_after(token.group(1), faint)
        elif token.group(2) == "C" and not faint:
            typed.append(" " * int(token.group(1) or 1))
    if not faint:
        typed.append(prompt[at:])
    return _ANY_ESC.sub("", "".join(typed)).replace("❯", "").strip()


def typing(backend, record) -> bool:
    """Fails closed: an unreadable screen or no box counts as someone typing."""
    try:
        return box_text(backend.screen(place_of(record), styled=True)) != ""
    except BackendError:
        return True


def only_a_startup_prompt(backend, record, first_goal_waits=False) -> bool:
    """Whether `typing` said yes only because a startup prompt has no box: its option row reads as text, and no box fails closed.
    A startup prompt is what `state` says it is, so a Claude reporting a status has none, whatever words are on its screen. Past that,
    only a first goal whose Claude has shown nothing yet, as a launch that timed out leaves it. Text in a box is someone's."""
    try:
        now = state(backend, record)
        text = box_text(backend.screen(place_of(record), styled=True))
    except BackendError:
        return False
    if now["kind"] == "startup":
        return text is None or bool(_OPTION.match(text))
    return bool(first_goal_waits and text is None and now["state"] == "starting")


REFUSALS = {None: ("agent_not_found", "no Claude is running there"), "starting": ("agent_not_ready", "it is still starting")}


def _check_owner(backend, record):
    """Ids are only as good as the record holding them. A block labelled for someone else never gets this worker's text."""
    try:
        label = backend.block_label(place_of(record))
    except (BackendError, ValueError) as exc:
        raise Refused("unknown", str(exc))
    if label and record.get("name") and label != record["name"]:
        raise Refused("not_its_block", f"{record['name']}'s record points at the block of {label}")


def send(backend, record, text, submit=True, wait_box=0.0):
    """Type to a Claude, or raise Refused with nothing typed: it can't take input, someone's text is in its box, or nobody
    can say. A BackendError from here on means it may have been typed."""
    try:
        now = state(backend, record)
    except BackendError as exc:
        raise Refused("unknown", str(exc))
    if now["state"] == "blocked":
        raise Refused("agent_blocked", now.get("msg") or "it is blocked")
    if now["state"] in REFUSALS:
        raise Refused(*REFUSALS[now["state"]])
    _check_owner(backend, record)
    give_up = time.monotonic() + wait_box
    while typing(backend, record):
        if time.monotonic() > give_up:
            raise Refused("box_not_empty", "its input box holds unsent text")
        time.sleep(0.5)
    target = place_of(record)
    backend.type(target, text)
    if submit:
        time.sleep(0.2)
        backend.press(target, "enter")


def wait(backend, record, until, timeout=30.0, poll=0.4) -> dict:
    deadline = time.monotonic() + timeout
    now = state(backend, record)
    while now["state"] not in until:
        if time.monotonic() >= deadline:
            raise Refused("timeout", f"{record.get('name')} is {now['state']}, not {sorted(map(str, until))}")
        time.sleep(poll)
        now = state(backend, record)
    return now


def exit_screen(screen: str):
    """What the screen wants after /exit, read from the bottom up: 'exited', 'submit', 'confirm', or None when no key may
    be sent. Enter on the dialog takes its first option, stopping the tasks; moving to the background is never picked."""
    rows = [" ".join(line.split()) for line in (screen or "").splitlines()]
    for at, row in reversed(list(enumerate(rows))):
        if row.startswith("Resume this session with:"):
            return "exited"
        if row.startswith("❯"):
            if row.startswith("❯ /exit"):
                return "submit"
            dialog = any("Background work is running" in above for above in rows[:at])
            return "confirm" if dialog and row == "❯ 1. Exit and stop tasks" else None
    return None


class BackgroundWork(Exception):
    pass


def graceful_exit(backend, record, grace=8.0, stop_tasks=True) -> bool:
    """/exit, one Enter on a screen that asks for exactly that. True when the Claude is gone. Never over unsent text."""
    name = record.get("name")
    if not stat_of(name):
        return True
    if typing(backend, record):
        return False
    place = place_of(record)
    try:
        backend.type(place, "/exit")
        time.sleep(0.2)
        backend.press(place, "enter")
    except BackendError:
        return False
    deadline, presses = time.monotonic() + grace, 0
    while stat_of(name):
        if time.monotonic() >= deadline:
            return False
        seen = None
        try:
            seen = exit_screen(backend.screen(place)) if presses < 3 else None
        except BackendError:
            pass
        if seen == "confirm" and not stop_tasks:
            backend.press(place, "esc")
            raise BackgroundWork(f"{record.get('name')} has background work running; its exit was cancelled")
        if seen in ("confirm", "submit"):
            backend.press(place, "enter")
            presses += 1
            time.sleep(0.5)
        else:
            time.sleep(0.2)
    return True


def kill_if_alive(name, grace=3.0) -> bool:
    """SIGKILL `name`'s own Claude if it outlived its block, as Rex sends only SIGHUP. Never a pid that isn't that Claude's."""
    limit = time.monotonic() + grace
    while stat_of(name):
        if time.monotonic() >= limit:
            pid = read_pid(name)
            if not pid or pid <= 1:
                return False
            try:
                sandbox.kill(pid, signal.SIGCONT)
                sandbox.kill(pid, signal.SIGKILL)
                return True
            except OSError:
                return False
        time.sleep(0.2)
    return False


def _ps_lines(*args):
    try:
        return subprocess.run([_ps_program(), *args], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def family(name) -> dict:
    """{pid: command} of what else runs on this worker's terminal, nohup included, while its Claude is there to name that
    terminal. Only this user's. Something that made its own session has left the terminal, and that is the limit of this."""
    pid = read_pid(name)
    if not pid or pid <= 1:
        return {}
    tty = next(iter(_ps_lines("-o", "tty=", "-p", str(pid))), "").strip()
    if not tty or tty.startswith("?"):
        return {}
    found = {}
    for line in _ps_lines("-t", tty, "-o", "pid=,uid=,command="):
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1] == str(os.getuid()) and int(parts[0]) not in (pid, os.getpid()) and int(parts[0]) > 1:
            found[int(parts[0])] = parts[2]
    return found


def _started_by(pid, when) -> bool:
    """Whether this pid's process was already running at `when`. A start nobody can read isn't vouched for."""
    try:
        started = time.strptime(" ".join(_ps_lines("-o", "lstart=", "-p", str(pid))[0].split()), "%a %b %d %H:%M:%S %Y")
    except (IndexError, ValueError):
        return False
    return time.mktime(started) <= when + 1


def stop_leftovers(found, grace=3.0, listed_at=None) -> list:
    """What outlived its worker: only a pid still running the command it had when it was listed, TERM and then KILL. With `listed_at`,
    also only one started by then: a pid reused since, by the same command, isn't what was listed."""
    def same(pid, command):
        return any(line.strip() == command for line in _ps_lines("-o", "command=", "-p", str(pid))) and (listed_at is None or _started_by(pid, listed_at))
    left = {pid: command for pid, command in found.items() if pid > 1 and same(pid, command)}
    for pid in left:
        try:
            sandbox.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(same(pid, command) for pid, command in left.items()):
        time.sleep(0.2)
    for pid, command in left.items():
        if same(pid, command):
            try:
                sandbox.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return sorted(left.values())


def transcript_of(path, session=None, name=None):
    """This conversation's transcript, or None. With no session on record, the one its running Claude has open. Never the newest in
    the folder: any other Claude started in a worktree writes there too."""
    session = session or (session_id(read_pid(name)) if name else None)
    named = claude_dir() / "projects" / "".join(c if c.isalnum() else "-" for c in str(path)) / f"{session}.jsonl"
    return named if session and named.is_file() else None


def largest(names):
    """(worker, megabytes) of the running Claude using the most memory, or None."""
    pids = {read_pid(name): name for name in names}
    pids.pop(None, None)
    if not pids:
        return None
    try:
        out = subprocess.run([_ps_program(), "-o", "pid=,rss=", "-p", ",".join(map(str, pids))], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [line.split() for line in out.splitlines()]
    sizes = [(int(r[1]), pids[int(r[0])]) for r in rows if len(r) == 2 and r[0].isdigit() and r[1].isdigit() and int(r[0]) in pids]
    if not sizes:
        return None
    kb, name = max(sizes)
    return name, kb // 1024


def session_id(pid):
    """The conversation a running Claude has open, from Claude's own registry."""
    data = store.read_json(claude_dir() / "sessions" / f"{pid}.json", {})
    return data.get("sessionId") if isinstance(data, dict) else None


def registry_entry(name=None, session=None):
    try:
        files = list((claude_dir() / "sessions").glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            entry = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and (entry.get("name") == name or (session and entry.get("sessionId") == session)):
            return entry
    return None
