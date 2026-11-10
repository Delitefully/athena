import json
import os
import subprocess
import unittest

from harness import Case, ROOT, store

from athena_rex.kernel import agents, bus, caps, config, ledger


class Ledger(Case):
    def test_the_lifecycle_table_is_what_the_fold_does(self):
        made = {None: [], "live": ["spawn"], "failed": ["spawn", "failed"], "damaged": ["update"], "retired": ["spawn", "retire"]}
        for state, nexts in ledger.LIFECYCLE.items():
            for kind, reached in (("spawn", "live"), ("failed", "failed"), ("retire", "retired"), ("update", "damaged")):
                if reached in nexts and (state is None or kind != "update"):
                    lines = [{"event": k, "name": "w", "ts": 1} for k in made[state]] + [{"event": kind, "name": "w", "ts": 2}]
                    self.assertEqual(ledger.fold(lines)["w"]["state"], reached, (state, kind))

    def test_a_damaged_record_never_reads_as_live(self):
        self.assertEqual(ledger.phase({"state": "damaged", "name": "w"}), "damaged")

    def test_a_lost_spawn_line_never_crashes_a_reader(self):
        ledger.append("update", "ghost", path="/x")
        ledger.append("review", "ghost", review={"verdict": "pass", "head": "abc"})
        self.assertEqual(ledger.workers()["ghost"]["state"], "damaged")
        self.assertEqual(ledger.live(), [])
        self.assertEqual([w["name"] for w in ledger.damaged()], ["ghost"])

    def test_a_torn_last_line_does_not_swallow_the_next_record(self):
        ledger.append("spawn", "a", path="/a")
        with open(store.path("ledger.jsonl"), "a") as f:
            f.write('{"event": "update", "na')
        ledger.append("retire", "a")
        self.assertEqual(ledger.workers()["a"]["state"], "retired")

    def test_reads_what_the_old_athena_wrote(self):
        old = [{"ts": 1, "event": "spawn", "name": "w", "pane": "w1:p1", "workspace": "w1", "repo": "/r", "branch": "b"},
               {"ts": 2, "event": "update", "name": "w", "goal_pending": "go"},
               {"ts": 3, "event": "update", "name": "w", "goal_pending": None},
               {"ts": 4, "event": "retire", "name": "w", "backup": "x"}]
        store.path("ledger.jsonl").write_text("".join(json.dumps(e) + "\n" for e in old))
        worker = ledger.get("w")
        self.assertEqual((worker["state"], worker["spawned"], worker["pane"]), ("retired", 1, "w1:p1"))
        self.assertFalse(ledger.managed(worker))

    def test_a_retire_killed_halfway_stops_hiding_exits_in_the_end(self):
        self.assertTrue(ledger.is_retiring({"retiring": 1000.0}, now=1100.0))
        self.assertFalse(ledger.is_retiring({"retiring": 1000.0}, now=1000.0 + ledger.RETIRING_EXPIRES + 1))


class Store(Case):
    def test_a_failed_write_keeps_the_old_content(self):
        target = store.path("hq.json")
        store.write_json(target, {"session": "kept"})
        real = os.replace

        def full_disk(*args):
            raise OSError(28, "No space left on device")
        os.replace = full_disk
        try:
            with self.assertRaises(OSError):
                store.write_json(target, {"session": "lost"})
        finally:
            os.replace = real
        self.assertEqual(store.read_json(target)["session"], "kept")

    def test_a_config_that_will_not_parse_uses_the_last_good_one(self):
        self.write_config(hq_skip_permissions=True)
        config.remember_good()
        (self.state / "config.json").write_text("{ broken")
        config.reset()
        self.assertTrue(config.get("hq_skip_permissions"))
        self.assertTrue(config.damaged)


class Bus(Case):
    def test_read_by_cursor_and_rotation(self):
        bus.emit("a")
        events, offset = bus.tail(0)
        self.assertEqual([e["topic"] for e in events], ["a"])
        bus.emit("b")
        events, offset = bus.tail(offset)
        self.assertEqual([e["topic"] for e in events], ["b"])
        self.assertEqual(bus.tail(offset + 1000)[1], offset)


class Liveness(Case):
    def test_ps_that_cannot_answer_is_unknown_not_gone(self):
        os.environ["ATHENA_PS"] = "/nonexistent/ps"
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("12345")
        with self.assertRaises(agents.Unknown):
            agents.stat_of("w")

    def _fake_ps(self, pid, command):
        script = self.tmp / "ps"
        script.write_text(f"#!/bin/sh\necho '{pid} S {command}'\n")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text(str(pid))

    def test_a_reused_pid_is_not_this_workers_claude(self):
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text(str(os.getpid()))
        self.assertIsNone(agents.stat_of("w"))
        self._fake_ps(4242, "claude --name someone-else --settings x")
        self.assertIsNone(agents.stat_of("w"))
        self.assertFalse(agents.kill_if_alive("w", grace=0))

    def test_a_name_that_starts_the_same_is_another_worker(self):
        self._fake_ps(4242, "claude --name w2 --settings x")
        self.assertIsNone(agents.stat_of("w"))
        self.assertFalse(agents.kill_if_alive("w", grace=0))

    def test_its_own_name_is_alive(self):
        self._fake_ps(4242, "claude --resume abc --name w --settings x")
        self.assertEqual(agents.stat_of("w"), "S")


class InputBox(Case):
    def test_placeholder_is_empty_and_typed_text_is_not(self):
        dim = "\x1b[2mTry something\x1b[0m"
        self.assertEqual(agents.box_text(f"header\n❯ {dim}\n"), "")
        self.assertEqual(agents.box_text("header\n❯ half a thought\n"), "half a thought")

    def test_no_box_counts_as_typing(self):
        self.assertIsNone(agents.box_text("a dialog with no prompt\n"))

    def test_exit_screens(self):
        self.assertEqual(agents.exit_screen("Background work is running\n❯ 1. Exit and stop tasks\n"), "confirm")
        self.assertEqual(agents.exit_screen("❯ /exit  Exit the CLI\n"), "submit")
        self.assertEqual(agents.exit_screen("❯ \nResume this session with: x\n"), "exited")
        self.assertIsNone(agents.exit_screen("❯ Yes, allow this\n"))


class Modules(Case):
    def test_a_capability_can_be_left_out(self):
        everything = caps.found()
        self.assertNotIn("card-writer", everything)
        self.assertNotIn("clamshell", everything)
        self.write_config(rex={"caps": [m for m in everything if m != "board"]})
        out = self.cli("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.cli("board").returncode, 2)
        self.assertNotIn("board ", self.cli("--help").stdout)
        self.assertEqual(self.cli("doctor", "--json").returncode in (0, 1), True)

    def test_a_hook_never_blocks_a_claude_when_its_module_is_off(self):
        self.write_config(rex={"caps": ["status"]})
        self.assertEqual(self.cli("hook").returncode, 0)

    def test_the_kernel_names_no_capability(self):
        names = set(caps.found())
        for path in (ROOT / "athena_rex" / "kernel").glob("*.py"):
            for line in path.read_text().splitlines():
                if line.lstrip().startswith(("from athena_rex.caps", "import athena_rex.caps")):
                    self.fail(f"{path.name}: {line}")
        self.assertTrue(names)

    def test_every_scope_module_exists(self):
        wanted = {"spawn", "retire", "status", "watch", "items", "dash", "doctor", "services", "hq", "hooks", "reviews",
                  "prs", "slack", "board", "config", "workspaces", "trust", "modules"}
        self.assertEqual(wanted - set(caps.found()), set())

    def _loaded_after(self, *argv, stdin=""):
        code = ("import sys, io; sys.path.insert(0, %r); sys.stdin = io.StringIO(%r)\nfrom athena_rex.cli import main\nmain(%r)\n"
                "print(' '.join(sorted(sys.modules)))" % (str(ROOT), stdin, list(argv)))
        out = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code], capture_output=True, text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)
        return set(out.stdout.split())

    def test_a_hook_loads_only_its_own_capability(self):
        loaded = self._loaded_after("hook", stdin="{}")
        self.assertEqual({m for m in loaded if m.startswith("athena_rex.caps.")}, {"athena_rex.caps.hooks"})
        self.assertFalse(loaded & {"urllib.request", "http.client", "email", "typing", "shutil", "argparse"}, loaded)

    def test_status_loads_nothing_heavy(self):
        self.assertFalse(self._loaded_after("status") & {"urllib.request", "http.client", "email", "typing"})

    def test_the_command_keeps_its_bytecode(self):
        cached = ROOT / "athena_rex" / "kernel" / "__pycache__"
        made = list(cached.glob("lab.*.pyc"))
        for old in made:
            old.unlink()
        self.assertEqual(self.cli("status").returncode, 0)
        self.assertTrue(list(cached.glob("lab.*.pyc")), "every command would compile all it imports, every time")

    def test_status_imports_no_other_capability(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['x', 'status']\n"
                "from athena_rex.cli import main\nmain()\n"
                "print(sorted(m for m in sys.modules if m.startswith('athena_rex.caps.')))")
        out = subprocess.run(["/usr/bin/python3", "-I", "-c", code, str(ROOT)], capture_output=True, text=True, env=os.environ)
        self.assertIn("['athena_rex.caps.status']", out.stdout)


class ScratchCopy(Case):
    def test_sanitizes(self):
        source = self.tmp / "source"
        (source / "workers").mkdir(parents=True)
        (source / "secrets").mkdir()
        (source / "secrets" / "x.env").write_text("TOKEN=1")
        (source / "dash.pid").write_text("{}")
        (source / "hq.json").write_text('{"session": "real"}')
        (source / "hq-start.json").write_text('{"session_id": "real"}')
        (source / "config.json").write_text('{"worktree_root": "/real"}')
        (source / "ledger.jsonl").write_text(json.dumps({"event": "spawn", "name": "live", "path": "/real/w"}) + "\n"
                                             + json.dumps({"event": "failed", "name": "bad", "path": "/real/b"}) + "\n")
        dest = self.tmp / "copy"
        out = subprocess.run(["/usr/bin/python3", str(ROOT / "scripts" / "scratch-state.py"), str(dest), "--from", str(source)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        for gone in ("secrets", "dash.pid", "hq.json", "hq-start.json"):
            self.assertFalse((dest / gone).exists(), gone)
        os.environ["ATHENA_STATE_DIR"] = str(dest)
        config.reset()
        self.assertEqual(ledger.live(), [])
        self.assertNotEqual(ledger.get("bad")["state"], "failed")
        self.assertTrue(str(config.path("worktree_root")).startswith(str(dest)))
        self.assertNotEqual(config.rex("dash_port"), 2843)

    def test_refuses_the_live_port(self):
        out = subprocess.run(["/usr/bin/python3", str(ROOT / "scripts" / "scratch-state.py"), str(self.tmp / "c"), "--port", "2843",
                              "--from", str(self.tmp)], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
