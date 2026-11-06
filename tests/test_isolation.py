"""No test run may reach the real state dir, whatever the shell says. A worker's own environment points
ATHENA_STATE_DIR at the live one, so a test that forgot helpers.isolated_env once wrote a phantom worker to the live
ledger. These tests only read paths: they must stay safe to run without the guard."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from athena_lib import paths

REAL = Path("~/.local/state/athena").expanduser()
TEMP = os.path.realpath(tempfile.gettempdir())


class IsolationTest(unittest.TestCase):
    def assert_scratch(self, state_dir):
        self.assertNotEqual(os.path.realpath(state_dir), os.path.realpath(REAL))
        self.assertTrue(os.path.realpath(state_dir).startswith(TEMP + os.sep), f"{state_dir} is not under {TEMP}")

    def test_a_test_with_no_setup_at_all_gets_a_scratch_state_dir(self):
        self.assert_scratch(paths.state_dir())

    def test_a_bare_envpatch_does_not_fall_back_to_the_real_one(self):
        # EnvPatch clears ATHENA_STATE_DIR, and with none set the default is the real one.
        env = helpers.EnvPatch({"ATHENA_HQ": "athena"})
        try:
            self.assert_scratch(paths.state_dir())
        finally:
            env.restore()

    def test_a_subprocess_inherits_the_scratch_state_dir(self):
        code = "from athena_lib import paths; print(paths.state_dir())"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=helpers.ROOT, check=True)
        self.assert_scratch(out.stdout.strip())
