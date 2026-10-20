# Troubleshooting

- **`athena: no config yet: run athena init first`**: `athena hq` ran without a terminal (a key binding, a script) before any config existed. Run `athena init` once in a terminal.
- **`✗ herdr: not found` after installing it**: the installer put it in a folder your shell does not search yet. Open a new terminal, or add that folder (often `~/.local/bin`) to `PATH`.
- **`herdr server did not start`**: read `logs/herdr-server.log` in the state dir; `herdr status server` shows which socket athena expects. A server from an older herdr: `herdr server stop`, then `athena`.
- **`athena` prints JSON instead of attaching**: it attaches only from a terminal outside herdr (no `HERDR_ENV`, stdin and stdout a tty). Inside herdr, `athena hq` focuses the space instead.
- **Workers in the wrong folder, or the hooks not seeing a worker**: `athena config show` lists the worktree root they use; an `ATHENA_WORKTREE_ROOT` in your shell wins over the file. Keep herdr's `[worktrees] directory` the same.
- **After a herdr restart, a worker's report draws as plain text, or a board column shows a dead terminal**: a worker spawned before athena registered resume commands came back as a bare `claude --resume`. The mod and the status hook still find it by its worktree; its next SessionStart (any `/clear`, compaction or resume) registers its full command. The board watcher moves the columns to the live terminals on its own (`athena board sync --force` does it at once). See [How it works](how-it-works.md#after-a-herdr-restart).
- **`config.json is not valid JSON`**: fix the file, or move it away and rerun `athena init`.
- **The watch mod is not running in HQ** (`pgrep -fl 'athena watch --tagged'` shows nothing): Claude Code is older than 2.1.286, mods are turned off, or the plugin was not reloaded since an update. HQ falls back to Monitor; see [How it works](how-it-works.md#fallback).
- **`go` is missing**: the dashboard needs Go; `athena hq` carries on without it. See [Dashboard](dashboard.md#failure-modes) for its other failure modes.
