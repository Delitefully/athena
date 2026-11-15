"""The supervisor (kernel/supervisor.py) that the one launchd job runs: kept-alive children restarted with a growing delay,
`every` children on a clock (a fake one here), Quit's mark, doctor's asks, and the real `athena services run` with only fake
children under it: its process group, its TERM, and a child an earlier supervisor left running."""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from unittest import mock

from harness import ROOT, store
from supervised import Supervised, fake

from athena.caps import dash, doctor
from athena.kernel import caps, supervisor

T0 = 1_000_000.0


class KeptAlive(Supervised):
    def test_each_is_a_child_and_one_that_crashes_starts_again_after_a_delay(self):
        self.mode("crasher", "exit 1")
        held = self.supervisor([fake("steady"), fake("crasher")])
        held.step(T0)
        self.assertEqual({c.name: c.state for c in held.children}, {"steady": "running", "crasher": "running"})
        self.assertEqual({c.proc.pid for c in held.children}, {pid for _, pid, _, _ in self.starts()}, "each started by it")
        self.assertTrue(all(ppid == os.getpid() for _, _, _, ppid in self.starts()), "its own children")
        self.exit_of(held, "crasher")
        held.step(T0 + 0.1)
        crasher = held.children[1]
        self.assertEqual((crasher.state, crasher.last_exit, crasher.delay), ("backing off", 1, supervisor.FIRST_DELAY))
        held.step(T0 + 0.5)
        self.assertEqual(len(self.starts("crasher")), 1, "not before its delay")
        held.step(T0 + 0.1 + supervisor.FIRST_DELAY)
        self.assertEqual(len(self.starts("crasher")), 2)
        self.assertEqual(len(self.starts("steady")), 1)

    def test_one_ended_by_a_signal_starts_again(self):
        held = self.supervisor([fake("steady")])
        held.step(T0)
        self.starts()
        held.children[0].proc.kill()  # the handle it holds: a crash by signal, as launchd's KeepAlive saw one
        self.exit_of(held, "steady")
        held.step(T0 + 1)
        self.assertEqual((held.children[0].state, held.children[0].last_exit), ("backing off", -9))
        held.step(T0 + 1 + supervisor.FIRST_DELAY)
        self.assertEqual(held.children[0].state, "running")
        self.assertEqual(len(self.starts("steady")), 2)

    def test_a_clean_exit_stays_down_until_the_supervisor_next_starts(self):
        self.mode("menubar", "exit 0")  # as `menubar run` does when the app is not built
        held = self.supervisor([fake("menubar", interactive=True)])
        held.step(T0)
        self.exit_of(held, "menubar")
        for later in (1, 60, 3600, 86400):
            held.step(T0 + later)
        self.assertEqual((held.children[0].state, len(self.starts("menubar"))), ("stopped", 1))
        again = self.supervisor([fake("menubar", interactive=True)])
        again.step(T0 + 86401)
        self.assertEqual(len(self.starts("menubar")), 2, "the next supervisor starts it again")

    def test_the_delay_doubles_to_its_cap_and_resets_after_a_healthy_run(self):
        self.mode("crasher", "exit 1")
        held = self.supervisor([fake("crasher")])
        child = held.children[0]
        now, delays = T0, []
        held.step(now)
        for _ in range(9):
            self.exit_of(held, "crasher")
            held.step(now)
            delays.append(child.delay)
            now = child.retry_at
            held.step(now)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(supervisor.MAX_DELAY, 60)
        self.starts()
        self.mode("crasher", "fail")  # the next run lasts until released, then exits 1
        self.exit_of(held, "crasher")
        held.step(now)
        now = child.retry_at
        held.step(now)
        self.assertEqual(child.state, "running")
        held.step(now + supervisor.HEALTHY_AFTER)
        self.assertEqual(child.delay, 0, f"reset after {supervisor.HEALTHY_AFTER:g}s of running")
        self.release("crasher")
        self.exit_of(held, "crasher")
        held.step(now + supervisor.HEALTHY_AFTER + 1)
        self.assertEqual((child.state, child.delay), ("backing off", supervisor.FIRST_DELAY), "a crash after it starts over")

    def test_one_crash_looping_child_never_delays_or_stops_the_others(self):
        self.mode("crasher", "exit 1")
        self.mode("tick", "exit 0")
        held = self.supervisor([fake("crasher"), fake("steady"), fake("tick", every=60, keep_alive=False)])
        now = T0
        for _ in range(40):
            began = time.monotonic()
            held.step(now)
            self.assertLess(time.monotonic() - began, 2, "a step never waits on a crashing child")
            self.exit_of(held, "crasher")
            self.exit_of(held, "tick")
            now += 15
        self.assertEqual(len(self.starts("steady")), 1)
        self.assertEqual(held.children[1].state, "running")
        self.assertEqual(len(self.starts("tick")), 10, "every 60s over 600s, from the first step")
        self.assertGreater(len(self.starts("crasher")), 5)


class Every(Supervised):
    def test_it_runs_on_its_interval(self):
        self.mode("doctor", "exit 0")
        held = self.supervisor([fake("doctor", every=60, keep_alive=False)])
        held.step(T0)
        self.exit_of(held, "doctor")
        held.step(T0 + 1)
        self.assertEqual(held.children[0].state, "waiting")
        held.step(T0 + 59)
        self.assertEqual(len(self.starts("doctor")), 1)
        held.step(T0 + 60)
        self.assertEqual(len(self.starts("doctor")), 2)
        self.exit_of(held, "doctor")
        held.step(T0 + 119)
        held.step(T0 + 120)
        self.assertEqual(len(self.starts("doctor")), 3)

    def test_after_a_long_gap_it_runs_once_not_once_per_missed_interval(self):
        self.mode("doctor", "exit 0")
        held = self.supervisor([fake("doctor", every=60, keep_alive=False)])
        held.step(T0)
        self.exit_of(held, "doctor")
        held.step(T0 + 8 * 3600)  # the Mac slept through 480 of them
        self.exit_of(held, "doctor")
        for later in (0.5, 1, 30, 59):
            held.step(T0 + 8 * 3600 + later)
        self.assertEqual(len(self.starts("doctor")), 2)
        held.step(T0 + 8 * 3600 + 60)
        self.assertEqual(len(self.starts("doctor")), 3)

    def test_a_run_still_going_at_its_time_is_not_started_twice(self):
        self.mode("hourly", "keep")
        held = self.supervisor([fake("hourly", every=3600)])
        held.step(T0)
        for later in (3600, 3601, 7200, 7300):
            held.step(T0 + later)
        self.assertEqual(len(self.starts("hourly")), 1)
        self.assertEqual(len(self.alive("hourly")), 1)
        self.release("hourly")
        self.exit_of(held, "hourly")
        os.unlink(self.kids / "release-hourly")
        held.step(T0 + 7400)
        self.assertEqual(len(self.starts("hourly")), 1, "the slot it ran through is skipped, not queued")
        held.step(T0 + 10800)
        self.assertEqual(len(self.starts("hourly")), 2)

    def test_the_clock_going_back_does_not_hold_it_off_for_longer_than_its_interval(self):
        self.mode("doctor", "exit 0")
        held = self.supervisor([fake("doctor", every=60, keep_alive=False)])
        held.step(T0)
        self.exit_of(held, "doctor")
        held.step(T0 - 3600)
        held.step(T0 - 3600 + 60)
        self.assertEqual(len(self.starts("doctor")), 2)


class Asked(Supervised):
    def test_quit_stops_what_the_mark_names_watchdog_first_and_start_brings_them_back_watchdog_last(self):
        names = ("doctor", "dash", "watch", "menubar", "beacon")
        held = self.supervisor([fake(n, every=60 if n == "doctor" else None, keep_alive=n != "doctor") for n in names])
        self.mode("doctor", "keep")
        held.step(T0)
        self.starts()
        store.write_json(store.path("services-stopped.json"), {"at": T0, "jobs": ["doctor", "dash", "watch"]})
        held.step(T0 + 1)
        self.assertEqual(self.stops(), ["doctor", "dash", "watch"])
        self.assertEqual(sorted(c.name for c in held.children if c.proc), ["beacon", "menubar"])
        self.assertTrue(all(c.held and c.state == "stopped" for c in held.children[:3]))
        for later in (60, 120, 600):
            held.step(T0 + later)
        self.assertEqual(len(self.starts()), 5, "held: neither its timer nor a crash brings one back")
        held.stop_all()  # the Mac logs out
        login = self.supervisor([fake(n) for n in names])
        login.step(T0 + 700)
        self.assertEqual(sorted(c.name for c in login.children if c.proc), ["beacon", "menubar"], "the mark holds across a login")
        store.path("services-stopped.json").unlink()
        before = len(self.spawned)
        login.step(T0 + 800)
        tags = {pid: tag for tag, pid, *_ in self.starts()}
        self.assertEqual([tags[pid] for pid in self.spawned[before:]], ["dash", "watch", "doctor"])
        self.assertEqual(len(self.alive()), 5)

    def test_an_ask_restarts_a_child_now_and_clears_its_backoff(self):
        self.mode("dash", "exit 1")
        held = self.supervisor([fake("dash")])
        child = held.children[0]
        now = T0
        held.step(now)
        for _ in range(5):
            self.exit_of(held, "dash")
            held.step(now)
            now = child.retry_at
            held.step(now)
        self.exit_of(held, "dash")
        held.step(now)
        self.assertEqual((child.state, child.delay), ("backing off", 32))
        self.starts()
        self.mode("dash", "run")
        store.ensure("run", "services-ask")
        store.write_text(store.path("run", "services-ask", "dash"), "now\n")
        held.step(now + 1)
        self.assertEqual((child.state, child.delay), ("running", 0))
        self.starts()
        self.assertEqual(len(self.alive("dash")), 1)
        self.assertEqual(list(store.path("run", "services-ask").iterdir()), [], "an ask is taken once")
        store.write_text(store.path("run", "services-ask", "dash"), "now\n")
        held.step(now + 2)
        self.assertEqual(len(self.starts("dash")), 7, "a child that has only just started is left to come up")

    def test_a_dash_in_backoff_and_a_doctor_pass_leave_exactly_one_dash(self):
        """Doctor's dash repair races nothing: it asks the supervisor, and starts no copy of its own beside it."""
        self.mode("dash", "exit 1")
        held = self.supervisor([fake("dash")], lock=True)
        child = held.children[0]
        now = time.time()
        held.step(now)
        for _ in range(4):
            self.exit_of(held, "dash")
            held.step(now)
            now = child.retry_at
            held.step(now)
        self.exit_of(held, "dash")
        held.step(now)
        self.assertEqual((child.state, child.delay), ("backing off", 16))
        self.starts()
        self.mode("dash", "run")
        self.stepping(held)
        before = len(self.starts("dash"))
        doctor.run(caps.CTX, fix=True)
        health = store.path("health.json").read_text()
        self.assertIn("the supervisor's dashboard did not answer", health, "the repair asked; a fake dash serves nothing")
        self.wait_for(lambda: child.state == "running", message="the supervisor restarted it when asked")
        self.assertEqual(len(self.starts("dash")), before + 1)
        self.assertEqual(len(self.alive("dash")), 1, "one dash")
        self.assertFalse(store.path("run", "dash.json").exists(), "no copy started outside the supervisor")
        self.assertEqual(child.delay, 0)

    def test_with_no_supervisor_the_repairs_start_their_own_copy_as_before(self):
        from athena.caps import services, watch
        self.assertFalse(services.supervisor_runs())
        self.assertFalse(services.ask_restart("dash"))
        started = []
        with mock.patch.object(watch.subprocess, "Popen", side_effect=lambda *a, **k: started.append(a[0])):
            watch._start_detached()
        self.assertEqual(started[0][1:], ["watch", "--tagged", "--wake"])


class TheProcess(Supervised):
    """The real `athena services run`, with fake services only."""

    def started(self, count, timeout=30):
        return self.wait_for(lambda: len(self.starts()) >= count and self.starts(), timeout,
                             f"{count} children did not start:\n{self.run_log()}")

    def test_its_children_share_its_process_group_so_launchd_clears_them_with_the_job(self):
        proc = self.services_run([["one", None, True], ["two", None, True], ["tick", 3600, True]])
        rows = self.started(3)
        group = os.getpgid(proc.pid)
        for tag, pid, pgid, ppid in rows:
            self.assertEqual((pgid, ppid), (group, proc.pid), f"{tag}: in its group, and its own child")
        self.assertTrue(store.path("run", "services.json").exists())
        listed = json.loads(store.path("run", "services.json").read_text())["children"]
        self.assertEqual({n: r["state"] for n, r in listed.items()}, {"one": "running", "two": "running", "tick": "running"})
        self.assertEqual(Path(store.path("logs", "service-one.log")).exists(), True, "each logs to its own file")

    def test_on_term_it_stops_its_children_and_none_outlives_it(self):
        for tag in ("one", "two"):
            self.mode(tag, "keep")  # a child that would outlive a supervisor that left it
        proc = self.services_run([["one", None, True], ["two", None, True]])
        self.started(2)
        self.assertEqual(len(self.alive()), 2)
        proc.terminate()
        self.assertEqual(proc.wait(30), 0)
        self.assertEqual(self.alive(), [], "each child was stopped and waited for before it exited")
        self.assertEqual(sorted(self.stops()), ["one", "two"])

    def test_one_started_while_a_child_of_an_earlier_one_runs_starts_no_second_copy(self):
        self.mode("one", "keep")
        first = self.services_run([["one", None, True]])
        (_, old, _, _), = self.started(1)
        first.kill()  # killed outright, as launchd's process group kill would not be here: its child lives on
        first.wait(30)
        self.assertEqual(self.alive("one"), [old])
        second = self.services_run([["one", None, True]])
        self.wait_for(lambda: json.loads(store.path("run", "services.json").read_text())["children"]["one"]["state"] == "waiting"
                      if store.path("run", "services.json").exists() else False, message=self.run_log())
        time.sleep(1)
        self.assertEqual(len(self.starts("one")), 1, "no second copy while the first lives")
        self.release(old)
        rows = self.wait_for(lambda: len(self.starts("one")) == 2 and self.starts("one"), message=self.run_log())
        self.assertEqual(rows[1][3], second.pid, "then its own")
        self.assertEqual(self.alive("one"), [rows[1][1]])


class OnlyThroughHandles(Supervised):
    def test_neither_the_supervisor_nor_the_services_command_signals_anything_but_its_own_children(self):
        for path in (ROOT / "athena/kernel/supervisor.py", ROOT / "athena/caps/services.py"):
            text = path.read_text()
            for pattern in (r"os\.kill\b", r"killpg", r"sandbox\.kill", r"send_signal", r"\.pid\b.*kill"):
                self.assertIsNone(re.search(pattern, text), f"{path.name}: {pattern}")
        text = (ROOT / "athena/kernel/supervisor.py").read_text()
        self.assertEqual(set(re.findall(r"proc\.(terminate|kill)\(\)", text)), {"terminate", "kill"}, "only through its handles")
        for pattern in ("start_new_session", "setsid", "setpgid", "process_group"):
            self.assertNotIn(pattern, text, "children stay in its process group")
