"""What a repo's setup script makes in a new worktree is not work: a file it copied from the main checkout while it is still the
same bytes, and output it declares as generated. Anything else ignored holds a retire as before."""
from pathlib import Path

from harness import Case, git, spawn_record

from athena.caps import retire
from athena.kernel import agents, caps, gitops, ledger

SETUP = """#!/bin/sh
# athena-copies: config/.env certs
# athena-generates: .generated
cp "$ATHENA_REPO/config/.env" config/.env
"""


class SetupFiles(Case):
    def _tree(self, setup=SETUP, ignores=".env\ncerts/\n.generated/\nnotes/\n"):
        self.write_config(worktree_root=str(self.home / "worktrees"), projects_dir=str(self.tmp / "projects"))
        repo = self.repo()
        (repo / ".gitignore").write_text(ignores)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "ignores")
        git(repo, "push", "-q")
        path = self.worktree(repo)
        if setup is not None:
            (self.state / "repos").mkdir(exist_ok=True)
            (self.state / "repos" / f"{repo.name}.setup").write_text(setup)
        return repo, path

    def _both(self, repo, path, relative, text="SECRET=1\n"):
        for root in (repo, path):
            self._write(root, relative, text)

    def _write(self, root, relative, text="x"):
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _worker(self, path, repo):
        spawn_record("w", path, repo)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")

    def test_an_identical_copy_is_not_work_and_retires(self):
        repo, path = self._tree()
        self._both(repo, path, "config/.env")
        self._both(repo, path, "certs/local.pem", "-----BEGIN-----\n")
        self.assertEqual(gitops.ignored_work(path), [])
        self._worker(path, repo)
        retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())
        self.assertTrue((repo / "config" / ".env").exists())

    def test_an_edited_copy_is_work_and_refuses_naming_it(self):
        repo, path = self._tree()
        self._both(repo, path, "config/.env")
        self._both(repo, path, "certs/local.pem", "-----BEGIN-----\n")
        self._write(path, "config/.env", "SECRET=edited\n")
        self._write(path, "certs/local.pem", "-----OTHER-----\n")
        self.assertEqual(gitops.ignored_work(path), ["certs/local.pem", "config/.env"])
        self._worker(path, repo)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("config/.env", str(caught.exception))
        self.assertTrue(path.exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_a_copy_whose_source_is_gone_or_a_link_is_work(self):
        repo, path = self._tree()
        self._write(path, "config/.env", "SECRET=1\n")
        self._both(repo, path, "certs/a.pem")
        (path / "certs" / "a.pem").unlink()
        (path / "certs" / "a.pem").symlink_to(repo / "certs" / "a.pem")
        self.assertEqual(gitops.ignored_work(path), ["certs/a.pem", "config/.env"])

    def test_a_source_reached_through_a_link_is_no_copy(self):
        repo, path = self._tree()
        self._write(path, "certs/local.pem", "edited here only")
        (repo / "certs").symlink_to(path / "certs")
        self.assertEqual(gitops.ignored_work(path), ["certs/local.pem"])

    def test_a_new_file_in_a_copied_folder_is_work(self):
        repo, path = self._tree()
        self._both(repo, path, "certs/a.pem")
        self._write(path, "certs/mine.pem")
        self.assertEqual(gitops.ignored_work(path), ["certs/mine.pem"])

    def test_a_declared_output_is_not_work_and_retires(self):
        repo, path = self._tree()
        self._write(path, "site/docs/.generated/server.ts")
        self._write(path, "site/guide/.generated/browser.ts")
        self.assertEqual(gitops.ignored_work(path), [])
        self._worker(path, repo)
        retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())

    def test_a_secret_in_a_declared_output_is_still_work(self):
        _, path = self._tree()
        self._write(path, "site/docs/.generated/prod.env")
        self.assertEqual(gitops.ignored_work(path), ["site/docs/.generated/prod.env"])

    def test_an_undeclared_env_refuses_as_before(self):
        repo, path = self._tree()
        self._both(repo, path, "services/api/.env")
        self._write(path, "notes/todo.md")
        self.assertEqual(gitops.ignored_work(path), ["notes/todo.md", "services/api/.env"])
        self._worker(path, repo)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("services/api/.env", str(caught.exception))

    def test_without_a_setup_file_nothing_changes(self):
        repo, path = self._tree(setup=None)
        self._both(repo, path, "config/.env")
        self._write(path, "site/.generated/x.ts")
        self.assertEqual(gitops.ignored_work(path), ["config/.env", "site/.generated/x.ts"])

    def test_a_nested_repo_under_a_declared_name_is_work(self):
        repo, path = self._tree()
        inner = path / "site" / ".generated"
        inner.mkdir(parents=True)
        git(inner, "init", "-q")
        self.assertEqual(gitops.ignored_work(path), ["site/.generated/"])

    def test_bad_declarations_exempt_nothing(self):
        bad = ("# athena-copies: ../main/config/.env /etc/passwd . config/../config/.env\n"
               "# athena-generates: * .env cred* a/b .*\n")
        repo, path = self._tree(setup=bad, ignores="*\n!.gitignore\n")
        self._both(repo, path, "config/.env")
        self._write(path, "x/.env")
        self.assertEqual(gitops.ignored_work(path), ["config/.env", "x/.env"])

    def test_the_main_checkout_is_never_its_own_copy(self):
        repo, _ = self._tree()
        self._write(repo, "config/.env")
        self.assertEqual(gitops.ignored_work(repo), ["config/.env"])

    def test_the_size_limit_still_applies_to_an_edited_copy_and_not_to_an_identical_one(self):
        repo, path = self._tree()
        big = "A" * (3 << 20)
        self._both(repo, path, "config/.env", big)
        self.write_config(worktree_root=str(self.home / "worktrees"), rex={"ignored_copy_max_mb": 1})
        self._worker(path, repo)
        self._write(path, "config/.env", big + "edited")
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w", force=True)
        self.assertIn("ignored_copy_max_mb", str(caught.exception))
        self.assertTrue(path.exists())
        self._write(path, "config/.env", big)
        retire.retire(caps.CTX, "w", force=True)
        self.assertFalse(path.exists())
