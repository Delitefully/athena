"""The dash's server, as the old athena dash's (its dash/server.go, actions.go, items.go, itemview.go and preview.go): it keeps the
last rendered <main>, pushes each new one to every open page over Server-Sent Events, and writes only what the human does on it:
answers appended to actions.jsonl, Done and Snooze in needs-you.json, and dismissals in dismissed-prs.json. It runs nothing.
Loopback only, a Host check against DNS rebinding, and a per-start token on anything that writes."""
import json
import mimetypes
import os
import queue
import re
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from athena.caps.dash import markdown, page, state, view
from athena.caps.dash.state import ASK_ID, PR_KEY_TEXT, UNDO_DEFAULT, Locked, clip, pr_key

TEXT_MAX = 2000  # characters of a reply, as the reply box counts them
BODY_MAX = 16 << 10  # bytes of a POST
SETTLE_AFTER = 6  # seconds an answer can still be undone; `item queue` and the doorbell see only older ones
SNOOZE_MAX = 60 * 24 * 3600
DISMISS_AGE = 30 * 24 * 3600
SEARCH_MAX = 50
FLAME_SCOPE = re.compile(r"^(athena|hq|workers|all|worker:[A-Za-z0-9._+:/@-]{1,40}|ticket:[A-Za-z0-9._+:/@-]{1,40})$")
FLAME_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREVIEWS = "previews"
PREVIEW_CSP = "sandbox allow-scripts"
HEAD_TEXT = re.compile(r"^[0-9a-f]{7,40}$")
_TIME_TEXT = re.compile(r"(<time [^>]*>)[^<]*(</time>)")


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


@dataclass
class Preview:
    name: str
    url: str


@dataclass
class FullView:
    """An item's own page: the human's words, where it stands, the brief, its previews, workers and PR, and its timeline."""
    ask: state.Ask
    kind: str = ""
    state: str = ""
    call: str = ""
    body: str = ""
    previews: List[Preview] = field(default_factory=list)
    workers: List[view.Item] = field(default_factory=list)
    pr: Optional[view.PRView] = None
    timeline: list = field(default_factory=list)
    actions: Optional[view.Actions] = None
    auto_at: float = 0.0
    done: bool = False
    snooze: bool = False
    reopen: bool = False
    wake: bool = False
    archived: bool = False


@dataclass
class SearchHit:
    id: str
    name: str
    kind: str
    state: str
    call: str
    when: float


def without_times(html: str) -> str:
    """The "4 min ago" texts left out, as the page's own timer keeps them current."""
    return _TIME_TEXT.sub(r"\1\2", html)


def _live_state_dir():
    try:
        from athena.kernel import store
        return store.state_dir()
    except Exception:
        return None


class Server:
    def __init__(self, state_dir, port: int, linear_workspace: str = "", clock=time.time, athena=None):
        """`athena`: read athena's own knowledge of workers and its Rex sessions too. On by default for the live state dir."""
        self.store = state.Store(state_dir)
        self.port = port
        self.linear_workspace = linear_workspace
        self.now = clock
        self.token = secrets.token_hex(16)
        self.boot = format(time.time_ns(), "x")
        self.athena = (Path(state_dir) == _live_state_dir()) if athena is None else athena
        if self.athena:
            self.store.extra = ["run/sessions.json", "run/follow.json"]
        self._refreshing = threading.Lock()
        self._dismissing = threading.Lock()
        self._mu = threading.Lock()
        self.main = ""
        self.view = view.View()
        self.version = 0
        self.clients = set()
        self.done = threading.Event()

    # Reading and pushing

    def load(self) -> state.Snapshot:
        snap = self.store.load()
        if self.athena:
            _overlay(snap)
        return snap

    def refresh(self, force=False) -> bool:
        """Re-reads the state when force is set or a file changed, and pushes the result if the page differs. A failure while
        building or rendering keeps the last good page."""
        with self._refreshing:
            try:
                if not self.store.changed() and not force:
                    return False
                now = self.now()
                snap = self.load()
                if self.apply_defaults(snap, now) > 0:
                    snap = self.load()
                v = view.build(snap, now, self.linear_workspace)
                if self.athena:
                    _athena_rows(v, snap, now)
                html = page.Page(now).main(v)
            except Exception as exc:  # noqa: BLE001: the page stays up
                print(f"render failed, keeping the last good page: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                return False
            with self._mu:
                if without_times(html) == without_times(self.main) and self.version:
                    self.view = v  # the same page, but answers are checked against the newest view
                    return False
                self.main, self.view = html, v
                self.version += 1
                for c in list(self.clients):
                    _offer(c, html)
                return True

    def poll(self, interval=1.0):
        last = time.monotonic()
        while not self.done.wait(interval):
            force = time.monotonic() - last >= 60
            if force:
                last = time.monotonic()
            self.refresh(force)

    def stop(self):
        self.done.set()

    def snapshot(self):
        with self._mu:
            return self.main, self.view, self.version

    # The human's answers

    def append_action(self, make):
        """Appends the line `make(current, max_seq)` builds, under ask.lock, and syncs it."""
        path = self.store.dir / state.ACTIONS
        with Locked(self.store.dir):
            cur, top = read_actions(path)
            line = make(cur, top)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n").encode())  # keys sorted, as Go's encoder
                os.fsync(fd)
            finally:
                os.close(fd)
            return line

    def apply_defaults(self, snap, now) -> int:
        """Queues each due default's recommended option for athena, as the human's own answer would be, marked auto."""
        n = 0
        for a in view.due_defaults(snap, now):
            def make(cur, top, a=a):
                if any(c.item == a.id and c.status != "cancelled" for c in cur.values()):
                    raise HTTPError(409, "answered meanwhile")
                return {"seq": top + 1, "ts": now, "item": a.id, "option": a.rec, "auto": True, "status": "queued"}
            try:
                self.append_action(make)
                n += 1
            except HTTPError:
                pass
            except OSError as exc:
                print(f"default for {a.id} not applied: {exc}", file=sys.stderr, flush=True)
        return n

    def take_action(self, req: dict) -> int:
        allowed = {"item": str, "option": str, "head": str, "text": (str, type(None)), "discuss": bool, "rule": bool, "seq": int,
                   "undo": bool}
        if not _fits(req, allowed):
            raise HTTPError(400, "not an answer")
        item, option, head = req.get("item", ""), req.get("option", ""), req.get("head", "")
        text_given = req.get("text") is not None
        discuss, rule, seq, undo = req.get("discuss", False), req.get("rule", False), req.get("seq", 0), req.get("undo", False)
        now = self.now()
        if item.startswith(view.PILL_ITEM) and not PR_KEY_TEXT.match(item[len(view.PILL_ITEM):]):
            raise HTTPError(400, "not a PR")
        if undo and seq == 0 and item.startswith(view.PILL_ITEM):
            if option or text_given or discuss:
                raise HTTPError(400, "undo names a seq, or a dismissed PR")
            self.undismiss(item[len(view.PILL_ITEM):])
            return 0
        if undo:
            if seq <= 0 or item or option or text_given or discuss or rule:
                raise HTTPError(400, "undo names a seq only")

            def cancel(cur, _top):
                a = cur.get(seq)
                if a is None or a.status != "queued":
                    raise HTTPError(409, "athena already has it")
                if now - a.ts >= SETTLE_AFTER:
                    raise HTTPError(409, "too late to undo")
                return {"seq": seq, "ts": now, "status": "cancelled"}
            self.append_action(cancel)
            return seq
        text = ""
        if text_given:
            text = req["text"].strip()
            if not text or len(text) > TEXT_MAX:
                raise HTTPError(400, f"a reply is 1 to {TEXT_MAX} characters")
        if sum((bool(option), text_given, bool(discuss))) != 1 or seq != 0:
            raise HTTPError(400, "one answer per POST")
        if head and (option != "go" or not HEAD_TEXT.match(head)):
            raise HTTPError(400, "a head goes with a go only")
        _, v, _ = self.snapshot()
        acts = v.actionable.get(item)
        if acts is None or not acts.allows(option, text, discuss):
            raise HTTPError(422, "the page does not offer that answer")
        if rule and (not acts.rule or discuss):
            raise HTTPError(422, "don't ask again goes with an answer to a decision")
        if option == "go" and (not acts.head or head != acts.head):
            raise HTTPError(409, "the PR moved on: look again")
        pill = v.pills.get(item)
        if pill is not None and option == "dismiss":
            self.dismiss(pill, now)
            return 0

        def queue_it(cur, top):
            # The page check ran outside the lock: an item a second tab answered meanwhile is refused here, and so is a PR's button
            # answer while one of them is queued for it. An item shown again as stuck takes a new one; a worker's row takes any number.
            for a in cur.values():
                if (acts.hides or view.SENT_TEXT.get(option) and view.SENT_TEXT.get(a.option)) and a.item == item and \
                        a.status == "queued" and now - a.ts < view.STUCK_AFTER:
                    raise HTTPError(409, "already answered")
            line = {"seq": top + 1, "ts": now, "item": item, "status": "queued"}
            if rule:
                line["rule"] = True
            if discuss:
                line["discuss"] = True
            elif text_given:
                line["text"] = text
            else:
                line["option"] = option
                if option == "go":
                    line["head"] = head
            return line
        return self.append_action(queue_it)["seq"]

    def _edit_dismissed(self, edit, now):
        with self._dismissing:
            path = self.store.dir / state.DISMISSED
            try:
                data = json.loads(path.read_bytes())
            except (OSError, ValueError):
                data = {}  # a dismissal is a convenience: an unreadable file starts over
            found = data.get("prs") if isinstance(data, dict) and isinstance(data.get("prs"), dict) else {}
            prs = {k: d for k, d in found.items() if isinstance(d, dict) and PR_KEY_TEXT.match(k) and now - state.n_(d, "ts") < DISMISS_AGE}
            edit(prs)
            state.write_private(path, (json.dumps({"prs": prs}, indent=2, sort_keys=True) + "\n").encode())

    def dismiss(self, p, now):
        """Off the Awaiting review row as it is now, until its head or review state changes. Never told to athena."""
        self._edit_dismissed(lambda prs: prs.__setitem__(p.key, {"head": p.head, "review": p.label, "ts": now}), now)

    def undismiss(self, key):
        def edit(prs):
            if key not in prs:
                raise HTTPError(409, "not dismissed")
            del prs[key]
        self._edit_dismissed(edit, self.now())

    def take_item_action(self, req: dict):
        """Done, Snooze, Reopen and Wake: the dash's own, written into needs-you.json under ask.lock. Never queued for athena."""
        if not _fits(req, {"item": str, "do": str, "until": (int, float)}) or not ASK_ID.match(req.get("item", "")):
            raise HTTPError(400, "not an item action")
        now = self.now()
        what, until = req.get("do", ""), float(req.get("until", 0) or 0)
        if what in ("done", "reopen", "wake"):
            if until != 0:
                raise HTTPError(400, "until goes with snooze")
        elif what == "snooze":
            if until <= now or until > now + SNOOZE_MAX:
                raise HTTPError(400, "snooze until a time in the next 60 days")
        else:
            raise HTTPError(400, "done, snooze, reopen or wake")

        def edit(it):
            open_ = not state.n_(it, "resolved")
            if what == "done":
                if not open_:
                    raise HTTPError(409, "done already")
                it.update(resolved=int(now), resolved_by="you", resolved_note="you marked it done")
                it.pop("snooze", None)
                it.pop("auto", None)
                _log(it, now, "you marked it done")
            elif what == "reopen":
                if open_ or state.s_(it, "resolved_by") != "you":
                    raise HTTPError(409, "only an item you marked done reopens here")
                for k in ("resolved", "resolved_by", "resolved_note"):
                    it.pop(k, None)
                _log(it, now, "you reopened it")
            elif what == "snooze":
                if not open_:
                    raise HTTPError(409, "it is resolved")
                it["snooze"] = {"until": int(until), "at": int(now) + 1}
                when = datetime.fromtimestamp(until)
                _log(it, now, f"you snoozed it until {when.strftime('%a')} {when.day} {when.strftime('%b %H:%M')}")
            elif what == "wake":
                if not isinstance(it.get("snooze"), (dict, int, float)) or isinstance(it.get("snooze"), bool):
                    raise HTTPError(409, "not snoozed")
                it.pop("snooze", None)
                _log(it, now, "snooze cancelled")
            it["updated"] = int(now)
        self.edit_item(req["item"], edit)

    def edit_item(self, item_id, edit):
        """One item of needs-you.json rewritten under ask.lock, atomically and private, every other item and field kept. The last
        generation is kept beside it, as athena's own writer keeps it."""
        path = self.store.dir / state.ASKS
        with Locked(self.store.dir):
            try:
                raw = path.read_bytes()
            except OSError:
                raise HTTPError(404, "no such item")
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise HTTPError(409, "needs-you.json is unreadable")
            for it in data.get("items") if isinstance(data.get("items"), list) else []:
                if isinstance(it, dict) and state.s_(it, "id") == item_id:
                    edit(it)
                    state.write_private(path.with_name(state.ASKS + ".prev"), raw)
                    state.write_private(path, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode())
                    return
            raise HTTPError(404, "no such item")

    # Pages

    def full_view(self, item_id) -> Optional[FullView]:
        snap = self.load()
        _, v, _ = self.snapshot()
        now = self.now()
        a = next((x for x in snap.asks if x.id == item_id), None)
        archived = False
        if a is None:
            for old in state.read_archive(self.store.dir):
                if old.id == item_id:
                    a, archived = old, True
        if a is None:
            return None
        fv = FullView(ask=a, kind=view.kind_label(a), call=view.inline(a.text), archived=archived, timeline=view.timeline(a, snap.actions))
        if a.body:
            fv.body = markdown.render(a.body)
        if not archived:
            fv.previews = [Preview(name=n.rstrip("/"), url=f"/preview/{a.id}/{n}") for n in a.previews]
        if a.pr:
            fv.pr = view.PRView(url="https://github.com/" + a.pr.replace("#", "/pull/", 1), text=a.pr)
        rows = {it.id: it for group in (v.needs_you, v.ready, v.in_progress, v.done) for it in group}
        for w in a.workers:
            row = rows.get("w-" + w)
            row = view.Item(name=w, label="not live") if row is None else view.Item(**{**row.__dict__})
            if not row.title:
                row.title = row.name
            fv.workers.append(row)
        clock = page.Page(now)
        if not a.open():
            fv.state = "resolved " + clock.ago(a.resolved) + (": " + a.resolved_note if a.resolved_note else "")
            fv.reopen = a.resolved_by == "you" and not archived
            return fv
        if a.snoozed(now):
            fv.state = "snoozed until " + clock.at(a.snooze_until)
            fv.wake = True
        elif a.applied is not None:
            fv.state = "athena took the rec on its own: " + a.applied.label
        else:
            fv.state = "open"
        fv.done, fv.snooze = True, not a.snoozed(now)
        fv.actions = v.actionable.get(a.id)
        if a.auto and a.applied is None:
            fv.auto_at = view.apply_at(a, snap.last_seen, now)
        return fv

    def search(self, q) -> list:
        """Items by their name, title, why, the human's words and how each resolved, open or archived, newest first."""
        if not q:
            return []
        words = q.lower().split()
        snap = self.store.load()
        hits, seen = [], set()
        for a in snap.asks + state.read_archive(self.store.dir):
            if a.id in seen:
                continue
            seen.add(a.id)
            hay = " ".join([a.id, a.name, a.text, a.why, a.words, a.resolved_note, a.pr, " ".join(a.workers)]).lower()
            if all(w in hay for w in words):
                hits.append(SearchHit(a.id, a.name, view.kind_label(a), "open" if a.open() else "resolved", view.inline(a.text), a.updated))
        hits.sort(key=lambda h: -h.when)
        return hits[:SEARCH_MAX]


def _fits(req, allowed) -> bool:
    """Go's strict decode: an object, no unknown field, each field of its type (an int not a bool, a whole number for an int)."""
    if not isinstance(req, dict):
        return False
    for k, v in req.items():
        kinds = allowed.get(k)
        if kinds is None:
            return False
        if isinstance(v, bool) and kinds is not bool and (not isinstance(kinds, tuple) or bool not in kinds):
            return False
        if kinds is int and isinstance(v, float) and v.is_integer():
            req[k] = int(v)
            continue
        if not isinstance(v, kinds):
            return False
    return True


def _log(it, now, text):
    log = it.get("log") if isinstance(it.get("log"), list) else []
    log.append({"ts": int(now), "text": text})
    it["log"] = log[-state.LOG_MAX:]


def read_actions(path):
    """actions.jsonl folded by seq, with the highest seq any line names (a compacted file keeps that line)."""
    lines, top = [], 0
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                top = max(top, int(state.n_(m, "seq")))
                lines.append(m)
    except FileNotFoundError:
        return {}, 0
    return {a.seq: a for a in state.fold_actions(lines)}, top


def _offer(c, html):
    """A page to a client without blocking: a slow client only ever gets the newest page."""
    try:
        c.put_nowait(html)
    except queue.Full:
        try:
            c.get_nowait()
        except queue.Empty:
            pass
        try:
            c.put_nowait(html)
        except queue.Full:
            pass


def _overlay(snap):
    """athena's own reading: each live worker's dead Claude, usage limit, stopped process or report from its transcript, which its
    hook file alone can't say; and the PRs it follows as its follow capability last found them (run/follow.json), which watch.json
    only copies once a watch loop."""
    from athena.kernel import store
    followed = store.read_dict(store.path("run", "follow.json"))
    if isinstance(followed.get("prs"), dict) or isinstance(followed.get("review_requests"), list):
        fresh = state.parse_watch({"prs": followed.get("prs") or {}, "review_requests": followed.get("review_requests") or []})
        if snap.watch is None:
            snap.watch = state.Watch(ts=state.n_(followed, "ts"))
        snap.watch.prs, snap.watch.review_requests = fresh.prs, fresh.review_requests
    try:
        from athena.kernel import caps
        status = caps.get("status")
        rows = status.collect() if status else []
    except Exception as exc:  # noqa: BLE001
        snap.notices.append(f"workers: {exc}")
        return
    for r in rows:
        if isinstance(r.get("session"), dict) and r["session"].get("id"):
            snap.sessions[r["name"]] = r["session"]
        if r.get("state_source") in ("limit", "process", "transcript", "filed") and r.get("state"):
            hook = snap.hooks.setdefault(r["name"], state.Hook())
            hook.state = r["state"]
            if r.get("state_source") in ("transcript", "filed") and isinstance(r.get("report"), dict):
                hook.report = state.parse_report(r["report"])


SESSION_KIND = {"hq": "the chief of staff", "orphan": "left behind", "other": "not athena's"}


def _athena_rows(v, snap, now):
    """athena's additions, kept by the human: each worker row names its Rex session (as `athena status` does), its Rex sessions
    are listed, and a note says while Claude's account differs. After the build, so a pill and a dismissal keep the plain label."""
    from athena.kernel import caps
    for group in (v.needs_you, v.in_progress, v.done):
        for it in group:
            session = snap.sessions.get(it.id[2:]) if it.id.startswith("w-") else None
            if session:
                name = session.get("label") or it.id[2:]
                it.label += f" · session “{name}”" + (" gone" if session.get("seen") is False else "")
    sessions = caps.get("sessions")
    seen = sessions.snapshot(now) if sessions else {}
    if sessions and seen.get("down"):
        v.notices.append(f"Rex didn't answer ({seen['down']}), so no sessions are listed")
    states = {w.name: (snap.hooks.get(w.name) or state.Hook()).state for w in snap.workers}
    order = ("hq", "worker", "orphan", "other")
    for s in sorted(seen.get("sessions") or [], key=lambda s: order.index(s["kind"]) if s.get("kind") in order else 9):
        kind = s.get("kind")
        if kind == "worker":
            label = f"{s.get('owner')} · {view.state_label(states.get(s.get('owner'), ''), None)}"
        else:
            label = SESSION_KIND.get(kind, kind or "") + (f" ({s.get('owner')})" if kind == "orphan" else "")
        v.sessions.append(view.Item(id="s-" + str(s.get("id", "")).rpartition(":")[2], name=str(s.get("label") or ""), label=label,
                                    icon="done" if kind == "orphan" else "idle" if kind == "other" else "working",
                                    badge=kind == "orphan", when=float(seen.get("ts") or now),
                                    session=str(s.get("id")) if kind != "other" else ""))
    limits = caps.get("limits")
    away = limits.unavailable() if limits else []
    if away:
        v.notices.append(f"Claude's account changed. Unavailable until `athena limits accept`: {', '.join(away)}")


class Handler(BaseHTTPRequestHandler):
    server_version = "athena-dash"
    protocol_version = "HTTP/1.1"
    dash: Server = None  # set per server by make_handler

    def log_message(self, *args):
        pass

    # Plumbing

    def _allowed_hosts(self):
        port = self.dash.port or self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _send(self, code, body, kind="text/html; charset=utf-8", headers=None, head_only=False):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if code >= 300 and self.command not in ("GET", "HEAD"):
            # A refused request's body may be unread, and would be taken for the next request line: Go's net/http closed it too.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not head_only and self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj) + "\n", "application/json", {"Cache-Control": "no-store"})

    def _not_found(self):
        self._send(404, "404 page not found\n", "text/plain; charset=utf-8")

    def end_headers(self):
        if getattr(self, "_guarded", False):
            host = self.headers.get("Host", "")
            scripts = "'self'" if host.startswith("[") else f"http://{host}/static/"
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if not getattr(self, "_is_preview", False):
                self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'self'; script-src {scripts}; img-src 'self' data:")
        super().end_headers()

    def _local(self) -> bool:
        """Only requests addressed to this server by its loopback name: a web page can't read it through DNS rebinding."""
        self._guarded = self._is_preview = False  # per request: a keep-alive connection reuses this handler
        if self.headers.get("Host", "") not in self._allowed_hosts():
            self._send(421, "athena dash answers on 127.0.0.1 only\n", "text/plain; charset=utf-8")
            return False
        self._guarded = True
        return True

    def _scheme(self, query):
        s = (query.get("scheme") or [""])[0]
        return s if s in ("light", "dark") else ""

    # Routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._local():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path)  # decoded, as Go's r.URL.Path is
        d = self.dash
        if path in ("/api/action", "/api/item", "/api/session"):
            return self._json(405, {"error": "POST only"})  # as the Go's checkPost, for a GET or a HEAD
        try:
            if path == "/":
                _, v, _ = d.snapshot()
                html = page.Page(d.now()).page(v, self._scheme(query), d.token, d.boot)
                return self._send(200, html, headers={"Cache-Control": "no-store"})
            if path == "/healthz":
                _, _, version = d.snapshot()
                return self._json(200, {"pid": os.getpid(), "version": version, "state": str(d.store.dir),
                                        "state_dir": str(d.store.dir), "hq_pid": 0, "start": d.boot})
            if path == "/events":
                return self._events()
            if path == "/glyphs":
                return self._send(200, page.Page.glyphs(self._scheme(query)), headers={"Cache-Control": "no-store"})
            if path.startswith("/item/"):
                item_id = path[len("/item/"):]
                fv = d.full_view(item_id) if ASK_ID.match(item_id) else None
                if fv is None:
                    return self._not_found()
                p = page.Page(d.now())
                html = p.item_main(fv) if (query.get("fragment") or [""])[0] == "1" else p.item_page(fv, self._scheme(query), d.token, d.boot)
                return self._send(200, html, headers={"Cache-Control": "no-store"})
            if path == "/items":
                q = (query.get("q") or [""])[0].strip()
                html = page.Page(d.now()).search_page(q, d.search(q), self._scheme(query), d.token, d.boot)
                return self._send(200, html, headers={"Cache-Control": "no-store"})
            if path.startswith("/preview/"):
                return self._preview(path)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path in ("/flame", "/flame/data", "/flame/status"):
                return self._flame(path, query)
        except Exception as exc:  # noqa: BLE001
            print(f"{path}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return self._send(500, "render failed\n", "text/plain; charset=utf-8")
        return self._not_found()

    def _flame(self, path, query):
        """The Flame page and its data, from the flame index (caps/flame); a 404 when that capability is off. Opening the page's
        data updates a stale index in the background, one at a time; /flame/status says when it has finished."""
        from athena.kernel import caps
        flame = caps.get("flame")
        if not flame:
            return self._not_found()
        if path == "/flame/status":
            return self._json(200, flame.status())
        if path == "/flame/data":
            pick = lambda key: (query.get(key) or [""])[0] or None  # noqa: E731
            scope, since, until = pick("scope") or "athena", pick("since"), pick("until")
            if not FLAME_SCOPE.match(scope) or any(day and not FLAME_DAY.match(day) for day in (since, until)):
                return self._json(400, {"error": "bad scope or date"})
            return self._json(200, flame.data(scope, since, until, refresh=True))
        from athena.caps.flame import page as flame_page
        d = self.dash
        p = page.Page(d.now())
        head = p.head("Flame · athena", self._scheme(query), d.token, d.boot, here="flame")
        head = head.replace("</head>", f'<link rel="stylesheet" href="/static/flame.css?v={page.e(d.boot)}">\n</head>', 1)
        foot = p.foot(d.boot).replace("</body>", f'<script src="/static/flame.js?v={page.e(d.boot)}"></script>\n</body>', 1)
        return self._send(200, f"{head}\n{flame_page.body()}\n{foot}", headers={"Cache-Control": "no-store"})

    def _static(self, name):
        target = page.STATIC / name
        from athena.kernel import caps
        flame = caps.get("flame") if name.startswith("flame.") else None
        if flame:
            from athena.caps.flame import page as flame_page
            target = flame_page.static_file(name) or target
        if "/" in name or name.startswith(".") or not target.is_file():
            return self._not_found()
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".woff2"):
            kind = "font/woff2"
        elif kind.startswith("text/") or name.endswith((".js", ".svg")):
            kind = {"js": "text/javascript", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], kind) + "; charset=utf-8"
        return self._send(200, target.read_bytes(), kind, {"Cache-Control": "max-age=3600"})

    def _preview(self, url_path):
        """A file of an item's preview, read-only and sandboxed with no allow-same-origin: its script reaches nothing of the dash.
        A hidden, empty or `..` segment, or a symlink anywhere along the path, is a 404; a folder shows its index.html."""
        self._is_preview = True
        h = {"Content-Security-Policy": PREVIEW_CSP, "Cache-Control": "no-store"}
        if self.command not in ("GET", "HEAD"):
            return self._send(405, "GET only\n", "text/plain; charset=utf-8", h)
        parts = url_path[len("/preview/"):].split("/")
        if len(parts) < 2 or not ASK_ID.match(parts[0]):
            return self._send(404, "404 page not found\n", "text/plain; charset=utf-8", h)
        dir_wanted = parts[-1] == ""
        if dir_wanted:
            parts = parts[:-1]
        for p in parts[1:]:
            if not p or p.startswith(".") or "\\" in p or "\x00" in p:
                return self._send(404, "404 page not found\n", "text/plain; charset=utf-8", h)
        full = str(self.dash.store.dir / PREVIEWS)
        try:
            st = os.lstat(full)
        except OSError:
            st = None
        for p in parts:
            if st is None or stat.S_ISLNK(st.st_mode):
                return self._send(404, "404 page not found\n", "text/plain; charset=utf-8", h)
            full = os.path.join(full, p)
            try:
                st = os.lstat(full)
            except OSError:
                st = None
        is_dir = st is not None and stat.S_ISDIR(st.st_mode)
        if is_dir:
            if not dir_wanted:
                return self._send(301, "", "text/html; charset=utf-8", {**h, "Location": url_path.rstrip("/").rsplit("/", 1)[-1] + "/"})
            full = os.path.join(full, "index.html")
            try:
                st = os.lstat(full)
            except OSError:
                st = None
        if st is None or not stat.S_ISREG(st.st_mode) or dir_wanted and not is_dir:
            return self._send(404, "404 page not found\n", "text/plain; charset=utf-8", h)
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return self._send(404, "404 page not found\n", "text/plain; charset=utf-8", h)
        with os.fdopen(fd, "rb") as f:
            data = f.read()
        kind = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        return self._send(200, data, kind, {**h, "Last-Modified": formatdate(st.st_mtime, usegmt=True)})

    def _events(self):
        d = self.dash
        c = queue.Queue(maxsize=1)
        with d._mu:
            d.clients.add(c)
            current = d.main
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # This start's id: a page from an earlier start (a restarted dash) sees another and reloads, for its token and files.
            self.wfile.write(f"retry: 2000\n\nevent: boot\ndata: {d.boot}\n\n".encode() + _event(current))
            self.wfile.flush()
            while not d.done.is_set():
                try:
                    html = c.get(timeout=25)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(_event(html))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with d._mu:
                d.clients.discard(c)

    def _check_post(self):
        """A POST from this server's own page, with a JSON body and the token of this start."""
        origin = self.headers.get("Origin", "")
        if not origin or origin != "http://" + self.headers.get("Host", ""):
            raise HTTPError(403, "this page's origin only")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPError(415, "JSON only")
        if not secrets.compare_digest(self.headers.get("X-Athena-Token", "").encode(), self.dash.token.encode()):
            raise HTTPError(403, "reload the page")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= BODY_MAX:  # a negative length would read to EOF; a longer body's rest would read as the next request
            self.close_connection = True
            raise HTTPError(400, "not an answer")
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            raise HTTPError(400, "not an answer")

    def do_POST(self):
        if not self._local():
            return
        path = unquote(urlparse(self.path).path)
        d = self.dash
        if path.startswith("/preview/"):
            return self._preview(path)  # its 405: the route only reads
        if path not in ("/api/action", "/api/item", "/api/session"):
            return self._not_found()
        try:
            req = self._check_post()
            if path == "/api/action":
                seq = d.take_action(req)
                d.refresh(True)
                return self._json(202, {"seq": seq})
            if path == "/api/item":
                d.take_item_action(req)
                d.refresh(True)
                return self._json(200, {"ok": True})
            return self._show_session(req)
        except HTTPError as exc:
            if exc.msg == "not an answer" and path == "/api/item":
                exc.msg = "not an item action"
            return self._json(exc.code, {"error": exc.msg})
        except OSError as exc:
            print(f"{path}: {exc}", file=sys.stderr, flush=True)
            return self._json(503, {"error": f"it could not be written: {exc}"})  # athena: why, not the old bare 500

    def _show_session(self, req):
        """athena's: show one of athena's Rex sessions, which Rex's Remote Control setting may refuse."""
        from athena.kernel import caps
        from athena.kernel.backend import BackendError
        wanted = str(req.get("session") or "") if isinstance(req, dict) else ""
        sessions = caps.get("sessions") if self.dash.athena else None
        if not sessions or not any(s.get("id") == wanted and s.get("kind") != "other" for s in sessions.snapshot().get("sessions") or []):
            raise HTTPError(409, "that session isn't one of athena's")
        try:
            caps.CTX.backend.focus(wanted)
        except BackendError as exc:
            raise HTTPError(409, f"Rex would not show it: {exc.message or exc.code}. Rex's Remote Control setting may be off.")
        return self._json(200, {"ok": True})

    def do_PUT(self):
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT  # a preflight too: never answered, so no other origin may POST JSON

    def _method_not_allowed(self):
        if self._local():
            self._json(405, {"error": "POST only"})


def _event(html: str) -> bytes:
    """One `main` event; every line of the HTML is its own data line, as SSE requires."""
    return ("event: main\n" + "".join(f"data: {line.rstrip(chr(13))}\n" for line in html.split("\n")) + "\n").encode()


def make_handler(dash: Server):
    return type("DashHandler", (Handler,), {"dash": dash})


class DashHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128  # the Go listener's backlog (SOMAXCONN): a burst of answers waits its turn, never a reset at connect


def start(state_dir, port: int, linear_workspace="", athena=None, poll=1.0, clock=time.time):
    """A server answering on 127.0.0.1:port (0 for any free one), its poll running: (dash, httpd). Tests and `serve` use it."""
    dash = Server(state_dir, port, linear_workspace, clock=clock, athena=athena)
    httpd = DashHTTPServer(("127.0.0.1", port), make_handler(dash))
    httpd.daemon_threads = True
    dash.port = httpd.server_address[1]
    dash.refresh(True)
    threading.Thread(target=dash.poll, args=(poll,), daemon=True).start()
    return dash, httpd


def _sessions_loop(dash):
    """The Rex sessions list stays current with no watch running, and no request ever waits on Rex."""
    from athena.kernel import caps
    sessions = caps.get("sessions")
    while sessions and not dash.done.is_set():
        try:
            sessions.refresh(caps.CTX)
        except Exception:  # noqa: BLE001
            pass
        dash.done.wait(sessions.REFRESH_EVERY)


def serve(port: int) -> int:
    from athena.kernel import store
    linear = os.environ.get("ATHENA_LINEAR_WORKSPACE", "")
    try:
        dash, httpd = start(store.state_dir(), port, linear, athena=True)
    except OSError as exc:
        print(f"port {port} is in use: {exc}; set ATHENA_DASH_PORT to another port", file=sys.stderr)
        return 3
    threading.Thread(target=_sessions_loop, args=(dash,), daemon=True).start()
    print(f"athena dash on http://127.0.0.1:{port}/ (pid {os.getpid()}, state {store.state_dir()})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
    return 0
