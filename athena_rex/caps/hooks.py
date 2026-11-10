"""Claude Code hook events to status files, and the report gate. Installed per session through `--settings`, not globally,
so it fires only in a Claude athena started: a Claude that one starts from its Bash tool has none of these hooks.
"""
import json
import os
import re
import subprocess
import sys
import time

from athena_rex.kernel import bus, caps, store

SUMMARY = "claude code hook events and the report gate (internal)"
SUMMARY_LIMIT = 300
DONE = {"DONE", "DONE_WITH_CONCERNS"}
BLOCKED = {"BLOCKED", "NEEDS_CONTEXT"}
IN_PROGRESS = "IN_PROGRESS"
NEEDS_HUMAN = {"permission_prompt", "agent_needs_input", "elicitation_dialog"}
MAX_BLOCKS = 3
BLOCKS_FORGOTTEN_AFTER = 300
REPORT_FORMAT = ('ATHENA-REPORT {"status":"DONE|DONE_WITH_CONCERNS|IN_PROGRESS|NEEDS_CONTEXT|BLOCKED","pr":"<url or empty>",'
                 '"head":"<commit sha>","verify":"<command> -> pass|fail","decisions":[],"concerns":[]}')
_REPORT_LINE = re.compile(r"^\s*ATHENA-REPORT (.*)$", re.M)
_PASS = re.compile(r"->\s*pass\b", re.I)
_NO_PR = re.compile(r"\bno (?:draft )?(?:PRs?|pull requests?)(?=\s*(?:$|[.,:;)(—–-]|(?:and|or|here|needed|required|at all|in this repo|in scope|is (?:needed|required|expected)|for this)\b))", re.I | re.M)
_NOT_PASS = re.compile(r"->\s*(?:pass\s*\|\s*fail|fail|not run|skipped|pending)\b", re.I)
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd")


def settings_block() -> dict:
    """The `hooks` key for a session's --settings file."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    command = f'"{root}/bin/athena-rex" hook'
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


def refusal(message, report, cwd, brief=None):
    """Why this turn may not end yet, or None. BLOCKED, NEEDS_CONTEXT and IN_PROGRESS need nothing but the line."""
    if report is None:
        if _has_report_line(message):
            return "That ATHENA-REPORT line doesn't parse as JSON. Finish with one that does: " + REPORT_FORMAT
        return ("This turn has no ATHENA-REPORT line. Say IN_PROGRESS if background work is still running, BLOCKED with "
                "your question in concerns if you are stuck, or carry on and report when done. Your last line, as: "
                + REPORT_FORMAT)
    status = str(report.get("status") or "").upper()
    if status not in DONE:
        return None
    verify = str(report.get("verify") or "")
    if not _PASS.search(verify) or _NOT_PASS.search(verify):
        return (f"{status} is only for a passing VERIFY. Run it at your last commit, show the output, and write "
                '`"verify":"<command> -> pass"`. If it can\'t pass, report BLOCKED and say why in concerns.')
    if cwd and not os.path.isdir(cwd):
        return f"Your worktree {cwd} is gone, so {status} cannot be checked. Report BLOCKED with that in concerns."
    head = _head(cwd)
    if head and not same_head(report.get("head"), head):
        return (f"The head in your report ({report.get('head') or 'empty'}) differs from this worktree's ({head[:12]}). "
                "Push, run VERIFY on that commit, and report it.")
    if head and _needs_pr(cwd, brief) and not str(report.get("pr") or "").startswith("https://"):
        return f"{status} needs your PR: this repo is on GitHub and the report's pr is empty. Open it and report its URL, or report BLOCKED and say why."
    return None


def next_state(state, event) -> tuple:
    """The worker's activity as a pure step: (state, report_changes). The one state machine for hook events.

    starting -SessionStart-> idle -UserPromptSubmit-> working -Stop-> done|blocked|idle (by report)
    any -Notification(needs human)-> blocked; working -Notification(idle_prompt)-> idle; any -SessionEnd-> exited
    """
    kind = event.get("hook_event_name") or ""
    if kind == "SessionStart":
        return "idle", {}
    if kind == "UserPromptSubmit":
        return "working", {"report": None}
    if kind == "Stop":
        message = event.get("last_assistant_message") or ""
        report = parse_report(message)
        cut = message.rfind("ATHENA-REPORT ") if report else -1
        status = str((report or {}).get("status") or "").upper()
        return ("done" if status in DONE else "blocked" if status in BLOCKED else "idle",  # IN_PROGRESS rests as idle
                {"report": report, "summary": (message[:cut] if cut >= 0 else message).strip()[-SUMMARY_LIMIT:]})
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
    state, changes = next_state(prev.get("state", "starting"), event)
    if state is None:
        return {}
    kind = event["hook_event_name"]
    report = changes.get("report", prev.get("report"))
    blocks = prev.get("stop_blocks") or 0
    block_ts = prev.get("stop_block_ts")
    if time.time() - (block_ts or 0) > BLOCKS_FORGOTTEN_AFTER:
        blocks = 0
    decision = None
    if kind in ("UserPromptSubmit", "SessionStart"):
        blocks = 0
    elif kind == "Stop" and gated:
        why = refusal(event.get("last_assistant_message") or "", report, event.get("cwd"))
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
            "stop_blocks": blocks, "stop_block_ts": block_ts}
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
