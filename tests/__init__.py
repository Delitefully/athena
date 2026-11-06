"""Every test run gets a scratch state dir, whatever the shell says.

A worker's environment points ATHENA_STATE_DIR at the live one, and with none set the default is the live one. A test
that forgot helpers.isolated_env once wrote a phantom worker to the live ledger. helpers.EnvPatch keeps to this too.
"""
import atexit
import os
import shutil
import tempfile

SCRATCH_ROOT = tempfile.mkdtemp(prefix="athena-tests-")
atexit.register(shutil.rmtree, SCRATCH_ROOT, ignore_errors=True)
SCRATCH_STATE_DIR = os.path.join(SCRATCH_ROOT, "state")
os.environ["ATHENA_STATE_DIR"] = SCRATCH_STATE_DIR
