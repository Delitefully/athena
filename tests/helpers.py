"""Test helpers: put the repository on sys.path and isolate every athena environment variable."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ATHENA_VARS = (
    "ATHENA_STATE_DIR", "ATHENA_WORKTREE_ROOT", "ATHENA_HERDR", "ATHENA_HQ", "ATHENA_MIN_COL",
    "ATHENA_MAX_WORKERS", "ATHENA_WORKER", "ATHENA_QUOTA_FILE", "ATHENA_HQ_PANE", "ATHENA_PLUGIN_DIR",
    "FAKE_HERDR_SCENARIO", "FAKE_HERDR_LOG", "ATHENA_EXIT_GRACE", "ATHENA_HQ_CWD",
)


class EnvPatch:
    def __init__(self, values):
        self.saved = {k: os.environ.get(k) for k in set(ATHENA_VARS) | set(values)}
        for k in ATHENA_VARS:
            os.environ.pop(k, None)
        os.environ.update(values)

    def set(self, key, value):
        if key not in self.saved:
            self.saved[key] = os.environ.get(key)
        os.environ[key] = value

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def isolated_env(tmp):
    """Point state, worktrees and the quota file at a temp dir; herdr at the fake."""
    tmp = Path(tmp)
    return EnvPatch({
        "ATHENA_STATE_DIR": str(tmp / "state"),
        "ATHENA_WORKTREE_ROOT": str(tmp / "worktrees"),
        "ATHENA_QUOTA_FILE": str(tmp / "quota.json"),
        "ATHENA_HERDR": str(ROOT / "tests" / "fake_herdr.py"),
        "FAKE_HERDR_LOG": str(tmp / "herdr.log"),
        "FAKE_HERDR_SCENARIO": str(tmp / "scenario.json"),
        "ATHENA_EXIT_GRACE": "0",
    })
