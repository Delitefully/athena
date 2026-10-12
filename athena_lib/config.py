"""The user's settings: config.json in athena's state dir, written by `athena init`.

Each key resolves in this order: its environment variable, the file, then a default. A state dir from before
config.json (it has a ledger or hq.json but no config) resolves to LEGACY, the values that install used, until
`athena init` writes them down.
"""
import json
import os
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
    "quota_file": None,  # the herdr-agent-quota plugin's state file; None skips the 5-hour quota gate
}

# Migration only: the values an install from before config.json ran with.
LEGACY = {
    "projects_dir": "~/Developer",
    "worktree_root": "~/Developer/.worktrees",
    "human_merge_repos": ["platform", "infrastructure"],
    "reapproval_repos": ["platform"],
    "model": "opus",
    "effort": "high",
    "quota_file": "~/.local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json",
}

ENV = {
    "projects_dir": "ATHENA_HQ_CWD",
    "worktree_root": "ATHENA_WORKTREE_ROOT",
    "model": "ATHENA_MODEL",
    "effort": "ATHENA_EFFORT",
    "quota_file": "ATHENA_QUOTA_FILE",
}

PATH_KEYS = ("projects_dir", "worktree_root", "quota_file")
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
        return dict(LEGACY)
    data = _read()
    return {k: data.get(k, v) for k, v in DEFAULTS.items()}


def get(key):
    var = ENV.get(key)
    if var and os.environ.get(var):
        return os.environ[var]
    return load()[key]


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
    return Path(str(repo)).name in (load().get(key) or [])


def _tilde(value):
    if not value:
        return value
    full = os.path.expanduser(str(value))
    home = os.path.expanduser("~")
    if full == home:
        return "~"
    if full.startswith(home.rstrip("/") + "/"):
        return "~/" + full[len(home.rstrip("/")) + 1:]
    return full


def write(values) -> Path:
    data = {"version": VERSION}
    for key, default in DEFAULTS.items():
        value = values.get(key, default)
        data[key] = _tilde(value) if key in PATH_KEYS else value
    target = config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, target)
    return target


def show() -> dict:
    """Every key's effective value and where it came from, for `athena config show`."""
    values = {}
    for key in DEFAULTS:
        resolved = path(key) if key in PATH_KEYS else get(key)
        values[key] = {"value": str(resolved) if isinstance(resolved, Path) else resolved, "from": source(key)}
    return {"file": str(config_file()), "exists": exists(), "values": values}
