# cos

A chief of staff for Claude Code in [herdr](https://herdr.dev). One Claude session in a herdr space named `hq` takes Linear tickets, launches a visible worker Claude for each in its own git worktree and herdr space, shows the workers as columns beside itself, and supervises them to merge-ready PRs. The human can type into any worker at any time.

Design and decisions: `docs/decisions.md`. Plan: `docs/plans/2026-10-07-cos-v1.md`.

## Use

- `prefix+shift+c` (or `cos hq`): open or focus the hq space. The chief of staff starts with its brief (`hq.md`) and the `cos` skill.
- Tell it what to start: "start PLT-4512 and PLT-4520". It reads Linear, writes a brief, shows a launch card, and runs `cos spawn`.
- `prefix+shift+b` (or `cos board toggle`): show or hide the worker columns beside hq.

## Commands

| Command | What it does |
|---|---|
| `cos preflight <repo>` | Main checkout clean, on main, fast-forwarded |
| `cos spawn --repo --branch --name --brief --goal ...` | Worktree space, worker Claude (`--name`, `worker.md`, per-worker settings), then its `/goal` |
| `cos status [--json] [--pr]` | Merged status: COS-REPORT, hook state, git, PR, herdr |
| `cos board on\|off\|toggle\|sync\|show` | Attach columns beside hq; a watcher keeps them in step with width and workers |
| `cos watch` | One line per change, for Monitor |
| `cos pr <name>` | PR summary |
| `cos nudge <name> <text>` | Type a slash command into a worker |
| `cos retire <name> [--force] [--keep-worktree]` | Back up, refuse to lose work, stop, remove the worktree space |
| `cos hq` | Create or focus hq |

## Pieces

- `cos_lib/`: stdlib Python 3.9+. State in `~/.local/state/cos/` (`ledger.jsonl`, `workers/`, `briefs/`, `settings/`, `claims/`, `board.json`, `hq.json`).
- `hooks/`: Claude Code plugin hooks that write `workers/<name>.json`; a no-op outside cos workers.
- `hq.md`, `worker.md`, `skills/cos/SKILL.md`: the prompts.
- `repos/<repo>.setup`: optional per-repo worktree setup (platform: env files, certs, `pnpm install`).
- The herdr auto-claude plugin skips spaces that `cos` claims (`~/.local/state/cos/claims/`).

## Install

```sh
ln -sf ~/Developer/cos/bin/cos ~/.local/bin/cos
claude plugin marketplace add ~/Developer/cos
claude plugin install cos@cos
```

## Test

`make test` (unit tests with a fake herdr). `tests/lab/` runs real workers against an isolated herdr server.

## Environment

`COS_STATE_DIR`, `COS_WORKTREE_ROOT` (default `~/Developer/.worktrees`), `COS_HERDR`, `COS_HQ` (default `cos`), `COS_MIN_COL` (70), `COS_MAX_WORKERS` (4, hard cap 5), `COS_PLUGIN_DIR` (load the plugin from a directory in workers), `COS_HQ_PANE`.
