"""REX-PALETTE: athena's actions in Rex's command palette, the athena side.

palette.lua asks the questions in Rex; everything it offers, every answer it may give and every write is decided here, in
athena/caps/palette. The Lua itself runs against a real Rex server in scripts/smoke.sh.
"""
import http.client
import json
import re
import threading
import time
from pathlib import Path
from unittest import mock

from harness import Case, ROOT, spawn_record

from athena.cli import UserError
from athena.kernel import caps, lab, store, world

REX_ICONS = set("document folder branch terminal code server database package globe link person tag clock star gear plus".split())


def palette():
    return caps.get("palette")


def items():
    return caps.get("items")


class Install(Case):
    def test_it_adds_the_line_once_and_keeps_the_rest_of_the_config(self):
        target = self.tmp / "rex" / "init.lua"
        target.parent.mkdir()
        target.write_text('rex.bind("cmd+d", "pane.split", { direction = "right" })\n')
        self.assertEqual(palette().install(target, explicit=True), "installed")
        first = target.read_bytes()
        self.assertEqual(palette().install(target, explicit=True), "already")
        self.assertEqual(target.read_bytes(), first, "a second install changes nothing")
        lines = target.read_text().splitlines()
        self.assertEqual(lines[0], 'rex.bind("cmd+d", "pane.split", { direction = "right" })')
        self.assertEqual(lines[1:], [palette().line()])
        self.assertEqual(palette().installed(target), "installed")

    def test_a_missing_file_is_made(self):
        target = self.tmp / "nowhere" / "rex" / "init.lua"
        self.assertEqual(palette().installed(target), "no file")
        self.assertEqual(palette().install(target, explicit=True), "installed")
        self.assertEqual(target.read_text(), palette().line() + "\n")

    def test_a_line_for_another_copy_or_state_dir_is_replaced_in_place(self):
        target = self.tmp / "init.lua"
        old = palette().line(lua="/old/athena/caps/palette/palette.lua", state_dir="/old/state")
        target.write_text(f"-- mine\n{old}\nrex.bind('cmd+k', 'session.switch')\n{old}\n")
        self.assertEqual(palette().installed(target), "another")
        self.assertEqual(palette().install(target, explicit=True), "updated")
        self.assertEqual(target.read_text().splitlines(), ["-- mine", palette().line(), "rex.bind('cmd+k', 'session.switch')"])

    def test_the_line_loads_this_file_for_this_state_dir_and_survives_its_absence(self):
        said = palette().line()
        self.assertIn(f'dofile("{palette().LUA}")', said)
        self.assertIn(f'state_dir = "{store.state_dir()}"', said)
        self.assertIn("pcall(function()", said, "a vanished file only warns: the rest of Rex's config still loads")
        self.assertTrue(said.endswith(palette().MARK))
        self.assertEqual(palette().LUA, Path(palette().__file__).with_name("palette.lua"))
        self.assertTrue(palette().LUA.is_file())

    def test_the_path_is_written_as_athena_was_reached_never_resolved(self):
        """Through `current`, as store.ROOT is: a line naming one release would dangle once the next one replaces it."""
        real = self.tmp / "releases" / "1.0-abc"
        real.mkdir(parents=True)
        current = self.tmp / "current"
        current.symlink_to(real)
        said = palette().line(lua=current / "athena/caps/palette/palette.lua")
        self.assertIn(str(current), said)
        self.assertNotIn(str(real), said)

    def test_quotes_and_backslashes_are_escaped_and_a_control_character_refused(self):
        said = palette().line(lua='/a "b"\\c/palette.lua', state_dir="/s")
        self.assertIn('dofile("/a \\"b\\"\\\\c/palette.lua")', said)
        with self.assertRaises(UserError):
            palette().line(lua="/a\nb.lua")

    def test_rexs_own_config_is_the_real_worlds_so_a_shut_door_refuses_it(self):
        target = palette().config_path()
        with self.assertRaises(world.Off):
            palette().install(target, explicit=False)
        self.assertFalse(target.exists())
        out = self.cli("palette", "install")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("writing Rex's config is off here", out.stderr)

    def test_a_lab_worker_never_writes_a_config_under_the_real_home(self):
        self.assertTrue(lab.active())
        target = Path(lab.real_home()) / ".config" / "rex" / "init.lua"
        with self.assertRaises(UserError) as said:
            palette().install(target, explicit=True)
        self.assertIn("scratch Rex config", str(said.exception))

    def test_where_rex_reads_its_config(self):
        self.assertEqual(palette().config_path({"REX_CONFIG": "/x/init.lua", "HOME": "/h"}), Path("/x/init.lua"))
        self.assertEqual(palette().config_path({"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"}), Path("/xdg/rex/init.lua"))
        self.assertEqual(palette().config_path({"HOME": "/h"}), Path("/h/.config/rex/init.lua"))

    def test_the_command_installs_and_shows(self):
        target = self.tmp / "init.lua"
        out = self.cli("palette", "install", "--config", str(target))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("rex config reload", out.stdout)
        self.assertIn("already loads them", self.cli("palette", "install", "--config", str(target)).stdout)
        shown = self.cli("palette", env={"REX_CONFIG": str(target)})
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(f"{target}: loads them", shown.stdout)
        for name, title in palette().ACTIONS:
            self.assertIn(name, shown.stdout)
        self.assertIn("does not exist yet", self.cli("palette", env={"REX_CONFIG": str(self.tmp / "none.lua")}).stdout)


class Decisions(Case):
    def setUp(self):
        super().setUp()
        add = items().add
        self.plain = add("Ship the docs first?", name="docs first", options=["Yes", "No"])
        self.rec = add("Which port?", name="port", options=["2843", "2844"], rec="2844")
        self.request = add("Look into the flaky test", name="flaky")
        self.theirs = add("athena's own call", name="theirs", kind="decision", next_="athena", options=["A", "B"])
        self.done = add("Done already?", name="done", options=["Yes", "No"])
        items().resolve(self.done["id"])
        self.later = add("Later?", name="later", options=["Yes", "No"])
        items().snooze(self.later["id"], "2h")
        self.prompt = add("Allow rm?", name="permission", options=["Approve", "Deny"])
        data = items().load()
        next(i for i in data["items"] if i["id"] == self.prompt["id"])["permission"] = {"worker": "w", "command": "rm -rf x"}
        items().save(data)

    def test_only_the_decisions_waiting_for_the_human_are_offered(self):
        got = palette().decisions()
        self.assertEqual(sorted(i["id"] for i in got["items"]), sorted([self.plain["id"], self.rec["id"]]))
        first = next(i for i in got["items"] if i["id"] == self.plain["id"])
        self.assertEqual(first, {"id": self.plain["id"], "title": "docs first", "subtitle": "Ship the docs first?", "icon": "tag",
                                 "detail": self.plain["id"]})
        self.assertEqual(set(got["answers"]), {self.plain["id"], self.rec["id"]})

    def test_each_takes_its_options_with_the_recommended_one_first_and_a_typed_reply(self):
        takes = palette().decisions()["answers"][self.rec["id"]]
        self.assertEqual(takes["title"], "Answer: port")
        self.assertTrue(takes["reply"])
        self.assertEqual(takes["options"], [{"id": "2844", "title": "2844", "icon": "star", "detail": "recommended"},
                                            {"id": "2843", "title": "2843"}])
        plain = palette().decisions()["answers"][self.plain["id"]]["options"]
        self.assertEqual([o["id"] for o in plain], ["yes", "no"])

    def test_an_answered_decision_leaves_the_list(self):
        palette().answer(self.plain["id"], option="yes")
        self.assertNotIn(self.plain["id"], [i["id"] for i in palette().decisions()["items"]])

    def test_listing_writes_nothing_not_even_a_due_default(self):
        auto = items().add("Low stakes?", name="auto", options=["Yes", "No"], rec="yes", auto=True, now=time.time() - 3 * 86400)
        actions = store.path("actions.jsonl")
        before = actions.read_bytes() if actions.exists() else b""
        offered = [i["id"] for i in palette().decisions()["items"]]
        self.assertIn(auto["id"], offered)
        self.assertEqual(actions.read_bytes() if actions.exists() else b"", before)

    def test_at_most_what_a_pick_takes(self):
        with mock.patch.object(palette(), "PICK_MAX", 1):
            self.assertEqual(len(palette().decisions()["items"]), 1)

    def test_none_waiting_is_an_empty_list(self):
        for item in (self.plain, self.rec):
            items().resolve(item["id"])
        self.assertEqual(palette().decisions(), {"items": [], "answers": {}})

    def test_icons_are_rexs_own(self):
        got = palette().decisions()
        used = {i.get("icon") for i in got["items"]} | {o.get("icon") for a in got["answers"].values() for o in a["options"]}
        self.assertLessEqual(used - {None}, REX_ICONS)


class Answer(Case):
    def setUp(self):
        super().setUp()
        self.a = items().add("Ship the docs first?", name="docs first", options=["Yes", "No"])
        self.b = items().add("Ship the tests first?", name="tests first", options=["Yes", "No"])

    def lines(self) -> list:
        return [json.loads(line) for line in store.path("actions.jsonl").read_text().splitlines()]

    def dash_answer(self, body):
        """The page's own POST, to the dashboard's own server."""
        from athena.caps.dash import server
        dash, httpd = server.start(store.state_dir(), 0, poll=3600.0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(dash.stop)
        base = f"http://127.0.0.1:{dash.port}"
        conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=15)
        conn.request("POST", "/api/action", body=json.dumps(body),
                     headers={"Origin": base, "Content-Type": "application/json", "X-Athena-Token": dash.token, "Connection": "close"})
        self.assertEqual(conn.getresponse().status, 202)
        conn.close()

    def test_an_answer_is_the_dashboards_own_line_and_queue_entry(self):
        said = palette().answer(self.a["id"], option="yes")
        self.assertEqual(said["said"], "Answered docs first: Yes. athena takes it up in a few seconds.")
        self.dash_answer({"item": self.b["id"], "option": "yes"})
        mine, page = self.lines()
        self.assertEqual(sorted(mine), sorted(page), "the same fields as the page's answer")
        self.assertEqual({k: v for k, v in mine.items() if k not in ("seq", "ts", "item")},
                         {k: v for k, v in page.items() if k not in ("seq", "ts", "item")})
        self.assertEqual(mine["seq"], said["seq"])
        later = time.time() + 7  # past the page's undo window, as HQ reads it
        queue = items().queue(now=later)
        self.assertEqual([e["item"] for e in queue], [self.a["id"], self.b["id"]])
        strip = lambda e: {k: v for k, v in e.items() if k not in ("seq", "item", "at", "ask")}  # noqa: E731
        self.assertEqual(strip(queue[0]), strip(queue[1]))
        self.assertEqual(queue[0]["option"], {"id": "yes", "label": "Yes", "do": "reply"})

    def test_a_typed_reply_is_the_pages_reply(self):
        palette().answer(self.a["id"], text="  only after the tests  ")
        self.dash_answer({"item": self.b["id"], "text": "  only after the tests  "})
        mine, page = self.lines()
        self.assertEqual((mine["text"], page["text"]), ("only after the tests", "only after the tests"))
        self.assertEqual(sorted(mine), sorted(page))
        self.assertEqual(items().queue(now=time.time() + 7)[0]["reply"], "only after the tests")

    def test_what_the_page_would_refuse_is_refused_and_nothing_is_written(self):
        for kwargs, words in (({"option": "maybe"}, "the page does not offer that answer"), ({"text": "   "}, "a reply is 1 to"),
                              ({"text": "x" * 2001}, "a reply is 1 to")):
            with self.assertRaises(UserError) as said:
                palette().answer(self.a["id"], **kwargs)
            self.assertIn(words, str(said.exception))
        self.assertFalse(store.path("actions.jsonl").exists())

    def test_only_a_decision_waiting_for_the_human(self):
        request = items().add("Look into it", name="flaky")
        theirs = items().add("athena's call", name="theirs", kind="decision", next_="athena", options=["A", "B"])
        for item_id in (request["id"], theirs["id"], "a999", "w-someone", "pr:o/r#1"):
            with self.assertRaises(UserError) as said:
                palette().answer(item_id, option="a")
            self.assertIn("is not a decision waiting for your answer", str(said.exception))
        palette().answer(self.a["id"], option="yes")
        with self.assertRaises(UserError):
            palette().answer(self.a["id"], option="no")
        self.assertEqual(len(self.lines()), 1, "a second answer to the same decision is refused")

    def test_a_permission_prompt_is_left_to_the_dashboard(self):
        data = items().load()
        next(i for i in data["items"] if i["id"] == self.a["id"])["permission"] = {"worker": "w", "command": "ls"}
        items().save(data)
        with self.assertRaises(UserError):
            palette().answer(self.a["id"], option="yes")

    def test_the_command_answers_in_json(self):
        out = self.cli("palette", "answer", self.a["id"], "--text=-starts with a dash")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["said"], "Replied to docs first: -starts with a dash. athena takes it up in a few seconds.")
        refused = self.cli("palette", "answer", self.b["id"], "--option", "maybe")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("athena: ", refused.stderr)
        self.assertEqual(self.cli("palette", "answer", self.b["id"]).returncode, 2, "an option or a text is required")


class WorkersAndOpen(Case):
    def setUp(self):
        super().setUp()
        repo = self.repo()
        spawn_record("alpha", self.tmp / "wt" / "alpha", repo, title="the alpha ticket", window="win-a")
        spawn_record("beta", self.tmp / "wt" / "beta", repo, window="win-b")
        store.write_json(store.path("workers", "alpha.json"),
                         {"state": "done", "report": {"status": "DONE", "pr": "https://github.com/acme/proj/pull/7"}})
        self.item = items().add("Look into it", name="flaky")

    def test_the_live_workers(self):
        got = palette().workers()["items"]
        self.assertEqual([w["id"] for w in got], sorted(["alpha", "beta"]))
        alpha = next(w for w in got if w["id"] == "alpha")
        self.assertEqual((alpha["title"], alpha["subtitle"], alpha["icon"]), ("alpha", "the alpha ticket", "terminal"))

    def test_none_live_is_an_empty_list(self):
        from athena.kernel import ledger
        for name in ("alpha", "beta"):
            ledger.append("retire", name)
        self.assertEqual(palette().workers(), {"items": []})
        self.assertEqual([t["id"] for t in palette().targets()["items"]], [f"item:{self.item['id']}"])

    def test_open_offers_each_workers_window_its_pull_request_and_every_open_item(self):
        ids = [t["id"] for t in palette().targets()["items"]]
        self.assertEqual(ids, ["worker:alpha", "pr:https://github.com/acme/proj/pull/7", "worker:beta", f"item:{self.item['id']}"])
        pr = palette().targets()["items"][1]
        self.assertEqual((pr["title"], pr["subtitle"], pr["icon"]), ("proj#7", "alpha's pull request", "branch"))

    def test_a_worker_is_shown_by_the_lua_in_the_app_that_asked(self):
        self.assertEqual(palette().open_target("worker:alpha"),
                         {"said": "Showing alpha.", "focus": {"session_id": "s1", "window_id": "win-a"}})

    def test_only_what_is_offered_opens(self):
        for target in ("pr:https://evil.example/x", "pr:https://github.com/acme/proj/pull/8", "worker:gamma", "item:a999", "x"):
            with self.assertRaises(UserError):
                palette().open_target(target)

    def test_a_page_opens_in_the_browser_only_through_the_door(self):
        with mock.patch("webbrowser.open") as browser:
            with self.assertRaises(world.Off):
                palette().open_target("pr:https://github.com/acme/proj/pull/7")
            browser.assert_not_called()
            with mock.patch.object(world, "require"):
                said = palette().open_target("pr:https://github.com/acme/proj/pull/7")
                browser.assert_called_once_with("https://github.com/acme/proj/pull/7")
                self.assertEqual(said, {"said": "Opened https://github.com/acme/proj/pull/7 in your browser."})
                dash = caps.get("dash")
                with mock.patch.object(dash, "on") as on:
                    palette().open_target(f"item:{self.item['id']}")
                on.assert_called_once()
                browser.assert_called_with(f"{dash.url()}item/{self.item['id']}")

    def test_more_than_a_pick_takes_is_cut_to_its_2000(self):
        data = items().load()
        now = int(time.time())
        data["items"] += [{"id": f"a{n}", "kind": "request", "text": f"item {n}", "name": f"item {n}", "next": "athena", "options": [],
                           "created": now, "updated": now, "log": []} for n in range(1000, 3001)]
        items().save(data)
        offered = palette().targets()["items"]
        self.assertEqual(len(offered), palette().PICK_MAX)
        self.assertEqual([t["id"] for t in offered[:3]], ["worker:alpha", "pr:https://github.com/acme/proj/pull/7", "worker:beta"],
                         "the workers first: the cut takes items off the end")

    def test_a_report_pr_that_is_not_a_github_pull_request_is_not_offered(self):
        store.write_json(store.path("workers", "beta.json"), {"state": "done", "report": {"pr": "javascript:alert(1)"}})
        self.assertFalse(any(t["id"].startswith("pr:javascript") for t in palette().targets()["items"]))


class TheLua(Case):
    """palette.lua's contract with athena; its behaviour runs against a real Rex server in scripts/smoke.sh."""

    def setUp(self):
        super().setUp()
        self.lua = palette().LUA.read_text()

    def test_it_defines_the_actions_athena_names(self):
        defined = re.findall(r'rex\.action\{\s*name = "([a-z_]+)",\s*title = "athena: ([^"]+)"', self.lua)
        self.assertEqual(defined, list(palette().ACTIONS))

    def test_it_calls_only_athena_commands_that_exist(self):
        verbs = set(re.findall(r'\{ "palette", "([a-z]+)"', self.lua))
        self.assertEqual(verbs, {"decisions", "workers", "targets", "answer", "open"})
        for verb in verbs:
            self.assertEqual(self.cli("palette", verb, "--help").returncode, 0, verb)
        for verb in ("send", "retire"):
            self.assertIn(f'{{ "{verb}", ', self.lua)
            self.assertIsNotNone(caps.find_command(verb))
        self.assertIn('{ "send", worker, "--", text }', self.lua, "a message that looks like a flag is still the message")

    def test_its_icons_are_rexs_own(self):
        self.assertLessEqual(set(re.findall(r'icon = "([a-z]+)"', self.lua)), REX_ICONS)

    def test_no_question_is_asked_where_rex_cannot_pause(self):
        """Rex answers rex.ui with failed inside pcall and xpcall."""
        self.assertNotIn("pcall", self.lua)

    def test_it_says_why_a_question_went_unanswered_for_every_reason_rex_gives(self):
        for reason in ("cancelled", "timeout", "no_client", "unsupported", "busy"):
            self.assertRegex(self.lua, rf"\n  {reason} = \"")
        self.assertIn('"the question could not be shown: "', self.lua, "and failed, with Rex's detail")
