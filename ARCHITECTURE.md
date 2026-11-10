# athena-rex architecture

A clean-room athena: same job, written fresh. The old code told us what must work, not how to build it.

## Module map

```
bin/athena-rex            the command; the lab guard runs here first, in shell
athena_rex/cli.py         finds the capability that owns a verb and imports only that one
athena_rex/kernel/        names no capability
  store.py                state dir, atomic JSON, appended lines, locks
  ledger.py               ledger.jsonl and the worker lifecycle state machine
  bus.py                  events.jsonl: emit from anywhere, read by cursor
  config.py               config.json, ours under the added `rex` key
  backend.py              the session backend interface
  agents.py               a Claude in a place: start, state, safe typing, exit, kill
  gitops.py               what guards a launch and a removal
  budget.py               memory, disk and load, checked before any launch
  caps.py                 the contract, discovery and loading
  modules.py              optional modules: the config list, public and private folders, on/off
  world.py                the one door to anything outside; shut unless the state dir's config opens it
  canary.py               the test run's tripwire, loaded only under test
  lab.py                  the lab's guard on the state dir; out of an installed copy with `install --lab-guard-out`, never out of a checkout
athena_rex/backends/      rex.py (the one that matters), fake.py (tests)
athena_rex/caps/          the capabilities, one file or directory each
athena_rex/modules/       public optional modules, off unless config lists them (clamshell); docs/modules.md
prompts/                  hq.md, worker.md
scripts/                  smoke.sh (VERIFY), test.sh (the only way to run tests), scratch-state.py, chaos-rex.py (the real-process chaos cases), measure.sh (the hot paths, the same way each time), install-transcript.sh
tests/                    contracts and things that broke, in an isolated HOME, inside a sandbox
```

## The contract

A capability is a module or package in `athena_rex/caps/`. It has `SUMMARY` and `commands()`, which returns `{verb: Command}`. It may also define any of these, each found by name:

| Function | What it gives | Who calls it |
|---|---|---|
| `checks(ctx)` | `[Check(name, ok, detail, repair, grace)]` | doctor, which owns grace, backoff and the repair budget |
| `services(ctx)` | `[Service(name, argv, every, keep_alive, interactive)]` | services, which only knows launchd |
| `tick(ctx, now)` | watch lines | the watch loop; a throw becomes a line, not a dead loop |
| `on_event(ctx, event)` | watch lines | the watch loop, per new bus event |

`config.json`'s `rex.caps` lists which capabilities load; with no list, everything found loads. Optional modules are separate:
`rex.modules` lists them (default none), from `athena_rex/modules/` or the private `rex.modules_dir`, and once loaded they are
capabilities like these, plus a menu entry (docs/modules.md). A capability reaches another only through `ctx.cap(name)`, which is `None` when it is absent. So leaving a module out of the list, or deleting its file, changes nothing else. A test checks that the kernel imports no capability.

The backend interface (`kernel/backend.py`) is 14 methods: start a program in a new space, replace a dead one, read a screen, type, press, close. A backend knows nothing about Claude; `agents.py` does.

## Old module to new

| Old | New | Notes |
|---|---|---|
| `herdr.py` | `backends/rex.py` | behind the interface |
| `paths.py` | `kernel/store.py` | |
| `ledger.py` | `kernel/ledger.py` | same file, compatible; adds `phase` |
| `config.py` | `kernel/config.py`, `caps/config.py` | |
| `gitops.py` | `kernel/gitops.py` | reads take no optional locks; WIP backups include untracked files |
| `composer.py` | `kernel/agents.py` | |
| `spawn.py` | `caps/spawn.py`, `caps/trust.py` | |
| `retire.py` | `caps/retire.py` | |
| `status.py` | `caps/status.py` | files and one `ps` |
| `sockets.py` | none | Rex runs the sessions (decision 10) |
| `hookstatus.py`, `hooks/worker-status`, `hooks.json` | `caps/hooks.py` | per-session `--settings`; the fast path and `nested()` are gone |
| `asks.py`, `requestlog.py` | `caps/items.py` | same files; redacts secret shapes |
| `brief.py` | `caps/brief.py` | |
| `pr.py` | `caps/prs.py` | |
| `watch.py`, `dashstate.py` | `caps/watch.py` | own process; restarts tell the gap |
| `doctor.py` | `caps/doctor.py` | rules only; the checks come from each capability |
| `service.py` | `caps/services.py` | |
| `hq.py`, `handoff.py`, `launch.py` | `caps/hq.py` | |
| `slack.py` | `caps/slack.py` | first version: DMs and listed channels; own state file |
| `board.py` | `caps/board.py` | a read-only live panel, see decisions |
| `dash.py`, `dash/*.go`, `page.html` | `caps/dash/` | Python standard library |
| `local/clamshell` | `modules/clamshell/` | a public module, off by default; decision and caffeinate; the root daemon stays the actuator |
| `hq.md`, `worker.md` | `prompts/` | |
| `init.py` | `caps/install.py` | see decision 31 |
| none | `caps/workspaces.py`, `caps/limits.py`, `caps/heavy.py`, `kernel/budget.py`, `kernel/bus.py`, `caps/sessions.py`, `caps/profiles.py`, `caps/permissions.py`, `caps/rules.py`, `caps/disk.py` | new |

**Dropped, with why:**
- Board columns of attached panes: Rex has no terminal mirror, and one session per worker was the point.
- `athena hq restart --update`: updating Claude is not HQ's restart's job. It also made every restart slow and risky (chaos C4).
- `reviewpost.py`, the automatic lane that posts PRs to Slack: `open-prs` keeps the record, posting stays HQ's by hand until someone wants the lane back.
- `--preview` on items: unused enough that the old flag was a silent no-op on edit. Pass a flag it lacks and argparse now refuses.
- `herdr-on-rex`, `report-agent-session`: the study's shim, and a herdr concept. Doctor relaunches from the ledger.

**Not built yet (for later workers):**
- The watch mod's status band and its completion block: Claude Code draws them from a mod, and athena-rex's HQ has none. Wakes are typed as athena's mod wrote them.
- Slack: replies in threads the human joined; a refused channel skipped for an hour.
- The `<w> post for review` lane (as a module).

The skill's procedures are `plugin/skills/athena-rex/`; the CI doorbell, `main moved`, stacks, review requests and retired workers' PRs are `caps/follow.py`. Messaging sockets aren't watched: Rex runs the sessions (decision 10).

## Decisions

The human delegated taste. These are the calls, each easy to reverse unless it says otherwise.

**0. Clean room, and what that does and doesn't mean here.** The same person read the old code and wrote this, so it isn't a clean room in the legal sense. A similarity scan (`difflib`, per function, against the old athena and the Rex prototype) found functions that came out too close, and those were rewritten from their behaviour. What remains above 0.7 is generic: three-line try/except helpers, often matching unrelated files. Shared by necessity, as contracts rather than code: file formats, the `refs/athena-backup/` namespace, the report line, the `NOT_HUMAN` prompt prefixes, and Claude's on-screen strings.

**1. Python standard library, on `/usr/bin/python3`.** No Go compiles are allowed here and there is no CI. It starts in 20 to 40 ms, against about 170 ms for the pyenv shim. Code stays 3.9-compatible. `bin/athena-rex` runs it with `-S`, and turns bytecode back on: Xcode's python keeps none by default, so every command compiled everything it imported.

**2. Start Claude directly, never type it into a shell.** `session.create` with a command runs a launcher script at the right size. This removes the 1024-byte line limit, the wait for a prompt, the shell history, the "is a shell in front" checks and the 80x24 redraw. A launcher records its pid before `exec`, so the pid is ours on any backend.

**3. One Rex session per repo, a window per worker, keyed by id.** A Rex session is close to a herdr space, as the human expected: session is workspace, window is tab, block is pane. Rex's sidebar groups workers by repo: spawn finds or makes the repo's session (`run/repo-sessions.json`, labelled with the repo folder's name) and opens the worker in a window and block labelled with its name; retire closes that window, and the session once it is empty. The ledger records session, window and block, and `status` finds a worker by its window. A worker from before keeps the session of its own it was given. HQ's session, window and block are all `athena`. Labels are for people. HQ's session moves to first in the list. After a server restart, ids return and programs don't, and a block comes back as a plain shell. So a revive opens a new window in the same session and closes the old block: one path for a crash, a restart and a lost session. Blocks stay open after exit, so a crash leaves a readable screen.

**4. Hooks and the gate go in each session's `--settings`, not a global plugin.** They fire only in a Claude athena started. This removes a whole class of bugs: status landing on the wrong worker from a matching cwd, nested Claudes (K6, K7), and the hook's shell fast path. It also turns the old plugin off in those sessions, so both versions can be installed together.

**5. Folder trust is written, not answered.** Claude's own error text names `projects[<main checkout>].hasTrustDialogAccepted`. `CLAUDE_CODE_SANDBOXED` skips the prompt too, and changes more than trust, so it is not used. Claude re-reads its config under `~/.claude.json.lock` before each write, so `trust` takes that lock, changes one key and renames. It writes nothing without the lock. **Checked with a real Claude:** without the key it stops at the prompt on a new repo's first worktree; with only the main-checkout key it goes to its input box. Rex starts programs through `/usr/bin/login`, which **resets HOME**, so an isolated HOME must be set on the program itself, not the server.

**6. Status is files first.** `status` reads the ledger, the status files and one `ps`, with no backend call unless `--live`. A dead Claude reads as `exited` from its pid. **Liveness has three answers:** a `ps` that cannot answer raises `Unknown`, which never reads as gone, and retire then removes nothing.

**7. Explicit state machines.** Worker lifecycle: `ledger.LIFECYCLE`, `ledger.phase`. Goal delivery: `ledger.GOAL_MOVES`. Retire: its stages, in `caps/retire.py`, none of them stored. HQ: `hq.state`. Activity: `hooks.next_state`, a pure step. Workspace: `workspaces.classify`, a pure function of facts. Whose move: `prs.move`. Clamshell: `evaluate` and `decide`. These replace flags read in many places.

**8. Shared state stays compatible; ours is additive.** Same files, same fields, for `ledger.jsonl`, `workers/`, `needs-you.json`, `actions.jsonl`, `requests.jsonl`, `history.jsonl`, `health.json`, `watch.json`, `hq.json`, `stack.json`, `open-prs-posted.json`, `config.json`. Rex places are the added fields `backend`, `space`, `block`. Doctor's memory is under an added `rex` key of `doctor.json`. New files: `events.jsonl`, `slack-rex.json`, `board-rex.json`, `service-rex.json`, `run/`, `settings/hq-rex.json`, `hq.json.prev`, `needs-you.json.prev`, `scratch/`. A worker with no `backend` is the old athena's: it is listed, and retire won't stop it. Not locked against the old athena: one runs at a time.

**9. The watch is its own process.** A flock makes it single, it persists its snapshot and undelivered wakes, and doctor restarts it alone. GitHub is fetched on its own thread, so a slow GitHub can't read as a dead loop, and liveness is measured per loop. It never costs HQ a restart. Lines are `wake`, `routine` or `quiet` where they are made, and pass an allowlist before they go out. Wakes are still typed as prompts (mitigated, not removed).

**10. Rex runs the sessions, HQ's and the workers'.** Doctor never brings a Claude back, starts the Rex server or restarts HQ; it says what is gone, and `athena-rex hq` opens HQ again on its conversation.

**11. The machine budget holds launches.** Spawn refuses under 25% free memory, under 10 GB of disk, or at load above 3 per core. An ultracode worker costs more. `--force-budget` goes past it for spawn. Readings it can't get count as room: this holds launches, it isn't an interlock.

**12. New report status `IN_PROGRESS`.** Rests as idle and wakes no one, so a worker with background work stops reporting a false BLOCKED. DONE doesn't need HEAD pushed: local-only work reports DONE, as in the old athena.

**13. Board replacement.** A read-only panel (`board open`), redrawn from status and capture, with the tail of each busy worker's screen. A glance, not a place to type. `board jump` needs Rex's Remote Control setting, which is off by default and which nothing here changes.

**14. Clamshell keeps the root daemon.** It reads `hq_alive` from `health.json`, which doctor still writes, so no new root code is needed. It needs no repointing either: the old path stays as a link (decision 32), so no `sudo` is needed when moving from athena.

**15. Default state dir `~/Library/Application Support/athena`,** `ATHENA_STATE_DIR` overrides. Paths with a space are quoted in hooks and handled by `plistlib` in services.

**16. Real side effects have one door, shut by default.** `kernel/world.py` is the only way to run `security`, `osascript`, `pmset`, `ioreg`, `caffeinate`, `launchctl` or `gh`, or to reach a module's real card, Slack, a browser, the human's Rex server or the real `~/.claude.json`. It opens only when `config.json` in the state dir itself says `rex.real_world: true` (`ATHENA_CONFIG` can't point it elsewhere), and never while `ATHENA_WORKER` starts with `rex-`. No environment variable opens it, because the first version of this guard hung on one: the test harness deleted `ATHENA_WORKER`, and the tests wrote a real card. A scratch copy can't inherit it. Places are compared by what they are, not how they're spelled: links are followed, case is ignored, and a folder is also known by device and inode (`lab.inside`), since this disk ignores case and `realpath` keeps a wrong spelling. With the door shut, the only Rex endpoint that may be named is one under the per-user scratch root with a `.athena-server-owner` mark beside its socket naming this state dir, so neither the human's server nor another worker's can be; and any Claude config under the real home is off limits. **`install` sets `rex.real_world: true` on a state dir it creates, and `install --real-world` on one that moved,** or the real install stops short of all of these. Until then a launch that stops at Claude's prompt says trust wasn't written. `ps`, `git`, `du` and `memory_pressure` stay outside the door on purpose: they only read. Under test `ps` is a recording stub too. `kernel/lab.py` and its shell copies separately keep the rewrite's workers off the live state dir; `install --lab-guard-out` takes them out of an installed copy.

**16a. Tests have four walls, and run only through `scripts/test.sh`.** An OS sandbox holds for every child, Python or not: no writes under the real HOME or `/Volumes`, no reads of live state, the card or Claude's files, none of the real tools, Rex or Claude, no network but loopback and not the live dashboard's port, no Unix socket but its own folder's, no signals outside. The harness stops at import outside it, asking the sandbox by a write only it forbids and accepting only its `EPERM`. The door is shut, and each test ends by checking that. Named tools resolve to recording stubs, first on `PATH`, and the card is a fake folder. A canary, a Python audit hook in the test process and its `bin/athena-rex` children, stops and records any reach for a real tool, an unexpected program, a guarded path, a Unix socket outside the test's own folders, the network, the live dashboard's port or a signal to a pid that isn't the test's, and the harness fails any test whose log has an entry, even if the test swallowed the error. `tests/test_world.py` proves each with poses, the real list directly with no file touched, and shell children against the sandbox. The sandbox uses `sandbox-exec`, which Apple marks deprecated, so a later macOS could drop it; the suite then refuses to run rather than running bare.

**17. Scratch copies come from `scripts/scratch-state.py`.** It copies named files only, never `secrets/`, pids, locks or `hq.json`. It clears live and failed workers, repoints roots, and turns HQ's bypass off.

**18. Text is typed in pieces of 200.** Over about 800 characters at once, Claude takes typed text for a paste: it arrives wrapped as `<pasted_content>`, it reads as data and not instructions, and a `/goal` never runs. Measured on a real Claude: 800 clean, 900 wrapped, and 3990 in pieces clean. Spawn's own goal clauses are about 450 characters, so this is the normal case, not an edge. Smoke checks a long prompt.

**19. A second source for reports: the transcript.** Reports still go through hooks. A status file that's lost, a Stop that never landed or a hook field that gets renamed fall back on Claude's own transcript, and a report read that way says so, since the gate never saw it. It counts only if no prompt or tool call follows it and the gate's own rules pass, so a long tool call never reads as last turn's report, and a DONE the gate sent back never reads as done. Its format is Claude's, so anything unexpected is simply no report.

**20. Limits, sign-in and outages are read from the bottom of a screen.** Claude tells no hook about them. It's the weakest input here: it's only read when a worker isn't working, only in the last 12 rows, and only as a row of its own, so a worker discussing limits, API errors or a login doesn't park itself or get typed at; a miss only means reading as idle, as before. A reset names no date, so it's taken as the nearest such time, which can be past: the line stays on screen after its reset.

**21. Slow is not dead, at every level.** A Rex timeout raises instead of reading as down, and doctor touches nothing for it. Load above 3 per core holds all repairs, since a repair only adds to it. So a starved Mac gets flags, and no action, until it recovers.

**22. Who watches doctor is the watch.** They're separate supervised processes, and each raises the other: doctor restarts a stale watch, and the watch tells HQ, once, about a stale doctor.

**23. A lost ledger comes back from a snapshot** doctor keeps beside it (`run/ledger-live.json`), and no launch happens until it has. A retire after the last snapshot and before the loss would bring that worker back as live; retiring it again costs little, and a lost live worker costs more.

**24. Retire stops what its worker left running,** as listed from its terminal before the stop, and only a pid still running that command. The old classifier refused such kills; this is a retire someone asked for, and it says what it stopped.

**25. On a full disk the gate outranks the lock, and only the gate and doctor's look do.** A lock file that can't be created raises, so nothing that decides (two HQs, a capacity, a heavy slot) runs on a lock it doesn't hold, and the command says the disk is full. Hooks opt in with `full_disk_ok`, so two hooks on one worker could race there: losing a status write is the smaller loss, as a gate that stops working turns off every report check. Doctor opts in to report, and repairs nothing without the lock. An event that can't be written is lost alone (`bus.emit` never raises), and a running watch carries on without its files.

**25a. A broken config with no good copy refuses.** Defaults would quietly change what HQ may do, so HQ and spawn stop and say so. A running worker carries on.

**26. An amended goal goes in even when Claude is busy,** unlike a first goal, as Claude queues what is typed while it works, and a worker looping on a goal may never be idle.

**27. The gate checks a PR's URL, not GitHub.** A hook has seconds, and the quota is shared. The watch's own fetch sees the real PR.

**28. A retire keeps no stage, only pointers.** A killed retire is finished by running it again, and each stage asks the world what is left. What the world can't say goes on record as it is made: where the work went (`retire_kept`, with `backup`, `wip_backup`, `trash`, `ignored_kept`), and what else ran on its terminal (`retire_kin`). Every save stays listed, since a re-run's wip can hold less than the first: a removal killed halfway reads as deleted files.

**29. A first goal is a goal state, not "at a startup prompt".** `first` means it hasn't gone out, whether Claude is at a prompt or its box was full. So the typing guard asks `agents.state`: only when it says startup, which a Claude reporting a status never is, does a numbered row or no box at all stop counting as typing. Past that, only a first goal whose Claude has shown nothing yet goes through, as a launch that timed out leaves it. Every move of a goal takes one lock, so an amend waits for a send in flight instead of being overwritten by its outcome.

**30. A snooze ends, and a default gives its whole wait again after it.** Applying it the moment the snooze ends would answer for someone who asked to be asked later. The same goes for a whole queue: what was waiting at the first look on a state dir gets its whole 12 hours from then, with one line saying so, so moving to a new install answers nothing on its first tick.

**31. The installer is a capability, not a script: `athena-rex install`.** One word for install, update, `--check`, `--rollback`, `--uninstall`, `--dry-run`, run from a checkout the first time. A release is a whole copy under `~/.local/share/athena-rex/releases/<id>`, and `current` is a link swapped atomically, so a running Claude's hook never sees a half-copied tree. For that to hold, everything handed out by path (hooks, prompts, launchd jobs) goes through `current`, never a release, and `bin/athena-rex` keeps its path logical. Two releases are kept. Nothing is written in `~/.claude`, as hooks and permissions go in each session's own `--settings`. It never edits a shell profile: it prints the line. Someone else's file at `~/.local/bin/athena-rex` stops it, and `--force` moves that aside. Launchd is `--services`, separate, since nobody wants two systems' jobs on one state. Where it improves on the old one: a checklist before anything changes, a dry run, one clear line per thing with `=` for unchanged, profile changes shown as a diff, and a rollback.

**32. A state dir can move by one rename and a link back.** Moving from athena, the old athena's hooks, its job files, live workers' settings and the clamshell root daemon all name `~/.local/state/athena`. Renaming it to the new default and leaving a link at the old path keeps all of them working, so live old workers aren't disturbed, there is nothing to repoint, and going back is a rename.

**32a. launchd is asked, not assumed.** A job file gone or a bootout's exit code says nothing of a running job, so a move goes by `launchctl print`, and `services` fails on a failed launchctl. Two systems on one state is the failure this exists to prevent, so it stops rather than says "stopped". Before the move it looks at what is running: a Claude named as HQ and the old athena-watch mod's `athena watch --tagged`. That mod needs the plugin and `ATHENA_ROLE=hq`, and the new HQ's settings turn the plugin off for that session.

**32b. HQ rotates by tokens, config `hq_rotate_tokens`, 400k unless set.** A transcript names no window, an HQ runs far past 200k on plain `opus`, and Claude Code does not compact at 200k, so no window is derived from the model. Doctor asks HQ to rotate once its context holds the line, and status and doctor show it as `hq context 171k of 400k (rotates at hq_rotate_tokens)`; a bare `hq rotate --if-context-over` rotates past it. Only `--if-context-over <pct>` measures a window: `rex.hq_context_window`, 1M unless set (`ATHENA_HQ_CONTEXT_WINDOW` overrides). HQ's own `--settings` (`settings/hq-rex.json`) set `"idleCompaction": false`, so Claude Code never compacts HQ while it sits idle: HQ rotates instead. Workers' settings leave it alone, and `autoCompactEnabled` is never set for either, the user's backstop near the limit. Config `hq_model` (default `""`) is the model HQ starts on: `""`, null or unset passes no `--model`, so HQ runs on the user's Claude Code default; `hq open --model` wins over it. Both keys are top-level, shared with the old athena's config, where they mean the same.

**33. The lab guard leaves an installed copy, not the source.** `install --lab-guard-out` rewrites that copy's `lab.py` and `bin/athena-rex`, and stays out through later updates (the `-live` release). The checkout keeps its guard, since it's what protects this state from a test or a `rex-*` worker there. In a checkout the shell guard names the new default path only in the account's own home, so an isolated HOME's default path is that HOME's own.

**34. Workers are windows in their repo's Rex session, in the words of the app.** `status` reads a snapshot the watch and the dash keep (`run/sessions.json`), so it stays a read of files: a label always, `(gone)` only from a fresh look that didn't find it, never from a server that didn't answer. `sessions` asks Rex for panes and who is looking. Only what the ledger says athena made can be an orphan; everything else is `other`, which nothing closes.

**35. A permission prompt's answer is pressed only while that same prompt is asked.** The dash writes an answer, and the watch's tick presses Enter or Escape only if the last thing on that session's screen is `Do you want to proceed?` with Yes under the cursor, and the command between its rules is exactly this one, by digest, so a secret needn't be kept to compare it. A late click presses nothing. A reply is words, not a yes. It never has a default, and "Always allow" adds that exact command, not a prefix, and never one holding a secret. Bash only: other tools' prompts are drawn differently, and stay blocked and wake HQ.

**36. Checkable orders are a hook, the rest prose.** Profiles' deny rules match how a command starts, so `cd x && git push -f` gets past them. `caps/rules.py` reads the line as a shell would, each command of it, so quoted words aren't commands, and `bash -c` and later lines are. It is a guard against mistakes, not a sandbox: a script can do what its text doesn't say. The hook adds about 80 ms to each Bash call, and only Bash.

**37. A wake carries its lines, rebuilt from fixed words (restored).** It was once a constant line read back with `watch --inbox`, which is kept for anyone who calls it. Decision 9's lines still pass the allowlist, and now go to `run/inbox.jsonl`. HQ reads them with a command, so they reach it as a tool's output, and what's typed in its voice is constant. It costs one tool call per wake. A wake that can't be typed leaves nothing in the inbox, and is held as before.

**38. Goals up to athena's 3990 characters, typed in pieces of 200 (restored).** Once it was under 800. Spawn's clauses are about 250 now, and `goal_text` refuses at 790, as anyone pasting one by hand has no pieces. Typing in pieces stays for everything else: a long prompt, or a `/goal` someone sends with `send`.

**39. A forced retire won't copy more than `rex.ignored_copy_max_mb` (2 GB) of git-ignored work.** It refuses before it has changed anything, and says how to get past it: a 40 GB copy into the trash on a nearly full disk is its own failure. The limit never means losing it.

**40. The menu bar app is Swift, and does everything through the CLI.** `menubar/` is an AppKit status item built by `menubar/build.sh` with swiftc, which `services install` runs when the app is missing or its sources changed (by content, as a release is a copy), and installed as the `menubar` login job, which runs `athena-rex menubar run`. It reads `services status --json`, `status --json`, `modules --json` (the modules menu contract) and `doctor --last --json`, and acts with `services stop|start|restart`, `dash open`, `hq open` and `modules action <name> <id>`, so it never names launchctl or the Rex server and the lab's doors hold for it. Quit athena is `services stop`: doctor first, so nothing brings the rest back, then athena's other jobs, each disabled as well as booted out so a login doesn't bring it back either. Rex runs the sessions, HQ's and the workers', so Quit never stops the Rex server or any Claude. Once the watch is down each capability's `on_quit` runs, so a card a module drives is set off rather than frozen on a stale colour. A module's jobs and the app's own are left: the app shows athena stopped, with Start athena, which is `services start`, loading them again with doctor last. While stopped, Open dashboard and Open HQ are off, as each would start part of athena outside launchd. Doctor's dash and watch repairs stand back too, as a repair then would undo Quit. A Doctor submenu shows the last pass from `doctor --last --json`, which reads health.json and runs nothing, read at most every 30 s; Run doctor --fix runs it. The app polls one at a time: a poll asked for while one runs runs once after it, so a slow athena-rex never piles up processes. Knot's working motion is drawn in Swift from the strands in `knot-working.svg`, with no frame files.

## What is proven, and how

- `scripts/smoke.sh`, on a private server, with real Claudes: trust (with a negative control), 3 workers at once, send, refusals, the report gate, a kill mid-turn reading as exited and left to Rex, watch, dash, the signal sandbox, HQ and its rotation.
- `tests/`, in an isolated HOME on the fake backend, behind decision 16a. `tests/test_chaos.py` is the chaos catalogue, one test per case.
- `scripts/chaos-rex.py --real`, on a private server: a killed server and a cut ledger, HUP ignored, what a worker leaves running, a real full disk, long prompts and goals, nested and foreign Claudes.
- `scripts/install-transcript.sh`: the installer, end to end, in an isolated HOME.
- `tests/test_shapes.py`: every shared file, made by its real writer, held to what the old athena reads.
- Not proven: how any of it looks in the Rex app, a real app update, launchd installs, real Slack and GitHub, the real card and the real daemon, a real usage limit or account switch, an expired login on a real account (its row is from a real Claude given a 401 by a local API), deny rules under the bypass flag, doctor starting a server by repair. All are off limits here or can't be had on demand.

Workspaces have their own design in [docs/workspaces.md](docs/workspaces.md).
