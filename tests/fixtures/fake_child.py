"""A stand-in for a service under the supervisor's tests: never the real dash, watch or menu bar.

    fake_child.py <tag>

A TERM is noted in stops.log, and it exits 0. It notes each start in $ATHENA_FAKE_CHILDREN/starts.log, and holds $ATHENA_FAKE_CHILDREN/alive/<tag>-<pid>.lock for as
long as it lives, so a test tells it is gone by taking that lock, never by signalling a pid. What it does comes from
$ATHENA_FAKE_CHILDREN/mode-<tag>, read when it starts (default "run"):

    run      until release-<tag> or release-<pid> appears, or its parent is gone (the supervisor killed outright): exit 0
    keep     until release-<tag> or release-<pid> appears, whatever happens to its parent
    exit N   exit N at once
    fail     until release-<tag> appears or its parent is gone, then exit 1
"""
import fcntl
import os
import signal
import sys
import time

tag = sys.argv[1]
home = os.environ["ATHENA_FAKE_CHILDREN"]


def _term(signum, frame):
    with open(os.path.join(home, "stops.log"), "a") as log:
        log.write(f"{tag}\n")
    sys.exit(0)


signal.signal(signal.SIGTERM, _term)  # before its start is noted: a test that has seen that may stop it at once
os.makedirs(os.path.join(home, "alive"), exist_ok=True)
fd = os.open(os.path.join(home, "alive", f"{tag}-{os.getpid()}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
try:
    with open(os.path.join(home, f"mode-{tag}")) as f:
        mode = f.read().split()
except FileNotFoundError:
    mode = ["run"]
with open(os.path.join(home, "starts.log"), "a") as log:  # after its mode is read: a test may change it once this is said
    log.write(f"{tag} {os.getpid()} {os.getpgrp()} {os.getppid()}\n")

if mode[0] == "exit":
    sys.exit(int(mode[1]))
parent = os.getppid()
releases = [os.path.join(home, f"release-{tag}"), os.path.join(home, f"release-{os.getpid()}")]
deadline = time.monotonic() + 300  # a backstop: no fake child outlives a test run by much, whatever went wrong
while time.monotonic() < deadline:
    if any(os.path.exists(r) for r in releases) or (mode[0] != "keep" and os.getppid() != parent):
        break
    time.sleep(0.02)
sys.exit(1 if mode[0] == "fail" else 0)
