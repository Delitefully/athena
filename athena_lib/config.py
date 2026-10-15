"""The user's settings: config.json in athena's state dir, written by `athena init`.

Each key resolves in this order: its environment variable, the file, then a default. A state dir from before
config.json (it has a ledger or hq.json but no config) resolves to LEGACY, the values that install used (the merge
lists from its repo map's merge policy, when it has one), until the first athena command writes them down. From then
on config.json is the single source for the merge rules.
"""
import json
import os
import re
from pathlib import Path

from athena_lib import paths

VERSION = 1

DEFAULTS = {
    "projects_dir": "~",  # where HQ starts, and where `athena init` looks for repos
    "worktree_root": None,  # None: <projects_dir>/.worktrees
    "human_merge_repos": [],  # repos (by folder name) the human merges; athena only prepares them
    "reapproval_repos": [],  # repos where a push after approval needs a new approval
    "model": "opus",
    "effort": "high",
    # athena watch's Slack doorbell (athena_lib/slack.py), on only where the athena-slack Keychain item exists. It
    # always reads the human's DMs and group DMs; these add channels.
    "slack_channels": [],  # channel ids it polls
    "slack_ext_channels": True,  # also every `ext-` channel the human is a member of
    "slack_allow_bots": [],  # bot or app ids whose messages count; other bots never do
    "ci_repos": [],  # repos (paths, or owner/name) whose main athena watch checks for failed CI with no live worker
    # When `<repo> main moved` wakes HQ: "active" (a working or blocked worker, or an unapproved open PR, in that
    # repo), "always" or "never". What it does not tell goes to logs/watch-suppressed.log.
    "watch_main_moved": "active",
}

# Migration only: the values an install from before config.json ran with.
LEGACY = {
    "projects_dir": "~/Developer",
    "worktree_root": "~/Developer/.worktrees",
    "human_merge_repos": [],  # named only in the repo map's merge policy (legacy_values)
    "reapproval_repos": [],
    "model": "opus",
    "effort": "high",
}

ENV = {
    "projects_dir": "ATHENA_HQ_CWD",
    "worktree_root": "ATHENA_WORKTREE_ROOT",
    "model": "ATHENA_MODEL",
    "effort": "ATHENA_EFFORT",
}

PATH_KEYS = ("projects_dir", "worktree_root")
OPTIONAL_KEYS = ("slack_channels", "slack_ext_channels", "slack_allow_bots", "ci_repos", "watch_main_moved")  # written only when set
LIST_KEYS = ("human_merge_repos", "reapproval_repos")


class ConfigError(Exception):
    pass


def config_file() -> Path:
    return Path(os.path.expanduser(os.environ.get("ATHENA_CONFIG") or paths.state_dir() / "config.json"))


def config_dir() -> Path:
    """Holds config.json and the user's own per-repo setup scripts (repos/<name>.setup)."""
    return config_file().parent


def exists() -> bool:
    return config_file().is_file()


def is_legacy() -> bool:
    state = paths.state_dir()
    return not exists() and ((state / "ledger.jsonl").exists() or (state / "hq.json").exists())


# repo-map.md's "# Merge policy" lines, where an install from before config.json kept its merge rules.
MERGE_POLICY = {"human_merge_repos": "human-merged", "reapproval_repos": "re-approval after push"}


def legacy_values() -> dict:
    """LEGACY, with the merge lists from the repo map's merge policy where it has them."""
    values = dict(LEGACY)
    try:
        text = (paths.state_dir() / "repo-map.md").read_text()
    except OSError:
        return values
    for key, label in MERGE_POLICY.items():
        found = re.search(rf"^\s*[-*]?\s*{re.escape(label)}\s*:(.*)$", text, re.I | re.M)
        if found:
            values[key] = repo_names(found.group(1))
    return values


REPO_NAME = re.compile(r"[A-Za-z0-9_.-]*[A-Za-z0-9_]")


def repo_names(text) -> list:
    """`web (acme/web), api (acme/api). Prose after the list.` -> ['web', 'api']: the names only, each validated."""
    text = re.sub(r"\([^)]*\)", " ", text)  # owner/name in parentheses
    text = re.split(r"[.;:](?=\s|$)", text, maxsplit=1)[0]  # the list ends at the first sentence end
    names = []
    for item in text.split(","):
        words = item.split()
        if not words:
            continue
        name = words[0].strip("`'\"").rstrip("/").rsplit("/", 1)[-1]  # a path or owner/name: its last part
        if REPO_NAME.fullmatch(name) and name.lower() != "none" and name not in names:
            names.append(name)
    return names


def _read() -> dict:
    path = config_file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}): fix it, or move it away and run `athena init`")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object: fix it, or move it away and run `athena init`")
    return data


def load() -> dict:
    """The file's values over the defaults (LEGACY for an install from before config.json), without env overrides."""
    if is_legacy():
        return legacy_values()
    data = _read()
    return {k: data.get(k, v) for k, v in DEFAULTS.items()}


def get(key):
    var = ENV.get(key)
    if var and os.environ.get(var):
        return os.environ[var]
    return load().get(key, DEFAULTS[key])  # LEGACY lacks the keys newer than config.json (the Slack doorbell's)


def source(key) -> str:
    var = ENV.get(key)
    if var and os.environ.get(var):
        return f"env {var}"
    if is_legacy():
        return "migrated (no config.json yet)"
    return "config.json" if key in _read() else "default"


def path(key):
    value = get(key)
    if key == "worktree_root" and not value:
        return path("projects_dir") / ".worktrees"
    return Path(os.path.expanduser(value)) if value else None


def listed(key, repo) -> bool:
    """Whether `repo` (a path, or an owner/name slug) is named in list `key`, by its last part, ignoring case."""
    return Path(str(repo)).name.lower() in {str(name).lower() for name in load().get(key) or []}


def _tilde(value):
    if not value:
        return value
    full = os.path.expanduser(str(value))
    for path, home in ((full, os.path.expanduser("~")), (os.path.realpath(full), os.path.realpath(os.path.expanduser("~")))):
        if path == home:
            return "~"
        if path.startswith(home.rstrip("/") + "/"):
            return "~/" + path[len(home.rstrip("/")) + 1:]
    return full


def write(values) -> Path:
    data = {"version": VERSION}
    for key, default in DEFAULTS.items():
        value = values.get(key, default)
        if key in OPTIONAL_KEYS and value == default:
            continue
        data[key] = _tilde(value) if key in PATH_KEYS else value
    target = config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, target)
    _write_root_hint()
    return target


def root_hint_file() -> Path:
    return paths.state_dir() / "worktree-root"


def _write_root_hint():
    """The worktree root from the file, or an older install's (never an env override), on one line:
    hooks/worker-status reads it without Python."""
    data = load() if is_legacy() else _read()
    root = data.get("worktree_root") or os.path.join(os.path.expanduser(data.get("projects_dir") or "~"), ".worktrees")
    hint = root_hint_file()
    hint.parent.mkdir(parents=True, exist_ok=True)
    tmp = hint.with_name(hint.name + ".tmp%d" % os.getpid())
    tmp.write_text(os.path.expanduser(root) + "\n")
    os.replace(tmp, hint)


def sync_root_hint():
    """Write the worktree-root file when it is missing, or older than config.json (edited by hand).

    An install from before config.json gets one too, so its hooks stay in shell even if the config is never written.
    """
    hint = root_hint_file()
    try:
        if is_legacy():
            if not hint.exists():
                _write_root_hint()
            return
        if not exists() or (hint.exists() and hint.stat().st_mtime >= config_file().stat().st_mtime):
            return
        _write_root_hint()
    except (OSError, ConfigError):
        pass


def show() -> dict:
    """Every key's effective value and where it came from, for `athena config show`."""
    values = {}
    for key in DEFAULTS:
        resolved = path(key) if key in PATH_KEYS else get(key)
        values[key] = {"value": str(resolved) if isinstance(resolved, Path) else resolved, "from": source(key)}
    return {"file": str(config_file()), "exists": exists(), "values": values}
