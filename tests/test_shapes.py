"""Files shared with the old athena, held to what it reads (its code at 4e3900d: each comment names a reader there, relative to athena_lib/).
Each file here is made by this build's real writer, and a wrong type fails: the old Python crashes on most of these, and the old dashboard
reads a wrong type as empty. Additions are fine, so unknown fields pass.
"""
import json
import time
from unittest import mock

from harness import Case, spawn_record, store

from athena_rex.caps import doctor, hooks, hq, install, items, profiles, prs, retire, spawn, watch
from athena_rex.kernel import agents, caps, config, ledger

NUM = (int, float)
NONE = type(None)
STR = (str,)
OR_NONE = lambda *kinds: (*kinds, NONE)  # noqa: E731

GOLDEN = {
    "ledger": {  # ledger.py:66-104, status.py:14-53, cli.py:88-161, watch.py:468
        "required": {"name": STR, "event": STR},
        "fields": {"ts": NUM, "repo": STR, "branch": STR, "path": OR_NONE(str), "linear": OR_NONE(str), "title": OR_NONE(str), "model": STR, "effort": STR,
                   "claude_args": (list,), "retiring": OR_NONE(*NUM), "cleaned": (bool,), "cleanup_error": OR_NONE(str), "review": OR_NONE(dict),
                   "goal_pending": OR_NONE(str), "prompt": OR_NONE(str), "goal_send": STR, "pending_reason": OR_NONE(str), "ultracode": (bool,)},
    },
    "worker": {  # status.py:17-30, doctor.py:351-390, hookstatus.py:179-204, watch.py:516-573
        "required": {"state": STR, "ts": NUM},
        "fields": {"summary": STR, "session_id": OR_NONE(str), "report": OR_NONE(dict), "last_report": OR_NONE(dict), "event": STR, "source": OR_NONE(str),
                   "resumed_from": OR_NONE(str), "stop_blocks": NUM, "stop_block_ts": OR_NONE(*NUM)},
    },
    "report": {"required": {}, "fields": {"status": STR, "pr": STR, "head": STR, "verify": STR, "decisions": (list,), "concerns": (list,)}},  # watch.py:573, dashstate.py:49
    "items": {"required": {"next": (int,), "items": (list,)}, "fields": {}},  # asks.py:125-141: a float `next` falls back to 1 and reuses ids
    "item": {  # asks.py:191-282, cli.py:253-260, handoff.py:96-104, dash/items.go:53-126
        "required": {"id": STR, "text": STR},
        "fields": {"kind": STR, "why": STR, "pr": STR, "body": STR, "words": STR, "name": STR, "next": STR, "created": NUM, "updated": NUM, "due": NUM, "resolved": NUM, "resolved_note": STR,
                   "rec": STR, "auto": (bool,), "stakes": (list,), "options": (list,), "log": (list,), "snooze": (dict,), "applied": (dict,), "workers": (list,), "links": (list,), "requests": (list,)},
    },
    "snooze": {"required": {"until": NUM, "at": NUM}, "fields": {}},  # cli.py:254 `i["snooze"].get("until")`, items.go:111
    "option": {"required": {"id": STR, "label": STR}, "fields": {"do": STR}},  # asks.py:376, state.go:589
    "log": {"required": {"ts": NUM, "text": STR}, "fields": {}},  # items.go:119
    "action": {"required": {"seq": (int,), "item": STR, "status": STR}, "fields": {"ts": NUM, "option": STR, "text": STR, "discuss": (bool,), "auto": (bool,), "rule": (bool,)}},  # asks.py:855-879, state.go:703-717
    "action_update": {"required": {"seq": (int,), "status": STR}, "fields": {"ts": NUM}},
    "request": {"required": {"id": STR, "text": STR, "ts": NUM}, "fields": {"session": OR_NONE(str)}},  # requestlog.py:84-137
    "history": {"required": {"name": STR, "ts": NUM}, "fields": {"pr": OR_NONE(dict), "repo": OR_NONE(str), "status": OR_NONE(str)}},  # watch.py:194-210, state.go:276
    "health": {"required": {"ts": NUM, "ok": (bool,), "problems": (list,)}, "fields": {"hq_alive": (bool,), "checks": (dict,), "actions": (list,)}},  # items.go:159-171, doctor.py:475, and the clamshell daemon's hq_alive
    "problem": {"required": {"check": STR}, "fields": {"detail": STR, "since": NUM}},  # doctor.py:475 reads p["check"] with no .get
    "watch": {"required": {"ts": NUM, "workers": (dict,)}, "fields": {}},  # doctor.py:269, dashstate.py:46, parseWatch
    "watch_worker": {"required": {}, "fields": {"state": STR, "detail": OR_NONE(str), "pr": OR_NONE(dict)}},
    "hq": {"required": {}, "fields": {"session": OR_NONE(str), "claude_args": (list,), "cwd": STR, "pane": OR_NONE(str), "workspace": OR_NONE(str), "routine_at": NUM}},  # hq.py:91-355, hookstatus.py:304-328
    "stack": {"required": {"stacks": (list,)}, "fields": {}},  # watch.py:109, state.go:434
    "chain": {"required": {"repo": STR, "prs": (list,)}, "fields": {}},
    "doctor": {"required": {}, "fields": {"down": (dict,), "attempts": (dict,), "continued": (dict,), "rotate_asked": NUM, "caffeinate_pid": (int,)}},  # doctor.py:69-124, 383-441
}


def wrong(kind, record) -> list:
    """What the old athena would trip on in this record."""
    if not isinstance(record, dict):
        return [f"{kind}: not an object: {record!r}"]
    spec, found = GOLDEN[kind], []
    for field, types in spec["required"].items():
        if field not in record:
            found.append(f"{kind}: no {field}")
        elif not isinstance(record[field], types) or isinstance(record[field], bool) and bool not in types:
            found.append(f"{kind}.{field}: {record[field]!r}")
    for field, types in spec["fields"].items():
        if field in record and (not isinstance(record[field], types) or isinstance(record[field], bool) and bool not in types):
            found.append(f"{kind}.{field}: {record[field]!r}")
    return found


class Checker(Case):
    def test_it_catches_what_the_old_athena_would(self):
        self.assertEqual(wrong("snooze", {"until": 1, "at": 2}), [])
        self.assertTrue(wrong("item", {"id": "a1", "text": "x", "snooze": 123}), "this build's first snooze")
        self.assertTrue(wrong("action", {"seq": 1.0, "item": "a", "status": "queued"}), "a float seq is dropped by the old Python")
        self.assertTrue(wrong("action", {"seq": 1, "item": "a"}), "with no status the old dash reads it as not queued")
        self.assertTrue(wrong("problem", {"detail": "x"}), "p['check'] has no .get there")
        self.assertTrue(wrong("ledger", {"name": "w", "event": "update", "cleaned": 0}))
        self.assertEqual(wrong("worker", {"state": "idle", "ts": 1.5, "added": "by this build"}), [])


class Shapes(Case):
    def _launched(self, name="w", **extra):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        with mock.patch.object(spawn, "_wait_started", return_value=("ready", None)), mock.patch.object(agents, "state", return_value={"state": "idle"}), \
                mock.patch.object(agents, "typing", return_value=False), mock.patch.object(agents, "wait", return_value={"state": "working"}):
            spawn.spawn(caps.CTX, repo, "t/w", name, prompt="start", fetch=False, **extra)
        return repo

    def _lines(self, name):
        return list(store.read_lines(store.path(name)))

    def test_the_ledger_through_a_whole_life(self):
        self._launched()
        hooks.apply({"hook_event_name": "SessionStart", "session_id": "c"}, "w")
        ledger.append("review", "w", review={"verdict": "pass", "head": "abc", "at": 1})
        retire.retire(caps.CTX, "w", force=True)
        lines = self._lines("ledger.jsonl")
        self.assertGreater(len(lines), 4)
        for line in lines:
            self.assertEqual(wrong("ledger", line), [])
        seen = set()
        for line in lines:  # ledger.py:85 raises KeyError on an update of a name with no line before it
            if line["event"] in ("spawn", "failed", "retire"):
                seen.add(line["name"])
            self.assertIn(line["name"], seen, line)
        self.assertEqual(ledger.get("w")["goal_send"], "deliver", "status.py:29 shows `prompt` only with this")

    def test_a_goal_in_flight_reads_as_the_old_athena_shows_it(self):
        fields = ledger.goal_fields("begin", "g")
        self.assertEqual((fields["prompt"], fields["goal_send"]), ("sending", "deliver"))

    def test_status_files(self):
        spawn_record("w", self.tmp, self.tmp)
        for event in ({"hook_event_name": "SessionStart", "session_id": "c", "source": "startup"},
                      {"hook_event_name": "UserPromptSubmit", "session_id": "c"},
                      {"hook_event_name": "Stop", "session_id": "c", "cwd": str(self.tmp),
                       "last_assistant_message": 'x\nATHENA-REPORT {"status":"BLOCKED","pr":"","head":"","verify":"none","decisions":[],"concerns":["q"]}'}):
            hooks.apply(event, "w")
            data = store.read_json(store.path("workers", "w.json"))
            self.assertEqual(wrong("worker", data), [])
        self.assertEqual(wrong("report", data["report"]), [])

    def test_items_through_every_move(self):
        now = time.time()
        items.add("one", options=["yes:reply", "no"], body="b", rec="yes", auto=True, workers=["w"], links=["http://x"], requests=["r1"], now=now)
        items.add("two", now=now)
        items.edit("a2", text="two again", why="y", next="you")
        items.snooze("a1", "1d", now=now)
        items.resolve("a2", "done", now=now)
        data = store.read_json(store.path("needs-you.json"))
        self.assertEqual(wrong("items", data), [])
        for item in data["items"]:
            self.assertEqual(wrong("item", item), [])
            for entry in item.get("log", []):
                self.assertEqual(wrong("log", entry), [])
            for option in item.get("options", []):
                self.assertEqual(wrong("option", option), [])
            if "snooze" in item:
                self.assertEqual(wrong("snooze", item["snooze"]), [])

    def test_answers_on_the_queue(self):
        items.add("one", options=["yes"], now=time.time())
        first = items.answer("a1", option="yes")
        items.undo(first["seq"])
        items.answer("a1", text="words")
        items.resolve("a1")
        lines = self._lines("actions.jsonl")
        self.assertEqual(wrong("action", lines[0]), [])
        for line in lines[1:]:
            self.assertEqual(wrong("action" if "item" in line else "action_update", line), [])

    def test_the_request_log(self):
        items.log_request("do the thing", "c")
        self.assertEqual(wrong("request", self._lines("requests.jsonl")[0]), [])

    def test_history(self):
        self._launched()
        with mock.patch.object(prs, "summary", return_value={"url": "https://github.com/o/r/pull/1", "state": "OPEN"}):
            retire.retire(caps.CTX, "w", force=True)
        line = self._lines("history.jsonl")[-1]
        self.assertEqual(wrong("history", line), [])
        self.assertTrue(line["pr"]["url"], "the old dash treats a PR with no url as none")

    def test_health_keeps_what_the_clamshell_daemon_reads(self):
        data = doctor.run(caps.CTX, fix=False)
        self.assertEqual(wrong("health", data), [])
        for problem in data["problems"]:
            self.assertEqual(wrong("problem", problem), [])
        self.assertIsInstance(store.read_json(store.path("health.json"))["hq_alive"], bool)

    def test_the_watch_snapshot(self):
        watch.save_snapshot({"w": {"state": "working", "detail": None, "pr": None, "report": None, "last_report": None, "mergeable": None, "quiet_s": None}}, time.time())
        data = store.read_json(store.path("watch.json"))
        self.assertEqual(wrong("watch", data), [])
        self.assertEqual(wrong("watch_worker", data["workers"]["w"]), [])

    def test_hqs_record(self):
        hq._save({"session": "c", "claude_args": ["--name", "athena"], "cwd": "/x", "space": "s", "block": "b", "backend": "fake"})
        self.assertEqual(wrong("hq", store.read_json(store.path("hq.json"))), [])

    def test_doctor_keeps_the_old_keys_and_adds_under_its_own(self):
        store.write_json(store.path("doctor.json"), {"down": {"x": 1.0}, "attempts": {"x": [1.0]}, "continued": {}, "rotate_asked": 5.0})
        doctor.run(caps.CTX, fix=False)
        data = store.read_json(store.path("doctor.json"))
        self.assertEqual(wrong("doctor", data), [])
        self.assertEqual(data["down"], {"x": 1.0})
        self.assertIn("rex", data)

    def test_config_keeps_every_old_key(self):
        old = {"projects_dir": "~/code", "worktree_root": None, "human_merge_repos": ["a"], "reapproval_repos": [], "model": "opus", "effort": "high", "slack_channels": ["c"],
               "slack_allow_bots": [], "slack_ext_channels": False, "slack_dm_active_minutes": 15, "open_prs_channel": None, "ci_repos": [], "watch_main_moved": "conflicts",
               "hq_update_interval_hours": 12, "hq_allow": ["Bash(x:*)"], "version": 1}
        (self.state / "config.json").write_text(json.dumps(old))
        config.reset()
        install.set_real_world()
        profiles.grant("worker", "Bash(y:*)")
        data = json.loads((self.state / "config.json").read_text())
        self.assertEqual({k: data[k] for k in old}, old)
        self.assertEqual(sorted(set(data) - set(old)), ["rex"])

    def test_stacks(self):
        prs.set_stack("o/r", ["1", 2])
        data = store.read_json(store.path("stack.json"))
        self.assertEqual(wrong("stack", data), [])
        self.assertEqual(wrong("chain", data["stacks"][0]), [])
        self.assertTrue(all(isinstance(n, int) for n in data["stacks"][0]["prs"]), "state.go:434 drops anything but a number")
