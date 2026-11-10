"""The board's replacement. Rex has one session per worker and no terminal mirror, so columns of attached panes are gone.

Instead: a read-only live panel, in a block beside HQ, with one row per worker and the tail of its screen, redrawn from
status and capture. A glance, never a place to type; `board jump` shows the real session in the app.
"""
import shutil
import sys
import time

from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, ledger, store
from athena_rex.kernel.backend import BackendError, Place

SUMMARY = "the worker board: a live read-only panel beside HQ"
TAIL = 3
MARKS = {"blocked": "!", "exited": "x", "idle-unreported": "?", "done": "+", "working": "*", "idle": "-", "starting": "."}


def render(rows, tails, width=100) -> str:
    out = []
    for r in rows:
        report = (r.get("report") or {}).get("status") or ""
        out.append(f"{MARKS.get(r['state'], ' ')} {r['name']:<16} {r['state']:<15} {report:<20} {r.get('title') or r.get('branch') or ''}"[:width])
        for line in tails.get(r["name"], []):
            out.append(f"    {line}"[:width])
    return "\n".join(out) if out else "no live workers"


def _clean(screen: str) -> list:
    rows = [line.rstrip() for line in screen.splitlines()]
    kept = [line for line in rows if line.strip() and not set(line.strip()) <= set("─━-❯ ")]
    return kept[-TAIL:]


def tails(ctx, rows) -> dict:
    out = {}
    for r in rows:
        if r.get("space") and r.get("block") and r["state"] in ("working", "blocked"):
            try:
                out[r["name"]] = _clean(ctx.backend.screen(Place(r["space"], r["block"])))
            except BackendError:
                pass
    return out


def show(ctx, with_tails=False) -> str:
    from athena_rex.caps.status import collect
    rows = collect()
    width = shutil.get_terminal_size((100, 24)).columns
    return render(rows, tails(ctx, rows) if with_tails else {}, width)


def _configure(p):
    p.add_argument("action", nargs="?", choices=["show", "live", "open", "close", "jump"], default="show")
    p.add_argument("name", nargs="?")
    p.add_argument("--interval", type=float, default=5.0)


def _run(args):
    ctx = caps.CTX
    if args.action == "show":
        print(show(ctx, with_tails=True))
    elif args.action == "live":
        while True:
            sys.stdout.write("\x1b[H\x1b[2J" + show(ctx, with_tails=True) + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    elif args.action == "open":
        hq = store.read_dict(store.path("hq.json"))
        if not hq.get("block"):
            raise UserError("HQ is not on this backend: run `athena-rex hq` first")
        binary = store.ROOT / "bin" / "athena-rex"
        env = ["env", f"ATHENA_STATE_DIR={store.state_dir()}"]
        place = ctx.backend.split(Place(hq["space"], hq["block"]), hq.get("cwd") or "/",
                                  [*env, str(binary), "board", "live"], "board", ratio=35)
        store.write_json(store.path("board-rex.json"), {"space": place.space, "block": place.block})
        print(f"board open in {place.block}")
    elif args.action == "close":
        saved = store.read_dict(store.path("board-rex.json"))
        if saved.get("block"):
            try:
                ctx.backend.close_block(Place(saved["space"], saved["block"]))
            except BackendError:
                pass
            store.path("board-rex.json").unlink(missing_ok=True)
    else:
        worker = ledger.get(args.name or "")
        if not worker or not worker.get("space"):
            raise UserError(f"no worker named {args.name} on this backend")
        try:
            ctx.backend.focus(worker["space"], worker.get("window"))
        except BackendError as exc:
            raise UserError(f"could not show it ({exc}). Rex needs Remote Control on to switch sessions from outside; "
                            f"its session is \"{worker.get('label')}\"")
    return 0


def commands():
    return {"board": caps.Command(SUMMARY, _configure, _run)}
