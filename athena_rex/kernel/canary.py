"""The test run's tripwire, loaded only when ATHENA_CANARY names a log. An audit hook stops any reach for a real tool, a
guarded path or the network before it happens, and writes it down, in the test process and in `bin/athena-rex` children.
Other children are not seen here: scripts/test.sh's sandbox holds for those.
"""
import json
import os
import socket
import sys

ALLOWED_PROGRAMS = {"git", "ps", "du", "true", "sh", "python3", "sleep"}
PATH_EVENTS = {"open", "os.scandir", "os.listdir", "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.utime",
               "os.truncate", "os.symlink", "shutil.move", "shutil.rmtree", "shutil.copyfile"}
LISTINGS = {"os.scandir", "os.listdir"}
LIVE_DASH_PORT = 2843


class CanaryError(RuntimeError):
    pass


class _State:
    log = None
    guarded = ()
    listings = ()
    tools = ()
    stubs = None
    roots = ()


def _name(path) -> str:
    """What a path is, not how it is spelled: links followed, and case folded as the disk folds it."""
    return os.path.realpath(path).casefold()


def configure(log, guarded=(), listings=(), tools=(), stubs=None, roots=()):
    _State.log, _State.guarded, _State.listings = log, tuple(guarded), tuple(listings)
    _State.tools, _State.stubs, _State.roots = tuple(tools), stubs, tuple(roots)


def env_for_children() -> dict:
    return {"ATHENA_CANARY": _State.log or "", "ATHENA_CANARY_CONFIG": json.dumps({
        "guarded": _State.guarded, "listings": _State.listings, "tools": _State.tools, "stubs": _State.stubs,
        "roots": _State.roots})}


def _trip(what: str, log=None):
    log = log or _State.log
    if log:
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (what + "\n").encode())
        finally:
            os.close(fd)
    raise CanaryError(f"canary: {what}")


def door_opened():
    """Called by world.py the moment it finds the door open under test, so a door opened mid-test or in a child alone is seen,
    in any child that kept ATHENA_CANARY."""
    _trip("the door to the real world opened", os.environ.get("ATHENA_CANARY"))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _check_path(event: str, value):
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if not isinstance(value, (str, os.PathLike)):
        return
    path = _name(os.fspath(value))
    if _State.log and path == _name(_State.log):
        return
    for root in _State.guarded:
        if _under(path, _name(root)):
            _trip(f"{event} reached guarded path {path}")
    if event in LISTINGS and path in {_name(p) for p in _State.listings}:
        _trip(f"{event} listed guarded folder {path}")


def _check_program(program, command=()):
    program = os.fspath(program) if isinstance(program, os.PathLike) else program
    if isinstance(program, bytes):
        program = program.decode(errors="replace")
    if not isinstance(program, str):
        return
    name = os.path.basename(program)
    words = [str(w) for w in command] if isinstance(command, (list, tuple)) else []
    if name in _State.tools:
        if not (_State.stubs and _under(os.path.abspath(program), _State.stubs)):
            _trip(f"ran real tool {program}")
        return
    if name == "sh" and words[1:2] == ["-c"]:
        _trip(f"ran a shell command: {words[2:3]}")
    if name == "git" and words[1:2] == ["-C"] and not any(_under(os.path.abspath(words[2]), r) for r in _State.roots):
        _trip(f"ran git in {words[2]}")
    if name in ALLOWED_PROGRAMS or ("/" in program and any(_under(os.path.abspath(program), root) for root in _State.roots)):
        return
    _trip(f"ran unexpected program {program}")


def _check_connect(address):
    if isinstance(address, bytes):
        address = address.decode(errors="replace")
    if isinstance(address, str):
        if not any(_under(_name(address), _name(root)) for root in _State.roots):
            _trip(f"connected to Unix socket {address}")
    elif isinstance(address, tuple) and address:
        host, port = address[0], address[1] if len(address) > 1 else 0
        if host not in ("127.0.0.1", "::1", "localhost"):
            _trip(f"opened a network connection to {host}:{port}")
        if port == LIVE_DASH_PORT:
            _trip("called the live dashboard's port")


def _check_signal(pid, sig):
    """A test signals only what it started. Asking with signal 0 goes back through here and goes no further."""
    if pid <= 1:
        _trip(f"signalled pid {pid}")
    if sig:
        try:
            os.kill(pid, 0)
        except OSError as exc:
            _trip(f"signalled pid {pid}, which this test did not start: {type(exc).__name__}")


def _hook(event, args):
    if event in PATH_EVENTS and args:
        if event != "os.symlink":
            _check_path(event, args[0])
        if event in ("os.rename", "shutil.move", "os.symlink") and len(args) > 1:
            _check_path(event, args[1])
    elif event == "subprocess.Popen":
        command = args[1]
        first = command[0] if isinstance(command, (list, tuple)) and command else command
        _check_program(args[0] or first, command)
    elif event in ("os.exec", "os.posix_spawn", "os.spawn") and args:
        _check_program(args[0])
    elif event == "os.system":
        _trip(f"ran a shell command: {args[0]!r}")
    elif event == "socket.connect":
        _check_connect(args[1])
    elif event == "socket.getaddrinfo" and args and args[0] not in ("127.0.0.1", "localhost", "::1", None, b"127.0.0.1"):
        _trip(f"looked up {args[0]}")
    elif event in ("os.kill", "os.killpg") and args:
        _check_signal(int(args[0]), args[1] if len(args) > 1 else None)


_installed = False


def install():
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def from_environment():
    """In a child: the parent's rules, from the environment. Deleting them only removes this extra layer: world.py stays shut."""
    log = os.environ.get("ATHENA_CANARY")
    if log:
        rules = json.loads(os.environ.get("ATHENA_CANARY_CONFIG") or "{}")
        configure(log, rules.get("guarded", ()), rules.get("listings", ()), rules.get("tools", ()), rules.get("stubs"),
                  rules.get("roots", ()))
        install()


__all__ = ["CanaryError", "configure", "env_for_children", "from_environment", "install", "socket"]
