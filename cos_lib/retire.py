"""Retire a worker: back up its head, refuse to lose work, stop its Claude, remove its worktree space."""
import os
import time

from cos_lib import gitops, herdr, ledger


class RetireError(Exception):
    pass


def retire(name, force=False, keep_worktree=False) -> dict:
    worker = ledger.workers().get(name)
    if not worker or worker.get("state") not in ("live", "failed"):
        raise RetireError(f"no live or failed worker named {name}")
    path, repo = worker.get("path"), worker.get("repo")
    result = {"name": name, "backup": None, "removed": False}
    if path and os.path.isdir(path):
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
            raise RetireError(f"{name} has " + " and ".join(problems) + f"; backup at {result['backup']}. "
                              "Push or commit first, or retire with --force.")
    pane = worker.get("pane")
    if pane:
        try:
            herdr.call("agent", "prompt", pane, "/exit", timeout=15)
            time.sleep(float(os.environ.get("COS_EXIT_GRACE", "2")))
        except herdr.HerdrError:
            pass
    if not keep_worktree and worker.get("workspace"):
        args = ["worktree", "remove", "--workspace", worker["workspace"]] + (["--force"] if force else [])
        try:
            herdr.call(*args, timeout=60)
            result["removed"] = True
        except herdr.HerdrError as exc:
            if path and os.path.isdir(path) and repo:
                extra = ["--force"] if force else []
                gitops.git(repo, "worktree", "remove", *extra, path)
                result["removed"] = True
            result["herdr_error"] = str(exc)
    ledger.append("retire", name, backup=result["backup"], removed=result["removed"])
    return result
