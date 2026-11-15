"""The revive module's tests, run by scripts/test.sh with its harness: an isolated HOME and state dir, the fake backend, the world
door shut. Each test loads the shipped module (athena/modules/revive) from the config list. Rex's instance and its panes'
foreground processes come from a FakeRex handed in place of the module's adapter; the panes, their labels and what is typed
into them are the fake backend's. Launch scripts are written by athena's own agents.launcher with spawn's and hq's own flags,
so the flags asserted are the ones athena really launches with.
"""
import argparse
import contextlib
import io
import json
import os
import shlex
import time
import shutil
from pathlib import Path

from harness import Case

from athena.backends import fake
from athena.kernel import agents, caps, config, ledger, modules, store
from athena.kernel.backend import BackendError

NOW = 1_800_000_000.0
STARTED = "2027-01-15T08:00:00.123456Z"  # NOW, as Rex writes it
IDS = {"athena": "00000000-0000-4000-8000-0000000000a0", "w1": "00000000-0000-4000-8000-0000000000a1",
       "w2": "00000000-0000-4000-8000-0000000000a2", "w3": "00000000-0000-4000-8000-0000000000a3"}
OLD_HQ = "00000000-0000-4000-8000-00000000000f"


class FakeRex:
    def __init__(self, revive):
        self.revive = revive
        self.now = None
        self.foregrounds = {}
        self.asked = []

    def instance(self, ctx):
        self.asked.append("instance")
        return self.now

    def blocks(self, ctx, space):
        self.asked.append(("blocks", space))
        found = fake._load()["spaces"].get(space)
        if found is None:
            raise BackendError("not_found", space)
        return {block: b.get("label") for block, b in found["blocks"].items()}

    def foreground(self, ctx, place):
        self.asked.append(("foreground", place.block))
        found = self.foregrounds.get(place.block, ("zsh", "-zsh"))
        if isinstance(found, Exception):
            raise found
        return None if found is None else self.revive.plan.Process(*found)


class ReviveCase(Case):
    def setUp(self):
        super().setUp()
        self.write_config(rex={"modules": ["revive"]})
        self.revive = caps.get("revive")
        self.assertIsNotNone(self.revive, modules.load("revive"))
        self.assertTrue(self.revive.PUBLIC)
        self.rex = self.revive.REX = FakeRex(self.revive)
        self.hq = caps.get("hq")
        self.spawn = caps.get("spawn")
        self.backend = caps.CTX.backend
        self.places = {}
        self.originals = {}
        self.pids = {}
        self.pace = self.revive.PACE_LIMIT + 1

    # --- a fleet as Rex restored it: blocks kept, each at an idle shell, no Claude running ---

    def restart(self, instance="rex-2", started=STARTED):
        self.rex.now = self.revive.Instance(instance, started)

    def add_hq(self, session=IDS["athena"]):
        cwd = self.tmp / "projects"
        cwd.mkdir(exist_ok=True)
        place = self.backend.create("athena", cwd, ["/bin/sh", "athena.sh"], block_label="athena", window_label="athena")
        args = self.hq.claude_args()
        agents.launcher("athena", cwd, ["--resume", OLD_HQ, *args], {"ATHENA_ROLE": "hq"})
        store.write_json(store.path("hq.json"), {"backend": "fake", "space": place.space, "window": place.window,
                                                 "block": place.block, "cwd": str(cwd), "claude_args": args, "session": OLD_HQ})
        if session:
            store.write_json(store.path("hq-start.json"), {"session_id": session})
        self.places["athena"] = place
        self.originals["athena"] = args
        return place

    def add_worker(self, name, session=None, space=None, **fields):
        path = self.tmp / "worktrees" / name
        path.mkdir(parents=True, exist_ok=True)
        if space:
            place = self.backend.new_window(space, path, ["/bin/sh", f"{name}.sh"], window_label=name, block_label=name)
        else:
            place = self.backend.create("repo", path, ["/bin/sh", f"{name}.sh"], block_label=name, window_label=name)
        settings = self.spawn.settings_for(name, self.tmp / "brief.md")
        args = self.spawn.claude_args(name, "opus", "high", settings)
        agents.launcher(name, path, args, self.spawn.claude_env(name, path))
        ledger.append("spawn", name, repo=str(path), branch=f"t/{name}", path=str(path), backend="fake", space=place.space,
                      window=place.window, block=place.block, claude_args=args, **fields)
        store.write_json(store.path("workers", f"{name}.json"), {"name": name, "state": "idle", "session_id": session or IDS[name]})
        self.places[name] = place
        self.originals[name] = args
        return place

    def fleet(self):
        self.add_hq()
        first = self.add_worker("w1")
        self.add_worker("w2", space=first.space)

    # --- what happened in the panes ---

    def typed(self) -> list:
        return [(block, text) for block, text in fake._load()["typed"]]

    def typed_names(self) -> list:
        """Whose panes had a relaunch typed, in order."""
        names = {place.block: name for name, place in self.places.items()}
        return [names[block] for block, text in self.typed() if text.startswith("exec /bin/sh ")]

    def exec_words(self, name) -> list:
        line = next(l for l in plan_script(name).read_text().splitlines() if l.startswith("exec "))
        return shlex.split(line[len("exec "):])

    def claude_running(self, name):
        """`name`'s Claude runs, as ps tells it: its pid on record, `--name <name>` on its command line. Any number at once."""
        self.pids[name] = 4240 + len(self.pids)
        agents.pid_file(name).parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file(name).write_text(str(self.pids[name]))
        stub = self.stubs / "ps-running"
        stub.write_text("#!/bin/sh\n" + "".join(f'echo "{pid} S claude --name {n} --model opus"\n' for n, pid in self.pids.items()))
        stub.chmod(0o755)
        os.environ["ATHENA_PS"] = str(stub)

    def show(self, name, state="idle", screen="❯ \n"):
        """What `name`'s Claude reports about itself, and its screen."""
        data = fake._load()
        place = self.places[name]
        data["spaces"][place.space]["blocks"][place.block].update(status={"state": state}, screen=screen)
        fake._save(data)

    def come_up(self, name, state="idle", screen="❯ \n"):
        self.claude_running(name)
        self.show(name, state, screen)

    def adopt(self, instance="rex-1"):
        self.rex.now = self.revive.Instance(instance, STARTED)
        self.assertEqual(self.revive.step(caps.CTX, NOW), [])
        self.assertEqual(self.revive.load().how.value, "adopted")

    def tick(self, now=NOW + 300):
        return self.revive.step(caps.CTX, now)

    def launch_all(self, start=NOW + 300) -> float:
        """A restart's three panes typed one by one, each after the last's pace ran out with no Claude up. The last tick's time."""
        now = start
        for at in range(3):
            now = start + at * self.pace
            self.tick(now)
        self.assertEqual(self.typed_names(), ["athena", "w1", "w2"])
        return now

    def outcomes(self) -> dict:
        return {o.name: o for o in self.revive.load().panes}

    def states(self) -> dict:
        return {name: o.state and o.state.value for name, o in self.outcomes().items()}


def plan_script(name) -> Path:
    return store.path("run", f"{name}.sh")


def exec_line(name) -> str:
    return f"exec /bin/sh {shlex.quote(str(plan_script(name)))}"


class RestartTests(ReviveCase):
    def test_a_new_instance_brings_back_hq_and_two_workers_on_their_conversations_with_their_exact_flags(self):
        self.fleet()
        self.adopt()
        before = {name: plan_script(name).read_text() for name in ("athena", "w1", "w2")}
        for name in before:
            agents.pid_file(name).write_text("999999")  # last boot's pid, stale now
        self.restart()
        lines = self.tick(NOW + 300)
        self.assertEqual([line.kind for line in lines], ["routine"])
        self.assertIn("reviving athena, w1, w2, one at a time", lines[0].text)
        self.assertEqual(self.typed_names(), ["athena"], "HQ first, and alone")
        self.assertFalse(agents.pid_file("athena").exists(), "the stale pid is gone: only the relaunch's own is read")
        self.assertTrue(agents.pid_file("w1").exists(), "w1 is untouched until its turn")
        self.come_up("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.typed_names(), ["athena", "w1"], "w1's turn once HQ is up")
        self.come_up("w1")
        self.tick(NOW + 320)
        self.assertEqual(self.typed_names(), ["athena", "w1", "w2"])
        self.assertFalse(agents.pid_file("w2").exists())
        relaunches = [(block, text) for block, text in self.typed() if text.startswith("exec ")]
        self.assertEqual(relaunches, [(self.places[name].block, exec_line(name)) for name in ("athena", "w1", "w2")])
        pressed = fake._load()["pressed"]
        cancels = [at for at, keys in enumerate(pressed) if keys[1:] == ["ctrl+c"]]
        self.assertEqual([pressed[at][0] for at in cancels], [self.places[name].block for name in ("athena", "w1", "w2")],
                         "pending input is cancelled first, in each pane in turn")
        self.assertEqual([pressed[at + 1] for at in cancels], [[pressed[at][0], "enter"] for at in cancels], "then the line sent")
        sessions = {"athena": IDS["athena"], "w1": IDS["w1"], "w2": IDS["w2"]}
        for name, session in sessions.items():
            words = self.exec_words(name)
            at = words.index("--resume")
            self.assertEqual(words[at + 1], session)
            self.assertEqual(words[at + 2:], self.originals[name], f"{name}: its flags are exactly its launch's")
            self.assertEqual(words.count("--resume"), 1)
            old, new = before[name].splitlines(), plan_script(name).read_text().splitlines()
            self.assertEqual([l for l in old if not l.startswith("exec ")], [l for l in new if not l.startswith("exec ")],
                             "the pid line and the cd are kept byte for byte")
        self.assertIn("ATHENA_ROLE=hq", self.exec_words("athena"))
        self.assertIn("ATHENA_WORKER=w1", self.exec_words("w1"))
        self.assertEqual(store.read_json(store.path("hq.json"))["session"], IDS["athena"], "hq.json takes the conversation it resumed")
        self.assertEqual(store.read_json(store.path("hq.json"))["block"], self.places["athena"].block)
        handled = self.revive.load()
        self.assertEqual((handled.how.value, handled.instance, handled.started_at), ("revived", "rex-2", STARTED))
        self.assertEqual(self.states(), {"athena": "prompted", "w1": "running", "w2": "typed"})
        self.assertEqual({n: o.session for n, o in self.outcomes().items()}, sessions)
        self.assertEqual(self.outcomes()["athena"].prompt, self.hq.ROUTINE)
        self.assertIsNone(self.outcomes()["w1"].prompt)

    def test_the_same_instance_seen_again_does_nothing(self):
        self.fleet()
        self.adopt()
        self.restart()
        for at, name in enumerate(("athena", "w1", "w2")):
            self.tick(NOW + 300 + at * 10)
            self.come_up(name)
        self.tick(NOW + 330)
        self.assertEqual(self.states(), {"athena": "prompted", "w1": "running", "w2": "running"})
        record, typed = store.path("revive.json").read_text(), self.typed()
        scripts = {n: plan_script(n).read_text() for n in ("athena", "w1", "w2")}
        asked = len(self.rex.asked)
        self.assertEqual(self.tick(NOW + 340), [])
        self.assertEqual(self.typed(), typed)
        self.assertEqual(store.path("revive.json").read_text(), record)
        self.assertEqual({n: plan_script(n).read_text() for n in scripts}, scripts)
        self.assertEqual(self.rex.asked[asked:], ["instance"], "only the instance is asked, no pane is looked at")

    def test_an_adopted_instance_is_never_acted_on(self):
        self.fleet()
        self.adopt("rex-1")
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.typed(), [])

    def test_the_first_look_adopts_rather_than_acts(self):
        self.fleet()
        self.restart()
        self.assertFalse(store.path("revive.json").exists())
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.typed(), [])
        self.assertEqual(self.revive.load().how.value, "adopted")

    def test_an_unreadable_record_adopts_rather_than_acts(self):
        self.fleet()
        store.path("revive.json").write_text('{"how": "revived", "instance": "rex-1", "panes": "no"}')
        self.restart()
        self.tick()
        self.assertEqual(self.typed(), [])

    def test_turning_it_on_adopts_the_running_instance(self):
        self.fleet()
        self.restart("rex-7")
        self.revive.start(caps.CTX)
        self.assertEqual((self.revive.load().how.value, self.revive.load().instance), ("adopted", "rex-7"))
        self.tick()
        self.assertEqual(self.typed(), [])

    def test_turning_it_on_with_rex_unreadable_adopts_the_next_one_seen(self):
        self.rex.now = None
        self.revive.start(caps.CTX)
        self.assertEqual(self.revive.load().how.value, "adopt-next")
        self.assertEqual(self.tick(), [])

    def test_no_rex_to_read_does_nothing(self):
        self.fleet()
        self.adopt()
        self.rex.now = None
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.revive.load().instance, "rex-1")

    def test_a_space_in_the_state_path_is_quoted_in_what_is_typed(self):
        """The real state dir is ~/Library/Application Support/athena: the typed line must name the script as one word."""
        spaced = self.tmp / "Library" / "Application Support" / "athena"
        spaced.mkdir(parents=True)
        (spaced / ".athena-scratch").write_text("")
        shutil.copy(self.state / "config.json", spaced / "config.json")
        os.environ["ATHENA_STATE_DIR"] = str(spaced)
        config.reset()
        self.add_hq()
        self.adopt()
        self.restart()
        self.tick()
        script_path = spaced / "run" / "athena.sh"
        self.assertEqual(self.typed(), [(self.places["athena"].block, f"exec /bin/sh '{script_path}'")])
        self.assertEqual(shlex.split(self.typed()[0][1]), ["exec", "/bin/sh", str(script_path)])
        self.assertIn("--resume", script_path.read_text())


class PaceTests(ReviveCase):
    def test_hq_goes_first_and_each_next_pane_waits_for_the_last_to_come_up_or_its_pace(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.assertEqual(self.typed_names(), ["athena"])
        self.assertEqual(self.states(), {"athena": "typed", "w1": "queued", "w2": "queued"})
        self.claude_running("athena")  # running, not drawn yet: not up
        self.tick(NOW + 300 + self.revive.PACE_LIMIT - 1)
        self.assertEqual(self.typed_names(), ["athena"], "the queue waits while HQ comes up")
        self.tick(NOW + 300 + self.pace)
        self.assertEqual(self.typed_names(), ["athena", "w1"], "past its pace, the next goes in anyway")
        self.assertEqual(self.states(), {"athena": "typed", "w1": "typed", "w2": "queued"}, "HQ is still followed")
        self.show("athena")
        self.tick(NOW + 300 + self.pace + 10)
        self.assertEqual(self.states()["athena"], "prompted")
        self.assertEqual(self.typed_names(), ["athena", "w1"], "w1 now holds the queue")

    def test_a_pane_no_longer_at_a_shell_by_its_turn_is_left_and_said_and_the_next_goes_on(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.rex.foregrounds[self.places["w1"].block] = ("vim", "vim")  # someone started something there meanwhile
        self.come_up("athena")
        lines = self.tick(NOW + 310)
        self.assertEqual(self.typed_names(), ["athena", "w2"], "nothing typed into w1")
        self.assertEqual((self.states()["w1"], self.outcomes()["w1"].detail), ("left", "by its turn it ran vim"))
        self.assertIn("revive: w1 by its turn it ran vim", [line.text for line in lines])
        self.assertEqual([line.kind for line in lines if line.text.startswith("revive: w1")], ["wake"])
        self.assertFalse(self.revive.checks(caps.CTX)[0].ok)

    def test_a_worker_hq_began_to_retire_before_its_turn_is_stood_down_quietly(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        ledger.append("update", "w1", retiring=time.time())  # HQ, back first, retires w1 while it waits its turn
        self.come_up("athena")
        lines = self.tick(NOW + 310)
        self.assertEqual((self.states()["w1"], self.outcomes()["w1"].detail), ("stood-down", "by its turn it was retiring"))
        self.assertEqual(self.typed_names(), ["athena", "w2"], "nothing typed into w1")
        self.assertFalse([line for line in lines if line.text.startswith("revive: w1")], "HQ's own retire isn't trouble")
        self.assertTrue(self.revive.checks(caps.CTX)[0].ok)

    def test_a_tick_that_dies_while_typing_leaves_the_pane_sending_and_it_is_never_typed_again(self):
        self.fleet()
        self.adopt()
        self.restart()
        real = self.backend.type
        self.backend.type = lambda place, text: (_ for _ in ()).throw(RuntimeError("the watch was killed here"))
        try:
            with self.assertRaises(RuntimeError):
                self.tick(NOW + 300)
        finally:
            self.backend.type = real
        self.assertEqual(json.loads(store.path("revive.json").read_text())["panes"][0]["state"], "sending",
                         "on disk before the keystrokes")
        self.tick(NOW + 310)
        self.tick(NOW + 300 + self.pace)
        self.assertEqual(self.typed_names(), ["w1"], "athena is never typed again; past its pace, w1's turn")
        self.come_up("athena")
        self.tick(NOW + 300 + self.pace + 10)
        self.assertEqual(self.states()["athena"], "prompted", "followed all the same")

    def test_rex_unable_to_say_by_its_turn_leaves_the_pane(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.rex.foregrounds[self.places["w1"].block] = BackendError("timeout", "slow")
        self.come_up("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.states()["w1"], "left")
        self.assertIn("Rex couldn't say", self.outcomes()["w1"].detail)

    def test_a_pane_whose_typing_a_dead_tick_left_unsure_is_never_typed_again(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        handled = self.revive.load()
        handled.panes[1].state = self.revive.State.SENDING  # w1: a tick died between this and its keystrokes
        self.revive.save(handled)
        self.come_up("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.typed_names(), ["athena"], "w1 is followed, not typed again, and holds the queue")
        self.come_up("w1")
        self.tick(NOW + 320)
        self.assertEqual(self.states()["w1"], "running")
        self.assertEqual(self.typed_names(), ["athena", "w2"])


class SkipTests(ReviveCase):
    def reasons(self) -> dict:
        return {o.name: (o.reason and o.reason.value, o.detail) for o in self.revive.load().panes}

    def test_each_pane_not_to_revive_is_skipped_with_its_reason(self):
        self.add_hq()
        first = self.add_worker("w1")
        self.add_worker("w2", space=first.space)
        self.add_worker("w3", space=first.space)
        self.add_worker("busy", session="00000000-0000-4000-8000-0000000000b1", space=first.space)
        self.add_worker("old", session="00000000-0000-4000-8000-0000000000b2", space=first.space)
        self.add_worker("closed", session="00000000-0000-4000-8000-0000000000b3", space=first.space)
        self.add_worker("leaving", session="00000000-0000-4000-8000-0000000000b4", space=first.space)
        self.add_worker("gone", session="00000000-0000-4000-8000-0000000000b5", space=first.space)
        self.add_worker("strange", session="00000000-0000-4000-8000-0000000000b6", space=first.space)
        self.add_worker("stale", session="00000000-0000-4000-8000-0000000000b7", space=first.space)
        self.rex.foregrounds[self.places["busy"].block] = ("vim", "vim")
        ledger.append("retire", "old")
        ledger.append("update", "closed", closed_by_human="unsaved work")
        ledger.append("update", "leaving", retiring=time.time())
        ledger.append("update", "stale", retiring=time.time() - ledger.RETIRING_EXPIRES - 600)  # a retire killed by the reboot
        store.write_json(store.path("workers", "w2.json"), {"name": "w2", "state": "idle"})  # no conversation on record
        self.backend.close_block(self.places["gone"])
        self.backend.label(self.places["strange"], block="someone-else")
        self.claude_running("w3")
        self.adopt()
        self.restart()
        lines = self.tick()
        self.assertEqual(self.reasons(), {
            "athena": (None, ""), "w1": (None, ""),
            "w2": ("no-conversation", ""), "w3": ("claude-running", ""), "busy": ("busy", "vim"), "old": ("retired", ""),
            "closed": ("closed-by-human", "unsaved work"), "leaving": ("retiring", ""), "gone": ("pane-gone", ""),
            "strange": ("not-its-pane", "someone-else"), "stale": ("retire-unfinished", "")})
        self.assertEqual(self.typed_names(), ["athena"])
        self.come_up("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.typed_names(), ["athena", "w1"], "only the two to bring back are ever typed into")
        self.assertIn("skipped", lines[0].text)
        self.assertIn("busy (its pane runs something else)", lines[0].text)
        self.assertIn("stale (a retire began and never finished)", lines[0].text)
        self.assertEqual(lines[0].kind, "wake", "a retire the restart cut short is said as trouble")
        self.assertIn("stale: a retire began and never finished", self.revive.checks(caps.CTX)[0].detail)

    def test_a_retire_mark_of_any_age_keeps_a_worker_down(self):
        self.add_worker("w1", retiring=time.time() - 10 * ledger.RETIRING_EXPIRES)
        self.assertFalse(ledger.is_retiring(ledger.get("w1")), "past its expiry, the ledger no longer reads it as retiring")
        self.assertEqual([d.reason.value for d in self.revive.decisions(caps.CTX)], ["retire-unfinished"])
        ledger.append("update", "w1", retiring=None)  # a retire that failed takes its mark off
        self.assertEqual([d.reason for d in self.revive.decisions(caps.CTX)], [None])

    def test_a_retired_worker_whose_pane_is_gone_is_not_listed(self):
        self.add_worker("w1")
        ledger.append("retire", "w1")
        self.backend.close_block(self.places["w1"])
        self.assertEqual(self.revive.decisions(caps.CTX), [])

    def test_a_worker_from_the_old_athena_is_not_athenas_pane_here(self):
        ledger.append("spawn", "legacy", repo="/r", branch="b", path="/r")
        self.assertEqual(self.revive.decisions(caps.CTX), [])

    def test_a_missing_or_foreign_launch_script_is_skipped(self):
        self.add_worker("w1")
        self.add_worker("w2", space=self.places["w1"].space)
        plan_script("w1").unlink()
        plan_script("w2").write_text("#!/bin/sh\necho $$ > /x.pid\nexec claude --name other\n")
        found = {d.name: d.reason.value for d in self.revive.decisions(caps.CTX)}
        self.assertEqual(found, {"w1": "no-script", "w2": "not-its-script"})

    def test_a_launch_script_spoiled_before_its_turn_fails_and_is_said(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        plan_script("w1").write_text("#!/bin/sh\necho $$ > /x.pid\nexec claude --name other\n")
        self.come_up("athena")
        lines = self.tick(NOW + 310)
        self.assertEqual(self.states()["w1"], "failed")
        self.assertIn("doesn't launch --name w1", self.outcomes()["w1"].detail)
        self.assertEqual(self.typed_names(), ["athena", "w2"])
        self.assertEqual([line.kind for line in lines if line.text.startswith("revive: w1")], ["wake"])

    def test_rex_unable_to_say_is_waited_for_while_rex_is_new(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.rex.foregrounds[self.places["athena"].block] = BackendError("timeout", "slow")
        self.assertEqual(self.tick(NOW + 30), [])
        self.assertEqual(self.typed(), [])
        del self.rex.foregrounds[self.places["athena"].block]
        self.tick(NOW + 50)
        self.assertEqual(self.typed_names(), ["athena"])

    def test_rex_unable_to_say_past_the_settle_limit_is_skipped_and_said_as_trouble(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.rex.foregrounds[self.places["athena"].block] = BackendError("timeout", "slow")
        lines = self.tick(NOW + self.revive.SETTLE_LIMIT + 1)
        self.assertEqual((self.outcomes()["athena"].reason.value, self.outcomes()["athena"].detail), ("unknown", "timeout: slow"))
        self.assertEqual([line.kind for line in lines], ["wake"])
        self.assertFalse(self.revive.checks(caps.CTX)[0].ok)
        self.assertIn("athena: timeout: slow", self.revive.checks(caps.CTX)[0].detail)
        self.assertEqual(self.revive.status(caps.CTX), (False, "didn't come back: athena"))

    def test_an_answer_from_rex_that_isnt_an_object_is_rex_unable_to_say(self):
        answer = self.revive._answer
        self.assertEqual(answer(lambda: {"foreground": None}), {"foreground": None})
        for garbled in (lambda: json.loads("{torn"), lambda: ["a", "list"], lambda: None):
            with self.assertRaises(BackendError):
                answer(garbled)

    def test_a_failed_worker_is_not_live(self):
        self.add_worker("w1")
        ledger.append("failed", "w1")
        self.assertEqual([(d.reason.value, d.detail) for d in self.revive.decisions(caps.CTX)], [("not-live", "failed")])

    def test_a_waiting_goal_is_not_typed_into_the_bare_shell(self):
        """spawn's tick delivers a waiting goal; after a restart that pane is a shell, so agents.send refuses it: only revive types."""
        self.add_worker("w1", goal_pending="/goal do it")
        self.adopt()
        self.restart()
        self.assertEqual(self.spawn.deliver_pending(caps.CTX, "w1")["outcome"], "waiting")
        self.tick()
        self.assertEqual(self.typed(), [(self.places["w1"].block, exec_line("w1"))])


    def test_hq_json_not_written_is_said_and_hq_comes_back_all_the_same(self):
        self.fleet()
        self.adopt()
        self.restart()
        real = self.hq._save
        self.hq._save = lambda record: (_ for _ in ()).throw(OSError("disk full"))
        try:
            lines = self.tick(NOW + 300)
        finally:
            self.hq._save = real
        self.assertEqual(self.states()["athena"], "typed")
        self.assertIn("revive: athena hq.json not updated: disk full", [line.text for line in lines])


class SettleTests(ReviveCase):
    def test_a_pane_still_at_login_is_waited_for_while_rex_is_new(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.rex.foregrounds[self.places["w1"].block] = ("login", None)
        self.assertEqual(self.tick(NOW + 30), [])
        self.assertEqual(self.typed(), [])
        self.assertEqual(self.revive.load().instance, "rex-1", "nothing recorded while waiting")
        del self.rex.foregrounds[self.places["w1"].block]
        self.tick(NOW + 50)
        self.assertEqual(self.states(), {"athena": "typed", "w1": "queued", "w2": "queued"})

    def test_past_the_settle_limit_a_pane_at_login_is_skipped(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.rex.foregrounds[self.places["w1"].block] = None
        self.tick(NOW + self.revive.SETTLE_LIMIT + 1)
        self.assertEqual(self.outcomes()["w1"].reason.value, "starting")
        self.assertEqual(self.states(), {"athena": "typed", "w1": None, "w2": "queued"})


class FollowTests(ReviveCase):
    def test_a_worker_whose_claude_comes_up_is_running(self):
        self.fleet()
        self.adopt()
        self.restart()
        last = self.launch_all()
        self.come_up("w1")
        self.assertEqual(self.tick(last + 10), [])
        self.assertEqual(self.states(), {"athena": "typed", "w1": "running", "w2": "typed"})

    def test_a_claude_that_runs_but_has_not_drawn_itself_is_not_up_yet(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.claude_running("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.states()["athena"], "typed")

    def test_hq_is_told_its_session_start_once_it_is_ready(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.come_up("athena", state="working")
        self.tick(NOW + 310)
        self.assertEqual(self.states()["athena"], "up", "up, and busy: nothing typed to it yet")
        self.assertNotIn(self.hq.ROUTINE, [text for _, text in self.typed()])
        self.show("athena")
        self.tick(NOW + 320)
        self.assertEqual(self.states()["athena"], "prompted")
        self.assertIn((self.places["athena"].block, self.hq.ROUTINE), self.typed())
        typed = self.typed()
        self.tick(NOW + 330)
        self.assertEqual([t for t in self.typed() if t[1] == self.hq.ROUTINE], [t for t in typed if t[1] == self.hq.ROUTINE],
                         "told once")

    def test_a_worker_that_was_mid_turn_is_nudged_to_carry_on_and_an_idle_one_is_not(self):
        self.fleet()
        store.write_json(store.path("workers", "w1.json"), {"name": "w1", "state": "working", "session_id": IDS["w1"]})
        self.adopt()
        self.restart()
        rows = {r["name"]: r.get("then") for r in self.revive.dry_run(caps.CTX)["panes"]}
        self.assertEqual(rows, {"athena": "its session start", "w1": "a nudge to carry on", "w2": None})
        last = self.launch_all()
        self.assertEqual(self.outcomes()["w1"].prompt, self.revive.CONTINUE)
        self.assertIsNone(self.outcomes()["w2"].prompt)
        self.come_up("w1")
        self.tick(last + 10)
        self.assertEqual(self.states()["w1"], "prompted")
        self.assertEqual(self.typed()[-1], (self.places["w1"].block, self.revive.CONTINUE))

    def test_a_prompt_refused_until_the_limit_is_given_up_on_with_why(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.come_up("athena", screen="❯ the human's draft\n")
        self.tick(NOW + 310)
        self.assertEqual(self.states()["athena"], "up")
        self.assertNotIn(self.hq.ROUTINE, [text for _, text in self.typed()], "nothing typed over someone's text")
        self.assertIn("prompt waits: box_not_empty", self.outcomes()["athena"].detail, "the reason is kept on record")
        lines = self.tick(NOW + 310 + self.revive.FOLLOW_LIMIT + 1)
        self.assertEqual(self.states()["athena"], "gave-up")
        self.assertTrue(lines[0].text.startswith("revive: athena its Claude never took its prompt (prompt waits: box_not_empty"),
                        lines[0].text)

    def test_an_answer_from_rex_that_doesnt_parse_while_following_doesnt_stall_the_queue(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.claude_running("athena")
        real = self.backend.program_status
        self.backend.program_status = lambda place: json.loads("{torn")
        try:
            self.tick(NOW + 310)
            self.tick(NOW + 300 + self.pace)
        finally:
            self.backend.program_status = real
        self.assertEqual(self.states(), {"athena": "typed", "w1": "typed", "w2": "queued"}, "not up, and the queue moved on")
        self.assertIn("its state couldn't be read", self.outcomes()["athena"].detail, "why is kept, for a give-up to say")

    def test_a_state_nobody_could_read_waits_rather_than_counts_as_sent(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.come_up("athena", state="working")
        self.tick(NOW + 310)
        real = self.backend.program_status
        self.backend.program_status = lambda place: (_ for _ in ()).throw(BackendError("timeout", "slow"))
        try:
            self.tick(NOW + 320)
        finally:
            self.backend.program_status = real
        self.assertEqual(self.states()["athena"], "up")
        self.assertIn("its state couldn't be read", self.outcomes()["athena"].detail)
        self.show("athena")
        self.tick(NOW + 330)
        self.assertEqual((self.states()["athena"], self.outcomes()["athena"].detail), ("prompted", ""))

    def test_an_answer_send_couldnt_parse_before_typing_waits_and_the_prompt_goes_later(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.come_up("athena")
        real = self.backend.screen
        self.backend.screen = lambda place, styled=False: (_ for _ in ()).throw(ValueError("torn"))
        try:
            self.tick(NOW + 310)
        finally:
            self.backend.screen = real
        self.assertEqual(self.states()["athena"], "up", "nothing was typed: it waits")
        self.assertIn("didn't parse", self.outcomes()["athena"].detail)
        self.tick(NOW + 320)
        self.assertEqual(self.states()["athena"], "prompted")

    def test_rex_failing_while_it_types_the_prompt_is_unsure_and_said(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick(NOW + 300)
        self.come_up("athena")
        real = self.backend.type
        self.backend.type = lambda place, text: (_ for _ in ()).throw(BackendError("timeout", "slow"))
        try:
            lines = self.tick(NOW + 310)
        finally:
            self.backend.type = real
        self.assertEqual(self.states()["athena"], "prompt-unsure")
        self.assertIn("wake", [line.kind for line in lines if line.text.startswith("revive: athena")])

    def test_a_pane_whose_claude_never_comes_up_is_given_up_on_and_said(self):
        self.fleet()
        self.adopt()
        self.restart()
        last = self.launch_all()
        lines = self.tick(last + self.revive.FOLLOW_LIMIT + 1)
        self.assertEqual(set(self.states().values()), {"gave-up"})
        self.assertEqual({o.detail for o in self.outcomes().values()}, {"its Claude never came up"})
        self.assertEqual([line.kind for line in lines], ["wake"] * 3)
        self.assertFalse(self.revive.checks(caps.CTX)[0].ok)
        self.assertEqual(self.revive.status(caps.CTX)[0], False)

    def test_typing_that_rex_fails_is_unconfirmed_and_still_followed(self):
        self.fleet()
        self.adopt()
        self.restart()
        real = self.backend.type
        self.backend.type = lambda place, text: (_ for _ in ()).throw(BackendError("timeout", "slow"))
        try:
            self.tick(NOW + 300)
        finally:
            self.backend.type = real
        self.assertEqual(self.states(), {"athena": "unconfirmed", "w1": "queued", "w2": "queued"})
        self.come_up("athena")
        self.tick(NOW + 310)
        self.assertEqual(self.states()["athena"], "prompted", "its line had arrived after all")
        self.assertEqual(self.typed_names(), ["w1"], "w1's turn")


class DryRunTests(ReviveCase):
    def run_cli(self, *argv) -> str:
        parser = argparse.ArgumentParser()
        self.revive.commands()["revive"].configure(parser)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.revive.commands()["revive"].run(parser.parse_args(list(argv))), 0)
        return out.getvalue()

    def test_the_dry_run_lists_what_the_tick_would_do_and_changes_nothing(self):
        self.fleet()
        self.add_worker("old", session="00000000-0000-4000-8000-0000000000b2", space=self.places["w1"].space)
        ledger.append("retire", "old")
        self.adopt()
        self.restart()
        files = {p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        out = json.loads(self.run_cli("--json"))
        self.assertEqual({p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}, files, "nothing written")
        self.assertEqual(self.typed(), [])
        self.assertEqual(out["instance"], {"id": "rex-2", "started_at": STARTED})
        self.assertEqual(out["last"]["instance"], "rex-1")
        planned = [(r["name"], r["revive"], r.get("reason")) for r in out["panes"]]
        self.assertEqual(planned, [("athena", True, None), ("old", False, "retired"), ("w1", True, None), ("w2", True, None)])
        text = self.run_cli()
        self.assertIn("dry run, nothing typed", text)
        self.assertIn("revive  athena", text)
        self.assertIn("skip    old", text)
        self.assertIn("retired", text)
        self.assertIn("last handled: Rex rex-1 adopted", text)
        self.tick()
        self.assertEqual([(o.name, o.revived, o.reason and o.reason.value) for o in self.revive.load().panes], planned)

    def test_the_verb_and_the_module_are_listed(self):
        done = self.cli("modules", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        found = next(m for m in json.loads(done.stdout) if m["name"] == "revive")
        self.assertEqual((found["loaded"], found["public"], found["state"]), (True, True, "on"))
        self.assertEqual(found["detail"], "waiting for a Rex restart")
        self.assertIsNotNone(caps.find_command("revive"))

    def test_rex_start_times_are_read_with_any_offset_and_precision(self):
        at = self.revive.started
        Instance = self.revive.Instance
        self.assertEqual(at(Instance("i", STARTED)), NOW)
        self.assertEqual(at(Instance("i", "2027-01-15T08:00:00.123456789Z")), NOW)
        self.assertEqual(at(Instance("i", "2027-01-15T00:00:00-08:00")), NOW)
        self.assertIsNone(at(Instance("i", None)))
        self.assertIsNone(at(Instance("i", "yesterday")))

    def test_doctor_line_says_the_last_restart_handled(self):
        self.fleet()
        self.adopt()
        self.restart()
        self.tick()
        check = self.revive.checks(caps.CTX)[0]
        self.assertTrue(check.ok)
        self.assertIn("Rex restart rex-2 handled", check.detail)
        self.assertIn("athena typed, w1 queued, w2 queued", check.detail)


class WatchTests(ReviveCase):
    """The watch's own loop body, as it runs from login: the module is reached through caps.loaded(), not called by hand."""

    def loop(self, now):
        from athena.caps import watch
        _, _, told = watch.step(caps.CTX, {}, 0, watch.Dedupe(), now=now)
        return [line.text for line in told]

    def test_the_watch_loop_revives_after_a_restart_and_says_so(self):
        self.fleet()
        self.restart("rex-1")
        self.loop(NOW)  # the first look adopts
        self.restart()
        told = self.loop(NOW + 300)
        self.assertEqual(self.typed_names(), ["athena"])
        self.assertIn("Rex restarted: reviving athena w1 w2 one at a time", told, "the watch's safe() drops the commas")
        self.assertFalse([t for t in told if "watch error" in t], told)
        self.come_up("athena")
        self.loop(NOW + 320)
        self.assertEqual(self.typed_names(), ["athena", "w1"])

    def test_trouble_reaches_the_watch_as_a_line(self):
        self.fleet()
        self.restart("rex-1")
        self.loop(NOW)
        self.restart()
        for at in range(3):
            self.loop(NOW + 300 + at * self.pace)
        told = self.loop(NOW + 300 + 2 * self.pace + self.revive.FOLLOW_LIMIT + 1)
        self.assertEqual([t for t in told if t.startswith("revive:")],
                         [f"revive: {name} its Claude never came up" for name in ("athena", "w1", "w2")])
        self.assertFalse([t for t in told if "watch error" in t], told)


class ScriptTests(ReviveCase):
    def test_an_old_resume_is_replaced_in_any_spelling(self):
        script = self.revive.script
        text = "#!/bin/sh\necho $$ > /s/run/x.pid\ncd /w || exit 111\nexec env A=1 claude --resume old --name x --resume=older -m\n"
        out = script.resumed(text, "x", "new")
        self.assertEqual(out.splitlines()[:3], text.splitlines()[:3])
        self.assertEqual(out.splitlines()[3], "exec env A=1 claude --resume new --name x -m")

    def test_a_sandboxed_launch_keeps_its_sandbox(self):
        self.write_config(rex={"modules": ["revive"], "signal_sandbox": True})
        self.add_worker("w1")
        words = self.exec_words("w1")
        out = self.revive.script.resumed(plan_script("w1").read_text(), "w1", IDS["w1"])
        new = shlex.split(next(l for l in out.splitlines() if l.startswith("exec "))[5:])
        self.assertEqual(words[0], "/usr/bin/sandbox-exec")
        at = words.index("--name")
        self.assertEqual(new, [*words[:at], "--resume", IDS["w1"], *words[at:]])

    def test_what_isnt_a_launch_is_refused(self):
        script = self.revive.script
        for text in ("", "#!/bin/sh\nexec claude --name x\n", "#!/bin/sh\necho $$ > p\nexec claude --name y\n",
                     "#!/bin/sh\necho $$ > p\nexec claude 'unclosed --name x\n", "#!/bin/sh\necho $$ > p\nexec env claude\n"):
            with self.assertRaises(script.NotLaunch, msg=text):
                script.resumed(text, "x", "s")


class OnByDefault(Case):
    """Shipped with `"default": true` in its manifest: listed and on whatever rex.modules says, until turned off."""

    def listed(self):
        done = self.cli("modules", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        return [(m["name"], m["loaded"], m["public"], m["state"]) for m in json.loads(done.stdout)]

    def revive_rows(self):
        return [row for row in self.listed() if row[0] == "revive"]

    def test_every_config_shape_loads_it_once(self):
        private = self.tmp / "private-modules" / "mine"
        private.mkdir(parents=True)
        (private / "manifest.json").write_text('{"name": "mine", "title": "Mine"}')
        (private / "__init__.py").write_text("")
        self.assertFalse((self.state / "config.json").exists())
        self.assertEqual(self.revive_rows(), [("revive", True, True, "on")], "no config at all")
        for listed in ([], ["clamshell"], ["mine"], ["revive", "clamshell"], ["clamshell", "revive"]):
            self.write_config(rex={"modules": listed, "modules_dir": str(private.parent)})
            config.reset()
            self.assertEqual(self.revive_rows(), [("revive", True, True, "on")], listed)
            self.assertEqual([name for name, _ in modules.active()].count("revive"), 1, listed)
            self.assertIsNotNone(caps.get("revive"), listed)
            self.assertNotIn("module:revive", self.cli("doctor", "--json").stdout, listed)

    def test_unload_turns_it_off_until_load_turns_it_back_on(self):
        self.write_config(rex={"modules": ["clamshell"]})
        done = self.cli("modules", "unload", "revive")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "off")
        config.reset()
        self.assertEqual(config.rex("modules"), ["clamshell"], "the config list is the user's: revive was never in it")
        self.assertEqual(self.revive_rows(), [("revive", True, True, "off")], "a new process reads it off")
        self.assertIsNone(caps.get("revive"))
        self.assertEqual(self.cli("revive").returncode, 2)
        self.assertEqual(self.cli("modules", "unload", "revive").returncode, 0, "unloading it again is no error")
        done = self.cli("modules", "load", "revive")
        self.assertEqual(done.returncode, 0, done.stderr)
        config.reset()
        self.assertEqual(config.rex("modules"), ["clamshell"], "load turns it on without writing it into the list")
        self.assertEqual(self.revive_rows(), [("revive", True, True, "on")])
        self.assertIsNotNone(caps.get("revive"))

    def test_the_menu_toggle_turns_it_off_and_on(self):
        self.write_config(rex={"modules": []})
        for state in ("off", "on", "off"):
            done = self.cli("modules", "action", "revive", "toggle")
            self.assertEqual((done.returncode, json.loads(done.stdout)["state"]), (0, state), done.stderr)
            self.assertEqual(self.revive_rows(), [("revive", True, True, state)])
        config.reset()
        self.assertIsNone(caps.get("revive"))
