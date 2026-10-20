"""athena: plumbing for the chief of staff. Run `athena -h`."""
import argparse
import json
import os
import re
import subprocess
import time
import sys
from pathlib import Path

from athena_lib import asks, board, config, dash, gitops, handoff, herdr, hq, init, launch, ledger, pr, retire, spawn, status, watch

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
                         effort=a.effort, force=a.force, setup=not a.no_setup, ultracode=a.ultracode,
                         workflow_size=a.workflow_size)
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


def _context_hint():
    """One line when HQ's context is filling up; never a reason for `athena status` to fail."""
    try:
        ctx = handoff.hq_context()
    except Exception:
        return
    if ctx and ctx["percent"] >= handoff.HINT_PERCENT:
        print(f"{'hq':<12}  context {handoff.describe(ctx)}: rotate soon (athena hq rotate, skill §1a)")


def cmd_status(a):
    rows = [status.collect(w, with_pr=a.pr) for w in ledger.live()]
    rows.sort(key=lambda r: (r["urgency"], r["name"]))
    left = ledger.leftovers()
    if a.json:
        _print(rows)
        return 0
    _context_hint()
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
        verdict = r.get("athena_review") or {}
        if verdict.get("verdict"):
            stale = r.get("pr") and not pr.same_head(verdict.get("head"), r["pr"].get("head"))
            bits.append(f"review={verdict['verdict']}@{str(verdict.get('head') or '-')[:7]}" + (" (stale)" if stale else ""))
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
    # HQ's settings set FORCE_HYPERLINK for Claude Code's own drawing; the gh, git and herdr the watch runs get none.
    os.environ.pop("FORCE_HYPERLINK", None)
    if a.slack_once:
        return watch.slack_once(window=a.window)
    watch.run(interval=a.interval, tagged=a.tagged)
    return 0


def _read_text(path):
    """A file's text, or stdin's for `-`."""
    return sys.stdin.read() if path == "-" else Path(os.path.expanduser(path)).read_text()


def cmd_ask(a):
    if a.action == "add":
        if not a.args or len(a.args) > 1:
            print('athena ask add "<one-sentence title>" [--why ...] [--pr ...] [--body ... | --body-file <path|->] '
                  '[--option label[:icon[:do]]] ... [--preview <file|dir>] ...', file=sys.stderr)
            return 2
        kind = "note" if a.note else "question" if a.question else None
        body = _read_text(a.body_file) if a.body_file else a.body or ""
        print(asks.add(a.args[0], kind=kind, why=a.why, pr=a.pr, options=a.option or [], body=body, previews=a.preview or []))
    elif a.action == "body":
        if len(a.args) != 1 or not a.file:
            print("athena ask body <id> --file <path|->  (an empty file clears it)", file=sys.stderr)
            return 2
        _print(asks.set_body(a.args[0], _read_text(a.file)))
    elif a.action == "done":
        if len(a.args) != 1:
            print("athena ask done <id>", file=sys.stderr)
            return 2
        _print(asks.done(a.args[0], keep=a.keep))
    elif a.action == "queue":
        _print(asks.queue())
    elif a.args:
        item = asks.show(a.args[0])
        if not item:
            print(f"no ask {a.args[0]}", file=sys.stderr)
            return 2
        _print(item)
    else:
        _print(asks.items())
    return 0


def cmd_stack(a):
    if a.action == "set":
        if not a.repo or not a.prs or not watch.is_slug(a.repo):
            print("athena stack set <owner/repo> <pr> [<pr> ...]", file=sys.stderr)
            return 2
        watch.set_stack(a.repo, a.prs)
    elif a.action == "clear":
        if not a.repo:
            print("athena stack clear <owner/repo>", file=sys.stderr)
            return 2
        watch.set_stack(a.repo, [])
    _print(watch.stacks())
    return 0


def cmd_pr(a):
    w = ledger.workers().get(a.name)
    if not w:
        print(f"no worker named {a.name}", file=sys.stderr)
        return 2
    _print(pr.summary(w["repo"], w["branch"]))
    return 0


def cmd_review(a):
    w = {w["name"]: w for w in ledger.live()}.get(a.name)
    if not w:
        print(f"{a.name} is not a live athena worker", file=sys.stderr)
        return 2
    head = a.head
    if not head and a.verdict == "start" and w.get("repo") and w.get("branch"):
        head = (pr.summary(w["repo"], w["branch"]) or {}).get("head")
    elif not head:  # a verdict is for the head the reviewer read, never one pushed during the review
        started = w.get("review") or {}
        head = started.get("head") if started.get("verdict") == "start" else None
        if not head:
            print(f"athena review: no head recorded at start: run athena review {a.name} start, or pass --head <sha>",
                  file=sys.stderr)
            return 2
    if not head or not re.fullmatch(r"[0-9a-fA-F]{7,40}", head):
        print(f"athena review: no head to record ({head or 'gh found no PR'}): pass --head <sha>", file=sys.stderr)
        return 2
    _print(ledger.review(a.name, a.verdict, head.lower()))
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


def _migrate():
    """An install from before config.json: write down the values it runs on, once."""
    if not config.is_legacy():
        return
    target = config.write(init.starting_values())
    print(f"athena: wrote {target} from your current setup (athena config show)", file=sys.stderr)


def _first_run():
    """No config.json yet: a new user runs athena init. An older install was migrated in main, or, if writing its
    config failed there, runs on its current values."""
    if config.exists() or config.is_legacy():
        return None
    if not sys.stdin.isatty():
        print("athena: no config yet: run `athena init` first", file=sys.stderr)
        return 2
    print("athena: first run, so athena init comes first\n")
    code = init.main([])
    return code or None


def cmd_hq(a):
    if a.action == "rotate":
        if a.child:
            _print(hq.rotate_now())
            return 0
        threads = _read_text(a.threads) if a.threads else None
        result = hq.rotate(threads=threads, dry_run=a.dry_run, if_context_over=a.if_context_over)
        if a.dry_run and "handoff" in result:
            print(result["handoff"], end="")
            print(f"\n--- launch (not run)\n{result['command']}\n\n--- first prompt\n{result['prompt']}")
        else:
            print(result["message"])
        return 0
    if a.action == "restart":
        if a.child:
            _print(hq.restart_now(update=not a.no_update))
        else:
            print(hq.restart(update=not a.no_update)["message"])
        return 0
    stop = _first_run()
    if stop is not None:
        return stop
    attach = not a.no_attach and launch.can_attach()
    if attach and not launch.server_running():
        print("athena: starting herdr")
        launch.start_server()
    result = hq.hq(cwd=a.cwd, focus=not a.no_focus, start_board=not a.no_board, start_dash=not a.no_dash)
    if not attach:
        _print(result)
        return 0
    print(f"athena: HQ is {'open' if result.get('created') else 'ready'} in space {result.get('workspace')}; attaching to herdr")
    launch.attach()
    return 0


def cmd_config(a):
    if a.action == "path":
        print(config.config_file())
    else:
        _print(config.show())
    return 0


def parser():
    p = argparse.ArgumentParser(prog="athena", description="Plumbing for the chief of staff. A bare `athena` opens HQ.")
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
    s.add_argument("--model", help="default: config `model` (athena config show)")
    s.add_argument("--effort", help="default: config `effort`")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-setup", action="store_true")
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

    s = sub.add_parser("watch", help="print one line per change (the athena-watch mod runs it in HQ; Monitor is the fallback)")
    s.add_argument("--interval", type=float, default=20)
    s.add_argument("--tagged", action="store_true", help="JSON lines for the mod: each change marked wake or not, and the status line")
    s.add_argument("--slack-once", action="store_true",
                   help="poll the Slack doorbell once and print its slack-new lines (ids and counts), then exit")
    s.add_argument("--window", type=float, default=0,
                   help="with --slack-once: a conversation seen for the first time counts back this many seconds")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("ask", help="athena's asks on the dashboard (needs-you.json) and the human's answers: add, body, done, show, queue")
    s.add_argument("action", choices=["add", "body", "done", "show", "queue"], nargs="?", default="show")
    s.add_argument("args", nargs="*", help="add: the title, one sentence; body, done, show: the id (show alone lists every ask)")
    k = s.add_mutually_exclusive_group()
    k.add_argument("--question", action="store_true", help="add: Discuss in Athena and Reply… on the dashboard (default when it has a ?)")
    k.add_argument("--note", action="store_true", help="add: only its options, or × with none")
    s.add_argument("--why", default="", help="add: one sentence, shown under it")
    s.add_argument("--pr", help="add: owner/name#n or a PR URL: shown under that PR's row")
    b = s.add_mutually_exclusive_group()
    b.add_argument("--body", help=f"add: Markdown the dashboard expands under it, up to {asks.BODY_MAX} characters")
    b.add_argument("--body-file", metavar="PATH", help="add: the body from a file, or - for stdin")
    s.add_argument("--file", metavar="PATH", help="body: the new body from a file, or - for stdin; empty clears it")
    s.add_argument("--option", action="append", metavar="LABEL[:ICON[:DO]]",
                   help=f"add: at most {asks.OPTIONS_MAX}, {asks.OPTIONS_MAX_BODY} with a body; label up to {asks.LABEL_MAX} "
                        "characters, icon from dash/icons.txt, do reply (default) or dismiss")
    s.add_argument("--preview", action="append", metavar="FILE|DIR",
                   help=f"add: local HTML the ask links to, copied into the state dir and served sandboxed; at most {asks.PREVIEWS_MAX}")
    s.add_argument("--keep", action="store_true", help="done: mark its answers done but keep the ask on the dashboard")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("stack", help="the stacked-PR chains athena watch follows (stack.json): set, clear, show")
    s.add_argument("action", choices=["set", "clear", "show"], nargs="?", default="show")
    s.add_argument("repo", nargs="?", help="owner/name, as gh --repo takes it")
    s.add_argument("prs", nargs="*", type=int, help="the chain's PR numbers, bottom first")
    s.set_defaults(func=cmd_stack)

    s = sub.add_parser("pr", help="PR summary for a worker")
    s.add_argument("name")
    s.set_defaults(func=cmd_pr)

    s = sub.add_parser("review", help="record HQ's review of a worker's PR: start, pass or fail, at its head")
    s.add_argument("name")
    s.add_argument("verdict", choices=pr.VERDICTS)
    s.add_argument("--head", help="the commit reviewed (default: start, the PR's current head; pass and fail, the head start recorded)")
    s.set_defaults(func=cmd_review)

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

    s = sub.add_parser("hq", help="create or focus the chief of staff's space; `restart` restarts its Claude in place, "
                                  "`rotate` replaces it with a fresh conversation that starts from a handoff",
                       description="athena hq: create or focus HQ. "
                                   "restart: update Claude Code and plugins, exit HQ's Claude and resume its conversation "
                                   "in the same pane. "
                                   "rotate: write the handoff (handoff.md in the state dir: fleet, PRs, open asks, "
                                   "in-flight threads, notes tail, standing orders) from athena's state, then, once HQ is "
                                   "idle, /exit its Claude (its conversation stays resumable) and start a NEW HQ "
                                   "conversation in the same pane, told to read the handoff and run session start. "
                                   "No updates; detached, like restart; log in logs/hq-restart.log.")
    s.add_argument("action", choices=["restart", "rotate"], nargs="?",
                   help="restart: resume the same conversation; rotate: a fresh conversation from a handoff")
    s.add_argument("--no-update", action="store_true", help="restart: skip claude update and the plugin updates")
    s.add_argument("--threads", metavar="FILE|-",
                   help="rotate: the in-flight threads HQ wrote for the handoff (default: threads.md in the state dir)")
    s.add_argument("--dry-run", action="store_true",
                   help="rotate: print the handoff, the launch command and the first prompt; run and write nothing")
    s.add_argument("--if-context-over", type=float, metavar="PCT",
                   help="rotate: only when HQ's context (read from its transcript) is at least PCT%% full")
    s.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--cwd")
    s.add_argument("--no-focus", action="store_true")
    s.add_argument("--no-board", action="store_true")
    s.add_argument("--no-dash", action="store_true")
    s.add_argument("--no-attach", action="store_true", help="outside herdr: open HQ but do not attach this terminal")
    s.set_defaults(func=cmd_hq)

    sub.add_parser("init", help="check herdr, claude, gh and git, ask for your folders and merge rules, write config.json "
                                "(athena init -h)")

    s = sub.add_parser("config", help="the effective config and where each value comes from: show, path")
    s.add_argument("action", choices=["show", "path"], nargs="?", default="show")
    s.set_defaults(func=cmd_config)
    return p


def main(argv=None):
    p = parser()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:  # init parses its own flags (athena init -h)
        try:
            return init.main(argv[1:])
        except (config.ConfigError, OSError) as exc:
            print(f"athena init: {exc}", file=sys.stderr)
            return 2
    args = p.parse_args(argv or ["hq"])  # a bare `athena` opens HQ
    try:
        _migrate()  # before any command, so every command reads the same written-down values
    except (config.ConfigError, OSError) as exc:
        print(f"athena: could not write the config ({exc}); running on the current values", file=sys.stderr)
    config.sync_root_hint()
    if args.cmd == "hq" and args.action != "rotate" and (args.threads or args.dry_run or args.if_context_over is not None):
        p.error("--threads, --dry-run and --if-context-over go with `athena hq rotate`")
    if args.cmd == "spawn" and args.workflow_size and not args.ultracode:
        p.error("--workflow-size needs --ultracode")
    try:
        return args.func(args)
    except (asks.AskError, spawn.SpawnError, retire.RetireError, dash.DashError, hq.RestartError, herdr.HerdrError, config.ConfigError, gitops.GitError, RuntimeError, OSError) as exc:
        print(f"athena {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
