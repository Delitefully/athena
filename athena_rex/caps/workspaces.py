"""Every worktree and scratch space: a known owner, a lifecycle, one list, and a cleanup that cannot lose work. docs/workspaces.md has the design.

State is computed from facts on every read, never stored, so it cannot drift:

    active -> crashed (owner live, its Claude gone: kept)
    active -> retired -> removable after the grace (or held, if it holds unsaved work)
    no owner -> orphan -> removable after the grace (or held)
    scratch  -> expired past its ttl
"""
import json
import os
import subprocess
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, config, gitops, lab, ledger, store

SUMMARY = "every worktree and scratch space: owner, size, state; safe cleanup"
GRACE_DAYS = 7
MARKER = ".athena-scratch.json"
# What the state dir is supposed to hold. Any other directory there is a leftover someone meant to clean up.
STATE_DIRS = {"workers", "briefs", "settings", "logs", "locks", "claims", "run", "scratch", "trash", "previews", "repos",
              "backups", "bin", "secrets", "scratch-roots"}


def encode_cwd(path: str) -> str:
    """How Claude names a project's scratch and transcript folders."""
    return "".join(c if c.isalnum() else "-" for c in path)


def classify(facts: dict, grace_days=GRACE_DAYS, now=None) -> dict:
    """The lifecycle as a pure function of facts: {state, removable, why}. Unknown always keeps."""
    now = time.time() if now is None else now
    age_days = (now - float(facts.get("mtime") or now)) / 86400
    owner = facts.get("owner_state")
    unsaved = bool(facts.get("unsaved"))
    if facts.get("in_use"):
        return {"state": "active", "removable": False, "why": "a running Claude has it open"}
    if owner == "live":
        if facts.get("owner_running") is False:
            return {"state": "crashed", "removable": False, "why": "its worker is live and its Claude is gone: resume or retire it"}
        return {"state": "active", "removable": False, "why": "its worker is live"}
    if facts.get("kind") == "scratch" and facts.get("ttl_days") is not None and age_days < float(facts["ttl_days"]):
        return {"state": "active", "removable": False, "why": f"inside its {facts['ttl_days']}-day ttl"}
    state = {"retired": "retired", "failed": "retired"}.get(owner, "expired" if facts.get("kind") == "scratch" else "orphan")
    if unsaved:
        return {"state": "held", "removable": False, "why": "holds work found nowhere else: " + facts.get("unsaved_detail", "unsaved")}
    if facts.get("kind") in ("worktree", "claude-worktree") and facts.get("git_known") is False:
        return {"state": "held", "removable": False, "why": "git cannot say what it holds"}
    if age_days < grace_days:
        return {"state": state, "removable": False, "why": f"younger than the {grace_days}-day grace"}
    if facts.get("kind") == "state":
        return {"state": state, "removable": False, "why": "in the state dir: only the human removes these"}
    return {"state": state, "removable": True, "why": "nothing unsaved, past the grace"}


def _in_use() -> list:
    root = agents.claude_dir() / "sessions"
    paths = []
    try:
        files = list(root.glob("*.json"))
    except OSError:
        return paths
    for f in files:
        entry = store.read_dict(f)
        pid = entry.get("pid")
        if entry.get("cwd") and isinstance(pid, int):
            try:
                os.kill(pid, 0)
                paths.append(os.path.realpath(entry["cwd"]))
            except PermissionError:
                paths.append(os.path.realpath(entry["cwd"]))
            except OSError:
                pass
    return paths


def _size_kb(path) -> int:
    try:
        out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=120).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return -1


def _mtime(path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _newest(path, depth=4) -> float:
    """A scratchpad's folder keeps its old mtime while a resumed session writes deep inside it."""
    newest = _mtime(path)
    if depth:
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        newest = max(newest, _newest(entry.path, depth - 1))
                    else:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            pass
    return newest


class Scan:
    def __init__(self, workers=None, worktree_root=None, projects=None, tmp_root=None, state=None):
        self.workers = workers if workers is not None else ledger.workers()
        self.worktree_root = Path(worktree_root) if worktree_root else config.path("worktree_root")
        self.projects = Path(projects) if projects else config.path("projects_dir")
        self.tmp_root = Path(tmp_root or config.rex("scratchpads"))
        self.state = Path(state) if state else store.state_dir()
        self.by_path = {os.path.realpath(w["path"]): w for w in self.workers.values() if w.get("path")}
        self.by_encoded = {encode_cwd(w["path"]): w for w in self.workers.values() if w.get("path")}
        self.in_use = _in_use()
        managed = [w["name"] for w in self.workers.values() if ledger.managed(w) and w.get("state") == "live"]
        found = agents.running(managed) if managed and not state else {}
        self.running = {name: name in found for name in managed} if not state else {}

    def _owner(self, worker):
        if not worker:
            return {"owner": None, "owner_state": None, "owner_running": None}
        return {"owner": worker.get("name"), "owner_state": worker.get("state"),
                "owner_running": self.running.get(worker.get("name"))}

    def _git_facts(self, path) -> dict:
        info = gitops.unsaved(path)
        detail = []
        if info.get("dirty"):
            detail.append(f"{len(info['dirty'])} uncommitted")
        if info.get("ahead"):
            detail.append(f"{info['ahead']} unpushed")
        if info.get("unknown"):
            detail.append("git cannot say")
        if info.get("modules"):
            detail.append(f"a submodule repository holding {info['modules']}")
        if info.get("ignored"):
            shown = info["ignored"][:3]
            detail.append(f"{len(info['ignored'])} git-ignored: " + ", ".join(shown) + (", ..." if info["ignored"][3:] else ""))
        return {"branch": info.get("branch"), "git_known": not info.get("unknown"), "unsaved": gitops.holds_work(info),
                "unsaved_detail": ", ".join(detail)}

    def worktrees(self) -> list:
        out = []
        try:
            repos = sorted(p for p in self.worktree_root.iterdir() if p.is_dir())
        except OSError:
            return out
        for repo in repos:
            for path in sorted(p for p in repo.iterdir() if p.is_dir()):
                worker = self.by_path.get(os.path.realpath(path))
                out.append({"kind": "worktree", "path": str(path), "repo": repo.name, **self._owner(worker),
                            **self._git_facts(path)})
        return out

    def claude_worktrees(self) -> list:
        out = []
        try:
            repos = sorted(p for p in self.projects.iterdir() if p.is_dir())
        except OSError:
            return out
        for repo in repos:
            root = repo / ".claude" / "worktrees"
            if not root.is_dir():
                continue
            for path in sorted(p for p in root.iterdir() if p.is_dir()):
                out.append({"kind": "claude-worktree", "path": str(path), "repo": repo.name, **self._owner(None),
                            **self._git_facts(path)})
        return out

    def scratchpads(self) -> list:
        out = []
        try:
            folders = sorted(p for p in self.tmp_root.iterdir() if p.is_dir())
        except OSError:
            return out
        for path in folders:
            worker = self.by_encoded.get(path.name)
            out.append({"kind": "scratchpad", "path": str(path), **self._owner(worker)})
        return out

    def scratch(self) -> list:
        out = []
        root = self.state / "scratch"
        try:
            owners = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            return out
        for owner_dir in owners:
            for path in sorted(p for p in owner_dir.iterdir() if p.is_dir()):
                marker = store.read_dict(path / MARKER)
                worker = self.workers.get(marker.get("owner") or owner_dir.name)
                facts = {"kind": "scratch", "path": str(path), **self._owner(worker),
                         "owner": marker.get("owner") or owner_dir.name, "ttl_days": marker.get("ttl_days"),
                         "note": marker.get("note")}
                out.append(facts)
        return out

    def leftovers(self) -> list:
        out = []
        try:
            entries = sorted(p for p in self.state.iterdir() if p.is_dir())
        except OSError:
            return out
        for path in entries:
            if path.name not in STATE_DIRS:
                out.append({"kind": "state", "path": str(path), **self._owner(None)})
        return out

    def all(self) -> list:
        return self.worktrees() + self.claude_worktrees() + self.scratch() + self.scratchpads() + self.leftovers()


def listing(scan=None, sizes=True, grace_days=GRACE_DAYS, now=None) -> list:
    scan = scan or Scan()
    rows = []
    for facts in scan.all():
        path = facts["path"]
        facts["mtime"] = _newest(path) if facts["kind"] == "scratchpad" else _mtime(path)
        facts["in_use"] = _within_any(path, scan.in_use) or (
            facts["kind"] == "scratchpad" and Path(path).name in {encode_cwd(u) for u in scan.in_use})
        rows.append({**facts, **classify(facts, grace_days, now), "size_kb": _size_kb(path) if sizes else None})
    return rows


def _within_any(path, used) -> bool:
    return any(lab.inside(u, [path]) for u in used)


def _may_touch(path) -> bool:
    """In the lab, cleanup acts only inside a marked scratch copy or a marked isolated HOME."""
    if not lab.active():
        return True
    roots = [place for place in (store.state_dir(), os.environ.get("HOME")) if lab.scratch_place(place)]
    return bool(roots) and lab.inside(path, roots)


def clean(apply=False, grace_days=GRACE_DAYS, now=None, scan=None) -> list:
    """What it would remove, or with `apply` what it did. Checks each one again at the moment of removal."""
    out = []
    scan = scan or Scan()
    for row in listing(scan, sizes=False, grace_days=grace_days, now=now):
        if not row["removable"]:
            continue
        entry = {"path": row["path"], "kind": row["kind"], "owner": row.get("owner"), "why": row["why"], "action": "would remove"}
        if apply:
            if not _may_touch(row["path"]):
                entry["action"] = "refused: off limits in the lab"
            else:
                entry["action"] = _remove(row)
        out.append(entry)
    return out


def _remove(row) -> str:
    path = row["path"]
    if row["kind"] in ("worktree", "claude-worktree"):
        info = gitops.unsaved(path)
        if gitops.holds_work(info):
            return "kept: it holds work now"
        common = gitops.main_checkout(path)
        head = gitops.git(path, "rev-parse", "HEAD", check=False)
        if head:
            gitops.backup_head(common, row.get("owner") or Path(path).name, head)
        try:
            gitops.remove_worktree(common, path)
        except gitops.GitError as exc:
            return f"kept: {exc}"
        return "removed; head kept under refs/athena-backup"
    return f"moved to {store.to_trash(path)}"


def new_scratch(name, owner, ttl_days=None, note=None) -> Path:
    if not store.valid_name(name) or not store.valid_name(owner):
        raise UserError("scratch names and owners are lowercase letters, digits, - and _")
    path = store.ensure("scratch", owner) / name
    path.mkdir(exist_ok=True)
    days = int(ttl_days if ttl_days is not None else config.rex("scratch_ttl_days"))
    store.write_json(path / MARKER, {"owner": owner, "created": time.time(), "ttl_days": days, "note": note})
    return path


def _human(kb) -> str:
    if kb is None or kb < 0:
        return "-"
    return f"{kb / (1 << 20):.1f}G" if kb >= 1 << 20 else f"{kb / 1024:.0f}M" if kb >= 1024 else f"{kb}K"


def _ls_cfg(p):
    p.add_argument("action", nargs="?", choices=["ls", "clean"], default="ls")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-size", action="store_true")
    p.add_argument("--apply", action="store_true", help="clean: really remove; without it, only say what it would")
    p.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    p.add_argument("--kind")
    p.add_argument("--worktree-root", help="read-only survey: look here instead of the configured root")
    p.add_argument("--projects")
    p.add_argument("--tmp-root")
    p.add_argument("--state-from", help="read-only survey: read a ledger and leftovers from this state dir")


def _ls(args):
    workers = None
    survey = any((args.worktree_root, args.projects, args.tmp_root, args.state_from))
    if args.state_from:
        saved = os.environ.get("ATHENA_STATE_DIR")
        workers = _workers_from(args.state_from)
        if saved:
            os.environ["ATHENA_STATE_DIR"] = saved
    scan = Scan(workers, args.worktree_root, args.projects, args.tmp_root, args.state_from)
    if args.action == "clean":
        if survey and args.apply:
            raise UserError("a survey only reads: --apply needs the configured roots")
        rows = clean(args.apply, args.grace_days, scan=scan)
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print(f"{r['action']:<16} {r['kind']:<16} {r['owner'] or '-':<18} {r['path']}")
            if not rows:
                print("nothing to clean")
        return 0
    rows = listing(scan, not args.no_size, args.grace_days)
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
    else:
        for r in sorted(rows, key=lambda x: (x["kind"], x["state"], -(x.get("size_kb") or 0))):
            print(f"{r['kind']:<16} {r['state']:<8} {_human(r.get('size_kb')):>7}  {r.get('owner') or '-':<20} {r['path']}"
                  + (f"  [{r['why']}]" if r["state"] in ("held", "crashed") else ""))
    return 0


def _workers_from(state_dir) -> dict:
    return ledger.fold(store.read_lines(Path(state_dir) / "ledger.jsonl"))


def _scratch_cfg(p):
    p.add_argument("action", choices=["new"])
    p.add_argument("name")
    p.add_argument("--owner", default=os.environ.get("ATHENA_WORKER") or "hq")
    p.add_argument("--ttl-days", type=int)
    p.add_argument("--note")


def _scratch(args):
    print(new_scratch(args.name, args.owner, args.ttl_days, args.note))
    return 0


def checks(ctx):
    """From the ledger and one ps, never git: the full listing's git look at every worktree made a doctor pass take minutes. Crashed as
    classify has it: a live worker of ours whose Claude is gone, holding a worktree that is there and no running Claude has open."""
    workers = [w for w in ledger.live() if ledger.managed(w) and w.get("path") and os.path.isdir(w["path"])]
    running = agents.running([w["name"] for w in workers]) if workers else {}
    used = _in_use() if workers else []
    crashed = [w for w in workers if w["name"] not in running and not _within_any(w["path"], used)]
    return [caps.Check("workspaces", True, f"{len(crashed)} crashed workers hold worktrees" if crashed else "no strays")]


def commands():
    return {"workspaces": caps.Command(SUMMARY, _ls_cfg, _ls),
            "scratch": caps.Command("a scratch space with an owner and a ttl", _scratch_cfg, _scratch)}
