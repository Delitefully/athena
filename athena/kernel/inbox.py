"""HQ's cross-session inbox: what athena says to HQ arrives there, shown as one dim `› Message from <sender>: … (ctrl+o to expand)`
line, and never in the human's input box. An idle HQ starts a turn with it; a busy one reads it between tool calls. Typing stays
the fallback, under the old rules (only while HQ is idle with an empty box), whenever the inbox can't be reached.

Claude Code binds a Unix socket per session (code.claude.com/docs/en/cross-session-messaging, "The session's inbox socket") and
exports its path to hooks as CLAUDE_CODE_MESSAGING_SOCKET. HQ's SessionStart hook (caps/hooks.py) writes it to hq-start.json with
the pid of HQ's Claude, so an inbox an earlier HQ recorded is never used: a rotation, revive or restart starts a new Claude, whose
own SessionStart writes its own. HQ's --settings set crossSessionInbound to accept (caps/hq.py), which is what lets a message with
no permission mode of its own through to a bypass-mode HQ instead of holding it for the human's approval.
"""
import json
import os
import re
import socket
import time
import uuid

from athena.kernel import agents, caps, store
from athena.kernel.backend import BackendError

TAG = "cross-session-message"
SEND_TIMEOUT = 5.0
LINGER = 0.15  # as Claude Code's own client on macOS: the connection ends about 150ms after the write
_CLOSER = re.compile(rf"<(?=\s*/?\s*{TAG})", re.I)


class Unreachable(Exception):
    pass


def encode(text, sender) -> bytes:
    """One line: a `user` frame whose content is the `<cross-session-message>` wrapper the receiver parses for the sender's name,
    as Claude Code 2.1.326's own SendMessage writes it. No `from` (nothing here has an inbox to answer to), no auth line (optional
    on macOS and Linux, and the token Claude Code exports is its own children's), no `from-mode` (athena is no session with a
    permission mode to claim). The wire format past the documented `{"type":"user","message":...}` stays here and nowhere else."""
    name = re.sub(r'["<>\r\n]', "", sender)
    body = _CLOSER.sub(r"<\\", text)
    content = f'<{TAG} from-name="{name}">\n{body}\n</{TAG}>'
    frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user", "message": {"role": "user", "content": content}, "priority": "next"}
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode()


def socket_of(name) -> str:
    """HQ's inbox, as its SessionStart hook recorded it, if that hook ran in the Claude now running under `name`."""
    start = store.read_dict(store.path("hq-start.json"))
    path = start.get("messaging_socket")
    if not path:
        raise Unreachable("HQ's SessionStart hook recorded no inbox (its Claude started before this athena, or has no messaging)")
    pid = agents.read_pid(name)
    if not pid or start.get("pid") != pid:
        raise Unreachable(f"the inbox on record is an earlier HQ's (pid {start.get('pid')}), not the running one's ({pid})")
    stem = os.path.basename(path).rsplit(".sock", 1)[0]
    if stem != str(pid) and not stem.startswith(f"{pid}-"):  # Claude Code names its socket after its own pid: <pid>.sock
        raise Unreachable(f"the inbox on record ({path}) isn't the socket of HQ's Claude (pid {pid})")
    try:  # a dead HQ's pid can be reused, and Claude Code names its socket after the pid: only HQ's own running Claude counts
        if not agents.stat_of(name):
            raise Unreachable(f"HQ's Claude (pid {pid}) isn't running")
    except agents.Unknown as exc:
        raise Unreachable(f"nobody can say HQ's Claude runs ({exc})")
    return path


def _connect(path) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(SEND_TIMEOUT)
    try:
        conn.connect(path)
    except OSError as exc:
        conn.close()
        raise Unreachable(f"HQ's inbox at {path} refused: {exc.strerror or exc}")
    return conn


def post(path, text, sender):
    """One message, one connection. Raises Unreachable when nothing took it."""
    line = encode(text, sender)
    conn = _connect(path)
    try:
        conn.sendall(line)
        time.sleep(LINGER)
    except OSError as exc:
        raise Unreachable(f"HQ's inbox at {path} refused: {exc.strerror or exc}")
    finally:
        conn.close()


def _settled_socket(name, settle) -> str:
    """socket_of, waited for up to `settle` seconds: a Claude just started may show itself ready a moment before its SessionStart
    hook has written its inbox down."""
    deadline = time.monotonic() + settle
    while True:
        try:
            return socket_of(name)
        except Unreachable:
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.5)


def deliver(backend, record, text, sender, wait_box=0.0, settle=0.0) -> str:
    """Say `text` to HQ: to its inbox when it answers, whatever HQ is doing; else typed, as before, only while it is idle and its
    box empty. Returns 'inbox' or 'typed'; raises agents.Refused (or BackendError) when neither took it."""
    try:
        post(_settled_socket(record["name"], settle), text, sender)
        return "inbox"
    except Unreachable:
        pass
    try:
        now = agents.state(backend, record)
    except BackendError as exc:  # nothing typed yet: as agents.send says it, so a caller waits rather than reads it as sent
        raise agents.Refused("unknown", str(exc))
    if now["state"] not in agents.READY:
        raise agents.Refused("not_idle", f"HQ is {now['state']}, and its inbox can't be reached")
    agents.send(backend, record, text, wait_box=wait_box)
    return "typed"


def check(name) -> caps.Check:
    """Doctor's line: where the next notice to HQ goes. Connecting and closing is Claude Code's own liveness probe. Typing still
    delivers, so falling back is said, not failed: an HQ started before this release types until its next start, and that is no
    alarm."""
    try:
        path = socket_of(name)
        _connect(path).close()
    except Unreachable as exc:
        return caps.Check("hq-delivery", True, f"falling back to typing: {exc}", short="typing")
    return caps.Check("hq-delivery", True, f"to HQ's inbox at {path}: notices arrive as collapsed lines, never typed", short="inbox")
