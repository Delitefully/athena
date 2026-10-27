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
# Claude Code 2.1.312, a session started with --name (HQ and every worker): the name sits in the box's top border.
NAMED_EMPTY = (FIXTURES / "prompt-named-empty.ansi").read_text()
NAMED_TYPED = (FIXTURES / "prompt-named-typed.ansi").read_text()
RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 40 + "\x1b[0m"


class TypedTest(unittest.TestCase):
    def test_typed_text_is_read(self):
        self.assertEqual(composer.typed(TYPED), "hello world")

    def test_the_dim_placeholder_is_an_empty_box(self):
        self.assertIn("Try", EMPTY)
        self.assertEqual(composer.typed(EMPTY), "")

    def test_a_named_sessions_box_is_read(self):
        self.assertIn("abc-101", NAMED_EMPTY.splitlines()[0])
        self.assertEqual(composer.typed(NAMED_EMPTY), "")
        self.assertEqual(composer.typed(NAMED_TYPED), "fix the export flag")

    def test_a_rule_is_only_rules_or_a_label_set_in_one(self):
        self.assertTrue(composer._rule("─" * 12))
        self.assertTrue(composer._rule("─" * 12 + " abc-101 ─"))
        self.assertFalse(composer._rule("── abc ─"))
        self.assertFalse(composer._rule("─" * 12 + " text after a rule"))

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


class FailClosedTest(unittest.TestCase):
    """Only an input box seen empty is safe to type into: anything unread or unrecognised counts as typing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def screen_is(self, stdout, **rule):
        write_scenario(self.tmp.name, [{"match": ["agent", "read", "w"], "stdout": stdout, **rule}])
        return composer.typing("w")

    def test_an_unreadable_screen_is_typing(self):
        self.assertTrue(self.screen_is("", exit=1))
        self.assertTrue(self.screen_is(""))

    def test_bash_mode_is_typing_even_with_nothing_after_its_prompt(self):
        self.assertTrue(self.screen_is("\n".join([RULE, "!\xa0", RULE])))
        self.assertTrue(self.screen_is("\n".join([RULE, "!\xa0rm -rf build", RULE])))

    def test_a_dialog_is_typing(self):
        self.assertTrue(self.screen_is("Do you want to proceed?\n❯ 1. Yes\n  2. No\n"))

    def test_an_old_box_in_the_scrollback_does_not_count(self):
        screen = "\n".join([RULE, "❯\xa0\x1b[2mTry this\x1b[0m", RULE, "output", RULE, "!\xa0ls", RULE])
        self.assertIsNone(composer.typed(screen))
        self.assertTrue(self.screen_is(screen))

    def test_only_an_empty_box_is_safe(self):
        self.assertFalse(self.screen_is(EMPTY))

    def test_an_agent_herdr_does_not_run_has_no_one_typing(self):
        err = '{"error":{"code":"agent_not_found","message":"agent target w not found"}}'
        self.assertFalse(self.screen_is("", stderr=err, exit=1))
        other = '{"error":{"code":"cli_timeout","message":"slow"}}'
        self.assertTrue(self.screen_is("", stderr=other, exit=1))  # any other failure still fails closed

    def test_exit_claude_sends_nothing_into_an_unreadable_pane(self):
        write_scenario(self.tmp.name, [{"match": ["agent", "get", "w"], "stdout": {"result": {"agent": {"name": "w"}}}},
                                       {"match": ["agent", "read", "w"], "stdout": "", "exit": 1}])
        self.assertFalse(retire.exit_claude("w", 0))
        self.assertEqual([c for c in logged_calls(self.tmp.name) if c[:2] == ["agent", "prompt"]], [])


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

    def test_an_idle_named_hq_is_restarted(self):
        gone = {"match": ["agent", "get"], "after": ["agent", "prompt", "athena", "/exit"], "stdout": "",
                "stderr": '{"error":{"code":"agent_not_found","message":"x"}}', "exit": 1}
        write_scenario(self.tmp.name, [{**alive("old"), "after": ["agent", "start"]}, gone, alive("old"), HQ_SPACE,
                                       {"match": ["agent", "read", "athena"], "stdout": NAMED_EMPTY}])
        result = hq.restart_now(update=False)
        self.assertTrue(result["resumed"])
        self.assertEqual(len(self.exits("athena")), 1)

    def test_a_named_hq_with_typed_text_is_not(self):
        write_scenario(self.tmp.name, [alive("old"), HQ_SPACE, {"match": ["agent", "read", "athena"], "stdout": NAMED_TYPED}])
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
    def named_worker(self, screen_rule):
        repo, _ = make_repo(self.tmp.name)
        wt = Path(self.tmp.name) / "worktrees" / "proj" / "abc-1"
        git(repo, "worktree", "add", "-q", "-b", "you/abc-1", str(wt), "main")
        ledger.append("spawn", "abc-1", repo=str(repo), path=os.path.realpath(wt), branch="you/abc-1",
                      workspace="w5", pane="w5:p1", terminal="term_1")
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get", "abc-1"], "stdout": {"result": {"agent": {"name": "abc-1"}}}, "times": 2},
            {"match": ["agent", "get", "abc-1"], "stdout": "", "stderr": '{"error":{"code":"agent_not_found","message":"x"}}',
             "exit": 1},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(wt)}}}}},
            screen_rule])
        return wt

    def test_retire_exits_an_idle_named_worker(self):
        self.named_worker({"match": ["agent", "read", "abc-1"], "stdout": NAMED_EMPTY})
        result = retire.retire("abc-1")
        self.assertEqual(len(self.exits("abc-1")), 1)
        self.assertTrue(result["removed"])

    def test_retire_leaves_a_named_worker_someone_is_typing_to(self):
        self.named_worker({"match": ["agent", "read", "abc-1"], "stdout": NAMED_TYPED})
        with self.assertRaises(retire.RetireError):
            retire.retire("abc-1")
        self.assertEqual(self.exits("abc-1"), [])

    def test_a_worker_whose_claude_already_exited_retires_without_force(self):
        err = '{"error":{"code":"agent_not_found","message":"agent target abc-1 not found"}}'
        wt = self.named_worker({"match": ["agent", "read", "abc-1"], "stdout": "", "stderr": err, "exit": 1})
        write_scenario(self.tmp.name, [
            {"match": ["agent"], "stdout": "", "stderr": err, "exit": 1},
            {"match": ["workspace", "get", "w5"], "stdout": {"result": {"workspace": {"worktree": {"checkout_path": str(wt)}}}}}])
        result = retire.retire("abc-1")
        self.assertTrue(result["removed"])

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
