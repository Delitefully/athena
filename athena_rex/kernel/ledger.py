"""Worker lifecycles: the append-only ledger.jsonl, shared with the old athena, and the state machines read from it.

`state` is what the old athena reads, and comes from the events (LIFECYCLE). `phase` adds what it leaves implicit, `goal_state` says
where a worker's goal is (GOAL_MOVES). All are computed from the record, so none can drift from it, and a move the table lacks
isn't refused: this file is shared, and a fold that refused would hide what happened.
Rex places are the added fields `backend`, `space` and `block`: a record with none is the old athena's worker.
"""
import os
import time
from pathlib import Path

from athena_rex.kernel import store

TERMINAL = {"retire": "retired", "failed": "failed"}
RETIRING_EXPIRES = 1800
SNAPSHOT_TRUSTED = 3600
LIFECYCLE = {None: {"live", "damaged"}, "live": {"failed", "retired"}, "failed": {"live", "retired"}, "damaged": {"live", "retired"}, "retired": {"live"}}
RETIRABLE = ("live", "failed", "damaged")
GOAL_MOVES = {
    "none": {"wait": {"first"}, "amend": {"amended"}},
    "first": {"wait": {"first"}, "amend": {"first"}, "begin": {"unconfirmed"}},
    "amended": {"amend": {"amended"}, "begin": {"unconfirmed"}},
    "unconfirmed": {"amend": {"amended"}, "sent": {"none"}, "refused": {"first", "amended"}, "resend": {"first", "amended"}},
}


def _file():
    return store.path("ledger.jsonl")


def append(event: str, name: str, **fields) -> dict:
    record = {"ts": time.time(), "event": event, "name": name, **fields}
    store.append_line(_file(), record, sync=True)
    return record


def events():
    return store.read_lines(_file())


def _apply(record, event) -> dict:
    """One event on one worker's record. A lost `spawn` line makes it `damaged`, never a crash for every reader."""
    kind = event.get("event")
    fields = {k: v for k, v in event.items() if k not in ("event", "ts")}
    fields["updated"] = event.get("ts")
    if kind == "spawn":
        return {**fields, "state": "live", "spawned": event.get("ts")}
    merged = {**(record or {}), **fields}
    if kind in TERMINAL:
        merged["state"] = TERMINAL[kind]
    elif record is None:
        merged["state"] = "damaged"
    return merged


def fold(lines) -> dict:
    out = {}
    for event in lines:
        name = event.get("name")
        if name and isinstance(name, str):
            out[name] = _apply(out.get(name), event)
    return out


def workers() -> dict:
    """Every worker's latest record."""
    return fold(events())


def live() -> list:
    return [w for w in workers().values() if w.get("state") == "live"]


def get(name):
    return workers().get(name)


def phase(worker) -> str:
    """launching -> (pending) -> live -> retiring, inside `state` live. A retire killed halfway stops reading as retiring in time."""
    state = (worker or {}).get("state")
    if state in ("retired", "failed", "damaged") or not state:
        return state or "none"
    if is_retiring(worker):
        return "retiring"
    if worker.get("goal_pending"):
        return "pending"
    if not worker.get("block") and worker.get("backend"):
        return "launching"
    return "live"


def goal_state(worker) -> str:
    """first: its first goal hasn't gone out, so its Claude may never have had an input box. amended: a restated one waits for a Claude
    that has had one. unconfirmed: told to the ledger before typing, and it may or may not have arrived."""
    if worker.get("goal_pending"):
        return "amended" if worker.get("goal_amending") else "first"
    return "unconfirmed" if worker.get("goal_unconfirmed") else "none"


def goal_fields(move, text=None, worker=None) -> dict:
    """The ledger fields of one move in GOAL_MOVES."""
    return {
        "wait": {"goal_pending": text, "goal_amending": None},
        "amend": {"goal_pending": text, "goal_amending": (goal_state(worker or {}) != "first") or None, "goal_unconfirmed": None},
        "begin": {"goal_pending": None, "goal_unconfirmed": text, "goal_told": None, "prompt": "sending", "goal_send": "deliver"},  # the old athena shows `prompt` only with it
        "refused": {"goal_pending": text, "goal_unconfirmed": None},
        "sent": {"goal_unconfirmed": None, "goal_amending": None, "pending_reason": None, "prompt": "sent"},
        "resend": {"goal_pending": text, "goal_unconfirmed": None},
    }[move]


def is_retiring(worker, now=None) -> bool:
    """A retire killed halfway leaves its mark; after a while the mark stops hiding a real exit."""
    mark = (worker or {}).get("retiring")
    return bool(mark) and (time.time() if now is None else now) - float(mark) < RETIRING_EXPIRES


def damaged() -> list:
    return [w for w in workers().values() if w.get("state") == "damaged"]


def _memory():
    return store.path("run", "ledger-live.json")


def lost() -> dict:
    """Workers the last snapshot had live and the ledger no longer knows at all: it was deleted, or cut short. A snapshot past its
    trust speaks only for what is still on disk, so a ledger reset on purpose isn't held up by workers long gone."""
    known = workers()
    memory = store.read_dict(_memory())
    fresh = time.time() - float(memory.get("ts") or 0) < SNAPSHOT_TRUSTED
    return {name: record for name, record in (memory.get("live") or {}).items()
            if name not in known and (fresh or os.path.isdir(record.get("path") or ""))}


def unreadable() -> tuple:
    """(unreadable lines, how many of them are new since the last look)."""
    count = store.unreadable_lines(_file())
    told = int(store.read_dict(_memory()).get("unreadable") or 0)
    return count, max(0, count - told)


def remember():
    """A snapshot of who is live and how much is unreadable, kept beside the ledger so a lost ledger can come back."""
    store.write_json(_memory(), {"ts": time.time(), "live": {w["name"]: w for w in live() if w.get("name")},
                                 "unreadable": store.unreadable_lines(_file())})


def restore() -> list:
    """Puts the snapshot's lost workers back as spawn lines. Fold's own bookkeeping is dropped; everything else is what it was."""
    back = lost()
    for name, record in back.items():
        fields = {k: v for k, v in record.items() if k not in ("name", "state", "spawned", "updated", "event", "ts")}
        append("spawn", name, restored=True, **fields)
    return sorted(back)


def managed(worker) -> bool:
    return bool(worker.get("backend") and worker.get("space"))


def find_by_path(cwd: str):
    here = Path(os.path.realpath(cwd))
    return next((w for w in live() if w.get("path") and Path(w["path"]) in (here, *here.parents)), None)


def reviews(name: str) -> list:
    """HQ's review records for this worker's current life: a respawn starts with none."""
    mine = [e for e in events() if e.get("name") == name]
    born = max((at for at, e in enumerate(mine) if e.get("event") == "spawn"), default=-1)
    return [e["review"] for e in mine[born + 1:] if e.get("event") == "review" and isinstance(e.get("review"), dict)]
