"""The typing guard: athena never sends /exit (or anything) into an input box holding text the human has not sent."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.gitrepo import git, make_repo
from tests.test_herdr import logged_calls, write_scenario
from tests.test_hq import HQ_SPACE, alive
from athena_lib import composer, hq, ledger, paths, retire

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TYPED = (FIXTURES / "prompt-typed.ansi").read_text()  # Claude Code 2.1.311 in herdr, `hello world` typed
EMPTY = (FIXTURES / "prompt-empty.ansi").read_text()  # the same box with its dim placeholder
RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 40 + "\x1b[0m"


class TypedTest(unittest.TestCase):
    def test_typed_text_is_read(self):
        self.assertEqual(composer.typed(TYPED), "hello world")

    def test_the_dim_placeholder_is_an_empty_box(self):
        self.assertIn("Try", EMPTY)
        self.assertEqual(composer.typed(EMPTY), "")

    def test_text_in_a_truecolour_with_a_2_in_it_is_not_dim(self):
        screen = "\n".join([RULE, "❯\xa0\x1b[38;2;200;200;200mfix the flag\x1b[0m", RULE])
        self.assertEqual(composer.typed(screen), "fix the flag")

    def test_a_cursor_drawn_over_the_placeholder_is_not_typing(self):
        screen = "\n".join([RULE, '❯\xa0\x1b[7mT\x1b[27m\x1b[2mry "create a util"\x1b[0m', RULE])
        self.assertEqual(composer.typed(screen), "")
        screen = "\n".join([RULE, '❯\xa0\x1b[0m\x1b[7m \x1b[0m\x1b[2mTry "create a util"\x1b[0m', RULE])
        self.assertEqual(composer.typed(screen), "")

    def test_a_cursor_after_typed_text_is_not_part_of_it(self):
        screen = "\n".join([RULE, "❯\xa0hello\x1b[7m \x1b[27m", RULE])
        self.assertEqual(composer.typed(screen), "hello")
        screen = "\n".join([RULE, "❯\xa0\x1b[38;2;7;7;7mhel\x1b[7ml\x1b[0mo", RULE])
        self.assertEqual(composer.typed(screen), "helo")  # the letter under the cursor is the one lost

    def test_a_message_over_several_lines_is_all_read(self):
        screen = "\n".join([RULE, "❯\xa0first line of a long", "  message the human typed", RULE])
        self.assertEqual(composer.typed(screen), "first line of a long message the human typed")

    def test_no_input_box_is_unknown(self):
        self.assertIsNone(composer.typed("Do you want to proceed?\n❯ 1. Yes\n  2. No\n"))
        self.assertIsNone(composer.typed(""))

    def test_the_last_box_counts(self):
        screen = "\n".join([RULE, "❯\xa0old scrollback", RULE, "output", RULE, "❯\xa0\x1b[2mTry this\x1b[0m", RULE])
        self.assertEqual(composer.typed(screen), "")


class GuardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def calls(self):
        return logged_calls(self.tmp.name)

    def exits(self, name):
        return [c for c in self.calls() if c[:3] == ["agent", "prompt", name] and c[3:] == ["/exit"]]


class ExitClaudeTest(GuardFixture):
    def test_reads_the_screen_with_its_styling(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "read", "w"], "stdout": TYPED}])
        self.assertTrue(composer.typing("w"))
        (read,) = [c for c in self.calls() if c[:2] == ["agent", "read"]]
        self.assertEqual(read[read.index("--format") + 1], "ansi")

    def test_no_exit_while_someone_is_typing(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get", "w"], "stdout": {"result": {"agent": {"name": "w"}}}},
                                       {"match": ["agent", "read", "w"], "stdout": TYPED}])
        self.assertFalse(retire.exit_claude("w", 0))
        self.assertEqual(self.exits("w"), [])

    def test_an_empty_box_gets_its_exit(self):
        gone = {"match": ["agent", "get", "w"], "after": ["agent", "prompt"], "stdout": "",
                "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1}
        write_scenario(self.tmp.name, [gone, {"match": ["agent", "get", "w"], "stdout": {"result": {"agent": {"name": "w"}}}},
                                       {"match": ["agent", "read", "w"], "stdout": EMPTY}])
        self.assertTrue(retire.exit_claude("w", 0))
        self.assertEqual(len(self.exits("w")), 1)


class RotateGuardTest(GuardFixture):
    def setUp(self):
        super().setUp()
        (paths.ensure() / "hq.json").write_text(json.dumps({"pane": "w8:p1", "workspace": "w8", "session": "old"}))
        for p in (mock.patch.object(hq.time, "sleep"), mock.patch.object(hq, "_say")):
            p.start()
            self.addCleanup(p.stop)

    def test_rotation_refuses_to_exit_hq_while_the_human_is_typing(self):
        write_scenario(self.tmp.name, [alive("old"), HQ_SPACE, {"match": ["agent", "read", "athena"], "stdout": TYPED}])
        with self.assertRaises(hq.RestartError) as caught:
            hq.rotate_now()
        self.assertIn("typing", str(caught.exception))
        self.assertEqual(self.exits("athena"), [])
        self.assertEqual([c for c in self.calls() if c[:2] == ["agent", "start"]], [])
        self.assertTrue(any(c[:2] == ["notification", "show"] for c in self.calls()))
        self.assertFalse(hq.lock_file().exists())

    def test_restart_refuses_too(self):
        write_scenario(self.tmp.name, [alive("old"), HQ_SPACE, {"match": ["agent", "read", "athena"], "stdout": TYPED}])
        with self.assertRaises(hq.RestartError):
            hq.restart_now(update=False)
        self.assertEqual(self.exits("athena"), [])

    def test_rotation_waits_for_the_box_to_clear(self):
        self.env.set("ATHENA_TYPING_WAIT", "30")
        screens = iter([TYPED, TYPED, EMPTY])
        with mock.patch.object(composer, "screen", side_effect=lambda name: next(screens, EMPTY)), \
                mock.patch.object(composer.time, "sleep"):
            self.assertTrue(composer.wait_clear("athena"))


class RetireGuardTest(GuardFixture):
    def test_retire_leaves_a_pane_someone_is_typing_in(self):
        repo, _ = make_repo(self.tmp.name)
        wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1"
        git(repo, "worktree", "add", "-q", "-b", "you/abc-1", str(wt), "main")
        ledger.append("spawn", "abc-1", repo=str(repo), path=os.path.realpath(wt), branch="you/abc-1",
                      workspace="w5", pane="w5:p1", terminal="term_1")
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get", "abc-1"], "stdout": {"result": {"agent": {"name": "abc-1"}}}},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(wt)}}}}},
            {"match": ["agent", "read", "abc-1"], "stdout": TYPED}])
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire("abc-1")
        self.assertIn("typing", str(caught.exception))
        self.assertEqual(self.exits("abc-1"), [])
        self.assertTrue(wt.is_dir())
        self.assertEqual(ledger.workers()["abc-1"]["state"], "live")


if __name__ == "__main__":
    unittest.main()
