"""The worker report gate: a worker's turn cannot end without an ATHENA-REPORT line, nor DONE without VERIFY passing
at its HEAD. BLOCKED and NEEDS_CONTEXT always end the turn; the gate gives way after a few blocks, so it never loops."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import logged_calls
from athena_lib import hookstatus, ledger, paths

HOOK = helpers.ROOT / "hooks" / "worker-status"


def report(**fields):
    return "Done.\nATHENA-REPORT " + json.dumps(fields)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.repo, _ = make_repo(self.tmp.name)
        self.head = git(self.repo, "rev-parse", "HEAD")
        ledger.touch()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def stop(self, message, **extra):
        return {"hook_event_name": "Stop", "last_assistant_message": message, "cwd": str(self.repo), **extra}

    def status(self, name="abc-1", gated=True):
        f = paths.state_dir() / "workers" / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None

    def test_a_turn_without_a_report_is_blocked_and_told_to_write_one(self):
        data = hookstatus.apply(self.stop("I fixed the parser and pushed."), "abc-1", gated=True)
        decision = hookstatus.gate(data)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("ATHENA-REPORT", decision["reason"])
        self.assertEqual(self.status()["state"], "working")  # the turn goes on

    def test_a_broken_report_line_is_blocked_with_why(self):
        data = hookstatus.apply(self.stop('ATHENA-REPORT {"status":"DONE",}'), "abc-1", gated=True)
        decision = hookstatus.gate(data)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("JSON", decision["reason"])

    def test_blocked_and_needs_context_always_end_the_turn(self):
        for status in ("BLOCKED", "NEEDS_CONTEXT"):
            msg = report(status=status, concerns=["NEED: which host?"])
            data = hookstatus.apply(self.stop(msg), "abc-1", gated=True)
            self.assertIsNone(hookstatus.gate(data), status)
        self.assertEqual(self.status()["state"], "blocked")

    def test_done_with_verify_passing_at_head_ends_the_turn(self):
        msg = report(status="DONE", pr="https://example.com/o/r/pull/1", head=self.head[:9], verify="make test -> pass")
        data = hookstatus.apply(self.stop(msg), "abc-1", gated=True)
        self.assertIsNone(hookstatus.gate(data))
        self.assertEqual(self.status()["state"], "done")

    def test_done_without_verify_passing_is_blocked(self):
        for verify in ("make test -> fail", "", "make test -> not run", "make test -> pass|fail"):
            msg = report(status="DONE", head=self.head, verify=verify)
            data = hookstatus.apply(self.stop(msg), "abc-1", gated=True)
            decision = hookstatus.gate(data)
            self.assertEqual((decision or {}).get("decision"), "block", verify)
            self.assertIn("VERIFY", decision["reason"])
            hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")  # a new turn: a fresh count

    def test_done_at_a_head_that_is_not_head_is_blocked(self):
        msg = report(status="DONE_WITH_CONCERNS", head="0123456789abcdef", verify="make test -> pass")
        decision = hookstatus.gate(hookstatus.apply(self.stop(msg), "abc-1", gated=True))
        self.assertEqual(decision["decision"], "block")
        self.assertIn(self.head[:12], decision["reason"])

    def test_the_gate_gives_way_after_a_few_blocks_so_it_never_loops(self):
        decisions = []
        for _ in range(hookstatus.MAX_STOP_BLOCKS + 2):
            data = hookstatus.apply(self.stop("still thinking", stop_hook_active=True), "abc-1", gated=True)
            decisions.append(hookstatus.gate(data))
        blocked = [d for d in decisions if d]
        self.assertEqual(len(blocked), hookstatus.MAX_STOP_BLOCKS)
        self.assertIsNone(decisions[-1])
        self.assertEqual((self.status()["state"], self.status()["report"]), ("idle-unreported", None))

    def give_way(self):
        for _ in range(hookstatus.MAX_STOP_BLOCKS + 1):
            hookstatus.apply(self.stop("still thinking"), "abc-1", gated=True)
        self.assertEqual(self.status()["state"], "idle-unreported")

    def test_a_new_prompt_or_session_moves_a_worker_out_of_idle_unreported(self):
        self.give_way()
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        self.assertEqual(self.status()["state"], "working")
        self.give_way()
        hookstatus.apply({"hook_event_name": "SessionStart", "source": "resume"}, "abc-1")
        self.assertEqual(self.status()["state"], "idle")

    def test_the_idle_prompt_keeps_idle_unreported(self):
        """Claude Code's idle_prompt comes about a minute after the turn: it must not hide that no report came."""
        self.give_way()
        hookstatus.apply({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, "abc-1")
        self.assertEqual(self.status()["state"], "idle-unreported")

    def test_a_done_the_gate_gave_way_on_is_no_done(self):
        msg = report(status="DONE", head=self.head, verify="make test -> fail")
        for _ in range(hookstatus.MAX_STOP_BLOCKS + 1):
            data = hookstatus.apply(self.stop(msg), "abc-1", gated=True)
        self.assertIsNone(hookstatus.gate(data))
        st = self.status()
        self.assertEqual((st["state"], st["report"]), ("idle-unreported", None))

    def test_the_gate_comes_back_after_a_quiet_while(self):
        """Under /goal one prompt can last hours: a block long after the last one counts from one again."""
        with mock.patch.object(hookstatus.time, "time", return_value=1000.0):
            for _ in range(hookstatus.MAX_STOP_BLOCKS + 1):
                hookstatus.apply(self.stop("no report"), "abc-1", gated=True)
        later = 1000.0 + hookstatus.BLOCKS_FORGOTTEN_AFTER + 1
        with mock.patch.object(hookstatus.time, "time", return_value=later):
            data = hookstatus.apply(self.stop("no report"), "abc-1", gated=True)
        self.assertEqual(hookstatus.gate(data)["decision"], "block")

    def test_a_new_prompt_or_a_report_resets_the_count(self):
        for _ in range(hookstatus.MAX_STOP_BLOCKS):
            hookstatus.apply(self.stop("no report"), "abc-1", gated=True)
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        data = hookstatus.apply(self.stop("no report"), "abc-1", gated=True)
        self.assertEqual(hookstatus.gate(data)["decision"], "block")
        hookstatus.apply(self.stop(report(status="BLOCKED", concerns=["x"])), "abc-1", gated=True)
        self.assertEqual(self.status()["stop_blocks"], 0)

    def test_only_on_stop(self):
        event = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
        data = hookstatus.apply(event, "abc-1", gated=True)
        self.assertIsNone(hookstatus.gate(data))


class GateThroughTheHookTest(unittest.TestCase):
    """Through hooks/worker-status, as Claude Code runs it: the block decision is the hook's stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        self.wt.mkdir(parents=True)
        ledger.touch()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def run_hook(self, message, env_extra):
        env = dict(os.environ)
        env.update(env_extra)
        payload = {"hook_event_name": "Stop", "cwd": str(self.wt), "last_assistant_message": message}
        return subprocess.run([str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env,
                              cwd=self.wt)

    def test_a_worker_ending_its_turn_without_a_report_is_stopped_and_told_to_write_one(self):
        proc = self.run_hook("Pushed the fix; CI is running.", {"ATHENA_WORKER": "abc-1"})
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("ATHENA-REPORT", out["reason"])

    def test_a_worker_with_a_report_ends_its_turn(self):
        proc = self.run_hook(report(status="BLOCKED", concerns=["NEED: a sign-in"]), {"ATHENA_WORKER": "abc-1"})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_a_session_found_only_by_its_cwd_is_never_gated(self):
        """A plain Claude the human opened in a worker's worktree: no ATHENA_WORKER, so no gate."""
        ledger.append("spawn", "abc-1", path=os.path.realpath(self.wt))
        proc = self.run_hook("just chatting", {})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()


FAKE_PS = helpers.ROOT / "tests" / "fake_ps.py"
OWN, NESTED = "2", "2,3"  # FAKE_PS_CLAUDE: the hook runs in a top-level Claude, or in one another Claude started


class NestedSessionTest(unittest.TestCase):
    """A Claude the worker starts itself (a nested `claude -p` from its Bash tool, resumed or compacted or not)
    inherits ATHENA_WORKER and HERDR_PANE_ID, so neither tells it apart; its process ancestry does: another Claude
    runs above it. Its hook events are not the worker's: no gate, no status, no `exited`, no resume registration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        self.wt.mkdir(parents=True)
        ledger.append("spawn", "abc-1", pane="w9:p1", path=os.path.realpath(self.wt),
                      claude_args=["--name", "abc-1"])
        self.hook({"hook_event_name": "SessionStart", "session_id": "own-1", "source": "startup"})
        self.hook({"hook_event_name": "Stop", "session_id": "own-1",
                   "last_assistant_message": report(status="BLOCKED", concerns=["NEED: x"])})

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def hook(self, payload, where=OWN, other=None):
        env = {**os.environ, "ATHENA_WORKER": "abc-1", "HERDR_PANE_ID": "w9:p1", "ATHENA_PS": str(FAKE_PS),
               "FAKE_PS_CLAUDE": where, **(other or {})}
        payload = {"cwd": str(self.wt), **payload}
        return subprocess.run([str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env,
                              cwd=self.wt)

    def status(self):
        return json.loads((paths.state_dir() / "workers" / "abc-1.json").read_text())

    def registered(self, sid):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "report-agent-session"] and sid in c]

    def test_a_nested_session_is_never_gated_and_writes_nothing(self):
        before = self.status()
        proc = self.hook({"hook_event_name": "Stop", "session_id": "nested-9", "last_assistant_message": "hi"}, NESTED)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        proc = self.hook({"hook_event_name": "Stop", "session_id": "nested-9",
                          "last_assistant_message": report(status="DONE", head="abc", verify="x -> pass")}, NESTED)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.status(), before)  # no report, state, stop_blocks or summary of its own

    def test_a_nested_session_resumed_or_compacted_is_still_not_the_worker(self):
        for source in ("startup", "resume", "compact", "clear"):
            self.hook({"hook_event_name": "SessionStart", "session_id": f"nested-{source}", "source": source}, NESTED)
            self.hook({"hook_event_name": "SessionEnd", "session_id": f"nested-{source}", "reason": "other"}, NESTED)
            self.assertEqual(self.status()["state"], "blocked", source)
            self.assertEqual(self.status()["session_id"], "own-1", source)
            self.assertEqual(self.registered(f"nested-{source}"), [], source)  # never the pane's resume command
        proc = self.hook({"hook_event_name": "Stop", "session_id": "own-1", "last_assistant_message": "no report"})
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")  # the worker is still gated

    def test_a_nested_session_leaves_the_workers_block_count_alone(self):
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "own-1"})
        for _ in range(hookstatus.MAX_STOP_BLOCKS):
            self.hook({"hook_event_name": "Stop", "session_id": "nested-9", "last_assistant_message": "x"}, NESTED)
        proc = self.hook({"hook_event_name": "Stop", "session_id": "own-1", "last_assistant_message": "no report"})
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")

    def test_the_workers_own_new_session_is_the_worker(self):
        """/clear, a resume, a compaction or a fresh start in the worker's own Claude: any session id."""
        for i, source in enumerate(("clear", "resume", "compact", "startup")):
            sid = f"own-{i + 2}"
            self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": source})
            self.assertEqual(self.status()["session_id"], sid, source)
            proc = self.hook({"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": "no report"})
            self.assertEqual(json.loads(proc.stdout)["decision"], "block", source)

    def test_a_respawned_worker_is_the_worker_from_its_first_event(self):
        """retire, then spawn under the same name: the new Claude's SessionStart fires inside `herdr agent start`,
        before the new spawn record is written, with a new session id."""
        self.hook({"hook_event_name": "Stop", "session_id": "own-1", "last_assistant_message":
                   report(status="DONE", head="", verify="make test -> pass")})
        ledger.append("retire", "abc-1")
        self.hook({"hook_event_name": "SessionStart", "session_id": "life2-1", "source": "startup"},
                  other={"HERDR_PANE_ID": "w12:p1"})
        st = self.status()
        self.assertEqual((st["state"], st["session_id"]), ("idle", "life2-1"))
        ledger.append("spawn", "abc-1", pane="w12:p1", path=os.path.realpath(self.wt))
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "life2-1"}, other={"HERDR_PANE_ID": "w12:p1"})
        proc = self.hook({"hook_event_name": "Stop", "session_id": "life2-1", "last_assistant_message": "working"},
                         other={"HERDR_PANE_ID": "w12:p1"})
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")
        self.assertIsNone(self.status()["report"])  # never the old life's DONE

    def test_no_claude_found_counts_as_the_worker(self):
        """ps unreadable or no Claude in the ancestry (a hook run by hand): the worker's, as before."""
        for where, ps in (("", str(FAKE_PS)), (OWN, "/nonexistent/ps")):
            proc = self.hook({"hook_event_name": "Stop", "session_id": "x", "last_assistant_message": "no report"},
                             where, {"ATHENA_PS": ps})
            self.assertEqual(json.loads(proc.stdout)["decision"], "block")
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "x"}, where, {"ATHENA_PS": ps})


class HookCodeTest(unittest.TestCase):
    """The hook runs the plugin's own athena_lib, never one in its cwd (a worker inside an athena checkout)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        ledger.touch()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_an_athena_lib_in_the_cwd_is_never_imported(self):
        checkout = Path(self.tmp.name) / "worktrees" / "athena" / "branch"
        (checkout / "athena_lib").mkdir(parents=True)
        mark = Path(self.tmp.name) / "wrong-code-ran"
        (checkout / "athena_lib" / "__init__.py").write_text("")
        (checkout / "athena_lib" / "hookstatus.py").write_text(
            f"open({str(mark)!r}, 'w').write('x')\ndef main():\n    pass\n")
        env = dict(os.environ, ATHENA_WORKER="abc-1")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(checkout)}
        proc = subprocess.run([str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env,
                              cwd=checkout)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(mark.exists())
        self.assertEqual(json.loads((paths.state_dir() / "workers" / "abc-1.json").read_text())["state"], "working")
