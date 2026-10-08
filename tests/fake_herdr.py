#!/usr/bin/env python3
"""A stand-in for the herdr CLI in tests.

Every call appends its argv (as one JSON list per line) to FAKE_HERDR_LOG. The reply comes from
the first rule in FAKE_HERDR_SCENARIO (a JSON list) whose "match" list is a prefix of argv:
{"match": ["agent", "start"], "stdout": {...} or "text", "stderr": "...", "exit": 0, "times": 1}.
A rule with "times" is used that many times, then skipped. Unmatched calls print {"result": {}}.
"""
import json
import os
import sys


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_HERDR_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    scenario_path = os.environ.get("FAKE_HERDR_SCENARIO")
    rules = []
    if scenario_path and os.path.exists(scenario_path):
        with open(scenario_path) as f:
            rules = json.load(f)
    for i, rule in enumerate(rules):
        match = rule.get("match", [])
        if argv[: len(match)] != match:
            continue
        if "times" in rule:
            if rule["times"] <= 0:
                continue
            rule["times"] -= 1
            with open(scenario_path, "w") as f:
                json.dump(rules, f)
        out = rule.get("stdout", {"result": {}})
        sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
        if rule.get("stderr"):
            sys.stderr.write(rule["stderr"])
        sys.exit(rule.get("exit", 0))
    sys.stdout.write(json.dumps({"result": {}}))


if __name__ == "__main__":
    main()
