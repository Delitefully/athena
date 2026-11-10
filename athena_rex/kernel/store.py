"""The state dir and how anything in it is read and written: atomic JSON, appended lines, advisory locks."""
import contextlib
import errno
import fcntl
import json
import os
import re
import time
from _thread import get_ident
from pathlib import Path

from athena_rex.kernel import lab

ROOT = Path(os.path.abspath(__file__)).parent.parent.parent
DEFAULT_STATE = "~/Library/Application Support/athena"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


KNOWN_STATE = ("~/.local/state/athena", DEFAULT_STATE)
_TEMP = re.compile(r"\.tmp\d+(?:\.\d+)?$")
TEMP_FOLDERS = ("", "workers", "run", "settings", "briefs")


def state_dir() -> Path:
    path = Path(os.path.expanduser(os.environ.get("ATHENA_STATE_DIR") or DEFAULT_STATE))
    lab.check_state(path)
    return path


def state_elsewhere():
    """Where state lives if this dir is empty and another known one isn't: a wrong path must not look like a fresh install."""
    here = state_dir()
    if (here / "ledger.jsonl").exists() or (here / lab.SCRATCH_MARK).exists():
        return None
    for known in KNOWN_STATE:
        other = Path(os.path.expanduser(known))
        if (other / "ledger.jsonl").exists() and not lab.inside(other, [here]):
            return other
    return None


def path(*parts) -> Path:
    return state_dir().joinpath(*parts)


def ensure(*parts) -> Path:
    target = state_dir().joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_json(target, default=None):
    try:
        with open(target) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_dict(target) -> dict:
    """A record: anything unreadable, or that isn't an object, reads as none, as nobody wrote it."""
    data = read_json(target)
    return data if isinstance(data, dict) else {}


def write_text(target, text: str, mode=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}.{get_ident()}")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is None:
            with contextlib.suppress(OSError):
                mode = os.stat(target).st_mode & 0o777
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def write_json(target, data, indent=None, mode=None):
    write_text(target, json.dumps(data, indent=indent, sort_keys=True, default=str), mode)


def append_line(target, record: dict, sync=False):
    """A cut-off last line is closed first, so one torn write never swallows the next record."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode()
    fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)


def _parsed(raw):
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def read_lines(target):
    """Each line that is a JSON object. Bytes, since a torn write can cut a character in two; anything else is skipped, and a
    missing file is no lines."""
    try:
        source = open(target, "rb")
    except OSError:
        return
    with source:
        for raw in source:
            record = _parsed(raw)
            if record is not None:
                yield record


def unreadable_lines(target) -> int:
    try:
        with open(target, "rb") as source:
            return sum(1 for raw in source if raw.strip() and _parsed(raw) is None)
    except OSError:
        return 0


def sweep_temps(older_than=3600.0, now=None):
    """A killed writer's temp files, once nothing can still be writing them."""
    now = time.time() if now is None else now
    for folder in TEMP_FOLDERS:
        try:
            entries = list(os.scandir(state_dir() / folder))
        except OSError:
            continue
        for entry in entries:
            with contextlib.suppress(OSError):
                if _TEMP.search(entry.name) and entry.is_file() and now - entry.stat().st_mtime > older_than:
                    os.unlink(entry.path)


UNHELD = object()
NO_ROOM = (errno.ENOSPC, errno.EDQUOT)


@contextlib.contextmanager
def locked(name: str, blocking=True, full_disk_ok=False):
    """Yields False when `blocking` is off and someone holds it. A lock file that can't be made raises, as most locks guard a decision
    that isn't worth making twice. With `full_disk_ok` the caller goes on anyway and is handed UNHELD, which is truthy: the report gate
    and doctor's look at the machine matter more on a full disk than the lock does."""
    target = state_dir() / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        if not (full_disk_ok and exc.errno in NO_ROOM):
            raise
        yield UNHELD
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def trash_place(name: str) -> Path:
    """A place in <state>/trash/<date>/ that isn't taken, so two tries in one second can't land in each other."""
    folder = ensure("trash", time.strftime("%Y%m%d"))
    stem, tries = f"{name}-{int(time.time())}", 0
    target = folder / stem
    while target.exists():
        tries += 1
        target = folder / f"{stem}-{tries}"
    return target


def to_trash(source, target=None) -> str:
    """Nothing athena clears is deleted outright: it waits in the trash."""
    import shutil
    target = target or trash_place(Path(source).name)
    shutil.move(str(source), str(target))
    return str(target)


def valid_name(name) -> bool:
    return bool(NAME_RE.match(name or ""))
