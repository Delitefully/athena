"""One status per worker, from files first: the report, the hook state and one `ps`. The backend only with --live, so it stays fast."""
import json
import os
import time

from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, config, ledger, store
from athena_rex.kernel.backend import last_look, session_of

SUMMARY = "what every worker is doing, from files in milliseconds"
REPORT_QUIET = 120
URGENCY = {"blocked": 0, "limit": 0, "done": 1, "exited": 1, "idle-unreported": 1, "idle": 2, "starting": 3, "working": 3, "unknown": 3}
ALIVE_STATES = {"starting", "working", "idle", "done", "blocked", "idle-unreported", "unknown"}


def _lost_report(worker, hook, state) -> dict:
    """A report whose Stop never landed, from the transcript, only when the hook has gone quiet. It is held to the gate's own rules, as
    a DONE the gate sent back would otherwise read as done. `idle-unreported` means the gate saw it and gave way, so it stays that."""
    quiet = time.time() - float(hook.get("ts") or 0) > REPORT_QUIET
    hooks = caps.get("hooks")
    if hook.get("report") or not hooks or not worker.get("path") or not (state == "exited" or (state == "working" and quiet)):
        return {}
    report = hooks.report_in(agents.transcript_of(worker["path"], hook.get("session_id") or worker.get("session"), worker.get("name")))
    if not report or hooks.refusal("", report, worker["path"], store.path("briefs", f"{worker['name']}.md")):
        return {}
    settled, _ = hooks.next_state(state, {"hook_event_name": "Stop", "last_assistant_message": "ATHENA-REPORT " + json.dumps(report)})
    return {"report": report, "state": state if state == "exited" else settled}


def row(worker, hook, alive, stat=None, seen=None) -> dict:
    """Merge a worker's sources, most trusted first: its report, its hook state, whether its process is there.
    `alive` None means nobody could say, and then the hook's word stands: unknown is never gone."""
    name = worker["name"]
    state = hook.get("state") or "unknown"
    source = "hook"
    if float(worker.get("limit_until") or 0) > time.time():
        state, source = "limit", "limit"
    elif ledger.goal_state(worker) == "first":
        state, source = "blocked", "pending"
    elif str(stat or "").startswith("T"):
        state, source = "blocked", "process"
    elif alive is False and state in ALIVE_STATES and state != "done":
        state, source = "exited", "process"
    elif alive is False and state == "done":
        source = "report"
    lost = _lost_report(worker, hook, state) if source != "limit" else {}
    if lost:
        state, source = lost["state"], "transcript"
    return {"name": name, "phase": ledger.phase(worker), "state": state, "state_source": source,
            "branch": worker.get("branch"), "linear": worker.get("linear"), "title": worker.get("title"),
            "label": worker.get("label"), "path": worker.get("path"), "repo": worker.get("repo"),
            "space": worker.get("space"), "window": worker.get("window"), "block": worker.get("block"), "backend": worker.get("backend"), "session": session_of(worker, seen),
            "summary": hook.get("summary", ""), "report": lost.get("report") or hook.get("report"), "last_report": hook.get("last_report"),
            "hook_ts": hook.get("ts"), "session_id": hook.get("session_id"), "pending": bool(worker.get("goal_pending")),
            "detail": "its report came from its transcript, as its Stop never landed" if lost else worker.get("pending_reason") if worker.get("goal_pending") else (
                "its Claude is stopped and not reading input" if str(stat or "").startswith("T") else (
                    "its goal may not have arrived: resume --resend" if worker.get("goal_unconfirmed") else None)),
            "athena_review": worker.get("review"), "ultracode": bool(worker.get("ultracode")),
            "urgency": URGENCY.get(state, 3)}


def collect(names=None) -> list:
    all_workers = ledger.workers()
    if names:
        missing = [n for n in names if n not in all_workers]
        if missing:
            raise UserError(f"no worker named {', '.join(missing)}")
        chosen = [{**all_workers[n], "name": n} for n in names]
    else:
        chosen = [w for w in all_workers.values() if w.get("state") == "live"]
    try:
        running = agents.running([w["name"] for w in chosen if ledger.managed(w)])
        known = True
    except agents.Unknown:
        running, known = {}, False
    seen = last_look()
    rows = []
    for worker in chosen:
        hook = store.read_dict(store.path("workers", f"{worker['name']}.json"))
        managed = ledger.managed(worker) and worker.get("state") == "live"
        alive = (worker["name"] in running) if managed and known else None
        rows.append(row(worker, hook, alive, running.get(worker["name"]), seen))
    return sorted(rows, key=lambda r: (r["urgency"], r["name"]))


def live_details(ctx, rows):
    sessions = caps.get("sessions")
    seen = sessions.refresh(ctx) if sessions else {}
    for r in rows:
        r["session"] = session_of(r, seen)
        if r.get("space") and r.get("block"):
            try:
                now = agents.state(ctx.backend, r)
            except Exception as exc:
                r["live"] = {"error": str(exc)}
                continue
            r["live"] = now
            if r["state_source"] == "hook" and r["state"] in ("unknown", "starting") and now["state"]:
                r["state"], r["state_source"] = now["state"], "backend"


def _session_words(r) -> str:
    session = r.get("session") or {}
    if not session.get("id"):
        return "no Rex session" if r.get("backend") else "on the old athena"
    return f'session "{session.get("label") or session["id"]}"' + (" (gone)" if session.get("seen") is False else "")


def _line(r) -> str:
    report = r.get("report") or {}
    bits = [f"{r['name']:<16}", f"{r['state']:<15}"]
    if report.get("status"):
        bits.append(str(report["status"]))
    live = r.get("live") or {}
    if live.get("state"):
        bits.append(f"live={live['state']}")
    review = r.get("athena_review") or {}
    if review.get("verdict"):
        stale = ""
        pr_head = (r.get("pr") or {}).get("head")
        if pr_head and review.get("head"):
            prs = caps.get("prs")
            if prs and not prs.same_head(review["head"], pr_head):
                stale = " (stale)"  # a verdict is for the head it read; the PR has moved on
        bits.append(f"review={review['verdict']}@{str(review.get('head'))[:7]}{stale}")
    if r.get("pr"):
        bits.append(f"#{r['pr'].get('number')}")
    bits.append(r.get("branch") or "-")
    bits.append(_session_words(r))
    if r.get("detail"):
        bits.append(f"({r['detail']})")
    return "  ".join(bits)


def _detail(r) -> list:
    report = r.get("report") or r.get("last_report") or {}
    lines = [_line(r), f"  session {(r.get('session') or {}).get('id') or '-'}  path {r.get('path')}"]
    if r.get("summary"):
        lines.append(f"  last words: {r['summary']}")
    for key in ("status", "pr", "head", "verify"):
        if report.get(key):
            lines.append(f"  {key}: {report[key]}")
    for key in ("decisions", "concerns"):
        for entry in report.get(key) or []:
            lines.append(f"  {key[:-1]}: {entry}")
    return lines


def _configure(p):
    p.add_argument("workers", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--live", action="store_true", help="also ask the backend")
    p.add_argument("--pr", action="store_true", help="also ask GitHub")
    p.add_argument("--time", action="store_true", help="print how long this took")


def _hq_line():
    hq = store.read_dict(store.path("hq.json"))
    if not hq.get("block"):
        return None
    try:
        running = "running" if agents.stat_of(config.hq_name()) else "gone"
    except agents.Unknown:
        running = "unknown"
    line = f"{'hq':<16}  {running:<15}  {hq.get('session') or 'no conversation yet'}"
    module = caps.get("hq")
    tokens = module.context_tokens(module.trusted_session(hq)) if module else None
    if tokens is not None:
        due = ": rotate soon (athena-rex hq rotate, skill §1a)" if module.rotate_due(tokens) else ""
        line += f"\nhq context {module.against_line(tokens)}{due}"
    return line


def _run(args):
    begun = time.perf_counter()
    rows = collect(args.workers or None)
    if args.live:
        live_details(caps.CTX, rows)
    if args.pr:
        prs = caps.get("prs")
        if prs:
            for r in rows:
                if r.get("repo") and r.get("branch"):
                    r["pr"] = prs.summary(r["repo"], r["branch"])
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
    else:
        hq_line = _hq_line()
        if hq_line and not args.workers:
            print(hq_line)
        for r in rows:
            print("\n".join(_detail(r)) if args.workers else _line(r))
        if not rows:
            print("no live workers")
    if args.time:
        print(f"elapsed {1000 * (time.perf_counter() - begun):.1f} ms, {len(rows)} workers")
    return 0


def _whoami(args):
    name = os.environ.get("ATHENA_WORKER")
    if not name:
        found = ledger.find_by_path(os.getcwd())
        name = found["name"] if found else None
    if not name:
        return 1
    print(name)
    return 0


def commands():
    return {"status": caps.Command(SUMMARY, _configure, _run),
            "whoami": caps.Command("the worker this session is", lambda p: None, _whoami)}
