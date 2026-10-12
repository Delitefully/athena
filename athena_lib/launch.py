"""`athena` from a plain terminal: start a herdr server if none runs, open HQ in it, then attach this terminal.

herdr's docs (https://herdr.dev/docs/cli-reference/, https://herdr.dev/docs/socket-api/,
https://herdr.dev/docs/persistence-remote/): `herdr server` runs the server headless, `herdr status server --json`
says whether one runs on the session's socket, and a plain `herdr` attaches to it (it would also start one, but
HQ's space has to exist before the client attaches). The client opens on the space the server has focused.
"""
import os
import subprocess
import sys
import time

from athena_lib import herdr, paths


def inside_herdr() -> bool:
    return bool(os.environ.get("HERDR_ENV"))  # herdr sets HERDR_ENV=1 in every pane


def can_attach() -> bool:
    """Only from a terminal outside herdr: never from a key binding, a script or a pane."""
    return not inside_herdr() and sys.stdin.isatty() and sys.stdout.isatty()


def server_running() -> bool:
    try:
        return bool(herdr.call("status", "server", "--json", timeout=15).get("running"))
    except herdr.HerdrError:
        return False


def start_server(wait=15.0):
    log = paths.ensure("logs") / "herdr-server.log"
    with open(log, "a") as out:
        subprocess.Popen(herdr.argv("server"), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, cwd=os.path.expanduser("~"))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if server_running():
            return
        time.sleep(0.2)
    raise herdr.HerdrError("server_not_started", f"herdr server did not start in {wait:.0f}s; see {log}")


def attach():
    """Replace this process with a herdr client on the same server (and session) athena talks to."""
    argv = herdr.argv()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], argv)
