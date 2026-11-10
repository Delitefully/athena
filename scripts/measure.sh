#!/bin/bash
# The same commands before and after, on a sanitized scratch copy: scripts/measure.sh <checkout> [state dir]. Medians of 30 runs, 10 for
# the watch, of a real process each. The fleet rows add 6 workers whose pids are gone, the costliest read: a ps and a look for a lost report.
repo=$(cd "${1:?usage: scripts/measure.sh <checkout> [state dir]}" && pwd -P)
state=${2:-$(mktemp -d "${TMPDIR:-/tmp}/ar-measure.XXXXXX")/state}
here=$(cd "$(dirname "$0")/.." && pwd -P)
/usr/bin/python3 "$here/scripts/scratch-state.py" "$state" --port 2894 >/dev/null || exit 1
exec env ATHENA_STATE_DIR="$state" ATHENA_WORKER=rex-measure ATHENA_HQ=measure-hq ATHENA_REX_SERVER= /usr/bin/python3 -I - "$repo/bin/athena-rex" "$state" <<'PY'
import json, os, statistics, subprocess, sys, time
binary, state = sys.argv[1:3]
stop = json.dumps({"hook_event_name": "Stop", "cwd": state, "last_assistant_message": 'ok\nATHENA-REPORT {"status":"BLOCKED","concerns":["m"]}'})
prompt = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": state})


def timed(argv, stdin, runs):
    took = []
    for _ in range(runs):
        begun = time.perf_counter()
        done = subprocess.run(argv, input=stdin, text=True, capture_output=True)
        took.append(1000 * (time.perf_counter() - begun))
        if done.returncode:
            sys.exit(f"{argv}: exit {done.returncode}: {done.stderr[-300:]}")
    return statistics.median(took)


def row(label, argv, stdin=None, runs=30):
    print(f"{label:<28}{timed([binary, *argv], stdin, runs):8.1f} ms")


with open(os.path.join(state, "ledger.jsonl")) as f:
    print(f"ledger lines                {sum(1 for _ in f):8d}")
timed([binary, "status"], None, 1)
row("status", ["status"])
row("hook Stop", ["hook"], stop)
row("hook UserPromptSubmit", ["hook"], prompt)
row("watch --once", ["watch", "--once"], runs=10)
os.makedirs(os.path.join(state, "run"), exist_ok=True)
os.makedirs(os.path.join(state, "workers"), exist_ok=True)
with open(os.path.join(state, "ledger.jsonl"), "a") as f:
    for n in range(6):
        name = f"fleet{n}"
        f.write(json.dumps({"ts": time.time(), "event": "spawn", "name": name, "repo": state, "branch": f"t/{n}", "path": state, "backend": "rex", "space": "s", "block": "b"}) + "\n")
        with open(os.path.join(state, "run", f"{name}.pid"), "w") as pid:
            pid.write(str(99000 + n))
        with open(os.path.join(state, "workers", f"{name}.json"), "w") as hook:
            json.dump({"name": name, "state": "idle", "ts": time.time(), "session_id": "x"}, hook)
row("status, 6 workers", ["status"])
row("watch --once, 6 workers", ["watch", "--once"], runs=10)
PY
