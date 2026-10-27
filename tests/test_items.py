"""Items (plan §3): names, kinds, editing in place, resolving, snooze, defaults that apply on their own, and the
migration of today's asks; the request log and its weekly check; `athena status <worker>`; the uncapped handoff."""
import io
import json
import re
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import asks, cli, handoff, hookstatus, ledger, paths, requestlog

T = 1791500000.0
FIXTURE = Path(__file__).resolve().parent / "data" / "needs-you-v1.json"


def answer(seq, item, ts=T, **kw):
    line = {"seq": seq, "ts": ts, "item": item, "status": "queued", **kw}
    with open(asks.actions_file(), "a") as f:
        f.write(json.dumps(line) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        paths.ensure()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class NamesAndKindsTest(Base):
    def test_add_names_an_item_two_to_four_words(self):
        # Without --name, a name is derived from the title, marked for renaming, with a hint (HQ's loaded skill may be
        # one from before names: a hard failure mid-session costs a round trip).
        code, out, err = self.cli("item", "add", "Pick the Copilot hosts? (rec both)")
        self.assertEqual((code, out), (0, "a1\n"))
        self.assertIn("athena item edit a1 --name", err)
        self.assertEqual((asks.show("a1")["name"], asks.show("a1")["name_derived"]), ("Pick Copilot hosts", True))
        self.assertIn("name derived: rename it", self.cli("item")[1])
        self.assertNotIn("name_derived", asks.edit("a1", name="Copilot hosts"))
        for bad in ("Copilot", "one two three four five", "x" * 40 + " y"):
            with self.subTest(name=bad):
                code, _, err = self.cli("item", "add", "Pick the hosts?", "--name", bad)
                self.assertEqual(code, 2)
                self.assertIn("2 to 4 words", err)
        code, out, err = self.cli("item", "add", "Pick the Copilot hosts? (rec both)", "--name", "Copilot  hosts")
        self.assertEqual((code, out, err), (0, "a2\n", ""))
        self.assertEqual(asks.show("a2")["name"], "Copilot hosts")
        # `athena ask` is the same command, and the list leads with the name, never a bare id.
        code, out, _ = self.cli("ask")
        self.assertTrue(out.startswith("Copilot hosts (a1, decision, next: you)"), out)

    def test_kinds_and_whose_move_by_default(self):
        cases = [("Ship it?", None, "decision", "you"), ("The quota resets at 14:00", None, "result", "you"),
                 ("Build the palette", "request", "request", "athena"), ("Check after CI", "promise", "promise", "athena"),
                 ("CodeQL out of memory", "incident", "incident", "athena"), ("Was it this?", "question", "decision", "you"),
                 ("Read it", "note", "result", "you")]
        for text, kind, want, nxt in cases:
            with self.subTest(text=text):
                item = asks.show(asks.add(text, kind=kind, name="Some item"))
                self.assertEqual((item["kind"], item["next"]), (want, nxt))
        with self.assertRaises(asks.AskError):
            asks.add("Ship?", kind="chore", name="Some item")
        self.assertEqual(asks.show(asks.add("Ship?", next="reviewer", name="Some item"))["next"], "reviewer")

    def test_a_derived_name_reads_like_one(self):
        self.assertEqual(asks.derive_name("Orbit: file a ticket to test it?"), "Orbit file ticket test")
        self.assertEqual(asks.derive_name("Copilot catalog hosts: list copilot.cloud.microsoft? (rec B)"), "Copilot catalog hosts")
        self.assertEqual(asks.derive_name("Re-enable auto-merge on **Dependabot** bundle #7012?"), "Re-enable auto-merge Dependabot")  # 32 characters at most
        self.assertEqual(asks.derive_name("Merge [the PR](https://github.com/a/b/pull/1) now?"), "Merge PR")
        self.assertLessEqual(len(asks.derive_name("Supercalifragilistic expialidocious antidisestablishmentarianism words")), asks.NAME_MAX)

    def test_due_dates(self):
        self.assertEqual(asks.parse_due("+2h", now=T), T + 7200)
        self.assertEqual(asks.parse_due("+3d", now=T), T + 3 * 86400)
        self.assertEqual(time.localtime(asks.parse_due("2026-10-29", now=T))[3:5], (9, 0))
        self.assertEqual(time.localtime(asks.parse_due("2026-10-29T11:30", now=T))[3:5], (11, 30))
        self.assertIsNone(asks.parse_due("none"))
        with self.assertRaises(asks.AskError):
            asks.parse_due("tomorrowish")


class DashAgreesTest(unittest.TestCase):
    def test_item_limits_kinds_and_stakes_match_the_dash(self):
        go = (paths.PLUGIN_ROOT / "dash" / "state.go").read_text()
        items_go = (paths.PLUGIN_ROOT / "dash" / "items.go").read_text()
        for name, value in (("nameMax", asks.NAME_MAX), ("wordsMax", asks.WORDS_MAX), ("logMax", asks.LOG_MAX),
                            ("linksMax", asks.LINKS_MAX), ("workersMax", asks.WORKERS_MAX)):
            with self.subTest(name=name):
                self.assertRegex(go, rf"\b{name}\s*=\s*{value}\b")
        for table, values in (("itemKinds", asks.KINDS), ("itemNext", asks.NEXT), ("itemStakes", asks.STAKES)):
            line = next(l for l in go.splitlines() if l.strip().startswith(table))
            self.assertEqual(sorted(re.findall(r'"([a-z-]+)": true', line)), sorted(values), table)
        for kind, nxt in asks.NEXT_BY_KIND.items():
            self.assertIn(f'"{kind}": "{nxt}"', go)
        self.assertIn(f'undoDefault  = "{asks.UNDO_DEFAULT}"', items_go)


class EditInPlaceTest(Base):
    def test_edit_keeps_the_id_and_says_what_changed(self):
        a1 = asks.add("CodeQL failed on main", kind="incident", name="CodeQL failure", now=T)
        a2 = asks.add("Ship?", name="Ship it", now=T)
        code, out, _ = self.cli("item", "edit", a1, "--name", "CodeQL out of memory", "--title", "CodeQL failed three times",
                                "--next", "ci", "--due", "+1d", "--worker", "abc-12", "--link", "Run|https://github.com/a/b/actions/runs/1")
        self.assertEqual(code, 0)
        item = asks.show(a1)
        self.assertEqual((item["id"], item["name"], item["text"], item["next"], item["workers"]),
                         (a1, "CodeQL out of memory", "CodeQL failed three times", "ci", ["abc-12"]))
        self.assertEqual(item["links"], [{"title": "Run", "url": "https://github.com/a/b/actions/runs/1"}])
        self.assertEqual(item["created"], int(T))  # its age is the item's, not the edit's
        self.assertEqual(item["log"][-1]["text"], "edited: title, name, next move, due date, worker abc-12, link")
        self.assertEqual([i["id"] for i in asks.items()], [a1, a2])  # in its place, not re-created
        self.assertEqual(asks._read()["next"], 3)
        self.assertEqual(self.cli("item", "edit", a1)[0], 2)  # nothing to change
        self.assertEqual(self.cli("item", "edit", "a9", "--name", "No such item")[0], 2)
        # --due none clears it; --pr '' unlinks the PR.
        asks.edit(a1, due="none", pr="acme/web#9")
        asks.edit(a1, pr="")
        self.assertNotIn("due", asks.show(a1))
        self.assertNotIn("pr", asks.show(a1))

    def test_one_incident_is_one_item_that_changes(self):
        a1 = asks.add("CodeQL failed on main", kind="incident", name="CodeQL out of memory")
        for n in range(3):
            asks.edit(a1, text=f"CodeQL failed {n + 2} times on main")
        self.assertEqual(len(asks.items()), 1)
        self.assertEqual(asks.show(a1)["text"], "CodeQL failed 4 times on main")


class ResolveTest(Base):
    def test_done_resolves_with_a_note_and_keeps_the_item(self):
        a1 = asks.add("Ship?", name="Ship it", now=T)
        code, out, _ = self.cli("item", "done", a1, "--resolution", "you replied in chat")
        self.assertEqual(json.loads(out)["resolved"], True)
        self.assertEqual(asks.items(), [])
        item = asks.show(a1)
        self.assertEqual(item["resolved_note"], "you replied in chat")
        self.assertEqual(item["log"][-1]["text"], "resolved: you replied in chat")
        self.assertEqual([i["id"] for i in asks.items(resolved=True)], [a1])

    def test_an_fyi_is_resolved_at_once(self):
        a1 = asks.add("Disk cleanup freed 210 GB", name="Disk cleanup", fyi=True)
        self.assertEqual(asks.items(), [])
        self.assertEqual(asks.show(a1)["resolved_note"], "FYI")

    def test_snooze_hides_until_then_and_new_activity_cancels_it(self):
        a1 = asks.add("Answer the sign-in question", kind="promise", name="Sign-in answer", now=T)
        item = asks.snooze(a1, "+2h", now=T + 10)
        self.assertEqual(item["snooze"]["until"], int(T + 10 + 7200))
        self.assertGreater(item["snooze"]["at"], item["updated"])  # snoozing is no activity of its own
        item = asks.edit(a1, why="They asked again.", now=T + 60)
        self.assertGreaterEqual(item["updated"], item["snooze"]["at"])  # the dash reads this as awake again
        with self.assertRaises(asks.AskError):
            asks.snooze(a1, "+0h", now=T)
        self.assertNotIn("snooze", asks.snooze(a1, None, now=T + 70))


class DefaultsTest(Base):
    """Decision 4: a reversible, low-stakes call's recommended option applies on its own; never one marked
    security, customer, money or one-way."""

    def test_never_for_a_call_marked_security_customer_money_or_one_way(self):
        for stake in asks.STAKES:
            with self.subTest(stake=stake):
                with self.assertRaises(asks.AskError) as e:
                    asks.add("Rotate it?", name="Rotate the key", options=["Yes"], rec="yes", auto=True, stakes=[stake])
                self.assertIn("decision 4", str(e.exception))
                code, _, err = self.cli("item", "add", "Rotate it?", "--name", "Rotate the key", "--option", "Yes",
                                        "--rec", "yes", "--auto", "--stakes", stake)
                self.assertEqual(code, 2, err)
        # Marking the stakes later takes the default off; turning it back on is refused.
        a1 = asks.add("Re-run it?", name="Flaky e2e", options=["Re-run", "Leave it"], rec="re-run", auto=True)
        item = asks.edit(a1, stakes=["one-way"])
        self.assertNotIn("auto", item)
        self.assertIn("default off", item["log"][-1]["text"])
        with self.assertRaises(asks.AskError):
            asks.edit(a1, auto=True)

    def test_a_default_needs_a_recommended_option_on_a_decision(self):
        with self.assertRaises(asks.AskError):
            asks.add("Re-run it?", name="Flaky e2e", options=["Re-run"], auto=True)
        with self.assertRaises(asks.AskError):
            asks.add("Re-run it?", name="Flaky e2e", options=["Re-run"], rec="nope")
        with self.assertRaises(asks.AskError):
            asks.add("Quota resets at 14:00", kind="result", name="Quota reset", options=["Ok"], rec="ok", auto=True)
        a1 = asks.add("Re-run it?", name="Flaky e2e", options=["Re-run", "Leave it"], rec="re-run", auto=True)
        self.assertEqual((asks.show(a1)["rec"], asks.show(a1)["auto"]), ("re-run", True))

    def test_an_applied_default_waits_for_done_or_undo(self):
        a1 = asks.add("Re-run it?", name="Flaky e2e", options=["Re-run", "Leave it"], rec="re-run", auto=True, now=T)
        answer(1, a1, ts=T, option="re-run", auto=True)
        got = asks.queue(now=T + asks.SETTLE)
        self.assertEqual((got[0]["option"]["id"], got[0]["auto"]), ("re-run", True))
        result = asks.done(a1, now=T + 60)
        self.assertEqual((result["resolved"], result.get("applied")), (False, True))
        item = asks.show(a1)
        self.assertEqual(item["applied"]["label"], "Re-run")
        self.assertNotIn("auto", item)  # it applied once; never again
        self.assertIn("athena took the rec on its own: Re-run", item["log"][-1]["text"])
        # Undo: the call is the human's again, its default off.
        answer(2, a1, ts=T + 100, option=asks.UNDO_DEFAULT)
        got = asks.queue(now=T + 200)
        self.assertEqual(got[0]["option"], {"id": "undo-default", "label": "Undo Re-run", "do": "reply"})
        asks.done(a1, now=T + 300)
        item = asks.show(a1)
        self.assertNotIn("applied", item)
        self.assertNotIn("resolved", item)
        self.assertNotIn("auto", item)

    def test_dont_ask_me_again_rides_on_the_answer(self):
        a1 = asks.add("Re-run it?", name="Flaky e2e", options=["Re-run"])
        answer(1, a1, option="re-run", rule=True)
        self.assertTrue(asks.queue(now=T + 60)[0]["rule"])


class MigrationTest(Base):
    def test_a_real_shaped_needs_you_json_loses_nothing(self):
        shutil.copy(FIXTURE, asks.asks_file())  # a copy: never the live one
        before = json.loads(FIXTURE.read_text())
        result = asks.migrate_items()
        self.assertEqual((result["before"], result["after"]), (27, 27))
        self.assertEqual(len(result["changed"]), 27)
        after = json.loads(asks.asks_file().read_text())
        self.assertEqual(after["next"], before["next"])
        self.assertEqual([i["id"] for i in after["items"]], [i["id"] for i in before["items"]])
        for old, new in zip(before["items"], after["items"]):
            for k, v in old.items():
                if k != "kind":
                    self.assertEqual(new[k], v, (old["id"], k))
            self.assertIn(new["kind"], asks.KINDS)
            self.assertTrue(new["name"] and new["name_derived"])
            self.assertEqual(new["next"], "you")
        kinds = {i["id"]: i["kind"] for i in after["items"]}
        self.assertEqual(kinds["a79"], "decision")  # a question
        notes = [o for o in before["items"] if o["kind"] == "note"]
        self.assertTrue(any(kinds[o["id"]] == "decision" for o in notes if o["options"]))  # a note with options is a call
        self.assertTrue(all(kinds[o["id"]] == "result" for o in notes if not o["options"]))
        self.assertEqual(len(asks.items()), 27)  # every one open, on the dash
        # The file from before is kept once; a second run changes nothing.
        self.assertEqual(json.loads(asks.asks_file().with_name("needs-you.json.v1").read_text()), before)
        self.assertEqual(asks.migrate_items()["changed"], [])
        # A dry run counts and writes nothing.
        shutil.copy(FIXTURE, asks.asks_file())
        self.assertEqual(asks.migrate_items(dry_run=True)["after"], 27)
        self.assertEqual(json.loads(asks.asks_file().read_text()), before)

    def test_cli_migrate(self):
        shutil.copy(FIXTURE, asks.asks_file())
        code, out, _ = self.cli("item", "migrate", "--dry-run")
        self.assertEqual((code, json.loads(out)["before"], json.loads(out)["after"]), (0, 27, 27))


class RequestLogTest(Base):
    def hook(self, prompt, role="hq", event="UserPromptSubmit"):
        env = {"ATHENA_ROLE": role} if role else {}
        return hookstatus.log_request({"hook_event_name": event, "prompt": prompt, "session_id": "s1"}, env)

    def test_hq_logs_what_the_human_types_and_nothing_athena_submits(self):
        self.assertEqual(self.hook("start abc-101 and abc-102"), "r1")
        self.assertEqual(self.hook("athena, what is the disk doing?"), "r2")
        for prompt in ("athena watch 22:19:\nabc-1 report DONE", "athena watch:\ndash-queue 4", "/compact", "/goal clear",
                       "<cross-session-message from=\"x\">hi</cross-session-message>", "<task-notification>done</task-notification>",
                       "HQ restarted: run your session-start routine", "Fresh HQ after `athena hq rotate`: load the skill",
                       "Start your HQ routine: run `athena status`", "athena fallback sweep: run the sweep subagent",
                       "athena doctor: HQ was resumed; run your session start",
                       "", "   "):
            with self.subTest(prompt=prompt):
                self.assertIsNone(self.hook(prompt))
        self.assertIsNone(self.hook("start abc-1", role=None))  # a worker's prompt is not the human's request to HQ
        self.assertIsNone(self.hook("start abc-1", event="Stop"))
        lines = [json.loads(l) for l in requestlog.log_file().read_text().splitlines()]
        self.assertEqual([(l["id"], l["text"], l["session"]) for l in lines],
                         [("r1", "start abc-101 and abc-102", "s1"), ("r2", "athena, what is the disk doing?", "s1")])
        self.assertEqual(requestlog.log_file().stat().st_mode & 0o777, 0o600)

    def test_the_hook_script_lets_hq_prompts_through(self):
        import subprocess
        (paths.state_dir() / "ledger.jsonl").write_text("")
        env = {**os.environ, "ATHENA_ROLE": "hq", "ATHENA_WORKTREE_ROOT": str(Path(self.tmp.name) / "worktrees")}
        env.pop("ATHENA_WORKER", None)
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "build the palette", "session_id": "s1"})
        hook = paths.PLUGIN_ROOT / "hooks" / "worker-status"
        subprocess.run([str(hook), "UserPromptSubmit"], input=payload, text=True, env=env, cwd=self.tmp.name, check=True)
        subprocess.run([str(hook), "Stop"], input=payload, text=True, env=env, cwd=self.tmp.name, check=True)
        self.assertEqual([r["text"] for r in requestlog.requests()], ["build the palette"])

    def test_the_weekly_check_finds_requests_with_no_item(self):
        now = time.time()
        r1 = requestlog.record("research System One on Vertex, Foundry and Bedrock", now=now - 3600)
        r2 = requestlog.record("set up the meeting for Wednesday", now=now - 7200)
        r3 = requestlog.record("thanks", now=now - 60)
        r4 = requestlog.record("an old one", now=now - 10 * 86400)
        asks.add("Which clouds carry System One?", kind="request", name="System One on clouds", requests=[r1])
        self.assertEqual([r["id"] for r in requestlog.check(now=now)], [r2, r3])
        requestlog.ignore([r3], now=now)
        self.assertEqual([r["id"] for r in requestlog.check(now=now)], [r2])
        code, out, _ = self.cli("item", "check")
        self.assertEqual(code, 0)
        self.assertIn("1 request of the last 7 days with no item", out)
        self.assertIn("set up the meeting for Wednesday", out)
        # Filing it, even as an item resolved since, is a home.
        a2 = asks.add("Meeting Wed or Thu", kind="promise", name="Customer meeting", requests=[r2], due="+1d")
        asks.done(a2)
        self.assertEqual(requestlog.check(now=now), [])
        self.assertIn("every request of the last 7 days has an item", self.cli("item", "check")[1])
        self.assertEqual([r["id"] for r in requestlog.check(days=30, now=now)], [r4])
        code, out, _ = self.cli("item", "requests")
        self.assertIn(f"-> {a2}", out)
        self.assertIn("(chat)", out)
        self.assertEqual(self.cli("item", "ignore", "r99")[0], 2)
        with self.assertRaises(asks.AskError):
            asks.add("x?", name="Bad request", requests=["12"])


class StatusDetailTest(Base):
    def test_status_of_one_worker_prints_its_whole_report(self):
        ledger.append("spawn", "abc-1", branch="you/abc-1", path=self.tmp.name)
        report = {"status": "DONE_WITH_CONCERNS", "pr": "https://github.com/acme/web/pull/7", "head": "abc1234",
                  "verify": "make test -> pass", "decisions": ["Stacked PR 2 on PR 1's branch"],
                  "concerns": ["CI not run on the rebased head", "One flaky test"]}
        message = "Pushed the fix and opened the PR.\nThe flaky test is unrelated.\n\nATHENA-REPORT " + json.dumps(report)
        hookstatus.apply({"hook_event_name": "Stop", "last_assistant_message": message}, "abc-1")
        self.assertEqual(hookstatus.read("abc-1")["summary"], "Pushed the fix and opened the PR.\nThe flaky test is unrelated.")
        with mock.patch("athena_lib.status.gitops.unpushed", return_value={"ahead": 2, "dirty": []}), \
                mock.patch("athena_lib.status.gitops.git", return_value="abc1234"), \
                mock.patch("athena_lib.status.herdr.call", return_value={"agent": {"agent_status": "done"}}):
            code, out, _ = self.cli("status", "abc-1")
            self.assertEqual(code, 0)
            for want in ("abc-1  done", "last words:", "    Pushed the fix and opened the PR.", "    status: DONE_WITH_CONCERNS",
                         "    verify: make test -> pass", "    decisions:", "      - Stacked PR 2 on PR 1's branch",
                         "      - CI not run on the rebased head", "      - One flaky test", "    pr: https://github.com/acme/web/pull/7"):
                self.assertIn(want, out)
            self.assertNotIn('"status"', out)  # never the report's raw JSON
            self.assertEqual(self.cli("status", "abc-9")[0], 2)


class WeekTest(Base):
    """Plan §6, "Your week, replayed", through the CLI HQ runs (the dash's side is dash/week_test.go)."""

    def test_a_one_off_question(self):
        rid = hookstatus.log_request({"hook_event_name": "UserPromptSubmit", "prompt": "is system one on vertex?",
                                      "session_id": "s"}, {"ATHENA_ROLE": "hq"})
        self.assertEqual([r["id"] for r in requestlog.check()], [rid])  # until it is filed, the check finds it
        code, out, _ = self.cli("item", "add", "Checking Vertex, Foundry and Bedrock", "--name", "System One on clouds",
                                "--kind", "request", "--request", rid, "--words", "is system one on vertex?")
        a1 = out.strip()
        self.assertEqual(requestlog.check(), [])
        self.cli("item", "edit", a1, "--kind", "result", "--next", "you", "--title", "None of them carry System One")
        self.assertEqual(asks.show(a1)["kind"], "result")
        # The human clicks Done on the dashboard (dash/items.go); a resolved item stays for search, then archives.
        asks.done(a1, note="you marked it done")
        self.assertEqual(asks.items(), [])
        self.assertEqual(requestlog.check(), [])

    def test_a_build_resolves_when_the_human_merged_it(self):
        a1 = asks.add("Feature flag no, shared UI no: take all recs?", name="Palette plan", options=["Take all recs"],
                      rec="take-all-recs", auto=True)
        answer(1, a1, option="take-all-recs")
        self.assertEqual(asks.queue(now=T + 60)[0]["ask"]["rec"], "take-all-recs")
        asks.done(a1, now=T + 60)
        a2 = asks.add("Building the palette", kind="request", name="⌘K palette", workers=["abc-7"], pr="acme/web#70")
        self.assertEqual(asks.show(a2)["next"], "athena")
        asks.done(a2, note="you merged it")
        self.assertEqual(asks.show(a2)["resolved_note"], "you merged it")

    def test_a_weekend_away_never_defaults_a_customer_call(self):
        with self.assertRaises(asks.AskError):
            asks.add("Send the drafted reply?", name="Customer reply", options=["Send"], rec="send", auto=True,
                     stakes=["customer"])
        a1 = asks.add("Send the drafted reply?", name="Customer reply", options=["Send"], rec="send", stakes=["customer"])
        self.assertNotIn("auto", asks.show(a1))

    def test_a_customer_thread_is_a_promise_with_a_due_date(self):
        code, out, _ = self.cli("item", "add", "Confirm the customer's engineer for Wed 11:30", "--name", "Customer meeting",
                                "--kind", "promise", "--next", "you", "--due", "2026-10-28T11:30",
                                "--body", "**Draft reply:** Wednesday 11:30 works.")
        a1 = out.strip()
        self.assertIn("due Wed 28 Oct 11:30", self.cli("item")[1])
        self.assertIn("due Wed 28 Oct 11:30", handoff.build(dry_run=True))
        self.cli("item", "done", a1, "--resolution", "you posted the reply")
        self.assertEqual(asks.show(a1)["resolved_note"], "you posted the reply")


class HandoffTest(Base):
    def test_every_open_item_is_in_the_handoff_by_name(self):
        for n in range(40):
            asks.add(f"Item number {n}?", name=f"Item {n} name")
        asks.add("Gone", name="Resolved one", fyi=True)
        text = handoff.build(dry_run=True)
        section = text.split("## Open items")[1].split("## ")[0]
        self.assertEqual(section.count("\n- Item "), 40)
        self.assertIn("- Item 39 name (a40, decision, next: you): Item number 39?", section)
        self.assertNotIn("Resolved one", section)
        self.assertNotIn("more", section)


if __name__ == "__main__":
    unittest.main()
