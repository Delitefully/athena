"""Test helpers: put the repository on sys.path and isolate every athena environment variable."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ATHENA_VARS = (
    "ATHENA_STATE_DIR", "ATHENA_WORKTREE_ROOT", "ATHENA_HERDR", "ATHENA_HQ", "ATHENA_MIN_COL",
    "ATHENA_MAX_WORKERS", "ATHENA_WORKER", "ATHENA_ROLE", "ATHENA_HQ_PANE", "ATHENA_PLUGIN_DIR",
    "FAKE_HERDR_SCENARIO", "FAKE_HERDR_LOG", "ATHENA_EXIT_GRACE", "ATHENA_HQ_CWD", "ATHENA_DASH_PORT",
    "ATHENA_LINEAR_WORKSPACE", "ATHENA_CONFIG", "ATHENA_MODEL", "ATHENA_EFFORT", "ATHENA_HERDR_INSTALL", "ATHENA_PYTHON",
    "ATHENA_HQ_CONTEXT_WINDOW", "ATHENA_HQ_SESSION_WAIT", "ATHENA_SESSION_WAIT", "ATHENA_PS", "FAKE_PS_CLAUDE",
    "ATHENA_TYPING_WAIT", "ATHENA_DOCTOR_GRACE",
)


# A brief athena brief check passes, for tests that spawn.
BRIEF = """# ABC-1: fix it
GOAL: The parser accepts an empty line.
SCOPE: In: the parser and its tests. Out: everything else.
ACCEPTANCE:
- `make test` passes with a new case for an empty line.
VERIFY: `make test`
FORBIDDEN: merging.
"""


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
    """Point state, worktrees and Claude's config dir (its session registry) at a temp dir; herdr at the fake."""
    tmp = Path(tmp)
    return EnvPatch({
        "ATHENA_STATE_DIR": str(tmp / "state"),
        "ATHENA_WORKTREE_ROOT": str(tmp / "worktrees"),
        "ATHENA_HERDR": str(ROOT / "tests" / "fake_herdr.py"),
        "FAKE_HERDR_LOG": str(tmp / "herdr.log"),
        "FAKE_HERDR_SCENARIO": str(tmp / "scenario.json"),
        "ATHENA_EXIT_GRACE": "0",
        "ATHENA_HQ_SESSION_WAIT": "0",
        "ATHENA_SESSION_WAIT": "0",
        "ATHENA_TYPING_WAIT": "0",
        "CLAUDE_CONFIG_DIR": str(tmp / "claude"),
    })
