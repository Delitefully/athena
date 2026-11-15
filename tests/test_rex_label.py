"""REX-LABEL: a worker never renames its repo's Rex session, and the watch puts a drifted one's label back.

Rex's SessionStart hook tells every Claude to name its session. A worker's session is its repo's, shared by every worker on it,
so one that renamed it would show the whole group under its own name in Rex's sidebar.
"""
import json
import shlex
import time
from pathlib import Path
from unittest import mock

from harness import Case

from athena.caps import hooks, hq, sessions, spawn
from athena.kernel import agents, caps, ledger, store

RULE = "Never rename the Rex session"


class Spawned(Case):
    def setUp(self):
        super().setUp()
        self.write_config(worktree_root=str(self.tmp / "wt"))

    def _spawn(self, repo, name):
        spawn.spawn(caps.CTX, repo, f"t/{name}", name, prompt="hi", timeout=0.1, fetch=False)
        return ledger.get(name)


class WorkerRule(Spawned):
    def test_the_prompt_spawn_hands_a_worker_says_never_rename_its_session(self):
        self._spawn(self.repo("proj"), "a")
        words = shlex.split((agents._run_dir() / "a.sh").read_text().splitlines()[-1])
        prompt = Path(words[words.index("--append-system-prompt-file") + 1]).read_text()
        self.assertIn(RULE, prompt)
        self.assertIn("rex session rename", prompt)
        self.assertIn("SessionStart", prompt, "it says it overrides Rex's naming instruction")

    def test_a_workers_session_start_says_it_beside_rexs_own(self):
        event = {"hook_event_name": "SessionStart", "session_id": "x", "source": "startup"}
        out = json.loads(hooks.main_hook(json.dumps(event), {"ATHENA_WORKER": "a"}))
        said = out["hookSpecificOutput"]
        self.assertEqual(said["hookEventName"], "SessionStart")
        self.assertIn(RULE, said["additionalContext"])
        self.assertIn("rex session rename", said["additionalContext"])

    def test_hqs_session_start_and_a_workers_other_events_say_nothing_of_it(self):
        event = {"hook_event_name": "SessionStart", "session_id": "x", "source": "startup"}
        self.assertEqual(hooks.main_hook(json.dumps(event), {"ATHENA_ROLE": "hq"}), "")
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "x", "prompt": "go"}
        self.assertEqual(hooks.main_hook(json.dumps(prompt), {"ATHENA_WORKER": "a"}), "")


class Relabel(Spawned):
    def _labels(self):
        return {s["id"]: s["label"] for s in caps.CTX.backend.spaces()}

    def test_a_renamed_repo_session_gets_its_label_back_and_hq_is_left_alone(self):
        repo = self.repo("proj")
        a, b = self._spawn(repo, "a"), self._spawn(repo, "b")
        hq.open_hq(focus=False, routine=False)
        hq_space = hq.load()["space"]
        caps.CTX.backend.rename(a["space"], "b")
        caps.CTX.backend.rename(hq_space, "hq-renamed")
        later = time.time() + 60
        lines = sessions.tick(caps.CTX, later)
        self.assertEqual(self._labels()[a["space"]], repo.name, "the label athena made it with")
        self.assertEqual(self._labels()[hq_space], "hq-renamed", "HQ's session is not this check's")
        said = [line for line in lines if "relabelled" in line.text]
        self.assertEqual(len(said), 1, lines)
        self.assertEqual(said[0].text, f"session 'b' relabelled {repo.name}: it is the repo's, shared by a, b")
        row = next(r for r in sessions.snapshot(later)["sessions"] if r["id"] == a["space"])
        self.assertEqual(row["label"], repo.name, "the snapshot shows it as it is now")
        self.assertEqual([l for l in sessions.tick(caps.CTX, later + 60) if "relabelled" in l.text], [], "said once")

    def test_a_session_with_its_label_and_one_not_athenas_are_not_touched(self):
        repo = self.repo("proj")
        a = self._spawn(repo, "a")
        other = caps.CTX.backend.create("mine", self.tmp, ["x"])
        with mock.patch.object(type(caps.CTX.backend), "label", side_effect=AssertionError("relabelled")):
            lines = sessions.tick(caps.CTX, time.time() + 60)
        self.assertEqual(self._labels(), {a["space"]: repo.name, other.space: "mine"})
        self.assertFalse([l for l in lines if "relabelled" in l.text])


class Docs(Case):
    def test_the_architecture_says_the_rule_and_the_relabel(self):
        text = (Path(__file__).resolve().parent.parent / "ARCHITECTURE.md").read_text()
        self.assertIn("a worker never renames it (REX-LABEL)", text)
        self.assertIn("`rex session rename`", text)
        self.assertIn("puts each repo session athena made back to the label it was made with", text)


class RelabelTrouble(Spawned):
    def test_rex_failing_any_way_loses_no_orphan_line(self):
        a = self._spawn(self.repo("proj"), "a")
        ledger.append("retire", "a")
        caps.CTX.backend.rename(a["space"], "drifted")
        for n, trouble in enumerate((OSError("rex gone"), ValueError("bad json"))):
            store.path("run", "orphans-told.json").unlink(missing_ok=True)
            with mock.patch.object(type(caps.CTX.backend), "label", side_effect=trouble):
                lines = sessions.tick(caps.CTX, time.time() + 60 * (n + 1))
            self.assertEqual([l.text for l in lines], ["session left behind by a: athena sessions close-orphans"], trouble)
