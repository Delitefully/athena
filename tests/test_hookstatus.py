import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import config, hookstatus, ledger, paths

HOOK = helpers.ROOT / "hooks" / "worker-status"


def run_hook(payload, env_extra=None, cwd=None, args=()):
    env = dict(os.environ)
    env.update(env_extra or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([str(HOOK), *args], input=data, capture_output=True, text=True, env=env, cwd=cwd)


class HookStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        self.wt.mkdir(parents=True)
        ledger.touch()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def status(self, name):
        f = paths.state_dir() / "workers" / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None

    def test_hook_noop_outside_worker(self):
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        proc = run_hook({"hook_event_name": "Stop", "cwd": str(other), "last_assistant_message": "hi"}, cwd=other)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertFalse((paths.state_dir() / "workers").exists())

    def test_worker_found_by_env(self):
        proc = run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.wt), "prompt": "secret text"},
                        {"ATHENA_WORKER": "abc-1"}, cwd=self.wt)
        self.assertEqual(proc.returncode, 0)
        st = self.status("abc-1")
        self.assertEqual(st["state"], "working")
        self.assertNotIn("secret", json.dumps(st))

    def test_worker_found_by_ledger_path(self):
        ledger.append("spawn", "abc-1", path=os.path.realpath(self.wt))
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.wt)}, cwd=self.wt)
        self.assertEqual(self.status("abc-1")["state"], "working")

    def test_worker_found_under_the_configs_worktree_root(self):
        os.environ.pop("ATHENA_WORKTREE_ROOT")
        root = Path(self.tmp.name) / "projects" / ".wt"
        wt = root / "proj" / "abc-2-y"
        wt.mkdir(parents=True)
        config.write({**config.DEFAULTS, "projects_dir": str(root.parent), "worktree_root": str(root)})
        ledger.append("spawn", "abc-2", path=os.path.realpath(wt))
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(wt)}, cwd=wt)
        self.assertEqual(self.status("abc-2")["state"], "working")
        other = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(other)}, cwd=other)
        self.assertIsNone(self.status("abc-1"))

    def python_stub(self):
        """An interpreter that fails loudly and leaves a mark, to prove the hook never started Python."""
        mark = Path(self.tmp.name) / "python-ran"
        stub = Path(self.tmp.name) / "python3"
        stub.write_text(f"#!/bin/sh\ntouch {mark}\necho 'python3 must not run here' >&2\nexit 99\n")
        stub.chmod(0o755)
        return stub, mark

    def test_a_non_worker_session_outside_the_root_never_starts_python(self):
        os.environ.pop("ATHENA_WORKTREE_ROOT")
        config.write({**config.DEFAULTS, "worktree_root": str(Path(self.tmp.name) / "worktrees")})
        stub, mark = self.python_stub()
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        proc = run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(other)},
                        {"ATHENA_PYTHON": str(stub), "PATH": f"{stub.parent}:/usr/bin:/bin"}, cwd=other)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(mark.exists())

    def test_an_older_install_reads_its_root_in_shell_before_any_config(self):
        os.environ.pop("ATHENA_WORKTREE_ROOT")
        config.sync_root_hint()  # what the first hook or athena command leaves behind
        self.assertFalse(config.exists())
        stub, mark = self.python_stub()
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(other)}, {"ATHENA_PYTHON": str(stub)}, cwd=other)
        self.assertFalse(mark.exists())

    def test_a_config_newer_than_the_root_file_falls_back_to_python(self):
        os.environ.pop("ATHENA_WORKTREE_ROOT")
        config.write({**config.DEFAULTS, "worktree_root": str(Path(self.tmp.name) / "worktrees")})
        hint = paths.state_dir() / "worktree-root"
        os.utime(hint, (1, 1))  # config.json edited by hand after the last write
        stub, mark = self.python_stub()
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(other)}, {"ATHENA_PYTHON": str(stub)}, cwd=other)
        self.assertTrue(mark.exists())

    def test_stop_with_report_sets_done(self):
        msg = 'All green.\nATHENA-REPORT {"status":"DONE","pr":"https://x/1","head":"abc","verify":"make test -> pass"}'
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": msg}, "abc-1")
        st = self.status("abc-1")
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["report"]["pr"], "https://x/1")

    def test_blocked_report(self):
        msg = 'ATHENA-REPORT {"status":"BLOCKED","concerns":["needs creds"]}'
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": msg}, "abc-1")
        self.assertEqual(self.status("abc-1")["state"], "blocked")

    def test_stop_without_report_is_idle(self):
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": "thinking about it"}, "abc-1")
        self.assertEqual(self.status("abc-1")["state"], "idle")

    def test_report_survives_later_prompt(self):
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": 'ATHENA-REPORT {"status":"DONE"}'}, "abc-1")
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        st = self.status("abc-1")
        self.assertEqual(st["state"], "working")
        self.assertIsNone(st["report"])
        self.assertEqual(st["last_report"]["status"], "DONE")

    def test_permission_prompt_blocked(self):
        hookstatus.apply({"hook_event_name": "Notification", "notification_type": "permission_prompt",
                          "message": "Claude needs your permission to use Bash"}, "abc-1")
        self.assertEqual(self.status("abc-1")["state"], "blocked")
        hookstatus.apply({"hook_event_name": "Notification", "message": "Claude needs your permission to use Edit"}, "abc-2")
        self.assertEqual(self.status("abc-2")["state"], "blocked")

    def test_idle_prompt_does_not_override_done(self):
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": 'ATHENA-REPORT {"status":"DONE"}'}, "abc-1")
        hookstatus.apply({"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "waiting"}, "abc-1")
        self.assertEqual(self.status("abc-1")["state"], "done")

    def test_a_resume_records_the_state_it_resumed_from(self):
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        hookstatus.apply({"hook_event_name": "SessionStart", "source": "resume", "session_id": "s2"}, "abc-1")
        st = self.status("abc-1")
        self.assertEqual((st["state"], st["source"], st["resumed_from"]), ("idle", "resume", "working"))
        hookstatus.apply({"hook_event_name": "SessionStart", "source": "startup"}, "abc-2")
        self.assertNotIn("resumed_from", self.status("abc-2"))
        hookstatus.apply({"hook_event_name": "UserPromptSubmit"}, "abc-1")
        self.assertNotIn("source", self.status("abc-1"))  # only the SessionStart record says how it started

    def test_session_end_exited(self):
        hookstatus.apply({"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}, "abc-1")
        self.assertEqual(self.status("abc-1")["state"], "exited")

    def test_summary_truncated_to_300(self):
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": "x" * 1000}, "abc-1")
        self.assertEqual(len(self.status("abc-1")["summary"]), 300)

    def test_hook_noop_without_ledger(self):
        (paths.state_dir() / "ledger.jsonl").unlink()
        proc = run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.wt)}, {"ATHENA_WORKER": "abc-1"}, cwd=self.wt)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(self.status("abc-1"))

    def test_garbage_stdin_exits_zero(self):
        proc = run_hook("{not json", {"ATHENA_WORKER": "abc-1"}, cwd=self.wt)
        self.assertEqual(proc.returncode, 0)

    def test_parse_report_takes_last(self):
        text = 'ATHENA-REPORT {"status":"BLOCKED"}\nlater\nATHENA-REPORT {"status":"DONE"}'
        self.assertEqual(hookstatus.parse_report(text)["status"], "DONE")
        self.assertIsNone(hookstatus.parse_report("ATHENA-REPORT {broken"))



ARGS = ["--name", "abc-1", "--model", "opus", "--effort", "high", "--settings", "/s/abc-1.json"]


class ResumeHookTest(unittest.TestCase):
    """On SessionStart a worker tells herdr its whole resume command again: /clear and a resume start new sessions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        self.wt.mkdir(parents=True)
        ledger.append("spawn", "abc-1", pane="w9:p1", path=os.path.realpath(self.wt), model="opus", effort="high",
                      claude_args=ARGS)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def reports(self):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "report-agent-session"]]

    def test_session_start_registers_the_new_session_with_the_spawn_args(self):
        event = {"hook_event_name": "SessionStart", "session_id": "sid-2", "source": "clear", "cwd": str(self.wt)}
        hookstatus.register_resume(event, "abc-1", {"HERDR_PANE_ID": "w9:p1"})
        [report] = self.reports()
        self.assertEqual(report[2], "w9:p1")
        self.assertEqual(report[report.index("--source") + 1], "athena")
        self.assertEqual(report[report.index("--agent-session-id") + 1], "sid-2")
        self.assertEqual(report[report.index("--") + 1:], ["claude", "--resume", "sid-2", *ARGS])

    def test_the_hook_registers_a_bare_resumed_worker_found_by_its_cwd(self):
        # After herdr's bare `claude --resume`, the session has no ATHENA_WORKER: the ledger finds it by its cwd.
        payload = {"hook_event_name": "SessionStart", "session_id": "sid-3", "source": "resume", "cwd": str(self.wt)}
        proc = run_hook(payload, {"HERDR_PANE_ID": "w9:p1"}, cwd=self.wt)
        self.assertEqual(proc.returncode, 0)
        [report] = self.reports()
        self.assertEqual(report[report.index("--") + 1:], ["claude", "--resume", "sid-3", *ARGS])

    def test_only_in_the_workers_own_pane(self):
        event = {"hook_event_name": "SessionStart", "session_id": "sid-2"}
        hookstatus.register_resume(event, "abc-1", {"HERDR_PANE_ID": "w5:p2"})
        hookstatus.register_resume(event, "abc-1", {})
        self.assertEqual(self.reports(), [])

    def test_other_events_and_missing_sessions_register_nothing(self):
        hookstatus.register_resume({"hook_event_name": "Stop", "session_id": "sid-2"}, "abc-1", {"HERDR_PANE_ID": "w9:p1"})
        hookstatus.register_resume({"hook_event_name": "SessionStart"}, "abc-1", {"HERDR_PANE_ID": "w9:p1"})
        self.assertEqual(self.reports(), [])

    def test_an_older_record_rebuilds_its_args_from_model_effort_and_settings(self):
        ledger.append("spawn", "old-1", pane="w8:p1", path="/nowhere", model="sonnet", effort="medium")
        settings = paths.ensure("settings") / "old-1.json"
        settings.write_text("{}")
        hookstatus.register_resume({"hook_event_name": "SessionStart", "session_id": "sid-4"}, "old-1",
                                   {"HERDR_PANE_ID": "w8:p1"})
        [report] = self.reports()
        argv = report[report.index("--") + 1:]
        self.assertEqual(argv[:3], ["claude", "--resume", "sid-4"])
        self.assertEqual(argv[argv.index("--name") + 1], "old-1")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--settings") + 1], str(settings))
        self.assertTrue(argv[argv.index("--append-system-prompt-file") + 1].endswith("worker.md"))

    def test_the_hook_gives_herdr_two_seconds(self):
        with mock.patch.object(hookstatus.herdr, "register_resume") as register:
            hookstatus.register_resume({"hook_event_name": "SessionStart", "session_id": "sid-2"}, "abc-1",
                                       {"HERDR_PANE_ID": "w9:p1"})
        register.assert_called_once_with("w9:p1", "sid-2", ARGS, timeout=2)

    def test_a_herdr_error_never_breaks_the_hook(self):
        write_scenario(self.tmp.name, [{"match": ["pane"], "stdout": "", "stderr": "boom", "exit": 3}])
        hookstatus.register_resume({"hook_event_name": "SessionStart", "session_id": "sid-2"}, "abc-1",
                                   {"HERDR_PANE_ID": "w9:p1"})


HQ_ARGS = ["--name", "athena", "--append-system-prompt-file", "/p/hq.md", "--settings", "/s/hq.json"]


class HqResumeHookTest(unittest.TestCase):
    """HQ registers its own resume command on SessionStart too, from the args `athena hq` kept in hq.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        ledger.touch()
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w1:p1", "claude_args": HQ_ARGS}))
        self.home = Path(self.tmp.name) / "dev"
        self.home.mkdir()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def reports(self):
        return [c for c in logged_calls(self.tmp.name) if c[:2] == ["pane", "report-agent-session"]]

    def test_the_hook_registers_hq_from_outside_the_worktree_root(self):
        payload = {"hook_event_name": "SessionStart", "session_id": "hq-2", "source": "clear", "cwd": str(self.home)}
        proc = run_hook(payload, {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1"}, cwd=self.home, args=["SessionStart"])
        self.assertEqual(proc.returncode, 0)
        [report] = self.reports()
        self.assertEqual(report[2], "w1:p1")
        self.assertEqual(report[report.index("--") + 1:], ["claude", "--resume", "hq-2", *HQ_ARGS])
        self.assertFalse((paths.state_dir() / "workers").exists() and any((paths.state_dir() / "workers").iterdir()))

    def test_hqs_other_events_stay_in_shell(self):
        # Only SessionStart starts Python in HQ: hooks.json names it as the hook's argument.
        stub = Path(self.tmp.name) / "python"
        mark = Path(self.tmp.name) / "python-ran"
        stub.write_text(f"#!/bin/sh\ntouch {mark}\ncat >/dev/null\n")
        stub.chmod(0o755)
        os.environ.pop("ATHENA_WORKTREE_ROOT")
        config.write({**config.DEFAULTS, "worktree_root": str(Path(self.tmp.name) / "worktrees")})
        env = {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1", "ATHENA_PYTHON": str(stub)}
        for event in ("UserPromptSubmit", "Stop", "Notification", "SessionEnd"):
            run_hook({"hook_event_name": event, "cwd": str(self.home)}, env, cwd=self.home)
        self.assertFalse(mark.exists())
        run_hook({"hook_event_name": "SessionStart", "cwd": str(self.home)}, env, cwd=self.home, args=["SessionStart"])
        self.assertTrue(mark.exists())

    def test_hooks_json_names_session_start_alone(self):
        hooks = json.loads((helpers.ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for event, entries in hooks.items():
            command = entries[0]["hooks"][0]["command"]
            self.assertEqual(command.endswith(" SessionStart"), event == "SessionStart", event)

    def test_not_hq_or_not_its_pane_registers_nothing(self):
        event = {"hook_event_name": "SessionStart", "session_id": "hq-2"}
        hookstatus.register_hq_resume(event, {"HERDR_PANE_ID": "w1:p1"})
        hookstatus.register_hq_resume(event, {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w2:p1"})
        hookstatus.register_hq_resume({"hook_event_name": "Stop", "session_id": "hq-2"},
                                      {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1"})
        self.assertEqual(self.reports(), [])

    def test_hqs_start_is_recorded_for_doctor(self):
        hookstatus.register_hq_resume({"hook_event_name": "SessionStart", "session_id": "hq-2", "source": "resume"},
                                      {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1"})
        start = json.loads((paths.state_dir() / "hq-start.json").read_text())
        self.assertEqual((start["source"], start["session_id"]), ("resume", "hq-2"))
        (paths.state_dir() / "hq-start.json").unlink()
        hookstatus.register_hq_resume({"hook_event_name": "SessionStart", "session_id": "x", "source": "resume"},
                                      {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w9:p9"})  # not HQ's pane
        self.assertFalse((paths.state_dir() / "hq-start.json").exists())

    def hq_session(self, session):
        (paths.state_dir() / "hq.json").write_text(json.dumps({"pane": "w1:p1", "claude_args": HQ_ARGS,
                                                               "session": session}))

    def start(self, sid, source, pane="w1:p1"):
        hookstatus.register_hq_resume({"hook_event_name": "SessionStart", "session_id": sid, "source": source},
                                      {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": pane})

    def test_a_nested_claude_started_from_hqs_bash_is_never_hq(self):
        # `claude -p` from HQ's Bash tool inherits ATHENA_ROLE=hq and HERDR_PANE_ID: a startup next to HQ's known
        # session is that, not HQ.
        self.hq_session("hq-1")
        self.start("nested-9", "startup")
        self.assertEqual(self.reports(), [])
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "hq-1")
        self.assertFalse((paths.state_dir() / "hq-start.json").exists())

    def test_a_nested_resume_or_compaction_never_moves_hqs_session(self):
        # `claude -p --resume X` from HQ's Bash, or a nested session that compacts: same env, same pane, another id.
        for source in ("resume", "compact", "clear", "startup"):
            self.hq_session("hq-1")
            with mock.patch.object(hookstatus, "nested", return_value=True):  # another Claude above it
                self.start("nested-9", source)
            self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "hq-1", source)
        self.assertEqual(self.reports(), [])
        self.assertFalse((paths.state_dir() / "hq-start.json").exists())

    def test_hqs_own_clear_registers_its_new_id_with_herdr(self):
        self.hq_session("hq-1")
        with mock.patch.object(hookstatus, "nested", return_value=False):
            self.start("post-clear", "clear")
        (report,) = self.reports()
        self.assertEqual(report[report.index("--") + 1:report.index("--") + 3], ["claude", "--resume"])
        self.assertEqual(report[report.index("--") + 3], "post-clear")
        self.assertEqual(json.loads((paths.state_dir() / "hq-start.json").read_text())["session_id"], "post-clear")
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "hq-1")  # doctor's to record

    def test_a_nested_clear_is_not_hqs(self):
        self.hq_session("hq-1")
        with mock.patch.object(hookstatus, "nested", return_value=True):
            self.start("nested-9", "clear")
        self.assertEqual(self.reports(), [])
        self.assertFalse((paths.state_dir() / "hq-start.json").exists())

    def test_a_nested_clear_through_the_hook_entry_registers_nothing(self):
        # The whole hook, as Claude runs it: main() drops a nested HQ session before anything else.
        self.hq_session("hq-1")
        payload = {"hook_event_name": "SessionStart", "session_id": "nested-9", "source": "clear",
                   "cwd": str(self.home)}
        with mock.patch.object(hookstatus, "nested", return_value=True), \
                mock.patch.dict(os.environ, {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1"}), \
                mock.patch.object(hookstatus.sys, "stdin", io.StringIO(json.dumps(payload))):
            hookstatus.main()
        self.assertEqual(self.reports(), [])

    def test_hqs_own_resume_and_compaction_keep_its_id_and_count(self):
        for source in ("resume", "compact"):
            self.hq_session("hq-1")
            self.start("hq-1", source)
            self.assertEqual(json.loads((paths.state_dir() / "hq-start.json").read_text())["source"], source)
        self.assertEqual(len(self.reports()), 2)

    def test_the_known_session_or_the_first_one_is_hq(self):
        self.hq_session("hq-1")
        self.start("hq-1", "startup")
        self.hq_session(None)
        self.start("hq-new", "startup")
        self.assertEqual(len(self.reports()), 2)
        self.assertIsNone(json.loads((paths.state_dir() / "hq.json").read_text())["session"])  # the hook never writes it

    def test_another_pane_is_never_hq(self):
        self.hq_session("hq-1")
        self.start("hq-1", "resume", pane="w9:p1")
        self.assertEqual(self.reports(), [])
        self.assertEqual(json.loads((paths.state_dir() / "hq.json").read_text())["session"], "hq-1")

    def test_an_hq_json_without_args_registers_nothing(self):
        (paths.state_dir() / "hq.json").write_text(json.dumps({"pane": "w1:p1"}))
        hookstatus.register_hq_resume({"hook_event_name": "SessionStart", "session_id": "hq-2"},
                                      {"ATHENA_ROLE": "hq", "HERDR_PANE_ID": "w1:p1"})
        self.assertEqual(self.reports(), [])


class WhoamiTest(unittest.TestCase):
    """`athena whoami`: the worker this session is, for the mod when a bare resume dropped ATHENA_WORKER."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1-x"
        (self.wt / "src").mkdir(parents=True)
        ledger.append("spawn", "abc-1", pane="w9:p1", path=os.path.realpath(self.wt))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def whoami(self, cwd, extra=None):
        env = {k: v for k, v in os.environ.items() if k != "ATHENA_WORKER"}
        env.update(extra or {})
        return subprocess.run([str(helpers.ROOT / "bin" / "athena"), "whoami"], cwd=cwd, env=env,
                              capture_output=True, text=True)

    def test_a_live_workers_worktree_names_it(self):
        proc = self.whoami(self.wt / "src")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "abc-1"))

    def test_the_environment_wins(self):
        proc = self.whoami(self.tmp.name, {"ATHENA_WORKER": "abc-1"})
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "abc-1"))

    def test_elsewhere_is_no_worker(self):
        proc = self.whoami(self.tmp.name)
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))

    def test_a_retired_worker_is_no_worker(self):
        ledger.append("retire", "abc-1")
        self.assertEqual(self.whoami(self.wt).returncode, 1)

if __name__ == "__main__":
    unittest.main()
