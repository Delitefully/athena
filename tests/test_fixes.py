"""HQ rotates by tokens, not by a percent of a window it can't know; HQ never compacts while idle."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from harness import Case

from athena_rex.caps import hq, retire, sessions, spawn, status
from athena_rex.kernel import agents, caps, ledger, store
from athena_rex.kernel.backend import Place


class Rotation(Case):
    def _context(self, tokens):
        folder = Path(self.home, ".claude", "projects", "x")
        folder.mkdir(parents=True, exist_ok=True)
        usage = {"input_tokens": 1000, "cache_read_input_tokens": tokens - 1000, "cache_creation_input_tokens": 0}
        (folder / "conv.jsonl").write_text(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
        store.write_json(store.path("hq.json"), {"space": "s", "block": "b", "session": "conv"})
        store.write_json(store.path("hq-start.json"), {"session_id": "conv"})

    def _check(self):
        return next(c for c in hq._running_checks(caps.CTX, hq.load()) if c.name == "hq-context")

    def _asked(self, tokens) -> bool:
        self._context(tokens)
        store.path("run", "rotation-asked.json").unlink(missing_ok=True)
        with mock.patch.object(agents, "state", return_value={"state": "idle"}):
            check = self._check()
        return not check.ok and check.repair is not None

    def test_rotation_is_asked_from_hq_rotate_tokens(self):
        self.assertFalse(self._asked(390_000))
        self.assertTrue(self._asked(410_000))
        self.assertTrue(self._asked(400_000), "at the line itself")

    def test_a_configured_line_is_the_one_asked_at(self):
        self.write_config(hq_rotate_tokens=250000)
        self.assertTrue(self._asked(260_000))
        self.assertFalse(self._asked(240_000))

    def test_a_line_that_is_not_a_positive_number_is_the_default(self):
        for bad in (0, -5, "lots", True):
            self.write_config(hq_rotate_tokens=bad)
            self.assertEqual(hq.rotate_tokens(), 400000, bad)

    def test_doctor_and_status_show_tokens_against_the_line(self):
        self._context(171_000)
        with mock.patch.object(agents, "state", return_value={"state": "idle"}):
            self.assertEqual(self._check().detail, "context 171k of 400k (rotates at hq_rotate_tokens)")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._run(argparse.Namespace(workers=[], json=False, live=False, pr=False, time=False))
        self.assertIn("hq context 171k of 400k (rotates at hq_rotate_tokens)", out.getvalue())

    def test_the_ask_names_the_line(self):
        self._context(410_000)
        with mock.patch.object(agents, "state", return_value={"state": "idle"}), mock.patch.object(agents, "typing", return_value=False), \
                mock.patch.object(agents, "send") as typed:
            self._check().repair()
        self.assertIn("410k of 400k", typed.call_args.args[2])

    def test_a_bare_if_context_over_uses_the_line_and_a_percent_the_window(self):
        self._context(390_000)
        with mock.patch.object(hq, "_cycle") as cycled:
            self.assertEqual(hq.rotate(if_context_over=hq.LINE, unattended=True)["outcome"], "skipped")
            self.assertEqual(hq.rotate(if_context_over=45, unattended=True)["outcome"], "skipped", "39% of the 1M window")
            cycled.assert_not_called()
            cycled.return_value = {}
            self.assertEqual(hq.rotate(if_context_over=35, unattended=True)["outcome"], "rotated")
            self._context(410_000)
            self.assertEqual(hq.rotate(if_context_over=hq.LINE, unattended=True)["outcome"], "rotated")

    def test_the_cli_takes_if_context_over_with_or_without_a_percent(self):
        parser = argparse.ArgumentParser()
        hq._configure(parser)
        self.assertIs(parser.parse_args(["rotate", "--if-context-over"]).if_context_over, hq.LINE)
        self.assertEqual(parser.parse_args(["rotate", "--if-context-over", "70"]).if_context_over, 70)
        self.assertIsNone(parser.parse_args(["rotate"]).if_context_over)

    def test_the_window_is_never_taken_to_be_200k(self):
        folder = Path(self.home, ".claude", "projects", "x")
        folder.mkdir(parents=True)
        (folder / "c.jsonl").write_text(json.dumps({"message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 150000}}}) + "\n")
        self.assertEqual(hq.context_percent("c"), 15, "rex.hq_context_window, 1M unless set")
        self.write_config(rex={"hq_context_window": 500000})
        self.assertEqual(hq.context_percent("c"), 30)


class HqSettings(Case):
    def test_hq_never_compacts_while_idle_and_a_worker_is_left_alone(self):
        args = hq.claude_args()
        settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
        self.assertIs(settings["idleCompaction"], False)
        self.assertNotIn("autoCompactEnabled", settings)
        worker = json.loads(spawn.settings_for("w", self.tmp / "brief.md").read_text())
        self.assertNotIn("idleCompaction", worker)
        self.assertNotIn("autoCompactEnabled", worker)

    def test_hq_passes_no_model_unless_hq_model_says(self):
        self.assertNotIn("--model", hq.claude_args())
        self.write_config(hq_model="")
        self.assertNotIn("--model", hq.claude_args())
        self.write_config(hq_model="opus[1m]")
        args = hq.claude_args()
        self.assertEqual(args[args.index("--model") + 1], "opus[1m]")
        self.assertEqual(hq.claude_args("sonnet")[hq.claude_args("sonnet").index("--model") + 1], "sonnet", "--model given wins")
        self.assertTrue(os.path.exists(args[args.index("--settings") + 1]))


class RotationDocs(Case):
    def test_the_skill_and_the_architecture_say_what_the_code_does(self):
        root = Path(__file__).resolve().parent.parent
        example = "(rotates at hq_rotate_tokens)"
        for doc in ("plugin/skills/athena-rex/SKILL.md", "ARCHITECTURE.md"):
            text = (root / doc).read_text()
            self.assertIn("hq_rotate_tokens", text, doc)
            self.assertIn("400k", text, doc)
            self.assertIn(example, text, doc)
            self.assertIn("--if-context-over", text, doc)
            self.assertNotIn("from 60%", text, doc)
        self.assertTrue(hq.against_line(171_000).endswith(example))
        self.assertIn("idleCompaction", (root / "ARCHITECTURE.md").read_text())


class RepoSessions(Case):
    """Rex's sidebar groups workers by repo: one session per repo, a window per worker."""

    def setUp(self):
        super().setUp()
        self.write_config(worktree_root=str(self.tmp / "wt"))

    def _fake(self):
        return json.loads(Path(os.environ["ATHENA_FAKE_BACKEND"]).read_text())

    def _spawn(self, repo, name):
        spawn.spawn(caps.CTX, repo, f"t/{name}", name, prompt="hi", timeout=0.1, fetch=False)
        return ledger.get(name)

    def _windows(self, space):
        return {w["label"] for w in caps.CTX.backend.windows(space)}

    def test_workers_of_one_repo_share_its_session_each_in_its_own_window(self):
        repo = self.repo("proj")
        a = self._spawn(repo, "a")
        b = self._spawn(repo, "b")
        self.assertEqual(a["space"], b["space"], "spawn found the repo's session")
        self.assertNotEqual(a["window"], b["window"])
        spaces = caps.CTX.backend.spaces()
        self.assertEqual([s["label"] for s in spaces], [repo.name], "the repo folder's name")
        self.assertEqual(self._windows(a["space"]), {"a", "b"})
        self.assertEqual(caps.CTX.backend.block_label(agents.place_of(b)), "b")
        other = self._spawn(self.repo("other"), "c")
        self.assertNotEqual(other["space"], a["space"])
        self.assertEqual(len(caps.CTX.backend.spaces()), 2)

    def test_a_repo_session_closed_by_hand_is_made_again(self):
        repo = self.repo("proj")
        a = self._spawn(repo, "a")
        caps.CTX.backend.close_space(a["space"])
        b = self._spawn(repo, "b")
        self.assertNotEqual(b["space"], a["space"])
        self.assertEqual(self._windows(b["space"]), {"b"})

    def test_retire_closes_its_window_and_the_last_one_closes_the_session(self):
        repo = self.repo("proj")
        a, b = self._spawn(repo, "a"), self._spawn(repo, "b")
        retire.retire(caps.CTX, "a", force=True)
        self.assertEqual(self._windows(a["space"]), {"b"}, "b's window stays")
        retire.retire(caps.CTX, "b", force=True)
        self.assertEqual(caps.CTX.backend.spaces(), [], "the empty session goes")
        c = self._spawn(repo, "c")
        self.assertEqual(self._windows(c["space"]), {"c"})

    def test_status_finds_a_worker_by_its_window(self):
        repo = self.repo("proj")
        a, b = self._spawn(repo, "a"), self._spawn(repo, "b")
        sessions.refresh(caps.CTX)
        rows = {r["name"]: r for r in status.collect()}
        self.assertIs(rows["a"]["session"]["seen"], True)
        self.assertIs(rows["b"]["session"]["seen"], True)
        caps.CTX.backend.close_window(agents.place_of(a))
        sessions.refresh(caps.CTX)
        rows = {r["name"]: r for r in status.collect()}
        self.assertIs(rows["a"]["session"]["seen"], False, "its window is gone, though its repo's session is not")
        self.assertIs(rows["b"]["session"]["seen"], True)
        row = next(r for r in sessions.refresh(caps.CTX)["sessions"] if r["id"] == a["space"])
        self.assertEqual((row["kind"], row["label"]), ("worker", repo.name))
        self.assertEqual(sorted(row["owners"]), ["a", "b"])

    def test_status_live_finds_a_worker_by_its_window_too(self):
        repo = self.repo("proj")
        a, b = self._spawn(repo, "a"), self._spawn(repo, "b")
        caps.CTX.backend.close_window(agents.place_of(a))
        rows = status.collect()
        status.live_details(caps.CTX, rows)
        rows = {r["name"]: r for r in rows}
        self.assertEqual(rows["a"]["window"], a["window"])
        self.assertIs(rows["a"]["session"]["seen"], False)
        self.assertIs(rows["b"]["session"]["seen"], True)

    def test_screen_and_send_reach_a_worker_in_a_shared_session(self):
        repo = self.repo("proj")
        self._spawn(repo, "a")
        b = self._spawn(repo, "b")
        caps.CTX.backend.press(agents.place_of(b), "enter")
        self.assertEqual(self._fake()["pressed"][-1], [b["block"], "enter"])

    def test_a_record_with_no_window_never_closes_a_repo_session(self):
        repo = self.repo("proj")
        a, b = self._spawn(repo, "a"), self._spawn(repo, "b")
        ledger.append("update", "a", window=None)
        retire.retire(caps.CTX, "a", force=True)
        self.assertEqual(self._windows(b["space"]), {"b"}, "b's window and the session stay")
        self.assertFalse(caps.CTX.backend.has_block(agents.place_of(a)), "a's own block went")

    def test_close_orphans_holds_the_repo_sessions_lock(self):
        repo = self.repo("proj")
        a = self._spawn(repo, "a")
        ledger.append("retire", "a")
        with mock.patch.object(store, "locked", wraps=store.locked) as taken, mock.patch.object(agents, "stat_of", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            sessions._run(argparse.Namespace(action="close-orphans", worker=None, json=False))
        self.assertIn("locks/repo-sessions.lock", [c.args[0] for c in taken.call_args_list])
        self.assertEqual(caps.CTX.backend.spaces(), [])
        self.assertNotIn(a["space"], store.read_dict(store.path("run", "repo-sessions.json")).values(), "and it is forgotten")

    def test_a_worker_in_its_own_session_from_before_keeps_working(self):
        place = caps.CTX.backend.create("old-label", self.tmp, ["x"], block_label="old")
        ledger.append("spawn", "old", path=str(self.tmp / "gone"), backend="fake", space=place.space, block=place.block)
        sessions.refresh(caps.CTX)
        self.assertIs({r["name"]: r for r in status.collect()}["old"]["session"]["seen"], True, "found by its session")
        retire.retire(caps.CTX, "old", force=True)
        self.assertEqual(caps.CTX.backend.spaces(), [], "its own session goes with it, as before")


class HqLabels(Case):
    def test_hq_is_athena_in_rexs_sidebar(self):
        hq.open_hq(focus=False, routine=False)
        record = hq.load()
        backend = caps.CTX.backend
        self.assertEqual([s["label"] for s in backend.spaces()], ["athena"])
        self.assertEqual([w["label"] for w in backend.windows(record["space"])], ["athena"])
        self.assertEqual(backend.block_label(Place(record["space"], record["block"])), "athena")

    def test_a_running_hq_is_labelled_athena_when_opened(self):
        place = caps.CTX.backend.create("athena-hq", self.tmp, ["x"], block_label="claude", window_label="claude")
        hq._save({"space": place.space, "block": place.block})
        with mock.patch.object(hq, "alive", return_value=True):
            self.assertEqual(hq.open_hq(focus=False, routine=False)["outcome"], "running")
        self.assertEqual([s["label"] for s in caps.CTX.backend.spaces()], ["athena"])
        self.assertEqual([w["label"] for w in caps.CTX.backend.windows(place.space)], ["athena"])
        self.assertEqual(caps.CTX.backend.block_label(place), "athena")

    def test_doctors_ask_starts_as_the_skill_says(self):
        self.assertTrue(hq.ROTATE_ASK.startswith("athena-rex doctor: your context is"))

    def test_an_hq_session_labelled_before_is_labelled_athena_when_opened(self):
        place = caps.CTX.backend.create("athena-hq", self.tmp, ["x"], block_label="claude", window_label="claude")
        hq._save({"space": place.space, "block": place.block})
        hq.open_hq(focus=False, routine=False)
        record = hq.load()
        self.assertEqual(record["space"], place.space, "the same session")
        self.assertEqual([s["label"] for s in caps.CTX.backend.spaces()], ["athena"])
        self.assertEqual([w["label"] for w in caps.CTX.backend.windows(record["space"])], ["athena"])
        self.assertEqual(caps.CTX.backend.block_label(Place(record["space"], record["block"])), "athena")


class TestDashes(Case):
    """A test's tmp dir, and with it a dash's pid file, goes in the test's own cleanup: the run's end-of-run sweep then found
    nothing, and doctor --fix's dashboard repair, or `hq`'s, left a dash serving after every run. Only what /healthz confirms is
    signalled, as dash.off() does: a pid file alone may name a reused pid."""

    def _sleeper(self):
        import subprocess
        proc = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)  # leads its own session, as a dash and others do
        self.addCleanup(proc.kill)
        return proc

    def _healthz(self, answer):
        """A stand-in dash's /healthz on a loopback port: {"pid", "state_dir"} as the real one says, or nothing at all."""
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _pid_file(self, folder, **fields):
        (self.tmp / folder / "run").mkdir(parents=True, exist_ok=True)
        (self.tmp / folder / "run" / "dash.json").write_text(json.dumps(fields))

    def _gone(self, proc) -> bool:
        import time
        for _ in range(40):
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    def test_only_a_dash_healthz_vouches_for_is_stopped_before_its_tmp_goes(self):
        dash, reused, foreign, silent = self._sleeper(), self._sleeper(), self._sleeper(), self._sleeper()
        self._pid_file("a", pid=dash.pid, port=self._healthz({"pid": dash.pid, "state_dir": str(self.tmp / "a")}))
        self._pid_file("b", pid=reused.pid, port=self._healthz({"pid": 999999, "state_dir": str(self.tmp / "b")}))
        self._pid_file("c", pid=foreign.pid, port=self._healthz({"pid": foreign.pid, "state_dir": "/somewhere/else"}))
        self._pid_file("d", pid=silent.pid, port=self._free_port_nobody_answers())
        for junk in ({"pid": -1}, {"pid": 1, "port": 2}, {"pid": 10 ** 30, "port": 2}, {"pid": "x"}, {"port": "x", "pid": 5}):
            self._pid_file(f"junk{len(list(self.tmp.glob('junk*')))}", **junk)
        self.stop_own_dashes()
        self.assertTrue(self._gone(dash), "its /healthz names that pid and a state dir in this test's tmp")
        self.assertIsNone(reused.poll(), "a dash answers there but at another pid: the file's pid was reused")
        self.assertIsNone(foreign.poll(), "it serves a state dir outside this test's tmp")
        self.assertIsNone(silent.poll(), "nothing answers: nothing vouches for the pid, so it is left")

    def _free_port_nobody_answers(self) -> int:
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
