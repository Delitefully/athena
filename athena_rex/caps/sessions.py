"""Rex sessions, as people see them in the app: one per repo with a window per worker, HQ's, and whatever else is on the server.

    worker  a repo's session with a live worker in it (or, from before repo sessions, a live worker's own)
    hq      the chief of staff's
    orphan  athena made it, and every worker in it has been retired or failed since
    other   not athena's. Listed, never touched

The watch and the dash keep a snapshot, so `status` stays a read of files. Only `sessions` itself asks Rex for the detail.
"""
import json
import time

from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, ledger, store, world
from athena_rex.kernel.backend import BackendError, last_look

SUMMARY = "every Rex session on the server: whose it is, what runs there, who is looking"
REFRESH_EVERY = 15


def _file():
    return store.path("run", "sessions.json")


def classify(spaces, workers, hq_space) -> list:
    """One row per session on the server. Pure. A repo's session holds several workers: it is a worker's while any of them is live,
    and `owner` is the last of those to come (or the last of all, for an orphan)."""
    by_space = {}
    for worker in sorted(workers.values(), key=lambda w: float(w.get("spawned") or 0)):
        if worker.get("space") and worker.get("backend"):
            by_space.setdefault(worker["space"], []).append(worker)
    rows = []
    for space in spaces:
        held = by_space.get(space["id"]) or []
        live = [w for w in held if w.get("state") == "live"]
        if hq_space and space["id"] == hq_space:
            kind, name, owners = "hq", "hq", ["hq"]
        elif live:
            kind, name, owners = "worker", live[-1]["name"], [w["name"] for w in live]
        elif held:
            kind, name, owners = "orphan", held[-1]["name"], [w["name"] for w in held]
        else:
            kind, name, owners = "other", None, []
        rows.append({"id": space["id"], "label": space.get("label"), "kind": kind, "owner": name, "owners": owners})
    return rows


def _with_windows(backend, rows) -> list:
    """A repo's session outlives its workers, so `status` finds one by its window: athena's sessions carry their windows' ids."""
    for row in rows:
        if row["kind"] in ("worker", "orphan"):
            try:
                row["windows"] = [w["id"] for w in backend.windows(row["id"])]
            except BackendError:
                pass
    return rows


def _hq_space():
    return store.read_dict(store.path("hq.json")).get("space")


def refresh(ctx, now=None) -> dict:
    """What the server says now, kept for whoever only reads files. A server that doesn't answer says so, rather than `no sessions`."""
    now = time.time() if now is None else now
    try:
        rows = _with_windows(ctx.backend, classify(ctx.backend.spaces(), ledger.workers(), _hq_space()))
        snapshot = {"ts": now, "sessions": rows}
    except BackendError as exc:
        snapshot = {"ts": now, "sessions": [], "down": exc.code}
    except world.Off:
        snapshot = {"ts": now, "sessions": [], "down": "off here"}
    try:
        store.write_json(_file(), snapshot)
    except OSError:
        pass
    return snapshot


snapshot = last_look


def _detailed(ctx, row) -> dict:
    try:
        return {**row, **ctx.backend.describe(row["id"])}
    except (BackendError, ValueError):
        return row


def _state_of(row, workers) -> str:
    if row["kind"] == "worker":
        return (store.read_dict(store.path("workers", f"{row['owner']}.json")).get("state") or "unknown")
    return "-"


def orphans(rows, workers) -> list:
    """Only sessions athena made, with no Claude of theirs still running in them."""
    found = []
    for row in rows:
        if row["kind"] != "orphan":
            continue
        try:
            if not any(agents.stat_of(owner) for owner in row.get("owners") or [row["owner"]]):
                found.append(row)
        except agents.Unknown:
            pass
    return found


def _configure(p):
    p.add_argument("action", nargs="?", choices=["list", "open", "close-orphans"], default="list")
    p.add_argument("worker", nargs="?", help="open: the worker whose session to show in the app")
    p.add_argument("--json", action="store_true")


def _open(ctx, name):
    record = store.read_dict(store.path("hq.json")) if name == "hq" else (ledger.get(name) or {})
    if not record.get("space"):
        raise UserError(f"no session on record for {name}")
    ctx.backend.focus(record["space"], record.get("window"))
    print(f"showing {name}'s session in the app")


def _run(args):
    ctx = caps.CTX
    if args.action == "open":
        if not args.worker:
            raise UserError("athena-rex sessions open <worker>")
        _open(ctx, args.worker)
        return 0
    seen = refresh(ctx)
    if seen.get("down"):
        print(f"the Rex server did not answer ({seen['down']})")
        return 1
    workers = ledger.workers()
    if args.action == "close-orphans":
        with store.locked("locks/repo-sessions.lock"):  # as spawn's and retire's, so no worker is put in a session closing now
            closed = orphans(refresh(ctx)["sessions"], workers)
            for row in closed:
                ctx.backend.close_space(row["id"])
                agents.forget_repo_session(row["id"])
                print(f"closed {row['label']} (left by {row['owner']})")
        print(f"{len(closed)} closed; sessions that aren't athena's were not touched")
        refresh(ctx)
        return 0
    rows = [{**_detailed(ctx, row), "state": _state_of(row, workers)} for row in seen["sessions"]]
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    print(f"{'SESSION':<28} {'KIND':<7} {'OWNER':<16} {'STATE':<16} {'PANES':<6} {'LOOKING':<8} ID")
    for row in rows:
        print(f"{str(row['label'])[:27]:<28} {row['kind']:<7} {row['owner'] or '-':<16} {row['state']:<16} {row.get('blocks', '-'):<6} {row.get('clients', '-'):<8} {row['id']}")
    if not rows:
        print("no sessions on this server")
    return 0


def tick(ctx, now):
    from athena_rex.caps.watch import Line
    if now - float(store.read_dict(_file()).get("ts") or 0) < REFRESH_EVERY:
        return []
    told = store.read_dict(store.path("run", "orphans-told.json")).get("ids") or []
    rows = refresh(ctx, now).get("sessions") or []
    left = [r["id"] for r in rows if r["kind"] == "orphan"]
    new = [r for r in rows if r["kind"] == "orphan" and r["id"] not in told]
    if set(left) != set(told):
        store.write_json(store.path("run", "orphans-told.json"), {"ids": left})
    return [Line(f"session left behind by {r['owner']}: athena-rex sessions close-orphans", "routine", r["owner"]) for r in new]


def checks(ctx):
    """Reported before anything is removed. Closing them is a person's or HQ's call, by the command it names."""
    seen = snapshot()
    left = [r for r in seen.get("sessions") or [] if r["kind"] == "orphan"]
    if not left:
        return [caps.Check("sessions", True, f"{len(seen.get('sessions') or [])} on the server" if seen else "not looked yet")]
    return [caps.Check("sessions", False, f"left behind by {', '.join(str(r['owner']) for r in left)}: athena-rex sessions close-orphans")]


def commands():
    return {"sessions": caps.Command(SUMMARY, _configure, _run)}
