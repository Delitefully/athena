import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import helpers
from athena_lib import cli, config, init, ledger, paths


def tool(bin_dir, name, body):
    path = Path(bin_dir) / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


HERDR_OK = 'case "$1" in --version) echo "herdr 0.9.3";; plugin) echo \'{"result":{"plugins":[]}}\';; esac'
CLAUDE_OK = ('case "$*" in "--version") echo "2.1.300 (Claude Code)";; '
             '"plugin list --json") echo \'[{"id":"athena@athena","scope":"user","enabled":true}]\';; esac')


class InitBase(unittest.TestCase):
    """A fresh HOME and a PATH holding only the fake tools a test puts there (plus the system's sh and git)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        for var in ("ATHENA_WORKTREE_ROOT", "ATHENA_QUOTA_FILE", "ATHENA_HERDR"):
            os.environ.pop(var, None)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.env.set("HOME", str(self.home))
        self.env.set("PATH", f"{self.bin}:/usr/bin:/bin")
        self.env.set("XDG_STATE_HOME", str(self.home / ".local" / "state"))
        tool(self.bin, "git", f'[ "$1 $2" = "config --global" ] && {{ echo "Ada"; exit 0; }}; exec /usr/bin/git "$@"')
        tool(self.bin, "gh", 'exit 0')

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def init(self, *args, answers=()):
        replies = iter(answers)
        asked = []

        def ask(question):
            asked.append(question)
            return next(replies)
        out = io.StringIO()
        with redirect_stdout(out):
            code = init.main(list(args), ask=ask)
        return code, out.getvalue(), asked


class FreshInstallTest(InitBase):
    def setUp(self):
        super().setUp()
        tool(self.bin, "herdr", HERDR_OK)
        tool(self.bin, "claude", CLAUDE_OK)

    def test_yes_with_projects_writes_a_config_and_every_path_resolves_from_it(self):
        code, out, asked = self.init("--yes", "--projects", "~/code")
        self.assertEqual(code, 0, out)
        self.assertEqual(asked, [])
        data = json.loads(config.config_file().read_text())
        self.assertEqual(data, {"version": 1, "projects_dir": "~/code", "worktree_root": "~/code/.worktrees",
                                "human_merge_repos": [], "reapproval_repos": [], "model": "opus",
                                "effort": "high", "quota_file": None})
        self.assertTrue((self.home / "code").is_dir())
        self.assertEqual(paths.projects_dir(), self.home / "code")
        self.assertEqual(paths.worktree_root(), self.home / "code" / ".worktrees")
        self.assertEqual(paths.worktree_path(self.home / "code" / "web", "you/abc-1-x"),
                         self.home / "code" / ".worktrees" / "web" / "abc-1-x")
        self.assertIsNone(paths.quota_file())
        self.assertIn("✓ herdr 0.9.3", out)
        self.assertIn("✓ athena plugin", out)

    def test_flags_set_every_value(self):
        code, out, _ = self.init("--yes", "--projects", "~/src", "--worktrees", "~/wt", "--human-merge", "web, api",
                                 "--reapproval", "web", "--model", "sonnet", "--effort", "medium",
                                 "--quota-file", "~/q.json")
        self.assertEqual(code, 0, out)
        cfg = config.load()
        self.assertEqual((cfg["worktree_root"], cfg["human_merge_repos"], cfg["reapproval_repos"]),
                         ("~/wt", ["web", "api"], ["web"]))
        self.assertEqual((cfg["model"], cfg["effort"], cfg["quota_file"]), ("sonnet", "medium", "~/q.json"))

    def test_interactive_prompts_offer_sensible_defaults(self):
        # Enter takes each default: the projects folder is ~/code outside a git repo, never ~/Developer.
        with mock.patch.object(init.os, "getcwd", return_value=str(self.home)):
            code, out, asked = self.init(answers=[""] * 7)
        self.assertEqual(code, 0, out)
        self.assertIn("[~/code]", asked[0])
        self.assertIn("[~/code/.worktrees]", asked[1])
        self.assertEqual(config.load()["projects_dir"], "~/code")
        self.assertNotIn("Developer", out + "".join(asked))

    def test_the_projects_default_is_the_parent_of_the_repo_you_stand_in(self):
        repo = self.home / "src" / "athena"
        repo.mkdir(parents=True)
        subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
        with mock.patch.object(init.os, "getcwd", return_value=str(repo)):
            self.assertEqual(init.suggested_projects(), "~/src")

    def test_a_detected_quota_file_is_offered(self):
        quota = self.home / ".local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json"
        quota.parent.mkdir(parents=True)
        quota.write_text("{}")
        self.init("--yes", "--projects", "~/code")
        self.assertEqual(config.load()["quota_file"],
                         "~/.local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json")

    def test_an_existing_config_is_kept_unless_a_flag_changes_it(self):
        self.init("--yes", "--projects", "~/code", "--human-merge", "web")
        self.init("--yes", "--model", "sonnet")
        cfg = config.load()
        self.assertEqual((cfg["projects_dir"], cfg["human_merge_repos"], cfg["model"]), ("~/code", ["web"], "sonnet"))

    def test_athena_init_runs_from_the_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["init", "--yes", "--projects", "~/code"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(config.exists())


class ChecksTest(InitBase):
    def test_herdr_missing_is_a_clear_failure_with_the_install_hint(self):
        tool(self.bin, "claude", CLAUDE_OK)
        code, out, asked = self.init("--yes", "--projects", "~/code")
        self.assertEqual(code, 1)
        self.assertIn("✗ herdr: not found", out)
        self.assertIn("curl -fsSL https://herdr.dev/install.sh | sh", out)
        self.assertIn("brew install herdr", out)
        self.assertEqual(asked, [])  # --yes never installs without --install-herdr
        self.assertTrue(config.exists())  # the config is still written

    def fake_installer(self):
        marker = Path(self.tmp.name) / "installed"
        self.env.set("ATHENA_HERDR_INSTALL",
                     f"touch {marker} && printf '#!/bin/sh\\n{HERDR_OK}\\n' > {self.bin}/herdr && chmod +x {self.bin}/herdr")
        return marker

    def test_the_install_runs_only_after_a_yes(self):
        tool(self.bin, "claude", CLAUDE_OK)
        marker = self.fake_installer()
        code, out, asked = self.init("--projects", "~/code", answers=["n"] + [""] * 6)
        self.assertFalse(marker.exists())
        self.assertIn("Install herdr now", asked[0])
        self.assertEqual(code, 1)

    def test_a_yes_installs_herdr_and_checks_it_again(self):
        tool(self.bin, "claude", CLAUDE_OK)
        marker = self.fake_installer()
        code, out, asked = self.init("--projects", "~/code", answers=["y"] + [""] * 6)
        self.assertTrue(marker.exists())
        self.assertIn("✓ herdr 0.9.3", out)
        self.assertEqual(code, 0, out)

    def test_install_herdr_flag_is_the_yes_for_scripts(self):
        tool(self.bin, "claude", CLAUDE_OK)
        marker = self.fake_installer()
        code, out, _ = self.init("--yes", "--install-herdr", "--projects", "~/code")
        self.assertTrue(marker.exists())
        self.assertEqual(code, 0, out)

    def test_an_old_herdr_is_a_failure(self):
        tool(self.bin, "herdr", 'echo "herdr 0.8.0"')
        tool(self.bin, "claude", CLAUDE_OK)
        code, out, _ = self.init("--yes", "--projects", "~/code")
        self.assertEqual(code, 1)
        self.assertIn("✗ herdr 0.8.0 is older than 0.9.1", out)

    def test_claude_and_the_plugin(self):
        tool(self.bin, "herdr", HERDR_OK)
        code, out, _ = self.init("--yes", "--projects", "~/code")
        self.assertEqual(code, 1)
        self.assertIn("✗ claude: not found", out)
        tool(self.bin, "claude", 'echo "[]"')
        code, out, _ = self.init("--yes")
        self.assertIn("✗ athena plugin: not installed", out)
        self.assertIn("claude plugin install athena@athena", out)
        self.env.set("ATHENA_PLUGIN_DIR", "/somewhere")
        code, out, _ = self.init("--yes")
        self.assertIn("✓ athena plugin: loaded from /somewhere", out)

    def test_gh_git_and_herdr_plugins(self):
        tool(self.bin, "herdr", HERDR_OK)
        tool(self.bin, "claude", CLAUDE_OK)
        tool(self.bin, "gh", 'exit 1')
        tool(self.bin, "git", '[ "$1 $2" = "config --global" ] && exit 1; exec /usr/bin/git "$@"')
        code, out, _ = self.init("--yes", "--projects", "~/code")
        self.assertIn("✗ gh: not signed in", out)
        self.assertIn("gh auth login", out)
        self.assertIn("✗ git identity", out)
        self.assertIn('git config --global user.name', out)
        self.assertIn("– herdr-agent-quota (optional)", out)
        self.assertEqual(code, 0, out)  # gh and git are needed for PRs, not to start athena


class MigrationTest(InitBase):
    """A state dir like today's: athena init writes today's effective values, so nothing changes after the upgrade."""

    def setUp(self):
        super().setUp()
        tool(self.bin, "herdr", HERDR_OK)
        tool(self.bin, "claude", CLAUDE_OK)
        state = paths.ensure()
        (state / "hq.json").write_text(json.dumps({"pane": "w1:p1", "workspace": "w1"}))
        ledger.append("spawn", "abc-1", repo=str(self.home / "Developer" / "web"), branch="you/abc-1-x",
                      path=str(self.home / "Developer" / ".worktrees" / "web" / "abc-1-x"), model="opus",
                      effort="high")

    def snapshot(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli.status, "collect",
                                                     side_effect=lambda w, with_pr: {"name": w["name"], "state": "working",
                                                                                     "urgency": 1, "git": {}}):
            cli.main(["status"])
        return (out.getvalue(), paths.worktree_path(self.home / "Developer" / "web", "you/abc-2-y"),
                paths.projects_dir(), paths.quota_file(), config.get("model"), config.get("effort"),
                config.listed("human_merge_repos", "platform"), config.listed("reapproval_repos", "platform"))

    def test_the_migrated_config_equals_todays_values(self):
        before = self.snapshot()
        code, out, asked = self.init("--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(asked, [])
        data = json.loads(config.config_file().read_text())
        self.assertEqual(data, {"version": 1, **config.LEGACY})
        self.assertEqual(data["projects_dir"], "~/Developer")
        self.assertEqual(data["worktree_root"], "~/Developer/.worktrees")
        self.assertEqual(data["human_merge_repos"], ["platform", "infrastructure"])
        self.assertEqual((data["model"], data["effort"]), ("opus", "high"))
        self.assertEqual(data["quota_file"], "~/.local/state/herdr/plugins/herdr-agent-quota/claude-statusline.json")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("abc-1", before[0])

    def test_interactive_init_offers_todays_values(self):
        code, out, asked = self.init(answers=[""] * 7)
        self.assertEqual(code, 0, out)
        self.assertIn("[~/Developer]", asked[0])
        self.assertIn("[platform, infrastructure]", asked[2])
        self.assertEqual(json.loads(config.config_file().read_text()), {"version": 1, **config.LEGACY})

    def plugin_repo(self):
        """A checkout of the plugin whose history had repos/web.setup, deleted since (as this upgrade deletes one)."""
        root = Path(self.tmp.name) / "plugin"
        (root / "repos").mkdir(parents=True)
        git = ["/usr/bin/git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git[:3] + ["init", "-q"], check=True)
        (root / "repos" / "web.setup").write_text("echo web setup\n")
        (root / "repos" / "example.setup").write_text("echo example\n")
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "setup"], check=True)
        (root / "repos" / "web.setup").unlink()
        subprocess.run(git + ["commit", "-qam", "move setup scripts to the config dir"], check=True)
        return root

    def test_setup_scripts_move_to_the_config_dir(self):
        root = self.plugin_repo()
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            code, out, _ = self.init("--yes")
        self.assertEqual(code, 0, out)
        moved = config.config_dir() / "repos" / "web.setup"
        self.assertEqual(moved.read_text(), "echo web setup\n")
        self.assertFalse((config.config_dir() / "repos" / "example.setup").exists())
        self.assertIn(f"moved setup script web.setup to {moved}", out)

    def test_a_setup_script_still_in_the_plugin_is_copied(self):
        root = Path(self.tmp.name) / "plugin"
        (root / "repos").mkdir(parents=True)
        (root / "repos" / "api.setup").write_text("echo api\n")
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            self.init("--yes")
        self.assertEqual((config.config_dir() / "repos" / "api.setup").read_text(), "echo api\n")

    def test_a_users_own_setup_script_is_never_overwritten(self):
        root = self.plugin_repo()
        mine = config.config_dir() / "repos" / "web.setup"
        mine.parent.mkdir(parents=True)
        mine.write_text("echo mine\n")
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            self.init("--yes")
        self.assertEqual(mine.read_text(), "echo mine\n")


if __name__ == "__main__":
    unittest.main()
