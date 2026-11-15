"""What the Flame page draws, from the index: a tree of frames for a scope and a span of days, and the top-frames tables.

Read only. The tree carries all four metrics on every node, so switching metric on the page costs no request. Frames under
0.1% of the root on every metric fold into one `…` frame per parent, so the page gets a few thousand nodes at most.
"""
import sqlite3
import time
from pathlib import Path

METRICS = ("time", "tokens", "cost", "output")
FOLD = 0.001
SCOPES = ("athena", "hq", "workers", "all")
_frames_cache = {}


def open_index(path: Path):
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    return db


def _frame_paths(db, key) -> dict:
    """{frame id: (name, ...)}, the whole trie, kept per index file and size of the trie."""
    count = db.execute("SELECT MAX(id) FROM frames").fetchone()[0] or 0
    cached = _frames_cache.get(key)
    if cached and cached[0] == count:
        return cached[1]
    rows = db.execute("SELECT id, parent, name FROM frames").fetchall()
    parent = {i: (p, n) for i, p, n in rows}
    paths = {0: ()}

    def walk(i):
        chain = []
        while i not in paths:
            p, n = parent[i]
            chain.append((i, n))
            i = p
        base = paths[i]
        for fid, name in reversed(chain):
            base = base + (name,)
            paths[fid] = base
        return paths

    for i in parent:
        if i not in paths:
            walk(i)
    _frames_cache[key] = (count, paths)
    return paths


def own_tool(path) -> tuple:
    """A step's own tool path, without the Agent or Workflow frames a subagent's steps hang under."""
    if len(path) > 2 and path[0] == "Agent":
        return own_tool(path[2:])
    if len(path) > 3 and path[0] == "Workflow":
        return own_tool(path[3:])
    return path


def keys_for(prefix) -> list:
    """Which top-frames rows a frame answers to, so hovering a row can light it: the frame that is the row, not its children."""
    keys = []
    if prefix[0] == "Agent" and len(prefix) == 2:
        keys.append(f"agent:{prefix[1]}")
    if prefix[0] == "Workflow" and len(prefix) == 2:
        keys.append(f"agent:workflow {prefix[1]}")
    tool = own_tool(prefix)
    if len(tool) == 1 and tool[0] != "MCP":
        keys.append(f"tool:{tool[0]}")
    if tool and tool[0] == "MCP" and len(tool) == 2:
        keys.append(f"tool:MCP {tool[1]}")
    if tool and tool[0] == "Bash" and len(tool) >= 2:
        keys.append(f"bash:{tool[-1]}")
    return keys


def _where(scope, since, until):
    clauses, args = [], []
    if scope == "athena":
        clauses.append("s.kind IN ('hq', 'worker')")
    elif scope == "hq":
        clauses.append("s.kind = 'hq'")
    elif scope == "workers":
        clauses.append("s.kind = 'worker'")
    elif scope.startswith("worker:"):
        clauses.append("s.kind = 'worker' AND s.name = ?")
        args.append(scope[len("worker:"):])
    elif scope.startswith("ticket:"):
        clauses.append("s.kind = 'worker' AND s.ticket = ?")
        args.append(scope[len("ticket:"):])
    if since:
        clauses.append("r.day >= ?")
        args.append(since)
    if until:
        clauses.append("r.day <= ?")
        args.append(until)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def _session_levels(scope, kind, name):
    if scope == "hq" or scope.startswith("worker:"):
        return ()
    if scope == "workers" or scope.startswith("ticket:"):
        return (name,)
    if scope == "athena":
        return ("HQ",) if kind == "hq" else ("workers", name)
    return ("athena", "HQ") if kind == "hq" else ("athena", "workers", name) if kind == "worker" else ("other", name)


def _root_name(scope):
    if scope.startswith("worker:"):
        return scope[len("worker:"):]
    if scope.startswith("ticket:"):
        return scope[len("ticket:"):]
    return {"athena": "athena", "hq": "HQ", "workers": "workers", "all": "everything"}.get(scope, scope)


def _node(name, kind=""):
    # v: time ms, total tokens, cost micro-dollars, output tokens; d: input, cache read, cache write, calls, model ms
    return {"n": name, "k": kind, "v": [0, 0, 0, 0], "d": [0, 0, 0, 0, 0], "c": {}}


def _kind(path, depth_in_frames):
    if depth_in_frames < 0:
        return "group"
    tool = own_tool(path[:depth_in_frames + 1])
    head = path[0]
    if head in ("Agent", "Workflow") and len(path[:depth_in_frames + 1]) <= (2 if head == "Agent" else 3):
        return "agent"
    head = tool[0] if tool else head
    if head == "reply":
        return "model"
    if head == "Bash":
        return "shell"
    if head in ("Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "MultiEdit"):
        return "files"
    if head in ("MCP", "WebFetch", "WebSearch"):
        return "web"
    if head in ("Agent", "Workflow", "Skill", "SendMessage"):
        return "agent"
    return "tool"


def tree(db, key, scope="athena", since=None, until=None) -> dict:
    started = time.monotonic()
    paths = _frame_paths(db, key)
    where, args = _where(scope, since, until)
    rows = db.execute(f"""SELECT s.kind, s.name, r.frame, SUM(r.ms), SUM(r.model_ms), SUM(r.input), SUM(r.output),
                          SUM(r.cache_read), SUM(r.cache_write), SUM(r.cost), SUM(r.calls)
                          FROM rollup r JOIN files f ON f.id = r.file JOIN sessions s ON s.id = f.session{where}
                          GROUP BY s.kind, s.name, r.frame""", args).fetchall()
    root = _node(_root_name(scope), "group")
    for kind, name, frame, ms, model_ms, inp, out, cr, cw, cost, calls in rows:
        levels = _session_levels(scope, kind, name)
        fpath = paths.get(frame, ("?",))
        full = levels + fpath
        values = (ms or 0, (inp or 0) + (out or 0) + (cr or 0) + (cw or 0), cost or 0, out or 0)
        detail = (inp or 0, cr or 0, cw or 0, calls or 0, model_ms or 0)
        node = root
        _add(node, values, detail)
        for depth, part in enumerate(full):
            child = node["c"].get(part)
            if child is None:
                child = node["c"][part] = _node(part, _kind(fpath, depth - len(levels)))
                if depth >= len(levels):
                    keys = keys_for(fpath[:depth - len(levels) + 1])
                    if keys:
                        child["h"] = keys
                if depth == len(levels) - 1 and kind in ("worker", "hq"):
                    child["s"] = name  # a session frame: the page asks for its ticket by name
            _add(child, values, detail)
            node = child
    folded = _fold(root, root["v"])
    sessions = _sessions(db, scope)
    return {"root": folded, "sessions": sessions, "ms": int((time.monotonic() - started) * 1000)}


def _add(node, values, detail):
    for i, v in enumerate(values):
        node["v"][i] += v
    for i, v in enumerate(detail):
        node["d"][i] += v


def _fold(node, total):
    """Children as a list, biggest first by time; the small ones on every metric become one `…`."""
    keep, small = [], _node("…", "fold")
    for child in node["c"].values():
        if all(child["v"][i] < total[i] * FOLD for i in range(4)):
            _add(small, child["v"], child["d"])
        else:
            keep.append(_fold(child, total))
    if any(small["v"]):
        keep.append(dict(small, c=[]))
    keep.sort(key=lambda c: -c["v"][0])
    out = {k: v for k, v in node.items() if k != "c"}
    if keep:
        out["c"] = keep
    return out


def _sessions(db, scope):
    """Per worker: ticket, title and wall span, for tooltips and the scope picker."""
    out = {}
    for kind, name, ticket, title, first, last in db.execute(
            "SELECT kind, name, MAX(ticket), MAX(title), MIN(first_ms), MAX(last_ms) FROM sessions "
            "WHERE kind IN ('hq', 'worker') GROUP BY kind, name"):
        out[name] = {"kind": kind, "ticket": ticket or "", "title": title or "", "first": first, "last": last}
    return out


def top(db, key, scope="athena", since=None, until=None) -> dict:
    """Ranked tables: by tool, by Bash command, by subagent type, by skill. Each row has all four metrics and its calls."""
    paths = _frame_paths(db, key)
    where, args = _where(scope, since, until)
    tables = {"tool": {}, "bash": {}, "agent": {}, "skill": {}}

    def add(table, label, values):
        row = tables[table].setdefault(label, [0, 0, 0, 0, 0])
        for i, v in enumerate(values):
            row[i] += v

    names = dict(db.execute("SELECT id, text FROM names").fetchall())
    for frame, skill, ms, inp, out, cr, cw, cost, calls in db.execute(
            f"""SELECT r.frame, r.skill, SUM(r.ms), SUM(r.input), SUM(r.output), SUM(r.cache_read), SUM(r.cache_write),
                SUM(r.cost), SUM(r.calls) FROM rollup r JOIN files f ON f.id = r.file JOIN sessions s ON s.id = f.session{where}
                GROUP BY r.frame, r.skill""", args):
        values = (ms or 0, (inp or 0) + (out or 0) + (cr or 0) + (cw or 0), cost or 0, out or 0, calls or 0)
        path = paths.get(frame, ("?",))
        tool = own_tool(path)
        head = tool[0] if tool else "?"
        add("tool", f"MCP {tool[1]}" if head == "MCP" and len(tool) > 1 else head, values)
        if head == "Bash" and len(tool) > 1:
            add("bash", tool[-1], values)
        if path[0] == "Agent" and len(path) > 1:
            add("agent", path[1], values)
        elif path[0] == "Workflow" and len(path) > 1:
            add("agent", f"workflow {path[1]}", values)
        if skill:
            add("skill", names.get(skill, "?"), values)
    return {name: sorted(([label, *row] for label, row in rows.items()), key=lambda r: -r[1])[:60]
            for name, rows in tables.items()}


def status(db) -> dict:
    import json
    row = db.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
    last = json.loads(row[0]) if row else {}
    counts = db.execute("SELECT COUNT(*), SUM(size) FROM files").fetchone()
    days = db.execute("SELECT MIN(day), MAX(day) FROM rollup").fetchone()
    try:  # lines the index holds no steps for, as every run since each file was first read could not read them
        skipped = db.execute("SELECT COALESCE(SUM(skipped), 0), COUNT(*) FILTER (WHERE skipped > 0) FROM files").fetchone()
        odd = [r[0] for r in db.execute("SELECT path FROM files WHERE skipped > 0 ORDER BY skipped DESC LIMIT 20")]
    except sqlite3.OperationalError:  # an index from before the count
        skipped, odd = (0, 0), []
    return {"last_run": last, "files": counts[0] or 0, "bytes": counts[1] or 0, "first_day": days[0], "last_day": days[1],
            "skipped_lines": skipped[0], "skipped_files": skipped[1], "odd_files": odd,
            "workers": [r[0] for r in db.execute("SELECT DISTINCT name FROM sessions WHERE kind='worker' ORDER BY name")],
            "tickets": [r[0] for r in db.execute("SELECT DISTINCT ticket FROM sessions WHERE kind='worker' AND ticket != '' "
                                                 "ORDER BY ticket")]}
