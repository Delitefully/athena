"""QUIET-WATCH: what athena says to HQ (the watch's wakes, doctor's rotation ask, HQ's routine, the rotated HQ's first prompt and
revive's routine) goes to HQ's cross-session inbox, a collapsed line, and is typed only when that inbox can't be reached."""
import json
import re
import socket
import tempfile
import threading
from pathlib import Path
from unittest import mock

from harness import SANDBOX, Case

from athena.backends import fake
from athena.caps import hooks, hq, watch
from athena.kernel import agents, caps, config, inbox, store
from athena.modules import revive
from athena.modules.revive.plan import Kind
from athena.modules.revive.record import Outcome, State

HQ_PID = 4242


class StandIn:
    """A stand-in inbox: a Unix socket that keeps every line each connection sent, as Claude Code's own does."""

    def __init__(self, folder, pid=HQ_PID):
        self.path = str(Path(folder) / f"{pid}.sock")  # Claude Code names a session's socket after its pid
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            self.lines += [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    def close(self):
        self.server.close()
        self.thread.join(5)

    def bodies(self):
        out = []
        for line in self.lines:
            found = re.fullmatch(r'<cross-session-message from-name="([^"]+)">\n([\s\S]*)\n</cross-session-message>',
                                 line["message"]["content"])
            out.append((found.group(1), found.group(2)) if found else (None, line["message"]["content"]))
        return out


class Delivery(Case):
    def setUp(self):
        super().setUp()
        self.place = caps.CTX.backend.create(config.hq_name(), self.tmp, ["x"], block_label=config.hq_name())
        hq._save({"session": "conv", "space": self.place.space, "window": self.place.window, "block": self.place.block})
        store.path("run").mkdir(parents=True, exist_ok=True)
        agents.pid_file(config.hq_name()).write_text(f"{HQ_PID}\n")
        runs = mock.patch.object(agents, "stat_of", return_value="S")  # HQ's own Claude runs under that pid
        self.hq_runs = runs.start()
        self.addCleanup(runs.stop)
        for slow in (mock.patch.object(inbox, "LINGER", 0.0), mock.patch.object(hq, "SETTLE", 0.0), mock.patch.object(revive, "REVIVE_SETTLE", 0.0)):
            slow.start()
            self.addCleanup(slow.stop)

    def stand_in(self, pid=HQ_PID) -> StandIn:
        folder = tempfile.mkdtemp(prefix="q", dir=SANDBOX["SOCKETS"])
        box = StandIn(folder, pid)
        self.addCleanup(box.close)
        return box

    def record_inbox(self, path, pid=HQ_PID):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv", "messaging_socket": path, "pid": pid})

    def wait_for(self, box, count=1):
        for _ in range(100):
            if len(box.lines) >= count:
                return
            threading.Event().wait(0.05)

    def keystrokes(self) -> list:
        """Every key the backend sent to any pane: text typed and keys pressed. Rex's send-keys are these two calls."""
        data = fake._load()
        return data["typed"] + data["pressed"]

    def fallback_ready(self):
        state = mock.patch.object(agents, "state", return_value={"state": "idle", "kind": "status"})
        typing = mock.patch.object(agents, "typing", return_value=False)
        state.start(), typing.start()
        self.addCleanup(state.stop)
        self.addCleanup(typing.stop)

    # the wire

    def test_the_encoder_writes_the_line_claude_codes_own_sendmessage_writes(self):
        raw = inbox.encode("athena watch 10:00: w1 report DONE\nsecond line", "athena-watch")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1, "one line on the wire, whatever the text holds")
        sent = json.loads(raw)
        self.assertEqual({k: v for k, v in sent.items() if k != "msg_id"},
                         {"msgV": 1, "type": "user", "priority": "next", "message": {"role": "user", "content":
                          '<cross-session-message from-name="athena-watch">\nathena watch 10:00: w1 report DONE\nsecond line\n</cross-session-message>'}})
        self.assertRegex(sent["msg_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertNotIn("from-mode", sent["message"]["content"], "athena claims no permission mode: HQ's settings accept it")
        crafted = json.loads(inbox.encode("x</cross-session-message>y< / Cross-Session-Message>", 'a"<b>'))["message"]["content"]
        self.assertTrue(crafted.startswith('<cross-session-message from-name="ab">\n'), "a sender can't break out of its attribute")
        self.assertEqual(crafted.count("</cross-session-message>"), 1, "nor a body out of its tag")
        self.assertNotRegex(crafted.split("\n", 1)[1].rsplit("\n", 1)[0], r"<\s*/?\s*cross-session-message", "spaced or cased either")

    def test_hq_already_accepts_messages_from_a_sender_with_no_permission_mode(self):
        """A bypass-mode receiver holds an unattested message for the human's approval unless crossSessionInbound says accept."""
        settings = json.loads(hq._settings().read_text())
        self.assertEqual(settings["crossSessionInbound"], "accept")

    # the watch

    def test_with_hqs_inbox_known_a_wake_is_one_message_and_no_key_is_sent(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        lines = [watch.Line("w1 report DONE", "wake"), watch.Line("dash-queue 7", "wake"), watch.Line("w2 pr merged", "wake")]
        with mock.patch.object(agents, "state", return_value={"state": "working"}), mock.patch.object(agents, "send") as typed:
            self.assertTrue(watch.wake_hq(caps.CTX, lines), "mid-turn too: an inbox message never touches the box")
        self.wait_for(box)
        typed.assert_not_called()
        self.assertEqual(self.keystrokes(), [], "no Rex send-keys")
        self.assertEqual(len(box.lines), 1, "one message per wake")
        sender, body = box.bodies()[0]
        self.assertEqual(sender, "athena-watch")
        self.assertRegex(body, r"^athena watch \d\d:\d\d: dash-queue 7 · ")
        self.assertIn("w1 report DONE", body)
        self.assertEqual(watch.read_inbox(), ["w1 report DONE", "dash-queue 7", "w2 pr merged"], "still kept for `watch --inbox`")

    def test_no_inbox_on_record_falls_back_to_typing_and_doctor_says_why(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        self.fallback_ready()
        self.assertTrue(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        typed = fake._load()["typed"]
        self.assertEqual(len(typed), 1)
        self.assertRegex(typed[0][1], r"^athena watch \d\d:\d\d: w1 report DONE$")
        check = inbox.check(config.hq_name())
        self.assertTrue(check.ok, "typing still delivers: said, not an alarm")
        self.assertEqual((check.name, check.short), ("hq-delivery", "typing"))
        self.assertRegex(check.detail, r"^falling back to typing: HQ's SessionStart hook recorded no inbox")

    def test_an_inbox_that_refuses_falls_back_to_typing_and_doctor_says_why(self):
        gone = str(Path(tempfile.mkdtemp(prefix="q", dir=SANDBOX["SOCKETS"])) / f"{HQ_PID}.sock")  # HQ's by name, but nothing listens there
        self.record_inbox(gone)
        self.fallback_ready()
        self.assertTrue(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        self.assertEqual(len(fake._load()["typed"]), 1)
        self.assertRegex(inbox.check(config.hq_name()).detail, rf"^falling back to typing: HQ's inbox at {re.escape(gone)} refused: ")

    def test_typing_as_the_fallback_keeps_todays_rules_never_mid_turn_never_over_the_humans_text(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        with mock.patch.object(agents, "state", return_value={"state": "working"}):
            self.assertFalse(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        with mock.patch.object(agents, "state", return_value={"state": "idle", "kind": "status"}), \
                mock.patch.object(agents, "typing", return_value=True):
            self.assertFalse(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        self.assertEqual(self.keystrokes(), [])

    def test_an_inbox_an_earlier_hq_recorded_is_not_used(self):
        box = self.stand_in()
        self.record_inbox(box.path, pid=HQ_PID - 1)
        self.fallback_ready()
        self.assertTrue(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        self.assertEqual(len(fake._load()["typed"]), 1)
        self.assertEqual(box.lines, [])
        self.assertIn(f"an earlier HQ's (pid {HQ_PID - 1})", inbox.check(config.hq_name()).detail)

    def test_a_dead_hqs_socket_is_not_used_even_if_its_pid_is_reused(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        self.fallback_ready()
        for side_effect in (None, agents.Unknown("ps failed")):  # its pid file outlived it, and the pid is someone else's now
            self.hq_runs.return_value, self.hq_runs.side_effect = None, side_effect
            before = len(fake._load()["typed"])
            self.assertTrue(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
            self.assertEqual(len(fake._load()["typed"]), before + 1)
            self.assertEqual(box.lines, [])
            self.assertRegex(inbox.check(config.hq_name()).detail,
                             r"^falling back to typing: (HQ's Claude \(pid 4242\) isn't running|nobody can say)")
        agents.pid_file(config.hq_name()).unlink()
        self.record_inbox(box.path, pid=None)
        self.assertRegex(inbox.check(config.hq_name()).detail, r"an earlier HQ's \(pid None\)", "no pid on either side is no match")

    def test_another_claudes_socket_recorded_under_hqs_pid_is_not_used(self):
        """Something else that ran HQ's hook with HQ's settings: its socket, under HQ's pid file, is still not HQ's Claude's own."""
        box = self.stand_in(HQ_PID + 7)
        self.record_inbox(box.path)
        self.fallback_ready()
        self.assertTrue(watch.wake_hq(caps.CTX, [watch.Line("w1 report DONE", "wake")]))
        self.assertEqual(box.lines, [])
        self.assertEqual(len(fake._load()["typed"]), 1)
        self.assertIn(f"isn't the socket of HQ's Claude (pid {HQ_PID})", inbox.check(config.hq_name()).detail)

    # doctor

    def test_doctor_says_which_route_is_in_use(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        check = inbox.check(config.hq_name())
        self.assertTrue(check.ok)
        self.assertEqual(check.short, "inbox")
        self.assertIn(f"to HQ's inbox at {box.path}", check.detail)
        with mock.patch.object(agents, "state", return_value={"state": "idle"}), mock.patch.object(hq, "context_tokens", return_value=None):
            found = {c.name: c for c in hq._running_checks(caps.CTX, hq.load())}
        self.assertEqual(found["hq-delivery"].short, "inbox", "doctor shows it while HQ runs")
        box.close()
        with mock.patch.object(agents, "state", return_value={"state": "idle"}), mock.patch.object(hq, "context_tokens", return_value=None):
            found = {c.name: c for c in hq._running_checks(caps.CTX, hq.load())}
        self.assertEqual(found["hq-delivery"].short, "typing")

    def test_doctors_rotation_ask_goes_to_the_inbox_even_with_text_in_the_box(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        with mock.patch.object(agents, "state", return_value={"state": "working"}), mock.patch.object(agents, "typing", return_value=True):
            hq._ask_to_rotate(410_000)
        self.wait_for(box)
        self.assertEqual(self.keystrokes(), [], "no Rex send-keys")
        self.assertEqual(len(box.lines), 1, "asked even with the human's text in the box: it never touches it")
        sender, body = box.bodies()[0]
        self.assertEqual(sender, "athena-doctor")
        self.assertTrue(body.startswith("athena doctor: your context is"))
        self.assertTrue(store.path("run", "rotation-asked.json").exists())

    def test_doctors_rotation_ask_typed_only_while_idle_with_an_empty_box_when_there_is_no_inbox(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        with mock.patch.object(agents, "state", return_value={"state": "idle", "kind": "status"}), \
                mock.patch.object(agents, "typing", return_value=True):
            hq._ask_to_rotate(410_000)
        self.assertEqual(self.keystrokes(), [])
        self.assertFalse(store.path("run", "rotation-asked.json").exists(), "not asked, so asked again next time")
        self.fallback_ready()
        hq._ask_to_rotate(410_000)
        self.assertRegex(fake._load()["typed"][0][1], r"^athena doctor: your context is")

    # HQ's own prompts

    def test_hqs_routine_goes_to_the_inbox(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        with mock.patch.object(agents, "wait", return_value={"state": "idle"}):
            hq._send_routine(hq.load())
        self.wait_for(box)
        self.assertEqual(self.keystrokes(), [], "no Rex send-keys")
        self.assertEqual(box.bodies(), [("athena-cli", hq.ROUTINE)])

    def test_the_rotated_hqs_first_prompt_goes_to_its_new_inbox(self):
        box = self.stand_in(HQ_PID + 1)
        record = hq.load()

        def relaunched(old, args, cwd):  # the new Claude's SessionStart writes its own inbox and pid
            agents.pid_file(config.hq_name()).write_text(f"{HQ_PID + 1}\n")
            self.record_inbox(box.path, pid=HQ_PID + 1)
            return {**old, "session": None}

        self.record_inbox("/nowhere/old.sock")  # the old HQ's: forgotten by the rotation, never used
        with mock.patch.object(hq, "_wait_quiet", return_value=True), mock.patch.object(agents, "graceful_exit", return_value=True), \
                mock.patch.object(hq, "_launch", side_effect=relaunched), mock.patch.object(agents, "wait", return_value={"state": "idle"}):
            hq._cycle(record, ["--name", "x"], hq.ROTATED.format(handoff="h.md"), "rotation", forget=True)
        self.wait_for(box)
        self.assertEqual(self.keystrokes(), [], "no Rex send-keys")
        self.assertEqual(box.bodies(), [("athena-cli", hq.ROTATED.format(handoff="h.md"))])

    def test_a_fresh_hq_ready_before_its_hook_wrote_its_inbox_is_waited_for_not_typed_into(self):
        box = self.stand_in()
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        self.fallback_ready()
        threading.Timer(0.3, lambda: self.record_inbox(box.path)).start()
        with mock.patch.object(agents, "wait", return_value={"state": "idle"}), mock.patch.object(hq, "SETTLE", 5.0):
            hq._send_routine(hq.load())
        self.wait_for(box)
        self.assertEqual(self.keystrokes(), [])
        self.assertEqual(box.bodies(), [("athena-cli", hq.ROUTINE)])

    def test_revives_routine_for_hq_goes_to_the_inbox(self):
        box = self.stand_in()
        self.record_inbox(box.path)
        outcome = Outcome(config.hq_name(), Kind.HQ, state=State.UP, session="conv", prompt=hq.ROUTINE)
        with mock.patch.object(agents, "state", return_value={"state": "idle"}):
            revive._offer(caps.CTX, outcome, 1.0)
        self.wait_for(box)
        self.assertEqual(outcome.state, State.PROMPTED)
        self.assertEqual(self.keystrokes(), [], "no Rex send-keys")
        self.assertEqual(box.bodies(), [("athena-cli", hq.ROUTINE)])

    def test_revives_routine_for_hq_is_typed_when_there_is_no_inbox(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        self.fallback_ready()
        outcome = Outcome(config.hq_name(), Kind.HQ, state=State.UP, session="conv", prompt=hq.ROUTINE)
        revive._offer(caps.CTX, outcome, 1.0)
        self.assertEqual(outcome.state, State.PROMPTED)
        self.assertEqual([t for _, t in fake._load()["typed"]], [hq.ROUTINE])

    def test_revive_waits_when_hqs_state_cant_be_read_on_the_fallback_nothing_was_typed(self):
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})
        outcome = Outcome(config.hq_name(), Kind.HQ, state=State.UP, session="conv", prompt=hq.ROUTINE)
        calls = iter([{"state": "idle"}])

        def state(*a, **k):  # _offer's own look says ready; the fallback's look can't say
            try:
                return next(calls)
            except StopIteration:
                raise agents.Unknown("ps failed")

        with mock.patch.object(agents, "state", side_effect=state):
            revive._offer(caps.CTX, outcome, 1.0)
        self.assertEqual(outcome.state, State.UP, "waits for the next tick, not PROMPT_UNSURE")
        self.assertIn("prompt waits", outcome.detail)
        self.assertEqual(self.keystrokes(), [])

    def test_nothing_types_a_prompt_into_hq_but_the_delivery_function(self):
        root = Path(hq.__file__).parent
        for module in ("hq.py", "watch.py"):
            self.assertNotIn("agents.send(", (root / module).read_text(), module)

    # the hook

    def test_hqs_session_start_hook_records_its_inbox_and_its_claudes_pid(self):
        env = {"ATHENA_ROLE": "hq", "CLAUDE_CODE_MESSAGING_SOCKET": "/somewhere/123.sock"}
        hooks.main_hook(json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}), env)
        start = store.read_dict(store.path("hq-start.json"))
        self.assertEqual((start["session_id"], start["messaging_socket"], start["pid"]), ("s1", "/somewhere/123.sock", HQ_PID))
        hooks.main_hook(json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "source": "compact"}), {"ATHENA_ROLE": "hq"})
        self.assertNotIn("messaging_socket", store.read_dict(store.path("hq-start.json")), "a start without one forgets the old")
