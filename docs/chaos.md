# Chaos catalogue

What can randomly break, and what should happen. **Today** comes from reading the old code at commit
2ea3764 (herdr backend), plus the Rex study and lab notes for Rex. Nothing here was injected: it is a paper exercise,
and every "inject" is for the rewrite's test suite, on a scratch state dir and a private server. File references are
to the old code. `<state>` is the state dir.

Injection helpers assumed: a fake `herdr`, `gh`, `git` or `ps` on `PATH` (old seams: `ATHENA_HERDR`, `ATHENA_PS`,
`ATHENA_PMSET`), a fake clock, a small disk image mounted as the state dir, and a test Claude on a private Rex server.

## Top 15, by likelihood times damage

| Rank | Case | Why |
|---|---|---|
| 1 | K1 worker dies mid-turn | Happens on every crash. Reads as `working` for ever, and nobody acts |
| 2 | M4 CPU starvation | Slow herdr reads as dead, and repairs act on it |
| 3 | M3 memory pressure | Can take the Mac down. Athena flags and keeps launching |
| 4 | W4 watch restarts mid-event | Every watch restart. Changes in the gap are never told |
| 5 | M5 disk full | Empties `hq.json`, and turns off the report gate |
| 6 | G1 GitHub or network slow | A slow tick reads as a dead watch, and HQ restarts for nothing |
| 7 | C1 usage limit | Can stop a worker for hours. No model of it at all |
| 8 | R1 server killed | Every Claude dies with it, the human's too |
| 9 | C4 Claude update changes wording | Releases are near daily. Exit, typing and prompt checks all read the screen |
| 10 | R3 app update | Not tested. The main cut-over blocker |
| 11 | S2 lost spawn line | One bad line crashes every ledger reader |
| 12 | S4 `hq.json` lost | Doctor reports green, and `athena hq` opens a stray space and repoints the record |
| 13 | S9 state dir moved or split | Certain at cut-over. Live sessions keep the old path |
| 14 | K4 worker frozen | Same silence as K1, with no process exit to find |
| 15 | M2 crash and reboot | Everything resumes at once, into the load that caused it |

## State files

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| S1 | Ledger's last line cut off (crash mid-write, short write) | Bad line skipped (`ledger.py:57`). No newline repair, so the next append joins it and is lost too. A lost `retire` brings the worker back as live | Each append starts by repairing a missing newline, and checks the full write. Bad lines are counted and reported | `truncate -s -20 ledger.jsonl`, then run a retire and read `workers()` |
| S2 ★11 | A worker's first ledger line is lost, then an `update` arrives | `out[name]["updated"]` raises KeyError (`ledger.py:85`). `workers()` throws for every caller: watch errors each tick, doctor checks that read it fail, spawn and retire fail. Workers with `ATHENA_WORKER` keep recording, except for their SessionStart resume registration (`hookstatus.py:35`, `:278`). A session on the cwd fallback stops recording | The fold never throws on data. An orphan event creates a `damaged` record and an item | Delete a `spawn` line from a scratch ledger with later `update` lines; call every reader |
| S3 | `ledger.jsonl` deleted | No live workers: watch says `gone from the ledger` for each. The shell hook exits early when the file is missing (`hooks/worker-status`), so all status stops silently. Capacity reads 0. Running workers are orphaned | Rebuild from a snapshot and from the live sessions. Refuse launches until reconciled | `rm ledger.jsonl` with test workers running |
| S4 ★12 | `hq.json` cut off, emptied or deleted | Eight plain `write_text` calls truncate first (`hq.py:103,139,152,199,263,306,326,912`). `load()` gives `{}`. Doctor records ok "no HQ yet" (`doctor.py:235`). `hq_alive` goes false, so the clamshell daemon drops. `athena hq` finds no pane and opens a new space to resume HQ's conversation. herdr's unique agent names should refuse the start, and `_resume_agent` swallows that when the name is owned (`hq.py:284-289`). So, read from the code and not run: a stray space, `hq.json` naming a pane with no Claude, and a false restart routine. Rex may not refuse a second Claude | Atomic writes with a previous generation. Discover HQ from live sessions before believing the file. Never run two Claudes on one conversation | `: > hq.json` with HQ up; run doctor, then `hq` with a fake herdr |
| S5 | `workers/<name>.json` corrupt or deleted | `read` gives None (`hookstatus.py:163`), so the next event starts from nothing: report, `last_report` and block count are gone | Keep the report in an append-only per-worker log. The status file is a cache that can be rebuilt | Corrupt the file, then send a Stop event with no report line |
| S6 | Status hooks overlap | Read, change, write with no lock (`hookstatus.py:200,239`). A Notification beside a Stop can drop the report | Append events and fold them, or lock | Run two `apply` calls concurrently with a slowed read |
| S7 | `needs-you.json` corrupt | Writers refuse, and nothing is overwritten (`asks.py:136`). Readers see no items. Doorbell tells once. The best-behaved file here, but the dash just looks empty | Keep this. Add a previous generation, and show "items unreadable" on the dash | `echo '{' > needs-you.json`; add an item; load the dash |
| S8 | `config.json` corrupt | Gated workers carry on. Spawn raises `ConfigError`. `_hq_skip_permissions` and `_hq_allow` fall back quietly (`hq.py:56-70`), so a restart silently changes HQ's permissions. Doc tells the human to fix it by hand | Keep the last good config and use it, with a loud flag. Never change permissions because a file failed to parse | Break the file, then run restart with `--dry-run`-style argv capture |
| S9 ★13 | State dir moved, or two in use | `ensure()` creates whatever path it is told, so a wrong path looks like a fresh install. Absolute paths live in the launchd job (`services.py` `CARRIED`), `settings/<name>.json` and `hq.json`'s args. Workers keep the old path until restarted. One test HQ on a temp dir still read real files | Refuse to start on an empty dir when a marker says state exists elsewhere. Move with all sessions parked, and verify no process holds the old path. Lab guard refuses the live path | Start with `ATHENA_STATE_DIR` on a new path while a worker's settings name the old one; assert refusal |
| S10 | Wrong schema version | Only `config.json` has a version. The others have none, so newer or older writers mix silently. Migrations are one-way (`needs-you.json.v1`) | Version every file. Refuse a newer major. Additive only while both versions run | Add `"version": 99`; add unknown fields; write old-shape records |
| S11 | Half-written leftovers | Temp names carry a pid and are never swept. `board.py:98` and `service.py:291` use a fixed `.tmp` that two writers can share | Sweep old temps at start. Always use unique names | Leave `x.tmp123` files; run two writers at once |
| S12 | Clock jumps backward | Watch age goes negative, under `WATCH_STALE`, so a dead watch reads healthy (`doctor.py:279`). Repair backoff lengthens. `down_for` shrinks | Use monotonic time within a process. Treat a negative age as unknown | Fake clock minus 1 hour during a doctor run |
| S13 | Clock jumps forward, or DST | Watch reads stale outside the wake grace, so HQ restarts. `retiring` marks expire early (`watch.py:469`). The mod reads the UTC offset once, so wake times are wrong after DST (`register.tsx:150`) | Same. Format times per use. Tell a jump from a sleep | Fake clock plus 1 hour; change `date +%z` |
| S14 | Lock left behind with a reused pid | `hq-restart.lock` trusts `kill(pid, 0)` with no command check (`hq.py:450`). A recycled pid blocks every restart and rotation, and each failed doctor repair counts toward giving up. `dash.py:146` and `board.py:302` do check | `flock` that dies with the process, everywhere | Write a live unrelated pid into the lock; run restart |
| S15 | Oversized line in a JSONL file | Dash stops reading that file at 4 MiB and shows a notice (`dash/state.go:245`). Later lines are hidden | Cap what goes in. Skip the line, not the rest | Append a 5 MiB line |
| S16 | `hq.json` and hq-start lost during rotation | `_forget_session` clears both before the fresh start (`hq.py:893`). A crash in that window leaves no session, and `previous_session` is never used to recover (`hq.py:158`). Doctor stops at "no conversation known" | Commit the switch only after the new session is confirmed. Fall back to `previous_session` | Kill the child between forget and start, in a test |
| S17 | Disk full during a state write | Files using a temp and rename stay intact, while `hq.json` empties and hook writes fail. See M5 for the whole case | Writes fail safe, and old content survives | Small disk image as the state dir, then fill it |

## Monitors and watch

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| W1 | Watch hung on a call | `gh` and herdr have timeouts, but most `git` calls don't (`gitops.py:12,20,34`). One loop runs everything, so a hung `git` stops the Slack and dash doorbells too. Doctor sees it after 5 min plus 90 s, and can only restart HQ at its next idle | Each source in its own task with a deadline. The dash and report paths never wait on the network or git | Fake `git` that sleeps 600 |
| W2 | Watch process dies | Mod restarts it, 5 s to 60 s, told once per run of quick exits (`register.tsx:215`). If the mod is gone, doctor restarts HQ, three times an hour, then flags it | A supervised service, independent of HQ | Kill the test watch; remove the mod in a test |
| W3 | Two watches running | No lock in `run` (`watch.py:929`). Goal sending is locked, nothing else: both write `watch.json` and `ci-failed.json`, both wake HQ. Mod plus the Monitor fallback gets there. Three board watchers were once seen, since fixed by `flock` | Single instance by `flock`, per state dir | Start two against one scratch state |
| W4 ★4 | Watch restarts mid-event | `prev` starts None, so the first tick skips `diff` (`watch.py:826`), and `watching: ...` is routine, not a wake (`watch.py:415`). A report, merge or failure in the gap is never told, unless HQ's own restart routine reads status. Only CI incidents persist | Persist the last snapshot. Diff against it at start, and tell what changed while down | Stop the watch, change a worker's status and PR, start it, assert the lines |
| W5 | Stale data shown as current | A failed `gh` keeps the last PR (`watch.py:776`). The dash shows an age after 120 s (`dash/view.go:18`). The status band shows none | Every value carries its age and source. Stale is drawn as stale | Fake `gh` failing for 10 minutes |
| W6 | Wake lost in the mod | Pending lines are in memory only (`core.ts`, `Waker`), so an HQ exit loses them. The "told" memory persists, and drops a repeat within 10 minutes, so a second real block can go unheard | A durable inbox with acknowledgement. Deduplicate by event id, not by text | Queue a line while busy; restart; repeat one line twice within the window |
| W7 | Doctor hung or dead | `flock` makes later runs skip, and the services supervisor never starts a run while the last one is going. `health.json` goes stale. The dash says unknown after 180 s, and clamshell treats it as stale. No notification, since doctor sends them | A small dead-man check outside doctor (launchd alert, or the dash raising a notification) | Fake herdr that never answers, with its timeouts removed |
| W8 | Doctor run by hand hides a problem | `finish` writes problems without `--fix` too, so the service tick no longer sees them as new and never notifies (`doctor.py:543-547`) | Track "notified" separately from "seen" | Run a read-only doctor, then a fix run, on a broken part |
| W9 | Doctor gives up | Three repairs an hour, then a flag. Fine by design, but the notification fires once | Escalate on a schedule while it stays down. Say what the human must do | Fake a repair that always fails |
| W10 | Slack doorbell silently off | No token, or a locked Keychain after reboot, sets off (`slack.py:349`). The note goes to stderr, which wakes no one (`watch.py:879`). Doctor has no Slack check | Report it as a source with a state on the dash | Fake `security` exiting 1 |
| W11 | Blocked HQ never flagged | `check_hq` records any agent status as ok (`doctor.py:245`) | Age of blocked is a check, with a limit | Fake agent status `blocked` for an hour |

## Rex and sessions

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| R1 ★8 | Server killed | herdr: launchd restarts at most every 30 s, herdr resumes its agents, doctor waits 90 s then resumes HQ. Rex study: every Claude dies, launchd did not restart it in 15+ s, the next `rex` call autostarts it from the bundle with `--exit-empty`, sessions return as empty shells, `resume_all` restored conversations in about 4 s, scrollback lost | Doctor detects a down server and starts it one fixed way. Resume all on the same conversations. List the human's own sessions, and resume or warn about them too | SIGKILL a private server with test Claudes |
| R2 | Server stopped by a stray call | Without `-S`, a call reaches or autostarts the human's server. A kill can take a live session of the human's | Every call names its endpoint. `stop` lists what would die, and a separate step confirms it | Assert the wrapper refuses a call with no endpoint |
| R3 ★10 | App update with live sessions | Not tested: no newer build existed. Unknown whether the old server keeps running, how a new app talks to it, and what happens to a server inside the bundle. Two kinds of server seen (launchd's, and the CLI's from the bundle) | Pin one way to start. Before any update, record sessions. After, verify pids or resume. Check versions at start | Copy of the app, swap the build under a private server; `app_check.py` |
| R4 | Socket gone or path too long | herdr: `server_running` says no on any error (`launch.py:33`), so doctor tries a start. Beside a live server `herdr server` exits "already running", and launchd retries every 30 s (`docs/troubleshooting.md`). Rex: macOS caps the path at 104 bytes, and scratchpad paths exceed it | Probe by connecting. Tell gone, refused and too long apart. Short socket paths by rule | `rm` the socket of a private server; a 120-byte path |
| R5 | Session renamed | HQ and workers are matched by name (`hq.py:29`, `doctor.py:231`). A rename reads as HQ gone: doctor waits, then tries to start into a busy pane. The watch resets labels each tick, which doesn't help a name match | Key on a stable session id. A name is a label | Rename a test session; run doctor |
| R6 | Ids reused after a restart | Handled for spaces by checking the checkout (`retire.py:14`), and for panes by agent name. The lost-pane fix has run only against a mocked herdr | Verify ownership before every action. Test against a real restart | Restart a private server, and swap sessions' ids |
| R7 | Dead client still listed | `rex client ls` keeps a dead app's record, once 30+ s (Rex study) | Never use it for liveness | Force-quit a test client; assert doctor ignores the list |
| R8 | Launch text goes wrong | Launching types into a shell: a 1024-byte line limit, must be at a prompt. New workers sit at 80x24 until opened. Startup prompts rest on screen text | Launch via a short script file. Set the size first where possible. Start only on SessionStart | A 2000-byte argv; a slow shell prompt |
| R9 | Process ignores SIGHUP | Rex sends SIGHUP only, and the process stayed (Rex study) | Kill after a grace, by owned pid, `pid > 1` | A test child that traps HUP |
| R10 | Lab touches live | Plugin hook fires on any Claude, so a smoke Claude inheriting `ATHENA_WORKER` or running in a worktree can overwrite a worker's file or trip the gate. `dash.pid` copied in a state backup points at the live dash. `/` on port 2843 false-passes. Inherited `ATHENA_STATE_DIR` hits live state (lab notes) | Refuse live paths while in a lab. Sanitize copies. Health checks verify the state dir they answer for, as `_adopt` does | Start from an unsanitized copy; assert refusal |

## Workers

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| K1 ★1 | Claude dies mid-turn (crash, kill, OOM) | No SessionEnd, so the file stays `working`. `status.collect` prefers hook state, and only uses herdr's when unknown (`status.py:43-52`). Doctor resumes a worker only for a missing socket (`doctor.py:346`), and tells one herdr resumed to carry on. A Claude that died mid-turn is neither case. Reads as working for ever | Reconcile hook state with the server each tick. Dead means resume once on the same conversation, tell it so, and cap the tries | SIGKILL a test Claude mid-turn |
| K2 | Hook missing or failing | Silent by design: `2>/dev/null`, `exit 0`, `except: pass`. Under load the 10 s timeout drops the event. No gate, no status, no one told. A bare resume loses `ATHENA_WORKER`, so the gate is off (`hookstatus.py:343`) | Expect SessionStart within a limit of launch, else flag. Track last hook time. Log hook errors to a file | Replace the hook with `exit 0`; launch without the plugin |
| K3 | Report lost | Stop hook failing loses it. Three bad reports go to `idle-unreported` with no report (`hookstatus.py:191`). `UserPromptSubmit` clears `report`, keeping `last_report`. SendMessage failed up to 20 times in one session | Report through a command that writes a durable record. Read the transcript as a second source. HQ acknowledges | Malformed last line; drop the Stop event; no SendMessage target |
| K4 ★14 | Worker frozen | State stays `working`. `hook_ts` is read and never compared (`status.py:28`). Study's prototype sees frozen as stopped | Activity from hooks and transcript growth. A limit raises one item with the screen. Then a stop and a resume, by policy | SIGSTOP a test Claude; a fake tool that never returns |
| K5 | Goal never arrives | `goal_pending=None` is written before sending (`spawn.py:393`), so a crash there leaves `prompt: sending` with nothing retrying. After `agent start`, the goal is typed with no screen check (`spawn.py:243`), and Enter can answer a prompt whose default is "No, exit". A worker can stick in `starting` | Send only after SessionStart. Confirm via UserPromptSubmit. Retry a confirmed miss | Kill between write and send; a fake Claude at a trust prompt |
| K6 | Nested Claude's events | `nested()` returns False when `ps` fails or takes over 5 s (`hookstatus.py:53,74`), so under load a nested run counts as the worker. Suspected in a test failure. Only `claude` is recognised | Tie events to the session id at launch | Fake `ps` that hangs; nested Claude under a worker |
| K7 | Foreign session in a worktree | Any Claude there writes the worker's file (`hookstatus.py:34-45`). | As K6 | HQ `cd`s into a worktree, then exits |
| K8 | Retire interrupted | The `retiring` mark clears in `finally`, but a killed process leaves it for 30 min, hiding a real exit. A half-removed directory needs `--force` by hand (`retire.py:193`) | Recorded steps, resumable, idempotent | Kill retire after each step in turn |
| K9 | herdr times out during retire | `agent_owned` false means "gone", so the checkout is removed under a live Claude (`retire.py:26,89,179`). Only when herdr first answers the screen read: an unreadable screen refuses without `--force` (`composer.py:109`, `retire.py:228`). | Three-valued answer. Unknown stops | Fake herdr timing out on `agent get` |
| K10 | Worktree gone under a worker | `_head` returns None and the DONE check is skipped (`hookstatus.py:141`). Spawn refuses an existing path | A missing worktree is a state, not a pass | `rm -rf` a scratch worktree, then send DONE |
| K11 | Descendants outlive the worker | Retire stops Claude, and nothing tracks its children. Stuck VERIFY runs were reported, and the classifier refused killing them | A process group per worker. Stop it on retire, and report leftovers | A worker that starts a detached `sleep` |
| K12 | Long goal truncated | `build_goal` cuts at 3990 characters (`spawn.py:143`), and the report and brief clauses are at the end | Move them first, or refuse | A 5000-character goal |
| K13 | Goal stale after a scope change | Worker loops on the stop hook until the human or HQ's nudge clears the goal. Nothing amends it. | An amend command | Change the brief after launch |

## Claude

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| C1 ★7 | Usage limit | No code knows of it. Workers stop, and may read as idle or stuck. HQ hitting it stops all routing. A worker can stop for hours, or leave PRs unpushed | Detect it, park as `limit` with a reset time, resume after. Show it once, not per worker. The kernel keeps running | Fake Claude printing the limit text; a stub API returning 429 |
| C2 | Account switch while running | Not detected. Connector identity, artifact ownership and the Chrome extension all change. HQ learned this by memory | Record the account at each session start. Identity checks before writes. Mark tools unavailable | Swap a scratch `CLAUDE_CONFIG_DIR`'s credentials between turns |
| C3 | Auth expired | Each session stops at its own login prompt. No global view. Each worker reports alone | One item: "sign in". Park everyone, then resume | Invalid credentials in a scratch config |
| C4 ★9 | Claude Code update | `update_claude` runs at HQ restart (`hq.py:515`), and sessions keep old code until resumed. Exit, typing and prompt logic match exact screen text (`retire.py:58-60`, `composer.py`). New wording reads as "someone is typing" or "did not exit" and refuses, safely but for good. A plugin update makes a new cache folder, leaving live sessions on the stale copy. A renamed hook field reads as no report | After an update, run a smoke session on a private server before trusting it. Prefer hooks and APIs over screen text. Pin wording in tests per release | Fake Claude with changed wording; rename a hook field |
| C5 | API outage | The session ends its turn with an error. The gate blocks up to 3 times, then `idle-unreported`. Nothing retries | Recognise it, back off, resume once | Stub API returning 529 |
| C6 | Conversation can't be resumed | `ResumeError`, never a fresh HQ on its own (`hq.py:281`). Doctor tries three an hour, then flags it. Good | Keep this. Offer "start fresh from the handoff" on the dash | Bad session id |
| C7 | Context fills up | Doctor asks HQ at 60% only when idle, at most every 2 hours (`doctor.py:418`). A busy HQ is never asked. If usage can't be read there is no check | Rotation is the kernel's job, not a request to the LLM | Fake transcript at 95% with a busy agent |
| C8 | Permission prompt or classifier refusal | Worker reports once and waits. HQ hits the same | One item with the exact command. Approve from the dash | Fake a denied action |

## GitHub, Slack and network

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| G1 ★6 | GitHub slow, or network down | Serial tick: up to 15 s herdr plus 30 s `gh` per worker, 60 s fetch per repo, 60 s batch, 30 s per CI target. Over `WATCH_STALE` when eight workers are live, so doctor restarts HQ, which fixes nothing | Source health per service. A slow source never fails liveness. No restart for an outside outage | Fake `gh` and `git fetch` that take their full timeouts |
| G2 | GitHub quota out | `gh` fails, None keeps the last value, no message. Polling continues at the same rate (`watch.py:773`). | Read the rate limit. Back off until reset. Show it | Fake `gh`: "API rate limit exceeded", exit 1 |
| G3 | `gh` auth expired | Same None, silently stale for ever (`pr.py:204-213`) | Tell auth from outage. One item | Fake `gh`: exit 4 |
| G4 | Pushes failing | Workers retry on their own, one started a retry loop. HQ filed an item by hand | One shared incident. Workers wait on it | Fake remote returning 500 |
| G5 | Actions not running (billing, runners) | Checks stay pending or fail with 0 steps. Workers on `gh pr checks --watch` wait. HQ diagnosed by hand | Detect jobs that never started, across repos, and raise one infra incident. Bound the wait | Fake checks with `startup_failure`, 0 steps |
| G6 | Slack outage or expired token | Network errors back off quietly, 429 honours `Retry-After`, token errors told once (`slack.py`). Good. Slack tools in HQ have hung | Keep this. Add deadlines to HQ's reads. Show source state | Local stub returning 429, 500, `token_revoked` |
| G7 | Chrome extension disconnected | Each worker reports once and stops that step. Six reports. No shared state | A capability state workers check before starting | Mark it unavailable; assert one item |
| G8 | Linear down | HQ's tool call fails, spawn doesn't need it | Spawn must never need it. Queue the ticket | Tool returning errors |

## Mac

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| M1 | Sleep and wake | Doctor gives the watch 5 min after a wake (`doctor.py:285`). Fixed after real pain. Caffeinate runs only on AC with a worker `working` (`doctor.py:493`) | Keep this. A wake is an event, and sources refresh in order | Fake `kern.waketime` and a stale `ts` |
| M2 ★15 | Crash, panic or power cut | No `fsync` on the ledger, status or `hq.json`, so the last writes can be lost. On login, jobs restart, herdr resumes all agents at once, and doctor tells mid-turn ones to carry on. Docker stays down after reboot. Load can stay high long after a reboot. After one crash, restart was blocked (fixed since) | `fsync` what matters. Reconcile before acting. Resume in stages, under the machine budget. Check dependencies and say what's missing | Kill the private server and truncate state; assert ordered, staged resume |
| M3 ★3 | Memory pressure | Flagged under 10%, nothing else (`doctor.py:434`). Spawn doesn't check. A leftover `heavy` tool has a floor, and athena doesn't use it | Budget in the kernel: no launches over it, heavy jobs queue, name the biggest worker. | Stub memory reader at 5% |
| M4 ★2 | CPU starvation | Hook's `ps` and herdr calls time out. `agent_owned` reads gone, `nested` reads false, and hook events drop at 10 s. Doctor may then try to resume a live HQ. Can kill the Mac | Unknown is not gone. Back off repairs when load is high. Watch load, and hold launches | Fake herdr answering late; test under `stress`-style load on a private server |
| M5 ★5 | Disk full | `hq.json` truncates to empty (S4). Hook writes fail inside `except: pass`, so status stops and the gate gives way, since its decision comes after the write (`hookstatus.py:239-240`). `doctor._save` raises in `finish`, which isn't guarded (`doctor.py:543`): no `health.json`, so the dash goes unknown and clamshell treats it as stale. Doctor notifies once. | Reserve space or keep a small emergency file. Writes fail safe. Stop launches under a floor. Clean named caches | Small disk image as the state dir, then fill it |
| M6 | macOS prompts and TCC | Launchd jobs and subagents can raise privacy prompts, seen with the wrong app name. Nothing notices a job waiting on one | List expected prompts in the installer, and test in an isolated HOME | A fresh HOME; record prompts by hand |
| M7 | Every process signalled | `kill(-1)` from a script. All apps, herdr and HQ restarted, and the pane was lost | No shell power in HQ. The test harness refuses pid <= 1. Recovery as for M2 | Assert the harness refuses -1 and 0. Never run it for real |
| M8 | Lid closed | A root daemon holds `disablesleep` only on good AC with HQ alive and a fresh heartbeat. Lid closed on battery leads to `sleepnow` (`core.swift:286`). HQ dying clears the flag but doesn't sleep the Mac | Keep the module's rules. Test the unplug case | The daemon's own test driver, with fake power and lid |

## Two things failing together

| Case | Trigger | Today | Should | Inject |
|---|---|---|---|---|
| X1 | Wake plus slow GitHub | Watch grace ends at 5 min, and a slow tick passes it. HQ restarts just after a wake | Liveness per loop (G1). Wake refreshes without restarts | Fake waketime with slow `gh` |
| X2 | Server restart plus watch restart | All workers resume while no watch runs. `prev` is None, so reports from the gap are lost (W4) | Persisted snapshot. Reconcile after resume | Restart both; have a worker report in the gap |
| X3 | Disk full plus crash | `hq.json` empty, last ledger line cut, health stale. Doctor green on HQ, and S2 can take readers down | Generations, tolerant fold, startup report on damage | Fill the disk, kill, reboot-style restart |
| X4 | Usage limit plus rotation | Doctor asks HQ to rotate, and a fresh HQ starts with no usage. The old conversation is forgotten first | No rotation while limited. Commit after confirmed start | Limit stub with context at 90% |
| X5 | Account switch plus rotation | A fresh HQ on another account acts as another person on any connector not yet local | Identity check in the start routine, before writes | Swap credentials, then rotate |
| X6 | HQ blocked plus worker crash | HQ at a question reads ok (W11). The worker reads working (K1). Nobody knows for hours | The kernel raises both, without HQ | Blocked HQ; SIGKILL a worker |
| X7 | Claude update plus server restart | Every session resumes on the new build. New prompt wording stops them all, and exits refuse (C4) | Update only with a smoke pass. Stage the resume | Fake new wording, then restart |
| X8 | Memory pressure plus resume all | Eight Claudes (about 3 GB) restart at once on a struggling Mac, and repeat | Stage by budget. Hold heavy work | Stub memory at 15%, restart with 8 test sessions |
| X9 | Quota out plus doctor restart | Quota stalls data, restart adds `status --pr` calls, and burns more | Budget covers routine calls. No restart for quota | Quota stub through a restart |
| X10 | herdr slow plus concurrent doctor and `athena hq` | `hq()` holds no lock, so both can split a pane or replace `hq.json` | One lock on HQ's record. Unknown never replaces | Two callers with a slow fake herdr |
| X11 | State move plus live workers | Workers keep the old path, doctor reads the new one: two truths (S9) | Park everything first. Verify none holds the old path | Rehearse on a copy, with a worker left running |
| X12 | Slack off plus HQ busy | Nothing rings and nothing says so (W10). Human believes DMs are watched. Missed DMs were reported | Source state visible on the dash | Fake `security` failing during a long turn |
