"""Worker status from Claude Code hook events. One JSON file per worker, written only by its own hooks.

Run as `python3 -m athena_lib.hookstatus` with the hook payload on stdin. Always exits 0.

The report gate: in a session athena started as a worker (ATHENA_WORKER, from its --settings), a turn cannot end
without an ATHENA-REPORT line, nor with DONE or DONE_WITH_CONCERNS unless VERIFY passed at the worktree's HEAD. The
hook prints Claude Code's `{"decision": "block", "reason": ...}` and the worker goes on to write the report. BLOCKED
and NEEDS_CONTEXT always end the turn. After MAX_STOP_BLOCKS blocks in a row (within BLOCKS_FORGOTTEN_AFTER of each
other) the gate gives way and the worker goes idle with no report, even one the gate refused, which athena watch tells
HQ as `->idle` without a report: the gate can never loop.
"""
import json
import os
import re
import subprocess
import sys
import time

from athena_lib import config, herdr, ledger, paths

SUMMARY_LIMIT = 300
DONE = {"DONE", "DONE_WITH_CONCERNS"}
BLOCKED = {"BLOCKED", "NEEDS_CONTEXT"}
NEEDS_HUMAN_TYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}
MAX_STOP_BLOCKS = 3  # blocks in a row before the gate gives way
BLOCKS_FORGOTTEN_AFTER = 300  # seconds: a block this long after the last one counts from one again
REPORT_FORMAT = ('ATHENA-REPORT {"status":"DONE|DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED","pr":"<url or empty>",'
                 '"head":"<commit sha>","verify":"<command> -> pass|fail","decisions":[],"concerns":[]}')
_PASS = re.compile(r"->\s*pass\b", re.I)
_NOT_PASS = re.compile(r"->\s*(?:pass\s*\|\s*fail|fail|not run|skipped|pending)\b", re.I)


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


def _processes():
    """{pid: (ppid, args)} from one `ps` (ATHENA_PS, for tests), or None when it cannot be read."""
    try:
        out = subprocess.run([os.environ.get("ATHENA_PS") or "ps", "-A", "-o", "pid=,ppid=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table or None


def _is_claude(args) -> bool:
    words = (args or "").split()
    return bool(words) and (os.path.basename(words[0]) == "claude" or "@anthropic-ai/claude-code/" in args)


def nested(pid=None, table=None) -> bool:
    """Whether the Claude that runs this hook was started by another Claude: a `claude -p` a worker runs from its
    Bash tool inherits ATHENA_WORKER, ATHENA_ROLE and HERDR_PANE_ID, and can resume or compact as the worker's own
    does, so neither its environment nor its session id tells it apart. Its ancestry does: above the hook's own
    Claude, another one. False when ps cannot be read or no Claude is found (a hook run by hand): the worker's."""
    table = _processes() if table is None else table
    if not table:
        return False
    pid, seen, claudes = pid or os.getpid(), set(), 0
    while pid in table and pid not in seen and len(seen) < 64:
        seen.add(pid)
        ppid, args = table[pid]
        if _is_claude(args):
            claudes += 1
            if claudes > 1:
                return True
        pid = ppid
    return False


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


def prose_summary(message, report):
    """The summary `athena status` and the dash show: the last SUMMARY_LIMIT characters of the prose before a parsed
    ATHENA-REPORT line, never the report's JSON (the report itself is kept whole in `report`)."""
    cut = message.rfind("ATHENA-REPORT ") if report else -1
    return (message[:cut] if cut >= 0 else message).strip()[-SUMMARY_LIMIT:]


def has_report_line(text) -> bool:
    return any(line.strip().startswith("ATHENA-REPORT") for line in (text or "").splitlines())


def _head(cwd):
    if not cwd:
        return None
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _missing(message, report, cwd):
    """Why this turn may not end yet, or None. Never asks more of BLOCKED or NEEDS_CONTEXT than the line itself."""
    if report is None:
        if has_report_line(message):
            return ("Your ATHENA-REPORT line is not valid JSON. End your message with one line, exactly: "
                    + REPORT_FORMAT)
        return ("End your turn with an ATHENA-REPORT line, even mid-task: report BLOCKED with your question in "
                "concerns if you are stuck or waiting on someone, or keep working and report when done. If you are "
                "waiting on background work you started, wait for it in this turn. The last line of your message, "
                "exactly: " + REPORT_FORMAT)
    status = str(report.get("status") or "").upper()
    if status not in DONE:
        return None
    verify = str(report.get("verify") or "")
    if not _PASS.search(verify) or _NOT_PASS.search(verify):
        return (f"{status} needs VERIFY passing: run the brief's VERIFY at your final commit, show its passing output, "
                f"and report `\"verify\":\"<command> -> pass\"`. If it cannot pass, report BLOCKED with the failure "
                f"in concerns instead.")
    head = _head(cwd)
    if head:
        from athena_lib import pr  # only for a DONE: pr is not needed on other events
        if not pr.same_head(report.get("head"), head):
            return (f"Your report's head ({report.get('head') or 'empty'}) is not your worktree's HEAD "
                    f"({head[:12]}). Commit and push, re-run VERIFY there, and report that head.")
    return None


def gate(data):
    """The Stop hook's answer for a worker's turn end: Claude Code's block decision, or None to let it end."""
    return (data or {}).get("gate")


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


def _gate(prev, event, report, state, gated):
    """The report gate on one hook event (module docstring): (state, report, its fields for the file, the decision).
    Only a gated Stop changes the state or report: blocked, it goes on working; given way, it goes idle unreported."""
    kind = event.get("hook_event_name")
    blocks, block_ts = prev.get("stop_blocks") or 0, prev.get("stop_block_ts")
    if time.time() - (block_ts or 0) > BLOCKS_FORGOTTEN_AFTER:
        blocks = 0  # under /goal one prompt can run for hours: only blocks in a row count
    decision = None
    if kind in ("UserPromptSubmit", "SessionStart"):  # a new prompt or a new session starts the count again
        blocks = 0
    elif kind == "Stop":
        why = _missing(event.get("last_assistant_message") or "", report, event.get("cwd")) if gated else None
        if why and blocks < MAX_STOP_BLOCKS:
            decision = {"decision": "block", "reason": why}
            state, report, blocks, block_ts = "working", None, blocks + 1, time.time()
        elif why:
            state, report = "idle", None
        elif report is not None:
            blocks = 0
    return state, report, {"stop_blocks": blocks, "stop_block_ts": block_ts}, decision


def apply(event, name, gated=False):
    """Record a hook event in the worker's file. `gated`: a Stop goes through the report gate (module docstring);
    its decision is in the result's `gate`, never in the file."""
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
        report = parse_report(message)
        summary = prose_summary(message, report)
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
    state, report, gate_fields, decision = _gate(prev, event, report, state, gated)
    data = {
        "name": name, "state": state, "event": kind, "ts": time.time(), "summary": summary,
        "report": report, "last_report": report or prev.get("last_report"),
        "session_id": event.get("session_id") or prev.get("session_id"), **gate_fields,
    }
    if kind == "SessionStart":
        # How the session started (startup, resume, clear, compact), and on a resume the state it was in: doctor
        # tells a worker herdr resumed mid-turn to carry on, and no other.
        data["source"] = event.get("source")
        if event.get("source") == "resume":
            data["resumed_from"] = prev.get("state")
    _write(name, data)
    return {**data, "gate": decision} if decision else data


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


def log_request(event, env):
    """HQ's UserPromptSubmit: what the human typed goes to the request log (requestlog.py)."""
    if event.get("hook_event_name") != "UserPromptSubmit" or env.get("ATHENA_ROLE") != "hq":
        return None
    from athena_lib import requestlog
    return requestlog.record(event.get("prompt"), event.get("session_id"))


def hq_owns(event, env, record) -> bool:
    """Whether a SessionStart is HQ's own Claude, not a `claude -p` HQ ran from its Bash tool: that one inherits
    ATHENA_ROLE=hq and HERDR_PANE_ID, may resume or compact a conversation of its own, and must never become the one
    herdr or doctor resumes. It is HQ's when it comes from HQ's pane and is the session hq.json knows (a resume and a
    compaction keep their id), or the first one, while hq.json knows none. A new id (a /clear) is recorded by
    `athena hq` and its restart and rotation, which ask herdr, never by this hook."""
    sid = _session_started(event)
    if not sid or env.get("ATHENA_ROLE") != "hq":
        return False
    pane = record.get("pane")
    if not pane or env.get("HERDR_PANE_ID") != pane:
        return False
    known = record.get("session")
    return not known or sid == known


def register_hq_resume(event, env):
    """The same for HQ (ATHENA_ROLE=hq) in its own pane, with the args `athena hq` kept in hq.json. Only HQ's own
    session counts (`hq_owns`); this hook never writes hq.json."""
    try:
        record = json.loads((paths.state_dir() / "hq.json").read_text())
    except (OSError, ValueError):
        return
    if not isinstance(record, dict) or not hq_owns(event, env, record):
        return
    sid = _session_started(event)
    # For doctor: a resumed HQ that no one sent its start routine gets it.
    target = paths.state_dir() / "hq-start.json"
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps({"source": event.get("source"), "session_id": sid, "ts": time.time()}))
    os.replace(tmp, target)
    if record.get("claude_args"):
        _register(record["pane"], sid, record["claude_args"])


def main():
    try:
        config.sync_root_hint()  # so the shell fast path reads a current worktree root next time
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return
        name = worker_name(os.environ, event.get("cwd") or os.getcwd())
        if (name or os.environ.get("ATHENA_ROLE") == "hq") and nested():
            return  # a Claude the worker (or HQ) started: nothing of it is theirs, not even a resume command
        if name:
            decision = gate(apply(event, name, gated=bool((os.environ.get("ATHENA_WORKER") or "").strip())))
            register_resume(event, name, os.environ)
            if decision:
                print(json.dumps(decision))
        else:
            register_hq_resume(event, os.environ)
            log_request(event, os.environ)
    except Exception:  # a hook must never break the session
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
