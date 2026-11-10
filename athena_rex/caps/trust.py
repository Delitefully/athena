"""Folder trust: no launch ever stops at Claude's trust prompt.

Claude's own messages name the way: `projects[<path>].hasTrustDialogAccepted: true` in its global config, keyed by the
main checkout (a worktree follows it). Claude rewrites that file often, re-reading it under `<file>.lock`, a directory
in the proper-lockfile convention, so this takes the same lock, changes one key and renames. Without the lock it writes
nothing and says so: a stale write would lose another Claude's changes.
"""
import json
import os
import shlex
import stat
import time
import unicodedata
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import caps, gitops, lab, store, world

SUMMARY = "make a folder trusted for Claude before it starts"
STALE_AFTER = 10.0
LOCK_WAIT = 8.0


class TrustError(Exception):
    pass


def config_path() -> Path:
    root = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(root) / ".claude.json" if root else Path(os.path.expanduser("~")) / ".claude.json"


def why_not(target: Path):
    """Why this can't be written, or None. Anything under the real home needs the door open, however it is spelled: a test
    or a lab run uses an isolated HOME, and writes there."""
    if os.environ.get("ATHENA_REX_TRUST") == "off":
        return "ATHENA_REX_TRUST=off"
    if lab.inside(target, [lab.real_home()]) and not world.enabled():
        return "rex.real_world is not set in this state dir's config"
    return None


def key_for(folder) -> str:
    try:
        return gitops.main_checkout(folder)
    except gitops.GitError:
        return os.path.realpath(folder)


def _acquire(lock: str, wait: float):
    deadline = time.monotonic() + wait
    while True:
        try:
            os.mkdir(lock)
            return
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > STALE_AFTER:
                    os.rmdir(lock)
                    continue
            except OSError:
                continue
        if time.monotonic() >= deadline:
            raise TrustError(f"{lock} stayed held for {wait:.0f}s; no change written")
        time.sleep(0.1)


def is_trusted(folder) -> bool:
    data = store.read_dict(config_path())
    key = key_for(folder)
    return any(((data.get("projects") or {}).get(k) or {}).get("hasTrustDialogAccepted") is True
               for k in (key, unicodedata.normalize("NFC", key)))


def ensure(folder, wait: float = LOCK_WAIT) -> dict:
    """Idempotent. {key, wrote} or raises TrustError. Other keys, and any writes made under the lock, are kept."""
    target = config_path()
    key = key_for(folder)
    readable = world.enabled() or not lab.inside(target, [lab.real_home()])
    if readable and is_trusted(folder):
        return {"key": key, "wrote": False}
    held = why_not(target)
    if held:
        return {"key": key, "wrote": False, "skipped": True, "why": held}
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = f"{target}.lock"
    _acquire(lock, wait)
    try:
        try:
            raw = target.read_text()
            mode = stat.S_IMODE(os.stat(target).st_mode)
        except FileNotFoundError:
            raw, mode = "{}", 0o600
        try:
            data = json.loads(raw)
        except ValueError:
            raise TrustError(f"{target} does not parse; left as it is")
        if not isinstance(data, dict):
            raise TrustError(f"{target} is not an object; left as it is")
        projects = data.setdefault("projects", {})
        for k in {key, unicodedata.normalize("NFC", key)}:
            entry = projects.setdefault(k, {})
            entry["hasTrustDialogAccepted"] = True
        real = Path(os.path.realpath(target))
        tmp = real.with_name(f"{real.name}.tmp.athena.{os.getpid()}")
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, real)
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass
    return {"key": key, "wrote": True}


def _configure(p):
    p.add_argument("folder", help="the folder a Claude will start in")
    p.add_argument("--check", action="store_true", help="say whether it is trusted, and change nothing")


def _run(args):
    if args.check:
        trusted = is_trusted(args.folder)
        print(f"{key_for(args.folder)}: {'trusted' if trusted else 'not trusted'} in {config_path()}")
        return 0 if trusted else 1
    try:
        out = ensure(args.folder)
    except TrustError as exc:
        raise UserError(str(exc))
    if out.get("skipped"):
        print(f"skipped: {shlex.quote(str(config_path()))} is off limits here; {out['key']} not changed")
    else:
        print(f"{out['key']}: trusted{' (written)' if out['wrote'] else ''}")
    return 0


def commands():
    return {"trust": caps.Command(SUMMARY, _configure, _run)}
