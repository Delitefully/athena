#!/usr/bin/python3
"""revive end to end, on a Rex server of its own: /usr/bin/python3 tests/revive_e2e/e2e.py [athena checkout]

Run by hand, outside scripts/test.sh, whose sandbox rightly refuses Rex's binary. It starts a
throwaway Rex server on its own socket and state folder, with a fake `claude` (fake_claude.c, built here) in a scratch state dir made
by scripts/scratch-state.py from an empty seed. Athena's own code launches HQ and three workers there, exactly as it launches them
for real; the server restarts; then the module's own tick, with its real Rex adapter, brings them back. It never connects to the
human's Rex server, reads no live state (ATHENA_WORKER=rex-... keeps the lab's guard on) and stops only the server it started, by
its pid, which takes its panes with it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else HERE.parent.parent
sys.path.insert(0, str(HERE.parent.parent))
from athena.kernel import config  # noqa: E402

REX = config.rex_binary()
IDS = {"athena": "33333333-3333-4333-8333-3333333333a0", "w1": "33333333-3333-4333-8333-3333333333a1",
       "w2": "33333333-3333-4333-8333-3333333333a2", "busy": "33333333-3333-4333-8333-3333333333a3"}
SCRATCH = Path(f"/private/tmp/claude-{os.getuid()}")
SCRATCH.mkdir(mode=0o700, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="rve.", dir=SCRATCH))  # short: a socket path has 104 bytes
STATE, SOCK, LOG = ROOT / "state", f"unix://{ROOT}/rex/rex.sock", ROOT / "log"
failures = []


def check(what, ok, said=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}" + (f": {said}" if said and not ok else ""))
    if not ok:
        failures.append(what)


def rex(*args, stdin=None) -> str:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("REX_")}
    return subprocess.run([REX, "--autostart=false", "-S", SOCK, *args], input=stdin, capture_output=True, text=True, env=clean,
                          check=True, timeout=30).stdout


class Server:
    def __init__(self):
        (ROOT / "rex" / "state").mkdir(parents=True)
        (ROOT / "rex" / "init.lua").write_text("")
        (ROOT / "rex" / ".athena-server-owner").write_text(str(STATE))  # world.private_server: this run's own, named by its state
        self.proc = None

    def start(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("REX_")}
        self.proc = subprocess.Popen([REX, "server", "run", "--listen", SOCK, "--state-dir", str(ROOT / "rex" / "state"), "--config",
                                      str(ROOT / "rex" / "init.lua")], stdout=open(ROOT / "rex" / "server.log", "a"),
                                     stderr=subprocess.STDOUT, env=clean)
        for _ in range(50):
            try:
                rex("ls")
                return
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        sys.exit("the test server didn't start")

    def stop(self):
        if self.proc and self.proc.poll() is None and self.proc.pid > 1:
            self.proc.terminate()
            self.proc.wait(timeout=20)
        time.sleep(1)


def athena(code):
    """Runs `code` against the scratch state dir in a fresh athena process, as the watch's own would, and returns what it prints."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REX_", "ATHENA_"))}
    env.update(ATHENA_WORKER="rex-revive-e2e", ATHENA_STATE_DIR=str(STATE))
    prelude = ("import json, sys, time\nsys.path.insert(0, %r)\nfrom athena.kernel import agents, caps, config, ledger, store\n"
               % str(CHECKOUT))
    done = subprocess.run([sys.executable, "-I", "-c", prelude + code], capture_output=True, text=True, env=env, timeout=120)
    if done.returncode:
        sys.exit(f"athena said:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def foreground(space, block):
    found = json.loads(rex("block", "call", "-s", space, "-b", block, "process")).get("foreground") or {}
    return found.get("argv0") or found.get("name")


def main():
    LOG.mkdir()
    (ROOT / "bin").mkdir()
    subprocess.run(["cc", "-O0", f"-DLOG_DIR=\"{LOG}\"", "-o", str(ROOT / "bin" / "claude"), str(HERE / "fake_claude.c")],
                   check=True)
    seed = ROOT / "seed"
    seed.mkdir()
    (seed / "config.json").write_text("{}")
    subprocess.run(["/usr/bin/python3", "-I", str(CHECKOUT / "scripts" / "scratch-state.py"), str(STATE), "--from", str(seed),
                    "--port", "2897"], check=True, capture_output=True)
    config = json.loads((STATE / "config.json").read_text())
    config["rex"].update(backend="rex", server=SOCK, binary=REX, claude=str(ROOT / "bin" / "claude"), modules=["revive"],
                         modules_dir=str(ROOT / "modules"))
    (STATE / "config.json").write_text(json.dumps(config, indent=2))
    for name in IDS:
        (ROOT / "work" / name).mkdir(parents=True)
    server = Server()
    try:
        server.start()
        run(server)
    finally:
        server.stop()
    if failures:
        print(f"{len(failures)} failed; kept for a look: {ROOT}")
        return 1
    print("all passed")
    shutil.rmtree(ROOT, ignore_errors=True)
    return 0


def run(server):
    print("1. Athena launches HQ and three workers, as it does for real")
    places = json.loads(athena(f"""
hq, spawn = caps.get("hq"), caps.get("spawn")
backend = caps.CTX.backend
out = {{}}
args = hq.claude_args()
place = agents.restart(backend, {{}}, "athena", {str(ROOT / "work" / "athena")!r}, args, {{"ATHENA_ROLE": "hq"}}, label="athena")
store.write_json(store.path("hq.json"), {{"backend": "rex", "space": place.space, "window": place.window, "block": place.block,
                 "cwd": {str(ROOT / "work" / "athena")!r}, "claude_args": args, "session": None}})
store.write_json(store.path("hq-start.json"), {{"session_id": {IDS["athena"]!r}}})
out["athena"] = list(place) + [args]
for name in ("w1", "w2", "busy"):
    cwd = {str(ROOT / "work")!r} + "/" + name
    args = spawn.claude_args(name, "opus", "high", spawn.settings_for(name, cwd + "/brief.md"))
    place = agents.start(backend, name, name, cwd, args, spawn.claude_env(name, cwd), repo={str(ROOT / "work")!r})
    ledger.append("spawn", name, repo={str(ROOT / "work")!r}, branch="t/" + name, path=cwd, backend="rex", space=place.space,
                  window=place.window, block=place.block, claude_args=args)
    turn = "working" if name == "w2" else "idle"  # w2 was mid-turn when Rex went
    store.write_json(store.path("workers", name + ".json"), {{"name": name, "state": turn, "session_id": {json.dumps(IDS)}[name]}})
    out[name] = list(place) + [args]
print(json.dumps(out))
"""))
    time.sleep(2)
    launched = {name: (LOG / f"{name}.argv").read_text().splitlines() for name in IDS}
    check("each fake Claude started with athena's own flags", all(launched[n][-len(places[n][3]):] == places[n][3] for n in IDS),
          json.dumps(launched))
    print("2. The module, turned on, adopts this server")
    athena("caps.get('revive').start(caps.CTX)")
    first = athena("print(caps.get('revive').load().instance)").strip()
    check("the running instance is adopted", bool(first))
    print("3. Rex restarts: every pane comes back as a login shell; the human starts something in one, leaves input in two")
    for f in LOG.glob("*.argv"):
        f.unlink()
    server.stop()
    server.start()
    time.sleep(3)
    shells = {name: foreground(places[name][0], places[name][1]) for name in IDS}
    check("each pane is a shell again, on its old block id", all(s and s.lstrip("-") in ("zsh", "bash") for s in shells.values()),
          json.dumps(shells))
    busy = places["busy"]
    rex("send", "-s", busy[0], "-b", busy[1], stdin="sleep 600")
    rex("send-key", "-s", busy[0], "-b", busy[1], "enter")
    w1, w2 = places["w1"], places["w2"]
    rex("send", "-s", w1[0], "-b", w1[1], stdin="echo 'a quote never closed")  # w1's shell left waiting for the rest of a line
    rex("send-key", "-s", w1[0], "-b", w1[1], "enter")
    rex("send", "-s", w2[0], "-b", w2[1], stdin="echo half typed, never sent ")  # and a half-typed line on w2's
    time.sleep(1)
    print("4. The watch's ticks, with the module's real Rex adapter: one pane at a time, HQ first")
    tick = "print(json.dumps([l.text for l in caps.get('revive').step(caps.CTX, time.time())]))"
    said = athena(tick)
    check("one line says what comes back", "reviving athena, w1, w2, one at a time; skipped busy (its pane runs something else)" in said,
          said)
    time.sleep(2)
    check("only HQ is relaunched at first", sorted(f.stem for f in LOG.glob("*.argv")) == ["athena"],
          " ".join(sorted(f.stem for f in LOG.glob("*.argv"))))
    deadline = time.time() + 90
    while not (LOG / "w2.argv").exists() and time.time() < deadline:
        athena(tick)
        time.sleep(2)
    typed_at = {name: (LOG / f"{name}.argv").stat().st_mtime if (LOG / f"{name}.argv").exists() else 0
                for name in ("athena", "w1", "w2")}
    check("each started after the one before it", 0 < typed_at["athena"] < typed_at["w1"] < typed_at["w2"], json.dumps(typed_at))
    time.sleep(2)
    for name in ("athena", "w1", "w2"):
        argv = (LOG / f"{name}.argv").read_text().splitlines() if (LOG / f"{name}.argv").exists() else []
        check(f"{name} came back with --resume {IDS[name][:8]} and its exact flags", argv == ["--resume", IDS[name], *places[name][3]],
              json.dumps(argv))
        pid = athena(f"print(agents.read_pid({name!r}), bool(agents.stat_of({name!r})))").split()
        check(f"{name}'s pid is recorded and athena sees its Claude", pid[1] == "True", " ".join(pid))
        check(f"{name} is in its own pane, block id unchanged",
              athena(f"print(caps.CTX.backend.block_label(agents.place_of({{'space': {places[name][0]!r}, 'block': {places[name][1]!r}}})))").strip() == name)
    check("busy was left alone", not (LOG / "busy.argv").exists() and foreground(busy[0], busy[1]) == "sleep")
    print("5. Later ticks: HQ is told its session start once ready, w2 (mid-turn) to carry on; w1 reads as running")
    deadline = time.time() + 60
    while time.time() < deadline:
        states = json.loads(athena("print(json.dumps({o.name: o.state and o.state.value for o in caps.get('revive').load().panes}))"))
        moving = json.loads(athena("record = caps.get('revive').record\n"
                                   "print(json.dumps([s.value for s in record.FOLLOWING | {record.State.QUEUED}]))"))
        if all(states.get(n) not in moving for n in ("athena", "w1", "w2")):
            break
        athena("caps.get('revive').step(caps.CTX, time.time())")
        time.sleep(2)
    check("states", states == {"athena": "prompted", "w1": "running", "w2": "prompted", "busy": None}, json.dumps(states))
    told = json.loads(athena("print(json.dumps([caps.get('hq').ROUTINE, caps.get('revive').CONTINUE]))"))
    for name, text in (("athena", told[0]), ("w2", told[1])):
        typed = (LOG / f"{name}.typed").read_text() if (LOG / f"{name}.typed").exists() else ""
        check(f"{name} was typed {'its session start' if name == 'athena' else 'its nudge'}, once", typed == text + "\n", typed)
    check("w1, idle when Rex went, was told nothing", not (LOG / "w1.typed").exists())
    print("6. The same instance seen again: nothing happens")
    before = (STATE / "revive.json").read_text()
    athena("caps.get('revive').step(caps.CTX, time.time())")
    check("the record is unchanged", (STATE / "revive.json").read_text() == before)


if __name__ == "__main__":
    sys.exit(main())
