"""Copy the fixture state dir to DEST with every timestamp moved so the fixture's "now" is the real now.

For screenshots and demos: `python3 dash/testdata/fresh.py /tmp/dash-state`. The Go tests use the fixture as is.
"""
import json
import shutil
import sys
import time
from pathlib import Path

FIXTURE_NOW = 1791500000
SRC = Path(__file__).resolve().parent / "state"


def shift(value, delta):
    if isinstance(value, dict):
        return {k: (v + delta if k == "ts" and isinstance(v, (int, float)) else shift(v, delta)) for k, v in value.items()}
    if isinstance(value, list):
        return [shift(v, delta) for v in value]
    return value


def main(dest):
    dest = Path(dest)
    delta = time.time() - FIXTURE_NOW
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(SRC, dest)
    for path in dest.rglob("*"):
        if path.suffix == ".json":
            path.write_text(json.dumps(shift(json.loads(path.read_text()), delta), indent=2, sort_keys=True))
        elif path.suffix == ".jsonl":
            lines = [json.dumps(shift(json.loads(line), delta), sort_keys=True) for line in path.read_text().splitlines() if line.strip()]
            path.write_text("\n".join(lines) + "\n")
    print(dest)


if __name__ == "__main__":
    main(sys.argv[1])
