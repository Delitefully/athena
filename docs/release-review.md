# Release review: athena 1.0.0

The release that renames athena-rex to athena: the rename, a clean install, `athena update`, the upgrade from the herdr
athena, a user-facing README, and a full review of the code.
Each fix below has its test in `tests/` (most in `tests/test_review.py`), and they run through `scripts/test.sh`.

## How the review ran

Four read-only reviewers each took one area: the kernel and backends; the worker and HQ lifecycle; the services and
background parts; and the scripts, `bin/`, hooks and the menu bar. They reported 44 findings. Each one below was read
against the code before it was fixed or parked. One of them, the `hq.json` save order, was known before the review.

## Fixed

| # | Where | What was wrong | Test |
|---|---|---|---|
| 1 | `athena/kernel/agents.py:135`, `athena/caps/hq.py` `open_hq` / `_cycle` | `athena hq` typed in HQ's own pane after a Rex restore closed that pane before saving the new one. The command ended there, `hq.json` named a dead block, doctor said BAD and the watch could not wake HQ. The backend's `replace` (open, then close) is gone. `agents.restart` only opens the new window, and the old block closes last, once the new place is on record and HQ has its prompt. | `test_release.HqRecord` (fails on the old code) |
| 2 | `athena/caps/dash/markdown.py` | **XSS** (high), in the regex renderer this branch first had. Item bodies were escaped without quotes, so a `"` in a bare link ended its `href`; HQ's review also found a bare URL linked again inside a named link's `<a>`. Local main replaced that renderer with a port of the Go dash's (goldmark with safe links), which the rebase keeps. The tests hold it to all four cases, HQ's nested link included. | `test_review.DashMarkdown` |
| 3 | `athena/kernel/config.py:116` (`update_rex`), `caps/profiles.py` `grant`, `caps/install` `set_real_world`, `kernel/modules.py` `write_list` | `grant` and `set_real_world` read a config that didn't parse as `{}` and wrote that back over it. The good-copy logic then remembered the stripped file. There was also no lock. Now there is one locked writer for `rex` keys, and it refuses a file that doesn't parse. | `test_kernel`/`test_modules` (existing) + install tests |
| 4 | `athena/kernel/agents.py:194` | A `ps` that failed (fork refused under memory pressure) was read as "no Claude running", so retire and doctor acted as if processes were dead. A failed `ps` is now `Unknown`. | `test_review.PsThatFails` |
| 5 | `athena/backends/rex.py` `_parsed`, `has_block`; `athena/caps/retire.py` `_stop` | A Rex answer that wasn't JSON, or lacked its block, escaped as `ValueError` or `KeyError` past every `BackendError` handler: now `BackendError("bad_reply")`. Retire read a timeout from `has_block` as "gone" and skipped the close, leaving an orphan block. It now asks with main's `strict=True` (REX-CLOSE's), so only `not_found` means gone; non-strict callers keep the old answer. | `test_review.RexAnswers` |
| 6 | `athena/kernel/gitops.py:15`, `:53`, `:129`, `:328` | Several git failures were silent. A fetch could sit at a credential prompt, and git's stdin was inherited. `ok()` had no timeout and threw raw errors. A failed ahead-count read as "nothing unpushed", in the function that decides whether a worktree may be deleted. `prune` let a `TimeoutExpired` escape. Now: no terminal prompt, stdin from `/dev/null`, timeouts, `GitError` everywhere, and checked counts. | `test_review.GitThatFails` |
| 7 | `athena/kernel/sandbox.py:96`, `athena/caps/watch.py:350` | A signal sent through Rex from inside the signal sandbox raised `BackendError` when Rex was down. Retire, `dash off` and `watch --stop` caught only `OSError`, so they crashed halfway. Now it is an `OSError`, as the kernel's own refusal is. | `test_review.SignalThroughRex` |
| 8 | `athena/caps/spawn.py:187`, `:255` | Ctrl-C during a launch's setup (up to 30 min) skipped the cleanup. The worker stayed live on record, counted against the cap, with its worktree left behind. A brief that wouldn't decode crashed the launch. Now Ctrl-C cleans up and re-raises, and an unreadable brief is refused before anything is made. | `test_review.SpawnInterrupted` |
| 9 | `athena/caps/hq.py:67` (`_amend`), `--cwd` in `_run` | Doctor's HQ check wrote `hq.json` without HQ's lock, from a record read earlier. It could write the old block back over a launch's new one. It now amends only under the lock, and only while the record still names the same place. | `test_review.HqRecordRace` |
| 10 | `athena/caps/hq.py:533` | `athena hq` ran a `doctor --fix` pass without doctor's lock, beside the launchd one. Both repaired, and the repair budget was bypassed. It now takes the same lock. | `test_review.HqDoctorPass` |
| 11 | `athena/caps/items.py:624` | The watch and the dash both apply due defaults. The second `answer` raised "already answered", which aborted the watch's tick and woke HQ with an error. It now skips that item. | `test_review.DefaultsTwice` |
| 12 | `athena/caps/trust.py:63` | A stale lock that `rmdir` couldn't remove looped forever, with no sleep and no deadline, so spawn hung at 100% CPU. It now waits out the deadline. | `test_review.SmallHoles` |
| 13 | `athena/caps/slack.py` `_record_posts` | One malformed value in `open-prs-posted.json` raised `ValueError` every poll. That repeated up to 40 Slack calls and woke HQ with an error each time. It now reads both sides through main's `_ts` (None for a bad value), so a damaged entry is written over. | `test_review.SmallHoles` |
| 14 | `athena/caps/dash/server.py` `_body` | Main's server capped a body at 16 KB, but a negative `Content-Length` still read until EOF, and one that isn't a number raised outside the handler. Both now get a 400 and the connection closes. | `test_review.SmallHoles` |
| 15 | `athena/caps/services.py:112` | A launchctl that hung past its timeout raised `TimeoutExpired` through `services install`. That left jobs half-done and the menu bar's status read a traceback. It is now a failed call, said like any other. | menubar/install tests (existing) |
| 16 | `athena/kernel/agents.py:58` | A worker's launch script holds its environment, and it was world-readable for an instant before its `chmod`. It is now created 0700. | `test_review.SmallHoles` |
| 17 | `scripts/test.sh:25` | Ctrl-C or TERM ran the cleanup and then carried on, so a run cut short could read as passed. INT and TERM now exit 130 and 143. | by reading |
| 18 | `athena/caps/install` (was `install.py`) | `_installed_command` let `TimeoutExpired` escape after `current` had already been swapped. `_prune` could remove another install's half-made copy. Commands now answer as a failed run, and install, update and rollback share one lock. | `test_update`, `test_release` |
| 20 | `scripts/upgrade-from-herdr.py` `cmd_old_hq`, `athena/caps/hq.py` `_second_fix` | The herdr server restored a pane and resumed the old herdr HQ on the same state as the new HQ. Exiting the old HQ is not enough: herdr resumes the Claude in every pane it restores. The upgrade now closes the old HQ's herdr workspace (from `hq.json`) once the old jobs are stopped. Before the state moves, the old HQ must be down. The workspace is closed only while herdr still labels it `athena`, as the herdr athena checked, since herdr reuses ids. A workspace already gone counts as closed, by herdr's error code. The step refuses inside a herdr pane, and says what to close by hand otherwise. Doctor's `hq-second` named the wrong command (`athena hq` starts these); it now says to /exit the second HQ and close its pane (in herdr, the workspace labelled athena), naming no id. | `test_upgrade.WholeRun` (old-hq), `test_review.SecondHq` |
| 19 | `athena/caps/items.py` `NOT_HUMAN` (by the rename) | HQ's own prompts began `athena-rex doctor:`, which the "not the human's request" filter (`athena doctor`) missed, so they could be logged as the human's requests. With the rename they match. | `test_caps` (existing) |

## Parked, with why

Lines are as of the final commit.

| Where | Finding | Why parked |
|---|---|---|
| `athena/caps/watch.py:448` (`_start_detached`) | Doctor's repair starts a watch outside launchd. If it wins the lock, the launchd copy exits 1, and `KeepAlive` relaunches it every 10 s. | The fix (repair by `launchctl kickstart`, or exit 0 on a held lock) changes how a stuck watch comes back. It needs a real-launchd check that this lab can't run. |
| `athena/caps/services.py:221` (`stop`) | Quit athena leaves a dash started outside launchd (by `athena hq` or a repair) running. | It is the same supervision question as the line above. |
| `athena/kernel/agents.py:294` (`send`) | `send` and `amend` aren't locked against a goal being typed, so text can interleave. | It needs a per-worker send lock shared with `_send_goal`'s, which touches every typing path. Rare: the human or HQ would have to type in the same seconds. |
| `athena/caps/hq.py:492` (`rotate`) | Two `hq rotate --now` at once can rotate twice. | Rotation runs once in two hours at most, behind a quiet wait. A generation check inside the lock is the fix. |
| `athena/caps/hq.py` `_run` | `athena hq` typed in HQ's old block now records the new place and sends the routine, but then ends before `_around_hq` (its doctor pass, dash and board). | Doctor's job restarts the dash and runs its pass within a minute. Running `_around_hq` before the close is the fix. |
| `athena/caps/sessions.py:136` (`close-orphans`) | `close-orphans` can close a repo session in the moment between a spawn's window and its ledger update. | It is a narrow window, and a person runs that command by hand. |
| `athena/caps/workspaces.py:242` (`clean`) | `clean --apply` can remove a clean, pushed, unowned worktree of the human's own under `.worktrees` after 7 days, judged by the directory's mtime. | The head is backed up under `refs/athena-backup/`, and dirty or unpushed worktrees are kept. An opt-in is a behaviour change for the human to decide. |
| `athena/caps/profiles.py:36` (`permissions`) | A grant removed from config keeps applying until the next `install`. | That is how the profile file works today (the install diffs and writes it). |
| `athena/caps/limits.py:193` | A worker that prints Claude's limit line can park the fleet. | It needs Claude's program status to agree. A false park lifts itself at the stated time. |
| `athena/caps/items.py:316` | An `add --preview` that fails after staging can leave its preview folder behind. | It is rare (a full disk). |
| `athena/caps/dash/__init__.py:75` (`off`) | `dash off` trusts the pid that `/healthz` reports. | Only a local process on the dash port, answering with this state dir, could misuse it. |
| `athena/caps/watch.py:298` (`read_inbox`) | The inbox writer doesn't take the inbox lock. | A line could be lost only in a microsecond window. |
| `athena/kernel/bus.py:16` (`emit`) | Rotation of `events.jsonl` can race. | The bus only tells and holds no state. |
| `athena/kernel/agents.py:383` (`kill_if_alive`) | `kill_if_alive` re-reads the pid file before the kill. | The pid is verified as that name's Claude on every loop, and the window is one statement. |
| `athena/kernel/config.py:151` (`rex`) | A non-number `ATHENA_MAX_WORKERS`, or a non-dict `rex`, breaks every command. | Hand-made config; doctor's config check names it. |
| `athena/kernel/sandbox.py:100` (`_through_rex`) | `_through_rex` says "sent" even if the process outlived the wait. | Callers verify by `ps` afterwards. |
| `athena/kernel/gitops.py:36` (`git`) | ssh can still prompt for a host key. | Overriding `GIT_SSH_COMMAND` would break a user's own `core.sshCommand`. The credential prompt and stdin are closed. |
| `athena/caps/heavy.py:25` | A SIGTERM to `athena heavy` doesn't reach its child. | — |
| `athena/caps/hooks.py:111` | The gate's worst case of three git calls can pass the hook's 10 s limit. | A killed hook fails open. |
| `athena/caps/install/__init__.py` `_install`, `rollback` | `current` is pointed a moment before the record is saved. | A kill in that moment leaves them disagreeing until the next install, which heals it. |
| `scripts/stop-dashes.sh:13` | Matched only by command line, a reused pid from a stale scratch `run/dash.json` could be the live dash. | It runs only on folders a test or smoke run made under `/private/tmp`, and a reused pid that is also a dash is unlikely. The fix needs `ps -E` in the stub too. |
| `scripts/test.sh:11`, `:14` | `mktemp` runs before the trap. A path with `"` would break the sandbox profile. | Developer-only paths. |
| `menubar/Sources/Cli.swift:61` | A CLI that ignores SIGTERM keeps one background thread until the 200 s limit. | Nothing blocks the main thread. |
| `bin/athena:6` | The shell guard skips a state dir that doesn't exist yet. The Python guard behind it still refuses. | The second wall holds. |
| `scripts/scratch-state.py`, `scripts/measure.sh`, shared temp paths | Developer scripts: a silent empty copy, a temp folder left behind, predictable names under a per-user folder. | Developer-only. |

## What a module reads

Kept stable: `<state>/run/<name>.sh` (same path and content, now created 0700), `workers/<name>.json`, `hq.json`
(`session`, `space`, `window`, `block`), the ledger's place fields, and the module API (`kernel/caps.py`,
`kernel/modules.py`). Private modules that import `athena_rex.*` get the same module objects as `athena.*`. Two changes
touch backends only:
- `Backend.replace` is gone. Use `new_window(space, cwd, argv, window_label, block_label, focus)`, then `close_block` once
  the new place is on record (`agents.close_replaced`).
- `Backend.has_block` keeps main's `strict=` parameter and answers; retire now asks strictly.

## The old name, where it stays

`git grep -n "athena-rex"` on this branch lists only these, each on purpose:
- `bin/athena-rex`, `athena_rex/__init__.py`, `ARCHITECTURE.md:9`: the alias and the import alias.
- `athena/caps/install/__init__.py`, `releases.py`: an athena-rex install's launcher becomes the alias, and its record's
  lab guard and checkout carry over.
- `athena/caps/services.py`: an athena-rex install's launchd jobs are retired (moved aside) once athena's are in.
- `athena/kernel/modules.py`, `scripts/test.sh`: the private modules folder's old default, read when the new one is absent; `scripts/test.sh` also reads its old `ATHENA_REX_MODULES_DIR` when `ATHENA_MODULES_DIR` is unset.
- `CHANGELOG.md`, this file: what was renamed.
- `tests/test_release.py`: the tests of all of the above.

## Calls made in this release

Each line is the call, why, and how to flip it.

- **Launchd labels are `local.athena.*`, not `athena.*`.** `athena.*` are the herdr athena's, and `services` takes every
  job under its prefix. The menu bar's bundle id was already `local.athena.menubar`. To flip: `PREFIX` in
  `caps/services.py`.
- **HQ's plugin is `athena-hq` (the skill stays `athena`).** That keeps it apart from the herdr athena's
  `athena@athena`, which HQ's settings turn off. To flip: `plugin/.claude-plugin/plugin.json`.
- **config.json's `rex` key stays.** Renaming it would migrate the live state. To flip: a migration in a later release.
- **The state dir path, `hq-rex.json`, `service-rex.json` and the other state file names stay.** They are state, not
  the product's name.
- **The lab guard (`ATHENA_WORKER=rex-*`) stays.** It means "a worker of athena's own lab", not the product.
- **`update` defaults to the `release` channel:** the newest `vX.Y.Z` tag, never older than what runs. The public repo
  has no tags yet, so `update` says so and points at `--channel main`. To flip: the default in `releases.load`.
- **Health check: no check that passed on the old release may fail on the new one.** A check the new release no longer
  has is not a failure, and a doctor that can't answer is. "Update available" is a passing doctor row and never fails
  doctor. To flip: `update._verdict`.
- **The update source is a clone under `~/.local/share/athena/source`, and a release is exported with `git archive`.**
  This keeps no `.git` and no untracked files. To flip: `update._source` / `_export`.
- **The default repo is the public one, `https://github.com/Delitefully/athena.git`.** The README already names it, and
  `--repo` overrides it (kept). To flip: `releases.REPO`.
- **The upgrade refuses while workers still run on the herdr athena.** Both commands are called `athena`, so the new one
  would answer the old workers' `athena report`. To flip: `cmd_check`'s row in `scripts/upgrade-from-herdr.py`.
- **The upgrade leaves `athena.herdr` running.** It is the herdr server's job, which may serve more than athena.
- **No `athena upgrade-from-herdr` verb.** The script is the one way, so nothing in the installed
  release talks about herdr.
