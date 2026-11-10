"""The lab's state guard: a development worker (ATHENA_WORKER=rex-*) never touches live state. Real side effects have their own
door, world.py, which the lab keeps shut. An installed copy can take the guard out (install --lab-guard-out)."""
import os
import pwd


SCRATCH_MARK = ".athena-scratch"


class Refused(Exception):
    pass


_HOME = pwd.getpwuid(os.getuid()).pw_dir
LIVE_PATHS = (
    os.path.join(_HOME, ".local/state/athena"),
    os.path.join(_HOME, "Library/Application Support/athena"),
)


def active() -> bool:
    return (os.environ.get("ATHENA_WORKER") or "").startswith("rex-")


def inside(path, roots) -> bool:
    """Whether `path` is one of `roots` or under one, by what it is and not how it is spelled: links are followed, case is
    ignored as the disk ignores it, and a folder is also known by its device and inode, so a firmlink can't hide it."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    known = set()
    for root in roots:
        resolved = os.path.realpath(root)
        if (real + os.sep).casefold().startswith(resolved.casefold().rstrip(os.sep) + os.sep):
            return True
        try:
            found = os.stat(resolved)
            known.add((found.st_dev, found.st_ino))
        except OSError:
            pass
    at = real
    while True:
        try:
            found = os.stat(at)
            if (found.st_dev, found.st_ino) in known:
                return True
        except OSError:
            pass
        parent = os.path.dirname(at)
        if parent == at:
            return False
        at = parent


def is_live(path) -> bool:
    return inside(path, LIVE_PATHS)


def overlaps_live(path) -> bool:
    """Live state, under it, or holding it: a state dir of `/` would make everything under it fair game."""
    return is_live(path) or any(inside(live, [path]) for live in LIVE_PATHS)


def off_limits(path) -> bool:
    """Where no scratch state dir can be: live state, anything holding it, and anywhere under the real home, whose projects
    and other data a state dir would otherwise make fair game."""
    return overlaps_live(path) or inside(path, [_HOME])


def scratch_place(path) -> bool:
    """Somewhere made to be thrown away: absolute, marked as such, outside the real home and not holding it. Being elsewhere isn't
    enough, since /private/tmp holds other sessions' work."""
    text = str(path or "").strip()
    return (os.path.isabs(text) and not inside(text, [_HOME]) and not inside(_HOME, [text])
            and os.path.isfile(os.path.join(text, SCRATCH_MARK)))


def check_state(path):
    if active() and off_limits(path):
        raise Refused(f"refusing {path}: ATHENA_WORKER={os.environ['ATHENA_WORKER']} may only use a sanitized scratch "
                      "copy. Set ATHENA_STATE_DIR to one (scripts/scratch-state.py).")


def real_home() -> str:
    return _HOME
