"""Append-only record of worker lifecycles. Only athena commands write it; anyone may read it."""
import json
import os
import time

from athena_lib import paths

TERMINAL = {"retire": "retired", "failed": "failed"}


def _file():
    return paths.ensure() / "ledger.jsonl"


def _read_file():
    return paths.state_dir() / "ledger.jsonl"


def append(event: str, name: str, **fields) -> dict:
    record = {"ts": time.time(), "event": event, "name": name, **fields}
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    return record


def review(name: str, verdict: str, head: str, **extra) -> dict:
    """HQ's own review of a worker's PR (`athena review`): start, pass or fail at the head it reviewed. The latest one
    stands; a respawn starts with none. A start also carries its `round` and, after the first, `since`: the head the
    last verdict was for, so the reviewer reads only what changed."""
    return append("review", name, review={"verdict": verdict, "head": head, "at": time.time(), **extra})


def reviews(name: str) -> list:
    """The review records of the worker's current life (since its latest spawn), oldest first."""
    out = []
    for ev in events():
        if ev.get("name") != name:
            continue
        if ev.get("event") == "spawn":
            out = []
        elif ev.get("event") == "review" and isinstance(ev.get("review"), dict):
            out.append(ev["review"])
    return out


def touch():
    _file().touch(exist_ok=True)


def events():
    try:
        with open(_read_file()) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def workers() -> dict:
    """Fold the events into the latest record per worker name."""
    out = {}
    for ev in events():
        name = ev.get("name")
        if not name:
            continue
        kind = ev.get("event")
        fields = {k: v for k, v in ev.items() if k not in ("event", "ts")}
        if kind == "spawn":
            out[name] = {**fields, "state": "live", "spawned": ev.get("ts")}
        elif kind == "failed":
            out[name] = {**fields, "state": "failed"}
        elif name in out:
            out[name].update(fields)
            if kind in TERMINAL:
                out[name]["state"] = TERMINAL[kind]
        elif kind in TERMINAL:
            out[name] = {**fields, "state": TERMINAL[kind]}
        out[name]["updated"] = ev.get("ts")
    return out


def live() -> list:
    return [w for w in workers().values() if w.get("state") == "live"]


def find_by_path(cwd: str):
    real = os.path.realpath(cwd)
    for w in live():
        path = w.get("path")
        if path and (real == path or real.startswith(path.rstrip("/") + "/")):
            return w
    return None


def leftovers() -> list:
    """Failed launches whose space or worktree may still exist: retire them."""
    return [w for w in workers().values() if w.get("state") == "failed" and w.get("cleaned") is False]
