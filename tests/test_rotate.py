import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from tests.test_hq import GONE, HQ_SPACE, ON_EXIT, alive
from athena_lib import asks, cli, handoff, hq, ledger, paths

HEADINGS = ["## Fleet", "## PRs", "## Open items", "## In-flight threads", "## Notes tail", "## Standing orders"]


def row(name, state="working", pr=None):
    return {"name": name, "state": state, "branch": f"you/{name}-thing", "linear": name.upper(), "summary": "on step 2",
            "report": None, "git": {"ahead": 3, "dirty": []}, "pr": pr, "pending": False}


PR = {"number": 41, "url": "https://github.com/o/r/pull/41", "title": "Thing", "state": "OPEN", "draft": True,
      "review": "", "checks": "pending", "head": "abc123"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.claude_dir = Path(self.tmp.name) / "claude"
        self.env.set("CLAUDE_CONFIG_DIR", str(self.claude_dir))
        self.state = paths.ensure()
        (self.state / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8", "session": "old"}))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def seed(self, notes=20):
        ledger.append("spawn", "abc-1", repo="/r", branch="you/abc-1-thing", path="/w/abc-1")
        ledger.append("spawn", "abc-2", repo="/r", branch="you/abc-2-other", path="/w/abc-2")
        asks.add("Merge acme/web#906 before the 2.4 cut? (rec yes)", why="the cut is Friday")
        (self.state / "notes.md").write_text("# Notes\n\n" + "".join(f"- note {i}\n  more on {i}\n" for i in range(notes)))
        (self.state / "standing-orders.md").write_text("- autonomous hotfixes\n")

    def collect(self, worker, **_):
        return row(worker["name"], pr=PR if worker["name"] == "abc-1" else None)


class HandoffTest(Fixture):
    def test_has_every_section_from_state(self):
        self.seed()
        with mock.patch.object(handoff.status, "collect", side_effect=self.collect) as collect:
            text = handoff.build(threads="- waiting on Ana about the export flag\n", previous_session="old")
        for heading in HEADINGS:
            self.assertIn(heading, text)
        for worker, kwargs in [(c.args[0]["name"], c.kwargs) for c in collect.call_args_list]:
            self.assertTrue(kwargs["with_pr"])
            self.assertFalse(kwargs["with_herdr"])  # a dry run must make no herdr call
        fleet = text.split("## Fleet")[1].split("## ")[0]
        self.assertIn("abc-1", fleet)
        self.assertIn("abc-2", fleet)
        prs = text.split("## PRs")[1].split("## ")[0]
        self.assertIn("https://github.com/o/r/pull/41", prs)
        self.assertIn("a1", text.split("## Open items")[1])
        self.assertIn("Merge acme/web#906", text)
        self.assertIn("waiting on Ana", text)
        notes = text.split("## Notes tail")[1].split("## ")[0]
        self.assertIn("note 19", notes)
        self.assertIn("more on 19", notes)
        self.assertNotIn("note 0\n", notes)
        self.assertIn(str(self.state / "standing-orders.md"), text)
        self.assertIn("claude --resume old", text)
        self.assertLessEqual(len(text.splitlines()), handoff.MAX_LINES)
        start = text.split("## Start here")[1]
        self.assertNotIn("re-arm", start)  # never read as "arm Monitor"
        self.assertIn("athena-watch mod", start)

    def test_dry_run_does_not_import_a_legacy_needs_you_file(self):
        (self.state / "needs-you.md").write_text("- Ship the export flag today?\n")
        text = handoff.build(dry_run=True)
        self.assertIn("Ship the export flag today?", text)
        self.assertTrue((self.state / "needs-you.md").exists())
        self.assertFalse((self.state / "needs-you.json").exists())

    def test_stays_under_the_line_cap(self):
        self.seed(notes=400)
        threads = "".join(f"- thread {i}\n" for i in range(300))
        with mock.patch.object(handoff.status, "collect", side_effect=self.collect):
            text = handoff.build(threads=threads)
        self.assertLessEqual(len(text.splitlines()), handoff.MAX_LINES)
        for heading in HEADINGS:
            self.assertIn(heading, text)
        self.assertIn("thread 0", text)
        self.assertIn("cut", text.split("## In-flight threads")[1].split("## ")[0])
        self.assertIn("note 399", text)

    def test_caps_each_line(self):
        text = handoff.build(threads="- " + "x" * 1000 + "\n")
        self.assertLessEqual(max(len(line) for line in text.splitlines()), handoff.LINE_MAX)
        self.assertIn("x" * 100, text)

    def test_threads_come_from_the_threads_file_without_flag(self):
        (self.state / "threads.md").write_text("- review round 2 on abc-1\n")
        text = handoff.build()
        self.assertIn("review round 2 on abc-1", text)

    def test_empty_state(self):
        text = handoff.build()
        for heading in HEADINGS:
            self.assertIn(heading, text)
        self.assertIn("no live workers", text)
        self.assertIn("none written", text)

    def test_notes_tail_keeps_whole_entries(self):
        text = "# Notes\n\n## platform\n- a\n- b\n  b2\n- c\n"
        self.assertEqual(handoff.notes_tail(text, 2), ["- b", "  b2", "- c"])


def transcript(lines):
    return "".join(json.dumps(line) + "\n" for line in lines)


def assistant(tokens, model="claude-opus-5-5", sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "requestedModel": model,
            "message": {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": tokens - 10, "output_tokens": 50}}}


class ContextTest(Fixture):
    def write(self, session, lines, project="-Users-x-Developer"):
        path = self.claude_dir / "projects" / project / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript(lines))
        return path

    def test_reads_the_last_main_thread_usage(self):
        self.write("old", [assistant(50_000), {"type": "user"}, assistant(120_000),
                           assistant(190_000, sidechain=True), {"type": "user", "message": {}}])
        ctx = handoff.context("old")
        self.assertEqual(ctx["tokens"], 120_000)
        self.assertEqual(ctx["window"], 200_000)
        self.assertEqual(ctx["percent"], 60)

    def test_skips_synthetic_entries_with_no_usage(self):
        synthetic = {"type": "assistant", "isSidechain": False, "message": {"model": "<synthetic>", "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}}}
        self.write("old", [assistant(120_000), synthetic])
        self.assertEqual(handoff.context("old")["tokens"], 120_000)

    def test_a_1m_model_has_a_1m_window(self):
        self.write("old", [assistant(300_000, model="claude-opus-5-5[1m]")])
        self.assertEqual(handoff.context("old")["percent"], 30)

    def test_a_reading_over_200k_means_a_1m_window(self):
        self.write("old", [assistant(300_000)])
        self.assertEqual(handoff.context("old")["window"], 1_000_000)

    def test_an_entry_without_requested_model_falls_back_to_the_200k_rule(self):
        def bare(tokens):
            entry = assistant(tokens)
            del entry["requestedModel"]
            entry["message"]["model"] = "claude-opus-5-5"
            return entry
        self.write("big", [bare(300_000)])
        self.write("small", [bare(100_000)])
        self.assertEqual(handoff.context("big")["window"], 1_000_000)
        self.assertEqual(handoff.context("small")["window"], 200_000)

    def test_reads_from_the_end_of_a_long_transcript(self):
        self.write("old", [assistant(10_000)] + [{"type": "user", "pad": "x" * 2000}] * 3000 + [assistant(20_000)])
        self.assertEqual(handoff.context("old")["tokens"], 20_000)

    def test_unknown_session_or_no_usage(self):
        self.assertIsNone(handoff.context("nope"))
        self.assertIsNone(handoff.context(None))
        self.write("empty", [{"type": "user"}])
        self.assertIsNone(handoff.context("empty"))

    def test_hq_context_uses_the_recorded_session(self):
        self.write("old", [assistant(160_000)])
        self.assertEqual(handoff.hq_context()["percent"], 80)

    def test_status_hints_at_rotation_when_context_is_high(self):
        self.write("old", [assistant(160_000)])
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr"):
            cli.main(["status"])
        self.assertIn("athena hq rotate", out.getvalue())
        self.assertIn("80%", out.getvalue())

    def test_status_says_nothing_when_context_is_low(self):
        self.write("old", [assistant(20_000)])
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr"):
            cli.main(["status"])
        self.assertNotIn("rotate", out.getvalue())


class RotateTest(Fixture):
    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(hq.time, "sleep"), mock.patch.object(hq, "_say")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def calls(self):
        return logged_calls(self.tmp.name)

    def starts(self):
        return [c for c in self.calls() if c[:2] == ["agent", "start"]]

    def popen(self):
        """Patch Popen for the detached child. It is subprocess's own, so herdr's calls are stubbed too."""
        session = mock.patch.object(hq, "_session", return_value=None)
        session.start()
        self.addCleanup(session.stop)
        return mock.patch.object(hq.subprocess, "Popen")

    def test_dry_run_prints_handoff_and_launch_and_touches_nothing(self):
        self.seed()
        with mock.patch.object(handoff.status, "collect", side_effect=self.collect), \
                mock.patch.object(hq.subprocess, "Popen") as popen, mock.patch.object(hq.herdr, "call") as call:
            result = hq.rotate(threads="- t1\n", dry_run=True)
        call.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.calls(), [])  # no herdr call at all
        self.assertFalse(handoff.handoff_file().exists())
        self.assertIn("## Fleet", result["handoff"])
        argv = result["launch"]
        self.assertEqual(argv[:3], ["agent", "start", "athena"])
        self.assertEqual(argv[argv.index("--pane") + 1], "w8:p1")
        claude = argv[argv.index("--") + 1:]
        self.assertNotIn("--resume", claude)
        self.assertEqual(claude[claude.index("--name") + 1], "athena")
        self.assertIn(str(handoff.handoff_file()), result["prompt"])
        self.assertIn("herdr agent start athena", result["command"])

    def test_cli_dry_run(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr"), mock.patch("sys.stdin", io.StringIO("- from stdin\n")):
            code = cli.main(["hq", "rotate", "--dry-run", "--threads", "-"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("## In-flight threads", text)
        self.assertIn("from stdin", text)
        self.assertIn("herdr agent start athena", text)
        self.assertIn("athena skill", text)

    def test_cli_dry_run_under_the_context_threshold_says_why(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr"):
            code = cli.main(["hq", "rotate", "--dry-run", "--if-context-over", "90"])
        self.assertEqual(code, 0)
        self.assertIn("not rotating", out.getvalue())

    def test_writes_the_handoff_then_detaches(self):
        with self.popen() as popen:
            popen.return_value.pid = 4242
            result = hq.rotate(threads="- t1\n")
        self.assertIn("t1", handoff.handoff_file().read_text())
        # Saved before the child's idle wait: a busy HQ skips the rotation, and the next one finds them.
        self.assertIn("t1", handoff.threads_file().read_text())
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["hq", "rotate", "--child"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(result["pid"], 4242)
        self.assertIn(str(handoff.handoff_file()), result["message"])

    def test_child_exits_old_hq_and_starts_a_fresh_one(self):
        (self.state / "threads.md").write_text("- t1\n")
        write_scenario(self.tmp.name, [alive("sess-new", after=["agent", "start"]),
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE, ON_EXIT])
        result = hq.rotate_now()
        self.assertTrue(result["rotated"])
        calls = self.calls()
        exit_at = calls.index(["agent", "prompt", "athena", "/exit"])
        wait = next(c for c in calls if c[:2] == ["agent", "wait"])
        self.assertLess(calls.index(wait), exit_at)
        (start,) = self.starts()
        self.assertGreater(calls.index(start), exit_at)
        self.assertEqual(start[start.index("--pane") + 1], "w8:p1")
        claude = start[start.index("--") + 1:]
        self.assertNotIn("--resume", claude)
        self.assertEqual(claude[claude.index("--name") + 1], "athena")
        routine = [c for c in calls if c[:3] == ["agent", "prompt", "athena"] and c[3] != "/exit"]
        self.assertEqual(len(routine), 1)
        self.assertIn(str(handoff.handoff_file()), routine[0][3])
        self.assertIn("athena skill", routine[0][3])
        record = json.loads((self.state / "hq.json").read_text())
        self.assertEqual(record["session"], "sess-new")
        self.assertEqual(record["previous_session"], "old")
        self.assertFalse((self.state / "threads.md").exists())  # its threads are in the handoff now
        self.assertTrue((self.state / "threads.prev.md").exists())
        self.assertFalse(hq.lock_file().exists())

    def test_the_fresh_hq_registers_its_whole_resume_command(self):
        write_scenario(self.tmp.name, [alive("sess-new", after=["agent", "start"]),
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE, ON_EXIT])
        hq.rotate_now()
        (start,) = self.starts()
        (report,) = [c for c in self.calls() if c[:2] == ["pane", "report-agent-session"]]
        self.assertEqual(report[2], "w8:p1")
        self.assertEqual(report[report.index("--") + 1:], ["claude", "--resume", "sess-new", *start[start.index("--") + 1:]])
        self.assertEqual(json.loads((self.state / "hq.json").read_text())["claude_args"], start[start.index("--") + 1:])

    def test_a_fresh_hq_whose_session_herdr_does_not_report_yet_is_not_recorded_as_the_old_one(self):
        no_session = {"match": ["agent", "get", "athena"], "after": ["agent", "start"],
                      "stdout": {"result": {"agent": {"name": "athena"}}}}
        write_scenario(self.tmp.name, [no_session, {**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE,
                                       ON_EXIT])
        result = hq.rotate_now()
        self.assertTrue(result["rotated"])
        record = json.loads((self.state / "hq.json").read_text())
        self.assertIsNone(record["session"])  # so hq_context() never reads the old transcript again
        self.assertEqual(record["previous_session"], "old")
        self.assertIsNone(handoff.hq_context())

    def test_after_a_rotation_a_nested_resume_of_the_old_conversation_never_becomes_hq(self):
        write_scenario(self.tmp.name, [alive("sess-new", after=["agent", "start"]),
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE, ON_EXIT])
        hq.rotate_now()
        start = json.loads((self.state / "hq-start.json").read_text())
        self.assertEqual(start["session_id"], "sess-new")  # the rotation vouches for the fresh HQ
        reports = len([c for c in self.calls() if c[:2] == ["pane", "report-agent-session"]])
        # HQ later runs `claude -p --resume old` from its Bash: herdr reports `old` for HQ's pane.
        self.assertFalse(hq.sync_session("old"))
        with mock.patch.object(hq, "_session", return_value="old"):
            self.assertEqual(hq.trusted_session(), "sess-new")
        self.assertEqual(json.loads((self.state / "hq.json").read_text())["session"], "sess-new")
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["pane", "report-agent-session"]]), reports)

    def test_the_hook_owns_the_fresh_hqs_first_session_start(self):
        # Between the old HQ's exit and the fresh one's start, hq.json names no conversation and hq-start.json is gone.
        from athena_lib import hookstatus
        seen = {}

        def start_agent(pane, *extra):
            seen["record"] = hq.load()
            seen["start"] = (self.state / "hq-start.json").exists()
            seen["owns"] = hookstatus.hq_owns({"hook_event_name": "SessionStart", "session_id": "sess-new",
                                               "source": "startup"}, {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w8:p1"},
                                              seen["record"])
        (self.state / "hq-start.json").write_text(json.dumps({"source": "resume", "session_id": "old", "ts": 1}))
        write_scenario(self.tmp.name, [{**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE, ON_EXIT])
        with mock.patch.object(hq, "_start_agent", side_effect=start_agent):
            hq.rotate_now()
        self.assertIsNone(seen["record"]["session"])
        self.assertEqual(seen["record"]["previous_session"], "old")
        self.assertFalse(seen["start"])
        self.assertTrue(seen["owns"])

    def test_a_fresh_hq_that_never_starts_leaves_the_old_conversation_as_hqs(self):
        err = json.dumps({"error": {"code": "agent_not_ready", "message": "trust dialog"}})
        write_scenario(self.tmp.name, [{"match": ["agent", "start"], "stdout": "", "stderr": err, "exit": 1},
                                       {**GONE, "after": ["agent", "send-keys"]}, alive("old"), HQ_SPACE, ON_EXIT])
        with self.assertRaises(hq.RestartError):
            hq.rotate_now()
        self.assertEqual(json.loads((self.state / "hq.json").read_text())["session"], "old")
        self.assertEqual(json.loads((self.state / "hq-start.json").read_text())["session_id"], "old")

    def test_a_busy_hq_is_not_rotated(self):
        err = json.dumps({"error": {"code": "timeout", "message": "still blocked"}})
        write_scenario(self.tmp.name, [{"match": ["agent", "wait"], "stdout": "", "stderr": err, "exit": 1},
                                       alive("old"), HQ_SPACE])
        with self.assertRaises(hq.RestartError):
            hq.rotate_now()
        self.assertNotIn(["agent", "prompt", "athena", "/exit"], self.calls())
        self.assertEqual(self.starts(), [])

    def test_if_context_over_skips_a_lean_hq(self):
        path = self.claude_dir / "projects" / "p" / "old.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(transcript([assistant(20_000)]))
        with self.popen() as popen:
            result = hq.rotate(if_context_over=60)
        popen.assert_not_called()
        self.assertFalse(result["rotated"])
        self.assertIn("10%", result["message"])

    def test_if_context_over_rotates_a_full_hq(self):
        path = self.claude_dir / "projects" / "p" / "old.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(transcript([assistant(150_000)]))
        with self.popen() as popen:
            popen.return_value.pid = 1
            hq.rotate(if_context_over=60)
        popen.assert_called_once()

    def test_if_context_over_without_a_readable_context_does_nothing(self):
        with self.popen() as popen:
            result = hq.rotate(if_context_over=60)
        popen.assert_not_called()
        self.assertFalse(result["rotated"])
        self.assertIn("not readable", result["message"])

    def test_no_hq_to_rotate(self):
        (self.state / "hq.json").unlink()
        with self.assertRaises(hq.RestartError):
            hq.rotate()


class RotateCliTest(unittest.TestCase):
    def test_parses(self):
        p = cli.parser()
        a = p.parse_args(["hq", "rotate", "--threads", "-", "--dry-run", "--if-context-over", "70"])
        self.assertEqual((a.action, a.threads, a.dry_run, a.if_context_over), ("rotate", "-", True, 70.0))
        self.assertTrue(p.parse_args(["hq", "rotate", "--child"]).child)

    def test_rotate_flags_need_rotate(self):
        for argv in (["hq", "--dry-run"], ["hq", "restart", "--threads", "-"], ["hq", "--if-context-over", "50"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
                cli.main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_help_names_rotate(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            cli.parser().parse_args(["hq", "rotate", "--help"])
        for word in ("rotate", "--threads", "--dry-run", "--if-context-over", "handoff"):
            self.assertIn(word, out.getvalue())


if __name__ == "__main__":
    unittest.main()
