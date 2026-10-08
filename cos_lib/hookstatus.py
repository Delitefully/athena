"""Worker status from Claude Code hook events. One JSON file per worker, written only by its own hooks.

Run as `python3 -m cos_lib.hookstatus` with the hook payload on stdin. Always exits 0.
"""
import json
import os
import sys
import time

from cos_lib import ledger, paths

SUMMARY_LIMIT = 300
DONE = {"DONE", "DONE_WITH_CONCERNS"}
BLOCKED = {"BLOCKED", "NEEDS_CONTEXT"}
NEEDS_HUMAN_TYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}


def worker_name(env, cwd):
    name = (env.get("COS_WORKER") or "").strip()
    if name:
        return name if paths.valid_name(name) else None
    if not cwd:
        return None
    root = os.path.realpath(paths.worktree_root())
    real = os.path.realpath(cwd)
    if not real.startswith(root.rstrip("/") + "/"):
        return None
    found = ledger.find_by_path(real)
    return found["name"] if found else None


def parse_report(text):
    if not text:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("COS-REPORT "):
            try:
                data = json.loads(line[len("COS-REPORT "):])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _file(name):
    return paths.ensure("workers") / f"{name}.json"


def read(name):
    try:
        with open(paths.state_dir() / "workers" / f"{name}.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(name, data):
    target = _file(name)
    tmp = target.with_suffix(".json.tmp%d" % os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, target)


def apply(event, name):
    prev = read(name) or {}
    kind = event.get("hook_event_name") or ""
    state = prev.get("state", "starting")
    report = prev.get("report")
    summary = prev.get("summary", "")
    if kind == "SessionStart":
        state = "idle"
    elif kind == "UserPromptSubmit":
        state, report = "working", None
    elif kind == "Stop":
        message = event.get("last_assistant_message") or ""
        summary = message.strip()[-SUMMARY_LIMIT:]
        report = parse_report(message)
        status = (report or {}).get("status", "").upper()
        state = "done" if status in DONE else "blocked" if status in BLOCKED else "idle"
    elif kind == "Notification":
        ntype = event.get("notification_type") or ""
        message = (event.get("message") or "").lower()
        if ntype in NEEDS_HUMAN_TYPES or "permission" in message:
            state = "blocked"
            summary = (event.get("message") or "")[:SUMMARY_LIMIT]
        elif ntype == "idle_prompt" and state not in ("done", "blocked"):
            state = "idle"
    elif kind == "SessionEnd":
        state = "exited"
    else:
        return prev
    data = {
        "name": name, "state": state, "event": kind, "ts": time.time(), "summary": summary,
        "report": report, "last_report": report or prev.get("last_report"),
        "session_id": event.get("session_id") or prev.get("session_id"),
    }
    _write(name, data)
    return data


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return
        name = worker_name(os.environ, event.get("cwd") or os.getcwd())
        if name:
            apply(event, name)
    except Exception:  # a hook must never break the session
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
