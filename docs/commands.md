# Commands

The everyday ones are in the [README](../README.md#using-it-day-to-day). This is the full reference.

## Starting and attaching

From any terminal, `athena` (or `athena hq`) starts a herdr server if none runs, creates or focuses the `athena` space with the chief of staff's Claude in it, then attaches your terminal to herdr with that space in front (it execs into `herdr`). Detach with `ctrl+b q`; HQ and the workers keep running, and `athena` brings you back. Inside herdr, `athena hq` opens or focuses the space as before. The first `athena` with no config runs `athena init` first.

## Key bindings and everyday use

- `prefix+shift+c` (or `athena hq`): open or focus the hq space. The chief of staff starts with its brief (`hq.md`) and the `athena` skill.
- Tell it what to start: "start abc-101 and abc-102". It reads Linear, writes a brief, shows a launch card, and runs `athena spawn`.
- `prefix+shift+h` (or `athena hq restart`): update Claude Code and its plugins, then restart HQ's Claude in the same pane on the same conversation; it runs its start routine by itself. HQ can run it about itself. Workers and the dashboard keep running.
- `athena hq rotate`: HQ's answer to a long conversation, instead of compacting it again. It writes `handoff.md` in the state dir (fleet, PRs, open asks, in-flight threads, notes tail, standing-orders path), then starts a fresh HQ conversation in the same pane that reads it and runs its start routine. HQ runs it daily or when `athena status` shows its context at 60% or more.
- `prefix+shift+b` (or `athena board toggle`): show or hide the worker columns beside hq.
- `athena dash open`: the dashboard in your browser.

## Reference

| Command | What it does |
|---|---|
| `athena preflight <repo>` | Main checkout clean, on main, fast-forwarded |
| `athena spawn --repo --branch --name --brief --goal ...` | Worktree space, worker Claude (`--name`, `worker.md`, per-worker settings), then its `/goal` |
| `athena status [--json] [--pr]` | Merged status: ATHENA-REPORT, hook state, git, PR, herdr |
| `athena board on\|off\|toggle\|sync\|show` | Attach columns beside hq; a watcher keeps them in step with width and workers |
| `athena dash on\|off\|show\|open` | The dashboard on 127.0.0.1; `show` prints its URL and pid |
| `athena watch [--tagged]` | One line per change; also saves its snapshot to `watch.json` for the dashboard. The watch mod runs it `--tagged` (JSON lines: each change marked wake or not, and the status band's board); plain output is for the Monitor fallback |
| `athena stack [set <owner/repo> <pr>... \| clear <owner/repo>]` | The stacked-PR chains `athena watch` follows (`stack.json`) |
| `athena pr <name>` | PR summary |
| `athena review <name> start\|pass\|fail [--head <sha>]` | Record HQ's review of a live worker's PR in the ledger, at `--head`. Without it, `start` records the PR's current head, and `pass` or `fail` the head that `start` recorded (refused with no `start` before it), so a push during the review is never passed. Until a `pass` at the PR's current head, the PR is `in review · athena` (or `review findings · worker fixing` after a `fail`), never the human's to merge nor in Needs you as `no reviewer requested`; `athena status` shows it as `review=<verdict>@<sha>`, `(stale)` once the head moved |
| `athena nudge <name> <text>` | Type a slash command into a worker |
| `athena resume <name>` | Send the goal of a worker that stopped at a startup prompt, once the human has answered it (`athena watch` does this on its own) |
| `athena retire <name> [--force] [--keep-worktree]` | Back up, refuse to lose work, stop, remove the worktree space |
| `athena` | Outside herdr, from a terminal: start herdr if needed, open HQ, attach. Inside herdr: same as `athena hq` |
| `athena init [--yes ...]` | Check herdr, claude and the plugin, gh, git; ask for folders, merge rules, model; write `config.json` |
| `athena config [show\|path]` | The effective config and where each value comes from |
| `athena hq [--no-attach]` | Create or focus hq; from a terminal outside herdr, also start herdr and attach |
| `athena hq rotate [--threads <file\|->] [--dry-run] [--if-context-over <pct>]` | Writes `handoff.md` from athena's state (`athena status --pr` fields with no herdr call, open asks, the last 12 entries of `notes.md`, the standing-orders path) plus HQ's in-flight threads (`--threads`, saved to `threads.md` first so a skipped rotation keeps them; else `threads.md`; moved to `threads.prev.md` after), at most 150 lines of at most 300 characters. Then, detached and only once HQ is idle, `/exit`s HQ's Claude (its conversation stays resumable; `hq.json` keeps it as `previous_session`) and starts a new conversation in the same pane, the way `athena hq` does, prompted to read the handoff and run session start. A busy HQ is left alone. No updates. `--dry-run` prints the handoff, the herdr launch command and the first prompt, and runs nothing. `--if-context-over` rotates only when HQ's context, read from its transcript, is at least that full. Log in `logs/hq-restart.log` |
| `athena hq restart [--no-update]` | Detached: `claude update`, the marketplaces and the user-scope plugin updates (concurrently), `/exit` HQ's Claude (confirming the background-work dialog), `--resume` its conversation in the same pane, prompt its start routine. A failed resume starts a fresh HQ and says so; log in `logs/hq-restart.log` |

## `athena init` in scripts

`athena init --yes --projects ~/code [--worktrees DIR] [--human-merge a,b] [--reapproval a] [--model M] [--effort E] [--install-herdr]` asks nothing (a flag, else the current config, else the default) and installs herdr only with `--install-herdr`. Run it again any time to change a value. What each value means is in [Configuration](configuration.md).
