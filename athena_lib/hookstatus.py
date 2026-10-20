"""Worker status from Claude Code hook events. One JSON file per worker, written only by its own hooks.

Run as `python3 -m athena_lib.hookstatus` with the hook payload on stdin. Always exits 0.
"""
import json
import os
import sys
import time

from athena_lib import config, herdr, ledger, paths

SUMMARY_LIMIT = 300
DONE = {"DONE", "DONE_WITH_CONCERNS"}
BLOCKED = {"BLOCKED", "NEEDS_CONTEXT"}
NEEDS_HUMAN_TYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}


def worker_name(env, cwd):
    name = (env.get("ATHENA_WORKER") or "").strip()
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
        if line.startswith("ATHENA-REPORT "):
            try:
                data = json.loads(line[len("ATHENA-REPORT "):])
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


def _resume_args(name, w):
    """The args the worker was spawned with; a record from before they were kept rebuilds them."""
    if w.get("claude_args"):
        return w["claude_args"]
    settings = paths.state_dir() / "settings" / f"{name}.json"
    if not settings.is_file():
        return None
    from athena_lib import spawn  # only for an older record: spawn imports most of athena
    return spawn.claude_args(name, w.get("model") or config.get("model"), w.get("effort") or config.get("effort"),
                             settings)


def _session_started(event):
    sid = event.get("session_id")
    if event.get("hook_event_name") == "SessionStart" and isinstance(sid, str) and sid:
        return sid
    return None


def _register(pane, sid, args):
    try:
        herdr.register_resume(pane, sid, args, timeout=2)  # the hook's own timeout is 10 s
    except herdr.HerdrError:
        pass


def register_resume(event, name, env):
    """On SessionStart in the worker's own herdr pane, give herdr the session's whole resume command again.

    /clear and a resume start a new session id, and herdr's own Claude hook reports it with no resume command;
    a worker herdr restored bare (no ATHENA_WORKER, found here by its cwd) gets its name and settings back next time.
    """
    sid = _session_started(event)
    if not sid:
        return
    w = ledger.workers().get(name)
    if not w or w.get("state") != "live" or not w.get("pane") or env.get("HERDR_PANE_ID") != w["pane"]:
        return
    args = _resume_args(name, w)
    if args:
        _register(w["pane"], sid, args)


def register_hq_resume(event, env):
    """The same for HQ (ATHENA_ROLE=hq) in its own pane, with the args `athena hq` kept in hq.json."""
    sid = _session_started(event)
    if not sid or env.get("ATHENA_ROLE") != "hq":
        return
    try:
        record = json.loads((paths.state_dir() / "hq.json").read_text())
    except (OSError, ValueError):
        return
    pane, args = record.get("pane"), record.get("claude_args")
    if pane and args and env.get("HERDR_PANE_ID") == pane:
        _register(pane, sid, args)


def main():
    try:
        config.sync_root_hint()  # so the shell fast path reads a current worktree root next time
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return
        name = worker_name(os.environ, event.get("cwd") or os.getcwd())
        if name:
            apply(event, name)
            register_resume(event, name, os.environ)
        else:
            register_hq_resume(event, os.environ)
    except Exception:  # a hook must never break the session
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
