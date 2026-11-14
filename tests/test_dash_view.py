"""The old athena dash's view tests, ported one to one from its dash/view_test.go, kinds_test.go, week_test.go and openprs_test.go:
each Go test is a method of the same name in snake_case, every assertion kept."""
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import Case, ROOT

from athena.caps.dash import page, server, state, view
from athena.caps.dash.state import (Action, Applied, Ask, HistoryEntry, Hook, Option, PR, Move, Report, ReviewRequest, Snapshot,
                                        Stack, Watch, WatchWorker, Worker, pr_key)
from athena.caps.dash.view import Item, build

FIXTURE = ROOT / "tests" / "fixtures" / "dash" / "state"
FIXTURE_NOW = 1791500000
AWAITING_ALEX = "awaiting review · alex"
POST_TS = FIXTURE_NOW - 3600  # when the fixtures' PRs were posted in #open-prs


# Helpers, as the Go tests' own (view_test.go, awaiting_test.go, kinds_test.go, week_test.go, openprs_test.go, state_test.go,
# server_test.go, actions_test.go, items_test.go).

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


def asks_of(*texts):
    """An item per text, a decision (the human's call) when it starts "? " (cut off) and a result otherwise."""
    out = []
    for i, t in enumerate(texts):
        a = Ask(id=f"a{i + 1}", kind="result", next="you", name=f"Item {i + 1}", text=t, created=FIXTURE_NOW - 60)
        if t.startswith("? "):
            a.kind, a.text = "decision", t[2:]
        out.append(a)
    return out


def find(items, name) -> Item:
    """The row by its name, its worker's name or its item's id."""
    for it in items:
        if it.name == name or it.id == "w-" + name or it.id == "ask-" + name:
            return it
    return Item()


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


def snapshot_of(*prs) -> Snapshot:
    s = Snapshot(hooks={}, watch=Watch(ts=FIXTURE_NOW, prs={}))
    for p in prs:
        s.watch.prs[pr_key(p.url)] = p
    return s


def pill_keys(pills) -> str:
    out = []
    for p in pills:
        k = p.key
        if p.behind:
            k += "[" + " ".join(b.num.lstrip("#") for b in p.behind) + "]"
        out.append(k)
    return " ".join(out)


def between(s, start, end) -> str:
    i = s.find(start)
    if i < 0:
        return ""
    j = s[i:].find(end)
    if j < 0:
        return ""
    return s[i:i + j]


def item(id_, kind, next_, name, text, created) -> Ask:
    return Ask(id=id_, kind=kind, next=next_, name=name, text=text, created=created, updated=created)


def decision_with_default() -> Ask:
    return Ask(id="a1", kind="decision", next="you", name="Flaky e2e", text="Re-run it?", rec="re-run", auto=True,
               options=[Option(id="re-run", label="Re-run", do="reply")], created=FIXTURE_NOW - 3600, updated=FIXTURE_NOW - 3600)


def render(v, now=FIXTURE_NOW) -> str:
    """The "main" template, as the page renders it."""
    return page.Page(now).main(v)


_PILL_TAG = re.compile(r'<(?:button|a) [^>]*class="pill[^"]*"[^>]*>')
_OPTION = re.compile(r'data-option="([^"]+)"')


def posted_snapshot(keys, *prs) -> Snapshot:
    """The PRs as snapshot_of has them, with #open-prs configured and open-prs-posted.json holding keys, each posted at POST_TS
    (None: no such file yet)."""
    s = snapshot_of(*prs)
    s.open_prs_channel = "C0REVIEWS01"
    if keys is not None:
        s.posted = {k: POST_TS for k in keys}
    return s


def write(path, text):
    path.write_text(text)


def queued(d):
    return state.Store(d).load().actions


class DashCase(Case):
    def row_pills(self, v, id_) -> str:
        """Renders v and lists the pills of row id in order: each data-option, discuss, reply, or open-pr for the link."""
        html = render(v)
        row = between(html, f'<li id="{id_}"', "</li>")
        if not row:
            self.fail(f"no row {id_} in {html}")
        row = row.split('<div class="fold"', 1)[0]  # an ask folded under it has pills of its own
        out = []
        for tag in _PILL_TAG.findall(row):
            if "data-discuss" in tag:
                out.append("discuss")
            elif "data-reply" in tag:
                out.append("reply")
            elif tag.startswith("<a "):
                out.append("open-pr")
            else:
                out.append(_OPTION.search(tag).group(1))
        return " ".join(out)

    def copy_fixture(self):
        dst = self.tmp / f"fixture-{len(list(self.tmp.glob('fixture-*')))}"
        shutil.copytree(FIXTURE, dst)
        return dst

    def dir(self):
        d = self.tmp / f"dir-{len(list(self.tmp.glob('dir-*')))}"
        d.mkdir()
        return d

    def start(self, d):
        """As server.start, but with the fixture's clock from its first refresh on: server.start renders once by the real clock first,
        and today every fixture default is long due, so that render would queue them into actions.jsonl."""
        dash = server.Server(d, 0, "acme", clock=lambda: FIXTURE_NOW, athena=False)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(dash))
        httpd.daemon_threads = True
        dash.port = httpd.server_address[1]
        dash.refresh(True)
        threading.Thread(target=dash.poll, args=(1.0,), daemon=True).start()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(dash.stop)
        return dash, f"http://127.0.0.1:{dash.port}"

    @staticmethod
    def _do(req):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def get(self, url):
        return self._do(urllib.request.Request(url))

    def _post(self, dash, base, path, body):
        req = urllib.request.Request(base + path, method="POST", data=body.encode(), headers={
            "Origin": base, "Content-Type": "application/json", "X-Athena-Token": dash.token})
        code, text = self._do(req)
        try:
            out = json.loads(text)
        except ValueError:
            out = None
        return code, out

    def post(self, dash, base, body):
        return self._post(dash, base, "/api/action", body)

    def item_post(self, dash, base, body):
        return self._post(dash, base, "/api/item", body)

    def open_prs_on(self, d, posted):
        """athena's config.json in d names an #open-prs channel, and open-prs-posted.json holds posted."""
        write(d / state.ATHENA_CONFIG, '{"version": 1, "open_prs_channel": "C0REVIEWS01"}')
        write(d / state.POSTED, posted)


# view_test.go

class View(DashCase):
    def test_build_groups_the_fixture(self):
        v = build(state.Store(FIXTURE).load(), FIXTURE_NOW, "acme")
        self.assertEqual(ids(v.needs_you), "a7 a2 abc-102 abc-106 web a1 a5 a6 abc-108 abc-104 api web web", "needs you")
        self.assertEqual(ids(v.in_progress), "abc-109 abc-101 abc-103 abc-107 a9 a12", "in progress")
        self.assertEqual(ids(v.done), "a10 abc-100", "done today")
        self.assertEqual(v.title, "(13) athena")

        # The screenshot case: every PR awaiting a teammate's review is a pill on the Awaiting review row, a live idle
        # worker's (#907) as much as a followed one's, and the stack.json chain 906 <- 908 <- 909 is one pill with two behind.
        self.assertEqual(pill_keys(v.awaiting), "acme/api#71 acme/docs#212 acme/web#6452 acme/web#907 acme/web#906[908 909]", "awaiting")
        own = v.awaiting[3]
        self.assertTrue(own.label == "awaiting review · alex" and own.title == "Saved searches sync across devices" and own.num == "#907"
                        and own.url == "https://github.com/acme/web/pull/907", f"abc-452's pill {own}")
        # Items: the human's calls in Needs you, the one due tomorrow first; results and an applied default in Ready for
        # you; an item on a live worker names that worker's row; athena's own and CI's moves In flight; today's resolved one
        # in Done; a snoozed one off the page.
        r = v.needs_you[0]
        self.assertTrue(r.id == "ask-a7" and r.lead() == "Customer meeting" and r.due != 0 and r.href == "/item/a7",
                        f"the meeting due tomorrow leads: {r}")
        self.assertEqual(ids(v.ready), "a3 a8", "ready")
        r = find(v.in_progress, "Billing export")
        self.assertTrue(r.id == "w-abc-103" and r.title == "Billing page empty state" and r.href == "/item/a4", f"a4 names abc-103's row: {r}")
        self.assertEqual(v.snoozed, 1)
        a1 = find(v.needs_you, "Export queue start")
        self.assertTrue(a1.label == "your call" and a1.auto_at == FIXTURE_NOW - 2000 + view.DEFAULT_AFTER and a1.actions.take_rec is not None
                        and a1.actions.take_rec.label == "After ABC-101", f"a1 {a1}")
        a6 = find(v.needs_you, "Security: staging")
        self.assertTrue(a6.auto_at == 0 and a6.actions.take_rec is not None, f"a call marked security has a rec, never a default of its own: {a6}")
        # A retired worker whose PR is still open leaves Done for the row of whose move it is.
        r = find(v.needs_you, "abc-104")
        self.assertTrue(r.label == "changes requested" and r.badge and r.pr.text == "#64" and r.ticket == "ABC-104", f"abc-104 {r}")
        rr = Item()
        for it in v.needs_you:
            if it.id == "rr-acme-web-6170":
                rr = it
        self.assertTrue(rr.label == "your review requested" and rr.badge and rr.title == "Tidy the export queue"
                        and rr.pr.url == "https://github.com/acme/web/pull/6170" and rr.detail == "by kim", f"review request {rr}")

        perm = find(v.needs_you, "abc-102")
        self.assertTrue(perm.label == "permission prompt" and perm.badge and perm.icon == "blocked"
                        and perm.detail == "Claude needs your permission to use Bash", f"abc-102 {perm}")
        ctx = find(v.needs_you, "abc-106")
        self.assertTrue(ctx.label == "needs context" and ctx.detail == "Which ceiling: 5 tries, or 24 hours?", f"abc-106 {ctx}")
        ready = find(v.needs_you, "abc-108")
        self.assertTrue(ready.label == "approved · yours to merge" and ready.icon == "ready" and ready.pr.text == "#48"
                        and ready.pr.checks == "success", f"abc-108 {ready}")
        self.assertTrue(ready.ticket_url == "https://linear.app/acme/issue/ABC-108" and ready.repo == "web", f"abc-108 links {ready}")

        w = find(v.in_progress, "abc-101")
        self.assertTrue(w.label == "working" and w.pr.text == "#41 draft" and w.pr.checks == "pending" and w.pr.stale_since == 0, f"abc-101 {w}")
        w = find(v.in_progress, "abc-103")
        self.assertTrue(w.label == "done, athena reviewing" and w.icon == "review" and not w.badge, f"abc-103 {w}")
        w = find(v.in_progress, "abc-109")
        self.assertTrue(w.icon == "fail" and w.pr.checks == "failure", f"abc-109 {w}")
        w = find(v.in_progress, "abc-107")
        self.assertTrue(w.pr is None and w.label == "idle" and w.detail == "Waiting for the design tokens to land on main.", f"abc-107 {w}")

        # Done is today's: abc-097 to abc-099 retired days ago, and are found by search, not on the page.
        self.assertEqual(find(v.done, "abc-098").id, "", "abc-098 retired days ago")
        # Retired before history.jsonl existed: the PR comes from the worker file's last report.
        d = find(v.done, "abc-100")
        self.assertTrue(d.label == "retired" and d.pr is not None and d.pr.text == "#39" and d.when == FIXTURE_NOW - 3 * 3600, f"abc-100 {d}")
        s = state.Store(FIXTURE).load()
        v = build(s, 1791406400 + 600, "acme")  # the day abc-098 retired
        d = find(v.done, "abc-098")
        self.assertTrue(d.label == "merged" and d.icon == "merged" and d.pr.url == "https://github.com/acme/api/pull/77", f"abc-098 {d}")

    def test_done_hides_retired_workers_with_no_pr_and_no_ticket(self):
        s = state.Store(FIXTURE).load()
        s.workers += [Worker(name="probe", state="retired", updated=FIXTURE_NOW - 3600),
                      Worker(name="pr-only", state="retired", updated=FIXTURE_NOW - 3600)]
        s.hooks["pr-only"] = Hook(last_report=Report(status="DONE", pr="https://github.com/acme/web/pull/5"))
        v = build(s, FIXTURE_NOW, "acme")
        # probe has neither, pr-only only a PR; Done is today's, a10 the item resolved an hour ago.
        self.assertEqual(ids(v.done), "pr-only a10 abc-100", "done")

    def test_build_cases(self):
        def live(name):
            return Worker(name=name, state="live", linear=name.upper(), updated=FIXTURE_NOW)

        def open_(draft, review, checks):
            return PR(number=7, url="https://github.com/acme/web/pull/7", state="OPEN", draft=draft, review=review, checks=checks)

        def moved(label, kind, needs):
            p = open_(False, "REVIEW_REQUIRED", "success")
            p.move = Move(label=label, kind=kind, needs=needs)
            return p

        def awaiting():
            return moved("awaiting review · alex", "awaiting", False)

        WW = WatchWorker
        cases = [
            ("pending goal", Worker(name="a", state="live", goal_pending=True), Hook(), None, "needs", "waiting at a startup prompt"),
            ("blocked report", live("b"), Hook(state="blocked", report=Report(status="BLOCKED", concerns=["x"])), None, "needs", "blocked"),
            ("blocked, its PR approved", live("b2"), Hook(state="blocked"), WW(pr=moved("approved · yours to merge", "approved", True)), "needs", "permission prompt"),
            ("done, a teammate reviews", live("c"), Hook(state="done"), WW(pr=awaiting()), "awaiting", "awaiting review · alex"),
            ("done, nobody asked to review", live("c2"), Hook(state="done"), WW(pr=moved("no reviewer requested", "unrequested", True)), "needs", "no reviewer requested"),
            ("done, checks failing", live("d"), Hook(state="done"), WW(pr=moved("checks failing", "failing", False)), "progress", "checks failing"),
            ("idle after a DONE report", live("l"), Hook(state="idle", last_report=Report(status="DONE")), WW(pr=awaiting()), "awaiting", "awaiting review · alex"),
            ("exited after DONE", live("m"), Hook(state="exited", last_report=Report(status="DONE_WITH_CONCERNS")), WW(pr=moved("no reviewer requested", "unrequested", True)), "needs", "no reviewer requested"),
            ("idle with no report", live("n"), Hook(state="idle"), WW(pr=awaiting()), "awaiting", "awaiting review · alex"),
            ("no review data yet", live("n2"), Hook(state="idle"), WW(pr=open_(False, "", "success")), "progress", "idle"),
            ("done, draft", live("e"), Hook(state="done"), WW(pr=open_(True, "", "success")), "progress", "done, athena reviewing"),
            ("approved while working", live("f"), Hook(state="working"), WW(pr=moved("approved · yours to merge", "approved", True)), "progress", "working"),
            ("approved, worker done", live("f1"), Hook(state="done"), WW(pr=moved("approved · yours to merge", "approved", True)), "needs", "approved · yours to merge"),
            ("no review rule, done", live("f7"), Hook(state="done"), WW(pr=moved("your go to merge", "mergeable", True)), "needs", "your go to merge"),
            ("no review rule, idle after DONE", live("f9"), Hook(state="idle", last_report=Report(status="DONE")), WW(pr=moved("your go to merge", "mergeable", True)), "needs", "your go to merge"),
            ("no review rule, idle with no report", live("f10"), Hook(state="idle"), WW(pr=moved("your go to merge", "mergeable", True)), "progress", "idle"),
            ("approved, exited mid-task", live("f11"), Hook(state="exited"), WW(pr=moved("approved · yours to merge", "approved", True)), "progress", "its Claude exited"),
            ("approved, idle with no report", live("f12"), Hook(state="idle"), WW(pr=moved("approved · yours to merge", "approved", True)), "progress", "idle"),
            ("checks running", live("f13"), Hook(state="done"), WW(pr=moved("checks running", "pending", False)), "progress", "checks running"),
            ("done, athena reviewing its PR", live("r1"), Hook(state="done"), WW(pr=moved("in review · athena", "reviewing", False)), "progress", "in review · athena"),
            ("idle after DONE, athena reviewing", live("r2"), Hook(state="idle", last_report=Report(status="DONE")), WW(pr=moved("in review · athena", "reviewing", False)), "progress", "in review · athena"),
            ("idle with no report, not in review yet", live("r3"), Hook(state="idle"), WW(pr=moved("in review · athena", "reviewing", False)), "progress", "idle"),
            ("working, not in review yet", live("r4"), Hook(state="working"), WW(pr=moved("in review · athena", "reviewing", False)), "progress", "working"),
            ("review findings, worker fixing", live("r5"), Hook(state="working"), WW(pr=moved("review findings · worker fixing", "changes", False)), "progress", "review findings · worker fixing"),
            ("review findings, worker idle", live("r6"), Hook(state="idle", last_report=Report(status="DONE")), WW(pr=moved("review findings · worker fixing", "changes", False)), "progress", "review findings · worker fixing"),
            ("no review rule, still working", live("f8"), Hook(state="working"), WW(pr=moved("your go to merge", "mergeable", True)), "progress", "working"),
            ("approved, not the human's repo", live("f2"), Hook(state="idle", last_report=Report(status="DONE_WITH_CONCERNS")), WW(pr=moved("approved · your go to merge", "approved", True)), "needs", "approved · your go to merge"),
            ("approved, in the merge queue", live("f3"), Hook(state="idle"), WW(pr=moved("approved · in merge queue", "queued", False)), "progress", "approved · in merge queue"),
            ("approved, auto-merge on", live("f4"), Hook(state="working"), WW(pr=moved("approved · auto-merge on", "queued", False)), "progress", "approved · auto-merge on"),
            ("changes requested, worker on it", live("f5"), Hook(state="working"), WW(pr=moved("changes requested · worker fixing", "changes", False)), "progress", "changes requested · worker fixing"),
            ("still working, nobody asked to review", live("f6"), Hook(state="working"), WW(pr=moved("no reviewer requested", "unrequested", True)), "progress", "working"),
            ("merged, not retired", live("g"), Hook(state="idle"), WW(pr=PR(url="u", state="MERGED")), "done", "merged, not retired yet"),
            ("herdr fills an unknown hook state", live("h"), Hook(), WW(state="blocked"), "needs", "permission prompt"),
            ("no hook file yet", live("i"), Hook(), None, "progress", "starting"),
            ("exited", live("j"), Hook(state="exited"), None, "progress", "its Claude exited"),
            ("the report gate gave way", live("u1"), Hook(state="idle-unreported"), None, "progress", "stopped without a report"),
            ("the gate gave way, its PR open", live("u2"), Hook(state="idle-unreported"), WW(pr=open_(False, "", "success")), "progress", "stopped without a report"),
            ("the gate gave way after a DONE report", live("u3"), Hook(state="idle-unreported", last_report=Report(status="DONE")), WW(pr=moved("your go to merge", "mergeable", True)), "needs", "your go to merge"),
            ("concerns", live("k"), Hook(state="done", report=Report(status="DONE_WITH_CONCERNS")), None, "progress", "done with concerns, athena reviewing"),
        ]
        for name, w, hook, watch, group, label in cases:
            with self.subTest(name):
                s = Snapshot(workers=[w], hooks={w.name: hook})
                if watch is not None:
                    s.watch = Watch(ts=FIXTURE_NOW, workers={w.name: watch})
                v = build(s, FIXTURE_NOW, "acme")
                if group == "awaiting":
                    self.assertTrue(len(v.awaiting) == 1 and v.awaiting[0].label == label and
                                    len(v.needs_you) + len(v.in_progress) + len(v.done) == 0,
                                    f"want a pill {label!r}, got awaiting={v.awaiting} needs={v.needs_you} progress={v.in_progress} done={v.done}")
                    continue
                got = {"needs": v.needs_you, "progress": v.in_progress, "done": v.done}[group]
                self.assertTrue(len(got) == 1 and got[0].label == label and len(v.awaiting) == 0,
                                f"want {group} {label!r}, got needs={v.needs_you} progress={v.in_progress} done={v.done}")

    def test_in_review_is_in_progress(self):
        """A DONE worker's PR that waits on athena's own review is never the human's to merge yet."""
        p = PR(number=7, url="https://github.com/acme/web/pull/7", state="OPEN", checks="success",
               move=Move(label="in review · athena", kind="reviewing", athena_merges=True))
        w = Worker(name="r", state="live", updated=FIXTURE_NOW)
        s = Snapshot(workers=[w], hooks={"r": Hook(state="done")}, watch=Watch(ts=FIXTURE_NOW, workers={"r": WatchWorker(pr=p)}))
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(len(v.in_progress) == 1 and len(v.needs_you) == 0, f"want one In progress row, got needs={v.needs_you} progress={v.in_progress}")
        it = v.in_progress[0]
        self.assertFalse(it.icon != "review" or it.badge or it.actions is not None and (it.actions.go or it.actions.open_pr != ""),
                         f"in review: icon {it.icon!r} badge {it.badge} actions {it.actions}")

    def test_stale_pr_state_shows_its_age(self):
        s = state.Store(FIXTURE).load()
        v = build(s, FIXTURE_NOW + 600, "acme")
        self.assertEqual(find(v.in_progress, "abc-101").pr.stale_since, s.watch.ts)

    def test_done_window_and_cap(self):
        s = Snapshot(hooks={})
        for i in range(20):
            s.workers.append(Worker(name="r" + chr(ord("a") + i), linear="ABC-1", state="retired", updated=FIXTURE_NOW - i * 3600))
        s.workers.append(Worker(name="old", linear="ABC-1", state="retired", updated=FIXTURE_NOW - 15 * 24 * 3600))
        s.workers.append(Worker(name="gone", state="failed", updated=FIXTURE_NOW))
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(len(v.done) == view.DONE_MAX and v.done[0].name == "ra" and find(v.done, "old").name == "", f"done {ids(v.done)}")

    def test_pr_from_report_url(self):
        pr = view.pr_from_report(None, Report(pr="https://github.com/acme/web/pull/123"))
        self.assertTrue(pr is not None and pr.number == 123, pr)
        self.assertIsNone(view.pr_from_report(Report(pr=""), Report(pr="not a url")), "only an https url makes a PR link")

    def test_inline_escapes_and_links(self):
        got = view.inline("<script>x</script> **merge** [PR](https://github.com/acme/web/pull/1) see https://x.test/a?b=1&c=2 "
                          "[bad](javascript:alert(1)) `main`")
        for want in [
            "&lt;script&gt;x&lt;/script&gt;",
            "<strong>merge</strong>",
            '<a href="https://github.com/acme/web/pull/1">PR</a>',
            '<a href="https://x.test/a?b=1&amp;c=2">https://x.test/a?b=1&amp;c=2</a>',
            "[bad](javascript:alert(1))",
            '<span class="code">main</span>',
        ]:
            with self.subTest(want):
                self.assertIn(want, got)

    def test_summary_never_shows_the_report_json(self):
        rep = Report(status="DONE")
        cases = {
            "cut before the report": Hook(summary='Opened the PR.\n\nATHENA-REPORT {"status":"DONE"}', report=rep),
            "only the report's tail": Hook(summary='ERGEABLE -> pass","decisions":["Identity path"],"head":"ac2d32bbb7"}', report=rep),
            "tail, last report only": Hook(summary='tree verified"],"status":"DONE"}', last_report=rep),
        }
        want = {"cut before the report": "Opened the PR.", "only the report's tail": "", "tail, last report only": ""}
        for name, h in cases.items():
            with self.subTest(name):
                self.assertEqual(view.summary(h), want[name])
        self.assertEqual(view.summary(Hook(summary='Asked: "which ceiling?"')), 'Asked: "which ceiling?"', "a plain summary with quotes")

    def test_done_worker_shows_its_first_concern(self):
        h = Hook(state="done", summary='x"],"status":"DONE_WITH_CONCERNS"}',
                 report=Report(status="DONE_WITH_CONCERNS", concerns=["CI not checked on the new head"]))
        v = build(Snapshot(workers=[Worker(name="a", state="live")], hooks={"a": h}), FIXTURE_NOW, "acme")
        self.assertEqual(v.in_progress[0].detail, "CI not checked on the new head")

    def test_a_verbose_concern_shows_as_one_capped_line(self):
        verbose = ("CI not checked on the new head,\nbecause the runner queue was backed up for an hour and the job never started on "
                   "this push")
        for name, h in {
            "done": Hook(state="done", report=Report(status="DONE_WITH_CONCERNS", concerns=[verbose])),
            "blocked": Hook(state="blocked", report=Report(status="BLOCKED", concerns=[verbose])),
        }.items():
            with self.subTest(name):
                v = build(Snapshot(workers=[Worker(name="a", state="live")], hooks={"a": h}), FIXTURE_NOW, "acme")
                d = (v.in_progress + v.needs_you)[0].detail
                self.assertFalse("\n" in d or len(d) > view.REPORT_ITEM_MAX or not d.endswith("…"), f"detail {d!r}")
                self.assertTrue(d.startswith("CI not checked on the new head, because"), f"detail {d!r}")
        self.assertEqual(view.one_line("  short\n  one "), "short one", "short")
        self.assertEqual(view.one_line("x" * view.REPORT_ITEM_MAX), "x" * view.REPORT_ITEM_MAX, "at the cap")

    def test_inline_never_writes_inside_a_link(self):
        got = view.inline("[x](https://a.test/`b`/**c**) and **d** https://e.test/`f`")
        want = '<a href="https://a.test/`b`/**c**">x</a> and <strong>d</strong> <a href="https://e.test/`f`">https://e.test/`f`</a>'
        self.assertEqual(got, want)

    def test_ticket_links_need_a_workspace(self):
        s = Snapshot(workers=[Worker(name="abc-1", linear="abc-1", state="live")], hooks={})
        it = build(s, FIXTURE_NOW, "").in_progress[0]
        self.assertTrue(it.ticket == "ABC-1" and it.ticket_url == "", f"no workspace must leave the id unlinked: {it}")
        s.linear_workspace = "from-config"
        it = build(s, FIXTURE_NOW, "").in_progress[0]
        self.assertEqual(it.ticket_url, "https://linear.app/from-config/issue/ABC-1", "dash.json workspace")
        it = build(s, FIXTURE_NOW, "from-env").in_progress[0]
        self.assertEqual(it.ticket_url, "https://linear.app/from-env/issue/ABC-1", "the environment wins over dash.json")

    def test_every_followed_pr_has_one_row_by_whose_move_it_is(self):
        s = Snapshot(hooks={}, workers=[
            Worker(name="abc-1", linear="ABC-1", state="live", repo="/src/web"),
            Worker(name="abc-2", linear="ABC-2", state="retired", repo="/src/web", updated=FIXTURE_NOW - 3600),
            Worker(name="abc-3", linear="ABC-3", state="retired", repo="/src/web", updated=FIXTURE_NOW - 3600),
        ])
        s.history = [HistoryEntry(ts=FIXTURE_NOW - 3600, name="abc-2", pr=followed(2, "", "", "", False)),
                     HistoryEntry(ts=FIXTURE_NOW - 3600, name="abc-3", pr=followed(3, "", "", "", False))]
        merged = followed(3, "abc-3", "", "", False)
        merged.state = "MERGED"
        s.watch = Watch(ts=FIXTURE_NOW, workers={"abc-1": WatchWorker(state="idle", pr=followed(1, "", "awaiting review · alex", "awaiting", False))},
                        prs={
                            "acme/web#1": followed(1, "", "awaiting review · alex", "awaiting", False),  # its live worker's row has it
                            "acme/web#2": followed(2, "abc-2", "no reviewer requested", "unrequested", True),
                            "acme/web#3": merged,
                            "acme/web#10": followed(10, "", "approved · in merge queue", "queued", False),
                            "acme/web#11": followed(11, "", "approved · auto-merge on", "queued", False),
                            "acme/web#12": followed(12, "", "checks failing", "failing", True),
                            "acme/web#13": followed(13, "", "draft", "draft", False),
                            "acme/web#14": followed(14, "", "approved · yours to merge", "approved", True),
                        })
        v = build(s, FIXTURE_NOW, "acme")

        def label(items):
            return " | ".join(f"{it.pr.text} {it.label}" for it in items)
        self.assertEqual(label(v.needs_you), "#12 checks failing | #2 no reviewer requested | #14 approved · yours to merge", "needs you")
        self.assertEqual(label(v.in_progress), "#10 approved · in merge queue | #11 approved · auto-merge on | #13 draft", "in progress")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#1", "awaiting")  # its live worker's PR, once
        self.assertEqual(label(v.done), "#3 merged", "done")
        r = v.needs_you[1]
        self.assertTrue(r.name == "abc-2" and r.ticket == "ABC-2" and r.icon == "done" and r.badge, f"retired worker's open PR {r}")
        r = v.needs_you[0]
        self.assertTrue(r.name == "web" and r.title == "change 12" and r.icon == "fail" and r.id == "pr-acme-web-12", f"stacked PR {r}")

    def test_an_item_about_a_pr_is_a_row_of_its_own(self):
        """An item about a PR is a row of its own, never folded under the PR's row: a decision stays in Needs you even when its PR's
        row is In flight, and its row links the PR. A PR URL in its text shows as owner/repo#n."""
        s = Snapshot(hooks={}, workers=[Worker(name="abc-1", state="live")])
        s.watch = Watch(ts=FIXTURE_NOW, workers={"abc-1": WatchWorker(pr=followed(1, "", "approved · in merge queue", "queued", False))},
                        prs={"acme/web#9": followed(9, "", "approved · auto-merge on", "queued", False)})
        s.asks = asks_of(
            "? Is https://github.com/acme/web/pull/99 still needed? It has been idle a week.",
            "FYI: the quota resets at 14:00, see https://x.test/q?when=now",
            "? Merge **ABC-1** first?",
        )
        s.asks[2].pr = "acme/web#1"
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(ids(v.needs_you), "a1 a3", "needs you")
        self.assertEqual(ids(v.ready), "a2", "ready")
        self.assertEqual(ids(v.in_progress), "abc-1 web", "in flight")
        self.assertIn('<a href="https://github.com/acme/web/pull/99">acme/web#99</a> still needed?', v.needs_you[0].call,
                      "a PR URL in an item shows as owner/repo#n")
        self.assertIn('<a href="https://x.test/q?when=now">https://x.test/q?when=now</a>', v.ready[0].call, "any other URL stays as it is")
        r = v.needs_you[1]
        self.assertTrue(r.pr is not None and r.pr.url == "https://github.com/acme/web/pull/1" and r.pr.text == "#1"
                        and r.call == "Merge <strong>ABC-1</strong> first?", f"a3 links its PR on its own row: {r}")
        for i, want in enumerate(["ask", "ask"]):
            self.assertEqual(v.needs_you[i].icon, want, v.needs_you[i].id)
        self.assertEqual(v.title, "(2) athena")

    def test_no_item_hides_behind_the_done_list(self):
        """The bug the plan found: an ask folded under a Done row past the last one shown vanished with it. Items never fold, so with a
        full Done list, an item about the oldest retired worker's PR is still in Needs you."""
        s = Snapshot(hooks={})
        for n in range(1, view.DONE_MAX + 4):
            name = f"abc-{n}"
            s.workers.append(Worker(name=name, linear=name.upper(), state="retired", updated=FIXTURE_NOW - 100 * n))
            merged = followed(n, "", "", "", False)
            merged.state = "MERGED"
            s.history.append(HistoryEntry(ts=FIXTURE_NOW - 100 * n, name=name, pr=merged))
        s.asks = asks_of(f"? Announce https://github.com/acme/web/pull/{view.DONE_MAX + 3} in the release notes?")
        s.asks[0].pr = f"acme/web#{view.DONE_MAX + 3}"
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(len(v.done) == view.DONE_MAX and ids(v.needs_you) == "a1", f"needs {ids(v.needs_you)}, done {len(v.done)}")

    def test_review_requests_are_capped_and_skip_followed_prs(self):
        s = Snapshot(hooks={}, watch=Watch(ts=FIXTURE_NOW, prs={"acme/web#5": followed(5, "", "draft", "draft", False)}))
        for n in range(1, view.REVIEW_REQUESTS_MAX + 4):
            s.watch.review_requests.append(ReviewRequest(repo="acme/web", number=n, title="<b>t</b>"))
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(len(v.needs_you), view.REVIEW_REQUESTS_MAX)
        for it in v.needs_you:
            self.assertNotEqual(it.pr.text, "#5", "a followed PR gets one row")
        it = v.needs_you[0]
        self.assertTrue(it.pr.url == "https://github.com/acme/web/pull/1" and it.title == "<b>t</b>" and it.detail == "", it)

    def test_a_row_never_prints_its_name_twice(self):
        for name, title, want in [
            ("Release notes draft", "Release notes draft: the 2.4 date is in the doc", "the 2.4 date is in the doc"),
            ("Staging login", "staging login — which fix?", "which fix?"),
            ("ABC-101 rollout", "ABC-101 rollout · after #12 merges", "after #12 merges"),
            ("Ship it", "Ship it", ""),
            ("Ship", "Shipping the release notes", "Shipping the release notes"),  # a word that only starts with the name
            ("Tool upgrade", "Upgrade the tool 1.2 → 1.3 now", "Upgrade the tool 1.2 → 1.3 now"),  # the name is in it, not at its start
            ("", "A title", "A title"),
        ]:
            with self.subTest(name=name, title=title):
                self.assertEqual(view.without_name(title, name), want)
        # An old item, as it is in needs-you.json: its row shows the name once, then the rest of its title.
        s = Snapshot(hooks={}, asks=[Ask(id="a1", kind="decision", next="you", name="Disk cleanup",
                                         text="Disk cleanup: free 20 GB on the build box", why="the disk is 95% full")])
        it = build(s, FIXTURE_NOW, "").needs_you[0]
        self.assertTrue(it.lead() == "Disk cleanup" and it.second() == "free 20 GB on the build box" and it.detail == "",
                        f"{it.lead()!r} {it.second()!r} {it.detail!r}")

    def test_the_filter_matches_a_row_by_name_title_pr_and_worker(self):
        v = build(state.Store(FIXTURE).load(), FIXTURE_NOW, "acme")
        all_ = v.needs_you + v.ready + v.in_progress + v.done

        def row(id_):
            for it in all_:
                if it.id == id_:
                    return it.search()
            self.fail(f"no row {id_}")
        # An item's row: its name and its whole title (the part its row leaves out too), lower-cased.
        s = row("ask-a1")
        self.assertTrue("export queue start" in s and "start **abc-111** now, or after abc-101 merges?" in s, f"ask-a1: {s!r}")
        # A worker's row an item names: the item's name, the worker's name and title, its ticket, repo and PR.
        for want in ["billing export", "abc-103", "#906", "acme/web#906"]:
            self.assertIn(want, row("w-abc-103"), f"w-abc-103 lacks {want!r}")
        # The Awaiting review pills match the same way.
        for p in v.awaiting:
            s = p.search()
            self.assertTrue(p.title.lower() in s and p.num in s and p.key in s, f"pill {p.id}: {s!r}")
        # On the page, every row and pill carries it.
        _, base = self.start(self.copy_fixture())
        _, html = self.get(base + "/")
        self.assertTrue('<li id="ask-a1" class="item has-view" data-search="export queue start' in html and 'class="apr" id="' in html
                        and html.count('data-search="') >= len(all_) + len(v.awaiting),
                        f"rows without data-search: {html.count('data-search=')} of {len(all_)}+{len(v.awaiting)}")

    def test_each_section_is_newest_first(self):
        """Each section lists its rows newest first by their time (When): within each of Needs you's tiers but the first, whose items
        keep their due-date order, and in Ready for you and In flight. Done already was."""
        def ago(minutes):
            return FIXTURE_NOW - minutes * 60

        def live(name, ts):
            return Worker(name=name, state="live", updated=ts)

        def due(a, at):
            a.due = at
            return a
        s = Snapshot(
            workers=[live("blk-old", ago(90)), live("blk-new", ago(5)), live("run-old", ago(80)), live("run-new", ago(3)),
                     live("pr-old", ago(70)), live("pr-new", ago(4))],
            hooks={"blk-old": Hook(state="blocked"), "blk-new": Hook(state="blocked"), "run-old": Hook(state="working"),
                   "run-new": Hook(state="working"), "pr-old": Hook(state="done"), "pr-new": Hook(state="done")},
            asks=[
                due(item("due-later", "decision", "you", "Due later", "?", ago(1)), FIXTURE_NOW + 7200),
                due(item("due-sooner", "decision", "you", "Due sooner", "?", ago(300)), FIXTURE_NOW + 3600),
                item("call-old", "decision", "you", "Old call", "?", ago(120)),
                item("call-new", "decision", "you", "New call", "?", ago(2)),
                item("call-mid", "decision", "you", "Mid call", "?", ago(30)),
                item("res-old", "result", "you", "Old result", "r", ago(100)),
                item("res-new", "result", "you", "New result", "r", ago(10)),
                item("req-old", "request", "athena", "Old request", "q", ago(85)),
                item("req-new", "request", "athena", "New request", "q", ago(1)),
            ],
        )

        def unrequested(n):
            return WatchWorker(pr=followed(n, "", "no reviewer requested", "unrequested", True))
        s.watch = Watch(ts=FIXTURE_NOW, workers={"pr-old": unrequested(1), "pr-new": unrequested(2)})
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(ids(v.needs_you), "due-sooner due-later blk-new blk-old call-new call-mid call-old pr-new pr-old", "needs you")
        self.assertEqual(ids(v.ready), "res-new res-old", "ready for you")
        self.assertEqual(ids(v.in_progress), "req-new run-new run-old req-old", "in flight")

    def test_awaiting_pills_are_newest_first(self):
        """Awaiting review: the newest PR first (the higher number, within a repo), while a stack's PRs behind it keep chain order."""
        v = build(snapshot_of(
            awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(5, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(3, "main", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(6, "b3", AWAITING_ALEX, "awaiting", False),
            awaiting_pr(4, "b3", AWAITING_ALEX, "awaiting", False),
        ), FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#5 acme/web#3[4 6] acme/web#1", "awaiting")
        # Two pills of one stack.json chain whose middle PR is gone: a chain's place never outranks the newest first, and pills from
        # chains and from none sort the same every time.
        s = snapshot_of(awaiting_pr(1, "main", AWAITING_ALEX, "awaiting", False), awaiting_pr(3, "main", AWAITING_ALEX, "awaiting", False),
                        awaiting_pr(9, "main", AWAITING_ALEX, "awaiting", False), awaiting_pr(5, "main", AWAITING_ALEX, "awaiting", False),
                        awaiting_pr(7, "main", AWAITING_ALEX, "awaiting", False))
        s.stacks = [Stack(repo="acme/web", prs=[1, 2, 3]), Stack(repo="acme/web", prs=[4, 9]), Stack(repo="acme/web", prs=[5, 100])]
        for _ in range(50):
            self.assertEqual(pill_keys(build(s, FIXTURE_NOW, "acme").awaiting), "acme/web#9 acme/web#7 acme/web#5 acme/web#3 acme/web#1",
                             "chained pills")


# kinds_test.go

class Kinds(DashCase):
    def test_each_kind_offers_its_buttons(self):
        for kind, athena_merges, want in [
            ("unrequested", False, "request-review discuss"),
            ("failing", False, "recheck discuss"),
            ("approved", False, "open-pr discuss"),  # a human-merge repo: the human merges on GitHub
            ("approved", True, "open-pr go discuss"),
            ("mergeable", False, "open-pr discuss"),
            ("mergeable", True, "open-pr go discuss"),
            ("changes", False, "open-pr discuss"),
            ("changes", True, "open-pr discuss"),  # Merge is for a PR that is ready, nothing else
        ]:
            with self.subTest(kind=kind, athena_merges=athena_merges):
                p = followed(20, "", "its label", kind, True)
                p.head, p.move.athena_merges = "4f2c1ab9d0e3", athena_merges
                v = build(snapshot_of(p), FIXTURE_NOW, "acme")
                self.assertEqual(self.row_pills(v, "pr-acme-web-20"), want)
                a = v.actionable.get("pr:acme/web#20")
                self.assertFalse(a is None or not a.discuss or a.reply or a.dismiss or a.hides, f"the row's actions {a}")

    def test_kind_buttons_are_named_and_link_the_pr(self):
        p = followed(20, "", "your go to merge", "mergeable", True)
        p.head, p.move.athena_merges = "4f2c1ab9d0e3", True
        html = render(build(snapshot_of(p), FIXTURE_NOW, "acme"), time.time())
        for want in [
            '<div class="acts options" data-item="pr:acme/web#20">',  # Discuss shows as its icon alone, as beside athena's options
            '<a class="pill" href="https://github.com/acme/web/pull/20" aria-label="Open PR #20 on GitHub">',
            '<button type="button" class="pill" data-option="go" data-head="4f2c1ab9d0e3" aria-label="Merge: tell athena go">',
        ]:
            self.assertIn(want, html)
        q = followed(21, "", "no reviewer requested", "unrequested", True)
        f = followed(22, "", "checks failing", "failing", True)
        html = render(build(snapshot_of(q, f), FIXTURE_NOW, "acme"), time.time())
        for want in ['data-option="request-review" aria-label="Request review"', 'data-option="recheck" aria-label="Re-check and fix"']:
            self.assertIn(want, html)

    def test_merge_only_with_a_head_to_bind_the_go_to(self):
        p = followed(20, "", "your go to merge", "mergeable", True)
        p.move.athena_merges = True  # but watch.json has no head for it
        self.assertEqual(self.row_pills(build(snapshot_of(p), FIXTURE_NOW, "acme"), "pr-acme-web-20"), "open-pr discuss")

    def test_other_rows_keep_their_buttons(self):
        v = build(state.Store(FIXTURE).load(), FIXTURE_NOW, "acme")
        # A blocked live worker: Discuss and Reply, on its row, as before.
        self.assertEqual(self.row_pills(v, "w-abc-102"), "discuss reply", "a blocked worker")
        # A live worker whose PR is approved: the PR's buttons, its Discuss naming the PR.
        self.assertEqual(self.row_pills(v, "w-abc-108"), "open-pr discuss", "abc-108")
        self.assertTrue(v.actionable.get("w-abc-108") is None and v.actionable.get("pr:acme/web#48") is not None,
                        "abc-108's row answers for its PR")
        # A retired worker's open PR, changes requested.
        self.assertEqual(self.row_pills(v, "w-abc-104"), "open-pr discuss", "abc-104")
        # Your review requested: nothing, as before.
        self.assertEqual(self.row_pills(v, "rr-acme-web-6170"), "", "a review request")
        # In progress rows placed by their PR's state offer nothing.
        for it in v.in_progress:
            self.assertFalse(it.moved and it.actions is not None and it.actions.item != "w-" + it.name, f"{it.id}: {it.actions}")

    def test_a_queued_kind_answer_shows_as_sent(self):
        p = followed(20, "", "your go to merge", "mergeable", True)
        p.head, p.move.athena_merges = "4f2c1ab9d0e3", True
        s = snapshot_of(p)
        s.actions = [Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#20", option="go", status="queued")]
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(self.row_pills(v, "pr-acme-web-20"), "open-pr discuss", "a second go is offered")
        self.assertEqual(find(v.needs_you, "web").sent, "merge go sent")
        # Past STUCK_AFTER it is offered again.
        self.assertEqual(self.row_pills(build(s, FIXTURE_NOW + view.STUCK_AFTER, "acme"), "pr-acme-web-20"), "open-pr go discuss", "stuck")
        # A Discuss waiting does not hold the buttons back.
        s.actions[0].option, s.actions[0].discuss = "", True
        self.assertEqual(self.row_pills(build(s, FIXTURE_NOW, "acme"), "pr-acme-web-20"), "open-pr go discuss", "discuss queued")

    def test_a_kind_action_reaches_the_queue(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        for i, body in enumerate([
            '{"item": "pr:acme/web#912", "option": "request-review"}',
            '{"item": "pr:acme/web#913", "option": "recheck"}',
            '{"item": "pr:acme/api#75", "option": "go", "head": "8d1c2b3a4f5e"}',
            '{"item": "pr:acme/web#48", "discuss": true}',
        ]):
            code, out = self.post(srv, base, body)
            self.assertTrue(code == 202 and out["seq"] == i + 1, f"{body}: {code} {out}")
        got = queued(d)
        self.assertTrue(len(got) == 4 and got[0].option == "request-review" and got[1].option == "recheck" and got[2].option == "go"
                        and got[3].discuss, got)
        # The go names the head the human saw, so athena merges that commit or none.
        data = (d / state.ACTIONS).read_text()
        self.assertTrue('"head":"8d1c2b3a4f5e"' in data and data.count('"head"') == 1, f"actions.jsonl {data}")
        # One at a time: the row says it is sent, and a second is refused.
        code, _ = self.post(srv, base, '{"item": "pr:acme/api#75", "option": "go", "head": "8d1c2b3a4f5e"}')
        self.assertEqual(code, 422, "a second go")

    def test_kind_actions_the_server_refuses(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        for body, want in [
            ('{"item": "pr:acme/web#4242", "option": "request-review"}', 422),  # a PR the dash does not show
            ('{"item": "pr:acme/web#41", "option": "recheck"}', 422),  # shown, but In progress, by its worker's state
            ('{"item": "pr:acme/web#912", "option": "go", "head": "a1b2c3d4e5f6"}', 422),  # not ready to merge
            ('{"item": "pr:acme/web#912", "option": "recheck"}', 422),  # not the button its kind has
            ('{"item": "pr:acme/web#48", "option": "go", "head": "1a2b3c4d5e6f"}', 422),  # a human-merge repo: no Merge
            ('{"item": "pr:acme/web#48", "text": "merge it"}', 422),  # no Reply on a PR's row
            ('{"item": "pr:acme/api#75", "option": "go"}', 409),  # a go must name the head it is for
            ('{"item": "pr:acme/api#75", "option": "go", "head": "0000000000aa"}', 409),  # the PR moved on since the page
            ('{"item": "pr:acme/web#912", "option": "request-review", "head": "a1b2c3d4e5f6"}', 400),  # a head is a go's only
            ('{"item": "w-abc-108", "discuss": true}', 422),  # its row answers for its PR now
        ]:
            with self.subTest(body):
                code, out = self.post(srv, base, body)
                self.assertEqual(code, want, f"{body}: {code} {out}")
        self.assertEqual(queued(d), [], "queued")

    def test_merge_only_where_watch_says_athena_merges(self):
        d = self.dir()

        def pr(n, move):
            return (f'"acme/api#{n}": {{"number": {n}, "url": "https://github.com/acme/api/pull/{n}", "state": "OPEN", '
                    f'"head": "8d1c2b3a4f5e", "move": {{"kind": "mergeable", "label": "your go to merge", "needs": true{move}}}}}')
        prs = ", ".join([pr(1, ', "human_merge": false'), pr(2, ', "human_merge": true'), pr(3, "")])
        write(d / state.WATCH, f'{{"ts": {FIXTURE_NOW}, "workers": {{}}, "prs": {{{prs}}}}}')
        v = build(state.Store(d).load(), FIXTURE_NOW, "acme")
        for id_, want in {
            "pr-acme-api-1": "open-pr go discuss",  # athena merges on the human's go
            "pr-acme-api-2": "open-pr discuss",  # in human_merge_repos: the human merges on GitHub
            "pr-acme-api-3": "open-pr discuss",  # a watch.json from before the field: never a Merge
        }.items():
            with self.subTest(id_):
                self.assertEqual(self.row_pills(v, id_), want)

    def test_the_label_says_the_checks_so_the_pr_does_not(self):
        failing = followed(20, "", "checks failing", "failing", True)
        failing.checks = "failure"
        running = followed(21, "", "checks running", "pending", False)
        running.checks = "pending"
        green = followed(22, "", "your go to merge", "mergeable", True)
        green.checks = "success"
        v = build(snapshot_of(failing, running, green), FIXTURE_NOW, "acme")
        self.assertEqual(find(v.needs_you, "web").pr.checks, "", "a failing row's PR repeats its checks")
        self.assertEqual(v.in_progress[0].pr.checks, "", "a running row's PR repeats its checks")
        for it in v.needs_you:
            if it.label == "your go to merge":
                self.assertEqual(it.pr.checks, "success", "a ready row's checks")

    def test_a_pr_row_keeps_its_status_line_beside_its_pills(self):
        p = followed(20, "", "your go to merge", "mergeable", True)
        p.head, p.move.athena_merges = "4f2c1ab9d0e3", True
        html = render(build(snapshot_of(p), FIXTURE_NOW, "acme"), time.time())
        # keep: the status line stays as it is, and the pills have a line of their own under it.
        self.assertIn('<div class="swap has-acts keep"><p class="meta"><span class="label">your go to merge</span>', html)
        _, base = self.start(self.copy_fixture())
        _, html = self.get(base + "/")
        self.assertTrue('<li id="w-abc-102" class="item act" ' in html and "keep" not in between(html, '<li id="w-abc-102"', "</li>"),
                        "a worker's row swaps its line for its pills, as before")


# week_test.go: plan §6, "Your week, replayed": the stories that run through items and the dash. (A crash or a reboot, and a full
# disk, are athena doctor's: v2-alwayson.)

class Week(DashCase):
    def test_week_a_one_off_question(self):
        """A one-off question: the item appears as the human types (athena's move, In flight); the answer lands in Ready for you with
        its report; they read it and click Done; it is searchable later."""
        now = float(FIXTURE_NOW)
        q = item("a1", "request", "athena", "Atlas on clouds", "Checking CloudA, CloudB and CloudC for Atlas models", now - 600)
        v = build(Snapshot(asks=[q]), now, "")
        self.assertTrue(ids(v.in_progress) == "a1" and len(v.needs_you) == 0, f"asked: in flight {ids(v.in_progress)!r}")
        q.kind, q.next, q.text, q.body = "result", "you", "None of CloudA, CloudB or CloudC carry Atlas", "**None.** Checked each catalog."
        v = build(Snapshot(asks=[q]), now, "")
        self.assertTrue(ids(v.ready) == "a1" and v.ready[0].actions is not None and v.ready[0].actions.done, f"answered: ready {ids(v.ready)!r}")
        d = self.dir()
        write(d / state.ASKS, '{"next": 2, "items": [{"id": "a1", "kind": "result", "next": "you", "name": "Atlas on clouds",\n'
                              '\t\t"text": "None of CloudA, CloudB or CloudC carry Atlas", "body": "**None.** Checked each catalog.", '
                              '"created": 1791499400}]}')
        srv, base = self.start(d)
        code, out = self.item_post(srv, base, '{"item": "a1", "do": "done"}')
        self.assertEqual(code, 200, f"Done: {code} {out}")
        _, v, _ = srv.snapshot()
        self.assertTrue(ids(v.ready) == "" and ids(v.done) == "a1", f"read: ready {ids(v.ready)!r} done {ids(v.done)!r}")
        _, found = self.get(base + "/items?q=atlas+clouds")
        self.assertIn('<a href="/item/a1">Atlas on clouds</a>', found, "not searchable later")

    def test_week_research_plan_build(self):
        """Research, plan, build: the restatement is a decision with a rec (Take all recs); the build is a request a worker carries,
        named on the worker's row; once its PR merges it waits in Ready for you until the human looks, unless they took the last step
        themselves (HQ resolves it then, "you merged it")."""
        now = float(FIXTURE_NOW)
        plan = item("a1", "decision", "you", "⌘K palette", "Feature flag no, shared UI no, DB only if perf needs it: take all recs?", now - 3600)
        plan.options, plan.rec = [Option(id="take-all-recs", label="Take all recs", do="reply")], "take-all-recs"
        v = build(Snapshot(asks=[plan]), now, "")
        r = v.needs_you[0]
        self.assertTrue(r.actions.take_rec is not None and r.actions.take_rec.label == "Take all recs", f"the restatement offers Take rec: {r}")
        s = Snapshot(hooks={"abc-7": Hook(state="working")}, workers=[Worker(name="abc-7", linear="ABC-7", title="Command palette", state="live")],
                     asks=[item("a2", "request", "worker", "⌘K palette", "Building it", now - 1800)])
        s.asks[0].workers, s.asks[0].pr = ["abc-7"], "acme/web#70"
        v = build(s, now, "acme")
        r = find(v.in_progress, "abc-7")
        self.assertTrue(r.lead() == "⌘K palette" and r.second() == "Command palette" and r.href == "/item/a2" and ids(v.in_progress) == "abc-7",
                        f"building: {r}")
        merged = followed(70, "", "", "", False)
        merged.state = "MERGED"
        s.workers[0].state, s.workers[0].updated = "retired", now - 60
        s.history = [HistoryEntry(ts=now - 60, name="abc-7", pr=merged)]
        v = build(s, now, "acme")
        r = find(v.ready, "a2")
        self.assertTrue(r.label == "merged: done when you have looked" and r.actions.done, f"merged: {v.ready}")
        s.asks[0].resolved, s.asks[0].resolved_note = now, "you merged it"
        v = build(s, now, "acme")
        self.assertTrue(find(v.done, "a2").label == "you merged it" and len(v.ready) == 0, f"the human merged it: done {v.done}")

    def test_week_a_weekend_away(self):
        """A weekend away: chat stays quiet and reversible calls take their default after the stated time, 2 h once the human is away;
        a call with stakes waits for them. Monday's Ready for you says what athena took."""
        friday = float(FIXTURE_NOW)
        rerun = decision_with_default()
        rerun.created, rerun.updated = friday + 600, friday + 600
        reply = item("a2", "decision", "you", "Customer reply", "Send the drafted reply? (rec send)", friday + 600)
        reply.options, reply.rec, reply.auto, reply.stakes = [Option(id="send", label="Send", do="reply")], "send", True, ["customer"]
        s = Snapshot(asks=[rerun, reply], last_seen=friday)
        self.assertEqual(view.due_defaults(s, friday + view.AWAY_AFTER), [], "too soon")
        got = view.due_defaults(s, friday + 600 + view.AWAY_AFTER)
        self.assertTrue(len(got) == 1 and got[0].id == "a1", f"Friday night: {got}")
        self.assertEqual(len(view.due_defaults(s, friday + 3 * 86400)), 1, "Monday, the customer reply still waits")
        s.actions = [Action(seq=1, ts=friday + 600 + view.AWAY_AFTER, item="a1", option="re-run", auto=True, status="done")]
        s.asks[0].applied = Applied(option="re-run", label="Re-run", ts=friday + 700 + view.AWAY_AFTER)
        v = build(s, friday + 3 * 86400, "")
        r = find(v.ready, "a1")
        self.assertTrue(r.label == "athena took the rec on its own: Re-run" and r.actions.undo == "Re-run", f"Monday: {v.ready}")
        self.assertEqual(ids(v.needs_you), "a2", "the call with stakes is still the human's")

    def test_week_a_customer_thread(self):
        """A customer thread: the meeting is a promise with a due date, so it sorts to the top the day before; the draft reply is in its
        full view; when the human posts it, athena resolves it."""
        now = float(FIXTURE_NOW)
        meeting = item("a3", "promise", "you", "Customer meeting", "Confirm the customer's engineer for Wed 11:30", now - 2 * 86400)
        meeting.due, meeting.body = now + 20 * 3600, "**Draft reply:** Wednesday 11:30 works."
        other = item("a1", "decision", "you", "Docs domain", "Move the docs domain?", now - 5 * 86400)
        s = Snapshot(hooks={"abc-1": Hook(state="blocked")}, workers=[Worker(name="abc-1", state="live")], asks=[other, meeting])
        v = build(s, now, "")
        self.assertEqual(ids(v.needs_you), "a3 abc-1 a1", "the day before, the meeting leads")
        self.assertEqual(v.needs_you[0].due, meeting.due, "no due date on the row")
        got = page.due_text(page.unix(meeting.due), page.unix(now))
        self.assertTrue(got == "due tmrw" or got.startswith("due "), got)
        s.asks[1].resolved, s.asks[1].resolved_note = now + 3600, "you posted the reply"
        v = build(s, now + 3700, "")
        self.assertTrue(find(v.done, "a3").label == "you posted the reply" and find(v.needs_you, "a3").id == "", f"posted: {v.done}")


# openprs_test.go

class OpenPRs(DashCase):
    def test_a_posted_unrequested_pr_awaits_review_with_its_marker(self):
        posted = awaiting_pr(20, "main", "no reviewer requested", "unrequested", True)
        unposted = awaiting_pr(21, "main", "no reviewer requested", "unrequested", True)
        v = build(posted_snapshot(["acme/web#20"], posted, unposted), FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#20" and v.awaiting_count == 1, f"awaiting {pill_keys(v.awaiting)}")
        p = v.awaiting[0]
        self.assertTrue(p.posted and p.label == "posted in #open-prs" and p.actions.recheck and p.actions.dismiss, f"pill {p}")
        self.assertTrue(len(v.needs_you) == 1 and v.needs_you[0].pr.url == unposted.url, f"needs you {v.needs_you}")
        html = render(v)
        pill = between(html, '<div class="apr" id="aw-acme-web-20"', "</div>\n</div>")
        self.assertTrue('<span class="apr-posted">posted in #open-prs</span>' in pill and ', posted in #open-prs"' in pill,
                        f"no marker on the pill: {pill}")
        self.assertEqual(self.row_pills(v, "pr-acme-web-21"), "post-open-prs request-review discuss", "the unposted row")
        self.assertIn('<button type="button" class="pill" data-option="post-open-prs" aria-label="Post to #open-prs">', html)

    def test_a_posted_pr_a_teammate_reviewed_since_is_the_humans_again(self):
        # A teammate reviewed it after the post (a comment-only review drops them from the requested reviewers), so no reviewer is
        # requested again: the human re-requests or answers, whatever #open-prs says.
        p = awaiting_pr(20, "main", "no reviewer requested", "unrequested", True)
        p.reviewed_at = POST_TS + 60
        v = build(posted_snapshot(["acme/web#20"], p), FIXTURE_NOW, "acme")
        self.assertTrue(len(v.awaiting) == 0 and len(v.needs_you) == 1, f"awaiting {pill_keys(v.awaiting)}, needs {len(v.needs_you)}")
        self.assertEqual(self.row_pills(v, "pr-acme-web-20"), "post-open-prs request-review discuss")
        # A review from before the post: the post still asks for a fresh look.
        p.reviewed_at = POST_TS - 60
        v = build(posted_snapshot(["acme/web#20"], p), FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#20", "awaiting")

    def test_a_re_post_after_the_review_awaits_review_again(self):
        p = awaiting_pr(20, "main", "no reviewer requested", "unrequested", True)
        p.reviewed_at = POST_TS + 60
        s = posted_snapshot(["acme/web#20"], p)
        s.posted["acme/web#20"] = POST_TS + 120  # posted again after the review (the doorbell keeps the latest)
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(pill_keys(v.awaiting) == "acme/web#20" and v.awaiting[0].posted, f"awaiting {pill_keys(v.awaiting)}")

    def test_only_a_teammates_review_counts(self):
        # watch.json as athena watch writes it: github-actions' COMMENTED review and the author's own are in "reviews", and
        # teammate_reviewed_at names only the teammate's (athena_lib/pr.py _teammate_reviewed_at; tests/test_open_prs.py).
        d = self.dir()
        self.open_prs_on(d, '{"acme/web#20": "1791496400.000100", "acme/web#21": "1791496400.000100"}')
        write(d / state.WATCH, '{"ts": ' + str(FIXTURE_NOW) + ''', "workers": {}, "prs": {
	 "acme/web#20": {"number": 20, "url": "https://github.com/acme/web/pull/20", "state": "OPEN",
	   "reviews": {"github-actions": "COMMENTED", "you": "COMMENTED"}, "teammate_reviewed_at": "",
	   "move": {"kind": "unrequested", "label": "no reviewer requested", "needs": true}},
	 "acme/web#21": {"number": 21, "url": "https://github.com/acme/web/pull/21", "state": "OPEN",
	   "reviews": {"github-actions": "COMMENTED", "kim": "COMMENTED"}, "teammate_reviewed_at": "2026-10-14T21:00:00Z",
	   "move": {"kind": "unrequested", "label": "no reviewer requested", "needs": true}}}}''')
        v = build(state.Store(d).load(), FIXTURE_NOW, "acme")
        self.assertEqual(pill_keys(v.awaiting), "acme/web#20", "a bot's or the author's review moved it")
        self.assertTrue(len(v.needs_you) == 1 and v.needs_you[0].pr.url == "https://github.com/acme/web/pull/21",
                        f"a teammate's review since the post did not: {v.needs_you}")

    def test_nothing_without_the_channel_configured(self):
        p = awaiting_pr(20, "main", "no reviewer requested", "unrequested", True)
        q = awaiting_pr(21, "main", "no reviewer requested", "unrequested", True)
        s = posted_snapshot(["acme/web#20"], p, q)
        s.open_prs_channel = ""  # the key removed, the file left behind
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(len(v.awaiting), 0, f"awaiting {pill_keys(v.awaiting)}")
        self.assertEqual(self.row_pills(v, "pr-acme-web-20"), "request-review discuss")
        self.assertEqual(self.row_pills(v, "pr-acme-web-21"), "request-review discuss")

    def test_other_kinds_keep_their_rows_when_posted(self):
        for kind in ["approved", "mergeable", "changes", "failing", "draft", "pending", "queued"]:
            with self.subTest(kind):
                needs = kind not in ("draft", "pending", "queued")
                p = awaiting_pr(20, "main", "its label", kind, needs)
                without = build(posted_snapshot(None, p), FIXTURE_NOW, "acme")
                with_ = build(posted_snapshot(["acme/web#20"], p), FIXTURE_NOW, "acme")
                self.assertTrue(len(with_.awaiting) == 0 and len(with_.needs_you) == len(without.needs_you)
                                and len(with_.in_progress) == len(without.in_progress),
                                f"{kind} moved: awaiting {pill_keys(with_.awaiting)}, needs {len(with_.needs_you)}/{len(without.needs_you)}, "
                                f"in progress {len(with_.in_progress)}/{len(without.in_progress)}")
        # Awaiting a teammate already: the pill is as it was, no marker.
        a = awaiting_pr(22, "main", AWAITING_ALEX, "awaiting", False)
        v = build(posted_snapshot(["acme/web#22"], a), FIXTURE_NOW, "acme")
        self.assertTrue(len(v.awaiting) == 1 and not v.awaiting[0].posted and v.awaiting[0].label == AWAITING_ALEX, v.awaiting)

    def test_the_post_button_is_on_unrequested_rows_only_once_the_file_exists(self):
        for kind, want in [
            ("unrequested", "post-open-prs request-review discuss"),
            ("failing", "recheck discuss"),
            ("approved", "open-pr discuss"),
            ("changes", "open-pr discuss"),
        ]:
            with self.subTest(kind):
                p = followed(20, "", "its label", kind, True)
                v = build(posted_snapshot(["acme/web#999"], p), FIXTURE_NOW, "acme")
                self.assertEqual(self.row_pills(v, "pr-acme-web-20"), want)
        # No open-prs-posted.json (no #open-prs channel configured): no button.
        p = followed(20, "", "no reviewer requested", "unrequested", True)
        self.assertEqual(self.row_pills(build(posted_snapshot(None, p), FIXTURE_NOW, "acme"), "pr-acme-web-20"), "request-review discuss",
                         "without the file")

    def test_a_queued_post_shows_posting_until_athena_is_done(self):
        p = followed(20, "", "no reviewer requested", "unrequested", True)
        s = posted_snapshot([], p)
        s.actions = [Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#20", option="post-open-prs", status="queued")]
        v = build(s, FIXTURE_NOW, "acme")
        self.assertEqual(find(v.needs_you, "web").sent, "posting…")
        self.assertEqual(self.row_pills(v, "pr-acme-web-20"), "discuss", "while posting")
        s.actions[0].status = "done"
        self.assertEqual(self.row_pills(build(s, FIXTURE_NOW, "acme"), "pr-acme-web-20"), "post-open-prs request-review discuss", "done")

    def test_post_to_open_prs_reaches_the_queue(self):
        d = self.copy_fixture()
        self.open_prs_on(d, "{}\n")
        srv, base = self.start(d)
        code, out = self.post(srv, base, '{"item": "pr:acme/web#912", "option": "post-open-prs"}')
        self.assertEqual(code, 202, f"{code} {out}")
        got = queued(d)
        self.assertTrue(len(got) == 1 and got[0].item == "pr:acme/web#912" and got[0].option == "post-open-prs" and got[0].status == "queued", got)
        main, _, _ = srv.snapshot()
        row = between(main, '<li id="pr-acme-web-912"', "</li>")
        self.assertTrue('<span class="sent">posting…</span>' in row and 'data-option="post-open-prs"' not in row, f"row {row}")
        for body, want in [
            ('{"item": "pr:acme/web#912", "option": "post-open-prs"}', 422),  # one at a time
            ('{"item": "pr:acme/web#48", "option": "post-open-prs"}', 422),  # approved: not unrequested
            ('{"item": "pr:acme/web#4242", "option": "post-open-prs"}', 422),
        ]:
            with self.subTest(body):
                code, out = self.post(srv, base, body)
                self.assertEqual(code, want, f"{body}: {code} {out}")
        # A PR posted meanwhile (the doorbell, or `athena open-prs add`) leaves Needs you for Awaiting review.
        write(d / state.POSTED, '{"acme/web#912": "1791499990.000100"}')
        srv.refresh(True)
        _, v, _ = srv.snapshot()
        self.assertIn("pr:acme/web#912", v.pills, f"not on the Awaiting review row: {pill_keys(v.awaiting)}")

    def test_a_posted_pill_is_not_rechecking_while_its_post_is_queued(self):
        p = awaiting_pr(20, "main", "no reviewer requested", "unrequested", True)
        s = posted_snapshot(["acme/web#20"], p)
        s.actions = [Action(seq=1, ts=FIXTURE_NOW - 30, item="pr:acme/web#20", option="post-open-prs", status="queued")]
        v = build(s, FIXTURE_NOW, "acme")
        self.assertTrue(len(v.awaiting) == 1 and not v.awaiting[0].rechecking and v.awaiting[0].actions.recheck, v.awaiting)
        s.actions[0].option = "recheck"
        self.assertTrue(build(s, FIXTURE_NOW, "acme").awaiting[0].rechecking, "a queued re-check no longer shows")

    def test_one_button_answer_per_pr_at_a_time(self):
        d = self.copy_fixture()
        self.open_prs_on(d, "{}\n")
        srv, base = self.start(d)
        # Two tabs: each saw Post to #open-prs and Request review offered before either answered.
        _, stale, _ = srv.snapshot()
        with srv._mu:
            srv.view = stale
        code, out = self.post(srv, base, '{"item": "pr:acme/web#912", "option": "post-open-prs"}')
        self.assertEqual(code, 202, f"{code} {out}")
        with srv._mu:
            srv.view = stale
        code, _ = self.post(srv, base, '{"item": "pr:acme/web#912", "option": "request-review"}')
        self.assertEqual(code, 409, "a second button answer for the PR")

    def test_without_the_file_the_post_is_refused(self):
        d = self.copy_fixture()
        srv, base = self.start(d)
        code, _ = self.post(srv, base, '{"item": "pr:acme/web#912", "option": "post-open-prs"}')
        self.assertEqual(code, 422)

    def test_the_posted_file_is_read_live_and_checked(self):
        d = self.dir()
        st = state.Store(d)
        st.changed()
        self.assertIsNone(st.load().posted, "no file")
        write(d / state.POSTED, '{"Acme/Web#12": "1.0", "not a key": "2.0", "acme/web#0": "3.0", "acme/api#7": 4}')
        self.assertTrue(st.changed(), "a new open-prs-posted.json is not a change")
        s = st.load()
        self.assertTrue(len(s.posted) == 2 and s.posted["acme/web#12"] == 1 and s.posted["acme/api#7"] == 4, s.posted)
        self.assertEqual(s.open_prs_channel, "", "no config.json, yet a channel")
        write(d / state.ATHENA_CONFIG, '{"open_prs_channel": "C0REVIEWS01"}')
        self.assertTrue(st.changed() and st.load().open_prs_channel == "C0REVIEWS01", "config.json's channel is not read live")
        write(d / state.ATHENA_CONFIG, '{"open_prs_channel": "#open-prs"}')
        self.assertEqual(st.load().open_prs_channel, "", "a malformed channel id is taken")
        write(d / state.POSTED, "{}")
        s = st.load()
        self.assertTrue(s.posted is not None and len(s.posted) == 0, f"an empty file turns the button on: {s.posted}")

    def test_the_lanes_record_is_no_post(self):
        """The automatic lane keeps its record in the same file, under "_auto" (athena_lib/slack.py LANE): a PR it asked HQ to post, or
        one it followed before it started, is not posted until HQ records the post."""
        d = self.dir()
        st = state.Store(d)
        write(d / state.POSTED, '{"_auto": {"acme/web#12": {"asked": 1.0, "rings": 1, "worker": "abc-12"},\n'
                                '\t\t"acme/web#13": {"followed": 1.0}}, "acme/api#7": "4.0"}')
        s = st.load()
        self.assertTrue(len(s.posted) == 1 and s.posted["acme/api#7"] == 4, s.posted)


if __name__ == "__main__":
    import unittest
    unittest.main()
