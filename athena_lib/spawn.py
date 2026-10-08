"""Launch one worker: preflight, worktree space, Claude with its brief and settings, then its /goal."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from athena_lib import gitops, herdr, ledger, paths, quota

HARD_CAP = 5


class SpawnError(Exception):
    pass


def _check_capacity(name, force, ignore_quota=False):
    live = ledger.live()
    if any(w["name"] == name for w in live):
        raise SpawnError(f"a worker named {name} is already live")
    if len(live) >= HARD_CAP:
        raise SpawnError(f"{len(live)} workers are live; the hard cap is {HARD_CAP}")
    cap = paths.max_workers()
    if len(live) >= cap and not force:
        raise SpawnError(f"{len(live)} workers are live; the cap is {cap} (use --force to go up to {HARD_CAP})")
    allowed, why = quota.gate()
    if not allowed and not ignore_quota:
        raise SpawnError(f"not launching: {why}")


def _create_worktree(repo, branch, path, label):
    args = ["worktree", "create", "--cwd", str(repo), "--branch", branch,
            *gitops.choose_base(repo, branch), "--path", str(path), "--label", label, "--no-focus"]
    return herdr.call(*args)


def _run_setup(repo, path, name):
    script = paths.PLUGIN_ROOT / "repos" / f"{Path(repo).name}.setup"
    if not script.exists():
        return None
    log = paths.ensure("logs") / f"{name}-setup.log"
    env = dict(os.environ, ATHENA_REPO=str(repo), ATHENA_WORKTREE=str(path))
    with open(log, "w") as out:
        proc = subprocess.run(["sh", str(script)], cwd=str(path), env=env, stdout=out, stderr=subprocess.STDOUT, timeout=1800)
    if proc.returncode != 0:
        raise SpawnError(f"setup script {script.name} failed (exit {proc.returncode}); log: {log}")
    return str(log)


def _write_settings(name, brief):
    settings = {
        "env": {"ATHENA_WORKER": name, "ATHENA_HQ": paths.hq_name(), "ATHENA_STATE_DIR": str(paths.state_dir()),
                "ATHENA_BRIEF": str(brief)},
        "crossSessionInbound": "accept",
        "enabledPlugins": {"keepwarm@keepwarm": False, "keepwarm-bundle@keepwarm": False},
    }
    target = paths.ensure("settings") / f"{name}.json"
    target.write_text(json.dumps(settings, indent=2) + "\n")
    return target


def build_goal(condition, name, brief):
    text = " ".join(condition.split())
    if text.startswith("/goal "):
        text = text[len("/goal "):]
    suffix = (f" Your brief is {brief}; read it first. Report to the session named {paths.hq_name()} with SendMessage, "
              f"and end your final message with a ATHENA-REPORT line.")
    return ("/goal " + text + suffix)[:3990]


def claude_args(name, model, effort, settings):
    args = ["--name", name, "--model", model, "--effort", effort,
            "--append-system-prompt-file", str(paths.PLUGIN_ROOT / "worker.md"), "--settings", str(settings)]
    plugin_dir = os.environ.get("ATHENA_PLUGIN_DIR")
    if plugin_dir:
        args += ["--plugin-dir", plugin_dir]
    return args


def spawn(repo, branch, name, brief_path, goal, linear=None, title=None, model="opus", effort="high",
          force=False, setup=True, ignore_quota=False):
    repo = Path(os.path.expanduser(str(repo))).resolve()
    if not paths.valid_name(name):
        raise SpawnError(f"invalid worker name {name!r}: use lowercase letters, digits, - and _ (max 32)")
    _check_capacity(name, force, ignore_quota)
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
    label = f"{name} {title}".strip()[:48] if title else name
    record = {"repo": str(repo), "branch": branch, "path": os.path.realpath(path), "linear": linear,
              "title": title, "model": model, "effort": effort}
    claim.write_text(name + "\n")
    try:
        created = _create_worktree(repo, branch, path, label)
        record.update(workspace=created["workspace"]["workspace_id"], pane=created["root_pane"]["pane_id"],
                      terminal=created["root_pane"]["terminal_id"])
        if setup:
            record["setup_log"] = _run_setup(repo, path, name)
        brief = paths.ensure("briefs") / f"{name}.md"
        shutil.copyfile(brief_path, brief)
        settings = _write_settings(name, brief)
        herdr.call("agent", "start", name, "--kind", "claude", "--pane", record["pane"], "--timeout", "120000",
                   "--", *claude_args(name, model, effort, settings), timeout=200)
    except (herdr.HerdrError, gitops.GitError, SpawnError, OSError, KeyError, subprocess.SubprocessError) as exc:
        claim.unlink(missing_ok=True)
        ledger.append("failed", name, reason=str(exc), **record)
        raise SpawnError(f"launch of {name} failed: {exc}") from exc
    claim.unlink(missing_ok=True)
    ledger.append("spawn", name, **record)
    goal_text = build_goal(goal, name, brief)
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
    tokens = ["--token", "athena=worker"] + (["--token", f"linear={linear}"] if linear else [])
    try:
        herdr.call("workspace", "report-metadata", record["workspace"], "--source", "athena", *tokens)
    except herdr.HerdrError:
        pass
    ledger.append("update", name, prompt=record["prompt"])
    return {"name": name, **record}
