"""The one door to everything outside: other programs that touch accounts, power or the desktop, a module's card on a mounted volume, Slack.

Closed by default. It opens only when config.json in the state dir itself says `rex.real_world: true`, an explicit act of
the installer, and never in the lab. ATHENA_CONFIG can't point it elsewhere, and no other variable opens
it, so a test or a scratch copy cannot reach the real world by deleting or setting one. A stub folder, marked as one and
outside the system's own folders, may stand in for a tool and record the call; a fake card folder may stand in for the
card. Neither can name anything real.
"""
import os
import subprocess
from pathlib import Path

from athena_rex.kernel import lab, store

TOOLS = ("security", "osascript", "pmset", "ioreg", "caffeinate", "launchctl", "gh")
STUB_MARK = ".athena-stubs"
SERVER_MARK = ".athena-server-owner"
REAL_PLACES = ("/usr", "/bin", "/sbin", "/System", "/Library", "/Applications", "/opt", "/Volumes")


class Off(lab.Refused):
    pass


def enabled() -> bool:
    if lab.active():
        return False
    data = store.read_json(store.state_dir() / "config.json", {})
    opened = isinstance(data, dict) and isinstance(data.get("rex"), dict) and data["rex"].get("real_world") is True
    if opened and os.environ.get("ATHENA_CANARY"):
        from athena_rex.kernel import canary
        canary.door_opened()
    return opened


def _is_real_place(path) -> bool:
    return lab.inside(path, REAL_PLACES)


def _stub(tool):
    root = os.environ.get("ATHENA_WORLD_STUBS")
    if not root or _is_real_place(root) or not os.path.isfile(os.path.join(root, STUB_MARK)):
        return None
    path = os.path.join(root, tool)
    return path if os.path.isfile(path) and not _is_real_place(path) else None


def stubbed(tool: str) -> bool:
    return bool(_stub(tool))


def available(tool: str) -> bool:
    return bool(_stub(tool)) or enabled()


def resolve(tool: str) -> str:
    stub = _stub(tool)
    if stub:
        return stub
    if enabled():
        return tool
    raise Off(f"{tool} is off here: rex.real_world is not set in this state dir's config")


def require(what: str):
    if not enabled():
        raise Off(f"{what} is off here: rex.real_world is not set in this state dir's config")


def run(tool: str, *args, timeout=30):
    return subprocess.run([resolve(tool), *args], capture_output=True, text=True, timeout=timeout)


def start(tool: str, *args):
    return subprocess.Popen([resolve(tool), *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def may_write_card(path) -> bool:
    """A fake card folder can't hold a link to a real card, or a program file that points out of it."""
    return enabled() or not _is_real_place(path)


def private_server(server) -> bool:
    """A Rex endpoint this run made: under the per-user scratch root, with a mark beside its socket naming this state dir. So with
    the door shut neither the human's server nor another worker's can be named."""
    text = str(server or "")
    socket = text[len("unix://"):]
    if not text.startswith("unix://") or not lab.inside(socket, [f"/private/tmp/claude-{os.getuid()}"]):
        return False
    try:
        owner = (Path(socket).parent / SERVER_MARK).read_text().strip()
    except OSError:
        return False
    state = store.state_dir()
    return bool(owner) and lab.inside(owner, [state]) and lab.inside(state, [owner])


def card_root():
    """Where to look for a module's card: a fake folder if named, the real mounts only with the door open."""
    fake = os.environ.get("ATHENA_CARD_DIR")
    if fake and not _is_real_place(fake):
        return Path(fake)
    return Path("/Volumes") if enabled() else None
