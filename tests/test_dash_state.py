"""The old athena dash's state, items and awaiting tests (its dash/state_test.go, items_test.go and awaiting_test.go), ported one Go
test to one method, each named as its Go test in snake_case, every assertion kept. The dash's fixture is tests/fixtures/dash/state."""
import copy
import json
import os
import shutil
import stat
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from harness import Case, ROOT

from athena.caps.dash import page, server, state, view
from athena.caps.dash.state import (Action, Applied, Ask, Hook, Option, PR, Move, Problem, Health, ReviewRequest, Snapshot,
                                        Stack, Dismissal, Watch, WatchWorker, Worker)
from athena.caps.dash.view import AWAY_AFTER, DEFAULT_AFTER, HEALTH_STALE, NEEDS_SHOWN, SHORT_MAX, STUCK_AFTER, build
from athena.kernel import ledger

FIXTURE = ROOT / "tests" / "fixtures" / "dash" / "state"
TITLES = ROOT / "tests" / "fixtures" / "dash" / "titles.json"
STATIC = ROOT / "athena" / "caps" / "dash" / "static"
FIXTURE_NOW = 1791500000


def write(path, text):
    with open(path, "wb") as f:
        f.write(text.encode() if isinstance(text, str) else text)


def ids(items) -> str:
    """A worker's row by its worker's name, an item's by its id, a PR's by its repo."""
    out = []
    for it in items:
        if it.id.startswith("w-"):
            out.append(it.id[2:])
        elif it.id.startswith("ask-"):
            out.append(it.id[4:])
        else:
            out.append(it.name)
    return " ".join(out)


def find(items, name) -> view.Item:
    """The row by its name, its worker's name or its item's id."""
    for it in items:
        if it.name == name or it.id == "w-" + name or it.id == "ask-" + name:
            return it
    return view.Item()


def between(s, start, end) -> str:
    i = s.find(start)
    if i < 0:
        return ""
    j = s[i:].find(end)
    if j < 0:
        return ""
    return s[i:i + j]


def followed(n, owner, label, kind, needs) -> PR:
    p = PR(number=n, url=f"https://github.com/acme/web/pull/{n}", state="OPEN", title=f"change {n}", owner=owner)
    if label:
        p.move = Move(label=label, kind=kind, needs=needs)
    return p


def awaiting_pr(n, base, label, kind, needs) -> PR:
    """An open acme/web PR on head branch b<n>, based on base, whose move is label (kind)."""
    p = followed(n, "", label, kind, needs)
    p.head, p.branch, p.base = f"sha{n}", f"b{n}", base
    return p


AWAITING_ALEX = "awaiting review · alex"


def snapshot_of(*prs) -> Snapshot:
    s = Snapshot(hooks={}, watch=Watch(ts=FIXTURE_NOW, prs={}))
    for p in prs:
        s.watch.prs[state.pr_key(p.url)] = p
    return s


def pill_keys(pills) -> str:
    """Each pill's key, with the keys stacked behind it in brackets."""
    out = []
    for p in pills:
        k = p.key
        if p.behind:
            k += "[" + " ".join(b.num.lstrip("#") for b in p.behind) + "]"
        out.append(k)
    return " ".join(out)


def decision_with_default() -> Ask:
    return Ask(id="a1", kind="decision", next="you", name="Flaky e2e", text="Re-run it?", rec="re-run", auto=True,
               options=[Option(id="re-run", label="Re-run", do="reply")], created=FIXTURE_NOW - 3600, updated=FIXTURE_NOW - 3600)


def queued(state_dir) -> list:
    return state.Store(state_dir).load().actions


class DashCase(Case):
    def copy_fixture(self):
        """testdata/state copied into a folder the test may change."""
        dst = self.tmp / f"dash-state-{len(list(self.tmp.glob('dash-state-*')))}"
        shutil.copytree(FIXTURE, dst)
        return dst

    def start(self, state_dir):
        """A copy of the fixture served on a loopback port, as the Go tests' start does: the clock at FIXTURE_NOW before the first
        refresh (server.start refreshes once on the real clock, which would apply the fixture's defaults), and no poll."""
        dash = server.Server(state_dir, 0, "acme", clock=lambda: FIXTURE_NOW, athena=False)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(dash))
        httpd.daemon_threads = True
        dash.port = httpd.server_address[1]
        dash.refresh(True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def stop():
            dash.stop()
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return dash, f"http://127.0.0.1:{dash.port}"

    def get(self, url):
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode()

    def item_post(self, dash, base, body, origin=None):
        req = urllib.request.Request(base + "/api/item", data=body.encode(), method="POST", headers={
            "Content-Type": "application/json", "Origin": origin if origin is not None else base, "X-Athena-Token": dash.token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            data = exc.read()
            try:
                return exc.code, json.loads(data)
            except ValueError:
                return exc.code, None

    def item_in(self, state_dir, item_id):
        try:
            data = (state_dir / state.ASKS).read_bytes()
            f = json.loads(data)
        except (OSError, ValueError) as exc:
            self.fail(f"needs-you.json: {exc}")
        if f.get("next") != 13:
            self.fail(f"needs-you.json: {data!r}")
        for it in f.get("items") or []:
            if it.get("id") == item_id:
                return it
        return None


class DashState(DashCase):
    """dash/state_test.go"""

    def test_load_fixture(self):
        s = state.Store(FIXTURE).load()
        self.assertTrue(len(s.workers) == 13 and len(s.hooks) == 12 and s.watch is not None and len(s.watch.workers) == 8
                        and len(s.watch.prs) == 10, f"workers {len(s.workers)} hooks {len(s.hooks)} watch {s.watch}")
        p = s.watch.prs.get("acme/web#906")
        self.assertTrue(p is not None and p.move is not None and p.move.label == "awaiting review · alex" and not p.move.needs
                        and p.title != "", f"stacked PR {p}")
        p = s.watch.prs["acme/web#908"]
        self.assertTrue(p.head == "9b1e07c4a2d8" and p.branch == "you/abc-147-step-3" and p.base == "you/abc-147-step-2",
                        f"a PR's head and branches {p}")
        self.assertTrue(len(s.stacks) == 1 and s.stacks[0].repo == "acme/web" and s.stacks[0].prs == [906, 908, 909], f"stacks {s.stacks}")
        p = s.watch.prs["acme/api#64"]
        self.assertTrue(p.owner == "abc-104" and p.move.needs and p.move.kind == "changes", f"retired worker's PR {p}")
        rr = s.watch.review_requests
        self.assertTrue(len(rr) == 1 and rr[0] == ReviewRequest(repo="acme/web", number=6170, title="Tidy the export queue", author="kim"),
                        f"review requests {rr}")
        self.assertTrue(len(s.history) == 3 and s.history[0].pr.state == "MERGED", f"history {s.history}")
        self.assertTrue(len(s.asks) == 12 and len(s.notices) == 0, f"asks {s.asks} notices {s.notices}")
        self.assertTrue(s.health is not None and s.health.ok and s.health.ts == FIXTURE_NOW - 30, f"health {s.health}")
        self.assertEqual(s.last_seen, FIXTURE_NOW - 2100, "the human was last seen typing r3")
        h = s.hooks["abc-106"]
        self.assertTrue(h.state == "blocked" and h.report.status == "NEEDS_CONTEXT" and
                        h.report.concerns[0] == "Which ceiling: 5 tries, or 24 hours?", f"hook {h}")

    def test_fold_ledger_mirrors_python(self):
        def ev(kind, name, ts, **kv):
            return {"event": kind, "name": name, "ts": ts, **kv}
        events = [
            ev("spawn", "a", 1, title="first", linear="ABC-1", goal_pending="the goal"),
            ev("update", "a", 2, goal_pending=None),
            ev("update", "ghost", 3),  # an update for a worker never spawned is ignored
            ev("spawn", "b", 4),
            ev("retire", "b", 5),
            ev("spawn", "b", 6, title="again"),  # a reused name starts a fresh record
            ev("failed", "c", 7),
            ev("retire", "d", 8),  # retired with no spawn on record
            {"event": "spawn", "ts": 9.0},
        ]
        ws = state.fold_ledger(events)
        got = {w.name: w for w in ws}
        self.assertTrue(len(ws) == 4 and "ghost" not in got, f"workers {ws}")
        a = got["a"]
        self.assertTrue(a.state == "live" and not a.goal_pending and a.spawned == 1 and a.updated == 2 and a.title == "first", f"a {a}")
        b = got["b"]
        self.assertTrue(b.state == "live" and b.title == "again" and b.spawned == 6, f"b {b}")
        self.assertTrue(got["c"].state == "failed" and got["d"].state == "retired" and got["d"].updated == 8, f"c {got['c']} d {got['d']}")
        # The Python fold it mirrors is athena's kernel/ledger.py, which reads the same ledger.jsonl: every worker the dash folds
        # reads the same there. The kernel adds one record the dash leaves out: an update for a worker never spawned is `damaged`.
        kernel = ledger.fold(events)
        self.assertEqual(set(kernel) - set(got), {"ghost"})
        self.assertEqual(kernel["ghost"]["state"], "damaged")
        for name, w in got.items():
            k = kernel[name]
            self.assertEqual((w.state, w.title, w.linear, w.spawned, w.updated, w.goal_pending),
                             (k.get("state"), k.get("title") or "", k.get("linear") or "", float(k.get("spawned") or 0),
                              float(k.get("updated") or 0), bool(k.get("goal_pending"))), name)

    def test_half_written_worker_file_keeps_last_good_state(self):
        d = self.copy_fixture()
        st = state.Store(d)
        st.load()
        path = d / "workers" / "abc-101.json"
        data = path.read_bytes()
        write(path, data[:len(data) // 2])
        s = st.load()
        self.assertEqual(s.hooks["abc-101"].state, "working", f"lost the last good state: {s.hooks.get('abc-101')}")
        self.assertTrue(len(s.notices) == 1 and "abc-101.json is unreadable; showing its last good state" in s.notices[0], f"notices {s.notices}")
        write(path, data)
        self.assertEqual(st.load().notices, [], "notice outlived the repair")

    def test_broken_file_with_no_good_state_is_skipped_quietly(self):
        d = self.copy_fixture()
        write(d / "workers" / "abc-200.json", '{"state": "work')
        write(d / "watch.json", "[]")
        s = state.Store(d).load()
        self.assertTrue("abc-200" not in s.hooks and s.watch is None, f"hook {s.hooks.get('abc-200')} watch {s.watch}")
        self.assertEqual("|".join(s.notices), "|".join(["watch.json is unreadable", "workers/abc-200.json is unreadable"]))
        # Without watch.json, no PR's move is known, so each worker shows its own state.
        v = build(s, 1791500000, "acme")
        self.assertTrue(len(v.in_progress) == 8 and len(v.needs_you) == 7,
                        f"a broken file blanked the page: {ids(v.needs_you)} / {ids(v.in_progress)}")

    def test_ledger_line_being_appended_is_skipped(self):
        d = self.copy_fixture()
        with open(d / "ledger.jsonl", "a") as f:
            f.write('{"event": "retire", "name": "abc-1')
        s = state.Store(d).load()
        self.assertTrue(len(s.workers) == 13 and s.workers[4].state == "live", f"workers {s.workers}")
        self.assertEqual(s.notices, ["ledger.jsonl: skipped 1 unreadable line"])

    def test_missing_state_dir_and_files(self):
        s = state.Store(self.tmp / "nope").load()
        self.assertTrue(len(s.workers) == 0 and len(s.notices) == 1 and s.notices[0].startswith("no athena state yet at "), s)
        empty = self.tmp / "empty"
        empty.mkdir()
        s = state.Store(empty).load()
        self.assertTrue(len(s.workers) == 0 and len(s.notices) == 0 and s.watch is None, s)
        v = build(s, 1, "acme")
        self.assertTrue(len(v.needs_you) + len(v.in_progress) + len(v.done) == 0 and v.title == "athena", v)

    def test_changed_sees_writes_new_files_and_deletes(self):
        d = self.copy_fixture()
        st = state.Store(d)
        self.assertTrue(st.changed(), "first poll must load")
        self.assertFalse(st.changed(), "nothing changed")
        write(d / "workers" / "abc-101.json", '{"state": "idle", "name": "abc-101"}')
        self.assertTrue(st.changed(), "missed a write")
        write(d / "workers" / "abc-300.json", "{}")
        self.assertTrue(st.changed(), "missed a new worker file")
        os.remove(d / "needs-you.json")
        self.assertTrue(st.changed(), "missed a delete")
        write(d / "notes.md", "not ours")
        self.assertFalse(st.changed(), "a file the page does not read is not a change")

    def test_parse_asks_drops_what_athena_ask_would_refuse(self):
        m = json.loads("""{"items": [
            {"id": "a1", "text": "Ship it?", "pr": "https://github.com/Acme/Web/pull/7", "options": [
                {"id": "yes", "label": "Yes", "icon": "check"}, {"id": "no", "label": "No", "icon": "rm -rf", "do": "dismiss"},
                {"id": "maybe", "label": "Maybe"}]},
            {"id": "a2", "kind": "note", "text": "FYI", "options": [{"id": "x", "label": "A label past sixteen"}, {"id": "dismiss", "label": "Ok"}]},
            {"id": "a1", "text": "a second a1"}, {"id": "Bad Id", "text": "t"}, {"id": "a3", "text": "  "}, "junk"]}""")
        got = state.parse_asks(m)
        self.assertEqual(len(got), 2, got)
        a = got[0]
        self.assertTrue(a.kind == "decision" and a.pr == "acme/web#7" and len(a.options) == 2 and
                        a.options[0] == Option("yes", "Yes", "check", "reply") and a.options[1] == Option("no", "No", "", "dismiss"),
                        f"an unknown icon shows none, a third option is dropped: {a}")
        b = got[1]
        self.assertTrue(b.kind == "result" and len(b.options) == 0, f"a long label and the reserved id are dropped: {b}")

    def test_parse_asks_drops_an_option_that_duplicates_discuss(self):
        """Every question has Discuss in Athena of its own, so an option labelled Discuss is dropped and frees its slot. A note has
        none, so it keeps its own."""
        m = json.loads("""{"items": [{"id": "a59", "text": "Take the recs?", "options": [
            {"id": "discuss", "label": "Discuss"}, {"id": "take", "label": "Take recs"},
            {"id": "discuss-2", "label": " discuss  in ATHENA "}, {"id": "later", "label": "Later"}]},
            {"id": "a61", "kind": "note", "text": "Shipping at 3pm", "options": [{"id": "discuss", "label": "Discuss"}]}]}""")
        got = state.parse_asks(m)
        self.assertTrue(len(got) == 2 and len(got[0].options) == 2 and got[0].options[0].id == "take" and got[0].options[1].id == "later",
                        f"a question's Discuss option is shown: {got}")
        self.assertTrue(len(got[1].options) == 1 and got[1].options[0].id == "discuss", f"a note loses its Discuss option: {got[1]}")

    def test_an_ask_with_a_body_carries_four_options_and_its_previews(self):
        m = json.loads("""{"items": [
            {"id": "a1", "text": "Which?", "body": "**Rec: B.**\\n\\n| A | B |\\n|---|---|\\n| 1 | 2 |\\n", "options": [
                {"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}, {"id": "e", "label": "E"}],
             "previews": ["mock.html", "site/", "../etc/passwd", "/abs", ".hidden", "a/b", "x.html", "y.html", 7]},
            {"id": "a2", "text": "Which?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}]}]}""")
        got = state.parse_asks(m)
        a = got[0]
        self.assertTrue(len(a.options) == 4 and a.options[3].id == "d" and a.body == "**Rec: B.**\n\n| A | B |\n|---|---|\n| 1 | 2 |",
                        f"a body allows four options: {a}")
        self.assertEqual(" ".join(got[0].previews), "mock.html site/ x.html y.html", f"only plain names, at most {state.PREVIEWS_MAX}")
        self.assertEqual(len(got[1].options), 2, f"no body, two options: {got[1]}")

    def test_a_title_is_one_sentence_as_athena_ask_holds_it(self):
        """The title rule is caps/items.py's: both read tests/fixtures/dash/titles.json."""
        cases = json.loads(TITLES.read_text())
        for s in cases["one"]:
            title, long = Ask(text=s).title()
            self.assertTrue(not long and title == s, f"one sentence, as it is: {s!r} -> {title!r}")
        for s in cases["many"]:
            _, long = Ask(text=s).title()
            self.assertTrue(long, f"more than one sentence: {s!r}")
        # An ask from before the rule still shows: its first sentence, its whole text in its panel.
        for text, want in {
            "Start **ABC-111** now, or after ABC-101 merges? The 5-hour quota is at 81%.": "Start **ABC-111** now, or after ABC-101 merges?",
            "Rename it to \"export v2.\" Then ship it?": "Rename it to \"export v2.\"",
            "word " * 40 + "end?": "word " * 23 + "word…",
        }.items():
            title, long = Ask(text=text).title()
            self.assertTrue(long and title == want, f"{text!r} -> {title!r}, want {want!r}")

    def test_fold_actions_by_seq(self):
        got = state.fold_actions([
            {"seq": 1.0, "ts": 10.0, "item": "a1", "option": "yes", "status": "queued"},
            {"seq": 2.0, "ts": 11.0, "item": "a2", "text": "hi", "status": "queued"},
            {"seq": 1.0, "ts": 12.0, "status": "cancelled"},
            {"seq": 3.0, "ts": 13.0, "item": "w-abc-1", "discuss": True, "status": "queued"},
            {"seq": 3.0, "ts": 14.0, "status": "done"},
            {"seq": 9.0, "status": "done"},  # a status for a seq never queued (compacted away) is skipped
            {"seq": 2.0, "item": "a9", "status": "queued"},
        ])
        want = [Action(seq=1, ts=10, item="a1", option="yes", status="cancelled"), Action(seq=2, ts=11, item="a2", text="hi", status="queued"),
                Action(seq=3, ts=13, item="w-abc-1", discuss=True, status="done")]
        self.assertEqual(got, want)

    def test_dash_config_is_read_live_and_checked(self):
        d = self.copy_fixture()
        st = state.Store(d)
        self.assertEqual(st.load().linear_workspace, "", "no dash.json")
        write(d / "dash.json", '{"linear_workspace": "acme"}')
        self.assertTrue(st.changed() and st.load().linear_workspace == "acme", "dash.json not picked up")
        write(d / "dash.json", '{"linear_workspace": "evil.test/x?"}')
        s = st.load()
        self.assertTrue(s.linear_workspace == "" and len(s.notices) == 1,
                        f"a workspace that is not a slug must be refused: {s.linear_workspace!r} {s.notices}")

    def test_load_from_several_goroutines_while_files_change(self):
        """The poll and the page's handlers (a full view, a search) all call load, so one Store is read from several threads while
        athena writes, half-writes and removes files under it."""
        d = self.copy_fixture()
        st = state.Store(d)
        path = d / "workers" / "abc-101.json"
        data = path.read_bytes()
        stop = threading.Event()

        def churn():
            i = 0
            while not stop.is_set():
                try:
                    if i % 3 == 0:
                        write(path, data[:len(data) // 2])
                    elif i % 3 == 1:
                        os.remove(path)
                    else:
                        write(path, data)
                except OSError:
                    pass
                i += 1
        errors = []

        def reader():
            try:
                for _ in range(50):
                    st.load()
            except Exception as exc:  # noqa: BLE001: a load must never fail
                errors.append(exc)
        churner = threading.Thread(target=churn)
        churner.start()
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for r in readers:
            r.start()
        for r in readers:
            r.join()
        stop.set()
        churner.join()
        self.assertEqual(errors, [])
        write(path, data)
        s = st.load()
        self.assertTrue(s.hooks["abc-101"].state == "working" and len(s.notices) == 0, f"after the churn: {s.hooks.get('abc-101')} {s.notices}")


class DashItems(DashCase):
    """dash/items_test.go"""

    def test_the_health_dot_is_never_green_without_a_fresh_check(self):
        now = float(FIXTURE_NOW)
        local = datetime.fromtimestamp
        for h, st, text in [
            (None, "unknown", "doctor hasn't reported"),
            (Health(ts=now - HEALTH_STALE - 1, ok=True), "unknown",
             "doctor not running · last check " + local(now - HEALTH_STALE - 1).strftime("%H:%M")),
            (Health(ts=now - 3 * 24 * 3600, ok=True), "unknown", "doctor not running · last check " + local(now - 3 * 24 * 3600).strftime("%a %H:%M")),
            (Health(ts=now - 30, ok=True), "ok", "all ok"),
            (Health(ts=now - 30, ok=False), "bad", "something is wrong"),
            (Health(ts=now - 30, problems=[Problem(check="dash", detail="down since 09:12"), Problem(check="board")]), "bad",
             "dash: down since 09:12 · +1 more"),
        ]:
            got = view.health_view(h, now)
            self.assertTrue(got.state == st and got.text == text, f"{h}: {got}")
        # athena: health.json dated in the future is a clock that moved back, never ok.
        got = view.health_view(Health(ts=now + 3600, ok=True), now)
        self.assertEqual(got.state, "unknown", got)
        # Stale or missing, the tooltip names the fix: the service that runs doctor every minute, or one check now (athena's).
        for h in (None, Health(ts=now - HEALTH_STALE - 1, ok=True)):
            v = view.health_view(h, now)
            self.assertTrue("athena services install" in v.title and "athena doctor --fix" in v.title, f"{h}: the tooltip names no fix: {v.title!r}")
        # health.json as v2's doctor writes it: a problem makes it not ok, whatever its ok says.
        m = json.loads("""{"ts": 1791499990, "ok": true, "problems": [{"check": "hq", "detail": "messaging socket missing", "since": 1791499000}],
            "checks": {"hq": {"ok": false, "detail": "messaging socket missing", "fixed": false}}}""")
        h = state.parse_health(m)
        self.assertTrue(not h.ok and len(h.problems) == 1 and h.problems[0].since == 1791499000, h)
        v = view.health_view(h, now)
        self.assertTrue(v.state == "bad" and v.text == "hq: messaging socket missing" and v.title == "hq: messaging socket missing", v)
        # On the page: one dot, and a change to health.json is pushed.
        d = self.copy_fixture()
        dash, base = self.start(d)
        _, body = self.get(base + "/")
        self.assertIn('<p class="health ok" title="athena doctor&#39;s last check: ', body, "no green dot")
        write(d / state.HEALTH, '{"ts": 1791499990, "ok": false, "problems": [{"check": "board", "detail": "watcher gone"}]}')
        self.assertTrue(dash.refresh(False), "a new health.json is not a change")
        main, _, _ = dash.snapshot()
        self.assertIn('<p class="health bad" title="board: watcher gone" role="status"><span class="dot" aria-hidden="true"></span>board: watcher gone</p>',
                      main, main[:400])
        os.remove(d / state.HEALTH)
        dash.refresh(False)
        main, _, _ = dash.snapshot()
        self.assertTrue('<p class="health unknown" title="athena doctor has not reported' in main and
                        "</span>doctor hasn&#39;t reported</p>" in main, "no health.json reads as unknown, saying so")

    def test_a_default_applies_only_when_due_and_never_to_a_call_with_stakes(self):
        """Decision 4: a default applies on its own after 12 h, or 2 h once the human has been away, and only to a reversible,
        low-stakes call: never one marked security, customer, money or one-way, whatever the file says."""
        now = float(FIXTURE_NOW)

        def due(s, at):
            return " ".join(a.id for a in view.due_defaults(s, at))
        a = decision_with_default()
        s = Snapshot(asks=[a], last_seen=now - 60)
        self.assertEqual(due(s, now), "", "an hour old, the human here")
        self.assertEqual(due(s, a.created + DEFAULT_AFTER), "a1", "12 h on")
        # Away since 2 h ago: 2 h after the later of the item's filing (an hour ago) and their leaving, so in an hour.
        s.last_seen = now - AWAY_AFTER
        self.assertEqual(due(s, now), "", "away, the item an hour old")
        self.assertEqual(view.apply_at(a, s.last_seen, now), now + 3600, "apply_at")
        self.assertEqual(due(s, now + 3600), "a1", "away, 2 h after filing")
        s.last_seen = now - 3 * AWAY_AFTER  # gone long before it was filed
        self.assertEqual(due(s, a.created + AWAY_AFTER), "a1", "away before it was filed")
        s.last_seen = 0  # nothing known of the human: only the 12 h
        self.assertEqual(due(s, now + 3 * 3600), "", "no activity known")
        # Never for a call with stakes, even marked auto by hand in the file, past both times.
        for stake in ("security", "customer", "money", "one-way"):
            b = copy.deepcopy(a)
            b.stakes = [stake]
            self.assertEqual(due(Snapshot(asks=[b], last_seen=now - 10 * AWAY_AFTER), now + 2 * DEFAULT_AFTER), "", f"{stake}: applied")
        m = json.loads("""{"items": [{"id": "a1", "kind": "decision", "name": "Rotate key", "text": "Rotate it?", "rec": "yes", "auto": true,
            "stakes": ["money"], "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}""")
        p = state.parse_asks(m)
        self.assertTrue(not p[0].auto and len(p[0].stakes) == 1, f"parsed {p[0]}")

        # Nor once answered, snoozed, applied, resolved, or for an option it does not offer, or anything but a decision.
        def answered(s):
            s.actions = [Action(seq=1, item="a1", option="re-run", status="queued")]

        def done(s):
            s.actions = [Action(seq=1, item="a1", discuss=True, status="done")]

        def snoozed(s):
            s.asks[0].snooze_until, s.asks[0].snoozed_at = now + 2 * DEFAULT_AFTER, now

        def applied(s):
            s.asks[0].applied = Applied(option="re-run")

        def resolved(s):
            s.asks[0].resolved = now

        def no_rec(s):
            s.asks[0].rec = "nope"

        def a_result(s):
            s.asks[0].kind = "result"
        for name, change in {"answered": answered, "done": done, "snoozed": snoozed, "applied": applied, "resolved": resolved,
                             "no rec": no_rec, "a result": a_result}.items():
            s = Snapshot(asks=[decision_with_default()], last_seen=now - 60)
            change(s)
            self.assertEqual(due(s, now + DEFAULT_AFTER), "", f"{name}: applied")
        # A cancelled answer (undone within its seconds) leaves it due.
        s = Snapshot(asks=[a], actions=[Action(seq=1, item="a1", option="re-run", status="cancelled")])
        self.assertEqual(due(s, now + DEFAULT_AFTER), "a1", "cancelled")

    def test_the_server_applies_a_due_default_once_and_shows_it(self):
        """The server applies a due default as an answer for athena, marked auto, once; the item moves to Ready for you, saying so,
        with Done and Undo; the human's later activity counts, an auto answer does not."""
        d = self.copy_fixture()
        dash, _ = self.start(d)
        dash.now = lambda: FIXTURE_NOW - 2000 + DEFAULT_AFTER + 5
        dash.refresh(True)
        dash.refresh(True)
        got = queued(d)
        self.assertTrue(len(got) == 1 and got[0].item == "a1" and got[0].option == "later" and got[0].auto and got[0].status == "queued", got)
        main, v, _ = dash.snapshot()
        r = find(v.ready, "Export queue start")
        self.assertTrue(r.label == "athena is taking the rec on its own: After ABC-101" and r.actions is not None and r.actions.done and
                        r.actions.undo == "After ABC-101", f"a1 {r}")
        self.assertIn('data-option="undo-default" data-tip="Undo After ABC-101: athena takes it back"', main, "no Undo on the row")
        self.assertEqual(dash.store.load().last_seen, FIXTURE_NOW - 2100, "an auto answer is not the human's activity")
        # The security item (a6) is a week old and marked: never applied.
        self.assertFalse(any(a.item == "a6" for a in got), "a6 applied")
        data = (d / state.ACTIONS).read_text()
        self.assertIn('"auto":true', data)

    def test_done_and_snooze_work_without_hq(self):
        """Done and Snooze are the dash's own: they work while HQ is busy or down, write needs-you.json themselves under its lock,
        keep every other field, and never queue anything for athena."""
        d = self.copy_fixture()
        dash, base = self.start(d)
        code, out = self.item_post(dash, base, '{"item": "a3", "do": "done"}')
        self.assertEqual(code, 200, out)
        a3 = self.item_in(d, "a3")
        self.assertTrue(a3["resolved"] == FIXTURE_NOW and a3["resolved_by"] == "you" and a3.get("body") is not None and
                        a3["name"] == "Palette film", f"a3 {a3}")
        log = a3.get("log")
        self.assertTrue(isinstance(log, list) and len(log) == 1 and log[0]["text"] == "you marked it done", f"a3's timeline {log}")
        self.assertFalse((d / state.ACTIONS).exists(), "Done queued something for athena")
        self.assertEqual(stat.S_IMODE(os.stat(d / state.ASKS).st_mode), 0o600)
        # athena: the edit keeps needs-you.json's last generation beside it, as its own writer does.
        self.assertTrue((d / (state.ASKS + ".prev")).exists())
        _, v, _ = dash.snapshot()
        self.assertTrue(find(v.ready, "a3").id == "" and find(v.done, "a3").label == "you marked it done", "a3 did not move to Done today")
        # Undo is Reopen, for what the human marked done here.
        code, _ = self.item_post(dash, base, '{"item": "a3", "do": "reopen"}')
        self.assertTrue(code == 200 and self.item_in(d, "a3").get("resolved") is None, f"reopen: {code}")
        # Snooze: off the page until then; Wake brings it back.
        until = FIXTURE_NOW + 7200
        code, out = self.item_post(dash, base, '{"item": "a5", "do": "snooze", "until": %d}' % until)
        self.assertEqual(code, 200, out)
        sn = self.item_in(d, "a5").get("snooze") or {}
        self.assertTrue(sn.get("until") == until and sn.get("at") == FIXTURE_NOW + 1, f"a5 {self.item_in(d, 'a5')}")
        _, v, _ = dash.snapshot()
        self.assertTrue(find(v.needs_you, "a5").id == "" and v.snoozed == 2, f"a5 still shows; snoozed {v.snoozed}")
        dash.now = lambda: until + 1
        dash.refresh(True)
        _, v, _ = dash.snapshot()
        self.assertNotEqual(find(v.needs_you, "a5").id, "", "a5 is not back when its time is up")
        dash.now = lambda: FIXTURE_NOW
        dash.refresh(True)
        code, _ = self.item_post(dash, base, '{"item": "a5", "do": "wake"}')
        self.assertTrue(code == 200 and self.item_in(d, "a5").get("snooze") is None, f"wake: {code}")
        # What it refuses.
        for body, want in {
            '{"item": "a10", "do": "done"}': 409,  # resolved already
            '{"item": "a1", "do": "reopen"}': 409,  # open
            '{"item": "a99", "do": "done"}': 404,
            '{"item": "a1", "do": "snooze", "until": 1}': 400,  # in the past
            '{"item": "a1", "do": "snooze", "until": 9999999999}': 400,  # past 60 days
            '{"item": "a1", "do": "merge"}': 400,
            '{"item": "a1", "do": "done", "until": 1791503600}': 400,
            '{"item": "../x", "do": "done"}': 400,
            '{"item": "a1", "do": "done", "extra": true}': 400,
            '{"item": "a1", "do": "wake"}': 409,
        }.items():
            code, _ = self.item_post(dash, base, body)
            self.assertEqual(code, want, body)
        # The same checks as an answer: this page's origin and token only.
        code, _ = self.item_post(dash, base, '{"item": "a1", "do": "done"}', origin="null")
        self.assertEqual(code, 403, "a preview's POST")
        self.assertIsNone(self.item_in(d, "a1").get("resolved"), "a1 was resolved")

    def test_ages_and_due_dates(self):
        """Ages and due dates as the rows say them."""
        for seconds, want in {30: "now", 10 * 60: "10m", 2 * 3600: "2h", 7 * 24 * 3600: "7d"}.items():
            self.assertEqual(page.short(seconds), want, seconds)
        now = datetime(2026, 10, 27, 10, 0, 0)
        for due, want in {
            now - timedelta(hours=2): "overdue 2h",
            now + timedelta(minutes=40): "due in 40m",
            datetime(2026, 10, 27, 17, 30): "due 17:30",
            datetime(2026, 10, 28, 11, 30): "due tmrw",
            datetime(2026, 10, 29, 9, 0): "due Thu",
            datetime(2026, 11, 14, 9, 0): "due 14 Nov",
        }.items():
            self.assertEqual(page.due_text(due, now), want, due)
        js = (STATIC / "app.js").read_text()
        for fn in ("function short(", "function dueText("):
            self.assertIn(fn, js, "app.js lacks it: the page's own timer keeps these fresh")

    def _add_items(self, d, items):
        path = d / "needs-you.json"
        f = json.loads(path.read_bytes())
        f["items"] += items
        write(path, json.dumps(f))
        os.chmod(path, 0o600)

    def test_needs_you_folds_past_seven_rows(self):
        """Needs you shows at most NEEDS_SHOWN rows; the rest fold behind "n more", saying when the first default applies."""
        d = self.copy_fixture()
        # Two calls newer than the fixture's default (a1) push it below the fold, newest first.
        self._add_items(d, [{"id": i, "kind": "decision", "next": "you", "name": "New call " + i, "text": "Go ahead?",
                             "created": FIXTURE_NOW - 60 * (n + 1), "updated": FIXTURE_NOW - 60 * (n + 1)} for n, i in enumerate(["n1", "n2"])])
        _, base = self.start(d)
        _, body = self.get(base + "/")
        needs = between(body, '<section class="group needs"', "</section>")
        shown = between(needs, "<ul>", '<details class="more"')
        self.assertEqual(shown.count("<li id="), NEEDS_SHOWN, "rows before the fold")
        self.assertTrue('<details class="more" data-fold="needs"><summary>8 more · athena takes the rec at ' in needs and
                        "unless you object</summary>" in needs, needs)

    def test_a_push_keeps_an_open_fold_open(self):
        """A push replaces <main> (app.js render), and a fold the human opened stays open: each fold has a key that holds across pages,
        app.js reopens the folds open under that key before the page shows, and a fold that grew shows every row it now has. The page
        has no JS test setup, so this checks the page and the code that keeps the folds."""
        d = self.copy_fixture()
        dash, base = self.start(d)

        def folds():
            _, body = self.get(base + "/")
            return (between(body, '<details class="more" data-fold="needs">', "</details>"),
                    between(body, '<details class="group done" data-fold="done"', "</details>"))
        needs, done = folds()
        self.assertTrue(needs.count("<li id=") == 6 and done != "", f"before: needs fold {needs!r}, done fold {done!r}")
        self._add_items(d, [{"id": i, "kind": "decision", "next": "you", "name": "Grown " + i, "text": "Go ahead?",
                             "created": FIXTURE_NOW - 7200, "updated": FIXTURE_NOW - 7200} for i in ("g1", "g2")])
        dash.refresh(True)
        needs, done = folds()
        self.assertTrue(needs.count("<li id=") == 8 and done != "",
                        f"after the push, the folds keep their keys and the grown one holds all 8 rows: {needs!r}, {done!r}")
        js = (STATIC / "app.js").read_text()
        render = between(js, "function render(html) {", "\n}\n")
        save = render.find('main.querySelectorAll("details[data-fold][open]:not([data-filtered])")')
        swap = render.find("main.innerHTML = html;")
        reopen = render.find("main.querySelector(`details[data-fold=\"${CSS.escape(key)}\"]`)")
        filt = render.find("filter();")
        self.assertFalse(save < 0 or swap < save or reopen < swap or filt < reopen,
                         f"render keeps no open fold across a push (save {save}, swap {swap}, reopen {reopen}, filter {filt})")
        # Focus on a PR stacked behind a pill: its stack fans out (.held, as with focus inside) while focus goes back.
        refocus = between(js, "function refocus(at) {", "\n}\n")
        css = (STATIC / "style.css").read_text()
        self.assertTrue("refocus(focus)" in render and 'stack?.classList.add("held")' in refocus and
                        ".stack:is(:hover, :focus-within, .held) > .behind {" in css, "a push drops focus from a PR stacked behind a pill")

    def test_stakes_it_cannot_read_block_the_default(self):
        """Review round 1: stakes the dash cannot read block a default (fail closed): a string, the wrong case, an unknown value, a
        wrong type. Only an absent or empty stakes list lets auto through."""
        for stakes in ('"security"', '["Security"]', '["secrets"]', "[7]", '{"security": true}', '["security", "secrets"]', '"x"', "true"):
            m = json.loads('{"items": [{"id": "a1", "kind": "decision", "name": "Rotate key", "text": "Rotate it?", "rec": "yes", "auto": true, '
                           '"stakes": ' + stakes + ', "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}')
            a = state.parse_asks(m)[0]
            self.assertFalse(a.auto, f"stakes {stakes}: auto")
            self.assertEqual(view.due_defaults(Snapshot(asks=[a]), FIXTURE_NOW + 2 * DEFAULT_AFTER), [], f"stakes {stakes}: the default applied")
        for stakes in ("[]", "null"):
            m = json.loads('{"items": [{"id": "a1", "kind": "decision", "name": "Re-run e2e", "text": "Re-run?", "rec": "yes", "auto": true, '
                           '"stakes": ' + stakes + ', "options": [{"id": "yes", "label": "Yes"}], "created": 1791400000}]}')
            self.assertTrue(state.parse_asks(m)[0].auto, f"stakes {stakes}: no stakes, yet not auto")

    def test_a_handled_auto_answer_leaves_ready_for_you(self):
        """Review round 1: an auto answer athena has done, or the human undid, no longer holds its item in Ready for you: once the
        Undo is handled (applied and auto gone from the item), it is the human's call again, in Needs you."""
        a = decision_with_default()
        a.auto = False  # athena handled the Undo: the default is off and nothing is applied
        for st in ("done", "cancelled"):
            s = Snapshot(asks=[a], actions=[Action(seq=1, ts=FIXTURE_NOW - 600, item="a1", option="re-run", auto=True, status=st)])
            v = build(s, FIXTURE_NOW, "")
            self.assertTrue(ids(v.ready) == "" and ids(v.needs_you) == "a1", f"{st}: ready {ids(v.ready)!r} needs {ids(v.needs_you)!r}")
        s = Snapshot(asks=[a], actions=[Action(seq=1, ts=FIXTURE_NOW - 600, item="a1", option="re-run", auto=True, status="queued")])
        self.assertEqual(ids(build(s, FIXTURE_NOW, "").ready), "a1", "a queued auto answer shows in Ready for you")


class DashAwaiting(DashCase):
    """dash/awaiting_test.go"""

    def test_the_row_shows_only_with_prs_awaiting_review(self):
        v = build(snapshot_of(awaiting_pr(1, "main", "draft", "draft", False)), FIXTURE_NOW, "acme")
        self.assertEqual(v.awaiting, [], "no PR awaits review")
        v = build(snapshot_of(awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False)), FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#1" and len(v.in_progress) == 0, f"awaiting {pill_keys(v.awaiting)}, in progress {v.in_progress}")
        p = v.awaiting[0]
        self.assertTrue(p.id == "aw-acme-web-1" and p.num == "#1" and p.title == "change 1" and p.actions.item == "pr:acme/web#1" and
                        p.actions.recheck and p.actions.dismiss, f"pill {p}")
        self.assertIs(v.actionable.get("pr:acme/web#1"), p.actions, "the pill's actions are not offered")

    def test_every_pr_shows_in_exactly_one_place(self):
        s = state.Store(FIXTURE).load()
        v = build(s, FIXTURE_NOW, "acme")
        seen = {}
        for group in (v.needs_you, v.in_progress, v.done):
            for it in group:
                if it.pr is not None and state.pr_key(it.pr.url):
                    seen[state.pr_key(it.pr.url)] = seen.get(state.pr_key(it.pr.url), 0) + 1

        def walk(pills):
            for p in pills:
                seen[p.key] = seen.get(p.key, 0) + 1
                walk(p.behind)
        walk(v.awaiting)
        for k, n in seen.items():
            self.assertEqual(n, 1, f"{k} shows {n} times")
        for k in ("acme/web#906", "acme/web#907", "acme/web#908", "acme/web#909", "acme/web#6452", "acme/api#71", "acme/docs#212"):
            self.assertEqual(seen.get(k), 1, f"{k} is missing")

    def test_stacks_from_base_branches_and_chains(self):
        # By base branch: 1 <- 2 <- 3, and 4 also on 1. 2 awaits review too; 3's checks run; 4 is a draft.
        s = snapshot_of(
            awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(2, "b1", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(3, "b2", "checks running", "pending", False),
            awaiting_pr(4, "b1", "draft", "draft", False),
            awaiting_pr(9, "main", "draft", "draft", False),  # on nothing awaiting: stays in progress
        )
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#1[2 3 4]", "awaiting")
        self.assertTrue(ids(v.in_progress) == "web" and v.in_progress[0].pr.text == "#9", f"in progress: {ids(v.in_progress)}")
        b = v.awaiting[0].behind
        self.assertTrue(b[0].waits_on == "#1" and b[1].waits_on == "#2" and b[2].waits_on == "#1" and b[1].label == "checks running" and
                        v.awaiting[0].waits_on == "", f"behind {b}")
        a3 = v.actionable.get("pr:acme/web#3")
        self.assertTrue(a3 is not None and a3.recheck, "a pill behind has its own actions")

        # stack.json's order wins over numbers and branches: 30 <- 10 <- 20.
        s = snapshot_of(
            awaiting_pr(30, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(10, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(20, "main", "checks running", "pending", False),
        )
        s.stacks = [Stack(repo="acme/web", prs=[30, 10, 20])]
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#30[10 20]", "chain")

        # A PR that needs the human keeps its row, and the chain behind it is not stacked through it.
        s = snapshot_of(
            awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(2, "b1", "checks failing", "failing", True),
            awaiting_pr(3, "b2", "checks running", "pending", False),
        )
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#1" and len(v.needs_you) == 1 and len(v.in_progress) == 1,
                        f"awaiting {pill_keys(v.awaiting)}, needs {v.needs_you}, progress {v.in_progress}")

        # A live worker still working on a PR stacked on one: its row is the worker's, so it stays.
        s = snapshot_of(awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False))
        s.workers = [Worker(name="abc-5", linear="ABC-5", state="live", branch="you/abc-5")]
        s.hooks["abc-5"] = Hook(state="working")
        s.watch.workers = {"abc-5": WatchWorker(pr=awaiting_pr(5, "b1", "checks running", "pending", False))}
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#1" and find(v.in_progress, "abc-5").label == "working",
                        f"awaiting {pill_keys(v.awaiting)}, in progress {v.in_progress}")

        # The ledger's branch stands in for a PR's own: an idle worker's PR, with a followed PR based on it.
        own = awaiting_pr(6, "main", AWAITING_ALEX, "awaiting", False)
        own.branch = ""
        s = snapshot_of(awaiting_pr(7, "you/abc-6", AWAITING_ALEX, "awaiting", False))
        s.workers = [Worker(name="abc-6", state="live", branch="you/abc-6")]
        s.hooks["abc-6"] = Hook(state="idle")
        s.watch.workers = {"abc-6": WatchWorker(pr=own)}
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#6[7]", "by the ledger's branch")

        # A cycle (bad data) neither loops nor loses a PR, and breaks the same way every time.
        s = snapshot_of(awaiting_pr(1, "b2", AWAITING_ALEX, "awaiting", False), awaiting_pr(2, "b1", AWAITING_ALEX, "awaiting", False))
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#2[1]", "cycle")

    def test_dismiss_holds_until_the_head_or_review_changes(self):
        s = snapshot_of(awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False), awaiting_pr(2, "main", AWAITING_ALEX, "awaiting", False))
        s.dismissed = {"acme/web#1": Dismissal(head="sha1", review=AWAITING_ALEX)}
        self.assertEqual(pill_keys(build(s, FIXTURE_NOW, "acme").awaiting), "acme/web#2", "dismissed")
        s.watch.prs["acme/web#1"].head = "sha1-new"  # a new commit
        self.assertEqual(pill_keys(build(s, FIXTURE_NOW, "acme").awaiting), "acme/web#2 acme/web#1", "after a push")
        s.watch.prs["acme/web#1"].head = "sha1"
        s.watch.prs["acme/web#1"].move.label = "awaiting review · alex, kim"  # another reviewer asked
        self.assertEqual(pill_keys(build(s, FIXTURE_NOW, "acme").awaiting), "acme/web#2 acme/web#1", "after a review change")

        # Dismissing a stack's front: an awaiting PR behind it is a pill of its own, the rest go back to In progress.
        # A dismissed draft behind a pill is hidden with it.
        s = snapshot_of(
            awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(2, "b1", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(3, "b2", "checks running", "pending", False),
            awaiting_pr(4, "b1", "draft", "draft", False),
        )
        s.dismissed = {"acme/web#1": Dismissal(head="sha1", review=AWAITING_ALEX)}
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#2[3]" and len(v.in_progress) == 1 and v.in_progress[0].pr.text == "#4",
                        f"front dismissed: {pill_keys(v.awaiting)}, in progress {v.in_progress}")
        # A dismissed PR is as if off the row: one behind is hidden, and a PR stacked only through it has its own row again.
        s.dismissed = {"acme/web#2": Dismissal(head="sha2", review=AWAITING_ALEX)}
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#1[4]" and len(v.in_progress) == 1 and v.in_progress[0].pr.text == "#3" and
                        v.pills.get("pr:acme/web#2") is None, f"behind dismissed: {pill_keys(v.awaiting)}, in progress {v.in_progress}")

    def test_rechecking_while_queued(self):
        s = snapshot_of(awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False))
        s.actions = [Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#1", option="recheck", status="queued")]
        p = build(s, FIXTURE_NOW, "acme").awaiting[0]
        self.assertTrue(p.rechecking and not p.actions.recheck and p.actions.dismiss, f"queued: {p}")
        for a in (
            Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#1", option="recheck", status="done"),
            Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#1", option="recheck", status="cancelled"),
            Action(seq=1, ts=FIXTURE_NOW - STUCK_AFTER, item="pr:acme/web#1", option="recheck", status="queued"),  # athena never took it up
        ):
            s.actions = [a]
            p = build(s, FIXTURE_NOW, "acme").awaiting[0]
            self.assertTrue(not p.rechecking and p.actions.recheck, f"{a}: {p}")

    def test_short_title(self):
        cases = {
            "Pin the Go toolchain": "Pin the Go toolchain",
            "x" * SHORT_MAX: "x" * SHORT_MAX,
            "Checkout keeps the promo code when a cart is restored on another device after sign-in": "Checkout keeps the promo code when a cart is…",
            "y" * 60: "y" * (SHORT_MAX - 1) + "…",
            "  Spaces   fold\tto one  ": "Spaces fold to one",
            "Billing export, step 3: upload, to object storage": "Billing export, step 3: upload, to object…",
        }
        for given, want in cases.items():
            got = view.short_title(given)
            self.assertEqual(got, want, given)
            self.assertLessEqual(len(got), SHORT_MAX, got)
