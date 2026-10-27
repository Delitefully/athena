"""Launch one worker: preflight, worktree space, Claude with its brief and settings, then its /goal."""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path

from athena_lib import brief as briefs, config, gitops, herdr, ledger, paths, retire

HARD_CAP = paths.HARD_CAP
STARTUP_BLOCKED = {"agent_not_ready", "agent_blocked"}
NOT_SENT = {"agent_blocked", "agent_not_ready"}  # herdr rejects these before typing anything
READY = {"idle", "done"}  # herdr: both mean ready for input; which one depends on seen state
SESSION_TRIES = 3  # asks for the session id to register the resume command, a second apart


class SpawnError(Exception):
    pass


def _check_capacity(name, force):
    live = ledger.live()
    if any(w["name"] == name for w in live):
        raise SpawnError(f"a worker named {name} is already live")
    if len(live) >= HARD_CAP:
        raise SpawnError(f"{len(live)} workers are live; the hard cap is {HARD_CAP}")
    cap = paths.max_workers()
    if len(live) >= cap and not force:
        raise SpawnError(f"{len(live)} workers are live; the cap is {cap} (use --force to go up to {HARD_CAP})")


def _drop_ids(text, ids):
    for i in ids:
        text = re.sub(rf"[\[(]?\b{re.escape(i)}\b[\])]?\s*[:·|—–-]?\s*", " ", text, flags=re.I)
    return " ".join(text.split()).strip(" :·|—–-")


def space_label(name, title=None, linear=None, branch=None):
    """The herdr space label: the title, never the ticket id.

    herdr already shows the Linear id (the linear metadata token) under the label,
    so an id in the label would show it twice. Without a title, the branch slug
    stands in; the worker name is the last resort.
    """
    ids = [i for i in {name, linear} if i]
    label = _drop_ids(title or "", ids)
    if not label and branch:
        slug = branch.rsplit("/", 1)[-1]
        for i in ids:
            slug = re.sub(rf"^{re.escape(i)}[-_]?", "", slug, flags=re.I)
        label = _drop_ids(slug.replace("-", " ").replace("_", " "), ids)
    return (label or name)[:48]


def _create_worktree(repo, branch, path, label):
    args = ["worktree", "create", "--cwd", str(repo), "--branch", branch,
            *gitops.choose_base(repo, branch), "--path", str(path), "--label", label, "--no-focus"]
    return herdr.call(*args)


def setup_script(repo):
    """repos/<name>.setup in the user's config dir, else in the plugin (which ships only example.setup)."""
    name = Path(repo).name
    for root in (config.config_dir(), paths.PLUGIN_ROOT):
        script = root / "repos" / f"{name}.setup"
        if script.is_file() and not (root == paths.PLUGIN_ROOT and name == "example"):
            return script
    return None


def _run_setup(repo, path, name):
    script = setup_script(repo)
    if not script:
        return None
    log = paths.ensure("logs") / f"{name}-setup.log"
    # Without HQ's FORCE_HYPERLINK, so the script's tools write no OSC 8 links into its log.
    env = {k: v for k, v in os.environ.items() if k != "FORCE_HYPERLINK"}
    env.update(ATHENA_REPO=str(repo), ATHENA_WORKTREE=str(path))
    with open(log, "w") as out:
        proc = subprocess.run(["sh", str(script)], cwd=str(path), env=env, stdout=out, stderr=subprocess.STDOUT, timeout=1800)
    if proc.returncode != 0:
        raise SpawnError(f"setup script {script.name} failed (exit {proc.returncode}); log: {log}")
    return str(log)


def _write_settings(name, brief, ultracode=False, workflow_size=None, repo=None):
    settings = {
        "env": {"ATHENA_WORKER": name, "ATHENA_HQ": paths.hq_name(), "ATHENA_STATE_DIR": str(paths.state_dir()),
                "ATHENA_BRIEF": str(brief)},
        "crossSessionInbound": "accept",
        # Remote Control is HQ's alone; --settings outranks the user's remoteControlAtStartup.
        "remoteControlAtStartup": False,
        "enabledPlugins": {"keepwarm@keepwarm": False, "keepwarm-bundle@keepwarm": False},
    }
    if repo and config.listed("reapproval_repos", repo):  # worker.md: say a push after approval needs a new one
        settings["env"]["ATHENA_REAPPROVAL"] = "1"
    if ultracode:  # the official settings key; the keyword in a prompt or file would not opt in
        settings["ultracode"] = True
        if workflow_size:
            settings["workflowSizeGuideline"] = workflow_size
    target = paths.ensure("settings") / f"{name}.json"
    target.write_text(json.dumps(settings, indent=2) + "\n")
    return target


def preferences_file() -> Path:
    """The human's standing design and build preferences, one per line (docs/configuration.md#preferences)."""
    return paths.state_dir() / "preferences.md"


def with_preferences(text) -> str:
    """The brief with the preferences file appended as its PREFERENCES field, unless it has one already or there is
    no file. The file's own headings and blank lines stay; its first-level title does not."""
    if "PREFERENCES" in briefs.parse(text):
        return text
    try:
        prefs = preferences_file().read_text()
    except OSError:
        return text
    rules = "\n".join(line for line in prefs.splitlines() if not line.startswith("# ")).strip()
    if not rules:
        return text
    return (text.rstrip("\n") + f"\nPREFERENCES (the human's standing preferences, from {preferences_file()}; "
            f"follow them like SCOPE, and where one conflicts with this brief, the brief wins):\n{rules}\n")


def build_goal(condition, name, brief):
    text = " ".join(condition.split())
    if text.startswith("/goal "):
        text = text[len("/goal "):]
    # The ATHENA-REPORT line is the report athena reads. SendMessage is a courtesy: when HQ isn't reachable
    # (a restarted or re-logged HQ), a goal that required it would never be met and the worker would loop.
    # Any status meets it: a BLOCKED report with a question ends the turn cleanly instead of looping until someone
    # clears the goal, and what happens after DONE is not the worker's to reach.
    suffix = (f" Your brief is {brief}; read it first. End your final message with an ATHENA-REPORT line: that line "
              f"is the report, and the goal is met once it is written, whatever its status (a BLOCKED or "
              f"NEEDS_CONTEXT report with your question meets it too). Merging, approvals, undrafting and checks you "
              f"cannot fix are out of scope. Also try SendMessage to {paths.hq_name()} once; if it fails, don't "
              f"retry.")
    return ("/goal " + text + suffix)[:3990]


def claude_args(name, model, effort, settings):
    args = ["--name", name, "--model", model, "--effort", effort,
            "--append-system-prompt-file", str(paths.PLUGIN_ROOT / "worker.md"), "--settings", str(settings)]
    plugin_dir = os.environ.get("ATHENA_PLUGIN_DIR")
    if plugin_dir:
        args += ["--plugin-dir", plugin_dir]
    return args


def brief_problems(brief_path, goal) -> list:
    """What athena brief check finds in a brief and goal (athena_lib/brief.py)."""
    try:
        text = Path(os.path.expanduser(str(brief_path))).read_text()
    except OSError as exc:
        raise SpawnError(f"cannot read the brief {brief_path}: {exc}")
    return briefs.check(text) + briefs.check_goal(goal)


def check_brief(brief_path, goal):
    """Refuse a brief or goal whose wording the goal evaluator could never call met, naming each rule and line."""
    found = brief_problems(brief_path, goal)
    if found:
        lines = "\n".join("- " + briefs.describe(p) for p in found)
        raise SpawnError(f"the brief or goal would keep the goal evaluator saying not met:\n{lines}\n"
                         f"Fix the brief (athena brief check {brief_path}), or pass --skip-brief-check where a check is "
                         f"wrong for this brief (the ledger records the skip).")


def spawn(repo, branch, name, brief_path, goal, linear=None, title=None, model=None, effort=None,
          force=False, setup=True, ultracode=False, workflow_size=None, brief_check=True):
    repo = Path(os.path.expanduser(str(repo))).resolve()
    model, effort = model or config.get("model"), effort or config.get("effort")
    if not paths.valid_name(name):
        raise SpawnError(f"invalid worker name {name!r}: use lowercase letters, digits, - and _ (max 32)")
    if name in paths.RESERVED_NAMES:
        raise SpawnError(f"{name!r} is reserved: athena watch prints lines that start with it")
    if workflow_size and not ultracode:
        raise SpawnError("--workflow-size needs --ultracode")
    _check_capacity(name, force)
    skipped = None
    if brief_check:
        check_brief(brief_path, goal)
    else:  # kept on the spawn record: which checks a launch went past
        skipped = [briefs.describe(p) for p in brief_problems(brief_path, goal)]
    ledger.touch()
    pre = gitops.preflight(repo)
    if not pre["ok"]:
        raise SpawnError(f"preflight failed for {repo}: " + "; ".join(pre["problems"]))
    path = paths.worktree_path(repo, branch)
    if path.exists():
        raise SpawnError(f"{path} already exists; open it with herdr worktree open, or retire the old worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = paths.claim_file(str(path))
    claim.parent.mkdir(parents=True, exist_ok=True)
    label = space_label(name, title, linear, branch)
    record = {"repo": str(repo), "branch": branch, "path": os.path.realpath(path), "linear": linear,
              "title": title, "model": model, "effort": effort}
    if ultracode:
        record["ultracode"] = True
        if workflow_size:
            record["workflow_size"] = workflow_size
    if skipped is not None:
        record["brief_check_skipped"] = skipped
    claim.write_text(name + "\n")
    pending = None
    try:
        created = _create_worktree(repo, branch, path, label)
        record.update(workspace=created["workspace"]["workspace_id"], pane=created["root_pane"]["pane_id"],
                      terminal=created["root_pane"]["terminal_id"])
        if setup:
            record["setup_log"] = _run_setup(repo, path, name)
        brief = paths.ensure("briefs") / f"{name}.md"
        brief.write_text(with_preferences(Path(os.path.expanduser(str(brief_path))).read_text()))
        settings = _write_settings(name, brief, ultracode, workflow_size, repo)
        record["claude_args"] = claude_args(name, model, effort, settings)
        _set_aside_status(name)
        try:
            herdr.call("agent", "start", name, "--kind", "claude", "--pane", record["pane"], "--timeout", "120000",
                       "--", *record["claude_args"], timeout=200)
            record.update(_register_resume(record["pane"], record["claude_args"]))
        except herdr.HerdrError as exc:
            if not _blocked_at_startup(name, record["pane"], exc):
                raise
            pending = str(exc)
    except (herdr.HerdrError, gitops.GitError, SpawnError, OSError, KeyError, subprocess.SubprocessError) as exc:
        claim.unlink(missing_ok=True)
        raise SpawnError(_fail(name, record, exc)) from exc
    claim.unlink(missing_ok=True)
    goal_text = build_goal(goal, name, brief)
    if pending:
        ledger.append("spawn", name, goal_pending=goal_text, pending_reason=pending, **record)
        _report_metadata(record["workspace"], linear)
        screen = _screen(record["pane"])
        return {"name": name, **record, "pending": True, "label": label, "screen": screen,
                "message": pending_message(name, label, record, screen)}
    ledger.append("spawn", name, **record)
    try:
        herdr.call("agent", "prompt", name, goal_text)
    except herdr.HerdrError as exc:
        ledger.append("update", name, prompt=f"failed: {exc.code}")
        raise SpawnError(f"{name} is running but its goal was not sent ({exc}); resend with: athena nudge {name} '<goal>'")
    try:
        herdr.call("agent", "wait", name, "--until", "working", "--until", "blocked", "--timeout", "30000", timeout=60)
        record["prompt"] = "accepted"
    except herdr.HerdrError as exc:
        record["prompt"] = f"unconfirmed: {exc.code}"
    _report_metadata(record["workspace"], linear)
    ledger.append("update", name, prompt=record["prompt"])
    return {"name": name, **record}


def _set_aside_status(name):
    """A worker spawned again under its old name starts with no status: its last life's workers/<name>.json (a DONE
    report, a block count) moves to <name>.json.prev before the new Claude's first hook event."""
    old = paths.state_dir() / "workers" / f"{name}.json"
    try:
        os.replace(old, old.with_name(f"{name}.json.prev"))
    except FileNotFoundError:
        pass


def _report_metadata(workspace, linear):
    tokens = ["--token", "athena=worker"] + (["--token", f"linear={linear}"] if linear else [])
    try:
        herdr.call("workspace", "report-metadata", workspace, "--source", "athena", *tokens)
    except herdr.HerdrError:
        pass


def _register_resume(pane, args) -> dict:
    """Give herdr the worker's whole resume command. Never fails the spawn: the worker's SessionStart hook tries again.

    Before Claude is past its startup prompts herdr knows no session yet; the hook registers it then.
    """
    sid = None
    for attempt in range(SESSION_TRIES):  # herdr's own Claude hook reports the session, maybe just after start
        if attempt:
            time.sleep(float(os.environ.get("ATHENA_SESSION_WAIT") or 1))
        try:
            session = herdr.call("agent", "get", pane, timeout=10).get("agent", {}).get("agent_session") or {}
        except herdr.HerdrError:
            return {}
        sid = session.get("value") if session.get("kind", "id") == "id" else None
        if sid:
            break
    if not sid:
        return {}
    try:
        herdr.register_resume(pane, sid, args)
    except herdr.HerdrError as exc:
        return {"session": sid, "resume": f"failed: {exc.code}"}
    return {"session": sid, "resume": "registered"}


def _agent(name, pane):
    """(status, problem) of the agent in the worker's pane. A different or missing agent is a problem, never a target."""
    try:
        agent = herdr.call("agent", "get", pane, timeout=10).get("agent", {})
    except herdr.HerdrError as exc:
        if exc.code == "agent_not_found":
            return None, f"no agent named {name} in pane {pane} ({exc.code})"
        return None, None
    if agent.get("name") != name or agent.get("pane_id") != pane:
        found = agent.get("name") or "an unnamed agent"
        return None, f"no agent named {name} in pane {pane} (found {found} in {agent.get('pane_id')})"
    return agent.get("agent_status"), None


def _agent_status(name, pane):
    return _agent(name, pane)[0]


def _blocked_at_startup(name, pane, exc) -> bool:
    """herdr says agent_not_ready when Claude stops at a startup prompt; for other errors, ask herdr."""
    return exc.code in STARTUP_BLOCKED or _agent_status(name, pane) == "blocked"


def _fail(name, record, exc) -> str:
    """Record a failed launch, then remove what it made. Returns the error message."""
    ledger.append("failed", name, reason=str(exc), cleaned=False, **record)
    try:
        removed = retire.remove_worktree(record, force=True)
        why = removed.get("herdr_error")  # herdr kept our space open: its Claude may still run there
        made = record.get("workspace") or os.path.isdir(record.get("path") or "")
        cleaned = not why and (removed["removed"] or not made)
    except (gitops.GitError, OSError, subprocess.SubprocessError) as cleanup_exc:
        cleaned, why = False, str(cleanup_exc)
    ledger.append("update", name, cleaned=cleaned, cleanup_error=why if not cleaned else None)
    if cleaned:
        return f"launch of {name} failed: {exc}"
    return f"launch of {name} failed: {exc}; its space could not be removed ({why}): athena retire {name}"


def _screen(pane) -> str:
    try:
        return herdr.text("agent", "read", pane, "--source", "visible", "--lines", "40", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""


def pending_message(name, label, record, screen="") -> str:
    lines = [line.strip() for line in (screen or "").splitlines() if line.strip()]
    if "trust" in (screen or "").lower():
        what = f"Claude asks whether you trust the folder {record.get('path')}"
    elif lines:
        what = f"Claude shows: {lines[-1][:120]}"
    else:
        what = "Claude is waiting at a startup prompt"
    return (f"{name} is waiting at a startup prompt. {what}. Open the space \"{label}\" (pane {record.get('pane')}, "
            f"or its board column) and answer it yourself; athena never answers it. Its goal is sent once Claude "
            f"is ready, by athena watch, or by hand with: athena resume {name}")


@contextlib.contextmanager
def _goal_lock(name):
    """One sender per worker: watch processes and resume may race."""
    f = open(paths.ensure("locks") / f"{name}.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        f.close()


def deliver(name) -> dict:
    """Send a worker's pending goal once its Claude is ready, at most once, to its own pane only.

    Outcomes: sent, waiting (not ready yet), missing (no agent of this worker's in its pane),
    unconfirmed (herdr may have typed it; never resent), not-pending, busy (another sender holds the lock).
    """
    with _goal_lock(name) as locked:
        if not locked:
            return {"name": name, "outcome": "busy"}
        w = ledger.workers().get(name)
        if not w or w.get("state") != "live" or not w.get("goal_pending"):
            return {"name": name, "outcome": "not-pending"}
        pane = w.get("pane")
        agent_status, problem = _agent(name, pane) if pane else (None, f"{name} has no pane")
        if problem:
            return {"name": name, "outcome": "missing", "problem": problem}
        if agent_status not in READY:
            return {"name": name, "outcome": "waiting", "agent_status": agent_status}
        goal = w["goal_pending"]
        ledger.append("update", name, goal_pending=None, prompt="sending", goal_send="deliver")  # written first: a crash cannot resend
        try:
            herdr.call("agent", "prompt", pane, goal)
        except herdr.HerdrError as exc:
            if exc.code in NOT_SENT:
                ledger.append("update", name, goal_pending=goal, prompt=f"not sent: {exc.code}")
                return {"name": name, "outcome": "waiting", "agent_status": exc.code}
            ledger.append("update", name, prompt=f"unconfirmed: {exc.code}", pending_reason=None)
            return {"name": name, "outcome": "unconfirmed", "error": str(exc)}
        ledger.append("update", name, prompt="sent", pending_reason=None)
        return {"name": name, "outcome": "sent"}


def deliver_pending() -> list:
    return [deliver(w["name"]) for w in ledger.live() if w.get("goal_pending")]


def resume(name) -> dict:
    """Send a pending goal by hand, once the human has answered the startup prompt."""
    result = deliver(name)
    outcome = result["outcome"]
    if outcome == "not-pending":
        raise SpawnError(f"{name} has no pending goal (not a live worker, or its goal was already sent)")
    if outcome == "waiting":
        raise SpawnError(f"{name} is not ready for its goal (herdr: {result.get('agent_status')}); if it shows a "
                         f"startup prompt, the human answers it in its space; then retry")
    if outcome == "missing":
        raise SpawnError(f"{result['problem']}: athena sends the goal only to the agent it started. If Claude was "
                         f"restarted there by hand, retire {name} and spawn it again")
    if outcome == "busy":
        raise SpawnError(f"another athena process is sending {name}'s goal right now")
    return result
