#!/usr/bin/python3 -I
"""Make a sanitized scratch copy of the state dir: scripts/scratch-state.py <dest> [--from <state>] [--port N] [--caps a,b].

Reads the source and never writes it. Copies only the files athena itself uses, then: no pids, locks or binaries, every
live worker cleared from the ledger, roots repointed into the copy, and a dash port other than the real one.
"""
import argparse
import json
import os
import pwd
import shutil
import sys
import time

KEEP_FILES = ("config.json", "ledger.jsonl", "needs-you.json", "history.jsonl", "items-archive.jsonl", "actions.jsonl",
              "requests.jsonl", "watch.json", "health.json", "stack.json", "open-prs-posted.json", "dash.json",
              "notes.md", "preferences.md", "handoff.md", "threads.md", "standing-orders.md")
KEEP_DIRS = ("workers",)
DEFAULT_SOURCE = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library/Application Support/athena")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("dest")
    p.add_argument("--from", dest="source", default=DEFAULT_SOURCE)
    p.add_argument("--port", type=int, default=2899)
    p.add_argument("--caps", help="comma-separated rex.caps; default: leave it out, so all found load")
    args = p.parse_args()
    if args.port == 2843:
        sys.exit("refusing port 2843: that is the live dashboard")
    if os.path.exists(args.dest) and os.listdir(args.dest):
        sys.exit(f"{args.dest} is not empty")
    os.makedirs(args.dest, exist_ok=True)
    open(os.path.join(args.dest, ".athena-scratch"), "w").close()
    for name in KEEP_FILES:
        src = os.path.join(args.source, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(args.dest, name))
    for name in KEEP_DIRS:
        src = os.path.join(args.source, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(args.dest, name))
    ledger = os.path.join(args.dest, "ledger.jsonl")
    live = {}
    if os.path.isfile(ledger):
        with open(ledger) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "spawn":
                    live[ev.get("name")] = True
                elif ev.get("event") == "failed":
                    live[ev.get("name")] = True
                elif ev.get("event") == "retire":
                    live.pop(ev.get("name"), None)
        with open(ledger, "a") as f:
            for name in live:
                f.write(json.dumps({"ts": time.time(), "event": "retire", "name": name, "sanitized": True},
                                   sort_keys=True) + "\n")
    cfg_path = os.path.join(args.dest, "config.json")
    cfg = {}
    if os.path.isfile(cfg_path):
        with open(cfg_path) as f:
            cfg = json.load(f)
    roots = os.path.join(os.path.realpath(args.dest), "scratch-roots")
    os.makedirs(os.path.join(roots, "projects"), exist_ok=True)
    os.makedirs(os.path.join(roots, "worktrees"), exist_ok=True)
    cfg.update(projects_dir=os.path.join(roots, "projects"), worktree_root=os.path.join(roots, "worktrees"))
    cfg["hq_skip_permissions"] = False
    cfg.pop("hq_allow", None)
    rex = cfg.setdefault("rex", {})
    rex.pop("real_world", None)
    rex["dash_port"] = args.port
    if args.caps:
        rex["caps"] = [m for m in args.caps.split(",") if m]
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(json.dumps({"dest": os.path.realpath(args.dest), "cleared_live": sorted(live), "port": args.port}))


if __name__ == "__main__":
    main()
