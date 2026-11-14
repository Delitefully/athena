"""The snapshot sorted into the page's groups, as the old athena dash built them (its dash/view.go, awaiting.go and items.go):
Needs you, Ready for you, Awaiting review, In flight, Done today. Pure over the snapshot and the time."""
import functools
import html
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from athena.caps.dash.state import (LINKS, OPTIONS_MAX, PR_KEY_URL, REPO_SLUG, UNDO_DEFAULT, Ask, Hook, Option, PR, Report,
                                        Snapshot, clip, pr_key)

DONE_WINDOW = 14 * 24 * 3600  # a retired worker stays under Done no longer than this
DONE_MAX = 12
STALE_AFTER = 120  # seconds after which watch.json's PR state is shown with its age
FETCH_STALE = 600  # seconds after which a live worker's PR, as GitHub was last asked, is shown with its age
REVIEW_REQUESTS_MAX = 10
STUCK_AFTER = 600  # seconds a queued answer may wait for athena before its item shows again
STUCK_WHY = "athena hasn't picked this up yet"
DEFAULT_AFTER = 12 * 3600
AWAY_AFTER = 2 * 3600
HEALTH_STALE = 180
DONE_TODAY = 24 * 3600
NEEDS_SHOWN = 7
SHORT_MAX = 48
REPORT_ITEM_MAX = 80
PILL_ITEM = "pr:"
SENT_TEXT = {"recheck": "re-check sent", "request-review": "review request sent", "go": "merge go sent", "post-open-prs": "posting…"}
DOCTOR_FIX = "Install its service to run it every minute: athena services install. To check once now: athena doctor --fix."


@dataclass
class Actions:
    """What a row or full view lets the human answer from the page, each a POST to /api/action naming item."""
    item: str = ""
    options: List[Option] = field(default_factory=list)
    take_rec: Optional[Option] = None
    done: bool = False
    snooze: bool = False
    undo: str = ""
    rule: bool = False
    open: str = ""
    discuss: bool = False
    reply: bool = False
    dismiss: bool = False
    recheck: bool = False
    hides: bool = False
    for_pr: bool = False
    request_review: bool = False
    go: bool = False
    head: str = ""
    open_pr: str = ""
    num: str = ""
    post_open_prs: bool = False

    def row_options(self) -> list:
        """An item's row offers only its one-click calls; its options are in its full view."""
        if self.open or len(self.options) > OPTIONS_MAX:
            return []
        return self.options

    def on_row(self) -> bool:
        return bool(self.row_options() or self.take_rec or self.done or self.snooze or self.undo or self.open or
                    (not self.open and (self.discuss or self.reply)) or self.dismiss or self.recheck or self.request_review or
                    self.go or self.open_pr or self.post_open_prs)

    def allows(self, option="", text="", discuss=False) -> bool:
        if discuss:
            return self.discuss
        if text:
            return self.reply
        special = {"dismiss": self.dismiss, "recheck": self.recheck, "request-review": self.request_review, "go": self.go,
                   UNDO_DEFAULT: bool(self.undo), "post-open-prs": self.post_open_prs}
        if option in special:
            return special[option]
        return any(o.id == option for o in self.options)


@dataclass
class PRView:
    url: str = ""
    text: str = ""
    checks: str = ""
    review: str = ""
    stale_since: float = 0.0


@dataclass
class Item:
    """One row: an item's, a worker's or a PR's, led by its name."""
    id: str = ""
    ticket: str = ""
    ticket_url: str = ""
    name: str = ""
    href: str = ""
    call: str = ""  # HTML, already escaped
    title: str = ""
    repo: str = ""
    icon: str = ""
    badge: bool = False
    label: str = ""
    detail: str = ""
    when: float = 0.0
    due: float = 0.0
    pr: Optional[PRView] = None
    actions: Optional[Actions] = None
    sent: str = ""
    auto_at: float = 0.0
    session: str = ""  # a Rex session's id: its row offers Show

    item: str = ""
    finds: List[str] = field(default_factory=list)
    raw_pr: Optional[PR] = None
    branch: str = ""
    moved: bool = False
    rank: int = 0
    posted: bool = False

    def lead(self) -> str:
        return self.title if not self.href and self.title else self.name

    def second(self) -> str:
        if self.call:
            return self.call
        if self.href and self.title and self.title != self.name:
            return html.escape(self.title)
        return ""

    def search(self) -> str:
        parts = [self.name, self.title, self.ticket, self.repo] + list(self.finds)
        if self.id.startswith("w-"):
            parts.append(self.id[2:])
        if self.pr is not None:
            parts += [self.pr.text, pr_key(self.pr.url)]
        return " ".join(" ".join(parts).split()).lower()


@dataclass
class Pill:
    id: str = ""
    key: str = ""
    url: str = ""
    title: str = ""
    short: str = ""
    num: str = ""
    label: str = ""
    posted: bool = False
    waits_on: str = ""
    rechecking: bool = False
    actions: Optional[Actions] = None
    behind: List["Pill"] = field(default_factory=list)
    head: str = ""

    def search(self) -> str:
        return " ".join([self.title, self.num, self.key]).lower()


@dataclass
class HealthView:
    state: str = "unknown"  # ok, bad, unknown
    text: str = ""
    title: str = ""


@dataclass
class View:
    title: str = "athena"
    health: HealthView = field(default_factory=HealthView)
    awaiting: List[Pill] = field(default_factory=list)
    awaiting_count: int = 0
    needs_you: List[Item] = field(default_factory=list)
    ready: List[Item] = field(default_factory=list)
    in_progress: List[Item] = field(default_factory=list)
    done: List[Item] = field(default_factory=list)
    sessions: List[Item] = field(default_factory=list)  # athena: its Rex sessions, kept by the human
    snoozed: int = 0
    notices: List[str] = field(default_factory=list)

    actionable: Dict[str, Actions] = field(default_factory=dict)
    pills: Dict[str, Pill] = field(default_factory=dict)
    awaiting_rows: List[Item] = field(default_factory=list)
    posted: Optional[Dict[str, float]] = None


def unix_local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def build(s: Snapshot, now: float, linear_workspace: str = "") -> View:
    """Sort the snapshot into the page's groups. now is in Unix seconds."""
    v = View(notices=list(s.notices))
    if s.open_prs_channel and s.posted is not None:
        v.posted = s.posted
    linear_workspace = linear_workspace or s.linear_workspace
    history = {}
    for h in s.history:
        if h.ts >= (history[h.name].ts if h.name in history else 0):
            history[h.name] = h
    watch_ts, watched = (s.watch.ts, s.watch.workers) if s.watch else (0.0, {})
    followed_prs = s.watch.prs if s.watch else {}
    review_requests = s.watch.review_requests if s.watch else []
    followed_stale = watch_ts if now - watch_ts > STALE_AFTER else 0.0
    owned = {p.owner: p for p in followed_prs.values() if p.owner}
    shown = set()
    retired = []
    for w in s.workers:
        hook = s.hooks.get(w.name) or Hook()
        it = Item(id="w-" + w.name, name=w.name, title=w.title, repo=w.repo.rstrip("/").rsplit("/", 1)[-1] if w.repo else "")
        if w.linear:
            it.ticket = w.linear.upper()
            if linear_workspace:
                it.ticket_url = f"https://linear.app/{linear_workspace}/issue/{it.ticket}"
        if w.state == "live":
            ww = watched.get(w.name)
            pr = ww.pr if ww else None
            stale = watch_ts if pr is not None and now - watch_ts > STALE_AFTER else 0.0
            if pr is not None and pr.fetched and now - pr.fetched > FETCH_STALE:
                stale = pr.fetched  # athena: the watch writes every loop, its GitHub fetch only every 2 minutes
            if pr is None:
                pr = pr_from_report(hook.report, hook.last_report)
            it.pr = pr_view(pr, stale)
            it.when = max(hook.ts, w.updated)
            it.actions = Actions(item=it.id, discuss=True, reply=True)
            it.raw_pr, it.branch = pr, w.branch
            if pr is not None:
                shown.add(pr_key(pr.url))
            state = "working" if hook.background and hook.state in ("idle", "idle-unreported") else hook.state  # its background work runs
            if state in ("", "unknown", "starting") and ww is not None and ww.state in ("blocked", "working", "idle", "done"):
                state = ww.state
            place_live(v, it, w, hook, state, pr)
        elif w.state == "retired":
            if now - w.updated > DONE_WINDOW:
                continue
            it.when = w.updated
            pr = history[w.name].pr if w.name in history else None
            if pr is None:
                pr = pr_from_report(hook.report, hook.last_report)
            f = owned.get(w.name)
            if f is not None and (pr is None or pr_key(pr.url) == pr_key(f.url)):
                shown.add(pr_key(f.url))
                if f.state == "OPEN" and f.move is not None:
                    it.pr = pr_view(f, followed_stale)
                    it.raw_pr, it.branch = f, w.branch
                    place_move(v, it, f.move)
                    continue
                pr = f
            if pr is None and not w.linear or now - w.updated > DONE_TODAY:
                continue
            it.pr = pr_view(pr, 0)
            it.icon, it.label = "retired", "retired"
            if pr is not None:
                if pr.state == "MERGED":
                    it.icon, it.label = "merged", "merged"
                elif pr.state == "CLOSED":
                    it.label = "closed unmerged"
            retired.append(it)
    for k in sorted(followed_prs):
        p = followed_prs[k]
        key = pr_key(p.url)
        if p.state != "OPEN" or p.move is None or key in shown or not key:
            continue
        shown.add(key)
        it = Item(id="pr-" + slug(k), name=repo_name(key), title=p.title, pr=pr_view(p, followed_stale), raw_pr=p)
        place_move(v, it, p.move)
    n = 0
    for r in review_requests:
        if not REPO_SLUG.match(r.repo) or r.number <= 0 or n >= REVIEW_REQUESTS_MAX:
            continue
        url = f"https://github.com/{r.repo}/pull/{r.number}"
        if pr_key(url) in shown:
            continue
        shown.add(pr_key(url))
        n += 1
        it = Item(id="rr-" + slug(f"{r.repo}-{r.number}"), name=repo_name(pr_key(url)), title=r.title, icon="done", badge=True,
                  label="your review requested", pr=PRView(url=url, text=f"#{r.number}"))
        if r.author:
            it.detail = "by " + r.author
        v.needs_you.append(it)
    v.done += retired
    stack_awaiting(v, s.stacks, s.dismissed, rechecks(s.actions), now)
    v.awaiting_count = len(v.pills)
    place_items(v, s, now)
    mark_sent(v, s.actions, now)
    for it in v.needs_you:
        it.rank = need_rank(it, now)
    v.needs_you.sort(key=lambda it: (it.rank, it.due if it.rank == 0 else -it.when))
    for group in (v.ready, v.in_progress, v.done):
        group.sort(key=lambda it: -it.when)
    v.done = v.done[:DONE_MAX]
    v.health = health_view(s.health, now)
    v.title = f"({len(v.needs_you)}) athena" if v.needs_you else "athena"
    v.actionable = {}
    for key, p in v.pills.items():
        v.actionable[key] = p.actions
    for group in (v.needs_you, v.ready, v.in_progress, v.done):
        for it in group:
            if it.actions is not None:
                v.actionable[it.actions.item] = it.actions
    return v


def rechecks(actions) -> dict:
    return waiting([a for a in actions if a.option == "recheck"])


def waiting(actions) -> dict:
    """When each item's oldest still queued answer was given."""
    out = {}
    for a in actions:
        if a.status == "queued" and (a.item not in out or a.ts < out[a.item]):
            out[a.item] = a.ts
    return out


def need_rank(it: Item, now: float) -> int:
    """Due within a day first; then a blocked worker or failing checks; then the human's own calls; then PR rows."""
    if it.due > 0 and it.due - now < DONE_TODAY:
        return 0
    if it.icon in ("blocked", "fail"):
        return 1
    if it.id.startswith("ask-"):
        return 2
    return 3


def kind_label(a: Ask) -> str:
    if a.kind == "decision" and a.next == "you":
        return "your call"
    if a.kind == "result":
        return "result"
    if a.kind == "promise":
        return "athena promised"
    if a.kind == "incident":
        return "incident"
    return "your move" if a.next == "you" else f"{a.next}'s move"


def place_items(v: View, s: Snapshot, now: float):
    """Every open item where whose move it is says; an item on a live worker names that worker's row."""
    queued = waiting(s.actions)
    auto = {a.item: a for a in s.actions if a.auto and a.status == "queued"}
    rows = {}
    for group in (v.needs_you, v.in_progress, v.done):
        for it in group:
            rows[it.id] = it
    merged = merged_prs(s)
    for a in s.asks:
        title, _ = a.title()
        it = Item(id="ask-" + a.id, name=a.name or "Untitled item", href="/item/" + a.id, call=inline(without_name(title, a.name)),
                  icon="note", label=kind_label(a), when=a.created, due=a.due, item=a.id, finds=[a.text, a.pr] + list(a.workers))
        if a.pr:
            it.pr = PRView(url="https://github.com/" + a.pr.replace("#", "/pull/", 1), text=f"#{pr_number_of(a.pr)}")
        acts = Actions(item=a.id, options=list(a.options), open=it.href)
        it.actions = acts
        if not a.open():
            if now - a.resolved < DONE_TODAY:
                it.icon, it.label, it.when, it.due, it.actions = "ok", "resolved", a.resolved, 0.0, None
                if a.resolved_note:
                    it.label = a.resolved_note
                v.done.append(it)
            continue
        if a.snoozed(now):
            v.snoozed += 1
            continue
        if a.id in auto or a.applied is not None:
            label, verb = a.option(auto[a.id].option if a.id in auto else "").label, "athena is taking the rec on its own: "
            if a.applied is not None:
                label, verb = a.applied.label, "athena took the rec on its own: "
            it.icon, it.badge, it.label = "ready", True, verb + label
            acts.done, acts.undo = True, label
            v.ready.append(it)
            continue
        if a.id in queued:
            if now - queued[a.id] < STUCK_AFTER:
                continue
            it.sent = STUCK_WHY
        if a.kind == "decision":
            acts.discuss = acts.reply = acts.hides = acts.rule = acts.snooze = True
            if a.rec:
                acts.take_rec = a.option(a.rec)
            if a.auto:
                it.auto_at = apply_at(a, s.last_seen, now)
            it.icon = "ask"
        elif a.kind == "result":
            acts.done = acts.snooze = True
            it.icon, it.badge = "done", True
        else:
            acts.done = True
            it.icon = {"request": "working", "promise": "idle", "incident": "fail"}.get(a.kind, "")
        if a.kind not in ("result", "decision") and a.pr and a.pr in merged:
            it.icon, it.badge, it.label = "merged", True, "merged: done when you have looked"
            acts.snooze = True
            v.ready.append(it)
            continue
        if a.kind != "decision" and a.next != "you":
            row = worker_row(rows, a.workers)
            if row is not None:
                if not row.title:
                    row.title = row.name
                row.name, row.href, row.item = it.name, it.href, a.id
                row.finds += it.finds
                if not row.ticket:
                    row.ticket = row.id[2:].upper() if row.id.startswith("w-") else row.id.upper()
                continue
        if a.kind == "result":
            v.ready.append(it)
        elif a.next == "you":
            v.needs_you.append(it)
        else:
            v.in_progress.append(it)


def without_name(title: str, name: str) -> str:
    """The title less the name it starts with (any case) and the stop after it, so the row says the name once."""
    if not name or len(title) < len(name) or title[:len(name)].lower() != name.lower():
        return title
    rest = title[len(name):]
    if rest and (unicodedata.category(rest[0])[0] in "LN"):
        return title
    return rest.lstrip(" :,;·—–-")


def worker_row(rows, workers):
    for w in workers:
        if "w-" + w in rows:
            return rows["w-" + w]
    return None


def merged_prs(s: Snapshot) -> set:
    out = set()
    if s.watch:
        for ww in s.watch.workers.values():
            if ww.pr is not None and ww.pr.state == "MERGED":
                out.add(pr_key(ww.pr.url))
        for p in s.watch.prs.values():
            if p.state == "MERGED":
                out.add(pr_key(p.url))
    for h in s.history:
        if h.pr is not None and h.pr.state == "MERGED":
            out.add(pr_key(h.pr.url))
    return out


def repo_name(key: str) -> str:
    repo = key.split("#", 1)[0]
    return repo.split("/", 1)[1] if "/" in repo else ""


MOVE_OVER_WORKING = {"approved", "queued", "changes"}
MOVE_MERGES = {"approved", "mergeable", "reviewing"}


def place_move(v: View, it: Item, m):
    """A PR's row where whose move it is says: Needs you when it is the human's, the Awaiting review row, else In flight."""
    it.label, it.badge, it.moved = m.label, m.needs, True
    it.icon = {"approved": "ready", "mergeable": "ready", "queued": "ok", "reviewing": "review", "failing": "fail", "pending": "working",
               "unrequested": "done"}.get(m.kind, "pr")
    if m.kind == "changes":
        it.icon = "done" if m.needs else "working"
    if it.pr is not None:
        it.pr.review = ""
        if m.kind in ("failing", "pending"):
            it.pr.checks = ""
    key = pr_key(it.raw_pr.url) if it.raw_pr is not None else ""
    posted = v.posted or {}
    if key and m.kind == "unrequested" and key in posted and it.raw_pr.reviewed_at <= posted[key]:
        it.label, it.posted = "posted in #open-prs", True
    if m.kind == "awaiting" and m.label == "posted in #open-prs":
        it.posted = True  # athena's prs.move says so itself
    if (m.kind == "awaiting" or it.posted) and key:
        v.awaiting_rows.append(it)
        return
    if m.needs:
        if key:
            it.actions = pr_actions(it, m, v.posted is not None)
        v.needs_you.append(it)
    else:
        v.in_progress.append(it)


def pr_actions(it: Item, m, open_prs: bool) -> Actions:
    """A Needs you PR row's buttons, by why it needs the human."""
    key = pr_key(it.raw_pr.url)
    a = Actions(item=PILL_ITEM + key, discuss=True, for_pr=True)
    if m.kind == "unrequested":
        a.request_review, a.post_open_prs = True, open_prs
    elif m.kind == "failing":
        a.recheck = True
    elif m.kind in ("approved", "mergeable"):
        a.open_pr, a.num = it.raw_pr.url, f"#{pr_number_of(key)}"
        a.go, a.head = m.athena_merges and it.raw_pr.head != "", it.raw_pr.head
    elif m.kind == "changes":
        a.open_pr, a.num = it.raw_pr.url, f"#{pr_number_of(key)}"
    else:
        a.for_pr, a.reply = False, True
    return a


def mark_sent(v: View, actions, now: float):
    """A PR row whose button answer athena has queued says so, and offers it again only once done, undone or stuck."""
    sent = {}
    for a in actions:
        if a.status == "queued" and now - a.ts < STUCK_AFTER and SENT_TEXT.get(a.option):
            sent[a.item] = SENT_TEXT[a.option]
    for it in v.needs_you:
        a = it.actions
        if a is not None and a.for_pr and sent.get(a.item):
            it.sent = sent[a.item]
            a.request_review = a.recheck = a.go = a.post_open_prs = False


def place_live(v: View, it: Item, w, hook: Hook, state: str, pr):
    report = hook.report
    text = summary(hook)
    if w.goal_pending:
        it.icon, it.badge, it.label = "blocked", True, "waiting at a startup prompt"
        it.detail = "Answer it in its Rex session; athena then sends the goal."
        v.needs_you.append(it)
    elif state == "blocked":
        it.icon, it.badge = "blocked", True
        it.label, it.detail = "permission prompt", text
        if report is not None and report.status in ("BLOCKED", "NEEDS_CONTEXT"):
            it.label = {"BLOCKED": "blocked", "NEEDS_CONTEXT": "needs context"}[report.status]
            if report.concerns:
                it.detail = one_line(report.concerns[0])
        v.needs_you.append(it)
    elif pr is not None and pr.state == "MERGED":
        it.icon, it.label = "merged", "merged, not retired yet"
        v.done.append(it)
    elif pr is not None and pr.state == "OPEN" and not pr.draft and pr.move is not None and \
            (state != "working" or pr.move.kind in MOVE_OVER_WORKING) and (pr.move.kind not in MOVE_MERGES or reported(state, hook)):
        it.detail = text
        place_move(v, it, pr.move)
    else:
        it.icon, it.label, it.detail = state_icon(state, pr), state_label(state, report), text
        if state == "working" and hook.background and hook.state != "working":
            it.label = "working (background)"
        if not it.detail and report is not None and report.concerns:
            it.detail = one_line(report.concerns[0])
        if pr is not None and pr.checks == "failure":
            it.icon = "fail"
        v.in_progress.append(it)


def reported(state: str, h: Hook) -> bool:
    if state == "done":
        return True
    r = h.report or h.last_report
    return state in ("idle", "idle-unreported", "exited") and r is not None and r.status in ("DONE", "DONE_WITH_CONCERNS")


def state_icon(state: str, pr) -> str:
    if state == "done":
        return "review"
    if state == "working":
        return "working"
    if state == "exited":
        return "exited"
    if state == "limit":
        return "idle"
    return "pr" if pr is not None else "idle"


def state_label(state: str, r) -> str:
    if state == "done":
        return "done with concerns, athena reviewing" if r is not None and r.status == "DONE_WITH_CONCERNS" else "done, athena reviewing"
    return {"working": "working", "idle": "idle", "idle-unreported": "stopped without a report", "exited": "its Claude exited",
            "limit": "parked at a usage limit", "": "starting", "unknown": "starting", "starting": "starting"}.get(state, state)


_PR_NUMBER = re.compile(r"/pull/(\d+)")


def pr_from_report(*reports) -> Optional[PR]:
    for r in reports:
        if r is not None and r.pr.startswith("https://"):
            m = _PR_NUMBER.search(r.pr)
            return PR(url=r.pr, number=int(m.group(1)) if m else 0)
    return None


def pr_view(pr, stale: float) -> Optional[PRView]:
    if pr is None:
        return None
    pv = PRView(url=pr.url, text="PR", checks=pr.checks, stale_since=stale)
    if pr.number > 0:
        pv.text = f"#{pr.number}"
    if pr.draft:
        pv.text += " draft"
    pv.review = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested"}.get(pr.review, "")
    if pr.checks == "none":
        pv.checks = ""
    return pv


_REPORT_TAIL = re.compile(r'"\s*[,:]\s*["\[{]|"\]?\s*}\s*$')


def summary(h: Hook) -> str:
    """The worker's last line of prose: never its ATHENA-REPORT, not even a cut-off tail of one."""
    s = h.summary
    i = s.rfind("ATHENA-REPORT")
    if i >= 0:
        s = s[:i]
    elif (h.report is not None or h.last_report is not None) and _REPORT_TAIL.search(s):
        return ""
    return last_line(s)


def one_line(s: str) -> str:
    s = " ".join(s.split())
    return s[:REPORT_ITEM_MAX - 1].rstrip(" ") + "…" if len(s) > REPORT_ITEM_MAX else s


def last_line(s: str) -> str:
    for line in reversed(s.strip().split("\n")):
        t = line.strip()
        if t and not t.startswith("ATHENA-REPORT"):
            return t
    return ""


_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    return _NON_SLUG.sub("-", s.lower()).strip("-")[:32]


_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def _esc(s: str) -> str:
    """As Go's template.HTMLEscapeString."""
    return s.replace("&", "&amp;").replace("'", "&#39;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;").replace("\x00", "�")


def _prose(s: str) -> str:
    out = _esc(s)
    out = _STRONG.sub(r"<strong>\1</strong>", out)
    return _CODE.sub(r'<span class="code">\1</span>', out)


def inline(s: str, own: str = "") -> str:
    """The little markdown a row's line uses: links, bare URLs, **bold** and `code`; escaped first. Under the row of PR `own`, a bare
    URL of that PR is dropped, and any other bare PR URL shows as its short owner/name#n."""
    b = []
    last = 0
    for m in LINKS.finditer(s):
        b.append(_prose(s[last:m.start()]))
        last = m.end()
        text = url = m.group(0)
        if m.group(1) is not None:
            text, url = m.group(1), m.group(2)
        else:
            key = pr_key(url)
            if key:
                if key == own:
                    if "".join(b).endswith(" ") and s[last:].startswith(" "):
                        last += 1
                    continue
                sm = PR_KEY_URL.match(url)
                text, url = f"{sm.group(1)}#{sm.group(2)}", sm.group(0)
        b.append(f'<a href="{_esc(url)}">{_esc(text)}</a>')
    b.append(_prose(s[last:]))
    return "".join(b).rstrip(" :;,-–—")


def stack_awaiting(v: View, stacks, dismissed, queued, now: float):
    """The Awaiting review row: a pill per PR waiting on a teammate's review, a PR stacked on one behind it, in chain order."""
    nodes = {}

    def hidden(it):
        d = dismissed.get(pr_key(it.raw_pr.url))
        return d is not None and d.head == it.raw_pr.head and d.review == it.label
    for it in v.awaiting_rows:
        if hidden(it):
            continue
        nodes[pr_key(it.raw_pr.url)] = (it, True)
    if not nodes:
        return
    for it in v.in_progress:
        if it.moved and it.raw_pr is not None and pr_key(it.raw_pr.url) and pr_key(it.raw_pr.url) not in nodes:
            nodes[pr_key(it.raw_pr.url)] = (it, False)
    parent, rank = {}, {}
    for c in stacks:
        for i, n in enumerate(c.prs):
            k = f"{c.repo}#{n}"
            rank[k] = i
            if i > 0:
                parent[k] = f"{c.repo}#{c.prs[i - 1]}"
    heads = {}
    for k, (it, _) in nodes.items():
        b = it.raw_pr.branch or it.branch
        if b:
            heads[repo_of(k) + "|" + b] = k
    for k, (it, _) in nodes.items():
        if k not in parent and it.raw_pr.base:
            p = heads.get(repo_of(k) + "|" + it.raw_pr.base)
            if p and p != k:
                parent[k] = p
    front = {}

    def front_of(k, seen):
        if k in front:
            return front[k]
        f = ""
        p = parent.get(k, "")
        if p and p in nodes and p not in seen:
            seen.add(k)
            f = front_of(p, seen)
        if not f and nodes[k][1]:
            f = k
        front[k] = f
        return f
    for k in sorted(nodes):
        front_of(k, set())
    children = {}
    for k in nodes:
        p = parent.get(k, "")
        if p and front.get(k) and front[k] != k:
            children.setdefault(p, []).append(k)
    def pill_cmp(a, b):
        """By repo, then place in a stack.json chain, then number: the order they stack in."""
        if repo_of(a) != repo_of(b):
            return -1 if repo_of(a) < repo_of(b) else 1
        if a in rank and b in rank and rank[a] != rank[b]:
            return rank[a] - rank[b]
        return pr_number_of(a) - pr_number_of(b)
    for kids in children.values():
        kids.sort(key=functools.cmp_to_key(pill_cmp))

    def pill(node, waits_on):
        p = new_pill(node[0], pr_key(node[0].raw_pr.url))
        if waits_on:
            p.waits_on = f"#{pr_number_of(waits_on)}"
        ts = queued.get(p.actions.item)
        if ts is not None and now - ts < STUCK_AFTER:
            p.rechecking, p.actions.recheck = True, False
        return p
    stacked = set()
    fronts = sorted((k for k, f in front.items() if f == k), key=lambda k: (repo_of(k), -pr_number_of(k)))
    for k in fronts:
        p = pill(nodes[k], "")

        def walk(k):
            for c in children.get(k, []):
                stacked.add(id(nodes[c][0]))
                if not hidden(nodes[c][0]):
                    p.behind.append(pill(nodes[c], k))
                walk(c)
        walk(k)
        v.awaiting.append(p)
    v.pills = {}
    for f in v.awaiting:
        v.pills[f.actions.item] = f
        for b in f.behind:
            v.pills[b.actions.item] = b
    if stacked:
        v.in_progress = [it for it in v.in_progress if id(it) not in stacked]


def new_pill(it: Item, key: str) -> Pill:
    title = (it.raw_pr.title or "").strip() or it.title or repo_name(key)
    n = pr_number_of(key)
    return Pill(id=f"aw-{slug(repo_of(key))}-{n}", key=key, url=it.raw_pr.url, title=title, short=short_title(title), num=f"#{n}",
                label=it.label, posted=it.posted, head=it.raw_pr.head, actions=Actions(item=PILL_ITEM + key, recheck=True, dismiss=True))


def short_title(s: str) -> str:
    s = " ".join(s.split())
    if len(s) <= SHORT_MAX:
        return s
    cut = s[:SHORT_MAX - 1]
    i = cut.rfind(" ")
    if i >= 0 and i >= SHORT_MAX - 12:
        cut = cut[:i]
    return cut.rstrip(" .,:;-–—·") + "…"


def repo_of(key: str) -> str:
    return key.split("#", 1)[0]


def pr_number_of(key: str) -> int:
    try:
        return int(key.split("#", 1)[1])
    except (IndexError, ValueError):
        return 0


def health_view(h, now: float) -> HealthView:
    """The page's one dot: never green without a fresh check, and grey saying why."""
    if h is None:
        return HealthView("unknown", "doctor hasn't reported", "athena doctor has not reported yet (no health.json). " + DOCTOR_FIX)
    if h.ts - now > 60:  # athena: a check from the future is a clock that moved back, never ok
        return HealthView("unknown", "the clock moved back since doctor's last check",
                          "health.json is dated after now: the clock moved back since doctor wrote it. " + DOCTOR_FIX)
    if now - h.ts > HEALTH_STALE:
        at, today = unix_local(h.ts), unix_local(now)
        last = at.strftime("%H:%M") if at.date() == today.date() else at.strftime("%a %H:%M")
        return HealthView("unknown", "doctor not running · last check " + last,
                          "athena doctor last checked at " + at.strftime("%a %H:%M") + " and should check every minute. " + DOCTOR_FIX)
    if h.ok:
        return HealthView("ok", "all ok", "athena doctor's last check: " + unix_local(h.ts).strftime("%H:%M"))
    lines = []
    for p in h.problems:
        line = (p.check + ": " + p.detail).strip() if p.detail else p.check
        lines.append(line[2:] if line.startswith(": ") else line)
    text = "something is wrong"
    if lines:
        text = clip(lines[0], 60)
        if len(lines) > 1:
            text += f" · +{len(lines) - 1} more"
    return HealthView("bad", text, "\n".join(lines))


def apply_at(a: Ask, last_seen: float, now: float) -> float:
    """When a default applies: DEFAULT_AFTER after filing, or, while the human has been away, AWAY_AFTER after the later of its
    filing and their last activity."""
    at = a.created + DEFAULT_AFTER
    if last_seen > 0 and now - last_seen >= AWAY_AFTER:
        at = min(at, max(a.created, last_seen) + AWAY_AFTER)
    return at


def due_defaults(s: Snapshot, now: float) -> list:
    """Items whose default applies now: never one with a stake, snoozed, applied, or with an answer queued or taken."""
    answered = {a.item for a in s.actions if a.status != "cancelled"}
    return [a for a in s.asks if a.auto and not a.stakes and a.kind == "decision" and a.offers(a.rec) and a.open() and
            not a.snoozed(now) and a.applied is None and a.id not in answered and now >= apply_at(a, s.last_seen, now)]


def timeline(a: Ask, actions) -> list:
    """An item's log and the answers given on it, oldest first."""
    from athena.caps.dash.state import LogEntry
    out = list(a.log)
    for act in actions:
        if act.item != a.id:
            continue
        if act.auto:
            what = "athena took the rec on its own: " + a.option(act.option).label
        elif act.discuss:
            what = "you asked to discuss it in HQ"
        elif act.text:
            what = "you replied: " + clip(act.text, 160)
        elif act.option == UNDO_DEFAULT:
            what = "you undid the default"
        else:
            what = "you answered: " + a.option(act.option).label
        if act.rule:
            what += " (and don't ask again)"
        if act.status == "cancelled":
            what += " (undone)"
        elif act.status == "queued":
            what += " (athena has it queued)"
        out.append(LogEntry(ts=act.ts, text=what))
    out.sort(key=lambda e: e.ts)
    return out


def now() -> float:
    return time.time()
