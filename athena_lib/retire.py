"""Retire a worker: back up its head, refuse to lose work, stop its Claude, remove its worktree space."""
import os
import shutil
import time

from athena_lib import dashstate, gitops, herdr, ledger


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


def _gone(name, grace) -> bool:
    deadline = time.monotonic() + grace
    while agent_owned(name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def stop_claude(worker) -> bool:
    """Exit the worker's Claude before its checkout is removed, so nothing writes into a directory being deleted.

    `/exit` alone is not enough: typing it opens Claude Code's slash-command menu, and the single Enter that
    `agent prompt` sends picks the suggestion instead of submitting it. So: /exit, then one more Enter, then close
    the pane when it is ours. True when the Claude is gone.
    """
    name = worker["name"]
    if not agent_owned(name):
        return True
    grace = float(os.environ.get("ATHENA_EXIT_GRACE", "5"))
    for step in (("agent", "prompt", name, "/exit"), ("agent", "send-keys", name, "enter")):
        try:
            herdr.call(*step, timeout=15)
        except herdr.HerdrError:
            pass
        if _gone(name, grace):
            return True
    if worker.get("pane") and workspace_owned(worker.get("workspace"), worker.get("path")):
        try:
            herdr.call("pane", "close", worker["pane"], timeout=15)
            return True
        except herdr.HerdrError:
            pass
    return False


def remove_worktree(worker, force=False) -> dict:
    """Remove the worker's space through herdr when it is still ours (that also stops its Claude), else the checkout.

    If herdr will not remove a space that is ours, leave the checkout too: removing only the checkout would leave
    a space the ledger forgets. The caller gets herdr_error and keeps the worker tracked.
    """
    path, repo = worker.get("path"), worker.get("repo")
    out = {"removed": False}
    orphan = bool(path) and os.path.isdir(path) and not gitops.ok(path, "rev-parse", "--git-dir")
    if orphan and force:
        # Only reachable with --force: the directory has no git metadata left for herdr or git to remove it by.
        if workspace_owned(worker.get("workspace"), path):
            try:
                herdr.call("workspace", "close", worker["workspace"], timeout=30)
            except herdr.HerdrError as exc:
                out["herdr_error"] = str(exc)
        shutil.rmtree(path, ignore_errors=True)
        if repo:
            gitops.git(repo, "worktree", "prune", check=False)
        out["removed"] = not os.path.exists(path)
        return out
    if workspace_owned(worker.get("workspace"), path):
        if not os.path.isdir(path):
            # The checkout went away outside athena; herdr's worktree remove would fail on it every time.
            try:
                herdr.call("workspace", "close", worker["workspace"], timeout=30)
                out["removed"] = True
            except herdr.HerdrError as exc:
                out["herdr_error"] = str(exc)
            if repo:
                gitops.git(repo, "worktree", "prune", check=False)
            return out
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
    orphan = exists and not gitops.ok(path, "rev-parse", "--git-dir")
    if orphan and not force:
        # The directory outlived its git metadata (a removal that stopped halfway): git can't say what it holds.
        raise RetireError(f"{path} is no longer a git worktree, so nothing in it can be checked or backed up; "
                          f"{name} stays tracked. Look inside, then retire with --force to close its space and "
                          "delete the directory.")
    if exists and not orphan:
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
    elif not stop_claude(worker) and not force and exists and not keep_worktree:
        # Only refuse when its checkout is about to be removed under a running Claude.
        ledger.append("update", name, cleanup_error="claude did not exit")
        raise RetireError(f"{name}'s Claude did not exit and its pane is not one athena can close; {name} stays "
                          f"tracked. Exit it by hand, then retry, or retire with --force.")
    if not keep_worktree:
        result.update(remove_worktree(worker, force=force))
        if result.get("herdr_error"):
            if not force:
                ledger.append("update", name, cleanup_error=result["herdr_error"])
                raise RetireError(f"herdr did not remove {name}'s space {worker.get('workspace')} "
                                  f"({result['herdr_error']}); {name} stays tracked. Retry with `athena retire {name}`, "
                                  f"or `athena retire {name} --keep-worktree`, then close the space by hand")
            result["space_left_open"] = worker.get("workspace")
    dashstate.record_retire(worker)
    ledger.append("retire", name, backup=result["backup"], wip_backup=result["wip_backup"], removed=result["removed"],
                  cleanup_error=result.get("herdr_error"))
    return result
