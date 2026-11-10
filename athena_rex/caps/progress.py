"""A worker's progress, for the band its mod draws (worker-plugin/): `workers/<name>.progress.json`.

The source is the task list the worker already keeps. A PostToolUse hook on Claude Code's task tools (TodoWrite, or
TaskCreate and TaskUpdate) turns each update into done, total and the step in progress, with no model call and no new
tool. When that list has been quiet for ten minutes while the worker works, the mod may ask Haiku for an estimate from
the transcript's tail, at most once in ten minutes: `ask` decides and builds the capped request, the mod sends it
through its session's own client, and `answer` records the reply. `rex.progress_haiku: false` turns that off.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from athena_rex.kernel import caps, config, store

SUMMARY = "a worker's progress from its task list, and a rare Haiku estimate (internal)"
TASK_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate")
QUIET = 600  # a task list newer than this needs no estimate
EVERY = 600  # at most one estimate per worker in this long
MODEL = "haiku"
MAX_TOKENS = 40
LINES = 40
LINE_MAX = 200
STEP_WORDS = 8
PLUGIN = store.ROOT / "worker-plugin"  # by the `current` link, as the hook command is: a resumed worker outlives its release
SYSTEM = ("You summarise a coding agent's progress for a one-line status bar. Reply with exactly one line, "
          "`<done>/<total> <current step>`: how many of its task's steps are done of how many it seems to have, "
          f"then what it is doing now in at most {STEP_WORDS} words. Nothing else.")
_ESTIMATE = re.compile(r"(\d+)\s*/\s*(\d+)\s+(.+)")


def launch_args() -> list:
    """What a worker's Claude is launched with to load the band. Workers only: HQ's launch never calls this."""
    return ["--plugin-dir", str(PLUGIN)]


def worker_env() -> dict:
    """A worker's --settings env: Claude Code gives Claude 5 models no task tools unless this is set, and the task
    list is what the band reads."""
    return {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}


def settings_hooks(command) -> dict:
    """The PostToolUse entry for a worker's --settings: the task tools, after they run."""
    return {"PostToolUse": [{"matcher": "|".join(TASK_TOOLS), "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}


def _file(name) -> Path:
    return store.path("workers", f"{name}.progress.json")


def _locked(name):
    return store.locked(f"locks/{name}.progress.lock", full_disk_ok=True)


def fresh(name):
    """A name spawned again starts from no progress: the old worker's file moves aside, as its status file does."""
    with _locked(name):
        if _file(name).exists():
            os.replace(_file(name), _file(name).with_name(f"{name}.progress.json.prev"))


def _tasks_after(tasks: dict, tool, given, response) -> dict:
    """The worker's task list after one call, as {id: {subject, active, status}} in the order made."""
    given = given if isinstance(given, dict) else {}
    if tool == "TodoWrite":
        todos = given.get("todos")
        if not isinstance(todos, list):
            return tasks
        return {str(i): {"subject": str(t.get("content") or ""), "active": str(t.get("activeForm") or ""),
                         "status": str(t.get("status") or "pending")}
                for i, t in enumerate(todos, 1) if isinstance(t, dict)}
    tasks = {k: dict(v) for k, v in tasks.items()}
    if tool == "TaskCreate":
        made = (response or {}).get("task") if isinstance(response, dict) else None
        tid = str((made or {}).get("id") or "") or str(max([int(k) for k in tasks if k.isdigit()] + [0]) + 1)
        tasks[tid] = {"subject": str(given.get("subject") or ""), "active": str(given.get("activeForm") or ""),
                      "status": "pending"}
    elif tool == "TaskUpdate":
        tid = str(given.get("taskId") or "")
        if not tid:
            return tasks
        if given.get("status") == "deleted":
            tasks.pop(tid, None)
            return tasks
        task = tasks.setdefault(tid, {"subject": f"task {tid}", "active": "", "status": "pending"})
        for key, field in (("subject", "subject"), ("activeForm", "active"), ("status", "status")):
            if given.get(key):
                task[field] = str(given[key])
    return tasks


def on_tool(event, name, now=None) -> dict:
    """PostToolUse for a task tool: the progress file from the worker's own list. No model call."""
    tool = event.get("tool_name")
    if tool not in TASK_TOOLS:
        return {}
    now = time.time() if now is None else now
    with _locked(name):
        prev = store.read_dict(_file(name))
        tasks = _tasks_after(prev.get("tasks") or {}, tool, event.get("tool_input"), event.get("tool_response"))
        doing = next((t for t in tasks.values() if t.get("status") == "in_progress"), None)
        step = (doing.get("active") or doing.get("subject")) if doing else ""
        same = prev.get("source") == "tasks" and prev.get("step") == step
        data = {**prev, "name": name, "done": sum(t.get("status") == "completed" for t in tasks.values()),
                "total": len(tasks), "step": step, "source": "tasks", "ts": now, "tasks_ts": now,
                "step_ts": prev.get("step_ts", now) if same else now, "tasks": tasks}
        store.write_json(_file(name), data)
        return data


def due(name, now, since=0.0) -> bool:
    """An estimate only while the worker works, its task list (or its session, if newer) quiet for QUIET, and none
    asked for in EVERY."""
    if config.rex("progress_haiku") is False:
        return False
    if store.read_dict(store.path("workers", f"{name}.json")).get("state") != "working":
        return False
    prev = store.read_dict(_file(name))
    if now - max(float(prev.get("tasks_ts") or 0), float(since or 0)) < QUIET:
        return False
    return now - float(prev.get("asked_ts") or 0) >= EVERY


def request(lines) -> dict:
    """The model call, capped: Haiku, a few dozen tokens out, the transcript's last LINES lines in."""
    tail = [str(line)[:LINE_MAX] for line in lines if str(line).strip()][-LINES:]
    return {"model": MODEL, "maxTokens": MAX_TOKENS, "effort": "low", "timeoutMs": 30000, "system": SYSTEM,
            "prompt": "The agent's transcript, newest last:\n\n" + "\n".join(tail) + "\n\nYour one line:"}


def ask(name, lines, now=None, since=0.0):
    """The request to send now, or None. Asking counts as the estimate's turn, so a failed call waits EVERY too."""
    now = time.time() if now is None else now
    with _locked(name):
        if not due(name, now, since):
            return None
        store.write_json(_file(name), {**store.read_dict(_file(name)), "name": name, "asked_ts": now})
    return request(lines)


def parse(text):
    """(done, total, step) from Haiku's line, or None."""
    for line in (text or "").splitlines():
        found = _ESTIMATE.search(line.strip().strip("`"))
        if found:
            done, total = int(found.group(1)), int(found.group(2))
            step = " ".join(found.group(3).strip(" .`").split()[:STEP_WORDS])
            if total > 0 and step:
                return min(done, total), total, step
    return None


def answer(name, text, now=None) -> dict:
    """The estimate into the progress file, unless it won't parse or a task-list update came in since it was asked."""
    now = time.time() if now is None else now
    parsed = parse(text)
    if not parsed:
        return {}
    with _locked(name):
        prev = store.read_dict(_file(name))
        if float(prev.get("tasks_ts") or 0) > float(prev.get("asked_ts") or 0):
            return {}
        done, total, step = parsed
        data = {**prev, "name": name, "done": done, "total": total, "step": step, "source": "estimate", "ts": now,
                "step_ts": prev.get("step_ts", now) if prev.get("step") == step else now}
        store.write_json(_file(name), data)
        return data


def _name(args) -> str:
    name = (args.name or os.environ.get("ATHENA_WORKER") or "").strip()
    if not store.valid_name(name):
        from athena_rex.cli import UserError
        raise UserError("which worker? --name or ATHENA_WORKER")
    return name


def _ask_cfg(p):
    p.add_argument("--name")
    p.add_argument("--since", type=float, default=0.0, help="when the session started, epoch seconds")


def _ask(args):
    """Transcript lines on stdin; prints the request as JSON when one is due, nothing otherwise."""
    found = ask(_name(args), sys.stdin.read().splitlines(), since=args.since)
    if found:
        print(json.dumps(found))
    return 0


def _answer_cfg(p):
    p.add_argument("--name")


def _answer(args):
    print(json.dumps(answer(_name(args), sys.stdin.read())))
    return 0


def commands():
    return {"progress": caps.Command("internal: progress ask|answer, the band's Haiku estimate", _sub_cfg, _sub, hidden=True)}


def _sub_cfg(p):
    sub = p.add_subparsers(dest="action", required=True)
    _ask_cfg(sub.add_parser("ask"))
    _answer_cfg(sub.add_parser("answer"))


def _sub(args):
    return _ask(args) if args.action == "ask" else _answer(args)
