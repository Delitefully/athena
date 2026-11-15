# Commands

`athena --help` lists every command, and each has `--help`. This page covers the ones whose behaviour is more than their help line.

## `athena spawn --agent pi`

A worker runs Claude Code unless HQ asks for Pi at spawn time:

```
athena spawn --agent pi --repo <path> --branch <branch> --name <name> --brief <file> --goal "<condition>" [--model <provider/id[:thinking]>] [--effort <level>]
```

- **Default.** `--agent` is `claude` everywhere. No config makes Pi the default, and HQ is always Claude.
- **Model.** With no `--model`, Pi runs its own configured default (its `settings.json`, never athena's `model`). A
  `--model` is passed to Pi's `--model` as written. `--effort` becomes Pi's `--thinking`; config `effort` is not used.
- **Refusals.** `--ultracode` is refused: ultracode and workflows are Claude Code's. A `pi` that isn't on PATH (config
  `rex.pi`, `ATHENA_PI`) is refused with that said. A Claude usage limit doesn't hold a Pi launch.
- **Launch.** Pi starts in the worker's own window and worktree as `pi --name <name> --session-id <id> --approve -e
  pi-extension/athena.ts --append-system-prompt prompts/worker-pi.md`. The conversation id is chosen at spawn and kept
  in the ledger as `pi_session`, and the launcher's environment carries what a Claude worker's `--settings` file
  carries: `ATHENA_STATE_DIR`, `ATHENA_BRIEF`, `ATHENA_HQ`, the gate, and this athena's `bin` first on PATH.
  `--approve` trusts the worktree's own Pi files for that run, as spawn marks a Claude worker's folder trusted.
- **Goal.** Pi has no `/goal`. Its goal goes to its inbox in words (`Your goal: <condition> Your brief is ...`), with
  the same clauses on the report and scope. Instead of SendMessage, it is told its report is how HQ hears from it.
- **The report gate.** `pi-extension/athena.ts` sends `athena hook` the events Claude Code's hooks send:
  `session_start` is SessionStart, `before_agent_start` is UserPromptSubmit, `agent_before_settle` is Stop,
  `session_shutdown` is SessionEnd, and a bash `tool_call` is PreToolUse. The gate's `block` at a Stop becomes one more
  turn carrying the gate's reason, so a Pi turn can't end without a report filed during it, as a Claude's can't. A run
  that ends with no Stop (aborted with Escape) sends an idle Notification. `rules.py` blocks the same git commands.
- **State.** Pi reports its own working, idle, done and blocked state to Rex (OSC 7501), which `agents.state` reads as
  it reads Claude's; a failed run (`error`) reads as idle. Pi sets its process title to `pi`, which hides its
  `--name`, so the extension writes `run/<name>.pi.json` with its pid, and a Pi counts as its worker's only under that
  pid. `athena status`, the board and the dashboard mark the row `[pi]`, and the watch tells its changes as it does a
  Claude's.
- **Messages.** `athena send <name> "<text>"` (and `nudge`, `amend`) write to `run/inbox/<name>/`. The extension reads
  it once a second and gives each message to Pi as the human's own words, as a follow-up while it works. Nothing is
  typed into Pi's editor. SendMessage doesn't reach Pi.
- **Elsewhere.** Retire asks Pi to quit through the inbox, then closes its window and kills it, as for Claude. Revive
  runs its launcher again, which brings back the same conversation (`--session-id`, no `--resume`). Doctor checks that
  its extension said hello. `athena progress` skips it, as there is no band without Claude's task tools. Flame has none
  of its time or tokens, as it reads Claude's transcripts. The usage-limit reader never reads its screen.

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
