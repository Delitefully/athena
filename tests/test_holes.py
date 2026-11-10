"""Regressions for what the review rounds found, mixed with positive controls that keep each fix from over-reaching. Not every
test here failed on the old code, and a test's name doesn't say which: that was measured by running them against old code.

Nothing here reaches anything real. Paths named under /Volumes do not exist, and only metadata of /Volumes itself is read.
The one real program run is `gh --version`, to show the sandbox stops it: on failure it would print a version and no more.
"""
import ast
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from harness import BIN, Case, ROOT, SANDBOX, card_writer, git, spawn_record, store

from athena_rex.caps import hq, retire, spawn, status, trust, workspaces
from athena_rex.cli import UserError
from athena_rex.kernel import agents, canary, caps, config, gitops, lab, ledger, world

NO_SUCH_CARD = "/Volumes/ar-no-such-card"


def _import_harness(**env):
    code = f"import sys; sys.path.insert(0, {str(ROOT / 'tests')!r}); import harness"
    full = {k: v for k, v in os.environ.items() if not k.startswith("ATHENA_")}
    return subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code], capture_output=True, text=True, env={**full, **env})


class SandboxCheck(Case):
    """Finding 1: any OSError on the probe passed for 'inside the sandbox'."""

    def test_a_probe_that_fails_for_another_reason_is_no_sandbox(self):
        for probe in ("/nonexistent/x", "/etc/ar-probe", str(self.tmp / "no-such-folder" / "x")):
            out = _import_harness(ATHENA_SANDBOX_PROBE_WRITE=probe)
            self.assertNotEqual(out.returncode, 0, f"{probe} passed for a sandbox")
            self.assertIn("scripts/test.sh", out.stderr + out.stdout)

    def test_no_probe_at_all_is_no_sandbox(self):
        self.assertNotEqual(_import_harness().returncode, 0)

    def test_the_real_probe_passes(self):
        out = _import_harness(ATHENA_SANDBOX_PROBE_WRITE=SANDBOX["PROBE_WRITE"])
        self.assertEqual(out.returncode, 0, out.stderr)


class Card(Case):
    """Finding 2: the door compared spellings, on a disk that ignores case, and never looked inside a fake folder."""

    def test_a_real_place_in_another_case_is_still_real(self):
        for spelling in ("/volumes", "/VOLUMES", "/vOlUmEs/"):
            os.environ["ATHENA_CARD_DIR"] = spelling
            self.assertIsNone(world.card_root(), spelling)

    def test_a_folder_that_links_to_a_real_place_is_real(self):
        link = self.tmp / "looks-fake"
        link.symlink_to("/Volumes")
        os.environ["ATHENA_CARD_DIR"] = str(link)
        self.assertIsNone(world.card_root())

    def test_a_card_in_a_fake_folder_that_links_to_a_real_one_is_refused(self):
        (self.tmp / "cards" / "ExampleCard").symlink_to(NO_SUCH_CARD)
        self.assertFalse(world.may_write_card(self.tmp / "cards" / "ExampleCard"))
        self.assertFalse(card_writer(self).write())

    def test_a_program_file_that_links_out_of_a_fake_card_is_not_written(self):
        card = self.tmp / "cards" / "ExampleCard"
        card.mkdir()
        (card / "PROGRAM.TXT").symlink_to(f"{NO_SUCH_CARD}/PROGRAM.TXT")
        self.assertFalse(card_writer(self).write())

    def test_a_stub_that_links_to_a_real_tool_is_no_stub(self):
        (self.stubs / "security").unlink()
        (self.stubs / "security").symlink_to("/usr/bin/security")
        with self.assertRaises(world.Off):
            world.resolve("security")

    def test_a_stub_folder_in_another_case_is_refused(self):
        os.environ["ATHENA_WORLD_STUBS"] = "/USR/BIN"
        with self.assertRaises(world.Off):
            world.resolve("security")

    def test_a_fake_card_still_works(self):
        (self.tmp / "cards" / "ExampleCard").mkdir()
        self.assertTrue(card_writer(self).write())

    def test_live_state_in_another_case_is_still_live(self):
        for live in lab.LIVE_PATHS[:2]:
            for spelling in (live.upper(), live.lower(), live.swapcase()):
                self.assertTrue(lab.is_live(spelling), spelling)
                self.assertTrue(lab.is_live(os.path.join(spelling, "workers")), spelling)

    def test_a_link_to_live_state_is_live(self):
        link = self.tmp / "looks-scratch"
        link.symlink_to(lab.LIVE_PATHS[0])
        self.assertTrue(lab.is_live(link))

    def test_a_different_folder_with_a_similar_name_is_not_live(self):
        self.assertFalse(lab.is_live(lab.LIVE_PATHS[0] + "-other"))

    def test_the_shell_guards_see_through_case_too(self):
        pose = self.home / ".local" / "state" / "athena"
        pose.mkdir(parents=True)
        spelled = str(pose).upper()
        out = subprocess.run([str(BIN), "status"], capture_output=True, text=True, env={**os.environ, "ATHENA_STATE_DIR": spelled})
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)

    def _canary_trips(self, path):
        saved = canary._State.log
        canary._State.log = str(self.tmp / "direct.log")
        try:
            with self.assertRaises(canary.CanaryError, msg=str(path)):
                canary._check_path("open", path)
        finally:
            canary._State.log = saved

    def test_the_canary_sees_through_case_and_links(self):
        self._canary_trips("/volumes/ar-no-such-card/PROGRAM.TXT")
        self._canary_trips(lab.LIVE_PATHS[0].upper() + "/ledger.jsonl")
        link = self.tmp / "link"
        link.symlink_to("/Volumes")
        self._canary_trips(link / "ar-no-such-card" / "PROGRAM.TXT")


class ClaudeConfig(Case):
    """Finding 3: only one exact spelling of the real file counted as the real file."""

    def _skipped(self):
        self.assertTrue(trust.ensure(self.tmp).get("skipped"), f"would have written {trust.config_path()}")

    def test_the_real_config_dir_needs_the_door(self):
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(lab.real_home(), ".claude")
        self._skipped()

    def test_the_real_home_in_another_case_needs_the_door(self):
        os.environ["HOME"] = lab.real_home().swapcase()
        self._skipped()

    def test_any_folder_under_the_real_home_needs_the_door(self):
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(lab.real_home(), ".claude-other")
        self._skipped()

    def test_an_isolated_home_is_written(self):
        self.assertTrue(trust.ensure(self.tmp)["wrote"])

    def test_smoke_clears_the_variable(self):
        self.assertIn("CLAUDE_CONFIG_DIR", " ".join(line for line in _smoke_lines() if line.startswith("unset ")))


def _smoke_lines() -> list:
    return (ROOT / "scripts" / "smoke.sh").read_text().splitlines()


class LiveConfig(Case):
    """Finding 4: ATHENA_CONFIG went round the state guard, and spawn runs setup scripts from beside that file."""

    def test_a_live_config_is_refused_like_live_state(self):
        for live in lab.LIVE_PATHS[:2]:
            target = os.path.join(live, "config.json")
            out = self.cli("status", env={"ATHENA_CONFIG": target})
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("refusing", out.stderr)
            self.assertEqual(self.cli("hook", env={"ATHENA_CONFIG": target}).returncode, 0)

    def test_a_scratch_config_elsewhere_still_works(self):
        other = self.tmp / "other.json"
        other.write_text('{"model": "haiku"}')
        os.environ["ATHENA_CONFIG"] = str(other)
        config.reset()
        self.assertEqual(config.get("model"), "haiku")


class SandboxTools(Case):
    """Finding 5: Seatbelt matches a resolved path, and the rule named the symlink."""

    def test_real_gh_cannot_run_in_a_shell_child(self):
        for path in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
            if os.path.lexists(path):
                out = _shell(self, f'"{path}" --version')
                self.assertNotEqual(out.returncode, 0, f"{path} ran: {out.stdout[:60]}")
                self.assertNotIn("gh version", out.stdout)

    def test_every_tool_rule_names_its_resolved_path(self):
        profile = Path(SANDBOX["PROFILE"]).read_text()
        for tool in ("/usr/bin/security", "/usr/bin/osascript", "/usr/bin/pmset", "/usr/sbin/ioreg", "/usr/bin/caffeinate",
                     "/bin/launchctl", "/usr/bin/open", "/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
            if os.path.lexists(tool):
                self.assertIn(f'"{os.path.realpath(tool)}"', profile, tool)


def _shell(case, body):
    path = case.tmp / "child.sh"
    path.write_text(f"#!/bin/sh\n{body}\n")
    return subprocess.run(["sh", str(path)], capture_output=True, text=True)


class SmokeGuard(Case):
    """Finding 6: smoke turned the lab guard off for two commands that don't need it."""

    def test_smoke_never_clears_the_worker_name(self):
        for line in _smoke_lines():
            self.assertNotIn("ATHENA_WORKER=", line.replace('"$ATHENA_WORKER"', ""), line)


class RexServer(Case):
    """Finding 7: naming a server skipped the door, so the human's own endpoint could be named."""

    def _backend(self):
        from athena_rex.backends import rex
        fake = self.tmp / "rex"
        fake.write_text("#!/bin/sh\nexit 4\n")
        fake.chmod(0o755)
        os.environ["ATHENA_REX_BIN"] = str(fake)
        config.reset()
        return rex.Backend()

    def test_only_a_private_endpoint_is_open_with_the_door_shut(self):
        home = lab.real_home()
        for server in (f"unix://{home}/Library/Application Support/rex/server.sock", "devbox", "unix:///tmp/other.sock",
                       f"unix:///private/tmp/claude-{os.getuid()}/../../etc/s.sock", "127.0.0.1:7777"):
            os.environ["ATHENA_REX_SERVER"] = server
            with self.assertRaises(world.Off, msg=server):
                self._backend().up()

    def test_a_config_cannot_name_one_either(self):
        self.write_config(rex={"server": "devbox"})
        with self.assertRaises(world.Off):
            self._backend().up()

    def test_a_private_endpoint_works(self):
        folder = Path(tempfile.mkdtemp(prefix="o", dir=SANDBOX["SOCKETS"]))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / world.SERVER_MARK).write_text(str(store.state_dir()))
        os.environ["ATHENA_REX_SERVER"] = f"unix://{folder}/s.sock"
        self.assertFalse(self._backend().up())

    def test_ps_is_a_stub_under_test_and_records_its_calls(self):
        spawn_record("w", self.tmp, self.tmp)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")
        status.collect()
        self.assertTrue(self.ps_calls(), "ps ran and left no record")


class GitOutput(Case):
    def test_the_first_status_line_keeps_its_path(self):
        repo = self.repo()
        path = self.worktree(repo)
        (path / "tracked.txt").write_text("a")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "x")
        git(path, "push", "-q")
        (path / "tracked.txt").write_text("b")
        self.assertEqual(gitops.unsaved(path)["dirty"], ["tracked.txt"])


class _Repos(Case):
    def _worker(self):
        repo = self.repo()
        path = self.worktree(repo)
        (path / "src").mkdir()
        (path / "src" / "foo.py").write_text("x")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "tracked")
        git(path, "push", "-q")
        spawn_record("w", path, repo)
        return repo, path

    def _embed(self, path, name="foo"):
        inner = path / name
        inner.mkdir()
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        git(inner, "commit", "-q", "--allow-empty", "-m", "only here")
        return inner

    def _sub(self, path, name="sub", pushed=False, dirty_elsewhere=True):
        """A tracked submodule, its commit on a remote if `pushed`."""
        inner = self._embed(path, name)
        if pushed:
            origin = self.tmp / f"{name}-origin.git"
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            git(inner, "remote", "add", "origin", str(origin))
            git(inner, "push", "-q", "-u", "origin", "HEAD")
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{git(inner, 'rev-parse', 'HEAD')},{name}")
        git(path, "commit", "-q", "-m", f"submodule {name}")
        git(path, "push", "-q")
        if dirty_elsewhere:
            (path / "other.txt").write_text("makes a forced retire remove with --force")
        return inner

    def _refused(self, inner):
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(inner.exists())
        self.assertEqual(ledger.get("w")["state"], "live")


class EmbeddedRepo(_Repos):
    """Round 1, finding 8: a substring test let an embedded repo through if its name appeared in any tracked path."""

    def test_a_name_that_appears_in_a_tracked_path_is_still_refused(self):
        _, path = self._worker()
        inner = self._embed(path)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue((inner / ".git").exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_a_tracked_submodule_with_changes_inside_is_refused(self):
        _, path = self._worker()
        inner = self._embed(path, "sub")
        sha = git(inner, "rev-parse", "HEAD")
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
        git(path, "commit", "-q", "-m", "submodule")
        git(path, "push", "-q")
        (inner / "changed.txt").write_text("only here")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue((inner / "changed.txt").exists())


class SubmoduleCommits(_Repos):
    """Round 2, finding 1: only the submodule's HEAD counted, and a failed check read as nothing to lose."""

    def test_a_clean_submodule_with_unpushed_commits_is_refused(self):
        _, path = self._worker()
        self._refused(self._sub(path))

    def test_head_pushed_but_another_branch_not_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        git(inner, "branch", "side")
        git(inner, "checkout", "-q", "side")
        git(inner, "commit", "-q", "--allow-empty", "-m", "only on this branch")
        git(inner, "checkout", "-q", "-")
        self._refused(inner)

    def test_a_stash_in_a_submodule_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        (inner / "tracked.txt").write_text("a")
        git(inner, "add", "-A")
        git(inner, "commit", "-q", "-m", "tracked")
        git(inner, "push", "-q")
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{git(inner, 'rev-parse', 'HEAD')},sub")
        git(path, "commit", "-q", "-m", "bump")
        git(path, "push", "-q")
        (inner / "tracked.txt").write_text("b")
        git(inner, "stash", "push", "-q")
        self._refused(inner)

    def test_a_nested_submodule_with_its_own_commits_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        deeper = inner / "deeper"
        deeper.mkdir()
        subprocess.run(["git", "init", "-q", str(deeper)], check=True)
        git(deeper, "commit", "-q", "--allow-empty", "-m", "only here")
        git(inner, "update-index", "--add", "--cacheinfo", f"160000,{git(deeper, 'rev-parse', 'HEAD')},deeper")
        git(inner, "commit", "-q", "-m", "nested")
        git(inner, "push", "-q")
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{git(inner, 'rev-parse', 'HEAD')},sub")
        git(path, "commit", "-q", "-m", "bump")
        git(path, "push", "-q")
        self._refused(inner)

    def test_ignored_work_in_a_submodule_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        (inner / ".git" / "info" / "exclude").write_text(".env\n")
        (inner / ".env").write_text("only here")
        self._refused(inner)

    def test_a_submodule_git_cannot_read_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        shutil.rmtree(inner / ".git")
        (inner / ".git").write_text("gitdir: /nonexistent/modules/sub\n")
        self._refused(inner)

    def test_a_changed_submodule_with_a_space_in_its_name_is_refused(self):
        _, path = self._worker()
        inner = self._sub(path, "my sub", pushed=True)
        (inner / "changed.txt").write_text("only here")
        self._refused(inner)

    def test_a_submodule_with_nothing_of_its_own_goes(self):
        repo, path = self._worker()
        self._sub(path, pushed=True)
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(repo, "show", f"{out['wip_backup']}:other.txt"), "makes a forced retire remove with --force")
        self.assertFalse(path.exists())

    def test_status_paths_with_spaces_come_back_whole(self):
        _, path = self._worker()
        (path / "a b.txt").write_text("x")
        self.assertIn("a b.txt", gitops.unsaved(path)["dirty"])

    def test_a_renamed_file_is_reported_once_by_its_new_name(self):
        _, path = self._worker()
        git(path, "mv", "src/foo.py", "src/bar baz.py")
        self.assertEqual(gitops.unsaved(path)["dirty"], ["src/bar baz.py"])


class QuotedNames(_Repos):
    """Round 3, finding 1: git C-quotes names without -z, so a submodule named like this never matched and looked empty."""

    def test_a_submodule_with_a_quoted_name_is_checked(self):
        for name in ("caf\u00e9", 'quo"te', "back\\slash"):
            with self.subTest(name=name):
                self.setUp_again()
                _, path = self._worker()
                inner = self._sub(path, name)
                self._refused(inner)

    def setUp_again(self):
        for leftover in ("repos", "home/worktrees"):
            shutil.rmtree(self.tmp / leftover, ignore_errors=True)
        store.path("ledger.jsonl").unlink(missing_ok=True)

    def test_a_nested_submodule_with_a_quoted_name_is_checked(self):
        _, path = self._worker()
        inner = self._sub(path, pushed=True)
        deeper = inner / "caf\u00e9"
        deeper.mkdir()
        subprocess.run(["git", "init", "-q", str(deeper)], check=True)
        git(deeper, "commit", "-q", "--allow-empty", "-m", "only here")
        git(inner, "update-index", "--add", "--cacheinfo", f"160000,{git(deeper, 'rev-parse', 'HEAD')},caf\u00e9")
        git(inner, "commit", "-q", "-m", "nested")
        git(inner, "push", "-q")
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{git(inner, 'rev-parse', 'HEAD')},sub")
        git(path, "commit", "-q", "-m", "bump")
        git(path, "push", "-q")
        self._refused(inner)

    def test_a_clean_submodule_with_a_quoted_name_goes(self):
        _, path = self._worker()
        self._sub(path, "caf\u00e9", pushed=True)
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_listed_link_whose_path_is_missing_holds(self):
        self.assertTrue(gitops._what_it_holds(self.tmp / "no-such-submodule"))


class CleanWorktreeForced(_Repos):
    """Round 3, finding 1: every other submodule test had a dirty file, so a forced retire of a clean worktree never ran."""

    def test_a_held_submodule_stops_it(self):
        _, path = self._worker()
        inner = self._sub(path, dirty_elsewhere=False)
        self._refused(inner)

    def test_a_submodule_with_nothing_of_its_own_goes(self):
        _, path = self._worker()
        self._sub(path, pushed=True, dirty_elsewhere=False)
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())
        self.assertEqual(ledger.get("w")["state"], "retired")

    def test_a_plain_retire_still_stays_tracked(self):
        _, path = self._worker()
        self._sub(path, pushed=True, dirty_elsewhere=False)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertTrue(path.exists())


class ScopedNames(Case):
    """Round 3, finding 2: these two names were disposable anywhere, and a hand-made settings.local.json can hold passwords."""

    def _tree(self, ignores):
        repo = self.repo()
        path = self.worktree(repo)
        (path / ".gitignore").write_text(ignores)
        return path

    def _write(self, path, relative):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

    def test_settings_local_outside_claude_is_work(self):
        path = self._tree("settings.local.json\n")
        self._write(path, "config/settings.local.json")
        self._write(path, "settings.local.json")
        self.assertEqual(sorted(gitops.ignored_work(path)), ["config/settings.local.json", "settings.local.json"])

    def test_cc_writes_outside_claude_is_work(self):
        path = self._tree(".cc-writes/\n")
        self._write(path, "notes/.cc-writes/mine.txt")
        self.assertEqual(gitops.ignored_work(path), ["notes/.cc-writes/mine.txt"])

    def test_under_claude_they_are_not(self):
        path = self._tree("settings.local.json\n.cc-writes/\n")
        self._write(path, ".claude/settings.local.json")
        self._write(path, ".claude/.cc-writes/a")
        self._write(path, "pkg/.claude/settings.local.json")
        self.assertEqual(gitops.ignored_work(path), [])


class ConfigPatterns(Case):
    """Round 3, finding 3: six probes can't prove a glob is safe, and an own-name match had no secret check."""

    def _held(self, ignores, name, pattern):
        repo = self.repo(f"p{abs(hash((name, str(pattern)))) % 100000}")
        path = self.tmp / "t" / repo.parent.name
        git(repo, "worktree", "add", "-q", "-b", repo.parent.name, str(path), "origin/main")
        (path / ".gitignore").write_text(ignores)
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        self.write_config(rex={"disposable": pattern if isinstance(pattern, list) else [pattern]})
        return gitops.ignored_work(path)

    def test_patterns_that_miss_the_old_probes_are_rejected(self):
        for pattern, name in ((".env.*", ".env.prod"), ("*.pem", "a.pem"), ("*.json", "a.json"), ("*.md", "n.md"), ("n*", "notes.txt"),
                              ("[!apx.]*", "notes.txt"), ("not?s.txt", "notes.txt"), ("*notes*", "notes.txt"), ("no*.txt", "notes.txt")):
            with self.subTest(pattern=pattern):
                self.assertEqual(self._held("*\n!.gitignore\n", name, pattern), [name])

    def test_a_config_can_never_make_a_secret_disposable(self):
        for pattern, name in (("prod.env", "prod.env"), (".envrc", ".envrc"), ("cred*", "credentials.json"), ("sec*", "secrets.yaml"),
                              ("secret.json", "secret.json")):
            with self.subTest(pattern=pattern):
                self.assertEqual(self._held("*\n!.gitignore\n", name, pattern), [name])

    def test_a_secret_in_a_folder_a_config_names_is_work(self):
        self.assertEqual(self._held("generated/\n", "generated/prod.env", "generated"), ["generated/prod.env"])

    def test_a_stem_cannot_make_a_secret_shaped_folder_disposable(self):
        self.assertEqual(self._held("secrets/\n", "secrets/api.txt", "sec*"), ["secrets/api.txt"])

    def test_a_secret_shaped_folder_inside_output_is_work(self):
        self.assertEqual(self._held("dist/\n", "dist/secrets/api.txt", []), ["dist/secrets/api.txt"])

    def test_a_plain_name_and_a_real_stem_still_work(self):
        self.assertEqual(self._held("*\n!.gitignore\n", "output.bin", "output.bin"), [])
        self.assertEqual(self._held("*\n!.gitignore\n", "generated-a.bin", "gen*"), [])

    def test_the_doc_no_longer_claims_probing(self):
        self.assertNotIn("can't be wildcards that would match", (ROOT / "docs" / "workspaces.md").read_text())


class SecretShapes(Case):
    """Round 3, finding 4: the secret test was case-sensitive on macOS, and missed common names."""

    def _in_dist(self, name):
        repo = self.repo(f"s{abs(hash(name)) % 100000}")
        path = self.tmp / "t" / repo.parent.name
        git(repo, "worktree", "add", "-q", "-b", repo.parent.name, str(path), "origin/main")
        (path / ".gitignore").write_text("dist/\n")
        (path / "dist").mkdir()
        (path / "dist" / name).write_text("x")
        return gitops.ignored_work(path)

    def test_case_does_not_hide_a_secret(self):
        for name in (".ENV", "Prod.PEM", "Credentials.json", "ID_RSA", "App.Sqlite"):
            with self.subTest(name=name):
                self.assertEqual(self._in_dist(name), [f"dist/{name}"])

    def test_the_names_it_used_to_miss(self):
        for name in ("secret.json", "client_secret.json", "client_secret_123.json", "a.env.local", ".envrc", ".npmrc", ".netrc", "x.tfstate",
                     "x.tfstate.backup"):
            with self.subTest(name=name):
                self.assertEqual(self._in_dist(name), [f"dist/{name}"])

    def test_a_trailing_dot_does_not_hide_a_secret(self):
        for name in ("x.pem.", ".env.", "id_rsa..", "a.key "):
            with self.subTest(name=name):
                self.assertEqual(self._in_dist(name), [f"dist/{name}"])

    def test_output_is_still_output(self):
        for name in ("app.js", "bundle.css", "keys.txt", "environment.js"):
            with self.subTest(name=name):
                self.assertEqual(self._in_dist(name), [])


class SmokeUsesTheRealGuard(Case):
    """Round 3, finding 5: smoke's own check trusted $HOME, and missed a dangling link and a `..` through a missing folder.
    Each run names a server that is refused next, so a guard that fails here stops there and reaches nothing."""

    def _refused(self, path):
        out = subprocess.run([str(ROOT / "scripts" / "smoke.sh"), "--state-dir", str(path), "--server", "unix:///nonexistent/s.sock"],
                             capture_output=True, text=True, timeout=20, cwd=str(self.tmp))
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refusing state dir", out.stderr, str(path))

    def test_the_real_live_paths_with_home_isolated(self):
        for live in lab.LIVE_PATHS:
            self._refused(live)
            self._refused(live.upper())
            self._refused(os.path.join(live, "workers"))

    def test_a_dotdot_through_a_missing_folder(self):
        state = os.path.dirname(lab.LIVE_PATHS[0])
        self._refused(os.path.join(state, "nope", "..", "athena", "new"))

    def test_a_dangling_link_into_live_state(self):
        link = self.tmp / "looks-scratch"
        link.symlink_to(os.path.join(lab.LIVE_PATHS[0], "no-such-thing"))
        self._refused(link)

    def test_a_parent_of_live_state(self):
        self._refused(os.path.dirname(lab.LIVE_PATHS[0]))
        self._refused("/")

    def test_a_guard_that_cannot_answer_refuses(self):
        copy = self.tmp / "broken"
        (copy / "scripts").mkdir(parents=True)
        (copy / "athena_rex" / "kernel").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "smoke.sh", copy / "scripts" / "smoke.sh")
        for package in (copy / "athena_rex", copy / "athena_rex" / "kernel"):
            (package / "__init__.py").write_text("")
        (copy / "athena_rex" / "kernel" / "lab.py").write_text("raise RuntimeError('this guard cannot answer')\n")
        out = subprocess.run([str(copy / "scripts" / "smoke.sh"), "--state-dir", str(self.state), "--server", "unix:///nonexistent/s.sock"],
                             capture_output=True, text=True, timeout=20, cwd=str(self.tmp))
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refusing state dir", out.stderr)


class WideHome(Case):
    """Round 3, finding 6: a HOME that holds the real home counted as isolated."""

    def _no(self):
        self.assertFalse(workspaces._may_touch(os.path.join(lab.real_home(), "Developer", "x")))
        self.assertFalse(workspaces._may_touch("/private/etc/x"))

    def test_root_empty_and_parents_are_not_isolated(self):
        for home in ("/", "", " ", os.path.dirname(lab.real_home()), lab.real_home(), lab.real_home().swapcase()):
            with self.subTest(home=home):
                os.environ["HOME"] = home
                self._no()

    def test_an_unset_home_is_not_isolated(self):
        os.environ.pop("HOME")
        self._no()

    def test_a_real_isolated_home_still_is(self):
        self.assertTrue(workspaces._may_touch(self.home / "x"))
        self.assertFalse(workspaces._may_touch(self.tmp / "beside-home"))

    def test_a_state_dir_holding_live_state_is_refused(self):
        for state in ("/", os.path.dirname(lab.LIVE_PATHS[0]), lab.real_home()):
            with self.subTest(state=state):
                with self.assertRaises(lab.Refused):
                    lab.check_state(state)
        self.assertEqual(self.cli("status", env={"ATHENA_STATE_DIR": "/"}).returncode, 2)


class _RealSubs(_Repos):
    def _setup(self):
        self.origin = self.tmp / "lib-origin.git"
        seed = self.tmp / "lib-seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        git(seed, "commit", "-q", "--allow-empty", "-m", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        repo = self.repo()
        git(repo, "submodule", "add", "-q", str(self.origin), "lib")
        git(repo, "commit", "-q", "-m", "add lib")
        git(repo, "push", "-q")
        path = self.worktree(repo)
        git(path, "submodule", "update", "-q", "--init")
        spawn_record("w", path, repo)
        return repo, path

    def _modules(self, path):
        return Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "modules"))

    def _commit_only_here(self, path, inner="lib"):
        git(path / inner, "commit", "-q", "--allow-empty", "-m", "only here")
        sha = git(path / inner, "rev-parse", "HEAD")
        git(path, "add", inner)
        git(path, "commit", "-q", "-m", "bump")
        git(path, "push", "-q")
        return sha

    def _type_of(self, modules, sha):
        return git(modules / "lib", "--git-dir=.", "--work-tree=.", "cat-file", "-t", sha)

    def _kept(self, path, sha):
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(ledger.get("w")["state"], "live")
        self.assertTrue(path.exists())
        self.assertEqual(self._type_of(self._modules(path), sha), "commit")


class RealSubmodules(_RealSubs):
    """Round 4: a real submodule's repository lives in .git/worktrees/<id>/modules/<name>, and removing the worktree with --force
    deletes it. Nothing looked there, so `deinit` or `git rm` hid the only copy of its commits. These use real
    `submodule add` and `update --init`, so they have a `.git` file and a modules folder."""

    def test_the_setup_is_a_real_submodule(self):
        _, path = self._setup()
        self.assertTrue((path / "lib" / ".git").is_file())
        self.assertTrue((self._modules(path) / "lib" / "HEAD").exists())
        self.assertIn("worktrees", str(self._modules(path)))

    def test_deinit_with_unpushed_commits_is_refused(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        git(path, "submodule", "deinit", "-q", "-f", "lib")
        self.assertFalse((path / "lib" / ".git").exists())
        self._kept(path, sha)

    def test_deinit_with_unpushed_commits_is_refused_on_the_wip_route(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        git(path, "submodule", "deinit", "-q", "-f", "lib")
        (path / "other.txt").write_text("dirty")
        self._kept(path, sha)

    def test_a_removed_link_with_unpushed_commits_is_refused(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        git(path, "rm", "-q", "-f", "lib")
        git(path, "commit", "-q", "-m", "drop lib")
        git(path, "push", "-q")
        self.assertEqual(gitops.unsaved(path)["dirty"], [])
        self._kept(path, sha)
        (path / "other.txt").write_text("dirty")
        self._kept(path, sha)

    def test_a_renamed_link_with_unpushed_commits_is_refused(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        git(path, "mv", "lib", "renamed")
        git(path, "commit", "-q", "-m", "rename")
        git(path, "push", "-q")
        self._kept(path, sha)

    def test_a_stash_left_in_a_removed_module_is_refused(self):
        _, path = self._setup()
        inner = path / "lib"
        (inner / "t.txt").write_text("a")
        git(inner, "add", "-A")
        git(inner, "stash", "push", "-q")
        git(path, "rm", "-q", "-f", "lib")
        git(path, "commit", "-q", "-m", "drop lib")
        git(path, "push", "-q")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(path.exists())

    def test_a_commit_only_a_reflog_remembers_is_refused(self):
        _, path = self._setup()
        inner = path / "lib"
        start = git(inner, "rev-parse", "HEAD")
        git(inner, "commit", "-q", "--allow-empty", "-m", "dropped")
        git(inner, "reset", "-q", "--hard", start)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(path.exists())

    def test_a_check_that_fails_holds(self):
        _, path = self._setup()
        git(path, "submodule", "deinit", "-q", "-f", "lib")
        shutil.rmtree(self._modules(path) / "lib" / "objects")
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(path.exists())

    def test_something_unknown_in_the_modules_folder_holds(self):
        _, path = self._setup()
        (self._modules(path) / "not-a-repository.txt").write_text("x")
        self.assertTrue(gitops.modules_hold(path))

    def test_it_shows_as_held_in_the_list(self):
        self.write_config(worktree_root=str(self.home / "worktrees"), projects_dir=str(self.tmp / "projects"))
        _, path = self._setup()
        self._commit_only_here(path)
        git(path, "submodule", "deinit", "-q", "-f", "lib")
        retire.retire(caps.CTX, "w", keep_worktree=True)
        later = time.time() + 90 * 86400
        row = next(r for r in workspaces.listing(workspaces.Scan(), sizes=False, now=later) if r["path"] == str(path))
        self.assertEqual(row["state"], "held")
        self.assertIn("lib", row["why"])

    def test_a_clean_checked_out_submodule_goes(self):
        _, path = self._setup()
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_clean_deinit_goes(self):
        _, path = self._setup()
        git(path, "submodule", "deinit", "-q", "-f", "lib")
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_clean_removed_link_goes(self):
        _, path = self._setup()
        git(path, "rm", "-q", "-f", "lib")
        git(path, "commit", "-q", "-m", "drop lib")
        git(path, "push", "-q")
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_clean_renamed_link_goes(self):
        _, path = self._setup()
        git(path, "mv", "lib", "renamed")
        git(path, "commit", "-q", "-m", "rename")
        git(path, "push", "-q")
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_broken_worktree_with_a_held_module_is_refused(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        (path / ".git").unlink()
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(path.exists())
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_a_vanished_worktree_holding_work_stays_tracked(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        shutil.rmtree(path)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertIn(str(path), str(caught.exception))
        self.assertEqual(ledger.get("w")["state"], "live")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def _other_worktree_at_prune(self, break_it, parsed=False):
        """Worktree w holds unpushed module commits and git would prune it. Worker b's retire would run that prune."""
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        admin = modules.parent
        break_it(path, admin)
        if parsed:
            self.assertIn(f"touch {admin}/locked", gitops.prune_refused(repo))
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "b", force=True)
        self.assertEqual(ledger.get("b")["state"], "live")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_another_worktree_with_its_git_file_gone_is_not_pruned(self):
        self._other_worktree_at_prune(lambda path, admin: (path / ".git").unlink())

    def test_another_worktree_with_an_empty_gitdir_file_is_not_pruned(self):
        self._other_worktree_at_prune(lambda path, admin: (admin / "gitdir").write_text(""), parsed=True)

    def test_another_worktree_with_an_unreadable_gitdir_file_is_not_pruned(self):
        def unreadable(path, admin):
            (admin / "gitdir").chmod(0)
            self.addCleanup((admin / "gitdir").chmod, 0o600)
        self._other_worktree_at_prune(unreadable, parsed=True)

    def test_another_worktree_with_no_gitdir_file_is_not_pruned(self):
        self._other_worktree_at_prune(lambda path, admin: (admin / "gitdir").unlink(), parsed=True)

    def test_another_worktree_duplicating_the_main_checkout_is_not_pruned(self):
        self._other_worktree_at_prune(lambda path, admin: (admin / "gitdir").write_text(f"{admin.parent.parent}\n"))

    def test_another_worktree_with_a_trailing_slash_is_not_pruned(self):
        def slash(path, admin):
            (admin / "gitdir").write_text((admin / "gitdir").read_text().rstrip() + "/\n")
        self._other_worktree_at_prune(slash)

    def test_a_duplicate_of_a_sibling_keeps_its_commits_whichever_git_drops(self):
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        kept = self.worktree(repo, "t/k", "k")
        (modules.parent / "gitdir").write_text(f"{kept}/.git\n")
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        try:
            retire.retire(caps.CTX, "b", force=True)
        except retire.RetireError:
            pass
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_an_answer_from_git_that_cannot_be_read_refuses(self):
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        admin = modules.parent
        (path / ".git").unlink()
        renamed = admin.parent / "a\nb"
        admin.rename(renamed)
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "b", force=True)
        self.assertIn("can't read", str(caught.exception))
        self.assertEqual(self._type_of(renamed / "modules", sha), "commit")

    def test_keep_worktree_on_a_vanished_holder_stays_tracked(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        shutil.rmtree(path)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", keep_worktree=True)
        self.assertIn(str(path), str(caught.exception))
        self.assertEqual(ledger.get("w")["state"], "live")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_keep_worktree_on_a_vanished_holder_with_an_empty_record_stays_tracked(self):
        _, path = self._setup()
        self._commit_only_here(path)
        (self._modules(path).parent / "gitdir").write_text("")
        shutil.rmtree(path)
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", keep_worktree=True)
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_keep_worktree_on_a_clean_vanished_worker_retires(self):
        _, path = self._setup()
        shutil.rmtree(path)
        retire.retire(caps.CTX, "w", keep_worktree=True)
        self.assertEqual(ledger.get("w")["state"], "retired")

    def test_what_the_refusal_says_clears_it_and_outlasts_gc(self):
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        admin = modules.parent
        shutil.rmtree(path)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertIn(f"touch {admin}/locked", str(caught.exception))
        (admin / "locked").touch()
        retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(ledger.get("w")["state"], "retired")
        for name in ("index", "gitdir"):
            os.utime(admin / name, (1, 1))
        git(repo, "gc", "-q")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_the_printed_lock_works_when_pasted_with_a_space_and_a_dollar_in_the_path(self):
        _, path = self._setup()
        sha = self._commit_only_here(path)
        admin = self._modules(path).parent
        shutil.rmtree(path)
        renamed = admin.parent / "a b$(x)c"
        admin.rename(renamed)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        command = shlex.split(str(caught.exception).split("go on: ", 1)[1])
        self.assertEqual(command, ["touch", f"{renamed}/locked"])
        Path(command[1]).touch()
        retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(ledger.get("w")["state"], "retired")
        self.assertEqual(self._type_of(renamed / "modules", sha), "commit")

    def test_a_corrupt_module_refusal_names_the_worktree_it_is_in(self):
        repo, path = self._setup()
        modules = self._modules(path)
        (next(modules.iterdir()) / "HEAD").write_text("garbage")
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "b", force=True)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn(f"touch {modules.parent}/locked", str(caught.exception))

    def test_a_refusal_after_the_move_says_where_the_folder_went(self):
        repo, path = self._setup()
        (path / ".git").unlink()
        real = gitops.prune

        def refused(_):
            raise gitops.GitError("no")
        gitops.prune = refused
        self.addCleanup(lambda: setattr(gitops, "prune", real))
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())
        self.assertIn(str(store.path("trash")), str(caught.exception))

    def test_an_orphaned_worker_cannot_prune_another_worktrees_module(self):
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        (other / ".git").unlink()
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "b", force=True)
        self.assertTrue(other.exists(), "it moved to the trash before the refusal")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_the_refusal_names_the_worktree_that_holds(self):
        repo, path = self._setup()
        self._commit_only_here(path)
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "b", force=True)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("commits found only there", str(caught.exception))

    def test_a_failed_read_is_not_reported_as_work_held(self):
        repo, path = self._setup()
        shutil.rmtree(path)
        real = gitops._admins

        def unreadable(_):
            raise gitops.GitError("worktrees folder unreadable")
        gitops._admins = unreadable
        self.addCleanup(lambda: setattr(gitops, "_admins", real))
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertIn("can't read", str(caught.exception))
        self.assertNotIn("holds", str(caught.exception))

    def test_a_worker_whose_folder_is_there_is_not_held_up_by_another(self):
        repo, path = self._setup()
        self._commit_only_here(path)
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        retire.retire(caps.CTX, "b", force=True)
        self.assertFalse(other.exists())
        self.assertEqual(ledger.get("b")["state"], "retired")

    def test_a_clean_other_worktree_does_not_stop_a_prune(self):
        repo, path = self._setup()
        admin = self._modules(path).parent
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        retire.retire(caps.CTX, "b", force=True)
        self.assertEqual(ledger.get("b")["state"], "retired")
        self.assertFalse(admin.exists(), "git prunes what the guard let through")

    def test_a_locked_worktree_does_not_stop_a_prune(self):
        repo, path = self._setup()
        sha = self._commit_only_here(path)
        modules = self._modules(path)
        git(repo, "worktree", "lock", str(path))
        (path / ".git").unlink()
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        retire.retire(caps.CTX, "b", force=True)
        self.assertEqual(ledger.get("b")["state"], "retired")
        self.assertEqual(self._type_of(modules, sha), "commit")

    def test_a_clean_vanished_worktree_is_pruned(self):
        repo, path = self._setup()
        modules = self._modules(path)
        shutil.rmtree(path)
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(modules.exists())
        self.assertEqual(len(git(repo, "worktree", "list").splitlines()), 1)


class RepoGone(_RealSubs):
    """A worker whose main checkout is gone could not retire by any route: both prune guards ask git, and git has no answer."""

    def _gone_vanished(self):
        repo, path = self._worker()
        shutil.rmtree(path)
        shutil.rmtree(repo)

    def test_a_vanished_worker_retires_on_every_route(self):
        for kwargs in ({}, {"force": True}, {"keep_worktree": True}):
            with self.subTest(**kwargs):
                shutil.rmtree(self.tmp / "repos", ignore_errors=True)
                shutil.rmtree(self.home / "worktrees", ignore_errors=True)
                self._gone_vanished()
                self.assertFalse(retire.retire(caps.CTX, "w", **kwargs)["removed"])
                self.assertEqual(ledger.get("w")["state"], "retired")

    def test_a_folder_left_behind_goes_to_the_trash_with_force(self):
        repo, path = self._worker()
        shutil.rmtree(repo)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("--force", str(caught.exception))
        self.assertTrue(path.exists())
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertTrue(Path(out["trash"]).exists())
        self.assertEqual(ledger.get("w")["state"], "retired")

    def test_a_repo_git_cannot_read_says_what_clears_it_and_that_does(self):
        repo, path = self._worker()
        shutil.rmtree(path)
        (repo / ".git" / "HEAD").write_text("garbage")
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertIn("move it away", str(caught.exception))
        self.assertEqual(ledger.get("w")["state"], "live")
        repo.rename(repo.with_name("moved"))
        retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(ledger.get("w")["state"], "retired")


class GitRunsNothingOfTheWorkers(_RealSubs):
    """Round 7: `git status` runs a repository's own core.fsmonitor, so looking at a submodule ran a worker's program."""

    def _armed(self):
        repo, path = self._worker()
        inner = self._sub(path, pushed=True, dirty_elsewhere=False)
        ran = self.tmp / "fsmonitor-ran"
        script = self.tmp / "fsmonitor.sh"
        script.write_text(f'#!/bin/sh\necho "$@" >> "{ran}"\nexit 1\n')
        script.chmod(0o755)
        git(inner, "config", "core.fsmonitor", str(script))
        return path, inner, ran

    def test_checking_a_submodule_runs_no_fsmonitor(self):
        path, inner, ran = self._armed()
        gitops.unsaved(path)
        gitops.refuse_nested_repos(path, git(path, "rev-parse", "HEAD"))
        gitops.status_paths(path, "--ignore-submodules=none")
        self.assertFalse(ran.exists(), ran.exists() and ran.read_text())

    def test_a_look_never_fetches_a_missing_object(self):
        origin = self.tmp / "partial.git"
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(origin, "config", "uploadpack.allowFilter", "true")
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "f").write_text("a blob a partial clone lacks")
        git(seed, "add", "f")
        git(seed, "commit", "-q", "-m", "one")
        blob = git(seed, "rev-parse", "HEAD:f")
        git(seed, "push", "-q", str(origin), "main")
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{origin}", str(clone)], check=True,
                       capture_output=True)
        self.assertFalse(gitops.ok(clone, "cat-file", "-e", blob))
        self.assertEqual(gitops.git(clone, "cat-file", "-p", blob, check=False), "")
        self.assertNotEqual(subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob], capture_output=True,
                                           env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}).returncode, 0)


class PartialClones(_Repos):
    """A look never fetches. A worktree or a fast-forward has to: refusing it there broke every launch in a blobless clone."""

    def _blobless(self):
        origin = self.tmp / "partial.git"
        seed = self.tmp / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(origin, "config", "uploadpack.allowFilter", "true")
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "f").write_text("one")
        git(seed, "add", "f")
        git(seed, "commit", "-q", "-m", "one")
        git(seed, "push", "-q", str(origin), "main")
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", f"file://{origin}", str(clone)], check=True, capture_output=True)
        (seed / "f").write_text("two")
        git(seed, "commit", "-qam", "two")
        git(seed, "push", "-q", str(origin), "main")
        return clone

    def test_a_launch_works_in_a_blobless_clone(self):
        clone = self._blobless()
        self.assertEqual(gitops.preflight(clone), [])
        self.assertEqual((clone / "f").read_text(), "two")
        path = self.tmp / "tree"
        gitops.add_worktree(clone, "t/x", path)
        self.assertEqual((path / "f").read_text(), "two")
        spawn_record("w", path, clone)
        retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())


class OddNames(_Repos):
    """Python's text mode turned Finder's `Icon\\r` into `Icon\\n`, a name that isn't there, and that stopped a retire."""

    def _worker_with_icon(self, ignores):
        _, path = self._worker()
        if ignores:
            (path / ".gitignore").write_text(ignores)
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "ignores")
            git(path, "push", "-q")
        (path / "Icon\r").write_text("x")
        return path

    def test_an_untracked_icon_is_reported_by_its_real_name(self):
        self.assertEqual(gitops.unsaved(self._worker_with_icon(""))["dirty"], ["Icon\r"])

    def test_an_ignored_icon_is_kept_and_does_not_stop_a_forced_retire(self):
        path = self._worker_with_icon("Icon?\n")
        self.assertEqual(gitops.ignored_work(path), ["Icon\r"])
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual((Path(out["ignored_kept"]) / "Icon\r").read_text(), "x")
        self.assertFalse(path.exists())

    def test_an_untracked_icon_is_saved_and_does_not_stop_a_forced_retire(self):
        path = self._worker_with_icon("")
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(path.parent.parent.parent.parent / "repos" / "proj" / "main", "show", f"{out['wip_backup']}:Icon\r"), "x")
        self.assertFalse(path.exists())


class ModulesFolders(_RealSubs):
    def test_an_empty_modules_folder_does_not_stop_a_forced_retire(self):
        repo, path = self._worker()
        Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "modules")).mkdir()
        self.assertTrue(gitops.needs_force(path))
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())

    def test_a_submodule_of_a_modules_own_worktree_is_walked(self):
        _, path = self._setup()
        modules = self._modules(path)
        inner_tree = self.tmp / "inner-tree"
        git(path / "lib", "worktree", "add", "-q", "-b", "inner", str(inner_tree), "HEAD")
        git(inner_tree, "submodule", "add", "-q", str(self.origin), "nested")
        git(inner_tree / "nested", "commit", "-q", "--allow-empty", "-m", "only here")
        sha = git(inner_tree / "nested", "rev-parse", "HEAD")
        nested = modules / "lib" / "worktrees" / "inner-tree" / "modules" / "nested"
        self.assertTrue(nested.is_dir())
        self.assertIn("1 commits found only there", gitops.modules_hold(path))
        with self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(nested, "--git-dir=.", "--work-tree=.", "cat-file", "-t", sha), "commit")


class DetachedHeads(_Repos):
    """A vanished worktree's detached HEAD is the only name its commits have, and the prune deletes that name."""

    def _detached_only_here(self, path):
        git(path, "switch", "-q", "--detach")
        git(path, "commit", "-q", "--allow-empty", "-m", "only here")
        sha = git(path, "rev-parse", "HEAD")
        shutil.rmtree(path)
        return sha

    def test_its_own_retire_keeps_the_head(self):
        repo, path = self._worker()
        sha = self._detached_only_here(path)
        out = retire.retire(caps.CTX, "w")
        self.assertEqual(git(repo, "rev-parse", out["backup"]), sha)

    def test_a_broken_worktree_keeps_it_too(self):
        repo, path = self._worker()
        git(path, "switch", "-q", "--detach")
        git(path, "commit", "-q", "--allow-empty", "-m", "only here")
        sha = git(path, "rev-parse", "HEAD")
        (path / ".git").unlink()
        out = retire.retire(caps.CTX, "w", force=True)
        self.assertEqual(git(repo, "rev-parse", out["backup"]), sha)

    def test_another_workers_prune_keeps_it(self):
        repo, path = self._worker()
        sha = self._detached_only_here(path)
        other = self.worktree(repo, "t/b", "b")
        spawn_record("b", other, repo)
        shutil.rmtree(other)
        retire.retire(caps.CTX, "b")
        self.assertIn(sha, git(repo, "for-each-ref", "--format=%(objectname)", "refs/athena-backup/"))

    def test_a_vanished_worktree_on_a_branch_needs_no_backup(self):
        repo, path = self._worker()
        shutil.rmtree(path)
        self.assertIsNone(retire.retire(caps.CTX, "w")["backup"])


def _runner():
    spec = importlib.util.spec_from_file_location("chaos_rex", ROOT / "scripts" / "chaos-rex.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KNOWN_PROGRAMS = {"git", "ps", "hdiutil", "memory_pressure", "/usr/bin/python3"}
KNOWN_NAMES = {"AR", "REX", "binary"}
KNOWN_UNDER_HERE = {("scripts", "smoke.sh")}
EVERY_STARTER = {"subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}}
NEVER = {"os": ("system", "popen", "exec", "spawn", "posix_spawn", "fork", "forkpty"), "pty": ("spawn", "fork")}


def _names_a_claude(node, aliases) -> bool:
    for part in ast.walk(node):
        if isinstance(part, ast.Name) and part.id in aliases:
            return True
        if isinstance(part, ast.Constant) and isinstance(part.value, str) and "claude" in re.split(r"[\s/]+", part.value):
            return True
    return False


def _known_program(first) -> bool:
    if isinstance(first, ast.Constant):
        return first.value in KNOWN_PROGRAMS
    plain = first.args[0] if isinstance(first, ast.Call) and getattr(first.func, "id", "") == "str" and len(first.args) == 1 else first
    if isinstance(plain, ast.Name):
        return plain.id in KNOWN_NAMES
    named = {n.id for n in ast.walk(first) if isinstance(n, ast.Name)} - {"str"}
    parts = {n.value for n in ast.walk(first) if isinstance(n, ast.Constant)}
    return named == {"HERE"} and any(parts == set(known) for known in KNOWN_UNDER_HERE) and not any(isinstance(n, ast.JoinedStr) for n in ast.walk(first))


def _claude_starts(source) -> tuple:
    """Every way the runner could start a program: (what is wrong, how many real Claudes it starts with their own environment). An
    argv must be a literal list starting with a program known not to be Claude, or carry env=run.claude_env(...), so a renamed
    variable is caught by what it isn't. Shell strings and exec are refused outright, and a pane gets no Claude through rx."""
    tree = ast.parse(source)
    aliases, wrong, started = set(), [], 0
    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets) and isinstance(node.value, ast.Call) and _names_a_claude(node.value, aliases):
                aliases.update(t.id for t in node.targets)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "os", "pty"):
            wrong.append(f"line {node.lineno}: from {node.module} import hides a start from this check")
        elif isinstance(node, ast.Import) and any(a.name == "pty" or (a.name in ("subprocess", "os") and a.asname) for a in node.names):
            wrong.append(f"line {node.lineno}: an alias hides a start from this check")
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
        attr = node.func.attr
        where = f"line {node.lineno}"
        if attr == "rx" and any(_names_a_claude(a, aliases) for a in node.args):
            wrong.append(f"{where}: a Claude started in a pane")
        if owner in NEVER and attr.startswith(NEVER[owner]):
            wrong.append(f"{where}: {owner}.{attr}")
        if owner not in EVERY_STARTER or attr not in EVERY_STARTER[owner]:
            continue
        env = next((k.value for k in node.keywords if k.arg == "env"), None)
        safe_env = isinstance(env, ast.Call) and isinstance(env.func, ast.Attribute) and env.func.attr == "claude_env"
        if attr in ("getoutput", "getstatusoutput") or any(k.arg == "shell" for k in node.keywords):
            wrong.append(f"{where}: a shell string")
        elif not (node.args and isinstance(node.args[0], ast.List) and node.args[0].elts):
            wrong.append(f"{where}: an argv this can't read")
        elif safe_env:
            started += 1
        elif not _known_program(node.args[0].elts[0]):
            wrong.append(f"{where}: an unknown program, without run.claude_env()")
        elif _names_a_claude(node.args[0], aliases):
            wrong.append(f"{where}: a Claude passed to another program")
    return wrong, started


class ChaosRunner(Case):
    """It signals real processes and starts real Claudes, so what it may touch is proved here, with nothing signalled."""

    def _run(self):
        run = _runner().Run(False)
        run.state = self.tmp / "scratch-state"
        return run

    def _kills(self, command, pid=4242, **expected):
        run = self._run()
        with mock.patch.object(run, "command_of", lambda p: command), mock.patch("os.kill") as killed:
            answer = run.kill_mine(pid, **expected)
        self.assertEqual(answer, killed.called)
        return killed.called

    def test_only_the_claude_with_the_name_it_started_is_signalled(self):
        self.assertTrue(self._kills("claude --name mine --settings x", name="mine"))
        for other in ("claude --name other --settings x", "claude --resume abc", "claude --name mine2", "vim mine", ""):
            self.assertFalse(self._kills(other, name="mine"), other)

    def test_a_plain_program_must_match_exactly(self):
        self.assertTrue(self._kills("sleep 900", exact="sleep 900"))
        self.assertFalse(self._kills("sleep 9000", exact="sleep 900"))

    def test_the_server_must_name_this_runs_own_folder(self):
        run = self._run()
        self.assertTrue(self._kills(f"rex server run --listen unix://{run.root}/s.sock", within=str(run.root)))
        self.assertFalse(self._kills("rex server run --listen unix:///private/tmp/other/s.sock", within=str(run.root)))

    def test_nothing_without_saying_what_it_must_be(self):
        with self.assertRaises(TypeError):
            self._run().kill_mine(4242)

    def test_pid_one_zero_and_below_are_never_signalled(self):
        for pid in (1, 0, -1):
            self.assertFalse(self._kills("claude --name mine", pid=pid, name="mine"), pid)

    def test_it_refuses_to_run_outside_the_lab(self):
        env = {k: v for k, v in os.environ.items() if k != "ATHENA_WORKER"}
        out = subprocess.run(["/usr/bin/python3", "-I", "-B", str(ROOT / "scripts" / "chaos-rex.py"), "--only", "none"], capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("lab", out.stderr)

    def test_a_real_claude_gets_scratch_state_and_no_herdr(self):
        run = self._run()
        with mock.patch.dict(os.environ, {"ATHENA_STATE_DIR": lab.LIVE_PATHS[0], "HERDR_PANE_ID": "7", "HERDR_ENV": "1", "ATHENA_WORKER": "rex-chaos"}):
            env = run.claude_env()
            named = run.claude_env("real")
        self.assertEqual(env["ATHENA_STATE_DIR"], str(run.state))
        self.assertNotIn("ATHENA_WORKER", env)
        self.assertEqual(named["ATHENA_WORKER"], "real")
        self.assertFalse([k for k in env if k.startswith("HERDR_")])

    def test_a_real_claude_gets_only_what_the_allowlist_names(self):
        run = self._run()
        leaks = {"ATHENA_BRIEF": "b", "ATHENA_GATE": "off", "ATHENA_REAPPROVAL": "1", "ATHENA_HQ": "athena", "CLAUDE_CODE_SESSION_ID": "mine",
                 "CLAUDE_CODE_MESSAGING_SOCKET": "/x", "CLAUDECODE": "1", "REX_CONFIG": "/x", "REX_LAB_SERVER": "x", "HERDR_ENV": "1", "ANYTHING_NEW": "x"}
        with mock.patch.dict(os.environ, {**leaks, "HOME": "/h", "PATH": "/p", "ATHENA_WORKER": "rex-chaos"}):
            env = run.claude_env()
            named = run.claude_env("real")
        self.assertFalse(set(leaks) & set(env), env)
        self.assertEqual((env["HOME"], env["PATH"]), ("/h", "/p"))
        self.assertEqual(set(named) - set(env), {"ATHENA_WORKER"})

    def test_every_real_claude_it_starts_has_that_environment(self):
        found, started = _claude_starts((ROOT / "scripts" / "chaos-rex.py").read_text())
        self.assertEqual(found, [])
        self.assertGreaterEqual(started, 3)

    def test_no_way_of_starting_a_claude_goes_round_it(self):
        head = "import os, shutil, subprocess\nclaude = shutil.which('claude')\n"
        for pose in ("subprocess.run([claude, '-p'], env=os.environ)",
                     "subprocess.run([claude, '-p'])",
                     "subprocess.run([HERE / 'wrap-claude.sh', '-p'])",
                     "subprocess.run([str(HERE / 'scripts' / 'wrap.sh'), '-p'])",
                     "subprocess.run([binary + '-wrapper', '-p'])",
                     "subprocess.Popen([claude, '-p'], env=dict(os.environ))",
                     "subprocess.check_output(['claude', '-p'])",
                     "subprocess.call(['/bin/sh', '-c', 'claude -p'])",
                     "subprocess.run('claude -p', shell=True, env=run.claude_env())",
                     "subprocess.run(f'{claude} -p', shell=True)",
                     "subprocess.getoutput('claude -p')",
                     "os.execv(claude, [claude])",
                     "os.execvpe('claude', ['claude'], os.environ)",
                     "os.spawnvp(os.P_WAIT, claude, [claude])",
                     "os.posix_spawn(claude, [claude], os.environ)",
                     "os.system('claude -p')",
                     "os.popen('claude -p')",
                     "the_tool = shutil.which('claude')\nsubprocess.run([the_tool, '-p'])",
                     "argv = [claude, '-p']\nsubprocess.run(argv)",
                     "run.rx('new-session', '--', claude)",
                     "the_tool = shutil.which('claude')\nrun.rx('send-text', the_tool)",
                     "run.rx('send-text', 'claude --resume x')",
                     "subprocess.run([REX, 'new-session', '--', claude])",
                     "from subprocess import Popen as P\nP(['x'])",
                     "import subprocess as s\ns.run([claude])",
                     "from os import execv\nexecv(claude, [claude])",
                     "import pty\npty.spawn([claude])"):
            with self.subTest(pose=pose):
                self.assertTrue(_claude_starts(head + pose)[0], pose)
        self.assertEqual(_claude_starts(head + "subprocess.run([claude, '-p'], env=run.claude_env())\nsubprocess.run([AR, 'spawn'], env=run.env(ATHENA_CLAUDE=claude))"), ([], 1))

    def test_the_server_its_panes_run_under_is_clean(self):
        run = self._run()
        with mock.patch.dict(os.environ, {"ATHENA_BRIEF": "b", "CLAUDE_CODE_SESSION_ID": "mine", "REX_LAB_SERVER": "x"}), mock.patch("subprocess.run") as started:
            run.server_up()
        self.assertEqual(set(started.call_args.kwargs["env"]), {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR", "LANG", "REX_CONFIG"})

    def test_a_process_dying_in_the_window_is_no_error(self):
        run = self._run()
        for gone in (ProcessLookupError(), PermissionError()):
            with mock.patch.object(run, "command_of", lambda p: "sleep 900"), mock.patch("os.kill", side_effect=gone):
                self.assertFalse(run.kill_mine(4242, exact="sleep 900"))

    def test_an_empty_match_is_no_match(self):
        for empty in ({"within": ""}, {"exact": ""}, {"name": ""}):
            with self.subTest(empty=empty), self.assertRaises(TypeError):
                self._kills("anything at all", **empty)

    def test_a_watch_dying_in_the_window_is_no_error(self):
        run = self._run()
        (run.state / "run").mkdir(parents=True)
        (run.state / "run" / "watch-state.json").write_text(json.dumps({"pid": 4242}))
        seen = subprocess.CompletedProcess([], 0, stdout=f"athena-rex watch --once ATHENA_STATE_DIR={run.state} X\n")
        with mock.patch("subprocess.run", return_value=seen), mock.patch("os.kill", side_effect=ProcessLookupError()):
            run.stop_watch()

    def test_finish_reaches_the_server_stop_whatever_fails_before_it(self):
        run = self._run()
        run.root = self.tmp / "runner"
        (run.state / "run").mkdir(parents=True)
        run.root.mkdir()
        (run.state / "run" / "w.pid").write_text("4242")
        with mock.patch.object(run, "stop_watch", side_effect=ValueError("torn")), mock.patch.object(run, "ar", side_effect=subprocess.TimeoutExpired("ar", 1)), \
                mock.patch.object(run, "command_of", lambda p: "claude --name w"), mock.patch("os.kill", side_effect=ProcessLookupError()), mock.patch.object(run, "rx") as rx:
            run.finish()
        rx.assert_called_once_with("server", "stop")
        self.assertFalse(run.root.exists())

class Measure(Case):
    def test_it_keeps_the_lab_guards_on(self):
        self.assertRegex((ROOT / "scripts" / "measure.sh").read_text(), r"\bATHENA_WORKER=rex-[a-z]+\b")
        self.assertNotRegex((ROOT / "scripts" / "measure.sh").read_text(), r"\bATHENA_WORKER=(?!rex-)")
        self.assertTrue(store.valid_name("rex-measure"))


class SmokeDeadline(Case):
    """Bash runs a TERM trap only after the foreground child returns, so a hung child kept smoke alive past its deadline."""

    def test_a_hung_child_does_not_outlive_the_deadline(self):
        pid_file = self.tmp / "hung.pid"
        fake = self.tmp / "rex"
        fake.write_text(f'#!/bin/sh\ncase "$*" in *"server start"*) echo $$ > "{pid_file}"; exec sleep 300 ;; esac\nexit 0\n')
        fake.chmod(0o755)
        begun = time.monotonic()
        out = subprocess.run([str(ROOT / "scripts" / "smoke.sh"), "--state-dir", str(self.state)], cwd=str(self.tmp), capture_output=True, text=True,
                             env={**os.environ, "ATHENA_REX_BIN": str(fake), "SMOKE_DEADLINE": "1"}, timeout=40)
        self.assertLess(time.monotonic() - begun, 30)
        self.assertEqual(out.returncode, 143, out.stdout + out.stderr)
        self.assertIn("over its 1s deadline", out.stderr)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


class StateDirIsolation(Case):
    """Round 4: a state dir of ~/Developer passed, which made all of it fair game for cleanup."""

    def test_a_state_dir_under_the_real_home_is_refused(self):
        for state in (os.path.join(lab.real_home(), "Developer"), os.path.join(lab.real_home(), "Developer", "x", "y"),
                      os.path.join(lab.real_home(), "Documents"), os.path.join(lab.real_home(), "Developer").upper()):
            with self.subTest(state=state):
                with self.assertRaises(lab.Refused):
                    lab.check_state(state)

    def test_the_command_refuses_it(self):
        out = self.cli("status", env={"ATHENA_STATE_DIR": os.path.join(lab.real_home(), "Developer")})
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)

    def test_smoke_refuses_it_even_with_the_lab_guard_off(self):
        out = subprocess.run([str(ROOT / "scripts" / "smoke.sh"), "--state-dir", os.path.join(lab.real_home(), "Developer"), "--server",
                              "unix:///nonexistent/s.sock"], capture_output=True, text=True, timeout=20,
                             env={k: v for k, v in os.environ.items() if k != "ATHENA_WORKER"})
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refusing state dir", out.stderr)

    def test_an_unmarked_folder_is_not_a_scratch_place(self):
        unmarked = self.tmp / "holds-real-things"
        (unmarked / "project").mkdir(parents=True)
        os.environ["ATHENA_STATE_DIR"] = str(unmarked)
        config.reset()
        self.assertFalse(workspaces._may_touch(unmarked / "project"))

    def test_an_unmarked_home_is_not_a_scratch_place(self):
        unmarked = self.tmp / "other-home"
        unmarked.mkdir()
        os.environ["HOME"] = str(unmarked)
        self.assertFalse(workspaces._may_touch(unmarked / "x"))

    def test_a_mark_cannot_make_the_real_home_a_scratch_place(self):
        real, inner, outside = self.tmp / "holder" / "real-home", self.tmp / "holder" / "real-home" / "Developer", self.tmp / "other"
        for place in (real, inner, real.parent, outside):
            place.mkdir(parents=True, exist_ok=True)
            (place / lab.SCRATCH_MARK).write_text("")
        with mock.patch.object(lab, "_HOME", str(real)):
            self.assertFalse(lab.scratch_place(real))
            self.assertFalse(lab.scratch_place(inner))
            self.assertFalse(lab.scratch_place(real.parent))
            self.assertTrue(lab.scratch_place(outside))

    def test_a_marked_scratch_place_is_one(self):
        self.assertTrue(lab.scratch_place(self.state))
        self.assertTrue(lab.scratch_place(self.home))
        self.assertTrue(workspaces._may_touch(self.state / "x"))
        self.assertTrue(workspaces._may_touch(self.home / "x"))

    def test_a_scratch_copy_is_marked(self):
        source = self.tmp / "source"
        source.mkdir()
        dest = self.tmp / "copy"
        out = subprocess.run(["/usr/bin/python3", str(ROOT / "scripts" / "scratch-state.py"), str(dest), "--from", str(source)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(lab.scratch_place(dest))


class WorkspacesDoc(Case):
    def test_it_does_not_read_as_if_force_removes_a_holding_submodule(self):
        text = (ROOT / "docs" / "workspaces.md").read_text()
        self.assertNotIn("So does a tracked submodule", text)
        self.assertIn("modules", text)


class Disposable(Case):
    """Round 2, finding 2: a pattern matched any part of a path, and a config could exempt everything."""

    def _tree(self, ignores):
        repo = self.repo()
        path = self.worktree(repo)
        (path / ".gitignore").write_text(ignores)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "ignores")
        git(path, "push", "-q")
        return path

    def _write(self, path, relative, text="x"):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _held(self, path):
        return " ".join(gitops.ignored_work(path))

    def test_an_env_file_under_a_folder_named_build_is_work(self):
        path = self._tree(".env\n")
        self._write(path, "services/build/.env")
        self.assertIn(".env", self._held(path))

    def test_a_hand_made_file_in_a_build_folder_git_does_not_ignore_is_work(self):
        path = self._tree("notes.txt\n")
        self._write(path, "services/build/notes.txt")
        self.assertEqual(gitops.ignored_work(path), ["services/build/notes.txt"])

    def test_a_repo_deep_inside_a_cache_folder_is_work(self):
        path = self._tree("node_modules/\n")
        inner = path / "node_modules" / "pkg"
        inner.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        self.assertIn("node_modules/pkg", self._held(path))

    def test_a_secret_inside_a_wholly_ignored_build_folder_is_work(self):
        path = self._tree("target/\n")
        self._write(path, "deploy/target/prod.env")
        self._write(path, "deploy/target/out.bin")
        held = gitops.ignored_work(path)
        self.assertEqual(held, ["deploy/target/prod.env"])

    def test_a_repo_under_a_disposable_name_is_work(self):
        for name in ("target", "node_modules"):
            path = self._tree(f"{name}/\n") if name == "target" else self._tree_again(name)
            inner = path / name
            inner.mkdir()
            subprocess.run(["git", "init", "-q", str(inner)], check=True)
            self.assertIn(name, self._held(path))

    def _tree_again(self, name):
        repo = self.tmp / "second"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / ".gitignore").write_text(f"{name}/\n")
        return repo

    def test_real_caches_do_not_count(self):
        path = self._tree("node_modules/\nbuild/\n__pycache__/\n*.pyc\n.DS_Store\n")
        self._write(path, "node_modules/pkg/test.pem")
        self._write(path, "web/build/app.js")
        self._write(path, "src/__pycache__/x.pyc")
        self._write(path, "src/y.pyc")
        self._write(path, ".DS_Store")
        self.assertEqual(gitops.ignored_work(path), [])

    def test_claude_files_the_global_ignore_covers_do_not_count(self):
        path = self._tree(".claude/.cc-writes/\n.claude/settings.local.json\n")
        self._write(path, ".claude/.cc-writes/a")
        self._write(path, ".claude/settings.local.json")
        self.assertEqual(gitops.ignored_work(path), [])

    def test_those_names_count_only_as_what_they_are(self):
        path = self._tree("build\nsettings.local.json/\n")
        self._write(path, "a/build")
        self._write(path, "b/settings.local.json/mine.txt")
        held = self._held(path)
        self.assertIn("a/build", held)
        self.assertIn("settings.local.json", held)

    def test_a_config_cannot_exempt_everything(self):
        for bad in (["*"], "*", ["**"], ["?*"], [".*"], ["*.env"], [3], {"a": 1}, True, ["a/b"]):
            path = self._tree(".env\n") if not (self.tmp / "repos" / "proj").exists() else self.tmp / "home" / "worktrees" / "proj" / "w"
            self._write(path, ".env")
            self.write_config(rex={"disposable": bad})
            self.assertIn(".env", self._held(path), repr(bad))

    def test_a_good_name_still_works_and_cannot_hide_a_repo(self):
        path = self._tree("generated/\n")
        self._write(path, "generated/x")
        self.write_config(rex={"disposable": ["generated"]})
        self.assertEqual(gitops.ignored_work(path), [])
        subprocess.run(["git", "init", "-q", str(path / "generated")], check=True)
        self.assertIn("generated", self._held(path))

    def test_only_the_state_dirs_own_config_can_name_them(self):
        path = self._tree("generated/\n")
        self._write(path, "generated/x")
        other = self.tmp / "other.json"
        other.write_text('{"rex": {"disposable": ["generated"]}}')
        os.environ["ATHENA_CONFIG"] = str(other)
        config.reset()
        self.assertIn("generated", self._held(path))


class CleanInTheLab(Case):
    """Round 2, finding 3: cleanup's lab check compared spellings, so the real home in another case counted as isolated."""

    def test_the_real_home_in_another_case_is_not_isolated(self):
        spelled = lab.real_home().swapcase()
        os.environ["HOME"] = spelled
        self.assertFalse(workspaces._may_touch(os.path.join(spelled, "Developer", "x")))
        self.assertFalse(workspaces._may_touch(os.path.join(lab.real_home(), "Developer", "x")))

    def test_an_isolated_home_and_a_scratch_copy_are_fine(self):
        self.assertTrue(workspaces._may_touch(self.home / "worktrees" / "x"))
        self.assertTrue(workspaces._may_touch(self.state / "scratch" / "x"))

    def test_a_scratchpad_in_use_in_another_case_stays(self):
        self.assertTrue(workspaces._within_any(str(self.tmp / "Pad"), [str(self.tmp / "pad").upper()]))


class SmokeRefusal(Case):
    """Round 2, finding 4: the guard skipped a live path that doesn't exist yet, and hung on a relative one."""

    def _smoke(self, *args):
        return subprocess.run([str(ROOT / "scripts" / "smoke.sh"), *args], capture_output=True, text=True, timeout=20,
                              cwd=str(self.tmp))

    def test_a_live_path_that_does_not_exist_yet_is_refused(self):
        missing = lab.LIVE_PATHS[1]
        for spelled in (missing, missing.upper(), os.path.join(missing, "workers")):
            out = self._smoke("--state-dir", spelled, "--server", "unix:///nonexistent/s.sock")
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("refusing state dir", out.stderr)

    def test_a_relative_missing_path_does_not_hang(self):
        (self.home / ".local" / "state" / "athena").mkdir(parents=True)
        for relative in ("foo", "foo/bar", "./foo"):
            out = self._smoke("--state-dir", relative, "--server", "unix:///nonexistent/s.sock")
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("refusing server", out.stderr)

    def test_a_relative_path_to_live_state_is_refused(self):
        relative = os.path.relpath(lab.LIVE_PATHS[0], self.tmp)
        out = self._smoke("--state-dir", relative, "--server", "unix:///nonexistent/s.sock")
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refusing state dir", out.stderr)


class SmokeInterrupted(Case):
    """A signal during a foreground command made cleanup exit with that command's status: a killed smoke could exit 0."""

    def test_a_terminated_smoke_never_exits_zero(self):
        started = self.tmp / "rex-started"
        fake = self.tmp / "rex"
        fake.write_text(f'#!/bin/sh\ncase "$*" in *"server start"*) touch "{started}"; sleep 2 ;; esac\nexit 0\n')
        fake.chmod(0o755)
        proc = subprocess.Popen([str(ROOT / "scripts" / "smoke.sh"), "--state-dir", str(self.state)], cwd=str(self.tmp),
                                env={**os.environ, "ATHENA_REX_BIN": str(fake)}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        self.assertTrue(started.exists(), "smoke never reached its server start")
        proc.terminate()
        self.assertEqual(proc.wait(timeout=30), 143)


class OwnServer(Case):
    """Round 2: any socket under the scratch root counted as private, so another worker's server could be named."""

    def _folder(self, owner=None):
        folder = Path(tempfile.mkdtemp(prefix="o", dir=SANDBOX["SOCKETS"]))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        if owner:
            (folder / world.SERVER_MARK).write_text(str(owner))
        return f"unix://{folder}/s.sock"

    def test_only_this_state_dirs_own_server_is_open(self):
        self.assertTrue(world.private_server(self._folder(store.state_dir())))

    def test_another_runs_server_is_not(self):
        self.assertFalse(world.private_server(self._folder("/private/tmp/somewhere/else/state")))

    def test_a_server_with_no_owner_is_not(self):
        self.assertFalse(world.private_server(self._folder()))

    def test_the_owner_is_compared_by_identity(self):
        self.assertTrue(world.private_server(self._folder(str(store.state_dir()).upper())))


class UnixSockets(Case):
    """Finding 9: the sandbox let tests connect to any Unix socket, the human's Rex socket included."""

    def _connect_from_child(self, path):
        code = f"import socket; s = socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect({path!r})"
        return subprocess.run(["/usr/bin/python3", "-I", "-c", code], capture_output=True, text=True)

    def _listen(self, folder):
        path = os.path.join(folder, "s")
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)
        return path

    def test_a_socket_outside_the_allowed_folder_is_denied(self):
        folder = tempfile.mkdtemp(prefix="ar", dir=f"/private/tmp/claude-{os.getuid()}")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        out = self._connect_from_child(self._listen(folder))
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not permitted", out.stderr.lower())

    def test_a_socket_in_the_allowed_folder_works(self):
        folder = tempfile.mkdtemp(prefix="s", dir=SANDBOX["SOCKETS"])
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        out = self._connect_from_child(self._listen(folder))
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_the_canary_stops_it_too(self):
        saved = canary._State.log
        canary._State.log = str(self.tmp / "direct.log")
        try:
            with self.assertRaises(canary.CanaryError):
                canary._check_connect(f"{lab.real_home()}/Library/Application Support/rex/server.sock")
        finally:
            canary._State.log = saved


class IgnoredFiles(Case):
    """Finding 10: git-ignored files went with the worktree, while the doc said cleanup can't lose work."""

    def _worker(self, ignores=".env\nnode_modules/\n*.pyc\nnotes/\n"):
        self.write_config(worktree_root=str(self.home / "worktrees"), projects_dir=str(self.tmp / "projects"))
        repo = self.repo()
        path = self.worktree(repo)
        (path / ".gitignore").write_text(ignores)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "ignores")
        git(path, "push", "-q")
        spawn_record("w", path, repo)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")
        return repo, path

    def _caches(self, path):
        (path / "node_modules" / "x").mkdir(parents=True)
        (path / "node_modules" / "x" / "i.js").write_text("x")
        (path / "a.pyc").write_text("x")

    def test_a_retire_holds_for_ignored_work_and_names_it(self):
        _, path = self._worker()
        (path / ".env").write_text("SECRET=only here")
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn(".env", str(caught.exception))
        self.assertTrue((path / ".env").exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_regenerable_caches_do_not_hold_a_retire(self):
        _, path = self._worker()
        self._caches(path)
        retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())

    def test_force_keeps_ignored_work_in_the_trash(self):
        _, path = self._worker()
        self._caches(path)
        (path / ".env").write_text("only here")
        (path / "notes").mkdir()
        (path / "notes" / "a.md").write_text("mine")
        out = retire.retire(caps.CTX, "w", force=True)
        kept = Path(out["ignored_kept"])
        self.assertEqual((kept / ".env").read_text(), "only here")
        self.assertEqual((kept / "notes" / "a.md").read_text(), "mine")
        self.assertFalse((kept / "node_modules").exists())
        self.assertFalse(path.exists())

    def test_a_config_can_name_more_caches(self):
        _, path = self._worker()
        self.write_config(worktree_root=str(self.home / "worktrees"), rex={"disposable": ["notes"]})
        (path / "notes").mkdir()
        (path / "notes" / "a.md").write_text("generated")
        retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())

    def test_clean_holds_for_ignored_work_and_says_so(self):
        _, path = self._worker()
        retire.retire(caps.CTX, "w", keep_worktree=True)
        (path / ".env").write_text("only here")
        later = time.time() + 90 * 86400
        scan = workspaces.Scan()
        row = next(r for r in workspaces.listing(scan, sizes=False, now=later) if r["path"] == str(path))
        self.assertEqual(row["state"], "held")
        self.assertIn(".env", row["why"])
        self.assertEqual(workspaces.clean(apply=True, now=later, scan=workspaces.Scan()), [])
        self.assertTrue((path / ".env").exists())

    def test_clean_removes_one_holding_only_caches(self):
        _, path = self._worker()
        retire.retire(caps.CTX, "w", keep_worktree=True)
        self._caches(path)
        later = time.time() + 90 * 86400
        out = workspaces.clean(apply=True, now=later, scan=workspaces.Scan())
        self.assertIn("removed", out[0]["action"])
        self.assertFalse(path.exists())

    def test_work_ignored_between_look_and_removal_keeps_it(self):
        _, path = self._worker()
        (path / ".env").write_text("late")
        self.assertIn("kept", workspaces._remove({"path": str(path), "kind": "worktree", "owner": "w"}))
        self.assertTrue((path / ".env").exists())

    def test_an_ignored_embedded_repo_is_held(self):
        _, path = self._worker("vendored/\n")
        inner = path / "vendored"
        inner.mkdir()
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        self.assertTrue(gitops.holds_work(gitops.unsaved(path)))

    def test_the_doc_says_what_is_true(self):
        text = (ROOT / "docs" / "workspaces.md").read_text()
        self.assertIn("git-ignored", text)


class TrustSkip(Case):
    """Finding 11: with the door shut on the real HOME, trust skipped in silence and the launch stopped at the prompt."""

    def test_spawn_says_trust_was_not_written(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        os.environ["HOME"] = lab.real_home()
        out = spawn.spawn(caps.CTX, repo, "t/a", "a", prompt="hi", timeout=0.1, fetch=False)
        self.assertIn("real_world", out["trust_skipped"])
        self.assertIn("trust", ledger.get("a")["pending_reason"])
        self.assertTrue(ledger.get("a").get("trust_skipped"))

    def test_the_command_prints_it(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        repo = self.repo()
        out = self.cli("spawn", "--repo", str(repo), "--branch", "t/a", "--name", "a", "--prompt", "hi", "--timeout", "0.1", "--no-fetch",
                       env={"HOME": lab.real_home()})
        self.assertEqual(out.returncode, 3, out.stderr)  # pending at a startup prompt, as the old athena exits
        self.assertIn("real_world", out.stdout + out.stderr)

    def test_a_written_trust_says_nothing(self):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        out = spawn.spawn(caps.CTX, self.repo(), "t/a", "a", prompt="hi", timeout=0.1, fetch=False)
        self.assertNotIn("trust_skipped", out)

    def test_hq_says_it_too(self):
        self.write_config(projects_dir=str(self.tmp))
        os.environ["HOME"] = lab.real_home()
        out = hq.open_hq(focus=False, routine=False)
        self.assertIn("real_world", out.get("trust_skipped", ""))


__all__ = ["json", "store", "UserError"]
