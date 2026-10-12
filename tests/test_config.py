import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import config, paths, quota


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        # Resolve every path from the config, not from the test env's overrides.
        for var in ("ATHENA_WORKTREE_ROOT", "ATHENA_QUOTA_FILE"):
            os.environ.pop(var, None)
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
        self.assertIsNone(cfg["quota_file"])
        self.assertEqual(paths.worktree_root(), self.home / ".worktrees")
        self.assertEqual(paths.projects_dir(), self.home)
        self.assertIsNone(paths.quota_file())

    def test_every_path_comes_from_the_file(self):
        self.write({"projects_dir": "~/code", "worktree_root": "~/code/.wt", "quota_file": "~/q.json",
                    "model": "sonnet", "effort": "medium", "human_merge_repos": ["web"], "reapproval_repos": []})
        self.assertEqual(paths.projects_dir(), self.home / "code")
        self.assertEqual(paths.worktree_root(), self.home / "code" / ".wt")
        self.assertEqual(paths.quota_file(), self.home / "q.json")
        self.assertEqual(config.get("model"), "sonnet")
        self.assertEqual(config.get("effort"), "medium")
        self.assertTrue(config.listed("human_merge_repos", "/somewhere/web"))
        self.assertFalse(config.listed("human_merge_repos", "/somewhere/api"))

    def test_the_worktree_root_follows_the_projects_folder_when_unset(self):
        self.write({"projects_dir": "~/src"})
        self.assertEqual(paths.worktree_root(), self.home / "src" / ".worktrees")

    def test_environment_variables_override_the_file(self):
        self.write({"projects_dir": "~/code", "worktree_root": "~/code/.wt", "model": "sonnet", "effort": "low",
                    "quota_file": "~/q.json"})
        self.env.set("ATHENA_WORKTREE_ROOT", str(Path(self.tmp.name) / "wt"))
        self.env.set("ATHENA_HQ_CWD", str(Path(self.tmp.name) / "hq"))
        self.env.set("ATHENA_MODEL", "haiku")
        self.env.set("ATHENA_EFFORT", "max")
        self.env.set("ATHENA_QUOTA_FILE", str(Path(self.tmp.name) / "quota.json"))
        self.assertEqual(paths.worktree_root(), Path(self.tmp.name) / "wt")
        self.assertEqual(paths.projects_dir(), Path(self.tmp.name) / "hq")
        self.assertEqual(config.get("model"), "haiku")
        self.assertEqual(config.get("effort"), "max")
        self.assertEqual(paths.quota_file(), Path(self.tmp.name) / "quota.json")
        self.assertEqual(config.source("model"), "env ATHENA_MODEL")
        self.assertEqual(config.source("human_merge_repos"), "default")

    def test_without_a_quota_file_the_gate_is_skipped(self):
        self.write({"quota_file": None})
        self.assertEqual(quota.gate(), (True, "quota unknown (no quota_file configured); not gating"))

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


class LegacyTest(unittest.TestCase):
    """A state dir from before config.json: today's effective values, unchanged, until `athena init` writes them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        for var in ("ATHENA_WORKTREE_ROOT", "ATHENA_QUOTA_FILE"):
            os.environ.pop(var, None)
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
        self.assertEqual(paths.quota_file(),
                         self.home / ".local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json")
        self.assertEqual((config.get("model"), config.get("effort")), ("opus", "high"))
        self.assertTrue(config.listed("human_merge_repos", "/x/platform"))
        self.assertTrue(config.listed("human_merge_repos", "/x/infrastructure"))
        self.assertTrue(config.listed("reapproval_repos", "/x/platform"))
        self.assertFalse(config.listed("reapproval_repos", "/x/infrastructure"))
        self.assertEqual(config.source("projects_dir"), "migrated (no config.json yet)")

    def test_the_repo_maps_merge_policy_is_migrated(self):
        (paths.state_dir() / "repo-map.md").write_text(
            "# Repos\n- ABC: ~/src/web\n\n# Merge policy\n- human-merged: `web`, api\n"
            "- re-approval after push: web\n")
        cfg = config.load()
        self.assertEqual((cfg["human_merge_repos"], cfg["reapproval_repos"]), (["web", "api"], ["web"]))
        self.assertEqual(cfg["projects_dir"], "~/Developer")

    def test_a_repo_map_without_a_merge_policy_keeps_the_legacy_lists(self):
        (paths.state_dir() / "repo-map.md").write_text("# Repos\n- ABC: ~/src/web\n")
        self.assertEqual(config.load(), config.LEGACY)

    def test_a_written_config_wins_over_the_legacy_values(self):
        config.write({**config.DEFAULTS, "projects_dir": "~/code"})
        self.assertFalse(config.is_legacy())
        self.assertEqual(config.load()["human_merge_repos"], [])


if __name__ == "__main__":
    unittest.main()
