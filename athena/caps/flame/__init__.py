"""Flame: where athena's time and tokens go. `flame index` reads Claude Code's transcripts into a small index in the state
dir; the dash's Flame page draws it. Design, data model and budgets: the flame PLAN.md; frames and costs: frames.py.

Nothing here writes outside `<state dir>/flame/`, and nothing reads a transcript's text into the index: only timestamps,
token counts, models, tool names, a command's first two words, subagent types and skill names.
"""
import json
import os
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

from athena.cli import UserError
from athena.kernel import agents, caps, store

STALE_S = 5 * 60  # the page updates an index older than this when it is opened
_runner = None  # the background index this process started, if any
_runner_lock = threading.Lock()
SUMMARY = "where athena's time and tokens went: indexes transcripts for the dash's Flame page"


def index_path() -> Path:
    return store.path("flame", "index.sqlite")


def projects_dir() -> Path:
    return agents.claude_dir() / "projects"


def last_run_at():
    """When the last index finished, from the index itself; None when there is none."""
    from athena.caps.flame import query
    db = query.open_index(index_path())
    if db is None:
        return None
    try:
        return (query.status(db).get("last_run") or {}).get("at")
    except Exception:  # noqa: BLE001 (an index being written for the first time has no tables yet)
        return None
    finally:
        db.close()


def running() -> bool:
    """An index is running: the one this process started, or any other holding the index's lock."""
    if _runner is not None and _runner.poll() is None:
        return True
    if not index_path().parent.is_dir():
        return False
    with store.locked("flame/index.lock", blocking=False) as held:
        return held is False


def refresh_if_stale(max_age=STALE_S, now=None) -> bool:
    """Starts `athena flame index --quiet` in the background when the index is older than `max_age` (or missing), at most one
    at a time; the command nices itself. Returns whether an index is running now. Only new bytes are read, so it is about a
    second; a first index reads everything once, in about a minute."""
    global _runner
    with _runner_lock:
        if running():
            return True
        at = last_run_at()
        if at and (now or time.time()) - at < max_age:
            return False
        if not projects_dir().is_dir():
            return False
        try:
            log = store.ensure("logs") / "flame.log"
            with open(log, "a") as out:
                _runner = subprocess.Popen([str(store.ROOT / "bin" / "athena"), "flame", "index", "--quiet"],
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        except OSError as exc:  # the page still gets the index it has; the dash's log says why it is not newer
            print(f"flame: could not start an index: {exc}", file=sys.stderr, flush=True)
            return False
        return True


def data(scope="athena", since=None, until=None, refresh=False) -> dict:
    """What `/flame/data` answers: the tree, the top-frames tables, the index's own status, and whether a fresher index is being
    made (`refresh` starts one when this one is stale). Empty when there's no index."""
    from athena.caps.flame import query
    refreshing = refresh_if_stale() if refresh else running()
    db = query.open_index(index_path())
    if db is None:
        return {"root": None, "top": {}, "sessions": {}, "status": {"files": 0}, "ms": 0, "refreshing": refreshing}
    try:
        key = str(index_path())
        out = query.tree(db, key, scope, since, until)
        out["top"] = query.top(db, key, scope, since, until)
        out["status"] = query.status(db)
        out["refreshing"] = refreshing
        return out
    finally:
        db.close()


def status() -> dict:
    """What the page asks while an index runs: when the last one finished, and whether one is running now."""
    return {"at": last_run_at(), "refreshing": running()}


def _configure(p):
    p.add_argument("action", nargs="?", choices=["index", "status"], default="status")
    p.add_argument("--projects", help="where Claude Code keeps transcripts (default: ~/.claude/projects)")
    p.add_argument("--full", action="store_true", help="forget what was read and read every transcript again")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")


def _run(args):
    if args.action == "index":
        return _index(args)
    path = index_path()
    if not path.exists():
        print("no index yet: athena flame index")
        return 0
    from athena.caps.flame import query
    db = query.open_index(path)
    try:
        info = query.status(db)
    finally:
        db.close()
    size = sum(p.stat().st_size for p in path.parent.glob("index.sqlite*"))
    info["index_bytes"] = size
    if args.json:
        print(json.dumps(info, indent=1))
        return 0
    last = info.get("last_run") or {}
    when = time.strftime("%b %d %H:%M", time.localtime(last["at"])) if last.get("at") else "never"
    print(f"{path}: {size / 1e6:.1f} MB, {info['files']} transcripts ({info['bytes'] / 1e9:.1f} GB), "
          f"days {info['first_day']} to {info['last_day']}, last run {when}")
    if info.get("skipped_lines"):
        print(f"{info['skipped_lines']} lines in {info['skipped_files']} transcripts could not be read and are not in the index:")
        for odd in info.get("odd_files") or []:
            print(f"  {odd}")
    if last.get("failed"):
        print(f"the last run could not index {last['failed']} transcripts; they are tried again next run:")
        for failed in last.get("failed_files") or []:
            print(f"  {failed}")
    return 0


def _index(args):
    from athena.caps.flame import ingest, label
    projects = Path(os.path.expanduser(args.projects)) if args.projects else projects_dir()
    if not projects.is_dir():
        raise UserError(f"no transcripts at {projects}")
    try:
        os.nice(10)  # a full run reads every transcript once: it waits behind anything a person is doing
    except OSError:
        pass
    store.ensure("flame")
    say = (lambda line: None) if args.quiet else (lambda line: print(line, flush=True))
    with store.locked("flame/index.lock", blocking=False) as held:
        if held is False:
            print("an index is already running")
            return 0
        counts = ingest.index(index_path(), projects, labels=label.label_all, full=args.full, say=say)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1 << 20) if sys.platform == "darwin" else rss / 1024  # bytes on macOS, KiB on Linux
    size = sum(p.stat().st_size for p in index_path().parent.glob("index.sqlite*"))
    if not args.quiet:
        print(f"read {counts['read']} of {counts['files']} transcripts, {counts['bytes'] / 1e9:.2f} GB, {counts['steps']} steps "
              f"in {counts['seconds']} s; peak RSS {rss_mb:.0f} MB; index {size / 1e6:.1f} MB at {index_path()}")
        if counts.get("skipped") or counts.get("failed"):
            print(f"skipped {counts['skipped']} lines of a shape it doesn't know; {counts['failed']} files failed and are tried next run")
    return 0


def commands():
    return {"flame": caps.Command(SUMMARY, _configure, _run)}
