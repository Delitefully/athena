"""The event flow: an append-only events.jsonl any process may emit to, read by cursor. A new file, so the old athena never sees it.
It tells, and holds no state of its own, so an event that can't be written costs a line and must never cost what caused it."""
import json
import os
import time

from athena_rex.kernel import store

ROTATE_AT = 4 << 20


def _file():
    return store.path("events.jsonl")


def emit(topic: str, **fields) -> dict:
    record = {"ts": time.time(), "topic": topic, **fields}
    target = _file()
    try:
        if target.stat().st_size > ROTATE_AT:
            os.replace(target, target.with_suffix(".jsonl.1"))
    except OSError:
        pass
    try:
        store.append_line(target, record)
    except OSError:
        pass
    return record


def tail(offset: int) -> tuple:
    """(events after byte `offset`, the new offset). A rotation restarts from the top of the new file."""
    target = _file()
    out = []
    try:
        size = target.stat().st_size
        if offset > size:
            offset = 0
        with open(target) as f:
            f.seek(offset)
            for line in f:
                if not line.endswith("\n"):
                    break
                offset += len(line.encode())
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:  # gone, or rotated between the stat and the open
        return out, 0
    return out, offset


def end() -> int:
    try:
        return _file().stat().st_size
    except OSError:
        return 0
