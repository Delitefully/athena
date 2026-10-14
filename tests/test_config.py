import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import config, paths, pr, watch

# The shape of a real repo map's merge policy (synthetic names): owners in parentheses, prose after the list.
REPO_MAP = ("# Repos\n- ABC: ~/src/web (acme/web)\n\n# Merge policy\n"
            "- human-merged: web (acme/web), api (acme/api). The human merges these; athena never does.\n"
            "- re-approval after push: web. A push after approval needs a new approval there.\n")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        # Resolve every path from the config, not from the test env's overrides.
        os.environ.pop("ATHENA_WORKTREE_ROOT", None)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env.set("HOME", str(self.home))

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def write(self, data):
        paths.ensure()
        config.config_file().write_text(json.dumps(data))

    def test_the_file_lives_in_the_state_dir(self):
        self.assertEqual(config.config_file(), paths.state_dir() / "config.json")
        self.env.set("ATHENA_CONFIG", str(Path(self.tmp.name) / "elsewhere.json"))
        self.assertEqual(config.config_file(), Path(self.tmp.name) / "elsewhere.json")

    def test_a_fresh_install_has_no_personal_defaults(self):
        cfg = config.load()
        self.assertFalse(config.exists())
        self.assertFalse(config.is_legacy())
        self.assertEqual(cfg["human_merge_repos"], [])
        self.assertEqual(cfg["reapproval_repos"], [])
        self.assertEqual(paths.worktree_root(), self.home / ".worktrees")
        self.assertEqual(paths.projects_dir(), self.home)

    def test_every_path_comes_from_the_file(self):
        self.write({"projects_dir": "~/code", "worktree_root": "~/code/.wt",
                    "model": "sonnet", "effort": "medium", "human_merge_repos": ["web"], "reapproval_repos": []})
        self.assertEqual(paths.projects_dir(), self.home / "code")
        self.assertEqual(paths.worktree_root(), self.home / "code" / ".wt")
        self.assertEqual(config.get("model"), "sonnet")
        self.assertEqual(config.get("effort"), "medium")
        self.assertTrue(config.listed("human_merge_repos", "/somewhere/web"))
        self.assertFalse(config.listed("human_merge_repos", "/somewhere/api"))

    def test_listed_ignores_case_and_reads_a_slug_by_its_repo_name(self):
        self.write({"human_merge_repos": ["platform"]})
        for repo in ("/src/Platform", "sun-security/Platform", "sun-security/platform", "PLATFORM"):
            self.assertTrue(config.listed("human_merge_repos", repo), repo)
        self.assertFalse(config.listed("human_merge_repos", "sun-security/platform-api"))

    def test_human_merge_fails_closed(self):
        # The dashboard offers Merge only where a move says human_merge is False: anything uncertain must say True.
        approved = {"state": "OPEN", "requested": [], "review": "APPROVED", "checks": "success"}
        self.write({"human_merge_repos": ["platform"]})
        self.assertTrue(watch._move(approved, False, "sun-security/Platform", "sun-security/Platform")["human_merge"])
        # A local folder named apart from its GitHub repo: either name being listed is enough.
        self.assertTrue(watch._move(approved, False, "/src/plat", "sun-security/platform")["human_merge"])
        self.assertFalse(watch._move(approved, False, "/src/api", "acme/api")["human_merge"])
        config.config_file().write_text('{"human_merge_repos": ["plat')  # cut off mid-write
        self.assertTrue(watch._move(approved, False, "/src/api", "acme/api")["human_merge"])

    def test_the_worktree_root_follows_the_projects_folder_when_unset(self):
        self.write({"projects_dir": "~/src"})
        self.assertEqual(paths.worktree_root(), self.home / "src" / ".worktrees")

    def test_environment_variables_override_the_file(self):
        self.write({"projects_dir": "~/code", "worktree_root": "~/code/.wt", "model": "sonnet", "effort": "low"})
        self.env.set("ATHENA_WORKTREE_ROOT", str(Path(self.tmp.name) / "wt"))
        self.env.set("ATHENA_HQ_CWD", str(Path(self.tmp.name) / "hq"))
        self.env.set("ATHENA_MODEL", "haiku")
        self.env.set("ATHENA_EFFORT", "max")
        self.assertEqual(paths.worktree_root(), Path(self.tmp.name) / "wt")
        self.assertEqual(paths.projects_dir(), Path(self.tmp.name) / "hq")
        self.assertEqual(config.get("model"), "haiku")
        self.assertEqual(config.get("effort"), "max")
        self.assertEqual(config.source("model"), "env ATHENA_MODEL")
        self.assertEqual(config.source("human_merge_repos"), "default")

    def test_a_broken_file_is_an_error_that_names_it(self):
        paths.ensure()
        config.config_file().write_text("{nope")
        with self.assertRaises(config.ConfigError) as caught:
            config.load()
        self.assertIn(str(config.config_file()), str(caught.exception))

    def test_write_keeps_paths_under_home_as_tilde(self):
        config.write({**config.DEFAULTS, "projects_dir": str(self.home / "code"),
                      "worktree_root": str(self.home / "code" / ".worktrees")})
        data = json.loads(config.config_file().read_text())
        self.assertEqual(data["projects_dir"], "~/code")
        self.assertEqual(data["worktree_root"], "~/code/.worktrees")
        self.assertEqual(data["version"], 1)


class RootHintTest(unittest.TestCase):
    """The worktree root in a one-line file, so the worker-status hook can read it without Python."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        os.environ.pop("ATHENA_WORKTREE_ROOT", None)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env.set("HOME", str(self.home))
        self.hint = paths.state_dir() / "worktree-root"

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_write_records_the_resolved_root(self):
        config.write({**config.DEFAULTS, "projects_dir": "~/code"})
        self.assertEqual(self.hint.read_text(), str(self.home / "code" / ".worktrees") + "\n")

    def test_an_environment_override_is_not_recorded(self):
        self.env.set("ATHENA_WORKTREE_ROOT", "/elsewhere")
        config.write({**config.DEFAULTS, "worktree_root": "~/wt"})
        self.assertEqual(self.hint.read_text(), str(self.home / "wt") + "\n")

    def test_sync_rewrites_a_root_file_older_than_the_config(self):
        config.write({**config.DEFAULTS, "worktree_root": "~/wt"})
        config.config_file().write_text(json.dumps({"worktree_root": "~/other"}))
        os.utime(self.hint, (1, 1))
        config.sync_root_hint()
        self.assertEqual(self.hint.read_text(), str(self.home / "other") + "\n")

    def test_sync_without_a_config_writes_nothing(self):
        config.sync_root_hint()
        self.assertFalse(self.hint.exists())

    def test_sync_in_an_older_install_records_its_root_before_any_config(self):
        (paths.ensure() / "ledger.jsonl").write_text("")
        config.sync_root_hint()
        self.assertFalse(config.exists())
        self.assertEqual(self.hint.read_text(), str(self.home / "Developer" / ".worktrees") + "\n")


class LegacyTest(unittest.TestCase):
    """A state dir from before config.json: today's effective values, unchanged, until `athena init` writes them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        os.environ.pop("ATHENA_WORKTREE_ROOT", None)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env.set("HOME", str(self.home))
        state = paths.ensure()
        (state / "ledger.jsonl").write_text("")
        (state / "hq.json").write_text("{}")

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_an_existing_state_dir_keeps_todays_values(self):
        self.assertTrue(config.is_legacy())
        cfg = config.load()
        self.assertEqual(cfg, config.LEGACY)
        self.assertEqual(paths.projects_dir(), self.home / "Developer")
        self.assertEqual(paths.worktree_root(), self.home / "Developer" / ".worktrees")
        self.assertEqual((config.get("model"), config.get("effort")), ("opus", "high"))
        self.assertEqual((cfg["human_merge_repos"], cfg["reapproval_repos"]), ([], []))  # only the repo map names repos
        self.assertEqual(config.source("projects_dir"), "migrated (no config.json yet)")

    def merge_lists(self, text):
        (paths.state_dir() / "repo-map.md").write_text(text)
        cfg = config.load()
        return cfg["human_merge_repos"], cfg["reapproval_repos"]

    def test_the_repo_maps_merge_policy_is_migrated(self):
        self.assertEqual(self.merge_lists(REPO_MAP), (["web", "api"], ["web"]))
        self.assertTrue(config.listed("human_merge_repos", "/x/api"))
        self.assertFalse(config.listed("human_merge_repos", "/x/athena"))
        self.assertEqual(config.load()["projects_dir"], "~/Developer")

    def test_only_repo_names_are_read_from_a_merge_policy_line(self):
        self.assertEqual(self.merge_lists("- human-merged: `web`, api\n- re-approval after push: none\n"),
                         (["web", "api"], []))
        self.assertEqual(self.merge_lists("* Human-merged: acme/web, ~/src/api.\n"), (["web", "api"], []))
        self.assertEqual(self.merge_lists("- human-merged: my.repo (acme/my.repo); see notes.\n"), (["my.repo"], []))
        self.assertEqual(self.merge_lists("- human-merged: web!, (acme/x), api\n"), (["api"], []))

    def test_a_repo_map_without_a_merge_policy_keeps_the_legacy_lists(self):
        (paths.state_dir() / "repo-map.md").write_text("# Repos\n- ABC: ~/src/web\n")
        self.assertEqual(config.load(), config.LEGACY)

    def test_a_written_config_wins_over_the_legacy_values(self):
        config.write({**config.DEFAULTS, "projects_dir": "~/code"})
        self.assertFalse(config.is_legacy())
        self.assertEqual(config.load()["human_merge_repos"], [])


if __name__ == "__main__":
    unittest.main()
