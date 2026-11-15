"""Two misses of the watch: a busy Slack conversation never rang again, and AI reviewers' inline threads never rang."""
import json
import time
from unittest import mock

from harness import Case, spawn_record, store

from athena.caps import follow, prs, slack, watch
from athena.kernel import caps, ledger

DAY = 86400.0
T0 = 1_794_300_000.0  # Nov 2026


class SlackLike:
    """conversations.history as Slack answers it: newest first, `limit` a page, a cursor while more remain. Given `oldest` and no
    `latest`, the page is the OLDEST `limit` messages after `oldest` (what hid a busy DM's new messages); otherwise the newest before
    `latest`. conversations.replies answers from `replies`."""

    def __init__(self, history, replies=None, dms=("D1",)):
        self.history, self.replies, self.dms, self.calls = history, replies or {}, list(dms), []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "auth.test":
            return {"user_id": "ME"}
        if method == "users.conversations":
            return {"channels": [{"id": d} for d in self.dms]}
        if method == "conversations.replies":
            return {"messages": self.replies.get((params["channel"], params["ts"]), [])}
        oldest, latest = float(params.get("oldest") or 0), params.get("latest")
        inside = [m for m in self.history.get(params["channel"], [])
                  if float(m["ts"]) > oldest and (latest is None or float(m["ts"]) < float(latest))]
        window = sorted(inside, key=lambda m: float(m["ts"]), reverse=not ("oldest" in params and latest is None))
        start, limit = int(params.get("cursor") or 0), int(params.get("limit") or 100)
        page, more = window[start:start + limit], start + limit < len(window)
        return {"messages": sorted(page, key=lambda m: float(m["ts"]), reverse=True), "has_more": more,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}

    def history_calls(self):
        return [p for m, p in self.calls if m == "conversations.history"]


def _busy(conversation_count=150, until=T0, user="U1"):
    """A conversation with `conversation_count` top-level messages in the six days before `until`."""
    step = 6 * DAY / conversation_count
    return [{"ts": f"{until - 6 * DAY + n * step:.6f}", "user": user} for n in range(conversation_count)]


class SlackBusyConversation(Case):
    def _started(self, dms=("D1",), at=T0):
        state = {}
        self.assertEqual(slack.poll(SlackLike({}, dms=dms), state, [], now=at), [])
        return state

    def test_a_conversation_with_over_100_messages_in_7_days_rings_its_new_message_once(self):
        state = self._started()
        new = {"ts": f"{T0 + 30:.6f}", "user": "U1"}
        client = SlackLike({"D1": _busy(150) + [new]})
        self.assertEqual(slack.poll(client, state, [], active_minutes=0, now=T0 + 60), [("D1", 1, True, False)])
        self.assertEqual(state["seen"]["D1"], new["ts"], "the cursor moves to the new message")
        self.assertEqual(slack.poll(client, state, [], active_minutes=0, now=T0 + 120), [], "and it rings once")
        later = {"ts": f"{T0 + 150:.6f}", "user": "U2"}
        client = SlackLike({"D1": _busy(150) + [new, later]})
        self.assertEqual(slack.poll(client, state, [], active_minutes=0, now=T0 + 180), [("D1", 1, True, False)],
                         "and the next one rings too: the cursor never sticks")

    def test_the_read_is_one_call_when_the_cursor_is_on_the_newest_page(self):
        state = self._started()
        client = SlackLike({"D1": _busy(150) + [{"ts": f"{T0 + 30:.6f}", "user": "U1"}]})
        slack.poll(client, state, [], active_minutes=0, now=T0 + 60)
        self.assertEqual(len(client.history_calls()), 1)
        self.assertNotIn("oldest", client.history_calls()[0], "never Slack's oldest-first page")

    def test_a_thread_in_a_busy_conversation_rings_from_the_newest_parents(self):
        state = self._started()
        parent = {"ts": f"{T0 - 100:.6f}", "user": "ME", "thread_ts": f"{T0 - 100:.6f}", "reply_count": 1,
                  "latest_reply": f"{T0 + 30:.6f}", "reply_users": ["U1"]}
        reply = {"ts": f"{T0 + 30:.6f}", "user": "U1", "thread_ts": parent["ts"]}
        client = SlackLike({"D1": _busy(150, until=T0 - 200) + [parent]}, {("D1", parent["ts"]): [parent, reply]})
        self.assertEqual(slack.poll(client, state, [], active_minutes=0, now=T0 + 60), [("D1", 1, True, False, parent["ts"])])

    def test_a_stale_cursor_catches_up_in_one_line_per_conversation_with_its_real_count(self):
        state = self._started(dms=("D1", "D2"))
        parent = {"ts": f"{T0 + 10:.6f}", "user": "ME", "thread_ts": f"{T0 + 10:.6f}", "reply_count": 2,
                  "latest_reply": f"{T0 + 900:.6f}", "reply_users": ["U1"]}
        replies = [parent, {"ts": f"{T0 + 800:.6f}", "user": "U1", "thread_ts": parent["ts"]},
                   {"ts": f"{T0 + 900:.6f}", "user": "U1", "thread_ts": parent["ts"]}]
        backlog = [{"ts": f"{T0 + 20 + n:.6f}", "user": "U1"} for n in range(250)]
        client = SlackLike({"D1": _busy(150) + [parent] + backlog, "D2": [{"ts": f"{T0 + 50 + n:.6f}", "user": "U2"} for n in range(3)]},
                           {("D1", parent["ts"]): replies}, dms=("D1", "D2"))
        rings = slack.poll(client, state, [], active_minutes=0, now=T0 + DAY)
        self.assertEqual(sorted(rings), [("D1", 252, True, False), ("D2", 3, True, False)],
                         "one line each: D1's 250 messages and its thread's 2 replies, D2's 3")
        self.assertEqual(slack.poll(client, state, [], active_minutes=0, now=T0 + DAY + 60), [], "nothing rings twice")
        self.assertEqual([slack.ring_line(r) for r in sorted(rings)], ["slack-new D1 252 dm", "slack-new D2 3 dm"])

    def test_the_busy_conversations_line_reaches_hq(self):
        base = time.time()
        new = {"ts": f"{base + 30:.6f}", "user": "U1"}
        fake = SlackLike({"D0ABCDEF1": _busy(150, until=base) + [new]}, dms=("D0ABCDEF1",))
        self.stub("security", "a-token")
        with mock.patch.object(slack, "Client", lambda token: fake):
            self.assertEqual(slack.tick(caps.CTX, base), [])
            lines = slack.tick(caps.CTX, base + slack.POLL_EVERY + 1)
        self.assertEqual([(line.text, line.kind) for line in lines], [("slack-new D0ABCDEF1 1 dm", "wake")])


def _node(thread_id, login, typename, resolved=False):
    return {"id": thread_id, "isResolved": resolved, "isOutdated": False, "path": "a.py", "line": 3,
            "comments": {"nodes": [{"author": {"login": login, "__typename": typename}, "body": "x", "url": "u"}]}}


class BotThreads(Case):
    """Every unresolved review thread an AI reviewer started on a PR athena follows rings, once, so
    each is replied to and resolved; several were once missed."""
    HEAD = "a" * 40

    def setUp(self):
        super().setUp()
        self.nodes = {}
        self.graphql = []

    def gh(self, pr=None):
        def answer(*args):
            if args[:2] == ("api", "graphql"):
                number = int(next(a for a in args if str(a).startswith("p="))[2:])
                self.graphql.append(number)
                start = int(next((a[2:] for a in args if str(a).startswith("c=")), 0) or 0)  # oldest first, 100 a page, as GitHub
                nodes = self.nodes.get(number, [])
                more = start + 100 < len(nodes)
                page = {"nodes": nodes[start:start + 100], "pageInfo": {"hasNextPage": more, "endCursor": str(start + 100) if more else None}}
                return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}})
            if args[:2] == ("pr", "view"):
                return json.dumps(pr) if pr else ""
            if args[:2] in (("run", "list"), ("search", "prs")):
                return "[]"
            raise prs.GhError("unexpected")
        return mock.patch.object(prs, "gh", side_effect=answer)

    def fetch(self, pr_of=None, pr=None):
        with self.gh(pr):
            found, lines = follow.fetch(caps.CTX, [], pr_of or {}, follow.known(), lambda t, w="": None)
        store.write_json(store.path("run", "follow.json"), found)
        return [(line.text, line.kind) for line in lines]

    def live(self):
        spawn_record("w1", self.tmp, self.tmp)
        return {"w1": {"number": 41, "url": "https://github.com/o/r/pull/41", "slug": "o/r", "state": "OPEN", "head": self.HEAD}}

    def test_a_live_workers_new_bot_thread_rings_and_a_resolved_or_human_one_does_not(self):
        pr_of = self.live()
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot"), _node("PRRT_human", "sam", "User"),
                          _node("PRRT_done", "copilot-pull-request-reviewer", "Bot", resolved=True)]
        told = self.fetch(pr_of)
        self.assertEqual(told, [("w1 bot threads +1 #41", "wake")])
        self.assertEqual(watch.allow_line(told[0][0]), told[0][0], "the line reaches HQ as it is")
        self.assertEqual(self.fetch(pr_of), [], "the same thread never rings twice")
        self.nodes[41].append(_node("PRRT_bot2", "github-actions", "Bot"))
        self.nodes[41].append(_node("PRRT_human2", "sam", "User"))
        self.assertEqual(self.fetch(pr_of), [("w1 bot threads +1 #41", "wake")], "only the new bot thread")
        self.assertEqual(self.fetch(pr_of), [])

    def test_a_new_bot_thread_past_the_first_100_still_rings(self):
        pr_of = self.live()
        self.nodes[41] = [_node(f"PRRT_old{n:03d}", "coderabbitai", "Bot", resolved=n % 3 == 0) for n in range(150)]
        self.assertEqual(self.fetch(pr_of), [("w1 bot threads +100 #41", "wake")], "every open one is read, past the first page")
        self.nodes[41].append(_node("PRRT_new", "coderabbitai", "Bot"))
        self.assertEqual(self.fetch(pr_of), [("w1 bot threads +1 #41", "wake")], "the newest, the 151st, rings")
        self.assertEqual(self.fetch(pr_of), [])

    def test_a_retired_workers_open_pr_rings_with_no_worker_named(self):
        store.append_line(store.path("history.jsonl"), {"ts": time.time(), "name": "old", "repo": "/x/r",
                                                       "pr": {"url": "https://github.com/o/r/pull/41", "state": "OPEN"}})
        pr = {"number": 41, "url": "https://github.com/o/r/pull/41", "state": "OPEN", "mergeable": "MERGEABLE", "baseRefName": "main",
              "headRefName": "b", "headRefOid": self.HEAD, "isDraft": False, "reviewDecision": "", "statusCheckRollup": [],
              "reviewRequests": [], "latestReviews": [], "author": {"login": "me"}}
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot"), _node("PRRT_bot2", "linear-code", "User"),
                          _node("PRRT_human", "sam", "User")]
        told = self.fetch(pr=pr)
        self.assertEqual(told, [("o/r#41 bot threads +2", "wake")])
        self.assertEqual(watch.allow_line(told[0][0]), told[0][0])
        self.assertEqual(self.fetch(pr=pr), [])

    def test_a_closed_pr_is_not_read(self):
        pr_of = self.live()
        pr_of["w1"]["state"] = "MERGED"
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot")]
        self.assertEqual(self.fetch(pr_of), [])
        self.assertEqual(self.graphql, [])

    def test_github_failing_to_say_keeps_what_was_known(self):
        pr_of = self.live()
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot")]
        self.assertEqual(len(self.fetch(pr_of)), 1)
        with mock.patch.object(prs, "threads", side_effect=prs.GhError("HTTP 502")):
            found, lines = follow.fetch(caps.CTX, [], pr_of, follow.known(), lambda t, w="": None)
        store.write_json(store.path("run", "follow.json"), found)
        self.assertEqual(lines, [])
        self.assertEqual(self.fetch(pr_of), [], "a failed read forgets nothing, so nothing rings again")

    def test_a_bot_thread_still_open_30_minutes_after_hqs_pass_rings_once_more(self):
        pr_of = self.live()
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot")]
        ledger.append("review", "w1", review={"verdict": "pass", "head": self.HEAD, "at": time.time() - 60})
        self.assertEqual(self.fetch(pr_of), [("w1 bot threads +1 #41", "wake")])
        self.assertEqual(self.fetch(pr_of), [], "not yet 30 minutes")
        ledger.append("review", "w1", review={"verdict": "pass", "head": self.HEAD, "at": time.time() - follow.BOT_REMIND - 1})
        self.assertEqual(self.fetch(pr_of), [("w1 bot threads +1 #41", "wake")])
        self.assertEqual(self.fetch(pr_of), [], "once")

    def test_no_reminder_for_a_pass_at_another_head_or_when_the_threads_are_resolved(self):
        pr_of = self.live()
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot")]
        self.fetch(pr_of)
        ledger.append("review", "w1", review={"verdict": "pass", "head": "b" * 40, "at": time.time() - follow.BOT_REMIND - 1})
        self.assertEqual(self.fetch(pr_of), [])
        ledger.append("review", "w1", review={"verdict": "pass", "head": self.HEAD, "at": time.time() - follow.BOT_REMIND - 1})
        self.nodes[41] = [_node("PRRT_bot1", "coderabbitai", "Bot", resolved=True)]
        self.assertEqual(self.fetch(pr_of), [])

    def test_two_bot_thread_lines_within_ten_minutes_both_reach_hq(self):
        dedupe = watch.Dedupe()
        first = watch.Line("w1 bot threads +1 #41", "wake", "w1", "PRRT_bot1")
        second = watch.Line("w1 bot threads +1 #41", "wake", "w1", "PRRT_bot2")
        self.assertTrue(dedupe.keep(first, 1000.0))
        self.assertTrue(dedupe.keep(second, 1060.0))
        self.assertFalse(dedupe.keep(first, 1120.0))

    def test_a_bot_comments_line_carries_its_pr_and_why(self):
        was = {"number": 41, "comments": 0, "all_comments": 1, "state": "OPEN"}
        now = {**was, "all_comments": 2}
        line = next(line for line in watch.diff({"w1": {"pr": was}}, {"w1": {"pr": now}}) if "bot comments" in line.text)
        self.assertEqual((line.text, line.kind), ("w1 bot comments #41", "quiet"))
        self.assertIn("bot threads", line.why)


class TheDocsSayIt(Case):
    def test_the_skill_the_worker_prompt_and_the_docs_name_the_line(self):
        from harness import ROOT
        skill = (ROOT / "plugin" / "skills" / "athena" / "SKILL.md").read_text()
        for words in ("`<w> bot threads +N #<n>`", "`<owner/repo>#<n> bot threads +N`", "gh-review-thread", "Never resolve a person's thread"):
            self.assertIn(words, skill)
        self.assertNotIn("review comments by bots or the PR's author alone", skill)
        worker = (ROOT / "prompts" / "worker.md").read_text()
        for words in ("gh-review-thread reply", "gh-review-thread resolve", "re-review"):
            self.assertIn(words, worker)
        docs = (ROOT / "docs" / "watch.md").read_text()
        for words in ("bot threads +N #<n>", "bot comments #<n>", "newest first"):
            self.assertIn(words, docs)
