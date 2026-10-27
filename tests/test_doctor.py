"""athena doctor: each part found down is repaired, the healthy ones are left alone, and health.json says what is
wrong. herdr is the fake (tests/fake_herdr.py) here; tests/test_doctor_herdr.py runs doctor against a real,
temporary herdr session."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_composer import TYPED
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import cli, doctor, handoff, hookstatus, hq, ledger, paths

RUNNING = {"match": ["status", "server", "--json"], "stdout": {"result": {"running": True}}}
STOPPED = {"match": ["status", "server", "--json"], "stdout": {"result": {"running": False}}}
GONE = {"match": ["agent", "get"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}',
        "exit": 1}
HQ_SPACE = {"match": ["workspace", "get", "w8"], "stdout": {"result": {"workspace": {"label": "athena"}}}}


def hq_agent(status="idle", after=None):
    rule = {"match": ["agent", "get", "w8:p1"],
            "stdout": {"result": {"agent": {"name": "athena", "agent_status": status,
                                            "agent_session": {"kind": "id", "value": "sess-9"}}}}}
    return {**rule, "after": after} if after else rule


def worker_agent(name, status="idle"):
    return {"match": ["agent", "get", name], "stdout": {"result": {"agent": {"name": name, "agent_status": status}}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.env.set("ATHENA_DOCTOR_GRACE", "0")
        self.env.set("ATHENA_HERDR", f"{sys.executable} {helpers.ROOT / 'tests' / 'fake_herdr.py'}")  # no shim: quick
        self.state = paths.ensure()
        (self.state / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8", "session": "sess-9",
                                                        "routine_at": time.time()}))
        self.tick_watch()
        self.dash = {"running": {"pid": 1, "port": 1, "url": "http://127.0.0.1:1/"}, "health": {"pid": 1}}
        self.board_pid = 4242
        self.mocks = {}
        for name, target, kw in [
            ("dash_start", (doctor.dash, "start"), {"return_value": {"url": "http://127.0.0.1:1/"}}),
            ("board_start", (doctor.board, "start_watcher"), {"return_value": 77}),
            ("server_start", (doctor.launch, "start_server"), {}),
            ("restart", (doctor.hq, "restart"), {"return_value": {"pid": 99}}),
            ("disk", (doctor.shutil, "disk_usage"), {"return_value": mock.Mock(free=500e9)}),
            ("memory", (doctor, "memory_free_percent"), {"return_value": 50}),
            ("ac", (doctor, "on_ac_power"), {"return_value": False}),
            ("hq_socket", (doctor.sockets, "hq_missing"), {"return_value": None}),
            ("worker_socket", (doctor.sockets, "worker_missing"), {"return_value": None}),
            ("context", (doctor.handoff, "hq_context"), {"return_value": None}),
        ]:
            p = mock.patch.object(*target, **kw)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        for target, fn in [((doctor.dash, "running"), lambda: self.dash["running"]),
                           ((doctor.dash, "_health"), lambda port: self.dash["health"]),
                           ((doctor.board, "running_pid"), lambda: self.board_pid)]:
            p = mock.patch.object(*target, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def tick_watch(self, ago=0):
        (self.state / "watch.json").write_text(json.dumps({"ts": time.time() - ago, "workers": {}}))

    def scenario(self, *rules):
        write_scenario(self.tmp.name, list(rules))

    def calls(self):
        return logged_calls(self.tmp.name)

    def starts(self):
        return [c for c in self.calls() if c[:2] == ["agent", "start"]]

    def prompts(self, name):
        return [c[3] for c in self.calls() if c[:3] == ["agent", "prompt", name]]

    def run_doctor(self, fix=True):
        return doctor.run(fix=fix)

    def assert_left_alone(self, *except_):
        for name in ("dash_start", "board_start", "server_start", "restart"):
            if name not in except_:
                self.mocks[name].assert_not_called()
        if "agent start" not in except_:
            self.assertEqual(self.starts(), [])

    def health(self):
        return json.loads((self.state / "health.json").read_text())


class HealthyTest(Fixture):
    def test_everything_up_is_left_alone(self):
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.assertTrue(health["ok"], health)
        self.assertEqual(health["actions"], [])
        self.assert_left_alone()
        self.assertEqual([c for c in self.calls() if c[:2] in (["agent", "prompt"], ["notification", "show"])], [])
        saved = self.health()
        self.assertEqual(set(saved), {"ts", "ok", "problems", "checks", "actions"})
        for check in ("herdr", "hq", "watch", "dash", "board", "disk", "memory"):
            self.assertTrue(saved["checks"][check]["ok"], check)

    def test_no_hq_yet_is_not_a_problem(self):
        (self.state / "hq.json").unlink()
        self.scenario(RUNNING)
        self.assertTrue(self.run_doctor()["ok"])
        self.assert_left_alone()


class RepairTest(Fixture):
    def test_herdr_server_gone_is_started(self):
        self.scenario({**STOPPED, "times": 1}, RUNNING, hq_agent())
        health = self.run_doctor()
        self.mocks["server_start"].assert_called_once_with()
        self.assertTrue(health["checks"]["herdr"]["fixed"])
        self.assert_left_alone("server_start")

    def test_herdr_under_the_service_is_started_by_the_service(self):
        self.scenario({**STOPPED, "times": 1}, RUNNING, hq_agent())
        with mock.patch.object(doctor.service, "start", return_value=True) as start, \
                mock.patch.object(doctor.time, "sleep"):
            health = self.run_doctor()
        start.assert_any_call("herdr")
        self.mocks["server_start"].assert_not_called()
        self.assertIn("herdr service", health["checks"]["herdr"]["detail"])

    def test_dash_down_is_started_bound_to_no_hq(self):
        self.dash["running"] = None
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.mocks["dash_start"].assert_called_once_with(hq=0)
        self.assertTrue(health["checks"]["dash"]["fixed"])
        self.assert_left_alone("dash_start")

    def test_a_dash_that_does_not_answer_is_down(self):
        self.dash["health"] = None
        self.scenario(RUNNING, hq_agent())
        self.run_doctor()
        self.mocks["dash_start"].assert_called_once_with(hq=0)

    def test_board_down_is_started(self):
        self.board_pid = None
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.mocks["board_start"].assert_called_once_with()
        self.assertIn("pid 77", health["checks"]["board"]["detail"])
        self.assert_left_alone("board_start")

    def test_dash_and_board_go_through_their_services_when_installed(self):
        self.dash["running"], self.board_pid = None, None
        self.scenario(RUNNING, hq_agent())
        with mock.patch.object(doctor.service, "start", return_value=True) as start:
            self.run_doctor()
        self.assertEqual([c.args[0] for c in start.call_args_list], ["dash", "board"])
        self.mocks["dash_start"].assert_not_called()
        self.mocks["board_start"].assert_not_called()

    def test_hq_gone_is_resumed_on_its_conversation_never_fresh(self):
        self.scenario(RUNNING, {**hq_agent(), "after": ["agent", "start"]}, GONE, HQ_SPACE)
        health = self.run_doctor()
        (start,) = self.starts()
        claude = start[start.index("--") + 1:]
        self.assertEqual(claude[:2], ["--resume", "sess-9"])
        self.assertEqual(start[start.index("--pane") + 1], "w8:p1")
        self.assertTrue(health["checks"]["hq"]["fixed"])
        self.assertEqual(self.prompts("athena"), [hq.RESTART_ROUTINE])
        self.assert_left_alone("agent start")

    def test_hq_gone_waits_for_herdr_to_resume_it_first(self):
        self.env.set("ATHENA_DOCTOR_GRACE", "90")
        self.scenario(RUNNING, GONE, HQ_SPACE)
        health = self.run_doctor()
        self.assertEqual(self.starts(), [])
        self.assertFalse(health["ok"])
        self.assertIn("waiting", health["checks"]["hq"]["detail"])

    def test_hq_with_no_known_conversation_is_flagged_not_started(self):
        (self.state / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8"}))
        self.scenario(RUNNING, GONE, HQ_SPACE)
        health = self.run_doctor()
        self.assertEqual(self.starts(), [])
        self.assertFalse(health["checks"]["hq"]["ok"])

    def test_watch_down_restarts_hq_on_its_conversation(self):
        self.tick_watch(ago=20 * 60)
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.mocks["restart"].assert_called_once_with(update=False, unattended=True)
        self.assertTrue(health["checks"]["watch"]["fixed"])
        self.assert_left_alone("restart")

    def test_watch_down_waits_while_hq_is_busy(self):
        self.tick_watch(ago=20 * 60)
        self.scenario(RUNNING, hq_agent("working"))
        health = self.run_doctor()
        self.mocks["restart"].assert_not_called()
        self.assertIn("next idle", health["checks"]["watch"]["detail"])
        self.assertEqual(json.loads((self.state / "doctor.json").read_text()).get("attempts", {}).get("watch"), None)

    def test_hqs_dead_socket_restarts_hq_on_its_conversation(self):
        self.mocks["hq_socket"].return_value = "/tmp/cc-socks/1.sock"
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.mocks["restart"].assert_called_once_with(update=False, unattended=True)
        self.assertTrue(health["checks"]["socket:hq"]["fixed"])

    def test_a_workers_dead_socket_restarts_it_in_place_on_its_conversation(self):
        ledger.append("spawn", "abc-1", repo="/r", branch="you/abc-1", path="/w/abc-1", pane="w5:p1",
                      claude_args=["--name", "abc-1", "--settings", "/s/abc-1.json"])
        hookstatus.apply({"hook_event_name": "SessionStart", "session_id": "w-sess"}, "abc-1")
        self.mocks["worker_socket"].side_effect = lambda name: "/tmp/cc-socks/2.sock" if name == "abc-1" else None
        gone = {**GONE, "match": ["agent", "get", "abc-1"], "after": ["agent", "prompt", "abc-1", "/exit"]}
        self.scenario(RUNNING, hq_agent(), {**worker_agent("abc-1"), "after": ["agent", "start"]}, gone,
                      worker_agent("abc-1"))
        health = self.run_doctor()
        self.assertIn(["agent", "prompt", "abc-1", "/exit"], self.calls())
        (start,) = self.starts()
        self.assertEqual(start[:3], ["agent", "start", "abc-1"])
        self.assertEqual(start[start.index("--pane") + 1], "w5:p1")
        self.assertEqual(start[start.index("--") + 1:], ["--resume", "w-sess", "--name", "abc-1", "--settings",
                                                        "/s/abc-1.json"])
        self.assertTrue(health["checks"]["socket:abc-1"]["fixed"])

    def test_a_worker_someone_is_typing_to_is_not_restarted(self):
        ledger.append("spawn", "abc-1", repo="/r", branch="you/abc-1", path="/w/abc-1", pane="w5:p1",
                      claude_args=["--name", "abc-1"])
        hookstatus.apply({"hook_event_name": "SessionStart", "session_id": "w-sess"}, "abc-1")
        self.mocks["worker_socket"].side_effect = lambda name: "/tmp/cc-socks/2.sock"
        self.scenario(RUNNING, hq_agent(), worker_agent("abc-1"),
                      {"match": ["agent", "read", "abc-1"], "stdout": TYPED})
        health = self.run_doctor()
        self.assertNotIn(["agent", "prompt", "abc-1", "/exit"], self.calls())
        self.assertEqual(self.starts(), [])
        self.assertIn("empty input box", health["checks"]["socket:abc-1"]["detail"])

    def test_check_only_repairs_nothing(self):
        self.dash["running"], self.board_pid = None, None
        self.tick_watch(ago=20 * 60)
        self.scenario(RUNNING, GONE, HQ_SPACE)
        health = self.run_doctor(fix=False)
        self.assert_left_alone()
        self.assertEqual({p["check"] for p in health["problems"]}, {"dash", "board", "hq"})
        self.assertEqual([c for c in self.calls() if c[:2] == ["notification", "show"]], [])


class BackoffTest(Fixture):
    def test_a_repair_that_keeps_failing_backs_off_then_stops_and_is_flagged_once(self):
        self.dash["running"] = None
        self.mocks["dash_start"].side_effect = RuntimeError("port 2843 is in use")
        self.scenario(RUNNING, hq_agent())
        tries, now = [], time.time()
        for minute in range(0, 70):
            self.tick_watch(ago=-minute * 60)  # the watch keeps ticking as the clock runs on
            with mock.patch.object(doctor.time, "time", return_value=now + minute * 60):
                self.run_doctor()
            tries.append(self.mocks["dash_start"].call_count)
        self.assertEqual(tries[0], 1)
        self.assertEqual(tries[1], 1)  # backing off
        self.assertEqual(max(tries[:59]), 3)  # three in the hour, then it stops
        detail = self.health()["checks"]["dash"]["detail"]
        self.assertIn("gave up", detail)
        notes = [c for c in self.calls() if c[:2] == ["notification", "show"]]
        self.assertEqual(len(notes), 1)  # told when it first went wrong, not every minute
        self.assertIn("dash", notes[0][notes[0].index("--body") + 1])

    def test_a_problem_keeps_its_since(self):
        self.board_pid = None
        self.mocks["board_start"].side_effect = RuntimeError("no")
        self.scenario(RUNNING, hq_agent())
        first = self.run_doctor()["problems"][0]["since"]
        with mock.patch.object(doctor.time, "time", return_value=time.time() + 600):
            again = self.run_doctor()
        self.assertEqual(again["problems"][0]["since"], first)

    def test_a_second_doctor_at_once_does_nothing(self):
        self.scenario(RUNNING, hq_agent())
        with doctor._lock() as held:
            self.assertTrue(held)
            self.assertEqual(doctor.run(), {"skipped": "another athena doctor is running"})


class ResumedTest(Fixture):
    def resumed_hq(self, ago=5):
        (self.state / "hq-start.json").write_text(json.dumps({"source": "resume", "session_id": "sess-9",
                                                              "ts": time.time() - ago}))
        record = json.loads((self.state / "hq.json").read_text())
        record["routine_at"] = time.time() - 3600
        (self.state / "hq.json").write_text(json.dumps(record))

    def test_a_resumed_hq_gets_its_start_routine_once(self):
        self.resumed_hq()
        self.scenario(RUNNING, hq_agent())
        self.run_doctor()
        self.assertEqual(self.prompts("athena"), [hq.RESTART_ROUTINE])
        self.run_doctor()
        self.assertEqual(len(self.prompts("athena")), 1)

    def test_not_while_the_human_types_in_hq(self):
        self.resumed_hq()
        self.scenario(RUNNING, hq_agent(), {"match": ["agent", "read", "athena"], "stdout": TYPED})
        self.run_doctor()
        self.assertEqual(self.prompts("athena"), [])

    def test_a_fresh_start_or_an_old_resume_gets_nothing(self):
        self.resumed_hq(ago=3 * 3600)
        self.scenario(RUNNING, hq_agent())
        self.run_doctor()
        (self.state / "hq-start.json").write_text(json.dumps({"source": "startup", "ts": time.time()}))
        self.run_doctor()
        self.assertEqual(self.prompts("athena"), [])

    def worker(self, name, before):
        ledger.append("spawn", name, repo="/r", branch=f"you/{name}", path=f"/w/{name}", pane="w5:p1")
        if before == "working":
            hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, name)
        elif before == "blocked":
            hookstatus.apply({"hook_event_name": "Stop",
                              "last_assistant_message": 'ATHENA-REPORT {"status":"BLOCKED"}'}, name)
        hookstatus.apply({"hook_event_name": "SessionStart", "source": "resume", "session_id": "s"}, name)

    def test_a_worker_resumed_mid_turn_is_told_to_carry_on_once(self):
        self.worker("abc-1", "working")
        self.worker("abc-2", "blocked")
        self.scenario(RUNNING, hq_agent(), worker_agent("abc-1"), worker_agent("abc-2"))
        self.run_doctor()
        self.run_doctor()
        self.assertEqual(self.prompts("abc-1"), [doctor.CONTINUE])
        self.assertEqual(self.prompts("abc-2"), [])  # BLOCKED waits for its answer


class ContextTest(Fixture):
    def test_past_the_line_hq_is_asked_once_to_write_its_threads_and_rotate(self):
        self.mocks["context"].return_value = {"percent": 72.0, "tokens": 144000, "window": 200000}
        self.scenario(RUNNING, hq_agent())
        self.run_doctor()
        self.run_doctor()
        (ask,) = self.prompts("athena")
        self.assertIn(str(handoff.threads_file()), ask)
        self.assertIn("athena hq rotate", ask)
        self.assertNotIn(["agent", "prompt", "athena", "/exit"], self.calls())  # HQ rotates itself, at idle

    def test_under_the_line_nothing_is_asked(self):
        self.mocks["context"].return_value = {"percent": 30.0, "tokens": 60000, "window": 200000}
        self.scenario(RUNNING, hq_agent())
        self.run_doctor()
        self.assertEqual(self.prompts("athena"), [])


class FlagsTest(Fixture):
    def test_low_disk_and_memory_are_flagged_never_fixed(self):
        self.mocks["disk"].return_value = mock.Mock(free=3e9)
        self.mocks["memory"].return_value = 4
        self.scenario(RUNNING, hq_agent())
        health = self.run_doctor()
        self.assertEqual({p["check"] for p in health["problems"]}, {"disk", "memory"})
        self.assert_left_alone()

    def test_caffeinate_while_a_worker_is_mid_turn_on_ac_power(self):
        ledger.append("spawn", "abc-1", repo="/r", branch="you/abc-1", path="/w/abc-1", pane="w5:p1")
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        self.mocks["ac"].return_value = True
        self.scenario(RUNNING, hq_agent(), worker_agent("abc-1", "working"))
        with mock.patch.object(doctor, "spawn_caffeinate", return_value=31337) as spawn, \
                mock.patch.object(doctor.sys, "platform", "darwin"):
            self.run_doctor()
            spawn.assert_called_once_with()
            with mock.patch.object(doctor, "_alive", return_value=True):
                self.run_doctor()  # the last one still runs: not a second
            spawn.assert_called_once_with()
            self.mocks["ac"].return_value = False
            with mock.patch.object(doctor, "_alive", return_value=False):
                self.run_doctor()  # on battery: none
            spawn.assert_called_once_with()


class RoundOneTest(Fixture):
    """The findings of review round 1, one test each."""

    def test_the_cap_holds_across_healthy_ticks(self):
        # A watch that stalls again a few minutes after each repair: at most 3 restarts an hour, then flagged.
        self.scenario(RUNNING, hq_agent())
        now = time.time()
        for minute in range(0, 60, 3):
            stale = (minute // 3) % 2 == 0
            self.tick_watch(ago=-minute * 60 + (20 * 60 if stale else 0))
            with mock.patch.object(doctor.time, "time", return_value=now + minute * 60):
                health = self.run_doctor()
        self.assertEqual(self.mocks["restart"].call_count, 3)
        self.tick_watch(ago=-60 * 60 + 20 * 60)
        with mock.patch.object(doctor.time, "time", return_value=now + 59 * 60):
            health = self.run_doctor()
        self.assertIn("gave up", health["checks"]["watch"]["detail"])
        self.assertEqual(self.mocks["restart"].call_count, 3)

    def test_no_known_conversation_means_no_restart_for_the_watch_or_the_socket(self):
        (self.state / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8", "routine_at": time.time()}))
        no_session = {"match": ["agent", "get"], "stdout": {"result": {"agent": {"name": "athena",
                                                                                  "agent_status": "idle"}}}}
        self.tick_watch(ago=20 * 60)
        self.mocks["hq_socket"].return_value = "/tmp/cc-socks/1.sock"
        self.scenario(RUNNING, no_session)
        health = self.run_doctor()
        self.mocks["restart"].assert_not_called()
        for check in ("watch", "socket:hq"):
            self.assertIn("never starts a fresh HQ", health["checks"][check]["detail"])

    def test_a_worker_with_background_work_is_not_exited(self):
        from tests.test_retire import DIALOG
        ledger.append("spawn", "abc-1", repo="/r", branch="you/abc-1", path="/w/abc-1", pane="w5:p1",
                      claude_args=["--name", "abc-1"])
        hookstatus.apply({"hook_event_name": "SessionStart", "session_id": "w-sess"}, "abc-1")
        self.mocks["worker_socket"].side_effect = lambda name: "/tmp/cc-socks/2.sock"
        self.scenario(RUNNING, hq_agent(), worker_agent("abc-1"),
                      {"match": ["agent", "read", "abc-1"], "stdout": DIALOG, "after": ["agent", "prompt", "abc-1", "/exit"]})
        health = self.run_doctor()
        self.assertIn(["agent", "send-keys", "abc-1", "esc"], self.calls())
        self.assertNotIn(["agent", "send-keys", "abc-1", "enter"], self.calls())
        self.assertEqual(self.starts(), [])
        self.assertIn("background work", health["checks"]["socket:abc-1"]["detail"])

    def test_a_herdr_prompt_that_fails_never_stops_the_run(self):
        self.mocks["context"].return_value = {"percent": 72.0, "tokens": 144000, "window": 200000}
        self.dash["running"] = None
        err = '{"error":{"code":"agent_not_ready","message":"busy"}}'
        self.scenario(RUNNING, hq_agent(), {"match": ["agent", "prompt"], "stdout": "", "stderr": err, "exit": 1})
        health = self.run_doctor()
        self.assertIn("the check failed", health["checks"]["hq context"]["detail"])
        self.mocks["dash_start"].assert_called_once_with(hq=0)  # the parts after it still ran
        self.assertIn("board", health["checks"])
        self.assertTrue((self.state / "health.json").exists())
        self.assertTrue((self.state / "doctor.json").exists())

    def test_a_bare_athena_attaches_when_doctor_breaks(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from athena_lib import launch
        self.scenario(RUNNING, hq_agent())
        with mock.patch.object(cli.doctor, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(launch.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(launch, "can_attach", return_value=True), \
                mock.patch.object(launch, "server_running", return_value=True), \
                mock.patch.object(launch.os, "execvp") as execvp, mock.patch.object(hq.board, "start_watcher"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli.main(["hq"])
        self.assertEqual(code, 0)
        execvp.assert_called_once()
        self.assertIn("did not run: boom", err.getvalue())


class CliTest(Fixture):
    def test_exit_code_and_quiet_output(self):
        import contextlib
        import io
        self.board_pid = None
        self.mocks["board_start"].side_effect = RuntimeError("no")
        self.scenario(RUNNING, hq_agent())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["doctor", "--fix", "--quiet"])
        self.assertEqual(code, 1)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith("BAD board") for line in lines), lines)
        self.assertFalse(any(line.startswith("ok ") for line in lines))


if __name__ == "__main__":
    unittest.main()
