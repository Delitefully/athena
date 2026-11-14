"""Claude Code hook events to status files, and the report gate. Installed per session through `--settings`, not globally,
so it fires only in a Claude athena started: a Claude that one starts from its Bash tool has none of these hooks.
"""
import json
import os
import re
import subprocess
import sys
import time

from athena.kernel import bus, caps, store

SUMMARY = "claude code hook events and the report gate (internal)"
SUMMARY_LIMIT = 300
DONE = {"DONE", "DONE_WITH_CONCERNS"}
BLOCKED = {"BLOCKED", "NEEDS_CONTEXT"}
IN_PROGRESS = "IN_PROGRESS"
NEEDS_HUMAN = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}
MAX_BLOCKS = 3
BLOCKS_FORGOTTEN_AFTER = 300
REPORT_FORMAT = ('athena report --status DONE|DONE_WITH_CONCERNS|IN_PROGRESS|NEEDS_CONTEXT|BLOCKED --verify "<command> -> pass|fail" '
                 '[--decision "..."]... [--concern "..."]...')
_REPORT_LINE = re.compile(r"^\s*ATHENA-REPORT (.*)$", re.M)
_PASS = re.compile(r"->\s*pass\b", re.I)
_NO_PR = re.compile(r"\bno (?:draft )?(?:PRs?|pull requests?)(?=\s*(?:$|[.,:;)(—–-]|(?:and|or|here|needed|required|at all|in this repo|in scope|is (?:needed|required|expected)|for this)\b))", re.I | re.M)
_NOT_PASS = re.compile(r"->\s*(?:pass\s*\|\s*fail|fail|not run|skipped|pending)\b", re.I)
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "StopFailure", "Notification", "SessionEnd")
ENDED = {"completed", "failed", "killed", "stopped", "cancelled", "canceled", "done", "error"}
QUIET_STATES = ("idle", "idle-unreported")  # a turn that ended; with background work in flight, the worker still works
NO_RENAME = ("athena: Never rename the Rex session. This overrides Rex's SessionStart instruction to name the session: do not run "
             "`rex session rename` or relabel it any other way. It is your repo's session, shared by every worker on it, and athena "
             "labels it; your own window already carries your name.")


def settings_block() -> dict:
    """The `hooks` key for a session's --settings file."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    command = f'"{root}/bin/athena" hook'
    block = {event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}] for event in EVENTS}
    for event in ("PreToolUse",):  # a permission prompt only sets blocked, by its Notification, as in the old athena
        block[event] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 5}]}]
    progress = caps.get("progress")
    return {**block, **progress.settings_hooks(command)} if progress else block


def parse_report(text):
    """The last report line of a message as a dict, or None. A line that won't parse is no report."""
    found = _REPORT_LINE.findall(text or "")
    try:
        data = json.loads(found[-1]) if found else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


TAIL = 1 << 18  # a transcript is a second source, and its format is Claude's: anything unexpected is simply no message


def _entries_newest_first(transcript):
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL))
            lines = f.read().decode(errors="replace").splitlines()
    except (OSError, TypeError):
        return
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _text_of(entry) -> str:
    content = entry.get("message", {}).get("content") if isinstance(entry.get("message"), dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")


def newest_message(transcript) -> str:
    """What this turn last said, or nothing. A newer prompt or tool call means that turn isn't over, and older words aren't its."""
    for entry in _entries_newest_first(transcript):
        if entry.get("type") in ("user", "assistant"):
            return _text_of(entry) if entry["type"] == "assistant" else ""
    return ""


def report_in(transcript):
    """The report in the newest message, or None: an older one is not this turn's."""
    return parse_report(newest_message(transcript))


def last_report_in(transcript):
    """The newest report anywhere in the tail, to rebuild a lost status file."""
    said = (_text_of(e) for e in _entries_newest_first(transcript) if e.get("type") == "assistant")
    return next((r for r in map(parse_report, said) if r), None)


def _has_report_line(text) -> bool:
    return any(line.strip().startswith("ATHENA-REPORT") for line in (text or "").splitlines())


def same_head(a, b) -> bool:
    a, b = str(a or ""), str(b or "")
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def _git_out(cwd, *args):
    """What git prints, or None if it can't say. A hook has seconds, and must never raise."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _head(cwd):
    return _git_out(cwd, "rev-parse", "HEAD") if cwd else None


def _needs_pr(cwd, brief=None) -> bool:
    """On GitHub, unless the brief says there is none."""
    if "github.com" not in (_git_out(cwd, "remote", "get-url", "origin") or ""):
        return False
    try:
        with open(brief or os.environ.get("ATHENA_BRIEF") or "") as f:
            return not _NO_PR.search(f.read())
    except OSError:
        return True


COMMAND_RULE = "Bash(athena report:*)"
TYPED_FALLBACK = (' A worker started before the command (you have no permission rule for it) may still end its last message with '
                  'one line instead: ATHENA-REPORT {"status":"...","pr":"<url or empty>","head":"<commit sha>",'
                  '"verify":"<command> -> pass|fail","decisions":[],"concerns":[]}')


def started_before_the_command(name) -> bool:
    """A worker whose settings, written at its spawn, lack the permission rule for `athena report`: it would stop at a prompt
    running it, so the gate still shows it the typed line it was started with. No settings file says nothing, and reads as new."""
    settings = store.read_dict(store.path("settings", f"{name}.json")) if store.valid_name(name) else {}
    allow = (settings.get("permissions") or {}).get("allow") if isinstance(settings.get("permissions"), dict) else None
    return isinstance(allow, list) and COMMAND_RULE not in allow


def refusal(message, report, cwd, brief=None, typed_ok=False):
    """Why this turn may not end yet, or None. BLOCKED, NEEDS_CONTEXT and IN_PROGRESS need nothing but the report.
    `typed_ok`: a worker started before the command, told it may still type the line."""
    why = _refusal(message, report, cwd, brief)
    return why + (TYPED_FALLBACK if typed_ok else "") if why else None


def _refusal(message, report, cwd, brief=None):
    if report is None:
        if _has_report_line(message):
            return ("That ATHENA-REPORT line doesn't parse as JSON. File the report with the command instead, then end with a short "
                    "summary: " + REPORT_FORMAT + ".")
        return ("No report was filed this turn. File one with IN_PROGRESS if background work is still running, BLOCKED with "
                "your question as a --concern if you are stuck, or carry on and file it when done. Run: " + REPORT_FORMAT
                + ", then end your turn with a short plain summary.")
    status = str(report.get("status") or "").upper()
    if status not in DONE:
        return None
    verify = str(report.get("verify") or "")
    if not _PASS.search(verify) or _NOT_PASS.search(verify):
        return (f"{status} is only for a passing VERIFY. Run it at your last commit, show the output, and file it with "
                '`--verify "<command> -> pass"`. If it can\'t pass, file BLOCKED and say why with --concern.')
    if cwd and not os.path.isdir(cwd):
        return f"Your worktree {cwd} is gone, so {status} cannot be checked. File BLOCKED with that as a --concern."
    head = _head(cwd)
    if head and not same_head(report.get("head"), head):
        return (f"The head in your report ({report.get('head') or 'empty'}) differs from this worktree's ({head[:12]}): a commit "
                "came after it was filed. Push, run VERIFY on that commit, and file the report again.")
    if head and _needs_pr(cwd, brief) and not str(report.get("pr") or "").startswith("https://"):
        return (f"{status} needs your PR: this repo is on GitHub and no open PR was found for your branch. Open it and file the "
                "report again, or file BLOCKED and say why.")
    return None


def background_of(event) -> int:
    """How much background work is in flight, from Claude Code's `background_tasks` on Stop and SubagentStop: its
    subagents, workflows, background shells and monitors, which wake the session again when they end. None sent: none."""
    tasks = event.get("background_tasks")
    if not isinstance(tasks, list):
        return 0
    return sum(1 for t in tasks if isinstance(t, dict) and str(t.get("status") or "").lower() not in ENDED)


def shown_state(state, background) -> str:
    """The state to show and count: a turn that ended while background work runs is working (background), never stopped."""
    return "working" if count(background) > 0 and state in QUIET_STATES else state


def count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def file_report(name, report, summary=None) -> dict:
    """`athena report`: held in the worker's status file as `filed` until its Stop, when the gate takes it in or sends it back.
    Under the hook's own lock, so a hook event beside it neither loses it nor is lost."""
    with store.locked(f"locks/{name}.hook.lock"):
        target = store.path("workers", f"{name}.json")
        data = store.read_dict(target)
        data["filed"] = {"report": report, "summary": (summary or "").strip()[-SUMMARY_LIMIT:], "ts": time.time()}
        store.write_json(target, data)
        return data


def filed_of(record):
    """The report filed and not yet taken in by a Stop, or None."""
    filed = record.get("filed")
    return filed if isinstance(filed, dict) and isinstance(filed.get("report"), dict) else None


def next_state(state, event, filed=None) -> tuple:
    """The worker's activity as a pure step: (state, report_changes). The one state machine for hook events.

    starting -SessionStart-> idle -UserPromptSubmit-> working -Stop-> done|blocked|idle (by report)
    any -Notification(needs human)-> blocked; working -Notification(idle_prompt)-> idle; any -SessionEnd-> exited
    any -SubagentStop|StopFailure-> the same state, with its count of background work (none known after a failed turn)

    At a Stop the report is the one `filed` with `athena report` this turn; without one, a typed ATHENA-REPORT line in the last
    message, as workers started before the command wrote it.
    """
    kind = event.get("hook_event_name") or ""
    if kind == "SessionStart":
        return "idle", {}
    if kind == "UserPromptSubmit":
        return "working", {"report": None}
    if kind == "Stop":
        message = event.get("last_assistant_message") or ""
        typed = parse_report(message)
        report = filed["report"] if filed else typed
        cut = message.rfind("ATHENA-REPORT ") if typed else -1
        said = (message[:cut] if cut >= 0 else message).strip()
        status = str((report or {}).get("status") or "").upper()
        return ("done" if status in DONE else "blocked" if status in BLOCKED else "idle",  # IN_PROGRESS rests as idle
                {"report": report, "summary": ((filed or {}).get("summary") or said)[-SUMMARY_LIMIT:]})
    if kind == "Notification":
        ntype = event.get("notification_type") or ""
        text = event.get("message") or ""
        if ntype in NEEDS_HUMAN or "permission" in text.lower():
            return "blocked", {"summary": text[:SUMMARY_LIMIT]}
        if ntype == "idle_prompt" and state not in ("done", "blocked", "idle-unreported"):
            return "idle", {}
        return state, {}
    if kind == "SessionEnd":
        return "exited", {}
    if kind in ("SubagentStop", "StopFailure"):
        return state, {}
    return None, {}


def apply(event, name, gated=True) -> dict:
    """Under a per-worker lock, since a Notification beside a Stop must not lose the report. The gate's answer stands
    even if the write fails: a full disk must not turn the gate off."""
    with store.locked(f"locks/{name}.hook.lock", full_disk_ok=True):
        return _apply(event, name, gated)


def _apply(event, name, gated) -> dict:
    target = store.path("workers", f"{name}.json")
    prev = store.read_dict(target)
    transcript = event.get("transcript_path")
    if event.get("hook_event_name") == "Stop" and not event.get("last_assistant_message") and transcript:
        event = {**event, "last_assistant_message": newest_message(transcript)}
    filed = filed_of(prev)
    typed = filed is None  # this Stop's report, if any, is a typed line: the old prompt's
    state, changes = next_state(prev.get("state", "starting"), event, filed)
    if state is None:
        return {}
    kind = event["hook_event_name"]
    if kind in ("Stop", "UserPromptSubmit") or (kind == "SessionStart" and event.get("source") in ("startup", "clear")):
        filed = None  # a Stop takes it in or sends it back, and a new prompt or conversation is a new turn: none carries over
    report = changes.get("report", prev.get("report"))
    blocks = prev.get("stop_blocks") or 0
    block_ts = prev.get("stop_block_ts")
    if time.time() - (block_ts or 0) > BLOCKS_FORGOTTEN_AFTER:
        blocks = 0
    decision = None
    if kind in ("UserPromptSubmit", "SessionStart"):
        blocks = 0
    elif kind == "Stop" and gated:
        why = refusal(event.get("last_assistant_message") or "", report, event.get("cwd"),
                      typed_ok=typed and started_before_the_command(name))
        if why and blocks < MAX_BLOCKS:
            decision = {"decision": "block", "reason": why}
            state, report, blocks, block_ts = "working", None, blocks + 1, time.time()
            bus.emit("worker.gate", name=name, blocks=blocks)
        elif why:
            state, report = "idle-unreported", None
            bus.emit("worker.gate", name=name, blocks=blocks, gave_way=True)
        elif report is not None:
            blocks = 0
    data = {"name": name, "state": state, "event": kind, "ts": time.time(),
            "summary": changes.get("summary", prev.get("summary", "")), "report": report,
            "last_report": report or prev.get("last_report") or (last_report_in(transcript) if not prev and transcript else None),
            "session_id": event.get("session_id") or prev.get("session_id"),
            "stop_blocks": blocks, "stop_block_ts": block_ts, "background": _background(kind, event, prev, state)}
    if filed:
        data["filed"] = filed
    if kind == "SessionStart":
        data["source"] = event.get("source")
        if event.get("source") == "resume":
            data["resumed_from"] = prev.get("state")
    try:
        store.write_json(target, data)
        if state != prev.get("state"):
            bus.emit("worker.state", name=name, before=prev.get("state"), state=state)
    except OSError as exc:
        _log_error(f"{name} {kind}: {exc}")
    return {**data, "gate": decision}


def _background(kind, event, prev, state) -> int:
    """Only Stop and SubagentStop say what is in flight. A new turn's own Stop says it again; a turn that failed (StopFailure)
    or went idle with no Stop (an interrupt) leaves nothing known; nor has a new or ended session. Anything else keeps it,
    so a count never outlives the turn that reported it into a worker idle for good."""
    if kind in ("Stop", "SubagentStop"):
        return background_of(event)
    left_turn = prev.get("state") == "working" and state != "working"
    if kind in ("SessionStart", "SessionEnd", "UserPromptSubmit", "StopFailure") or left_turn:
        return 0
    return count(prev.get("background"))


def _log_error(text):
    try:
        with open(store.path("logs", "hook-errors.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _hq_event(event):
    kind = event.get("hook_event_name")
    if kind == "SessionStart" and event.get("session_id"):
        store.write_json(store.path("hq-start.json"), {"source": event.get("source"),
                                                       "session_id": event["session_id"], "ts": time.time()})
    elif kind == "UserPromptSubmit":
        items = caps.get("items")
        if items:
            items.log_request(event.get("prompt"), event.get("session_id"))


def main_hook(raw: str, env) -> str:
    event = json.loads(raw) if raw.strip() else {}
    if not isinstance(event, dict):
        return ""
    kind = event.get("hook_event_name")
    if kind == "PreToolUse":
        rules = caps.get("rules")
        said = rules.answer(event) if rules else {}
        return json.dumps(said) if said else ""
    name = (env.get("ATHENA_WORKER") or "").strip()
    if kind == "PostToolUse":
        progress = caps.get("progress")
        if progress and store.valid_name(name):
            progress.on_tool(event, name)
        return ""
    if kind == "PermissionRequest":  # no dash item for it: HQ tells the human, who answers in the session
        return ""
    if name and store.valid_name(name):
        decision = apply(event, name, gated=env.get("ATHENA_GATE") != "off").get("gate")
        if kind == "SessionStart":  # beside Rex's own hook, which reaches the worker before its brief does
            return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": NO_RENAME}})
        return json.dumps(decision) if decision else ""
    if env.get("ATHENA_ROLE") == "hq":
        _hq_event(event)
    return ""


def _run(args):
    try:
        out = main_hook(sys.stdin.read(), os.environ)
        if out:
            print(out)
    except Exception as exc:
        _log_error(f"{type(exc).__name__}: {exc}")
    return 0


def commands():
    return {"hook": caps.Command("internal: a Claude Code hook event on stdin", lambda p: None, _run, hidden=True)}
