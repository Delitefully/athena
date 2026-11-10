import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import types
import urllib.error
import urllib.request
from unittest import mock

from harness import BIN, Case, ROOT, SANDBOX, dead_pid, spawn_record, store

from athena_rex.caps import brief, doctor, hooks, hq, items, prs, reviews, services, slack, watch
from athena_rex.caps.dash import markdown, view
from athena_rex.cli import UserError
from athena_rex.kernel import agents, bus, caps, config, ledger, world


class Items(Case):
    def test_file_stays_readable_by_the_old_shape(self):
        item = items.add("Which cloud?", name="Clouds", kind="decision", options=["Yes", "No"], rec="yes", words="asked")
        data = json.loads(store.path("needs-you.json").read_text())
        self.assertEqual(data["next"], 2)
        self.assertEqual(item["id"], "a1")
        for key in ("id", "kind", "text", "why", "options", "name", "next", "created", "updated", "log"):
            self.assertIn(key, data["items"][0])

    def test_unknown_fields_are_kept(self):
        store.write_json(store.path("needs-you.json"), {"next": 2, "items": [{"id": "a1", "text": "t", "previews": ["x"]}]})
        items.edit("a1", why="new")
        self.assertEqual(items.load()["items"][0]["previews"], ["x"])

    def test_a_file_that_will_not_parse_is_never_overwritten(self):
        store.path("needs-you.json").write_text("{ broken")
        with self.assertRaises(UserError):
            items.add("New thing")
        self.assertEqual(store.path("needs-you.json").read_text(), "{ broken")

    def test_a_default_never_applies_with_stakes(self):
        with self.assertRaises(UserError):
            items.add("Risky?", options=["a"], rec="a", auto=True, stakes=["money"])
        item = items.add("Safe?", options=["a"], rec="a", auto=True)
        items.edit(item["id"], stakes=["security"])
        self.assertEqual(items.apply_defaults(now=time.time() + 90000), [])

    def test_defaults_apply_at_their_time_and_only_once(self):
        now = time.time()
        items.apply_defaults(now=now - 1)
        item = items.add("Safe?", options=["a"], rec="a", auto=True, now=now)
        store.append_line(store.path("requests.jsonl"), {"id": "r1", "ts": now, "text": "hi"})
        self.assertEqual(items.apply_defaults(now=now + 3600), [])
        applied = items.apply_defaults(now=now + 3 * 3600)
        self.assertEqual([a["item"] for a in applied], [item["id"]])
        self.assertEqual(items.apply_defaults(now=now + 4 * 3600), [])

    def test_queue_waits_out_the_undo_window(self):
        item = items.add("Q?", options=["yes"], kind="decision")
        now = time.time()
        answer = items.answer(item["id"], now=now, option="yes")
        self.assertEqual(items.queue(now=now + 1), [])
        self.assertTrue(items.undo(answer["seq"], now=now + 1))
        again = items.answer(item["id"], now=now + 2, option="yes")
        self.assertEqual(items.queue(now=now + 20)[0]["option"]["id"], "yes")
        with self.assertRaises(UserError):
            items.answer(item["id"], now=now + 3, option="yes")
        items.resolve(item["id"], "done", now=now + 30)
        self.assertEqual(items.queue(now=now + 30), [])
        self.assertGreater(again["seq"], answer["seq"])

    def test_resolved_items_archive_after_a_week(self):
        item = items.add("Old", now=1000.0)
        items.resolve(item["id"], now=1000.0)
        other = items.add("Later")
        items.resolve(other["id"], now=time.time())
        self.assertEqual([i["id"] for i in items.load()["items"]], [other["id"]])
        self.assertEqual(list(store.read_lines(store.path("items-archive.jsonl")))[0]["id"], item["id"])

    def test_request_log_redacts_secrets_and_skips_what_is_not_human(self):
        token = "ghp_" + "a" * 36
        record = items.log_request(f"use {token} please", "s")
        self.assertNotIn(token, record["text"])
        self.assertIsNone(items.log_request("athena watch 22:19: x", "s"))
        self.assertEqual(os.stat(store.path("requests.jsonl")).st_mode & 0o777, 0o600)

    def test_edit_rejects_a_flag_it_does_not_do(self):
        out = self.cli("item", "edit", "a1", "--preview", "x")
        self.assertEqual(out.returncode, 2)


class Brief(Case):
    def test_rules(self):
        good = "GOAL: Build the thing.\nSCOPE: x\nACCEPTANCE:\n- The page returns 200 on /\nVERIFY: make check\nFORBIDDEN: y\n"
        self.assertEqual(brief.check(good), [])
        self.assertTrue(any("missing" in p for p in brief.check("GOAL: x\n")))
        self.assertTrue(any("others" in p for p in brief.check(good.replace("Build the thing.", "the PR is merged"))))
        self.assertTrue(any("VERIFY" in p for p in brief.check(good.replace("make check", "gh pr checks 12 --watch"))))
        self.assertTrue(brief.check_goal("SendMessage to athena must succeed"))


class Reviews(Case):
    def test_rounds_and_heads(self):
        spawn_record("w", self.tmp, self.tmp)
        with self.assertRaises(UserError):
            reviews.record("w", "pass")
        self.assertEqual(reviews.record("w", "start", head="a" * 40)["round"], 1)
        self.assertEqual(reviews.record("w", "fail")["head"], "a" * 40)
        second = reviews.record("w", "start", head="b" * 40)
        self.assertEqual((second["round"], second["since"]), (2, "a" * 40))
        reviews.record("w", "pass")
        for _ in range(4):  # rounds past a pass, a rebase say, only warn
            reviews.record("w", "start", head="b" * 40)

    def test_from_round_three_after_two_fails_review_needs_force_up_to_eight(self):
        spawn_record("w", self.tmp, self.tmp)
        for head in ("a", "b"):
            reviews.record("w", "start", head=head * 40)
            reviews.record("w", "fail")
        with self.assertRaises(UserError) as caught:
            reviews.record("w", "start", head="c" * 40)
        self.assertIn("--force", str(caught.exception))
        self.assertEqual(reviews.record("w", "start", head="c" * 40, force=True)["round"], 3)
        for _ in range(5):
            reviews.record("w", "start", head="c" * 40, force=True)
        with self.assertRaises(UserError):
            reviews.record("w", "start", head="c" * 40, force=True)  # --force from round 3 never lifts the 8-round cap


class Prs(Case):
    def test_checks_reduce(self):
        self.assertEqual(prs.reduce_checks([]), "none")
        self.assertEqual(prs.reduce_checks([{"conclusion": "SUCCESS", "status": "COMPLETED"}]), "success")
        self.assertEqual(prs.reduce_checks([{"status": "IN_PROGRESS"}, {"conclusion": "SUCCESS", "status": "COMPLETED"}]), "pending")
        self.assertEqual(prs.reduce_checks([{"conclusion": "FAILURE", "status": "COMPLETED"}]), "failure")

    def test_whose_move(self):
        base = {"state": "OPEN", "checks": "success", "review": "APPROVED", "head": "a" * 40}
        self.assertEqual(prs.move(base, False, True)["what"], "approved · yours to merge")
        self.assertEqual(prs.move(base, True, True, None, True)["what"], "in review · athena")
        self.assertEqual(prs.move(base, True, True, {"verdict": "pass", "head": "a" * 40}, True)["section"], "you")
        self.assertEqual(prs.move(base, True, True, {"verdict": "pass", "head": "b" * 40}, True)["section"], "flight")
        self.assertEqual(prs.move({**base, "checks": "failure"}, True, False)["what"], "checks failing")
        self.assertEqual(prs.move({**base, "state": "MERGED"}, True, False)["section"], "done")
        waiting = {"state": "OPEN", "checks": "success", "review": "REVIEW_REQUIRED", "requested": ["sam"]}
        self.assertEqual(prs.move(waiting, False, False)["section"], "awaiting")

    def test_a_repo_not_on_github_never_reaches_gh(self):
        repo = self.repo()
        self.assertIsNone(prs.summary(repo, "main"))
        self.assertEqual(self.world_calls(), [])

    def test_bots_do_not_count_as_review_comments(self):
        out = prs.reduce({"author": {"login": "me"}, "comments": [{"author": {"login": "copilot-x"}},
                                                                  {"author": {"login": "sam"}}, {"author": {"login": "me"}}]})
        self.assertEqual(out["comments"], 1)


class Watch(Case):
    def test_diff_and_kinds(self):
        lines = watch.diff({"w": {"state": "working"}}, {"w": {"state": "blocked", "report": {"status": "BLOCKED"}}})
        kinds = {line.text: line.kind for line in lines}
        self.assertEqual(kinds["w state working->blocked"], "wake")
        self.assertEqual(kinds["w report BLOCKED"], "wake")
        progress = watch.diff({"w": {"state": "working"}}, {"w": {"state": "idle", "report": {"status": "IN_PROGRESS"}}})
        self.assertEqual({line.text: line.kind for line in progress}["w report IN_PROGRESS"], "routine")

    def test_retiring_exit_is_quiet(self):
        lines = watch.diff({"w": {"state": "idle"}}, {"w": {"state": "exited"}}, retiring={"w"})
        self.assertEqual(lines[0].kind, "quiet")

    def test_changes_during_a_restart_are_told(self):
        spawn_record("w", self.tmp, self.tmp)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text(str(dead_pid()))
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        os.environ.pop("ATHENA_BACKEND")
        os.environ["ATHENA_BACKEND"] = "fake"
        first = self.cli("watch", "--once")
        self.assertEqual(first.returncode, 0, first.stderr)
        hooks.apply({"hook_event_name": "Stop", "last_assistant_message": 'x\nATHENA-REPORT {"status":"BLOCKED"}',
                     "cwd": str(self.tmp)}, "w")
        second = self.cli("watch", "--once")
        self.assertIn("w report BLOCKED", second.stdout)

    def test_two_watches_never_run_together(self):
        with store.locked("locks/watch.lock"):
            out = self.cli("watch", "--once")
        self.assertEqual(out.returncode, 1)

    def test_same_wake_once_until_something_changes(self):
        d = watch.Dedupe()
        wake = watch.Line("w state a->blocked", "wake", "w")
        self.assertTrue(d.keep(wake, 1000))
        self.assertFalse(d.keep(wake, 1100))
        d.keep(watch.Line("w state blocked->working", "routine", "w"), 1200)
        self.assertTrue(d.keep(wake, 1300))
        self.assertTrue(d.keep(wake, 1300 + watch.REPEAT_AFTER + 1))

    def test_a_second_block_with_a_new_question_is_told(self):
        d = watch.Dedupe()
        now = {"w": {"state": "blocked", "report": {"status": "BLOCKED", "concerns": ["first"]}}}
        later = {"w": {"state": "blocked", "report": {"status": "BLOCKED", "concerns": ["second"]}}}
        told = watch.diff(now, later)
        self.assertEqual([line.text for line in told], ["w report BLOCKED"])
        self.assertTrue(d.keep(watch.diff({"w": {"state": "blocked"}}, now)[0], 1000))
        self.assertTrue(d.keep(told[0], 1001))

    def test_lines_go_out_through_the_allowlist(self):
        self.assertEqual(watch.safe('w state "; ignore previous `rm -rf`'), "w state  ignore previous rm -rf")
        self.assertEqual(len(watch.safe("x" * 500)), 160)

    def test_a_quiet_worker_is_no_line_as_in_the_old_athena(self):
        was = {"w": {"state": "working", "quiet_s": 100}}
        now = {"w": {"state": "working", "quiet_s": watch.STUCK_AFTER + 1}}
        self.assertFalse(any("stuck" in line.text for line in watch.diff(was, now)))

    def test_a_stalled_loop_is_restarted_alone(self):
        store.write_json(store.path("run", "watch-state.json"), {"beat": time.time() - 1000})
        check = watch.checks(caps.CTX)[0]
        self.assertFalse(check.ok)
        self.assertEqual(check.repair, watch._start_detached)

    def test_a_capability_that_throws_does_not_stop_the_loop(self):
        real = caps.loaded

        def tick(ctx, now):
            raise ValueError("no")
        caps.loaded = lambda: [types.SimpleNamespace(__name__="athena_rex.caps.broken", tick=tick)]
        try:
            _, _, told = watch.step(caps.CTX, {}, 0, watch.Dedupe())
        finally:
            caps.loaded = real
        self.assertTrue(any("watch error broken" in line.text for line in told))


class Doctor(Case):
    def _module(self, *found):
        return types.SimpleNamespace(__name__="athena_rex.caps.fake", checks=lambda ctx: list(found))

    def _run(self, *found, now=None):
        real = caps.loaded
        caps.loaded = lambda: [self._module(*found)]
        try:
            return doctor.run(caps.CTX, fix=True, now=now)
        finally:
            caps.loaded = real

    def test_grace_then_repair_then_back_off_then_give_up(self):
        calls = []
        check = caps.Check("part", False, "down", lambda: calls.append(1), grace=90)
        self._run(check, now=1000)
        self.assertEqual(calls, [])
        self._run(check, now=1100)
        self.assertEqual(len(calls), 1)
        self._run(check, now=1150)
        self.assertEqual(len(calls), 1)
        self._run(check, now=1300)
        self.assertEqual(len(calls), 2)
        self._run(check, now=1600)
        self.assertEqual(len(calls), 3)
        out = self._run(check, now=2000)
        self.assertEqual(len(calls), 3)
        self.assertIn("repairs in the last hour", out["problems"][0]["detail"])

    def test_writes_what_readers_expect(self):
        out = self._run(caps.Check("a", False, "bad"), caps.Check("b", True, "fine"), now=time.time())
        data = store.read_json(store.path("health.json"))
        self.assertEqual(data, json.loads(json.dumps(out)))
        for key in ("ts", "ok", "problems", "checks", "actions", "hq_alive"):
            self.assertIn(key, data)
        self.assertEqual(data["problems"][0]["check"], "a")

    def test_the_old_athenas_keys_in_its_memory_stay(self):
        store.write_json(store.path("doctor.json"), {"caffeinate_pid": 5})
        self._run(caps.Check("a", True), now=1000)
        self.assertEqual(store.read_json(store.path("doctor.json"))["caffeinate_pid"], 5)

    def test_a_check_that_throws_is_a_finding(self):
        def checks(ctx):
            raise ValueError("no")
        real = caps.loaded
        caps.loaded = lambda: [types.SimpleNamespace(__name__="athena_rex.caps.broken", checks=checks)]
        try:
            out = doctor.run(caps.CTX, fix=False, now=1000)
        finally:
            caps.loaded = real
        self.assertEqual(out["problems"][0]["check"], "broken")

    def test_health_is_stamped_when_the_pass_ends(self):
        """A pass stamped with its start wrote a heartbeat already stale when the pass was slow."""
        started = time.time() - doctor.DOCTOR_STALE - 60
        out = self._run(caps.Check("a", True), now=started)
        self.assertGreater(out["ts"], started + doctor.DOCTOR_STALE)
        self.assertEqual(store.read_json(store.path("health.json"))["ts"], out["ts"])
        self.assertEqual(doctor.tick(caps.CTX, time.time()), [], "the watch does not call a doctor that just reported dead")

    def test_busy_says_so_in_json(self):
        with store.locked("doctor.lock", blocking=False) as got, mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertTrue(got)
            self.assertEqual(doctor._run(types.SimpleNamespace(fix=True, json=True, quiet=False, last=False)), 0)
        self.assertEqual(json.loads(out.getvalue()), {"busy": True, "detail": "another doctor is running"})
        self.assertEqual(err.getvalue(), "")
        with store.locked("doctor.lock", blocking=False), mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            doctor._run(types.SimpleNamespace(fix=True, json=False, quiet=True, last=False))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", "another doctor is running\n"))

    def _slow_module(self):
        """A module whose check sleeps while `slow` exists, and says when it began."""
        folder = self.tmp / "modules" / "slow"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text('{"name": "slow", "title": "Slow"}')
        (folder / "__init__.py").write_text(
            "import os, time\nfrom athena_rex.kernel import caps\nSUMMARY = 'a slow check'\n"
            f"FLAG, BEGAN = {str(self.tmp / 'slow')!r}, {str(self.tmp / 'began')!r}\n"
            "def checks(ctx):\n    open(BEGAN, 'w').close()\n    while os.path.exists(FLAG):\n        time.sleep(0.05)\n"
            "    return [caps.Check('slow', True, 'done')]\n")
        self.write_config(rex={"modules": ["slow"], "modules_dir": str(self.tmp / "modules")})
        (self.tmp / "slow").write_text("")

    def _doctor(self, **env):
        return subprocess.Popen([str(BIN), "doctor", "--json"], env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _began(self):
        for _ in range(400):
            if (self.tmp / "began").exists():
                return
            time.sleep(0.05)
        self.fail("the slow check never began")

    def test_a_pass_past_its_time_limit_ends_and_frees_the_lock(self):
        self._slow_module()
        first = self._doctor(ATHENA_DOCTOR_LIMIT="3")
        self._began()
        busy = self.cli("doctor", "--json")
        self.assertEqual(json.loads(busy.stdout)["busy"], True, busy.stderr)
        out, err = first.communicate(timeout=30)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("doctor gave up after 3s", err)
        self.assertNotIn("Traceback", err)
        (self.tmp / "slow").unlink()
        again = self.cli("doctor", "--json")
        self.assertNotIn("another doctor", again.stdout + again.stderr)
        self.assertEqual(json.loads(again.stdout)["checks"]["slow"], {"ok": True, "detail": "done"})

    def test_a_killed_doctor_leaves_its_lock_free(self):
        self._slow_module()
        first = self._doctor()
        self._began()
        first.kill()
        first.communicate(timeout=30)
        (self.tmp / "slow").unlink()
        again = self.cli("doctor", "--json")
        self.assertIn("slow", json.loads(again.stdout)["checks"], again.stderr)

    def test_the_time_limit_is_not_left_armed(self):
        seen = {"ok": True, "checks": {}, "actions": [], "problems": []}
        before = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(doctor, "run", return_value=seen), mock.patch("sys.stdout", new_callable=io.StringIO):
            doctor._run(types.SimpleNamespace(fix=False, json=True, quiet=False, last=False))
        self.assertEqual(signal.alarm(0), 0, "an alarm left armed would end HQ, which runs doctor in-process")
        self.assertIs(signal.getsignal(signal.SIGALRM), before)

    def test_a_retiring_worker_is_left_alone(self):
        spawn_record("w", self.tmp, self.tmp, retiring=time.time())
        self.assertFalse(any(c.name == "worker:w" for c in doctor.checks(caps.CTX)))


class Services(Case):
    def test_each_capability_lists_its_own(self):
        names = {s.name for s in services.wanted(caps.CTX)}
        self.assertTrue({"dash", "watch", "doctor"} <= names)

    def test_a_state_path_with_spaces_survives(self):
        os.environ["ATHENA_STATE_DIR"] = str(self.tmp / "Application Support" / "athena")
        store.ensure()
        plist = services.plist_for(caps.Service("x", ["watch"]))
        self.assertIn(" ", plist["EnvironmentVariables"]["ATHENA_STATE_DIR"])
        self.assertEqual(plist["ProgramArguments"][1:], ["watch"])


class Hq(Case):
    def test_a_lost_record_falls_back_a_generation(self):
        hq._save({"session": "one", "block": "b"})
        hq._save({"session": "two", "block": "b"})
        store.path("hq.json").unlink()
        self.assertEqual(hq.load()["session"], "one")

    def test_a_running_hq_never_gets_a_second_claude(self):
        real = hq.alive
        hq.alive = lambda: True
        self.addCleanup(lambda: setattr(hq, "alive", real))
        self.assertEqual(hq.open_hq(focus=False)["outcome"], "running")
        self.assertEqual(caps.CTX.backend.spaces(), [])
        check = hq.checks(caps.CTX)[0]
        self.assertFalse(check.ok)
        self.assertIsNone(check.repair)

    def test_a_gone_hq_comes_back_on_its_conversation(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        out = hq.open_hq(focus=False, routine=False)
        self.assertEqual(out["outcome"], "resumed")
        self.assertIn("--resume conv", store.path("run", "athena.sh").read_text())
        record = hq.load()
        self.assertEqual(record["session"], "conv")
        self.assertEqual(record["claude_args"][0], "--name")
        self.assertNotIn("conv", record["claude_args"])

    def test_hq_with_no_conversation_is_flagged_not_started(self):
        hq._save({"block": "b", "space": "s"})
        self.assertIsNone(hq.checks(caps.CTX)[0].repair)

    def test_handoff_holds_every_open_item_uncut(self):
        title = "A decision with a title that must arrive whole in the handoff"
        items.add(title, name="Named", kind="decision", options=["a"])
        text = hq.build_handoff("thread one")
        self.assertIn(title, text)
        self.assertIn("thread one", text)

    def test_context_percent_reads_the_transcript(self):
        folder = self.home / ".claude" / "projects" / "x"
        folder.mkdir(parents=True)
        (folder / "s1.jsonl").write_text(json.dumps({"message": {"usage": {"input_tokens": 1000, "cache_read_input_tokens": 99000}}}) + "\n")
        self.assertEqual(hq.context_percent("s1", window=200000), 50)
        self.assertIsNone(hq.context_percent("missing"))



class Slack(Case):
    class Client:
        def __init__(self, history):
            self.history = history

        def call(self, method, **params):
            if method == "auth.test":
                return {"user_id": "ME"}
            if method == "users.conversations":
                return {"channels": [{"id": "D1"}]}
            return {"messages": self.history.get(params["channel"], [])}

    def test_first_poll_rings_nothing_then_counts_others_only(self):
        state = {}
        self.assertEqual(slack.poll(self.Client({}), state, ["C1"], now=1000.0), [])
        msgs = [{"ts": "1001.0", "user": "U1"}, {"ts": "1002.0", "user": "ME"}, {"ts": "1003.0", "user": "U2", "bot_id": "B"},
                {"ts": "1004.0", "user": "U1", "subtype": "channel_join"}]
        rings = slack.poll(self.Client({"C1": msgs}), state, ["C1"], now=1100.0)
        self.assertEqual(rings, [("C1", 1, False, False)])

    def test_a_dm_the_human_is_in_is_active(self):
        state = {}
        slack.poll(self.Client({}), state, [], now=1000.0)
        msgs = [{"ts": "1050.0", "user": "ME"}, {"ts": "1060.0", "user": "U1"}]
        self.assertEqual(slack.poll(self.Client({"D1": msgs}), state, [], now=1100.0), [("D1", 1, True, True)])

    def test_with_no_stub_the_token_is_never_read(self):
        os.environ.pop("ATHENA_WORLD_STUBS")
        with self.assertRaises(world.Off):
            slack.read_token()

    def test_off_says_so(self):
        store.write_json(store.path("slack-rex.json"), {"off": "no token"})
        self.assertFalse(slack.checks(caps.CTX)[0].ok)


class Clamshell(Case):
    """A public module now, off by default: these run it from the config list, as `modules load clamshell` leaves it."""
    BATT = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t{}%; charging"

    def setUp(self):
        super().setUp()
        self.write_config(rex={"modules": ["clamshell"]})
        self.clamshell = caps.get("clamshell")
        self.assertIsNotNone(self.clamshell)

    def test_power(self):
        self.assertTrue(self.clamshell.parse_power(self.BATT.format(80))[0])
        self.assertFalse(self.clamshell.parse_power(self.BATT.format(10))[0])
        self.assertFalse(self.clamshell.parse_power("Now drawing from 'Battery Power'\n")[0])
        self.assertFalse(self.clamshell.parse_power("")[0])

    def test_decide(self):
        fresh = {"ts": 1000, "hq_alive": True}
        self.assertTrue(self.clamshell.decide(True, "ok", True, fresh, 1000)["awake"])
        battery = self.clamshell.decide(False, "on battery", True, fresh, 1000)
        self.assertEqual((battery["awake"], battery["sleep_now"]), (False, True))
        self.assertFalse(self.clamshell.decide(True, "ok", True, {"ts": 0, "hq_alive": True}, 1000)["awake"])
        self.assertFalse(self.clamshell.decide(True, "ok", True, {"ts": 1000, "hq_alive": False}, 1000)["awake"])
        self.assertFalse(self.clamshell.decide(True, "ok", None, None, 1000)["sleep_now"])

    def test_with_no_stub_power_is_never_read(self):
        os.environ.pop("ATHENA_WORLD_STUBS")
        with self.assertRaises(world.Off):
            self.clamshell.current(1000)
        self.assertEqual(self.clamshell.checks(caps.CTX)[0].detail, "not read here")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dash(Case):
    def setUp(self):
        super().setUp()
        self.port = _free_port()
        os.environ["ATHENA_DASH_PORT"] = str(self.port)
        config.reset()
        items.add("Is <script>alert(1)</script> safe?", name="Evil <b>", kind="decision", options=["Yes"], rec="yes",
                  body="# Title\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert(1)) [ok](https://example.com)")
        self.proc = subprocess.Popen([str(self._bin()), "dash", "run"], env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(100):
            try:
                self._get("/healthz")
                break
            except OSError:
                time.sleep(0.05)

    def _bin(self):
        from harness import BIN
        return BIN

    def _stop(self):
        self.proc.terminate()
        self.proc.wait()

    def _get(self, path, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": host} if host else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()

    def _post(self, path, body, token=None, origin=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if token:
            headers["X-Athena-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _token(self):
        page = self._get("/")[1]
        return page.split('name="token" content="')[1].split('"')[0]

    def test_open_items_on_the_page_and_escaped(self):
        status, page = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn('id="a1"', page)
        self.assertIn("&lt;script&gt;alert(1)", page)
        self.assertNotIn("<script>alert(1)", page)

    def test_item_view_drops_raw_html_and_bad_links(self):
        page = self._get("/item/a1")[1]
        self.assertNotIn("<img", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn('href="https://example.com"', page)

    def test_another_host_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/", host="evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_writes_need_token_and_origin(self):
        body = {"item": "a1", "option": "yes"}
        self.assertEqual(self._post("/api/action", body), 403)
        self.assertEqual(self._post("/api/action", body, token=self._token(), origin=False), 403)
        self.assertEqual(self._post("/api/action", {"item": "a1", "option": "nope"}, token=self._token()), 409)
        self.assertEqual(self._post("/api/action", body, token=self._token()), 200)
        self.assertEqual(self._post("/api/action", body, token=self._token()), 409)
        line = list(store.read_lines(store.path("actions.jsonl")))[0]
        self.assertEqual((line["item"], line["option"], line["status"]), ("a1", "yes", "queued"))

    def test_done_is_written_by_the_page_itself(self):
        self.assertEqual(self._post("/api/item", {"item": "a1", "do": "done"}, token=self._token()), 200)
        self.assertTrue(items.load()["items"][0]["resolved"])

    def test_a_broken_state_file_never_takes_the_page_down(self):
        store.path("watch.json").write_text("{ broken")
        store.path("ledger.jsonl").write_text('{"event":"update","name":"ghost"}\n')
        store.path("health.json").write_text("nope")
        self.assertEqual(self._get("/")[0], 200)

    def test_health_is_never_green_without_a_fresh_check(self):
        now = time.time()
        self.assertEqual(view._health(None, now)["color"], "grey")
        self.assertEqual(view._health({"ts": now - 500, "ok": True}, now)["color"], "grey")
        self.assertEqual(view._health({"ts": now, "ok": True}, now)["color"], "green")
        self.assertEqual(view._health({"ts": now, "ok": False, "problems": [{"check": "x"}]}, now)["color"], "red")

    def test_a_test_runs_cleanup_stops_its_own_dash_and_nothing_else(self):
        """dash.on() starts the dash in a session of its own, so a run cut short left it serving."""
        store.write_json(store.path("run", "dash.json"), {"pid": self.proc.pid, "port": self.port})
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        for n, pid in enumerate((other.pid, -1, 0, 1, "x")):
            (self.tmp / f"s{n}" / "run").mkdir(parents=True)
            (self.tmp / f"s{n}" / "run" / "dash.json").write_text(json.dumps({"pid": pid, "port": 1}))
        ps = self.tmp / "ps-commands"  # what the real ps says of each, `ps -ww -o command= -p <pid>`; the sandbox can't run it
        ps.write_text(f"#!/bin/sh\ncase \"$5\" in\n{self.proc.pid}) echo '/usr/bin/python3 -I -S -c import sys\\012from athena_rex.cli import main {ROOT} dash run' ;;\n"
                      "*) echo 'sleep 60' ;;\nesac\n")
        ps.chmod(0o755)
        done = subprocess.run(["sh", str(ROOT / "scripts" / "stop-dashes.sh"), str(self.tmp)], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "ATHENA_STOP_DASHES_PS": str(ps)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNotNone(self.proc.wait(timeout=10))
        self.assertIsNone(other.poll(), "a pid file naming something that isn't a dash stops nothing")
        for script in ("test.sh", "smoke.sh"):
            self.assertIn("stop-dashes.sh", (ROOT / "scripts" / script).read_text(), script)

    def test_markdown(self):
        self.assertIn("<table>", markdown.render("|a|b|\n|-|-|\n|1|2|"))
        self.assertIn("<li>x</li>", markdown.render("- x"))
        self.assertIn("&lt;b&gt;", markdown.render("<b>"))


class Bus_(Case):
    def test_the_gate_and_state_changes_reach_the_bus(self):
        hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "hi", "cwd": str(self.tmp)}, "w")
        topics = [e["topic"] for e in bus.tail(0)[0]]
        self.assertIn("worker.gate", topics)
