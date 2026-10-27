#!/usr/bin/env python3
"""A stand-in for launchctl and systemctl --user in tests.

Every call appends its argv (one JSON list per line) to FAKE_SERVICE_LOG. FAKE_SERVICE_STATE (a JSON list) holds
what is loaded: launchctl bootstrap/bootout and systemctl enable/disable add and remove names, and launchctl print
and systemctl is-enabled answer from it, as the real tools do.
"""
import json
import os
import sys


def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_SERVICE_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\n")
    state_file = os.environ["FAKE_SERVICE_STATE"]
    try:
        with open(state_file) as f:
            loaded = set(json.load(f))
    except (OSError, ValueError):
        loaded = set()
    code = 0
    if argv[:1] == ["--user"]:
        verb, name = argv[1], argv[-1]
        if verb == "is-enabled":
            print("enabled" if name in loaded else "disabled")
            code = 0 if name in loaded else 1
        elif verb == "enable":
            loaded.add(name)
        elif verb == "disable":
            loaded.discard(name)
    else:
        verb = argv[0]
        if verb == "print":
            code = 0 if argv[1].rsplit("/", 1)[-1] in loaded else 113
        elif verb == "bootstrap":
            loaded.add(os.path.basename(argv[2])[:-len(".plist")])
        elif verb == "bootout":
            label = argv[1].rsplit("/", 1)[-1]
            code = 0 if label in loaded else 3
            loaded.discard(label)
    with open(state_file, "w") as f:
        json.dump(sorted(loaded), f)
    sys.exit(code)


if __name__ == "__main__":
    main()
