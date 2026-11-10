"""One slot for heavy work across every session, and none while the Mac is short of room: builds and test runs took it down."""
import subprocess
import sys
import time

from athena_rex.kernel import budget, caps, store

SUMMARY = "run a heavy command in the one slot, when the Mac has room"
GAVE_UP = 75
POLL = 2.0


def _configure(p):
    p.add_argument("--wait", type=float, default=1800.0, help="seconds to wait for the slot and for room")
    p.add_argument("command", nargs="+", help="the command, after --")


def _run(args):
    deadline = time.monotonic() + args.wait
    while True:
        short = budget.over()
        with store.locked("locks/heavy.lock", blocking=False) as free:
            if free and not short:
                try:
                    return subprocess.call(args.command)
                except OSError as exc:
                    print(f"athena-rex: {args.command[0]} did not start: {exc}", file=sys.stderr)
                    return 127
        if time.monotonic() >= deadline:
            print(f"athena-rex: gave up after {args.wait:.0f}s: {'; '.join(short) or 'another session has the heavy slot'}", file=sys.stderr)
            return GAVE_UP
        time.sleep(POLL)


def commands():
    return {"heavy": caps.Command(SUMMARY, _configure, _run)}

