"""Retire a worker: back up its head, refuse to lose work, stop its Claude, remove its worktree space."""
import os
import time

from athena_lib import gitops, herdr, ledger


class RetireError(Exception):
    pass


def workspace_owned(workspace_id, path) -> bool:
    """herdr may reuse ids after a restart: only act on a workspace whose checkout is the worker's path."""
    if not workspace_id or not path:
        return False
    try:
        ws = herdr.call("workspace", "get", workspace_id, timeout=10).get("workspace", {})
    except herdr.HerdrError:
        return False
    checkout = (ws.get("worktree") or {}).get("checkout_path")
    return bool(checkout) and os.path.realpath(checkout) == os.path.realpath(path)


def agent_owned(name) -> bool:
    try:
        agent = herdr.call("agent", "get", name, timeout=10).get("agent", {})
    except herdr.HerdrError:
        return False
    return agent.get("name") == name


def remove_worktree(worker, force=False) -> dict:
    """Remove the worker's space through herdr when it is still ours (that also stops its Claude), else the checkout.

    If herdr will not remove a space that is ours, leave the checkout too: removing only the checkout would leave
    a space the ledger forgets. The caller gets herdr_error and keeps the worker tracked.
    """
    path, repo = worker.get("path"), worker.get("repo")
    out = {"removed": False}
    if workspace_owned(worker.get("workspace"), path):
        args = ["worktree", "remove", "--workspace", worker["workspace"]] + (["--force"] if force else [])
        try:
            herdr.call(*args, timeout=60)
            out["removed"] = True
            return out
        except herdr.HerdrError as exc:
            out["herdr_error"] = str(exc)
            return out
    if path and os.path.isdir(path) and repo:
        gitops.git(repo, "worktree", "remove", *(["--force"] if force else []), path)
        out["removed"] = True
    return out


def retire(name, force=False, keep_worktree=False) -> dict:
    worker = ledger.workers().get(name)
    if not worker or worker.get("state") not in ("live", "failed"):
        raise RetireError(f"no live or failed worker named {name}")
    path, repo = worker.get("path"), worker.get("repo")
    result = {"name": name, "backup": None, "wip_backup": None, "removed": False}
    exists = bool(path) and os.path.isdir(path)
    if exists:
        head = gitops.git(path, "rev-parse", "HEAD", check=False)
        if head and repo:
            result["backup"] = gitops.backup_ref(repo, name, head)
        state = gitops.unpushed(path)
        problems = []
        if state["dirty"]:
            problems.append(f"{len(state['dirty'])} uncommitted files")
        if state["ahead"]:
            problems.append(f"{state['ahead']} unpushed commit{'s' if state['ahead'] != 1 else ''}")
        if problems and not force:
            raise RetireError(f"{name} has " + " and ".join(problems) + f"; head backed up at {result['backup']}. "
                              "Push or commit first, or retire with --force.")
        if state["dirty"] and repo:
            wip = gitops.git(path, "stash", "create", "athena retire " + name, check=False)
            if wip:
                result["wip_backup"] = f"refs/athena-backup/{name}/{int(time.time())}-wip"
                gitops.git(repo, "update-ref", result["wip_backup"], wip)
    if worker.get("goal_pending"):
        # Its Claude may sit at a startup prompt: typing /exit there could answer it. Closing the pane stops it.
        if workspace_owned(worker.get("workspace"), path) and worker.get("pane"):
            try:
                herdr.call("pane", "close", worker["pane"], timeout=15)
            except herdr.HerdrError:
                pass
    elif agent_owned(name):
        try:
            herdr.call("agent", "prompt", name, "/exit", timeout=15)
            time.sleep(float(os.environ.get("ATHENA_EXIT_GRACE", "2")))
        except herdr.HerdrError:
            pass
    if not keep_worktree:
        result.update(remove_worktree(worker, force=force))
        if result.get("herdr_error"):
            ledger.append("update", name, cleanup_error=result["herdr_error"])
            raise RetireError(f"herdr did not remove {name}'s space ({result['herdr_error']}); {name} stays tracked. "
                              f"Retry with: athena retire {name}")
    ledger.append("retire", name, backup=result["backup"], wip_backup=result["wip_backup"], removed=result["removed"])
    return result
