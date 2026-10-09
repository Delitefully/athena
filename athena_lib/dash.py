"""The dashboard server's lifecycle: build it, start it beside HQ, show it, stop it. See docs/dashboard.md.

The server is the Go program in `dash/`, built on first use into the state dir under a name carrying the hash of
its sources, so an edit rebuilds and an unchanged tree starts at once. It binds 127.0.0.1 only and reads files only.
"""
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from athena_lib import paths

SRC = paths.PLUGIN_ROOT / "dash"
DEFAULT_PORT = 2843
GO_FALLBACKS = ("/opt/homebrew/bin/go", "/usr/local/go/bin/go", "/usr/local/bin/go")


class DashError(Exception):
    pass


def port() -> int:
    return int(os.environ.get("ATHENA_DASH_PORT") or DEFAULT_PORT)


def url(p=None) -> str:
    return f"http://127.0.0.1:{p or port()}/"


def go_bin():
    """herdr's key bindings may run athena with a short PATH, so look where Go usually lives too."""
    found = shutil.which("go")
    if found:
        return found
    return next((g for g in GO_FALLBACKS if os.access(g, os.X_OK)), None)


def sources():
    return sorted(p for p in SRC.rglob("*") if p.is_file() and not p.name.endswith("_test.go")
                  and "testdata" not in p.relative_to(SRC).parts)


def source_hash() -> str:
    h = hashlib.sha256()
    for p in sources():
        h.update(p.relative_to(SRC).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def binary() -> Path:
    bin_dir = paths.ensure("bin")
    target = bin_dir / f"athena-dash-{source_hash()}"
    if target.exists():
        return target
    go = go_bin()
    if not go:
        raise DashError("the dashboard needs Go to build once: install it (brew install go)")
    tmp = bin_dir / f".{target.name}.{secrets.token_hex(6)}"  # two racing builds never share a temp file
    proc = subprocess.run([go, "build", "-trimpath", "-o", str(tmp), "."], cwd=str(SRC), capture_output=True, text=True,
                          env=dict(os.environ, CGO_ENABLED="0"), timeout=300)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise DashError(f"go build failed: {proc.stderr.strip()[-600:]}")
    os.replace(tmp, target)
    for old in bin_dir.glob("athena-dash-*"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def find_hq(ps_output: str, hq_name: str):
    """The pid that means "HQ is open": the shell of the HQ Claude's pane, or that Claude itself when herdr started it
    directly. The HQ Claude is the `claude --name <hq_name>` process with hq.md; a worker's own Claude is not it."""
    procs = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    for pid, (ppid, command) in sorted(procs.items()):
        argv = command.split()
        if os.path.basename(argv[0]) != "claude" or "hq.md" not in command:
            continue
        if not any(a == "--name" and b == hq_name for a, b in zip(argv, argv[1:])):
            continue
        parent = procs.get(ppid, (0, ""))[1]
        if ppid > 1 and parent and os.path.basename(parent.split()[0]) != "herdr":
            return ppid
        return pid
    return None


def hq_pid():
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return find_hq(out, paths.hq_name())


def _pidfile():
    return paths.ensure() / "dash.pid"


def running():
    """The record in dash.pid, only if its process is alive and really is our server."""
    try:
        rec = json.loads(_pidfile().read_text())
        pid = int(rec["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return rec if _ours(pid) else None


def _health(p):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{p}/healthz", timeout=1) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def _failure(log, code, p):
    """Why the server exited. The log is shared, so look for this failure's own words, not just the last line."""
    try:
        lines = log.read_text().strip().splitlines()
    except OSError:
        lines = []
    for line in reversed(lines[-20:]):
        if f"port {p} is in use" in line or "cannot listen" in line:
            return line
    return f"the dashboard exited with {code}; see {log}"


def _ours(pid) -> bool:
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "athena-dash-" in cmd


def _adopt(p):
    """A server for this state dir already answers on the port (a concurrent `dash on` started it): use it."""
    health = _health(p)
    if not health or health.get("state") != str(paths.state_dir()) or not _ours(health.get("pid")):
        return None
    rec = {"pid": health["pid"], "port": p, "url": url(p), "hq_pid": health.get("hq_pid") or None, "bin": None}
    _pidfile().write_text(json.dumps(rec))
    return rec


def start(hq=None) -> dict:
    """Start the server unless it runs. `hq` is the pid it lives for; by default the HQ found by `hq_pid()`."""
    rec = running() or _adopt(port())
    if rec:
        return {**rec, "started": False}
    exe = binary()
    hq = hq if hq is not None else hq_pid()
    p = port()
    cmd = [str(exe), "-state-dir", str(paths.state_dir()), "-port", str(p)] + (["-hq-pid", str(hq)] if hq else [])
    log = paths.ensure("logs") / "dash.log"
    with open(log, "a") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        health = _health(p)
        if health and health.get("pid") == proc.pid:
            rec = {"pid": proc.pid, "port": p, "url": url(p), "hq_pid": hq, "bin": str(exe)}
            _pidfile().write_text(json.dumps(rec))
            return {**rec, "started": True}
        if proc.poll() is not None or health:
            # Lost a race with a concurrent `dash on`: its server holds the port, so use it.
            adopted = _adopt(p)
            if adopted:
                if proc.poll() is None:
                    proc.terminate()
                return {**adopted, "started": False}
            if proc.poll() is not None:
                raise DashError(_failure(log, proc.returncode, p))
        time.sleep(0.05)
    proc.terminate()
    raise DashError(f"the dashboard did not answer on {url(p)} within 5 s; see {log}")


def stop():
    rec = running()
    if rec:
        pid = int(rec["pid"])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.waitpid(pid, os.WNOHANG)  # reap it when this process started it
            except ChildProcessError:
                pass
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
    _pidfile().unlink(missing_ok=True)
    return rec
