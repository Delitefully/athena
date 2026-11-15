"""A test fixture: a module whose services are fake children (tests/fixtures/fake_child.py), so a test can run the real
`athena services run` with no real dash, watch or menu bar under it. $ATHENA_FAKE_SERVICES names them, as JSON:
[[name, every or null, keep_alive], ...]."""
import json
import os
import sys
from pathlib import Path

from athena.kernel import caps

SUMMARY = "fake services, for the supervisor's tests"
CHILD = Path(__file__).resolve().parent.parent.parent / "fake_child.py"


def services(ctx):
    listed = json.loads(os.environ.get("ATHENA_FAKE_SERVICES") or "[]")
    return [caps.Service(name, ["fake-child", name], every, keep_alive) for name, every, keep_alive in listed]


def _configure(p):
    p.add_argument("tag")


def _run(args):
    os.execv(sys.executable, [sys.executable, str(CHILD), args.tag])


def commands():
    return {"fake-child": caps.Command("a fake service", _configure, _run, hidden=True)}
