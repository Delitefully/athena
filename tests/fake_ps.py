#!/usr/bin/env python3
"""A stand-in for `ps -A -o pid=,ppid=,args=` (ATHENA_PS): the real table, with Claude placed where a test wants it.

Ancestors of the process that ran it are numbered from 0 (its parent: the hook's Python), 1 (hooks/worker-status), 2
(whatever ran the hook: the test) and up. FAKE_PS_CLAUDE lists the levels that are `claude` (for example "2": the hook
runs in a top-level Claude; "2,3": in a Claude another Claude started). Every other Claude in the table, such as the
one running the test suite, is renamed, so the answer never depends on where the tests run.
"""
import os
import subprocess
import sys

rows = []
for line in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines():
    parts = line.split(None, 2)
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        rows.append([int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""])
parents = {pid: ppid for pid, ppid, _ in rows}
levels, pid = {}, os.getppid()
for level in range(64):
    levels[pid] = level
    pid = parents.get(pid)
    if not pid or pid in levels:
        break
want = {int(n) for n in (os.environ.get("FAKE_PS_CLAUDE") or "").split(",") if n.strip().isdigit()}
for row in rows:
    row[2] = "claude --name x" if levels.get(row[0]) in want else ("other" if "claude" in row[2] else row[2])
    print(f"{row[0]} {row[1]} {row[2]}")
sys.exit(0)
