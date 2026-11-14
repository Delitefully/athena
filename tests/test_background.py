"""A worker whose turn ended while its subagents, workflows, background shells or monitors still run is working (background):
Claude Code's Stop and SubagentStop hooks carry `background_tasks`, the in-flight work that will wake the session again."""
import json
import os
import time

from harness import Case, spawn_record, store

from athena.caps import board, hooks, status, watch
from athena.caps.dash import state as dash_state, view
from athena.kernel import agents, caps

WORKFLOW = {"id": "w1", "type": "workflow", "status": "running", "description": "triage", "name": "gw-arch-triage"}
SUBAGENT = {"id": "a1", "type": "subagent", "status": "running", "description": "review", "agent_type": "general-purpose"}
IN_PROGRESS = 'waiting on the workflow\nATHENA-REPORT {"status":"IN_PROGRESS"}'


class Background(Case):
    def setUp(self):
        super().setUp()
        spawn_record("w", self.tmp, self.tmp)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("12345")
        os.environ["ATHENA_PS"] = "/nonexistent/ps"  # nobody can say whether its Claude runs: the hook's word stands

    def _stop(self, tasks, message=IN_PROGRESS):
        event = {"hook_event_name": "Stop", "last_assistant_message": message, "cwd": str(self.tmp), "session_id": "s1"}
        if tasks is not None:
            event["background_tasks"] = tasks
        return hooks.apply(event, "w")

    def _subagent_stop(self, tasks):
        return hooks.apply({"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "general-purpose",
                            "background_tasks": tasks, "session_id": "s1"}, "w")

    def _row(self):
        return status.collect()[0]

    def _watch(self, prev):
        _, _, told = watch.step(caps.CTX, prev, 0, watch.Dedupe())
        return [line.text for line in told if line.worker == "w"]

    def test_the_hooks_say_how_much_background_work_runs(self):
        self.assertIn("SubagentStop", hooks.settings_block())
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        out = self._stop([WORKFLOW, SUBAGENT, {**SUBAGENT, "id": "a2", "status": "completed"}])
        self.assertEqual((out["state"], out["background"]), ("idle", 2), "the turn is over; a finished task is not in flight")
        self.assertEqual(self._stop(None)["background"], 0, "a Claude Code that sends no background_tasks: none")

    def test_status_and_board_rows_show_it_as_working_background(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self._stop([WORKFLOW])
        row = self._row()
        self.assertEqual((row["state"], row["state_source"], row["background"]), ("working", "background", 1))
        self.assertIn("working (background)", status._line(row))
        self.assertIn("* w", board.render([row], {}))
        self.assertIn("working (background)", board.render([row], {}))

    def test_the_watch_sends_no_idle_line_while_background_work_runs(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self._stop([WORKFLOW])
        told = self._watch({"w": {"state": "working"}})
        self.assertFalse([t for t in told if "->idle" in t], told)
        self.assertEqual([t for t in told if "report" in t], ["w report IN_PROGRESS"], "the report line is as today")

    def test_a_gate_that_gives_way_while_background_work_runs_is_no_stopped_early(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        for _ in range(hooks.MAX_BLOCKS):
            self.assertEqual(self._stop([SUBAGENT], "no report")["gate"]["decision"], "block")
        out = self._stop([SUBAGENT], "no report")
        self.assertEqual((out["state"], out["background"]), ("idle-unreported", 1))
        self.assertEqual(self._row()["state"], "working")
        self.assertFalse([t for t in self._watch({"w": {"state": "working"}}) if "idle" in t])

    def test_a_subagent_ending_while_idle_updates_the_count_and_never_the_turn(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self.assertEqual(self._subagent_stop([WORKFLOW])["state"], "working", "a subagent ending mid-turn leaves the turn be")
        self._stop([WORKFLOW, SUBAGENT])
        out = self._subagent_stop([WORKFLOW])
        self.assertEqual((out["state"], out["background"], out["report"]["status"]), ("idle", 1, "IN_PROGRESS"))
        self.assertEqual(self._row()["state"], "working")
        self._subagent_stop([])
        self.assertEqual(self._row()["state"], "idle")

    def test_when_the_background_work_ends_it_is_idle_again_and_the_watch_says_so(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self._stop([WORKFLOW])
        prev = {"w": {"state": "working", "report": {"status": "IN_PROGRESS"}}}
        self.assertEqual(self._watch(prev), [])
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")  # the workflow's end wakes the session
        self._stop([])
        row = self._row()
        self.assertEqual((row["state"], row["state_source"], row["background"]), ("idle", "hook", 0))
        self.assertIn("w state working->idle", self._watch(prev))

    def test_a_wake_turn_that_dies_without_a_stop_leaves_no_stale_count(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self._stop([WORKFLOW])
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")  # the workflow's end wakes it; this turn is interrupted
        hooks.apply({"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "waiting"}, "w")
        self.assertEqual((self._row()["state"], self._row()["background"]), ("idle", 0))
        self._stop([WORKFLOW])  # no UserPromptSubmit for the wake, and its turn fails on an API error or a limit
        hooks.apply({"hook_event_name": "StopFailure", "error": "rate_limit"}, "w")
        self.assertEqual((self._row()["state"], self._row()["background"]), ("idle", 0))
        self.assertIn("StopFailure", hooks.settings_block())

    def test_an_idle_prompt_while_background_work_runs_keeps_it(self):
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        self._stop([WORKFLOW])
        hooks.apply({"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "waiting"}, "w")
        self.assertEqual(self._row()["state"], "working")

    def test_a_new_session_has_no_background_work(self):
        self._stop([WORKFLOW])
        self.assertEqual(hooks.apply({"hook_event_name": "SessionStart", "source": "resume"}, "w")["background"], 0)

    def test_a_report_still_wins(self):
        out = self._stop([WORKFLOW], 'x\nATHENA-REPORT {"status":"BLOCKED"}')
        self.assertEqual(out["background"], 1)
        self.assertEqual(self._row()["state"], "blocked")

    def test_the_dash_shows_it_as_working_background(self):
        worker = dash_state.Worker(name="w", repo="r", branch="t/w", state="live", spawned=time.time(), updated=time.time())
        for hook, label in ((dash_state.Hook(state="idle", ts=time.time(), background=2), "working (background)"),
                            (dash_state.Hook(state="idle-unreported", ts=time.time(), background=1), "working (background)"),
                            (dash_state.Hook(state="idle", ts=time.time()), "idle")):
            v = view.build(dash_state.Snapshot(workers=[worker], hooks={"w": hook}), time.time(), "")
            item = next(it for it in v.in_progress if it.id.endswith("w"))
            self.assertEqual(item.label, label)
            self.assertEqual(item.icon, "working" if hook.background else "idle")
        self.assertEqual(dash_state.parse_hook(json.loads('{"state":"idle","background":3}')).background, 3)
