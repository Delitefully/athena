# Changelog

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
- **Renamed from athena-rex.** The command, the package and the install are `athena`. `athena-rex` still works as an
  alias that says so, private modules that import `athena_rex` still load, and installing over an athena-rex install
  turns its command into that alias and retires its launchd jobs.
- **Fixes.** `athena hq` run in HQ's own pane after a Rex restore no longer leaves `hq.json` naming the closed pane.
  The rest are in `docs/release-review.md`.
