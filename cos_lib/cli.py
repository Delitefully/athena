"""cos: plumbing for the chief of staff. Run `cos -h`."""
import argparse
import json
import sys
from pathlib import Path

from cos_lib import board, gitops, herdr, hq, ledger, pr, retire, spawn, status, watch


def _print(data):
    print(json.dumps(data, indent=2, default=str))


def cmd_preflight(a):
    result = gitops.preflight(Path(a.repo).expanduser(), pull=not a.no_pull)
    _print(result)
    return 0 if result["ok"] else 1


def cmd_spawn(a):
    goal = Path(a.goal_file).read_text() if a.goal_file else a.goal
    result = spawn.spawn(a.repo, a.branch, a.name, a.brief, goal, linear=a.linear, title=a.title, model=a.model,
                         effort=a.effort, force=a.force, setup=not a.no_setup, ignore_quota=a.ignore_quota)
    _print(result)
    if board.running_pid():
        return 0
    try:
        board.sync()
    except Exception as exc:  # the board is optional; HQ may not exist yet
        print(f"board not updated: {exc}", file=sys.stderr)
    return 0


def cmd_status(a):
    rows = [status.collect(w, with_pr=a.pr) for w in ledger.live()]
    rows.sort(key=lambda r: (r["urgency"], r["name"]))
    if a.json:
        _print(rows)
        return 0
    if not rows:
        print("no live workers")
        return 0
    for r in rows:
        git = r.get("git") or {}
        bits = [f"{r['name']:<12}", f"{r['state']:<8}", f"herdr={r.get('herdr') or '-':<8}",
                f"ahead={git.get('ahead', '-')}", f"dirty={len(git.get('dirty') or [])}"]
        if r.get("pr"):
            p = r["pr"]
            bits.append(f"pr#{p['number']} {p['checks']} {p['review'] or 'no-review'}")
        report = r.get("report") or {}
        if report:
            bits.append(f"report={report.get('status')}")
        print("  ".join(bits))
        if r.get("summary"):
            print("    " + r["summary"].replace("\n", " ")[-160:])
    return 0


def cmd_board(a):
    if a.action == "sync":
        _print(board.sync(force=a.force))
    elif a.action == "watch":
        board.watch()
    elif a.action == "on":
        print(f"board watcher pid {board.start_watcher()}")
        _print(board.sync(force=True))
    elif a.action == "off":
        board.stop_watcher()
        board.clear()
        print("board off")
    elif a.action == "toggle":
        if board.running_pid():
            board.stop_watcher()
            board.clear()
            print("board off")
        else:
            print(f"board watcher pid {board.start_watcher()}")
            board.sync(force=True)
    else:
        _print(board.load())
    return 0


def cmd_watch(a):
    watch.run(interval=a.interval)
    return 0


def cmd_pr(a):
    w = ledger.workers().get(a.name)
    if not w:
        print(f"no worker named {a.name}", file=sys.stderr)
        return 2
    _print(pr.summary(w["repo"], w["branch"]))
    return 0


def cmd_nudge(a):
    if a.name not in {w["name"] for w in ledger.live()}:
        print(f"{a.name} is not a live cos worker", file=sys.stderr)
        return 2
    _print(herdr.call("agent", "prompt", a.name, " ".join(a.text)))
    return 0


def cmd_retire(a):
    _print(retire.retire(a.name, force=a.force, keep_worktree=a.keep_worktree))
    if board.running_pid():
        return 0
    try:
        board.sync(force=True)
    except Exception:
        pass
    return 0


def cmd_hq(a):
    _print(hq.hq(cwd=a.cwd, focus=not a.no_focus, start_board=not a.no_board))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="cos", description="Plumbing for the chief of staff.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("preflight", help="check a main checkout is clean and on main, and fast-forward it")
    s.add_argument("repo")
    s.add_argument("--no-pull", action="store_true")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("spawn", help="launch a worker in a new worktree space")
    s.add_argument("--repo", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--brief", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--goal")
    g.add_argument("--goal-file")
    s.add_argument("--linear")
    s.add_argument("--title")
    s.add_argument("--model", default="opus")
    s.add_argument("--effort", default="high")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-setup", action="store_true")
    s.add_argument("--ignore-quota", action="store_true", help="launch even above the 5-hour quota gate")
    s.set_defaults(func=cmd_spawn)

    s = sub.add_parser("status", help="merged status of live workers")
    s.add_argument("--json", action="store_true")
    s.add_argument("--pr", action="store_true", help="include PR state from gh (slower)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("board", help="worker columns beside HQ")
    s.add_argument("action", choices=["sync", "watch", "on", "off", "toggle", "show"], nargs="?", default="show")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("watch", help="print one line per change (run under Monitor)")
    s.add_argument("--interval", type=float, default=20)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("pr", help="PR summary for a worker")
    s.add_argument("name")
    s.set_defaults(func=cmd_pr)

    s = sub.add_parser("nudge", help="type a prompt or slash command into a worker (use SendMessage for messages)")
    s.add_argument("name")
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_nudge)

    s = sub.add_parser("retire", help="back up, stop and remove a worker")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    s.add_argument("--keep-worktree", action="store_true")
    s.set_defaults(func=cmd_retire)

    s = sub.add_parser("hq", help="create or focus the chief of staff's space")
    s.add_argument("--cwd")
    s.add_argument("--no-focus", action="store_true")
    s.add_argument("--no-board", action="store_true")
    s.set_defaults(func=cmd_hq)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (spawn.SpawnError, retire.RetireError, herdr.HerdrError, gitops.GitError, RuntimeError, OSError) as exc:
        print(f"cos {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
