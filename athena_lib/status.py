"""One status per worker, merged from several sources, most trusted first:
the worker's ATHENA-REPORT, its hook state, git and PR state, then herdr's screen-read status."""
from athena_lib import gitops, herdr, hookstatus, pr

URGENCY = {"blocked": 0, "done": 1, "exited": 1, "idle": 2, "starting": 3, "working": 3, "unknown": 3}


def urgency(state: str) -> int:
    return URGENCY.get(state or "unknown", 3)


def quick_state(worker) -> str:
    """Hook state only: cheap enough for the board loop. A pending worker needs the human, so it counts as blocked."""
    if worker.get("goal_pending"):
        return "blocked"
    hook = hookstatus.read(worker["name"]) or {}
    return hook.get("state") or "unknown"


def collect(worker, with_pr=False, with_herdr=True) -> dict:
    name = worker["name"]
    hook = hookstatus.read(name) or {}
    out = {"name": name, "branch": worker.get("branch"), "linear": worker.get("linear"),
           "pane": worker.get("pane"), "workspace": worker.get("workspace"),
           "state": hook.get("state") or "unknown", "summary": hook.get("summary", ""),
           "report": hook.get("report"), "last_report": hook.get("last_report"),
           "hook_ts": hook.get("ts"), "herdr": None, "git": None, "pr": None,
           "pending": bool(worker.get("goal_pending")), "detail": None}
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
        out["detail"] = "startup prompt" if out["state"] == "blocked" else "goal pending"
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
