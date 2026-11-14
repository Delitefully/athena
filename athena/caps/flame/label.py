"""Who a session was: HQ, a worker (with its ticket), or someone else's. From athena's own records only, recomputed every run.

In order: Claude Code's `agent-name` record (athena names each window after its worker, and HQ `athena`); a worker's
`workers/<name>.json` session id and `hq.json`'s; then the session's folder against the ledger's spawn paths, the spawn
closest before the session's start.
"""
import os

from athena.caps.flame import frames
from athena.kernel import config, store

SLACK_MS = 10 * 60 * 1000


def _ms(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number * 1000) if number < 1e11 else int(number)  # the ledger keeps seconds


def athena_facts() -> dict:
    spawns = {}
    by_path = {}
    for record in store.read_lines(store.path("ledger.jsonl")):
        if record.get("event") != "spawn" or not record.get("name"):
            continue
        entry = {"name": str(record["name"]), "ticket": record.get("linear") or "", "title": record.get("title") or "",
                 "ts": _ms(record.get("ts")) or 0}
        spawns.setdefault(entry["name"], []).append(entry)
        if record.get("path"):
            by_path.setdefault(os.path.realpath(str(record["path"])), []).append(entry)
    for record in store.read_lines(store.path("history.jsonl")):
        name = record.get("name")
        if name and name not in spawns:
            spawns[str(name)] = [{"name": str(name), "ticket": record.get("linear") or "", "title": record.get("title") or "",
                                  "ts": _ms(record.get("ts")) or 0}]
    sessions = {}
    workers = store.path("workers")
    try:
        names = os.listdir(workers)
    except OSError:
        names = []
    for entry in names:
        if entry.endswith(".json"):
            data = store.read_json(workers / entry, {})
            if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                sessions[data["session_id"]] = ("worker", entry[:-5])
    hq = store.read_json(store.path("hq.json"), {})
    hq = hq if isinstance(hq, dict) else {}
    for key in ("session", "previous_session"):
        if isinstance(hq.get(key), str):
            sessions[hq[key]] = ("hq", config.hq_name())
    return {"spawns": spawns, "by_path": by_path, "sessions": sessions, "hq": config.hq_name()}


def _closest(entries, first):
    if not entries:
        return None
    before = [e for e in entries if e["ts"] <= (first or 0) + SLACK_MS]
    return max(before, key=lambda e: e["ts"]) if before else min(entries, key=lambda e: e["ts"])


def classify(session, agent_name, cwd, first, facts):
    """(kind, name, ticket, title)."""
    spawns = facts["spawns"]
    if agent_name == facts["hq"]:
        return "hq", "HQ", "", ""
    if agent_name in spawns:
        spawn = _closest(spawns[agent_name], first)
        return "worker", agent_name, spawn["ticket"], spawn["title"]
    known = facts["sessions"].get(session)
    if known and known[0] == "hq":
        return "hq", "HQ", "", ""
    if known:
        spawn = _closest(spawns.get(known[1]) or [], first)
        return "worker", known[1], spawn["ticket"] if spawn else "", spawn["title"] if spawn else ""
    if cwd:
        spawn = _closest(facts["by_path"].get(os.path.realpath(cwd)) or [], first)
        if spawn:
            return "worker", spawn["name"], spawn["ticket"], spawn["title"]
    place = os.path.basename(str(cwd or "").rstrip("/")) or "unknown"
    return "other", frames.safe(place, "unknown"), "", ""


def label_all(db, facts=None):
    facts = facts if facts is not None else athena_facts()
    rows = db.execute("""SELECT session, MIN(first_ms), MAX(last_ms),
                         MAX(CASE WHEN kind='main' THEN agent_name END), MAX(CASE WHEN kind='main' THEN cwd END), MAX(cwd)
                         FROM files GROUP BY session""").fetchall()
    out = []
    for session, first, last, agent_name, main_cwd, any_cwd in rows:
        cwd = main_cwd or any_cwd
        kind, name, ticket, title = classify(session, agent_name, cwd, first, facts)
        out.append((session, kind, name, str(ticket)[:40], str(title)[:200], cwd, first, last))
    db.execute("DELETE FROM sessions")
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", out)
    return len(out)
