# Configuration

## Requirements in full

- macOS or Linux, with Python 3.9 or later as `/usr/bin/python3` (athena is standard-library Python).
- [herdr](https://herdr.dev) 0.9.1 or later. `athena init` offers to install it if it is missing.
- [Claude Code](https://claude.com/claude-code) as `claude`, with this plugin installed ([Install](../README.md#install)). The watch mod needs Claude Code 2.1.286 or later; older versions fall back to Monitor.
- [gh](https://cli.github.com), signed in (`gh auth login`), and a git identity (`git config --global user.name` and `user.email`): workers push branches and open PRs.
- Go, only for the dashboard (built on first use).

## `athena init`

`athena init` checks what athena needs and prints each check as ✓ or ✗ with its fix: herdr (and its version), `claude` and the athena plugin, `gh` signed in, and a git identity. If herdr is missing it shows the install command from herdr.dev (`curl -fsSL https://herdr.dev/install.sh | sh`, or `brew install herdr`) and runs it only if you say yes. Then it asks for:

- your **projects folder**, where your repos live and HQ starts (default: the folder holding the repo you run it from, else `~/code`);
- the **worktree root**, one git worktree per worker (default `<projects>/.worktrees`);
- the **repos you merge yourself** (athena prepares them and stops), and the repos where **a push after approval needs a new approval**;
- the workers' default **model and effort** (opus/high).

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
  "effort": "high"
}
```

- `human_merge_repos`: repos (by folder name) the human merges; HQ prepares them and stops. `reapproval_repos`: repos where a push after approval needs a new approval; their workers get `ATHENA_REAPPROVAL=1` and say so.
- `athena watch` ([How it works](how-it-works.md#doorbells-ci-and-slack)) reads six optional keys, which `athena init` does not ask for and `config.json` holds only once you add them by hand:
  - `watch_main_moved` (default `"conflicts"`): when `<repo> main moved` wakes HQ. `"conflicts"`: never; HQ hears `<w> pr conflicts #<n>` instead when a live worker's open PR turns CONFLICTING (told in every mode). `"active"`: only while that repo has a live worker working or blocked, or a live worker's open PR not yet approved; `"always"`; `"never"`. An unknown value counts as `"conflicts"`. A line it keeps back goes to `logs/watch-suppressed.log`.
  - `ci_repos` (default `[]`): repos whose default branch the watch checks for failed CI runs even with no live worker on them, each a path (`~/Developer/web`) or an `owner/name`. Repos with a live worker are always checked.
  - `slack_channels` (default `[]`): Slack channel ids the Slack doorbell polls. It always reads your DMs and group DMs.
  - `slack_ext_channels` (default `true`): also poll every `ext-` channel you are a member of.
  - `slack_allow_bots` (default `[]`): bot or app ids whose messages count; no other bot's do.
  - `slack_dm_active_minutes` (default `15`): a DM or group DM you wrote in within this many minutes does not wake HQ; its new messages go to `logs/watch-suppressed.log`, and once the window lapses the doorbell rings once with the messages that came after your last one. `0` turns it off.
- `hq_update_interval_hours` (default `6`, optional like the watch's keys): `athena hq restart` skips `claude update` and the plugin updates when they last ran without a warning less than this many hours ago (kept in `hq-update.json` in the state dir), and says so in `logs/hq-restart.log`. `0` updates on every restart; `athena hq restart --update` forces one, `--no-update` skips it.
- An environment variable overrides its key: `ATHENA_HQ_CWD` (`projects_dir`), `ATHENA_WORKTREE_ROOT`, `ATHENA_MODEL`, `ATHENA_EFFORT`. `ATHENA_CONFIG` points at another file. `ATHENA_HQ_CONTEXT_WINDOW` sets the context window `athena status` and `athena hq rotate --if-context-over` measure HQ against (default: 1M when HQ's model ends in `[1m]`, else 200k).
- Upgrading from a version without `config.json`: the first athena command writes that version's values down (one line on stderr, no prompt). Nothing changes. Per-repo setup scripts are not moved: put yours in `repos/` next to `config.json`.

## Environment variables

`ATHENA_STATE_DIR`, `ATHENA_WORKTREE_ROOT` (overrides config `worktree_root`), `ATHENA_HERDR`, `ATHENA_HQ` (default `athena`), `ATHENA_MIN_COL` (70), `ATHENA_MAX_WORKERS` (4, hard cap 8), `ATHENA_PLUGIN_DIR` (load the plugin from a directory in workers), `ATHENA_HQ_PANE`, `ATHENA_DASH_PORT` (2843), `ATHENA_LINEAR_WORKSPACE` (the Linear workspace in ticket links; or `linear_workspace` in `~/.local/state/athena/dash.json`; neither leaves ticket ids unlinked).

## State files

State lives in `~/.local/state/athena/` (`ATHENA_STATE_DIR` moves it): `ledger.jsonl`, `workers/`, `briefs/`, `settings/`, `claims/`, `board.json`, `hq.json`, `stack.json`, and for the dashboard `watch.json`, `history.jsonl`, `needs-you.json`, `actions.jsonl`. Logs are in `logs/` there (`herdr-server.log`, `hq-restart.log`, `dash.log`, and `watch-suppressed.log`, the watch lines kept from HQ, rotated to `.1` at 1 MB). Who writes each dashboard file and what it holds is in [Dashboard](dashboard.md#files-read).

## Per-repo worktree setup

`repos/example.setup` is an example per-repo worktree setup. Your own go in `repos/<repo>.setup` next to `config.json` (looked up there first); `athena spawn` runs the one named after the repo's folder in each new worktree.
