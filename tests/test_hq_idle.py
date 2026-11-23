"""HQ-IDLE: a Claude whose OSC 7501 probe got no reply never sends Rex a program status. Past a startup grace, `state` reads its
screen instead, so HQ's rotation (and every other idle check) is not skipped forever; the skip and doctor name the real reason."""
import json
import os
import time
from unittest import mock

from harness import Case, spawn_record

from athena.caps import doctor, hq
from athena.kernel import agents, caps, config, ledger, store
from athena.kernel.backend import BackendError

RULE = "─" * 40
IDLE = f"✻ Brewed for 9m 13s · done 3:14 PM\n\n{RULE}\n❯ \n{RULE}\n  Opus 5.5 · high\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
WORKING = f"· Infusing… (3m 15s · ↓ 8.1k tokens)\n\n{RULE}\n❯ \n{RULE}\n  Opus 5.5 · high\n  ⏵⏵ auto mode on · 1 shell · esc to interrupt\n"
FOOTER_ONLY = f"\n{RULE}\n❯ \n{RULE}\n  Opus 5.5 · high\n⏵⏵automodeon (shift+tabtocycle)·esctointerrupt·←1agent\n"  # Rex draws spaces as cursor moves
TYPED = f"✻ Brewed for 9m 13s\n\n{RULE}\n❯ half a thought\n{RULE}\n  Opus 5.5 · high\n"
OLD_WORDS = "  I said esc to interrupt earlier, and\n· Thinking… too\n" + "\n" * 24  # far above the box: scrollback, not now
NO_BOX = "Loading…\n"
FOOT = "  Opus 5.5 · high\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"  # a footer with no "esc to interrupt": cut off, or never there
PERMISSION = (f"✻ Brewed for 9m 13s\n\n{RULE}\n Bash command\n   rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n"
              "   2. Yes, and don't ask again\n   3. No, and tell Claude what to do differently (esc)\n")
MENU = (f"✻ Brewed for 9m 13s\n\n{RULE}\n ☐ Approach\n\nWhich approach?\n\n❯ 1. Small\n     Keep it small\n  2. Wide\n"
        "  3. Type something.\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n")
BACKGROUND = f"✻ Brewed for 9m 13s\n\n{RULE}\n Background work is running\n ❯ 1. Exit and stop tasks\n   2. Cancel\n"
OLD_BOX = f"❯ an older prompt of mine\n  ⎿  answered\n\n✻ Brewed for 9m 13s\n\n{RULE}\n Some dialog with no box\n   1. Go on\n"
LONG_TODO = "✳ Running the tests… (2m 3s)\n" + "".join(f"  ◻ step {n} of the plan\n" for n in range(32)) + f"\n{RULE}\n❯ \n{RULE}\n{FOOT}"
NO_DONE_ROW = f"⏺ Here is what I found.\n  Two things.\n\n{RULE}\n❯ \n{RULE}\n{FOOT}"
QUOTED_HINT = f"✻ Brewed for 9m 13s\n\n{RULE}\n❯ \n{RULE}\n{FOOT}".replace("✻ Brewed", "⏺ It said esc to interrupt.\n\n✻ Brewed")
FAINT, PLAIN, ORANGE = "\x1b[2m", "\x1b[0m", "\x1b[38;2;215;119;87m"
STYLED_IDLE = (f"{ORANGE}✻{PLAIN} Brewed\x1b[1Cfor\x1b[1C9m\x1b[1C13s\n\n{RULE}\n❯\x1b[1C\x1b[7m \x1b[27m{FAINT}Try \"fix the tests\"{PLAIN}\n"
               f"{RULE}\n{FOOT}")
STYLED_WORKING = (f"{ORANGE}✶{PLAIN}\x1b[1CInfusing…\x1b[1C(3m\x1b[1C15s)\n\n{RULE}\n❯\x1b[1C\x1b[7m \x1b[27m{FAINT}Try \"x\"{PLAIN}\n"
                  f"{RULE}\n{FOOT}")


class Fallback(Case):
    def setUp(self):
        super().setUp()
        self.place = caps.CTX.backend.create(config.hq_name(), self.tmp, ["x"], block_label=config.hq_name())
        hq._save({"session": "conv", "space": self.place.space, "window": self.place.window, "block": self.place.block})
        store.ensure("run")
        agents.pid_file(config.hq_name()).write_text("4242\n")
        runs = mock.patch.object(agents, "stat_of", return_value="S")
        runs.start()
        self.addCleanup(runs.stop)

    def show(self, screen, status_=None, place=None):
        place = place or self.place
        path = os.environ["ATHENA_FAKE_BACKEND"]
        data = json.load(open(path))
        data["spaces"][place.space]["blocks"][place.block].update(screen=screen, status=status_)
        json.dump(data, open(path, "w"))

    def started_ago(self, seconds, name=None):
        then = time.time() - seconds
        os.utime(agents.pid_file(name or config.hq_name()), (then, then))

    def now(self):
        return agents.state(caps.CTX.backend, hq._record_for_agents(hq.load()))


class State(Fallback):
    def test_no_record_past_the_grace_with_an_empty_box_is_idle(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(IDLE)
        now = self.now()
        self.assertEqual((now["state"], now["kind"]), ("idle", "unreported"))
        self.assertIn("no program status from Rex", now["msg"])

    def test_no_record_with_a_spinner_is_working(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(WORKING)
        self.assertEqual(self.now()["state"], "working")

    def test_esc_to_interrupt_in_the_footer_alone_is_working_even_with_its_spaces_gone(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(FOOTER_ONLY)
        self.assertEqual(self.now()["state"], "working")

    def test_a_spinner_of_several_words_above_a_todo_list_is_working(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        todos = "".join(f"  ◻ step {n} of the plan\n" for n in range(10))
        self.show(f"✳ Running the tests… (2m 3s)\n{todos}\n{RULE}\n❯ \n{RULE}\n  Opus 5.5 · high\n  ⏵⏵ auto mode on\n")
        self.assertEqual(self.now()["state"], "working")

    def test_busy_words_far_up_in_scrollback_do_not_count(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(OLD_WORDS + IDLE)
        self.assertEqual(self.now()["state"], "idle")

    def test_typed_text_or_no_box_stays_starting(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        for screen in (TYPED, NO_BOX, ""):
            self.show(screen)
            now = self.now()
            self.assertEqual((now["state"], now["kind"], now["msg"]), ("starting", "unreported", "no program status from Rex"), screen)

    def test_never_idle_without_positive_evidence(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        for name, screen in (("permission prompt", PERMISSION), ("selection menu", MENU), ("background-work dialog", BACKGROUND),
                             ("a dialog under an older ❯ row", OLD_BOX), ("typed text", TYPED), ("no done row above the box", NO_DONE_ROW)):
            self.show(screen)
            self.assertEqual(self.now()["state"], "starting", name)

    def test_a_spinner_thirty_rows_above_the_box_with_no_footer_hint_is_working(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(LONG_TODO)
        self.assertEqual(self.now()["state"], "working")

    def test_the_footer_hint_counts_only_below_the_box(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(QUOTED_HINT)
        self.assertEqual(self.now()["state"], "idle")

    def test_a_styled_screen_with_a_faint_placeholder_and_a_coloured_spinner(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(STYLED_IDLE)
        self.assertEqual(self.now()["state"], "idle")
        self.show(STYLED_WORKING)
        self.assertEqual(self.now()["state"], "working")

    def test_a_screen_read_that_fails_or_is_empty_is_starting(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show("")
        self.assertEqual(self.now()["state"], "starting")
        real = caps.CTX.backend.screen

        def styled_fails(place, styled=False):
            if styled:
                raise BackendError("timeout", "rex did not answer")
            return real(place)
        self.show(IDLE)
        with mock.patch.object(caps.CTX.backend, "screen", side_effect=styled_fails):
            self.assertEqual(self.now()["state"], "starting")

    def test_no_record_within_the_grace_is_starting(self):
        self.started_ago(agents.STATUS_GRACE - 30)
        self.show(IDLE)
        now = self.now()
        self.assertEqual((now["state"], now["kind"]), ("starting", None))

    def test_an_unreadable_start_counts_as_within_the_grace(self):
        agents.pid_file(config.hq_name()).unlink()
        self.show(IDLE)
        self.assertEqual(self.now()["state"], "starting")

    def test_a_real_record_always_wins(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(IDLE, {"state": "working"})
        self.assertEqual(self.now()["state"], "working")
        self.show(WORKING, {"state": "idle"})
        self.assertEqual(self.now()["state"], "idle")
        self.show(IDLE, {"state": None, "kind": "x"})  # a record, if an empty one: not Rex hearing nothing
        self.assertEqual(self.now()["state"], "starting")

    def test_a_pi_gets_no_screen_fallback(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(IDLE)
        record = {**hq._record_for_agents(hq.load()), "agent": "pi"}
        self.assertEqual(agents.state(caps.CTX.backend, record)["state"], "starting")


class Skip(Fallback):
    def cycle(self):
        with mock.patch.object(hq, "TYPING_WAIT", 0.01), mock.patch.object(agents, "graceful_exit") as exits:
            with self.assertRaises(hq.UserError) as raised:
                hq._cycle(hq.load(), [], "p", "rotation")
        exits.assert_not_called()
        return str(raised.exception)

    def test_the_skip_names_the_state_and_that_rex_has_no_status(self):
        self.show(NO_BOX)
        self.started_ago(agents.STATUS_GRACE + 5)
        self.assertEqual(self.cycle(), "rotation skipped: HQ state=starting (no program status from Rex)")

    def test_the_skip_names_a_working_hq(self):
        self.show(IDLE, {"state": "working"})
        self.assertEqual(self.cycle(), "rotation skipped: HQ state=working")

    def test_the_skip_names_someone_typing(self):
        self.show(TYPED, {"state": "idle"})
        self.assertEqual(self.cycle(), "rotation skipped: someone is typing in HQ's box")

    def test_the_skip_names_a_backend_that_did_not_answer(self):
        with mock.patch.object(agents, "state", side_effect=BackendError("timeout", "rex did not answer")):
            self.assertIn("rotation skipped: Rex did not answer", self.cycle())

    def test_a_screen_read_idle_needs_a_second_idle_read(self):
        idle = {"state": "idle", "kind": agents.UNREPORTED, "msg": "no program status from Rex; read from its screen"}
        working = {**idle, "state": "working"}
        with mock.patch.object(hq, "SCREEN_CONFIRM", 0.0), mock.patch.object(agents, "typing", return_value=False):
            with mock.patch.object(agents, "state", side_effect=[idle, working] * 50):
                self.assertEqual(hq._wait_quiet(caps.CTX.backend, {}, 0.5),
                                 "HQ state=working (no program status from Rex; read from its screen)")
            with mock.patch.object(agents, "state", side_effect=[idle, idle]):
                self.assertIsNone(hq._wait_quiet(caps.CTX.backend, {}, 5))

    def test_a_failing_screen_read_never_raises_out_of_the_rotation(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        with mock.patch.object(caps.CTX.backend, "screen", side_effect=BackendError("timeout", "rex did not answer")):
            self.assertIn("rotation skipped: Rex did not answer", self.cycle())

    def test_a_quiet_hq_is_not_skipped(self):
        self.show(IDLE, {"state": "idle"})
        self.assertIsNone(hq._wait_quiet(caps.CTX.backend, hq._record_for_agents(hq.load()), 1))


class Doctor(Fallback):
    def hq_status(self):
        with mock.patch.object(hq, "context_tokens", return_value=None):
            return [c for c in hq._running_checks(caps.CTX, hq.load()) if c.name == "hq-status"]

    def test_doctor_flags_hq_whose_claude_reports_no_status(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(IDLE)
        [check] = self.hq_status()
        self.assertFalse(check.ok)
        self.assertIn("Claude reports no status to Rex (OSC 7501 probe likely failed)", check.detail)
        self.assertIn("athena hq stop && athena hq", check.detail)
        self.assertIsNone(check.repair, "doctor never restarts HQ itself")

    def test_doctor_is_quiet_about_hq_reporting_status_or_within_its_grace(self):
        self.started_ago(agents.STATUS_GRACE + 5)
        self.show(IDLE, {"state": "idle"})
        self.assertEqual(self.hq_status(), [])
        self.started_ago(10)
        self.show(IDLE)
        self.assertEqual(self.hq_status(), [])

    def test_doctor_flags_a_worker_whose_claude_reports_no_status(self):
        place = caps.CTX.backend.create("w", self.tmp, ["x"], block_label="w")
        spawn_record("w", self.tmp, self.tmp, block=False)
        ledger.append("update", "w", space=place.space, block=place.block)
        store.write_json(store.path("workers", "w.json"), {"state": "idle"})  # its hooks said hello
        agents.pid_file("w").write_text("4343\n")
        with mock.patch.object(agents, "running", return_value={"w": "S"}):
            self.started_ago(agents.STATUS_GRACE + 5, "w")
            self.show(IDLE, place=place)
            [check] = doctor._workers_hooks(caps.CTX)
            self.assertEqual(check.name, "status:w")
            self.assertFalse(check.ok)
            self.assertIn("Claude reports no status to Rex (OSC 7501 probe likely failed)", check.detail)
            self.show(IDLE, {"state": "idle"}, place=place)
            self.assertEqual(doctor._workers_hooks(caps.CTX), [])
            self.started_ago(10, "w")
            self.show(IDLE, place=place)
            self.assertEqual(doctor._workers_hooks(caps.CTX), [])
