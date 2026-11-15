"""The watch: one loop that turns state changes into lines, and tells HQ the ones that matter.

Every loop it folds the workers into a snapshot, diffs it against the last one, reads new bus events, and asks each
capability for what it has (`tick`). A line is `wake`, `routine` or `quiet`, decided where it is made, not by parsing
it later. Lines are rebuilt from an allowlist before they go out: HQ reads a wake as the person's own words.
"""
import collections
import contextlib
import dataclasses
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import namedtuple

from athena.cli import UserError
from athena.kernel import agents, bus, caps, config, gitops, ledger, store, sandbox
from athena.kernel.backend import BackendError

SUMMARY = "one line per change, waking HQ for what matters"
WAKE_LINES = 8
REPEAT_AFTER = 600
STUCK_AFTER = float(os.environ.get("ATHENA_STUCK_SECONDS") or 1800)
_SAFE = re.compile(r"[^A-Za-z0-9_.#:/>+ ·-]")
_WORKER_STATES = {"blocked", "exited", "idle-unreported"}
WATCH_STALE = 300
CLOCK_SLACK = 60
MAX_FAILED_LOOPS = 10
CLOSE_SETTLE = 1.0  # a session kill's events come together: by then its session is gone, and that is no window close
CLOSE_WAIT = 60.0  # a closed window's Claude has this long to exit; it is never stopped by force
CLOSE_SLACK = 5.0  # how late a close may be heard after athena's own retire that made it gave up


Line = namedtuple("Line", "text kind worker key why", defaults=("routine", "", "", ""))  # why: a quiet line's reason, in the log


def safe(text: str) -> str:
    return _SAFE.sub("", str(text))[:160].strip() or "a watch line not shown here (athena status)"


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
        lines.append(Line(f"{name} bot comments #{now.get('number')}", "quiet", name,
                          why="comments or reviews by bots or the PR's author only; their inline threads ring as bot threads"))
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
    from athena.caps import status
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


def window_closed(event):
    """(session, window) of a Rex `window_closed` event, else None. A session killed says only that its blocks closed."""
    if not isinstance(event, dict) or event.get("type") != "session_event":
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    inner = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if inner.get("type") != "window_closed":
        return None
    space, window = payload.get("session_id"), (inner.get("payload") or {}).get("window_id")
    return (space, window) if isinstance(space, str) and isinstance(window, str) and space and window else None


DROP, HOLD, RETIRE, GIVE_UP = "drop", "hold", "retire", "give up"
MARK = {"retired": "retired", "unsaved work kept": "unsaved work", "retire failed": "retire failed"}


@dataclasses.dataclass
class Close:
    """A window closed in Rex, kept in run/closes.json until what came of it is told. `worker` is the live worker it was found to be;
    `verdict` (retire or give up) and `started` are saved together before it is acted on, `outcome` once that is known: a watch killed
    anywhere in between finishes it on restart, and runs a retire again only for a close whose verdict was retire."""
    session: str
    window: str
    heard: float
    worker: str = None
    started: float = None
    outcome: str = None
    verdict: str = None

    def line(self) -> Line:
        return Line(f"{self.worker} closed in Rex · {self.outcome}", "wake", self.worker)


class Closes:
    """Windows closed in Rex, heard on a thread of their own so a worker's is acted on within seconds, not a loop later."""

    def __init__(self, backend):
        self.backend = backend
        self.pending = []
        for kept in store.read_dict(_closes_file()).get("closes") or []:
            try:
                self.pending.append(Close(**kept))
            except TypeError:
                pass
        self.heard = collections.deque()
        self.stopped = threading.Event()

    def take(self, event, now=None):
        found = window_closed(event)
        if found:
            self.heard.append(Close(*found, time.time() if now is None else now))

    def waiting(self) -> bool:
        return bool(self.heard or self.pending)

    def held(self) -> set:
        """Workers whose window closed and whose close is not acted on yet: their snapshot stays as it was until it is, so an exit
        that turns out to be news (a close dropped after all) is still told."""
        return {close.worker for close in self.pending if close.worker and not close.outcome}

    def save(self):
        _remember(_closes_file(), {"closes": [dataclasses.asdict(close) for close in self.pending]})

    def follow(self):
        """Rex's stream for good: followed again after a pause when it ends (a server restart), a longer one each time it ends at once."""
        pause = 1.0
        while not self.stopped.is_set():
            began = time.monotonic()
            try:
                for event in self.backend.events(self.stopped):
                    self.take(event)
                    if self.stopped.is_set():
                        return
                if not self.stopped.is_set():
                    suppress("rex events ended")
            except NotImplementedError:
                return
            except Exception as exc:
                suppress("rex events stopped", f"{type(exc).__name__}: {exc}"[:200])
            pause = 1.0 if time.monotonic() - began > 60 else min(pause * 2, 60.0)
            self.stopped.wait(pause)

    def start(self):
        threading.Thread(target=self.follow, name="rex-events", daemon=True).start()

    def stop(self):
        """Ends the thread and the stream's own process with it, even one blocked waiting for Rex to say something."""
        self.stopped.set()
        try:
            self.backend.stop_events()
        except Exception as exc:
            suppress("rex events not stopped", f"{type(exc).__name__}: {exc}"[:200])

    def handle(self, ctx, now) -> list:
        """Acts on every close that is due, and returns the line of each whose outcome is known and not yet told (`told`)."""
        while self.heard:
            self.pending.append(self.heard.popleft())
        verdicts = {}
        for close in self.pending:
            if close.outcome:
                continue
            try:
                verdict = close.verdict if close.started and close.verdict in (RETIRE, GIVE_UP) else _look(ctx, close, now)
            except Exception as exc:  # told the old way, as ->exited; one close never stops the loop
                suppress(f"a window close not acted on {close.window}", f"{type(exc).__name__}: {exc}"[:200])
                verdict = DROP
            verdicts[id(close)] = verdict
            if verdict in (RETIRE, GIVE_UP):
                close.verdict, close.started = verdict, close.started or now
                self.save()
                close.outcome = _act(ctx, close, verdict, now)
                self.save()
        self.pending = [close for close in self.pending if verdicts.get(id(close)) != DROP]
        self.save()
        return [close.line() for close in self.pending if close.outcome]

    def told(self):
        """Its lines are out and on record: printed, and any wake held in the watch's state."""
        if any(close.outcome for close in self.pending):
            self.pending = [close for close in self.pending if not close.outcome]
            self.save()


def _closes_file():
    return store.path("run", "closes.json")


def _worker_of(space, window):
    """The live worker in that window, by the ledger. HQ's window is never a worker's."""
    if store.read_dict(store.path("hq.json")).get("window") == window:
        return None
    return next((w for w in ledger.workers().values() if w.get("space") == space and w.get("window") == window
                 and w.get("state") == "live" and ledger.managed(w)), None)


def _retiring_when(name, at) -> bool:
    """Whether a retire of athena's own was under way when the window closed. Retire can close a worker's block and then fail, and it
    takes its mark off as it fails, so a close it made can be heard once the worker reads as live again. A mark never taken off (a
    retire killed) speaks for RETIRING_EXPIRES, as everywhere else."""
    began = None
    for event in ledger.events():
        if event.get("name") != name or "retiring" not in event:
            continue
        if event["retiring"]:
            began = float(event["retiring"])
        elif began is not None:
            if began - 1 <= at <= float(event.get("ts") or 0) + CLOSE_SLACK:
                return True
            began = None
    return began is not None and began - 1 <= at < began + ledger.RETIRING_EXPIRES


def _killed_by_hand(space) -> bool:
    """A repo session that is gone, and athena still lists: the human killed it. One athena closes once its last window went, it forgets."""
    return space in store.read_dict(store.path("run", "repo-sessions.json")).values()


def _look(ctx, close, now) -> str:
    """DROP, HOLD, RETIRE or GIVE_UP for a close not acted on yet. Only a live worker's block that is gone was closed by the human:
    athena's own closes come from a retire or a failed launch, and the worker is retiring or failed by then, or was while it closed; a
    pane moved to another window keeps its block; a session the human killed says no window closed, and is gone, still listed."""
    worker = _worker_of(close.session, close.window)
    if not worker or ledger.is_retiring(worker, now) or worker.get("closed_by_human") or not ctx.cap("retire"):
        return DROP
    name = close.worker = worker["name"]
    if _retiring_when(name, float(close.heard)):
        return DROP
    waited = now - float(close.heard)
    if waited < CLOSE_SETTLE:
        return HOLD
    try:
        if close.session not in {s["id"] for s in ctx.backend.spaces()}:
            if _killed_by_hand(close.session):
                return DROP
        elif ctx.backend.has_block(agents.place_of(worker), strict=True):
            return DROP
        alive = agents.stat_of(name)
    except BackendError as exc:  # agents.Unknown too: nobody can say, so nothing is touched, and it is never a retire
        if waited < CLOSE_WAIT:
            return HOLD
        suppress(f"{name} window closed, Rex or ps could not say more", str(exc)[:200])
        return GIVE_UP
    if alive:
        return HOLD if waited < CLOSE_WAIT else GIVE_UP
    return RETIRE


def _act(ctx, close, verdict, now) -> str:
    """The outcome, and only then the worker's mark and its items. The ordinary retire, never --force: its refusal over unsaved work
    is the guard, and it backs the head up first. What retire prints goes to the suppressed log, never onto the watch's own output."""
    name = close.worker
    worker = ledger.get(name) or {}
    if verdict == GIVE_UP:
        outcome = "retire failed"
    elif worker.get("state") == "retired":
        outcome = "retired"  # a retire finished before a restart could record it
    else:
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said):
                ctx.cap("retire").retire(ctx, name)
            outcome = "retired"
        except Exception as exc:
            suppress(f"{name} closed in Rex, not retired", f"{type(exc).__name__}: {exc}"[:300])
            path = worker.get("path")
            unsaved = path and os.path.isdir(path) and gitops.holds_work(gitops.unsaved(path))
            outcome = "unsaved work kept" if unsaved else "retire failed"
        if said.getvalue().strip():
            suppress(f"{name} retire said", safe(said.getvalue().replace("\n", " ")))
    ledger.append("update", name, closed_by_human=MARK[outcome])
    _turn_items(ctx, name, now)
    return outcome


def _turn_items(ctx, name, now):
    found = ctx.cap("items")
    if found:
        try:
            found.window_closed(name, now)
        except Exception as exc:
            suppress(f"{name} items not updated", f"{type(exc).__name__}: {exc}"[:200])


def step(ctx, prev, offset, dedupe, now=None, closes=None) -> tuple:
    """One loop, touching only files and the backend's read side, and retiring a worker whose window the human closed: (snapshot,
    new bus offset, lines to tell). Closes come after the snapshot and before the diff, so a closed worker's exit is never told."""
    now = time.time() if now is None else now
    cur = snapshot(ctx, prev, now)
    lines = closes.handle(ctx, now) if closes is not None else []
    for name in closes.held() if closes is not None else ():
        if name in prev:
            cur[name] = prev[name]
        else:
            cur.pop(name, None)
    retiring = {w["name"] for w in ledger.workers().values() if ledger.is_retiring(w, now) or w.get("state") == "retired"
                or (w.get("state") == "live" and w.get("closed_by_human"))}
    lines += list(diff(prev, cur, retiring))
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
            suppress(safe(line.text), line.why)
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
_SLACK = r"slack-new ([A-Z][A-Z0-9]{5,20}) (\d{1,6})( dm)?( thread \d{1,12}(?:\.\d{1,6})?)?"
_EXACT = [re.compile(f"^{r}$") for r in (
    f"{_NAME} report {_REPORT}", rf"{_NAME} state {_STATE}->{_STATE}(?: \(startup prompt\))?", f"{_NAME} pr (?:merged|closed|open)",
    rf"{_NAME} pr conflicts #\d{{1,7}}", f"{_NAME} checks {_CHECK}->{_CHECK}", f"{_NAME} review (?:CHANGES_REQUESTED|APPROVED|REVIEW_REQUIRED)",
    rf"{_NAME} review comments \+\d{{1,4}}", r"[A-Za-z0-9._-]{1,64} main moved [0-9a-f]{7}\.\.[0-9a-f]{7}", f"{_NAME} gone from the ledger",
rf"{_NAME} new worker \({_STATE}(?:, startup prompt)?\)", f"{_NAME} goal sent",
    rf"{_NAME} post for review (?:#\d{{1,7}}|failed)", r"[A-Za-z0-9._-]{1,64} main ci recovered [A-Za-z0-9._-]{1,32}", r"dash-queue \d{1,9}", _SLACK,
    rf"{_NAME} bot threads \+\d{{1,4}} #\d{{1,7}}", r"[\w.-]{1,100}/[\w.-]{1,100}#\d{1,7} bot threads \+\d{1,4}",
    f"{_NAME} goal unconfirmed", rf"{_NAME} closed in Rex · (?:retired|unsaved work kept|retire failed)")]  # athena's lines only: the new ones (stuck, timebox, limits, defaults armed) are gone
_REDUCED = [
    (re.compile(rf"^({_NAME}) pr opened https://github\.com/[\w.-]+/[\w.-]+/pull/(\d{{1,7}})$"), lambda m: f"{m[1]} pr opened #{m[2]}"),
    (re.compile(rf"^({_NAME}) pr opened #(\d{{1,7}})$"), lambda m: f"{m[1]} pr opened #{m[2]}"),
    (re.compile(r"^stack [\w.-]+/[\w.-]+#(\d{1,7}): \S.* -> (OPEN|MERGED|CLOSED) base=\S+(?: DRAFT)? (MERGEABLE|CONFLICTING|UNKNOWN)$"),
     lambda m: f"stack #{m[1]}: OPEN {m[3]}" if m[2] == "OPEN" else f"stack #{m[1]}: {m[2]}"),
    (re.compile(r"^([A-Za-z0-9._-]{1,64}) main ci failed (.{1,200}?) (\d{1,12})( \(attempt \d{1,3}\))?$"),
     lambda m: f"{m[1]} main ci failed {safe_workflow(m[2])} {m[3]}{m[4] or ''}"),
    (re.compile(rf"^({_NAME}) goal not sent: .+$"), lambda m: f"{m[1]} goal not sent"),
    (re.compile(r"^watch error\b.*$"), lambda m: "watch error (athena status)"),
]
HELD = "a watch line not shown here (athena status)"


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
    up to WAKE_LINES with `+N more (athena status)`. On one line, joined by ` · `, as typing a newline would submit it halfway."""
    texts = list(dict.fromkeys(allow_line(line.text) or HELD for line in lines))
    rings = [t for t in texts if RING.match(t)]
    others = [t for t in texts if not RING.match(t)]
    kept = others[:max(WAKE_LINES - len(rings), 0)]
    shown = rings + [t if len(t) <= MAX_LINE else t[:MAX_LINE - 1] + "…" for t in kept]
    if len(others) > len(kept):
        shown.append(f"+{len(others) - len(kept)} more (athena status)")
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
    try:
        sandbox.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise UserError(f"the watch at pid {pid} could not be signalled: {exc}")
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


def _terminated(signum, frame):
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _following(closes, on):
    """Rex's stream is followed only by a watch that loops, and no longer than it does: a TERM (launchd, `watch --stop`) ends the loop
    as an exit does, so the stream's own process, in a session of its own, goes with it."""
    previous = None
    if on:
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, _terminated)
        closes.start()
    try:
        yield
    finally:
        closes.stop()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _loop(ctx, args):
    state = store.read_dict(store.path("run", "watch-state.json"))
    prev = state.get("snapshot")
    fresh = prev is None
    prev = _seen_by_the_last_watch() if fresh else prev
    dedupe = Dedupe(state.get("told"))
    offset = int(state.get("offset") if "offset" in state else bus.end())
    held = [Line(**line) for line in state.get("held") or []]
    closes = Closes(ctx.backend)
    with store.locked("locks/watch.lock", blocking=False) as got, _following(closes, got and not args.once):
        if not got:
            print("another watch is running", file=sys.stderr)
            return 1
        failed = 0
        while True:
            try:
                cur, offset, told = step(ctx, prev, offset, dedupe, closes=closes)
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
            closes.told()
            if args.once:
                return 0
            _nap(args.interval, closes)


def _queue_top() -> int:
    items = caps.get("items")
    try:
        return max((a["seq"] for a in items.queue()), default=0) if items else 0
    except Exception:
        return 0


def _nap(seconds, closes=None):
    """Sleep till the next loop, but look at the dashboard's queue each second, as the old athena did: an answer that settles rings now,
    not up to a loop later. A window closed in Rex ends it too, after the moment a session kill needs to show itself."""
    rung = int(store.read_dict(store.path("run", "queue-rung.json")).get("rung") or 0)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        if _queue_top() > rung:
            return
        if closes is not None and closes.waiting():
            time.sleep(CLOSE_SETTLE)
            return


def beat_age(now=None):
    """Seconds since the loop last came round; None if it never ran. Measures the loop, not any one source."""
    state = store.read_dict(store.path("run", "watch-state.json"))
    return None if not state.get("beat") else (time.time() if now is None else now) - float(state["beat"])


def _start_detached():
    log = store.ensure("logs") / "watch.log"
    binary = store.ROOT / "bin" / "athena"
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
            return [caps.Check("watch", False, "its job is installed, and it has never come round", _start_detached, float(os.environ.get("ATHENA_DOCTOR_GRACE") or 120), short="never came round")]
        return [caps.Check("watch", True, "has not run on this state dir yet", short="not run yet")]
    if age < -CLOCK_SLACK:
        return [caps.Check("watch", False, f"its last beat is {int(-age)}s in the future: the clock moved back, so its age says nothing", _start_detached)]
    return [caps.Check("watch", age < WATCH_STALE, f"loop last came round {int(age)}s ago", _start_detached)]


def services(ctx):
    return [caps.Service("watch", ["watch", "--tagged", "--wake"])]


def commands():
    return {"watch": caps.Command(SUMMARY, _configure, run)}
