"""Flame: the transcript ingest and the dash's Flame page, on small made-up transcripts. No real transcript is read here."""
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from unittest import mock

from harness import BIN, Case, _free_port, spawn_record

from athena.caps import flame
from athena.caps.flame import frames, ingest, label, query
from athena.kernel import config, store

T0 = 1_790_000_000_000  # a made-up moment, in ms


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"


class Transcript:
    """Writes a made-up Claude Code transcript, record by record, in the shapes Claude Code uses."""

    def __init__(self, path, session="s-1", cwd="/tmp/work"):
        self.path = path
        self.session = session
        self.cwd = cwd
        self.lines = []
        path.parent.mkdir(parents=True, exist_ok=True)

    def _base(self, kind, at):
        return {"parentUuid": None, "isSidechain": False, "type": kind, "sessionId": self.session, "cwd": self.cwd, "timestamp": iso(at)}

    def name(self, agent_name):
        self.lines.append({"type": "agent-name", "agentName": agent_name, "sessionId": self.session})
        return self

    def prompt(self, at, text="do the thing"):
        self.lines.append({**self._base("user", at), "message": {"role": "user", "content": text}})
        return self

    def attachment(self, at):
        self.lines.append({**self._base("attachment", at), "attachment": {"type": "hook"}})
        return self

    def assistant(self, at, mid, blocks, usage, model="claude-opus-5-5", skill=None):
        """One record per block, the usage repeated on each, as Claude Code writes them."""
        for block in blocks:
            record = {**self._base("assistant", at), "message": {"id": mid, "model": model, "role": "assistant", "content": [block],
                                                                 "usage": usage}}
            if skill:
                record["attributionSkill"] = skill
            self.lines.append(record)
        return self

    def result(self, at, tool_id):
        self.lines.append({**self._base("user", at), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "made-up output"}]}, "toolUseResult": {}})
        return self

    def noise(self):
        self.lines.append({"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "x" * 500}]}})
        return self

    def write(self, upto=None):
        lines = self.lines if upto is None else self.lines[:upto]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in lines))
        return self


def usage(inp=10, out=100, read=1000, write5=0, write1=200):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read, "cache_creation_input_tokens": write5 + write1,
            "cache_creation": {"ephemeral_5m_input_tokens": write5, "ephemeral_1h_input_tokens": write1}}


def bash(tool_id, command):
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}


class Frames(Case):
    def test_a_command_is_named_by_its_first_real_word(self):
        cases = {
            "git status": ["git", "git status"],
            "cd /tmp/x && git -C /tmp/y push origin main": ["git", "git push"],
            "FOO=1 BAR=2 nice -n 10 gh pr checks 12 --watch": ["gh", "gh pr"],
            "timeout 30 /usr/bin/python3 -I scripts/smoke.py --fast": ["python3", "python3 smoke.py"],
            "export X=1; scripts/test.sh 2>&1 | tail -5": ["test.sh"],
            "ls -la": ["ls"],
            "echo hi\nrm -rf /": ["echo"],
            "until gh pr checks 12; do sleep 30; done": ["loop", "loop: gh pr"],
            "while [ ! -f x ]; do sleep 5; done": ["loop", "loop: sleep"],
            "for f in a b; do git add \"$f\"; done": ["loop", "loop: git add"],
            "for i in 1; do for j in 2; do ls; done; done": ["loop", "loop: ls"],
            "if grep -q x f; then echo y; fi": ["if", "if: grep"],
            '"$(cat secret)" --flag': ["other"],
            "": ["other"],
        }
        for command, want in cases.items():
            self.assertEqual(frames.bash_family(command), want, command)

    def test_names_that_dont_look_like_names_are_other(self):
        self.assertEqual(frames.safe("a b"), "other")
        self.assertEqual(frames.safe("x" * 41), "other")
        self.assertEqual(frames.tool_path("mcp__claude-in-chrome__computer", {}), ["MCP", "claude-in-chrome", "computer"])
        self.assertEqual(frames.tool_path("Agent", {"subagent_type": "Explore"}), ["Agent", "Explore"])
        self.assertEqual(frames.tool_path("Agent", {}), ["Agent", "general-purpose"])
        self.assertEqual(frames.tool_path("Workflow", {"script": "export const meta = { name: 'review-it', description: 'x' }"}),
                         ["Workflow", "review-it"])
        self.assertEqual(frames.tool_path("Skill", {"skill": "dataviz"}), ["Skill", "dataviz"])

    def test_cost_is_list_price_with_cache_writes_by_ttl(self):
        # Opus 5.5: $4 in, $20 out, $0.20 read; 1-hour writes at 2x input
        got = frames.cost_micro("claude-opus-5-5", {"in": 1_000_000, "out": 1_000_000, "cr": 1_000_000, "cw": 1_000_000, "cw1": 1_000_000})
        self.assertEqual(got, 32_200_000)
        self.assertEqual(frames.cost_micro("claude-sonnet-5-5", {"out": 1_000_000}), 10_000_000)
        self.assertEqual(frames.cost_micro("claude-opus-5-5", {"out": 1_000_000, "fast": 1}), 40_000_000)


class Ingest(Case):
    def setUp(self):
        super().setUp()
        self.projects = self.home / ".claude" / "projects"
        self.db_path = self.state / "flame" / "index.sqlite"

    def index(self, **kw):
        return ingest.index(self.db_path, self.projects, labels=label.label_all, **kw)

    def db(self):
        return sqlite3.connect(str(self.db_path))

    def session(self, name="w1", session="s-1", folder="proj"):
        t = Transcript(self.projects / folder / f"{session}.jsonl", session=session)
        t.name(name).prompt(T0)
        t.attachment(T0 + 500)
        t.assistant(T0 + 2_000, "m1", [{"type": "thinking", "thinking": ""}, bash("t1", "cd x && git push origin")], usage())
        t.noise()
        t.result(T0 + 6_000, "t1")
        t.assistant(T0 + 9_000, "m2", [{"type": "text", "text": "done"}], usage(inp=5, out=50, read=1200, write1=0))
        t.prompt(T0 + 600_000, "next")
        return t

    def steps(self):
        return self.db().execute("""SELECT s.kind, f.name, s.ms, s.input, s.output, s.cache_read, s.cache_write FROM steps s
                                    JOIN frames f ON f.id = s.frame ORDER BY s.start_ms, s.kind""").fetchall()

    def test_a_step_is_its_call_and_its_run_and_usage_counts_once(self):
        self.session().write()
        self.index()
        steps = self.steps()
        # the call: from the last record before it (the attachment at +0.5 s) to its last record (+2 s); its usage once, not twice
        self.assertIn((0, "git push", 1_500, 10, 100, 1000, 200), steps)
        self.assertIn((1, "git push", 4_000, 0, 0, 0, 0), steps)  # the run: tool_use to tool_result
        self.assertIn((0, "reply", 3_000, 5, 50, 1200, 0), steps)

    def test_tokens_are_conserved_and_an_index_in_pieces_equals_one_in_one_go(self):
        t = self.session()
        t.write(upto=5)  # stops between a tool call and its result
        self.index()
        t.write()
        self.index()
        pieces = self.db().execute("SELECT SUM(ms), SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), COUNT(*) FROM rollup").fetchone()
        self.assertEqual(pieces[1:5], (15, 150, 2200, 200), "every message's usage counted exactly once")
        self.index(full=True)
        whole = self.db().execute("SELECT SUM(ms), SUM(input), SUM(output), SUM(cache_read), SUM(cache_write), COUNT(*) FROM rollup").fetchone()
        self.assertEqual(pieces, whole)

    def test_a_second_run_with_nothing_new_reads_nothing(self):
        self.session().write()
        self.index()
        counts = self.index()
        self.assertEqual(counts["read"], 0)

    def test_parallel_runs_share_their_overlap(self):
        t = Transcript(self.projects / "proj" / "s-2.jsonl", session="s-2")
        t.prompt(T0).assistant(T0 + 1_000, "m1", [bash("a", "sleep 4"), bash("b", "sleep 2")], usage(out=100))
        t.result(T0 + 3_000, "b").result(T0 + 5_000, "a").prompt(T0 + 9_000)
        t.write()
        self.index()
        runs = dict(self.db().execute("SELECT f.name, SUM(s.ms) FROM steps s JOIN frames f ON f.id = s.frame WHERE s.kind = 1 "
                                      "GROUP BY f.name").fetchall())
        each = sorted(r[0] for r in self.db().execute("SELECT ms FROM steps WHERE kind = 1"))
        self.assertEqual(each, [1_000, 3_000])
        self.assertEqual(runs, {"sleep": 4_000}, "both sleeps are one frame: 1 s each for the 2 s they overlap, 2 s alone")
        calls = self.db().execute("SELECT output FROM steps WHERE kind = 0").fetchall()
        self.assertEqual(sorted(c[0] for c in calls), [50, 50], "a call with two tool calls splits its tokens between them")

    def test_idle_time_is_not_model_time(self):
        t = Transcript(self.projects / "proj" / "s-3.jsonl", session="s-3")
        t.prompt(T0).assistant(T0 + 3 * 3600_000, "m1", [{"type": "text", "text": "late"}], usage()).prompt(T0 + 3 * 3600_000 + 1)
        t.write()
        self.index()
        self.assertEqual(self.db().execute("SELECT ms FROM steps").fetchone()[0], ingest.CALL_CAP_MS)

    def test_a_quiet_file_finishes_its_last_reply(self):
        t = Transcript(self.projects / "proj" / "s-4.jsonl", session="s-4")
        t.prompt(T0).assistant(T0 + 2_000, "m1", [{"type": "text", "text": "bye"}], usage(out=7)).write()
        self.index()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM steps").fetchone()[0], 0, "a fresh file may still be writing it")
        old = time.time() - 2 * ingest.IDLE_S
        os.utime(t.path, (old, old))
        self.index()
        self.assertEqual(self.db().execute("SELECT SUM(output) FROM steps").fetchone()[0], 7)

    def test_subagents_and_workflow_agents_hang_under_their_frames(self):
        self.session(session="s-5").write()
        sub = self.projects / "proj" / "s-5" / "subagents"
        a = Transcript(sub / "agent-a1.jsonl", session="s-5").prompt(T0 + 2_100)
        a.assistant(T0 + 3_000, "x1", [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/x"}}], usage(out=3))
        a.result(T0 + 3_500, "r1").write()
        (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore"}))
        wf = sub / "workflows" / "wf_abc"
        w = Transcript(wf / "agent-b1.jsonl", session="s-5").prompt(T0 + 4_000)
        w.assistant(T0 + 5_000, "y1", [{"type": "text", "text": "verdict"}], usage(out=4)).prompt(T0 + 6_000).write()
        (wf / "agent-b1.meta.json").write_text(json.dumps({"agentType": "general-purpose", "workflowPhase": "Review"}))
        (self.projects / "proj" / "s-5" / "workflows").mkdir(parents=True)
        (self.projects / "proj" / "s-5" / "workflows" / "wf_abc.json").write_text(json.dumps({"workflowName": "review-it"}))
        self.index()
        db = self.db()
        key = str(self.db_path)
        paths = query._frame_paths(db, key)
        used = {paths[f] for (f,) in db.execute("SELECT DISTINCT frame FROM steps")}
        self.assertIn(("Agent", "Explore", "Read"), used)
        self.assertIn(("Workflow", "review-it", "Review", "reply"), used)
        files = dict(db.execute("SELECT kind, session FROM files").fetchall())
        self.assertEqual(files, {"main": "s-5", "agent": "s-5", "workflow": "s-5"})

    def test_a_file_that_was_rewritten_is_read_again_from_the_start(self):
        t = self.session(session="s-6")
        t.write()
        self.index()
        t.lines = t.lines[:2]
        t.write()
        self.index()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM steps").fetchone()[0], 0)

    def test_sessions_are_named_from_athenas_records(self):
        spawn_record("w1", "/tmp/w1-tree", "/tmp/repo", linear="TCK-1", title="made-up ticket")
        spawn_record("w2", "/tmp/w2-tree", "/tmp/repo", linear="TCK-2", title="another")
        self.session(name="w1", session="a-1").write()
        Transcript(self.projects / "p2" / "a-2.jsonl", session="a-2", cwd="/tmp/w2-tree").prompt(T0).write()  # by its folder
        Transcript(self.projects / "p3" / "a-3.jsonl", session="a-3", cwd="/tmp/hq").name("athena").prompt(T0).write()
        Transcript(self.projects / "p4" / "a-4.jsonl", session="a-4", cwd="/tmp/elsewhere").prompt(T0).write()
        self.index()
        got = {r[0]: r[1:] for r in self.db().execute("SELECT id, kind, name, ticket, title FROM sessions")}
        self.assertEqual(got["a-1"], ("worker", "w1", "TCK-1", "made-up ticket"))
        self.assertEqual(got["a-2"], ("worker", "w2", "TCK-2", "another"))
        self.assertEqual(got["a-3"], ("hq", "HQ", "", ""))
        self.assertEqual(got["a-4"], ("other", "elsewhere", "", ""))

    def test_the_tree_and_top_tables_for_a_scope(self):
        spawn_record("w1", "/tmp/w1-tree", "/tmp/repo", linear="TCK-1", title="t")
        self.session(name="w1", session="a-1").write()
        Transcript(self.projects / "p3" / "a-3.jsonl", session="a-3").name("athena").prompt(T0).assistant(
            T0 + 1_000, "h1", [bash("h", "gh pr checks 1")], usage(out=9)).result(T0 + 2_000, "h").prompt(T0 + 3_000).write()
        self.index()
        out = flame.data("athena")
        root = out["root"]
        self.assertEqual(root["n"], "athena")
        names = [c["n"] for c in root["c"]]
        self.assertEqual(sorted(names), ["HQ", "workers"])
        workers = next(c for c in root["c"] if c["n"] == "workers")
        w1 = workers["c"][0]
        self.assertEqual((w1["n"], w1.get("s")), ("w1", "w1"))
        bash_frame = next(c for c in w1["c"] if c["n"] == "Bash")
        self.assertIn("tool:Bash", bash_frame["h"])
        self.assertEqual(root["v"][3], 100 + 50 + 9, "output tokens add up to the root")
        top = out["top"]
        self.assertEqual({r[0] for r in top["bash"]}, {"git push", "gh pr"})
        self.assertEqual(flame.data("hq")["root"]["n"], "HQ")
        self.assertEqual(flame.data("worker:w1")["root"]["v"][3], 150)
        self.assertEqual(flame.data("ticket:TCK-1")["root"]["v"][3], 150)
        self.assertEqual(out["sessions"]["w1"]["ticket"], "TCK-1")

    def test_a_stale_index_starts_one_update_at_a_time(self):
        self.session().write()
        self.index()
        self.addCleanup(setattr, flame, "_runner", None)
        self.assertFalse(flame.refresh_if_stale(), "fresh: nothing starts")
        self.assertTrue(flame.refresh_if_stale(now=time.time() + 3600))
        runner = flame._runner
        self.assertTrue(flame.refresh_if_stale(now=time.time() + 3600), "still running")
        self.assertIs(flame._runner, runner, "and not started twice")
        self.assertEqual(runner.wait(60), 0)
        self.assertFalse(flame.running())
        self.assertGreater(flame.last_run_at(), 0)

    def test_a_line_of_the_wrong_shape_is_skipped_and_never_stops_a_run(self):
        self.session(session="good").write()
        bad = Transcript(self.projects / "proj" / "poison.jsonl", session="poison")
        base = {"type": "assistant", "timestamp": iso(T0 + 1_000), "sessionId": "poison"}
        bad.prompt(T0)
        bad.lines += [
            {**base, "message": "a string, not a message"},
            {**base, "type": "user", "message": {"content": 7}},
            {**base, "message": {"id": "m9", "model": "claude-opus-5-5", "content": 3}},
            {**base, "message": {"id": "m8", "model": "claude-opus-5-5", "usage": usage(),
                                 "content": [{"type": "tool_use", "id": ["a", "list"], "name": "Bash", "input": {}}]}},
            {**base, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": {"a": 1}}]}},
            {**base, "message": {"id": "m7", "model": {"an": "object"}, "usage": usage(out=5), "content": [{"type": "text"}]}},
            {**base, "message": {"id": "m6", "model": ["a", "list"], "usage": usage(out=6),
                                 "content": [{"type": "tool_use", "id": "t6", "name": {"x": 1}, "input": {}}]}},
            {**base, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t6"}]}},
            {**base, "message": {"id": "m5", "model": "claude-opus-5-5", "usage": {"input_tokens": 10 ** 20, "output_tokens": 4},
                                 "content": [{"type": "text"}]}},
        ]
        bad.assistant(T0 + 5_000, "ok1", [{"type": "text", "text": "fine"}], usage(out=11)).prompt(T0 + 6_000)
        bad.write()
        lines = bad.path.read_text().splitlines(True)
        lines.insert(len(lines) - 2, "[" * 200_000 + "\n")  # nested past what any JSON parser will go
        bad.path.write_text("".join(lines))
        counts = self.index()
        self.assertEqual(counts["failed"], 0, "no odd line fails its file")
        self.assertGreaterEqual(counts["skipped"], 9, "and each odd one is counted, so the page's health note is honest")
        db = self.db()
        offsets = dict(db.execute("SELECT session, offset = size FROM files").fetchall())
        self.assertEqual(offsets, {"good": 1, "poison": 1}, "both files read to their end")
        self.assertEqual(db.execute("SELECT SUM(output) FROM steps s JOIN files f ON f.id = s.file WHERE f.session = 'good'").fetchone()[0], 150)
        self.assertIn(11, [r[0] for r in db.execute("SELECT output FROM steps s JOIN files f ON f.id = s.file WHERE f.session = 'poison'")],
                      "the good lines after the bad ones still count")
        self.assertEqual(self.index()["read"], 0, "and the next run doesn't read them again")
        names = {r[0] for r in db.execute("SELECT text FROM names")}
        self.assertEqual(names, {"claude-opus-5-5"}, "only a string is ever interned as a model")
        biggest = db.execute("SELECT MAX(input), MAX(output) FROM steps").fetchone()
        self.assertLess(biggest[0], 2 ** 53, "a count no real message has is taken as 0")
        self.assertIn("9 lines in 1 transcripts could not be read", self.cli("flame", "status").stdout, "and it stays said")

    def test_a_file_that_fails_is_said_on_the_page_and_in_status(self):
        self.session(session="good").write()
        Transcript(self.projects / "proj" / "bad.jsonl", session="bad").prompt(T0).write()
        real = ingest._one

        def failing(db, interner, counts, path, *rest):
            if path.endswith("bad.jsonl"):
                raise RuntimeError("made up")
            return real(db, interner, counts, path, *rest)
        with mock.patch.object(ingest, "_one", failing):
            counts = self.index()
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(flame.data("all")["status"]["last_run"]["failed"], 1, "the page reads it from the index")
        status = self.cli("flame", "status")
        self.assertIn("could not index", status.stdout)
        self.assertIn("bad.jsonl: RuntimeError", status.stdout)
        self.assertEqual(self.index()["failed"], 0, "and it is tried again, and read, on the next run")

    def test_a_full_rebuild_is_seen_whole_or_not_at_all(self):
        self.session(session="a").write()
        self.session(session="b").write()
        self.index()
        before = self.db().execute("SELECT COUNT(*), SUM(output) FROM rollup").fetchone()
        seen = []
        real = ingest._one

        def watched(*args, **kw):
            seen.append(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True).execute(
                "SELECT COUNT(*), SUM(output) FROM rollup").fetchone())
            return real(*args, **kw)
        with mock.patch.object(ingest, "_one", watched):
            self.index(full=True)
        self.assertEqual(seen, [before, before], "a reader mid-rebuild sees the old index, whole")
        self.assertEqual(self.db().execute("SELECT COUNT(*), SUM(output) FROM rollup").fetchone(), before)

    def test_a_refresh_that_cannot_start_still_serves_the_index(self):
        self.session().write()
        self.index()
        self.addCleanup(setattr, flame, "_runner", None)
        with mock.patch.object(flame.subprocess, "Popen", side_effect=OSError("no room")):
            with mock.patch.object(flame, "last_run_at", return_value=1):
                out = flame.data("all", refresh=True)
        self.assertFalse(out["refreshing"])
        self.assertEqual(out["root"]["v"][3], 150, "the index it has")

    def test_a_refresh_never_runs_beside_another_index(self):
        self.session().write()
        self.index()
        self.addCleanup(setattr, flame, "_runner", None)
        at = flame.last_run_at()
        with store.locked("flame/index.lock") as held:  # as a `flame index --full` holds it
            self.assertTrue(held)
            self.assertTrue(flame.refresh_if_stale(now=time.time() + 3600), "one is running")
            self.assertIsNone(flame._runner, "so none is started")
            second = self.cli("flame", "index")
            self.assertEqual((second.returncode, second.stdout.strip()), (0, "an index is already running"))
        self.assertEqual(flame.last_run_at(), at)

    def test_no_transcript_text_reaches_the_index(self):
        t = Transcript(self.projects / "proj" / "s-7.jsonl", session="s-7")
        t.prompt(T0, "SECRET-PROMPT-WORDS").assistant(T0 + 1_000, "m1", [
            {"type": "text", "text": "SECRET-REPLY"}, bash("t", "curl -H 'Authorization: SECRET-TOKEN' https://x")], usage())
        t.result(T0 + 2_000, "t").prompt(T0 + 3_000).write()
        self.index()
        raw = self.db_path.read_bytes() + b"".join(p.read_bytes() for p in self.db_path.parent.glob("index.sqlite-*"))
        self.assertNotIn(b"SECRET", raw)
        self.assertNotIn(b"made-up output", raw)


class Cli(Case):
    def test_index_and_status_through_the_command(self):
        projects = self.home / ".claude" / "projects"
        Transcript(projects / "proj" / "s-1.jsonl").prompt(T0).assistant(T0 + 1_000, "m", [bash("t", "ls")], usage()).result(
            T0 + 2_000, "t").prompt(T0 + 3_000).write()
        before = self.cli("flame")
        self.assertIn("no index yet", before.stdout)
        done = self.cli("flame", "index")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("read 1 of 1 transcripts", done.stdout)
        self.assertIn("peak RSS", done.stdout)
        status = self.cli("flame", "status", "--json")
        self.assertEqual(json.loads(status.stdout)["files"], 1)
        again = self.cli("flame", "index", "--quiet")
        self.assertEqual((again.returncode, again.stdout), (0, ""))

    def test_no_transcripts_is_said(self):
        done = self.cli("flame", "index", "--projects", str(self.tmp / "nowhere"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("no transcripts", done.stderr)


class Page(Case):
    def setUp(self):
        super().setUp()
        self.port = _free_port()
        os.environ["ATHENA_DASH_PORT"] = str(self.port)
        config.reset()
        projects = self.home / ".claude" / "projects"
        Transcript(projects / "proj" / "s-1.jsonl").name("athena").prompt(T0).assistant(
            T0 + 1_000, "m", [bash("t", "ls")], usage()).result(T0 + 2_000, "t").prompt(T0 + 3_000).write()
        ingest.index(store.path("flame", "index.sqlite"), projects, labels=label.label_all)
        self.proc = subprocess.Popen([str(BIN), "dash", "run"], env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.proc.terminate(), self.proc.wait()))
        for _ in range(100):
            try:
                self._get("/healthz")
                break
            except OSError:
                time.sleep(0.05)

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode()

    def test_the_page_its_data_and_the_link_to_it(self):
        status, kind, page = self._get("/flame")
        self.assertEqual(status, 200)
        self.assertIn('<main class="full flame"', page, "a full page: the dash's live pushes leave it alone")
        self.assertIn('<a href="/flame" aria-current="page">Flame</a>', page)
        self.assertIn('href="/static/flame.css', page)
        self.assertIn('src="/static/flame.js', page)
        self.assertNotIn("<style", page, "the dash's CSP takes styles only from files")
        self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)", page), [], "and scripts only from files")
        self.assertIn('<a href="/flame">Flame</a>', self._get("/")[2])
        for name, want in (("flame.js", "text/javascript"), ("flame.css", "text/css")):
            status, kind, _ = self._get(f"/static/{name}")
            self.assertEqual((status, kind.split(";")[0]), (200, want))
        status, kind, body = self._get("/flame/data?scope=hq")
        self.assertEqual((status, kind), (200, "application/json"))
        data = json.loads(body)
        self.assertEqual(data["root"]["n"], "HQ")
        self.assertEqual([c["n"] for c in data["root"]["c"]], ["Bash"])
        self.assertFalse(data["refreshing"], "a fresh index is left alone")

    def test_a_stale_index_updates_itself_when_the_page_asks(self):
        db = sqlite3.connect(str(store.path("flame", "index.sqlite")))
        old = int(time.time()) - 3600
        db.execute("UPDATE meta SET value = ? WHERE key = 'last_run'", (json.dumps({"at": old}),))
        db.commit()
        db.close()
        Transcript(self.home / ".claude" / "projects" / "proj" / "s-2.jsonl", session="s-2").name("athena").prompt(T0).assistant(
            T0 + 1_000, "m2", [bash("u", "git status")], usage(out=7)).result(T0 + 2_000, "u").prompt(T0 + 3_000).write()
        first = json.loads(self._get("/flame/data?scope=hq")[2])
        self.assertTrue(first["refreshing"])
        for _ in range(300):
            st = json.loads(self._get("/flame/status")[2])
            if not st["refreshing"] and (st["at"] or 0) > old:
                break
            time.sleep(0.1)
        self.assertGreater(st["at"], old, "the index ran")
        after = json.loads(self._get("/flame/data?scope=hq")[2])
        self.assertFalse(after["refreshing"], "now fresh, nothing more starts")
        self.assertIn("git", [c["n"] for c in after["root"]["c"][0]["c"]], "the new transcript is in")

    def test_a_bad_scope_or_date_is_refused(self):
        for bad in ("/flame/data?scope=../x", "/flame/data?since=yesterday", "/flame/data?scope=worker:a%20b"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._get(bad)
            self.assertEqual(caught.exception.code, 400, bad)
