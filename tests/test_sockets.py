"""The messaging-socket check: a live session whose socket file is gone can't be reached by SendMessage."""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import hookstatus, ledger, sockets, watch


class SessionsCase(unittest.TestCase):
    """A tmp state dir and a tmp ~/.claude/sessions; this test process is the live pid."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = helpers.isolated_env(self.root)
        self.env.set("ATHENA_HQ", "athena")
        self.env.set("CLAUDE_CONFIG_DIR", str(self.root / "claude"))
        (self.root / "claude" / "sessions").mkdir(parents=True)
        self.socks = self.root / "s"
        self.socks.mkdir()
        self.live = os.getpid()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def register(self, pid, name, sid=None, sock=None):
        entry = {"pid": pid, "sessionId": sid or f"sid-{name}", "name": name, "status": "idle",
                 "messagingSocketPath": str(sock or self.socks / f"{pid}.sock")}
        (self.root / "claude" / "sessions" / f"{pid}.json").write_text(json.dumps(entry))
        return entry["messagingSocketPath"]

    def bind(self, path):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))


class SocketCheckTest(SessionsCase):
    def test_socket_present_is_not_flagged(self):
        path = self.register(self.live, "abc-1")
        self.bind(path)
        self.assertIsNone(sockets.missing("abc-1"))

    def test_socket_missing_with_a_live_pid_is_flagged(self):
        path = self.register(self.live, "abc-1")
        self.assertEqual(sockets.missing("abc-1"), path)

    def test_a_plain_file_in_its_place_is_flagged(self):
        path = self.register(self.live, "abc-1")
        Path(path).write_text("")
        self.assertEqual(sockets.missing("abc-1"), path)

    def test_socket_missing_with_a_dead_pid_is_not_flagged(self):
        self.register(self.live, "abc-1")
        with mock.patch.object(sockets, "_pid_alive", return_value=False):
            self.assertIsNone(sockets.missing("abc-1"))

    def test_no_registry_entry_is_not_flagged(self):
        self.register(self.live, "someone-else")
        self.assertIsNone(sockets.missing("abc-1"))
        self.assertIsNone(sockets.missing("abc-1", session_id="sid-unknown"))

    def test_no_sessions_dir_is_not_flagged(self):
        (self.root / "claude" / "sessions").rmdir()
        self.assertIsNone(sockets.missing("abc-1"))

    def test_an_entry_without_a_socket_path_is_not_flagged(self):
        (self.root / "claude" / "sessions" / "1.json").write_text(json.dumps({"pid": self.live, "name": "abc-1"}))
        (self.root / "claude" / "sessions" / "2.json").write_text("not json")
        self.assertIsNone(sockets.missing("abc-1"))

    def test_matched_by_session_id_when_renamed(self):
        path = self.register(self.live, "renamed", sid="sid-7")
        self.assertEqual(sockets.missing("abc-1", session_id="sid-7"), path)

    def test_a_worker_is_found_by_its_hook_session_id(self):
        path = self.register(self.live, "renamed", sid="sid-7")
        hookstatus._write("abc-1", {"name": "abc-1", "state": "working", "session_id": "sid-7"})
        self.assertEqual(sockets.worker_missing("abc-1"), path)

    def test_hq_is_found_by_its_name_and_by_hq_json(self):
        path = self.register(self.live, "athena")
        self.assertEqual(sockets.hq_missing(), path)
        os.remove(self.root / "claude" / "sessions" / f"{self.live}.json")
        path = self.register(self.live, "other", sid="sid-hq")
        (self.root / "state" / "hq.json").write_text(json.dumps({"session": "sid-hq"}))
        self.assertEqual(sockets.hq_missing(), path)

    def test_status_line_for_hq(self):
        path = self.register(self.live, "athena")
        self.assertEqual(sockets.hq_status_line(),
                         f"hq            messaging socket missing ({path}): rotate with athena hq rotate")
        self.bind(path)
        self.assertIsNone(sockets.hq_status_line())


class SocketWatchTest(SessionsCase):
    """The watch says `<name> messaging socket missing` once per turn, not every poll."""

    def setUp(self):
        super().setUp()
        ledger.append("spawn", "abc-1", branch="b")

    def tick(self, prev):
        with mock.patch.object(watch.spawn, "deliver_pending", return_value=[]), \
                mock.patch.object(watch.status, "collect", side_effect=self.collect):
            return watch.step(prev, with_pr=False)

    def collect(self, w, with_pr=False, with_herdr=True):
        return {"name": w["name"], "state": "working", "pr": None, "report": None, "detail": None,
                "socket_missing": sockets.worker_missing(w["name"])}

    def socket_lines(self, lines):
        return [line for line in lines if "messaging socket" in line]

    def test_the_line_fires_only_on_a_transition(self):
        cur, lines = self.tick(None)
        self.assertEqual(self.socket_lines(lines), [])
        path = self.register(self.live, "abc-1")
        cur, lines = self.tick(cur)
        self.assertEqual(self.socket_lines(lines), ["abc-1 messaging socket missing"])
        cur, lines = self.tick(cur)
        self.assertEqual(self.socket_lines(lines), [])  # still missing: said once
        self.bind(path)
        cur, lines = self.tick(cur)
        self.assertEqual(self.socket_lines(lines), [])
        os.remove(path)
        cur, lines = self.tick(cur)
        self.assertEqual(self.socket_lines(lines), ["abc-1 messaging socket missing"])  # broken again: told again

    def test_hq_is_told_by_its_name_and_a_restarted_watch_tells_it(self):
        self.register(self.live, "athena")
        cur, lines = self.tick(None)
        self.assertEqual(self.socket_lines(lines), ["athena messaging socket missing"])
        cur, lines = self.tick(cur)
        self.assertEqual(self.socket_lines(lines), [])

    def test_the_line_wakes_hq_but_not_for_a_retiring_worker(self):
        line = "abc-1 messaging socket missing"
        self.assertEqual(watch.classify(line, ctx=watch.Context()), "wake")
        ledger.append("retire", "abc-1")
        self.assertEqual(watch.classify(line, ctx=watch.Context()), "quiet")


if __name__ == "__main__":
    unittest.main()
