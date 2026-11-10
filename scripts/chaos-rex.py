#!/usr/bin/python3 -I
"""The chaos cases that need real processes (ids as in tests/test_chaos.py), on a private Rex server of its own and a sanitized scratch copy.

    scripts/chaos-rex.py [--real] [--only R1,K11] [--keep]

A stand-in `claude` is a real process with `--name` in its argv, so killing, stopping and retiring it are real. --real adds a few
tiny Haiku turns for what only a Claude can show. Signals go only to pids this run started, looked at first.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from athena_rex.kernel import lab  # noqa: E402

REX = os.environ.get("ATHENA_REX_BIN") or "/Applications/Rex Beta.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex"
AR = str(HERE / "bin" / "athena-rex")
HAIKU = "claude-haiku-5-5"
CLAUDE_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "TERM")
FAKE = """#!/bin/bash
# Stands in for Claude: a real process, with its name in its argv. The file named in $ATHENA_FAKE_MODE says what else it does.
name=unknown
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do [ "${args[$i]}" = "--name" ] && name=${args[$((i + 1))]}; done
mode=$(cat "%(root)s/mode" 2>/dev/null)
case "$mode" in
*no-hup*) trap '' HUP ;;
esac
case "$mode" in
*child*) nohup sleep 900 >/dev/null 2>&1 & echo $! >>"%(root)s/children" ;;
esac
printf 'Quick safety check\\n'
exec -a "claude --name $name --settings x" cat
"""


class Run:
    def __init__(self, keep):
        self.keep = keep
        self.root = Path(f"/private/tmp/claude-{os.getuid()}/arc{os.getpid()}")
        self.endpoint = f"unix://{self.root}/s.sock"
        self.failures = []
        self.mine = []
        self.begun = time.monotonic()

    def say(self, text):
        print(f"{int(time.monotonic() - self.begun):4d}s  {text}", flush=True)

    def check(self, case, what, ok, detail=""):
        self.say(f"{'PASS' if ok else 'FAIL'}  {case}: {what}" + (f"  [{str(detail)[:300]}]" if detail and not ok else ""))
        if not ok:
            self.failures.append(f"{case}: {what}")
        return ok

    def env(self, **extra):
        out = {**os.environ, "ATHENA_STATE_DIR": str(self.state), "ATHENA_REX_SERVER": self.endpoint, "ATHENA_REX_BIN": REX,
               "ATHENA_HQ": "chaos-hq", "ATHENA_CLAUDE": str(self.root / "bin" / "claude"), "ATHENA_REX_TRUST": "off",
               "ATHENA_DOCTOR_GRACE": "0", **extra}
        for key in ("ATHENA_CONFIG", "ATHENA_ROLE", "CLAUDE_CONFIG_DIR", *[k for k in out if k.startswith("HERDR_")]):
            out.pop(key, None)
        return out

    def ar(self, *args, timeout=120, **extra):
        return subprocess.run([AR, *args], capture_output=True, text=True, env=self.env(**extra), timeout=timeout, cwd=str(self.root))

    def rx(self, *args, timeout=30):
        return subprocess.run([REX, "-S", self.endpoint, "--autostart=false", *args], capture_output=True, text=True, timeout=timeout)

    def server_up(self):
        clean = {k: os.environ.get(k, "") for k in ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR")}
        clean.update(LANG=os.environ.get("LANG", "en_US.UTF-8"), REX_CONFIG=str(self.root / "empty.lua"))
        subprocess.run([REX, "-S", self.endpoint, "server", "start", "--state-dir", str(self.root / "rex")], env=clean, check=True, capture_output=True)

    def server_pid(self):
        return json.loads(self.rx("whoami", "--json").stdout)["pid"]

    def setup(self):
        self.root.mkdir(parents=True)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        subprocess.run(["/usr/bin/python3", str(HERE / "scripts" / "scratch-state.py"), str(self.state), "--port", "2896"], check=True, capture_output=True)
        (self.root / "empty.lua").write_text("-- empty\n")
        (self.root / ".athena-server-owner").write_text(str(self.state))
        (self.root / "bin").mkdir()
        fake = self.root / "bin" / "claude"
        fake.write_text(FAKE % {"root": self.root})
        fake.chmod(0o755)
        self.server_up()
        self.repo = self.root / "t" / "proj"
        origin = self.root / "t" / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        identity = ["-c", "user.name=chaos", "-c", "user.email=chaos@localhost"]
        subprocess.run(["git", "-C", str(self.repo), *identity, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(origin)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", "-u", "origin", "main"], check=True)

    def mode(self, text):
        (self.root / "mode").write_text(text)

    def spawn(self, name, *extra):
        out = self.ar("spawn", "--repo", str(self.repo), "--branch", f"t/{name}", "--name", name, "--no-fetch", "--force", "--force-budget", "--no-gate",
                      "--timeout", "2", *extra)
        if out.returncode:
            raise RuntimeError(f"spawn {name}: {out.stdout} {out.stderr}")
        return self.pid(name)

    def pid(self, name):
        for _ in range(50):
            try:
                return int((self.state / "run" / f"{name}.pid").read_text())
            except (OSError, ValueError):
                time.sleep(0.1)
        raise RuntimeError(f"{name} wrote no pid")

    def alive(self, pid):
        command = subprocess.run(["ps", "-o", "stat=,command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return command or None

    def command_of(self, pid):
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def kill_mine(self, pid, sig=signal.SIGKILL, *, name=None, exact=None, within=None):
        """Only a pid this run started, only if it still is what it was, and it has to say which it must be: the Claude with this
        `--name`, exactly this command, or a command naming this run's own folder. A pid can be reused, by another Claude too."""
        if not (name or exact or within):
            raise TypeError("kill_mine needs a name, exact or within that says something")
        if int(pid) <= 1:
            return False
        command = self.command_of(pid)
        words = command.split()
        if name:
            mine = any(word == "--name" and following == name for word, following in zip(words, words[1:]))
        elif exact:
            mine = command == exact
        else:
            mine = within in command
        if not mine:
            return False
        try:
            os.kill(int(pid), sig)
        except OSError:
            return False
        return True

    def claude_env(self, worker=None):
        """For a real Claude started directly: what a login needs and scratch state, and a worker's name only if the case means one.
        The installed plugin's hook fires on it and reads what it finds, so nothing else of this session goes along."""
        out = {key: os.environ[key] for key in CLAUDE_ENV if key in os.environ}
        out["ATHENA_STATE_DIR"] = str(self.state)
        if worker:
            out["ATHENA_WORKER"] = worker
        return out

    def status(self, name):
        rows = json.loads(self.ar("status", name, "--json").stdout)
        return rows[0]

    def doctor(self, *args):
        out = self.ar("doctor", "--json", *args, timeout=180)
        return json.loads(out.stdout) if out.stdout.strip() else {"error": out.stderr, "problems": [], "checks": {}}

    def wait(self, seconds, test):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if test():
                return True
            time.sleep(0.3)
        return False

    def retire(self, name, *args):
        return self.ar("retire", name, *args)

    def stop_watch(self):
        """Doctor's detached watch, by the pid it recorded, only if that pid is still a watch on this state dir."""
        pid = int((json.loads((self.state / "run" / "watch-state.json").read_text()) if (self.state / "run" / "watch-state.json").exists() else {}).get("pid") or 0)
        seen = subprocess.run(["ps", "-E", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout if pid > 1 else ""
        if " watch " in seen and f"ATHENA_STATE_DIR={self.state} " in seen:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
            self.wait(5, lambda: not self.alive(pid))

    def stop_mine(self):
        if (self.root / "children").exists():
            for line in (self.root / "children").read_text().split():
                if line.isdigit():
                    self.kill_mine(int(line), exact="sleep 900")
        for pid_file in (self.state / "run").glob("*.pid"):
            try:
                self.kill_mine(int(pid_file.read_text()), name=pid_file.stem)
            except ValueError:
                pass

    def detach(self):
        if self.mount:
            subprocess.run(["hdiutil", "detach", "-force", str(self.mount)], capture_output=True)

    def finish(self):
        """Each step whatever came before it: a step that fails must not leave a server running."""
        for step in (self.stop_watch, self.stop_mine, lambda: self.ar("dash", "off"), lambda: self.rx("server", "stop"), self.detach):
            try:
                step()
            except Exception as exc:
                self.say(f"cleanup: {type(exc).__name__}: {exc}")
        if self.keep:
            self.say(f"kept {self.root}")
        else:
            shutil.rmtree(self.root, ignore_errors=True)

    mount = None


def r1_r6_x2_m2(run):
    """R1 server SIGKILLed, R6 ids after a restart, X2 a report in the gap, M2 a crash with a cut ledger: one path, staged."""
    pids = {name: run.spawn(name) for name in ("rex-ra", "rex-rb")}
    run.ar("watch", "--once")
    server = run.server_pid()
    run.check("R1", "the pid to kill is the server we started", str(run.root) in (run.alive(server) or ""), run.alive(server))
    run.kill_mine(server, within=f"{run.root}/")
    run.check("R1", "the server is gone", run.wait(5, lambda: run.rx("whoami", "--json").returncode == 4))
    run.check("R1", "every program on it died too, or is dying", run.wait(10, lambda: not any(run.alive(p) for p in pids.values())),
              [run.alive(p) for p in pids.values()])
    found = run.doctor("--fix")
    server_row = found["checks"].get("server") or {}
    run.check("R1", "doctor says the server is down, and with the door shut it starts none", server_row.get("ok") is False, found)
    run.check("R1", "no server came up by itself", run.rx("whoami", "--json").returncode == 4)
    run.check("R2", "a call with no endpoint is refused and reaches nothing", run.ar("status", "--live", ATHENA_REX_SERVER="").returncode == 0
              and run.ar("spawn", "--repo", str(run.repo), "--branch", "t/x", "--name", "x", "--no-fetch", ATHENA_REX_SERVER="").returncode != 0)
    hook = json.dumps({"hook_event_name": "Stop", "last_assistant_message": 'gap\nATHENA-REPORT {"status":"BLOCKED","concerns":["in the gap"]}', "cwd": str(run.state)})
    subprocess.run([AR, "hook"], input=hook, text=True, env=run.env(ATHENA_WORKER="rex-ra", ATHENA_GATE="off"), check=True)
    with open(run.state / "ledger.jsonl", "a") as f:
        f.write('{"event": "update", "name": "rex-ra", "to')
    run.server_up()
    sessions = json.loads(run.rx("ls", "--json").stdout)["sessions"]
    run.check("R6", "the sessions come back with their ids", {s["label"] for s in sessions} >= {"rex ra", "rex rb"}, sessions)
    first = run.doctor("--fix")
    revived = [a for a in first["actions"] if a.startswith("repaired worker:")]
    run.check("M2", "after a crash, doctor brings no worker back: Rex runs the sessions", not revived, first["actions"])
    told = run.ar("watch", "--once").stdout
    run.check("X2", "the watch told the report from the gap", "rex-ra report BLOCKED" in told, told)
    run.check("R6", "still 2 sessions, not 4", len(json.loads(run.rx("ls", "--json").stdout)["sessions"]) == 2)
    for name in ("rex-ra", "rex-rb"):
        run.retire(name, "--force")


def r3(run):
    """R3 an app update: no newer build exists, so the nearest thing: the bundle the server runs from is replaced under it."""
    bundle = Path(REX).parents[2]
    copy = run.root / "app" / bundle.name
    shutil.copytree(bundle, copy, symlinks=True)
    binary = copy / "Contents" / "MacOS" / "rex"
    socket = f"unix://{run.root}/u.sock"
    clean = {k: os.environ.get(k, "") for k in ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR")}
    clean.update(REX_CONFIG=str(run.root / "empty.lua"))
    subprocess.run([str(binary), "-S", socket, "server", "start", "--state-dir", str(run.root / "rex-u")], env=clean, check=True, capture_output=True)
    made = subprocess.run([str(binary), "-S", socket, "--autostart=false", "new-session", "--help"], capture_output=True, text=True)
    shutil.rmtree(copy)
    shutil.copytree(bundle, copy, symlinks=True)
    who = subprocess.run([REX, "-S", socket, "--autostart=false", "whoami", "--json"], capture_output=True, text=True)
    run.check("R3", "a server whose bundle was swapped under it still answers the other binary", who.returncode == 0, who.stderr or made.stderr)
    subprocess.run([REX, "-S", socket, "--autostart=false", "server", "stop"], capture_output=True)
    shutil.rmtree(run.root / "app")


def r4(run):
    """R4 a socket gone, refused or too long: each must say which."""
    socket = run.root / "s.sock"
    moved = run.root / "s.moved"
    socket.rename(moved)
    gone = run.doctor()["checks"].get("server") or {}
    run.check("R4", "a removed socket reads as gone, by name", gone.get("ok") is False and "gone" in gone.get("detail", ""), gone)
    moved.rename(socket)
    run.check("R4", "and the server is fine once it's back", (run.doctor()["checks"].get("server") or {}).get("ok") is True)
    long_dir = run.root / ("x" * 100)
    long_dir.mkdir()
    (long_dir / ".athena-server-owner").write_text(str(run.state))
    out = run.ar("doctor", "--json", ATHENA_REX_SERVER=f"unix://{long_dir}/s.sock")
    row = (json.loads(out.stdout)["checks"].get("server") or {}) if out.stdout.strip() else {}
    run.check("R4", "a path past macOS's limit says so", "104" in row.get("detail", ""), row or out.stderr)
    shutil.rmtree(long_dir)


def r5_r7(run):
    """R5 a session renamed, R7 what clients list."""
    run.mode("")
    run.spawn("rn")
    record = run.status("rn")
    run.rx("session", "rename", record["space"], "a new label")
    found = run.doctor()
    run.check("R5", "a renamed session is still that worker", (found["checks"].get("worker:rn") or {}).get("ok") is True, found["checks"])
    clients = run.rx("client", "ls", "--json")
    text = (HERE / "athena_rex").rglob("*.py")
    named = [p.name for p in text if '"client"' in p.read_text()]
    run.check("R7", "nothing here reads Rex's client list", not named, named)
    run.check("R7", "doctor answers with whatever it holds", clients.returncode == 0 and "server" in found["checks"])
    run.retire("rn", "--force")


def r6_swap(run):
    """R6 ownership: a record pointing at another worker's block must not type into it."""
    run.mode("")
    run.spawn("own-a")
    run.spawn("own-b")
    a, b = run.status("own-a"), run.status("own-b")
    with open(run.state / "ledger.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "event": "update", "name": "own-a", "space": b["space"], "block": b["block"]}) + "\n")
    out = run.ar("send", "own-a", "this-must-not-arrive")
    screen = run.rx("capture", "-s", b["space"], "-b", b["block"], "--trim").stdout
    run.check("R6", "a record pointing at another worker's block types nothing into it", "this-must-not-arrive" not in screen, (out.returncode, out.stdout, out.stderr))
    blocks = json.loads(run.rx("block", "ls", "-s", b["space"], "--json").stdout)["blocks"]
    run.check("R6", "real Rex keeps the label ownership is checked by", any(x.get("block_id") == b["block"] and x.get("label") == "own-b" for x in blocks), blocks)
    with open(run.state / "ledger.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "event": "update", "name": "own-a", "space": a["space"], "block": a["block"]}) + "\n")
    run.retire("own-a", "--force")
    run.retire("own-b", "--force")


def r8(run):
    """R8 launch text: a command far over the shell's line limit."""
    run.mode("")
    title = "long " * 500
    run.spawn("longarg", "--title", title[:2400])
    run.check("R8", "a 2400-byte title and a long argv launch with nothing typed", bool(run.alive(run.pid("longarg"))))
    size = (run.state / "run" / "longarg.sh").stat().st_size
    run.check("R8", "the command is in a file", size > 100, size)
    run.retire("longarg", "--force")


def r9(run):
    """R9 a process that ignores SIGHUP."""
    run.mode("no-hup")
    pid = run.spawn("nohup")
    out = run.retire("nohup", "--force")
    run.check("R9", "retire stops a process that ignores HUP", out.returncode == 0 and run.wait(10, lambda: not run.alive(pid)), (out.stdout, out.stderr, run.alive(pid)))
    run.mode("")


def r10(run):
    """R10 the lab touching live: every door into live state refuses, by what it is."""
    for live in lab.LIVE_PATHS[:2]:
        out = subprocess.run([AR, "status"], capture_output=True, text=True, env={**os.environ, "ATHENA_STATE_DIR": live})
        run.check("R10", f"{os.path.basename(live)} is refused", out.returncode == 2, out.stdout + out.stderr)
    unsafe = subprocess.run([str(HERE / "scripts" / "smoke.sh"), "--state-dir", lab.LIVE_PATHS[0]], capture_output=True, text=True, timeout=20)
    run.check("R10", "smoke refuses it too", unsafe.returncode == 2, unsafe.stderr)
    files = {p.name for p in run.state.iterdir()}
    run.check("R10", "the scratch copy holds no pids, locks or bin", not {f for f in files if f.endswith((".pid", ".lock"))} and "bin" not in files and "locks" not in files or "locks" in files and not (run.state / "dash.pid").exists(), sorted(files))
    cfg = json.loads((run.state / "config.json").read_text())
    run.check("R10", "and points at no live root or port", str(lab.real_home()) not in json.dumps(cfg.get("worktree_root", "")) and cfg["rex"]["dash_port"] != 2843, cfg.get("worktree_root"))


def w2_w3(run):
    """W2 the watch killed, W3 two of them."""
    watcher = subprocess.Popen([AR, "watch", "--interval", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=run.env(), cwd=str(run.root))
    def beating():
        try:
            return json.loads((run.state / "run" / "watch-state.json").read_text()).get("pid") == watcher.pid
        except (OSError, ValueError):
            return False
    run.check("W3", "it comes up", run.wait(15, beating))
    second = run.ar("watch", "--once")
    run.check("W3", "a second watch on the same state refuses", second.returncode == 1 and "another watch" in second.stderr, second.stderr)
    watcher.kill()
    watcher.wait()
    second = run.ar("watch", "--once")
    run.check("W2", "its lock died with it, so a new one starts", second.returncode == 0, second.stderr)
    beat = json.loads((run.state / "run" / "watch-state.json").read_text())
    beat["beat"] = time.time() - 1000
    (run.state / "run" / "watch-state.json").write_text(json.dumps(beat))
    found = run.doctor("--fix")
    run.check("W2", "doctor restarts it alone", "repaired watch" in found["actions"], found["actions"])
    run.check("W2", "and HQ is not touched", not any("hq" in a for a in found["actions"]), found["actions"])
    run.check("W2", "the restarted loop beats", run.wait(30, lambda: json.loads((run.state / "run" / "watch-state.json").read_text()).get("beat", 0) > time.time() - 60))
    run.stop_watch()


def k1_k4(run):
    """K1 a kill, K4 a stop, on real processes."""
    run.mode("")
    pid = run.spawn("frozen")
    run.kill_mine(pid, signal.SIGSTOP, name="frozen")
    time.sleep(0.5)
    row = run.status("frozen")
    run.check("K4", "a stopped process reads as blocked, with why", row["state"] == "blocked" and "stopped" in (row.get("detail") or ""), row)
    out = run.retire("frozen", "--force")
    run.check("K4", "and can still be retired", out.returncode == 0 and run.wait(10, lambda: not run.alive(pid)), (out.stderr, run.alive(pid)))
    pid = run.spawn("rex-killed")
    subprocess.run([AR, "hook"], input=json.dumps({"hook_event_name": "UserPromptSubmit"}), text=True, env=run.env(ATHENA_WORKER="rex-killed"))
    run.kill_mine(pid, name="rex-killed")
    run.wait(5, lambda: not run.alive(pid))
    run.check("K1", "killed mid-turn reads as exited, not working", run.status("rex-killed")["state"] == "exited")
    run.retire("rex-killed", "--force")


def k11(run):
    """K11 what a worker started, left running after it."""
    run.mode("child")
    run.spawn("parent")
    time.sleep(0.5)
    child = int((run.root / "children").read_text().split()[-1])
    run.check("K11", "the worker's child runs", bool(run.alive(child)))
    out = run.retire("parent", "--force", "--json")
    gone = run.wait(10, lambda: not run.alive(child))
    run.check("K11", "retire stops what its worker left running, and says so", gone and "leftovers" in out.stdout, (out.stdout, out.stderr, run.alive(child)))
    run.kill_mine(child, exact="sleep 900")
    run.mode("")


def disk(run):
    """S17, M5, X3 on a real full disk: a small image holds the state dir."""
    image = run.root / "small.dmg"
    mount = run.root / "mnt"
    mount.mkdir()
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "6m", "-fs", "HFS+", "-volname", "arc", str(image)], check=True)
    subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(mount), str(image)], check=True)
    run.mount = mount
    state = mount / "state"
    subprocess.run(["/usr/bin/python3", str(HERE / "scripts" / "scratch-state.py"), str(state), "--from", str(run.root / "no-source"), "--port", "2895"], check=True, capture_output=True)
    (run.root / ".athena-server-owner").write_text(str(state))
    real_state, run.state = run.state, state
    try:
        run.spawn("rex-full")
        hq_file = state / "hq.json"
        hq_file.write_text(json.dumps({"session": "conv", "space": "s", "block": "b"}))
        (state / "workers").mkdir(exist_ok=True)
        hook = {"hook_event_name": "Stop", "last_assistant_message": 'x\nATHENA-REPORT {"status":"BLOCKED","concerns":["before"]}', "cwd": str(run.root)}
        subprocess.run([AR, "hook"], input=json.dumps(hook), text=True, env=run.env(ATHENA_WORKER="rex-full"), check=True)
        ledger_size = (state / "ledger.jsonl").stat().st_size
        filler = mount / "filler"
        with open(filler, "wb") as f:
            try:
                while True:
                    f.write(b"\0" * 65536)
                    f.flush()
            except OSError:
                pass
        try:
            with open(mount / "filler2", "wb") as f:
                while True:
                    f.write(b"\0")
                    f.flush()
        except OSError:
            pass
        run.check("M5", "the disk really is full", shutil.disk_usage(mount).free < 8192, shutil.disk_usage(mount).free)
        status_before = json.loads((state / "workers" / "rex-full.json").read_text())
        gate = subprocess.run([AR, "hook"], input=json.dumps({"hook_event_name": "Stop", "last_assistant_message": "no report", "cwd": str(run.root)}), capture_output=True, text=True,
                              env=run.env(ATHENA_WORKER="rex-full"))
        run.check("M5", "the gate still sends the turn back on a full disk", '"block"' in gate.stdout, (gate.stdout, gate.stderr))
        run.check("S17", "the status file survives a failed write", json.loads((state / "workers" / "rex-full.json").read_text())["report"] == status_before["report"])
        run.check("S17", "no temp left behind", not list((state / "workers").glob("*.tmp*")), list((state / "workers").iterdir()))
        before = hq_file.read_text()
        doc = run.ar("doctor", "--json")
        run.check("M5", "doctor doesn't crash on it", doc.returncode in (0, 1) and doc.stdout.strip().startswith("{"), doc.stderr[-400:])
        run.check("S17", "hq.json is untouched", hq_file.read_text() == before)
        spawned = run.ar("status", "--json")
        run.check("M5", "status still reads", spawned.returncode == 0, spawned.stderr)
        added = run.ar("item", "add", "a thing", timeout=30)
        run.check("M5", "a write that can't happen says so and keeps its file", added.returncode != 0 or "Traceback" not in added.stderr, added.stderr[-300:])
        retired = run.retire("rex-full", "--force")
        run.check("M5", "a retire on a full disk fails clean, not half-done", "Traceback" not in retired.stderr, retired.stderr[-400:])
        filler.unlink()
        (mount / "filler2").unlink(missing_ok=True)
        run.check("X3", "the ledger's last record reads after the disk is freed", (state / "ledger.jsonl").stat().st_size >= ledger_size)
        after = run.ar("status", "rex-full", "--json")
        run.check("X3", "readers are fine after", after.returncode == 0, after.stderr)
        with open(state / "ledger.jsonl", "rb+") as f:
            f.truncate((state / "ledger.jsonl").stat().st_size - 9)
        (state / "hq.json").write_text("")
        (state / "hq.json.prev").unlink(missing_ok=True)
        (state / "hq-start.json").write_text(json.dumps({"session_id": "conv"}))
        report = run.doctor()
        names = [p["check"] for p in report["problems"]]
        run.check("X3", "a disk-full crash reads as damage, not green", "hq" in names and "ledger" in names, names)
    finally:
        run.ar("retire", "rex-full", "--force")
        run.rx("kill", "--all")
        run.state = real_state
        (run.root / ".athena-server-owner").write_text(str(real_state))
        subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True)
        run.mount = None


def real_permissions(run):
    """C8 on a real Haiku in Claude's default permission mode: a prompt becomes one item with the exact command, the
    dash's answer presses it, and a rule refuses before any prompt."""
    claude = shutil.which("claude")
    extra = {"ATHENA_CLAUDE": f"{claude} --permission-mode default"}
    spawned = subprocess.run([AR, "spawn", "--repo", str(run.repo), "--branch", "t/perm", "--name", "real-perm", "--model", HAIKU, "--effort", "low", "--no-fetch", "--force", "--force-budget", "--no-gate",
                              "--timeout", "60"], capture_output=True, text=True, env=run.env(**extra), cwd=str(run.root))
    run.check("C8", "a Haiku worker starts in default mode", spawned.returncode == 0, spawned.stderr)
    place = run.status("real-perm")
    if "startup prompt" in spawned.stdout or "waiting" in spawned.stdout:
        run.rx("send-key", "-s", place["space"], "-b", place["block"], "down")
        time.sleep(0.3)
        run.rx("send-key", "-s", place["space"], "-b", place["block"], "enter")

    def live_state():
        return (json.loads(run.ar("status", "real-perm", "--json", "--live").stdout)[0].get("live") or {}).get("state")
    run.check("C8", "it reaches idle", run.wait(60, lambda: live_state() in ("idle", "done")))

    def open_item(command):
        data = json.loads((run.state / "needs-you.json").read_text())
        return next((i for i in data["items"] if not i.get("resolved") and (i.get("permission") or {}).get("command") == command), None)
    run.ar("dash", "on")
    page = urllib.request.urlopen("http://127.0.0.1:2896/", timeout=5).read().decode()
    token = re.search(r'name="token" content="(\w+)"', page).group(1)

    def answer(item, option):
        request = urllib.request.Request("http://127.0.0.1:2896/api/action", method="POST", data=json.dumps({"item": item["id"], "option": option}).encode(),
                                         headers={"Content-Type": "application/json", "X-Athena-Token": token, "Origin": "http://127.0.0.1:2896"})
        return urllib.request.urlopen(request, timeout=5).status
    path = Path(place["path"])
    for command, option, made in (("touch approved-by-dash.txt", "approve", True), ("touch denied-by-dash.txt", "deny", False)):
        run.ar("send", "real-perm", f"Run this exact Bash command and nothing else, once: {command}", **extra)
        run.check("C8", f"{command}: one item with the exact command", run.wait(45, lambda: open_item(command) is not None), run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout[-500:])
        item = open_item(command)
        if not item:
            continue
        run.check("C8", f"{command}: the dash takes the answer", answer(item, option) == 200)
        time.sleep(7)
        run.ar("watch", "--once", **extra)
        target = path / command.split()[1]
        run.check("C8", f"{command}: {option} did what it says", run.wait(30, lambda: target.exists()) if made else (time.sleep(5) or not target.exists()), run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout[-500:])
        run.check("C8", f"{command}: the item closed", open_item(command) is None)
        run.wait(30, lambda: live_state() in ("idle", "done"))
    run.ar("dash", "off")
    # A worker's own prompt tells it never to do these, so a real one declines before any hook: the command it would run is asked directly.
    event = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": f"cd {path} && git push --force origin t/perm"}})
    said = subprocess.run([AR, "hook"], input=event, capture_output=True, text=True, env=run.env(ATHENA_WORKER="real-perm"), cwd=str(path))
    run.check("17", "the hook this worker's settings name refuses a force-push", '"permissionDecision": "deny"' in said.stdout, said.stdout + said.stderr)
    settings = json.loads((run.state / "settings" / "real-perm.json").read_text())
    run.check("17", "and those settings have it, on Bash", settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash")
    out = run.ar("retire", "real-perm", "--force", **extra)
    run.check("C8", "it retires", out.returncode == 0, out.stderr[-300:])


def real(run):
    """K3 K6 K7 K13 C6 R8 on one real Haiku worker: what only a Claude can show."""
    claude = shutil.which("claude")
    extra = {"ATHENA_CLAUDE": claude}
    spawned = subprocess.run([AR, "spawn", "--repo", str(run.repo), "--branch", "t/real", "--name", "real", "--model", HAIKU, "--effort", "low", "--no-fetch", "--force",
                              "--force-budget", "--no-gate", "--timeout", "60"], capture_output=True, text=True, env=run.env(**extra), cwd=str(run.root))
    run.check("real", "a Haiku worker starts", spawned.returncode == 0, spawned.stderr)
    place = run.status("real")

    def rows():
        return run.ar("status", "real", "--json", "--live")

    def state():
        return (json.loads(rows().stdout)[0].get("live") or {}).get("state")
    if "startup prompt" in spawned.stdout or "waiting" in spawned.stdout:
        run.rx("send-key", "-s", place["space"], "-b", place["block"], "down")
        time.sleep(0.3)
        run.rx("send-key", "-s", place["space"], "-b", place["block"], "enter")
    run.check("real", "it reaches idle", run.wait(60, lambda: state() in ("idle", "done")))

    def typed(text):
        out = subprocess.run([AR, "send", "real", text], capture_output=True, text=True, env=run.env(**extra), cwd=str(run.root))
        return out

    def report(concern):
        return f'Reply with exactly this one line and nothing else: ATHENA-REPORT {{"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["{concern}"]}}'

    def concern():
        return ((run.status("real").get("report") or {}).get("concerns") or [""])[0]
    padding = " ".join(f"word{n}" for n in range(450))
    typed(f"{report('long-prompt')} These numbered words are only there to make this message long: {padding}")
    run.check("R8", "a 3000-character prompt typed through Rex arrives whole", run.wait(90, lambda: concern() == "long-prompt"),
              (concern(), run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout[-900:]))
    long_goal = "The word kiwi has been written in a reply. These words do not matter: " + " ".join(f"z{n}" for n in range(300))
    (run.state / "briefs").mkdir(exist_ok=True)
    (run.state / "briefs" / "real.md").write_text("Reply briefly.\n")
    amended = run.ar("send", "real", f"/goal {long_goal} The final message ends with exactly this line: {report('long-goal').split(': ', 1)[1]}", ATHENA_CLAUDE=claude)
    time.sleep(2)
    run.wait(90, lambda: state() in ("idle", "done", "blocked"))
    screen = run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout
    run.check("R8", "a /goal over the paste limit runs as a goal", amended.returncode == 0 and "pasted_content" not in screen and "Goal" in screen, screen[-500:])
    hook_file = run.state / "workers" / "real.json"
    saved = hook_file.read_text()
    hook_file.write_text(json.dumps({**json.loads(saved), "report": None, "last_report": None, "state": "exited", "ts": time.time() - 600}))
    row = run.status("real")
    wanted = json.loads(saved).get("report") or {}
    run.check("K3", "with the Stop lost, the report is read from the real transcript", row.get("state_source") == "transcript" and (row.get("report") or {}).get("status") == wanted.get("status"), row)
    hook_file.write_text(saved)
    before = hook_file.read_text()
    nested = subprocess.run([claude, "-p", "--model", HAIKU, "Reply with the word nested"], capture_output=True, text=True, timeout=120, cwd=str(run.status("real")["path"]),
                            env=run.claude_env())
    run.check("K7", "a foreign Claude in the worktree ran", nested.returncode == 0, nested.stderr[-200:])
    run.check("K7", "and its events did not touch the worker's file", hook_file.read_text() == before)
    inside = run.status("real")["path"]
    before = hook_file.read_text()
    subprocess.run([claude, "-p", "--model", HAIKU, "Reply with the word nested"], capture_output=True, text=True, timeout=120, cwd=inside, env=run.claude_env("real"))
    run.check("K6", "a nested Claude carrying the worker's name leaves its status alone", hook_file.read_text() == before, hook_file.read_text()[:300])
    first = run.ar("amend", "real", "The word apple has been written in a reply.", ATHENA_CLAUDE=claude)
    run.check("K13", "amend sends a goal", first.returncode == 0 and "sent" in first.stdout, first.stdout + first.stderr)
    run.wait(60, lambda: state() in ("idle", "done", "blocked"))
    second = run.ar("amend", "real", "The word pear has been written in a reply.", ATHENA_CLAUDE=claude)
    run.wait(60, lambda: state() in ("idle", "done", "blocked"))
    screen = run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout
    run.check("K13", "a second goal replaces the first", second.returncode == 0 and "pear" in screen, screen[-500:])
    run.say("screen after two goals: " + " | ".join(line.strip() for line in screen.splitlines()[-12:] if line.strip())[:500])
    run.ar("send", "real", "Use the Bash tool to run: sleep 15. Then reply with the word slept.", ATHENA_CLAUDE=claude)
    run.wait(30, lambda: state() == "working")
    busy = run.ar("amend", "real", "The word plum has been written in a reply.", ATHENA_CLAUDE=claude)
    run.check("K13", "an amend typed while it works goes in", busy.returncode == 0 and "sent" in busy.stdout, busy.stdout + busy.stderr)
    run.check("K13", "and it runs when the turn ends", run.wait(90, lambda: "plum" in run.rx("capture", "-s", place["space"], "-b", place["block"], "--trim").stdout and state() in ("idle", "done", "blocked")))
    out = run.ar("retire", "real", "--force", ATHENA_CLAUDE=claude)
    run.check("real", "it retires", out.returncode == 0, out.stderr)
    done = subprocess.run([claude, "--resume", "00000000-0000-4000-8000-000000000000", "--model", HAIKU, "--name", "badconv"], capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, cwd=str(run.root), env=run.claude_env())
    run.check("C6", "Claude refuses a conversation that doesn't exist, and exits", done.returncode != 0, (done.returncode, done.stdout[-200:]))
    run.say(f"C6 wording: {(done.stdout + done.stderr).strip()[-160:]}")


CASES = {"R1": r1_r6_x2_m2, "R3": r3, "R4": r4, "R5": r5_r7, "R6": r6_swap, "R8": r8, "R9": r9, "R10": r10, "W2": w2_w3, "K1": k1_k4, "K11": k11, "DISK": disk}


def main():
    if not lab.active():
        sys.exit("chaos-rex.py runs only in the lab: ATHENA_WORKER must start with rex-, so the guards on live state are on")
    p = argparse.ArgumentParser()
    p.add_argument("--real", action="store_true")
    p.add_argument("--only")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()
    free = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    percent = next((int(line.rsplit(" ", 1)[-1].rstrip("%")) for line in free.splitlines() if "free percentage" in line), 100)
    if percent < 40:
        sys.exit(f"only {percent}% memory free, needs 40%")
    run = Run(args.keep)
    if lab.off_limits(run.root):
        sys.exit(f"refusing {run.root}: live state and the real home are never used")
    wanted = args.only.split(",") if args.only else list(CASES)
    try:
        run.setup()
        for name in wanted:
            if name in CASES:
                run.say(f"-- {name}")
                try:
                    CASES[name](run)
                except Exception as exc:
                    run.check(name, f"ran to its end: {type(exc).__name__}", False, exc)
        if args.real:
            run.say("-- real")
            try:
                real(run)
            except Exception as exc:
                run.check("real", f"ran to its end: {type(exc).__name__}", False, exc)
            try:
                real_permissions(run)
            except Exception as exc:
                run.check("C8", f"ran to its end: {type(exc).__name__}", False, exc)
    finally:
        run.finish()
    run.say(f"{len(run.failures)} failures")
    for failure in run.failures:
        print(f"  {failure}")
    return 1 if run.failures else 0


if __name__ == "__main__":
    sys.exit(main())
