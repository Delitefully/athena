"""athena's asks for the dashboard, and the human's answers to them.

`needs-you.json`: the decisions and notes that belong to no worker, written only here (`athena ask add`, `done`).
`actions.jsonl`: the human's answers from the dashboard. The dash appends a line per answer (dash/actions.go), and a
later line with only its seq and a status moves it on: queued -> done (athena acted) or cancelled (the human undid it
within SETTLE seconds). `athena ask done` compacts it. Both writers hold `ask.lock`.

`athena watch` rings HQ with `dash-queue <seq>` once an answer settles (Doorbell); HQ then reads `athena ask queue`.
An answer's item is an ask's id, `w-<worker>` for a live worker's row, or `pr:owner/name#n` for a PR: on the Awaiting
review row (`recheck`: inspect the PR and get it ready again), or a Needs you row placed by its whose-move state
(`discuss`, `recheck`, `request-review`, or `go` with the `head` it is for). SKILL.md says what HQ does with each.
The answers reach HQ only through that tool output: no item text or reply ever goes into a wake prompt.
"""
import fcntl
import json
import os
import re
import shutil
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from athena_lib import paths

SETTLE = 6  # seconds an answer can still be undone; the queue and the doorbell see only older ones (dash/actions.go)
KEEP_DONE = 24 * 3600  # done and cancelled answers older than this are compacted away
OPTIONS_MAX, LABEL_MAX, WHY_MAX = 2, 16, 200  # two 16-character labels fit the Needs column (dash/state.go)
# An ask's text is its title on the dashboard: one short sentence. The rest goes in its body, a Markdown panel the
# dashboard expands under it, where up to OPTIONS_MAX_BODY options show as full-size buttons.
TEXT_MAX, BODY_MAX, OPTIONS_MAX_BODY = 120, 8000, 4
LEGACY_TEXT_MAX = 600  # a needs-you.md line, imported as it was
# Previews: local HTML an ask links to, copied into <state>/previews/<id>/ and served sandboxed by the dash.
PREVIEWS_MAX, PREVIEW_BYTES_MAX, PREVIEW_FILES_MAX = 4, 20 << 20, 500

_ID = re.compile(r"^[a-z0-9-]{1,32}$")
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_KEY = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
DISCUSS_LABEL = r"(?i)^discuss(\s+in\s+athena)?$"  # a question has Discuss in Athena already (dash/state.go discussLabel)
_PILL = re.compile(r"^pr:([a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100})#([1-9][0-9]{0,9})$")  # the dash's prKeyText
# A PR's buttons on the dashboard, by option: Re-check on the Awaiting review row or a failing Needs you row, Request
# review and Merge (a go, for the head the human saw) on a Needs you row (dash/view.go prActions).
PR_OPTIONS = {"recheck": {"id": "recheck", "label": "Re-check and fix", "do": "reply"},
              "request-review": {"id": "request-review", "label": "Request review", "do": "reply"},
              "go": {"id": "go", "label": "Merge", "do": "reply"}}
_HEAD = re.compile(r"^[0-9a-f]{7,40}$")  # the dash's headText
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|https?://[^\s<)]*[^\s<).,;:!?'\"]")  # dash/view.go links
# One sentence: no stop followed by more text that does not start in lower case ("Ship now... or wait?" is one), once
# links, initials (a.m., U.S., e.g.), abbreviations (etc., vs., approx., No., Dr.) and a closing parenthetical such as "(rec B)" are set aside.
# dash/state.go sentenceEnd reads it the same way (dash/testdata/titles.json).
_ABBREV = re.compile(r"\b(?:(?:[a-z]\.){2,}|(?:etc|vs|cf|approx|incl|no|nr|dr|mr|mrs|ms|st|jr|sr)\.)", re.I)
_PAREN_TAIL = re.compile(r"\s*\([^()]*\)[.!?]?\s*$")
_SENTENCE_END = re.compile(r"[.!?\u2026]+[\"')\]\u2019\u201d]*\s+[^\sa-z]")
_PREVIEW_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class AskError(Exception):
    pass


def asks_file():
    return paths.state_dir() / "needs-you.json"


def actions_file():
    return paths.state_dir() / "actions.jsonl"


def previews_dir():
    return paths.state_dir() / "previews"


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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # a temp file left over from a failed write keeps its old mode otherwise
    with os.fdopen(fd, "w") as f:
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
            item = {"id": f"a{data['next']}", "kind": "question" if asks_question(line) else "note", "text": line[:LEGACY_TEXT_MAX],
                    "created": int(time.time())}
            data["next"] += 1
            data["items"].append(item)
            added.append(item["id"])
        _save(data)
        os.replace(legacy, legacy.with_name("needs-you.md.migrated"))
    return added


def items(import_legacy=True):
    """The asks. `import_legacy=False` (a dry run) reads needs-you.json and leaves a needs-you.md where it is."""
    if import_legacy:
        migrate()
    return _read()["items"]


def legacy_lines() -> list:
    """The lines a needs-you.md not yet imported holds."""
    try:
        return _legacy_lines(_legacy_file().read_text())
    except OSError:
        return []


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


def one_sentence(text) -> bool:
    """Whether text is one sentence: no sentence end followed by more text, setting aside links, e.g. and the like,
    and a closing parenthetical such as "(rec B)"."""
    t = _ABBREV.sub("x", _LINK.sub(" ", text))  # a link is no text of its own: "Merge #43? <url>" is one sentence
    return not _SENTENCE_END.search(_PAREN_TAIL.sub("", t))


_BODY_HINT = "put the rest in its body (--body, --body-file <path|->, or later athena ask body <id> --file)"


def _title(text):
    text = " ".join((text or "").split())
    if not text or len(text) > TEXT_MAX:
        raise AskError(f"an ask's text is its title on the dashboard, 1 to {TEXT_MAX} characters: the question and its key "
                       f"details, no more; {_BODY_HINT}")
    if not one_sentence(text):
        raise AskError(f"an ask's text is its title on the dashboard, one sentence: {_BODY_HINT}")
    return text


def _body(body):
    """The Markdown the dashboard expands under an ask: its lines kept, CRLF read as LF, blank lines around it dropped."""
    body = re.sub(r"\A(?:[ \t]*\n)+", "", (body or "").replace("\r\n", "\n")).rstrip()
    if len(body) > BODY_MAX:
        raise AskError(f"a body is at most {BODY_MAX} characters, not {len(body)}")
    return body


def previews_root():
    """<state>/previews, private: only athena and the dash read it."""
    root = previews_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _preview_files(src, raw, budget):
    """A preview folder's files to copy, by path relative to it; hidden ones are left out (and never looked at). Refuses
    any symlink it would copy, and stops as soon as the folder is past what is left of the budget [bytes, files]."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                raise AskError(f"--preview {raw}: {(Path(dirpath) / d).relative_to(src)} is a symlink; a preview copies files only")
        for n in sorted(f for f in filenames if not f.startswith(".")):
            p = Path(dirpath) / n
            if p.is_symlink():
                raise AskError(f"--preview {raw}: {p.relative_to(src)} is a symlink; a preview copies files only")
            if not p.is_file():
                raise AskError(f"--preview {raw}: {p.relative_to(src)} is not a regular file")
            out.append(p.relative_to(src))
            budget[0] -= p.stat().st_size
            budget[1] -= 1
            if budget[0] < 0 or budget[1] < 0:
                raise AskError(_PREVIEW_TOO_BIG)
    if Path("index.html") not in out:
        raise AskError(f"--preview {raw}: a folder opens on its index.html, and it has none")
    return out


def _plan_previews(srcs):
    """Check every preview before anything is copied: [(entry, source, files)], entry being the name the dash links
    (a folder's ends in /), files None for a single file."""
    if len(srcs) > PREVIEWS_MAX:
        raise AskError(f"at most {PREVIEWS_MAX} previews")
    plan, budget = [], [PREVIEW_BYTES_MAX, PREVIEW_FILES_MAX]
    for raw in srcs:
        src = Path(os.path.expanduser(raw))
        if src.is_symlink():
            raise AskError(f"--preview {raw} is a symlink; give the file or folder itself")
        name = _preview_name(src)
        if src.is_dir():
            files = _preview_files(src, raw, budget)
            entry = name + "/"
        elif src.is_file():
            files = None
            budget[0] -= src.stat().st_size
            budget[1] -= 1
            if budget[0] < 0 or budget[1] < 0:
                raise AskError(_PREVIEW_TOO_BIG)
            entry = name
        else:
            raise AskError(f"--preview {raw}: no such file or folder")
        if any(e.rstrip("/") == name for e, _, _ in plan):
            raise AskError(f"--preview {raw}: two previews named {name}")
        plan.append((entry, src, files))
    return plan


_PREVIEW_TOO_BIG = f"previews are at most {PREVIEW_BYTES_MAX >> 20} MB and {PREVIEW_FILES_MAX} files in all"
PREVIEW_NAME_MAX = 100  # the dash links a name of up to 128 characters (dash/state.go previewEntry)


def _preview_name(src):
    """The name a preview is linked by: its file or folder name in letters, digits and . _ -, its extension kept."""
    name = _PREVIEW_NAME.sub("-", src.name).strip("-.") or "preview"
    if len(name) > PREVIEW_NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        ext = dot + ext if dot and len(ext) <= 10 and src.is_file() else ""
        name = name[:PREVIEW_NAME_MAX - len(ext)].rstrip("-.") + ext
    return name


def _copy_file(src, dst):
    """Copies src's bytes, never through a symlink (one swapped in since the check fails here), into a new 0600 file."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AskError(f"--preview: {src} is not a regular file")
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(out, "wb") as w, os.fdopen(os.dup(fd), "rb") as r:
            shutil.copyfileobj(r, w)
    finally:
        os.close(fd)


def _copy_previews(plan):
    """Copies the previews into a new hidden folder of <state>/previews, outside ask.lock (which the dash's answers
    need too); add() renames it to the ask's id under the lock. The dash serves no hidden folder."""
    root = previews_root()
    for old in root.glob(".incoming-*"):  # left by an add that was killed mid-copy
        try:
            if time.time() - old.stat().st_mtime > 3600:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    dest = root / f".incoming-{os.getpid()}-{time.monotonic_ns()}"
    dest.mkdir(mode=0o700)
    try:
        _copy_into(plan, dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _copy_into(plan, dest):
    for entry, src, files in plan:
        if files is None:
            _copy_file(src, dest / entry)
            continue
        for rel in files:
            d = dest
            for part in (Path(entry) / rel).parent.parts:
                d = d / part
                d.mkdir(mode=0o700, exist_ok=True)
            _copy_file(src / rel, dest / entry / rel)


def _remove_previews(item_id):
    if _ID.match(item_id or ""):
        shutil.rmtree(previews_dir() / item_id, ignore_errors=True)


def add(text, kind=None, why="", pr=None, options=(), body="", previews=()):
    """Add an ask; returns its id. A question gets Discuss and Reply on the dashboard, a note only its options. Its
    text is the title, one short sentence; body is the Markdown the dashboard expands under it, and previews are files
    or folders of local HTML it links to, copied into the state dir."""
    text = _title(text)
    why = " ".join((why or "").split())
    body = _body(body)
    if len(why) > WHY_MAX:
        raise AskError(f"--why is one sentence, at most {WHY_MAX} characters")
    most = OPTIONS_MAX_BODY if body else OPTIONS_MAX
    if len(options) > most:
        more = f"; up to {OPTIONS_MAX_BODY} with a body (--body)" if not body else ""
        raise AskError(f"at most {most} options{more}: Discuss in Athena and Reply… come with every question")
    kind = kind or ("question" if asks_question(text) else "note")
    for spec in options:
        label = spec.split(":")[0].strip()
        if kind == "question" and re.match(DISCUSS_LABEL, label):
            raise AskError(f"no {label!r} option: the dashboard gives every question Discuss in Athena")
    taken = set()
    item = {"kind": kind, "text": text, "why": why, "options": [_option(o, taken) for o in options]}
    if body:
        item["body"] = body
    key = _pr_key(pr)
    if key:
        item["pr"] = key
    plan = _plan_previews(list(previews))
    if plan:
        item["previews"] = [entry for entry, _, _ in plan]
    migrate()
    incoming = _copy_previews(plan) if plan else None
    try:
        with _locked():
            data = _read()
            item = {"id": f"a{data['next']}", **item, "created": int(time.time())}
            if incoming:  # in place before the ask is saved, so the dash never links a preview that is not there
                _remove_previews(item["id"])
                os.rename(incoming, previews_dir() / item["id"])
                incoming = None
            try:
                data["next"] += 1
                data["items"].append(item)
                _save(data)
            except BaseException:
                if plan:
                    _remove_previews(item["id"])
                raise
    finally:
        if incoming:
            shutil.rmtree(incoming, ignore_errors=True)
    return item["id"]


def set_body(item_id, body):
    """Attach a body to an open ask, or replace or (empty) clear it. An ask with more than OPTIONS_MAX options keeps one."""
    body = _body(body)
    migrate()
    with _locked():
        data = _read()
        item = next((i for i in data["items"] if i.get("id") == item_id), None)
        if item is None:
            raise AskError(f"no ask {item_id}")
        if not body and len(item.get("options") or []) > OPTIONS_MAX:
            raise AskError(f"{item_id} has more than {OPTIONS_MAX} options, which show only with a body")
        if body:
            item["body"] = body
        else:
            item.pop("body", None)
        _save(data)
    return item


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
        for k in ("option", "text", "head"):
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
        pill = _PILL.match(a["item"])
        if a["item"].startswith("w-"):
            entry["worker"] = a["item"][2:]
        elif pill:
            entry["pr"] = f"{pill.group(1)}#{pill.group(2)}"
            entry["url"] = f"https://github.com/{pill.group(1)}/pull/{pill.group(2)}"
        elif item:
            entry["ask"] = {k: item[k] for k in ("kind", "text", "why", "pr", "body") if item.get(k)}
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
            elif pill and a.get("option") in PR_OPTIONS:
                opt = dict(PR_OPTIONS[a["option"]])
                if a["option"] == "go":
                    if _HEAD.match(a.get("head") or ""):
                        entry["head"] = a["head"]
                    else:
                        opt = {"id": "go-without-head", "label": "Merge", "do": "dismiss"}  # no head, no go
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
                _remove_previews(item_id)
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
