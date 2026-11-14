"""REX-PROGRESS: a worker's progress band. The worker's own task list feeds workers/<name>.progress.json through a
PostToolUse hook, at no model cost; a Haiku estimate fills in only when that list has gone quiet; the band's mod is
loaded by workers alone."""
import json
import os
import shutil
import subprocess
import time

from harness import BIN, ROOT, Case, store

from athena.caps import hooks, hq, install, progress, spawn
from athena.kernel import caps


def post(tool, tool_input, response=None, name="w1"):
    event = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input,
             "tool_response": response if response is not None else {}, "session_id": "s", "cwd": "/tmp"}
    return hooks.main_hook(json.dumps(event), {"ATHENA_WORKER": name})


def read(name="w1") -> dict:
    return store.read_dict(store.path("workers", f"{name}.progress.json"))


class Launch(Case):
    def test_a_worker_is_launched_with_the_progress_mod_and_hq_is_not(self):
        plugin = str(ROOT / "worker-plugin")
        worker = spawn.claude_args("w1", "claude-haiku-5-5", "low", self.tmp / "s.json")
        self.assertIn("--plugin-dir", worker)
        self.assertEqual(worker[worker.index("--plugin-dir") + 1], plugin)
        self.assertNotIn(plugin, hq.claude_args())
        manifest = json.loads((ROOT / "worker-plugin" / "hooks" / "hooks.json").read_text())
        for module in manifest["modules"]:
            self.assertTrue((ROOT / "worker-plugin" / "hooks" / module).is_file(), module)
        self.assertTrue((ROOT / "worker-plugin" / ".claude-plugin" / "plugin.json").is_file())
        self.assertIn("worker-plugin", install.releases.SHIP)  # an installed release carries it
        self.assertEqual(progress.PLUGIN, store.ROOT / "worker-plugin")  # by the `current` link, not one release

    def test_a_worker_has_the_task_tools_turned_on(self):
        """Claude Code gives Claude 5 models no task tools unless CLAUDE_CODE_ENABLE_TODO_TOOLS is set."""
        settings = json.loads(spawn.settings_for("w1", self.tmp / "brief.md").read_text())
        self.assertEqual(settings["env"]["CLAUDE_CODE_ENABLE_TODO_TOOLS"], "1")

    def test_the_settings_hook_the_task_list_tools_after_they_run(self):
        block = hooks.settings_block()
        matcher = block["PostToolUse"][0]["matcher"]
        for tool in ("TodoWrite", "TaskCreate", "TaskUpdate"):
            self.assertIn(tool, matcher.split("|"))
        self.assertIn("athena\" hook", block["PostToolUse"][0]["hooks"][0]["command"])

    def test_the_worker_prompt_asks_for_a_current_task_list(self):
        text = (ROOT / "prompts" / "worker.md").read_text()
        self.assertIn("task list", text)
        self.assertIn("ACCEPTANCE", text)


class Respawn(Case):
    def test_a_name_spawned_again_starts_its_band_from_nothing(self):
        post("TodoWrite", {"todos": [{"content": f"old {i}", "status": "completed", "activeForm": "-"} for i in range(5)]}, name="n")
        self.assertEqual(read("n")["total"], 5)
        spawn.spawn(caps.CTX, self.repo(), "t/n", "n", fetch=False, timeout=0.1)
        self.assertEqual(read("n"), {})
        self.assertEqual(json.loads((self.state / "workers" / "n.progress.json.prev").read_text())["total"], 5)
        post("TaskCreate", {"subject": "new", "description": "-"}, {"task": {"id": "1", "subject": "new"}}, name="n")
        self.assertEqual((read("n")["done"], read("n")["total"], list(read("n")["tasks"])), (0, 1, ["1"]))


class TaskList(Case):
    def test_task_tools_become_the_progress_file(self):
        for i, subject in enumerate(("write the hook", "write the mod", "run the lab"), 1):
            self.assertEqual(post("TaskCreate", {"subject": subject, "description": "-", "activeForm": subject.replace("write", "writing")},
                                  {"task": {"id": str(i), "subject": subject}}), "")
        post("TaskUpdate", {"taskId": "1", "status": "in_progress"}, {"success": True, "taskId": "1"})
        post("TaskUpdate", {"taskId": "1", "status": "completed"}, {"success": True, "taskId": "1"})
        post("TaskUpdate", {"taskId": "2", "status": "in_progress", "activeForm": "Writing the band"}, {"success": True, "taskId": "2"})
        p = read()
        self.assertEqual((p["done"], p["total"], p["step"], p["source"]), (1, 3, "Writing the band", "tasks"))
        self.assertAlmostEqual(p["ts"], time.time(), delta=5)
        self.assertEqual(p["tasks_ts"], p["ts"])
        post("TaskUpdate", {"taskId": "2", "status": "deleted"}, {"success": True, "taskId": "2"})
        self.assertEqual((read()["done"], read()["total"]), (1, 2))
        post("TaskCreate", {"subject": "a fourth", "description": "-"}, {})  # no id in the response
        self.assertEqual(list(read()["tasks"]), ["1", "3", "4"])

    def test_a_todo_list_is_read_whole(self):
        post("TodoWrite", {"todos": [
            {"content": "Port the hook", "status": "completed", "activeForm": "Porting the hook"},
            {"content": "Port the band", "status": "in_progress", "activeForm": "Porting the band"},
            {"content": "Prove it", "status": "pending", "activeForm": "Proving it"}]})
        p = read()
        self.assertEqual((p["done"], p["total"], p["step"], p["source"]), (1, 3, "Porting the band", "tasks"))

    def test_the_step_keeps_its_start_time_until_it_changes(self):
        post("TodoWrite", {"todos": [{"content": "A", "status": "in_progress", "activeForm": "Doing A"}]})
        first = read()["step_ts"]
        store.write_json(store.path("workers", "w1.progress.json"), {**read(), "step_ts": first - 300})
        post("TodoWrite", {"todos": [{"content": "A", "status": "in_progress", "activeForm": "Doing A"},
                                     {"content": "B", "status": "pending", "activeForm": "Doing B"}]})
        self.assertEqual(read()["step_ts"], first - 300)
        post("TodoWrite", {"todos": [{"content": "A", "status": "completed", "activeForm": "Doing A"},
                                     {"content": "B", "status": "in_progress", "activeForm": "Doing B"}]})
        self.assertGreater(read()["step_ts"], first - 1)

    def test_other_tools_and_sessions_that_are_not_workers_write_nothing(self):
        post("Bash", {"command": "ls"})
        self.assertEqual(read(), {})
        event = {"hook_event_name": "PostToolUse", "tool_name": "TodoWrite", "tool_input": {"todos": []}}
        self.assertEqual(hooks.main_hook(json.dumps(event), {}), "")
        self.assertEqual(list((self.state / "workers").glob("*.progress.json")) if (self.state / "workers").exists() else [], [])


class Fallback(Case):
    LINES = [f"assistant: line {i}" for i in range(60)]

    def setUp(self):
        super().setUp()
        store.write_json(store.path("workers", "w1.json"), {"name": "w1", "state": "working", "ts": time.time()})

    def fake_client(self, reply="2/5 writing the parity tests now"):
        asked = []

        def complete(request):
            asked.append(request)
            return reply
        return asked, complete

    def run_fallback(self, now, client, since=0.0):
        """What the mod does: ask, hand the request to its model client, give the answer back."""
        request = progress.ask("w1", self.LINES, now, since)
        if request:
            progress.answer("w1", client(request), now)
        return request

    def test_skipped_while_the_task_list_is_under_ten_minutes_old(self):
        post("TodoWrite", {"todos": [{"content": "A", "status": "in_progress", "activeForm": "Doing A"}]})
        asked, client = self.fake_client()
        self.assertIsNone(self.run_fallback(time.time() + 9 * 60, client))
        self.assertEqual(asked, [])
        self.assertEqual(read()["source"], "tasks")

    def test_skipped_when_it_ran_under_ten_minutes_ago(self):
        asked, client = self.fake_client()
        now = time.time()
        self.assertIsNotNone(self.run_fallback(now, client))
        self.assertIsNone(self.run_fallback(now + 9 * 60, client))
        self.assertEqual(len(asked), 1)
        self.assertIsNotNone(self.run_fallback(now + 11 * 60, client))
        self.assertEqual(len(asked), 2)

    def test_skipped_unless_working_or_when_turned_off(self):
        asked, client = self.fake_client()
        store.write_json(store.path("workers", "w1.json"), {"name": "w1", "state": "idle"})
        self.assertIsNone(self.run_fallback(time.time(), client))
        store.write_json(store.path("workers", "w1.json"), {"name": "w1", "state": "working"})
        self.write_config(rex={"progress_haiku": False})
        self.assertIsNone(self.run_fallback(time.time(), client))
        self.assertEqual(asked, [])
        self.write_config(rex={"progress_haiku": True})
        self.assertIsNone(self.run_fallback(time.time(), client, since=time.time() - 60))  # the session is new
        self.assertEqual(asked, [])
        self.assertIsNotNone(self.run_fallback(time.time(), client, since=time.time() - 11 * 60))

    def test_when_due_it_builds_a_capped_haiku_request_and_records_the_estimate(self):
        asked, client = self.fake_client()
        self.run_fallback(time.time(), client)
        (request,) = asked
        self.assertEqual(request["model"], "haiku")
        self.assertLessEqual(request["maxTokens"], 60)
        self.assertIn("line 59", request["prompt"])
        self.assertNotIn("line 19\n", request["prompt"])  # the last 40 lines only
        self.assertLessEqual(request["prompt"].count("assistant: line"), 40)
        p = read()
        self.assertEqual((p["done"], p["total"], p["step"], p["source"]), (2, 5, "writing the parity tests now", "estimate"))

    def test_an_answer_that_will_not_parse_or_a_newer_task_list_leaves_the_file(self):
        now = time.time()
        _, client = self.fake_client("I cannot tell")
        self.run_fallback(now, client)
        self.assertNotIn("source", read())
        post("TodoWrite", {"todos": [{"content": "A", "status": "in_progress", "activeForm": "Doing A"}]})
        progress.answer("w1", "4/4 all done", now)  # asked before the task list came in
        self.assertEqual(read()["source"], "tasks")

    def piped(self, *args, input_text):
        """--name, not ATHENA_WORKER: the harness's own rex- name keeps the lab guard on in the child."""
        return subprocess.run([str(BIN), *args, "--name", "w1"], input=input_text, capture_output=True, text=True,
                              env=dict(os.environ), timeout=60)

    def test_the_cli_asks_and_answers_as_the_mod_calls_it(self):
        out = self.piped("progress", "ask", "--since", "0", input_text="\n".join(self.LINES))
        self.assertEqual(out.returncode, 0, out.stderr)
        request = json.loads(out.stdout)
        self.assertEqual((request["model"], request["maxTokens"] <= 60), ("haiku", True))
        out = self.piped("progress", "answer", input_text="3/4 checking the band")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((read()["done"], read()["step"], read()["source"]), (3, "checking the band", "estimate"))
        again = self.piped("progress", "ask", "--since", "0", input_text="x")
        self.assertEqual(again.stdout.strip(), "")


MOD = ROOT / "worker-plugin" / "hooks" / "progress-mod"
NODE = shutil.which("node")
DRIVER = """
import * as core from './core.ts'
let text = ''
for await (const chunk of process.stdin) text += chunk
const out = JSON.parse(text).map(([fn, ...args]) => {
  if (fn === 'line') {
    const [progress, live, now] = args
    const b = core.band(core.parseProgress(JSON.stringify(progress)), live, now)
    return b ? core.bandLine(b) : null
  }
  return core[fn](...args)
})
process.stdout.write(JSON.stringify(out))
"""


def core(case, *calls):
    """Each call run in the band's own core.ts under Node (type stripping): [fn, ...args] -> its result. `line` is
    the band drawn as plain text from a progress file's fields, the live view and now. Node runs by a link in the
    test's own folder, as the canary lets a test run only what is in there; test.sh's sandbox holds for it as for any
    child, and core.ts is pure: it reads, writes and reaches nothing."""
    if not NODE:
        raise AssertionError("node is needed to test the band's core.ts")
    node = case.tmp / "node"
    if not node.exists():
        node.symlink_to(os.path.realpath(NODE))
    out = subprocess.run([str(node), "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", DRIVER],
                         input=json.dumps(list(calls)), capture_output=True, text=True, cwd=MOD, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def live(state="working", in_turn=True, closed_at=0, spans=None):
    return {"state": state, "inTurn": in_turn, "closedAt": closed_at, "spans": spans if spans is not None else [{"start": 0}]}


H = 3600
WATCHING = {"done": 3, "total": 6, "step": "Watching PR checks at reset head 6bbecc0141", "source": "tasks",
            "ts": 100, "tasks_ts": 100, "step_ts": 50, "list_ts": 1}
ZERO = __import__("re").compile(r"(?<![\dh])0m")


class Band(Case):
    """PROGRESS-FIX: the band shows only live, true progress."""

    def test_a_turn_that_ended_after_its_goal_was_cleared_mid_list_shows_its_state_not_the_step(self):
        """The goal cleared at 3/6, the turn ended done, and hours later the band still read the step."""
        ended = live("done", in_turn=False, closed_at=200, spans=[{"start": 0, "end": 200}])
        (line,) = core(self, ["line", WATCHING, ended, 200 + 5 * H])
        self.assertEqual(line, "done")
        idle = live("idle", in_turn=False, spans=[{"start": 0, "end": 200}])
        self.assertEqual(core(self, ["line", WATCHING, idle, 200 + 5 * H]), ["idle"])
        # A later turn, the list untouched since the done report: still not that list's step.
        later = live("working", closed_at=200, spans=[{"start": 0, "end": 200}, {"start": 5 * H}])
        (line,) = core(self, ["line", WATCHING, later, 5 * H + 60])
        self.assertEqual(line, "working")
        # Once the worker writes its list again in that turn, the band is live again.
        (line,) = core(self, ["line", {**WATCHING, "ts": 5 * H + 30, "step_ts": 5 * H + 30}, later, 5 * H + 150])
        self.assertTrue(line.startswith("▰▰▰▱▱ 3/6 · Watching PR checks"), line)

    def test_a_compaction_while_idle_leaves_no_step_behind(self):
        """'Compacted while idle': the turn is over and the list from before it is not shown, then or next turn."""
        compacted = 4 * H
        idle = live("idle", in_turn=False, closed_at=compacted, spans=[{"start": 0, "end": 200}])
        self.assertEqual(core(self, ["line", WATCHING, idle, compacted + 60]), ["idle"])
        nxt = live("working", closed_at=compacted, spans=[{"start": 0, "end": 200}, {"start": compacted + 120}])
        self.assertEqual(core(self, ["line", WATCHING, nxt, compacted + 180]), ["working"])

    def test_a_list_finished_at_9_of_9_and_cleared_shows_no_stale_count(self):
        nine = {**WATCHING, "done": 9, "total": 9, "step": ""}
        self.assertEqual(core(self, ["line", nine, live(), 300]), ["working"])
        self.assertEqual(core(self, ["line", nine, live("done", in_turn=False), 300]), ["done"])
        # The hook's side: Claude Code empties a list once all of it is done, and a list made after that starts anew.
        todos = [{"content": f"t{i}", "status": "completed", "activeForm": f"doing t{i}"} for i in range(9)]
        post("TodoWrite", {"todos": todos}, {"oldTodos": todos, "newTodos": []})
        self.assertEqual((read()["done"], read()["total"]), (0, 0))
        for i in range(1, 10):
            post("TaskCreate", {"subject": f"t{i}", "description": "-"}, {"task": {"id": str(i), "subject": f"t{i}"}})
        for i in range(1, 10):
            post("TaskUpdate", {"taskId": str(i), "status": "completed"}, {"success": True, "taskId": str(i)})
        self.assertEqual((read()["done"], read()["total"]), (9, 9))
        post("TaskCreate", {"subject": "next", "description": "-"}, {"task": {"id": "1", "subject": "next"}})
        self.assertEqual((read()["done"], read()["total"], list(read()["tasks"])), (0, 1, ["1"]))

    def test_the_time_counts_only_the_working_time_not_a_long_idle_gap(self):
        """10 minutes on the step, five idle hours, then 3 more minutes: 13m, not 5h13m."""
        spans = [{"start": 50, "end": 650}, {"start": 650 + 5 * H}]
        (line,) = core(self, ["line", WATCHING, live(spans=spans), 650 + 5 * H + 180])
        self.assertIn(" · 13m", line)
        self.assertNotIn("5h", line)
        self.assertEqual(core(self, ["worked", spans, 50, 650 + 5 * H + 180], ["worked", spans, 400, 700]), [780, 250])

    def test_the_eta_reads_eta_n_m_with_a_real_value_and_nothing_otherwise(self):
        """3 of 6 done in 30 working minutes since the list began (an idle hour between): eta 30m."""
        spans = [{"start": 1, "end": 20 * 60 + 1}, {"start": 20 * 60 + 1 + H}]
        now = 30 * 60 + 1 + H
        with_eta, no_list_start, none_done, estimated, all_but_started = core(
            self, ["line", {**WATCHING, "step_ts": 1}, live(spans=spans), now],
            ["line", {**WATCHING, "step_ts": 1, "list_ts": None}, live(spans=spans), now],
            ["line", {**WATCHING, "done": 0, "step_ts": 1}, live(spans=spans), now],
            ["line", {**WATCHING, "source": "estimate", "step_ts": 1}, live(spans=spans), now],
            ["line", {**WATCHING, "step_ts": now - 20, "ts": now - 20}, live(spans=[{"start": now - 30}]), now])
        self.assertEqual(with_eta, "▰▰▰▱▱ 3/6 · Watching PR checks at reset head 6bbecc0141 · 30m · eta 30m")
        self.assertEqual(no_list_start, "▰▰▰▱▱ 3/6 · Watching PR checks at reset head 6bbecc0141 · 30m")
        self.assertEqual(none_done, "▱▱▱▱▱ 0/6 · Watching PR checks at reset head 6bbecc0141 · 30m")
        self.assertEqual(estimated, "▰▰▰▱▱ 3/6 · Watching PR checks at reset head 6bbecc0141 · 30m")
        self.assertEqual(all_but_started, "▰▰▰▱▱ 3/6 · Watching PR checks at reset head 6bbecc0141")  # under a minute in
        self.assertEqual(core(self, ["eta", {"done": 1, "total": 2, "source": "tasks", "listTs": 1}, [{"start": 0}], 90]), ["eta 2m"])
        self.assertEqual(core(self, ["eta", {"done": 1, "total": 2, "source": "tasks", "listTs": 1}, [{"start": 0}], 30]), [""])
        for line in (with_eta, no_list_start, none_done, estimated, all_but_started):
            self.assertNotIn("estimate", line)
            self.assertNotIn("task list", line)
            self.assertIsNone(ZERO.search(line), line)

    def test_the_list_start_is_kept_until_a_new_list_begins(self):
        post("TodoWrite", {"todos": [{"content": "A", "status": "in_progress", "activeForm": "Doing A"},
                                     {"content": "B", "status": "pending", "activeForm": "Doing B"}]})
        first = read()["list_ts"]
        store.write_json(store.path("workers", "w1.progress.json"), {**read(), "list_ts": first - 300})
        post("TodoWrite", {"todos": [{"content": "A", "status": "completed", "activeForm": "Doing A"},
                                     {"content": "B", "status": "in_progress", "activeForm": "Doing B"}]})
        self.assertEqual(read()["list_ts"], first - 300)
        post("TodoWrite", {"todos": [{"content": "A", "status": "completed", "activeForm": "Doing A"},
                                     {"content": "B", "status": "completed", "activeForm": "Doing B"}]})
        post("TodoWrite", {"todos": [{"content": "C", "status": "in_progress", "activeForm": "Doing C"}]})
        self.assertGreater(read()["list_ts"], first - 1)
