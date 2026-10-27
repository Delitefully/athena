"""athena ask: needs-you.json, the migration from needs-you.md, the dashboard's answer queue and its doorbell."""
import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
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
        if argv[:1] == ("add",) and "--name" not in argv:
            argv = (*argv, "--name", "Test item")  # without one, add derives a name and hints (tests/test_items.py)
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
                         {"kind": "decision", "text": "Close ABC-4550 as a duplicate?", "why": "Same stack trace.", "pr": "acme/web#7"})
        self.assertEqual(item["options"], [{"id": "close-it", "label": "Close it", "do": "reply", "icon": "check"},
                                           {"id": "not-now", "label": "Not now", "do": "dismiss", "icon": "x"}])
        self.assertEqual(asks.show("a2")["kind"], "result")
        code, out, _ = self.run_cli("show", "a2")
        self.assertEqual(json.loads(out)["id"], "a2")
        self.assertEqual(self.run_cli("show", "a9")[0], 2)
        # Ids are never reused: one done and gone, the next is still new.
        asks.done("a2")
        self.assertEqual(asks.add("Next?"), "a3")

    def test_a_question_refuses_an_option_that_duplicates_discuss(self):
        for label in ("Discuss", " discuss  in athena "):
            with self.subTest(label=label):
                code, _, err = self.run_cli("add", "Take the recs?", "--option", label)
                self.assertEqual(code, 2, err)
                self.assertIn("Discuss in Athena", err)
        self.assertFalse(asks.asks_file().exists())
        # A note with an option that asks for a reply is a decision, which has Discuss in Athena already; one whose
        # options need nothing is a result, which has none, so its own Discuss option stays.
        self.assertEqual(self.run_cli("add", "Shipping at 3pm", "--note", "--option", "Discuss")[0], 2)
        self.assertEqual(self.run_cli("add", "Shipping at 3pm", "--note", "--option", "Discuss::dismiss")[:2], (0, "a1\n"))

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
        self.assertEqual(len(names), 25)
        sprite = (paths.PLUGIN_ROOT / "dash" / "page.html").read_text()
        self.assertEqual(sprite.count("<symbol "), 25)
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
                         [("a1", "decision"), ("a2", "result"), ("a3", "result"), ("a4", "decision")])
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
        self.assertEqual(got[0]["ask"], {"name": "Close ABC-4550", "kind": "decision", "text": "Close ABC-4550?"})
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
        self.assertEqual(asks.done("pr:acme/web#906", now=T + asks.SETTLE), {"item": "pr:acme/web#906", "resolved": False, "done": [1]})
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
        self.assertEqual(result, {"item": a1, "resolved": True, "done": [3]})
        self.assertEqual([i["id"] for i in asks.items()], [a2])
        folded = asks.fold(asks._lines())
        self.assertEqual({s: a["status"] for s, a in folded.items()}, {2: "cancelled", 3: "done", 4: "queued"})
        # --keep: its answers are done, the ask stays on the dashboard.
        self.assertEqual(asks.done(a1, keep=True, now=T + 3 + asks.SETTLE)["done"], [4])
        self.assertEqual(asks.done(a2, keep=True, now=T)["resolved"], False)
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
        self.assertEqual(json.loads(out), {"item": a1, "resolved": True, "done": [1]})
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
        # A head on anything but a go never reaches HQ, and a go without a head that is a commit is no go at all:
        # it reads as an option HQ never acts on.
        answer(4, "pr:acme/web#912", option="request-review", head="8d1c2b3a4f5e")
        answer(5, "pr:acme/api#75", option="go", head="main; rm -rf /")
        answer(6, "pr:acme/api#75", option="go")
        got = asks.queue(now=T + asks.SETTLE)
        self.assertNotIn("head", got[3])
        for e in got[4:]:
            self.assertEqual((e["option"], "head" in e), ({"id": "go-without-head", "label": "Merge", "do": "dismiss"}, False))


class AskDetailTest(unittest.TestCase):
    """An ask's title, its Markdown body and its previews: what the dashboard's expandable panel shows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        paths.ensure()
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def run_cli(self, *argv, stdin=""):
        if argv[:1] == ("add",) and "--name" not in argv:
            argv = (*argv, "--name", "Test item")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(["ask", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_body_keeps_its_lines_and_the_queue_carries_it(self):
        body = "**Rec: B.**\n\n| Option | Cost |\n|---|---|\n| A | 2 d |\n| B | 1 d |\n\n- one\n- two\n"
        (self.src / "body.md").write_text(body)
        code, out, err = self.run_cli("add", "Copilot catalog hosts: list copilot.cloud.microsoft? (rec B)",
                                      "--body-file", str(self.src / "body.md"))
        self.assertEqual((code, out), (0, "a1\n"), err)
        self.assertEqual(asks.show("a1")["body"], body.rstrip())
        # From stdin, and inline.
        self.assertEqual(self.run_cli("add", "Ship it?", "--body-file", "-", stdin="line one\n\nline two\n")[1], "a2\n")
        self.assertEqual(asks.show("a2")["body"], "line one\n\nline two")
        self.assertEqual(self.run_cli("add", "Ship it now?", "--body", "Short.\nTwo lines.")[1], "a3\n")
        self.assertEqual(asks.show("a3")["body"], "Short.\nTwo lines.")
        self.assertNotIn("body", asks.show(asks.add("No body?")))
        answer(1, "a1", option="dismiss")
        self.assertEqual(asks.queue(now=T + asks.SETTLE)[0]["ask"]["body"], body.rstrip())

    def test_ask_body_attaches_and_replaces(self):
        a1 = asks.add("Ship it?")
        (self.src / "b.md").write_text("First take.\n")
        self.assertEqual(self.run_cli("body", a1, "--file", str(self.src / "b.md"))[0], 0)
        self.assertEqual(asks.show(a1)["body"], "First take.")
        self.assertEqual(self.run_cli("body", a1, "--file", "-", stdin="Second take.\n\n- with a list\n")[0], 0)
        self.assertEqual(asks.show(a1)["body"], "Second take.\n\n- with a list")
        code, _, err = self.run_cli("body", "a9", "--file", "-", stdin="x")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.run_cli("body", a1)[0], 2)  # --file is required
        # An empty body clears it, unless the ask needs one for its options.
        self.assertEqual(self.run_cli("body", a1, "--file", "-", stdin="\n")[0], 0)
        self.assertNotIn("body", asks.show(a1))
        a2 = asks.add("Which?", body="Rec A.", options=["A", "B", "C"])
        with self.assertRaises(asks.AskError):
            asks.set_body(a2, "")

    def test_a_title_is_one_short_sentence(self):
        cases = json.loads((paths.PLUGIN_ROOT / "dash" / "testdata" / "titles.json").read_text())
        for text in cases["one"]:
            with self.subTest(text=text):
                self.assertTrue(asks.one_sentence(text))
        for text in cases["many"]:
            with self.subTest(text=text):
                self.assertFalse(asks.one_sentence(text))
                code, _, err = self.run_cli("add", text)
                self.assertEqual(code, 2)
                self.assertIn("one sentence", err)
                self.assertIn("--body", err)
        code, _, err = self.run_cli("add", "x" * 120 + "?")
        self.assertEqual(code, 2)
        self.assertIn("120", err)
        self.assertIn("--body", err)
        self.assertEqual(self.run_cli("add", "x" * 119 + "?")[0], 0)
        self.assertEqual(asks.TEXT_MAX, 120)

    def test_four_options_with_a_body_two_without(self):
        opts = ["--option", "A", "--option", "B", "--option", "C"]
        self.assertEqual(self.run_cli("add", "Which?", *opts)[0], 2)
        self.assertEqual(self.run_cli("add", "Which?", "--body", "Rec A.", *opts, "--option", "D")[0], 0)
        self.assertEqual(len(asks.show("a1")["options"]), 4)
        self.assertEqual(self.run_cli("add", "Which?", "--body", "Rec A.", *opts, "--option", "D", "--option", "E")[0], 2)

    def test_option_limits_match_the_dash(self):
        go = (paths.PLUGIN_ROOT / "dash" / "state.go").read_text()
        for name, value in (("optionsMax", asks.OPTIONS_MAX), ("optionsMaxBody", asks.OPTIONS_MAX_BODY),
                            ("labelMax", asks.LABEL_MAX), ("askTitleMax", asks.TEXT_MAX), ("askWhyMax", asks.WHY_MAX),
                            ("askBodyMax", asks.BODY_MAX), ("previewsMax", asks.PREVIEWS_MAX)):
            with self.subTest(name=name):
                self.assertRegex(go, rf"\b{name}\s*=\s*{value}\b")
        self.assertIn(f"discussLabel = regexp.MustCompile(`{asks.DISCUSS_LABEL}`)", go)

    def test_body_limit(self):
        self.assertEqual(asks.BODY_MAX, 8000)
        self.assertEqual(asks.add("Ship?", body="x" * 8000), "a1")
        with self.assertRaises(asks.AskError):
            asks.add("Ship?", body="x" * 8001)
        with self.assertRaises(asks.AskError):
            asks.set_body("a1", "x" * 8001)

    def test_needs_you_json_is_private(self):
        asks.add("Ship?")
        self.assertEqual(asks.asks_file().stat().st_mode & 0o777, 0o600)
        os.chmod(asks.asks_file(), 0o644)  # one written before this rule
        asks.add("And then?")
        self.assertEqual(asks.asks_file().stat().st_mode & 0o777, 0o600)
        answer(1, "a1", ts=T - asks.KEEP_DONE - 100)
        status(1, "done", ts=T - asks.KEEP_DONE - 100)
        answer(2, "a9")
        asks.done("a1", now=T)  # compacts actions.jsonl, rewriting it
        self.assertEqual(asks.actions_file().stat().st_mode & 0o777, 0o600)

    def test_a_preview_is_copied_into_the_state_dir(self):
        (self.src / "mock.html").write_text("<h1>mock</h1>")
        site = self.src / "My site"
        (site / "css").mkdir(parents=True)
        (site / "index.html").write_text("<link rel=stylesheet href=css/a.css>")
        (site / "css" / "a.css").write_text("h1{}")
        code, out, err = self.run_cli("add", "Which layout?", "--body", "Rec A.", "--preview", str(self.src / "mock.html"),
                                      "--preview", str(site))
        self.assertEqual((code, out), (0, "a1\n"), err)
        self.assertEqual(asks.show("a1")["previews"], ["mock.html", "My-site/"])
        root = asks.previews_dir() / "a1"
        self.assertEqual((root / "mock.html").read_text(), "<h1>mock</h1>")
        self.assertEqual((root / "My-site" / "css" / "a.css").read_text(), "h1{}")
        for d in (asks.previews_dir(), root, root / "My-site", root / "My-site" / "css"):
            self.assertEqual(d.stat().st_mode & 0o777, 0o700, d)
        self.assertEqual((root / "mock.html").stat().st_mode & 0o777, 0o600)
        # A resolved item keeps its copy for its full view; the copy goes when the item is archived.
        asks.done("a1", keep=True)
        self.assertTrue(root.exists())
        asks.done("a1", now=T)
        self.assertTrue(root.exists())
        asks.add("Next?", now=T + asks.KEEP_RESOLVED + 60)  # any later write archives what resolved long ago
        self.assertFalse(root.exists())
        self.assertEqual([i["id"] for i in asks.archived()], ["a1"])

    def test_a_preview_refuses_symlinks_and_size(self):
        (self.src / "secret").write_text("token")
        site = self.src / "site"
        site.mkdir()
        (site / "index.html").write_text("hi")
        (site / "leak").symlink_to(self.src / "secret")
        (self.src / "link.html").symlink_to(site / "index.html")
        big = self.src / "big.html"
        big.write_bytes(b"x" * (asks.PREVIEW_BYTES_MAX + 1))
        for path in (site, self.src / "link.html", big, self.src / "missing.html"):
            with self.subTest(path=path.name):
                code, _, err = self.run_cli("add", "Which?", "--preview", str(path))
                self.assertEqual(code, 2, err)
        self.assertFalse(asks.asks_file().exists())
        self.assertFalse((asks.previews_dir() / "a1").exists())
        too_many = [a for i in range(asks.PREVIEWS_MAX + 1) for a in ("--preview", str(self.src / "secret"))]
        self.assertEqual(self.run_cli("add", "Which?", *too_many)[0], 2)


    def test_a_folder_preview_opens_on_its_index_html(self):
        site = self.src / "site"
        site.mkdir()
        (site / "main.html").write_text("hi")
        code, _, err = self.run_cli("add", "Which?", "--preview", str(site))
        self.assertEqual(code, 2)
        self.assertIn("index.html", err)

    def test_a_long_preview_name_is_cut_keeping_its_extension(self):
        f = self.src / ("x" * 150 + ".html")
        f.write_text("hi")
        a1 = asks.add("Which?", previews=[str(f)])
        name = asks.show(a1)["previews"][0]
        self.assertEqual((len(name), name[-5:]), (asks.PREVIEW_NAME_MAX, ".html"))
        self.assertTrue((asks.previews_dir() / a1 / name).is_file())

    def test_a_preview_past_the_file_cap_stops_early(self):
        site = self.src / "big"
        site.mkdir()
        (site / "index.html").write_text("hi")
        for i in range(2 * asks.PREVIEW_FILES_MAX):
            (site / f"f{i}.txt").write_text("")
        with mock.patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as st:
            with self.assertRaises(asks.AskError):
                asks.add("Which?", previews=[str(site)])
        # Up to three stats a file it looked at (a symlink? a file? how big?), and it stops at the cap: not the whole folder.
        self.assertLessEqual(st.call_count, 3 * (asks.PREVIEW_FILES_MAX + 2))

    def test_a_hidden_symlink_is_left_out_not_refused(self):
        site = self.src / "site"
        site.mkdir()
        (site / "index.html").write_text("hi")
        (site / ".env").symlink_to(self.src)  # never copied, so never a reason to refuse
        a1 = asks.add("Which?", previews=[str(site)])
        self.assertEqual(sorted(p.name for p in (asks.previews_dir() / a1 / "site").iterdir()), ["index.html"])

    def test_a_copy_left_by_a_killed_add_is_swept(self):
        (self.src / "mock.html").write_text("hi")
        stale = asks.previews_root() / ".incoming-1-1"
        stale.mkdir()
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        fresh = asks.previews_root() / ".incoming-2-2"  # another add's, still copying
        fresh.mkdir()
        asks.add("Which?", previews=[str(self.src / "mock.html")])
        self.assertEqual((stale.exists(), fresh.exists()), (False, True))

    def test_previews_are_copied_outside_the_lock(self):
        (self.src / "mock.html").write_text("hi")
        held = []
        real = asks._copy_into

        def copy(plan, dest):
            fd = os.open(paths.state_dir() / "ask.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the dash could append an answer now
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(plan, dest)

        with mock.patch("athena_lib.asks._copy_into", side_effect=copy):
            a1 = asks.add("Which?", previews=[str(self.src / "mock.html")])
        self.assertEqual(held, [False])
        self.assertEqual([p.name for p in asks.previews_dir().iterdir()], [a1])  # no .incoming- left behind


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
