"""athena ask: needs-you.json, the migration from needs-you.md, the dashboard's answer queue and its doorbell."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests import helpers
from athena_lib import asks, cli, paths, watch

T = 1791500000.0


def answer(seq, item, ts=T, **kw):
    """A line as the dash appends it (dash/actions.go)."""
    line = {"seq": seq, "ts": ts, "item": item, "status": "queued", **(kw or {"option": "yes"})}
    with open(asks.actions_file(), "a") as f:
        f.write(json.dumps(line) + "\n")


def status(seq, st, ts=T):
    with open(asks.actions_file(), "a") as f:
        f.write(json.dumps({"seq": seq, "ts": ts, "status": st}) + "\n")


class AsksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        paths.ensure()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["ask", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_add_prints_the_new_id_and_show_reads_it(self):
        code, out, _ = self.run_cli("add", "Close  ABC-4550 as a duplicate?", "--why", "Same stack trace.", "--pr",
                                    "https://github.com/acme/web/pull/7", "--option", "Close it:check", "--option", "Not now:x:dismiss")
        self.assertEqual((code, out), (0, "a1\n"))
        self.assertEqual(self.run_cli("add", "FYI: the quota resets at 14:00")[1], "a2\n")
        item = asks.show("a1")
        self.assertEqual({k: item[k] for k in ("kind", "text", "why", "pr")},
                         {"kind": "question", "text": "Close ABC-4550 as a duplicate?", "why": "Same stack trace.", "pr": "acme/web#7"})
        self.assertEqual(item["options"], [{"id": "close-it", "label": "Close it", "do": "reply", "icon": "check"},
                                           {"id": "not-now", "label": "Not now", "do": "dismiss", "icon": "x"}])
        self.assertEqual(asks.show("a2")["kind"], "note")
        code, out, _ = self.run_cli("show", "a2")
        self.assertEqual(json.loads(out)["id"], "a2")
        self.assertEqual(self.run_cli("show", "a9")[0], 2)
        # Ids are never reused: one done and gone, the next is still new.
        asks.done("a2")
        self.assertEqual(asks.add("Next?"), "a3")

    def test_add_refuses_what_the_dashboard_cannot_show(self):
        for argv in (["add", "x?", "--option", "a", "--option", "b", "--option", "c"],
                     ["add", "x?", "--option", "A label past sixteen"],
                     ["add", "x?", "--option", "Yes:check:merge"],
                     ["add", "x?", "--pr", "https://example.com/pull/1"],
                     ["add", "x?", "--why", "w" * 201],
                     ["add", "   "]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 2, err)
        self.assertFalse(asks.asks_file().exists())
        # An unknown icon is not an error: the option shows none, and the list of icons is printed.
        code, _, err = self.run_cli("add", "Ship?", "--option", "Ship:rocket")
        self.assertEqual(code, 0)
        self.assertIn("sparkles", err)
        self.assertNotIn("icon", asks.show("a1")["options"][0])

    def test_icons_txt_matches_the_page_sprite_and_the_skill(self):
        names = asks.icons()
        self.assertEqual(len(names), 24)
        sprite = (paths.PLUGIN_ROOT / "dash" / "page.html").read_text()
        self.assertEqual(sprite.count("<symbol "), 24)
        skill = (paths.PLUGIN_ROOT / "skills" / "athena" / "SKILL.md").read_text()
        for n in names:
            self.assertIn(f'<symbol id="i-{n}" ', sprite)
        self.assertIn("Icons: " + " ".join(names), skill)

    def test_writes_are_atomic(self):
        asks.add("First?")
        before = asks.asks_file().read_text()
        with mock.patch("athena_lib.asks.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                asks.add("Second?")
        self.assertEqual(asks.asks_file().read_text(), before)
        asks.add("Third?")
        self.assertEqual([i["id"] for i in asks.items()], ["a1", "a2"])
        leftover = [p.name for p in asks.asks_file().parent.iterdir() if ".tmp" in p.name]
        self.assertLessEqual(len(leftover), 1)  # at most the one the failed replace left behind

    def test_needs_you_md_migrates_once(self):
        (paths.state_dir() / "needs-you.md").write_text(
            "# Needs you\n\n<!-- athena keeps this list -->\n\n- Start **ABC-111** now? The quota is at 81%.\n"
            "- Approve and merge ABC-452: https://github.com/acme/web/pull/907\n"
            "- Draft notes: [the plan](https://x.test/p?v=2)\n* Which first: ABC-5 or [ABC-6?](https://x.test/6)\n")
        self.assertEqual(asks.migrate(), ["a1", "a2", "a3", "a4"])
        got = asks.items()
        self.assertEqual([(i["id"], i["kind"]) for i in got],
                         [("a1", "question"), ("a2", "note"), ("a3", "note"), ("a4", "question")])
        self.assertEqual(got[1]["text"], "Approve and merge ABC-452: https://github.com/acme/web/pull/907")
        self.assertFalse((paths.state_dir() / "needs-you.md").exists())
        self.assertTrue((paths.state_dir() / "needs-you.md.migrated").exists())
        self.assertEqual(asks.migrate(), [])
        # A line written there out of habit later is picked up too, once.
        (paths.state_dir() / "needs-you.md").write_text("- Start **ABC-111** now? The quota is at 81%.\n- New one?\n")
        self.assertEqual([i["id"] for i in asks.items()], ["a1", "a2", "a3", "a4", "a5"])

    def test_fold_reads_the_queue_as_the_dash_does(self):
        # The same lines as dash/state_test.go TestFoldActionsBySeq.
        lines = [{"seq": 1, "ts": 10, "item": "a1", "option": "yes", "status": "queued"},
                 {"seq": 2, "ts": 11, "item": "a2", "text": "hi", "status": "queued"},
                 {"seq": 1, "ts": 12, "status": "cancelled"},
                 {"seq": 3, "ts": 13, "item": "w-abc-1", "discuss": True, "status": "queued"},
                 {"seq": 3, "ts": 14, "status": "done"},
                 {"seq": 9, "status": "done"},
                 {"seq": 2, "item": "a9", "status": "queued"}]
        got = asks.fold(lines)
        self.assertEqual([(a["seq"], a["item"], a["status"]) for a in got.values()],
                         [(1, "a1", "cancelled"), (2, "a2", "queued"), (3, "w-abc-1", "done")])
        self.assertEqual((got[2]["text"], got[3].get("discuss"), got[1]["changed"]), ("hi", True, 12))

    def test_queue_lists_settled_answers_with_what_they_answer(self):
        a1 = asks.add("Close ABC-4550?", options=["Close it:check", "Not now:x:dismiss"])
        a2 = asks.add("FYI: the quota resets at 14:00")
        answer(1, a1, option="close-it")
        answer(2, a2, option="dismiss")
        answer(3, "w-abc-1", text="rebase first, then ask me")
        answer(4, a1, discuss=True)
        answer(5, a1, ts=T + 4, option="not-now")  # still undoable
        answer(6, a2, option="dismiss")
        status(6, "cancelled")
        got = asks.queue(now=T + asks.SETTLE)
        self.assertEqual([e["seq"] for e in got], [1, 2, 3, 4])
        self.assertEqual(got[0]["option"], {"id": "close-it", "label": "Close it", "do": "reply", "icon": "check"})
        self.assertEqual(got[0]["ask"], {"kind": "question", "text": "Close ABC-4550?"})
        self.assertEqual(got[1]["option"]["do"], "dismiss")
        self.assertEqual((got[2]["worker"], got[2]["reply"]), ("abc-1", "rebase first, then ask me"))
        self.assertTrue(got[3]["discuss"])
        self.assertEqual([e["seq"] for e in asks.queue(now=T + 4 + asks.SETTLE)], [1, 2, 3, 4, 5])

    def test_a_recheck_from_the_awaiting_review_row_names_its_pr(self):
        answer(1, "pr:acme/web#906", option="recheck")
        answer(2, "pr:acme/web#907", ts=T + 3, option="recheck")  # still undoable
        got = asks.queue(now=T + asks.SETTLE)
        self.assertEqual(got, [{"seq": 1, "item": "pr:acme/web#906", "at": T, "pr": "acme/web#906",
                                "url": "https://github.com/acme/web/pull/906",
                                "option": {"id": "recheck", "label": "Re-check and fix", "do": "reply"}}])
        # HQ marks it done by its item, as any answer: the pill stops showing "re-checking…".
        self.assertEqual(asks.done("pr:acme/web#906", now=T + asks.SETTLE), {"item": "pr:acme/web#906", "removed": False, "done": [1]})
        self.assertEqual([e["seq"] for e in asks.queue(now=T + 3 + asks.SETTLE)], [2])
        code, out, _ = self.run_cli("done", "pr:acme/web#907")
        self.assertEqual((code, json.loads(out)["done"]), (0, [2]))
        # Anything else that starts pr: is no PR: it reads as an ask that is gone.
        answer(3, "pr:acme/web#906; rm -rf /", option="recheck")
        self.assertEqual(asks.queue(now=T + 60)[0]["ask"], None)

    def test_done_marks_settled_answers_and_compacts(self):
        a1 = asks.add("Ship?")
        a2 = asks.add("And then?")
        old = T - asks.KEEP_DONE - 100
        answer(1, a2, ts=old)
        status(1, "done", ts=old + 1)  # done a day ago: dropped
        answer(2, a2, ts=old)
        status(2, "cancelled", ts=T - 60)  # cancelled recently: kept
        answer(3, a1)
        answer(4, a1, ts=T + 3, discuss=True)  # given after athena read the queue: not done yet
        result = asks.done(a1, now=T + asks.SETTLE)
        self.assertEqual(result, {"item": a1, "removed": True, "done": [3]})
        self.assertEqual([i["id"] for i in asks.items()], [a2])
        folded = asks.fold(asks._lines())
        self.assertEqual({s: a["status"] for s, a in folded.items()}, {2: "cancelled", 3: "done", 4: "queued"})
        # --keep: its answers are done, the ask stays on the dashboard.
        self.assertEqual(asks.done(a1, keep=True, now=T + 3 + asks.SETTLE)["done"], [4])
        self.assertEqual(asks.done(a2, keep=True, now=T)["removed"], False)
        self.assertEqual([i["id"] for i in asks.items()], [a2])

    def test_compaction_keeps_the_highest_seq(self):
        old = T - asks.KEEP_DONE - 100
        answer(7, "a1", ts=old)
        status(7, "done", ts=old)
        asks.done("a1", now=T)
        self.assertEqual(max(l["seq"] for l in asks._lines()), 7)  # the dash counts on from it

    def test_cli_queue_and_done(self):
        a1 = asks.add("Ship?")
        answer(1, a1, ts=1.0)
        code, out, _ = self.run_cli("queue")
        self.assertEqual((code, [e["seq"] for e in json.loads(out)]), (0, [1]))
        code, out, _ = self.run_cli("done", a1)
        self.assertEqual(json.loads(out), {"item": a1, "removed": True, "done": [1]})
        self.assertEqual(json.loads(self.run_cli("queue")[1]), [])


    def test_a_needs_you_pr_rows_buttons_name_its_pr(self):
        answer(1, "pr:acme/web#912", option="request-review")
        answer(2, "pr:acme/api#75", option="go", head="8d1c2b3a4f5e")
        answer(3, "pr:acme/web#48", discuss=True)
        got = asks.queue(now=T + asks.SETTLE)
        self.assertEqual(got[0], {"seq": 1, "item": "pr:acme/web#912", "at": T, "pr": "acme/web#912",
                                  "url": "https://github.com/acme/web/pull/912",
                                  "option": {"id": "request-review", "label": "Request review", "do": "reply"}})
        # A go is for the head the human saw, and only that one.
        self.assertEqual((got[1]["pr"], got[1]["option"], got[1]["head"]),
                         ("acme/api#75", {"id": "go", "label": "Merge", "do": "reply"}, "8d1c2b3a4f5e"))
        self.assertEqual((got[2]["pr"], got[2].get("discuss"), "option" in got[2]), ("acme/web#48", True, False))
        # A head on anything but a go, or one that is not a commit, never reaches HQ.
        answer(4, "pr:acme/web#912", option="request-review", head="8d1c2b3a4f5e")
        answer(5, "pr:acme/api#75", option="go", head="main; rm -rf /")
        got = asks.queue(now=T + asks.SETTLE)
        self.assertNotIn("head", got[3])
        self.assertNotIn("head", got[4])


class DoorbellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        paths.ensure()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_rings_once_per_settled_answer_never_twice_the_same(self):
        bell = asks.Doorbell()
        self.assertIsNone(bell.check(now=T))
        answer(1, "a1")
        self.assertIsNone(bell.check(now=T + 1))  # still undoable
        # It settles with no write to the file: the bell still looks, every second, while one waits.
        self.assertEqual(bell.check(now=T + asks.SETTLE), "dash-queue 1")
        self.assertIsNone(bell.check(now=T + asks.SETTLE + 1))
        answer(2, "a2", ts=T + 10)
        answer(3, "a3", ts=T + 10)
        self.assertEqual(bell.check(now=T + 10 + asks.SETTLE), "dash-queue 3")
        answer(4, "a4", ts=T + 20)
        status(4, "cancelled", ts=T + 21)
        self.assertIsNone(bell.check(now=T + 30))  # undone: nothing to ring for

    def test_rings_for_a_recheck_as_for_any_answer(self):
        bell = asks.Doorbell()
        answer(1, "pr:acme/web#906", option="recheck")
        self.assertIsNone(bell.check(now=T + 1))  # still undoable from the toast
        self.assertEqual(bell.check(now=T + asks.SETTLE), "dash-queue 1")

    def test_rings_for_a_pr_rows_button_as_for_any_answer(self):
        bell = asks.Doorbell()
        answer(1, "pr:acme/api#75", option="go", head="8d1c2b3a4f5e")
        self.assertIsNone(bell.check(now=T + 1))
        self.assertEqual(bell.check(now=T + asks.SETTLE), "dash-queue 1")

    def test_only_a_stat_while_nothing_changed(self):
        bell = asks.Doorbell()
        answer(1, "a1")
        bell.check(now=T + asks.SETTLE)
        with mock.patch("athena_lib.asks._lines", side_effect=AssertionError("read the file")):
            self.assertIsNone(bell.check(now=T + 60))

    def test_a_restarted_watch_rings_for_what_is_still_queued(self):
        answer(1, "a1")
        answer(2, "a2")
        status(2, "done")
        answer(3, "w-abc-1", discuss=True)
        first = asks.Doorbell()
        self.assertEqual(first.check(now=T + 60), "dash-queue 3")
        restarted = asks.Doorbell()
        self.assertEqual(restarted.check(now=T + 120), "dash-queue 3")
        asks.done("a1", now=T + 120)
        asks.done("w-abc-1", now=T + 120)
        self.assertIsNone(asks.Doorbell().check(now=T + 180))

    def test_twelve_rapid_answers_all_reach_the_queue(self):
        # Twelve clicks inside two seconds: the bell rings once per second at most as they settle, each ring a new
        # line (the waker's dedupe never drops one), and the queue HQ reads then holds all twelve, in order.
        bell, rings = asks.Doorbell(), []
        for i in range(12):
            answer(i + 1, f"a{i + 1}", ts=T + i * 0.15, discuss=True)
            if i in (3, 7):
                rings.append(bell.check(now=T + i * 0.15))
        for step in range(10):
            rings.append(bell.check(now=T + 2 + step))
        rings = [r for r in rings if r]
        seqs = [int(r.split()[1]) for r in rings]
        self.assertLessEqual(len(rings), 3)
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(rings[-1], "dash-queue 12")
        self.assertEqual([e["seq"] for e in asks.queue(now=T + 12)], list(range(1, 13)))

    def test_watch_prints_the_doorbell_line(self):
        answer(1, "a1")
        for tagged, want in ((True, '{"line": "dash-queue 1", "wake": true}\n'), (False, "dash-queue 1\n")):
            out = io.StringIO()
            with redirect_stdout(out), mock.patch("athena_lib.asks.time.time", return_value=T + 60):
                watch.ring(asks.Doorbell(), tagged=tagged)
            self.assertEqual(out.getvalue(), want)
        self.assertEqual(watch.classify("dash-queue 41"), "wake")

    def test_a_queue_it_cannot_read_is_told_once(self):
        bell, out = asks.Doorbell(), io.StringIO()
        with redirect_stdout(out), mock.patch.object(bell, "check", side_effect=ValueError("bad")):
            watch.ring(bell, tagged=True)
            watch.ring(bell, tagged=True)
        self.assertEqual(out.getvalue().count("dash queue: bad"), 1)


if __name__ == "__main__":
    unittest.main()
