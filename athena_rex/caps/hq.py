"""HQ: the chief of staff's Claude in its own Rex session, first in the list, and its rotation to a fresh conversation. Rex runs the
session: nothing here brings a gone HQ back by itself.

hq.json stays what the old athena reads (session, previous_session, claude_args, cwd), with `backend`, `space` and
`block` added. A rotation runs detached, since HQ asks for it from its own Bash tool and /exit ends it.

    absent -open-> running -stop-> absent          running -rotate-> running
    running -its Claude dies-> gone or gone-fresh  gone, lost -open-> running (doctor only flags it)
    gone-fresh -open-> running, on a new conversation

Computed by `state`, never stored. `unrecorded` and `lost` are a lost hq.json, with its Claude running or gone.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import agents, bus, caps, config, lab, sandbox, store
from athena_rex.kernel.backend import BackendError

SUMMARY = "open, rotate or stop the chief of staff's session"
PROMPTS = store.ROOT / "prompts"
PLUGIN = store.ROOT / "plugin"
ROUTINE = ("Start your HQ routine: load the athena-rex skill, run `athena-rex status`, check `athena-rex doctor` shows the watch "
           "running (Monitor only as the skill's fallback), then tell me what is live and wait.")
ROTATED = ("Fresh HQ after `athena-rex hq rotate`: load the athena-rex skill, then read {handoff} in full (your fleet, PRs, every open "
           "item by name, in-flight threads, notes tail and standing orders). Run the skill's session start (§1) from it, including §1a: "
           "check `athena-rex doctor` shows the watch running, re-create the fallback sweep cron with the sweep subagent brief, act on "
           "`athena-rex ask queue`. Then tell me in at most five lines what needs me and what is live.")
TYPING_WAIT = float(os.environ.get("ATHENA_TYPING_WAIT") or 120)
LINE = object()  # `--if-context-over` with no percent: rotate once HQ's context holds config hq_rotate_tokens
BLOCKED_LIMIT = 600
LAST_WORDS = 2000
ASK_FORGOTTEN_AFTER = 3600
ROTATE_ASK = ("athena-rex doctor: your context is {ctx}. Write your in-flight threads (what you promised, what you wait on, what is half "
              "done) to {threads}, then run `athena-rex hq rotate`. It rotates once you are idle, never while the human is typing.")
ROTATE_ASK_EVERY = 2 * 3600
STATES = {"absent": "no HQ on this backend yet", "running": "running",
          "unrecorded": "HQ's Claude runs but its record is lost; no second one will be started",
          "lost": "HQ's record is lost and its Claude is gone; its conversation is known",
          "gone": "HQ's Claude is gone", "gone-fresh": "HQ's Claude is gone and no conversation is known; flagged, never started fresh"}


name = config.hq_name


def load() -> dict:
    """hq.json, else its previous generation: a lost record must not look like no HQ at all."""
    for name_ in ("hq.json", "hq.json.prev"):
        data = store.read_json(store.path(name_), None)
        if isinstance(data, dict) and data:
            return data
    return {}


def _save(record):
    current = store.path("hq.json")
    if store.read_json(current, None):
        store.write_text(store.path("hq.json.prev"), current.read_text())
    store.write_json(current, record, indent=2)


def resumable(record=None):
    """What a dead HQ comes back on. previous_session covers a rotation lost after its switch and before the new one started."""
    record = load() if record is None else record
    return trusted_session(record) or record.get("previous_session")


def alive() -> bool:
    return bool(agents.stat_of(name()))


def _settings() -> Path:
    env = {"ATHENA_ROLE": "hq", "ATHENA_HQ": name(), "ATHENA_STATE_DIR": str(store.state_dir()), "FORCE_HYPERLINK": "1"}
    if os.environ.get("ATHENA_CONFIG"):
        env["ATHENA_CONFIG"] = os.environ["ATHENA_CONFIG"]
    hooks = caps.get("hooks")
    # idleCompaction off: HQ sits idle for hours and rotates instead (skill §1a). autoCompactEnabled stays the user's, the backstop
    # near the limit, and workers keep the user's idle compaction.
    settings = {"env": env, "remoteControlAtStartup": not lab.active(), "crossSessionInbound": "accept", "idleCompaction": False,
                "enabledPlugins": {"athena@athena": False},
                "hooks": hooks.settings_block() if hooks else {}}
    profiles = caps.get("profiles")
    if profiles:
        settings["permissions"] = profiles.permissions("hq")
    elif config.get("hq_allow"):
        settings["permissions"] = {"allow": list(config.get("hq_allow"))}
    target = store.ensure("settings") / "hq-rex.json"
    store.write_text(target, json.dumps(settings, indent=2) + "\n")
    return target


def hq_model() -> str:
    """config hq_model: the model HQ's Claude starts on. "", unset, null or not a string: no --model, the user's default."""
    value = config.get("hq_model")
    return value if isinstance(value, str) else ""


def claude_args(model=None) -> list:
    """`model` is `hq open --model`'s; else config hq_model; else no --model at all."""
    if config.unusable():
        raise UserError(f"{config.config_file()} does not parse and there is no good copy: fix it first, since its defaults could change what HQ may do")
    args = ["--name", name(), "--append-system-prompt-file", str(PROMPTS / "hq.md"), "--plugin-dir", str(PLUGIN),
            "--settings", str(_settings())]
    model = model or hq_model()
    if model:
        args += ["--model", model]
    if config.get("hq_skip_permissions"):
        args.append("--dangerously-skip-permissions")
    return args


def trusted_session(record=None):
    """The conversation to come back to: the one HQ's own SessionStart hook saw last, if newer than the record."""
    record = load() if record is None else record
    started = store.read_dict(store.path("hq-start.json"))
    return started.get("session_id") or record.get("session")


def _label(backend, place):
    """In Rex's sidebar HQ is its name: its session, window and block, whatever an older athena labelled them. Trouble is not fatal."""
    try:
        backend.label(place, session=name(), window=name(), block=name())
    except BackendError:
        pass


def _launch(record, args, cwd) -> dict:
    sandbox.require(UserError)
    ctx = caps.CTX
    module = caps.get("trust")
    held = None
    if module:
        try:
            held = module.ensure(cwd).get("why")
        except module.TrustError as exc:
            raise UserError(f"could not mark {cwd} trusted: {exc}")
    place = agents.restart(ctx.backend, record, name(), cwd, args, {"ATHENA_ROLE": "hq"}, label=name())
    _label(ctx.backend, place)
    try:
        ctx.backend.to_front(place.space)
    except BackendError:
        pass
    resumed = args[1] if args[:1] == ["--resume"] else None
    updated = {**record, "backend": ctx.backend.name, "space": place.space, "window": place.window, "block": place.block, "cwd": str(cwd),
               "claude_args": args[2:] if resumed else list(args), "session": resumed}
    updated.pop("trust_skipped", None)
    _save(updated)
    if held:
        print(f"folder trust was not written ({held}): unless it is already trusted, HQ's Claude stops at its prompt", file=sys.stderr)
        return {**updated, "trust_skipped": held}
    return updated


def open_hq(model=None, focus=True, routine=True) -> dict:
    with store.locked("locks/hq.lock"):
        out = _open_hq(model, focus)
    if routine and out["outcome"] != "running":
        _send_routine(out)
    return out


def _send_routine(record):
    """A session cannot start its own turn: HQ comes up idle until something types to it. Trouble here is said, not fatal."""
    target = _record_for_agents(record)
    backend = caps.CTX.backend
    try:
        agents.wait(backend, target, agents.READY, timeout=60)
        agents.send(backend, target, ROUTINE)
    except (agents.Refused, BackendError) as exc:
        print(f"HQ is up but its routine was not sent ({exc})", file=sys.stderr)


def stop(force=False) -> bool:
    """Exit HQ and close its session; its conversation stays on record for the next start. With force, a busy or typed-in HQ goes too."""
    record = load()
    if not record.get("space"):
        return False
    backend = caps.CTX.backend
    with store.locked("locks/hq.lock"):
        target = _record_for_agents(record)
        if not agents.graceful_exit(backend, target, grace=10) and not force:
            raise UserError("HQ's Claude did not exit: its box may hold unsent text, or it is busy. --force closes its session anyway")
        try:
            backend.close_space(record["space"])
        except BackendError as exc:
            if exc.code not in ("not_found", "server_down"):
                raise
        if force:
            agents.kill_if_alive(name())
        _save({**record, "space": None, "window": None, "block": None})
    return True


def _open_hq(model, focus) -> dict:
    """Idempotent: a running HQ is only focused; a gone one comes back on its conversation, fresh only if it had none."""
    ctx = caps.CTX
    record = load()
    if alive():
        if record.get("space") and record.get("block"):
            _label(ctx.backend, agents.place_of(record))
        if focus and record.get("space"):
            try:
                ctx.backend.focus(record["space"], record.get("window"))
            except BackendError:
                pass
        return {"outcome": "running", **record}
    elsewhere = store.state_elsewhere()
    if elsewhere:
        raise UserError(f"{store.state_dir()} is empty and state lives at {elsewhere}: set ATHENA_STATE_DIR, or move it first")
    cwd = record.get("cwd") or str(config.path("projects_dir"))
    session = resumable(record)
    args = claude_args(model)
    if session:
        args = ["--resume", session, *args]
    updated = _launch(record, args, cwd)
    bus.emit("hq.started", resumed=bool(session))
    return {"outcome": "resumed" if session else "started", **updated}


def _record_for_agents(record) -> dict:
    return {**record, "name": name()}


def _wait_quiet(backend, record, wait) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            now = agents.state(backend, record)
            if now["state"] in agents.READY and not agents.typing(backend, record):
                return True
        except BackendError:
            pass
        time.sleep(1)
    return False


def _cycle(record, args, prompt, why, forget=False, unattended=False) -> dict:
    """Exit HQ when it is quiet, start it again with `args`, then prompt it. A busy or typed-in HQ is left alone. As the old athena's: a
    rotation HQ asked for answers Claude's background-work dialog with "Exit and stop tasks"; an `unattended` one never stops HQ's
    background work, and skips instead."""
    sandbox.require(UserError)
    ctx = caps.CTX
    target = _record_for_agents(record)
    if not _wait_quiet(ctx.backend, target, TYPING_WAIT):
        raise UserError(f"{why} skipped: HQ is busy or someone is typing in its box")
    with store.locked("locks/hq.lock"):
        try:
            gone = agents.graceful_exit(ctx.backend, target, grace=15, stop_tasks=not unattended)
        except agents.BackgroundWork as exc:
            raise UserError(f"{why} skipped: {exc}")
        if not gone:
            raise UserError(f"{why} failed: HQ's Claude did not exit")
        if forget:
            store.path("hq-start.json").unlink(missing_ok=True)
        updated = _launch(record, args, record.get("cwd") or str(config.path("projects_dir")))
    target = _record_for_agents(updated)
    try:
        agents.wait(ctx.backend, target, agents.READY, timeout=120)
        agents.send(ctx.backend, target, prompt)
    except (agents.Refused, BackendError) as exc:
        raise UserError(f"{why}: HQ is back but its prompt was not sent ({exc})")
    return updated


def transcript(session) -> Path:
    found = list((agents.claude_dir() / "projects").glob(f"*/{session}.jsonl"))
    return found[0] if found else None


def context_tokens(session):
    """HQ's context in tokens, from the last usage in its transcript: input, cache-read and cache-creation, what the next turn starts
    from. None when it cannot say."""
    path = transcript(session) if session else None
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - (1 << 20)))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            usage = (json.loads(line).get("message") or {}).get("usage")
        except ValueError:
            continue
        if usage:
            used = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if used:  # a synthetic entry (an API error, an interrupted turn) carries no usage
                return used
    return None


def context_percent(session, window=None):
    """HQ's context as a percent of a window: only `hq rotate --if-context-over <pct>` measures that. A transcript doesn't name its
    window and Claude Code doesn't compact at 200k, so the window is config's, `rex.hq_context_window` (1M unless set), or
    ATHENA_HQ_CONTEXT_WINDOW. Never one derived from the model."""
    used = context_tokens(session)
    if used is None:
        return None
    size = int(window or os.environ.get("ATHENA_HQ_CONTEXT_WINDOW") or config.rex("hq_context_window") or 1_000_000)
    return round(100 * used / size)


def rotate_tokens() -> int:
    """config hq_rotate_tokens: the context, in tokens, from which athena asks HQ to rotate. Not a positive number: the default."""
    value = config.get("hq_rotate_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return config.DEFAULTS["hq_rotate_tokens"]
    return int(value)


def rotate_due(tokens) -> bool:
    """Doctor's ask, status's line and a bare `hq rotate --if-context-over` all ask this."""
    return tokens is not None and tokens >= rotate_tokens()


def against_line(tokens) -> str:
    """HQ's context as status and doctor show it: `171k of 400k (rotates at hq_rotate_tokens)`."""
    return f"{tokens // 1000}k of {rotate_tokens() // 1000}k (rotates at hq_rotate_tokens)"


HANDOFF_LINES = 150
HANDOFF_LINE_MAX = 300
NOTES_ENTRIES = 12
THREADS_MAX = 40


def notes_tail(text, n) -> list:
    """The last `n` entries of notes.md: a top-level bullet or a heading starts one, indented lines continue it."""
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(("- ", "* ", "#")) or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    entries = [e for e in entries if not e[0].startswith("# ")]
    return [line for entry in entries[-n:] for line in entry]


def _ahead(r):
    path = r.get("path")
    if not path or not os.path.isdir(path):
        return "-"
    try:
        from athena_rex.kernel import gitops
        base = gitops.default_branch(r.get("repo") or path)
        return gitops.git(path, "rev-list", "--count", f"origin/{base}..HEAD", check=False, timeout=10) or "-"
    except Exception:
        return "-"


def _fleet(rows) -> list:
    out = []
    for r in rows:
        report = r.get("report") or {}
        bits = [f"- {r['name']}", r.get("state") or "unknown", r.get("branch") or "-", f"ahead {_ahead(r)}"]
        if report.get("status"):
            bits.append(f"report {report['status']}" + (f" @ {str(report['head'])[:8]}" if report.get("head") else ""))
        if r.get("pending"):
            bits.append("goal pending")
        line = " · ".join(bits)
        if r.get("summary"):
            line += f": {str(r['summary']).replace(chr(10), ' ')[-100:]}"
        out.append(line)
    return out or ["no live workers"]


def _prs(rows) -> list:
    prs = caps.get("prs")
    out = []
    for r in rows:
        found = prs.summary(r.get("repo"), r.get("branch")) if prs and r.get("repo") and r.get("branch") else None
        if found:
            out.append(f"- {r['name']} #{found.get('number')} {found.get('state')}{' draft' if found.get('draft') else ''} · "
                       f"checks {found.get('checks') or '-'} · review {found.get('review') or 'none'} · {found.get('url')}")
    for chain in (prs.stacks() if prs else []):
        out.append(f"- stack {chain.get('repo')}: " + " → ".join(f"#{n}" for n in chain.get("prs") or []))
    return out or ["no open PRs on live workers, no stacks"]


def _item_line(i) -> str:
    bits = [i.get("kind") or "item", f"next: {i.get('next') or '?'}"]
    if i.get("due"):
        bits.append("due " + time.strftime("%a %d %b %H:%M", time.localtime(float(i["due"]))))
    if i.get("snooze"):
        bits.append("snoozed until " + time.strftime("%a %d %b %H:%M", time.localtime(float((i["snooze"] or {}).get("until") or 0))))
    if i.get("auto"):
        bits.append(f"default {i.get('rec')} applies on its own")
    if i.get("applied"):
        bits.append(f"default applied: {(i['applied'] or {}).get('label')}")
    line = f"- {i.get('name') or '?'} ({i.get('id')}, {', '.join(bits)}): {i.get('text', '')}"
    return line + (f" (pr {i['pr']})" if i.get("pr") else "") + (f" · workers {', '.join(i['workers'])}" if i.get("workers") else "")


def build_handoff(threads=None, now=None, previous_session=None) -> str:
    """Everything a fresh HQ needs, from state rather than from memory of the chat: at most HANDOFF_LINES lines besides the open items,
    which are never cut (the notes tail gives way instead)."""
    status = caps.get("status")
    rows = status.collect() if status else []
    items = caps.get("items")
    open_items = [_item_line(i) for i in (items.open_items() if items else []) if i.get("id")] or ["none"]
    thread_lines = [line.rstrip() for line in (threads or "").splitlines() if line.strip()] or [
        f"none written: HQ writes them before rotating (`athena-rex hq rotate --threads <file|->` or {store.path('threads.md')})"]
    if len(thread_lines) > THREADS_MAX:
        thread_lines = thread_lines[:THREADS_MAX - 1] + [f"… ({len(thread_lines) - THREADS_MAX + 1} more lines cut; the rest is in the rotated HQ's conversation)"]
    head = [f"# HQ handoff · {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}", "",
            "Written by `athena-rex hq rotate` from athena's state. Where this and `athena-rex status --pr` differ, trust status."]
    if previous_session:
        head.append(f"Previous HQ conversation: `claude --resume {previous_session}`, only if a thread needs it, and only in a separate "
                    "terminal, never from HQ's Bash: there it would run as a nested session of HQ.")
    sections = [("Fleet", _fleet(rows)), ("PRs", _prs(rows)), ("Open items", open_items), ("In-flight threads", thread_lines)]
    orders = ("Standing orders", [f"Read in full: {store.path('standing-orders.md')}", f"Repo map: {store.path('repo-map.md')}",
                                  f"Notes: {store.path('notes.md')}"])
    start = ["## Start here",
             "Load the athena-rex skill, then run its session start (§1) from this handoff. Doctor keeps athena-rex watch running "
             "(no Monitor unless the skill's fallback applies); re-create the session-only crons, the fallback sweep among them (§1a)."]

    def render(notes):
        lines = head + [""]
        for title, body in sections + [("Notes tail", notes), orders]:
            lines += [f"## {title}", *body, ""]
        return lines + start

    try:
        notes = notes_tail(store.path("notes.md").read_text(), NOTES_ENTRIES)
    except OSError:
        notes = []
    room = max(HANDOFF_LINES - len(render([])) - 1, 1)
    if len(notes) > room:
        notes = ["… (older notes cut)"] + notes[len(notes) - room + 1:]
    lines = render(notes or ["notes.md is empty"])
    return "\n".join(line if len(line) <= HANDOFF_LINE_MAX else line[:HANDOFF_LINE_MAX - 1] + "…" for line in lines) + "\n"


def _threads_file():
    return store.path("threads.md")


def threads_for_handoff(record) -> str:
    """What HQ wrote in threads.md, else its last words: a rotation nobody asked for must not hand off nothing."""
    threads = _threads_file()
    try:
        written = threads.read_text() if threads.is_file() else ""
    except OSError:
        written = ""
    if written.strip():
        return written
    hooks = caps.get("hooks")
    path = transcript(trusted_session(record))
    said = hooks.newest_message(path) if hooks and path else ""
    return f"(HQ wrote none. Its last words:)\n{said.strip()[-LAST_WORDS:]}" if said.strip() else ""


def read_threads(given):
    """`--threads`: `-` is stdin, read here and never by a detached child, whose stdin is empty; else a file, else the text itself."""
    if given is None:
        return None
    if given == "-":
        return sys.stdin.read()
    if os.path.exists(given):
        with open(given) as f:
            return f.read()
    return given


def rotate(threads=None, dry_run=False, if_context_over=None, unattended=False) -> dict:
    record = load()
    limits = caps.get("limits")
    held = limits.why_not_rotate() if limits and not dry_run else None
    if held:
        raise UserError(f"rotation skipped: {held}")
    if not dry_run:
        sandbox.require(UserError)
    if if_context_over is LINE:
        used = context_tokens(trusted_session(record))
        if not rotate_due(used):
            return {"outcome": "skipped", "context": against_line(used) if used is not None else None}
    elif if_context_over is not None:
        used = context_percent(trusted_session(record))
        if used is None or used < if_context_over:
            return {"outcome": "skipped", "context": used}
    previous = trusted_session(record)
    text = build_handoff(threads if threads is not None and threads.strip() else threads_for_handoff(record), previous_session=previous)
    handoff = store.path("handoff.md")
    if dry_run:
        print(text)
        print(f"launch: claude {' '.join(claude_args())}")
        print(f"prompt: {ROTATED.format(handoff=handoff)}")
        return {"outcome": "dry-run"}
    store.write_text(handoff, text)
    updated = _cycle({**record, "previous_session": previous, "session": None}, claude_args(),
                     ROTATED.format(handoff=handoff), "rotation", forget=True, unattended=unattended)
    if _threads_file().exists():
        os.replace(_threads_file(), _threads_file().with_name("threads.prev.md"))
    return {"outcome": "rotated", **updated}


def _around_hq(args):
    """What the old `athena hq` did beside opening HQ: a doctor --fix pass, its lines printed, the dashboard started and the board opened.
    Trouble here is said, never fatal: HQ is up."""
    if not getattr(args, "no_doctor", False):
        doctor = caps.get("doctor")
        if doctor:
            try:
                health = doctor.run(caps.CTX, fix=True)
                for action in health.get("actions") or []:
                    print(f"doctor did: {action}")
                for problem in health.get("problems") or []:
                    print(f"doctor: {problem.get('check')}: {problem.get('detail')}")
            except Exception as exc:
                print(f"doctor did not run: {exc}", file=sys.stderr)
    if not getattr(args, "no_dash", False):
        dash = caps.get("dash")
        if dash and hasattr(dash, "ours"):
            try:
                if not dash.ours():
                    dash.on()
                    print("dashboard started")
            except Exception as exc:
                print(f"the dashboard did not start: {exc}", file=sys.stderr)
    if not getattr(args, "no_board", False):
        board = caps.get("board")
        opener = getattr(board, "open_board", None) if board else None
        if opener:
            try:
                opener()
            except Exception as exc:
                print(f"the board did not open: {exc}", file=sys.stderr)


def _detach(*argv) -> Path:
    log = store.ensure("logs") / "hq-restart.log"
    binary = store.ROOT / "bin" / "athena-rex"
    with open(log, "a") as out:
        subprocess.Popen([str(binary), *argv], stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    return log


def _configure(p):
    p.add_argument("action", nargs="?", choices=["open", "rotate", "status", "stop"], default="open")
    p.add_argument("--no-routine", action="store_true", help="open it without sending its start routine")
    p.add_argument("--cwd", help="open: HQ's folder (default: config projects_dir)")
    p.add_argument("--no-focus", action="store_true", help="open: don't bring HQ's session forward")
    p.add_argument("--no-doctor", action="store_true", help="open: skip the doctor --fix pass")
    p.add_argument("--no-dash", action="store_true", help="open: don't start the dashboard")
    p.add_argument("--no-board", action="store_true", help="open: don't open the board")
    p.add_argument("--model")
    p.add_argument("--threads", metavar="FILE|-", help="rotate: the in-flight threads for the handoff (default: threads.md in the state dir)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--if-context-over", type=int, nargs="?", const=LINE, metavar="PCT",
                   help="rotate: only once HQ's context holds config hq_rotate_tokens (400k by default); given PCT, only when it is "
                        "at least PCT%% of rex.hq_context_window")
    p.add_argument("--now", action="store_true", help="internal: do it in this process")
    p.add_argument("--unattended", action="store_true", help="rotate: never stop HQ's background work, skip instead")
    p.add_argument("--force", action="store_true", help="stop: even if it is busy or someone is typing in its box")


def _run(args):
    if args.action == "open":
        if args.cwd:
            record = load()
            if not alive():
                _save({**record, "cwd": os.path.realpath(os.path.expanduser(args.cwd))})
        out = open_hq(args.model, focus=not args.no_focus, routine=not args.no_routine)
        print(f"HQ {out['outcome']} in session {out.get('space')}")
        _around_hq(args)
    elif args.action == "stop":
        print("HQ stopped" if stop(args.force) else "no HQ on this backend")
    elif args.action == "status":
        record = load()
        session = trusted_session(record)
        print(json.dumps({"alive": alive(), "session": session, "space": record.get("space"), "block": record.get("block"),
                          "context_tokens": context_tokens(session), "rotate_tokens": rotate_tokens(),
                          "context_percent": context_percent(session)}, indent=1))
    else:
        threads = read_threads(args.threads)
        if args.dry_run or args.now:
            print(json.dumps({k: v for k, v in rotate(threads, args.dry_run, args.if_context_over, args.unattended).items()
                              if k in ("outcome", "context")}))
            return 0
        if threads is not None:  # kept before the detached child waits for quiet: a skipped rotation's threads are read next time
            store.write_text(_threads_file(), threads)
        over = args.if_context_over
        extra = [] if over is None else ["--if-context-over"] if over is LINE else ["--if-context-over", str(over)]
        extra += ["--unattended"] if args.unattended else []
        if args.if_context_over is not None:  # the child checks the context first, and writes the handoff only if it rotates
            log = _detach("hq", "rotate", "--now", *extra)
            what = f"{rotate_tokens() // 1000}k tokens (hq_rotate_tokens)" if over is LINE else f"{over}% of its window"
            print(f"rotating HQ once it is idle if its context holds {what}; log at {log}")
            return 0
        handoff = store.path("handoff.md")
        store.write_text(handoff, build_handoff(threads_for_handoff(load()), previous_session=trusted_session()))
        log = _detach("hq", "rotate", "--now", *extra)
        print(f"handoff written to {handoff}; rotating HQ once it is idle; log at {log}")
    return 0


def _asked_file():
    return store.path("run", "rotation-asked.json")


def _ask_to_rotate(used):
    """As the old doctor: once its context holds hq_rotate_tokens, HQ is asked, at most once in two hours and only while it is idle with an empty box, to write its
    threads and rotate itself. Nothing rotates it by force. This runs inside `doctor --json`, so it prints nothing."""
    record = _record_for_agents(load())
    backend = caps.CTX.backend
    try:
        if agents.state(backend, record)["state"] not in agents.READY or agents.typing(backend, record):
            return
        agents.send(backend, record, ROTATE_ASK.format(ctx=against_line(used), threads=store.path("threads.md")))
    except (agents.Refused, BackendError):
        return
    store.write_json(_asked_file(), {"ts": time.time()})


def state(record, running) -> str:
    """Which of STATES HQ is in, from its record and whether its Claude runs."""
    if running:
        return "running" if record.get("block") else "unrecorded"
    if record.get("block"):
        return "gone" if resumable(record) else "gone-fresh"
    return "lost" if not record and trusted_session(record) else "absent"


def _running_checks(ctx, record) -> list:
    session = trusted_session(record)
    if session != record.get("session"):
        _save({**record, "session": session})
    out = [caps.Check("hq", True, f"running, conversation {session}")]
    now = agents.state(ctx.backend, _record_for_agents(record))
    memory = store.read_dict(store.path("run", "hq-blocked.json"))
    if now["state"] == "blocked":
        since = float(memory.get("since") or time.time())
        store.write_json(store.path("run", "hq-blocked.json"), {"since": since})
        age = time.time() - since
        out.append(caps.Check("hq-blocked", age < BLOCKED_LIMIT, f"blocked {int(age / 60)} min: {now.get('msg') or now.get('kind')}"))
    elif memory:
        store.path("run", "hq-blocked.json").unlink(missing_ok=True)
    used = context_tokens(session)
    if rotate_due(used):
        asked = float(store.read_dict(_asked_file()).get("ts") or 0)
        due = time.time() - asked >= ROTATE_ASK_EVERY
        out.append(caps.Check("hq-context", not due, f"context {against_line(used)}: rotation asked for",
                              (lambda: _ask_to_rotate(used)) if due else None))
    elif used is not None:
        out.append(caps.Check("hq-context", True, f"context {against_line(used)}"))
    return out


def _others_on_this_state() -> list:
    """Any Claude named as HQ but its own, and the old athena-watch mod's watch: `athena hq` from habit starts both on this state. In the lab
    the Mac's real processes aren't this scratch state's, so only a stand-in table is looked at."""
    if lab.active() and not os.environ.get("ATHENA_PS"):
        return []
    own = agents.read_pid(name())
    named = re.compile(rf"(?:^|\s)--name\s+{re.escape(name())}(?:\s|$)")
    found = []
    for line in agents._ps_lines("-axo", "pid=,command="):
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        if named.search(parts[1]) and "claude" in parts[1] and parts[0] != str(own):
            found.append(f"another HQ at pid {parts[0]}")
        elif re.search(r"\bathena watch --tagged\s*$", parts[1]):
            found.append(f"the old athena's watch at pid {parts[0]}")
    return found


def _sandbox_check():
    from athena_rex.kernel import sandbox
    if not sandbox.enabled():
        return caps.Check("signal-sandbox", True, "off: opt-in with rex.signal_sandbox: true (or inside a sandbox already), so Claudes start without the signal sandbox")
    why = sandbox.problem()
    if why:
        return caps.Check("signal-sandbox", False, f"Claudes won't start: {why}")
    return caps.Check("signal-sandbox", True, "each Claude starts where it can't signal what it didn't start")


def checks(ctx):
    others = _others_on_this_state()
    second = [_sandbox_check(), caps.Check("hq-second", not others, f"two systems on one state: {'; '.join(others)}. Exit it: `athena hq` starts these" if others else "only this HQ")]
    record = load()
    now = state(record, alive())
    if now == "running":
        return _running_checks(ctx, record) + second
    if now == "absent":
        return [caps.Check("hq", True, "no HQ on this backend yet")] + second
    return [caps.Check("hq", False, f"{STATES[now]}: Rex runs the sessions, so doctor only says so (`athena-rex hq` opens it again)")] + second


def commands():
    return {"hq": caps.Command(SUMMARY, _configure, _run)}
