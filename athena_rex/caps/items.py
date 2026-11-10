"""Items: everything asked of or promised to the human, in needs-you.json, shared with the old athena; the dashboard's
answers in actions.jsonl, read as a queue; and the log of what the human typed into HQ.

All writes hold ask.lock, the lock the old athena and any dashboard use. An unknown field on an item is kept.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import bus, caps, store

SUMMARY = "items on the dashboard, its answer queue, and the request log"
KINDS = ("decision", "request", "result", "promise", "incident")
NEXT = ("you", "athena", "worker", "reviewer", "customer", "colleague", "ci")
SETTLE = 6.0
ARCHIVE_AFTER = 7 * 86400
TITLE_MAX = 120
BODY_MAX = 8000
TEXT_MAX = 4000
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abeprs]-[A-Za-z0-9-]{10,}"
                     r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
NOT_HUMAN = ("athena watch ", "athena watch:", "athena doctor", "athena fallback sweep", "Start your HQ routine",
             "HQ restarted:", "Fresh HQ after", "/", "<", "dash-queue ", "slack-new ", "athena promise ",
             "athena recheck ", "athena check:", "[Cross-session ")


def redacted(text) -> str:
    return _SECRET.sub("[secret removed]", text or "")


def _file():
    return store.path("needs-you.json")


def _readable(target):
    data = store.read_json(target, None)
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def _previous():
    return _file().with_name("needs-you.json.prev")


def load() -> dict:
    """A file that won't parse reads as its last generation, marked, so nothing can be saved over it."""
    data = _readable(_file())
    if data is not None:
        return data
    if not _file().exists():
        return {"next": 1, "items": [], "unreadable": False}
    return {"next": 1, "items": [], **(_readable(_previous()) or {}), "unreadable": True}


def _refuse_unreadable(data):
    if data.get("unreadable"):
        raise UserError(f"{_file()} does not parse; nothing was written. Fix it, or restore {_previous()}")


def save(data):
    """Refuses to replace a file it could not read: a parse failure must not become an empty list on disk."""
    _refuse_unreadable(data)
    data.pop("unreadable", None)
    if _readable(_file()) is not None:
        store.write_text(_previous(), _file().read_text(), mode=0o600)
    store.write_json(_file(), data, indent=1, mode=0o600)


def is_open(item) -> bool:
    return not item.get("resolved")


def snooze_until(item) -> float:
    """Written as the old athena does, {until, at}. A plain number was this build's own first form."""
    value = item.get("snooze")
    return float((value.get("until") if isinstance(value, dict) else value) or 0)


def snoozed(item, now) -> bool:
    return snooze_until(item) > now


def open_items() -> list:
    return [i for i in load()["items"] if is_open(i)]


def find(data, item_id):
    for item in data["items"]:
        if item.get("id") == item_id:
            return item
    raise UserError(f"no item {item_id}")


def _log(item, text, now):
    item.setdefault("log", []).append({"ts": int(now), "text": text})
    item["updated"] = int(now)


def derive_name(text) -> str:
    words = re.findall(r"[\w'’-]+", text or "")[:4]
    return " ".join(words) or "unnamed"


def _check_title(text):
    text = " ".join((text or "").split())
    if not text:
        raise UserError("an item needs a title")
    if len(text) > TITLE_MAX:
        raise UserError(f"the title is {len(text)} characters; at most {TITLE_MAX}. The rest goes in --body")
    return text


def _check_default(item):
    if item.get("auto"):
        if not item.get("rec"):
            raise UserError("--auto needs --rec")
        if item.get("stakes"):
            raise UserError("a call with stakes is never auto")


ICONS = ("check x send message-circle bell clock timer rotate-ccw play pause git-merge git-pull-request eye thumbs-up thumbs-down "
         "file-plus ticket flag archive trash-2 link external-link at-sign sparkles chevron-down").split()
LABEL_MAX = 16
STAKES = ("security", "customer", "money", "one-way")
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_KEY = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")


def _option(spec, taken) -> dict:
    """`label[:icon[:do]]`, do being reply (the default: athena acts on it) or dismiss (nothing to do), as the old athena reads it."""
    label, icon, do = (str(spec).split(":") + ["", ""])[:3]
    label = label.strip()
    if not label or len(label) > LABEL_MAX:
        raise UserError(f"an option's label is 1 to {LABEL_MAX} characters: {label!r}")
    do = do or "reply"
    if do not in ("reply", "dismiss"):
        raise UserError(f"an option's do is reply or dismiss, not {do!r}")
    if icon and icon not in ICONS:
        print(f"athena-rex item: no icon {icon!r}; the option shows none. Icons: {' '.join(ICONS)}", file=sys.stderr)
        icon = ""
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:28] or "option"
    oid, n = base, 2
    while oid in taken or oid == "dismiss":
        oid, n = f"{base}-{n}", n + 1
    taken.add(oid)
    return {"id": oid, "label": label, "do": do, **({"icon": icon} if icon else {})}


PREVIEWS_MAX, PREVIEW_BYTES_MAX, PREVIEW_FILES_MAX = 4, 20 << 20, 500


def previews_dir():
    return store.path("previews")


def _preview_name(src) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", src.name).strip("-.") or "preview")[:100]


def plan_previews(srcs) -> list:
    """[(entry, source, files)] for each preview, checked before anything is copied: files only, no symlinks, a folder with its
    index.html, at most 20 MB and 500 files in all. A folder's entry ends in /."""
    import os
    from pathlib import Path
    if len(srcs) > PREVIEWS_MAX:
        raise UserError(f"at most {PREVIEWS_MAX} previews")
    plan, budget = [], [PREVIEW_BYTES_MAX, PREVIEW_FILES_MAX]

    def spend(path):
        budget[0] -= path.stat().st_size
        budget[1] -= 1
        if budget[0] < 0 or budget[1] < 0:
            raise UserError(f"previews are at most {PREVIEW_BYTES_MAX >> 20} MB and {PREVIEW_FILES_MAX} files in all")
    for raw in srcs:
        src = Path(os.path.expanduser(raw))
        if src.is_symlink():
            raise UserError(f"--preview {raw} is a symlink; give the file or folder itself")
        name = _preview_name(src)
        if src.is_dir():
            files = []
            for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for d in dirnames:
                    if (Path(dirpath) / d).is_symlink():
                        raise UserError(f"--preview {raw}: a symlink inside; a preview copies files only")
                for n in sorted(f for f in filenames if not f.startswith(".")):
                    path = Path(dirpath) / n
                    if path.is_symlink() or not path.is_file():
                        raise UserError(f"--preview {raw}: {path.relative_to(src)} is not a regular file")
                    spend(path)
                    files.append(path.relative_to(src))
            if Path("index.html") not in files:
                raise UserError(f"--preview {raw}: a folder opens on its index.html, and it has none")
            plan.append((name + "/", src, files))
        elif src.is_file():
            spend(src)
            plan.append((name, src, None))
        else:
            raise UserError(f"--preview {raw}: no such file or folder")
    names = [entry.rstrip("/") for entry, _, _ in plan]
    if len(set(names)) != len(names):
        raise UserError("two previews with one name")
    return plan


def stage_previews(plan):
    """Copied outside ask.lock into a folder of their own, renamed into place under it once the item has its id."""
    import tempfile
    previews_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=".staged-", dir=previews_dir()))
    try:
        copy_previews(root, plan)
    except BaseException:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def copy_previews(root, plan) -> list:
    import os
    import shutil
    for entry, src, files in plan:
        if files is None:
            shutil.copyfile(src, root / entry, follow_symlinks=False)
            continue
        for rel in files:
            target = root / entry / rel
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(src / rel, target, follow_symlinks=False)
            os.chmod(target, 0o600)
    return [entry for entry, _, _ in plan]


def remove_previews(item_id):
    import shutil
    if store.valid_name(str(item_id)):
        shutil.rmtree(previews_dir() / str(item_id), ignore_errors=True)


def _options(specs) -> list:
    taken = set()
    return [_option(spec, taken) for spec in specs or []]


def parse_due(text, now=None):
    """YYYY-MM-DD (09:00 local that day), YYYY-MM-DDTHH:MM (local), +Nh or +Nd from now; none clears it. Unix seconds, as the old athena keeps it."""
    now = time.time() if now is None else now
    text = str(text or "").strip()
    if text in ("", "none"):
        return None
    found = re.fullmatch(r"\+(\d{1,4})([hd])", text)
    if found:
        return int(now + int(found.group(1)) * (3600 if found.group(2) == "h" else 86400))
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            stamp = time.strptime(text + (" 09:00" if fmt == "%Y-%m-%d" else ""), fmt + (" %H:%M" if fmt == "%Y-%m-%d" else ""))
        except ValueError:
            continue
        return int(time.mktime(stamp))
    raise UserError(f"--due takes YYYY-MM-DD, YYYY-MM-DDTHH:MM, +Nh, +Nd or none, not {text!r}")


def parse_link(spec) -> dict:
    """`title|url` or a bare http(s) URL."""
    title, _, url = spec.rpartition("|") if "|" in spec else ("", "", spec)
    url = url.strip()
    if not re.match(r"^https?://\S+$", url):
        raise UserError(f"a link is title|https://... or a bare URL, not {spec!r}")
    return {"title": " ".join(title.split())[:80] or url, "url": url}


def parse_stakes(values) -> list:
    out = [s.strip() for value in values or [] for s in str(value).split(",") if s.strip()]
    wrong = [s for s in out if s not in STAKES]
    if wrong:
        raise UserError(f"--stakes takes {', '.join(STAKES)}, not {', '.join(wrong)}")
    return out


def pr_key(pr):
    """owner/name#n from a PR URL or key; '' unlinks."""
    pr = str(pr or "").strip()
    if not pr:
        return ""
    found = _PR_URL.fullmatch(pr.rstrip("/")) or _PR_KEY.fullmatch(pr)
    if not found:
        raise UserError(f"--pr takes a GitHub PR URL or owner/name#n, not {pr!r}")
    return f"{found.group(1)}#{found.group(2)}"


def add(text, name=None, kind=None, next_=None, why="", body="", options=(), rec=None, auto=False, stakes=(), due=None,
        words=None, requests=(), workers=(), pr=None, links=(), fyi=False, now=None, previews=()) -> dict:
    now = time.time() if now is None else now
    text = _check_title(text)
    if len(body or "") > BODY_MAX:
        raise UserError(f"the body is {len(body)} characters; at most {BODY_MAX}")
    kind = kind or ("decision" if options else "request")
    if kind not in KINDS:
        raise UserError(f"--kind is one of {', '.join(KINDS)}")
    next_ = next_ or ("you" if kind in ("decision", "result") else "athena")
    if next_ not in NEXT:
        raise UserError(f"--next is one of {', '.join(NEXT)}")
    opts = _options(options)
    if len(opts) > (4 if body else 2):
        raise UserError("at most 2 options, 4 with a body")
    due = parse_due(due, now) if isinstance(due, str) else due
    links = [parse_link(link) if isinstance(link, str) else link for link in links or ()]
    stakes = parse_stakes(stakes)
    pr = pr_key(pr) if pr else pr
    plan = plan_previews(list(previews or ()))
    staged = stage_previews(plan) if plan else None
    with store.locked("ask.lock"):
        data = load()
        number = int(data.get("next") or 1)
        data["next"] = number + 1
        item = {"id": f"a{number}", "kind": kind, "text": text, "why": why or "", "options": opts,
                "name": name or derive_name(text), "next": next_, "created": int(now), "updated": int(now), "log": []}
        for key, value in (("body", body), ("words", words), ("due", due), ("pr", pr), ("rec", rec),
                           ("requests", list(requests)), ("workers", list(workers)), ("links", list(links)),
                           ("stakes", list(stakes))):
            if value:
                item[key] = value
        if auto:
            item["auto"] = True
        _check_default(item)
        _log(item, "filed", now)
        if staged:
            item["previews"] = [entry for entry, _, _ in plan]
            os.replace(staged, previews_dir() / item["id"])
        if fyi:
            item["resolved"], item["resolved_note"] = int(now), "fyi"
        data["items"].append(item)
        save(data)
    bus.emit("item.added", item=item["id"])
    return item


def edit(item_id, now=None, **fields) -> dict:
    now = time.time() if now is None else now
    with store.locked("ask.lock"):
        data = load()
        item = find(data, item_id)
        changed = []
        for key, value in fields.items():
            if value is None:
                continue
            if key == "text":
                value = _check_title(value)
            elif key == "options":
                most = 4 if item.get("body") else 2
                if len(value) > most:
                    raise UserError(f"at most {most} options")
                value = _options(value)
                if item.get("rec") and item["rec"] not in {o["id"] for o in value}:
                    item.pop("rec", None)
                    item.pop("auto", None)
            elif key == "due":
                value = parse_due(value, now)
            elif key == "links":
                value = [parse_link(link) for link in value]
            elif key == "stakes":
                value = parse_stakes(value)
            elif key == "pr":
                value = pr_key(value)
            elif key == "body" and len(value) > BODY_MAX:
                raise UserError(f"the body is {len(value)} characters; at most {BODY_MAX}")
            if key == "auto" and value is False:
                item.pop("auto", None)
            elif value in ("", None, []) and key in ("due", "pr", "stakes", "body"):
                item.pop(key, None)
            else:
                item[key] = value
            changed.append(key)
        if not changed:
            raise UserError("nothing to change: name a field (athena-rex item edit -h)")
        if item.get("stakes") and item.get("auto") and fields.get("auto") is not True:
            item.pop("auto")
        _check_default(item)
        item.pop("snooze", None)
        _log(item, "edited: " + ", ".join(changed), now)
        save(data)
    return item


def resolve(item_id, note="", keep=False, now=None) -> dict:
    """HQ acted on `item_id`, as the old athena's `item done`: its settled answers are done, and the item resolves with `note`, unless
    `keep`. A default that applied on its own does not resolve it: it moves to Ready for you as `applied`, until the human clicks Done
    or Undo; an undo-default done puts the decision back, its default off. Any answer of the human's turns the default off for good.
    A worker's (`w-`) or a PR's (`pr:`) answers have no item: they are only marked done."""
    now = time.time() if now is None else now
    with store.locked("ask.lock"):
        settled = [a for a in _fold(_action_lines()).values() if a["status"] == "queued" and a.get("item") == item_id
                   and now - a["ts"] >= SETTLE]
        data = None
        if not item_id.startswith(("w-", "pr:")):  # read before anything is marked: a failed read changes nothing
            data = load()
            _refuse_unreadable(data)
            item = find(data, item_id)
        for answer in settled:
            store.append_line(_actions(), {"seq": answer["seq"], "status": "done", "ts": now})
        if data is None:
            return {"item": item_id, "resolved": False, "done": [a["seq"] for a in settled]}
        out = {"item": item_id, "resolved": False, "done": [a["seq"] for a in settled]}
        if is_open(item):
            labels = {o.get("id"): o.get("label") for o in item.get("options") or []}
            auto = [a for a in settled if a.get("auto")]
            undone = [a for a in settled if a.get("option") == UNDO_DEFAULT]
            human = [a for a in settled if not a.get("auto") and a.get("option") != UNDO_DEFAULT]
            if human and item.pop("auto", None):
                _log(item, "default off: you answered", now)
            if undone:
                item.pop("applied", None)
                item.pop("auto", None)
                _log(item, "default undone: the call is yours again", now)
            elif auto:
                option = auto[-1].get("option")
                item["applied"] = {"option": option, "label": labels.get(option) or option, "ts": int(now)}
                item.pop("auto", None)
                out["applied"] = True
                _log(item, f"athena took the rec on its own: {labels.get(option) or option}", now)
            elif keep:
                if settled:
                    _log(item, "answered; kept open", now)
            else:
                item["resolved"] = int(now)
                if note:
                    item["resolved_note"] = note
                item.pop("snooze", None)
                item.pop("auto", None)
                out["resolved"] = True
                _log(item, f"resolved: {note}" if note else "resolved", now)
        _archive(data, now)
        save(data)
    return out


def dash_done(item_id, now=None) -> dict:
    """The dash's own Done, as the old dash's: the human took the last step or read it. It resolves the item and turns its default off,
    and never touches the answer queue: only HQ's `item done` consumes that."""
    now = time.time() if now is None else now
    with store.locked("ask.lock"):
        data = load()
        _refuse_unreadable(data)
        item = find(data, item_id)
        if not is_open(item):
            raise UserError("done already")
        item.update(resolved=int(now), resolved_by="you", resolved_note="you marked it done")
        item.pop("snooze", None)
        item.pop("auto", None)
        _log(item, "you marked it done", now)
        save(data)
    return item


def snooze(item_id, until, now=None) -> dict:
    now = time.time() if now is None else now
    stamp = parse_when(until, now)
    with store.locked("ask.lock"):
        data = load()
        item = find(data, item_id)
        item["snooze"] = {"until": int(stamp), "at": int(now) + 1}
        _log(item, f"snoozed until {time.strftime('%Y-%m-%d %H:%M', time.localtime(stamp))}", now)
        save(data)
    return item


def parse_when(text, now=None) -> float:
    now = time.time() if now is None else now
    text = str(text).strip()
    found = re.fullmatch(r"(\d+)\s*([mhd])", text)
    if found:
        return now + int(found.group(1)) * {"m": 60, "h": 3600, "d": 86400}[found.group(2)]
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            pass
    raise UserError(f"cannot read the time {text!r}: use 2h, 3d or 2026-11-09")


def _archive(data, now):
    keep = []
    for item in data["items"]:
        if item.get("resolved") and now - float(item["resolved"]) > ARCHIVE_AFTER:
            store.append_line(store.path("items-archive.jsonl"), item)
            remove_previews(item.get("id"))
        else:
            keep.append(item)
    data["items"] = keep


def _actions():
    return store.path("actions.jsonl")


def _action_lines() -> list:
    return list(store.read_lines(_actions()))


def _fold(lines) -> dict:
    out = {}
    for line in lines:
        seq = line.get("seq")
        if seq is None:
            continue
        if line.get("status") == "queued":
            out[seq] = dict(line)
        elif seq in out:
            out[seq]["status"] = line.get("status")
    return out


UNDO_DEFAULT = "undo-default"
STUCK_AFTER = 600
PR_OPTIONS = {"recheck": {"id": "recheck", "label": "Re-check and fix", "do": "reply"},
              "request-review": {"id": "request-review", "label": "Request review", "do": "reply"},
              "go": {"id": "go", "label": "Merge", "do": "reply"},
              "post-open-prs": {"id": "post-open-prs", "label": "Post to #open-prs", "do": "reply"}}
SENT_TEXT = {"recheck": "re-check sent", "request-review": "review request sent", "go": "merge go sent", "post-open-prs": "posting…"}
_PILL = re.compile(r"^pr:([a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100})#([1-9][0-9]{0,9})$")
_HEAD = re.compile(r"^[0-9a-f]{7,40}$")


def answer(item_id, now=None, hides=True, **fields) -> dict:
    """An answer from the page: queued for HQ, as the human or a default that applied gave it. As the old dash: an item whose answer hides
    it takes one at a time, and so does a PR's button (Re-check, Request review, Post, Merge); a worker's row or a Discuss takes any
    number. An answer stuck past STUCK_AFTER takes a new one."""
    now = time.time() if now is None else now
    with store.locked("ask.lock"):
        lines = _fold(_action_lines())
        for a in lines.values():
            if a.get("item") != item_id or a["status"] != "queued" or now - float(a["ts"]) >= STUCK_AFTER:
                continue
            if hides or SENT_TEXT.get(fields.get("option")) and SENT_TEXT.get(a.get("option")):
                raise UserError(f"{item_id} is already answered")
        seq = max(lines, default=0) + 1
        record = {"seq": seq, "ts": now, "item": item_id, "status": "queued", **fields}
        store.append_line(_actions(), record)
    return record


def undo(seq, now=None) -> bool:
    now = time.time() if now is None else now
    with store.locked("ask.lock"):
        found = _fold(_action_lines()).get(seq)
        if not found or found["status"] != "queued" or now - found["ts"] > SETTLE:
            return False
        store.append_line(_actions(), {"seq": seq, "status": "cancelled", "ts": now})
    return True


def queue(now=None, permissions=False) -> list:
    """Settled answers, oldest first; each with what it answers, so HQ needs no second read. An answer to a permission prompt is pressed
    by `permissions` and isn't HQ's to read."""
    now = time.time() if now is None else now
    known = {i.get("id"): i for i in load()["items"]}
    out = []
    for answered in sorted(_fold(_action_lines()).values(), key=lambda a: a["seq"]):
        if answered["status"] != "queued" or now - answered["ts"] < SETTLE:
            continue
        item = known.get(answered.get("item"))
        if bool((item or {}).get("permission")) != permissions:
            continue
        entry = {"seq": answered["seq"], "item": answered.get("item"), "at": answered["ts"]}
        pill = _PILL.match(str(answered.get("item") or ""))
        if str(answered.get("item", "")).startswith("w-"):
            entry["worker"] = answered["item"][2:]
        elif pill:
            entry["pr"] = f"{pill.group(1)}#{pill.group(2)}"
            entry["url"] = f"https://github.com/{pill.group(1)}/pull/{pill.group(2)}"
        elif item:
            entry["ask"] = {k: item[k] for k in ("name", "kind", "text", "why", "pr", "body", "words", "rec")
                            if item.get(k)}
        else:
            entry["ask"] = None
        if answered.get("discuss"):
            entry["discuss"] = True
        elif "text" in answered:
            entry["reply"] = answered["text"]
        elif "option" in answered:
            option = next((o for o in (item or {}).get("options", []) if o.get("id") == answered["option"]), None)
            if answered["option"] == UNDO_DEFAULT and item and item.get("applied"):
                option = {"id": UNDO_DEFAULT, "label": "Undo " + str(item["applied"].get("label") or ""), "do": "reply"}
            elif pill and answered["option"] in PR_OPTIONS:
                option = dict(PR_OPTIONS[answered["option"]])
                if answered["option"] == "go":
                    if _HEAD.match(str(answered.get("head") or "")):
                        entry["head"] = answered["head"]
                    else:
                        option = {"id": "go-without-head", "label": "Merge", "do": "dismiss"}
            entry["option"] = option or {"id": answered["option"]}
        for key in ("auto", "rule"):
            if answered.get(key):
                entry[key] = True
        out.append(entry)
    return out


def apply_defaults(now=None) -> list:
    """Decisions filed with --auto whose default has come due, as the old dash applied them: 12 h after filing, or 2 h after the later of
    filing and the human's last activity once they have been away 2 h. Never with stakes of any kind that are unreadable or high."""
    now = time.time() if now is None else now
    applied = []
    last_seen = last_activity()
    away = now - last_seen > 2 * 3600
    answered = {a.get("item") for a in _fold(_action_lines()).values()}
    for item in load()["items"]:
        if not (item.get("auto") and item.get("rec") and is_open(item)) or item.get("applied") or snoozed(item, now):
            continue
        if item.get("stakes") or item["id"] in answered:
            continue
        created = max(float(item.get("created") or now), snooze_until(item))  # a snooze that ended gives the whole wait again
        due = created + 12 * 3600
        if away:
            due = min(due, max(created, last_seen) + 2 * 3600)
        if now >= due:
            applied.append(answer(item["id"], now=now, option=item["rec"], auto=True))
    return applied


def last_activity() -> float:
    latest = 0.0
    for line in store.read_lines(store.path("requests.jsonl")):
        latest = max(latest, float(line.get("ts") or 0))
    for line in _action_lines():
        if line.get("status") == "queued" and not line.get("auto"):
            latest = max(latest, float(line.get("ts") or 0))
    return latest


def log_request(prompt, session=None):
    text = (prompt or "").strip()
    if not text or text.startswith(NOT_HUMAN):
        return None
    with store.locked("requests.lock"):
        count = sum(1 for line in store.read_lines(store.path("requests.jsonl")) if line.get("id"))
        record = {"id": f"r{count + 1}", "ts": time.time(), "session": session,
                  "text": _SECRET.sub("[secret removed]", text)[:TEXT_MAX]}
        store.append_line(store.path("requests.jsonl"), record)
    return record


def requests_list(days=7, now=None) -> list:
    now = time.time() if now is None else now
    filed = {}
    data = load()
    archived = list(store.read_lines(store.path("items-archive.jsonl")))
    for item in data["items"] + archived:
        for rid in item.get("requests") or []:
            filed[rid] = item.get("name") or item.get("id")
    ignored = set()
    rows = []
    for line in store.read_lines(store.path("requests.jsonl")):
        if line.get("ignore"):
            ignored.add(line["ignore"])
        elif line.get("id") and now - float(line.get("ts") or 0) <= days * 86400:
            rows.append(line)
    return [{**r, "item": filed.get(r["id"]), "ignored": r["id"] in ignored} for r in rows]


def _row(i) -> str:
    state = "resolved" if i.get("resolved") else ("snoozed" if snoozed(i, time.time()) else i.get("next", "-"))
    return f"{i.get('id'):<6} {str(i.get('name'))[:28]:<28} {i.get('kind', '-'):<9} {state:<9} {i.get('text', '')[:80]}"


def _configure(p):
    sub = p.add_subparsers(dest="action")
    a = sub.add_parser("add")
    a.add_argument("title")
    for flag in ("--name", "--kind", "--next", "--why", "--body", "--due", "--words", "--pr", "--rec"):
        a.add_argument(flag)
    a.add_argument("--body-file")
    a.add_argument("--option", action="append", default=[])
    a.add_argument("--request", action="append", default=[])
    a.add_argument("--worker", action="append", default=[])
    a.add_argument("--link", action="append", default=[])
    a.add_argument("--stakes", action="append", default=[])
    a.add_argument("--auto", action="store_true")
    a.add_argument("--fyi", action="store_true")
    a.add_argument("--preview", action="append", default=[], metavar="FILE|DIR",
                   help=f"local HTML its full view embeds, copied into the state dir and served sandboxed; at most {PREVIEWS_MAX}")
    a.add_argument("--words-file")
    e = sub.add_parser("edit")
    e.add_argument("id")
    for flag in ("--title", "--name", "--kind", "--next", "--why", "--body", "--due", "--pr", "--rec", "--words"):
        e.add_argument(flag)
    e.add_argument("--body-file")
    e.add_argument("--words-file")
    e.add_argument("--option", action="append")
    e.add_argument("--request", action="append")
    e.add_argument("--worker", action="append")
    e.add_argument("--link", action="append")
    e.add_argument("--stakes", action="append")
    auto = e.add_mutually_exclusive_group()
    auto.add_argument("--auto", action="store_true")
    auto.add_argument("--no-auto", action="store_true")
    b = sub.add_parser("body")
    b.add_argument("id")
    b.add_argument("--file", required=True, help="the new body from a file, or - for stdin; empty clears it")
    d = sub.add_parser("done")
    d.add_argument("id")
    d.add_argument("--resolution", default="")
    d.add_argument("--keep", action="store_true")
    s = sub.add_parser("snooze")
    s.add_argument("id")
    s.add_argument("--until", required=True)
    show = sub.add_parser("show")
    show.add_argument("id", nargs="?")
    sub.add_parser("queue")
    r = sub.add_parser("requests")
    r.add_argument("--days", type=int, default=7)
    c = sub.add_parser("check")
    c.add_argument("--days", type=int, default=7)
    i = sub.add_parser("ignore")
    i.add_argument("ids", nargs="+")


def _read(path) -> str:
    """A file, or stdin for `-`."""
    if path == "-":
        return sys.stdin.read()
    with open(path) as f:
        return f.read()


def _body(args):
    if getattr(args, "body_file", None):
        return _read(args.body_file)
    return args.body


def _words(args):
    if getattr(args, "words_file", None):
        return _read(args.words_file)
    return args.words


def _run(args):
    action = args.action
    if action == "add":
        item = add(args.title, args.name, args.kind, args.next, args.why or "", _body(args) or "", args.option, args.rec,
                   args.auto, args.stakes, args.due, _words(args), args.request, args.worker, args.pr, args.link, args.fyi, previews=args.preview)
        print(f"{item['id']} {item['name']}")
    elif action == "edit":
        item = edit(args.id, text=args.title, name=args.name, kind=args.kind, next=args.next, why=args.why,
                    body=_body(args), due=args.due, pr=args.pr, rec=args.rec, words=_words(args), options=args.option,
                    requests=args.request, workers=args.worker, links=args.link, stakes=args.stakes,
                    auto=True if args.auto else False if args.no_auto else None)
        print(f"{item['id']} {item['name']}")
    elif action == "body":
        item = edit(args.id, body=_read(args.file).strip("\n"))
        print(f"{item['id']} {item['name']}")
    elif action == "done":
        resolve(args.id, args.resolution, args.keep)
        print(f"{args.id} done")
    elif action == "snooze":
        snooze(args.id, args.until)
    elif action == "show" and args.id:
        print(json.dumps(find(load(), args.id), indent=1))
    elif action == "queue":
        print(json.dumps(queue(), indent=1))
    elif action in ("requests", "check"):
        rows = requests_list(args.days)
        if action == "check":
            rows = [r for r in rows if not r["item"] and not r["ignored"]]
        for r in rows:
            print(f"{r['id']:<6} {r.get('item') or '-':<24} {r['text'][:100]}")
        return 1 if action == "check" and rows else 0
    elif action == "ignore":
        for rid in args.ids:
            store.append_line(store.path("requests.jsonl"), {"ignore": rid, "ts": time.time()})
    else:
        for item in sorted(open_items(), key=lambda i: i.get("updated") or 0, reverse=True):
            print(_row(item))
    return 0


def checks(ctx):
    unreadable = load().get("unreadable")
    return [caps.Check("items", not unreadable, "needs-you.json does not parse; showing its last generation" if unreadable else "ok")]


def tick(ctx, now):
    from athena_rex.caps.watch import Line
    lines = []
    applied = apply_defaults(now)
    pending = queue(now)
    state = store.read_dict(store.path("run", "queue-rung.json"))
    newest = max((a["seq"] for a in pending), default=0)
    if newest > int(state.get("rung") or 0):
        store.write_json(store.path("run", "queue-rung.json"), {"rung": newest})
        lines.append(Line(f"dash-queue {newest}", "wake"))
    return lines + [Line(f"default applied {a['item']}", "routine") for a in applied]


def commands():
    return {"item": caps.Command(SUMMARY, _configure, _run), "ask": caps.Command("alias of item", _configure, _run, hidden=True)}
