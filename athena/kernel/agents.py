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

from athena.kernel import config, store, sandbox
from athena.kernel.backend import BackendError, Place

READY = {"idle", "done"}
AGENTS = ("claude", "pi")  # what a worker can run; Claude is the default everywhere, and HQ is always Claude
STARTUP = re.compile(r"Quick safety check|trust this folder|Do you trust|Enter to confirm|Press Enter to continue")
_CSI = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])")
_ANY_ESC = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_OPTION = re.compile(r"\d+\. ")
STATUS_GRACE = float(os.environ.get("ATHENA_STATUS_GRACE") or 180)  # running this long with no program status: Rex never will hear one
NO_STATUS = "no program status from Rex"
UNREPORTED = "unreported"  # the kind of a state read from the screen, as Rex has no record for that Claude
_BUSY = re.compile(r"^\s*[·✢✳✶✻✽*]\s*\S[^\n]*…|esc\s*to\s*interrupt", re.M)  # Claude's spinner row, or its footer's hint; Rex drops spaces
BUSY_ROWS = 20  # the rows above the box that can hold the spinner, a todo list between them; busy words further up are scrollback


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


def agent_of(record) -> str:
    """The program a worker runs: `claude` unless its record says otherwise."""
    return (record or {}).get("agent") or "claude"


def launcher(name, cwd, argv, env=None, agent="claude") -> Path:
    """The whole command in a file: nothing is typed, and no argument length limit applies."""
    claude = shlex.split(str(config.rex(agent)))
    words = sandbox.wrap(["env"] + [f"{k}={v}" for k, v in sorted((env or {}).items())] + claude + [str(a) for a in argv])
    path = _run_dir() / f"{name}.sh"
    text = (f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file(name)))}\ncd {shlex.quote(str(cwd))} || exit 111\n"
            "exec " + " ".join(shlex.quote(w) for w in words) + "\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)  # never readable by others, not even for an instant: it holds env
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o700)  # one made before keeps its old mode through O_CREAT
    return path


def start(backend, name, label, cwd, argv, env=None, repo=None, agent="claude") -> Place:
    """Given `repo`, in a window named for it in the repo's session, made with its first worker: Rex's sidebar groups workers by
    repo. Without, in a session of its own."""
    pid_file(name).unlink(missing_ok=True)
    forget_pi(name)  # a new worker of an old name never reads the old one's mark or messages, a quit retire left unread above all
    run = ["/bin/sh", str(launcher(name, cwd, argv, env, agent))]
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
        place = backend.create(repo_session_label(key), cwd, run, block_label=name, window_label=name)
        store.write_json(_repo_sessions(), {**known, key: place.space})
        return place


def _repo_sessions() -> Path:
    return store.path("run", "repo-sessions.json")


def repo_session_label(key) -> str:
    """The label a repo's session is made with, and kept at: its folder's name."""
    return Path(key).name[:128]


def repo_session_labels() -> dict:
    """{space: the label athena made it with}, for each repo session on record."""
    return {space: repo_session_label(key) for key, space in store.read_dict(_repo_sessions()).items() if space}


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


def restart(backend, record, name, cwd, argv, env=None, label=None, agent="claude") -> Place:
    """In a new window of the same space when it still exists, else in a new space. Its window and block carry its name.
    The old block stays open: the caller records the new place, then closes it with close_replaced."""
    pid_file(name).unlink(missing_ok=True)
    run = ["/bin/sh", str(launcher(name, cwd, argv, env, agent))]
    known = {s["id"] for s in backend.spaces()}
    if record.get("space") in known:
        return backend.new_window(record["space"], cwd, run, window_label=name, block_label=name, focus=True)
    return backend.create(label or name, cwd, run, block_label=name, window_label=name)


def close_replaced(backend, record, place):
    """The block `record` names, once `place` has replaced it in the same space and is on record. It may be the very block
    running this process: closing it can end it, so nothing may come after this that has to happen."""
    if record.get("space") != place.space or not record.get("block") or record["block"] == place.block:
        return
    try:
        backend.close_block(place_of(record))
    except BackendError:
        pass


def stat_of(name):
    """The process state of `name`'s own Claude, or None if none runs: its recorded pid, still a Claude, still started
    under that name. A crashed worker's pid can be reused, by another Claude too. Raises Unknown when ps can't answer."""
    return running([name]).get(name)


def running(names) -> dict:
    """{name: stat} for those of `names` whose Claude (or Pi) runs, in one ps. Pi sets its process title to `pi`, which hides its
    `--name`, so a Pi is its worker's only when athena's extension inside it wrote that same pid to `run/<name>.pi.json`."""
    pids = {name: read_pid(name) for name in names}
    found = _ps([p for p in pids.values() if p])
    out = {}
    for name, pid in pids.items():
        row = found.get(pid)
        words = row[1].split() if row else []
        if any(w == "--name" and following == name for w, following in zip(words, words[1:])):
            out[name] = row[0]
        elif row and _is_pi(row[1]) and pi_pid(name) == pid:
            out[name] = row[0]
    return out


def pi_mark(name) -> Path:
    """Written by athena's Pi extension as Pi starts: {pid, session}. Pi's process title hides the flags it was started with."""
    return store.path("run", f"{name}.pi.json")


def pi_pid(name):
    try:
        return int(store.read_dict(pi_mark(name)).get("pid"))
    except (TypeError, ValueError):
        return None


def read_pid(name):
    try:
        return int(pid_file(name).read_text().strip())
    except (OSError, ValueError):
        return None


def _is_claude(command: str) -> bool:
    head = (command or "").split(" ")[0]
    return "claude" in os.path.basename(head) or "/claude/versions/" in head


def _is_pi(command: str) -> bool:
    """Pi once its process title is set (`pi`), or node running Pi's command before that."""
    words = (command or "").split()
    if not words:
        return False
    if os.path.basename(words[0]) == "pi":
        return True
    return os.path.basename(words[0]) == "node" and len(words) > 1 and (os.path.basename(words[1]) == "pi" or "pi-coding-agent" in words[1])


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
        done = subprocess.run([_ps_program(), "-o", "pid=,stat=,command=", "-p",
                               ",".join(map(str, pids))], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unknown(f"ps did not answer: {type(exc).__name__}")
    if done.returncode not in (0, 1) or done.returncode == 1 and done.stderr.strip():  # 1 and silent: none of them runs
        raise Unknown(f"ps failed ({done.returncode}): {done.stderr.strip()[:200]}")
    out = done.stdout
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and (_is_claude(parts[2]) or _is_pi(parts[2])):
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
    if reported and reported.get("state") == "error" and agent_of(record) == "pi":
        return {**out, **reported, "state": "idle"}  # Pi says error once a run failed, and takes the next prompt as at idle
    if reported and reported.get("state"):
        return {**out, **reported}
    if STARTUP.search(backend.screen(place)):
        return {**out, "state": "blocked", "kind": "startup", "msg": "startup prompt"}
    if unreported(record, name, reported):
        guessed = screen_state(backend.screen(place, styled=True))
        return {**out, "state": guessed, "kind": UNREPORTED, "msg": NO_STATUS + ("" if guessed == "starting" else "; read from its screen")}
    return {**out, "state": "starting"}


def running_for(name):
    """Seconds since `name`'s launcher wrote its pid, just before it exec'd the Claude; None when that can't be read."""
    try:
        return time.time() - pid_file(name).stat().st_mtime
    except OSError:
        return None


def unreported(record, name, reported) -> bool:
    """Whether a running Claude has told Rex nothing past its startup grace: its OSC 7501 probe got no reply (Rex restarting as it
    started), so it never will. A record, even one with no state, is Rex hearing it. A Pi has no screen to read."""
    return not reported and agent_of(record) == "claude" and (running_for(name) or 0) >= STATUS_GRACE


def screen_state(styled_screen: str) -> str:
    """What a Claude that reports nothing is doing, from its screen: working with its spinner or "esc to interrupt" by the box, idle
    with an empty box and neither, else starting. Working is looked for first, since a working Claude's box is empty too."""
    rows = styled_screen.splitlines()
    plain = [_ANY_ESC.sub("", row) for row in rows]
    box = next((at for at in range(len(plain) - 1, -1, -1) if plain[at].lstrip().startswith("❯")), None)
    if box is None:
        return "starting"
    if _BUSY.search("\n".join(plain[max(0, box - BUSY_ROWS):])):
        return "working"
    return "idle" if box_text(styled_screen) == "" else "starting"


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
    """Fails closed: an unreadable screen or no box counts as someone typing. A Pi is never typed into (its inbox is read by
    athena's extension), so what is in its editor is never in the way."""
    if agent_of(record) == "pi":
        return False
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
    if agent_of(record) == "pi":
        if not submit:
            raise Refused("not_typed", "a Pi worker's messages go through its inbox and are always sent: there is no box to leave text in")
        post(record["name"], {"kind": "message", "text": text})
        return
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


def inbox(name) -> Path:
    return store.path("run", "inbox", name)


def forget_pi(name):
    """A Pi worker's mark and the messages nobody read: once it is retired, or before a launch under its name."""
    pi_mark(name).unlink(missing_ok=True)
    folder = inbox(name)
    for left in (folder.glob("*") if folder.is_dir() else []):
        if left.is_file():
            left.unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


def post(name, message: dict) -> Path:
    """A message for a Pi worker, picked up by athena's extension inside it within a second: {"kind": "message", "text"} goes to Pi
    as the human's own words (queued as a follow-up while it works), {"kind": "quit"} ends it once it is idle. Each in a file of
    its own, named so they sort in the order sent."""
    target = store.ensure("run", "inbox", name) / f"{time.time_ns():020d}-{os.getpid()}.json"
    store.write_text(target, json.dumps(message) + "\n", mode=0o600)
    return target


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
    if agent_of(record) == "pi":
        return _pi_quit(name, grace)
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


def _pi_quit(name, grace) -> bool:
    """Pi has no /exit screen: its extension is asked to shut it down, which Pi does once it is idle."""
    post(name, {"kind": "quit"})
    deadline = time.monotonic() + grace
    while stat_of(name):
        if time.monotonic() >= deadline:
            return False
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
