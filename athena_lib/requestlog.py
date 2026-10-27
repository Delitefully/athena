"""What the human types into HQ, logged outside the model, and the weekly check that every request has an item.

`requests.jsonl` in the state dir, private: one line per prompt the human submitted in HQ, `{"id": "r12", "ts",
"session", "text"}`, written by HQ's UserPromptSubmit hook (hooks/worker-status -> hookstatus). Prompts athena itself
submits are not the human's and are left out: the athena-watch mod's wakes, the start, restart and rotation routines,
cron prompts, slash commands, and anything Claude Code wraps in a tag (a message from another session, a task
notification). HQ files each new request as an item (`athena item add --request r12`); a line `{"ignore": "r12"}`
marks one that was chat, not a request (`athena item ignore`). The weekly check lists the requests in its window that
neither an item (open, resolved or archived) nor an ignore line accounts for.
"""
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager

from athena_lib import asks, paths

TEXT_MAX = 4000  # characters of a prompt kept; a pasted log is cut
CHECK_DAYS = 7
SHOW_MAX = 160  # characters of a request the check prints
# NOT_HUMAN: the prefixes of every prompt athena submits into HQ itself, never logged as the human's: the watch mod's
# wake (hooks/watch-mod/core.ts promptText), hq.py's start, restart and rotation routines, athena doctor's resume and
# rotate prompts, the fallback sweep's cron, slash commands, and what Claude Code wraps in a tag. A prompt athena
# submits must start with one of these, or it is logged as a request; other modules import this list.
NOT_HUMAN = ("athena watch ", "athena watch:", "athena doctor", "athena fallback sweep", "Start your HQ routine",
             "HQ restarted:", "Fresh HQ after", "/", "<", "dash-queue ", "slack-new ")
_ID = re.compile(r"^r([1-9][0-9]{0,9})$")


def log_file():
    return paths.state_dir() / "requests.jsonl"


@contextmanager
def _locked():
    fd = os.open(paths.ensure() / "requests.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _lines():
    try:
        text = log_file().read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _append(lines):
    fd = os.open(log_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, "".join(json.dumps(l, sort_keys=True) + "\n" for l in lines).encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def is_human(text) -> bool:
    t = (text or "").lstrip()
    return bool(t) and not t.startswith(NOT_HUMAN)


def record(text, session=None, now=None):
    """Log one prompt the human typed into HQ; returns its id, or None when it is not the human's."""
    if not isinstance(text, str) or not is_human(text):
        return None
    now = time.time() if now is None else now
    with _locked():
        last = 0
        for line in _lines():
            m = _ID.match(str(line.get("id") or ""))
            if m:
                last = max(last, int(m.group(1)))
        rid = f"r{last + 1}"
        entry = {"id": rid, "ts": round(now, 3), "text": text.strip()[:TEXT_MAX]}
        if isinstance(session, str) and session:
            entry["session"] = session
        _append([entry])
    return rid


def requests(since=None):
    """The logged requests, oldest first, each with `ignored` when marked chat."""
    lines = _lines()
    ignored = {l["ignore"] for l in lines if isinstance(l.get("ignore"), str)}
    out = []
    for l in lines:
        if _ID.match(str(l.get("id") or "")) and isinstance(l.get("text"), str):
            if since is not None and (l.get("ts") or 0) < since:
                continue
            out.append({**l, "ignored": l["id"] in ignored})
    return out


def ignore(ids, now=None):
    """Mark requests as chat, not requests: the weekly check stops listing them."""
    now = time.time() if now is None else now
    known = {r["id"] for r in requests()}
    bad = [i for i in ids if i not in known]
    if bad:
        raise asks.AskError(f"no request {', '.join(bad)} in the log (athena item requests)")
    with _locked():
        _append([{"ignore": i, "ts": round(now, 3)} for i in ids])
    return list(ids)


def filed():
    """request id -> the item that holds it, over open, resolved and archived items."""
    out = {}
    for item in asks.archived() + asks.items(resolved=True):
        for r in item.get("requests") or []:
            out[r] = item.get("id")
    return out


def check(days=CHECK_DAYS, now=None):
    """The requests of the last `days` days with no item and not marked chat: what fell through."""
    now = time.time() if now is None else now
    homes = filed()
    return [r for r in requests(since=now - days * 86400) if not r["ignored"] and r["id"] not in homes]


def describe(r) -> str:
    when = time.strftime("%a %d %b %H:%M", time.localtime(r.get("ts") or 0))
    text = " ".join(r["text"].split())
    if len(text) > SHOW_MAX:
        text = text[:SHOW_MAX - 1] + "…"
    return f"{r['id']}  {when}  {text}"
