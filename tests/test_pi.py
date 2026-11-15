"""Pi as a second worker agent. Claude stays the default, and its launch is byte for byte what it was before Pi."""
import json
import os
import shlex
import subprocess
import sys
import time
from unittest import mock

from harness import BIN, ROOT, Case, spawn_record

from athena.caps import retire, spawn, status
from athena.kernel import agents, caps, config, ledger, store

FIXTURE = ROOT / "tests" / "fixtures" / "claude-launch.json"


def _neutral(text, case) -> str:
    """The launch with this test's own places named by placeholders, so it compares across runs and checkouts."""
    for real, mark in ((str(case.tmp), "<tmp>"), (str(ROOT), "<root>")):
        text = text.replace(real, mark).replace(os.path.realpath(real), mark)
    return text


class ClaudeLaunchUnchanged(Case):
    def _spawn(self, **more):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        spawn.spawn(caps.CTX, self.repo(), "t/a", "a", prompt="hi", timeout=0.1, fetch=False, **more)
        launcher = store.path("run", "a.sh").read_text()
        settings = json.loads(store.path("settings", "a.json").read_text())
        return {"launcher": _neutral(launcher, self),
                "settings": json.loads(_neutral(json.dumps(settings), self)),
                "claude_args": json.loads(_neutral(json.dumps(ledger.get("a")["claude_args"]), self))}

    def test_a_spawn_without_the_agent_flag_launches_claude_as_main_did(self):
        """tests/fixtures/claude-launch.json was written from main before Pi was added: the launcher script, the worker's
        settings and the ledger's claude_args are what main made."""
        seen = self._spawn()
        if os.environ.get("WRITE_CLAUDE_LAUNCH_FIXTURE"):
            FIXTURE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
        self.assertEqual(seen, json.loads(FIXTURE.read_text()))


SESSION = "0f0f0f0f-1111-4222-8333-444444444444"


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_if_sleep(pid):
    if pid > 1 and _alive(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class PiCase(Case):
    """A Pi worker as athena sees one: its ledger record, its window in the fake backend, and its Pi running as ps tells it,
    under the pid its extension wrote to run/<name>.pi.json (Pi's process title is `pi`, which hides its --name)."""

    def pi_worker(self, name="pi-a", path=None, repo=None, status_=None, **fields):
        place = caps.CTX.backend.create(name, self.tmp, ["x"], block_label=name)
        spawn_record(name, path or self.tmp, repo or self.tmp, block=False, agent="pi", pi_session=SESSION, **fields)
        ledger.append("update", name, space=place.space, block=place.block)
        self.set_status(place, status_)
        return place

    def set_status(self, place, status_=None, screen=""):
        path = os.environ["ATHENA_FAKE_BACKEND"]
        data = json.load(open(path))
        data["spaces"][place.space]["blocks"][place.block].update(screen=screen, status=status_)
        json.dump(data, open(path, "w"))

    def pi_running(self, name="pi-a", pid=4321, mark_pid=None, command="pi"):
        store.ensure("run")
        agents.pid_file(name).write_text(str(pid))
        store.write_json(agents.pi_mark(name), {"pid": mark_pid or pid, "session": SESSION})
        stub = self.stubs / "ps-pi"
        stub.write_text(f"#!/bin/sh\necho '{pid} S {command}'\n")
        stub.chmod(0o755)
        os.environ["ATHENA_PS"] = str(stub)

    def inbox(self, name="pi-a") -> list:
        folder = agents.inbox(name)
        return [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def fake(self) -> dict:
        return json.load(open(os.environ["ATHENA_FAKE_BACKEND"]))

    def cli(self, *args, env=None, input_=None):
        full = {**os.environ, **(env or {})}
        return subprocess.run([str(BIN), *args], capture_output=True, text=True, env=full, timeout=60, input=input_)


class PiSpawn(PiCase):
    def _spawn(self, name="a", **more):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        return spawn.spawn(caps.CTX, self.repo(), "t/a", name, timeout=0.1, fetch=False, agent="pi", **more)

    def _exec(self, name="a") -> list:
        line = next(l for l in store.path("run", f"{name}.sh").read_text().splitlines() if l.startswith("exec "))
        return shlex.split(line[len("exec "):])

    def test_spawn_with_the_pi_agent_launches_pi_in_its_worktree_with_athenas_extension_and_prompt(self):
        out = self._spawn(prompt="hi")
        words = self._exec()
        at = words.index("true")  # ATHENA_PI, the harness's stand-in for the pi command
        env = dict(w.split("=", 1) for w in words[1:at])
        worker = ledger.get("a")
        self.assertEqual(words[at + 1:at + 3], ["--name", "a"])
        self.assertEqual(words[at + 3], "--session-id")
        self.assertEqual(words[at + 4], worker["pi_session"])
        self.assertIn("--approve", words)
        self.assertEqual(words[words.index("-e") + 1], str(ROOT / "pi-extension" / "athena.ts"))
        self.assertEqual(words[words.index("--append-system-prompt") + 1], str(ROOT / "prompts" / "worker-pi.md"))
        self.assertTrue((ROOT / "pi-extension" / "athena.ts").is_file() and (ROOT / "prompts" / "worker-pi.md").is_file())
        self.assertEqual(worker["agent"], "pi")
        self.assertEqual(worker["agent_args"], words[at + 1:])
        self.assertNotIn("claude_args", worker)
        launcher = store.path("run", "a.sh").read_text()
        self.assertIn(f"cd {shlex.quote(worker['path'])} ||", launcher)  # its own worktree
        self.assertEqual(out["path"], worker["path"])
        for key, value in {"ATHENA_WORKER": "a", "ATHENA_STATE_DIR": str(self.state), "ATHENA_HQ": "athena",
                           "ATHENA_BRIEF": str(self.state / "briefs" / "a.md"), "ATHENA_BIN": str(ROOT / "bin" / "athena")}.items():
            self.assertEqual(env[key], value)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(ROOT / "bin"))  # the athena report it runs is this athena
        self.assertFalse(store.path("settings", "a.json").exists())  # Pi reads no --settings
        self.assertNotIn("--plugin-dir", words)  # no Claude mod, no progress band

    def test_pi_runs_its_own_default_model_unless_one_is_given_and_then_exactly_that(self):
        self.write_config(worktree_root=str(self.tmp / "wt"), model="opus", effort="high")
        self._spawn(prompt="hi")
        self.assertNotIn("--model", self._exec())
        self.assertNotIn("--thinking", self._exec())
        self.assertIsNone(ledger.get("a")["model"])
        given = "cloudflare-workers-ai/@cf/deepseek-ai/deepseek-v4.1-flash-1017:high"
        repo = self.repo("other")
        spawn.spawn(caps.CTX, repo, "t/b", "b", timeout=0.1, fetch=False, agent="pi", prompt="hi", model=given, effort="low")
        words = self._exec("b")
        self.assertEqual(words[words.index("--model") + 1], given)
        self.assertEqual(words[words.index("--thinking") + 1], "low")
        self.assertEqual(ledger.get("b")["model"], given)

    def test_pi_with_ultracode_is_refused_and_says_why(self):
        with self.assertRaises(spawn.SpawnError) as caught:
            self._spawn(prompt="hi", ultracode=True)
        self.assertIn("ultracode", str(caught.exception))
        self.assertIn("Pi has neither", str(caught.exception))
        self.assertIsNone(ledger.get("a"))  # refused before anything was made

    def test_pi_not_on_path_is_refused_and_says_so(self):
        os.environ["ATHENA_PI"] = "no-such-pi-anywhere"
        config.reset()
        with self.assertRaises(spawn.SpawnError) as caught:
            self._spawn(prompt="hi")
        self.assertIn("pi is not on PATH", str(caught.exception))
        self.assertIsNone(ledger.get("a"))

    def test_the_cli_takes_agent_pi_and_the_default_is_claude(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        out = self.cli("spawn", "--repo", str(repo), "--branch", "t/c", "--name", "c", "--prompt", "hi", "--timeout", "0.1",
                       "--no-fetch", "--json")
        self.assertNotIn('"agent"', out.stdout)
        self.assertIn("claude_args", ledger.get("c"))
        out = self.cli("spawn", "--agent", "pi", "--repo", str(repo), "--branch", "t/p", "--name", "p", "--prompt", "hi",
                       "--timeout", "0.1", "--no-fetch", "--json")
        self.assertEqual(ledger.get("p")["agent"], "pi", out.stderr)
        out = self.cli("spawn", "--agent", "pi", "--ultracode", "--repo", str(repo), "--branch", "t/u", "--name", "u")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ultracode", out.stdout + out.stderr)

    def test_its_goal_goes_to_its_inbox_in_words_with_its_brief_once_pi_is_up(self):
        brief = self.tmp / "brief.md"
        brief.write_text("GOAL: x\nSCOPE: y\nACCEPTANCE:\n- z\nVERIFY: `true` exits 0.\nTIMEBOX: 1 hour\nFORBIDDEN: none\n")
        out = self._spawn(brief_path=str(brief), goal="the file exists", brief_check=False)
        self.assertTrue(out["pending"])  # nothing ran it in the fake backend, so its goal waits
        self.assertEqual(self.inbox("a"), [])
        worker = ledger.get("a")
        self.set_status(agents.place_of(worker), {"state": "idle"})
        self.pi_running("a")
        self.assertEqual(spawn.deliver_pending(caps.CTX, "a")["outcome"], "sent")
        [message] = self.inbox("a")
        self.assertEqual(message["kind"], "message")
        self.assertTrue(message["text"].startswith("Your goal: the file exists Your brief is "))
        self.assertIn(str(self.state / "briefs" / "a.md"), message["text"])
        self.assertNotIn("/goal", message["text"])
        self.assertNotIn("SendMessage", message["text"])
        self.assertEqual(self.fake()["typed"], [])  # never typed into Pi's editor

    def test_an_amend_goes_to_a_pi_worker_in_words(self):
        self.pi_worker(status_={"state": "working"})
        self.pi_running()
        spawn.amend(caps.CTX, "pi-a", "a new condition")
        [message] = self.inbox()
        self.assertTrue(message["text"].startswith("Your goal: a new condition"))


class PiStatus(PiCase):
    def test_a_running_pi_reads_as_its_hook_state_marked_pi_and_a_stranger_on_its_pid_as_exited(self):
        self.pi_worker()
        self.pi_running()
        store.write_json(store.path("workers", "pi-a.json"), {"state": "working", "ts": time.time()})
        [row] = status.collect()
        self.assertEqual((row["state"], row["agent"]), ("working", "pi"))
        self.assertIn("[pi]", status._line(row))
        self.pi_running(mark_pid=999)  # its pid, now someone else's pi
        self.assertEqual(status.collect()[0]["state"], "exited")
        os.unlink(agents.pi_mark("pi-a"))
        self.assertEqual(status.collect()[0]["state"], "exited")

    def test_pi_state_comes_from_rex_as_pi_reports_it_and_a_failed_run_is_idle(self):
        place = self.pi_worker(status_={"state": "working"})
        self.pi_running()
        record = {**ledger.get("pi-a"), "name": "pi-a"}
        self.assertEqual(agents.state(caps.CTX.backend, record)["state"], "working")
        self.set_status(place, {"state": "error", "msg": "API error"})
        self.assertEqual(agents.state(caps.CTX.backend, record)["state"], "idle")

    def test_a_claude_line_is_unchanged_and_has_no_agent_mark(self):
        spawn_record("w", self.tmp, self.tmp)
        [row] = status.collect()
        self.assertEqual(row["agent"], "claude")
        self.assertNotIn("[", status._line(row))

    def test_the_board_and_the_dash_show_the_agent(self):
        from athena.caps import board
        from athena.caps.dash import state as dash_state
        self.pi_worker()
        spawn_record("w", self.tmp, self.tmp)
        rows = status.collect()
        drawn = board.render(rows, {})
        self.assertIn("[pi]", next(l for l in drawn.splitlines() if " pi-a " in l))
        self.assertNotIn("[", next(l for l in drawn.splitlines() if " w " in l))
        events = [json.loads(l) for l in store.path("ledger.jsonl").read_text().splitlines() if l.strip()]
        workers = {w.name: w for w in dash_state.fold_ledger(events)}
        self.assertEqual((workers["pi-a"].agent, workers["w"].agent), ("pi", "claude"))

    def test_the_watch_tells_a_pi_workers_report_as_it_does_a_claudes(self):
        from athena.caps import watch
        self.pi_worker()
        self.pi_running()
        store.write_json(store.path("workers", "pi-a.json"), {"state": "working", "ts": time.time()})
        out = self.cli("watch", "--once", "--tagged")
        self.assertEqual(out.returncode, 0, out.stderr)
        report = {"status": "DONE", "pr": "", "head": "a" * 40, "verify": "x -> pass", "decisions": [], "concerns": []}
        store.write_json(store.path("workers", "pi-a.json"), {"state": "done", "ts": time.time(), "report": report})
        lines = [json.loads(l)["line"] for l in self.cli("watch", "--once", "--tagged").stdout.splitlines() if l.startswith("{")]
        self.assertIn("pi-a report DONE", lines)


class PiGate(PiCase):
    """The events athena's Pi extension sends `athena hook`, as it sends them: the gate and the state machine are Claude's own."""

    def _hook(self, event, name="pi-a"):
        out = self.cli("hook", env={"ATHENA_WORKER": name, "ATHENA_STATE_DIR": str(self.state)}, input_=json.dumps(event))
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def _state(self, name="pi-a"):
        return store.read_dict(store.path("workers", f"{name}.json")).get("state")

    def test_a_pi_turn_is_held_to_a_filed_report_and_the_report_is_what_status_shows(self):
        repo = self.repo()
        path = self.worktree(repo)
        self.pi_worker(path=path, repo=repo)
        cwd = str(path)
        self._hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": cwd})
        self.assertEqual(self._state(), "idle")
        self._hook({"hook_event_name": "UserPromptSubmit", "prompt": "Your goal: ...", "cwd": cwd})
        self.assertEqual(self._state(), "working")
        said = self._hook({"hook_event_name": "Stop", "last_assistant_message": "all done", "cwd": cwd})
        self.assertEqual(said["decision"], "block")  # the extension turns this into one more turn, with this reason
        self.assertIn("No report was filed", said["reason"])
        filed = self.cli("report", "--status", "DONE", "--verify", "true -> pass", env={"ATHENA_WORKER": "pi-a"})
        self.assertIn("report filed: DONE", filed.stdout, filed.stderr)
        self.assertIsNone(self._hook({"hook_event_name": "Stop", "last_assistant_message": "done, filed", "cwd": cwd}))
        self.assertEqual(self._state(), "done")
        self.pi_running()
        [row] = status.collect()
        self.assertEqual((row["state"], row["report"]["status"], row["agent"]), ("done", "DONE", "pi"))
        self._hook({"hook_event_name": "SessionEnd", "reason": "quit", "cwd": cwd})
        self.assertEqual(self._state(), "exited")

    def test_a_run_that_ended_with_no_stop_rests_as_idle(self):
        self._hook({"hook_event_name": "SessionStart", "source": "startup"})
        self._hook({"hook_event_name": "UserPromptSubmit", "prompt": "x"})
        self._hook({"hook_event_name": "Notification", "notification_type": "idle_prompt"})  # aborted with Escape
        self.assertEqual(self._state(), "idle")

    def test_the_git_rules_hold_in_pi_too(self):
        said = self._hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push --force origin x"}})
        self.assertEqual(said["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self._hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git status"}}))

    def test_the_extension_sends_these_events_and_acts_on_the_answers(self):
        """The extension's side of the contract, read from its source: each event it sends, and the two answers it acts on."""
        source = (ROOT / "pi-extension" / "athena.ts").read_text()
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "Notification", "PreToolUse", "SessionEnd"):
            self.assertIn(f'hook_event_name: "{event}"', source)
        self.assertIn('said?.decision !== "block"', source)
        self.assertIn("continue: true", source)
        self.assertIn('out?.permissionDecision === "deny"', source)
        self.assertIn("${NAME}.pi.json", source)
        self.assertIn('join(STATE, "run", "inbox", NAME)', source)


class PiMessages(PiCase):
    def test_athena_send_reaches_a_pi_worker_through_its_inbox_and_never_types(self):
        self.pi_worker(status_={"state": "working"})
        self.pi_running()
        out = self.cli("send", "pi-a", "main moved: rebase")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.inbox(), [{"kind": "message", "text": "main moved: rebase"}])
        self.assertEqual(self.fake()["typed"], [])

    def test_a_pi_that_is_not_running_or_blocked_gets_nothing(self):
        place = self.pi_worker(status_={"state": "idle"})
        out = self.cli("send", "pi-a", "hello")
        self.assertNotEqual(out.returncode, 0)
        self.pi_running()
        self.set_status(place, {"state": "blocked", "kind": "auth", "msg": "Log in"})
        self.assertNotEqual(self.cli("send", "pi-a", "hello").returncode, 0)
        self.assertEqual(self.inbox(), [])


class PiElsewhere(PiCase):
    """Every other path that assumed Claude, for a Pi worker."""

    def test_retire_asks_pi_to_quit_through_its_inbox_never_types_exit_and_finishes(self):
        repo = self.repo()
        path = self.worktree(repo)
        self.pi_worker(path=path, repo=repo, status_={"state": "idle"})
        made = "import subprocess; print(subprocess.Popen(['/bin/sleep', '60'], stdout=subprocess.DEVNULL).pid)"  # then it exits
        pid = int(subprocess.run([sys.executable, "-c", made], capture_output=True, text=True).stdout)
        self.assertGreater(pid, 1)
        self.addCleanup(lambda: _kill_if_sleep(pid))  # a sleep it made, if still there
        self.pi_running(pid=pid)
        stub = self.stubs / "ps-pi"  # this pi is there only while the sleep is, and not its own child: nobody leaves it a zombie
        stub.write_text(f"#!/bin/sh\nkill -0 {pid} 2>/dev/null && echo '{pid} S pi'\nexit 0\n")
        out = retire.retire(caps.CTX, "pi-a", grace=0.5)
        self.assertEqual(ledger.get("pi-a")["state"], "retired", out)
        self.assertFalse(_alive(pid))  # killed, as a last resort: nothing read its quit here
        self.assertEqual(self.fake()["typed"], [])  # no /exit typed into Pi's editor
        self.assertFalse(path.exists())
        self.assertEqual(self.inbox(), [])  # its unread quit is gone with it, so a new pi-a doesn't quit at once
        self.assertFalse(agents.pi_mark("pi-a").exists())

    def test_a_new_pi_of_an_old_name_starts_with_an_empty_inbox(self):
        agents.post("a", {"kind": "quit"})
        store.write_json(agents.pi_mark("a"), {"pid": 77})
        self.write_config(worktree_root=str(self.tmp / "wt"))
        spawn.spawn(caps.CTX, self.repo(), "t/a", "a", timeout=0.1, fetch=False, agent="pi", prompt="hi")
        self.assertEqual(self.inbox("a"), [])
        self.assertFalse(agents.pi_mark("a").exists())

    def test_send_no_submit_is_refused_for_a_pi(self):
        self.pi_worker(status_={"state": "idle"})
        self.pi_running()
        out = self.cli("send", "pi-a", "--no-submit", "half")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.inbox(), [])

    def test_graceful_exit_of_a_pi_posts_quit(self):
        self.pi_worker(status_={"state": "idle"})
        self.pi_running()
        self.assertFalse(agents.graceful_exit(caps.CTX.backend, {**ledger.get("pi-a"), "name": "pi-a"}, grace=0.2))
        self.assertEqual(self.inbox(), [{"kind": "quit"}])
        self.assertEqual(self.fake()["typed"], [])

    def test_revive_brings_a_pi_back_on_its_own_launcher_and_session_never_with_resume(self):
        from athena.modules.revive import script
        self.pi_worker()
        self.assertEqual(spawn.conversation("pi-a", ledger.get("pi-a")), SESSION)
        launch = (f"#!/bin/sh\necho $$ > /x/run/pi-a.pid\ncd /w || exit 111\nexec env ATHENA_WORKER=pi-a pi --name pi-a "
                  f"--session-id {SESSION} --approve -e /r/pi-extension/athena.ts\n")
        self.assertEqual(script.resumed(launch, "pi-a", SESSION), launch)
        with self.assertRaises(script.NotLaunch):
            script.resumed(launch, "pi-a", "another-session")
        claude = "#!/bin/sh\necho $$ > /x/run/w.pid\ncd /w || exit 111\nexec env ATHENA_WORKER=w claude --name w --model opus\n"
        self.assertIn("claude --resume s1 --name w", script.resumed(claude, "w", "s1"))  # Claude's as before

    def test_revive_dry_run_names_pis_session_flag(self):
        from athena.modules import revive
        lines = revive.describe({"instance": None, "last": revive.load().to_json(), "panes": [
            {"name": "pi-a", "kind": "worker", "revive": True, "session": SESSION, "at": "zsh", "then": None, "agent": "pi"},
            {"name": "w", "kind": "worker", "revive": True, "session": "s1", "at": "zsh", "then": None, "agent": "claude"}]})
        self.assertTrue(any(f"pi --session-id {SESSION}" in l for l in lines))
        self.assertTrue(any("--resume s1" in l for l in lines))

    def test_progress_skips_a_pi_worker_and_says_why(self):
        self.pi_worker()
        out = self.cli("progress", "ask", "--name", "pi-a", input_="")
        self.assertEqual((out.returncode, out.stdout), (0, ""))
        self.assertIn("pi-a runs Pi, so it has no progress band", out.stderr)

    def test_flame_has_nothing_of_a_pi_and_does_not_trip_on_one(self):
        from athena.caps.flame import label
        self.pi_worker()
        store.write_json(store.path("workers", "pi-a.json"), {"state": "idle", "session_id": None})
        facts = label.athena_facts()
        self.assertNotIn(None, facts["sessions"])
        self.assertIn("pi-a", facts["spawns"])

    def test_doctor_checks_a_running_pis_hooks_said_hello(self):
        from athena.caps import doctor
        self.pi_worker()
        self.pi_running()
        with mock.patch.object(doctor, "HELLO_WITHIN", -1):
            [check] = doctor._workers_hooks(caps.CTX)
        self.assertEqual(check.name, "hooks:pi-a")  # no SessionStart from its extension: said, as for a Claude
        store.write_json(store.path("workers", "pi-a.json"), {"state": "idle"})
        with mock.patch.object(doctor, "HELLO_WITHIN", -1):
            self.assertEqual(doctor._workers_hooks(caps.CTX), [])

    def test_review_records_a_pi_workers_review(self):
        from athena.caps import reviews
        self.pi_worker()
        self.assertEqual(reviews.record("pi-a", "start", head="a" * 40)["round"], 1)
        reviews.record("pi-a", "pass")

    def test_the_usage_limit_reader_never_reads_a_pi_screen(self):
        from athena.caps import limits
        place = self.pi_worker(status_={"state": "idle"})
        self.pi_running()
        self.set_status(place, {"state": "idle"}, screen="You've hit your limit · resets 3pm\n")
        self.assertEqual(limits.tick(caps.CTX, time.time()), [])
        self.assertIsNone(limits.parked())
        self.assertFalse(ledger.get("pi-a").get("limit_seen"))


INBOX_DRIVER = """
import { HOOK_TIMEOUT_MS, RETRY_AFTER_MS, makeInbox } from './athena.ts'
import { readdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
const folder = process.env.INBOX
const sent = []
let idle = true, shut = 0
const pi = { sendUserMessage: (text, options) => sent.push([text, options?.deliverAs ?? null]) }
const ctx = { isIdle: () => idle, shutdown: () => { shut += 1 } }
const put = (n, message) => writeFileSync(join(folder, `${String(n).padStart(20, '0')}-1.json`), JSON.stringify(message))
const files = () => readdirSync(folder).sort()
const out = {}
put(1, { kind: 'message', text: 'the goal' })
put(2, { kind: 'message', text: 'main moved' })
const inbox = makeInbox(folder, pi)
inbox.tick(ctx, 1000)
inbox.tick(ctx, 1500)  // Pi reads idle until the goal's run starts: the second must not go out as a prompt beside it
out.first = { sent: [...sent], files: files() }
inbox.accepted('the goal'); idle = false  // before_agent_start: its run began
inbox.tick(ctx, 2000)
out.second = { sent: [...sent], files: files() }
inbox.accepted('main moved')  // input, followUp: queued
out.queued = files()
idle = true
put(3, { kind: 'message', text: 'no run took this' })
inbox.tick(ctx, 3000)
inbox.tick(ctx, 3000 + HOOK_TIMEOUT_MS + 1)  // Pi may still be awaiting its settled handlers, a hook among them: not yet
out.waited = sent.length
for (const now of [3000 + RETRY_AFTER_MS, 3000 + 2 * RETRY_AFTER_MS, 3000 + 3 * RETRY_AFTER_MS]) inbox.tick(ctx, now)
out.retried = { sent: sent.slice(2), files: files() }
put(4, { kind: 'quit' })
inbox.tick(ctx, 50000)
out.quit = { shut, files: files() }
writeFileSync(join(folder, `${'5'.padStart(20, '0')}-1.json.taken`), JSON.stringify({ kind: 'message', text: 'again' }))
makeInbox(folder, pi)
out.restarted = files()
out.timers = { hook: HOOK_TIMEOUT_MS, retry: RETRY_AFTER_MS }
process.stdout.write(JSON.stringify(out))
"""


class PiInbox(Case):
    """The extension's own inbox loop, run under Node (type stripping) with a stand-in Pi, as tests/test_progress.py runs the
    band's core.ts: two messages in one tick while Pi is idle."""

    def _run(self) -> dict:
        from test_progress import NODE
        if not NODE:
            raise AssertionError("node is needed to test the Pi extension's inbox")
        node = self.tmp / "node"
        node.symlink_to(os.path.realpath(NODE))
        folder = self.tmp / "inbox"
        folder.mkdir()
        out = subprocess.run([str(node), "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", INBOX_DRIVER],
                             capture_output=True, text=True, cwd=ROOT / "pi-extension", timeout=60,
                             env={**os.environ, "INBOX": str(folder), "ATHENA_WORKER": "", "ATHENA_STATE_DIR": "", "ATHENA_BIN": ""})
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    def test_two_messages_in_one_tick_while_idle_go_one_at_a_time_and_none_is_lost(self):
        out = self._run()
        first, second = "0" * 19 + "1-1.json", "0" * 19 + "2-1.json"
        self.assertEqual(out["first"]["sent"], [["the goal", None]])  # only the first goes out as a new prompt
        self.assertEqual(out["first"]["files"], [f"{first}.taken", second])  # claimed until Pi has it; the next waits
        self.assertEqual(out["second"]["sent"], [["the goal", None], ["main moved", "followUp"]])
        self.assertEqual(out["second"]["files"], [f"{second}.taken"])
        self.assertEqual(out["queued"], [])

    def test_a_message_no_run_took_is_sent_again_then_kept_not_lost(self):
        out = self._run()
        self.assertEqual(out["waited"], 3)  # the two before it, and its first send: no retry inside a hook's timeout
        self.assertEqual(out["retried"]["sent"], [["no run took this", None]] * 3)
        self.assertEqual(out["retried"]["files"], ["0" * 19 + "3-1.failed"])

    def test_a_retry_waits_out_the_longest_hook_so_a_parked_prompt_is_never_sent_twice(self):
        timers = self._run()["timers"]
        self.assertGreater(timers["retry"], timers["hook"])

    def test_quit_ends_pi_and_a_claimed_file_left_by_a_restart_goes_back_in_the_queue(self):
        out = self._run()
        self.assertEqual(out["quit"], {"shut": 1, "files": ["0" * 19 + "3-1.failed"]})
        self.assertEqual(out["restarted"], ["0" * 19 + "3-1.failed", "0" * 19 + "5-1.json"])
