# Decisions

Taken on 2026-10-07 from the Chief of Staff Blueprint (https://claude.ai/artifact/TN2e6btFJV9uY4TGk1UZth). The user accepted every recommended option.

| Id | Question | Decision |
|---|---|---|
| D1 | Board layout | The chief of staff's pane stays pinned left; worker attach columns are added beside it in the same tab with `pane split`, never with `layout.apply`. |
| D2 | Who starts a worker's Claude | `cos spawn` starts it with `--name`, the worker brief and per-worker `--settings`. auto-claude skips any space whose cwd has a claim file. |
| D3 | Worktree location | `~/Developer/.worktrees/<repo>/<slug>` everywhere; herdr's `[worktrees] directory` matches. Sunlite keeps its own scheme. |
| D4 | Worker git authority | Commit, push its own branch, open a draft PR. Never merge, never push to main. No force-push, except `--force-with-lease` on its own branch after a rebase the chief of staff asked for. |
| D5 | Who merges | platform and infrastructure: the user. Other repos: the chief of staff after the user's explicit go, pinned with `--match-head-commit`. |
| D6 | Launch confirmation | Show a launch card and launch, unless the brief has gaps. |
| D7 | Default models | Workers Opus at high effort; reviewers a fresh Opus; Fable for hard design consults. |
| D8 | Concurrency | 4 live workers by default, 5 at most (`--force`); no new launch above 85% of the 5-hour quota (`--ignore-quota` overrides, only when the human says so). |
| D9 | keepwarm | Off in workers (per-worker settings), on in the chief of staff. |
| D10 | herdr 0.9.3 | At a quiet moment before relying on the board. herdr is a Homebrew install, so `herdr update --handoff` is unavailable; `brew upgrade herdr` then a server restart. The user picks the moment. |
| D11 | Main checkouts not on main | The chief of staff proposes a cleanup per repo; the user confirms each. `cos preflight` refuses to launch until a checkout is clean and on main. |

## Implementation decisions

- Worker hooks ship in the cos plugin and do nothing unless the session is a cos worker: `COS_WORKER` is set, or the cwd is under the worktree root and in the ledger.
- Status is merged from four sources, most trusted first: the worker's `COS-REPORT` line, hook state, git and PR state, herdr's screen-read status.
- One writer per state file: `ledger.jsonl` (cos commands), `workers/<name>.json` (that worker's hooks), `board.json` (board sync).
