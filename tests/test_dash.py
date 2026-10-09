import json
import os
import socket
import tempfile
import time
import unittest
import urllib.request

from tests import helpers
from athena_lib import dash, paths

PS = """\
    1     0 /sbin/launchd
 4587     1 herdr server
 4703  4587 -zsh
 4757  4703 claude --name athena --append-system-prompt-file /src/athena/hq.md --settings /s/hq.json
 5001  4587 -zsh
 5002  5001 claude --name athena-dash --model opus --append-system-prompt-file /src/athena/worker.md
 6001  4587 claude --name athena --append-system-prompt-file /src/athena/hq.md
 7001  4703 grep claude --name athena hq.md
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HqPidTest(unittest.TestCase):
    def test_watches_the_hq_pane_shell(self):
        self.assertEqual(dash.find_hq(PS, "athena"), 4703)

    def test_never_a_workers_claude(self):
        self.assertIsNone(dash.find_hq(PS, "athena-dash"))
        self.assertIsNone(dash.find_hq(PS, "nobody"))

    def test_claude_itself_when_its_parent_is_herdr(self):
        only = "\n".join(line for line in PS.splitlines() if not line.strip().startswith(("4757", "4703")))
        self.assertEqual(dash.find_hq(only, "athena"), 6001)


class SourceHashTest(unittest.TestCase):
    def test_hash_covers_the_server_but_not_its_tests(self):
        files = [p.relative_to(dash.SRC).as_posix() for p in dash.sources()]
        self.assertIn("main.go", files)
        self.assertIn("static/style.css", files)
        self.assertNotIn("server_test.go", files)
        self.assertFalse(any(f.startswith("testdata/") for f in files))
        self.assertEqual(dash.source_hash(), dash.source_hash())


@unittest.skipUnless(dash.go_bin(), "go is not installed")
class LifecycleTest(unittest.TestCase):
    """Builds and runs the real server on a free port against a temp state dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.port = free_port()
        self.env.set("ATHENA_DASH_PORT", str(self.port))
        self.env.set("ATHENA_HQ", "athena-test-no-such-hq")
        paths.ensure("workers")

    def tearDown(self):
        dash.stop()
        self.env.restore()
        self.tmp.cleanup()

    def fetch(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.read().decode()

    def test_on_show_off(self):
        self.assertIsNone(dash.running())
        first = dash.start()
        self.assertTrue(first["started"])
        self.assertIsNone(first["hq_pid"])
        self.assertEqual(first["url"], f"http://127.0.0.1:{self.port}/")
        self.assertEqual(json.loads(self.fetch("/healthz"))["pid"], first["pid"])
        self.assertIn("Needs you", self.fetch("/"))

        again = dash.start()
        self.assertFalse(again["started"], "a second on must not start a second server")
        self.assertEqual(again["pid"], first["pid"])
        self.assertEqual(dash.running()["pid"], first["pid"])

        stopped = dash.stop()
        self.assertEqual(stopped["pid"], first["pid"])
        self.assertIsNone(dash.running())
        with self.assertRaises(OSError):
            os.kill(first["pid"], 0)
        self.assertIsNone(dash.stop(), "off is idempotent")

    def test_port_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", self.port))
            s.listen()
            with self.assertRaises(dash.DashError) as ctx:
                dash.start()
        self.assertIn(f"port {self.port} is in use", str(ctx.exception))
        self.assertIsNone(dash.running())
        self.assertFalse((paths.state_dir() / "dash.pid").exists())

    def test_stale_pid_file_is_ignored(self):
        (paths.ensure() / "dash.pid").write_text(json.dumps({"pid": os.getpid(), "port": self.port}))
        self.assertIsNone(dash.running(), "a live pid that is not our server is not running")

    def test_server_exits_when_hq_goes(self):
        sleeper = __import__("subprocess").Popen(["sleep", "30"])
        try:
            rec = dash.start(hq=sleeper.pid)
            self.assertEqual(rec["hq_pid"], sleeper.pid)
        finally:
            sleeper.kill()
            sleeper.wait()
        deadline = time.monotonic() + 5
        while dash.running() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(dash.running())


if __name__ == "__main__":
    unittest.main()
