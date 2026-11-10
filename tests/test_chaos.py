"""The chaos catalogue, injected on the fake backend: one test per case, named by its id. The cases that need real processes and a real
Rex server are in scripts/chaos-rex.py."""
import argparse
import calendar
import errno
import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock

from harness import BIN, Case, ROOT, git, spawn_record, store

from athena_rex.caps import brief, doctor, hooks, hq, items, prs, retire, slack, spawn, status, watch
from athena_rex.caps.dash import view
from athena_rex.cli import UserError
from athena_rex.kernel import agents, canary, caps, config, gitops, ledger
from athena_rex.kernel.backend import BackendError, Place

NOON_IN_LA = calendar.timegm((2026, 11, 6, 20, 0, 0))
REPORT = 'all done\nATHENA-REPORT {"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["%s"]}'


class _Chaos(Case):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch("athena_rex.caps.dash.on"), mock.patch.object(prs, "fetch_due", lambda now: False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        prs._latest.clear()

    def _alive(self, **pids):
        """A fake ps that says each worker's Claude runs, under its pid."""
        rows = "".join(f"echo '{pid} S claude --name {name.replace('_', '-')} --settings x'\n" for name, pid in pids.items())
        script = self.tmp / "ps-alive"
        script.write_text(f"#!/bin/sh\n{rows}")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        store.ensure("run")
        for name, pid in pids.items():
            agents.pid_file(name.replace("_", "-")).write_text(str(pid))

    def _block(self, name="w", screen="", status_=None, **fields):
        """A worker in a real space of the fake backend."""
        place = caps.CTX.backend.create(name, self.tmp, ["x"])
        spawn_record(name, self.tmp, self.tmp, block=False, space=place.space, block_id=None, **fields)
        ledger.append("update", name, space=place.space, block=place.block)
        self._screen(place, screen, status_)
        return place

    def _screen(self, place, screen, status_=None):
        path = os.environ["ATHENA_FAKE_BACKEND"]
        data = json.load(open(path))
        data["spaces"][place.space]["blocks"][place.block].update(screen=screen, status=status_)
        json.dump(data, open(path, "w"))

    def _fake(self):
        return json.load(open(os.environ["ATHENA_FAKE_BACKEND"]))

    def _checks(self, name):
        return [c for c in doctor.checks(caps.CTX) if c.name == name]

    def _gh(self, body):
        script = self.stubs / "gh"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)

    def _github_worker(self):
        repo = self.repo()
        git(repo, "remote", "set-url", "origin", "git@github.com:someone/thing.git")
        spawn_record("w", self.tmp, repo)
        return [{"name": "w", "repo": str(repo), "branch": "t/w"}]


class StateFiles(_Chaos):
    def test_s1_a_torn_ledger_loses_no_retire_and_is_counted(self):
        spawn_record("w", self.tmp, self.tmp)
        with open(store.path("ledger.jsonl"), "rb+") as f:
            f.truncate(os.path.getsize(store.path("ledger.jsonl")) - 20)
        spawn_record("b", self.tmp, self.tmp)
        ledger.append("retire", "b")
        self.assertEqual(ledger.get("b")["state"], "retired")
        first = self._checks("ledger")[0]
        self.assertFalse(first.ok)
        self.assertIn("1 unreadable", first.detail)
        second = self._checks("ledger")[0]
        self.assertTrue(second.ok, "told once, not on every pass for ever")
        self.assertIn("1 unreadable", second.detail)

    def test_s2_no_shape_of_line_takes_a_reader_down(self):
        spawn_record("w", self.tmp, self.tmp)
        with open(store.path("ledger.jsonl"), "ab") as f:
            f.write(b'[1, 2]\n3\n"text"\nnull\n\xff\xfe\x00garbage\n{"event": "update", "name": 5}\n{"event": "update", "name": "orphan"}\n')
        spawn_record("b", self.tmp, self.tmp)
        self.assertEqual(ledger.get("b")["state"], "live")
        self.assertEqual(ledger.get("orphan")["state"], "damaged")
        status.collect()
        watch.step(caps.CTX, {}, 0, watch.Dedupe())
        ledger.damaged()
        self.assertFalse(self._checks("ledger")[0].ok)

    def test_s2_a_damaged_worker_can_be_retired(self):
        ledger.append("update", "orphan", path=str(self.tmp / "gone"))
        retire.retire(caps.CTX, "orphan")
        self.assertEqual(ledger.get("orphan")["state"], "retired")

    def test_s3_a_lost_ledger_holds_launches_and_comes_back(self):
        repo = self.repo()
        spawn_record("w", self.tmp, repo, claude_args=["--name", "w"])
        spawn_record("gone", self.tmp, repo)
        ledger.append("retire", "gone")
        self.assertTrue(self._checks("ledger")[0].ok)
        store.path("ledger.jsonl").unlink()
        found = self._checks("ledger")[0]
        self.assertFalse(found.ok)
        self.assertIn("w", found.detail)
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False)
        found.repair()
        self.assertEqual(ledger.get("w")["state"], "live")
        self.assertEqual(ledger.get("w")["claude_args"], ["--name", "w"])
        self.assertIsNone(ledger.get("gone"))
        self.assertTrue(self._checks("ledger")[0].ok)

    def test_s4_an_emptied_record_with_hq_up_starts_no_second(self):
        hq._save({"session": "conv", "block": "b", "space": "s"})
        hq._save({"session": "conv", "block": "b", "space": "s"})
        store.path("hq.json").write_text("")
        self._alive(athena=4242)
        self.assertEqual(hq.open_hq(focus=False, routine=False)["outcome"], "running")
        self.assertEqual(caps.CTX.backend.spaces(), [])

    def test_s4_both_generations_lost_with_a_known_conversation_is_not_green(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        found = hq.checks(caps.CTX)[0]
        self.assertFalse(found.ok)
        self.assertIsNone(found.repair, "Rex runs the sessions: doctor only flags it")

    def test_s4_a_stopped_hq_stays_stopped(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        hq._save({"session": "conv", "space": None, "block": None})
        self.assertTrue(hq.checks(caps.CTX)[0].ok)

    def test_s5_a_corrupt_status_file_rebuilds_its_report(self):
        transcript = self.tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": REPORT % "kept"}]}}) + "\n")
        store.path("workers").mkdir(parents=True, exist_ok=True)
        store.path("workers", "w.json").write_text("{broken")
        out = hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "no report here", "transcript_path": str(transcript)}, "w", gated=False)
        self.assertEqual(out["last_report"]["concerns"], ["kept"])

    def test_s6_overlapping_hooks_keep_the_report(self):
        env = {**os.environ, "ATHENA_WORKER": "w", "ATHENA_GATE": "off"}
        stop = json.dumps({"hook_event_name": "Stop", "last_assistant_message": REPORT % "kept", "cwd": str(self.tmp)})
        note = json.dumps({"hook_event_name": "Notification", "notification_type": "other", "message": "m"})
        procs = [subprocess.Popen([str(BIN), "hook"], stdin=subprocess.PIPE, env=env, text=True) for _ in range(10)]
        for at, proc in enumerate(procs):
            proc.stdin.write(stop if at == 0 else note)
            proc.stdin.close()
        for proc in procs:
            proc.wait(timeout=30)
        self.assertEqual(store.read_json(store.path("workers", "w.json"))["report"]["concerns"], ["kept"])

    def test_s7_a_broken_items_file_says_so_and_falls_back_a_generation(self):
        items.add("first thing")
        items.add("second thing")
        store.path("needs-you.json").write_text("{")
        self.assertTrue(any("items" in note and "unreadable" in note for note in view.fold()["notes"]))
        self.assertIn("first thing", [i["text"] for i in items.open_items()])
        with self.assertRaises(UserError):
            items.add("third thing")
        self.assertEqual(store.path("needs-you.json").read_text(), "{")

    def test_s8_a_broken_config_never_changes_what_hq_may_do(self):
        self.write_config(hq_skip_permissions=True)
        config.remember_good()
        store.path("config.json").write_text("{broken")
        config.reset()
        self.assertIn("--dangerously-skip-permissions", hq.claude_args())
        self.assertFalse(self._checks("config")[0].ok)

    def test_s8_a_broken_config_with_no_good_copy_refuses_rather_than_guessing(self):
        repo = self.repo()
        store.path("config.json").write_text("{broken")
        config.reset()
        with self.assertRaises(UserError):
            hq.claude_args()
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False)

    def test_s9_an_empty_state_dir_is_refused_when_state_is_elsewhere(self):
        repo = self.repo()
        old = self.home / ".local" / "state" / "athena"
        old.mkdir(parents=True)
        (old / "ledger.jsonl").write_text("{}\n")
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        os.environ["ATHENA_STATE_DIR"] = str(fresh)
        config.reset()
        with self.assertRaises(UserError) as caught:
            spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False)
        self.assertIn(str(old), str(caught.exception))
        (fresh / ".athena-scratch").write_text("")
        spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False, timeout=0.1)

    def test_s10_a_newer_config_is_flagged_and_unknown_fields_stay(self):
        with open(store.path("ledger.jsonl"), "a") as f:
            f.write(json.dumps({"event": "spawn", "name": "w", "ts": 1, "from_the_future": {"a": 1}}) + "\n")
        self.assertEqual(ledger.get("w")["from_the_future"], {"a": 1})
        self.write_config(version=99)
        found = self._checks("config")[0]
        self.assertFalse(found.ok)
        self.assertIn("99", found.detail)

    def test_s11_old_temps_are_swept_and_fresh_ones_kept(self):
        old, fresh = store.path("x.json.tmp123"), store.path("y.json.tmp456")
        store.ensure("workers")
        deep = store.path("workers", "w.json.tmp123")
        for path in (old, fresh, deep):
            path.write_text("half")
        for path in (old, deep):
            os.utime(path, (time.time() - 7200, time.time() - 7200))
        doctor.run(caps.CTX, fix=False)
        self.assertFalse(old.exists())
        self.assertFalse(deep.exists())
        self.assertTrue(fresh.exists())

    def test_s11_two_writers_in_one_process_never_share_a_temp(self):
        failures = []

        def writer():
            for _ in range(100):
                try:
                    store.write_json(store.path("shared.json"), {"a": 1})
                except OSError as exc:
                    failures.append(exc)
        threads = [threading.Thread(target=writer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])

    def test_s12_a_clock_jump_back_never_reads_as_healthy(self):
        store.write_json(store.path("run", "watch-state.json"), {"beat": time.time() + 3600})
        self.assertFalse(watch.checks(caps.CTX)[0].ok)
        store.write_json(store.path("health.json"), {"ts": time.time() + 3600, "ok": True})
        self.assertNotEqual(view.fold()["health"]["color"], "green")

    def test_s13_a_clock_jump_forward_restarts_the_watch_alone(self):
        store.write_json(store.path("run", "watch-state.json"), {"beat": time.time() - 3600})
        found = watch.checks(caps.CTX)[0]
        self.assertFalse(found.ok)
        self.assertEqual(found.repair, watch._start_detached)

    def test_s14_a_lock_file_naming_a_live_pid_blocks_nothing(self):
        for name in ("locks/hq.lock", "ask.lock", "doctor.lock", "locks/watch.lock"):
            path = store.path(*name.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(os.getpid()))
            with store.locked(name, blocking=False) as got:
                self.assertTrue(got, name)

    def test_s15_an_oversized_line_hides_nothing_after_it(self):
        spawn_record("w", self.tmp, self.tmp)
        with open(store.path("ledger.jsonl"), "a") as f:
            f.write(json.dumps({"event": "update", "name": "w", "blob": "x" * (5 << 20)}) + "\n")
        spawn_record("b", self.tmp, self.tmp)
        self.assertEqual(ledger.get("b")["state"], "live")

    def test_s16_a_rotation_lost_before_its_start_falls_back(self):
        hq._save({"session": None, "previous_session": "old", "space": "s", "block": "b"})
        found = hq.checks(caps.CTX)[0]
        self.assertIsNone(found.repair, "Rex runs the sessions: doctor only flags it")
        self.assertEqual(hq.open_hq(focus=False, routine=False)["outcome"], "resumed")
        self.assertIn("--resume old", store.path("run", "athena.sh").read_text())

    def test_m5_a_lock_file_that_cannot_be_made_stops_nothing(self):
        real = os.open

        def full(path, flags, *a, **k):
            if str(path).endswith(".lock") and flags & os.O_CREAT:
                raise OSError(28, "No space left on device")
            return real(path, flags, *a, **k)
        with mock.patch("os.open", full):
            with store.locked("doctor.lock", blocking=False, full_disk_ok=True) as got:
                self.assertTrue(got)
            out = hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "no report"}, "w")
        self.assertEqual(out["gate"]["decision"], "block")

    def _full_disk_for_locks(self):
        real = os.open

        def full(path, flags, *a, **k):
            if str(path).endswith(".lock") and flags & os.O_CREAT:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, flags, *a, **k)
        return mock.patch("os.open", full)

    def test_m5_the_gate_holds_when_nothing_at_all_can_be_written(self):
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "full")), mock.patch.object(store, "append_line", side_effect=OSError(errno.ENOSPC, "full")):
            out = hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "no report"}, "w")
        self.assertEqual(out["gate"]["decision"], "block")

    def test_m5_an_event_that_cannot_be_written_does_not_undo_a_retire(self):
        repo = self.repo()
        path = self.worktree(repo)
        spawn_record("w", path, repo, block=False)
        real = store.append_line

        def no_room_for_events(target, record, sync=False):
            if str(target).endswith("events.jsonl"):
                raise OSError(errno.ENOSPC, "full")
            return real(target, record, sync)
        with mock.patch.object(store, "append_line", no_room_for_events):
            self.assertFalse(retire.retire(caps.CTX, "w")["removed"] is None)
        self.assertEqual(ledger.get("w")["state"], "retired")

    def test_m5_a_running_watch_lives_through_a_full_disk(self):
        class FullLog(io.StringIO):
            def write(self, text):
                raise OSError(errno.ENOSPC, "full")
        self._block("w", screen="", status_={"state": "idle"})
        store.write_json(store.path("run", "watch-state.json"), {"snapshot": {}, "offset": 0})
        with mock.patch("os.fsync", side_effect=OSError(errno.ENOSPC, "full")), mock.patch("sys.stdout", FullLog()):
            self.assertEqual(watch.run(mock.Mock(once=True, tagged=False, wake=False, interval=0)), 0)

    def test_w_a_loop_that_throws_leaves_the_beat_stale_and_the_watch_alive(self):
        with mock.patch.object(watch, "step", side_effect=ValueError("a report that is no object")):
            self.assertEqual(watch.run(mock.Mock(once=True, tagged=False, wake=False, interval=0)), 1)
        self.assertIsNone(watch.beat_age())
        self.assertIn("ValueError", store.path("logs", "watch-suppressed.log").read_text())

    def test_w6_wakes_past_the_first_eight_go_as_a_count_in_the_same_wake(self):
        lines = [watch.Line(f"w{n} report DONE", "wake") for n in range(10)]
        with mock.patch.object(watch, "step", return_value=({}, 0, lines)), mock.patch.object(watch, "wake_hq", return_value=True) as woke:
            watch.run(mock.Mock(once=True, tagged=True, wake=True, interval=0))
        self.assertEqual(len(woke.call_args.args[1]), 10)
        self.assertEqual(store.read_json(store.path("run", "watch-state.json"))["held"], [])
        self.assertTrue(watch.wake_text(lines, "10:00").endswith("+2 more (athena-rex status)"))

    def test_w_what_was_told_long_ago_is_forgotten(self):
        dedupe = watch.Dedupe({"dash-queue 1": 100.0})
        self.assertTrue(dedupe.keep(watch.Line("x", "wake"), 100.0 + watch.REPEAT_AFTER + 1))
        self.assertNotIn("dash-queue 1", dedupe.told)

    def test_w_a_retire_mark_past_its_expiry_does_not_hide_a_real_exit(self):
        spawn_record("w", self.tmp, self.tmp, retiring=time.time() - ledger.RETIRING_EXPIRES - 1)
        store.ensure("workers")
        store.write_json(store.path("workers", "w.json"), {"state": "idle", "ts": time.time()})
        _, _, told = watch.step(caps.CTX, {"w": {"state": "idle"}}, 0, watch.Dedupe())
        self.assertIn("wake", [line.kind for line in told if "exited" in line.text])

    def test_m5_a_quota_is_a_full_disk_to_the_gate(self):
        real = os.open

        def over(path, flags, *a, **k):
            if str(path).endswith(".lock") and flags & os.O_CREAT:
                raise OSError(errno.EDQUOT, "Disc quota exceeded")
            return real(path, flags, *a, **k)
        with mock.patch("os.open", over):
            self.assertEqual(hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "no report"}, "w")["gate"]["decision"], "block")

    def test_d_a_check_that_vanished_does_not_spend_the_next_ones_grace(self):
        doctor._save({"down": {"worker:long-gone": 100.0}})
        doctor.run(caps.CTX, fix=False)
        self.assertNotIn("worker:long-gone", doctor._load()["down"])

    def test_i_a_resolve_on_a_broken_file_changes_nothing(self):
        added = items.add("a thing")
        items.add("a second, so a generation is kept")
        store.append_line(store.path("actions.jsonl"), {"seq": 1, "ts": 1, "item": added["id"], "status": "queued", "option": "x"})
        before = store.path("actions.jsonl").read_bytes()
        store.path("needs-you.json").write_text("{")
        with self.assertRaises(UserError):
            items.resolve(added["id"])
        self.assertEqual(store.path("actions.jsonl").read_bytes(), before)

    def test_i_a_snooze_ends(self):
        now = time.time()
        added = items.add("a thing")
        items.snooze(added["id"], "1d", now=now)
        item = items.find(items.load(), added["id"])
        self.assertTrue(items.snoozed(item, now))
        self.assertFalse(items.snoozed(item, now + 2 * 86400))
        self.assertNotIn("snoozed", items._row({**item, "snooze": int(now - 60)}))
        self.assertIn("snoozed", items._row({**item, "snooze": int(now + 60)}))

    def test_i_a_default_gives_its_full_wait_after_a_snooze_ends(self):
        now = time.time()
        added = items.add("a thing", options=["yes"], rec="yes", auto=True, now=now)
        items.snooze(added["id"], "1h", now=now)
        ended = now + 7200
        with mock.patch.object(items, "last_activity", return_value=ended):
            self.assertEqual(items.apply_defaults(ended + 60), [])
            self.assertEqual(len(items.apply_defaults(ended + 12 * 3600 + 60)), 1)

    def test_hq_stop_on_a_session_already_gone_stays_stopped(self):
        store.write_json(store.path("hq.json"), {"space": "s", "block": "b", "session": "conv"})
        with mock.patch.object(caps.CTX.backend, "close_space", side_effect=BackendError("not_found")):
            self.assertTrue(hq.stop())
        self.assertIsNone(hq.load()["block"])
        self.assertEqual(hq.state(hq.load(), False), "absent")

    def test_g_a_pr_from_another_branch_is_not_this_lifes(self):
        prs._latest["x"] = {"number": 1, "state": "MERGED", "branch": "t/old"}
        rows = [{"name": "x", "branch": "t/new", "repo": "r"}]
        self.assertIsNone(prs.snapshot_workers(rows, False, {"x": {"pr": {"number": 1, "branch": "t/old"}}})["x"])
        rows = [{"name": "x", "branch": "t/old", "repo": "r"}]
        self.assertEqual(prs.snapshot_workers(rows, False, {})["x"]["number"], 1)

    def test_m5_a_lock_that_guards_a_decision_does_not_pretend(self):
        with self._full_disk_for_locks():
            for name in ("locks/spawn.lock", "locks/hq.lock", "ask.lock", "locks/watch.lock", "locks/heavy.lock"):
                with self.subTest(name=name), self.assertRaises(OSError):
                    with store.locked(name):
                        self.fail("ran without its lock")

    def test_m5_the_command_says_the_disk_is_full(self):
        from athena_rex import cli
        spawn_record("w", self.tmp, self.tmp)
        with self._full_disk_for_locks(), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.main(["item", "add", "a thing"]), 1)
        self.assertIn("disk is full", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_m5_a_doctor_with_no_lock_looks_and_repairs_nothing(self):
        seen = {"ok": True, "checks": {}, "actions": [], "problems": []}
        with self._full_disk_for_locks(), mock.patch.object(doctor, "run", return_value=seen) as run:
            doctor._run(mock.Mock(fix=True, json=False, quiet=True, last=False))
        self.assertFalse(run.call_args.kwargs["fix"])

    def test_m5_doctor_reports_with_nothing_it_can_write(self):
        spawn_record("w", self.tmp, self.tmp)
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            out = doctor.run(caps.CTX, fix=False)
        self.assertNotIn("doctor", [p["check"] for p in out["problems"]], out["problems"])
        self.assertIn("ledger", out["checks"])

    def test_s5_a_status_file_that_is_json_but_no_object_reads_as_none(self):
        spawn_record("w", self.tmp, self.tmp)
        store.ensure("workers")
        store.path("workers", "w.json").write_text("[1, 2]")
        self.assertEqual(status.collect()[0]["name"], "w")
        self.assertEqual(hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "no report"}, "w")["gate"]["decision"], "block")

    def test_hq_has_one_named_state_for_each_way_it_can_be(self):
        placed = {"space": "s", "block": "b", "session": "conv"}
        stopped = {"space": None, "block": None, "session": "conv"}
        for record, running, started, wanted in (({}, False, None, "absent"), (stopped, False, None, "absent"), ({}, True, None, "unrecorded"),
                                                 (stopped, True, None, "unrecorded"), ({}, False, "conv", "lost"), (placed, True, None, "running"),
                                                 (placed, False, None, "gone"), ({"space": "s", "block": "b"}, False, None, "gone-fresh"),
                                                 ({"space": "s", "block": "b"}, False, "conv", "gone"),
                                                 ({"space": "s", "block": "b", "previous_session": "old"}, False, None, "gone")):
            with self.subTest(record=record, running=running, started=started):
                store.path("hq-start.json").unlink(missing_ok=True)
                if started:
                    store.write_json(store.path("hq-start.json"), {"session_id": started})
                self.assertEqual(hq.state(record, running), wanted)
                self.assertIn(wanted, hq.STATES)

    def test_s17_a_failed_write_keeps_the_old_content_and_no_temp(self):
        target = store.path("x.json")
        store.write_json(target, {"a": 1})
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                store.write_json(target, {"a": 2})
        self.assertEqual(store.read_json(target), {"a": 1})
        self.assertEqual(list(store.state_dir().glob("x.json.tmp*")), [])


class Rex(_Chaos):
    def _rex(self, server):
        from athena_rex.backends.rex import Backend
        os.environ["ATHENA_REX_SERVER"] = server
        config.reset()
        return Backend()

    def test_r4_a_socket_gone_and_a_path_too_long_are_told_apart(self):
        gone = self._rex(f"unix://{self.tmp}/no-such.sock").why_down()
        self.assertIn("gone", gone)
        long_path = "/private/tmp/" + "x" * 100 + "/s.sock"
        self.assertIn("104", self._rex(f"unix://{long_path}").why_down())

    def test_r8_long_text_is_typed_in_pieces_a_claude_will_not_take_for_a_paste(self):
        from athena_rex.backends.rex import Backend, TYPE_PIECE
        sent = []
        text = "/goal " + "\u00e9word " * 700
        with mock.patch.object(Backend, "_run", lambda self, *args, stdin=None, **kw: sent.append(stdin)), mock.patch("time.sleep"):
            Backend().type(Place("s", "b"), text)
        self.assertEqual("".join(sent), text)
        self.assertGreater(len(sent), 1)
        self.assertLessEqual(max(map(len, sent)), TYPE_PIECE)

    def test_r6_a_record_pointing_at_another_workers_block_types_nothing(self):
        theirs = caps.CTX.backend.create("b", self.tmp, ["x"], block_label="b")
        self._screen(theirs, "\x1b[2m❯ \x1b[0m", {"state": "idle"})
        ledger.append("spawn", "a", path=str(self.tmp), backend="fake", space=theirs.space, block=theirs.block)
        self._alive(a=4242)
        with self.assertRaises(agents.Refused) as caught:
            agents.send(caps.CTX.backend, {**ledger.get("a"), "name": "a"}, "not for you")
        self.assertEqual(caught.exception.code, "not_its_block")
        self.assertEqual(self._fake()["typed"], [])

    def test_r6_its_own_block_still_takes_text(self):
        mine = caps.CTX.backend.create("a", self.tmp, ["x"], block_label="a")
        self._screen(mine, "\x1b[2m❯ \x1b[0m", {"state": "idle"})
        ledger.append("spawn", "a", path=str(self.tmp), backend="fake", space=mine.space, block=mine.block)
        self._alive(a=4242)
        agents.send(caps.CTX.backend, {**ledger.get("a"), "name": "a"}, "for you")
        self.assertEqual(len(self._fake()["typed"]), 1)


class Monitors(_Chaos):
    def test_w1_and_g1_a_hung_gh_never_holds_the_loop(self):
        self._github_worker()
        self._gh("sleep 4")
        begun = time.monotonic()
        with mock.patch.object(prs, "fetch_due", lambda now: True):
            watch.step(caps.CTX, {}, 0, watch.Dedupe())
        self.assertLess(time.monotonic() - begun, 3)
        prs._fetcher.join()

    def test_w5_a_stale_pr_is_drawn_as_stale(self):
        spawn_record("w", self.tmp, self.tmp)
        store.write_json(store.path("watch.json"), {"ts": time.time(), "workers": {"w": {"state": "idle", "pr": {
            "number": 7, "fetched": time.time() - 1200, "url": "https://example.com/7"}}}})
        self.assertTrue(any("GitHub" in note and "20" in note for note in view.fold()["notes"]), view.fold()["notes"])

    def test_w6_an_undelivered_wake_survives_a_restart(self):
        spawn_record("w", self.tmp, self.tmp)
        self.cli("watch", "--once", "--wake")
        hooks.apply({"hook_event_name": "Stop", "last_assistant_message": REPORT % "q", "cwd": str(self.tmp)}, "w", gated=False)
        self.cli("watch", "--once", "--wake")
        held = store.read_json(store.path("run", "watch-state.json"))["held"]
        self.assertTrue(any("w report BLOCKED" in line["text"] for line in held))
        self.cli("watch", "--once", "--wake")
        held = store.read_json(store.path("run", "watch-state.json"))["held"]
        self.assertTrue(any("w report BLOCKED" in line["text"] for line in held))

    def test_w7_a_dead_doctor_is_raised_by_the_watch_once(self):
        store.write_json(store.path("health.json"), {"ts": time.time() - 900, "ok": True})
        first = doctor.tick(caps.CTX, time.time())
        self.assertTrue(any(line.kind == "wake" and "doctor" in line.text for line in first))
        self.assertEqual(doctor.tick(caps.CTX, time.time()), [])

    def test_w9_a_part_that_stays_down_is_told_again(self):
        def run(now):
            with mock.patch.object(caps, "loaded", lambda: [mock.Mock(__name__="athena_rex.caps.fake", spec=["checks", "__name__"],
                                                                      checks=lambda ctx: [caps.Check("part", False, "down")])]):
                doctor.run(caps.CTX, fix=True, now=now)
        base = time.time()
        run(base)
        run(base + 1800)
        told = [c for c in self.world_calls() if c.startswith("osascript")]
        self.assertEqual(len(told), 1)
        run(base + 3700)
        self.assertEqual(len([c for c in self.world_calls() if c.startswith("osascript")]), 2)

    def test_w11_a_blocked_hq_is_raised_with_its_age(self):
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._screen(place, "", {"state": "blocked", "msg": "a question"})
        self._alive(athena=4242)
        store.write_json(store.path("run", "hq-blocked.json"), {"since": time.time() - 3600})
        found = next(c for c in hq.checks(caps.CTX) if c.name == "hq-blocked")
        self.assertFalse(found.ok)
        self.assertIn("60 min", found.detail)


class Workers(_Chaos):
    def test_k2_a_session_that_never_said_hello_is_flagged(self):
        spawn_record("w", self.tmp, self.tmp)
        self._alive(w=4242)
        with mock.patch.object(time, "time", lambda real=time.time: real() + 3600):
            self.assertFalse(next(c for c in doctor.checks(caps.CTX) if c.name == "hooks:w").ok)
        hooks.apply({"hook_event_name": "SessionStart"}, "w")
        with mock.patch.object(time, "time", lambda real=time.time: real() + 3600):
            self.assertEqual([c for c in doctor.checks(caps.CTX) if c.name == "hooks:w" and not c.ok], [])

    def _transcript(self, text):
        root = self.tmp / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(root)
        encoded = "".join(c if c.isalnum() else "-" for c in str(self.tmp))
        folder = root / "projects" / encoded
        folder.mkdir(parents=True)
        path = folder / "sess.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
                        + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        return path

    def test_k3_a_report_whose_stop_was_lost_is_read_from_the_transcript(self):
        spawn_record("w", self.tmp, self.tmp)
        self._transcript(REPORT % "from the transcript")
        hooks.apply({"hook_event_name": "UserPromptSubmit", "session_id": "sess"}, "w")
        row = status.collect()[0]
        self.assertEqual(row["report"]["concerns"], ["from the transcript"])

    def test_k3_last_turns_report_is_not_this_turns(self):
        spawn_record("w", self.tmp, self.tmp)
        path = self._transcript(REPORT % "last turn")
        hooks.apply({"hook_event_name": "UserPromptSubmit", "session_id": "sess"}, "w")
        target = store.path("workers", "w.json")
        store.write_json(target, {**store.read_json(target), "ts": time.time() - 600})
        tool_use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 900"}}]}}
        for newer in ({"type": "user", "message": {"content": "do the next thing"}}, tool_use):
            with open(path, "a") as f:
                f.write(json.dumps(newer) + "\n")
            self.assertIsNone(hooks.report_in(path), newer)
            self.assertIsNone(status.collect()[0]["report"])

    def test_k3_a_done_the_gate_would_refuse_never_reads_as_done(self):
        repo = self.repo()
        path = self.worktree(repo)
        spawn_record("w", path, repo)
        encoded = "".join(c if c.isalnum() else "-" for c in str(path))
        root = self.tmp / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(root)
        folder = root / "projects" / encoded
        folder.mkdir(parents=True)
        done = f'ATHENA-REPORT {{"status":"DONE","verify":"smoke -> fail","head":"{git(path, "rev-parse", "HEAD")}"}}'
        (folder / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": done}]}}) + "\n")
        hooks.apply({"hook_event_name": "UserPromptSubmit", "session_id": "s"}, "w")
        target = store.path("workers", "w.json")
        for state in ("working", "idle-unreported"):
            with self.subTest(state=state):
                store.write_json(target, {**store.read_json(target), "state": state, "ts": time.time() - 600})
                row = status.collect()[0]
                self.assertNotEqual(row["state"], "done")
                self.assertIsNone(row["report"])
        good = done.replace("smoke -> fail", "smoke -> pass")
        (folder / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": good}]}}) + "\n")
        store.write_json(target, {**store.read_json(target), "state": "working", "ts": time.time() - 600})
        self.assertEqual(status.collect()[0]["report"]["status"], "DONE")

    def test_c4_a_renamed_hook_field_reads_the_transcript(self):
        path = self._transcript(REPORT % "renamed")
        out = hooks.apply({"hook_event_name": "Stop", "transcript_path": str(path), "cwd": str(self.tmp)}, "w")
        self.assertIsNone(out["gate"])
        self.assertEqual(out["report"]["concerns"], ["renamed"])

    def _stuck(self):
        place = self._block("w", screen="line one\nsk-abcdefghijklmnopqrstuvwxyz\nline two")
        self._alive(w=4242)
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        target = store.path("workers", "w.json")
        data = store.read_json(target)
        data["ts"] = time.time() - 7200
        store.write_json(target, data)
        return place

    def test_k4_a_quiet_worker_wakes_no_one_and_files_nothing_as_in_the_old_athena(self):
        self._stuck()
        _, _, told = watch.step(caps.CTX, {"w": {"state": "working", "quiet_s": 1}}, 0, watch.Dedupe())
        self.assertFalse(any("stuck" in line.text for line in told))
        self.assertEqual(items.open_items(), [])

    def test_k4_a_timebox_past_its_end_is_told_once(self):
        spawn_record("w", self.tmp, self.tmp, timebox_s=60)
        data = spawn.tick(caps.CTX, time.time() + 120)
        self.assertTrue(any("timebox" in line.text and line.kind == "quiet" for line in data), "logged, never woken")
        self.assertEqual([line for line in spawn.tick(caps.CTX, time.time() + 130) if "timebox" in line.text], [])

    def test_k4_a_brief_timebox_is_read(self):
        self.assertEqual(brief.timebox_seconds("GOAL: x\nTIMEBOX: 4 hours or 400 turns; then commit\n"), 14400)
        self.assertEqual(brief.timebox_seconds("TIMEBOX: 90 minutes\n"), 5400)
        self.assertIsNone(brief.timebox_seconds("no box"))

    def _retire_child(self, target, before=True, force=False):
        """A real process really killed at this step, with no `finally` to tidy up behind it."""
        module, _, attr = target.rpartition(".")
        code = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
sys.dont_write_bytecode = True
from athena_rex.kernel import agents, caps, gitops, ledger, store
from athena_rex.caps import retire
owner = {module}
real = getattr(owner, {attr!r})
def dying(*a, **k):
    if {before!r}:
        os._exit(9)
    real(*a, **k)
    os._exit(9)
setattr(owner, {attr!r}, dying)
retire.retire(caps.CTX, "w", force={force!r})
"""
        out = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code], capture_output=True, text=True, env=os.environ, timeout=60)
        self.assertEqual(out.returncode, 9, out.stderr)

    def test_k8_a_retire_killed_at_any_step_can_be_run_again(self):
        for at, (target, before) in enumerate((("gitops.backup_head", True), ("gitops.backup_head", False), ("gitops.remove_worktree", True),
                                               ("gitops.remove_worktree", False), ("caps.CTX.backend.close_space", True), ("retire._history", True),
                                               ("ledger.append", True))):
            with self.subTest(target=target, before=before):
                name = f"r{at}"
                repo = self.repo(name)
                path = self.worktree(repo, f"t/{name}", name)
                place = caps.CTX.backend.create("w", path, ["x"])
                store.path("ledger.jsonl").unlink(missing_ok=True)
                ledger.append("spawn", "w", repo=str(repo), branch=f"t/{name}", path=str(path), backend="fake", space=place.space, block=place.block)
                self._retire_child(target, before)
                retire.retire(caps.CTX, "w")
                self.assertEqual(ledger.get("w")["state"], "retired")
                self.assertFalse(path.exists())
                self.assertNotIn(str(path), git(repo, "worktree", "list"))

    def _worker_to_retire(self, name="proj"):
        repo = self.repo(name)
        path = self.worktree(repo)
        place = caps.CTX.backend.create("w", path, ["x"])
        ledger.append("spawn", "w", repo=str(repo), branch="t/w", path=str(path), backend="fake", space=place.space, block=place.block)
        return repo, path

    def _gone_for_good(self, repo, path):
        worker = ledger.get("w")
        self.assertEqual(worker["state"], "retired")
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(repo, "worktree", "list"))
        return worker

    def test_k8_a_dirty_retire_killed_after_the_removal_keeps_its_pointers(self):
        repo, path = self._worker_to_retire()
        (path / "note.txt").write_text("only here")
        self._retire_child("gitops.remove_worktree", before=False, force=True)
        self.assertFalse(path.exists())
        out = retire.retire(caps.CTX, "w")
        worker = self._gone_for_good(repo, path)
        self.assertEqual(git(repo, "show", f"{worker['wip_backup']}:note.txt"), "only here")
        self.assertTrue(worker["backup"].startswith("refs/athena-backup/w/"))
        self.assertEqual((out["wip_backup"], out["backup"]), (worker["wip_backup"], worker["backup"]))

    def test_k8_two_backups_in_one_second_keep_both(self):
        repo = self.repo()
        git(repo, "commit", "-q", "--allow-empty", "-m", "two")
        new, old = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
        with mock.patch("time.time", return_value=1800000000.0):
            first, again, other = gitops.backup_head(repo, "w", old), gitops.backup_head(repo, "w", old), gitops.backup_head(repo, "w", new)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual((git(repo, "rev-parse", first), git(repo, "rev-parse", other)), (old, new))

    def test_k8_a_copy_that_never_happened_is_not_named(self):
        repo, path = self._worker_to_retire()
        self._with_ignored_notes(path)
        self._retire_child("gitops.keep_ignored", before=True, force=True)
        (path / "notes" / "a.txt").unlink()
        (path / "notes").rmdir()
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertIsNone(out.get("ignored_kept"))
        self.assertIsNone(self._gone_for_good(repo, path).get("ignored_kept"))

    def test_k8_a_rerun_still_says_the_worktree_was_removed(self):
        repo, path = self._worker_to_retire()
        self._retire_child("caps.CTX.backend.close_space", before=True)
        self.assertTrue(retire.retire(caps.CTX, "w")["removed"])
        self.assertTrue(self._gone_for_good(repo, path)["removed"])

    def test_k8_a_dirty_retire_killed_before_the_removal_lists_every_save(self):
        repo, path = self._worker_to_retire()
        (path / "note.txt").write_text("only here")
        self._retire_child("gitops.remove_worktree", before=True, force=True)
        first = ledger.get("w")["wip_backup"]
        (path / "note.txt").unlink()
        (path / "later.txt").write_text("written after")
        retire.retire(caps.CTX, "w", force=True)
        worker = self._gone_for_good(repo, path)
        self.assertEqual(git(repo, "show", f"{first}:note.txt"), "only here")
        self.assertEqual(git(repo, "show", f"{worker['wip_backup']}:later.txt"), "written after")
        self.assertIn(first, worker["retire_kept"])
        self.assertIn(worker["wip_backup"], worker["retire_kept"])

    def _with_ignored_notes(self, path):
        (path / ".gitignore").write_text("notes/\n")
        git(path, "add", ".gitignore")
        git(path, "commit", "-q", "-m", "ignore")
        git(path, "push", "-q", "origin", "HEAD")
        (path / "notes").mkdir()
        (path / "notes" / "a.txt").write_text("only here")

    def test_k8_ignored_work_is_found_again_after_a_kill(self):
        for at, before in ((0, False), (1, True)):
            with self.subTest(after_the_removal=not before):
                store.path("ledger.jsonl").unlink(missing_ok=True)
                repo, path = self._worker_to_retire(f"p{at}")
                self._with_ignored_notes(path)
                self._retire_child("gitops.remove_worktree", before=before, force=True)
                retire.retire(caps.CTX, "w", force=True)
                worker = self._gone_for_good(repo, path)
                self.assertEqual(Path(worker["ignored_kept"], "notes", "a.txt").read_text(), "only here")

    def test_k8_a_retire_killed_twice_in_one_second_does_not_collide_in_the_trash(self):
        with mock.patch("time.time", return_value=1800000000.0):
            first = store.trash_place("w-ignored")
            first.mkdir()
            self.assertNotEqual(store.trash_place("w-ignored"), first)

    def test_k8_an_orphan_killed_after_the_move_says_where_it_went(self):
        repo, path = self._worker_to_retire()
        (path / "kept.txt").write_text("only here")
        (path / ".git").unlink()
        self._retire_child("store.to_trash", before=False, force=True)
        self.assertFalse(path.exists())
        retire.retire(caps.CTX, "w")
        worker = self._gone_for_good(repo, path)
        self.assertEqual(Path(worker["trash"], "kept.txt").read_text(), "only here")

    def test_k8_an_orphan_killed_before_the_move_finishes_when_forced_again(self):
        repo, path = self._worker_to_retire()
        (path / "kept.txt").write_text("only here")
        (path / ".git").unlink()
        self._retire_child("store.to_trash", before=True, force=True)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertTrue((path / "kept.txt").exists())
        retire.retire(caps.CTX, "w", force=True)
        worker = self._gone_for_good(repo, path)
        self.assertEqual(Path(worker["trash"], "kept.txt").read_text(), "only here")

    def test_k8_a_removal_killed_with_only_the_record_left(self):
        repo, path = self._worker_to_retire()
        self._retire_child("gitops.remove_worktree", before=True)
        shutil.rmtree(path)
        self.assertIn(str(path), git(repo, "worktree", "list"))
        retire.retire(caps.CTX, "w")
        worker = self._gone_for_good(repo, path)
        self.assertTrue(worker["backup"].startswith("refs/athena-backup/w/"))

    def test_k8_a_removal_killed_halfway_through_the_files(self):
        repo, path = self._worker_to_retire()
        (path / "a.txt").write_text("a")
        git(path, "add", "a.txt")
        git(path, "commit", "-q", "-m", "a")
        git(path, "push", "-q", "origin", "HEAD")
        self._retire_child("gitops.remove_worktree", before=True)
        kept = ledger.get("w")["backup"]
        (path / "a.txt").unlink()
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertEqual(ledger.get("w")["state"], "live")
        retire.retire(caps.CTX, "w", force=True)
        worker = self._gone_for_good(repo, path)
        self.assertIn(kept, worker["retire_kept"])

    def test_k8_a_removal_killed_after_the_git_file_went(self):
        repo, path = self._worker_to_retire()
        self._retire_child("gitops.remove_worktree", before=True)
        (path / ".git").unlink()
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(self._gone_for_good(repo, path)["trash"])

    def test_k8_a_rerun_stops_what_the_killed_retire_listed(self):
        repo, path = self._worker_to_retire()
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.addCleanup(child.wait)
        script = self.tmp / "ps-family"
        gone = self.tmp / "its-claude-is-gone"
        script.write_text(f"""#!/bin/sh
case "$*" in
*tty=*) echo ttys009 ;;
*-t*) echo '4242 {os.getuid()} claude --name w'; echo '{child.pid} {os.getuid()} sleep 60' ;;
*lstart=*) date -v-1H '+%a %b %e %H:%M:%S %Y' ;;
*-p\\ {child.pid}*) echo 'sleep 60' ;;
*stat=*) [ -e "{gone}" ] || echo '4242 S claude --name w --settings x' ;;
esac
""")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        store.ensure("run")
        agents.pid_file("w").write_text("4242")
        self._retire_child("retire._stop_claude", before=True, force=True)
        gone.write_text("")
        out = retire.retire(caps.CTX, "w")
        self.assertEqual(out["leftovers"], ["sleep 60"])
        self.assertEqual(child.wait(timeout=5), -15)
        self._gone_for_good(repo, path)

    def test_k11_what_a_worker_left_running_is_found_and_stopped(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.addCleanup(child.wait)
        script = self.tmp / "ps-family"
        script.write_text(f"""#!/bin/sh
case "$*" in
*tty=*) echo ttys009 ;;
*-t*) echo '4242 {os.getuid()} claude --name w'; echo '{child.pid} {os.getuid()} sleep 60'; echo '77 0 login -pf someone' ;;
*-p\\ {child.pid}*) echo 'sleep 60' ;;
esac
""")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        store.ensure("run")
        agents.pid_file("w").write_text("4242")
        family = agents.family("w")
        self.assertEqual(family, {child.pid: "sleep 60"})
        self.assertEqual(agents.stop_leftovers(family, grace=2), ["sleep 60"])
        self.assertEqual(child.wait(timeout=5), -15)

    def test_k11_a_pid_now_something_else_is_left_alone(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.addCleanup(child.wait)
        script = self.tmp / "ps-other"
        script.write_text(f"#!/bin/sh\necho 'something else entirely'\n")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        self.assertEqual(agents.stop_leftovers({child.pid: "sleep 60"}, grace=0), [])
        self.assertIsNone(child.poll())

    def _amending_worker(self, screen):
        repo = self.repo()
        path = self.worktree(repo)
        place = caps.CTX.backend.create("w", path, ["x"], block_label="w")
        ledger.append("spawn", "w", repo=str(repo), branch="t/w", path=str(path), backend="fake", space=place.space, block=place.block)
        ledger.append("update", "w", goal_pending="/goal new", goal_amending=True)
        self._screen(place, screen, {"state": "idle"})
        self._alive(w=4242)
        return place, path

    def test_k13_a_refused_amend_does_not_open_the_typing_guard(self):
        place, path = self._amending_worker("❯ someone typed this and has not sent it")
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("typing", str(caught.exception))
        self.assertIn(place.block, self._fake()["spaces"][place.space]["blocks"])
        self.assertTrue(path.exists())

    def test_k13_a_refused_amend_still_exits_gently(self):
        place, _ = self._amending_worker("\x1b[2m❯ \x1b[0m")
        with self.assertRaises(retire.RetireError), mock.patch("os.kill") as killed:
            retire.retire(caps.CTX, "w", grace=0.1)
        self.assertIn([place.block, "/exit"], self._fake()["typed"])
        self.assertEqual({call.args[0] for call in killed.call_args_list}, {4242})

    TRUST_PROMPT = "Quick safety check\n\n\u276f 1. Yes, I trust this folder\n  2. No, exit\n\nEnter to confirm"

    def _at_a_prompt(self, screen, **fields):
        repo = self.repo()
        path = self.worktree(repo)
        place = caps.CTX.backend.create("w", path, ["x"], block_label="w")
        ledger.append("spawn", "w", repo=str(repo), branch="t/w", path=str(path), backend="fake", space=place.space, block=place.block, **fields)
        self._screen(place, screen)
        self._alive(w=4242)
        return place

    def _plain_retire_refusal(self):
        with self.assertRaises(retire.RetireError) as caught, mock.patch("os.kill"):
            retire.retire(caps.CTX, "w", grace=0.1)
        return str(caught.exception)

    def test_k13_an_amend_at_a_startup_prompt_is_still_a_startup_prompt(self):
        place = self._at_a_prompt(self.TRUST_PROMPT, goal_pending="/goal first", pending_reason="startup prompt")
        store.ensure("briefs")
        self.assertEqual(spawn.amend(caps.CTX, "w", "a new condition")["outcome"], "waiting")
        self.assertEqual(ledger.goal_state(ledger.get("w")), "first")
        self.assertIn("a new condition", ledger.get("w")["goal_pending"])
        self.assertEqual(status.collect()[0]["state"], "blocked")
        self.assertNotIn("typing", self._plain_retire_refusal())
        self.assertNotIn(place.block, self._fake()["spaces"][place.space]["blocks"])
        self.assertEqual(self._fake()["typed"], [])

    def test_k13_a_startup_prompt_with_no_goal_is_no_one_typing(self):
        self._at_a_prompt(self.TRUST_PROMPT, pending_reason="startup prompt")
        self.assertNotIn("typing", self._plain_retire_refusal())

    def test_k13_a_first_goal_that_never_got_a_screen_is_no_one_typing(self):
        self._at_a_prompt("", goal_pending="/goal first")
        self.assertNotIn("typing", self._plain_retire_refusal())

    def test_k13_a_first_goal_refused_by_a_full_box_keeps_the_typing_guard(self):
        place = self._at_a_prompt("\u276f someone typed this", goal_pending="/goal first", pending_reason="box_not_empty")
        self.assertIn("typing", self._plain_retire_refusal())
        self.assertIn(place.block, self._fake()["spaces"][place.space]["blocks"])

    def test_k13_startup_words_on_screen_do_not_open_the_guard_over_typed_text(self):
        self._at_a_prompt("Do you trust this answer?\n\u276f half a sentence")
        self.assertIn("typing", self._plain_retire_refusal())

    def test_k13_a_typed_list_is_not_an_option_row(self):
        self._at_a_prompt("\u276f 1. do this first\n  2. then that", goal_pending="/goal first")
        self.assertIn("typing", self._plain_retire_refusal())

    def _ready_worker(self, screen="\x1b[2m\u276f \x1b[0m", **fields):
        self._block("w", status_={"state": "idle"}, screen=screen, **fields)
        self._alive(w=4242)
        store.ensure("briefs")
        store.path("briefs", "w.md").write_text("the brief")
        return {**ledger.get("w"), "name": "w"}

    def test_k13_an_amend_during_a_launch_is_the_goal_that_goes_out(self):
        record = self._ready_worker()
        ledger.append("update", "w", goal_pending="/goal amended", goal_amending=True)
        with mock.patch.object(agents, "wait"):
            spawn._deliver(caps.CTX.backend, record, "/goal first")
        self.assertEqual([t[1] for t in self._fake()["typed"]], ["/goal amended"])

    def test_k13_an_amend_during_a_launch_outlives_a_startup_prompt_with_no_goal(self):
        self._ready_worker()
        ledger.append("update", "w", goal_pending="/goal amended", goal_amending=True)
        spawn._goal_waits("w", None, pending_reason="startup prompt")
        self.assertEqual(ledger.get("w")["goal_pending"], "/goal amended")

    def test_k13_an_amend_already_delivered_is_not_replaced_by_the_launchs_goal(self):
        record = self._ready_worker(goal_amended="second")
        with mock.patch.object(agents, "wait"):
            spawn._deliver(caps.CTX.backend, record, "/goal first")
        self.assertEqual(self._fake()["typed"], [])
        self.assertIsNone(ledger.get("w").get("goal_pending"))

    def test_k13_an_amend_during_a_send_goes_out_after_it(self):
        self._ready_worker(goal_pending="/goal first")
        sent, threads = [], []

        def sending(backend, record, text, **kwargs):
            sent.append(text)
            if len(sent) == 1:
                threads.append(threading.Thread(target=spawn.amend, args=(caps.CTX, "w", "second")))
                threads[0].start()
                time.sleep(0.3)
        with mock.patch.object(agents, "send", sending):
            spawn.deliver_pending(caps.CTX, "w")
            threads[0].join(timeout=10)
        self.assertEqual(len(sent), 2, sent)
        self.assertIn("second", sent[1])
        self.assertEqual(ledger.goal_state(ledger.get("w")), "none")

    def test_k13_an_amend_replaces_a_goal_that_may_have_arrived(self):
        self._at_a_prompt(self.TRUST_PROMPT, goal_unconfirmed="/goal first")
        store.ensure("briefs")
        spawn.amend(caps.CTX, "w", "second")
        self.assertIsNone(ledger.get("w")["goal_unconfirmed"])
        with self.assertRaises(UserError):
            spawn._resume(argparse.Namespace(name="w", resend=True))

    def test_k5_a_full_box_is_refused_once_on_the_ledger(self):
        self._ready_worker(screen="\u276f someone typed", goal_pending="/goal new", goal_amending=True)
        for _ in range(3):
            self.assertEqual(spawn.deliver_pending(caps.CTX, "w")["outcome"], "waiting")
        lines = list(ledger.events())
        self.assertEqual(sum(1 for line in lines if line.get("prompt") == "sending"), 1)
        self.assertEqual(ledger.get("w")["goal_pending"], "/goal new")
        self._screen(ledger_place(), "\x1b[2m\u276f \x1b[0m", {"state": "idle"})
        self.assertEqual(spawn.deliver_pending(caps.CTX, "w")["outcome"], "sent")

    def test_k5_a_send_in_flight_is_not_told_as_unconfirmed(self):
        self._ready_worker(goal_unconfirmed="/goal first")
        with store.locked("locks/w.goal.lock"):
            self.assertEqual(spawn.tick(caps.CTX, time.time()), [])
        self.assertEqual([line.kind for line in spawn.tick(caps.CTX, time.time())], ["wake"])

    def test_k5_a_screen_that_stops_answering_after_the_send_is_no_failed_launch(self):
        record = self._ready_worker()
        with mock.patch.object(agents, "wait", side_effect=BackendError("timeout")):
            self.assertTrue(spawn._deliver(caps.CTX.backend, record, "/goal first").startswith("unconfirmed"))

    def _reports_a_status(self, screen):
        place = self._at_a_prompt(screen)
        self._screen(place, screen, {"state": "idle"})
        self.assertIn("typing", self._plain_retire_refusal())
        self.assertIn(place.block, self._fake()["spaces"][place.space]["blocks"])

    def test_k13_startup_words_do_not_make_typed_text_an_option_for_a_claude_with_a_status(self):
        self._reports_a_status("Do you trust this? Enter to confirm\n\u276f 1. half a list")

    def test_k13_startup_words_do_not_open_the_guard_on_no_box_for_a_claude_with_a_status(self):
        self._reports_a_status("Do you trust this? Enter to confirm")

    def test_k13_a_first_goal_with_a_status_and_no_box_keeps_the_guard(self):
        place = self._at_a_prompt("a permission dialog", goal_pending="/goal first")
        self._screen(place, "a permission dialog", {"state": "blocked", "msg": "permission"})
        self.assertIn("typing", self._plain_retire_refusal())

    def test_k13_no_box_at_all_still_fails_closed(self):
        self._at_a_prompt("something else entirely")
        self.assertIn("typing", self._plain_retire_refusal())

    def test_k13_an_amended_goal_is_queued_into_a_busy_claude(self):
        self._block("w", status_={"state": "working"}, screen="\x1b[2m❯ \x1b[0m")
        self._alive(w=4242)
        store.ensure("briefs")
        store.path("briefs", "w.md").write_text("the brief")
        self.assertEqual(self.cli("amend", "w", "a new condition").returncode, 0)
        self.assertEqual(len(self._fake()["typed"]), 1)

    def test_i14_done_on_github_needs_a_pr_unless_the_brief_says_none(self):
        repo = self.repo()
        git(repo, "remote", "set-url", "--add", "--push", "origin", str(repo.parent / "origin.git"))
        path = self.worktree(repo)
        git(path, "remote", "set-url", "origin", "git@github.com:someone/thing.git")
        git(path, "remote", "set-url", "--push", "origin", str(repo.parent / "origin.git"))
        head = git(path, "rev-parse", "HEAD")
        report = {"status": "DONE", "verify": "x -> pass", "head": head, "pr": ""}
        self.assertIn("PR", hooks.refusal("x", report, str(path)))
        self.assertIsNone(hooks.refusal("x", {**report, "pr": "https://github.com/someone/thing/pull/3"}, str(path)))
        brief_file = self.tmp / "brief.md"
        brief_file.write_text("NOTES: There is no PR for this one.\n")
        os.environ["ATHENA_BRIEF"] = str(brief_file)
        self.assertIsNone(hooks.refusal("x", report, str(path)))

    def test_i14_a_repo_not_on_github_needs_no_pr(self):
        repo = self.repo()
        path = self.worktree(repo)
        report = {"status": "DONE", "verify": "x -> pass", "head": git(path, "rev-parse", "HEAD"), "pr": ""}
        self.assertIsNone(hooks.refusal("x", report, str(path)))

    def test_k13_a_goal_can_be_amended(self):
        self._block("w", status_={"state": "idle"}, screen="\x1b[2m❯ \x1b[0m")
        self._alive(w=4242)
        store.ensure("briefs")
        store.path("briefs", "w.md").write_text("the brief")
        out = self.cli("amend", "w", "a new condition")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(any(t[1].startswith("/goal a new condition") and "the brief" not in t[1][:30] for t in self._fake()["typed"]), self._fake()["typed"])
        worker = ledger.get("w")
        self.assertEqual(worker["goal_amended"], "a new condition")
        self.assertFalse(worker.get("goal_pending"))


def ledger_place():
    return agents.place_of(ledger.get("w"))


class ClaudeItself(_Chaos):
    LIMIT = "You've hit your 5-hour limit · resets 3pm (America/Los_Angeles)"

    def test_c1_a_limit_parks_every_worker_with_one_line(self):
        limits = caps.get("limits")
        soon = time.strftime("%I:%M%p", time.gmtime(time.time() + 7200)).lower().lstrip("0")
        for name in ("a", "b"):
            self._block(name, screen=f"text\nYou've hit your 5-hour limit · resets {soon} (UTC)\n")
        self._alive(a=4242, b=4243)
        lines = limits.tick(caps.CTX, time.time())
        self.assertEqual(len([line for line in lines if "usage limit" in line.text and line.kind == "quiet"]), 1, "logged, never woken")
        for name in ("a", "b"):
            self.assertTrue(ledger.get(name)["limit_until"])
        self.assertEqual({r["state"] for r in status.collect()}, {"limit"})
        self.assertEqual(limits.tick(caps.CTX, time.time()), [])

    def test_c5_words_in_a_finished_message_are_no_api_error_or_expired_login(self):
        limits = caps.get("limits")
        self._block("w", screen="The retry handles API Error: 503, and the docs say Please run /login first.\n\x1b[2m\u276f \x1b[0m", status_={"state": "idle"})
        self._alive(w=4242)
        limits.tick(caps.CTX, time.time())
        self.assertFalse(ledger.get("w").get("api_error_at"))
        self.assertEqual(items.open_items(), [])

    def test_c3_every_wording_of_an_expired_login_is_seen(self):
        limits = caps.get("limits")
        for row in ("Not logged in · Please run /login", "OAuth token revoked · Please run /login", "Session expired. Please run /login to sign in",
                    "Your session has expired. Please run /login", "Invalid API key · Please run /login", "OAuth token has expired",
                    "  \u23bf  API Error: 401 OAuth token has expired. Please run /login"):
            with self.subTest(row=row):
                self.assertTrue(limits.AUTH.search(f"text above\n{row}\nmore\n"))
        for row in ("The retry handles this, and the docs say Please run /login first, then go on.", "I read that OAuth token has expired in the log", "grep -n 'Please run /login' src"):
            with self.subTest(row=row):
                self.assertFalse(limits.AUTH.search(f"{row}\n"))

    def test_c3_expired_auth_in_an_api_error_row_is_still_seen(self):
        limits = caps.get("limits")
        self._block("w", screen="  \u23bf  API Error: 401 OAuth token has expired. Please run /login\n", status_={"state": "idle"})
        self._alive(w=4242)
        self.assertIn("sign-in", " ".join(line.text for line in limits.tick(caps.CTX, time.time())))

    def test_c1_a_read_not_due_asks_the_backend_nothing(self):
        limits = caps.get("limits")
        self._block("a", screen="", status_={"state": "idle"})
        self._alive(a=4242)
        now = time.time()
        backend = caps.CTX.backend
        limits.tick(caps.CTX, now)
        with mock.patch.object(backend, "program_status", wraps=backend.program_status) as asked:
            limits.tick(caps.CTX, now + 20)
        self.assertEqual(asked.call_count, 0)

    def test_c1_a_busy_worker_is_read_as_soon_as_it_is_not(self):
        limits = caps.get("limits")
        place = self._block("a", screen="", status_={"state": "working"})
        self._alive(a=4242)
        now = time.time()
        soon = time.strftime("%I:%M%p", time.gmtime(now + 7200)).lower().lstrip("0")
        limits.tick(caps.CTX, now)
        self._screen(place, f"You've hit your 5-hour limit · resets {soon} (UTC)\n", {"state": "idle"})
        limits.tick(caps.CTX, now + 20)
        self.assertTrue(ledger.get("a")["limit_until"])

    def test_c1_the_reset_time_is_read_in_its_own_zone(self):
        limits = caps.get("limits")
        noon_la = NOON_IN_LA
        found = limits.read_reset("You've hit your 5-hour limit · resets 3pm (America/Los_Angeles)", noon_la)
        self.assertTrue(noon_la < found <= noon_la + 86400)
        self.assertEqual(time.strftime("%M", time.gmtime(found)), "00")
        self.assertEqual(limits.read_reset("You've hit your 5-hour limit · resets 3:30pm (America/Los_Angeles)", noon_la) - found, 1800)
        self.assertIsNone(limits.read_reset("nothing like that", noon_la))

    def test_c1_after_the_reset_each_is_told_to_carry_on_once(self):
        limits = caps.get("limits")
        self._block("w", screen="\x1b[2m❯ \x1b[0m", status_={"state": "idle"}, limit_until=time.time() - 5)
        self._alive(w=4242)
        limits.tick(caps.CTX, time.time())
        self.assertEqual(len(self._fake()["typed"]), 1)
        self.assertFalse(ledger.get("w").get("limit_until"))
        limits.tick(caps.CTX, time.time())
        self.assertEqual(len(self._fake()["typed"]), 1)

    def test_c1_a_limit_line_still_on_screen_after_the_reset_parks_nothing(self):
        limits = caps.get("limits")
        noon = NOON_IN_LA
        reset = limits.read_reset(self.LIMIT, noon)
        self._block("w", screen=f"{self.LIMIT}\nok\n\x1b[2m❯ \x1b[0m", status_={"state": "idle"})
        self._alive(w=4242)
        limits.tick(caps.CTX, noon)
        self.assertEqual(ledger.get("w")["limit_until"], reset)
        limits.tick(caps.CTX, reset + 5)
        self.assertEqual(len(self._fake()["typed"]), 1)
        self.assertEqual(limits.tick(caps.CTX, reset + 100), [])
        self.assertFalse(ledger.get("w").get("limit_until"))
        self.assertIsNone(limits.parked(reset + 100))

    def test_c1_a_limit_first_seen_after_its_reset_carries_on_and_parks_nothing(self):
        limits = caps.get("limits")
        noon = NOON_IN_LA
        reset = limits.read_reset(self.LIMIT, noon)
        self._block("w", screen=f"{self.LIMIT}\n\x1b[2m❯ \x1b[0m", status_={"state": "idle"})
        self._alive(w=4242)
        limits.tick(caps.CTX, reset + 3600)
        self.assertFalse(ledger.get("w").get("limit_until"))
        self.assertEqual(len(self._fake()["typed"]), 1)
        limits.tick(caps.CTX, reset + 3700)
        self.assertEqual(len(self._fake()["typed"]), 1)

    def test_c1_a_worker_talking_about_limits_parks_nobody(self):
        limits = caps.get("limits")
        for at, words in enumerate(("You've hit your daily limit", f"the screen said: {self.LIMIT}", "resets 5:75pm (America/Los_Angeles)")):
            with self.subTest(words=words):
                self._block(f"t{at}", screen=f"{words}\n")
        self._alive(t0=4242, t1=4243, t2=4244)
        self.assertEqual(limits.tick(caps.CTX, time.time()), [])
        self.assertIsNone(limits.parked())
        self.assertIsNone(limits.read_reset("You've hit your 5-hour limit · resets 5:75pm (America/Los_Angeles)", time.time()))

    def test_c1_no_launch_during_a_limit(self):
        repo = self.repo()
        spawn_record("w", self.tmp, repo, limit_until=time.time() + 600)
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False)

    def test_c2_an_account_switch_is_flagged(self):
        config_file = self.home / ".claude.json"
        config_file.write_text(json.dumps({"oauthAccount": {"accountUuid": "one"}}))
        limits = caps.get("limits")
        self.assertTrue(next(c for c in limits.checks(caps.CTX) if c.name == "account").ok)
        config_file.write_text(json.dumps({"oauthAccount": {"accountUuid": "two"}}))
        self.assertFalse(next(c for c in limits.checks(caps.CTX) if c.name == "account").ok)

    def test_c3_expired_auth_is_one_item_not_one_per_worker(self):
        limits = caps.get("limits")
        for name in ("a", "b"):
            self._block(name, screen="Invalid API key · Please run /login\n")
        self._alive(a=4242, b=4243)
        limits.tick(caps.CTX, time.time())
        limits.tick(caps.CTX, time.time())
        self.assertEqual(len([i for i in items.open_items() if "sign in" in i["text"].lower()]), 1)

    def test_c4_new_wording_refuses_and_says_why_and_force_gets_through(self):
        repo = self.repo()
        path = self.worktree(repo)
        place = caps.CTX.backend.create("w", path, ["x"])
        ledger.append("spawn", "w", repo=str(repo), branch="t/w", path=str(path), backend="fake", space=place.space, block=place.block)
        self._screen(place, "> a box drawn some new way", {"state": "idle"})
        self._alive(w=os.getpid())
        with self.assertRaises(agents.Refused) as caught:
            agents.send(caps.CTX.backend, {**ledger.get("w"), "name": "w"}, "hello")
        self.assertEqual(caught.exception.code, "box_not_empty")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")

    def test_c5_an_api_outage_backs_off_and_resumes_once(self):
        limits = caps.get("limits")
        self._block("w", screen="API Error: 529 overloaded_error\n\x1b[2m❯ \x1b[0m", status_={"state": "idle"})
        self._alive(w=4242)
        base = time.time()
        self.assertEqual(limits.tick(caps.CTX, base)[0].kind, "routine")
        self.assertEqual(self._fake()["typed"], [])
        limits.tick(caps.CTX, base + limits.API_BACKOFF + 1)
        self.assertEqual(len(self._fake()["typed"]), 1)
        limits.tick(caps.CTX, base + 2 * limits.API_BACKOFF + 2)
        self.assertEqual(len(self._fake()["typed"]), 1, "resumes once")

    def test_c6_a_conversation_gone_is_flagged_never_started_fresh(self):
        hq._save({"block": "b", "space": "s"})
        found = hq.checks(caps.CTX)[0]
        self.assertFalse(found.ok)
        self.assertIsNone(found.repair)

    def _context(self, thousands):
        root = self.tmp / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(root)
        (root / "projects" / "x").mkdir(parents=True, exist_ok=True)
        (root / "projects" / "x" / "conv.jsonl").write_text(json.dumps({"message": {"usage": {"input_tokens": thousands * 1000}}}) + "\n")
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._screen(place, "", {"state": "working"})
        self._alive(athena=4242)

    def test_c7_past_hq_rotate_tokens_hq_is_asked_to_rotate_itself(self):
        self._context(410)
        found = next(c for c in hq.checks(caps.CTX) if c.name == "hq-context")
        self.assertFalse(found.ok)
        self.assertIsNotNone(found.repair)

    def test_c7_once_asked_it_only_says_so_for_two_hours(self):
        self._context(410)
        store.write_json(store.path("run", "rotation-asked.json"), {"ts": time.time()})
        self.assertIsNone(next(c for c in hq.checks(caps.CTX) if c.name == "hq-context").repair)

    def test_c8_a_permission_prompt_is_blocked_and_wakes(self):
        out = hooks.apply({"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "Claude needs permission to run: x"}, "w")
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(watch.diff({"w": {"state": "working"}}, {"w": {"state": "blocked"}})[0].kind, "wake")


class Outside(_Chaos):
    def test_g2_quota_backs_off_shows_and_recovers(self):
        rows = self._github_worker()
        self._gh('echo "API rate limit exceeded" >&2; exit 1')
        prs._fetch_all(rows)
        self.assertGreater(store.read_json(store.path("run", "prs-tick.json"))["ts"], time.time())
        found = next(c for c in prs.checks(caps.CTX) if c.name == "github")
        self.assertFalse(found.ok)
        self.assertIn("rate limit", found.detail)
        self._gh("echo '{\"number\": 7, \"state\": \"OPEN\"}'")
        prs._fetch_all(rows)
        self.assertLessEqual(store.read_json(store.path("run", "prs-tick.json"), {}).get("ts", 0), time.time())
        self.assertTrue(next(c for c in prs.checks(caps.CTX) if c.name == "github").ok)

    def test_g3_expired_auth_is_told_apart_from_an_outage(self):
        rows = self._github_worker()
        self._gh('echo "To get started with GitHub CLI, please run: gh auth login" >&2; exit 4')
        prs._fetch_all(rows)
        self.assertIn("sign in", next(c for c in prs.checks(caps.CTX) if c.name == "github").detail)
        self._gh('echo "error connecting to api.github.com" >&2; exit 1')
        prs._fetch_all(rows)
        self.assertIn("cannot reach", next(c for c in prs.checks(caps.CTX) if c.name == "github").detail)

    def test_g3_no_pr_is_not_an_outage(self):
        rows = self._github_worker()
        self._gh('echo "no pull requests found for branch" >&2; exit 1')
        prs._fetch_all(rows)
        self.assertTrue(next(c for c in prs.checks(caps.CTX) if c.name == "github").ok)

    def test_g5_a_job_that_never_started_counts_as_failing(self):
        self.assertEqual(prs.reduce_checks([{"status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}]), "failure")

    def _slack_tick(self, *errors, now=None):
        class Client:
            def __init__(self, token):
                pass

            def call(self, method, **params):
                raise errors[0]
        self.stub("security", "a-token")
        with mock.patch.object(slack, "Client", Client):
            return slack.tick(caps.CTX, time.time() if now is None else now)

    def test_g6_an_outage_backs_off_quietly(self):
        base = time.time()
        self.assertEqual(self._slack_tick(slack.SlackError("auth.test: URLError"), now=base), [])
        self.assertEqual(self._slack_tick(slack.SlackError("auth.test: URLError"), now=base + 1), [])
        self.assertEqual(store.read_json(store.path("slack-rex.json"))["fails"], 1, "held by its backoff")
        self.assertEqual(self._slack_tick(slack.SlackError("auth.test: URLError"), now=base + 2 * slack.POLL_EVERY + 1), [])
        self.assertEqual(store.read_json(store.path("slack-rex.json"))["fails"], 2)

    def test_g6_rate_limits_honour_retry_after(self):
        base = time.time()
        self._slack_tick(slack.RateLimited("conversations.history", 300), now=base)
        self.assertGreaterEqual(store.read_json(store.path("slack-rex.json"))["retry_at"], base + 300)

    def test_g6_a_revoked_token_is_told_once_and_says_off(self):
        first = self._slack_tick(slack.SlackError("auth.test: token_revoked"))
        self.assertEqual(len([line for line in first if line.kind == "wake"]), 1)
        again = self._slack_tick(slack.SlackError("auth.test: token_revoked"), now=time.time() + 700)
        self.assertEqual(again, [])
        self.assertFalse(slack.checks(caps.CTX)[0].ok)

    def test_g8_a_launch_never_needs_linear(self):
        repo = self.repo()
        out = spawn.spawn(caps.CTX, repo, "t/n", "n", linear="ABC-1", fetch=False, timeout=0.1)
        self.assertEqual(out["linear"], "ABC-1")


class Mac(_Chaos):
    def test_m1_a_wake_with_a_stale_watch_costs_hq_nothing(self):
        store.write_json(store.path("run", "watch-state.json"), {"beat": time.time() - 3000})
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._alive(athena=4242)
        with mock.patch.object(watch, "_start_detached") as restarted, mock.patch.object(hq, "_cycle") as cycled:
            doctor.run(caps.CTX, fix=True)
        cycled.assert_not_called()
        self.assertTrue(restarted.called)

    def test_m3_low_memory_holds_launches_and_names_the_biggest(self):
        os.environ["ATHENA_BUDGET_FAKE"] = "5,500,1.0"
        repo = self.repo()
        for name, pid in (("a", 4242), ("b", 4243)):
            spawn_record(name, self.tmp, repo)
        script = self.tmp / "ps-rss"
        script.write_text("#!/bin/sh\necho '4242 S claude --name a'\necho '4243 S claude --name b'\n"
                          "case \"$*\" in *rss*) echo '4242 100000'; echo '4243 2000000' ;; esac\n")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        store.ensure("run")
        agents.pid_file("a").write_text("4242")
        agents.pid_file("b").write_text("4243")
        found = next(c for c in doctor.checks(caps.CTX) if c.name == "memory")
        self.assertFalse(found.ok)
        self.assertIn("b", found.detail.split("biggest")[-1])
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/n", "n", fetch=False)

    def test_m3_one_heavy_slot_for_everyone(self):
        with store.locked("locks/heavy.lock"):
            out = self.cli("heavy", "--wait", "0", "--", "true")
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("slot", out.stderr)
        out = self.cli("heavy", "--wait", "0", "--", "sh", str(self._script("exit 7")))
        self.assertEqual(out.returncode, 7)

    def test_m3_heavy_work_waits_out_low_memory(self):
        out = self.cli("heavy", "--wait", "0", "--", "true", env={"ATHENA_BUDGET_FAKE": "5,500,1.0"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("memory", out.stderr)

    def _script(self, body):
        path = self.tmp / "s.sh"
        path.write_text(f"#!/bin/sh\n{body}\n")
        return path

    def test_m4_a_slow_server_is_not_a_dead_server(self):
        started = []
        backend = caps.CTX.backend
        with mock.patch.object(type(backend), "up", side_effect=BackendError("timeout", "no answer")), \
                mock.patch.object(type(backend), "start_server", lambda self: started.append(1)):
            out = doctor.run(caps.CTX, fix=True)
        self.assertEqual(started, [])
        self.assertIn("server", [p["check"] for p in out["problems"]])

    def test_m4_a_timeout_is_not_reported_as_down_by_rex(self):
        from athena_rex.backends.rex import Backend
        with mock.patch.object(Backend, "_run", side_effect=BackendError("timeout", "no answer")):
            with self.assertRaises(BackendError):
                Backend().up()
        with mock.patch.object(Backend, "_run", side_effect=BackendError("server_down", "gone")):
            self.assertFalse(Backend().up())

    def test_m4_high_load_holds_repairs(self):
        os.environ["ATHENA_BUDGET_FAKE"] = "80,500,99.0"
        calls = []
        check = caps.Check("part", False, "down", lambda: calls.append(1))
        with mock.patch.object(caps, "loaded", lambda: [mock.Mock(__name__="athena_rex.caps.fake", spec=["checks", "__name__"], checks=lambda ctx: [check])]):
            out = doctor.run(caps.CTX, fix=True)
        self.assertEqual(calls, [])
        self.assertIn("load", out["problems"][0]["detail"])

    def test_m7_every_signal_path_refuses_minus_one_zero_and_one(self):
        with mock.patch.object(canary._State, "log", str(self.tmp / "own-canary.log")):
            for pid in (-1, 0, 1):
                with self.subTest(pid=pid), self.assertRaises(canary.CanaryError):
                    canary._hook("os.kill", (pid, 15))
        store.ensure("run")
        script = self.tmp / "ps-liar"
        script.write_text("#!/bin/sh\necho '-1 S claude --name w'\necho '0 S claude --name w'\necho '1 S claude --name w'\n")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        with mock.patch("os.kill") as killed:
            for pid in ("-1", "0", "1"):
                agents.pid_file("w").write_text(pid)
                self.assertFalse(agents.kill_if_alive("w", grace=0))
        killed.assert_not_called()


class Together(_Chaos):
    def test_x1_a_wake_with_a_slow_github_costs_hq_nothing(self):
        self._github_worker()
        self._gh("sleep 3")
        store.write_json(store.path("run", "watch-state.json"), {"beat": time.time() - 3000})
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._alive(athena=4242)
        begun = time.monotonic()
        with mock.patch.object(prs, "fetch_due", lambda now: True):
            watch.step(caps.CTX, {}, 0, watch.Dedupe())
        self.assertLess(time.monotonic() - begun, 2)
        with mock.patch.object(watch, "_start_detached") as restarted, mock.patch.object(hq, "_cycle") as cycled:
            doctor.run(caps.CTX, fix=True)
        prs._fetcher.join()
        restarted.assert_called_once()
        cycled.assert_not_called()

    def test_x3_disk_full_then_crash_is_not_green(self):
        spawn_record("w", self.tmp, self.tmp, claude_args=["--name", "w"])
        with open(store.path("ledger.jsonl"), "a") as f:
            f.write('{"event": "retire", "na')
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        store.path("hq.json").write_text("")
        status.collect()
        out = doctor.run(caps.CTX, fix=False)
        names = [p["check"] for p in out["problems"]]
        self.assertIn("hq", names)
        self.assertIn("ledger", names)

    def test_x4_no_rotation_during_a_limit(self):
        limits = caps.get("limits")
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        limits.park("limit", time.time() + 600)
        with self.assertRaises(UserError):
            hq.rotate()
        self.assertFalse(store.path("handoff.md").exists())

    def test_x5_no_rotation_on_another_account(self):
        limits = caps.get("limits")
        config_file = self.home / ".claude.json"
        config_file.write_text(json.dumps({"oauthAccount": {"accountUuid": "one"}}))
        limits.checks(caps.CTX)
        config_file.write_text(json.dumps({"oauthAccount": {"accountUuid": "two"}}))
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        with self.assertRaises(UserError):
            hq.rotate()

    def test_x6_a_blocked_hq_is_raised_and_a_dead_worker_left_to_rex(self):
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._screen(place, "", {"state": "blocked", "msg": "a question"})
        self._alive(athena=4242)
        store.write_json(store.path("run", "hq-blocked.json"), {"since": time.time() - 3600})
        spawn_record("w", self.tmp, self.tmp, claude_args=["--name", "w"])
        names = [p["check"] for p in doctor.run(caps.CTX, fix=False)["problems"]]
        self.assertIn("hq-blocked", names)
        self.assertNotIn("worker:w", names)

    def test_x9_quota_costs_hq_no_restart(self):
        self._github_worker()
        self._gh('echo "API rate limit exceeded" >&2; exit 1')
        place = caps.CTX.backend.create("athena", self.tmp, ["x"])
        hq._save({"session": "conv", "space": place.space, "block": place.block})
        self._alive(athena=4242)
        with mock.patch.object(hq, "_cycle") as cycled:
            doctor.run(caps.CTX, fix=True)
        cycled.assert_not_called()

    def test_x10_two_callers_at_once_leave_one_space_and_a_good_record(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        procs = [subprocess.Popen([str(BIN), "hq", "--no-routine"], env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(4)]
        for proc in procs:
            proc.wait(timeout=30)
        self.assertEqual(len(self._fake()["spaces"]), 1)
        record = store.read_json(store.path("hq.json"))
        self.assertIn(record["space"], self._fake()["spaces"])
        self.assertIn(record["block"], self._fake()["spaces"][record["space"]]["blocks"])

    def test_x12_a_silent_doorbell_shows_on_the_dash(self):
        store.write_json(store.path("slack-rex.json"), {"off": "the keychain is locked", "polled": time.time()})
        self.assertFalse(slack.checks(caps.CTX)[0].ok)
        self.assertIn("keychain", slack.checks(caps.CTX)[0].detail)

