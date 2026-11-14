"""Reports leave the chat (REPORTS-OUT): a worker files one with `athena report`, the Stop hook's gate takes in the one filed
this turn, and status, watch, review and the dash read it from the worker's status file. A typed ATHENA-REPORT line still works."""
import json
import os
import subprocess
import time

from harness import BIN, Case, git, spawn_record, store

from athena.caps import brief, hooks, prs, spawn, status, watch
from athena.caps.dash import state as dash_state, view as dash_view
from athena.kernel import caps

NAME = "w"
PR_URL = "https://github.com/acme/web/pull/7"


class Reports(Case):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.path = self.worktree(self.repo_path)
        spawn_record(NAME, self.path, self.repo_path, block=False)  # no Rex place: its process is nobody's to judge, so the hook's word stands

    def head(self):
        return git(self.path, "rev-parse", "HEAD")

    def report(self, *args, cwd=None, worker=NAME):
        """The command as a worker runs it: from its worktree, its name in ATHENA_WORKER."""
        env = {**os.environ, "ATHENA_WORKER": worker}
        return subprocess.run([str(BIN), "report", *args], capture_output=True, text=True, env=env, cwd=str(cwd or self.path),
                              timeout=60)

    def prompt(self):
        return hooks.apply({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(self.path)}, NAME)

    def stop(self, message="Did the thing."):
        return hooks.apply({"hook_event_name": "Stop", "last_assistant_message": message, "cwd": str(self.path),
                            "session_id": "s1"}, NAME)

    def status_file(self):
        return store.read_dict(store.path("workers", f"{NAME}.json"))

    def on_github(self):
        git(self.path, "remote", "set-url", "origin", "git@github.com:acme/web.git")

    # -- the command

    def test_report_files_a_report_with_head_and_pr_filled_in(self):
        self.on_github()
        self.stub("gh", json.dumps({"url": PR_URL, "state": "OPEN"}))
        self.prompt()
        done = self.report("--status", "done", "--verify", "scripts/test.sh -> pass", "--decision", "kept it small",
                           "--concern", "follow-up: one", "--concern", "follow-up: two")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"report filed: DONE @ {self.head()[:12]}, pr {PR_URL}", done.stdout)
        filed = self.status_file()["filed"]
        self.assertEqual(filed["report"], {"status": "DONE", "pr": PR_URL, "head": self.head(), "verify": "scripts/test.sh -> pass",
                                           "decisions": ["kept it small"], "concerns": ["follow-up: one", "follow-up: two"]})
        self.assertIsNone(self.status_file().get("report"), "not the report until its Stop takes it in")
        self.assertTrue(any(c.startswith("gh pr view t/w --repo acme/web") for c in self.world_calls()), self.world_calls())

    def test_report_off_github_asks_no_one_and_leaves_pr_empty(self):
        out = self.report("--status", "BLOCKED", "--verify", "none", "--concern", "which ceiling?")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.status_file()["filed"]["report"]["pr"], "")
        self.assertEqual(self.world_calls(), [])

    def test_report_says_when_gh_cannot_say(self):
        self.on_github()
        script = self.stubs / "gh"
        script.write_text('#!/bin/sh\necho "gh $*" >> "$ATHENA_WORLD_CALLS"\necho "HTTP 502" >&2\nexit 1\n')
        out = self.report("--status", "IN_PROGRESS", "--verify", "smoke -> fail")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("pr none: gh could not say", out.stdout)

    def test_report_refuses_outside_a_worker_and_files_no_done_the_gate_would_refuse(self):
        outside = self.report("--status", "BLOCKED", "--verify", "none", worker="")
        self.assertNotEqual(outside.returncode, 0)
        self.assertIn("ATHENA_WORKER", outside.stderr)
        failing = self.report("--status", "DONE", "--verify", "scripts/test.sh -> fail")
        self.assertEqual(failing.returncode, 1)
        self.assertIn("not filed", failing.stdout)
        self.assertIn("VERIFY", failing.stdout)
        self.assertNotIn("filed", self.status_file())
        bad = self.report("--status", "MAYBE", "--verify", "x -> pass")
        self.assertEqual(bad.returncode, 2)

    def test_report_keeps_what_the_hook_wrote(self):
        hooks.apply({"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}, NAME)
        self.prompt()
        self.report("--status", "IN_PROGRESS", "--verify", "smoke -> fail")
        record = self.status_file()
        self.assertEqual((record["state"], record["session_id"]), ("working", "s1"))

    # -- the gate

    def test_the_gate_accepts_a_turn_whose_report_was_filed_this_turn(self):
        self.prompt()
        self.report("--status", "DONE", "--verify", "scripts/test.sh -> pass", "--summary", "Moved reports out of the chat.")
        out = self.stop("All done: the report is filed.")
        self.assertIsNone(out["gate"])
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["report"]["head"], self.head())
        self.assertEqual(out["summary"], "Moved reports out of the chat.")
        self.assertNotIn("filed", self.status_file(), "a Stop takes the filed report in")

    def test_without_a_summary_the_last_message_is_the_summary(self):
        self.prompt()
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", "which ceiling?")
        out = self.stop("Blocked on which ceiling to use.")
        self.assertEqual((out["state"], out["summary"]), ("blocked", "Blocked on which ceiling to use."))

    def test_the_gate_refuses_a_turn_with_no_report_filed(self):
        self.prompt()
        out = self.stop("I think I am done.")
        self.assertEqual(out["gate"]["decision"], "block")
        self.assertIn("athena report", out["gate"]["reason"])
        self.assertNotIn("ATHENA-REPORT {", out["gate"]["reason"], "the gate never asks for the typed line")

    def test_a_worker_started_before_the_command_is_still_shown_the_typed_line(self):
        """Its settings, written at its spawn, have no rule for the command: it would stop at a permission prompt running it."""
        target = store.ensure("settings") / f"{NAME}.json"
        target.write_text(json.dumps({"permissions": {"allow": ["Bash(athena whoami:*)"]}}))
        self.prompt()
        old = self.stop("Still working.")["gate"]["reason"]
        self.assertIn("athena report", old)
        self.assertIn("ATHENA-REPORT {", old)
        typed_done = {"status": "DONE", "pr": "", "head": self.head(), "verify": "smoke -> fail"}
        refused = self.stop("Done.\nATHENA-REPORT " + json.dumps(typed_done))["gate"]["reason"]
        self.assertIn("VERIFY", refused)
        self.assertIn("ATHENA-REPORT {", refused, "an old worker's typed DONE, sent back, is told it may type the line again")
        target.write_text(json.dumps({"permissions": {"allow": ["Bash(athena report:*)"]}}))
        self.assertNotIn("ATHENA-REPORT {", self.stop("Still working.")["gate"]["reason"])

    def test_a_report_counts_for_one_stop_only(self):
        """Another Stop hook (the /goal one) can keep the turn going after the gate took the report in: the next Stop needs a new one."""
        self.prompt()
        self.report("--status", "IN_PROGRESS", "--verify", "smoke -> fail")
        self.assertIsNone(self.stop()["gate"])
        self.assertEqual(self.stop()["gate"]["decision"], "block")

    def test_a_report_from_before_a_new_prompt_is_not_this_turns(self):
        self.prompt()
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", "stale")
        self.prompt()  # interrupted before its Stop, then a new prompt
        self.assertEqual(self.stop()["gate"]["decision"], "block")

    def test_a_report_filed_survives_other_hook_events_before_its_stop(self):
        self.prompt()
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", "q")
        hooks.apply({"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "waiting"}, NAME)
        hooks.apply({"hook_event_name": "SessionStart", "session_id": "s1", "source": "compact"}, NAME)
        self.assertEqual(self.stop()["state"], "blocked")

    def test_a_filed_report_and_background_work_each_survive_the_rebuild_and_clear_on_their_own_rules(self):
        """Both ride across every hook event's rebuild of the status file: a SubagentStop keeps the filed report and counts the
        background; the Stop takes the report in and counts again; a failed turn drops the count and keeps a report not yet taken
        in; a new prompt drops both."""
        running = [{"id": "w1", "type": "workflow", "status": "running"}]
        self.prompt()
        self.report("--status", "IN_PROGRESS", "--verify", "smoke -> fail", "--concern", "waiting on the workflow")
        hooks.apply({"hook_event_name": "SubagentStop", "background_tasks": running, "session_id": "s1"}, NAME)
        record = self.status_file()
        self.assertEqual((record["background"], record["filed"]["report"]["status"]), (1, "IN_PROGRESS"))
        out = hooks.apply({"hook_event_name": "Stop", "last_assistant_message": "Waiting on it.", "cwd": str(self.path),
                           "session_id": "s1", "background_tasks": running}, NAME)
        self.assertIsNone(out["gate"])
        record = self.status_file()
        self.assertEqual((record["state"], record["report"]["status"], record["background"]), ("idle", "IN_PROGRESS", 1))
        self.assertNotIn("filed", record)
        row = status.collect([NAME])[0]
        self.assertEqual((row["state"], row["state_source"], row["report"]["status"]), ("working", "background", "IN_PROGRESS"))

        self.prompt()
        self.assertEqual(self.status_file()["background"], 0)
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", "q")
        hooks.apply({"hook_event_name": "SubagentStop", "background_tasks": running, "session_id": "s1"}, NAME)
        hooks.apply({"hook_event_name": "StopFailure", "session_id": "s1"}, NAME)
        record = self.status_file()
        self.assertEqual((record["background"], record["filed"]["report"]["status"]), (0, "BLOCKED"))
        self.prompt()
        record = self.status_file()
        self.assertEqual(record["background"], 0)
        self.assertNotIn("filed", record)

    def test_done_without_verify_passing_at_head_is_still_refused(self):
        self.prompt()
        self.assertEqual(self.report("--status", "DONE", "--verify", "scripts/test.sh -> pass").returncode, 0)
        git(self.path, "commit", "-q", "--allow-empty", "-m", "after the report")
        out = self.stop()
        self.assertEqual(out["gate"]["decision"], "block")
        self.assertIn("differs", out["gate"]["reason"])
        self.assertIsNone(out["report"], "a refused DONE is never the report")
        # A DONE whose VERIFY did not pass, written to the file by hand, is refused at the Stop too.
        hooks.file_report(NAME, {"status": "DONE", "verify": "scripts/test.sh -> fail", "head": self.head()})
        out = self.stop()
        self.assertIn("VERIFY", out["gate"]["reason"])
        self.assertEqual(out["state"], "working")

    def test_done_on_github_with_no_open_pr_is_refused(self):
        self.on_github()
        script = self.stubs / "gh"
        script.write_text('#!/bin/sh\necho "no pull requests found for branch \\"t/w\\"" >&2\nexit 1\n')
        out = self.report("--status", "DONE", "--verify", "x -> pass")
        self.assertEqual(out.returncode, 1)
        self.assertIn("needs your PR", out.stdout)

    # -- a typed line, from a worker started before the command

    def test_a_typed_line_still_reaches_the_same_file_and_readers(self):
        self.prompt()
        typed = {"status": "BLOCKED", "pr": "", "head": "", "verify": "none", "decisions": [], "concerns": ["typed"]}
        out = self.stop("Stuck.\nATHENA-REPORT " + json.dumps(typed))
        self.assertIsNone(out["gate"])
        self.assertEqual((out["state"], out["summary"]), ("blocked", "Stuck."))
        self.assertEqual(self.status_file()["report"], typed)
        row = status.collect()[0]
        self.assertEqual(row["report"]["concerns"], ["typed"])
        self.assertIn("blocked", status._line(row))

    def test_a_filed_report_wins_over_a_typed_line(self):
        self.prompt()
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", "filed")
        out = self.stop('x\nATHENA-REPORT {"status":"IN_PROGRESS","concerns":["typed"]}')
        self.assertEqual(out["report"]["concerns"], ["filed"])

    # -- the readers: status, watch, review and the dash

    def _filed_and_stopped(self, *args):
        self.prompt()
        out = self.report(*args)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNone(self.stop("Short summary.")["gate"])

    def test_status_shows_a_report_filed_with_the_command(self):
        self._filed_and_stopped("--status", "BLOCKED", "--verify", "none", "--concern", "which ceiling?")
        row = status.collect([NAME])[0]
        self.assertEqual((row["state"], row["report"]["concerns"]), ("blocked", ["which ceiling?"]))
        detail = "\n".join(status._detail(row))
        self.assertIn("concern: which ceiling?", detail)
        self.assertIn("last words: Short summary.", detail)

    def _transcript(self, *entries):
        root = self.tmp / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(root)
        folder = root / "projects" / "".join(c if c.isalnum() else "-" for c in str(self.path))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "s1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

    def _quiet_with_a_filed_report(self, concern):
        self.prompt()
        self.report("--status", "BLOCKED", "--verify", "none", "--concern", concern)
        target = store.path("workers", f"{NAME}.json")
        store.write_json(target, {**store.read_dict(target), "ts": time.time() - 600})

    def test_status_reads_a_filed_report_whose_stop_never_landed(self):
        self._quiet_with_a_filed_report("lost stop")
        self._transcript({"type": "assistant", "message": {"content": [{"type": "text", "text": "Blocked on the ceiling."}]}})
        row = status.collect()[0]
        self.assertEqual((row["state"], row["state_source"]), ("blocked", "filed"))
        self.assertEqual(row["report"]["concerns"], ["lost stop"])

    def test_a_filed_report_before_a_long_tool_call_is_still_mid_turn(self):
        self._quiet_with_a_filed_report("early")
        tool_use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 900"}}]}}
        self._transcript(tool_use)
        row = status.collect()[0]
        self.assertEqual((row["state"], row["report"]), ("working", None))

    def test_watch_tells_a_report_filed_with_the_command(self):
        before = watch.snapshot(caps.CTX, {}, time.time(), fetch=False)
        self._filed_and_stopped("--status", "BLOCKED", "--verify", "none", "--concern", "which ceiling?")
        now = watch.snapshot(caps.CTX, before, time.time(), fetch=False)
        lines = [line.text for line in watch.diff(before, now)]
        self.assertIn(f"{NAME} report BLOCKED", lines)

    def test_review_reads_a_done_filed_with_the_command(self):
        self._filed_and_stopped("--status", "DONE", "--verify", "scripts/test.sh -> pass")
        rows = status.collect()
        pr = {"state": "OPEN", "checks": "success", "review": "", "head": self.head(), "requested": [], "branch": "t/w",
              "url": PR_URL, "number": 7}
        moves = prs.snapshot_workers(rows, False, {NAME: {"pr": pr}})
        self.assertEqual(moves[NAME]["move"]["what"], "in review · athena")

    def test_the_dash_shows_a_report_filed_with_the_command(self):
        self._filed_and_stopped("--status", "DONE_WITH_CONCERNS", "--verify", "scripts/test.sh -> pass",
                                "--concern", "CI not checked on the new head")
        snap = dash_state.Store(store.state_dir()).load()
        self.assertEqual(snap.hooks[NAME].report.status, "DONE_WITH_CONCERNS")
        built = dash_view.build(snap, time.time())
        [mine] = [i for i in built.in_progress if i.name == NAME]
        self.assertEqual((mine.label, mine.detail), ("done with concerns, athena reviewing", "Short summary."))


class ShippedText(Case):
    def test_spawn_goal_names_the_command_not_the_line(self):
        goal = spawn.goal_text("X is done when tests pass.", "/b.md")
        self.assertIn("athena report", goal)
        self.assertNotIn("ATHENA-REPORT", goal)

    def test_brief_check_flags_a_goal_asking_for_the_typed_line(self):
        found = brief.check_goal("X is done when (4) the final message ends with an ATHENA-REPORT line.")
        self.assertTrue(any("athena report" in f for f in found), found)
        self.assertEqual(brief.check_goal("X is done when (4) a report is filed with athena report."), [])

    def test_no_shipped_text_tells_a_worker_to_type_the_line(self):
        root = BIN.parent.parent
        for rel in ("prompts/worker.md", "prompts/hq.md", "plugin/skills/athena/SKILL.md"):
            text = (root / rel).read_text()
            with self.subTest(rel):
                self.assertNotIn("ATHENA-REPORT {", text)
                self.assertNotRegex(text, r"(?i)(end|finish)[^.\n]{0,40}with (an|exactly one|one)[^.\n]{0,20}ATHENA-REPORT")
        self.assertIn("athena report --status", (root / "prompts/worker.md").read_text())
        self.assertIn("athena report", (root / "plugin/skills/athena/SKILL.md").read_text())
        self.assertIn("## `athena report`", (root / "docs/commands.md").read_text())

    def test_workers_may_run_the_command_without_asking(self):
        from athena.caps import profiles
        self.assertIn("Bash(athena report:*)", profiles.permissions("worker")["allow"])
