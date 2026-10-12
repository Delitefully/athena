"""athena's asks for the dashboard, and the human's answers to them.

`needs-you.json`: the decisions and notes that belong to no worker, written only here (`athena ask add`, `done`).
`actions.jsonl`: the human's answers from the dashboard. The dash appends a line per answer (dash/actions.go), and a
later line with only its seq and a status moves it on: queued -> done (athena acted) or cancelled (the human undid it
within SETTLE seconds). `athena ask done` compacts it. Both writers hold `ask.lock`.

`athena watch` rings HQ with `dash-queue <seq>` once an answer settles (Doorbell); HQ then reads `athena ask queue`.
The answers reach HQ only through that tool output: no item text or reply ever goes into a wake prompt.
"""
import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager

from athena_lib import paths

SETTLE = 6  # seconds an answer can still be undone; the queue and the doorbell see only older ones (dash/actions.go)
KEEP_DONE = 24 * 3600  # done and cancelled answers older than this are compacted away
OPTIONS_MAX, LABEL_MAX, TEXT_MAX, WHY_MAX = 2, 16, 600, 200  # two 16-character labels fit the Needs column (dash/state.go)

_ID = re.compile(r"^[a-z0-9-]{1,32}$")
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_KEY = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|https?://[^\s<)]+")


class AskError(Exception):
    pass


def asks_file():
    return paths.state_dir() / "needs-you.json"


def actions_file():
    return paths.state_dir() / "actions.jsonl"


def _legacy_file():
    return paths.state_dir() / "needs-you.md"


def icons() -> list:
    """The icon names an option may use: dash/icons.txt, as the dashboard serves them."""
    text = (paths.PLUGIN_ROOT / "dash" / "icons.txt").read_text()
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


@contextmanager
def _locked():
    fd = os.open(paths.ensure() / "ask.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _write_atomic(target, text):
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)  # the dash never reads it half-written


def _read():
    try:
        data = json.loads(asks_file().read_text())
    except (OSError, ValueError):
        return {"next": 1, "items": []}
    items = [i for i in data.get("items", []) if isinstance(i, dict)] if isinstance(data, dict) else []
    nxt = data.get("next") if isinstance(data, dict) and isinstance(data.get("next"), int) else 1
    return {"next": max(nxt, 1), "items": items}


def _save(data):
    _write_atomic(asks_file(), json.dumps(data, indent=2) + "\n")


def asks_question(text) -> bool:
    """A question mark anywhere outside a URL (a link's own text counts), as the dashboard read needs-you.md."""
    for m in _LINK.finditer(text):
        if m.group(1) and "?" in m.group(1):
            return True
    return "?" in _LINK.sub(" ", text)


def _legacy_lines(text):
    """needs-you.md's asks: one per list item or plain line; headings, comments and blank lines are skipped."""
    out, in_comment = [], False
    for line in text.splitlines():
        t = line.strip()
        if in_comment:
            in_comment = "-->" not in t
            continue
        if t.startswith("<!--"):
            in_comment = "-->" not in t
            continue
        if not t or t.startswith("#"):
            continue
        for p in ("- [ ] ", "- ", "* ", "+ "):
            if t.startswith(p):
                t = t[len(p):].strip()
                break
        if t:
            out.append(t)
    return out


def migrate():
    """Import needs-you.md's lines as asks (a question when one has a `?`, else a note), then rename it
    needs-you.md.migrated. Run before every read, so a line written there out of habit is picked up too."""
    legacy = _legacy_file()
    if not legacy.exists():
        return []
    with _locked():
        try:
            text = legacy.read_text()
        except OSError:
            return []
        data = _read()
        have = {i.get("text") for i in data["items"]}
        added = []
        for line in _legacy_lines(text):
            if line in have:
                continue
            item = {"id": f"a{data['next']}", "kind": "question" if asks_question(line) else "note", "text": line[:TEXT_MAX],
                    "created": int(time.time())}
            data["next"] += 1
            data["items"].append(item)
            added.append(item["id"])
        _save(data)
        os.replace(legacy, legacy.with_name("needs-you.md.migrated"))
    return added


def items():
    migrate()
    return _read()["items"]


def show(item_id):
    for item in items():
        if item.get("id") == item_id:
            return item
    return None


def _pr_key(pr):
    if not pr:
        return None
    m = _PR_URL.fullmatch(pr.rstrip("/")) or _PR_KEY.fullmatch(pr)
    if not m:
        raise AskError(f"--pr takes a GitHub PR URL or owner/name#n, not {pr!r}")
    return f"{m.group(1)}#{m.group(2)}"


def _option(spec, taken):
    """`label[:icon[:do]]`, do being reply (the default: athena acts on it) or dismiss (nothing to do)."""
    label, icon, do = (spec.split(":") + ["", ""])[:3]
    label = label.strip()
    if not label or len(label) > LABEL_MAX:
        raise AskError(f"an option's label is 1 to {LABEL_MAX} characters: {label!r}")
    do = do or "reply"
    if do not in ("reply", "dismiss"):
        raise AskError(f"an option's do is reply or dismiss, not {do!r}")
    known = icons()
    if icon and icon not in known:
        print(f"athena ask: no icon {icon!r}; the option shows none. Icons: {' '.join(known)}", file=sys.stderr)
        icon = ""
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:28] or "option"
    oid, n = base, 2
    while oid in taken or oid == "dismiss":  # `dismiss` is the × a note with no options gets
        oid, n = f"{base}-{n}", n + 1
    taken.add(oid)
    out = {"id": oid, "label": label, "do": do}
    if icon:
        out["icon"] = icon
    return out


def add(text, kind=None, why="", pr=None, options=()):
    """Add an ask; returns its id. A question gets Discuss and Reply on the dashboard, a note only its options."""
    text = " ".join((text or "").split())
    why = " ".join((why or "").split())
    if not text or len(text) > TEXT_MAX:
        raise AskError(f"an ask is 1 to {TEXT_MAX} characters")
    if len(why) > WHY_MAX:
        raise AskError(f"--why is one sentence, at most {WHY_MAX} characters")
    if len(options) > OPTIONS_MAX:
        raise AskError(f"at most {OPTIONS_MAX} options: Discuss in Athena and Reply… come with every question")
    kind = kind or ("question" if asks_question(text) else "note")
    taken = set()
    item = {"kind": kind, "text": text, "why": why, "options": [_option(o, taken) for o in options]}
    key = _pr_key(pr)
    if key:
        item["pr"] = key
    migrate()
    with _locked():
        data = _read()
        item = {"id": f"a{data['next']}", **item, "created": int(time.time())}
        data["next"] += 1
        data["items"].append(item)
        _save(data)
    return item["id"]


def _lines():
    try:
        text = actions_file().read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("seq"), int) and value["seq"] > 0:
            out.append(value)
    return out


def fold(lines):
    """seq -> the answer, as dash/state.go foldActions reads it: the first line with an item queues it, a later line
    with a status moves it on. Each answer carries `changed`, the ts of its last line."""
    out = {}
    for m in lines:
        seq = m["seq"]
        if seq in out:
            if m.get("status") in ("done", "cancelled"):
                ts = m.get("ts") if isinstance(m.get("ts"), (int, float)) else out[seq]["changed"]
                out[seq].update(status=m["status"], changed=ts)
            continue
        if not m.get("item"):
            continue
        ts = m.get("ts") if isinstance(m.get("ts"), (int, float)) else 0
        a = {"seq": seq, "ts": ts, "item": str(m["item"]), "status": m.get("status") or "queued"}
        for k in ("option", "text"):
            if isinstance(m.get(k), str):
                a[k] = m[k]
        if m.get("discuss") is True:
            a["discuss"] = True
        a["changed"] = a["ts"]
        out[seq] = a
    return dict(sorted(out.items()))


def _settled(now=None):
    now = time.time() if now is None else now
    return [a for a in fold(_lines()).values() if a["status"] == "queued" and now - a["ts"] >= SETTLE]


def queue(now=None):
    """The answers athena has to act on, in seq order: each with what it answers (the ask, or the worker of a row)
    and the answer itself (an option with its label and do, a reply's text, or discuss)."""
    asks = {i.get("id"): i for i in items()}
    out = []
    for a in _settled(now):
        entry = {"seq": a["seq"], "item": a["item"], "at": a["ts"]}
        item = asks.get(a["item"])
        if a["item"].startswith("w-"):
            entry["worker"] = a["item"][2:]
        elif item:
            entry["ask"] = {k: item[k] for k in ("kind", "text", "why", "pr") if item.get(k)}
        else:
            entry["ask"] = None  # done already, or removed
        if a.get("discuss"):
            entry["discuss"] = True
        elif "text" in a:
            entry["reply"] = a["text"]
        else:
            opt = next((o for o in (item or {}).get("options", []) if o.get("id") == a.get("option")), None)
            if a.get("option") == "dismiss":
                opt = {"id": "dismiss", "label": "×", "do": "dismiss"}
            entry["option"] = opt or {"id": a.get("option")}
        out.append(entry)
    return out


def _append(lines):
    fd = os.open(actions_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, "".join(json.dumps(l, sort_keys=True) + "\n" for l in lines).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _compact(now):
    """Drop the lines of answers done or cancelled over KEEP_DONE ago, keeping the highest seq's (the dash counts on)."""
    lines = _lines()
    if not lines:
        return
    folded = fold(lines)
    top = max(l["seq"] for l in lines)
    drop = {s for s, a in folded.items() if a["status"] != "queued" and now - a["changed"] > KEEP_DONE and s != top}
    if drop:
        _write_atomic(actions_file(), "".join(json.dumps(l, sort_keys=True) + "\n" for l in lines if l["seq"] not in drop))


def done(item_id, keep=False, now=None):
    """athena acted on `item_id`: its settled answers are done, and the ask leaves needs-you.json unless `keep`
    (then the dashboard shows it again, say after a discussion that left it open). Compacts actions.jsonl."""
    now = time.time() if now is None else now
    migrate()
    with _locked():
        seqs = [a["seq"] for a in _settled(now) if a["item"] == item_id]
        if seqs:
            _append([{"seq": s, "ts": now, "status": "done"} for s in seqs])
        removed = False
        if not keep:
            data = _read()
            left = [i for i in data["items"] if i.get("id") != item_id]
            removed = len(left) != len(data["items"])
            if removed:
                data["items"] = left
                _save(data)
        _compact(now)
    return {"item": item_id, "removed": removed, "done": seqs}


class Doorbell:
    """`athena watch`'s ring for the dashboard: `dash-queue <seq>` once answers settle, for the highest settled seq
    above the last it rang for. It starts at 0, so a restarted watch rings for whatever is still queued. One stat a
    call while nothing changes and nothing waits to settle."""

    def __init__(self):
        self.rung = 0
        self.error = None
        self._sig = None
        self._waiting = True

    def check(self, now=None):
        try:
            st = actions_file().stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig == self._sig and not self._waiting:
            return None
        self._sig = sig
        now = time.time() if now is None else now
        fresh = [a for a in fold(_lines()).values() if a["status"] == "queued" and a["seq"] > self.rung]
        settled = [a["seq"] for a in fresh if now - a["ts"] >= SETTLE]
        self._waiting = len(settled) < len(fresh)
        if not settled:
            return None
        self.rung = max(settled)
        return f"dash-queue {self.rung}"
