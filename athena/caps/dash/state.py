"""The state files the page is built from, read as the old athena dash read them (its dash/state.go and items.go): a file caught
mid-write or broken by hand shows its last good parse, a missing one is an empty source, and nothing here ever fails the page.
athena writes the same files (ARCHITECTURE.md decision 8); its own knowledge of a worker (a dead Claude, a usage limit, a
startup prompt) comes in through `overlay`."""
import fcntl
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

LEDGER = "ledger.jsonl"
WATCH = "watch.json"
HISTORY = "history.jsonl"
ASKS = "needs-you.json"
ACTIONS = "actions.jsonl"
DISMISSED = "dismissed-prs.json"
STACKS = "stack.json"
HEALTH = "health.json"
REQUESTS = "requests.jsonl"
DASH_CONFIG = "dash.json"
POSTED = "open-prs-posted.json"
ATHENA_CONFIG = "config.json"
WORKERS = "workers"
ARCHIVE = "items-archive.jsonl"
LOCK = "ask.lock"

# The limits `athena item add` holds an item to (caps/items.py).
OPTIONS_MAX = 2
OPTIONS_MAX_BODY = 4
LABEL_MAX = 16
TITLE_MAX = 120
WHY_MAX = 200
BODY_MAX = 8000
PREVIEWS_MAX = 4
TEXT_MAX = 600
NAME_MAX = 32
WORDS_MAX = 2000
LOG_MAX = 50
LINKS_MAX = 10
WORKERS_MAX = 6
UNDO_DEFAULT = "undo-default"

ITEM_KINDS = {"decision", "request", "result", "promise", "incident"}
ITEM_NEXT = {"you", "athena", "worker", "reviewer", "customer", "colleague", "ci"}
NEXT_BY_KIND = {"decision": "you", "result": "you", "request": "athena", "promise": "athena", "incident": "athena"}
ITEM_STAKES = {"security", "customer", "money", "one-way"}

ASK_ID = re.compile(r"^[a-z0-9-]{1,32}$")
PREVIEW_ENTRY = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}/?$")
DISCUSS_LABEL = re.compile(r"(?i)^discuss(\s+in\s+athena)?$")
HTTP_LINK = re.compile(r"^https?://\S+$")
WORKSPACE_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
CHANNEL_ID = re.compile(r"^[A-Z][A-Z0-9]{5,20}$")
REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PR_KEY_TEXT = re.compile(r"^[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}#[1-9][0-9]{0,9}$")
PR_KEY_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
# A markdown link to an http(s) url, or a bare url, which never ends in punctuation (caps/items.py agrees).
LINKS = re.compile(r"""\[([^\]]+)\]\((https?://[^)\s]+)\)|https?://[^\s<)]*[^\s<).,;:!?'"]""")


def _icons():
    out = set()
    try:
        text = (Path(__file__).with_name("icons.txt")).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


ICONS = _icons()


# Tolerant field access: a field of the wrong type reads as empty rather than failing the whole file.
def s_(m, k) -> str:
    v = m.get(k) if isinstance(m, dict) else None
    return v if isinstance(v, str) else ""


def n_(m, k) -> float:
    v = m.get(k) if isinstance(m, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def o_(m, k):
    v = m.get(k) if isinstance(m, dict) else None
    return v if isinstance(v, dict) else None


def truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v != ""
    return False


def clip(s: str, n: int) -> str:
    return s[:n - 1] + "…" if len(s) > n else s


def pr_key(url) -> str:
    """A PR's `owner/name#n`, lower case, from its GitHub URL; empty for any other URL."""
    m = PR_KEY_URL.match(str(url or ""))
    return f"{m.group(1).lower()}#{m.group(2)}" if m else ""


@dataclass
class Report:
    status: str = ""
    pr: str = ""
    head: str = ""
    concerns: List[str] = field(default_factory=list)


@dataclass
class Hook:
    state: str = ""
    summary: str = ""
    ts: float = 0.0
    report: Optional[Report] = None
    last_report: Optional[Report] = None
    background: int = 0  # athena: background work in flight when its turn ended (caps/hooks.py)


@dataclass
class Move:
    """Whose move an open PR waits on (caps/prs.py `move`): its row's label, and whether the row is in Needs you."""
    label: str = ""
    kind: str = ""
    needs: bool = False
    athena_merges: bool = False  # human_merge is false: athena merges on the human's go. A move without it counts as human-merged


@dataclass
class PR:
    number: int = 0
    url: str = ""
    title: str = ""
    state: str = ""
    draft: bool = False
    review: str = ""
    checks: str = ""
    comments: int = 0
    move: Optional[Move] = None
    owner: str = ""
    head: str = ""
    branch: str = ""
    base: str = ""
    reviewed_at: float = 0.0
    fetched: float = 0.0  # athena: when GitHub was last asked


@dataclass
class WatchWorker:
    state: str = ""
    detail: str = ""
    pr: Optional[PR] = None


@dataclass
class ReviewRequest:
    repo: str = ""
    number: int = 0
    title: str = ""
    author: str = ""


@dataclass
class Watch:
    ts: float = 0.0
    workers: Dict[str, WatchWorker] = field(default_factory=dict)
    prs: Dict[str, PR] = field(default_factory=dict)
    review_requests: List[ReviewRequest] = field(default_factory=list)


@dataclass
class HistoryEntry:
    ts: float = 0.0
    name: str = ""
    status: str = ""
    pr: Optional[PR] = None


@dataclass
class Worker:
    name: str = ""
    linear: str = ""
    title: str = ""
    repo: str = ""
    branch: str = ""
    state: str = ""  # live, retired, failed
    spawned: float = 0.0
    updated: float = 0.0
    goal_pending: bool = False


@dataclass
class Stack:
    repo: str = ""
    prs: List[int] = field(default_factory=list)


@dataclass
class Dismissal:
    head: str = ""
    review: str = ""
    ts: float = 0.0


@dataclass
class Option:
    id: str = ""
    label: str = ""
    icon: str = ""
    do: str = ""


@dataclass
class Link:
    title: str = ""
    url: str = ""


@dataclass
class LogEntry:
    ts: float = 0.0
    text: str = ""


@dataclass
class Applied:
    option: str = ""
    label: str = ""
    ts: float = 0.0


@dataclass
class Problem:
    check: str = ""
    detail: str = ""
    since: float = 0.0


@dataclass
class Health:
    ts: float = 0.0
    ok: bool = False
    problems: List[Problem] = field(default_factory=list)


@dataclass
class Action:
    """One answer from the page, as actions.jsonl folds: a line with an item queues it, a later line with its seq moves it on."""
    seq: int = 0
    ts: float = 0.0
    item: str = ""
    option: str = ""
    text: str = ""
    discuss: bool = False
    auto: bool = False
    rule: bool = False
    status: str = ""


@dataclass
class Ask:
    """One item of needs-you.json: anything the human asked for or needs to know about."""
    id: str = ""
    kind: str = ""
    text: str = ""
    why: str = ""
    pr: str = ""
    body: str = ""
    previews: List[str] = field(default_factory=list)
    options: List[Option] = field(default_factory=list)
    name: str = ""
    name_derived: bool = False
    next: str = ""
    due: float = 0.0
    words: str = ""
    workers: List[str] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    rec: str = ""
    auto: bool = False
    stakes: List[str] = field(default_factory=list)
    created: float = 0.0
    updated: float = 0.0
    resolved: float = 0.0
    resolved_note: str = ""
    resolved_by: str = ""
    snooze_until: float = 0.0
    snoozed_at: float = 0.0
    applied: Optional[Applied] = None
    log: List[LogEntry] = field(default_factory=list)

    def open(self) -> bool:
        return self.resolved == 0

    def snoozed(self, now: float) -> bool:
        """Hidden until snooze_until, unless something happened to it since it was snoozed."""
        return self.snooze_until > now and self.updated < self.snoozed_at

    def offers(self, option: str) -> bool:
        return any(o.id == option for o in self.options)

    def option(self, oid: str) -> Option:
        for o in self.options:
            if o.id == oid:
                return o
        return Option(id=oid, label=oid)

    def title(self):
        """(title, long): the text as its row shows it, one sentence of at most TITLE_MAX characters, never cut inside a link."""
        end = sentence_end(self.text)
        if end < 0 and len(self.text) <= TITLE_MAX:
            return self.text, False
        t = self.text[:end] if end >= 0 else self.text
        if len(t) > TITLE_MAX:
            cut = TITLE_MAX - 1
            for m in LINKS.finditer(t):
                if m.start() < cut < m.end():
                    cut = m.start()
            return t[:cut].rstrip(" ") + "…", True
        return t, True


@dataclass
class Snapshot:
    workers: List[Worker] = field(default_factory=list)
    hooks: Dict[str, Hook] = field(default_factory=dict)
    watch: Optional[Watch] = None
    history: List[HistoryEntry] = field(default_factory=list)
    asks: List[Ask] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    stacks: List[Stack] = field(default_factory=list)
    dismissed: Dict[str, Dismissal] = field(default_factory=dict)
    notices: List[str] = field(default_factory=list)
    health: Optional[Health] = None
    last_seen: float = 0.0
    linear_workspace: str = ""
    posted: Optional[Dict[str, float]] = None
    open_prs_channel: str = ""
    sessions: Dict[str, dict] = field(default_factory=dict)  # athena: each live worker's Rex session, from its status


# One sentence, as caps/items.py reads it: links, initials and abbreviations set aside, and a closing parenthetical too.
_ABBREV = re.compile(r"(?i)\b(?:(?:[a-z]\.){2,}|(?:etc|vs|cf|approx|incl|no|nr|dr|mr|mrs|ms|st|jr|sr)\.)")
_TAIL = re.compile(r"\s*\([^()]*\)[.!?]?\s*$")
_STOP = re.compile(r"""([.!?…]+["')\]’”]*)\s+[^\sa-z]""")
_LEADING_BLANK = re.compile(r"\A(?:[ \t]*\r?\n)+")


def sentence_end(text: str) -> int:
    """The offset just past text's first sentence when more follows it, else -1."""
    masked = list(text)
    for regex, mask in ((LINKS, " "), (_ABBREV, "x")):
        for m in regex.finditer("".join(masked)):
            for i in range(m.start(), m.end()):
                masked[i] = mask
    joined = "".join(masked)
    tail = _TAIL.search(joined)
    if tail:
        joined = joined[:tail.start()]
    m = _STOP.search(joined)
    return m.end(1) if m else -1


def trim_body(s: str) -> str:
    return _LEADING_BLANK.sub("", s, count=1).rstrip(" \t\r\n")


def item_kind(kind: str, options) -> str:
    """An item's kind, mapping one from before items: a question is a decision, a note a result, or a decision when it offers an
    option that asks for a reply."""
    if kind in ITEM_KINDS:
        return kind
    if kind == "note":
        return "decision" if any(o.do != "dismiss" for o in options) else "result"
    return "decision"


def parse_report(m) -> Optional[Report]:
    if m is None:
        return None
    r = Report(status=s_(m, "status").upper(), pr=s_(m, "pr"), head=s_(m, "head"))
    if isinstance(m.get("concerns"), list):
        r.concerns = [c for c in m["concerns"] if isinstance(c, str) and c]
    return r


def _count(x: float) -> int:
    return int(x) if math.isfinite(x) and x > 0 else 0


def parse_hook(m) -> Hook:
    return Hook(state=s_(m, "state"), summary=s_(m, "summary"), ts=n_(m, "ts"), report=parse_report(o_(m, "report")),
                last_report=parse_report(o_(m, "last_report")), background=_count(n_(m, "background")))


def _rfc3339(text: str) -> float:
    if not text:
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() // 1
    except ValueError:
        return 0.0


def parse_pr(m) -> Optional[PR]:
    if m is None or not s_(m, "url"):
        return None
    p = PR(number=int(n_(m, "number")), url=s_(m, "url"), title=s_(m, "title"), state=s_(m, "state").upper(),
           draft=truthy(m.get("draft")), review=s_(m, "review").upper(), checks=s_(m, "checks"), comments=int(n_(m, "comments")),
           owner=s_(m, "owner"), head=s_(m, "head"), branch=s_(m, "branch"), base=s_(m, "base"),
           reviewed_at=_rfc3339(s_(m, "teammate_reviewed_at")), fetched=n_(m, "fetched"))
    mv = o_(m, "move")
    if mv is not None and s_(mv, "label"):
        p.move = Move(label=s_(mv, "label"), kind=s_(mv, "kind"), needs=truthy(mv.get("needs")), athena_merges=mv.get("human_merge") is False)
    return p


def parse_watch(m) -> Watch:
    w = Watch(ts=n_(m, "ts"))
    for name, v in (o_(m, "workers") or {}).items():
        if isinstance(v, dict):
            w.workers[name] = WatchWorker(state=s_(v, "state"), detail=s_(v, "detail"), pr=parse_pr(o_(v, "pr")))
    for key, v in (o_(m, "prs") or {}).items():
        if isinstance(v, dict):
            p = parse_pr(v)
            if p is not None:
                w.prs[key] = p
    if isinstance(m.get("review_requests"), list):
        for r in m["review_requests"]:
            if isinstance(r, dict) and n_(r, "number") > 0 and s_(r, "repo"):
                w.review_requests.append(ReviewRequest(repo=s_(r, "repo"), number=int(n_(r, "number")), title=s_(r, "title"),
                                                       author=s_(r, "author")))
    return w


def parse_stacks(m) -> List[Stack]:
    out = []
    for c in m.get("stacks") if isinstance(m.get("stacks"), list) else []:
        if not isinstance(c, dict) or not REPO_SLUG.match(s_(c, "repo")):
            continue
        st = Stack(repo=s_(c, "repo").lower())
        for n in c.get("prs") if isinstance(c.get("prs"), list) else []:
            if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0:
                st.prs.append(int(n))
        out.append(st)
    return out


def parse_dismissed(m) -> Dict[str, Dismissal]:
    out = {}
    for key, v in (o_(m, "prs") or {}).items():
        if isinstance(v, dict) and PR_KEY_TEXT.match(key):
            out[key] = Dismissal(head=s_(v, "head"), review=s_(v, "review"), ts=n_(v, "ts"))
    return out


def parse_posted(m) -> Dict[str, float]:
    """open-prs-posted.json, {"owner/name#n": "<ts>"}: each key lower case, with its post's ts (0 when unreadable)."""
    out = {}
    for key, v in m.items():
        k = key.lower()
        if not PR_KEY_TEXT.match(k):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = 0.0
        else:
            out[k] = 0.0
    return out


def parse_health(m) -> Health:
    h = Health(ts=n_(m, "ts"), ok=m.get("ok") is True)
    for p in m.get("problems") if isinstance(m.get("problems"), list) else []:
        if isinstance(p, dict) and (s_(p, "check") or s_(p, "detail")):
            h.problems.append(Problem(check=clip(s_(p, "check"), 40), detail=clip(s_(p, "detail"), 160), since=n_(p, "since")))
    if h.problems:
        h.ok = False
    return h


def parse_item_fields(a: Ask, im: dict):
    """What an item adds to an ask, dropping what `athena item add` would refuse; stakes fail closed."""
    a.name = " ".join(s_(im, "name").split())
    if not a.name or len(a.name) > NAME_MAX:
        a.name = clip(a.name, NAME_MAX)
    a.name_derived = im.get("name_derived") is True
    a.next = s_(im, "next")
    if a.next not in ITEM_NEXT:
        a.next = NEXT_BY_KIND.get(a.kind, "")
    a.due = n_(im, "due")
    a.words = clip(s_(im, "words"), WORDS_MAX)
    for w in im.get("workers") if isinstance(im.get("workers"), list) else []:
        if isinstance(w, str) and ASK_ID.match(w) and len(a.workers) < WORKERS_MAX:
            a.workers.append(w)
    for lm in im.get("links") if isinstance(im.get("links"), list) else []:
        if isinstance(lm, dict) and HTTP_LINK.match(s_(lm, "url")) and len(a.links) < LINKS_MAX:
            title = clip(s_(lm, "title").strip(), 80) or s_(lm, "url")
            a.links.append(Link(title=title, url=s_(lm, "url")))
    a.rec = s_(im, "rec")
    if not a.offers(a.rec):
        a.rec = ""
    unreadable = False
    stakes = im.get("stakes")
    if stakes is None:
        pass
    elif isinstance(stakes, list):
        for st in stakes:
            if isinstance(st, str) and st in ITEM_STAKES:
                a.stakes.append(st)
            else:
                unreadable = True
    else:
        unreadable = True
    if unreadable and not a.stakes:
        a.stakes = ["unknown"]
    a.auto = im.get("auto") is True and a.rec != "" and not a.stakes and not unreadable and a.kind == "decision"
    a.created, a.updated = n_(im, "created"), n_(im, "updated")
    if a.updated < a.created:
        a.updated = a.created
    a.resolved, a.resolved_note, a.resolved_by = n_(im, "resolved"), clip(s_(im, "resolved_note"), 200), s_(im, "resolved_by")
    sm = o_(im, "snooze")
    if sm is not None:
        a.snooze_until, a.snoozed_at = n_(sm, "until"), n_(sm, "at")
    elif isinstance(im.get("snooze"), (int, float)) and not isinstance(im.get("snooze"), bool):
        a.snooze_until, a.snoozed_at = float(im["snooze"]), a.updated + 1  # this build's first form, a plain number
    am = o_(im, "applied")
    if am is not None and s_(am, "option"):
        a.applied = Applied(option=s_(am, "option"), label=clip(s_(am, "label"), LABEL_MAX + 8), ts=n_(am, "ts"))
    for lm in im.get("log") if isinstance(im.get("log"), list) else []:
        if isinstance(lm, dict) and s_(lm, "text"):
            a.log.append(LogEntry(ts=n_(lm, "ts"), text=clip(s_(lm, "text"), 200)))
    a.log = a.log[-LOG_MAX:]


def parse_asks(m) -> List[Ask]:
    out, seen = [], set()
    for im in m.get("items") if isinstance(m.get("items"), list) else []:
        if not isinstance(im, dict):
            continue
        a = Ask(id=s_(im, "id"), kind=s_(im, "kind"), text=clip(s_(im, "text"), TEXT_MAX), why=clip(s_(im, "why"), WHY_MAX),
                pr=s_(im, "pr").lower(), body=trim_body(clip(s_(im, "body"), BODY_MAX)))
        k = pr_key(s_(im, "pr"))
        if k:
            a.pr = k
        if not ASK_ID.match(a.id) or not a.text.strip() or a.id in seen:
            continue
        seen.add(a.id)
        most = OPTIONS_MAX_BODY if a.body else OPTIONS_MAX
        for om in im.get("options") if isinstance(im.get("options"), list) else []:
            if not isinstance(om, dict) or len(a.options) == most:
                continue
            o = Option(id=s_(om, "id"), label=s_(om, "label").strip(), icon=s_(om, "icon"), do=s_(om, "do"))
            if not ASK_ID.match(o.id) or o.id in ("dismiss", UNDO_DEFAULT) or not o.label or len(o.label) > LABEL_MAX or \
                    a.kind not in ("note", "result") and DISCUSS_LABEL.match(o.label):
                continue
            if o.icon not in ICONS:
                o.icon = ""
            if o.do != "dismiss":
                o.do = "reply"
            a.options.append(o)
        a.kind = item_kind(a.kind, a.options)
        for p in im.get("previews") if isinstance(im.get("previews"), list) else []:
            if isinstance(p, str) and PREVIEW_ENTRY.match(p) and len(a.previews) < PREVIEWS_MAX:
                a.previews.append(p)
        parse_item_fields(a, im)
        out.append(a)
    return out


def fold_actions(lines) -> List[Action]:
    by_seq, order = {}, []
    for m in lines:
        seq = int(n_(m, "seq"))
        if seq <= 0:
            continue
        if seq in by_seq:
            if s_(m, "status") in ("done", "cancelled"):
                by_seq[seq].status = s_(m, "status")
            continue
        if not s_(m, "item"):
            continue
        by_seq[seq] = Action(seq=seq, ts=n_(m, "ts"), item=s_(m, "item"), option=s_(m, "option"), text=s_(m, "text"),
                             discuss=truthy(m.get("discuss")), auto=m.get("auto") is True, rule=m.get("rule") is True,
                             status=s_(m, "status"))
        order.append(seq)
    return [by_seq[s] for s in sorted(order)]


_TERMINAL = {"retire": "retired", "failed": "failed"}


def fold_ledger(events) -> List[Worker]:
    """As kernel/ledger.py folds it: spawn starts a record, updates merge into it, retire and failed end it."""
    recs, order = {}, []
    for ev in events:
        name = s_(ev, "name")
        if not name:
            continue
        kind = s_(ev, "event")
        fields = {k: v for k, v in ev.items() if k not in ("event", "ts")}
        exists = name in recs
        rec = recs.get(name)
        if kind == "spawn":
            rec = dict(fields, state="live", spawned=ev.get("ts"))
        elif kind == "failed":
            rec = dict(fields, state="failed")
        elif exists:
            rec.update(fields)
            if kind in _TERMINAL:
                rec["state"] = _TERMINAL[kind]
        elif kind in _TERMINAL:
            rec = dict(fields, state=_TERMINAL[kind])
        else:
            continue
        if not exists:
            order.append(name)
        rec["updated"] = ev.get("ts")
        recs[name] = rec
    return [Worker(name=n, linear=s_(recs[n], "linear"), title=s_(recs[n], "title"), repo=s_(recs[n], "repo"),
                   branch=s_(recs[n], "branch"), state=s_(recs[n], "state"), spawned=n_(recs[n], "spawned"),
                   updated=n_(recs[n], "updated"), goal_pending=truthy(recs[n].get("goal_pending"))) for n in order]


def last_request(path) -> float:
    """When the human last typed into HQ: the newest line of requests.jsonl with an id, read from its last 64 KB."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - (64 << 10)))
            data = f.read()
    except OSError:
        return 0.0
    last = 0.0
    for line in data.split(b"\n"):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and s_(m, "id"):
            last = max(last, n_(m, "ts"))
    return last


def read_archive(state_dir) -> List[Ask]:
    items = []
    try:
        with open(Path(state_dir) / ARCHIVE, "rb") as f:
            for raw in f:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(m, dict):
                    items.append(m)
    except OSError:
        return []
    return parse_asks({"items": items})


class Store:
    """Reads the state dir and remembers the last good parse of every JSON file. Safe for concurrent use."""

    def __init__(self, state_dir, config_path=None):
        self.dir = Path(state_dir)
        if config_path is None:
            env = os.environ.get("ATHENA_CONFIG") or ""
            config_path = Path(os.path.expanduser(env)) if env else self.dir / ATHENA_CONFIG
        self.config = Path(config_path)
        self._lock = threading.Lock()
        self._sigs = None
        self.extra = []  # more files whose change re-renders the page
        self._good = {}

    def path(self, rel) -> Path:
        return rel if isinstance(rel, Path) and rel.is_absolute() else self.dir / rel

    def _watched(self) -> dict:
        out = {}

        def add(rel):
            try:
                st = os.stat(self.path(rel))
            except OSError:
                return
            if (st.st_mode & 0o170000) == 0o100000:
                out[str(rel)] = (st.st_size, st.st_mtime_ns)
        for rel in (LEDGER, WATCH, HISTORY, ASKS, ACTIONS, DASH_CONFIG, DISMISSED, STACKS, HEALTH, REQUESTS, POSTED, self.config, *self.extra):
            add(rel)
        try:
            names = os.listdir(self.dir / WORKERS)
        except OSError:
            names = []
        for name in names:
            if name.endswith(".json"):
                add(f"{WORKERS}/{name}")
        return out

    def changed(self) -> bool:
        """Whether any watched file appeared, went away or changed since the last call."""
        with self._lock:
            now = self._watched()
            same = self._sigs is not None and now == self._sigs
            self._sigs = now
            return not same

    def _object(self, rel, notices):
        try:
            data = self.path(rel).read_bytes()
        except OSError:
            self._good.pop(str(rel), None)
            return None
        try:
            m = json.loads(data)
        except ValueError:
            m = None
        if not isinstance(m, dict):
            if str(rel) in self._good:
                notices.append(f"{rel} is unreadable; showing its last good state")
                return self._good[str(rel)]
            previous = self._previous(rel)
            if previous is not None:  # athena keeps needs-you.json's last generation beside it
                notices.append(f"{rel} is unreadable; showing its last good state")
                return previous
            notices.append(f"{rel} is unreadable")
            return None
        self._good[str(rel)] = m
        return m

    def _previous(self, rel):
        if str(rel) != ASKS:
            return None
        try:
            m = json.loads(self.path(ASKS + ".prev").read_bytes())
        except (OSError, ValueError):
            return None
        return m if isinstance(m, dict) else None

    def _lines(self, rel, notices) -> list:
        try:
            f = open(self.path(rel), "rb")
        except OSError:
            return []
        out, bad = [], 0
        with f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    m = None
                if not isinstance(m, dict):
                    bad += 1
                    continue
                out.append(m)
        if bad:
            notices.append(f"{rel}: skipped {bad} unreadable line{'' if bad == 1 else 's'}")
        return out

    def load(self) -> Snapshot:
        """Every source. It never fails: what cannot be read is empty, with a quiet notice."""
        with self._lock:
            snap = Snapshot()
            if not self.dir.is_dir():
                snap.notices.append(f"no athena state yet at {self.dir}")
                return snap
            snap.workers = fold_ledger(self._lines(LEDGER, snap.notices))
            try:
                names = sorted(os.listdir(self.dir / WORKERS))
            except OSError:
                names = []
            seen = set()
            for entry in names:
                if not entry.endswith(".json"):
                    continue
                rel = f"{WORKERS}/{entry}"
                seen.add(rel)
                m = self._object(rel, snap.notices)
                if m is not None:
                    snap.hooks[entry[:-5]] = parse_hook(m)
            for rel in [r for r in self._good if r.startswith(WORKERS + "/") and r not in seen]:
                del self._good[rel]
            m = self._object(WATCH, snap.notices)
            if m is not None:
                snap.watch = parse_watch(m)
            for h in self._lines(HISTORY, snap.notices):
                snap.history.append(HistoryEntry(ts=n_(h, "ts"), name=s_(h, "name"), status=s_(h, "status"), pr=parse_pr(o_(h, "pr"))))
            m = self._object(DASH_CONFIG, snap.notices)
            if m is not None:
                ws = s_(m, "linear_workspace")
                if WORKSPACE_SLUG.match(ws):
                    snap.linear_workspace = ws
                elif ws:
                    snap.notices.append("dash.json: linear_workspace must be a Linear workspace slug")
            m = self._object(ASKS, snap.notices)
            if m is not None:
                snap.asks = parse_asks(m)
            snap.actions = fold_actions(self._lines(ACTIONS, snap.notices))
            m = self._object(STACKS, snap.notices)
            if m is not None:
                snap.stacks = parse_stacks(m)
            m = self._object(DISMISSED, snap.notices)
            if m is not None:
                snap.dismissed = parse_dismissed(m)
            m = self._object(HEALTH, snap.notices)
            if m is not None:
                snap.health = parse_health(m)
            snap.last_seen = last_request(self.dir / REQUESTS)
            for a in snap.actions:
                if not a.auto and a.ts > snap.last_seen:
                    snap.last_seen = a.ts
            m = self._object(POSTED, snap.notices)
            if m is not None:
                snap.posted = parse_posted(m)
            m = self._object(self.config, snap.notices)
            if m is not None and CHANNEL_ID.match(s_(m, "open_prs_channel")):
                snap.open_prs_channel = s_(m, "open_prs_channel")
            snap.notices.sort()
            return snap


class Locked:
    """ask.lock, held while actions.jsonl is appended to or needs-you.json rewritten, here and by `athena item`."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / LOCK

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def write_private(path: Path, data: bytes):
    """Atomically, mode 0600: a temp file beside it, then a rename."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def now() -> float:
    return time.time()


def floor(x: float) -> int:
    return int(math.floor(x))
