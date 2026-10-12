import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from athena_lib import config, hookstatus, ledger, paths

HOOK = helpers.ROOT / "hooks" / "worker-status"


def run_hook(payload, env_extra=None, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([str(HOOK)], input=data, capture_output=True, text=True, env=env, cwd=cwd)


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
        wt = root / "proj" / "plt-2-y"
        wt.mkdir(parents=True)
        config.write({**config.DEFAULTS, "projects_dir": str(root.parent), "worktree_root": str(root)})
        ledger.append("spawn", "plt-2", path=os.path.realpath(wt))
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(wt)}, cwd=wt)
        self.assertEqual(self.status("plt-2")["state"], "working")
        other = Path(self.tmp.name) / "worktrees" / "proj" / "plt-1-x"
        run_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(other)}, cwd=other)
        self.assertIsNone(self.status("plt-1"))

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


if __name__ == "__main__":
    unittest.main()
