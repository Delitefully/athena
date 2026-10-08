"""Where athena keeps things. Every location can be moved with an environment variable."""
import hashlib
import os
import re
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _env_path(var, default):
    return Path(os.path.expanduser(os.environ.get(var) or default))


def state_dir() -> Path:
    return _env_path("ATHENA_STATE_DIR", "~/.local/state/athena")


def worktree_root() -> Path:
    return _env_path("ATHENA_WORKTREE_ROOT", "~/Developer/.worktrees")


def quota_file() -> Path:
    return _env_path(
        "ATHENA_QUOTA_FILE", "~/.local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json"
    )


def herdr_bin() -> str:
    return os.environ.get("ATHENA_HERDR") or "herdr"


def hq_name() -> str:
    return os.environ.get("ATHENA_HQ") or "athena"


def min_col() -> int:
    return int(os.environ.get("ATHENA_MIN_COL") or 70)


def max_workers() -> int:
    return min(int(os.environ.get("ATHENA_MAX_WORKERS") or 4), 6)


def ensure(sub: str = "") -> Path:
    path = state_dir() / sub if sub else state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def slug(branch: str) -> str:
    """`gabriel/plt-4512-SSO Fix` -> `plt-4512-sso-fix`: drop the first path segment when there are several."""
    parts = branch.split("/")
    tail = "-".join(parts[1:]) if len(parts) > 1 else parts[0]
    return re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")


def worktree_path(repo: Path, branch: str) -> Path:
    return worktree_root() / Path(repo).name / slug(branch)


def claim_file(cwd: str) -> Path:
    """Must match the auto-claude plugin: printf '%s' <realpath> | shasum | cut -c 1-16."""
    digest = hashlib.sha1(os.path.realpath(cwd).encode()).hexdigest()[:16]
    return state_dir() / "claims" / digest


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))
