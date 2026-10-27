"""athena doctor against a real herdr server in a temporary session (its own config and state dirs, never the
user's): the server, the dashboard and the board watcher stopped, doctor brings each back, and leaves them alone
once they run. Skipped where herdr or Go is not installed."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import board, dash, doctor, launch

HERDR = shutil.which("herdr")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HERDR and dash.go_bin(), "needs herdr and Go")
class RealHerdrTest(unittest.TestCase):
    def setUp(self):
        # herdr's socket lives under XDG_CONFIG_HOME, and a unix socket path is short: keep the root short too.
        self.root = Path(tempfile.mkdtemp(prefix="adr", dir="/tmp"))
        self.env = helpers.isolated_env(str(self.root))
        (self.root / "cfg" / "herdr").mkdir(parents=True)
        (self.root / "cfg" / "herdr" / "config.toml").write_text("[server]\nheadless_cols = 120\nheadless_rows = 40\n")
        wrapper = self.root / "h"
        wrapper.write_text(f'#!/bin/sh\nexec env XDG_CONFIG_HOME="{self.root}/cfg" XDG_STATE_HOME="{self.root}/xs" '
                           f'"{HERDR}" --session athena-doctor-test "$@"\n')
        wrapper.chmod(0o755)
        self.herdr = str(wrapper)
        self.env.set("ATHENA_HERDR", self.herdr)
        self.env.set("ATHENA_DASH_PORT", str(free_port()))
        self.env.set("ATHENA_DOCTOR_GRACE", "0")
        self.env.set("ATHENA_HQ", "athena-doctor-test-no-hq")
        for p in (mock.patch.object(doctor, "memory_free_percent", return_value=50),
                  mock.patch.object(doctor.shutil, "disk_usage", return_value=mock.Mock(free=500e9))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        dash.stop()
        board.stop_watcher()
        subprocess.run([self.herdr, "server", "stop"], capture_output=True, timeout=30)
        self.env.restore()
        shutil.rmtree(self.root, ignore_errors=True)

    def wait(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return predicate()

    def test_doctor_brings_back_herdr_the_dash_and_the_board_and_then_leaves_them_alone(self):
        self.assertFalse(launch.server_running())
        first = doctor.run(fix=True)
        self.assertEqual({c for c, v in first["checks"].items() if v.get("fixed")}, {"herdr", "dash", "board"}, first)
        self.assertTrue(launch.server_running())
        self.assertIn("athena-doctor-test", subprocess.run([self.herdr, "status", "server"], capture_output=True,
                                                           text=True).stdout)
        self.assertTrue(self.wait(lambda: dash._health(dash.port())))
        self.assertFalse(dash.running()["hq_pid"])  # bound to no HQ: it outlives HQ
        watcher = board.running_pid()
        self.assertTrue(watcher)

        again = doctor.run(fix=True)
        self.assertEqual(again["actions"], [])
        self.assertTrue(again["ok"], again)
        self.assertEqual(board.running_pid(), watcher)

        # Everything stops: the server (as a crash or a terminal quit would take it), the dashboard and the board.
        subprocess.run([self.herdr, "server", "stop"], capture_output=True, timeout=30)
        self.assertTrue(self.wait(lambda: not launch.server_running()))
        dash.stop()
        board.stop_watcher()
        self.assertTrue(self.wait(lambda: not board.running_pid()))
        # Three minutes on: past the backoff that follows the first run's repairs (they count for an hour).
        with mock.patch.object(doctor.time, "time", return_value=time.time() + 180):
            third = doctor.run(fix=True)
        self.assertEqual({c for c, v in third["checks"].items() if v.get("fixed")}, {"herdr", "dash", "board"}, third)
        self.assertTrue(launch.server_running())
        self.assertTrue(self.wait(lambda: dash._health(dash.port())))
        health = json.loads((Path(os.environ["ATHENA_STATE_DIR"]) / "health.json").read_text())
        self.assertTrue(health["ok"])
        self.assertEqual(len(health["actions"]), 3)


if __name__ == "__main__":
    unittest.main()
