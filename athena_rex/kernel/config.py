"""config.json, shared with the old athena: its keys keep their meaning, and all of ours live under the added `rex` key."""
import os
from pathlib import Path

from athena_rex.kernel import lab, store

DEFAULTS = {
    "projects_dir": "~",
    "worktree_root": None,
    "human_merge_repos": [],
    "reapproval_repos": [],
    "model": "opus",
    "effort": "high",
    "slack_channels": [],
    "slack_allow_bots": [],
    "slack_dm_active_minutes": 15,
    "open_prs_channel": None,
    "ci_repos": [],
    "watch_main_moved": "conflicts",
    "hq_allow": [],
    "hq_skip_permissions": False,
    # HQ's own Claude (never a worker's) starts with `--model <hq_model>`; "" or None: no --model (the user's default).
    "hq_model": "",
    # athena asks HQ to rotate once its context holds this many tokens. Claude Code doesn't compact at 200k: no window is assumed.
    "hq_rotate_tokens": 400000,
}
ENV = {"projects_dir": "ATHENA_HQ_CWD", "worktree_root": "ATHENA_WORKTREE_ROOT", "model": "ATHENA_MODEL",
       "effort": "ATHENA_EFFORT"}
REX_DEFAULTS = {
    "backend": "rex",
    "binary": "/Applications/Rex Beta.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex",
    "server": None,
    "size": "160x48",
    "caps": None,
    "modules": [],
    "modules_dir": "~/.config/athena-rex/modules",
    "dash_port": 2843,
    "max_workers": 4,
    "hard_cap": 8,
    "claude": "claude",
    "scratch_ttl_days": 14,
    "bots": [],
    "ignored_copy_max_mb": 2048,
    "grants": {},
    "hq_context_window": 1000000,
    "caches": {},
    "repos": {},
    "scratchpads": f"/private/tmp/claude-{os.getuid()}",
}
REX_ENV = {"backend": "ATHENA_BACKEND", "binary": "ATHENA_REX_BIN", "server": "ATHENA_REX_SERVER",
           "dash_port": "ATHENA_DASH_PORT", "max_workers": "ATHENA_MAX_WORKERS", "claude": "ATHENA_CLAUDE",
           "scratchpads": "ATHENA_SCRATCHPADS"}

KNOWN_VERSION = 1
_cache = None
_stamp = None
damaged = False


def config_file() -> Path:
    """ATHENA_CONFIG gets the same guard as the state dir: a live config names real roots, and setup scripts sit beside it."""
    target = Path(os.path.expanduser(os.environ.get("ATHENA_CONFIG") or store.state_dir() / "config.json"))
    lab.check_state(target)
    return target


def _mtime(target):
    try:
        return target.stat().st_mtime_ns
    except OSError:
        return None


def load() -> dict:
    """A config that does not parse falls back to the last good copy, never to defaults: a broken file must not quietly
    change what HQ may do. `damaged` says so, and doctor flags it."""
    global _cache, _stamp, damaged
    if _cache is None:
        target = config_file()
        _stamp = _mtime(target)
        data = store.read_json(target, None)
        damaged = False
        if not isinstance(data, dict):
            damaged = target.exists()
            data = store.read_json(_good(), {}) if damaged else {}
        _cache = data if isinstance(data, dict) else {}
    return _cache


def _good():
    return store.path("run", "config.good.json")


def remember_good():
    load()
    if not damaged and config_file().exists():
        store.write_json(_good(), _cache)


def unusable() -> bool:
    """Broken with no good copy to fall back on. Defaults would quietly change what HQ may do, so whoever acts on it refuses."""
    load()
    return damaged and not _good().exists()


def newer_than_known():
    version = load().get("version")
    return version if isinstance(version, int) and version > KNOWN_VERSION else None


def reset():
    global _cache
    _cache = None


def refresh():
    """Read the file again if it changed since it was read: a long-running process sees another's edit."""
    if _cache is not None and _mtime(config_file()) != _stamp:
        reset()


def get(key):
    env = ENV.get(key)
    if env and os.environ.get(env):
        return os.environ[env]
    value = load().get(key)
    return DEFAULTS.get(key) if value is None else value


def path(key) -> Path:
    if key == "worktree_root" and not get(key):
        return path("projects_dir") / ".worktrees"
    return Path(str(get(key))).expanduser()


def rex(key):
    env = REX_ENV.get(key)
    if env and os.environ.get(env):
        value = os.environ[env]
        return int(value) if isinstance(REX_DEFAULTS.get(key), int) else value
    value = (load().get("rex") or {}).get(key)
    return REX_DEFAULTS.get(key) if value is None else value


def hq_name() -> str:
    return os.environ.get("ATHENA_HQ") or "athena"


def listed(key, repo) -> bool:
    """Whether `repo` (a path, or an owner/name slug) is named in list `key`, by its last part, ignoring case. A config that won't parse
    counts as listed in human_merge_repos, so Merge is never offered on a guess."""
    if unusable() and key == "human_merge_repos":
        return True
    return Path(str(repo)).name.lower() in {str(name).lower() for name in get(key) or []}


def repo_profile(repo) -> dict:
    """rex.repos.<repo's folder name>: {"identity": {"name", "email"}, "outside": true}. Work that isn't athena's says so here."""
    found = (rex("repos") or {}).get(Path(str(repo)).name)
    return found if isinstance(found, dict) else {}


def origin(key) -> str:
    if ENV.get(key) and os.environ.get(ENV[key]):
        return f"env {ENV[key]}"
    if key in load():
        return str(config_file())
    return "default"
