"""Launch one worker: preflight, worktree, trust, its own Rex session running Claude, then its goal."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import agents, budget, bus, caps, config, gitops, ledger, sandbox, store
from athena_rex.kernel.backend import BackendError

SUMMARY = "launch a worker: worktree, its own session, Claude, its goal"
RESERVED = {"dash-queue", "slack-new", "athena", "hq"}
OLD_PLUGIN_OFF = {"athena@athena": False, "keepwarm@keepwarm": False, "keepwarm-bundle@keepwarm": False,
                  "chrome-devtools-mcp@chrome-devtools-plugins": False}
PROMPTS = store.ROOT / "prompts"


class SpawnError(UserError):
    pass


def label_for(name, title=None, linear=None, branch=None) -> str:
    """What a session is called in the app: its title with no ticket id, else its branch's words, else its name."""
    drop = "|".join(re.escape(i) for i in (name, linear) if i)
    source = title or (branch.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ") if branch else "")
    words = [w for w in source.split() if not re.fullmatch(rf"[\[(]?(?:{drop})[\])]?[:·|—–-]?", w, re.I)]
    return (" ".join(words).strip(" :·|—–-") or name)[:48]


def _check_capacity(name, force):
    names = [w["name"] for w in ledger.live()]
    hard = int(config.rex("hard_cap"))
    soft = 0 if force else min(int(config.rex("max_workers")), hard)
    if name in names:
        raise SpawnError(f"{name} is already live")
    if len(names) >= hard:
        raise SpawnError(f"the hard cap is {hard}, and {len(names)} are live")
    if soft and len(names) >= soft:
        raise SpawnError(f"the cap is {soft}, and {len(names)} are live; --force goes up to {hard}")


def claude_env(name, repo) -> dict:
    """What its Claude runs under: its name, and its repo's own git identity if that repo has one, so personal work commits as personal."""
    env = {"ATHENA_WORKER": name}
    identity = config.repo_profile(repo).get("identity") or {}
    if identity.get("name") and identity.get("email"):
        env.update(GIT_AUTHOR_NAME=identity["name"], GIT_COMMITTER_NAME=identity["name"], GIT_AUTHOR_EMAIL=identity["email"], GIT_COMMITTER_EMAIL=identity["email"])
    return env


def settings_for(name, brief, gate=True, ultracode=False, workflow_size=None, repo=None) -> Path:
    env = {"ATHENA_WORKER": name, "ATHENA_HQ": config.hq_name(),
           "ATHENA_STATE_DIR": str(store.state_dir()), "ATHENA_BRIEF": str(brief)}
    if os.environ.get("ATHENA_CONFIG"):
        env["ATHENA_CONFIG"] = os.environ["ATHENA_CONFIG"]
    if not gate:
        env["ATHENA_GATE"] = "off"
    if repo and config.listed("reapproval_repos", repo):
        env["ATHENA_REAPPROVAL"] = "1"
    progress = caps.get("progress")  # the task tools its progress band reads
    env.update(progress.worker_env() if progress else {})
    hooks = caps.get("hooks")
    settings = {"env": env, "crossSessionInbound": "accept", "remoteControlAtStartup": False,
                "enabledPlugins": dict(OLD_PLUGIN_OFF), "hooks": hooks.settings_block() if hooks else {}}
    profiles = caps.get("profiles")
    if profiles:
        settings["permissions"] = profiles.permissions("worker")
    if ultracode:
        settings["ultracode"] = True
        if workflow_size:
            settings["workflowSizeGuideline"] = workflow_size
    target = store.ensure("settings") / f"{name}.json"
    store.write_text(target, json.dumps(settings, indent=2) + "\n")
    return target


def claude_args(name, model, effort, settings, prompt_file=None) -> list:
    args = ["--name", name, "--model", model, "--effort", effort, "--settings", str(settings)]
    progress = caps.get("progress")  # the progress band's mod, for workers alone
    args += progress.launch_args() if progress else []
    return args + ["--append-system-prompt-file", str(prompt_file or PROMPTS / "worker.md")]


GOAL_MAX = 3990  # the old athena's cap; typed in pieces of 200, a goal this long arrives whole (decision 18)


def goal_text(condition, brief) -> str:
    text = " ".join(condition.split())
    if text.startswith("/goal "):
        text = text[len("/goal "):]
    hq = config.hq_name()
    goal = (f"/goal {text} Your brief is {brief}; read it first. End your final message with an ATHENA-REPORT line: that line "
            f"is the report, and the goal is met once it is written, whatever its status (a BLOCKED or "
            f"NEEDS_CONTEXT report with your question meets it too). Merging, approvals, undrafting and checks you "
            f"cannot fix are out of scope. Also try SendMessage to {hq} once; if it fails, don't retry.")
    if len(goal) > GOAL_MAX:
        raise SpawnError(f"the goal is {len(goal)} characters with its clauses; at most {GOAL_MAX}. Move detail to the brief")
    return goal


def with_preferences(text: str) -> str:
    """The brief, with the human's preferences.md added as its PREFERENCES, unless it has its own or there is none."""
    source = store.path("preferences.md")
    if re.search(r"^\s*(?:\*\*)?PREFERENCES\b", text, re.M) or not source.is_file():
        return text
    rules = [line for line in source.read_text().splitlines() if not line.startswith("# ")]
    body = "\n".join(rules).strip()
    heading = ("PREFERENCES (the human's standing preferences; follow them like SCOPE, and where one conflicts with this "
               "brief, the brief wins):")
    return f"{text.rstrip()}\n{heading}\n{body}\n" if body else text


def _run_setup(repo, path, name):
    """The human's own per-repo script beside config.json, in the new worktree. None means this repo has none."""
    script = config.config_file().parent / "repos" / f"{Path(repo).name}.setup"
    if not script.is_file():
        return None
    log = store.ensure("logs") / f"{name}-setup.log"
    with open(log, "w") as output:
        code = subprocess.call(["sh", str(script)], cwd=str(path), stdout=output, stderr=subprocess.STDOUT, timeout=1800,
                               env=dict(os.environ, ATHENA_REPO=str(repo), ATHENA_WORKTREE=str(path)))
    if code:
        raise SpawnError(f"{script.name} exited {code} in {path}; see {log}")
    return str(log)


def _ensure_trust(path) -> dict:
    module = caps.get("trust")
    if not module:
        return {}
    try:
        return module.ensure(path)
    except module.TrustError as exc:
        return {"error": str(exc)}


def _wait_started(backend, record, timeout):
    """Until Claude can take input, or stops at a prompt only the human may answer. Exits early when it dies."""
    begun, deadline = time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = agents.state(backend, record)
        if now["state"] in agents.READY or now["state"] == "working":
            return "ready", None
        if now["state"] == "blocked":
            return "pending", now.get("msg") or "blocked at startup"
        if not now["state"] and time.monotonic() - begun > 20:
            tail = backend.screen(agents.place_of(record))[-300:].strip()
            raise SpawnError(f"Claude did not start: {tail}")
        time.sleep(0.4)
    return "pending", f"not ready after {timeout:.0f}s"


def spawn(ctx, repo, branch, name, brief_path=None, goal=None, prompt=None, linear=None, title=None, model=None,
          effort=None, force=False, setup=True, ultracode=False, workflow_size=None, brief_check=True, gate=True,
          fetch=True, timeout=120.0, system_prompt=None, force_budget=False) -> dict:
    repo = Path(os.path.expanduser(str(repo))).resolve()
    model, effort = model or config.get("model"), effort or config.get("effort")
    if not store.valid_name(name) or name in RESERVED:
        raise SpawnError(f"invalid worker name {name!r}: lowercase letters, digits, - and _ (max 32), not reserved")
    if config.repo_profile(repo).get("outside"):
        raise SpawnError(f"{repo.name} is marked outside athena (rex.repos in config): work on it by hand")
    if workflow_size and not ultracode:
        raise SpawnError("--workflow-size needs --ultracode")
    if goal and not brief_path:
        raise SpawnError("--goal needs --brief")
    elsewhere = store.state_elsewhere()
    if elsewhere:
        raise SpawnError(f"{store.state_dir()} is empty and state lives at {elsewhere}: set ATHENA_STATE_DIR, or move it first")
    if config.unusable():
        raise SpawnError(f"{config.config_file()} does not parse and there is no good copy: fix it first")
    sandbox.require(SpawnError)
    limits = caps.get("limits")
    until = limits.parked() if limits else None
    if until and not force_budget:
        raise SpawnError(f"a usage limit holds until {time.strftime('%H:%M', time.localtime(until))}, so no launch (--force-budget goes past it)")
    missing = ledger.lost()
    if missing:
        raise SpawnError(f"the ledger has lost {', '.join(sorted(missing))}: `athena-rex doctor --fix` restores them. No launch until then")
    skipped = None
    briefs = caps.get("brief")
    if briefs and brief_path:
        found = briefs.problems(brief_path, goal or "")
        if found and brief_check:
            raise SpawnError("the brief or goal would keep the goal evaluator saying not met:\n"
                             + "\n".join(f"- {p}" for p in found) + "\n(--skip-brief-check overrides)")
        skipped = found or None
    if goal and brief_path:
        goal_text(goal, store.path("briefs", f"{name}.md"))  # the path it is sent with: a launch never stops at its length
    held = budget.over(3 if ultracode else 1)
    if held and not force_budget:
        raise SpawnError("this Mac has no room for another worker: " + "; ".join(held) + " (--force-budget goes past it)")
    pre = gitops.preflight(repo, fetch=fetch)
    if pre:
        raise SpawnError(f"preflight failed for {repo}: " + "; ".join(pre))
    path = config.path("worktree_root") / repo.name / gitops.slug(branch)
    if path.exists():
        raise SpawnError(f"{path} already exists; retire the old worker first")
    backend = ctx.backend
    try:
        if not backend.up():
            raise SpawnError(f"the {backend.name} server is not running")
    except BackendError as exc:
        raise SpawnError(f"the {backend.name} server did not answer ({exc}), so no launch")
    label = label_for(name, title, linear, branch)
    record = {"repo": str(repo), "branch": branch, "path": os.path.join(os.path.realpath(path.parent), path.name),
              "linear": linear, "title": title, "model": model, "effort": effort, "backend": backend.name,
              "label": label}
    if ultracode:
        record.update(ultracode=True, workflow_size=workflow_size)
    if skipped:
        record["brief_check_skipped"] = skipped
    if brief_path and briefs:
        record["timebox_s"] = briefs.timebox_seconds(Path(os.path.expanduser(str(brief_path))).read_text())
    with store.locked("locks/spawn.lock"):
        _check_capacity(name, force)
        ledger.append("spawn", name, **record)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitops.add_worktree(repo, branch, path)
        record.update(path=os.path.realpath(path), made_worktree=True)
        ledger.append("update", name, path=record["path"])
        if setup:
            record["setup_log"] = _run_setup(repo, path, name)
        brief = store.ensure("briefs") / f"{name}.md"
        if brief_path:
            store.write_text(brief, with_preferences(Path(os.path.expanduser(str(brief_path))).read_text()))
        settings = settings_for(name, brief, gate, ultracode, workflow_size, repo)
        record["claude_args"] = claude_args(name, model, effort, settings, system_prompt)
        trust = _ensure_trust(path)
        if trust.get("error"):
            raise SpawnError(f"could not mark {path} trusted: {trust['error']}")
        if trust.get("skipped"):
            record["trust_skipped"] = trust["why"]
            ledger.append("update", name, trust_skipped=trust["why"])
        old = store.path("workers", f"{name}.json")
        if old.exists():
            os.replace(old, old.with_name(f"{name}.json.prev"))
        if progress := caps.get("progress"):  # and its band starts from nothing
            progress.fresh(name)
        place = agents.start(backend, name, label, path, record["claude_args"], claude_env(name, repo), repo=repo)
        record.update(space=place.space, window=place.window, block=place.block)
        ledger.append("update", name, space=place.space, window=place.window, block=place.block, claude_args=record["claude_args"],
                      setup_log=record.get("setup_log"))
        outcome, why = _wait_started(backend, {**record, "name": name}, timeout)
    except (SpawnError, BackendError, gitops.GitError, OSError, subprocess.SubprocessError) as exc:
        raise SpawnError(_fail(ctx, name, record, exc))
    text = (goal_text(goal, brief) if goal else prompt) or ""
    if outcome == "pending" and record.get("trust_skipped"):
        why = f"{why}; folder trust was not written: {record['trust_skipped']}"
    if outcome == "pending":
        _goal_waits(name, text or None, pending_reason=why)
        bus.emit("worker.pending", name=name, why=why)
        return {"name": name, **record, "pending": True, "why": why}
    result = {"name": name, **record, "pending": False}
    if text:
        result["prompt"] = _deliver(backend, {**record, "name": name}, text)
    bus.emit("worker.spawned", name=name)
    return result


def _deliver(backend, record, text) -> str:
    """The first goal of a Claude that is ready, on record before anything is typed: a launch killed now leaves a goal that waits or
    says it may have arrived, never one nobody knows of."""
    name = record["name"]
    _goal_waits(name, text)
    out = _send_goal(backend, name, wait_box=15, busy_ok=True)
    if out["outcome"] in ("busy", "not-pending"):
        return "on its way"
    if out["outcome"] != "sent":
        return f"{out['outcome']}: {out.get('state') or out.get('code')}"
    try:
        agents.wait(backend, record, {"working", "blocked"}, timeout=30)
        outcome = "accepted"
    except (agents.Refused, BackendError) as exc:
        outcome = f"unconfirmed: {exc}"
        ledger.append("update", name, prompt=outcome, goal_unconfirmed=text)
        return outcome
    ledger.append("update", name, prompt=outcome)
    return outcome


def _fail(ctx, name, record, exc) -> str:
    ledger.append("failed", name, reason=str(exc), cleaned=False)
    why = None
    try:
        if record.get("space"):
            agents.kill_if_alive(name, grace=0)
            agents.close(ctx.backend, record)
        if record.get("made_worktree"):
            gitops.remove_worktree(record["repo"], record["path"], force=True)
    except (BackendError, gitops.GitError, OSError) as cleanup:
        why = str(cleanup)
    ledger.append("update", name, cleaned=not why, cleanup_error=why, **({"path": None} if not why else {}))
    if why:
        return f"launch of {name} failed: {exc}; its leftovers could not be removed ({why}): athena-rex retire {name}"
    return f"launch of {name} failed: {exc}"


def _goal_lock(name, blocking=True):
    """Every move of a goal, so an amend can't land in the middle of a send and be overwritten by its outcome."""
    return store.locked(f"locks/{name}.goal.lock", blocking=blocking)


def _goal_waits(name, text, **fields):
    """A launch's goal waits, unless an amend got there first, waiting or already sent: that one restates it."""
    with _goal_lock(name):
        worker = ledger.get(name)
        waits = ledger.goal_state(worker) == "none" and not worker.get("goal_amended")
        ledger.append("update", name, **fields, **(ledger.goal_fields("wait", text) if waits else {}))


def deliver_pending(ctx, name) -> dict:
    return _send_goal(ctx.backend, name)


def _send_goal(backend, name, wait_box=0.0, busy_ok=False) -> dict:
    """A waiting goal goes out once, when its Claude can take it. The ledger is told before typing, so a crash can leave one
    unconfirmed, which status and the watch say and `resume --resend` settles, but never sends it twice on its own."""
    with _goal_lock(name, blocking=False) as free:
        if not free:
            return {"name": name, "outcome": "busy"}
        worker = ledger.get(name)
        if not (worker and worker.get("state") == "live" and worker.get("goal_pending")):
            return {"name": name, "outcome": "not-pending"}
        target = {**worker, "name": name}
        try:
            current = agents.state(backend, target)["state"]
        except BackendError as exc:
            current = str(exc)
        queued = (busy_ok or ledger.goal_state(worker) == "amended") and current == "working"  # Claude queues what is typed while it works
        if current not in agents.READY and not queued:
            return {"name": name, "outcome": "waiting", "state": current}
        if not wait_box and worker.get("prompt") == "not sent: box_not_empty" and agents.typing(backend, target):
            return {"name": name, "outcome": "waiting", "state": "box_not_empty"}  # on record once, not two ledger lines a loop
        goal = worker["goal_pending"]
        ledger.append("update", name, **ledger.goal_fields("begin", goal))
        try:
            agents.send(backend, target, goal, wait_box=wait_box)
        except agents.Refused as exc:
            ledger.append("update", name, pending_reason=str(exc), **{**ledger.goal_fields("refused", goal), "prompt": f"not sent: {exc.code}"})
            return {"name": name, "outcome": "waiting", "state": exc.code}
        except BackendError as exc:
            ledger.append("update", name, prompt=f"unconfirmed: {exc.code}", pending_reason=None)
            return {"name": name, "outcome": "unconfirmed", "error": str(exc), "code": exc.code}
        ledger.append("update", name, **ledger.goal_fields("sent"))
        return {"name": name, "outcome": "sent"}


def amend(ctx, name, condition) -> dict:
    """A running worker's goal restated. It goes out when its Claude is next ready, so a busy worker is not interrupted."""
    worker = ledger.get(name)
    if not worker or worker.get("state") != "live":
        raise SpawnError(f"no live worker named {name}")
    if not ledger.managed(worker):
        raise SpawnError(f"{name} is not on this backend; it belongs to the old athena")
    text = goal_text(condition, store.path("briefs", f"{name}.md"))
    with _goal_lock(name):
        ledger.append("update", name, goal_amended=condition, pending_reason="its amended goal waits for it to be ready",
                      **ledger.goal_fields("amend", text, ledger.get(name)))
    bus.emit("worker.amended", name=name)
    return deliver_pending(ctx, name)


def conversation(name, worker):
    hook = store.read_dict(store.path("workers", f"{name}.json"))
    return hook.get("session_id") or worker.get("session")


def _configure(p):
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--brief")
    p.add_argument("--goal", help="the /goal condition; needs --brief")
    p.add_argument("--goal-file", help="the /goal condition from a file")
    p.add_argument("--prompt", help="a plain first prompt instead of a /goal")
    p.add_argument("--linear")
    p.add_argument("--title")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--force", action="store_true", help="go past the soft worker cap")
    p.add_argument("--force-budget", action="store_true", help="launch even though this Mac is short of room")
    p.add_argument("--no-setup", action="store_true")
    p.add_argument("--ultracode", action="store_true")
    p.add_argument("--workflow-size", choices=["small", "medium", "large", "unrestricted"])
    p.add_argument("--skip-brief-check", action="store_true")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--no-gate", action="store_true", help="no report gate for this worker")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--json", action="store_true")


def _say_trust(out):
    if out.get("trust_skipped") and out.get("pending"):
        print(f"folder trust was not written ({out['trust_skipped']}), and its Claude has stopped at a prompt", file=sys.stderr)


def _run(args):
    if args.goal_file:
        if args.goal:
            raise UserError("--goal or --goal-file, not both")
        try:
            args.goal = Path(os.path.expanduser(args.goal_file)).read_text()
        except OSError as exc:
            raise UserError(f"cannot read the goal file {args.goal_file}: {exc}")
    if args.workflow_size and not args.ultracode:
        raise UserError("--workflow-size needs --ultracode")
    out = spawn(caps.CTX, args.repo, args.branch, args.name, args.brief, args.goal, args.prompt, args.linear, args.title,
                args.model, args.effort, args.force, not args.no_setup, args.ultracode, args.workflow_size,
                not args.skip_brief_check, not args.no_gate, not args.no_fetch, args.timeout,
                force_budget=args.force_budget)
    _say_trust(out)
    if args.json:
        print(json.dumps(out, indent=1))
    elif out.get("pending"):
        print(f"{args.name} is waiting at a startup prompt ({out['why']}) in session \"{out['label']}\". "
              f"Answer it there; its goal goes out with: athena-rex resume {args.name}")
    else:
        print(f"{args.name} is running in session \"{out['label']}\" ({out['path']})")
        if str(out.get("prompt", "")).startswith(("waiting", "unconfirmed")):
            print(f"its goal: {out['prompt']}; see athena-rex status {args.name}")
    return 3 if out.get("pending") else 0


def _resume_cfg(p):
    p.add_argument("name")
    p.add_argument("--resend", action="store_true", help="send again a goal that may not have arrived")


def _resume(args):
    worker = ledger.get(args.name)
    if not worker or worker.get("state") != "live":
        raise SpawnError(f"no live worker named {args.name}")
    if args.resend:
        with _goal_lock(args.name):
            goal = ledger.get(args.name).get("goal_unconfirmed")
            if not goal:
                raise SpawnError(f"{args.name} has no unconfirmed goal")
            ledger.append("update", args.name, **ledger.goal_fields("resend", goal))
        out = deliver_pending(caps.CTX, args.name)
    elif worker.get("goal_pending"):
        out = deliver_pending(caps.CTX, args.name)
    else:
        raise SpawnError(f"{args.name} has no goal waiting. Rex runs the sessions: a Claude that is gone is brought back in Rex, or retired")
    print(json.dumps(out))
    return 0 if out.get("outcome") in ("sent", "already running") else 1


def _amend_cfg(p):
    p.add_argument("name")
    p.add_argument("condition", help="the new /goal condition")


def _amend(args):
    out = amend(caps.CTX, args.name, args.condition)
    print(json.dumps(out))
    return 0


def tick(ctx, now):
    from athena_rex.caps.watch import Line
    lines = []
    for worker in ledger.live():
        box = worker.get("timebox_s")
        if box and not worker.get("timebox_told") and now - float(worker.get("spawned") or now) > float(box):
            ledger.append("update", worker["name"], timebox_told=True)
            lines.append(Line(f"{worker['name']} is past its timebox", "quiet", worker["name"]))  # not told to HQ
        if worker.get("goal_pending") and ledger.managed(worker):
            if deliver_pending(ctx, worker["name"]).get("outcome") == "sent":
                lines.append(Line(f"{worker['name']} goal sent", "routine"))
        elif worker.get("goal_unconfirmed") and not worker.get("goal_told"):
            with _goal_lock(worker["name"], blocking=False) as free:  # held means it is on its way, not lost
                current = ledger.get(worker["name"]) if free else {}
                if current.get("goal_unconfirmed") and not current.get("goal_told"):
                    ledger.append("update", worker["name"], goal_told=True)
                    lines.append(Line(f"{worker['name']} goal unconfirmed", "wake", worker["name"]))
    return lines


def commands():
    return {"spawn": caps.Command(SUMMARY, _configure, _run),
            "resume": caps.Command("send a worker's waiting goal (Rex runs the sessions: a gone Claude comes back in Rex)",
                                   _resume_cfg, _resume),
            "amend": caps.Command("restate a running worker's goal", _amend_cfg, _amend)}
