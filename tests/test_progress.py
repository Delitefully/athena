"""A worker's progress band. The worker's own task list feeds workers/<name>.progress.json through a
PostToolUse hook, at no model cost; a Haiku estimate fills in only when that list has gone quiet; the band's mod is
loaded by workers alone."""
import json
import os
import subprocess
import time

from harness import BIN, ROOT, Case, store

from athena_rex.caps import hooks, hq, install, progress, spawn
from athena_rex.kernel import caps


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
        self.assertIn("worker-plugin", install.SHIP)  # an installed release carries it
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
        self.assertIn("athena-rex\" hook", block["PostToolUse"][0]["hooks"][0]["command"])

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
