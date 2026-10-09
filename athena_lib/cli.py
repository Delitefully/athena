"""athena: plumbing for the chief of staff. Run `athena -h`."""
import argparse
import json
import re
import subprocess
import time
import sys
from pathlib import Path

from athena_lib import board, dash, gitops, herdr, hq, ledger, pr, retire, spawn, status, watch

PENDING = 3  # spawn: the worker is live, but its Claude waits at a startup prompt and its goal is not sent yet


def _print(data):
    print(json.dumps(data, indent=2, default=str))


def cmd_preflight(a):
    result = gitops.preflight(Path(a.repo).expanduser(), pull=not a.no_pull)
    _print(result)
    return 0 if result["ok"] else 1


def cmd_spawn(a):
    goal = Path(a.goal_file).read_text() if a.goal_file else a.goal
    result = spawn.spawn(a.repo, a.branch, a.name, a.brief, goal, linear=a.linear, title=a.title, model=a.model,
                         effort=a.effort, force=a.force, setup=not a.no_setup, ignore_quota=a.ignore_quota,
                         ultracode=a.ultracode, workflow_size=a.workflow_size)
    _print({k: v for k, v in result.items() if k != "screen"})
    code = 0
    if result.get("pending"):
        print(result["message"], file=sys.stderr)
        code = PENDING
    if board.running_pid():
        return code
    try:
        board.sync()
    except Exception as exc:  # the board is optional; HQ may not exist yet
        print(f"board not updated: {exc}", file=sys.stderr)
    return code


def cmd_status(a):
    rows = [status.collect(w, with_pr=a.pr) for w in ledger.live()]
    rows.sort(key=lambda r: (r["urgency"], r["name"]))
    left = ledger.leftovers()
    if a.json:
        _print(rows)
        return 0
    for w in left:
        print(f"{w['name']:<12}  failed    space not removed ({w.get('cleanup_error') or 'unknown'}): athena retire {w['name']}")
    if not rows:
        print("no live workers")
        return 0
    for r in rows:
        git = r.get("git") or {}
        bits = [f"{r['name']:<12}", f"{r['state']:<8}", f"herdr={r.get('herdr') or '-':<8}",
                f"ahead={git.get('ahead', '-')}", f"dirty={len(git.get('dirty') or [])}"]
        if r.get("ultracode"):
            bits.append("UC" + (f":{r['workflow_size']}" if r.get("workflow_size") else ""))
        if r.get("pr"):
            p = r["pr"]
            bits.append(f"pr#{p['number']} {p['checks']} {p['review'] or 'no-review'}")
        report = r.get("report") or {}
        if report:
            bits.append(f"report={report.get('status')}")
        if r.get("pending"):
            bits.append("goal pending" + (f" ({r['detail']})" if r.get("detail") else ""))
        elif (r.get("prompt") or "").startswith(("sending", "unconfirmed")):
            bits.append(f"goal {r['prompt']} (check its pane; do not resend blindly)")
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


def _dash_line(rec, note=""):
    hq = f", lives while HQ pid {rec['hq_pid']} does" if rec.get("hq_pid") else ", no HQ found: it runs until `athena dash off`"
    return f"dash {rec['url']} pid {rec['pid']}{hq}{note}"


def cmd_dash(a):
    if a.action in ("on", "open"):
        rec = dash.start()
        print(_dash_line(rec, "" if rec["started"] else " (already running)"))
        if a.action == "open":
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", rec["url"]], check=False)
    elif a.action == "off":
        rec = dash.stop()
        print(f"dash off (stopped pid {rec['pid']})" if rec else "dash off")
    else:
        rec = dash.running()
        print(_dash_line(rec) if rec else "dash off")
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
    live = {w["name"]: w for w in ledger.live()}
    if a.name not in live:
        print(f"{a.name} is not a live athena worker", file=sys.stderr)
        return 2
    if live[a.name].get("goal_pending"):
        print(f"{a.name} has not received its goal yet and may be at a startup prompt; the human answers that in its "
              f"space, then: athena resume {a.name}", file=sys.stderr)
        return 2
    text = " ".join(a.text)
    _print(herdr.call("agent", "prompt", a.name, text))
    if re.fullmatch(r"/\S+", text):
        # A bare slash command leaves Claude Code's command menu open, and the prompt's Enter only picks the
        # suggestion. One more Enter submits it; on an already submitted command it lands on an empty prompt.
        time.sleep(0.5)
        herdr.call("agent", "send-keys", a.name, "enter")
    return 0


def cmd_resume(a):
    _print(spawn.resume(a.name))
    return 0


def cmd_retire(a):
    result = retire.retire(a.name, force=a.force, keep_worktree=a.keep_worktree)
    _print(result)
    if result.get("space_left_open"):
        print(f"{a.name} is retired, but herdr did not remove space {result['space_left_open']} "
              f"({result['herdr_error']}): close it by hand", file=sys.stderr)
    if board.running_pid():
        return 0
    try:
        board.sync(force=True)
    except Exception:
        pass
    return 0


def cmd_hq(a):
    if a.action == "restart":
        if a.child:
            _print(hq.restart_now(update=not a.no_update))
        else:
            print(hq.restart(update=not a.no_update)["message"])
        return 0
    _print(hq.hq(cwd=a.cwd, focus=not a.no_focus, start_board=not a.no_board, start_dash=not a.no_dash))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="athena", description="Plumbing for the chief of staff.")
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
    s.add_argument("--ultracode", action="store_true", help="turn ultracode on in the worker's settings (only when the human asks)")
    s.add_argument("--workflow-size", choices=["small", "medium", "large", "unrestricted"],
                   help="the worker's workflow size guideline; needs --ultracode")
    s.set_defaults(func=cmd_spawn)

    s = sub.add_parser("status", help="merged status of live workers")
    s.add_argument("--json", action="store_true")
    s.add_argument("--pr", action="store_true", help="include PR state from gh (slower)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("board", help="worker columns beside HQ")
    s.add_argument("action", choices=["sync", "watch", "on", "off", "toggle", "show"], nargs="?", default="show")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("dash", help="the dashboard on 127.0.0.1: on, off, show, open")
    s.add_argument("action", choices=["on", "off", "show", "open"], nargs="?", default="show")
    s.set_defaults(func=cmd_dash)

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

    s = sub.add_parser("resume", help="send the goal of a worker that waited at a startup prompt")
    s.add_argument("name")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("retire", help="back up, stop and remove a worker")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    s.add_argument("--keep-worktree", action="store_true")
    s.set_defaults(func=cmd_retire)

    s = sub.add_parser("hq", help="create or focus the chief of staff's space; `restart` restarts its Claude in place")
    s.add_argument("action", choices=["restart"], nargs="?",
                   help="update Claude Code and plugins, exit HQ's Claude and resume its conversation in the same pane")
    s.add_argument("--no-update", action="store_true", help="restart: skip claude update and the plugin updates")
    s.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--cwd")
    s.add_argument("--no-focus", action="store_true")
    s.add_argument("--no-board", action="store_true")
    s.add_argument("--no-dash", action="store_true")
    s.set_defaults(func=cmd_hq)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.cmd == "spawn" and args.workflow_size and not args.ultracode:
        p.error("--workflow-size needs --ultracode")
    try:
        return args.func(args)
    except (spawn.SpawnError, retire.RetireError, dash.DashError, hq.RestartError, herdr.HerdrError, gitops.GitError, RuntimeError, OSError) as exc:
        print(f"athena {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
