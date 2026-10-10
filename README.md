<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-lockup-dark.svg">
  <img src="assets/logo-lockup.svg" alt="athena" width="360">
</picture>

A chief of staff for Claude Code in [herdr](https://herdr.dev). One Claude session named athena, in a herdr space of the same name, takes Linear tickets, launches a visible worker Claude for each in its own git worktree and herdr space, shows the workers as columns beside itself, and supervises them to merge-ready PRs. The human can type into any worker at any time.

## How it works

![athena's red thread weaves through three workers, takes one worker's question to you, and ties off when the work merges](assets/one-thread.svg)

athena sits in its own space with the workers as columns beside it. It launches each ticket with a launch card, and when a worker gets stuck or finishes, a watch line appears and athena acts on it. When a worker needs a decision, athena asks you in its own pane and passes your answer on. Each worker is a full Claude session, so you can also type into its column directly.

![A ticket's path: brief, a worker on its own worktree branch builds, tests and pushes, draft PR, review sends a finding back, ready](assets/ticket-path.svg)

A ticket becomes a brief and a worker in its own worktree. The worker builds, tests, pushes and opens a draft PR. athena checks CI, has a fresh reviewer read the diff, and sends findings back until the PR is ready. Then it asks you: you merge, or tell athena to.

![athena watch: routine progress streams by quietly; athena acts on a blocked worker, a failed check, a finished worker and a moved main](assets/watch.svg)

`athena watch` prints one line per change, and athena reacts to the ones that matter: it asks you about a blocked worker, sends a failed check back to its worker, starts a review on DONE, and asks a repo's workers to rebase when its main moves. Routine progress stays quiet.

### The watch mod

In the HQ session, `athena watch` is run by a Claude Code mod that ships in this plugin (`hooks/watch-mod/`), not by Monitor tool calls, so routine changes never reach HQ's chat. The mod is active only where `ATHENA_ROLE=hq` (which `athena hq` sets); workers and every other session load it and see no change.

- It runs `athena watch --tagged` for the whole session and restarts it if it exits (5 s, backing off to a minute). There is no 30-minute expiry to re-arm.
- Each line is marked actionable or routine by `watch.classify`. Actionable lines wake HQ with one short prompt, a stamped header and one event per line, such as `athena watch 22:19:` then `plt-4041 report DONE`: a report (DONE, DONE_WITH_CONCERNS, BLOCKED, NEEDS_CONTEXT), a worker turning blocked or exiting, a PR opened, merged or closed, failing checks, CHANGES_REQUESTED or APPROVED, new review comments, `main moved`, a stacked PR merged or newly CONFLICTING, an unexpected `gone from the ledger`, a worker that failed to start, and watch errors. Lines that arrive within a few seconds, or while HQ is running a turn, go out together once HQ is idle. The same line is told once in ten minutes unless the worker (or stacked PR) changed in between: a routine line about it lets its events be told again, so a check that fails, goes pending after a fix and fails again wakes HQ twice, while a repeat with no change between wakes it once. What was told is remembered across a mod reload and an HQ restart.
- Because HQ reads a wake as the person's own words, every line is rebuilt from an allowlist before it goes out: worker names as the ledger spells them (`^[a-z0-9][a-z0-9_-]{0,31}$`), fixed state, report, check and review words, PR numbers as `#n`, counts as integers. Free text (a `goal not sent` reason, an exception, a branch name, a PR URL's owner and repository) is cut to its allowlisted part, and a line it cannot rebuild goes out as `a watch line not shown here (athena status)`. Each regex is anchored.
- The band above the prompt draws the last wake as athena's own row, `∞ athena 22:19` then one event per line, until the human next types a prompt.
- The prompt is submitted `asUser`, so the transcript row shows just the prompt, under Claude Code's `› Prompt from the athena plugin` label, without the frame and the "This is how Claude Code surfaces a prompt a plugin submits…" sentence that a framed plugin prompt carries. Claude Code 2.1.296 raises no `ui.render` for a plugin's prompt row, so the mod cannot redraw it yet; its `UserMessage` hook draws a compact `∞ athena 22:19 <event>` row once a build does.
- Routine lines (working and idle flips, pending or green checks, `done->idle`, the `watching:` line) only update athena's status band above HQ's prompt, such as `∞ athena plt-9 blocked · #5856 #6547 #6552 need you · pr6519 working · ✓ plt-4041, plt-4042 done`: athena's mark in madder, then what needs the human in the theme's warning colour (blocked, NEEDS_CONTEXT and exited workers, then the PRs awaiting a review or merge), the active workers by state, and the done ones in the success colour. Narrower, the done ones fold to a count first (`✓ 2 done`), then the active ones, the PRs and the needs. The PRs are a live worker's open, ready PR once it stopped or is approved, a stacked PR next to land (open on main, not a draft) or conflicting, and every PR that `needs-you.md` links, each once. The model never reads the band.
- In a worker (`ATHENA_WORKER` set), the `ATHENA-REPORT` line its final message ends with draws as a completion block: athena's mark and the status in its colour (Done green, Done with concerns yellow, Blocked red, Needs context orange), the PR as a `#n` link, the head, the verify line, the decisions and the concerns (yellow). Drawing only: the stored message, the status hook's parsing and what athena reads are unchanged, and a line that does not parse is drawn as is.
- Monitor rows in HQ's transcript (from before the mod, or from the fallback) are drawn as nothing, in HQ only: the call, its result, and its `Monitor event:`, stream-ended and expiry notifications. The model still reads them; ctrl+o still shows the notifications.
- Stacked PRs: `athena stack set <owner/repo> <pr> ...` lists a chain in `stack.json`; `athena watch` follows each PR's state, base and mergeability every two minutes.

**Fallback.** If the mod is not loaded (Claude Code older than 2.1.286, mods turned off, or the plugin not reloaded since an update; `pgrep -fl 'athena watch --tagged'` shows nothing), HQ arms Monitor with `athena watch` itself and re-arms it every 30 minutes, as the `athena` skill says.

To try the mod without real workers, start a throwaway session with `ATHENA_ROLE=hq` and `ATHENA_WATCH_CMD=tests/lab/fake-watch` in its `--settings` env, and append watch lines to its `FAKE_WATCH_LINES` file.

## Use

- `prefix+shift+c` (or `athena hq`): open or focus the hq space. The chief of staff starts with its brief (`hq.md`) and the `athena` skill.
- Tell it what to start: "start abc-101 and abc-102". It reads Linear, writes a brief, shows a launch card, and runs `athena spawn`.
- `prefix+shift+h` (or `athena hq restart`): update Claude Code and its plugins, then restart HQ's Claude in the same pane on the same conversation; it runs its start routine by itself. HQ can run it about itself. Workers and the dashboard keep running.
- `prefix+shift+b` (or `athena board toggle`): show or hide the worker columns beside hq.
- `athena dash open`: the dashboard in your browser.

## Dashboard

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/dashboard-dark.png">
  <img src="assets/dashboard.png" alt="athena dash: needs you, in progress and done, with ticket and PR links" width="960">
</picture>

While HQ is open, `athena dash` serves one page on `http://127.0.0.1:2843/`: what needs you (a blocked worker, a question, a PR ready for you, and athena's own asks from `needs-you.md`), what is in progress with its PR checks, and what was done in the last two weeks, each linked to its ticket and PR. It reads athena's state files only, makes no network calls, and updates by itself within a second of a change. `athena hq` starts it, and closing HQ's space stops it. It is a small Go program in `dash/`, built on first use (Go required); the plan and its failure modes are in [docs/dashboard.md](docs/dashboard.md).

## Commands

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
| `athena nudge <name> <text>` | Type a slash command into a worker |
| `athena resume <name>` | Send the goal of a worker that stopped at a startup prompt, once the human has answered it (`athena watch` does this on its own) |
| `athena retire <name> [--force] [--keep-worktree]` | Back up, refuse to lose work, stop, remove the worktree space |
| `athena hq` | Create or focus hq |
| `athena hq restart [--no-update]` | Detached: `claude update`, the marketplaces and the user-scope plugin updates (concurrently), `/exit` HQ's Claude (confirming the background-work dialog), `--resume` its conversation in the same pane, prompt its start routine. A failed resume starts a fresh HQ and says so; log in `logs/hq-restart.log` |

## Pieces

- `athena_lib/`: stdlib Python 3.9+. State in `~/.local/state/athena/` (`ledger.jsonl`, `workers/`, `briefs/`, `settings/`, `claims/`, `board.json`, `hq.json`, `stack.json`, and for the dashboard `watch.json`, `history.jsonl`, `needs-you.md`).
- `dash/`: the dashboard server, Go standard library only, with the Instrument fonts (OFL) embedded.
- `hooks/`: Claude Code plugin hooks that write `workers/<name>.json` (a no-op outside athena workers), and `hooks/watch-mod/`, the watch mod (a no-op outside HQ).
- `hq.md`, `worker.md`, `skills/athena/SKILL.md`: the prompts.
- `repos/<repo>.setup`: optional per-repo worktree setup (platform: env files, certs, `pnpm install`).
- The herdr auto-claude plugin skips spaces that `athena` claims (`~/.local/state/athena/claims/`).

## Install

```sh
ln -sf ~/Developer/athena/bin/athena ~/.local/bin/athena
claude plugin marketplace add ~/Developer/athena
claude plugin install athena@athena
```

## Test

`make test` (unit tests with a fake herdr, `go test` for the dashboard, and `claude plugin validate` and `claude plugin test` for the watch mod when `claude` is installed). `tests/lab/` runs real workers against an isolated herdr server.

## Environment

`ATHENA_STATE_DIR`, `ATHENA_WORKTREE_ROOT` (default `~/Developer/.worktrees`), `ATHENA_HERDR`, `ATHENA_HQ` (default `athena`), `ATHENA_MIN_COL` (70), `ATHENA_MAX_WORKERS` (4, hard cap 6), `ATHENA_PLUGIN_DIR` (load the plugin from a directory in workers), `ATHENA_HQ_PANE`, `ATHENA_DASH_PORT` (2843), `ATHENA_LINEAR_WORKSPACE` (the Linear workspace in ticket links; or `linear_workspace` in `~/.local/state/athena/dash.json`; neither leaves ticket ids unlinked).

## Update after editing

The `athena` CLI and the prompt files (`hq.md`, `worker.md`) run from this repository. The hooks, the watch mod and the `athena` skill run from the plugin: bump `version` in `.claude-plugin/plugin.json`, then `claude plugin marketplace update athena && claude plugin update athena@athena`, and restart the sessions that should pick it up (or `/reload-plugins` in them; the plugin is read from its folder when its marketplace is that folder).
