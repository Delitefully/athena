"""The dashboard: one page on 127.0.0.1 that reads state files and writes only what the human does on it. Standard library only."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, lab, store, world, sandbox

SUMMARY = "the dashboard: needs you, ready, in flight, done today"
LIVE_PORT = 2843


def port() -> int:
    return int(config.rex("dash_port"))


def url() -> str:
    return f"http://127.0.0.1:{port()}/"


def _pid_file():
    return store.path("run", "dash.json")


def healthz():
    """What answers on our port, or None. In the lab the live dashboard's port is never even asked."""
    if lab.active() and port() == LIVE_PORT:
        return None
    try:
        with urllib.request.urlopen(url() + "healthz", timeout=2) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, ValueError, OSError):
        return None


def ours():
    data = healthz()
    return data if data and data.get("state_dir") == str(store.state_dir()) else None


def on(wait=5.0) -> dict:
    if lab.active() and port() == LIVE_PORT:
        raise lab.Refused(f"port {LIVE_PORT} is the live dashboard; use a scratch copy's port")
    running = ours()
    if running:
        return running
    if healthz():
        raise UserError(f"port {port()} answers for another state dir; set ATHENA_DASH_PORT")
    binary = store.ROOT / "bin" / "athena-rex"
    log = store.ensure("logs") / "dash.log"
    with open(log, "a") as out:
        proc = subprocess.Popen([str(binary), "dash", "run"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True)
    store.write_json(_pid_file(), {"pid": proc.pid, "port": port()})  # before it answers: one that never does is still found to stop
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        running = ours()
        if running:
            store.write_json(_pid_file(), {"pid": running["pid"], "port": port()})
            return running
        if proc.poll() is not None:
            _pid_file().unlink(missing_ok=True)
            raise UserError(f"the dashboard exited at once; see {log}")
        time.sleep(0.1)
    raise UserError(f"the dashboard did not answer in {wait:.0f}s; see {log}")


def off() -> bool:
    """Only a server that says it serves this state dir, at the pid it recorded, gets the signal."""
    running = ours()
    if not running:
        return False
    pid = int(running["pid"])
    if pid > 1:
        try:
            sandbox.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise UserError(f"the dashboard at pid {pid} could not be stopped: {exc}")
    _pid_file().unlink(missing_ok=True)
    return True


def _configure(p):
    p.add_argument("action", nargs="?", choices=["on", "off", "show", "open", "run"], default="show")


def _run(args):
    if args.action == "run":
        from athena_rex.caps.dash import server
        return server.serve(port())
    if args.action == "on":
        running = on()
        print(f"{url()} pid {running['pid']}")
    elif args.action == "off":
        print("stopped" if off() else "not running")
    elif args.action == "open":
        on()
        world.require("opening a browser")
        webbrowser.open(url())
    else:
        running = ours()
        print(f"{url()} pid {running['pid']}" if running else "dash off")
    return 0


def checks(ctx):
    held = ctx.cap("services")
    if held and held.is_stopped():  # a repair now would undo Quit athena
        return [caps.Check("dashboard", True, "stopped by Quit athena: Start athena brings it back")]
    if ours():
        return [caps.Check("dashboard", True, url())]
    return [caps.Check("dashboard", False, f"nothing answers on {url()}", lambda: on())]


def services(ctx):
    return [caps.Service("dash", ["dash", "run"])]


def commands():
    return {"dash": caps.Command(SUMMARY, _configure, _run)}
