"""The signal sandbox: every Claude athena-rex starts runs under a sandbox-exec profile that denies signals to processes outside it.

A script a Claude runs, `os.kill(-1, ...)` included, can signal only what that Claude started; the rest of the Mac's processes,
other Claudes, HQ, the dashboard and Rex itself are out of its reach. Everything else is allowed: this is a guard against a stray
kill, not a confinement.

The way out, for the legitimate stops athena-rex makes from inside one (HQ retiring a worker, `dash off`, `watch --stop`): a signal
the kernel refuses is handed to the Rex server, which runs outside every sandbox, as `/bin/kill -<sig> <pid>` in a throwaway session.
Callers have already checked the pid is the process they mean. Doctor runs from launchd, outside, and needs no way out.

It is opt-in: `rex.signal_sandbox: true` in config, until scripts/sandbox-proof.sh has passed on the machine. Once on, where sandbox-exec
is missing or the profile doesn't load, no Claude starts: spawn, resume and HQ's start refuse, and doctor says why, until it is fixed or
turned off. A known limit: macOS refuses a sandbox inside one (sandbox_apply exits 71), so a sandboxed worker can't run anything that
starts its own sandbox-exec, this repo's scripts/test.sh among them. Launches aren't that: Rex's server starts them, outside.
"""
import errno
import os
import signal
import subprocess
import time

from athena_rex.kernel import config, lab

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILE = "(version 1)(allow default)(deny signal)(allow signal (target same-sandbox))"
MARK = "ATHENA_SIGNAL_SANDBOX"
# What tries the profile before a launch; tests point it at a stub, as ATHENA_PS for ps. The launch itself always runs SANDBOX_EXEC,
# which won't start Claude on a profile that doesn't load.
PROBE_ENV = "ATHENA_SIGNAL_SANDBOX_PROBE"


class Unavailable(lab.Refused):
    """The sandbox is on in config but can't start here, so the launch is refused."""


def enabled() -> bool:
    """On only when config says rex.signal_sandbox: true. A launch goes through the Rex server, which runs outside every sandbox, so it is
    never nested, even when HQ, inside one, asks for it."""
    return config.rex("signal_sandbox") is True


def problem():
    """Why a Claude can't start under the sandbox here, or None when it can or it is off. The profile is tried on /usr/bin/true."""
    if not enabled():
        return None
    off = "; set rex.signal_sandbox: false in config to start Claudes without it"
    if not os.path.exists(SANDBOX_EXEC):
        return f"{SANDBOX_EXEC} is missing, so the signal sandbox can't start{off}"
    probe = os.environ.get(PROBE_ENV) or SANDBOX_EXEC
    try:
        done = subprocess.run([probe, "-p", PROFILE, "/usr/bin/true"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the signal sandbox's profile did not load: {exc}{off}"
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout or f"exit {done.returncode}").split())[:200]
        return f"the signal sandbox's profile did not load: {said}{off}"
    return None


def require(error=Unavailable):
    """Refuse a launch, with `error`, when the sandbox is on and can't start."""
    why = problem()
    if why:
        raise error(f"not starting Claude: {why}")


def wrap(words) -> list:
    """`words` run under the profile, marked so whatever runs inside knows to take the way out. Refuses when it can't start."""
    if not enabled():
        return list(words)
    require()
    return [SANDBOX_EXEC, "-p", PROFILE, "env", f"{MARK}=1", *words]


def inside() -> bool:
    return os.environ.get(MARK) == "1"


def kill(pid, sig) -> bool:
    """Send `sig` to `pid`, a process the caller has already identified; through Rex when the sandbox refuses it. Never pid 1 or less:
    a group or every process is never this function's to signal."""
    pid = int(pid)
    if pid <= 1:
        raise ValueError(f"refusing to signal pid {pid}")
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        if not inside() or exc.errno != errno.EPERM:
            raise
    return _through_rex(pid, sig)


def _through_rex(pid, sig) -> bool:
    from athena_rex.kernel import caps
    backend = caps.CTX.backend
    number = int(sig)
    place = backend.create("athena-signal", "/", ["/bin/kill", f"-{number}", str(pid)], block_label="athena-signal")
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            if number in (signal.SIGCONT, signal.SIGWINCH):
                break
            time.sleep(0.2)
        return True
    finally:
        try:
            backend.close_space(place.space)
        except Exception:
            pass
