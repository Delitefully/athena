"""`athena init`: check what athena needs, ask for the user's folders and merge rules, write config.json.

Interactive by default; `--yes` takes each value from its flag, else the current config, else the default.
herdr is installed only after the user says yes (or passes --install-herdr), never silently.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from athena_lib import config, paths

HERDR_INSTALL = "curl -fsSL https://herdr.dev/install.sh | sh"  # https://herdr.dev/docs/install/
HERDR_MIN = (0, 9, 1)  # the oldest herdr athena ran against end to end (tests/lab/README.md)
OK, FAIL, OPTIONAL = "✓", "✗", "–"
REQUIRED = ("herdr", "claude", "athena plugin")  # without these athena cannot start HQ


def _run(argv, timeout=30):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return proc.returncode, (proc.stdout or "").strip()


def _check(name, ok, detail, fix=None):
    return {"name": name, "mark": OK if ok else FAIL, "detail": detail, "fix": fix}


def _version(text):
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in found.groups()) if found else None


def install_command():
    return os.environ.get("ATHENA_HERDR_INSTALL") or HERDR_INSTALL


def check_herdr():
    argv = shlex.split(paths.herdr_bin())
    if not shutil.which(argv[0]):
        return _check("herdr", False, "not found",
                      f"install it: {HERDR_INSTALL} (or brew install herdr; see https://herdr.dev/docs/install/)")
    code, out = _run(argv + ["--version"])
    found = _version(out)
    want = ".".join(map(str, HERDR_MIN))
    if code != 0 or not found:
        return _check("herdr", False, f"`{' '.join(argv)} --version` failed", "reinstall herdr: " + HERDR_INSTALL)
    shown = ".".join(map(str, found))
    if found < HERDR_MIN:
        return {**_check("herdr", False, "", "herdr update (or brew upgrade herdr), then herdr server stop"),
                "line": f"herdr {shown} is older than {want}"}
    return {**_check("herdr", True, ""), "line": f"herdr {shown}"}


def claude_bin():
    found = shutil.which("claude")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/claude")
    return local if os.access(local, os.X_OK) else None


def check_claude():
    claude = claude_bin()
    if not claude:
        return _check("claude", False, "not found", "install Claude Code: https://claude.com/claude-code")
    code, out = _run([claude, "--version"])
    return _check("claude", code == 0, out.split("\n")[0] if code == 0 else "`claude --version` failed",
                  None if code == 0 else "reinstall Claude Code")


def check_plugin():
    if os.environ.get("ATHENA_PLUGIN_DIR"):
        return _check("athena plugin", True, f"loaded from {os.environ['ATHENA_PLUGIN_DIR']}")
    fix = f"claude plugin marketplace add {paths.PLUGIN_ROOT} && claude plugin install athena@athena"
    claude = claude_bin()
    if not claude:
        return _check("athena plugin", False, "cannot check without claude", fix)
    code, out = _run([claude, "plugin", "list", "--json"], timeout=60)
    try:
        plugins = json.loads(out) if code == 0 else []
    except ValueError:
        plugins = []
    found = [p for p in plugins if isinstance(p, dict) and str(p.get("id", "")).startswith("athena@")]
    if not found:
        return _check("athena plugin", False, "not installed", fix)
    if found[0].get("enabled") is False:
        return _check("athena plugin", False, "installed but disabled", f"claude plugin enable {found[0]['id']}")
    return _check("athena plugin", True, found[0]["id"])


def check_gh():
    if not shutil.which("gh"):
        return _check("gh", False, "not found", "install the GitHub CLI (https://cli.github.com), then gh auth login")
    code, _ = _run(["gh", "auth", "status"])
    return _check("gh", code == 0, "signed in" if code == 0 else "not signed in", None if code == 0 else "gh auth login")


def check_git():
    name = _run(["git", "config", "--global", "--get", "user.name"])
    email = _run(["git", "config", "--global", "--get", "user.email"])
    if name[0] == 0 and email[0] == 0 and name[1] and email[1]:
        return _check("git identity", True, f"{name[1]} <{email[1]}>")
    return _check("git identity", False, "no global user.name or user.email",
                  'git config --global user.name "Your Name" && git config --global user.email you@example.com')


# herdr plugins athena can use. None is required.
HERDR_PLUGINS = {
    "herdr-agent-quota": "the 5-hour quota gate in athena spawn; without it the gate is skipped",
}


def check_herdr_plugins():
    argv = shlex.split(paths.herdr_bin())
    installed = set()
    if shutil.which(argv[0]):
        code, out = _run(argv + ["plugin", "list", "--json"])
        try:
            data = json.loads(out) if code == 0 else {}
            installed = {p.get("plugin_id") for p in (data.get("result") or {}).get("plugins") or []}
        except (ValueError, AttributeError):
            pass
    return [{"name": name, "mark": OK if name in installed else OPTIONAL, "optional": True,
             "detail": why} for name, why in HERDR_PLUGINS.items()]


def checks():
    return [check_herdr(), check_claude(), check_plugin(), check_gh(), check_git(), *check_herdr_plugins()]


def render(check):
    line = check.get("line") or (check["name"] + (f": {check['detail']}" if check.get("detail") else ""))
    if check.get("optional"):
        line = f"{check['name']} (optional): {'installed' if check['mark'] == OK else 'not installed'}; {check['detail']}"
    text = f"  {check['mark']} {line}"
    if check["mark"] == FAIL and check.get("fix"):
        text += f"\n      fix: {check['fix']}"
    return text


def quota_guess():
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    found = Path(state) / "herdr" / "plugins" / "herdr-agent-quota" / "claude-statusline.json"
    return str(found) if found.exists() else None


def suggested_projects():
    """The folder holding the repo you stand in (its parent), else ~/code."""
    code, top = _run(["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"])
    if code == 0 and top:
        return config._tilde(str(Path(top).parent))
    return "~/code"


def starting_values():
    """What the prompts offer: the config, today's values for an install from before config.json, else defaults."""
    if config.exists() or config.is_legacy():
        values = config.load()
    else:
        values = dict(config.DEFAULTS, projects_dir=suggested_projects(), quota_file=quota_guess())
    if not values.get("worktree_root"):
        values["worktree_root"] = None
    return values


def _split(text):
    return [part.strip() for part in re.split(r"[,\s]+", text or "") if part.strip()]


PROMPTS = [
    ("projects_dir", "Projects folder (where your repos live; HQ starts here)"),
    ("worktree_root", "Worktree root (one git worktree per worker)"),
    ("human_merge_repos", "Repos you merge yourself (folder names, comma-separated; athena only prepares them)"),
    ("reapproval_repos", "Repos where a push after approval needs a new approval"),
    ("model", "Worker model"),
    ("effort", "Worker effort"),
    ("quota_file", "herdr-agent-quota state file (empty: no quota gate)"),
]


def _shown(value):
    if isinstance(value, list):
        return ", ".join(value) or "none"
    return config._tilde(value) if value else "none"


def _beside(projects):
    return config._tilde(str(Path(os.path.expanduser(projects)) / ".worktrees"))


def collect(values, flags, interactive, ask):
    # A worktree root left at <projects>/.worktrees follows a new projects folder.
    follows = not values.get("worktree_root") or config._tilde(values["worktree_root"]) == _beside(values["projects_dir"])
    for key, question in PROMPTS:
        if key == "worktree_root" and follows:
            values[key] = _beside(values["projects_dir"])
        if flags.get(key) is not None:
            given = flags[key]
            values[key] = _split(given) if key in config.LIST_KEYS else given
            continue
        if not interactive:
            continue
        answer = ask(f"{question} [{_shown(values.get(key))}]: ").strip()
        if not answer:
            continue
        if key in config.LIST_KEYS:
            values[key] = [] if answer.lower() == "none" else _split(answer)
        elif key == "quota_file" and answer.lower() == "none":
            values[key] = None
        else:
            values[key] = answer
    return values


def _deleted_setups(root):
    """repos/<name>.setup files the plugin's git history deleted, newest first: {name: content}."""
    code, out = _run(["git", "-C", str(root), "log", "--no-renames", "--diff-filter=D", "--name-only",
                      "--format=@%H", "--", "repos"])
    found, commit = {}, None
    for line in (out.splitlines() if code == 0 else []):
        if line.startswith("@"):
            commit = line[1:]
        elif line.endswith(".setup") and commit and Path(line).name not in found:
            got = _run(["git", "-C", str(root), "show", f"{commit}^:{line}"])
            if got[0] == 0:
                found[Path(line).name] = got[1] + "\n"
    return found


def migrate_setups():
    """Move the per-repo setup scripts an older install kept in the plugin into the user's config dir."""
    dest = config.config_dir() / "repos"
    scripts = {p.name: p.read_text() for p in sorted((paths.PLUGIN_ROOT / "repos").glob("*.setup"))}
    for name, text in _deleted_setups(paths.PLUGIN_ROOT).items():
        scripts.setdefault(name, text)
    moved = []
    for name, text in scripts.items():
        if name == "example.setup" or (dest / name).exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(text)
        moved.append(dest / name)
    return moved


def parser():
    p = argparse.ArgumentParser(prog="athena init", description="Check what athena needs and write its config.")
    p.add_argument("--yes", "-y", action="store_true", help="ask nothing: flags, else the current config, else defaults")
    p.add_argument("--projects", help="your projects folder (HQ starts here)")
    p.add_argument("--worktrees", help="the worktree root (default <projects>/.worktrees)")
    p.add_argument("--human-merge", help="repos you merge yourself, comma-separated")
    p.add_argument("--reapproval", help="repos where a push after approval needs a new approval")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--quota-file")
    p.add_argument("--install-herdr", action="store_true", help="with --yes: install herdr if it is missing")
    return p


def main(argv=None, ask=input) -> int:
    a = parser().parse_args(argv)
    interactive = not a.yes
    legacy = config.is_legacy()
    print("athena init: checking what athena needs")
    found = checks()
    herdr_check = found[0]
    if herdr_check["mark"] == FAIL and herdr_check["detail"] == "not found":
        print(render(herdr_check))
        cmd = install_command()
        go = a.install_herdr or (interactive and ask(f"Install herdr now with `{cmd}`? [y/N] ").strip().lower()
                                 in ("y", "yes"))
        if go:
            print(f"running: {cmd}")
            subprocess.run(cmd, shell=True)
            found[0] = check_herdr()
            if found[0]["mark"] == FAIL:
                found[0]["fix"] = "herdr was installed but is not on PATH: open a new terminal, or add its folder to PATH"
    for check in found:
        print(render(check))
    flags = {"projects_dir": a.projects, "worktree_root": a.worktrees, "human_merge_repos": a.human_merge,
             "reapproval_repos": a.reapproval, "model": a.model, "effort": a.effort, "quota_file": a.quota_file}
    if legacy:
        print("found an install from before config.json: offering its current values, so nothing changes")
    values = collect(starting_values(), flags, interactive, ask)
    target = config.write(values)
    print(f"wrote {target}")
    projects = config.path("projects_dir")
    if projects and not projects.exists():
        projects.mkdir(parents=True)
        print(f"created {projects}")
    if legacy:
        for moved in migrate_setups():
            print(f"moved setup script {moved.name} to {moved}")
    failed = [c["name"] for c in found if c["mark"] == FAIL and c["name"] in REQUIRED]
    if failed:
        print(f"athena cannot start until these are fixed: {', '.join(failed)}")
        return 1
    print("next: run `athena` to open HQ (it starts herdr if needed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
