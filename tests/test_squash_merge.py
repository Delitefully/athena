"""A squash-merged PR's commits are on the default branch as one new commit, so its branch reads as unpushed once the remote
branch is deleted. Retire takes the PR's word for it: MERGED into the default branch, with the head the worktree is still on."""
import contextlib
import io
import json
from types import SimpleNamespace
from unittest import mock

from harness import Case, git, spawn_record

from athena.caps import prs, retire
from athena.kernel import agents, caps, gitops, ledger


class SquashMerged(Case):
    def _merged(self, after=False):
        """t/w pushed, squash-merged into origin's main, its remote branch deleted and pruned: what GitHub leaves behind."""
        repo = self.repo()
        path = self.worktree(repo)
        (path / "feature.txt").write_text("feature\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "the feature")
        git(path, "push", "-q")
        head = git(path, "rev-parse", "HEAD")
        git(repo, "merge", "-q", "--squash", "t/w")
        git(repo, "commit", "-q", "-m", "the feature (#7)")
        git(repo, "push", "-q", "origin", "main")
        squash = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "-q", "origin", "--delete", "t/w")
        git(path, "fetch", "-q", "--prune")
        if after:
            git(path, "commit", "-q", "--allow-empty", "-m", "a fix after the merge")
        spawn_record("w", path, repo)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")
        return repo, path, head, squash

    def _with_pr(self, head, squash, state="MERGED", base="main"):
        """gh answers the retire's merged-PR question, and nothing else (history's own lookup finds no PR)."""
        answer = {"number": 7, "url": "https://github.com/acme/proj/pull/7", "state": state, "headRefOid": head,
                  "baseRefName": base, "mergeCommit": {"oid": squash} if state == "MERGED" else None}

        def gh(*args, timeout=30):
            if "mergeCommit" in args[-1]:
                return json.dumps(answer)
            raise prs.GhError("not this one")
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(prs, "gh", side_effect=gh))
        stack.enter_context(mock.patch.object(gitops, "github_slug", return_value="acme/proj"))
        return stack

    def test_the_branch_reads_as_unpushed_without_the_pr(self):
        _, path, _, _ = self._merged()
        self.assertEqual(gitops.unsaved(path)["ahead"], 1)
        with self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("1 unpushed", str(caught.exception))
        self.assertTrue(path.exists())

    def test_a_squash_merged_branch_retires_without_force_and_keeps_its_head(self):
        repo, path, head, squash = self._merged()
        with self._with_pr(head, squash):
            out = retire.retire(caps.CTX, "w")
        self.assertFalse(path.exists())
        self.assertEqual(ledger.get("w")["state"], "retired")
        self.assertEqual(git(repo, "rev-parse", out["backup"]), head)
        self.assertTrue(out["backup"].startswith("refs/athena-backup/w/"))
        self.assertEqual(out["merged"], {"number": 7, "url": "https://github.com/acme/proj/pull/7", "head": head, "merge_commit": squash})

    def test_the_success_message_says_why(self):
        _, _, head, squash = self._merged()
        printed = io.StringIO()
        with self._with_pr(head, squash), contextlib.redirect_stdout(printed):
            retire._run(SimpleNamespace(name="w", force=False, keep_worktree=False, json=False))
        self.assertIn(f"merged as {squash[:12]} in #7", printed.getvalue())

    def test_a_commit_after_the_merge_refuses_naming_it(self):
        _, path, head, squash = self._merged(after=True)
        late = git(path, "rev-parse", "HEAD")
        with self._with_pr(head, squash), self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        message = str(caught.exception)
        self.assertIn(f"merged as {squash[:12]} in #7", message)
        self.assertIn(late[:12], message)
        self.assertIn("a fix after the merge", message)
        self.assertNotIn(head[:12], message.split("after it:")[-1])
        self.assertTrue(path.exists())
        self.assertEqual(ledger.get("w")["state"], "live")

    def test_a_head_rewritten_after_the_merge_refuses(self):
        _, path, head, squash = self._merged()
        git(path, "commit", "-q", "--amend", "-m", "the feature, reworded")
        with self._with_pr(head, squash), self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn(f"merged as {squash[:12]} in #7 at {head[:12]}", str(caught.exception))
        self.assertIn("does not build on", str(caught.exception))
        self.assertTrue(path.exists())

    def test_an_open_pr_on_the_same_head_still_refuses(self):
        _, path, head, squash = self._merged()
        with self._with_pr(head, squash, state="OPEN"), self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("1 unpushed", str(caught.exception))
        self.assertNotIn("merged as", str(caught.exception))
        self.assertTrue(path.exists())

    def test_a_pr_merged_into_another_branch_still_refuses(self):
        _, path, head, squash = self._merged()
        with self._with_pr(head, squash, base="release"), self.assertRaises(retire.RetireError):
            retire.retire(caps.CTX, "w")
        self.assertTrue(path.exists())

    def test_dirty_files_still_refuse_on_a_merged_branch(self):
        _, path, head, squash = self._merged()
        (path / "notes.txt").write_text("mine\n")
        with self._with_pr(head, squash), self.assertRaises(retire.RetireError) as caught:
            retire.retire(caps.CTX, "w")
        self.assertIn("1 uncommitted", str(caught.exception))
        self.assertTrue(path.exists())


class Unmerged(Case):
    def test_unpushed_commits_with_no_pr_refuse_as_before(self):
        repo = self.repo()
        path = self.worktree(repo)
        git(path, "commit", "-q", "--allow-empty", "-m", "unpushed")
        spawn_record("w", path, repo)
        agents.pid_file("w").parent.mkdir(parents=True, exist_ok=True)
        agents.pid_file("w").write_text("4242")
        with mock.patch.object(gitops, "github_slug", return_value="acme/proj"), \
                mock.patch.object(prs, "gh", side_effect=prs.GhError("no pull requests found for branch \"t/w\"")):
            with self.assertRaises(retire.RetireError) as caught:
                retire.retire(caps.CTX, "w")
        self.assertIn("1 unpushed", str(caught.exception))
        self.assertNotIn("merged as", str(caught.exception))
        self.assertTrue(path.exists())
        self.assertEqual(ledger.get("w")["state"], "live")
