"""athena: plumbing for the chief of staff. Run `athena -h`."""
import argparse
import json
import os
import re
import subprocess
import time
import sys
from datetime import datetime
from pathlib import Path

from athena_lib import (asks, board, brief, config, dash, gitops, handoff, herdr, hookstatus, hq, init, launch,
                        ledger, pr, requestlog, retire, service, slack, sockets, spawn, status, watch)

REVIEW_ROUNDS = 3  # HQ's review passes before open findings go to the human
REVIEW_FAILS_BEFORE_RESTART = 2  # failed rounds after which a worker restarts fresh rather than being nudged again
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
                         workflow_size=a.workflow_size, brief_check=not a.skip_brief_check)
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


def cmd_brief(a):
    try:
        text = Path(a.file).expanduser().read_text()
    except OSError as exc:
        print(f"athena brief check: {exc}", file=sys.stderr)
        return 2
    found = brief.check(text) + (brief.check_goal(a.goal) if a.goal else [])
    if a.json:
        _print(found)
    elif found:
        print("\n".join(brief.describe(p) for p in found))
    else:
        print("brief ok")
    return 1 if found else 0


def _context_hint():
    """One line when HQ's context is filling up; never a reason for `athena status` to fail."""
    try:
        ctx = handoff.hq_context()
    except Exception:
        return
    if ctx and ctx["percent"] >= handoff.HINT_PERCENT:
        print(f"{'hq':<12}  context {handoff.describe(ctx)}: rotate soon (athena hq rotate, skill §1a)")


REPORT_LISTS = ("decisions", "concerns")


def _worker_detail(r):
    """`athena status <worker>`: everything known about one worker, the whole report one field a line."""
    out = [f"{r['name']}  {r['state']}  ({r.get('state_source')})  branch {r.get('branch') or '-'}"]
    git = r.get("git") or {}
    if git:
        out.append(f"  git: head {git.get('head') or '-'}, {git.get('ahead', '-')} ahead, {len(git.get('dirty') or [])} dirty")
    if r.get("pr"):
        p = r["pr"]
        out.append(f"  pr: #{p.get('number')} {p.get('state') or ''} checks {p.get('checks') or '-'} "
                   f"review {p.get('review') or 'none'} {p.get('url') or ''}".rstrip())
    verdict = r.get("athena_review") or {}
    if verdict.get("verdict"):
        out.append(f"  athena review: {verdict['verdict']} @ {str(verdict.get('head') or '-')[:7]}")
    if r.get("socket_missing"):
        out.append(f"  messaging socket missing: {r['socket_missing']}")
    if r.get("summary"):
        out.append("  last words:")
        out += ["    " + line for line in r["summary"].strip().splitlines() if line.strip()]
    report = r.get("report") or r.get("last_report")
    if report:
        out.append("  report" + ("" if r.get("report") else " (the last one; none since its last prompt)") + ":")
        for k in ("status", "pr", "head", "verify"):
            if report.get(k):
                out.append(f"    {k}: {report[k]}")
        for k in REPORT_LISTS:
            entries = [e for e in report.get(k) or [] if isinstance(e, str)]
            if entries:
                out.append(f"    {k}:")
                out += [f"      - {e}" for e in entries]
        for k, v in report.items():
            if k not in ("status", "pr", "head", "verify", *REPORT_LISTS) and v not in (None, "", []):
                out.append(f"    {k}: {v if isinstance(v, str) else json.dumps(v)}")
    else:
        out.append("  no report yet")
    return "\n".join(out)


def cmd_status(a):
    if a.names:
        live = {w["name"]: w for w in ledger.live()}
        missing = [n for n in a.names if n not in live]
        if missing:
            print(f"not a live worker: {', '.join(missing)}", file=sys.stderr)
            return 2
        rows = [status.collect(live[n], with_pr=a.pr) for n in a.names]
        if a.json:
            _print(rows)
        else:
            print("\n\n".join(_worker_detail(r) for r in rows))
        return 0
    rows = [status.collect(w, with_pr=a.pr) for w in ledger.live()]
    rows.sort(key=lambda r: (r["urgency"], r["name"]))
    left = ledger.leftovers()
    if a.json:
        _print(rows)
        return 0
    _context_hint()
    hq_socket = sockets.hq_status_line()
    if hq_socket:
        print(hq_socket)
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
        if r.get("socket_missing"):
            bits.append(f"messaging socket missing ({r['socket_missing']}): relaunch it")
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
    elif a.action == "run":
        rec = dash.run()  # returns only when a dashboard already runs: this process became the dashboard otherwise
        print(f"dash already runs: {_dash_line(rec)}", file=sys.stderr)
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


ADD_USAGE = ('athena item add "<one-sentence title>" [--name "<2-4 words>"] [--kind decision|request|result|promise|incident] '
             '[--why ...] [--pr ...] [--body ... | --body-file <path|->] [--option label[:icon[:do]]] ... [--rec <option>] '
             '[--auto] [--next ...] [--due ...] [--words ...] [--request rN] [--worker w] [--link title|url] [--stakes ...] '
             '[--preview <file|dir>] ...')


def _fields(a):
    """The item fields add and edit share, from the parsed flags; None for each flag not given."""
    words = _read_text(a.words_file) if a.words_file else a.words
    stakes = None if a.stakes is None else [s for s in re.split(r"[,\s]+", a.stakes) if s]
    return {"name": a.name, "kind": a.kind, "next": a.next, "due": a.due, "words": words,
            "requests": a.request or [], "workers": a.worker or [], "links": a.link or [], "rec": a.rec,
            "stakes": stakes}


def _item_line(i, now=None):
    """One item as a person reads it: its name first, never a bare id."""
    now = time.time() if now is None else now
    bits = [f"{i.get('name') or '?'}", f"({i['id']}, {i.get('kind')}, next: {i.get('next')})"]
    if i.get("due"):
        bits.append("due " + time.strftime("%a %d %b %H:%M", time.localtime(i["due"])))
    if i.get("snooze") and i["snooze"].get("until", 0) > now:
        bits.append("snoozed")
    if i.get("applied"):
        bits.append(f"default applied: {i['applied'].get('label')}")
    elif i.get("auto"):
        bits.append(f"default: {i.get('rec')}")
    if i.get("name_derived"):
        bits.append("name derived: rename it")
    return " ".join(bits) + f": {i.get('text', '')}"


def cmd_ask(a):
    if a.action == "add":
        if not a.args or len(a.args) > 1:
            print(ADD_USAGE, file=sys.stderr)
            return 2
        kind = a.kind or ("note" if a.note else "decision" if a.question else None)  # a note with options is a decision
        body = _read_text(a.body_file) if a.body_file else a.body or ""
        f = _fields(a)
        new = asks.add(a.args[0], kind=kind, why=a.why, pr=a.pr, options=a.option or [], body=body, previews=a.preview or [],
                       name=f["name"], next=f["next"], due=f["due"], words=f["words"], requests=f["requests"],
                       workers=f["workers"], links=f["links"], rec=f["rec"], auto=a.auto, stakes=f["stakes"] or (),
                       fyi=a.fyi)
        print(new)
        if not a.name:
            print(f"athena item: named it {asks.show(new)['name']!r} from its title; give it 2 to 4 words the human would "
                  f"recognise: athena item edit {new} --name \"...\"", file=sys.stderr)
    elif a.action == "edit":
        if len(a.args) != 1:
            print("athena item edit <id> [--name ...] [--title ...] [--why ...] [--kind ...] [--next ...] [--due ...|none] "
                  "[--words ...] [--pr ...|''] [--option ...] [--rec ...] [--auto|--no-auto] [--stakes ...] [--request rN] "
                  "[--worker w] [--link title|url]", file=sys.stderr)
            return 2
        f = _fields(a)
        if a.note or a.question:
            f["kind"] = "note" if a.note else "decision"  # a note with options is a decision
        _print(asks.edit(a.args[0], text=a.title, why=a.why, pr=a.pr, options=a.option,
                         auto=a.auto if a.auto or a.no_auto else None, **f))
    elif a.action == "body":
        if len(a.args) != 1 or not a.file:
            print("athena item body <id> --file <path|->  (an empty file clears it)", file=sys.stderr)
            return 2
        _print(asks.set_body(a.args[0], _read_text(a.file)))
    elif a.action == "done":
        if len(a.args) != 1:
            print('athena item done <id> [--resolution "you merged it"] [--keep]', file=sys.stderr)
            return 2
        _print(asks.done(a.args[0], keep=a.keep, note=a.note_text or ""))
    elif a.action == "snooze":
        if len(a.args) != 1 or not (a.until or a.wake):
            print("athena item snooze <id> --until <YYYY-MM-DD[THH:MM]|+Nh|+Nd>  (or --wake to bring it back now)", file=sys.stderr)
            return 2
        _print(asks.snooze(a.args[0], None if a.wake else a.until))
    elif a.action == "queue":
        _print(asks.queue())
    elif a.action == "migrate":
        _print(asks.migrate_items(dry_run=a.dry_run))
    elif a.action == "requests":
        for r in requestlog.requests(since=time.time() - a.days * 86400):
            home = requestlog.filed().get(r["id"])
            print(requestlog.describe(r) + (f"  -> {home}" if home else "  (chat)" if r["ignored"] else "  NO ITEM"))
    elif a.action == "check":
        lost = requestlog.check(days=a.days)
        if a.json:
            _print(lost)
        elif not lost:
            print(f"every request of the last {a.days:g} days has an item")
        else:
            print(f"{len(lost)} request{'s' if len(lost) != 1 else ''} of the last {a.days:g} days with no item "
                  "(file each: athena item add ... --request <id>, or athena item ignore <id> for chat):")
            for r in lost:
                print("  " + requestlog.describe(r))
    elif a.action == "ignore":
        if not a.args:
            print("athena item ignore <request id> ...", file=sys.stderr)
            return 2
        _print(requestlog.ignore(a.args))
    elif a.args:
        item = asks.show(a.args[0])
        if not item:
            print(f"no item {a.args[0]}", file=sys.stderr)
            return 2
        _print(item)
    elif a.json:
        _print(asks.items())
    else:
        found = asks.items()
        for i in found:
            print(_item_line(i))
        if not found:
            print("no open items")
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


def _open_prs_check(prs):
    """Exit 0: the PR's review-channel post still counts (no teammate review since), so HQ posts nothing; 1: post it; 2:
    cannot tell, so HQ posts nothing and tells the human. Every error is 2: only a clear answer may say post."""
    key = slack.pr_key(prs[0]) if len(prs) == 1 else None
    if not key:
        print("athena open-prs check <owner/name#n|PR URL>", file=sys.stderr)
        return 2
    try:
        repo, n = key.split("#")
        got = (pr.batch([(repo, int(n))]) or {}).get(key)
        if got is None:
            raise RuntimeError("gh failed")
        reviewed, posted = got.get("teammate_reviewed_at") or "", slack.posted().get(key)
        # A post counts until a teammate reviews the PR after it; then a fresh post is wanted (the dash agrees).
        counts = posted is not None and (not reviewed or float(posted) > datetime.fromisoformat(
            reviewed.replace("Z", "+00:00")).timestamp())
        out = json.dumps({"pr": key, "posted": posted, "teammate_reviewed_at": reviewed or None, "counts": counts},
                         indent=2)
    except Exception as exc:  # noqa: BLE001: any failure is "cannot tell", never "post it"
        print(f"athena open-prs check: cannot tell whether {key}'s post still counts ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 2
    print(out)
    return 0 if counts else 1


def cmd_open_prs(a):
    if a.action == "check":
        return _open_prs_check(a.prs)
    if a.action == "add":
        if not config.get("open_prs_channel"):
            print("athena open-prs add: no open_prs_channel in config.json, so no post to record", file=sys.stderr)
            return 2
        keys = []
        for given in a.prs:
            key = slack.pr_key(given)
            if not key:
                print(f"athena open-prs add: not a PR: {given!r} (owner/name#n or its GitHub URL)", file=sys.stderr)
                return 2
            keys.append(key)
        if not keys:
            print("athena open-prs add <owner/name#n|PR URL> ...", file=sys.stderr)
            return 2
        now = time.time()
        slack.record_posted({k: f"{now:.6f}" for k in keys}, now=now)
    _print(slack.posted())
    return 0


def cmd_pr(a):
    w = ledger.workers().get(a.name)
    if not w:
        print(f"no worker named {a.name}", file=sys.stderr)
        return 2
    _print(pr.summary(w["repo"], w["branch"]))
    return 0


_PR_URL = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)\b")
_PR_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def cmd_threads(a):
    found = _PR_URL.match(a.target) or _PR_REF.match(a.target)
    if found:
        slug, number = found.group(1), int(found.group(2))
    else:
        w = ledger.workers().get(a.target)
        p = pr.summary(w["repo"], w["branch"]) if w and w.get("repo") and w.get("branch") else None
        url = _PR_URL.match((p or {}).get("url") or "")
        if not url:
            print(f"athena threads: {a.target} is no PR (owner/name#n or its URL) and no worker with a PR",
                  file=sys.stderr)
            return 2
        slug, number = url.group(1), int(url.group(2))
    found = pr.threads(slug, number)
    if found is None:
        print(f"athena threads: cannot read {slug}#{number}'s review threads from GitHub", file=sys.stderr)
        return 2
    _print(found)
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
    extra = {}
    if a.verdict == "start":
        past = ledger.reviews(a.name)
        fails = sum(1 for r in past if r.get("verdict") == "fail")
        if fails >= REVIEW_FAILS_BEFORE_RESTART and not a.force:
            print(f"athena review: {a.name} failed {fails} review rounds. Restart it fresh from the updated brief "
                  f"(athena retire {a.name}, then athena spawn with the findings in the brief) instead of a round "
                  f"{fails + 1}; --force reviews again anyway", file=sys.stderr)
            return 2
        extra["round"] = 1 + sum(1 for r in past if r.get("verdict") == "start")
        verdicts = [r for r in past if r.get("verdict") in ("pass", "fail") and r.get("head")]
        if verdicts:
            extra["since"] = verdicts[-1]["head"]
        if extra["round"] > REVIEW_ROUNDS:
            print(f"athena review: round {extra['round']} for {a.name}: past {REVIEW_ROUNDS}, review only what changed "
                  f"since {extra.get('since', 'the last verdict')}, and bring open findings to the human",
                  file=sys.stderr)
    _print(ledger.review(a.name, a.verdict, head.lower(), **extra))
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
            _print(hq.restart_now(update=not a.no_update, force_update=a.update))
        else:
            print(hq.restart(update=not a.no_update, force_update=a.update)["message"])
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


def cmd_whoami(a):
    """The worker this session is: ATHENA_WORKER, else the live worker whose worktree holds the cwd. Exit 1: none."""
    name = hookstatus.worker_name(os.environ, os.getcwd())
    if not name:
        return 1
    print(name)
    return 0


def cmd_service(a):
    if a.action == "status":
        rows = service.status(a.platform, a.instance)
        if a.json:
            _print(rows)
            return 0
        for r in rows:
            print(f"{r['job']:<7} {r['name']:<28} {'installed' if r['installed'] else 'not installed':<14} "
                  f"{'loaded' if r['loaded'] else 'not loaded'}")
        return 0
    fn = service.install if a.action == "install" else service.uninstall
    result = fn(a.platform, a.instance, dry_run=a.dry_run)
    if a.dry_run:
        return 0
    if a.json:
        _print(result)
        return 0
    if a.action == "install":
        for path, change in result["files"].items():
            print(f"{change:<9} {path}")
    else:
        for path in result["removed"]:
            print(f"removed   {path}")
        if not result["removed"] and not result["ran"]:
            print("nothing installed")
    for cmd in result["ran"]:
        print(f"ran       {cmd}")
    for note in result.get("notes", []):
        print(f"note: {note}")
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
    s.add_argument("--force", action="store_true",
                   help=f"go past the cap (ATHENA_MAX_WORKERS, default 4), up to {spawn.HARD_CAP} live workers")
    s.add_argument("--no-setup", action="store_true")
    s.add_argument("--ultracode", action="store_true", help="turn ultracode on in the worker's settings (only when the human asks)")
    s.add_argument("--workflow-size", choices=["small", "medium", "large", "unrestricted"],
                   help="the worker's workflow size guideline; needs --ultracode")
    s.add_argument("--skip-brief-check", action="store_true",
                   help="launch even when athena brief check finds wording the goal evaluator could never call met")
    s.set_defaults(func=cmd_spawn)

    s = sub.add_parser("brief", help="check a brief (and a goal) for wording the goal evaluator could never call met",
                       description="athena brief check <file> [--goal TEXT]: exit 0 and `brief ok`, or exit 1 and one "
                                   "line per problem (outcome, human, vague, remote, unrunnable, delivery, missing). "
                                   "athena spawn runs the same check.")
    s.add_argument("action", choices=["check"])
    s.add_argument("file")
    s.add_argument("--goal", help="the /goal condition HQ will pass to athena spawn")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("status", help="merged status of live workers; with names, each one's full report")
    s.add_argument("names", nargs="*", help="a worker's name: everything known about it, its whole report included")
    s.add_argument("--json", action="store_true")
    s.add_argument("--pr", action="store_true", help="include PR state from gh (slower)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("board", help="worker columns beside HQ")
    s.add_argument("action", choices=["sync", "watch", "on", "off", "toggle", "show"], nargs="?", default="show")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("dash", help="the dashboard on 127.0.0.1: on, off, show, open; run (foreground, for a service)")
    s.add_argument("action", choices=["on", "off", "show", "open", "run"], nargs="?", default="show")
    s.set_defaults(func=cmd_dash)

    s = sub.add_parser("watch", help="print one line per change (the athena-watch mod runs it in HQ; Monitor is the fallback)")
    s.add_argument("--interval", type=float, default=20)
    s.add_argument("--tagged", action="store_true", help="JSON lines for the mod: each change marked wake or not, and the status line")
    s.add_argument("--slack-once", action="store_true",
                   help="poll the Slack doorbell once and print its slack-new lines (ids and counts), then exit")
    s.add_argument("--window", type=float, default=0,
                   help="with --slack-once: a conversation seen for the first time counts back this many seconds")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("item", aliases=["ask"],
                       help="athena's items on the dashboard (needs-you.json) and the human's answers: add, edit, body, done, "
                            "snooze, show, queue, migrate; the request log: requests, check, ignore")
    s.add_argument("action", choices=["add", "edit", "body", "done", "snooze", "show", "queue", "migrate", "requests",
                                      "check", "ignore"], nargs="?", default="show")
    s.add_argument("args", nargs="*", help="add: the title, one sentence; edit, body, done, snooze, show: the id (show alone "
                                           "lists the open items); ignore: request ids")
    k = s.add_mutually_exclusive_group()
    k.add_argument("--question", action="store_true", help="add: a decision (the default when the title has a ?)")
    k.add_argument("--note", action="store_true", help="add: a result, for the human to read (the default without a ?)")
    s.add_argument("--name", help="add, edit: 2 to 4 words the human would recognise (add derives one from the title "
                                  "without it, marked for renaming)")
    s.add_argument("--kind", choices=list(asks.KINDS), help="add, edit: what it is (default: decision with a ?, else result)")
    s.add_argument("--title", help="edit: the new title, one sentence")
    s.add_argument("--why", default=None, help="add, edit: one sentence, shown under it")
    s.add_argument("--next", choices=list(asks.NEXT), help="add, edit: whose move it is (default: you for a decision or "
                                                          "result, athena otherwise)")
    s.add_argument("--due", help="add, edit: YYYY-MM-DD (09:00), YYYY-MM-DDTHH:MM, +Nh, +Nd, or none")
    w = s.add_mutually_exclusive_group()
    w.add_argument("--words", help="add, edit: the human's own words, quoted in its full view")
    w.add_argument("--words-file", metavar="PATH", help="add, edit: the words from a file, or - for stdin")
    s.add_argument("--request", action="append", metavar="rN", help="add, edit: the request-log id(s) it files")
    s.add_argument("--worker", action="append", help="add, edit: a worker on it (its row shows the item's name)")
    s.add_argument("--link", action="append", metavar="TITLE|URL", help="add, edit: a report, screenshot or page it links")
    s.add_argument("--pr", help="add, edit: owner/name#n or a PR URL ('' unlinks)")
    b = s.add_mutually_exclusive_group()
    b.add_argument("--body", help=f"add: Markdown its full view shows, up to {asks.BODY_MAX} characters")
    b.add_argument("--body-file", metavar="PATH", help="add: the body from a file, or - for stdin")
    s.add_argument("--file", metavar="PATH", help="body: the new body from a file, or - for stdin; empty clears it")
    s.add_argument("--option", action="append", metavar="LABEL[:ICON[:DO]]",
                   help=f"add, edit (replaces them): at most {asks.OPTIONS_MAX}, {asks.OPTIONS_MAX_BODY} with a body; label up "
                        f"to {asks.LABEL_MAX} characters, icon from dash/icons.txt, do reply (default) or dismiss")
    s.add_argument("--rec", help="add, edit: the recommended option's id (its label in lower case, dashes for spaces): "
                                 "Take rec on the dashboard")
    d = s.add_mutually_exclusive_group()
    d.add_argument("--auto", action="store_true", help="add, edit: the rec applies on its own after 12 h (2 h once the human "
                                                       "is away); reversible, low-stakes decisions only (decision 4)")
    d.add_argument("--no-auto", action="store_true", help="edit: no default applies on its own")
    s.add_argument("--stakes", metavar="LIST", help=f"add, edit: any of {', '.join(asks.STAKES)} (comma-separated; '' clears): "
                                                    "never a default on its own")
    s.add_argument("--fyi", action="store_true", help="add: a tiny result, resolved at once: a line in Done")
    s.add_argument("--preview", action="append", metavar="FILE|DIR",
                   help=f"add: local HTML its full view embeds, copied into the state dir and served sandboxed; at most {asks.PREVIEWS_MAX}")
    s.add_argument("--keep", action="store_true", help="done: mark its answers done but keep the item open")
    s.add_argument("--note-text", "--resolution", dest="note_text", metavar="TEXT",
                   help='done: how it resolved, on its timeline ("you merged it", "you replied in chat")')
    s.add_argument("--until", help="snooze: YYYY-MM-DD (09:00), YYYY-MM-DDTHH:MM, +Nh or +Nd")
    s.add_argument("--wake", action="store_true", help="snooze: bring it back now")
    s.add_argument("--dry-run", action="store_true", help="migrate: count and list what would change, write nothing")
    s.add_argument("--days", type=float, default=requestlog.CHECK_DAYS, help="requests, check: how far back (default 7)")
    s.add_argument("--json", action="store_true", help="show, check: JSON")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("stack", help="the stacked-PR chains athena watch follows (stack.json): set, clear, show")
    s.add_argument("action", choices=["set", "clear", "show"], nargs="?", default="show")
    s.add_argument("repo", nargs="?", help="owner/name, as gh --repo takes it")
    s.add_argument("prs", nargs="*", type=int, help="the chain's PR numbers, bottom first")
    s.set_defaults(func=cmd_stack)

    s = sub.add_parser("open-prs", help="the PRs posted for review in config open_prs_channel (open-prs-posted.json): "
                                       "show, add, check")
    s.add_argument("action", choices=["add", "show", "check"], nargs="?", default="show")
    s.add_argument("prs", nargs="*", help="owner/name#n or a PR's GitHub URL. add: posted just now. check: exit 0 when "
                                          "its post still counts (no teammate review since), 1 when a post is wanted, "
                                          "2 when GitHub cannot say")
    s.set_defaults(func=cmd_open_prs)

    s = sub.add_parser("pr", help="PR summary for a worker")
    s.add_argument("name")
    s.set_defaults(func=cmd_pr)

    s = sub.add_parser("review", help="record HQ's review of a worker's PR: start, pass or fail, at its head")
    s.add_argument("name")
    s.add_argument("verdict", choices=pr.VERDICTS)
    s.add_argument("--head", help="the commit reviewed (default: start, the PR's current head; pass and fail, the head start recorded)")
    s.add_argument("--force", action="store_true", help="start: review again after two failed rounds")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("threads", help="a PR's unresolved review threads: who (bot or not), where, the first comment")
    s.add_argument("target", help="a worker's name, owner/name#n, or a PR's GitHub URL")
    s.set_defaults(func=cmd_threads)

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
                                   "restart: update Claude Code and plugins (unless they last ran without a warning "
                                   "within config hq_update_interval_hours, 6 by default), exit HQ's Claude and resume "
                                   "its conversation in the same pane. "
                                   "rotate: write the handoff (handoff.md in the state dir: fleet, PRs, every open item, "
                                   "in-flight threads, notes tail, standing orders) from athena's state, then, once HQ is "
                                   "idle, /exit its Claude (its conversation stays resumable) and start a NEW HQ "
                                   "conversation in the same pane, told to read the handoff and run session start. "
                                   "No updates; detached, like restart; log in logs/hq-restart.log.")
    s.add_argument("action", choices=["restart", "rotate"], nargs="?",
                   help="restart: resume the same conversation; rotate: a fresh conversation from a handoff")
    updates = s.add_mutually_exclusive_group()
    updates.add_argument("--no-update", action="store_true", help="restart: skip claude update and the plugin updates")
    updates.add_argument("--update", action="store_true",
                         help="restart: run claude update and the plugin updates even if they ran within config "
                              "hq_update_interval_hours")
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

    s = sub.add_parser("whoami", help="print the worker this session is (ATHENA_WORKER, else by its worktree); exit 1 if none")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("service", help="herdr, the dashboard, the board and doctor as login services (launchd, systemd): "
                                       "install, uninstall, status; --dry-run prints what it would write and run",
                       description="athena service: run herdr's server, the dashboard and the board watcher as user "
                                   "services that restart after a crash and come back after a reboot, and `athena "
                                   "doctor --fix` every minute. launchd agents on macOS, systemd user units on Linux. "
                                   "Install and uninstall are idempotent. Each job carries this shell's athena "
                                   "environment (ATHENA_STATE_DIR, ATHENA_HERDR, ...), so use --instance for a second "
                                   "install against another state dir and herdr session.")
    s.add_argument("action", choices=["install", "uninstall", "status"], nargs="?", default="status")
    s.add_argument("--dry-run", action="store_true", help="print the files and commands; write and run nothing")
    s.add_argument("--instance", help="a second install beside the default one: its jobs are athena.<instance>.<job>")
    s.add_argument("--platform", choices=["launchd", "systemd"], help="default: launchd on macOS, systemd on Linux")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_service)

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
    except (asks.AskError, spawn.SpawnError, retire.RetireError, dash.DashError, hq.RestartError, hq.ResumeError, service.ServiceError, herdr.HerdrError, config.ConfigError, gitops.GitError, RuntimeError, OSError) as exc:
        print(f"athena {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
