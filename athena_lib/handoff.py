"""The handoff a fresh HQ starts from (`athena hq rotate`), and how full HQ's context is.

The handoff is generated from athena's own state, the authority a fresh HQ trusts: the ledger and each live worker's
merged status (PR from gh, no herdr call), the open dashboard asks, the tail of notes.md, the standing-orders path,
and the in-flight threads only HQ knows, which it writes before rotating (`--threads`, or threads.md).

HQ's context is read from its own transcript, `<claude config dir>/projects/*/<session>.jsonl`: the last main-thread
assistant entry's input, cache-read and cache-creation tokens are what the next turn starts from. Its window is 1M
when that entry's requestedModel ends in `[1m]`, else 200k (`ATHENA_HQ_CONTEXT_WINDOW` overrides).
"""
import json
import os
import re
import time
from pathlib import Path

from athena_lib import asks, ledger, paths, status

MAX_LINES = 150
NOTES_ENTRIES = 12
THREADS_MAX = 40
ASKS_MAX = 15
HINT_PERCENT = 60  # `athena status` suggests a rotation from here


def handoff_file() -> Path:
    return paths.ensure() / "handoff.md"


def threads_file() -> Path:
    return paths.state_dir() / "threads.md"


def notes_tail(text, n):
    """The last `n` entries of notes.md: a top-level bullet or a heading starts one, indented lines continue it."""
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(("- ", "* ", "#")) or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    entries = [e for e in entries if not e[0].startswith("# ")]  # the file's own title
    return [line for entry in entries[-n:] for line in entry]


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _cap(lines, limit, where):
    if len(lines) <= limit:
        return lines
    return lines[:limit - 1] + [f"… ({len(lines) - limit + 1} more lines cut; the rest is in {where})"]


def _fleet(rows):
    if not rows:
        return ["no live workers"]
    out = []
    for r in rows:
        git = r.get("git") or {}
        report = r.get("report") or {}
        bits = [f"- {r['name']}", r.get("state") or "unknown", r.get("branch") or "-", f"ahead {git.get('ahead', '-')}"]
        if report.get("status"):
            bits.append(f"report {report['status']}" + (f" @ {report['head'][:8]}" if report.get("head") else ""))
        if r.get("pending"):
            bits.append("goal pending")
        line = " · ".join(bits)
        if r.get("summary"):
            line += f": {r['summary'].replace(chr(10), ' ')[-100:]}"
        out.append(line)
    return out


def _prs(rows):
    out = []
    for r in rows:
        p = r.get("pr")
        if p:
            out.append(f"- {r['name']} #{p.get('number')} {p.get('state')}{' draft' if p.get('draft') else ''} · "
                       f"checks {p.get('checks') or '-'} · review {p.get('review') or 'none'} · {p.get('url')}")
    from athena_lib import watch  # watch imports hq, which imports this module
    for chain in watch.stacks():
        out.append(f"- stack {chain['repo']}: " + " → ".join(f"#{n}" for n in chain["prs"]))
    return out or ["no open PRs on live workers, no stacks"]


def _asks():
    items = [i for i in asks.items() if i.get("id")]
    out = [f"- {i['id']} ({i.get('kind') or 'ask'}) {i.get('text', '')}" + (f" (pr {i['pr']})" if i.get("pr") else "")
           for i in items[:ASKS_MAX]]
    if len(items) > ASKS_MAX:
        out.append(f"… {len(items) - ASKS_MAX} more: `athena ask`")
    return out or ["none"]


def build(threads=None, previous_session=None, now=None) -> str:
    """The handoff's Markdown, at most MAX_LINES lines. `threads`: HQ's text, else threads.md, else none."""
    state = paths.state_dir()
    rows = [status.collect(w, with_pr=True, with_herdr=False) for w in ledger.live()]
    rows.sort(key=lambda r: (r.get("urgency", 3), r["name"]))
    if threads is None:
        threads = _read(threads_file())
    thread_lines = [line.rstrip() for line in threads.splitlines() if line.strip()] or [
        f"none written: HQ writes them before rotating (`athena hq rotate --threads <file|->` or {threads_file()})"]
    thread_lines = _cap(thread_lines, THREADS_MAX, "the rotated HQ's conversation")
    head = [f"# HQ handoff · {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}", "",
            "Written by `athena hq rotate` from athena's state. Where this and `athena status --pr` differ, trust status."]
    if previous_session:
        head.append(f"Previous HQ conversation: `claude --resume {previous_session}` (only if a thread needs it).")
    sections = [
        ("Fleet", _fleet(rows)),
        ("PRs", _prs(rows)),
        ("Open asks", _asks()),
        ("In-flight threads", thread_lines),
        ("Standing orders", [f"Read in full: {state / 'standing-orders.md'}. Repo map: {state / 'repo-map.md'}. "
                             f"Notes: {state / 'notes.md'}."]),
    ]
    start = ["## Start here",
             "Load the athena skill, then run its session start (§1) from this handoff: re-arm the watch check and "
             "re-create the session-only crons, the fallback sweep among them (§1a)."]

    def render(notes):
        lines = head + [""]
        for title, body in sections[:4] + [("Notes tail", notes)] + sections[4:]:
            lines += [f"## {title}", *body, ""]
        return lines + start

    fixed = len(render([]))
    notes = notes_tail(_read(state / "notes.md"), NOTES_ENTRIES)
    room = max(MAX_LINES - fixed - 1, 1)
    if len(notes) > room:
        notes = ["… (older notes cut)"] + notes[len(notes) - room + 1:]
    lines = render(notes or ["notes.md is empty"])
    return "\n".join(lines[:MAX_LINES]) + "\n"


def write(text) -> Path:
    target = handoff_file()
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, target)
    return target


def _claude_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def transcript(session):
    if not session or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
        return None
    found = sorted((_claude_dir() / "projects").glob(f"*/{session}.jsonl"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def _lines_from_end(path, chunk=1 << 20):
    """A transcript's lines, last first, read backwards a chunk at a time: HQ's can be hundreds of MB."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, rest = f.tell(), b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + rest).split(b"\n")
            rest = parts.pop(0)
            yield from reversed(parts)
        if rest:
            yield rest


def context(session):
    """{"tokens", "window", "percent", "session"} of a Claude session's context, or None when it cannot be read."""
    path = transcript(session)
    if not path:
        return None
    try:
        for raw in _lines_from_end(path):
            if b'"assistant"' not in raw or b'"usage"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            usage = (entry.get("message") or {}).get("usage")
            if entry.get("type") != "assistant" or entry.get("isSidechain") or not isinstance(usage, dict):
                continue
            tokens = sum(int(usage.get(k) or 0) for k in
                         ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            model = entry.get("requestedModel") or (entry.get("message") or {}).get("model") or ""
            window = int(os.environ.get("ATHENA_HQ_CONTEXT_WINDOW") or (1_000_000 if "[1m]" in model else 200_000))
            return {"tokens": tokens, "window": window, "percent": round(100 * tokens / window), "session": session}
    except OSError:
        return None
    return None


def hq_context():
    """HQ's context, from the conversation hq.json records. No herdr call: `athena status` runs this."""
    try:
        session = json.loads((paths.state_dir() / "hq.json").read_text()).get("session")
    except (OSError, ValueError):
        return None
    return context(session)


def describe(ctx) -> str:
    return f"{ctx['percent']}% ({ctx['tokens'] // 1000}k of {ctx['window'] // 1000}k)"
