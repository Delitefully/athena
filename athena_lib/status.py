"""One status per worker, merged from several sources, most trusted first:
the worker's ATHENA-REPORT, its hook state, git and PR state, then herdr's screen-read status."""
import json

from athena_lib import gitops, herdr, hookstatus, ledger, pr, sockets

URGENCY = {"blocked": 0, "done": 1, "exited": 1, "idle-unreported": 1, "idle": 2, "starting": 3, "working": 3, "unknown": 3}


def urgency(state: str) -> int:
    return URGENCY.get(state or "unknown", 3)


def quick_state(worker) -> str:
    """Hook state only: cheap enough for the board loop. A pending worker needs the human, so it counts as blocked."""
    if worker.get("goal_pending"):
        return "blocked"
    hook = hookstatus.read(worker["name"]) or {}
    return hook.get("state") or "unknown"


def _is_goal_record(record) -> bool:
    """Claude Code's own record of a /goal that ran. Only the record's shape counts: a pasted goal, or a brief read
    later, can name these words anywhere."""
    if not isinstance(record, dict):
        return False
    attachment = record.get("attachment")
    if record.get("type") == "attachment" and isinstance(attachment, dict):
        return attachment.get("type") == "goal_status"
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return record.get("type") == "user" and isinstance(content, str) and content.startswith("<local-command-stdout>Goal set:")


def _has_goal_record(path) -> bool:
    try:
        with open(path, "rb") as f:
            for line in f:
                if b"goal_status" in line or b"Goal set:" in line:  # most lines never need parsing
                    try:
                        if _is_goal_record(json.loads(line)):
                            return True
                    except ValueError:
                        pass
    except OSError:
        pass
    return False


def goal_took(name, session=None) -> bool:
    """Whether the worker's transcript shows its /goal ran as a command. Either session id will do: herdr may not know
    one yet at spawn, and the hook's is the newer after a resume."""
    from athena_lib import handoff  # handoff imports this module
    sessions = {(hookstatus.read(name) or {}).get("session_id"), session}
    return any(_has_goal_record(p) for p in map(handoff.transcript, sessions) if p)


def _goal_unconfirmed(worker, write=True) -> bool:
    """Read again each time: enter pressed on a send left in the input box, or a resend, clears it. Once seen it stays
    seen, in the ledger: a /clear moves the worker on to a transcript with no goal record in it."""
    if worker.get("goal_state") != "unconfirmed":
        return False
    if not goal_took(worker["name"], worker.get("session")):
        return True
    if write:
        ledger.append("update", worker["name"], goal_state="confirmed")
    return False


def collect(worker, with_pr=False, with_herdr=True, write=True) -> dict:
    """write=False for a caller that promises to write nothing, such as a dry run."""
    name = worker["name"]
    hook = hookstatus.read(name) or {}
    out = {"name": name, "branch": worker.get("branch"), "linear": worker.get("linear"),
           "pane": worker.get("pane"), "workspace": worker.get("workspace"),
           "ultracode": bool(worker.get("ultracode")), "workflow_size": worker.get("workflow_size"),
           "state": hook.get("state") or "unknown", "summary": hook.get("summary", ""),
           "report": hook.get("report"), "last_report": hook.get("last_report"),
           "hook_ts": hook.get("ts"), "herdr": None, "git": None, "pr": None,
           "pending": bool(worker.get("goal_pending")), "athena_review": worker.get("review"), "detail": None, "prompt": worker.get("prompt") if worker.get("goal_send") == "deliver" else None,
           "socket_missing": sockets.missing(name, hook.get("session_id")),
           "goal_unconfirmed": _goal_unconfirmed(worker, write)}
    if with_herdr and worker.get("pane"):
        try:
            agent = herdr.call("agent", "get", worker["pane"], timeout=15).get("agent", {})
            out["herdr"] = agent.get("agent_status")
        except herdr.HerdrError as exc:
            out["herdr"] = f"error:{exc.code}"
    if worker.get("path"):
        try:
            out["git"] = gitops.unpushed(worker["path"])
            out["git"]["head"] = gitops.git(worker["path"], "rev-parse", "--short", "HEAD", check=False)
        except gitops.GitError:
            out["git"] = None
    if out["pending"]:
        # Its goal waits for a ready Claude: blocked means a startup prompt only the human may answer.
        out["state"] = "blocked" if out["herdr"] == "blocked" else "starting"
        out["detail"] = "startup prompt" if out["state"] == "blocked" else None
        out["state_source"] = "pending"
    elif out["state"] in ("unknown", "starting") and out["herdr"] in ("blocked", "working", "idle", "done"):
        out["state"] = out["herdr"]
        out["state_source"] = "herdr"
    else:
        out["state_source"] = "hook"
    if with_pr and worker.get("repo") and worker.get("branch"):
        out["pr"] = pr.summary(worker["repo"], worker["branch"])
    out["urgency"] = urgency(out["state"])
    return out
