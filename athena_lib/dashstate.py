"""State files kept for the dashboard (`athena dash`), which reads files only.

`watch.json`: the last `athena watch` snapshot, so the page knows each live PR's checks and review.
`history.jsonl`: one line per retired worker with its PR as last known, so a merge is still known after retire.
"""
import json
import os
import sys
import time

from athena_lib import hookstatus, paths, pr


def _watch_file():
    return paths.ensure() / "watch.json"


def save_watch(snapshot, now=None):
    target = _watch_file()
    tmp = target.with_suffix(".json.tmp%d" % os.getpid())
    tmp.write_text(json.dumps({"ts": time.time() if now is None else now, "workers": snapshot.get("workers", {})},
                              sort_keys=True, default=str))
    os.replace(tmp, target)


def load_watch():
    try:
        data = json.loads((paths.state_dir() / "watch.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _last_pr(worker, report):
    """gh first: HQ retires right after it merges, sooner than `athena watch` sees the merge."""
    name = worker["name"]
    if worker.get("repo") and worker.get("branch"):
        found = pr.summary(worker["repo"], worker["branch"])
        if found:
            return found
    watched = (load_watch().get("workers") or {}).get(name) or {}
    if watched.get("pr"):
        return watched["pr"]
    if (report.get("pr") or "").startswith("https://"):
        return {"url": report["pr"]}
    return None


def record_retire(worker):
    """Append the worker to history.jsonl. Never raises: the record must not stand in the way of a retire."""
    try:
        hook = hookstatus.read(worker["name"]) or {}
        report = hook.get("report") or hook.get("last_report") or {}
        entry = {"ts": time.time(), "name": worker["name"], "linear": worker.get("linear"), "title": worker.get("title"),
                 "repo": worker.get("repo"), "branch": worker.get("branch"), "status": report.get("status"),
                 "pr": _last_pr(worker, report)}
        line = json.dumps(entry, sort_keys=True, default=str) + "\n"
        fd = os.open(paths.ensure() / "history.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception as exc:
        print(f"history not recorded: {exc}", file=sys.stderr)
