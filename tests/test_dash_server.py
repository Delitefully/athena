"""The old athena dash's own tests (its dash/actions_test.go, awaiting_actions_test.go, server_test.go and fullview_test.go), one Go test
to one method of the same name, every assertion kept, against the Python port in athena/caps/dash. Then the old Python tests of the
dash's lifecycle and state files (tests/test_dash.py, tests/test_dashstate.py), as far as they apply to athena.

athena's deliberate differences, which these tests hold: a write that fails answers 503 with the reason (the Go's bare 500 "could
not record it"); /healthz also carries state_dir and start; a health.json dated in the future shows unknown; the item edit writes
needs-you.json with indent=1 and sort_keys and keeps needs-you.json.prev; app.js has a Show handler for athena's Rex sessions."""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from harness import BIN, ROOT, Case, dead_pid, spawn_record, store

from athena.caps.dash import markdown, page, server, state, view

FIXTURE = ROOT / "tests" / "fixtures" / "dash" / "state"
FIXTURE_NOW = 1791500000
TEXT_MAX = server.TEXT_MAX
BODY_MAX = server.BODY_MAX
SETTLE_AFTER = server.SETTLE_AFTER
STUCK_AFTER = view.STUCK_AFTER
ACTIONS_FILE = state.ACTIONS
ASKS_FILE = state.ASKS
DISMISS_FILE = state.DISMISSED
WATCH_FILE = state.WATCH
ARCHIVE_FILE = state.ARCHIVE
PREVIEWS_DIR = server.PREVIEWS
PREVIEW_CSP = server.PREVIEW_CSP

# Every icon a row can show, as the view sets Item.icon.
ROW_GLYPHS = ["blocked", "fail", "done", "review", "ready", "working", "idle", "exited", "pr", "merged", "retired", "ok", "note", "ask"]


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        return self.headers.get(name) or ""


def request(base, method, path, body=None, headers=None) -> Response:
    """One request with http.client, so a Host, a method or a missing header is sent as given, and no redirect is followed."""
    u = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    try:
        conn.request(method, path, body=body.encode() if isinstance(body, str) else body, headers=headers or {})
        r = conn.getresponse()
        return Response(r.status, r.headers, r.read().decode("utf-8", "replace"))
    finally:
        conn.close()


def get(base, path, host="") -> tuple:
    r = request(base, "GET", path, headers={"Host": host} if host else None)
    return r, r.body


def between(s, start, end) -> str:
    i = s.find(start)
    if i < 0:
        return ""
    j = s[i:].find(end)
    if j < 0:
        return ""
    return s[i:i + j]


def find(items, name) -> view.Item:
    for it in items:
        if it.name == name or it.id == "w-" + name or it.id == "ask-" + name:
            return it
    return view.Item()


def pill_keys(pills) -> str:
    out = []
    for p in pills:
        k = p.key
        if p.behind:
            k += "[" + " ".join(b.num.lstrip("#") for b in p.behind) + "]"
        out.append(k)
    return " ".join(out)


def write(path, text):
    Path(path).write_text(text)


def read_event(r) -> str:
    """One SSE event's joined data lines."""
    data, event = [], ""
    while True:
        line = r.readline()
        if not line:
            raise AssertionError("stream ended")
        line = line.decode().rstrip("\n")
        if line == "" and event:
            return "\n".join(data)
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])


class DashCase(Case):
    """start(): a copy of the fixture served on a loopback port, its clock at FIXTURE_NOW, as the Go tests' start did."""

    def copy_fixture(self) -> Path:
        self._copies = getattr(self, "_copies", 0) + 1
        dst = self.tmp / f"fixture-{self._copies}"
        shutil.copytree(FIXTURE, dst)
        return dst

    def start(self, d, linear="acme", now=FIXTURE_NOW, poll=3600.0):
        """server.start's steps, with the clock set before the first refresh as the Go start set it: start() itself refreshes with the
        real clock, and that refresh would apply the fixture's long-due default (a1) as of today. The Go start ran no poll: a long one
        here, so nothing refreshes behind a test's back unless a test asks. athena is off: the fixture alone."""
        dash = server.Server(d, 0, linear, athena=False)
        if now is not None:
            dash.now = lambda: now
        httpd = server.DashHTTPServer(("127.0.0.1", 0), server.make_handler(dash))
        httpd.daemon_threads = True
        dash.port = httpd.server_address[1]
        dash.refresh(True)
        threading.Thread(target=dash.poll, args=(poll,), daemon=True).start()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(dash.stop)
        return dash, f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(self, dash, base, body, change=None, path="/api/action"):
        """An answer as the page sends it; change edits the request first, to break one of its checks."""
        req = {"method": "POST", "headers": {"Origin": base, "Content-Type": "application/json", "X-Athena-Token": dash.token}}
        if change is not None:
            change(req)
        r = request(base, req["method"], path, body=body, headers=req["headers"])
        try:
            out = json.loads(r.body)
        except ValueError:
            out = None
        return r.status, out

    def queued(self, d) -> list:
        return state.Store(d).load().actions


# dash/actions_test.go

class Actions(DashCase):
    def test_an_answer_is_queued_and_hides_its_ask_at_once(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        _, body = get(base, "/")
        self.assertIn('id="ask-a5"', body, "the page carries the ask and its token")
        self.assertIn(f'<meta name="athena-token" content="{srv.token}">', body, "the page carries the ask and its token")
        code, out = self.post(srv, base, '{"item": "a5", "option": "yes"}')
        self.assertEqual((code, out), (202, {"seq": 1}))
        # refresh(True) ran before the reply: the page every tab gets next no longer has the ask.
        main, _, _ = srv.snapshot()
        self.assertNotIn('id="ask-a5"', main, "the answered ask is still on the page")
        got = self.queued(d)
        self.assertEqual(got, [state.Action(seq=1, ts=FIXTURE_NOW, item="a5", option="yes", status="queued")])
        data = (d / ACTIONS_FILE).read_text()
        self.assertRegex(data, r'^\{"item":"a5","option":"yes","seq":1,"status":"queued","ts":1791500000\}\n$', "one JSON line per answer")

    def test_every_kind_of_answer(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        for body in ['{"item": "a1", "discuss": true}',
                     '{"item": "a2", "text": "  Thursday, and tell #design  "}',
                     '{"item": "a5", "option": "no"}',  # an option of the item's, from its full view
                     '{"item": "w-abc-101", "discuss": true}',
                     '{"item": "w-abc-101", "text": "rebase first"}']:
            code, out = self.post(srv, base, body)
            self.assertEqual(code, 202, f"{body}: {code} {out}")
        got = self.queued(d)
        self.assertEqual(len(got), 5, got)
        self.assertTrue(got[0].discuss)
        self.assertEqual(got[1].text, "Thursday, and tell #design")
        self.assertEqual(got[2].option, "no")
        self.assertEqual(got[4].item, "w-abc-101")
        # A worker's row stays: it is the worker's state, not an ask.
        main, _, _ = srv.snapshot()
        self.assertIn('id="w-abc-101"', main, "asks hide, worker rows stay")
        self.assertNotIn('id="ask-a1"', main, "asks hide, worker rows stay")

    def test_posts_the_server_refuses(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        long = '{"item": "a2", "text": "' + "x" * (TEXT_MAX + 1) + '"}'

        def drop(name):
            return lambda r: r["headers"].pop(name, None)

        def put(name, value):
            return lambda r: r["headers"].__setitem__(name, value)

        def method(m):
            return lambda r: r.__setitem__("method", m)
        cases = [
            ("no Origin", '{"item": "a5", "option": "yes"}', drop("Origin"), 403),
            ("another Origin", '{"item": "a5", "option": "yes"}', put("Origin", "http://evil.test"), 403),
            ("another port", '{"item": "a5", "option": "yes"}', put("Origin", "http://127.0.0.1:1"), 403),
            ("a form post", '{"item": "a5", "option": "yes"}', put("Content-Type", "text/plain"), 415),
            ("no content type", '{"item": "a5", "option": "yes"}', drop("Content-Type"), 415),
            ("no token", '{"item": "a5", "option": "yes"}', drop("X-Athena-Token"), 403),
            ("a wrong token", '{"item": "a5", "option": "yes"}', put("X-Athena-Token", "0123"), 403),
            ("a GET", "", method("GET"), 405),
            ("a preflight", "", method("OPTIONS"), 405),
            ("a reply past 2000 characters", long, None, 400),
            ("a body past the cap", '{"item": "a2", "text": "' + "x" * BODY_MAX + '"}', None, 400),
            ("an empty reply", '{"item": "a2", "text": "   "}', None, 400),
            ("two answers", '{"item": "a5", "option": "yes", "discuss": true}', None, 400),
            ("no answer", '{"item": "a5"}', None, 400),
            ("an unknown field", '{"item": "a5", "option": "yes", "url": "https://x.test"}', None, 400),
            ("an unknown item", '{"item": "a99", "option": "yes"}', None, 422),
            ("an option it lacks", '{"item": "a5", "option": "maybe"}', None, 422),
            ("× on a question", '{"item": "a5", "option": "dismiss"}', None, 422),
            ("Reply on a note", '{"item": "a4", "text": "hi"}', None, 422),
            ("a retired worker", '{"item": "w-abc-098", "discuss": true}', None, 422),
            ("an option on a worker", '{"item": "w-abc-101", "option": "yes"}', None, 422),
            ("undo with an item", '{"seq": 1, "undo": true, "item": "a5"}', None, 400),
        ]
        for name, body, change, want in cases:
            code, out = self.post(srv, base, body, change)
            with self.subTest(name):
                self.assertEqual(code, want, f"{name}: {code} {out}")
        self.assertFalse((d / ACTIONS_FILE).exists(), "nothing refused is written")
        # The page is served with no CORS header, so no other origin may read a reply either.
        resp, _ = get(base, "/")
        self.assertEqual(resp.header("Access-Control-Allow-Origin"), "", "no CORS")

    def test_undo_within_the_settle_window(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        self.post(srv, base, '{"item": "a5", "option": "yes"}')
        srv.now = lambda: FIXTURE_NOW + SETTLE_AFTER - 1
        code, out = self.post(srv, base, '{"seq": 1, "undo": true}')
        self.assertEqual(code, 202, out)
        got = self.queued(d)
        self.assertEqual(got[0].status, "cancelled", got)
        main, _, _ = srv.snapshot()
        self.assertIn('id="ask-a5"', main, "an undone answer brings its ask back at once")
        self.assertEqual(self.post(srv, base, '{"seq": 1, "undo": true}')[0], 409, "an answer is undone once")
        # Past the window, athena may already be acting on it: undo is refused.
        self.post(srv, base, '{"item": "a5", "option": "no"}')
        srv.now = lambda: FIXTURE_NOW + 2 * SETTLE_AFTER
        self.assertEqual(self.post(srv, base, '{"seq": 2, "undo": true}')[0], 409, "too late to undo")
        self.assertEqual(self.post(srv, base, '{"seq": 7, "undo": true}')[0], 409, "an unknown seq")

    def test_rapid_answers_are_all_queued_in_order(self):
        d = self.copy_fixture()
        ids = ["q" + chr(ord("a") + i) for i in range(1, 13)]
        write(d / ASKS_FILE, '{"items": [' + ",".join(f'{{"id": "{i}", "text": "Question {i[1]}?"}}' for i in ids) + "]}")
        srv, base = self.start(d)
        codes = []
        lock = threading.Lock()

        def answer(item):
            code, _ = self.post(srv, base, f'{{"item": "{item}", "discuss": true}}')
            with lock:
                codes.append(code)
        threads = [threading.Thread(target=answer, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for c in codes:
            self.assertEqual(c, 202, f"a rapid answer was refused: {c}")
        got = self.queued(d)
        seen = set()
        for i, a in enumerate(got):
            self.assertFalse(a.seq != i + 1 or a.status != "queued" or a.item in seen, f"seq {i + 1}: {got}")
            seen.add(a.item)
        self.assertEqual(len(got), 12, f"{len(got)} of 12 queued")
        main, _, _ = srv.snapshot()
        self.assertNotIn('id="ask-q', main, "every answered ask is hidden")

    def test_a_compacted_queue_keeps_counting_from_its_last_seq(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        # `athena ask done` compacted everything away but the last line.
        write(d / ACTIONS_FILE, '{"seq": 41, "ts": 1, "item": "a9", "option": "yes", "status": "queued"}\n{"seq": 41, "ts": 2, "status": "done"}\n')
        _, out = self.post(srv, base, '{"item": "a5", "option": "yes"}')
        self.assertEqual(out, {"seq": 42})

    def test_a_stuck_answer_brings_its_ask_back(self):
        s = state.Snapshot(asks=[state.Ask(id="a1", kind="decision", next="you", name="Ship it", text="Ship?", why="the window closes at 5")],
                           actions=[state.Action(seq=1, ts=FIXTURE_NOW - STUCK_AFTER + 1, item="a1", discuss=True, status="queued")])
        v = view.build(s, FIXTURE_NOW, "")
        self.assertFalse(v.needs_you or v.actionable.get("a1") is not None, f"hidden while athena has it queued: {v.needs_you}")
        s.actions[0].ts = FIXTURE_NOW - STUCK_AFTER
        v = view.build(s, FIXTURE_NOW, "")
        self.assertEqual(len(v.needs_you), 1, f"back after {STUCK_AFTER} s, saying why: {v.needs_you}")
        self.assertEqual(v.needs_you[0].sent, view.STUCK_WHY)
        self.assertIsNotNone(v.actionable.get("a1"))
        for st in ("done", "cancelled"):
            s.actions[0].status = st
            v = view.build(s, FIXTURE_NOW, "")
            self.assertEqual(len(v.needs_you), 1, f"{st}: {v.needs_you}")
            self.assertEqual((v.needs_you[0].sent, v.needs_you[0].detail), ("", ""), st)

    def test_what_each_row_offers(self):
        v = view.build(state.Store(FIXTURE).load(), FIXTURE_NOW, "acme")
        # A decision: Take rec, Snooze and Open on its row; every option, Discuss, Reply and "Don't ask me this again" in its full view,
        # which answers for the same item.
        a1 = v.actionable.get("a1")
        self.assertIsNotNone(a1)
        self.assertEqual(len(a1.options), 4, a1)
        self.assertTrue(a1.discuss and a1.reply and a1.rule and not a1.dismiss and a1.hides and not a1.row_options(), a1)
        self.assertTrue(a1.take_rec is not None and a1.take_rec.id == "later" and a1.snooze and not a1.done and a1.open == "/item/a1", a1)
        a5 = v.actionable.get("a5")
        self.assertTrue(a5 is not None and not a5.row_options() and a5.take_rec.label == "Close it", f"its options are its full view's: {a5}")
        # A result: Done, Snooze and Open; no answer for athena.
        n = v.actionable.get("a3")
        self.assertTrue(n is not None and n.done and n.snooze and not n.discuss and not n.reply and not n.rule and n.take_rec is None,
                        f"a result: {n}")
        # A default athena applied on its own: Done or Undo.
        n = v.actionable.get("a8")
        self.assertTrue(n is not None and n.done and n.undo == "Re-run" and n.allows(state.UNDO_DEFAULT, "", False), f"an applied default: {n}")
        self.assertFalse(v.actionable["a3"].allows(state.UNDO_DEFAULT, "", False) or v.actionable["a1"].allows(state.UNDO_DEFAULT, "", False),
                         "undo-default only where a default applied")
        w = v.actionable.get("w-abc-102")
        self.assertTrue(w is not None and w.discuss and w.reply and not w.dismiss and not w.hides and not w.options and w.open == "",
                        f"a live worker: Discuss and Reply, no dismiss: {w}")
        for key in ("w-abc-100", "pr-acme-web-906", "rr-acme-web-6170", "a10"):
            self.assertIsNone(v.actionable.get(key),
                              "no actions on a retired worker's Done row, an In flight PR row, a review request or a resolved item")
        it = find(v.needs_you, "Export queue start")
        self.assertEqual(it.detail, "", f"an item's why is its full view's, not its row's: {it}")

    def test_pills_take_the_place_of_the_label_line(self):
        _, base = self.start(self.copy_fixture())
        _, body = get(base, "/")
        # An item: the one-click calls share a cell with its label line, so no space is reserved for them.
        self.assertIn('<div class="swap has-acts"><p class="meta"><span class="label">your call</span></p><div class="acts options" data-item="a5" data-hides>'
                      '<button type="button" class="pill rec" data-option="yes" data-tip="Recommended: Close it">', body,
                      "an item's pills are not in its label line's cell")
        # The whole row opens its full view (app.js), its name the link: no Open pill.
        row = between(body, '<li id="ask-a5"', "</li>")
        self.assertIn(' class="item has-view" ', row, f"an item's row: {row}")
        self.assertIn('<span class="name"><a href="/item/a5">', row, f"an item's row: {row}")
        self.assertNotIn(">Open</a>", row, f"an item's row: {row}")
        # A worker's row: they take the place of its whole state line.
        self.assertRegex(body, r'(?s)<li id="w-abc-101" .*?<div class="swap has-acts"><p class="meta">.*?</p><div class="acts" data-item="w-abc-101">',
                         "a worker's pills are not in its state line's cell")
        # A row with no pills has no pill-height line.
        self.assertRegex(body, r'(?s)<li id="w-abc-100" .*?<div class="swap"><p class="meta">', "a retired row's line is not left as it is")
        # Icon-only pills are named by aria-label (the tooltip's text), never a native title.
        for want in ('data-discuss aria-label="Discuss in Athena"', 'data-reply aria-label="Reply…"'):
            self.assertIn(want, body)
        self.assertNotRegex(body, r'class="pill[^"]*"[^>]* title=', "a pill has a native title as well as its tooltip")

    def test_a_reply_is_counted_in_characters_as_the_reply_box_counts_them(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        # 2000 two-byte characters: what the reply box's maxlength lets through, 4000 bytes.
        code, out = self.post(srv, base, '{"item": "a2", "text": "' + "é" * TEXT_MAX + '"}')
        self.assertEqual(code, 202, out)
        got = self.queued(d)
        self.assertTrue(len(got) == 1 and len(got[0].text) == TEXT_MAX, got)

    def test_two_tabs_answering_one_ask_at_once_queue_one_answer(self):
        for round_ in range(5):
            d = self.copy_fixture()
            srv, base = self.start(d)
            codes = []
            lock = threading.Lock()

            def answer(opt):
                code, _ = self.post(srv, base, f'{{"item": "a5", "option": "{opt}"}}')
                with lock:
                    codes.append(code)
            threads = [threading.Thread(target=answer, args=(o,)) for o in ("yes", "no")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Whichever lost the race got 409, or 422 if the page had already dropped the ask.
            self.assertEqual(codes.count(202), 1, f"round {round_}: {codes}")
            self.assertEqual(codes.count(409) + codes.count(422), 1, f"round {round_}: {codes}")
            self.assertEqual(len(self.queued(d)), 1, f"round {round_}")

    def test_the_lock_refuses_a_second_answer_but_not_a_workers_or_a_stuck_one(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        self.post(srv, base, '{"item": "a5", "option": "yes"}')
        # Another tab still shows the ask (its page check passes): the lock refuses it.
        write(d / ACTIONS_FILE, '{"seq": 1, "ts": 1791500000, "item": "a5", "option": "yes", "status": "queued"}\n')
        srv.view.actionable["a5"] = view.Actions(item="a5", options=[state.Option(id="no", label="No")], hides=True)
        code, out = self.post(srv, base, '{"item": "a5", "option": "no"}')
        self.assertEqual(code, 409, out)
        # Past STUCK_AFTER the ask shows again, and takes a new answer.
        srv.now = lambda: FIXTURE_NOW + STUCK_AFTER
        srv.refresh(True)
        code, out = self.post(srv, base, '{"item": "a5", "option": "no"}')
        self.assertEqual(code, 202, f"stuck: {out}")
        for body in ('{"item": "w-abc-101", "discuss": true}', '{"item": "w-abc-101", "text": "and rebase"}'):
            code, out = self.post(srv, base, body)
            self.assertEqual(code, 202, f"a worker's row takes any number: {out}")

    def test_events_tell_the_page_which_start_serves_it(self):
        srv, base = self.start(self.copy_fixture())
        _, body = get(base, "/")
        self.assertIn(f'<meta name="athena-boot" content="{srv.boot}">', body, "the page lacks its start id")
        u = urllib.parse.urlsplit(base)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/events")
        r = conn.getresponse()
        buf = b""
        while len(buf) < 40 or b"\n\n" not in buf[buf.find(b"event: boot"):]:
            chunk = r.read1(256)
            if not chunk:
                break
            buf += chunk
        self.assertIn(f"event: boot\ndata: {srv.boot}\n\n", buf.decode())


# dash/awaiting_actions_test.go

class AwaitingActions(DashCase):
    def test_recheck_queues_for_athena_naming_the_pr(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        code, out = self.post(srv, base, '{"item": "pr:acme/web#907", "option": "recheck"}')
        self.assertEqual((code, out), (202, {"seq": 1}))
        got = self.queued(d)
        self.assertEqual(got, [state.Action(seq=1, ts=FIXTURE_NOW, item="pr:acme/web#907", option="recheck", status="queued")])
        # The pill stays, re-checking, with no second Re-check until athena takes this one up.
        main, _, _ = srv.snapshot()
        pill = between(main, '<div class="apr rechecking" id="aw-acme-web-907"', "</div>\n</div>")
        self.assertTrue(pill and 'data-option="recheck"' not in pill and 'data-option="dismiss"' in pill and ', re-checking"' in pill,
                        f"pill {pill!r}")
        code, out = self.post(srv, base, '{"item": "pr:acme/web#907", "option": "recheck"}')
        self.assertEqual(code, 422, f"a second re-check: {out}")
        # Undo, as for any answer: Re-check is offered again.
        code, out = self.post(srv, base, '{"seq": 1, "undo": true}')
        self.assertEqual(code, 202, f"undo: {out}")
        main, _, _ = srv.snapshot()
        self.assertIn('<div class="apr" id="aw-acme-web-907" ', main, "still re-checking after the undo")
        # A pill stacked behind another has its own Re-check.
        code, out = self.post(srv, base, '{"item": "pr:acme/web#909", "option": "recheck"}')
        self.assertEqual(code, 202, f"behind: {out}")

    def test_pill_actions_the_server_refuses(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        for body, want in [
            ('{"item": "pr:acme/web#4242", "option": "recheck"}', 422),  # a PR the dash does not show
            ('{"item": "pr:acme/web#48", "option": "recheck"}', 422),  # shown, but as a Needs you row
            ('{"item": "pr:Acme/Web#907", "option": "recheck"}', 400),  # keys are lower case, as pr_key makes them
            ('{"item": "pr:acme/web#907; rm -rf", "option": "recheck"}', 400),
            ('{"item": "pr:https://github.com/acme/web/pull/907", "option": "recheck"}', 400),
            ('{"item": "pr:acme/web#0", "option": "recheck"}', 400),
            ('{"item": "pr:acme/web#907", "discuss": true}', 422),
            ('{"item": "pr:acme/web#907", "text": "fix it"}', 422),
            ('{"item": "pr:acme/web#907", "option": "merge"}', 422),
            ('{"item": "w-abc-101", "option": "recheck"}', 422),
            ('{"item": "a5", "option": "recheck"}', 422),
            ('{"item": "pr:acme/web#907", "undo": true}', 409),  # not dismissed
            ('{"item": "pr:acme/web#907", "option": "dismiss", "undo": true}', 400),
        ]:
            code, out = self.post(srv, base, body)
            with self.subTest(body):
                self.assertEqual(code, want, f"{body}: {code} {out}")
        self.assertEqual(self.queued(d), [])

    def test_dismiss_is_the_dashs_own_and_undoes(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        code, out = self.post(srv, base, '{"item": "pr:acme/web#907", "option": "dismiss"}')
        self.assertEqual((code, out), (202, {"seq": 0}))
        # Never in actions.jsonl, so the doorbell does not ring and athena is not woken.
        self.assertFalse((d / ACTIONS_FILE).exists(), "actions.jsonl written")
        data = (d / DISMISS_FILE).read_text()
        prs = json.loads(data).get("prs") or {}
        dm = prs.get("acme/web#907") or {}
        self.assertEqual((dm.get("head"), dm.get("review"), dm.get("ts")), ("4f2c1ab9d0e3", "awaiting review · alex", float(FIXTURE_NOW)),
                         f"dismissed-prs.json {data}")
        self.assertEqual((d / DISMISS_FILE).stat().st_mode & 0o777, 0o600)
        main, _, _ = srv.snapshot()
        self.assertNotIn('id="aw-acme-web-907"', main, "the dismissed pill is still on the page")
        self.assertIn('Awaiting review <span class="count">6</span>', main, "the dismissed pill is still on the page")
        self.assertEqual(self.post(srv, base, '{"item": "pr:acme/web#907", "option": "dismiss"}')[0], 422, "a dismissed pill offers nothing")
        # Undo names the PR, as a dismiss has no seq.
        code, out = self.post(srv, base, '{"item": "pr:acme/web#907", "undo": true}')
        self.assertEqual(code, 202, f"undo: {out}")
        main, _, _ = srv.snapshot()
        self.assertIn('id="aw-acme-web-907"', main, "undo did not bring the pill back")
        self.assertEqual(self.post(srv, base, '{"item": "pr:acme/web#907", "undo": true}')[0], 409, "a second undo")

        # Dismissed again, it shows once its PR has a new head commit.
        self.post(srv, base, '{"item": "pr:acme/web#907", "option": "dismiss"}')
        watch = (d / WATCH_FILE).read_text()
        tail = ('''"merge_state": "BLOCKED",
        "mergeable": "MERGEABLE",
        "move": {
          "human_merge": true,
          "kind": "awaiting",
          "label": "awaiting review · alex",
          "needs": false
        },
        "number": 907''')
        old = '"head": "4f2c1ab9d0e3",\n        ' + tail
        self.assertIn(old, watch, "the fixture's #907 as the Go test edits it")
        (d / WATCH_FILE).write_text(watch.replace(old, '"head": "0000000aaaaa",\n        ' + tail, 1))
        srv.refresh(True)
        main, _, _ = srv.snapshot()
        self.assertIn('id="aw-acme-web-907"', main, "a new commit did not bring the pill back")

    def test_dismissing_a_stacks_front_promotes_the_next(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        code, out = self.post(srv, base, '{"item": "pr:acme/web#906", "option": "dismiss"}')
        self.assertEqual(code, 202, out)
        _, v, _ = srv.snapshot()
        self.assertEqual(pill_keys(v.awaiting), "acme/api#71 acme/docs#212 acme/web#6452 acme/web#908[909] acme/web#907")

    def test_the_row_is_absent_with_nothing_awaiting(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        for k in ("906", "907", "6452"):
            self.post(srv, base, f'{{"item": "pr:acme/web#{k}", "option": "dismiss"}}')
        self.post(srv, base, '{"item": "pr:acme/web#908", "option": "dismiss"}')  # promoted when 906 went
        self.post(srv, base, '{"item": "pr:acme/api#71", "option": "dismiss"}')
        self.post(srv, base, '{"item": "pr:acme/docs#212", "option": "dismiss"}')
        main, v, _ = srv.snapshot()
        self.assertFalse(v.awaiting or "awaiting" in main or 'class="apr' in main, f"the row is still there: {pill_keys(v.awaiting)}")
        # #909 waited on #908 only: back in In flight, its own row.
        self.assertIn('id="pr-acme-web-909"', main, "#909 has no place")


# dash/server_test.go

class ServerTest(DashCase):
    def test_one_keep_alive_connection_keeps_each_requests_own_headers_and_bytes(self):
        """Python's handler serves a connection's requests with one instance, where Go served each on its own: a preview's missing
        page CSP must not carry over to the next page, and a refused POST's unread body must not become the next request."""
        dash, base = self.start(self.copy_fixture())
        port = int(base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(conn.close)
        host = {"Host": f"127.0.0.1:{port}"}
        conn.request("GET", "/preview/a1/export-screen.html", headers=host)
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.getheader("Content-Security-Policy"), "sandbox allow-scripts")
        conn.request("GET", "/", headers=host)
        page = conn.getresponse()
        page.read()
        self.assertIn("script-src", page.getheader("Content-Security-Policy") or "", "the page's own CSP, after a preview")
        conn.request("POST", "/api/action", body=b'{"item": "a5", "option": "yes"}', headers={
            **host, "Origin": base, "Content-Type": "application/json", "X-Athena-Token": "stale"})
        refused = conn.getresponse()
        refused.read()
        self.assertEqual((refused.status, refused.getheader("Connection")), (403, "close"))
        conn.close()
        conn.request("GET", "/healthz", headers=host)
        after = conn.getresponse()
        self.assertEqual(after.status, 200, "the next request is read as one")
        after.read()

    def test_listen_binds_loopback_only(self):
        _, httpd = server.start(self.copy_fixture(), 0, "", athena=False, poll=3600)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1", f"bound {httpd.server_address}")
        finally:
            httpd.server_close()

    def test_page_renders_the_groups(self):
        _, base = self.start(self.copy_fixture())
        resp, body = get(base, "/")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.header("Content-Type").startswith("text/html"), resp.header("Content-Type"))
        for want in [
            "<title>(13) athena</title>",
            '<p class="health ok" title="athena doctor&#39;s last check: ',
            '<h2 id="h-awaiting">Awaiting review <span class="count">7</span></h2>',
            '<h2 id="h-needs">Needs you <span class="count">13</span></h2>',
            '<details class="more" data-fold="needs"><summary>6 more</summary>',  # newest first: the default (a1) shows above the fold
            '<span class="auto">athena takes the rec at ',
            '<h2 id="h-ready">Ready for you <span class="count">2</span></h2>',
            '<h2 id="h-progress">In flight <span class="count">6</span></h2>',
            '<details class="group done" data-fold="done" aria-labelledby="h-done"><summary><h2 id="h-done">Done today <span class="count">2</span></h2></summary>',
            '<p class="snoozed">1 snoozed',
            '<a class="ticket" href="https://linear.app/acme/issue/ABC-108">ABC-108</a>',
            '<a href="https://github.com/acme/web/pull/48">#48</a>',
            '<span class="checks failure">checks failing</span>',
            '<span class="name"><a href="/item/a1">Export queue start</a></span> <span class="call">Start <strong>ABC-111</strong> now',
            '<span class="name"><a href="/item/a7">Customer meeting</a></span>',
            '<time class="when due" data-due="',
            ">due tmrw</time>",
            '<span class="name">Rate limits for the export API</span>',  # a worker's row leads with its title, the ticket in its meta
            '<li id="w-abc-102" class="item act" ',
        ]:
            self.assertIn(want, body, f"page lacks {want}")
        # Nothing loads from another host: every src and stylesheet is local.
        for m in re.finditer(r'(?:src|href)="([^"]+)"', body):
            if m.group(1).startswith("http"):
                self.assertIn('href="https://', m.group(0), f"remote resource {m.group(0)}")
        self.assertNotIn('<link rel="stylesheet" href="http', body, "remote resource")
        self.assertNotIn('src="http', body, "remote resource")

    def test_scheme_parameter_pins_the_scheme(self):
        _, base = self.start(self.copy_fixture())
        self.assertIn('<html lang="en" data-scheme="dark">', get(base, "/?scheme=dark")[1], "dark not pinned")
        self.assertIn('<html lang="en">', get(base, "/?scheme=%22%3E")[1], "a bad scheme must be ignored")

    def test_only_loopback_hosts_are_answered(self):
        _, base = self.start(self.copy_fixture())
        port = base.rsplit(":", 1)[1]
        for host, want in {f"127.0.0.1:{port}": 200, f"localhost:{port}": 200, f"attacker.test:{port}": 421, "127.0.0.1:1": 421}.items():
            resp, _ = get(base, "/healthz", host)
            self.assertEqual(resp.status, want, f"host {host}")

    def test_fonts_and_static_files_are_embedded(self):
        """The Go embedded them in its binary; here they are files beside the server, served the same."""
        _, base = self.start(self.copy_fixture())
        for p in ("InstrumentSans.woff2", "InstrumentSerif-Regular.woff2", "style.css", "app.js", "mark.svg"):
            resp, body = get(base, "/static/" + p)
            self.assertTrue(resp.status == 200 and len(body) >= 100, f"{p}: {resp.status}, {len(body)} bytes")
        _, css = get(base, "/static/style.css")
        self.assertNotIn("http", css, "style.css must not load from another host")
        self.assertNotIn("@import", css, "style.css must not load from another host")

    def test_events_push_a_changed_file_to_an_open_page(self):
        d = self.copy_fixture()
        srv, base = self.start(d, poll=0.02)
        u = urllib.parse.urlsplit(base)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", "/events")
        r = conn.getresponse()
        self.assertEqual(r.headers.get("Content-Type"), "text/event-stream")
        self.assertEqual(read_event(r), srv.boot, "the stream opens with its start id")
        first = read_event(r)
        self.assertTrue('id="w-abc-101"' in first and "In flight" in first, f"first event is not the page: {first[:200]}")
        write(d / "workers" / "abc-101.json",
              '{"name": "abc-101", "state": "blocked", "summary": "Claude needs your permission to use Write", "ts": 1791500000}')
        got = []
        reader = threading.Thread(target=lambda: got.append(read_event(r)), daemon=True)
        reader.start()
        reader.join(2)
        self.assertTrue(got, "no update within 2 s")
        body = got[0]
        self.assertIn("Claude needs your permission to use Write", body, f"update lacks the change: {body[:300]}")
        needs = body[:body.index('id="h-progress"')]
        self.assertIn('id="w-abc-101"', needs, "abc-101 did not move to Needs you")

    def test_unchanged_state_is_not_pushed_again(self):
        d = self.copy_fixture()
        srv, _ = self.start(d)
        self.assertFalse(srv.refresh(True), "the same page was broadcast twice")
        os.utime(d / "ledger.jsonl", (time.time() + 5, time.time() + 5))
        self.assertFalse(srv.refresh(False), "a touched file with the same content changed the page")

    def test_the_minute_tick_does_not_repush_an_unchanged_page(self):
        srv, _ = self.start(self.copy_fixture())
        srv.now = lambda: FIXTURE_NOW + 80  # the minute texts change; watch.json is not stale yet
        self.assertFalse(srv.refresh(True), "only the relative times changed, and the page's own timer updates those")
        srv.now = lambda: FIXTURE_NOW + 15 * 24 * 3600  # the Done window moved on
        self.assertTrue(srv.refresh(True), "a real change in the page was not pushed")

    def test_offer_keeps_only_the_newest_page(self):
        import queue
        c = queue.Queue(maxsize=1)
        server._offer(c, "old")
        server._offer(c, "new")
        self.assertEqual(c.get_nowait(), "new")

    def test_watch_pid_stops_when_the_process_is_gone(self):
        self.skipTest("athena's dash is bound to no HQ process: a service runs it, and /healthz says hq_pid 0; there is no watchPid")

    def test_unlinked_ticket_is_still_shown(self):
        _, base = self.start(self.copy_fixture(), linear="", now=None)
        _, body = get(base, "/")
        self.assertIn('<span class="ticket">ABC-108</span>', body, "without a workspace the ticket id shows, unlinked")
        self.assertNotIn("linear.app", body, "without a workspace the ticket id shows, unlinked")

    def test_every_row_glyph_is_in_the_sprite_at_one_weight(self):
        sprite = page.SPRITE[page.SPRITE.index("<defs>"):page.SPRITE.index("</defs>")]
        for gid in ROW_GLYPHS:
            self.assertIn(f'<g id="i-{gid}">', sprite, f"the sprite lacks i-{gid}")
        # The stroke comes from .icon alone, so every glyph has the same weight.
        self.assertNotIn("stroke-width", sprite, "a glyph sets its own stroke-width")

    def test_items_show_their_kinds_glyph(self):
        d = self.copy_fixture()
        write(d / ASKS_FILE, '''{"items": [{"id": "a1", "kind": "question", "name": "Duplicate ticket", "text": "Close ABC-4550?"},
		{"id": "a2", "kind": "note", "name": "Quota reset", "text": "The quota resets at 14:00."}]}''')
        _, base = self.start(d)
        _, body = get(base, "/")
        ask = r'<svg class="icon" [^>]*><use href="#i-ask"/></svg>\s*<div class="body">\s*<p class="line"><span class="name"><a href="/item/a1">Duplicate ticket</a></span> <span class="call">Close ABC-4550\?</span>'
        result = r'<svg class="icon badge" [^>]*><use href="#i-done"/></svg>\s*<div class="body">\s*<p class="line"><span class="name"><a href="/item/a2">Quota reset</a></span> <span class="call">The quota resets'
        self.assertRegex(body, ask, "a decision lacks the question mark")
        self.assertRegex(body, result, "a result lacks its badge")

    def test_glyph_sheet_shows_every_row_glyph_beside_a_line(self):
        _, base = self.start(self.copy_fixture())
        resp, body = get(base, "/glyphs?scheme=dark")
        self.assertEqual(resp.status, 200)
        self.assertIn('<html lang="en" data-scheme="dark">', body, "scheme not pinned")
        for gid in ROW_GLYPHS:
            self.assertIn(f'<use href="#i-{gid}"/></svg>', body, f"the sheet lacks {gid}")

    def test_benchmark_render(self):
        self.skipTest("BenchmarkRender is a Go benchmark, not a test: it asserts nothing")


# dash/fullview_test.go

class FullView(DashCase):
    def test_the_full_view_shows_the_whole_item(self):
        """An item's full view (plan §3): its own link, your words, where it stands, the decision brief with every option and the rec
        marked, the previews embedded, its workers and links, the timeline, and the real actions."""
        _, base = self.start(self.copy_fixture())
        resp, body = get(base, "/item/a1")
        self.assertEqual(resp.status, 200)
        for want in [
            "<title>Export queue start · athena</title>",
            '<main class="full" data-item="a1">',
            "<h1>Export queue start</h1>",
            '<p class="facts"><span>your call</span><span class="state">open</span>',
            '<h2>You asked</h2><blockquote class="words">Can we start the export rework now, or wait until the sign-in fix lands?</blockquote>',
            '<h2>Where it stands</h2><p class="call">Start <strong>ABC-111</strong> now, or after ABC-101 merges? (rec: after)</p><p>ABC-111 touches the same export queue as ABC-101.</p>',
            "<h2>What's needed from you</h2>", "<th>Option</th>", "<td>Split it</td>",
            '<p class="auto">athena takes the rec at ',
            '<div class="acts bar" data-item="a1" data-hides><button type="button" class="pill" data-option="now">',
            'data-option="later">', 'After ABC-101 <span class="tag">rec</span></button>', 'data-option="split"', 'data-option="hold"',
            "Discuss in Athena</button>", "Reply…</button>", '<label class="rule"><input type="checkbox" name="rule"> Don\'t ask me this again</label>',
            '<iframe src="/preview/a1/export-screen.html" sandbox="allow-scripts" loading="lazy" title="export-screen.html"></iframe>',
            '<ol class="timeline"><li><time data-ts=', "filed</li>",
            '<div class="item-acts" data-item="a1"><button type="button" class="pill" data-do="done">', 'data-do="snooze" data-for="7200"',
        ]:
            self.assertIn(want, body, f"a1's full view lacks {want}")
        self.assertNotIn('data-do="reopen"', body, "an open item does not reopen")
        # A refresh of its own, for each push: the <main> alone.
        _, frag = get(base, "/item/a1?fragment=1")
        self.assertTrue("<html" not in frag and "<h1>Export queue start</h1>" in frag, f"fragment {frag[:200]}")
        # A result's report; an item on a worker shows the worker's row; one resolved here can be reopened, and offers no answers any more.
        _, a3 = get(base, "/item/a3")
        self.assertIn("<h2>Report</h2>", a3, a3)
        self.assertNotIn('class="acts bar"', a3, a3)
        _, a4 = get(base, "/item/a4")
        self.assertIn('<span class="name">Billing page empty state</span> <span class="label">done, athena reviewing</span>', a4,
                      f"a4's workers and PR {between(a4, '<h2>Workers', '</section>')}")
        self.assertIn('<a href="https://github.com/acme/web/pull/906">acme/web#906</a>', a4,
                      f"a4's workers and PR {between(a4, '<h2>Workers', '</section>')}")
        _, a10 = get(base, "/item/a10")
        self.assertIn('<span class="state">resolved 1 h ago: you marked it done</span>', a10, between(a10, "<article", "</article>"))
        self.assertIn('data-do="reopen"', a10)
        self.assertNotIn('data-do="done"', a10)
        self.assertNotIn('class="acts bar"', a10)
        _, a6 = get(base, "/item/a6")
        self.assertIn('<span class="stakes">security: always your call</span>', a6, between(a6, "<article", "</article>"))
        self.assertNotIn('class="auto"', a6)
        for path in ("/item/a99", "/item/A1", "/item/../needs-you.json", "/item/"):
            self.assertEqual(get(base, path)[0].status, 404, path)

    def test_an_archived_item_is_found_and_shown(self):
        """An item archived after a week still has its full view, and a search finds it."""
        d = self.copy_fixture()
        write(d / ARCHIVE_FILE, '{"id": "a0", "kind": "result", "name": "Old disk cleanup", "text": "Freed 210 GB", "resolved": 1791000000, '
                                '"resolved_note": "you marked it done", "created": 1790990000, "updated": 1791000000}\nnot json\n')
        _, base = self.start(d)
        _, body = get(base, "/item/a0")
        self.assertTrue("<h1>Old disk cleanup</h1>" in body and 'data-do="reopen"' not in body, between(body, "<article", "</article>"))
        _, found = get(base, "/items?q=disk+cleanup")
        self.assertTrue('<a href="/item/a0">Old disk cleanup</a>' in found and "<span>resolved</span>" in found,
                        f"search {between(found, '<main', '</main>')}")
        _, found = get(base, "/items?q=export")
        self.assertTrue('href="/item/a1">Export queue start</a>' in found and 'href="/item/a4">Billing export</a>' in found and
                        "Old disk" not in found, f"search {between(found, '<main', '</main>')}")
        _, none = get(base, "/items?q=%3Cscript%3E")
        self.assertTrue("No item matches." in none and "<script>" not in none, "a query is escaped")

    def test_an_answer_from_the_full_view_is_checked_as_the_rows_is(self):
        """The full view's buttons answer as the row's do: the same item, checked against the same actions."""
        for body, want in [
            ('{"item": "a1", "option": "split"}', 202),  # the third option, in the full view only
            ('{"item": "a1", "option": "hold", "rule": true}', 202),
            ('{"item": "a1", "text": "after, and always", "rule": true}', 202),
            ('{"item": "a1", "discuss": true, "rule": true}', 422),  # a rule goes with an answer
            ('{"item": "a1", "option": "fifth"}', 422),
            ('{"item": "a3", "text": "merge it"}', 422),  # a result: nothing to answer
            ('{"item": "w-abc-101", "text": "rebase", "rule": true}', 422),
            ('{"item": "a8", "option": "undo-default"}', 202),
            ('{"item": "a5", "option": "undo-default"}', 422),  # no default applied there
            ('{"item": "a6", "option": "on-it"}', 202),
        ]:
            d = self.copy_fixture()
            srv, base = self.start(d)
            code, out = self.post(srv, base, body)
            with self.subTest(body):
                self.assertEqual(code, want, f"{body}: {code} {out}")
                if code == 202 and '"rule"' in body:
                    got = self.queued(d)
                    self.assertTrue(len(got) == 1 and got[0].rule, f"{body}: the rule did not ride on the answer: {got}")
        # A fifth option in the file is dropped, so it is refused like any the page does not offer.
        d = self.copy_fixture()
        write(d / ASKS_FILE, '''{"items": [{"id": "a1", "name": "Pick one", "text": "Which?", "body": "Rec A.", "options": [
		{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}, {"id": "e", "label": "E"}]}]}''')
        srv, base = self.start(d)
        self.assertEqual(self.post(srv, base, '{"item": "a1", "option": "e"}')[0], 422, "a fifth option")
        self.assertEqual(self.post(srv, base, '{"item": "a1", "option": "d"}')[0], 202, "a fourth option")

    def test_a_preview_is_served_sandboxed(self):
        srv, base = self.start(self.copy_fixture())
        resp, body = get(base, "/preview/a1/export-screen.html")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.header("Content-Type").startswith("text/html"), resp.header("Content-Type"))
        self.assertIn("Export billing data", body)
        # An opaque origin: its script can read nothing of the dash, so not the token, and its POSTs carry Origin null.
        csp = resp.header("Content-Security-Policy")
        self.assertTrue(csp == "sandbox allow-scripts" and "allow-same-origin" not in csp, f"CSP {csp!r}")
        self.assertEqual((resp.header("Cache-Control"), resp.header("X-Content-Type-Options")), ("no-store", "nosniff"), resp.headers)
        self.assertNotIn(srv.token, body, "a preview carries the token")
        # The dash's own page keeps its CSP: the preview's does not leak onto it.
        resp, _ = get(base, "/")
        self.assertTrue(resp.header("Content-Security-Policy").startswith("default-src 'self'"), resp.header("Content-Security-Policy"))

    def test_a_preview_cannot_answer_an_ask(self):
        """What a sandboxed preview's script could send, even knowing the token: its Origin is "null", and the server refuses it."""
        d = self.copy_fixture()
        srv, base = self.start(d)
        for origin in ("null", ""):
            def change(r, origin=origin):
                if origin:
                    r["headers"]["Origin"] = origin
                else:
                    r["headers"].pop("Origin")
            code, out = self.post(srv, base, '{"item": "a1", "option": "now"}', change)
            self.assertEqual(code, 403, f"Origin {origin!r}: {out}")
        self.assertEqual(len(self.queued(d)), 0, "an answer was queued")

    def test_a_preview_path_cannot_leave_its_folder(self):
        d = self.copy_fixture()
        root = d / PREVIEWS_DIR
        write(d / "secret.txt", "SECRET")
        (root / "a1" / "site" / "css").mkdir(parents=True)
        write(root / "a1" / "site" / "index.html", "<p>site</p>")
        write(root / "a1" / "site" / "css" / "a.css", "p{}")
        write(root / "a1" / ".hidden", "SECRET")
        os.symlink(d / "secret.txt", root / "a1" / "leak.html")
        os.symlink(d, root / "a1" / "state")
        os.symlink(d, root / "a9")
        _, base = self.start(d)
        for path, want in {
            "/preview/a1/site/": 200,
            "/preview/a1/site/css/a.css": 200,
            "/preview/a1/site": 301,  # to site/, so its relative links resolve inside it
            "/preview/a1/leak.html": 404,  # a symlink to a file outside
            "/preview/a1/state/secret.txt": 404,  # through a symlinked folder
            "/preview/a9/secret.txt": 404,  # an ask's folder that is a symlink
            "/preview/a1/.hidden": 404,
            "/preview/a1/%2e%2e/%2e%2e/secret.txt": 404,
            "/preview/a1/..%2f..%2fsecret.txt": 404,
            "/preview/a1/site%5c..%5c..%5csecret.txt": 404,
            "/preview/A1/export-screen.html": 404,  # not an ask id
            "/preview/a1/export-screen.html/": 404,
            "/preview/a1/missing.html": 404,
            "/preview/a2/": 404,  # no previews
        }.items():
            resp = request(base, "GET", path)
            data = resp.body[:4096]
            with self.subTest(path):
                if not (want == 404 and 300 <= resp.status < 500 and resp.status != 200):
                    self.assertEqual(resp.status, want, path)
                self.assertFalse("SECRET" in data or '"next"' in data, f"{path} served what is outside its folder")
                if resp.status == 200:
                    self.assertEqual(resp.header("Content-Security-Policy"), PREVIEW_CSP, path)
        # A POST is refused too: the route only reads.
        resp = request(base, "POST", "/preview/a1/export-screen.html", body="x", headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 405, "POST")

    def test_tooltips_are_the_bodys_own_never_a_pills_pseudo_element(self):
        """Tooltips are one element of the body, placed by app.js, so no clipped .swap, .acts or .panel can cut one off: none is a pill's
        own ::after any more, and the page has the one."""
        css = (page.STATIC / "style.css").read_text()
        self.assertTrue("attr(aria-label)" not in css and ".tip {" in css, "a tooltip is drawn inside its pill")
        js = (page.STATIC / "app.js").read_text()
        self.assertIn("function showTip", js, "app.js does not place the tooltip")
        _, base = self.start(self.copy_fixture())
        for path in ("/", "/item/a1"):
            _, body = get(base, path)
            self.assertIn('<div class="tip" aria-hidden="true" hidden></div>', body, f"{path} has no tooltip outside <main>")
            self.assertGreater(body.index('class="tip"'), body.index("</main>"), f"{path} has no tooltip outside <main>")
        _, body = get(base, "/item/a1")
        self.assertIn('rel="noopener noreferrer" data-tip="Opens in a tab of its own, sandboxed', body, "a preview link says nothing of its tab")

    # Markdown: markdown.render has no indented code block yet (it renders "<p>git rebase main</p>"); the Markdown agent owns it.
    @unittest.skipUnless("<pre><code>" in markdown.render("    x"), "markdown.render does not render an indented code block yet")
    def test_a_body_keeps_its_first_indent(self):
        """A body keeps its first line's indent, as `athena ask add` does: an indented code block stays one."""
        a = state.parse_asks(json.loads('{"items": [{"id": "a1", "text": "Rebase?", "body": "\\n\\n    git rebase main\\n\\nThen push.\\n\\n"}]}'))[0]
        self.assertEqual(a.body, "    git rebase main\n\nThen push.")
        self.assertIn("<pre><code>git rebase main", markdown.render(a.body), repr(a.body))

    def test_a_long_title_is_never_cut_inside_a_link(self):
        """An older item's title is never cut inside a link: the row would link a cut URL."""
        text = "word " * 20 + "see [the PR](https://github.com/acme/web/pull/1234) and decide now"
        title, long = state.Ask(text=text).title()
        self.assertTrue(long)
        self.assertEqual(title, ("word " * 20).strip() + " see…")

    def test_the_page_runs_scripts_from_static_only(self):
        """The page runs scripts from /static/ only: a preview's own .js is never one of the page's."""
        _, base = self.start(self.copy_fixture())
        for path in ("/", "/item/a1", "/items?q=x"):
            resp, _ = get(base, path)
            self.assertIn(f"script-src {base}/static/;", resp.header("Content-Security-Policy"), path)
            self.assertEqual(resp.header("X-Frame-Options"), "", f"{path}: a frame option would block the full view's own previews")

    def test_a_bodys_table_breaks_only_between_words(self):
        self.skipTest("style.css's table rules are owned by the agent porting the Markdown renderer and its CSS")


# The old athena's tests/test_dash.py: the `dash on|off|show` lifecycle, as athena/caps/dash/__init__.py has it.

class Lifecycle(Case):
    """The real server, started as `dash on` starts it, on the harness's free port against its scratch state dir."""

    def setUp(self):
        super().setUp()
        from athena.caps import dash
        self.dash = dash
        self.port = int(os.environ["ATHENA_DASH_PORT"])
        self.addCleanup(self._off)

    def _off(self):
        try:
            self.dash.off()
        except Exception:  # noqa: BLE001: the harness's own cleanup stops it too
            pass

    def fetch(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.read().decode()

    def alive(self, pid) -> bool:
        """on() started it as this process's child: once stopped it is a zombie until reaped, which kill(pid, 0) still finds."""
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_hq_pid_finding(self):
        self.skipTest("HqPidTest (find_hq): athena's dash is a service bound to no HQ process, so nothing finds HQ's pid")

    def test_source_hash_covers_the_server_but_not_its_tests(self):
        self.skipTest("SourceHashTest: the hash decided when to rebuild the Go binary; athena's dash is Python, nothing is built")

    def test_on_show_off(self):
        self.assertIsNone(self.dash.ours())
        first = self.dash.on()
        self.assertEqual(self.dash.url(), f"http://127.0.0.1:{self.port}/")
        self.assertEqual(first["state_dir"], str(self.state))
        self.assertEqual(json.loads(self.fetch("/healthz"))["pid"], first["pid"])
        self.assertIn("Needs you", self.fetch("/"))
        self.assertEqual(store.read_dict(store.path("run", "dash.json")), {"pid": first["pid"], "port": self.port})

        again = self.dash.on()
        self.assertEqual(again["pid"], first["pid"], "a second on must not start a second server")
        self.assertEqual(self.dash.ours()["pid"], first["pid"])
        shown = self.dash._run(type("A", (), {"action": "show"})())
        self.assertEqual(shown, 0)

        self.assertTrue(self.dash.off())
        deadline = time.monotonic() + 5
        while self.alive(first["pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.dash.ours())
        self.assertFalse(self.alive(first["pid"]))
        self.assertFalse(self.dash.off(), "off is idempotent")
        self.assertFalse(store.path("run", "dash.json").exists())

    def test_run_becomes_the_server_in_the_foreground_for_a_service(self):
        proc = subprocess.Popen([str(BIN), "dash", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.dash.ours():
            time.sleep(0.05)
        running = self.dash.ours()
        self.assertIsNotNone(running)
        self.assertEqual(running["pid"], proc.pid)  # the service manager supervises the server itself
        self.assertEqual(running["hq_pid"], 0)  # bound to no HQ
        self.assertEqual(json.loads(self.fetch("/healthz"))["pid"], proc.pid)
        # The old `dash run` said "already runs" and exited 0; athena's finds its port taken and says so, exiting 3.
        second = subprocess.run([str(BIN), "dash", "run"], capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 3, second.stderr)
        self.assertIn(f"port {self.port} is in use", second.stderr)
        self.assertIsNone(proc.poll())

    def test_a_second_on_adopts_a_server_it_did_not_record(self):
        first = self.dash.on()
        store.path("run", "dash.json").unlink()  # as when two `dash on` raced and one lost
        again = self.dash.on()
        self.assertEqual(again["pid"], first["pid"])
        self.assertEqual(self.dash.ours()["pid"], first["pid"])

    def test_racing_starts_share_one_server(self):
        results, errors = [], []

        def one():
            try:
                results.append(self.dash.on())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({r["pid"] for r in results}), 1, results)
        self.assertEqual(self.dash.ours()["pid"], results[0]["pid"])

    def test_adopted_server_keeps_its_hq(self):
        self.skipTest("athena's dash is bound to no HQ process (hq_pid is always 0), so there is no HQ to keep")

    def test_port_in_use(self):
        from athena.cli import UserError
        with socket.socket() as s:
            s.bind(("127.0.0.1", self.port))
            s.listen()
            with self.assertRaises(UserError) as caught:
                self.dash.on()
        # The old said "port N is in use" itself; athena says the dashboard exited at once, and its log says why.
        self.assertIn("exited at once", str(caught.exception))
        self.assertIn(f"port {self.port} is in use", (store.path("logs") / "dash.log").read_text())
        self.assertIsNone(self.dash.ours())
        self.assertFalse(store.path("run", "dash.json").exists())

    def test_stale_pid_file_is_ignored(self):
        store.write_json(store.path("run", "dash.json"), {"pid": os.getpid(), "port": self.port})
        self.assertIsNone(self.dash.ours(), "a live pid that is not our server is not running")
        self.assertFalse(self.dash.off(), "and is never signalled")

    def test_server_exits_when_hq_goes(self):
        self.skipTest("athena's dash is bound to no HQ process: it runs as a service until stopped")


# The old athena's tests/test_dashstate.py: watch.json, as athena's watch writes it, and history.jsonl, as its retire does.

class WatchFile(Case):
    def test_save_and_load_round_trip(self):
        from athena.caps import watch
        workers = {"a": {"state": "working", "pr": {"url": "u", "checks": "pending"}, "report": None, "detail": None}}
        watch.save_snapshot(workers, 100.0)
        saved = json.loads(store.path("watch.json").read_text())
        self.assertEqual(saved, {"ts": 100.0, "workers": workers})
        self.assertEqual(state.Store(store.state_dir()).load().watch.workers["a"].pr.url, "u")
        self.assertEqual([p.name for p in store.state_dir().glob("watch.json.*")], [], "no temp file left behind")

    def test_load_survives_missing_and_broken_files(self):
        self.assertIsNone(state.Store(store.state_dir()).load().watch)
        store.path("watch.json").write_text('{"ts": 1, "work')
        snap = state.Store(store.state_dir()).load()
        self.assertIsNone(snap.watch)
        self.assertIn("watch.json is unreadable", snap.notices)

    def test_watch_tick_writes_the_snapshot(self):
        from athena.caps import watch
        from athena.kernel import caps
        row = {"state": "working", "detail": None, "report": None, "last_report": None, "pr": {"url": "u"}, "mergeable": None, "quiet_s": None}
        with mock.patch.object(watch, "snapshot", return_value={"a": row}):
            prev, _, _ = watch.step(caps.CTX, {}, 0, watch.Dedupe())
        saved = store.read_json(store.path("watch.json"))
        self.assertEqual(saved["workers"], prev)
        self.assertEqual(saved["workers"]["a"]["pr"], {"url": "u"})


class History(Case):
    def setUp(self):
        super().setUp()
        from athena.kernel import agents
        self.repo_dir = self.repo()
        self.path = self.worktree(self.repo_dir)
        spawn_record("abc-1", self.path, self.repo_dir, linear="ABC-1", title="A title")
        pid = agents.pid_file("abc-1")
        pid.parent.mkdir(parents=True, exist_ok=True)
        pid.write_text(str(dead_pid()))
        store.write_json(store.path("workers", "abc-1.json"),
                         {"state": "done", "report": None, "last_report": {"status": "DONE", "pr": "https://github.com/acme/web/pull/9"}})

    def history(self):
        return list(store.read_lines(store.path("history.jsonl")))

    def test_retire_records_the_pr_from_gh(self):
        from athena.caps import prs, retire
        from athena.kernel import caps, ledger
        merged = {"number": 9, "url": "https://github.com/acme/web/pull/9", "state": "MERGED", "draft": False}
        with mock.patch.object(prs, "summary", return_value=merged) as summary:
            retire.retire(caps.CTX, "abc-1", keep_worktree=True)
        summary.assert_called_once_with(str(self.repo_dir), "t/w")
        [entry] = self.history()
        self.assertEqual(entry["pr"], merged)
        self.assertEqual((entry["name"], entry["linear"], entry["title"], entry["status"]), ("abc-1", "ABC-1", "A title", "DONE"))
        retired = ledger.get("abc-1")
        self.assertEqual(retired["state"], "retired")
        # The dash reads it back: the retired row's PR is the one gh named.
        snap = state.Store(store.state_dir()).load()
        self.assertEqual(snap.history[0].pr.state, "MERGED")

    def test_retire_falls_back_to_the_report(self):
        """The old fell back to watch.json's PR, then to the report's; athena's retire asks gh, then takes the report's."""
        from athena.caps import prs, retire
        from athena.kernel import caps
        with mock.patch.object(prs, "summary", return_value=None):
            retire.retire(caps.CTX, "abc-1", keep_worktree=True)
        [entry] = self.history()
        self.assertEqual(entry["pr"], {"url": "https://github.com/acme/web/pull/9"})

    def test_history_never_blocks_a_retire(self):
        from athena.caps import prs, retire
        from athena.kernel import caps, ledger
        with mock.patch.object(prs, "summary", side_effect=RuntimeError("gh broke")):
            retire.retire(caps.CTX, "abc-1", keep_worktree=True)
        self.assertEqual(ledger.get("abc-1")["state"], "retired")

