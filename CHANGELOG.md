# Changelog

## Unreleased

- **Retire after a squash merge.** `athena retire <w>` no longer needs `--force` for a worker whose PR was squash-merged:
  when its branch reads as unpushed, retire asks `gh` for the branch's PR, and a PR merged into the default branch at the
  worktree's very head counts as saved (`w retired; its commits are saved, merged as <sha> in #<n>`). A commit made after
  the merge still refuses, and is named; so does a rewritten head, and anything uncommitted or git-ignored, as before. The
  head is still backed up under `refs/athena-backup/<w>/`. See `docs/workspaces.md`.
- **Quiet watch.** What athena says to HQ (the watch's wakes, doctor's rotation ask, HQ's routine and a rotated or
  revived HQ's first prompt) goes to HQ's cross-session inbox, a dim `› Message from @athena-watch: …` line, instead of
  being typed into its input box. A half-typed prompt stays put, and a busy HQ reads it between tool calls. Typing stays
  the fallback when the inbox can't be reached, and `athena doctor` says which route is in use (`hq-delivery`). An HQ
  started before this release types until its next start. See `docs/watch.md`.
- **Pi workers.** `athena spawn --agent pi` launches a worker that runs Pi instead of Claude Code, on Pi's own default
  model (DeepSeek, say) unless `--model` names one, which is passed as written. Claude stays the default everywhere,
  and HQ is always Claude. athena's Pi extension (`pi-extension/athena.ts`, loaded per launch with `-e`, never installed)
  sends the same hook events, so a Pi turn is held to a filed report like a Claude's, and the git rules hold. HQ
  messages a Pi worker with `athena send`, through its inbox. Status, the watch, the board and the dashboard show it,
  marked `[pi]`. Retire, revive, doctor and review handle it, and progress, flame and the usage-limit reader skip it.
  `--agent pi --ultracode` is refused, and so is a `pi` that isn't on PATH. `scripts/pi-proof.sh` runs one on a
  private Rex server. See `docs/commands.md`.

## 1.0.0

The first release of athena on Rex, which replaces the herdr athena.

- **Workers in Rex.** One Rex session per repo and a window per worker, named for its ticket. Rex keeps the sessions:
  quitting athena stops none of them.
- **Install, update, uninstall.** `bin/athena install --services` from a checkout installs athena and its background
  jobs. `athena update` brings the newest release beside the running one and keeps it only if doctor finds nothing
  newly broken, rolling back otherwise; `athena install --rollback` goes back by hand. `athena --version` says which
  runs, and `athena doctor` says when a newer one is out.
- **Upgrade from the herdr athena.** `scripts/upgrade-from-herdr.py run` (with `--dry-run` to look first) moves the
  state, the jobs, the command and the plugin over, and `rollback` goes back. See `docs/upgrade-from-herdr.md`.
- **Fixes.** `athena hq` run in HQ's own pane after a Rex restore no longer leaves `hq.json` naming the closed pane.
  The rest are in `docs/release-review.md`.
