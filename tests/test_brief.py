"""Brief and goal checks: a brief whose wording would keep the goal evaluator saying "not met" is refused before spawn.

The briefs in tests/briefs/ are real ones whose workers looped, anonymised (see its README)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import brief, spawn

BRIEFS = helpers.ROOT / "tests" / "briefs"


def problems(name):
    return brief.check((BRIEFS / name).read_text())


def kinds(found):
    return {(p["field"], p["kind"]) for p in found}


class BriefCheckTest(unittest.TestCase):
    def test_a_good_brief_passes(self):
        self.assertEqual(problems("good.md"), [])

    def test_ship_in_the_goal_is_an_outcome_the_worker_cannot_reach(self):
        found = problems("ship-one-pr.md")
        self.assertIn(("GOAL", "outcome"), kinds(found))
        self.assertIn("Ship", [p for p in found if p["kind"] == "outcome"][0]["text"])

    def test_words_that_are_no_outcome_here(self):
        for text in ("Build it per the approved plan.", "No merge commits; resolve merge conflicts.",
                     "Rebase onto main after #6860 merged.", "Keep the landing page.",
                     "Evidence from the released build.", "A Merge button, for repos outside human_merge_repos.",
                     "The board hides PRs that are merged.", "The dash lists PRs ready for review in their own column.",
                     "Auto-merge turns on when the PR is approved.",
                     "Retarget the next PR of a stack once the bottom one is merged."):
            self.assertEqual(brief.check_goal(text), [], text)

    def test_merge_ready_and_a_verify_that_waits_on_ci(self):
        found = kinds(problems("rebase-merge-ready.md"))
        self.assertIn(("GOAL", "outcome"), found)
        self.assertIn(("VERIFY", "remote"), found)

    def test_each_problem_names_its_rule_and_line(self):
        found = problems("human-signs-in.md")
        human = [p for p in found if p["kind"] == "human"][0]
        lines = (BRIEFS / "human-signs-in.md").read_text().splitlines()
        self.assertIn("The human signs in", lines[human["line"] - 1])
        self.assertTrue(brief.describe(human).startswith(f"line {human['line']}: ACCEPTANCE (human)"))
        goal = [p for p in problems("ship-one-pr.md") if p["field"] == "GOAL"][0]
        self.assertTrue(lines and (BRIEFS / "ship-one-pr.md").read_text().splitlines()[goal["line"] - 1]
                        .startswith("GOAL: Ship"))

    def test_features_about_people_are_no_human_step(self):
        for item in ("A user can sign in to the app with Google.", "When you click a row in the table, it expands.",
                     "The prompt tells the human which space to open."):
            text = f"GOAL: g\nSCOPE: s\nACCEPTANCE:\n- {item}\nVERIFY: `make test`\nFORBIDDEN: f\n"
            self.assertEqual(brief.check(text), [], item)

    def test_a_human_sign_in_in_acceptance_belongs_in_human_checks(self):
        found = problems("human-signs-in.md")
        human = [p for p in found if p["kind"] == "human"]
        self.assertEqual(len(human), 1)
        self.assertEqual(human[0]["field"], "ACCEPTANCE")
        self.assertIn("HUMAN-CHECKS", human[0]["fix"])
        self.assertIn(("VERIFY", "remote"), kinds(found))  # "CI green." is no command

    def test_vague_acceptance_and_a_non_draft_pr(self):
        found = problems("vague-docs.md")
        vague = [p["text"] for p in found if p["kind"] == "vague"]
        self.assertEqual(vague, ["Docs updated."])
        self.assertIn(("ACCEPTANCE", "outcome"), kinds(found))  # undrafting is HQ's, after its review

    def test_human_checks_and_forbidden_are_never_linted(self):
        text = (BRIEFS / "good.md").read_text()
        self.assertIn("The human signs in", text)
        self.assertIn("merging", text)
        self.assertEqual(brief.check(text), [])

    def test_missing_and_empty_fields(self):
        found = brief.check("# ABC-1: x\nGOAL: y\nSCOPE:\nACCEPTANCE:\n- `make test` passes with the new case.\n")
        missing = {p["field"] for p in found if p["kind"] == "missing"}
        self.assertEqual(missing, {"SCOPE", "VERIFY", "FORBIDDEN"})

    def test_a_placeholder_or_manual_verify(self):
        for verify in ("<the exact commands>", "TBD", "check manually in the browser"):
            text = f"GOAL: g\nSCOPE: s\nACCEPTANCE:\n- `make test` passes.\nVERIFY: {verify}\nFORBIDDEN: f\n"
            found = brief.check(text)
            self.assertIn(("VERIFY", "unrunnable"), kinds(found), verify)

    def test_fields_parse_with_bullets_and_bold(self):
        fields = brief.parse("**GOAL:** one\nmore\nSCOPE: a\n- b\nVERIFY: `x`\nFORBIDDEN (ask first): f\n")
        self.assertEqual(fields["FORBIDDEN"], "f")
        self.assertEqual(fields["GOAL"], "one\nmore")
        self.assertEqual(fields["SCOPE"], "a\n- b")
        self.assertEqual(brief.items("- one\n- two\n  still two\n* three"), ["one", "two still two", "three"])


class GoalCheckTest(unittest.TestCase):
    TEMPLATE = ("ABC-1 is done when: (1) each ACCEPTANCE item in the brief is shown met in this transcript, (2) make "
                "test was run and its passing output is shown, (3) the branch is pushed and a draft PR exists with "
                "its URL printed, (4) the final message ends with an ATHENA-REPORT line.")

    def test_the_skill_template_passes(self):
        self.assertEqual(brief.check_goal(self.TEMPLATE), [])

    def test_a_goal_that_needs_sendmessage_to_succeed_is_refused(self):
        """784 "not met" verdicts came from this clause while HQ's socket was gone."""
        found = brief.check_goal(self.TEMPLATE + " Report to the session named athena with SendMessage.")
        self.assertEqual([p["kind"] for p in found], ["delivery"])

    def test_a_goal_that_needs_a_merge_or_approval_is_refused(self):
        for tail in (" (5) the PR is merged.", " (5) the PR is approved.",
                     " (5) the PR is ready for review, not a draft."):
            self.assertEqual([p["kind"] for p in brief.check_goal(self.TEMPLATE + tail)], ["outcome"], tail)


class SpawnRefusesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_spawn_refuses_a_failing_brief_before_making_anything(self):
        with mock.patch.object(spawn.gitops, "preflight") as preflight:
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn.spawn(self.tmp.name, "you/abc-501-x", "abc-501", BRIEFS / "ship-one-pr.md",
                            GoalCheckTest.TEMPLATE)
        self.assertIn("Ship", str(caught.exception))
        self.assertIn("--skip-brief-check", str(caught.exception))
        self.assertIn("line 3: GOAL (outcome)", str(caught.exception))
        preflight.assert_not_called()

    def test_spawn_refuses_a_failing_goal(self):
        with mock.patch.object(spawn.gitops, "preflight") as preflight:
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn.spawn(self.tmp.name, "you/abc-505-x", "abc-505", BRIEFS / "good.md",
                            GoalCheckTest.TEMPLATE + " (5) the PR is merged.")
        self.assertIn("merged", str(caught.exception))
        preflight.assert_not_called()

    def test_the_check_command(self):
        def run(*args):
            return subprocess.run([str(helpers.ROOT / "bin" / "athena"), "brief", "check", *args],
                                  env=dict(os.environ), capture_output=True, text=True)
        ok = run(str(BRIEFS / "good.md"))
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "brief ok"))
        bad = run(str(BRIEFS / "human-signs-in.md"), "--goal", GoalCheckTest.TEMPLATE)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("HUMAN-CHECKS", bad.stdout)
        out = json.loads(run(str(BRIEFS / "vague-docs.md"), "--json").stdout)
        self.assertTrue(any(p["kind"] == "vague" for p in out))


if __name__ == "__main__":
    unittest.main()
