# Configuration

## Requirements in full

- macOS or Linux, with Python 3.9 or later as `/usr/bin/python3` (athena is standard-library Python).
- [herdr](https://herdr.dev) 0.9.1 or later. `athena init` offers to install it if it is missing.
- [Claude Code](https://claude.com/claude-code) as `claude`, with this plugin installed ([Install](../README.md#install)). The watch mod needs Claude Code 2.1.286 or later; older versions fall back to Monitor.
- [gh](https://cli.github.com), signed in (`gh auth login`), and a git identity (`git config --global user.name` and `user.email`): workers push branches and open PRs.
- Go, only for the dashboard (built on first use).
- Optional: the herdr-agent-quota herdr plugin, for the 5-hour quota gate. Without it the gate is skipped.

## `athena init`

`athena init` checks what athena needs and prints each check as ✓ or ✗ with its fix: herdr (and its version), `claude` and the athena plugin, `gh` signed in, a git identity, and the optional herdr plugins. If herdr is missing it shows the install command from herdr.dev (`curl -fsSL https://herdr.dev/install.sh | sh`, or `brew install herdr`) and runs it only if you say yes. Then it asks for:

- your **projects folder**, where your repos live and HQ starts (default: the folder holding the repo you run it from, else `~/code`);
- the **worktree root**, one git worktree per worker (default `<projects>/.worktrees`);
- the **repos you merge yourself** (athena prepares them and stops), and the repos where **a push after approval needs a new approval**;
- the workers' default **model and effort** (opus/high);
- the herdr-agent-quota **state file**, if you have that plugin.

and writes them to `config.json` in athena's state dir. Run it again any time to change a value; the flags for scripts are in [Commands](commands.md#athena-init-in-scripts).

## config.json

`athena init` writes `config.json` in the state dir (`~/.local/state/athena/` unless `ATHENA_STATE_DIR` moves it); `athena config show` prints each value and where it came from, `athena config path` the file. Edit it by hand or rerun `athena init`.

```json
{
  "version": 1,
  "projects_dir": "~/code",
  "worktree_root": "~/code/.worktrees",
  "human_merge_repos": ["web"],
  "reapproval_repos": [],
  "model": "opus",
  "effort": "high",
  "quota_file": null
}
```

- `human_merge_repos`: repos (by folder name) the human merges; HQ prepares them and stops. `reapproval_repos`: repos where a push after approval needs a new approval; their workers get `ATHENA_REAPPROVAL=1` and say so.
- `quota_file`: the herdr-agent-quota plugin's `claude-statusline.json`. With none, `athena spawn` does not gate on the 5-hour quota.
- An environment variable overrides its key: `ATHENA_HQ_CWD` (`projects_dir`), `ATHENA_WORKTREE_ROOT`, `ATHENA_MODEL`, `ATHENA_EFFORT`, `ATHENA_QUOTA_FILE`. `ATHENA_CONFIG` points at another file.
- Upgrading from a version without `config.json`: the first athena command writes that version's values down (one line on stderr, no prompt). Nothing changes. Per-repo setup scripts are not moved: put yours in `repos/` next to `config.json`.

## Environment variables

`ATHENA_STATE_DIR`, `ATHENA_WORKTREE_ROOT` (overrides config `worktree_root`), `ATHENA_HERDR`, `ATHENA_HQ` (default `athena`), `ATHENA_MIN_COL` (70), `ATHENA_MAX_WORKERS` (4, hard cap 6), `ATHENA_PLUGIN_DIR` (load the plugin from a directory in workers), `ATHENA_HQ_PANE`, `ATHENA_DASH_PORT` (2843), `ATHENA_LINEAR_WORKSPACE` (the Linear workspace in ticket links; or `linear_workspace` in `~/.local/state/athena/dash.json`; neither leaves ticket ids unlinked).

## State files

State lives in `~/.local/state/athena/` (`ATHENA_STATE_DIR` moves it): `ledger.jsonl`, `workers/`, `briefs/`, `settings/`, `claims/`, `board.json`, `hq.json`, `stack.json`, and for the dashboard `watch.json`, `history.jsonl`, `needs-you.json`, `actions.jsonl`. Logs are in `logs/` there (`herdr-server.log`, `hq-restart.log`, `dash.log`). Who writes each dashboard file and what it holds is in [Dashboard](dashboard.md#files-read).

## Per-repo worktree setup

`repos/example.setup` is an example per-repo worktree setup. Your own go in `repos/<repo>.setup` next to `config.json` (looked up there first); `athena spawn` runs the one named after the repo's folder in each new worktree.
