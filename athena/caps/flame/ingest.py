"""Claude Code transcripts into the flame index: one pass over new bytes per file, one line in memory at a time.

A session is a loop of steps: a model call, then the action it chose. Each step becomes a `call` row (the model call's time
and tokens, split evenly over the tool calls it made, or `reply` if it made none) and a `run` row per tool call (the tool's
own time, parallel runs sharing their overlap). What is still open when a run stops (a message whose records are still
coming, a tool still running) is kept in the file's `state` and finished on the next run. PLAN.md has the reasoning.
"""
import calendar
import json
import os
import sqlite3
import time
from pathlib import Path

from athena.caps.flame import frames

SCHEMA_VERSION = 1
CALL_CAP_MS = 10 * 60 * 1000  # a model call longer than this was a session left idle, not generation
RUN_CAP_MS = 20 * 60 * 1000  # a tool waiting longer than this was waiting on a person
IDLE_S = 30 * 60  # a file untouched this long has finished its last step: what is open is closed
SKIP_PREFIXES = (b'{"type":"api', b'{"type":"last-prompt', b'{"type":"atis', b'{"type":"mode', b'{"type":"permission-mode',
                 b'{"type":"file-history', b'{"type":"bridge', b'{"type":"frame-link', b'{"type":"pr-link', b'{"type":"cost',
                 b'{"type":"tools-used', b'{"type":"artifact', b'{"type":"session-owner', b'{"type":"ai-title')
CALL, RUN = 0, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, kind TEXT, session TEXT, prefix TEXT,
  inode INTEGER, size INTEGER, mtime INTEGER, offset INTEGER, state TEXT, first_ms INTEGER, last_ms INTEGER,
  cwd TEXT, agent_name TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, kind TEXT, name TEXT, ticket TEXT, title TEXT, cwd TEXT,
  first_ms INTEGER, last_ms INTEGER);
CREATE TABLE IF NOT EXISTS frames (id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, UNIQUE(parent, name));
CREATE TABLE IF NOT EXISTS names (id INTEGER PRIMARY KEY, text TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS steps (file INTEGER, turn INTEGER, kind INTEGER, start_ms INTEGER, end_ms INTEGER, ms INTEGER,
  frame INTEGER, model INTEGER, skill INTEGER, input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost INTEGER);
CREATE INDEX IF NOT EXISTS steps_file ON steps(file);
CREATE TABLE IF NOT EXISTS rollup (file INTEGER, day TEXT, frame INTEGER, skill INTEGER, ms INTEGER, model_ms INTEGER,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, cost INTEGER, calls INTEGER);
CREATE INDEX IF NOT EXISTS rollup_file ON rollup(file);
CREATE INDEX IF NOT EXISTS rollup_day ON rollup(day);
CREATE TABLE IF NOT EXISTS turns (file INTEGER, turn INTEGER, start_ms INTEGER, end_ms INTEGER, steps INTEGER);
CREATE INDEX IF NOT EXISTS turns_file ON turns(file);
"""


def ts_ms(text):
    """`2026-11-13T03:16:00.123Z` in milliseconds; None for anything else. Faster than fromisoformat, and 3.9 has no `Z`."""
    if not isinstance(text, str) or len(text) < 19 or text[10] != "T":
        return None
    try:
        secs = calendar.timegm((int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]),
                                int(text[17:19]), 0, 0, 0))
        frac = text[19:]
        millis = int((frac[1:4] + "00")[:3]) if frac.startswith(".") else 0
        return secs * 1000 + millis
    except ValueError:
        return None


COUNT_MAX = 2 ** 53  # a token count at or past this is no real count, and past 2**63 SQLite can't even store it


def _usage(message):
    """The message's token counts, and how many of them were odd (not a whole number from 0 up to COUNT_MAX), each taken as 0."""
    u = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    odd = [0]

    def num(v):
        if v is None:
            return 0
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < COUNT_MAX:
            odd[0] += 1
            return 0
        return v
    counts = {"in": num(u.get("input_tokens")), "out": num(u.get("output_tokens")), "cr": num(u.get("cache_read_input_tokens")),
              "cw": num(u.get("cache_creation_input_tokens")), "cw5": num(split.get("ephemeral_5m_input_tokens")),
              "cw1": num(split.get("ephemeral_1h_input_tokens")), "fast": 1 if u.get("speed") == "fast" else 0}
    return counts, odd[0]


def _text(value, most=200):
    """A string field, or None: a transcript's ids, models and names are strings, and only those are interned or used as keys."""
    return value if isinstance(value, str) and 0 < len(value) <= most else None


def _message(record) -> dict:
    message = record.get("message")
    return message if isinstance(message, dict) else {}


def _is_prompt(record) -> bool:
    if record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = _message(record).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def new_state() -> dict:
    return {"prev": None, "turn": 0, "msg": None, "runs": {}, "groups": {}, "closed": []}


class Parser:
    """One file's steps. Feed it records in order; it appends rows to `steps` as each step finishes."""

    def __init__(self, prefix, state=None):
        self.prefix = list(prefix)
        self.s = state or new_state()
        self.steps = []  # (turn, kind, start, end, ms, path tuple, model, skill, in, out, cr, cw, cost)
        self.bad = 0  # records of a shape this parser doesn't know, skipped
        self.info = {"session": None, "cwd": None, "agent_name": None, "first": None, "last": None}

    # -- records
    def feed(self, record):
        kind = record.get("type")
        if kind == "agent-name":
            name = record.get("agentName")
            if isinstance(name, str):
                self.info["agent_name"] = name
            return
        at = ts_ms(record.get("timestamp"))
        if self.info["session"] is None and isinstance(record.get("sessionId"), str):
            self.info["session"] = record["sessionId"]
        if self.info["cwd"] is None and isinstance(record.get("cwd"), str):
            self.info["cwd"] = record["cwd"]
        if at is None:
            return
        if self.info["first"] is None or at < self.info["first"]:
            self.info["first"] = at
        if self.info["last"] is None or at > self.info["last"]:
            self.info["last"] = at
        if kind == "assistant":
            self._assistant(record, at)
        elif kind == "user":
            self._user(record, at)
        else:
            self.s["prev"] = at

    def _assistant(self, record, at):
        message = _message(record)
        mid = _text(message.get("id"))
        model = _text(message.get("model"))  # only a string is a model: anything else is None, so nothing odd is interned
        if record.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            self.s["prev"] = at
            return
        if not mid:  # no message, or one without a string id: dropped, and said in the run's count
            self.bad += 1
            self.s["prev"] = at
            return
        if message.get("model") is not None and model is None:
            self.bad += 1  # an odd model: the message still counts, priced as the default
        msg = self.s["msg"]
        if (msg is None or msg["id"] != mid) and mid in self.s.setdefault("closed", []):
            self.s["prev"] = at  # a late record of a message already counted
            return
        if msg is None or msg["id"] != mid:
            self._close_message()
            msg = self.s["msg"] = {"id": mid, "anchor": self.s["prev"] if self.s["prev"] is not None else at, "first": at,
                                   "last": at, "model": model, "skill": None, "usage": {}, "tools": []}
        msg["last"] = max(msg["last"], at)
        usage, odd = _usage(message)
        self.bad += odd
        for key, value in usage.items():
            msg["usage"][key] = max(msg["usage"].get(key, 0), value)
        skill = record.get("attributionSkill")
        if isinstance(skill, str) and not msg["skill"]:
            msg["skill"] = frames.safe(skill)
        content = message.get("content")
        if content is not None and not isinstance(content, list):
            self.bad += 1
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and not _text(block.get("id")):
                self.bad += 1  # a tool call without a string id: its run can't be matched, so it is dropped
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                path = frames.tool_path(block.get("name"), block.get("input"))
                msg["tools"].append(path)
                self.s["runs"][block["id"]] = {"start": at, "path": path, "group": mid, "credit": frames.credits_run(block.get("name")),
                                               "model": model, "skill": msg["skill"], "turn": self.s["turn"]}
                group = self.s["groups"].setdefault(mid, {"open": 0, "done": []})
                group["open"] += 1

    def _user(self, record, at):
        if _is_prompt(record):
            self._close_message()
            self._close_open_runs(self.s["prev"] if self.s["prev"] is not None else at)
            self.s["turn"] += 1
            self.s["prev"] = at
            return
        content = _message(record).get("content")
        if not isinstance(content, (list, str)):
            self.bad += 1  # a user record without a message or content: dropped
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = _text(block.get("tool_use_id"))
                    if tool_id is None:
                        self.bad += 1
                    self._end_run(tool_id, at)
        self.s["prev"] = at

    def finish(self):
        """The file has gone quiet: its last message and anything still running are as finished as they will get."""
        self._close_message()
        if self.s["runs"] or self.s["groups"]:
            self._close_open_runs(self.s["prev"] if self.s["prev"] is not None else 0)

    # -- steps
    def _close_message(self):
        msg = self.s["msg"]
        if msg is None:
            return
        self.s["msg"] = None
        self.s["closed"] = (self.s.get("closed") or [])[-7:] + [msg["id"]]
        ms = min(max(msg["last"] - msg["anchor"], 0), CALL_CAP_MS)
        paths = msg["tools"] or [["reply"]]
        n = len(paths)
        usage = msg["usage"]
        cost = frames.cost_micro(msg["model"], usage)
        for i, path in enumerate(paths):
            share = lambda v: v // n + (1 if i < v % n else 0)  # noqa: E731  shares that add up to the whole
            self.steps.append((self.s["turn"], CALL, msg["anchor"], msg["last"], share(ms), tuple(path), msg["model"], msg["skill"],
                               share(usage.get("in", 0)), share(usage.get("out", 0)), share(usage.get("cr", 0)),
                               share(usage.get("cw", 0)), share(cost)))

    def _end_run(self, tool_id, at):
        run = self.s["runs"].pop(tool_id, None) if tool_id else None
        if run is None:
            return
        run["end"] = max(at, run["start"])
        group = self.s["groups"].get(run["group"])
        if group is None:
            group = self.s["groups"][run["group"]] = {"open": 1, "done": []}
        group["done"].append(run)
        group["open"] -= 1
        if group["open"] <= 0:
            self._emit_group(self.s["groups"].pop(run["group"])["done"])

    def _close_open_runs(self, at):
        for tool_id in list(self.s["runs"]):
            self._end_run(tool_id, at)
        for key in list(self.s["groups"]):
            group = self.s["groups"].pop(key)
            if group["done"]:
                self._emit_group(group["done"])

    def _emit_group(self, runs):
        """Runs from one model call share their overlap: on a stretch where k of them run, each is credited 1/k of it."""
        credited = [r for r in runs if r["credit"]]
        shares = {id(r): 0.0 for r in credited}
        points = sorted({p for r in credited for p in (r["start"], r["end"])})
        for a, b in zip(points, points[1:]):
            live = [r for r in credited if r["start"] <= a and r["end"] >= b]
            for r in live:
                shares[id(r)] += (b - a) / len(live)
        for r in runs:
            ms = min(int(round(shares.get(id(r), 0.0))), RUN_CAP_MS) if r["credit"] else 0
            self.steps.append((r["turn"], RUN, r["start"], r["end"], ms, tuple(r["path"]), r["model"], r["skill"], 0, 0, 0, 0, 0))


def read_new(path, offset, prefix, state, idle=False):
    """Parse complete lines from `offset`. Returns (parser, new offset). Never holds more than one line."""
    parser = Parser(prefix, state)
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written: the next run reads it whole
            pos += len(line)
            if line.startswith(SKIP_PREFIXES):
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):  # not JSON, or nested past what the parser will go
                parser.bad += 1
                continue
            if isinstance(record, dict):
                try:
                    parser.feed(record)
                except Exception:  # noqa: BLE001  a line of an unknown shape is skipped and counted, never fatal to its file
                    parser.bad += 1
    if idle:
        parser.finish()
    return parser, pos


# -- discovery

def discover(projects: Path) -> list:
    """(path, kind, session id, prefix) for every transcript: sessions, their subagents, and their workflows' agents."""
    found = []
    try:
        project_dirs = [p for p in projects.iterdir() if p.is_dir()]
    except OSError:
        return found
    for project in sorted(project_dirs):
        try:
            entries = list(os.scandir(project))
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(".jsonl") and entry.is_file():
                found.append((entry.path, "main", entry.name[:-6], []))
            elif entry.is_dir() and entry.name not in ("memory", "tool-results"):
                found.extend(_subagents(Path(entry.path)))
    return found


def _meta(path: Path) -> dict:
    try:
        with open(path.with_suffix(".meta.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _subagents(session_dir: Path) -> list:
    session = session_dir.name
    out = []
    agents = session_dir / "subagents"
    for path in sorted(agents.glob("agent-*.jsonl")):
        meta = _meta(path)
        out.append((str(path), "agent", session, ["Agent", frames.safe(meta.get("agentType") or "general-purpose")]))
    for run_dir in sorted(agents.glob("workflows/wf_*")):
        record = {}
        try:
            with open(session_dir / "workflows" / f"{run_dir.name}.json") as f:
                record = json.load(f)
        except (OSError, ValueError):
            pass
        name = frames.safe((record or {}).get("workflowName") if isinstance(record, dict) else None, "workflow")
        for path in sorted(run_dir.glob("agent-*.jsonl")):
            phase = frames.safe(_meta(path).get("workflowPhase") or "agents")
            out.append((str(path), "workflow", session, ["Workflow", name, phase]))
    return out


# -- the database

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    try:  # added after the first index was built: each file's count of lines it couldn't read
        db.execute("ALTER TABLE files ADD COLUMN skipped INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if version is None:
        db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        db.commit()
    return db


class Interner:
    def __init__(self, db):
        self.db = db
        self.frames = {(p, n): i for i, p, n in db.execute("SELECT id, parent, name FROM frames")}
        self.names = {t: i for i, t in db.execute("SELECT id, text FROM names")}

    def frame(self, path) -> int:
        parent = 0
        for name in path:
            key = (parent, name)
            found = self.frames.get(key)
            if found is None:
                found = self.db.execute("INSERT INTO frames (parent, name) VALUES (?, ?)", key).lastrowid
                self.frames[key] = found
            parent = found
        return parent

    def name(self, text):
        if not text:
            return 0
        found = self.names.get(text)
        if found is None:
            found = self.db.execute("INSERT INTO names (text) VALUES (?)", (text,)).lastrowid
            self.names[text] = found
        return found


def _open(state_text) -> bool:
    if not state_text:
        return False
    state = json.loads(state_text)
    return bool(state.get("msg") or state.get("runs") or state.get("groups"))


def _rebuild(db, file_id):
    db.execute("DELETE FROM rollup WHERE file=?", (file_id,))
    db.execute("""INSERT INTO rollup SELECT file, date(start_ms/1000, 'unixepoch', 'localtime'), frame, skill, SUM(ms), SUM(CASE WHEN kind=0 THEN ms ELSE 0 END), SUM(input),
                  SUM(output), SUM(cache_read), SUM(cache_write), SUM(cost), SUM(kind=0) FROM steps WHERE file=?
                  GROUP BY 2, frame, skill""", (file_id,))
    db.execute("DELETE FROM turns WHERE file=?", (file_id,))
    db.execute("""INSERT INTO turns SELECT file, turn, MIN(start_ms), MAX(end_ms), COUNT(*) FROM steps WHERE file=? GROUP BY turn""",
               (file_id,))


def index(db_path: Path, projects: Path, labels=None, full=False, say=lambda line: None, budget_s=None) -> dict:
    """One incremental pass. Returns counts. `labels` is a callable that names sessions (see label.py)."""
    started = time.monotonic()
    db = connect(db_path)
    if full:
        # One transaction for the whole rebuild: a reader (the dash) sees the old index until the new one commits, whole.
        for table in ("steps", "rollup", "turns", "files"):
            db.execute(f"DELETE FROM {table}")
    known = {row[1]: row for row in db.execute("SELECT id, path, inode, size, mtime, offset, state, skipped FROM files")}
    interner = Interner(db)
    found = discover(projects)
    now = time.time()
    todo = []
    for path, kind, session, prefix in found:
        try:
            st = os.stat(path)
        except OSError:
            continue
        row = known.get(path)
        idle = now - st.st_mtime > IDLE_S
        if row and row[2] == st.st_ino and row[3] == st.st_size and row[5] <= st.st_size and not (idle and _open(row[6])):
            continue  # nothing new, and nothing left open to finish
        todo.append((st.st_size - (row[5] if row and row[2] == st.st_ino and row[5] <= st.st_size else 0), path, kind, session,
                     prefix, st, row, idle))
    todo.sort(key=lambda t: -t[0])  # biggest first
    counts = {"files": len(found), "read": 0, "bytes": 0, "steps": 0, "reset": 0, "skipped": 0, "failed": 0, "failed_files": []}
    pending = 0
    for _, path, kind, session, prefix, st, row, idle in todo:
        if budget_s is not None and time.monotonic() - started > budget_s:
            break
        db.execute("SAVEPOINT one_file")
        try:
            pending += _one(db, interner, counts, path, kind, session, prefix, st, row, idle)
            db.execute("RELEASE one_file")
        except Exception as exc:  # noqa: BLE001  one file's trouble never stops the run: its writes are undone, it is said, tried next time
            db.execute("ROLLBACK TO one_file")
            db.execute("RELEASE one_file")
            interner.__init__(db)
            counts["failed"] += 1
            if len(counts["failed_files"]) < 20:
                counts["failed_files"].append(f"{path}: {type(exc).__name__}")
            say(f"  skipped {path}: {type(exc).__name__}")
            continue
        if pending > 256 << 20 and not full:
            db.commit()
            pending = 0
            say(f"  {counts['read']} of {len(todo)} files, {counts['bytes'] >> 20} MB")
    gone = set(known) - {f[0] for f in found}
    for path in gone:
        file_id = known[path][0]
        for table in ("steps", "rollup", "turns"):
            db.execute(f"DELETE FROM {table} WHERE file=?", (file_id,))
        db.execute("DELETE FROM files WHERE id=?", (file_id,))
    counts["gone"] = len(gone)
    if labels:
        labels(db)
    counts["seconds"] = round(time.monotonic() - started, 1)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (json.dumps({"at": int(time.time()), **counts}),))
    db.commit()
    db.close()
    return counts


def _one(db, interner, counts, path, kind, session, prefix, st, row, idle) -> int:
    """Read one file's new lines into the index. Returns the bytes read."""
    if row and (row[2] != st.st_ino or row[5] > st.st_size):
        db.execute("DELETE FROM steps WHERE file=?", (row[0],))
        counts["reset"] += 1
        row = None
    offset = row[5] if row else 0
    state = json.loads(row[6]) if row and row[6] else None
    parser, new_offset = read_new(path, offset, prefix, state, idle)
    if row is None:
        file_id = db.execute("""INSERT INTO files (path, kind, session, prefix) VALUES (?, ?, ?, ?)
                                ON CONFLICT(path) DO UPDATE SET kind=excluded.kind RETURNING id""",
                             (path, kind, session, json.dumps(prefix))).fetchone()[0]
        db.execute("DELETE FROM steps WHERE file=?", (file_id,))
        first = last = None
    else:
        file_id = row[0]
        first, last = db.execute("SELECT first_ms, last_ms FROM files WHERE id=?", (file_id,)).fetchone()
    info = parser.info
    if kind == "main" and info["session"]:
        session = info["session"]
    first = min(x for x in (first, info["first"]) if x is not None) if (first or info["first"]) else None
    last = max(x for x in (last, info["last"]) if x is not None) if (last or info["last"]) else None
    rows = []
    for t, k, a, b, ms, p, m, sk, i, o, cr, cw, c in parser.steps:
        try:  # one odd step never fails its file: it is skipped and counted, like an odd line
            rows.append((file_id, int(t), int(k), int(a), int(b), int(ms), interner.frame([x if isinstance(x, str) else "other" for x in prefix + list(p)]),
                         interner.name(_text(m)), interner.name(_text(sk)), int(i), int(o), int(cr), int(cw), int(c)))
        except Exception:  # noqa: BLE001
            parser.bad += 1
    db.execute("SAVEPOINT steps_batch")
    try:
        db.executemany("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("RELEASE steps_batch")
    except (OverflowError, sqlite3.Error):  # one row SQLite won't take never costs the file its others
        db.execute("ROLLBACK TO steps_batch")  # what the batch got in before it stopped goes, so nothing is in twice
        db.execute("RELEASE steps_batch")
        for one in rows:
            try:
                db.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", one)
            except (OverflowError, sqlite3.Error):
                parser.bad += 1
    db.execute("""UPDATE files SET session=?, inode=?, size=?, mtime=?, offset=?, state=?, first_ms=?, last_ms=?,
                  cwd=COALESCE(cwd, ?), agent_name=COALESCE(?, agent_name), skipped=? + ? WHERE id=?""",
               (session, st.st_ino, st.st_size, int(st.st_mtime), new_offset, json.dumps(parser.s), first, last, info["cwd"],
                info["agent_name"], (row and row[7]) or 0, parser.bad, file_id))
    _rebuild(db, file_id)
    counts["read"] += 1
    counts["bytes"] += new_offset - offset
    counts["steps"] += len(rows)
    counts["skipped"] += parser.bad
    return new_offset - offset
