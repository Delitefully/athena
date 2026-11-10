"""Retire a worker: save everything only it holds, stop its Claude, remove its worktree and its session.

    saved -> marked -> stopped -> removed -> closed -> retired

Backups come first and are never skipped, --force included: unsaved work becomes refs under refs/athena-backup/<name>/ before
anything is touched, and a backup that fails stops the retire. Any failure leaves the worker tracked and takes the mark off. A kill
leaves both, and the way back is to run it again: each stage asks the world what is left to do, so a stage is never stored and never
wrong. What the world can't say goes on record as it is made: where the work went (`_note`), and what else ran on its terminal.
"""
import json
import os
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import agents, bus, caps, config, gitops, ledger, store
from athena_rex.kernel.backend import BackendError

SUMMARY = "save a worker's work, stop it, remove its worktree and session"
POINTERS = ("backup", "wip_backup", "trash", "ignored_kept")
KIN_FORGOTTEN_AFTER = 86400


class RetireError(UserError):
    pass


def _at_a_startup_prompt(record) -> bool:
    return ledger.goal_state(record) == "first"


def _stop_claude(backend, record, grace) -> bool:
    """True when no Claude of this worker is running. Closing the block is the fallback, then SIGKILL of our own pid."""
    name = record["name"]
    if not agents.stat_of(name):
        return True
    if not _at_a_startup_prompt(record):
        try:
            if agents.graceful_exit(backend, record, grace=grace):
                return True
        except BackendError:
            pass
    if record.get("space") and record.get("block"):
        try:
            backend.close_block(agents.place_of(record))
        except BackendError:
            pass
    agents.kill_if_alive(name)
    return not agents.stat_of(name)


def _may_forget(name, path, repo, keep_worktree):
    """Its own record goes with it, now by the prune or within months by `git gc`, with nothing tracking what it holds."""
    why = repo and ((path and gitops.admin_modules_hold(repo, path)) or (not keep_worktree and gitops.prune_refused(repo)))
    if why:
        raise RetireError(f"{name} stays tracked, and nothing was removed: {why}")


def _save_work(name, path, repo, force, keep_worktree=False) -> dict:
    """Keeps a head ref, and with --force a ref of the changes. Refuses, naming it, whatever only this worktree holds. With
    --keep-worktree nothing goes, so only a vanished or broken worktree's own record that holds work refuses."""
    out = {"backup": None, "wip_backup": None, "ignored": []}
    if not (path and os.path.isdir(path)):
        _may_forget(name, path, repo, keep_worktree)
        return {**out, "backup": gitops.keep_detached(repo, name, path) if repo and path else None}
    if not gitops.ok(path, "rev-parse", "--git-dir"):
        if not force:
            raise RetireError(f"git can't read {path} any more, so {name} stays tracked. Look inside; --force moves the "
                              "folder to the trash.")
        _may_forget(name, path, repo, keep_worktree)
        return {**out, "orphan": True, "backup": gitops.keep_detached(repo, name, path) if repo else None}
    head = gitops.git(path, "rev-parse", "HEAD", check=False)
    if head and repo:
        out["backup"] = gitops.backup_head(repo, name, head)
    info = gitops.unsaved(path)
    if info.get("unknown"):
        raise RetireError(f"git cannot say what {path} holds ({info.get('error')}); {name} stays tracked")
    if keep_worktree:
        return out
    if (info["dirty"] or info["ahead"] or info["ignored"]) and not force:
        listed = ", ".join(info["ignored"][:8]) + (", ..." if info["ignored"][8:] else "")
        also = f" and git-ignored: {listed}" if info["ignored"] else ""
        raise RetireError(f"{name} still holds {len(info['dirty'])} uncommitted and {info['ahead']} unpushed{also}; its head is "
                          f"kept at {out['backup']}. Save them, or use --force, which keeps them first.")
    limit = int(config.rex("ignored_copy_max_mb"))
    size = gitops.size_of(path, info["ignored"]) // (1 << 20)
    if size > limit:
        raise RetireError(f"{name} holds {size} MB of git-ignored work, over rex.ignored_copy_max_mb ({limit}), and --force copies it to the "
                          "trash first. Move it, name it in rex.disposable if it can be regenerated, or raise the limit; nothing was changed.")
    out["ignored"] = info["ignored"]
    if head:
        gitops.refuse_nested_repos(path, head)
    if info["dirty"]:
        if not repo:
            raise RetireError(f"{name} has uncommitted files and no repo to save them in; it stays tracked")
        out["wip_backup"] = gitops.backup_wip(path, repo, name)
    return out


def _history(ctx, worker, name):
    try:
        hook = store.read_dict(store.path("workers", f"{name}.json"))
        report = hook.get("report") or hook.get("last_report") or {}
        pr = None
        prs = ctx.cap("prs")
        if prs and worker.get("repo") and worker.get("branch"):
            pr = prs.summary(worker["repo"], worker["branch"])
        if not pr and str(report.get("pr") or "").startswith("https://"):
            pr = {"url": report["pr"]}
        store.append_line(store.path("history.jsonl"), {
            "ts": time.time(), "name": name, "linear": worker.get("linear"), "title": worker.get("title"),
            "repo": worker.get("repo"), "branch": worker.get("branch"), "status": report.get("status"), "pr": pr})
    except Exception as exc:
        print(f"history not recorded: {exc}")


def _note(result, **pointers):
    """On record the moment it exists, so a retire killed next still says where the work went, and a re-run adds to the list."""
    found = {key: value for key, value in pointers.items() if value}
    result["kept"] += [value for value in found.values() if value not in result["kept"]]
    result.update(found)
    ledger.append("update", result["name"], retire_kept=result["kept"], **found)


def _kin_on_record(worker, now) -> tuple:
    """What a first try listed, and when. A re-run long after can't vouch for a pid, so it goes by age as well as by start time."""
    listed = worker.get("retire_kin_at")
    if not worker.get("retire_kin") or not listed or now - float(listed) > KIN_FORGOTTEN_AFTER:
        return {}, None
    return {int(pid): command for pid, command in worker["retire_kin"].items()}, float(listed)


def _stop(ctx, name, worker, record, force, grace, result):
    backend = ctx.backend if ledger.managed(worker) else None
    alive = bool(backend and agents.stat_of(name))
    if backend and not agents.read_pid(name) and worker.get("block") and backend.has_block(agents.place_of(record)):
        backend.close_block(agents.place_of(record))
    if alive and not force and agents.typing(backend, record) and not agents.only_a_startup_prompt(backend, record, _at_a_startup_prompt(worker)):
        raise RetireError(f"someone is typing in {name}'s input box and has not sent it; {name} stays tracked. "
                          "Retry once it is empty, or retire with --force.")
    kin, listed = _kin_on_record(worker, time.time())
    if alive:
        kin, listed = agents.family(name), time.time()
        if kin:
            ledger.append("update", name, retire_kin={str(pid): command for pid, command in kin.items()}, retire_kin_at=listed)
        if not _stop_claude(backend, record, grace):
            raise RetireError(f"{name}'s Claude did not stop; {name} stays tracked")
    if kin:
        result["leftovers"] = agents.stop_leftovers(kin, listed_at=listed)


def _remove(name, worker, path, repo, force, keep_worktree, result):
    if keep_worktree:
        return
    if not (path and os.path.isdir(path)):
        if repo:
            gitops.prune(repo)
        return
    if result.get("orphan"):
        target = store.trash_place(Path(path).name)
        _note(result, trash=str(target))
        store.to_trash(path, target)
        try:
            if repo:
                gitops.prune(repo)
        except gitops.GitError as exc:
            raise gitops.GitError(f"its folder is at {target}: {exc}")
    else:
        # Saved again now that nothing is writing: a Claude mid-turn wrote until the moment it stopped.
        late = _save_work(name, path, repo, force)
        _note(result, backup=late["backup"], wip_backup=late["wip_backup"])
        if late["ignored"]:
            target = store.trash_place(f"{name}-ignored")
            _note(result, ignored_kept=str(target))
            gitops.keep_ignored(path, late["ignored"], target)
        gitops.remove_worktree(repo, path, force=bool(late["wip_backup"]) or (force and gitops.needs_force(path)))
    result["removed"] = True
    ledger.append("update", name, removed=True)


def _close(ctx, name, worker, result):
    if ledger.managed(worker):
        try:
            agents.close(ctx.backend, worker)
        except BackendError as exc:
            if exc.code not in ("not_found", "server_down"):
                result["space_left_open"] = worker["space"]
                result["backend_error"] = str(exc)
    for leftover in (agents.pid_file(name), store.path("run", f"{name}.sh")):
        leftover.unlink(missing_ok=True)


def retire(ctx, name, force=False, keep_worktree=False, grace=8.0) -> dict:
    worker = ledger.get(name)
    if not worker or worker.get("state") not in ledger.RETIRABLE:
        raise RetireError(f"no live, failed or damaged worker named {name}")
    path, repo = worker.get("path"), worker.get("repo")
    if any(w.get("path") == path and w["name"] != name for w in ledger.live()):
        path = None
    record = {**worker, "name": name}
    if worker.get("state") == "live" and worker.get("backend") is None and not force:
        raise RetireError(f"{name} was started by the old athena; retire it there, or use --force to remove only its "
                          "checkout (its Claude and pane stay)")
    try:
        saved = _save_work(name, path, repo, force, keep_worktree)
    except gitops.GitError as exc:
        raise RetireError(f"{name} stays tracked, and nothing was removed: {exc}")
    result = {**saved, "name": name, "removed": bool(worker.get("removed")), "kept": list(worker.get("retire_kept") or [])}
    result.update({key: worker[key] for key in POINTERS if worker.get(key) and not saved.get(key)})
    done = False
    try:
        _note(result, backup=saved["backup"], wip_backup=saved["wip_backup"])
        ledger.append("update", name, retiring=time.time())
        _stop(ctx, name, worker, record, force, grace, result)
        _remove(name, worker, path, repo, force, keep_worktree, result)
        _close(ctx, name, worker, result)
        _history(ctx, worker, name)
        result["kept"] = [kept for kept in result["kept"] if kept.startswith("refs/") or os.path.exists(kept)]
        for key in ("trash", "ignored_kept"):  # on record before the move, so a kill between the two leaves a name for nothing
            if result.get(key) not in result["kept"]:
                result.pop(key, None)
        ledger.append("retire", name, removed=result["removed"], cleanup_error=result.get("backend_error"), retiring=None,
                      retire_kept=result["kept"], **{key: result.get(key) for key in POINTERS})
        bus.emit("worker.retired", name=name)
        done = True
    except agents.Unknown as exc:
        raise RetireError(f"cannot tell whether {name}'s Claude is running ({exc}); {name} stays tracked and nothing was removed")
    except (gitops.GitError, OSError) as exc:
        ledger.append("update", name, cleanup_error=str(exc))
        raise RetireError(f"{name} stays tracked: {exc}")
    except BackendError as exc:
        raise RetireError(f"the backend did not answer ({exc}); {name} stays tracked")
    finally:
        if not done:
            ledger.append("update", name, retiring=None)
    return result


def _configure(p):
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="go past unsaved work (saved under a ref first) and a typed box")
    p.add_argument("--keep-worktree", action="store_true")
    p.add_argument("--json", action="store_true")


def _run(args):
    out = retire(caps.CTX, args.name, args.force, args.keep_worktree)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        if out.get("ignored_kept"):
            print(f"its git-ignored work is at {out['ignored_kept']}")
        if out.get("wip_backup"):
            print(f"{args.name} retired; its unsaved work is at {out['wip_backup']}")
        else:
            print(f"{args.name} retired" + (f"; head kept at {out['backup']}" if out.get("backup") else ""))
        earlier = [kept for kept in out["kept"] if kept not in (out.get("backup"), out.get("wip_backup"), out.get("ignored_kept"))]
        if earlier:
            print(f"also kept: {', '.join(earlier)}")
        if out.get("leftovers"):
            print(f"stopped what it left running: {', '.join(out['leftovers'])}")
        if out.get("space_left_open"):
            print(f"its session {out['space_left_open']} did not close ({out['backend_error']}): close it by hand")
    return 0


def commands():
    return {"retire": caps.Command(SUMMARY, _configure, _run)}
