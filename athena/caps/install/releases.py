"""The release store: whole copies side by side, one link that says which one runs, one record that says what each is.

    ~/.local/share/athena/releases/<version>-<hash>   a whole copy, never changed once made
    ~/.local/share/athena/current                     a link, swapped atomically: running Claudes' hooks never see a half-copied tree
    ~/.local/share/athena/install.json                the record, below

The record names everything an install or an update decides by, so nothing is read off the folder or the link alone:

    {"schema": 2,
     "current":  {"release", "version", "commit"},     what runs
     "previous": {"release", "version", "commit"} | null, what it replaced, kept on disk to go back to
     "source":   a checkout | null,                    where `install` last copied from: what runs, unless an update came since
     "repo", "channel",                                where `update` looks, and for what: "release" (vX.Y.Z tags) or "main"
     "lab_guard": "on" | "out",
     "switch":   null | {"from", "to", "since"},       an update between the link's swap and its health check's verdict
     "latest":   null | {"version", "ref", "commit", "checked"},   what `update --check` last found
     "last_update": null | {"result", "detail", "ts"}}
"""
import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from athena.cli import UserError
from athena.kernel import caps, store

SHIP = ("VERSION", "bin", "athena", "prompts", "scripts", "docs", "menubar", "plugin", "worker-plugin", "ARCHITECTURE.md")
SCHEMA = 2
REPO = "https://github.com/Delitefully/athena.git"
CHANNELS = ("release", "main")
_GUARD_SHELL = re.compile(r'^# The guard runs here too.*?^esac\n', re.M | re.S)
_GUARD_ACTIVE = 'return (os.environ.get("ATHENA_WORKER") or "").startswith("rex-")'
_GUARD_PATHS = re.compile(r"^LIVE_PATHS = \(.*?^\)\n", re.M | re.S)


def home() -> Path:
    return Path(os.path.expanduser("~"))


def root() -> Path:
    return home() / ".local/share/athena"


def record_file() -> Path:
    return root() / "install.json"


@contextlib.contextmanager
def locked():
    """Yields False when someone holds it. Held by an update, an install and a rollback, which all change `current` and the
    record; it lives beside them, as the state dir may be elsewhere or not made yet."""
    root().mkdir(parents=True, exist_ok=True)
    fd = os.open(root() / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def short(path) -> str:
    text, here = str(path), str(home())
    return "~" + text[len(here):] if text.startswith(here + os.sep) else text


def version_of(tree) -> str:
    try:
        text = Path(tree, "VERSION").read_text().strip()
    except OSError:
        raise UserError(f"{tree} has no VERSION file: not an athena release")
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise UserError(f"{tree}/VERSION says {text!r}, not a version like 1.2.3")
    return text


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def _entry(value):
    """A release as the record names it; schema 1 named a bare release id."""
    if isinstance(value, dict) and value.get("release"):
        return {"release": str(value["release"]), "version": value.get("version"), "commit": value.get("commit")}
    if isinstance(value, str) and value:
        return {"release": value, "version": None, "commit": None}
    return None


def load() -> dict:
    """The record, whole: every field present. Schema 1 (release, previous, source, lab_guard) is read as 2."""
    data = store.read_dict(record_file())
    flat = data.get("schema") is None and isinstance(data.get("release"), str)
    switch = data.get("switch") if isinstance(data.get("switch"), dict) else None
    return {
        "schema": SCHEMA,
        "current": _entry(data.get("release") if flat else data.get("current")),
        "previous": _entry(data.get("previous")),
        "source": data.get("source") if isinstance(data.get("source"), str) else None,
        "repo": data.get("repo") if isinstance(data.get("repo"), str) and data.get("repo") else REPO,
        "channel": data.get("channel") if data.get("channel") in CHANNELS else "release",
        "lab_guard": "out" if data.get("lab_guard") == "out" else "on",
        "switch": {"from": _entry(switch.get("from")), "to": _entry(switch.get("to")), "since": switch.get("since")} if switch else None,
        "latest": data.get("latest") if isinstance(data.get("latest"), dict) else None,
        "last_update": data.get("last_update") if isinstance(data.get("last_update"), dict) else None,
    }


def save(record):
    """Written only when it says something new, so an install over itself changes nothing."""
    if record_file().exists() and load() == {k: v for k, v in record.items() if k != "ts"}:
        return
    store.write_json(record_file(), {**record, "schema": SCHEMA, "ts": time.time()}, indent=1)


def files(source) -> list:
    out = []
    for entry in SHIP:
        place = Path(source, entry)
        if place.is_file():
            out.append(place)
        for folder, dirs, names in os.walk(place):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "build"))
            out.extend(Path(folder, n) for n in sorted(names) if not n.endswith((".pyc", ".DS_Store")))
    return sorted(out)


def release_id(source, guard_out) -> str:
    digest = hashlib.sha256()
    for path in files(source):
        digest.update(str(path.relative_to(source)).encode() + b"\0" + path.read_bytes() + b"\0")
    return f"{version_of(source)}-{digest.hexdigest()[:12]}" + ("-live" if guard_out else "")


def take_the_guard_out(release):
    """The lab's guards leave this copy only, never the tree it came from, which keeps them for whoever works on athena there."""
    release = Path(release)
    if (release / ".git").exists():
        raise UserError(f"{release} is a checkout, not a release; the guard stays there")
    lab_file, command = release / "athena/kernel/lab.py", release / "bin/athena"
    text, shell = lab_file.read_text(), command.read_text()
    if _GUARD_ACTIVE not in text or not _GUARD_PATHS.search(text) or not _GUARD_SHELL.search(shell):
        raise UserError("the lab guard is not where this build expects it; nothing was changed")
    lab_file.write_text(_GUARD_PATHS.sub("LIVE_PATHS = ()\n", text).replace(_GUARD_ACTIVE, "return False"))
    command.write_text(_GUARD_SHELL.sub("", shell))


def make(source, guard_out) -> dict:
    """The release for this tree, made once: copied whole beside its final name, then renamed into place, so a release that
    exists is complete. An interrupted copy leaves only a dot-folder, which the next make or prune clears."""
    name = release_id(source, guard_out)
    target = root() / "releases" / name
    if not target.is_dir():
        partial = target.with_name(f".{name}.{os.getpid()}")
        shutil.rmtree(partial, ignore_errors=True)
        for path in files(source):
            copy = partial / path.relative_to(source)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
        if guard_out:
            take_the_guard_out(partial)
        os.replace(partial, target)
    return {"release": name, "version": version_of(target), "commit": None}


def checkout_commit(tree):
    """HEAD of `tree` when it is a git checkout with nothing changed, so a release made from it is that commit; else None."""
    try:
        head = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        changed = subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], capture_output=True, text=True, timeout=60,
                                 stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip() if head.returncode == 0 and changed.returncode == 0 and not changed.stdout.strip() else None


def installed_command(*argv, timeout=120, own_session=False):
    """The installed copy, run as itself. A copy that hangs or won't start answers as a failed run, never a traceback. In its
    own session it outlives a kill of this process's group, as launchd's restart of the job running this does."""
    try:
        return subprocess.run([str(root() / "current/bin/athena"), *argv], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, start_new_session=own_session)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", f"{type(exc).__name__}: {exc}")


def installed_jobs() -> list:
    """athena's own launchd jobs, by label. Asked before `current` moves, and handed to restart_services: this process puts the
    `current` link on its path, so an import after the swap would load the other release's code into it."""
    services = caps.get("services")
    return sorted(p.stem for p in services.agents_dir().glob(f"{services.PREFIX}.*.plist")) if services else []


def restart_services(jobs) -> str:
    """athena's own launchd `jobs` onto the release `current` names, through that release's own command; nothing is imported
    here. Last in whatever calls it, after the record is saved: run from the doctor job, the restart ends the very process
    that asked for it, and launchd takes that job's process group with it. So the restart runs in a session of its own, and
    goes on to the watch and the dash."""
    if not jobs:
        return "no launchd jobs to restart"
    done = installed_command("services", "restart", own_session=True)
    said = (done.stdout or done.stderr).strip()
    return said.splitlines()[-1] if said else "jobs restarted"


def exists(entry) -> bool:
    return bool(entry) and (root() / "releases" / entry["release"] / "bin" / "athena").is_file()


def point(entry):
    """`current` names this release, swapped in one rename."""
    link = root() / "current"
    fresh = link.with_name(f".current.{os.getpid()}")
    fresh.unlink(missing_ok=True)
    os.symlink(Path("releases") / entry["release"], fresh)
    os.replace(fresh, link)


def pointed() -> str:
    link = root() / "current"
    return os.path.basename(os.readlink(link)) if link.is_symlink() else ""


def prune(*keep):
    """Every release but these, and any copy an interrupted make left."""
    names = {entry["release"] for entry in keep if entry}
    folder = root() / "releases"
    for entry in sorted(folder.iterdir() if folder.is_dir() else []):
        if entry.name not in names:  # a dot-folder too: only one make runs at a time, under locked()
            shutil.rmtree(entry, ignore_errors=True)
