"""athena's items: everything the human asked for or needs to know about, and their answers from the dashboard.

`needs-you.json` holds the items (plan §3: today's asks, grown up and stored the same way). An item has a short name
(2 to 4 words), a kind (decision, request, result, promise or incident), the owner of its next move, an age, an
optional due date, the human's own words, links to its workers and PR, and a timeline (`log`). `athena item` (alias
`athena ask`) adds and edits them in place, keeping their id; the dashboard writes only what works without HQ: Done
and Snooze (dash/items.go), under the same `ask.lock`. A resolved item stays, with when and how it was resolved, for
the Done list and the weekly check; one resolved over KEEP_RESOLVED ago moves to `items-archive.jsonl`.
`actions.jsonl`: the human's answers from the dashboard. The dash appends a line per answer (dash/actions.go), and a
later line with only its seq and a status moves it on: queued -> done (athena acted) or cancelled (the human undid it
within SETTLE seconds). `athena ask done` compacts it. Both writers hold `ask.lock`. A default that applied on its own
(decision 4) is an answer the dash queued with `auto`, for the item's recommended option.

`athena watch` rings HQ with `dash-queue <seq>` once an answer settles (Doorbell); HQ then reads `athena ask queue`.
An answer's item is an item's id, `w-<worker>` for a live worker's row, or `pr:owner/name#n` for a PR: on the Awaiting
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

# Items (plan §3). The dash mirrors these in dash/state.go; tests/test_asks.py checks the two agree.
KINDS = ("decision", "request", "result", "promise", "incident")
LEGACY_KINDS = {"question": "decision", "note": "result"}  # an ask from before items: a note with options is a decision
NEXT = ("you", "athena", "worker", "reviewer", "customer", "colleague", "ci")
NEXT_BY_KIND = {"decision": "you", "result": "you", "request": "athena", "promise": "athena", "incident": "athena"}
# Calls a default never applies to on its own (decision 4): the human makes each of these.
STAKES = ("security", "customer", "money", "one-way")
NAME_MAX, NAME_WORDS_MIN, NAME_WORDS_MAX = 32, 2, 4
WORDS_MAX, LOG_MAX, LINKS_MAX, WORKERS_MAX, NOTE_MAX = 2000, 50, 10, 6, 200
KEEP_RESOLVED = 7 * 24 * 3600  # a resolved item stays in needs-you.json this long, then moves to items-archive.jsonl
UNDO_DEFAULT = "undo-default"  # the answer that takes back a default athena applied on its own
_REQUEST_ID = re.compile(r"^r[1-9][0-9]{0,9}$")
_WORKER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


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


def archive_file():
    return paths.state_dir() / "items-archive.jsonl"


_NAME_SKIP = re.compile(r"[*`_~\[\]()\\]")
_NAME_STOP = {"a", "an", "the", "to", "of", "on", "in", "for", "and", "or", "is", "are", "do", "we", "it", "this", "that",
              "fyi", "rec", "re", "should", "can", "could", "would", "will", "please", "now", "yes", "no"}


def derive_name(text) -> str:
    """A name for an item that has none (one from before names): the words before a leading "Name:", else the first
    few words that carry meaning. HQ renames it in triage (`athena item edit <id> --name`)."""
    t = _PAREN_TAIL.sub("", _LINK.sub(lambda m: m.group(1) or "", text or ""))  # "(rec B)" names nothing
    t = _NAME_SKIP.sub("", t).strip()
    head, colon, _ = t.partition(":")
    words = head.split() if colon and 1 <= len(head.split()) <= NAME_WORDS_MAX else []
    if len(words) < NAME_WORDS_MIN:
        rest = [w.strip(".,;:!?\"'") for w in t.split()]
        words = words + [w for w in rest if w and w.lower() not in _NAME_STOP and w not in words]
    name = ""
    for w in words[:NAME_WORDS_MAX]:
        if len(name) + len(w) + 1 > NAME_MAX:
            break
        name = f"{name} {w}".strip()
    return name or "Untitled item"


def check_name(name) -> str:
    """2 to 4 words you would recognise, at most NAME_MAX characters: "⌘K palette", "Disk cleanup"."""
    name = " ".join((name or "").split())
    if not (NAME_WORDS_MIN <= len(name.split()) <= NAME_WORDS_MAX) or len(name) > NAME_MAX:
        raise AskError(f"a name is {NAME_WORDS_MIN} to {NAME_WORDS_MAX} words you would recognise, at most {NAME_MAX} "
                       f"characters (\"Disk cleanup\", \"System One on clouds\"), not {name!r}")
    return name


def normal_kind(kind, options=()) -> str:
    """An item's kind; a legacy question is a decision, and a legacy note one too when it offers options."""
    if kind in KINDS:
        return kind
    if kind == "note" and any(o.get("do") != "dismiss" for o in options if isinstance(o, dict)):
        return "decision"
    return LEGACY_KINDS.get(kind, "decision")


def normalize(item) -> bool:
    """Bring an item from before items up to date in place, losing nothing: a name (marked derived), a kind, the next
    move's owner, `updated`. Returns whether it changed."""
    before = json.dumps(item, sort_keys=True)
    kind = normal_kind(item.get("kind"), item.get("options") or [])
    if kind != item.get("kind"):
        item["kind"] = kind
    if not isinstance(item.get("name"), str) or not item["name"].strip():
        item["name"] = derive_name(item.get("text", ""))
        item["name_derived"] = True
    if item.get("next") not in NEXT:
        item["next"] = NEXT_BY_KIND[kind]
    if not isinstance(item.get("created"), (int, float)):
        item["created"] = int(time.time())
    if not isinstance(item.get("updated"), (int, float)):
        item["updated"] = item["created"]
    return json.dumps(item, sort_keys=True) != before


def is_open(item) -> bool:
    return not item.get("resolved")


def _log(item, text, now):
    log = item.setdefault("log", [])
    log.append({"ts": int(now), "text": text[:NOTE_MAX]})
    del log[:-LOG_MAX]


def _archive(data, now):
    """Move items resolved over KEEP_RESOLVED ago to items-archive.jsonl, previews and all gone. Under the lock."""
    old = [i for i in data["items"] if i.get("resolved") and now - i["resolved"] > KEEP_RESOLVED]
    if not old:
        return
    fd = os.open(archive_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, "".join(json.dumps(i, sort_keys=True) + "\n" for i in old).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    data["items"] = [i for i in data["items"] if i not in old]
    for i in old:
        _remove_previews(i.get("id"))


def archived() -> list:
    """Items resolved long ago, oldest first: what the weekly check and a search read besides needs-you.json."""
    try:
        text = archive_file().read_text()
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


def migrate_items(dry_run=False):
    """Bring every item in needs-you.json up to items (plan §3), once, keeping each as it was, its id included; a copy
    of the file from before goes to needs-you.json.v1. Returns {before, after, changed}: counts, and the ids touched."""
    migrate()
    with _locked():
        try:
            raw = json.loads(asks_file().read_text())
            before = len(raw.get("items", [])) if isinstance(raw, dict) else 0
        except (OSError, ValueError):
            return {"before": 0, "after": 0, "changed": []}
        data = _read()
        changed = [i.get("id") for i in data["items"] if normalize(i)]
        if changed and not dry_run:
            backup = asks_file().with_name("needs-you.json.v1")
            if not backup.exists():
                _write_atomic(backup, asks_file().read_text())
            _save(data)
    return {"before": before, "after": len(data["items"]), "changed": changed}


_DUE_REL = re.compile(r"^\+(\d{1,4})([hd])$")


def parse_due(text, now=None):
    """A due date: YYYY-MM-DD (09:00 local that day), YYYY-MM-DDTHH:MM (local), +Nh or +Nd from now; none clears it."""
    now = time.time() if now is None else now
    text = (text or "").strip()
    if text in ("", "none"):
        return None
    m = _DUE_REL.match(text)
    if m:
        return int(now + int(m.group(1)) * (3600 if m.group(2) == "h" else 86400))
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = time.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            t = time.strptime(text + " 09:00", "%Y-%m-%d %H:%M")
        return int(time.mktime(t))
    raise AskError(f"--due takes YYYY-MM-DD, YYYY-MM-DDTHH:MM, +Nh, +Nd or none, not {text!r}")


def _link(spec):
    """`title|url` or a bare http(s) URL."""
    title, bar, url = spec.rpartition("|") if "|" in spec else ("", "", spec)
    url = url.strip()
    if not re.match(r"^https?://\S+$", url):
        raise AskError(f"a link is title|https://... or a bare URL, not {spec!r}")
    return {"title": " ".join(title.split())[:80] or url, "url": url}


def _check_default(item):
    """A default applies on its own only to a reversible, low-stakes call: never one marked security, customer,
    money or one-way, and only for a recommended option the item offers."""
    if not item.get("auto"):
        return
    if item.get("stakes"):
        raise AskError(f"no default applies on its own to a call marked {', '.join(item['stakes'])}: "
                       "the human makes it (decision 4)")
    if not item.get("rec") or item["rec"] not in {o.get("id") for o in item.get("options") or []}:
        raise AskError("--auto needs --rec naming one of the item's options")
    if item.get("kind") != "decision":
        raise AskError("--auto is for a decision")


def _apply_fields(item, now, *, name=None, kind=None, next=None, due=None, words=None, requests=(), workers=(),
                  links=(), rec=None, auto=None, stakes=None):
    """The item fields add and edit share. None leaves a field as it is."""
    changed = []
    if name is not None:
        item["name"] = check_name(name)
        item.pop("name_derived", None)
        changed.append("name")
    if kind is not None:
        if kind not in KINDS and kind not in LEGACY_KINDS:
            raise AskError(f"a kind is one of {', '.join(KINDS)}, not {kind!r}")
        item["kind"] = normal_kind(kind, item.get("options") or [])
        changed.append("kind")
    if next is not None:
        if next not in NEXT:
            raise AskError(f"--next is one of {', '.join(NEXT)}, not {next!r}")
        item["next"] = next
        changed.append("next move")
    if due is not None:
        ts = parse_due(due, now)
        if ts is None:
            item.pop("due", None)
        else:
            item["due"] = ts
        changed.append("due date")
    if words is not None:
        words = (words or "").strip()
        if len(words) > WORDS_MAX:
            raise AskError(f"--words is the human's own words, at most {WORDS_MAX} characters")
        if words:
            item["words"] = words
        else:
            item.pop("words", None)
        changed.append("your words")
    for r in requests:
        if not _REQUEST_ID.match(r):
            raise AskError(f"--request takes a request id from `athena item requests` (r12), not {r!r}")
        if r not in item.setdefault("requests", []):
            item["requests"].append(r)
            changed.append(f"request {r}")
    for w in workers:
        if not _WORKER.match(w):
            raise AskError(f"--worker takes a worker's name, not {w!r}")
        if w not in item.setdefault("workers", []):
            item["workers"].append(w)
            changed.append(f"worker {w}")
    if len(item.get("workers") or []) > WORKERS_MAX:
        raise AskError(f"at most {WORKERS_MAX} workers")
    for spec in links:
        link = _link(spec)
        if link not in item.setdefault("links", []):
            item["links"].append(link)
            changed.append("link")
    if len(item.get("links") or []) > LINKS_MAX:
        raise AskError(f"at most {LINKS_MAX} links")
    for k in ("requests", "workers", "links"):
        if not item.get(k):
            item.pop(k, None)
    if rec is not None:
        if rec:
            item["rec"] = rec
        else:
            item.pop("rec", None)
        changed.append("recommendation")
    if stakes is not None:
        bad = [s for s in stakes if s not in STAKES]
        if bad:
            raise AskError(f"--stakes is any of {', '.join(STAKES)}, not {', '.join(bad)}")
        if stakes:
            item["stakes"] = sorted(set(stakes))
        else:
            item.pop("stakes", None)
        changed.append("stakes")
    if auto is not None:
        if auto:
            item["auto"] = True
        else:
            item.pop("auto", None)
        changed.append("default")
    if item.get("stakes") and auto is None and item.pop("auto", None):
        changed.append("default off: the call is marked " + ", ".join(item["stakes"]))
    _check_default(item)
    return changed


def items(import_legacy=True, resolved=False):
    """The open items, each brought up to date. `import_legacy=False` (a dry run) writes nothing: no import of a
    needs-you.md, no migration. `resolved` includes the items resolved but not yet archived."""
    if import_legacy:
        migrate_items()
    out = _read()["items"]
    for i in out:
        normalize(i)
    return out if resolved else [i for i in out if is_open(i)]


def legacy_lines() -> list:
    """The lines a needs-you.md not yet imported holds."""
    try:
        return _legacy_lines(_legacy_file().read_text())
    except OSError:
        return []


def show(item_id):
    for item in items(resolved=True):
        if item.get("id") == item_id:
            return item
    for item in reversed(archived()):
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


def add(text, kind=None, why="", pr=None, options=(), body="", previews=(), name=None, next=None, due=None,
        words=None, requests=(), workers=(), links=(), rec=None, auto=False, stakes=(), fyi=False, now=None):
    """Add an item; returns its id. Its text is the title, one short sentence: the call for a decision, where it
    stands for anything else. A decision gets Discuss and Reply on the dashboard; a result waits for the human's Done.
    body is the Markdown its full view shows, and previews are files or folders of local HTML it embeds, copied into
    the state dir. Without a name one is derived from the text (`athena item add` requires one). `fyi` files a tiny
    result already resolved, a line in Done. `auto` lets the recommended option `rec` apply on its own (decision 4)."""
    now = time.time() if now is None else now
    text = _title(text)
    why = " ".join((why or "").split())
    body = _body(body)
    if len(why) > WHY_MAX:
        raise AskError(f"--why is one sentence, at most {WHY_MAX} characters")
    most = OPTIONS_MAX_BODY if body else OPTIONS_MAX
    if len(options) > most:
        more = f"; up to {OPTIONS_MAX_BODY} with a body (--body)" if not body else ""
        raise AskError(f"at most {most} options{more}: Discuss in Athena and Reply… come with every question")
    if kind is not None and kind not in KINDS and kind not in LEGACY_KINDS:
        raise AskError(f"a kind is one of {', '.join(KINDS)}, not {kind!r}")
    dos = [{"do": (spec.split(":") + ["", ""])[2] or "reply"} for spec in options]
    kind = normal_kind(kind or ("question" if asks_question(text) else "note"), dos)
    for spec in options:
        label = spec.split(":")[0].strip()
        if kind == "decision" and re.match(DISCUSS_LABEL, label):
            raise AskError(f"no {label!r} option: the dashboard gives every decision Discuss in Athena")
    taken = set()
    opts = [_option(o, taken) for o in options]
    item = {"kind": kind, "text": text, "why": why, "options": opts}
    if body:
        item["body"] = body
    key = _pr_key(pr)
    if key:
        item["pr"] = key
    if name is None:
        item["name"], item["name_derived"] = derive_name(text), True
    _apply_fields(item, now, name=name, next=next, due=due, words=words, requests=requests, workers=workers,
                  links=links, rec=rec, auto=auto or None, stakes=list(stakes) or None)
    item.setdefault("next", NEXT_BY_KIND[kind])
    if rec and rec not in {o["id"] for o in opts}:
        raise AskError(f"--rec names one of the item's options ({', '.join(o['id'] for o in opts) or 'it has none'})")
    plan = _plan_previews(list(previews))
    if plan:
        item["previews"] = [entry for entry, _, _ in plan]
    migrate()
    incoming = _copy_previews(plan) if plan else None
    try:
        with _locked():
            data = _read()
            item = {"id": f"a{data['next']}", **item, "created": int(now), "updated": int(now)}
            _log(item, "filed", now)
            if fyi:
                item.update(resolved=int(now), resolved_note="FYI")
            if incoming:  # in place before the item is saved, so the dash never links a preview that is not there
                _remove_previews(item["id"])
                os.rename(incoming, previews_dir() / item["id"])
                incoming = None
            try:
                data["next"] += 1
                data["items"].append(item)
                _archive(data, now)
                _save(data)
            except BaseException:
                if plan:
                    _remove_previews(item["id"])
                raise
    finally:
        if incoming:
            shutil.rmtree(incoming, ignore_errors=True)
    return item["id"]


def _edit(item_id, change, now=None):
    """Run change(item, now) on an item under the lock and save it; returns the item. The item keeps its id."""
    now = time.time() if now is None else now
    migrate_items()
    with _locked():
        data = _read()
        item = next((i for i in data["items"] if i.get("id") == item_id), None)
        if item is None:
            raise AskError(f"no item {item_id}")
        normalize(item)
        change(item, now)
        item["updated"] = int(now)
        _archive(data, now)
        _save(data)
    return item


def edit(item_id, text=None, why=None, pr=None, options=None, now=None, **fields):
    """Edit an item in place: it keeps its id, its age and its place, and its timeline says what changed. `fields`
    are those add takes (name, kind, next, due, words, requests, workers, links, rec, auto, stakes); None leaves one
    as it is. `options` replaces the options; `pr` "" unlinks the PR."""
    def change(item, now):
        changed = []
        if text is not None:
            item["text"] = _title(text)
            changed.append("title")
        if why is not None:
            w = " ".join(why.split())
            if len(w) > WHY_MAX:
                raise AskError(f"--why is one sentence, at most {WHY_MAX} characters")
            item["why"] = w
            changed.append("why")
        if pr is not None:
            key = _pr_key(pr)
            if key:
                item["pr"] = key
            else:
                item.pop("pr", None)
            changed.append("PR")
        if options is not None:
            most = OPTIONS_MAX_BODY if item.get("body") else OPTIONS_MAX
            if len(options) > most:
                raise AskError(f"at most {most} options")
            taken = set()
            item["options"] = [_option(o, taken) for o in options]
            if item.get("rec") and item["rec"] not in taken:
                item.pop("rec", None)
                item.pop("auto", None)
            changed.append("options")
        changed += _apply_fields(item, now, **fields)
        if not changed:
            raise AskError("nothing to change: name a field (athena item edit -h)")
        _log(item, "edited: " + ", ".join(changed), now)
    return _edit(item_id, change, now)


def snooze(item_id, until, now=None):
    """Hide an open item until `until` (a due-date string, or unix seconds); it comes back unread. New activity on
    it (an edit) cancels the snooze. `until` None wakes it now."""
    def change(item, now):
        if not is_open(item):
            raise AskError(f"{item_id} is resolved")
        if until is None:
            item.pop("snooze", None)
            _log(item, "snooze cancelled", now)
            return
        ts = until if isinstance(until, (int, float)) else parse_due(until, now)
        if not ts or ts <= now:
            raise AskError("snooze until a time still to come")
        item["snooze"] = {"until": int(ts), "at": int(now) + 1}  # past this edit's own `updated`
        _log(item, "snoozed until " + time.strftime("%a %d %b %H:%M", time.localtime(ts)), now)
    return _edit(item_id, change, now)


def set_body(item_id, body, now=None):
    """Attach a body to an open item, or replace or (empty) clear it. One with more than OPTIONS_MAX options keeps one."""
    body = _body(body)

    def change(item, now):
        if not body and len(item.get("options") or []) > OPTIONS_MAX:
            raise AskError(f"{item_id} has more than {OPTIONS_MAX} options, which show only with a body")
        if body:
            item["body"] = body
        else:
            item.pop("body", None)
        _log(item, "edited: body", now)
    return _edit(item_id, change, now)


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
        for k in ("discuss", "auto", "rule"):
            if m.get(k) is True:
                a[k] = True
        a["changed"] = a["ts"]
        out[seq] = a
    return dict(sorted(out.items()))


def _settled(now=None):
    now = time.time() if now is None else now
    return [a for a in fold(_lines()).values() if a["status"] == "queued" and now - a["ts"] >= SETTLE]


def queue(now=None):
    """The answers athena has to act on, in seq order: each with what it answers (the item, or the worker of a row)
    and the answer itself (an option with its label and do, a reply's text, or discuss). `auto`: a default the dash
    applied on its own; `rule`: the human ticked "Don't ask me this again" with it; option `undo-default`: take back
    the default athena applied."""
    asks = {i.get("id"): i for i in items(resolved=True)}
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
            entry["ask"] = {k: item[k] for k in ("name", "kind", "text", "why", "pr", "body", "words", "rec") if item.get(k)}
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
            elif a.get("option") == UNDO_DEFAULT and item and item.get("applied"):
                opt = {"id": UNDO_DEFAULT, "label": "Undo " + item["applied"].get("label", ""), "do": "reply"}
            elif pill and a.get("option") in PR_OPTIONS:
                opt = dict(PR_OPTIONS[a["option"]])
                if a["option"] == "go":
                    if _HEAD.match(a.get("head") or ""):
                        entry["head"] = a["head"]
                    else:
                        opt = {"id": "go-without-head", "label": "Merge", "do": "dismiss"}  # no head, no go
            entry["option"] = opt or {"id": a.get("option")}
        for k in ("auto", "rule"):
            if a.get(k):
                entry[k] = True
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


def done(item_id, keep=False, note="", now=None):
    """athena acted on `item_id`: its settled answers are done, and the item resolves, with `note` on its timeline
    ("you merged it", "you replied in chat"), unless `keep` (then the dashboard shows it again, say after a discussion
    that left it open). A default that applied on its own does not resolve its item: it moves to Ready for you, saying
    so, until the human clicks Done or Undo; an Undo done puts the decision back, its default off. Compacts
    actions.jsonl. A resolved item stays KEEP_RESOLVED for the Done list, then moves to items-archive.jsonl."""
    now = time.time() if now is None else now
    note = " ".join((note or "").split())[:NOTE_MAX]
    migrate_items()
    resolved = applied = False
    with _locked():
        settled = [a for a in _settled(now) if a["item"] == item_id]
        seqs = [a["seq"] for a in settled]
        if seqs:
            _append([{"seq": s, "ts": now, "status": "done"} for s in seqs])
        data = _read()
        item = next((i for i in data["items"] if i.get("id") == item_id), None)
        if item is not None and is_open(item):
            normalize(item)
            labels = {o.get("id"): o.get("label") for o in item.get("options") or []}
            auto = [a for a in settled if a.get("auto")]
            undone = [a for a in settled if a.get("option") == UNDO_DEFAULT]
            if undone:
                item.pop("applied", None)
                item.pop("auto", None)
                _log(item, "default undone: the call is yours again", now)
            elif auto:
                opt = auto[-1].get("option")
                item["applied"] = {"option": opt, "label": labels.get(opt) or opt, "ts": int(now)}
                item.pop("auto", None)
                applied = True
                _log(item, f"athena took the rec on its own: {labels.get(opt) or opt}", now)
            elif keep:
                if settled:
                    _log(item, "answered; kept open", now)
            else:
                item.update(resolved=int(now))
                if note:
                    item["resolved_note"] = note
                item.pop("snooze", None)
                item.pop("auto", None)
                resolved = True
                _log(item, "resolved" + (f": {note}" if note else ""), now)
            item["updated"] = int(now)
        _archive(data, now)
        _save(data)
        _compact(now)
    out = {"item": item_id, "resolved": resolved, "done": seqs}
    if applied:
        out["applied"] = True
    return out


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
