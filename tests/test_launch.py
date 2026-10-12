import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.test_herdr import logged_calls, write_scenario
from athena_lib import cli, config, hq, launch, paths

CREATED = {"result": {"workspace": {"workspace_id": "w1"}, "root_pane": {"pane_id": "w1:p1", "terminal_id": "t1"}}}
STOPPED = {"status": "not_running", "running": False}
RUNNING = {"status": "running", "running": True, "version": "0.9.3"}


class LaunchTest(unittest.TestCase):
    """`athena` from a plain terminal, against the fake herdr."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.saved_herdr_env = os.environ.pop("HERDR_ENV", None)
        self.projects = Path(self.tmp.name) / "code"
        self.projects.mkdir()
        config.write({**config.DEFAULTS, "projects_dir": str(self.projects)})
        self.patches = [mock.patch.object(hq.board, "start_watcher"), mock.patch.object(hq.dash, "start")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self.saved_herdr_env is not None:
            os.environ["HERDR_ENV"] = self.saved_herdr_env
        self.env.restore()
        self.tmp.cleanup()

    def athena(self, *argv, tty=True):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: tty
        with mock.patch.object(launch.sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(launch.os, "execvp") as execvp, mock.patch.object(launch.time, "sleep"), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue(), execvp

    def test_a_plain_terminal_starts_herdr_opens_hq_and_attaches(self):
        write_scenario(self.tmp.name, [
            {"match": ["status", "server", "--json"], "stdout": STOPPED, "times": 1},
            {"match": ["status", "server", "--json"], "stdout": RUNNING},
            {"match": ["workspace", "create"], "stdout": CREATED},
        ])
        code, out, err, execvp = self.athena()
        self.assertEqual(code, 0, err)
        calls = logged_calls(self.tmp.name)
        verbs = [c[:2] for c in calls]
        self.assertIn(["server"], [c[:1] for c in calls])
        self.assertLess(verbs.index(["server"]), verbs.index(["workspace", "create"]))
        create = calls[verbs.index(["workspace", "create"])]
        self.assertEqual(create[create.index("--cwd") + 1], os.path.realpath(self.projects))
        self.assertIn("--focus", create)
        herdr = str(helpers.ROOT / "tests" / "fake_herdr.py")
        execvp.assert_called_once_with(herdr, [herdr])
        self.assertIn("starting herdr", out)
        self.assertIn("attaching to herdr", out)

    def test_a_running_server_is_reused(self):
        write_scenario(self.tmp.name, [
            {"match": ["status", "server", "--json"], "stdout": RUNNING},
            {"match": ["workspace", "create"], "stdout": CREATED},
        ])
        code, _, err, execvp = self.athena("hq")
        self.assertEqual(code, 0, err)
        self.assertNotIn(["server"], [c[:1] for c in logged_calls(self.tmp.name)])
        execvp.assert_called_once()

    def test_an_existing_hq_is_focused_then_attached(self):
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w1:p1", "workspace": "w1"}))
        write_scenario(self.tmp.name, [
            {"match": ["status", "server", "--json"], "stdout": RUNNING},
            {"match": ["agent", "get"], "stdout": {"result": {"agent": {"name": "athena"}}}},
        ])
        code, _, err, execvp = self.athena()
        self.assertEqual(code, 0, err)
        self.assertIn(["workspace", "focus", "w1"], logged_calls(self.tmp.name))
        execvp.assert_called_once()

    def test_inside_herdr_it_behaves_as_before(self):
        self.env.set("HERDR_ENV", "1")
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        code, out, err, execvp = self.athena("hq")
        self.assertEqual(code, 0, err)
        execvp.assert_not_called()
        self.assertNotIn(["status", "server", "--json"], logged_calls(self.tmp.name))
        self.assertEqual(json.loads(out)["pane"], "w1:p1")

    def test_without_a_terminal_it_never_attaches(self):
        # A herdr key binding runs `athena hq` with no tty and, possibly, no HERDR_ENV.
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])
        code, out, err, execvp = self.athena("hq", tty=False)
        self.assertEqual(code, 0, err)
        execvp.assert_not_called()
        self.assertNotIn(["server"], [c[:1] for c in logged_calls(self.tmp.name)])

    def test_no_attach(self):
        write_scenario(self.tmp.name, [
            {"match": ["status", "server", "--json"], "stdout": RUNNING},
            {"match": ["workspace", "create"], "stdout": CREATED},
        ])
        code, _, err, execvp = self.athena("hq", "--no-attach")
        self.assertEqual(code, 0, err)
        execvp.assert_not_called()

    def test_a_server_that_never_comes_up_is_an_error(self):
        write_scenario(self.tmp.name, [{"match": ["status", "server", "--json"], "stdout": STOPPED}])
        with mock.patch.object(launch.time, "monotonic", side_effect=[0, 0, 100]):
            code, _, err, execvp = self.athena()
        self.assertEqual(code, 2)
        self.assertIn("herdr server did not start", err)
        execvp.assert_not_called()


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.env.set("HERDR_ENV", "1")
        for var in ("ATHENA_WORKTREE_ROOT", "ATHENA_QUOTA_FILE"):
            os.environ.pop(var, None)
        self.patches = [mock.patch.object(hq.board, "start_watcher"), mock.patch.object(hq.dash, "start")]
        for p in self.patches:
            p.start()
        write_scenario(self.tmp.name, [{"match": ["workspace", "create"], "stdout": CREATED}])

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.restore()
        self.tmp.cleanup()

    def test_a_new_user_without_a_terminal_is_told_to_run_init(self):
        err = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), redirect_stderr(err):
            self.assertEqual(cli.main(["hq"]), 2)
        self.assertIn("run `athena init` first", err.getvalue())
        self.assertEqual(logged_calls(self.tmp.name), [])

    def test_a_new_user_at_a_terminal_runs_init_first(self):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(cli.init, "main", return_value=1) as init_main, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([]), 1)
        init_main.assert_called_once_with([])
        self.assertEqual(logged_calls(self.tmp.name), [])

    def test_an_older_install_writes_todays_values_and_opens_hq(self):
        state = paths.ensure()
        (state / "ledger.jsonl").write_text("")
        err = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), redirect_stderr(err), \
                redirect_stdout(io.StringIO()), mock.patch.object(hq.os.path, "realpath", side_effect=lambda p: p):
            self.assertEqual(cli.main(["hq"]), 0)
        self.assertEqual(json.loads(config.config_file().read_text()), {"version": 1, **config.LEGACY})
        self.assertIn("from your current setup", err.getvalue())
        create = logged_calls(self.tmp.name)[0]
        self.assertEqual(create[create.index("--cwd") + 1], os.path.expanduser("~/Developer"))

    def test_any_command_in_an_older_install_writes_the_config_and_moves_setup_scripts(self):
        # A spawn may come before any `athena hq`: it must still find the setup script the plugin no longer carries.
        (paths.ensure() / "ledger.jsonl").write_text("")
        root = Path(self.tmp.name) / "plugin"
        (root / "repos").mkdir(parents=True)
        (root / "repos" / "web.setup").write_text("echo web\n")
        err = io.StringIO()
        with mock.patch.object(paths, "PLUGIN_ROOT", root), redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["status"]), 0)
        self.assertEqual(json.loads(config.config_file().read_text()), {"version": 1, **config.LEGACY})
        self.assertEqual((config.config_dir() / "repos" / "web.setup").read_text(), "echo web\n")
        self.assertIn("moved web.setup", err.getvalue())


if __name__ == "__main__":
    unittest.main()
