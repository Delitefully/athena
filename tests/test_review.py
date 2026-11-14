"""The release review's holes, one test each: docs/release-review.md names the finding each one holds."""
import json
import os
import re
import time
from unittest import mock

from harness import Case, store

from athena.caps.dash import markdown


class DashMarkdown(Case):
    """An item body is anyone's text: no link in it may add an attribute, a handler or a script to the page."""

    def links(self, page):
        return re.findall(r"<a\s[^>]*>", page)

    def assert_inert(self, page):
        for tag in self.links(page):
            attrs = re.findall(r'\s([a-z-]+)=', tag)
            self.assertTrue(set(attrs) <= {"href", "rel", "title"}, tag)
            self.assertNotRegex(tag, r"(?i)href=\"\s*javascript:", tag)
        self.assertNotRegex(page, r"(?i)<a\s[^>]*\son[a-z]+=", page)

    def test_a_quote_in_a_bare_link_cannot_leave_its_attribute(self):
        self.assert_inert(markdown.render('see https://x.test/"onmouseover="alert(1)"style="position:fixed'))

    def test_a_quote_in_a_named_link_stays_in_its_href(self):
        self.assert_inert(markdown.render('[x](https://x.test/"onfocus="y)'))

    def test_a_bare_url_inside_a_named_link_is_not_linked_again(self):
        """HQ's case: an autolink rendered inside the <a> a named link already wrote put a handler outside its quotes."""
        page = markdown.render("[t](https://a/https://x/onmouseover=location=/javascript:alert%281%29/.source+)")
        self.assert_inert(page)
        self.assertEqual(len(self.links(page)), 1, page)

    def test_links_still_link(self):
        page = markdown.render("[docs](https://x.test/a?b=1&c=2) and https://y.test/p?q=1&r=2")
        self.assertIn('href="https://x.test/a?b=1&amp;c=2"', page)
        self.assertIn('href="https://y.test/p?q=1&amp;r=2"', page)
        self.assertEqual(len(self.links(page)), 2, page)

class PsThatFails(Case):
    def test_a_ps_that_fails_is_nobody_can_say_never_none_running(self):
        from athena.kernel import agents
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")
        for script, unknown in (("echo 'ps: fork: Resource temporarily unavailable' >&2; exit 1", True), ("exit 2", True),
                                ("exit 1", False)):
            ps = self.tmp / "ps"
            ps.write_text(f"#!/bin/sh\n{script}\n")
            ps.chmod(0o755)
            os.environ["ATHENA_PS"] = str(ps)
            if unknown:
                with self.assertRaises(agents.Unknown, msg=script):
                    agents.stat_of("w")
            else:
                self.assertIsNone(agents.stat_of("w"), "ps -p of a pid that isn't running exits 1 and says nothing")


class RexAnswers(Case):
    def _backend(self, answer):
        from athena.backends.rex import Backend
        from athena.kernel.backend import BackendError

        def run(backend, *args, **kw):
            if isinstance(answer, BackendError):
                raise answer
            return answer
        patch = mock.patch.object(Backend, "_run", run)
        patch.start()
        self.addCleanup(patch.stop)
        return Backend()

    def test_an_answer_that_is_not_json_or_lacks_its_block_is_a_backend_failure(self):
        from athena.kernel.backend import BackendError, Place
        for answer in ("not json", "[]", '{"session_id": "s"}', '{"initial_windows": []}'):
            with self.assertRaises(BackendError, msg=answer) as caught:
                self._backend(answer).create("w", str(self.tmp), ["true"])
            self.assertEqual(caught.exception.code, "bad_reply")
        with self.assertRaises(BackendError):
            self._backend("{}").new_window("s", str(self.tmp), ["true"], "w")
        with self.assertRaises(BackendError):
            self._backend("{}").split(Place("s", "b"), str(self.tmp), ["true"], "w")

    def test_strict_has_block_is_false_only_when_rex_says_not_found(self):
        from athena.kernel.backend import BackendError, Place
        self.assertFalse(self._backend(BackendError("not_found", "no session")).has_block(Place("s", "b"), strict=True))
        for code in ("timeout", "exit_1", "server_down"):
            with self.assertRaises(BackendError, msg=code):
                self._backend(BackendError(code, "x")).has_block(Place("s", "b"), strict=True)
        with self.assertRaises(BackendError):
            self._backend("garbled").has_block(Place("s", "b"), strict=True)

    def test_retire_asks_strictly_so_a_slow_rex_never_skips_its_close(self):
        import inspect
        from athena.caps import retire
        self.assertIn("has_block(agents.place_of(record), strict=True)", inspect.getsource(retire._stop))


class GitThatFails(Case):
    def test_an_ahead_count_git_cannot_give_is_unknown_never_nothing_unpushed(self):
        from athena.kernel import gitops
        repo = self.repo()
        real = gitops.git

        def failing(cwd, *args, **kw):
            if args[:2] == ("rev-list", "--count"):  # as git() does with a failing git: raise when checked, else ""
                if kw.get("check", True):
                    raise gitops.GitError("git rev-list: fatal: bad object")
                return ""
            return real(cwd, *args, **kw)
        with mock.patch.object(gitops, "git", failing):
            info = gitops.unsaved(repo)
        self.assertTrue(info["unknown"])
        self.assertTrue(gitops.holds_work(info), "a worktree git can't vouch for is kept")

    def test_a_git_that_hangs_is_no_answer_not_a_no(self):
        from athena.kernel import gitops
        hang = self.stubs / "git"
        hang.write_text("#!/bin/sh\nsleep 30\n")
        hang.chmod(0o755)
        with self.assertRaises(gitops.GitError):
            gitops.ok(self.tmp, "rev-parse", "--verify", "HEAD", timeout=1)
        with self.assertRaises(gitops.GitError):
            gitops.git(self.tmp, "status", timeout=1)

    def test_git_is_never_left_to_prompt_for_credentials(self):
        from athena.kernel import gitops
        self.assertEqual(gitops._ENV["GIT_TERMINAL_PROMPT"], "0")


class SignalThroughRex(Case):
    def test_a_signal_rex_cannot_send_is_an_oserror_every_caller_handles(self):
        import errno
        from athena.kernel import sandbox
        from athena.kernel.backend import BackendError
        refused = PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(sandbox, "inside", return_value=True), mock.patch("os.kill", side_effect=refused), \
                mock.patch.object(sandbox, "_through_rex", side_effect=BackendError("server_down", "no server")):
            with self.assertRaises(OSError) as caught:
                sandbox.kill(4242, 15)
        self.assertIn("Rex could not send it", str(caught.exception))


class SpawnInterrupted(Case):
    def test_ctrl_c_mid_launch_leaves_no_live_worker_and_no_worktree(self):
        from athena.caps import spawn
        from athena.kernel import agents, caps, ledger
        from harness import git
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        with mock.patch.object(agents, "start", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", fetch=False)
        self.assertEqual(ledger.get("a")["state"], "failed", "it no longer counts as live against the cap")
        self.assertTrue(ledger.get("a")["cleaned"])
        self.assertEqual(len(git(repo, "worktree", "list").splitlines()), 1)

    def test_a_brief_that_will_not_decode_fails_the_launch_cleanly(self):
        from athena.caps import spawn
        from athena.cli import UserError
        from athena.kernel import caps, ledger
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        brief = self.tmp / "brief.md"
        brief.write_bytes(b"\xff\xfe not utf-8 \x80")
        with self.assertRaises(UserError) as caught:
            spawn.spawn(caps.CTX, repo, "t/b", "b", prompt="hi", fetch=False, brief_path=str(brief))
        self.assertIn("can't be read as text", str(caught.exception))
        self.assertIsNone(ledger.get("b"), "refused before anything was made")


class HqRecordRace(Case):
    def test_doctor_never_writes_an_old_place_back_over_a_new_launch(self):
        from athena.caps import hq
        old = {"space": "s", "block": "b-old", "window": "w", "session": None}
        hq._save({**old, "space": "s", "block": "b-new"})  # a launch recorded its new place after doctor read the old one
        hq._amend(old, session="conv")
        self.assertEqual(hq.load()["block"], "b-new")
        self.assertIsNone(hq.load().get("session"), "nothing written for a place no longer on record")
        hq._amend({"space": "s", "block": "b-new"}, session="conv")
        self.assertEqual((hq.load()["block"], hq.load()["session"]), ("b-new", "conv"))


class HqDoctorPass(Case):
    def test_athena_hq_runs_no_doctor_pass_beside_another(self):
        from athena.caps import doctor, hq
        from harness import store
        args = mock.Mock(no_doctor=False, no_dash=True, no_board=True)
        with store.locked("doctor.lock"), mock.patch.object(doctor, "run") as ran:
            hq._around_hq(args)
        ran.assert_not_called()


class DefaultsTwice(Case):
    def test_a_default_another_process_applied_first_skips_and_the_rest_still_apply(self):
        from athena.caps import items
        from athena.cli import UserError
        first = items.add("One?", options=["a"], rec="a", auto=True)
        items.add("Two?", options=["a"], rec="a", auto=True)
        real = items.answer

        def raced(item_id, **kw):
            if item_id == first["id"]:
                raise UserError(f"{item_id} is already answered")
            return real(item_id, **kw)
        with mock.patch.object(items, "answer", raced):
            done = items.apply_defaults(now=time.time() + 13 * 3600)
        self.assertEqual(len(done), 1)


class SmallHoles(Case):
    def test_a_stale_lock_that_will_not_go_waits_out_the_deadline_never_spins(self):
        from athena.caps import trust
        lock = self.tmp / "claude.json.lock"
        lock.write_text("a file, so rmdir fails with something other than gone")
        os.utime(lock, (1, 1))
        started = time.monotonic()
        with self.assertRaises(trust.TrustError):
            trust._acquire(str(lock), wait=0.5)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_damaged_posted_ts_is_written_over_not_the_end_of_the_poll(self):
        from athena.caps import slack
        from harness import store
        store.write_json(store.path("open-prs-posted.json"), {"o/r#1": "not a number"})
        slack._record_posts({"text": "https://github.com/o/r/pull/1", "ts": "1700000000.1"})
        self.assertEqual(store.read_dict(store.path("open-prs-posted.json"))["o/r#1"], "1700000000.1")

    def test_the_dash_refuses_a_body_length_it_would_hang_or_desync_on(self):
        import socket
        from athena.caps.dash import server
        dash, httpd = server.start(store.state_dir(), 0)
        self.addCleanup(httpd.server_close)
        self.addCleanup(dash.stop)
        self.addCleanup(httpd.shutdown)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = dash.port
        for length in ("-5", str(server.BODY_MAX + 1), "x"):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall((f"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
                              f"Content-Type: application/json\r\nX-Athena-Token: {dash.token}\r\nContent-Length: {length}\r\n\r\n{{}}").encode())
                self.assertIn(b" 400 ", conn.recv(200), length)

    def test_a_launch_script_is_never_readable_by_others(self):
        from athena.kernel import agents
        with mock.patch("os.umask", return_value=0o022):
            path = agents.launcher("w", self.tmp, ["--name", "w"], {"TOKEN": "x"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.name, "w.sh", "the revive module reads run/<name>.sh")


class SecondHq(Case):
    def test_a_second_hq_says_how_to_end_it_for_good(self):
        from athena.caps import hq
        from athena.kernel import caps
        script = self.tmp / "ps"
        script.write_text("#!/bin/sh\necho '4242 /x/claude --resume c --name athena --append-system-prompt-file /x/hq.md'\n")
        script.chmod(0o755)
        os.environ["ATHENA_PS"] = str(script)
        hq._save({"session": "conv", "workspace": "w7", "pane": "p3"})
        found = next(c for c in hq.checks(caps.CTX) if c.name == "hq-second")
        self.assertFalse(found.ok)
        self.assertIn("the workspace labelled athena", found.detail)
        self.assertNotIn("w7", found.detail, "herdr reuses ids after a restart: an id on record may name another workspace now")
        self.assertIn("session restore resumes it", found.detail)
        self.assertNotIn("`athena hq` starts these", found.detail, "the old advice pointed at the wrong command")


class SpawnBase(Case):
    """A worker builds on local main when it is a fast-forward ahead of origin's, as work merged here and not pushed yet is the
    base (the release prep once forked from origin/main, 52 commits behind local main)."""

    def test_local_main_strictly_ahead_is_the_base(self):
        from athena.kernel import gitops
        from harness import git
        repo = self.repo()
        git(repo, "commit", "-q", "--allow-empty", "-m", "merged here, not pushed")
        self.assertEqual(gitops.new_branch_base(repo), "main")
        path = self.tmp / "wt-ahead"
        gitops.add_worktree(repo, "t/ahead", path)
        self.assertEqual(git(path, "log", "-1", "--format=%s"), "merged here, not pushed")

    def test_a_worker_cut_from_local_main_holds_only_its_own_commits(self):
        """Local main's unpushed commits are on local main: a worker that made none holds no work, so retire and REX-CLOSE go on."""
        from athena.kernel import gitops
        from harness import git
        repo = self.repo()
        for n in range(3):
            git(repo, "commit", "-q", "--allow-empty", "-m", f"merged here {n}")
        path = self.tmp / "wt-own"
        gitops.add_worktree(repo, "t/own", path)
        info = gitops.unsaved(path)
        self.assertEqual(info["ahead"], 0)
        self.assertFalse(gitops.holds_work(info))
        git(path, "commit", "-q", "--allow-empty", "-m", "the worker's own")
        self.assertEqual(gitops.unsaved(path)["ahead"], 1)

    def test_origin_is_the_base_when_local_main_is_level_behind_or_diverged(self):
        from athena.kernel import gitops
        from harness import git
        repo = self.repo()
        self.assertEqual(gitops.new_branch_base(repo), "origin/main", "level")
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(repo.parent / "origin.git"), str(other))
        git(other, "commit", "-q", "--allow-empty", "-m", "on origin")
        git(other, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q", "origin")
        self.assertEqual(gitops.new_branch_base(repo), "origin/main", "behind")
        git(repo, "commit", "-q", "--allow-empty", "-m", "local only")
        self.assertEqual(gitops.new_branch_base(repo), "origin/main", "diverged")

    def test_a_preflight_git_cannot_answer_is_a_clean_refusal(self):
        from athena.caps import spawn
        from athena.cli import UserError
        from athena.kernel import caps, gitops
        repo = self.repo()
        with mock.patch.object(gitops, "preflight", side_effect=gitops.GitError("git rev-parse: no answer (TimeoutExpired)")):
            with self.assertRaises(UserError) as caught:
                spawn.spawn(caps.CTX, repo, "t/x", "x", prompt="hi", fetch=False)
        self.assertIn("preflight failed", str(caught.exception))
