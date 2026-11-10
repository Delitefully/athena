"""The walls between a test run and the real world, and proof each one fails when it should. Scratch paths pose as the
card, the live state dir and a real tool: nothing here goes near the real ones."""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path

from harness import BIN, Case, ROOT, SANDBOX, card_writer, dead_pid, guarded, spawn_record, store

from athena_rex.caps import doctor, services, slack
from athena_rex.kernel import agents, canary, caps, config, lab, world


def _run_inner(case_class) -> unittest.TestResult:
    """Run a test the way the suite would, and return what the harness made of it."""
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(case_class).run(result)
    return result


def _messages(result) -> str:
    return "\n".join(text for _, text in result.failures + result.errors)


class Canary(Case):
    """Each inner test does one bad thing. If the harness then still calls it a pass, the canary is worthless."""

    def _assert_tripped(self, case_class, wanted):
        saved = dict(os.environ)
        result = _run_inner(case_class)
        os.environ.clear()
        os.environ.update(saved)
        self._restore_rules(saved)
        config.reset()
        self.assertFalse(result.wasSuccessful(), "the harness let a real-world reach pass")
        self.assertIn(wanted, _messages(result))

    def _restore_rules(self, saved):
        rules = json.loads(saved["ATHENA_CANARY_CONFIG"])
        canary.configure(saved["ATHENA_CANARY"], rules["guarded"], rules["listings"], rules["tools"], rules["stubs"], rules["roots"])

    def test_a_write_to_a_path_posing_as_the_card_fails_the_test_and_never_lands(self):
        pose = Path(self.tmp) / "posing-volumes"
        (pose / "ExampleCard").mkdir(parents=True)

        class Bad(Case):
            extra_guarded = (str(pose),)

            def test(self):
                os.environ["ATHENA_CARD_DIR"] = str(pose)
                card_writer(self).write()
        self._assert_tripped(Bad, "reached guarded path")
        self.assertFalse((pose / "ExampleCard" / "PROGRAM.TXT").exists())

    def test_a_child_process_writing_the_posing_card_fails_the_test_too(self):
        pose = Path(self.tmp) / "posing-volumes"
        (pose / "ExampleCard").mkdir(parents=True)

        class Bad(Case):
            extra_guarded = (str(pose),)

            def test(self):
                card_writer(self)
                self.cli("watch", "--once", env={"ATHENA_CARD_DIR": str(pose)})
        self._assert_tripped(Bad, "reached guarded path")
        self.assertFalse((pose / "ExampleCard" / "PROGRAM.TXT").exists())

    def test_reaching_a_path_posing_as_live_state_fails_the_test(self):
        pose = Path(self.tmp) / "posing-live-state"
        pose.mkdir()
        (pose / "ledger.jsonl").write_text("{}\n")

        class Bad(Case):
            extra_guarded = (str(pose),)

            def test(self):
                (pose / "ledger.jsonl").read_text()
        self._assert_tripped(Bad, "reached guarded path")

    def test_a_tool_outside_the_stubs_fails_the_test_and_never_runs(self):
        pose = Path(self.tmp) / "posing-bin"
        pose.mkdir()
        marker = pose / "ran"
        tool = pose / "pmset"
        tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
        tool.chmod(0o755)

        class Bad(Case):
            def test(self):
                subprocess.run([str(tool), "-g"])
        self._assert_tripped(Bad, "ran real tool")
        self.assertFalse(marker.exists())

    def test_an_unexpected_program_fails_the_test(self):
        class Bad(Case):
            def test(self):
                subprocess.run(["/usr/bin/open", "--help"], capture_output=True)
        self._assert_tripped(Bad, "ran unexpected program")

    def test_the_network_fails_the_test_before_anything_is_sent(self):
        class Bad(Case):
            def test(self):
                with socket.socket() as s:
                    s.connect(("192.0.2.1", 443))
        self._assert_tripped(Bad, "network connection")

    def test_the_live_dashboards_port_fails_the_test(self):
        class Bad(Case):
            def test(self):
                with socket.socket() as s:
                    s.connect(("127.0.0.1", canary.LIVE_DASH_PORT))
        self._assert_tripped(Bad, "live dashboard")

    def test_swallowing_the_error_does_not_save_the_test(self):
        pose = Path(self.tmp) / "posing"
        pose.mkdir()

        class Bad(Case):
            extra_guarded = (str(pose),)

            def test(self):
                try:
                    (pose / "x").write_text("x")
                except Exception:
                    pass
        self._assert_tripped(Bad, "reached guarded path")

    def test_the_real_targets_are_on_the_list_and_trip_it(self):
        """The poses above add their own path. This is the list the real breach needs, checked with no file touched."""
        home = lab.real_home()
        real = ["/Volumes/ExampleCard/PROGRAM.TXT", f"{home}/.local/state/athena/ledger.jsonl",
                f"{home}/Library/Application Support/athena/ledger.jsonl", f"{home}/.claude.json",
                f"{home}/.claude/settings.json", f"{home}/Library/LaunchAgents/x.plist"]
        self.assertTrue(set(lab.LIVE_PATHS[:2]) <= set(guarded()))
        scratch_log = self.tmp / "direct-canary.log"
        saved = canary._State.log
        canary._State.log = str(scratch_log)
        try:
            for path in real:
                with self.assertRaises(canary.CanaryError, msg=path):
                    canary._check_path("open", path)
            for program in ("/usr/bin/security", "/usr/sbin/ioreg", "/usr/bin/pmset", "osascript", "caffeinate", "launchctl", "gh"):
                with self.assertRaises(canary.CanaryError, msg=program):
                    canary._check_program(program)
            for command in (["sh", "-c", "x"], ["env", "/usr/sbin/ioreg"], ["git", "-C", f"{home}/Developer/x", "status"]):
                with self.assertRaises(canary.CanaryError, msg=str(command)):
                    canary._check_program(command[0], command)
        finally:
            canary._State.log = saved

    def test_a_clean_test_passes(self):
        class Good(Case):
            def test(self):
                store.write_json(store.path("x.json"), {})
        saved = dict(os.environ)
        result = _run_inner(Good)
        os.environ.clear()
        os.environ.update(saved)
        self._restore_rules(saved)
        config.reset()
        self.assertTrue(result.wasSuccessful(), _messages(result))


class Door(Case):
    def test_it_is_shut_with_no_environment_at_all(self):
        os.environ.pop("ATHENA_WORKER")
        os.environ.pop("ATHENA_WORLD_STUBS")
        config.reset()
        self.assertFalse(lab.active())
        self.assertFalse(world.enabled())
        self.assertIsNone(_card_root_without_fake())
        for tool in world.TOOLS:
            with self.assertRaises(world.Off):
                world.resolve(tool)

    def test_no_environment_variable_opens_it(self):
        os.environ.pop("ATHENA_WORKER")
        for key in ("ATHENA_REAL_WORLD", "REX_REAL_WORLD", "ATHENA_REX_REAL_WORLD", "real_world"):
            os.environ[key] = "true"
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text('{"rex": {"real_world": true}}')
        os.environ["ATHENA_CONFIG"] = str(elsewhere)
        config.reset()
        self.assertFalse(lab.active())
        self.assertFalse(world.enabled())

    def test_a_stub_or_fake_card_cannot_name_a_real_place(self):
        os.environ["ATHENA_WORLD_STUBS"] = "/usr/bin"
        with self.assertRaises(world.Off):
            world.resolve("security")
        os.environ["ATHENA_CARD_DIR"] = "/Volumes"
        self.assertIsNone(world.card_root())
        unmarked = self.tmp / "unmarked"
        unmarked.mkdir()
        (unmarked / "security").write_text("")
        os.environ["ATHENA_WORLD_STUBS"] = str(unmarked)
        with self.assertRaises(world.Off):
            world.resolve("security")

    def test_the_lab_keeps_it_shut_even_if_the_config_asks(self):
        self.write_config(rex={"real_world": True})
        self.assertTrue(lab.active())
        self.assertFalse(world.enabled())

    def test_a_scratch_copy_cannot_carry_it_over(self):
        source = self.tmp / "source"
        source.mkdir()
        (source / "config.json").write_text('{"rex": {"real_world": true}}')
        dest = self.tmp / "copy"
        out = subprocess.run(["/usr/bin/python3", str(ROOT / "scripts" / "scratch-state.py"), str(dest), "--from", str(source)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("real_world", (dest / "config.json").read_text())

    def test_a_test_that_leaves_it_open_fails(self):
        class Bad(Case):
            def test(self):
                self.write_config(rex={"real_world": True})
                os.environ.pop("ATHENA_WORKER")
        saved = dict(os.environ)
        result = _run_inner(Bad)
        os.environ.clear()
        os.environ.update(saved)
        rules = json.loads(saved["ATHENA_CANARY_CONFIG"])
        canary.configure(saved["ATHENA_CANARY"], rules["guarded"], rules["listings"], rules["tools"], rules["stubs"], rules["roots"])
        config.reset()
        self.assertFalse(result.wasSuccessful())
        self.assertIn("the door to the real world opened", _messages(result))

    def test_a_door_left_open_with_the_canary_stripped_fails(self):
        class Bad(Case):
            def test(self):
                self.write_config(rex={"real_world": True})
                os.environ.pop("ATHENA_WORKER")
                os.environ.pop("ATHENA_CANARY")
        saved = dict(os.environ)
        result = _run_inner(Bad)
        os.environ.clear()
        os.environ.update(saved)
        rules = json.loads(saved["ATHENA_CANARY_CONFIG"])
        canary.configure(saved["ATHENA_CANARY"], rules["guarded"], rules["listings"], rules["tools"], rules["stubs"], rules["roots"])
        config.reset()
        self.assertFalse(result.wasSuccessful(), "the door stayed open and the harness called it a pass")
        self.assertIn("left the door", _messages(result))

    def _door_opened_in(self, body):
        class Bad(Case):
            test = body
        saved = dict(os.environ)
        result = _run_inner(Bad)
        os.environ.clear()
        os.environ.update(saved)
        rules = json.loads(saved["ATHENA_CANARY_CONFIG"])
        canary.configure(saved["ATHENA_CANARY"], rules["guarded"], rules["listings"], rules["tools"], rules["stubs"], rules["roots"])
        config.reset()
        self.assertFalse(result.wasSuccessful())
        self.assertIn("door", _messages(result))

    def test_a_door_opened_and_shut_again_mid_test_fails(self):
        def body(inner):
            inner.write_config(rex={"real_world": True})
            worker = os.environ.pop("ATHENA_WORKER")
            try:
                world.enabled()
            except canary.CanaryError:
                pass
            os.environ["ATHENA_WORKER"] = worker
            inner.write_config()
        self._door_opened_in(body)

    def test_a_door_opened_only_in_a_child_fails(self):
        def body(inner):
            inner.write_config(rex={"real_world": True})
            env = {k: v for k, v in os.environ.items() if k not in ("ATHENA_WORKER", "ATHENA_WORLD_STUBS")}
            subprocess.run([str(ROOT / "bin" / "athena-rex"), "doctor", "--quiet"], capture_output=True, env=env, timeout=30)
            inner.write_config()
        self._door_opened_in(body)

    def test_a_door_opened_in_a_plain_python_child_fails(self):
        def body(inner):
            inner.write_config(rex={"real_world": True})
            env = {k: v for k, v in os.environ.items() if k != "ATHENA_WORKER"}
            code = ("import sys; sys.path.insert(0, sys.argv[1])\nfrom athena_rex.kernel import world\n"
                    "try:\n    world.enabled()\nexcept Exception:\n    pass\n")
            subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code, str(ROOT)], capture_output=True, env=env, timeout=30)
            inner.write_config()
        self._door_opened_in(body)

    def test_the_only_tests_that_write_the_key_keep_the_lab_guard_on(self):
        for path in (ROOT / "tests").glob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"real_world[\"']?\s*[:=]", line) and path.name != "test_world.py":
                    self.fail(f"{path.name}:{number}: {line}")


def _card_root_without_fake():
    saved = os.environ.pop("ATHENA_CARD_DIR", None)
    try:
        return world.card_root()
    finally:
        if saved:
            os.environ["ATHENA_CARD_DIR"] = saved


class Sandbox(Case):
    """The wall for children the canary can't see. These run non-Python children on purpose."""

    def test_a_shell_child_cannot_write_what_the_sandbox_forbids(self):
        probe = SANDBOX["PROBE_WRITE"]
        out = subprocess.run(["sh", str(self._script(f'printf x > "{probe}"'))], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(probe))

    def test_a_shell_child_cannot_run_what_the_sandbox_forbids(self):
        marker = self.tmp / "ran"
        out = subprocess.run(["sh", str(self._script(f'"{SANDBOX["PROBE_EXEC"]}" "{marker}"'))],
                             capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(marker.exists())

    def test_a_shell_child_cannot_signal_a_process_outside(self):
        """`not permitted` is the sandbox refusing a process that exists. A dead pid, or none, says something else and fails."""
        out = subprocess.run(["sh", str(self._script(f'kill -0 {SANDBOX["OUTSIDER"]}'))], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not permitted", out.stderr)

    def test_a_shell_child_cannot_write_the_real_home(self):
        target = os.path.join(lab.real_home(), f".ar-sandbox-test-{os.getpid()}")
        out = subprocess.run(["sh", str(self._script(f'printf x > "{target}"'))], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(target))

    def _script(self, body):
        path = self.tmp / "child.sh"
        path.write_text(f"#!/bin/sh\n{body}\n")
        return path


class Sweep(Case):
    """Every path out goes through the door. A new one written straight into a capability fails here."""

    def _sources(self):
        for path in (ROOT / "athena_rex").rglob("*.py"):
            if path.name not in ("world.py", "canary.py"):
                yield path, path.read_text()

    def test_no_tool_is_run_except_through_the_door(self):
        tools = "|".join(world.TOOLS)
        pattern = re.compile(rf"(?:subprocess\.\w+|Popen)\(\s*\[\s*[\"'](?:{tools}|/usr/bin/\w+|/usr/sbin/\w+)")
        for path, text in self._sources():
            self.assertIsNone(pattern.search(text), path.name)

    def test_real_devices_and_services_are_named_nowhere_else(self):
        for path, text in self._sources():
            self.assertNotIn("/Volumes", text, path.name)
            if path.name != "slack.py":
                self.assertNotIn("slack.com", text, path.name)

    def test_the_network_is_used_only_by_slack_and_the_dashboards_own_loopback(self):
        for path, text in self._sources():
            if "urllib.request.urlopen" in text:
                self.assertIn(path.name, ("slack.py", "__init__.py"), path.name)


class Stubs(Case):
    def test_the_doorbell_asks_the_stub_and_gets_no_token(self):
        self.assertIsNone(slack.read_token())
        self.assertEqual(self.world_calls(), ["security find-generic-password -s athena-slack -w"])

    def test_a_watch_child_reaches_only_stubs_and_the_fake_card(self):
        card = self.tmp / "cards" / "ExampleCard"
        card.mkdir()
        card_writer(self)
        out = self.cli("watch", "--once")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("security find-generic-password -s athena-slack -w", self.world_calls())
        self.assertTrue((card / "PROGRAM.TXT").exists())

    def test_slack_itself_is_shut_even_with_a_token(self):
        with self.assertRaises(world.Off):
            slack.Client("fake-token").call("auth.test")

    def test_doctor_notifies_through_a_stub_once(self):
        def problem(ctx):
            return [caps.Check("part", False, "down")]
        real = caps.loaded
        caps.loaded = lambda: [types.SimpleNamespace(__name__="x", checks=problem)]
        try:
            doctor.run(caps.CTX, fix=True, now=1000)
            doctor.run(caps.CTX, fix=True, now=1100)
            doctor.run(caps.CTX, fix=False, now=1200)
        finally:
            caps.loaded = real
        self.assertEqual(len([c for c in self.world_calls() if c.startswith("osascript")]), 1)

    def test_a_read_only_doctor_run_does_not_use_up_the_notification(self):
        def problem(ctx):
            return [caps.Check("part", False, "down")]
        real = caps.loaded
        caps.loaded = lambda: [types.SimpleNamespace(__name__="x", checks=problem)]
        try:
            doctor.run(caps.CTX, fix=False, now=1000)
            self.assertEqual(self.world_calls(), [])
            doctor.run(caps.CTX, fix=True, now=1100)
        finally:
            caps.loaded = real
        self.assertEqual(len(self.world_calls()), 1)

    def test_power_reads_are_stubs_and_caffeinate_is_recorded_not_run(self):
        self.stub("pmset", "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; charging")
        spawn_record("w", self.tmp, self.tmp)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text(str(dead_pid()))
        self.write_config(rex={"modules": ["clamshell"]})
        clamshell = caps.get("clamshell")
        out = clamshell.current(1000)
        self.assertFalse(out["awake"])
        self.assertTrue(any(c.startswith("pmset") for c in self.world_calls()))
        self.assertTrue(any(c.startswith("ioreg") for c in self.world_calls()))
        real = clamshell._any_working
        clamshell._any_working = lambda: True
        try:
            clamshell.caffeinate()
        finally:
            clamshell._any_working = real
        for _ in range(100):
            if any(c.startswith("caffeinate") for c in self.world_calls()):
                break
            time.sleep(0.02)
        self.assertTrue(any(c.startswith("caffeinate -i") for c in self.world_calls()))

    def test_launchd_is_a_stub_and_writes_only_the_fake_folder(self):
        out = services.install(caps.CTX)
        self.assertTrue(any("wrote" in a for a in out))
        self.assertTrue((self.tmp / "agents" / "athena-rex.watch.plist").exists())
        self.assertTrue(any(c.startswith("launchctl bootstrap") for c in self.world_calls()))

    def test_with_no_stub_launchd_is_refused_and_writes_nothing(self):
        os.environ.pop("ATHENA_WORLD_STUBS")
        with self.assertRaises(world.Off):
            services.install(caps.CTX)
        self.assertFalse((self.tmp / "agents").exists())
        self.assertTrue(any("write" in a for a in services.install(caps.CTX, dry_run=True)))

    def test_the_default_rex_server_is_shut_outside_the_door(self):
        from athena_rex.backends import rex
        os.environ.pop("ATHENA_WORKER")
        config.reset()
        with self.assertRaises(world.Off):
            rex.Backend().up()
        fake_rex = self.tmp / "rex"
        fake_rex.write_text("#!/bin/sh\nexit 4\n")
        fake_rex.chmod(0o755)
        folder = Path(tempfile.mkdtemp(prefix="o", dir=SANDBOX["SOCKETS"]))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / world.SERVER_MARK).write_text(str(store.state_dir()))
        os.environ["ATHENA_REX_SERVER"] = f"unix://{folder}/s.sock"
        os.environ["ATHENA_REX_BIN"] = str(fake_rex)
        config.reset()
        self.assertFalse(rex.Backend().up())
        with self.assertRaises(world.Off):
            rex.Backend().start_server()

    def test_trust_needs_the_door_to_write_the_real_file(self):
        from athena_rex.caps import trust
        os.environ.pop("ATHENA_WORKER")
        os.environ["HOME"] = lab.real_home()
        config.reset()
        self.assertTrue(trust.ensure(self.tmp)["skipped"])


class LabGuards(Case):
    def _pose(self):
        live = self.home / ".local" / "state" / "athena"
        live.mkdir(parents=True)
        return (live,)

    def test_the_commands_guard_refuses_live_state(self):
        for state in self._pose():
            out = subprocess.run([str(BIN), "status"], capture_output=True, text=True, env={**os.environ, "ATHENA_STATE_DIR": str(state)})
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn("refusing", out.stderr)

    def test_smoke_refuses_live_state_and_other_servers(self):
        elsewhere = ["--server", "unix:///nonexistent/elsewhere.sock"]
        for args in (["--state-dir", lab.LIVE_PATHS[0], *elsewhere], ["--state-dir", lab.LIVE_PATHS[1], *elsewhere], elsewhere):
            out = subprocess.run([str(ROOT / "scripts" / "smoke.sh"), *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn("refusing", out.stderr)

    def test_the_python_guard_knows_the_real_paths_without_touching_them(self):
        for live in lab.LIVE_PATHS[:2]:
            self.assertTrue(lab.is_live(live))
            with self.assertRaises(lab.Refused):
                lab.check_state(live)
            with self.assertRaises(lab.Refused):
                lab.check_state(os.path.join(live, "workers"))
        self.assertFalse(lab.is_live(self.state))
