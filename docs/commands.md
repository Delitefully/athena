# Commands

`athena --help` lists every command, and each has `--help`. This page covers the ones whose behaviour is more than their help line.

## `athena report`

A worker files its report with this, in its own session, instead of typing it into the chat:

```
athena report --status DONE|DONE_WITH_CONCERNS|IN_PROGRESS|BLOCKED|NEEDS_CONTEXT --verify "<command> -> pass|fail" \
  [--decision "<one short line>"]... [--concern "<one short line>"]... [--summary "<a sentence or two>"]
```

- **Who:** the worker named by `ATHENA_WORKER`, which spawn sets in the worker's environment. Anywhere else it refuses.
- **What it fills in:** `head` is the worktree's `git rev-parse HEAD` (the worktree on record in the ledger, else the current folder), and `pr` is the open PR for the worktree's branch, asked of `gh` only when `origin` is on GitHub. No open PR, or a repo off GitHub, leaves it empty. If `gh` cannot say, the PR is left empty and the command prints why.
- **What it writes:** the report, with the same fields a typed line had (`status`, `pr`, `head`, `verify`, `decisions`, `concerns`), goes into the worker's status file `workers/<name>.json` as `filed`, under the hook's own lock. `--summary` stands in for the worker's last words. Without it, the final chat message is the summary.
- **When it counts:** at the turn's Stop. The report gate takes in the report filed this turn and moves it to `report`, the field status, watch, review and the dash read. A turn with none filed is sent back, as before. A DONE or DONE_WITH_CONCERNS still needs `--verify "... -> pass"` at the worktree's HEAD, and a PR when the repo is on GitHub. Every Stop uses up what was filed, accepted or sent back, and so does a new prompt. A turn that is sent back, or that goes on after another Stop hook, needs a new filing.
- **Refusals:** a DONE the gate would refuse is not filed. The command prints the gate's reason and exits 1, so the worker can fix it and file again.
- **Output:** one line, `report filed: <STATUS> @ <head>, pr <url|none>, verify <...>`, which is what a goal evaluator reading the transcript sees.

**Typed lines still work.** A worker started before this command can still end its message with `ATHENA-REPORT {...}`. When nothing was filed this turn, the gate parses that line as it always has and writes it to the same `report` field. When both are there, the filed report wins. A worker whose settings, written at its spawn, have no permission rule for `athena report` would stop at a prompt running it, so when the gate sends its turn back it also shows that worker the typed line.

## `athena brief check <brief> [--goal "<condition>"]`

Flags wording a goal evaluator could never call met. `--goal` also flags a goal that needs a SendMessage to succeed, and one that asks for a typed `ATHENA-REPORT` line, since a worker files its report with `athena report`. Exit 1 on any problem. `athena spawn` runs it, and `--skip-brief-check` overrides it.

## `athena flame index [--full] [--projects DIR] [--quiet]`, `athena flame status [--json]`

Reads Claude Code's transcripts into the index behind the dash's Flame page (`<state dir>/flame/index.sqlite`). Only the
bytes added since the last run are read. A line of a shape it doesn't know is skipped and counted, and a file that fails is
tried again next run. `--full` reads everything again in one transaction, so the page shows the old index until the new one
is whole. One index runs at a time: a second one prints `an index is already running` and exits 0. You rarely need to run
it: opening the Flame page updates an index more than 5 minutes old in the background. `status` prints the index's size, what
it covers and its last run. [flame.md](flame.md) has the design.
