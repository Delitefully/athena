import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock

from harness import Case, dead_pid, git, spawn_record, store

from athena_rex.caps import hooks, retire, spawn, status, trust, workspaces
from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, gitops, lab, ledger
from athena_rex.kernel.backend import BackendError


def real_pid_file(name, pid):
    path = agents.pid_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))



class Trust(Case):
    def _file(self):
        return self.home / ".claude.json"

    def test_first_worktree_of_a_new_repo_keys_on_the_main_checkout(self):
        repo = self.repo()
        path = self.worktree(repo)
        out = trust.ensure(path)
        data = json.loads(self._file().read_text())
        self.assertEqual(out["key"], os.path.realpath(repo))
        self.assertIs(data["projects"][os.path.realpath(repo)]["hasTrustDialogAccepted"], True)
        self.assertTrue(trust.is_trusted(path))
        self.assertFalse(trust.ensure(path)["wrote"])

    def test_other_keys_and_mode_survive(self):
        self._file().write_text(json.dumps({"keep": 1, "projects": {"/other": {"a": 2}}}))
        os.chmod(self._file(), 0o600)
        trust.ensure(self.repo())
        data = json.loads(self._file().read_text())
        self.assertEqual((data["keep"], data["projects"]["/other"]["a"]), (1, 2))
        self.assertEqual(os.stat(self._file()).st_mode & 0o777, 0o600)

    def test_a_held_lock_means_no_write(self):
        self._file().write_text("{}")
        lock = f"{self._file()}.lock"
        os.mkdir(lock)
        self.addCleanup(lambda: os.path.exists(lock) and os.rmdir(lock))
        with self.assertRaises(trust.TrustError):
            trust.ensure(self.repo(), wait=0.3)
        self.assertEqual(self._file().read_text(), "{}")

    def test_a_stale_lock_is_taken_over(self):
        lock = f"{self._file()}.lock"
        os.mkdir(lock)
        old = time.time() - trust.STALE_AFTER - 5
        os.utime(lock, (old, old))
        self.assertTrue(trust.ensure(self.repo(), wait=1)["wrote"])
        self.assertFalse(os.path.exists(lock))

    def test_a_file_that_will_not_parse_is_left_alone(self):
        self._file().write_text("{ partial")
        with self.assertRaises(trust.TrustError):
            trust.ensure(self.repo())
        self.assertEqual(self._file().read_text(), "{ partial")

    def test_running_claudes_keep_their_writes(self):
        """A stand-in for Claude rewriting the file under the same lock, while spawns add keys."""
        self._file().write_text(json.dumps({"counter": 0, "projects": {}}))
        lock = f"{self._file()}.lock"
        stop = threading.Event()
        written = [0]

        def claude():
            while not stop.is_set():
                try:
                    os.mkdir(lock)
                except FileExistsError:
                    time.sleep(0.001)
                    continue
                try:
                    data = json.loads(self._file().read_text())
                    written[0] += 1
                    data["counter"] = written[0]
                    tmp = f"{self._file()}.c"
                    with open(tmp, "w") as f:
                        json.dump(data, f)
                    os.replace(tmp, self._file())
                finally:
                    os.rmdir(lock)
                time.sleep(0.001)
        thread = threading.Thread(target=claude)
        thread.start()
        folders = []
        try:
            for i in range(12):
                folder = self.tmp / f"plain{i}"
                folder.mkdir()
                folders.append(folder)
                trust.ensure(folder, wait=5)
        finally:
            stop.set()
            thread.join()
        data = json.loads(self._file().read_text())
        self.assertEqual(data["counter"], written[0])
        for folder in folders:
            self.assertIs(data["projects"][os.path.realpath(folder)]["hasTrustDialogAccepted"], True)

    def test_the_lab_never_writes_the_real_file(self):
        os.environ["HOME"] = lab.real_home()
        self.assertTrue(trust.ensure(self.tmp)["skipped"])

    def test_spawn_trusts_before_it_starts_claude(self):
        repo = self.repo()
        seen = {}
        real = agents.start

        def start(backend, name, label, cwd, argv, env=None, **more):
            seen["trusted"] = trust.is_trusted(cwd)
            return real(backend, name, label, cwd, argv, env, **more)
        agents.start = start
        self.addCleanup(lambda: setattr(agents, "start", real))
        self.write_config(worktree_root=str(self.tmp / "wt"))
        spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", timeout=0.1, fetch=False)
        self.assertTrue(seen["trusted"])


class Gate(Case):
    def _stop(self, name, message, cwd=None, gated=True):
        return hooks.apply({"hook_event_name": "Stop", "last_assistant_message": message, "cwd": cwd or str(self.tmp),
                            "session_id": "s1"}, name, gated)

    def test_no_report_is_sent_back_then_the_gate_gives_way(self):
        for _ in range(hooks.MAX_BLOCKS):
            self.assertEqual(self._stop("w", "hello")["gate"]["decision"], "block")
        out = self._stop("w", "hello")
        self.assertIsNone(out["gate"])
        self.assertEqual(out["state"], "idle-unreported")

    def _report(self, **fields):
        return "done\nATHENA-REPORT " + json.dumps(fields)

    def test_done_needs_verify_and_head_and_not_a_push(self):
        repo = self.repo()
        path = self.worktree(repo)
        head = git(path, "rev-parse", "HEAD")
        bad_verify = self._stop("a", self._report(status="DONE", verify="x -> fail", head=head), str(path))
        self.assertIn("VERIFY", bad_verify["gate"]["reason"])
        bad_head = self._stop("b", self._report(status="DONE", verify="x -> pass", head="deadbeef"), str(path))
        self.assertIn("differs", bad_head["gate"]["reason"])
        self.assertEqual(self._stop("c", self._report(status="DONE", verify="x -> pass", head=head), str(path))["state"], "done")
        git(path, "commit", "-q", "--allow-empty", "-m", "unpushed")
        head = git(path, "rev-parse", "HEAD")
        unpushed = self._stop("d", self._report(status="DONE", verify="x -> pass", head=head), str(path))
        self.assertEqual(unpushed["state"], "done", "local-only work reports DONE, as in the old athena")

    def test_blocked_and_in_progress_always_end_the_turn(self):
        self.assertEqual(self._stop("a", self._report(status="BLOCKED"))["state"], "blocked")
        out = self._stop("b", self._report(status="IN_PROGRESS"))
        self.assertIsNone(out["gate"])
        self.assertEqual(out["state"], "idle")

    def test_a_full_disk_does_not_turn_the_gate_off(self):
        real = store.write_json

        def full(*args, **kwargs):
            raise OSError(28, "No space left on device")
        hooks.store.write_json = full
        try:
            self.assertEqual(self._stop("w", "hello")["gate"]["decision"], "block")
        finally:
            hooks.store.write_json = real

    def test_a_worker_with_gone_worktree_cannot_report_done(self):
        out = self._stop("w", self._report(status="DONE", verify="x -> pass", head="a" * 40), str(self.tmp / "nope"))
        self.assertIn("gone", out["gate"]["reason"])

    def test_events_only_reach_the_named_worker(self):
        out = hooks.main_hook(json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(self.tmp)}), {})
        self.assertEqual(out, "")
        self.assertFalse(store.path("workers").exists())


class Status(Case):
    def test_a_dead_claude_never_reads_as_working(self):
        spawn_record("w", "/x", "/r")
        real_pid_file("w", dead_pid())
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        row = status.collect()[0]
        self.assertEqual((row["state"], row["state_source"]), ("exited", "process"))

    def test_ps_failing_is_not_a_death(self):
        spawn_record("w", "/x", "/r")
        real_pid_file("w", 12345)
        hooks.apply({"hook_event_name": "UserPromptSubmit"}, "w")
        os.environ["ATHENA_PS"] = "/nonexistent/ps"
        self.assertEqual(status.collect()[0]["state"], "working")

    def test_a_pending_goal_shows_as_blocked(self):
        spawn_record("w", "/x", "/r", goal_pending="go", pending_reason="startup prompt")
        self.assertEqual(status.collect()[0]["state"], "blocked")


class Retire(Case):
    def _worker(self, name="w"):
        repo = self.repo()
        path = self.worktree(repo)
        spawn_record(name, path, repo)
        real_pid_file(name, dead_pid())
        return repo, path

    def test_crashed_worker_with_unsaved_work_is_refused_and_kept(self):
        repo, path = self._worker()
        (path / "note.txt").write_text("only here")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertTrue((path / "note.txt").exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_force_saves_untracked_and_tracked_work_first(self):
        repo, path = self._worker()
        (path / "note.txt").write_text("only here")
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(repo, "show", f"{out['wip_backup']}:note.txt"), "only here")
        self.assertFalse(path.exists())
        self.assertEqual(ledger.get("w")["state"], "retired")

    def test_unpushed_commits_are_refused_then_kept_under_a_ref(self):
        repo, path = self._worker()
        git(path, "commit", "-q", "--allow-empty", "-m", "unpushed")
        head = git(path, "rev-parse", "HEAD")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(repo, "rev-parse", out["backup"]), head)

    def test_when_nobody_can_say_the_checkout_stays(self):
        repo, path = self._worker()
        real_pid_file("w", 12345)
        os.environ["ATHENA_PS"] = "/nonexistent/ps"
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(path.exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_a_retire_saves_what_a_dying_claude_wrote_last(self):
        repo, path = self._worker()
        (path / "first.txt").write_text("early")
        real = retire._stop_claude
        calls = []

        def writes_while_stopping(*args, **kwargs):
            calls.append(1)
            (path / "late.txt").write_text("written during the exit")
            return True
        retire._stop_claude = writes_while_stopping
        real_stat = agents.stat_of
        agents.stat_of = lambda name: "S"
        typing = agents.typing
        agents.typing = lambda *a: False
        try:
            out = retire.retire(caps.CTX, "w", force=True)
        finally:
            retire._stop_claude, agents.stat_of, agents.typing = real, real_stat, typing
        self.assertTrue(calls)
        self.assertEqual(git(repo, "show", f"{out['wip_backup']}:late.txt"), "written during the exit")
        self.assertEqual(git(repo, "show", f"{out['wip_backup']}:first.txt"), "early")

    def test_work_that_appears_during_a_plain_retire_stops_it(self):
        repo, path = self._worker()
        real = retire._stop_claude

        def writes_while_stopping(*args, **kwargs):
            (path / "late.txt").write_text("written during the exit")
            return True
        retire._stop_claude = writes_while_stopping
        real_stat = agents.stat_of
        agents.stat_of = lambda name: "S"
        typing = agents.typing
        agents.typing = lambda *a: False
        try:
            with self.assertRaises(retire.RetireError):
                retire.retire(caps.CTX, "w")
        finally:
            retire._stop_claude, agents.stat_of, agents.typing = real, real_stat, typing
        self.assertTrue((path / "late.txt").exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_a_broken_worktree_goes_to_trash_not_away(self):
        repo, path = self._worker()
        (path / "note.txt").write_text("only here")
        (path / ".git").unlink()
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())
        self.assertEqual((Path(out["trash"]) / "note.txt").read_text(), "only here")

    def test_retiring_a_failed_worker_leaves_the_folder_of_the_one_that_came_after(self):
        repo = self.repo()
        path = self.worktree(repo)
        ledger.append("spawn", "old", repo=str(repo), path=str(path), backend="fake")
        ledger.append("failed", "old", reason="setup failed", cleaned=True)
        spawn_record("new", path, repo)
        retire.retire(caps.CTX, "old", force=True)
        self.assertTrue(path.exists())
        self.assertEqual(ledger.get("old")["state"], "retired")

    def test_a_cleaned_failed_launch_keeps_no_path(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        real = agents.start

        def explode(*args, **kwargs):
            raise OSError("no")
        agents.start = explode
        self.addCleanup(lambda: setattr(agents, "start", real))
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", fetch=False)
        self.assertIsNone(ledger.get("a").get("path"))

    def test_untracked_files_count_whatever_the_repo_says_to_show(self):
        repo, path = self._worker()
        git(path, "config", "status.showUntrackedFiles", "no")
        (path / "note.txt").write_text("only here")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertTrue((path / "note.txt").exists())

    def test_a_repo_inside_the_worktree_stops_the_retire(self):
        repo, path = self._worker()
        inner = path / "inner"
        inner.mkdir()
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        git(inner, "commit", "-q", "--allow-empty", "-m", "only here")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(inner.exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_old_athenas_worker_is_not_ours_to_stop(self):
        ledger.append("spawn", "old", pane="w1:p1", workspace="w1", path=str(self.tmp), repo=str(self.tmp))
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "old")

    def test_a_clean_retire_records_history(self):
        self._worker()
        retire.retire(caps.CTX, "w")
        self.assertEqual(list(store.read_lines(store.path("history.jsonl")))[0]["name"], "w")


class Spawn(Case):
    def test_no_room_means_no_launch(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        os.environ["ATHENA_BUDGET_FAKE"] = "5,500,1.0"
        with self.assertRaisesRegex(UserError, "no room"):
            spawn.spawn(caps.CTX, self.repo(), "t/a", "a", prompt="hi", fetch=False)
        self.assertEqual(ledger.workers(), {})

    def test_full_disk_holds_launches(self):
        from athena_rex.kernel import budget
        self.assertTrue(budget.over(1, {"memory": 80, "disk_gb": 2, "load": 1.0, "cpus": 8}))
        self.assertTrue(budget.over(1, {"memory": 80, "disk_gb": 90, "load": 90.0, "cpus": 8}))
        self.assertFalse(budget.over(1, {"memory": None, "disk_gb": 90, "load": 1.0, "cpus": 8}))
        self.assertTrue(budget.over(3, {"memory": 40, "disk_gb": 90, "load": 1.0, "cpus": 8}))

    def test_capacity(self):
        self.write_config(worktree_root=str(self.tmp / "wt"), rex={"max_workers": 1})
        ledger.append("spawn", "other", backend="fake", space="s", block="b")
        with self.assertRaisesRegex(UserError, "cap is 1"):
            spawn.spawn(caps.CTX, self.repo(), "t/a", "a", prompt="hi", fetch=False)

    def test_a_goal_too_long_is_refused_not_cut(self):
        with self.assertRaises(UserError):
            spawn.goal_text("x " * 3000, "/b")

    def test_losing_a_race_for_a_path_leaves_the_winners_worktree(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        real = gitops.add_worktree
        winner = self.tmp / "wt" / "main" / "a"

        def lose(repo_, branch, path):
            real(repo_, "t/other", path)
            real(repo_, branch, path)
        gitops.add_worktree = lose
        self.addCleanup(lambda: setattr(gitops, "add_worktree", real))
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", fetch=False)
        self.assertTrue(winner.exists())

    def test_a_failed_launch_leaves_nothing_behind(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        real = agents.start

        def explode(*args, **kwargs):
            raise OSError("no")
        agents.start = explode
        self.addCleanup(lambda: setattr(agents, "start", real))
        with self.assertRaises(UserError):
            spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", fetch=False)
        self.assertEqual(ledger.get("a")["state"], "failed")
        self.assertEqual(ledger.get("a")["cleaned"], True)
        self.assertEqual(len(git(repo, "worktree", "list").splitlines()), 1)

    def test_settings_turn_the_old_plugin_off_and_install_hooks_with_quoting(self):
        target = spawn.settings_for("w", "/b")
        data = json.loads(target.read_text())
        self.assertIs(data["enabledPlugins"]["athena@athena"], False)
        self.assertEqual(data["env"]["ATHENA_STATE_DIR"], str(store.state_dir()))
        command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith('"') and command.endswith('" hook'))



class Goal(Case):
    def _worker(self, **fields):
        spawn_record("w", self.tmp, self.tmp, **fields)
        return {**ledger.get("w"), "name": "w"}

    def _send_raises(self, exc):
        real = agents.send

        def boom(*args, **kwargs):
            raise exc
        agents.send = boom
        self.addCleanup(lambda: setattr(agents, "send", real))

    def _ready(self):
        real = agents.state
        agents.state = lambda *a, **k: {"state": "idle"}
        self.addCleanup(lambda: setattr(agents, "state", real))

    def test_a_refused_first_goal_waits_for_the_next_try(self):
        record = self._worker()
        self._send_raises(agents.Refused("box_not_empty"))
        self.assertTrue(spawn._deliver(caps.CTX.backend, record, "the goal").startswith("waiting"))
        self.assertEqual(ledger.get("w")["goal_pending"], "the goal")

    def test_a_first_goal_is_on_record_before_it_is_typed(self):
        record = self._worker()
        self._ready()
        self._send_raises(SystemExit(9))
        with self.assertRaises(SystemExit):
            spawn._deliver(caps.CTX.backend, record, "the goal")
        self.assertEqual(ledger.get("w")["goal_unconfirmed"], "the goal")
        self.assertEqual(ledger.goal_state(ledger.get("w")), "unconfirmed")

    def test_each_goal_move_lands_where_the_table_says(self):
        worker_in = {"none": {}, "first": {"goal_pending": "g"}, "amended": {"goal_pending": "g", "goal_amending": True},
                     "unconfirmed": {"goal_unconfirmed": "g"}}
        for state, moves in ledger.GOAL_MOVES.items():
            self.assertEqual(ledger.goal_state(worker_in[state]), state)
            for move, allowed in moves.items():
                with self.subTest(state=state, move=move):
                    after = {**worker_in[state], **ledger.goal_fields(move, "new", worker_in[state])}
                    self.assertIn(ledger.goal_state(after), allowed)

    def test_a_refused_goal_stays_pending(self):
        self._worker(goal_pending="the goal")
        self._ready()
        self._send_raises(agents.Refused("unknown"))
        self.assertEqual(spawn.deliver_pending(caps.CTX, "w")["outcome"], "waiting")
        self.assertEqual(ledger.get("w")["goal_pending"], "the goal")

    def test_a_goal_that_may_have_been_typed_is_said_and_not_sent_twice(self):
        self._worker(goal_pending="the goal")
        self._ready()
        self._send_raises(BackendError("timeout"))
        self.assertEqual(spawn.deliver_pending(caps.CTX, "w")["outcome"], "unconfirmed")
        worker = ledger.get("w")
        self.assertIsNone(worker["goal_pending"])
        self.assertEqual(worker["goal_unconfirmed"], "the goal")
        self.assertIn("resend", status.collect()[0]["detail"])
        self.assertEqual([line.kind for line in spawn.tick(caps.CTX, 0)], ["wake"])
        self.assertEqual(spawn.tick(caps.CTX, 0), [])

    def test_send_says_unknown_when_nobody_can_tell(self):
        record = self._worker()
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("12345")
        os.environ["ATHENA_PS"] = "/nonexistent/ps"
        with self.assertRaises(agents.Refused):
            agents.send(caps.CTX.backend, record, "x")
        self.assertEqual(json.loads((self.tmp / "fake.json").read_text() if (self.tmp / "fake.json").exists() else '{"typed": []}')["typed"], [])


class Workspaces(Case):
    def _rows(self, **kw):
        return {r["path"]: r for r in workspaces.listing(sizes=False, **kw)}

    def test_lifecycle_table(self):
        old = time.time() - 90 * 86400
        cases = [({"owner_state": "live", "owner_running": True}, "active", False),
                 ({"owner_state": "live", "owner_running": False}, "crashed", False),
                 ({"owner_state": "retired"}, "retired", True),
                 ({"owner_state": "retired", "unsaved": True, "unsaved_detail": "1 uncommitted"}, "held", False),
                 ({}, "orphan", True),
                 ({"unsaved": True}, "held", False),
                 ({"kind": "scratch", "ttl_days": 900}, "active", False),
                 ({"kind": "scratch", "ttl_days": 1}, "expired", True),
                 ({"kind": "state"}, "orphan", False),
                 ({"in_use": True}, "active", False),
                 ({"kind": "worktree", "git_known": False}, "held", False)]
        for facts, state, removable in cases:
            out = workspaces.classify({"mtime": old, **facts})
            self.assertEqual((out["state"], out["removable"]), (state, removable), facts)
        self.assertFalse(workspaces.classify({"mtime": time.time()})["removable"])

    def _setup(self):
        self.write_config(worktree_root=str(self.home / "worktrees"), projects_dir=str(self.tmp / "projects"))
        return self.repo()

    def _scan(self):
        return workspaces.Scan()

    def test_crashed_worker_keeps_its_worktree_with_its_work(self):
        repo = self._setup()
        path = self.worktree(repo)
        spawn_record("w", path, repo)
        real_pid_file("w", dead_pid())
        (path / "note.txt").write_text("unsaved")
        rows = {r["path"]: r for r in workspaces.listing(self._scan(), sizes=False, now=time.time() + 90 * 86400)}
        self.assertEqual(rows[str(path)]["state"], "crashed")
        self.assertEqual(workspaces.clean(apply=True, now=time.time() + 90 * 86400, scan=self._scan()), [])
        self.assertTrue((path / "note.txt").exists())

    def test_doctors_check_finds_a_crashed_worker_from_the_ledger_and_never_runs_git(self):
        """Its pass took 7 minutes: it ran ~8 git calls on each of 262 worktrees to report what the ledger and ps already say."""
        self.write_config(worktree_root=str(self.home / "worktrees"), projects_dir=str(self.tmp / "projects"))
        for n in range(30):
            (self.home / "worktrees" / "proj" / f"other-{n}").mkdir(parents=True)
        crashed, running = self.home / "worktrees" / "proj" / "w", self.home / "worktrees" / "proj" / "r"
        crashed.mkdir()
        running.mkdir()
        spawn_record("w", crashed, self.tmp)
        spawn_record("r", running, self.tmp)
        spawn_record("gone", self.tmp / "no-such-worktree", self.tmp)
        spawn_record("plain", crashed, self.tmp, block=False)  # not one of ours to run: never crashed
        real_pid_file("w", dead_pid())
        real_pid_file("gone", dead_pid())
        real_pid_file("r", 4242)
        ps = self.tmp / "ps-alive"
        ps.write_text("#!/bin/sh\necho '4242 S claude --name r --settings x'\n")
        ps.chmod(0o755)
        os.environ["ATHENA_PS"] = str(ps)
        no = AssertionError("the check ran git or scanned every worktree")
        with mock.patch.object(gitops, "unsaved", side_effect=no), mock.patch.object(gitops, "ignored_work", side_effect=no), \
                mock.patch.object(workspaces, "Scan", side_effect=no), mock.patch.object(gitops, "git", side_effect=no):
            found = workspaces.checks(caps.CTX)
        self.assertEqual([(c.name, c.ok, c.detail) for c in found], [("workspaces", True, "1 crashed workers hold worktrees")])
        real_pid_file("w", 4242)
        ps.write_text("#!/bin/sh\necho '4242 S claude --name r --settings x'\necho '4243 S claude --name w --settings x'\n")
        real_pid_file("w", 4243)
        self.assertEqual(workspaces.checks(caps.CTX)[0].detail, "no strays")

    def test_retired_worker_that_held_unsaved_work_is_never_cleaned(self):
        repo = self._setup()
        path = self.worktree(repo)
        spawn_record("w", path, repo)
        real_pid_file("w", dead_pid())
        retire.retire(caps.CTX, "w", keep_worktree=True)
        (path / "note.txt").write_text("unsaved")
        later = time.time() + 90 * 86400
        row = next(r for r in workspaces.listing(self._scan(), sizes=False, now=later) if r["path"] == str(path))
        self.assertEqual(row["state"], "held")
        self.assertIn("uncommitted", row["why"])
        self.assertEqual(workspaces.clean(apply=True, now=later, scan=self._scan()), [])
        self.assertTrue((path / "note.txt").exists())

    def test_retired_and_clean_goes_with_its_head_kept(self):
        repo = self._setup()
        path = self.worktree(repo)
        spawn_record("w", path, repo)
        real_pid_file("w", dead_pid())
        retire.retire(caps.CTX, "w", keep_worktree=True)
        later = time.time() + 90 * 86400
        dry = workspaces.clean(apply=False, now=later, scan=self._scan())
        self.assertEqual(dry[0]["action"], "would remove")
        self.assertTrue(path.exists())
        out = workspaces.clean(apply=True, now=later, scan=self._scan())
        self.assertIn("removed", out[0]["action"])
        self.assertFalse(path.exists())
        self.assertTrue(gitops.backups(repo, "w"))

    def test_work_appearing_between_look_and_removal_keeps_it(self):
        repo = self._setup()
        path = self.worktree(repo)
        (path / "late.txt").write_text("x")
        self.assertIn("kept", workspaces._remove({"path": str(path), "kind": "worktree", "owner": "w"}))
        self.assertTrue(path.exists())

    def test_in_the_lab_cleanup_stays_out_of_the_real_home(self):
        os.environ["HOME"] = lab.real_home()
        self.assertFalse(workspaces._may_touch(os.path.join(lab.real_home(), "Developer", "x")))
        self.assertTrue(workspaces._may_touch(self.state / "scratch" / "x"))

    def test_scratch_has_an_owner_and_a_ttl_and_is_listed(self):
        self._setup()
        path = workspaces.new_scratch("probe", "w", ttl_days=3, note="why")
        row = next(r for r in workspaces.listing(self._scan(), sizes=False) if r["path"] == str(path))
        self.assertEqual((row["kind"], row["owner"], row["state"]), ("scratch", "w", "active"))
        later = time.time() + 30 * 86400
        out = workspaces.clean(apply=True, now=later, scan=self._scan())
        self.assertIn("moved to", out[0]["action"])
        self.assertFalse(path.exists())

    def test_a_scratchpad_in_use_by_a_claude_not_in_the_ledger_stays(self):
        self._setup()
        pads = self.tmp / "pads"
        cwd = str(self.tmp / "someones" / "project")
        pad = pads / workspaces.encode_cwd(cwd)
        pad.mkdir(parents=True)
        sessions = self.home / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "1.json").write_text(json.dumps({"pid": os.getpid(), "cwd": cwd}))
        later = time.time() + 90 * 86400
        row = next(r for r in workspaces.listing(workspaces.Scan(tmp_root=pads), sizes=False, now=later) if r["path"] == str(pad))
        self.assertEqual((row["state"], row["removable"]), ("active", False))

    def test_a_scratchpad_written_deep_inside_is_not_old(self):
        pad = self.tmp / "pad"
        (pad / "session" / "scratchpad").mkdir(parents=True)
        old = time.time() - 90 * 86400
        os.utime(pad, (old, old))
        (pad / "session" / "scratchpad" / "fresh").write_text("x")
        self.assertGreater(workspaces._newest(pad), time.time() - 60)

    def test_a_claude_scratchpad_finds_its_owner(self):
        repo = self._setup()
        path = self.worktree(repo)
        spawn_record("w", path, repo)
        pads = self.tmp / "pads"
        (pads / workspaces.encode_cwd(str(path))).mkdir(parents=True)
        (pads / "-somewhere-else").mkdir()
        rows = workspaces.Scan(tmp_root=pads).scratchpads()
        self.assertEqual(sorted(str(r["owner"]) for r in rows), ["None", "w"])

    def test_state_leftovers_are_only_ever_reported(self):
        self._setup()
        (self.state / "drafts").mkdir()
        later = time.time() + 90 * 86400
        rows = workspaces.listing(self._scan(), sizes=False, now=later)
        self.assertFalse(next(r for r in rows if r["path"].endswith("drafts"))["removable"])
