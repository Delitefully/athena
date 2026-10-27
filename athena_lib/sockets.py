"""Whether a live Claude session can still be messaged.

Claude Code registers each session in ~/.claude/sessions/<pid>.json, with its name, sessionId and the unix socket
other sessions' SendMessage reaches it on (messagingSocketPath). When that socket file is gone while the process
lives, every SendMessage to the session fails ("no agent named 'athena' is reachable") until it restarts. athena
only reads the registry: it never touches or recreates a socket.
"""
import json
import os
import stat
from pathlib import Path

from athena_lib import hookstatus, paths


def sessions_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "sessions"


def _pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _is_socket(path) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def entries() -> list:
    out = []
    try:
        files = sorted(sessions_dir().glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            entry = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def missing(name, session_id=None, found=None):
    """The socket path of a live session named `name` (or with id `session_id`) whose socket file is gone or is not
    a socket; None when it is there, the session's pid is dead, or the session has no registry entry."""
    for entry in entries() if found is None else found:
        if entry.get("name") != name and not (session_id and entry.get("sessionId") == session_id):
            continue
        path, pid = entry.get("messagingSocketPath"), entry.get("pid")
        if not path or not isinstance(pid, int) or not _pid_alive(pid):
            continue
        if not _is_socket(path):
            return path
    return None


def worker_missing(name, found=None):
    return missing(name, (hookstatus.read(name) or {}).get("session_id"), found)


def hq_missing(found=None):
    from athena_lib import hq  # hq imports board, which imports status
    return missing(paths.hq_name(), hq.load().get("session"), found)


def hq_status_line():
    """`athena status`'s line for HQ's socket, or None while it is there."""
    path = hq_missing()
    if not path:
        return None
    return f"{'hq':<12}  messaging socket missing ({path}): rotate with athena hq rotate"
