"""The watch: one loop that turns state changes into lines, and tells HQ the ones that matter.

Every loop it folds the workers into a snapshot, diffs it against the last one, reads new bus events, and asks each
capability for what it has (`tick`). A line is `wake`, `routine` or `quiet`, decided where it is made, not by parsing
it later. Lines are rebuilt from an allowlist before they go out: HQ reads a wake as the person's own words.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import namedtuple

from athena_rex.cli import UserError
from athena_rex.kernel import agents, bus, caps, config, ledger, store, sandbox

SUMMARY = "one line per change, waking HQ for what matters"
WAKE_LINES = 8
REPEAT_AFTER = 600
STUCK_AFTER = float(os.environ.get("ATHENA_STUCK_SECONDS") or 1800)
_SAFE = re.compile(r"[^A-Za-z0-9_.#:/>+ -]")
_WORKER_STATES = {"blocked", "exited", "idle-unreported"}
WATCH_STALE = 300
CLOCK_SLACK = 60
MAX_FAILED_LOOPS = 10


Line = namedtuple("Line", "text kind worker key", defaults=("routine", "", ""))


def safe(text: str) -> str:
    return _SAFE.sub("", str(text))[:160].strip() or "a watch line not shown here (athena-rex status)"


def _short(sha):
    return str(sha or "")[:7]


def _told_as(report):
    """What makes a report news: a second BLOCKED with a new question at the same head is one."""
    return (str(report.get("status") or "").upper(), _short(report.get("head")), str(report.get("concerns") or ""))


def diff(prev: dict, cur: dict, retiring=frozenset()) -> list:
    lines = []
    for name, now in cur.items():
        before = prev.get(name)
        if before is None:
            lines.append(Line(f"{name} new worker ({now.get('state')})", "routine", name))
            continue
        if before.get("state") != now.get("state"):
            quiet = name in retiring and now.get("state") == "exited"
            kind = "quiet" if quiet else ("wake" if now.get("state") in _WORKER_STATES else "routine")
            lines.append(Line(f"{name} state {before.get('state')}->{now.get('state')}", kind, name))
        report, was = now.get("report") or {}, before.get("report") or {}
        if report.get("status") and _told_as(report) != _told_as(was):
            status = str(report["status"]).upper()
            kind = "routine" if status == "IN_PROGRESS" else "wake"
            if status.startswith("DONE") and _told_as(report) == _told_as(before.get("last_report") or {}):
                kind = "quiet"
            lines.append(Line(f"{name} report {status}", kind, name, "|".join(_told_as(report))))
        lines.extend(_pr_lines(name, before.get("pr") or {}, now.get("pr") or {}))
    for name, before in prev.items():
        if name not in cur:
            lines.append(Line(f"{name} gone from the ledger", "quiet" if name in retiring else "wake", name))
    return lines


OLD_CHECKS = {"pass": "success", "fail": "failure"}  # this build's first words, in snapshots kept before the rename


def _pr_lines(name, was, now) -> list:
    lines = []
    if now and not was:
        return [Line(f"{name} pr opened {now.get('url') or '#' + str(now.get('number'))}", "wake", name)]
    if not (was and now):
        return lines
    was = {**was, "checks": OLD_CHECKS.get(was.get("checks"), was.get("checks"))}
    if was.get("checks") != now.get("checks"):
        kind = "wake" if now.get("checks") == "failure" else "routine"
        lines.append(Line(f"{name} checks {was.get('checks')}->{now.get('checks')}", kind, name))
    more = (now.get("comments") or 0) - (was.get("comments") or 0)
    if more > 0:
        lines.append(Line(f"{name} review comments +{more}", "wake", name))
    elif (now.get("all_comments") or 0) > (was.get("all_comments") or 0):
        lines.append(Line(f"{name} bot comments", "quiet", name))
    if now.get("review") and now.get("review") != was.get("review"):
        lines.append(Line(f"{name} review {now['review']}", "wake", name))
    if now.get("state") != was.get("state"):
        lines.append(Line(f"{name} pr {str(now.get('state')).lower()}", "wake", name))
    if now.get("mergeable") == "CONFLICTING" and was.get("mergeable") != "CONFLICTING":
        lines.append(Line(f"{name} pr conflicts #{now.get('number')}", "wake", name))
    elif was.get("mergeable") == "CONFLICTING" and now.get("mergeable") == "MERGEABLE":
        lines.append(Line(f"{name} pr mergeable #{now.get('number')}", "routine", name))
    return lines


class Dedupe:
    """The same line is told once in REPEAT_AFTER, unless a routine line about its worker came between."""

    def __init__(self, told=None):
        self.told = told or {}

    def keep(self, line: Line, now: float) -> bool:
        self.told = {k: v for k, v in self.told.items() if now - float(v or 0) < REPEAT_AFTER}
        if line.kind != "wake":
            if line.worker:
                self.told = {k: v for k, v in self.told.items() if not k.startswith(line.worker + " ")}
            return True
        if line.text.startswith("slack-new"):
            return True
        key = f"{line.text} {line.key}" if line.key else line.text
        if now - float(self.told.get(key) or 0) < REPEAT_AFTER:
            return False
        self.told[key] = now
        return True


def suppress(text, why=""):
    target = store.path("logs", "watch-suppressed.log")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 1 << 20:
            target.replace(target.with_suffix(".log.1"))
        with open(target, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}{' (' + why + ')' if why else ''}\n")
    except OSError:
        pass


def _remember(target, data):
    """A full disk costs the watch its memory across a restart, never the loop: the disk filling up is when it is needed most."""
    try:
        store.write_json(target, data)
    except OSError as exc:
        suppress("not written", f"{target.name}: {exc}")


def snapshot(ctx, prev: dict, now: float, fetch=None) -> dict:
    from athena_rex.caps import status
    rows = status.collect()
    prs = ctx.cap("prs")
    pr_of = {}
    if prs:
        due = prs.fetch_due(now) if fetch is None else fetch
        if due:
            _remember(store.path("run", "prs-tick.json"), {"ts": now})
        pr_of = prs.snapshot_workers(rows, due, prev)
    return {r["name"]: {"state": r["state"], "detail": r.get("detail"), "report": r.get("report"),
                        "last_report": r.get("last_report"), "pr": pr_of.get(r["name"]),
                        "mergeable": (pr_of.get(r["name"]) or {}).get("mergeable"),
                        "quiet_s": quiet_seconds(r, now)} for r in rows}


def quiet_seconds(r, now):
    """How long a working worker has shown no sign of life: the newest of its last hook and its transcript's growth."""
    if r["state"] != "working":
        return None
    newest = float(r.get("hook_ts") or 0)
    if r.get("session_id") and r.get("path"):
        try:
            newest = max(newest, agents.transcript_of(r["path"], r["session_id"]).stat().st_mtime)
        except (OSError, AttributeError):
            pass
    return now - newest if newest else None


def save_snapshot(workers, now):
    old = store.read_dict(store.path("watch.json"))
    followed = store.read_dict(store.path("run", "follow.json"))
    data = {"ts": now, "workers": workers}
    for key in ("prs", "review_requests", "stacks"):  # what the follow capability found, where the old athena's dash reads it
        if key in followed:
            data[key] = followed[key]
        elif key in old:
            data[key] = old[key]
    _remember(store.path("watch.json"), data)


def step(ctx, prev, offset, dedupe, now=None) -> tuple:
    """One loop, touching only files and the backend's read side: (snapshot, new bus offset, lines to tell)."""
    now = time.time() if now is None else now
    cur = snapshot(ctx, prev, now)
    retiring = {w["name"] for w in ledger.workers().values() if ledger.is_retiring(w, now) or w.get("state") == "retired"}
    lines = list(diff(prev, cur, retiring))
    events, offset = bus.tail(offset)
    for module in caps.loaded():
        if module.__name__.endswith(".watch"):
            continue
        handler = getattr(module, "on_event", None)
        if handler:
            for event in events:
                try:
                    lines.extend(handler(ctx, event) or [])
                except Exception as exc:
                    lines.append(Line(f"watch error {module.__name__.rsplit('.', 1)[-1]} {type(exc).__name__}", "wake"))
        ticker = getattr(module, "tick", None)
        if ticker:
            try:
                lines.extend(ticker(ctx, now) or [])
            except Exception as exc:
                lines.append(Line(f"watch error {module.__name__.rsplit('.', 1)[-1]} {type(exc).__name__}", "wake"))
    told = []
    for line in lines:
        if line.kind == "quiet":
            suppress(safe(line.text))
        elif dedupe.keep(line, now):
            told.append(Line(safe(line.text), line.kind, line.worker, line.key))
    save_snapshot(cur, now)
    return cur, offset, told


def _inbox():
    return store.path("run", "inbox.jsonl")


MAX_LINE = 200
_NAME = r"[a-z][a-z0-9_-]{0,31}"
_STATE = r"(?:working|idle-unreported|idle|done|blocked|exited|starting|unknown|limit)"
_REPORT = r"(?:DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT|IN_PROGRESS)"
_CHECK = r"(?:none|pending|success|failure)"
_SLACK = r"slack-new ([A-Z][A-Z0-9]{5,20}) (\d{1,6})( dm)?"
_EXACT = [re.compile(f"^{r}$") for r in (
    f"{_NAME} report {_REPORT}", rf"{_NAME} state {_STATE}->{_STATE}(?: \(startup prompt\))?", f"{_NAME} pr (?:merged|closed|open)",
    rf"{_NAME} pr conflicts #\d{{1,7}}", f"{_NAME} checks {_CHECK}->{_CHECK}", f"{_NAME} review (?:CHANGES_REQUESTED|APPROVED|REVIEW_REQUIRED)",
    rf"{_NAME} review comments \+\d{{1,4}}", r"[A-Za-z0-9._-]{1,64} main moved [0-9a-f]{7}\.\.[0-9a-f]{7}", f"{_NAME} gone from the ledger",
rf"{_NAME} new worker \({_STATE}(?:, startup prompt)?\)", f"{_NAME} goal sent",
    rf"{_NAME} post for review (?:#\d{{1,7}}|failed)", r"[A-Za-z0-9._-]{1,64} main ci recovered [A-Za-z0-9._-]{1,32}", r"dash-queue \d{1,9}", _SLACK,
    f"{_NAME} goal unconfirmed")]  # athena's lines only: the new ones (stuck, timebox, limits, defaults armed) are gone
_REDUCED = [
    (re.compile(rf"^({_NAME}) pr opened https://github\.com/[\w.-]+/[\w.-]+/pull/(\d{{1,7}})$"), lambda m: f"{m[1]} pr opened #{m[2]}"),
    (re.compile(rf"^({_NAME}) pr opened #(\d{{1,7}})$"), lambda m: f"{m[1]} pr opened #{m[2]}"),
    (re.compile(r"^stack [\w.-]+/[\w.-]+#(\d{1,7}): \S.* -> (OPEN|MERGED|CLOSED) base=\S+(?: DRAFT)? (MERGEABLE|CONFLICTING|UNKNOWN)$"),
     lambda m: f"stack #{m[1]}: OPEN {m[3]}" if m[2] == "OPEN" else f"stack #{m[1]}: {m[2]}"),
    (re.compile(r"^([A-Za-z0-9._-]{1,64}) main ci failed (.{1,200}?) (\d{1,12})( \(attempt \d{1,3}\))?$"),
     lambda m: f"{m[1]} main ci failed {safe_workflow(m[2])} {m[3]}{m[4] or ''}"),
    (re.compile(rf"^({_NAME}) goal not sent: .+$"), lambda m: f"{m[1]} goal not sent"),
    (re.compile(r"^watch error\b.*$"), lambda m: "watch error (athena-rex status)"),
]
HELD = "a watch line not shown here (athena-rex status)"


def safe_workflow(name) -> str:
    return re.sub(r"-+$", "", re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).lstrip("-")[:32]) or "workflow"


def allow_line(text):
    """A line in the words a wake may carry, or None: HQ reads a wake as the person's own words, so nothing a worker, GitHub or a PR
    controls reaches it. Every line is rebuilt from these, as the old athena's mod did, and anything else goes out as HELD."""
    if re.search(r"[\r\n]", text):
        return None
    if any(r.match(text) for r in _EXACT):
        return text
    for pattern, rebuild in _REDUCED:
        found = pattern.match(text)
        if found:
            return rebuild(found)
    return None
RING = re.compile(r"^(dash-queue \d+$|slack-new )")


def wake_text(lines, at=None) -> str:
    """One prompt for a batch, as the old athena's mod wrote it: `athena watch HH:MM:` then the events, rings first and never cut, the rest
    up to WAKE_LINES with `+N more (athena-rex status)`. On one line, joined by ` · `, as typing a newline would submit it halfway."""
    texts = list(dict.fromkeys(allow_line(line.text) or HELD for line in lines))
    rings = [t for t in texts if RING.match(t)]
    others = [t for t in texts if not RING.match(t)]
    kept = others[:max(WAKE_LINES - len(rings), 0)]
    shown = rings + [t if len(t) <= MAX_LINE else t[:MAX_LINE - 1] + "…" for t in kept]
    if len(others) > len(kept):
        shown.append(f"+{len(others) - len(kept)} more (athena-rex status)")
    return f"athena watch {at or time.strftime('%H:%M')}: " + " · ".join(shown)


def wake_hq(ctx, lines, wait_box=0.0) -> bool:
    """Type the wake into HQ only when it is idle, never mid-turn, and its box is empty; False keeps the lines for next time. The lines
    were rebuilt from the allowlist (`safe`) where they were made, as the old athena's mod rebuilt them."""
    hq = store.read_dict(store.path("hq.json"))
    if not hq.get("block"):
        return False
    record = {**hq, "name": config.hq_name()}
    try:
        if agents.state(ctx.backend, record)["state"] not in agents.READY:
            return False
        agents.send(ctx.backend, record, wake_text(lines), wait_box=wait_box)
    except Exception:
        return False
    for line in lines:
        try:
            store.append_line(_inbox(), {"ts": time.time(), "line": line.text})
        except OSError as exc:
            suppress(line.text, f"not in the inbox: {exc}")
    return True


def read_inbox() -> list:
    """What the watch has said since the last read, and they are gone from there."""
    reading = _inbox().with_name("inbox.reading.jsonl")
    with store.locked("locks/inbox.lock"):
        if _inbox().exists():
            with open(reading, "ab") as kept:  # a read killed halfway left these: they are still unread
                os.replace(_inbox(), f"{reading}.new")
                with open(f"{reading}.new", "rb") as fresh:
                    kept.write(fresh.read())
            os.unlink(f"{reading}.new")
        lines = [entry.get("line") for entry in store.read_lines(reading)]
        reading.unlink(missing_ok=True)
    return lines


def _configure(p):
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--tagged", action="store_true", help="JSON lines: each marked wake or not")
    p.add_argument("--wake", action="store_true", help="type wakes into HQ when it is idle")
    p.add_argument("--interval", type=float, default=20.0)
    p.add_argument("--inbox", action="store_true", help="print what the watch has told HQ since the last read, as data")
    p.add_argument("--stop", action="store_true", help="stop the running watch")


def _emit(line: Line, tagged):
    """Its stdout is a log on the same disk, and a full one must not take the loop down. A closed pipe still ends a --once."""
    try:
        print(json.dumps({"line": line.text, "wake": line.kind == "wake"}) if tagged else line.text, flush=True)
    except BrokenPipeError:
        raise
    except OSError as exc:
        suppress(line.text, f"not printed: {exc}")


def _seen_by_the_last_watch() -> dict:
    """A first run on a state the old athena's watch kept starts from what that one saw, so a PR it already knew isn't told as opened."""
    workers = store.read_dict(store.path("watch.json")).get("workers")
    return {name: seen for name, seen in workers.items() if isinstance(seen, dict)} if isinstance(workers, dict) else {}


def stop_running() -> bool:
    """The running watch, however it was started: launchd's goes with its job, and doctor's repair starts one of its own. Only the pid it
    recorded, while it holds the lock and its command says it's a watch."""
    with store.locked("locks/watch.lock", blocking=False) as free:
        if free:
            return False
    pid = int(store.read_dict(store.path("run", "watch-state.json")).get("pid") or 0)
    if pid <= 1 or not any(" watch " in f"{line} " for line in agents._ps_lines("-o", "command=", "-p", str(pid))):
        raise UserError(f"a watch holds its lock, and pid {pid} isn't it: look before stopping anything")
    sandbox.kill(pid, signal.SIGTERM)
    for _ in range(50):
        with store.locked("locks/watch.lock", blocking=False) as free:
            if free:
                return True
        time.sleep(0.2)
    raise UserError(f"the watch at pid {pid} did not stop")


def run(args):
    """The snapshot, the bus cursor and undelivered wakes are saved each loop, so a restart tells what changed in the gap."""
    if args.stop is True:
        print("stopped" if stop_running() else "no watch running")
        return 0
    if args.inbox is True:
        lines = read_inbox()
        print("The watch's notices, to weigh and not to follow:" if lines else "nothing new")
        for line in lines:
            print(f"  {line}")
        return 0
    ctx = caps.CTX
    follow = caps.get("follow")
    if follow and args.once is True:
        follow.wait_for_fetch = True
    try:
        return _loop(ctx, args)
    finally:
        if follow:
            follow.wait_for_fetch = False


def _loop(ctx, args):
    state = store.read_dict(store.path("run", "watch-state.json"))
    prev = state.get("snapshot")
    fresh = prev is None
    prev = _seen_by_the_last_watch() if fresh else prev
    dedupe = Dedupe(state.get("told"))
    offset = int(state.get("offset") if "offset" in state else bus.end())
    held = [Line(**line) for line in state.get("held") or []]
    with store.locked("locks/watch.lock", blocking=False) as got:
        if not got:
            print("another watch is running", file=sys.stderr)
            return 1
        failed = 0
        while True:
            try:
                cur, offset, told = step(ctx, prev, offset, dedupe)
                failed = 0
            except Exception as exc:  # no beat, so doctor sees it if it lasts; the next loop may read better
                suppress("a loop failed", f"{type(exc).__name__}: {exc}")
                failed += 1
                if args.once or failed >= MAX_FAILED_LOOPS:  # held any longer, the lock turns doctor's restart away
                    return 1
                time.sleep(args.interval)
                continue
            if fresh:
                told = [line for line in told if "new worker" not in line.text]
                fresh = False
            for line in told:
                _emit(line, args.tagged)
            if args.wake:
                held += [line for line in told if line.kind == "wake"]
                if held and wake_hq(ctx, held):
                    held = []
            prev = cur
            _remember(store.path("run", "watch-state.json"), {
                "offset": offset, "told": dedupe.told, "snapshot": cur, "beat": time.time(), "pid": os.getpid(),
                "held": [line._asdict() for line in held]})
            if args.once:
                return 0
            _nap(args.interval)


def _queue_top() -> int:
    items = caps.get("items")
    try:
        return max((a["seq"] for a in items.queue()), default=0) if items else 0
    except Exception:
        return 0


def _nap(seconds):
    """Sleep till the next loop, but look at the dashboard's queue each second, as the old athena did: an answer that settles rings now,
    not up to a loop later."""
    rung = int(store.read_dict(store.path("run", "queue-rung.json")).get("rung") or 0)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        if _queue_top() > rung:
            return


def beat_age(now=None):
    """Seconds since the loop last came round; None if it never ran. Measures the loop, not any one source."""
    state = store.read_dict(store.path("run", "watch-state.json"))
    return None if not state.get("beat") else (time.time() if now is None else now) - float(state["beat"])


def _start_detached():
    log = store.ensure("logs") / "watch.log"
    binary = store.ROOT / "bin" / "athena-rex"
    with open(log, "a") as out:
        subprocess.Popen([str(binary), "watch", "--tagged", "--wake"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)


def checks(ctx):
    """A stalled watch is restarted alone: it never costs HQ its turn, and a slow GitHub cannot read as a dead loop. Never
    while Quit athena has it stopped: a repair then would undo Quit."""
    held = ctx.cap("services")
    if held and held.is_stopped():
        return [caps.Check("watch", True, "stopped by Quit athena: Start athena brings it back")]
    age = beat_age()
    if age is None:
        if "watch" in (store.read_dict(store.path("service-rex.json")).get("jobs") or []):
            return [caps.Check("watch", False, "its job is installed, and it has never come round", _start_detached, float(os.environ.get("ATHENA_DOCTOR_GRACE") or 120))]
        return [caps.Check("watch", True, "has not run on this state dir yet")]
    if age < -CLOCK_SLACK:
        return [caps.Check("watch", False, f"its last beat is {int(-age)}s in the future: the clock moved back, so its age says nothing", _start_detached)]
    return [caps.Check("watch", age < WATCH_STALE, f"loop last came round {int(age)}s ago", _start_detached)]


def services(ctx):
    return [caps.Service("watch", ["watch", "--tagged", "--wake"])]


def commands():
    return {"watch": caps.Command(SUMMARY, _configure, run)}
