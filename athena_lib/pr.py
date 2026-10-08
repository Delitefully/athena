"""Pull request state from `gh`, reduced to what the chief of staff acts on."""
import json
import subprocess

FIELDS = "number,url,state,isDraft,reviewDecision,headRefOid,mergeable,statusCheckRollup,comments,reviews"


def reduce_checks(rollup) -> str:
    states = []
    for check in rollup or []:
        value = (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        states.append(value)
    if not states:
        return "none"
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") for s in states):
        return "failure"
    if any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED", "") for s in states):
        return "pending"
    return "success"


def reduce(data: dict) -> dict:
    return {
        "number": data.get("number"), "url": data.get("url"), "state": data.get("state"),
        "draft": data.get("isDraft"), "review": data.get("reviewDecision") or "",
        "head": (data.get("headRefOid") or "")[:12], "mergeable": data.get("mergeable"),
        "checks": reduce_checks(data.get("statusCheckRollup")),
        "comments": len(data.get("comments") or []) + sum(1 for r in data.get("reviews") or [] if r.get("body")),
    }


def summary(repo, branch):
    try:
        proc = subprocess.run(["gh", "pr", "view", branch, "--json", FIELDS], cwd=str(repo),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return reduce(json.loads(proc.stdout))
    except ValueError:
        return None
