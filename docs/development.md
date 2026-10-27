# Development

## Pieces

- `athena_lib/`: stdlib Python 3.9+. State in `~/.local/state/athena/` (`ledger.jsonl`, `workers/`, `briefs/`, `settings/`, `claims/`, `board.json`, `hq.json`, `stack.json`, and for the dashboard `watch.json`, `history.jsonl`, `needs-you.json`, `items-archive.jsonl`, `actions.jsonl`, `health.json`, `requests.jsonl`).
- `dash/`: the dashboard server, Go 1.22+: the standard library and goldmark (MIT, for an item's Markdown body), vendored in `dash/vendor/` so it builds offline, with the Instrument fonts (OFL) embedded.
- `hooks/`: Claude Code plugin hooks that write `workers/<name>.json` (a no-op outside athena workers), and `hooks/watch-mod/`, the watch mod (a no-op outside HQ).
- `hq.md`, `worker.md`, `skills/athena/SKILL.md`: the prompts; `skills/athena/never-ask.md`: what HQ does without asking and what it always asks.
- `repos/example.setup`: an example per-repo worktree setup. Your own go in `repos/<repo>.setup` next to `config.json` (looked up there first); `athena spawn` runs the one named after the repo's folder in each new worktree.
- The herdr auto-claude plugin skips spaces that `athena` claims (`~/.local/state/athena/claims/`).

## Test

`make test` (unit tests with a fake herdr, `go test` for the dashboard, and `claude plugin validate` and `claude plugin test` for the watch mod when `claude` is installed). [`tests/lab/`](../tests/lab/README.md) runs real workers against an isolated herdr server. `tests/test_doctor_herdr.py` runs `athena doctor` against a real herdr server in a temporary session of its own (skipped without herdr or Go), and `tests/fake_service.py` stands in for `launchctl` and `systemctl` in the `athena service` tests. To try the services for real without touching your own install, install from a shell whose `ATHENA_STATE_DIR` and `ATHENA_HERDR` (`herdr --session <test>`, with its own `XDG_CONFIG_HOME`) point elsewhere, with `--instance <test>`, and uninstall it after.

## Try the watch mod

To try the mod without real workers, start a throwaway session with `ATHENA_ROLE=hq` and `ATHENA_WATCH_CMD=tests/lab/fake-watch` in its `--settings` env, and append watch lines to its `FAKE_WATCH_LINES` file.

## Update after editing

The `athena` CLI and the prompt files (`hq.md`, `worker.md`) run from this repository. The hooks, the watch mod and the `athena` skill run from the plugin: bump `version` in `.claude-plugin/plugin.json`, then `claude plugin marketplace update athena && claude plugin update athena@athena`, and restart the sessions that should pick it up (or `/reload-plugins` in them; the plugin is read from its folder when its marketplace is that folder).
