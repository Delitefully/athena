"""Gap 9's refusal: with the signal sandbox turned on (opt-in), a Claude athena-rex starts runs under it or doesn't start."""
import os
from unittest import mock

from harness import Case, spawn_record, store

from athena_rex.caps import hq, spawn
from athena_rex.kernel import agents, caps, sandbox


class On(Case):
    """The sandbox is opt-in: these turn it on, as a human would, and keep it on through any config a test writes."""

    def setUp(self):
        super().setUp()
        self.write_config()

    def write_config(self, **fields):
        fields["rex"] = {"signal_sandbox": True, **(fields.get("rex") or {})}
        super().write_config(**fields)


class Launch(On):
    def test_spawns_and_hqs_built_launch_command_runs_claude_under_the_profile(self):
        script = agents.launcher("w1", self.tmp, ["--name", "w1"]).read_text()
        exec_line = [line for line in script.splitlines() if line.startswith("exec ")][0]
        self.assertTrue(exec_line.startswith(f"exec {sandbox.SANDBOX_EXEC} -p '{sandbox.PROFILE}' env {sandbox.MARK}=1 env "), exec_line)
        self.assertIn("(deny signal)(allow signal (target same-sandbox))", sandbox.PROFILE)
        hq.open_hq(focus=False, routine=False)
        hq_line = [line for line in store.path("run", "athena.sh").read_text().splitlines() if line.startswith("exec ")][0]
        self.assertTrue(hq_line.startswith(f"exec {sandbox.SANDBOX_EXEC} -p '{sandbox.PROFILE}' env {sandbox.MARK}=1 env ATHENA_ROLE=hq "), hq_line)

    def test_the_profile_is_tried_before_a_launch(self):
        self.assertIsNone(sandbox.problem())
        tried = (self.tmp / "world-calls.log.sandbox-exec").read_text()
        self.assertEqual(tried, f"sandbox-exec -p {sandbox.PROFILE} /usr/bin/true\n")


class Refusal(On):
    def missing(self):
        return mock.patch.object(sandbox, "SANDBOX_EXEC", str(self.tmp / "no-sandbox-exec"))

    def broken(self):
        """sandbox-exec as it answers a profile it can't load (exit 65), from a stub: the canary keeps the real one out of tests."""
        stub = self.tmp / "broken-sandbox-exec"
        stub.write_text('#!/bin/sh\necho "sandbox-exec: unbound variable: no-such-operation at <input string>" >&2\nexit 65\n')
        stub.chmod(0o755)
        return mock.patch.dict(os.environ, {sandbox.PROBE_ENV: str(stub)})

    def test_a_missing_sandbox_exec_refuses_spawn_before_anything_is_made(self):
        repo = self.repo()
        self.write_config(worktree_root=str(self.tmp / "wt"))
        with self.missing(), mock.patch.object(agents, "start") as start:
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", timeout=0.1, fetch=False)
        self.assertIn("no-sandbox-exec is missing", str(caught.exception))
        self.assertIn("rex.signal_sandbox: false", str(caught.exception))
        start.assert_not_called()
        self.assertFalse((self.tmp / "wt").exists(), "no worktree is made for a launch that is refused")

    def test_a_profile_that_wont_load_refuses_hq_and_the_launcher(self):
        with self.broken():
            with self.assertRaises(hq.UserError) as caught:
                hq.open_hq(focus=False, routine=False)
            self.assertIn("the signal sandbox's profile did not load", str(caught.exception))
            self.assertIn("no-such-operation", str(caught.exception))
            self.assertFalse(store.path("run", "athena.sh").exists())
            with self.assertRaises(sandbox.Unavailable):
                agents.launcher("w1", self.tmp, ["--name", "w1"])

    def test_off_in_config_launches_without_it_and_doctor_says_off(self):
        self.write_config(rex={"signal_sandbox": False})
        with self.missing():
            self.assertNotIn("sandbox-exec", agents.launcher("w1", self.tmp, ["--name", "w1"]).read_text())
            hq.open_hq(focus=False, routine=False)
            check = hq._sandbox_check()
        self.assertTrue(check.ok)
        self.assertTrue(check.detail.startswith("off"), check.detail)
        self.assertIn("without the signal sandbox", check.detail)

    def test_doctor_flags_a_sandbox_that_cant_start(self):
        for broken in (self.missing, self.broken):
            with broken():
                check = hq._sandbox_check()
            self.assertFalse(check.ok)
            self.assertIn("Claudes won't start", check.detail)

    def test_a_broken_sandbox_refuses_hq_restart_and_rotate_before_hq_is_stopped(self):
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        with self.broken(), mock.patch.object(agents, "graceful_exit") as stop:
            with self.assertRaises(hq.UserError):
                hq._cycle(hq.load(), ["--resume", "conv"], "hi", "restart")
            with self.assertRaises(hq.UserError) as caught:
                hq.rotate()
        self.assertIn("profile did not load", str(caught.exception))
        stop.assert_not_called()
        self.assertTrue(store.path("hq-start.json").exists())
        self.assertFalse(store.path("handoff.md").exists())



class Kernel(Case):
    def test_under_the_profiles_signal_rules_an_outsider_is_refused(self):
        """scripts/test.sh runs every test under the signal rules of sandbox.PROFILE, with a sleep it started outside them."""
        # Don't "fix" this to expect kill(-1, ...) to fail. On macOS it returns even under (deny signal) alone: kill() is POSIX,
        # so -1 counts the caller itself, which may always signal itself. Delivery to anything outside is refused all the same,
        # as below and in scripts/sandbox-proof.sh, which shows kill(-1, 0) returning while the outsider is refused and lives.
        # Signal 0 only: a real signal to -1 is never sent, here or anywhere, and the canary trips on any pid of 1 or less.
        from harness import SANDBOX
        rules = open(SANDBOX["PROFILE"]).read()
        self.assertIn("(deny signal)\n(allow signal (target same-sandbox))", rules)
        self.assertIn("(deny signal)(allow signal (target same-sandbox))", sandbox.PROFILE)
        outsider = int(SANDBOX["OUTSIDER"])
        self.assertGreater(outsider, 1)
        with self.assertRaises(PermissionError):
            os.kill(outsider, 0)
