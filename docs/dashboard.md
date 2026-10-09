# Dashboard

`athena dash` serves one page on `http://127.0.0.1:2843/` while athena's HQ is open: what needs you, what is in progress, and what is done, each with its ticket and PR. It reads athena's state files and nothing else, and it updates by itself when they change.

## Plan

The plan below is after a /refute pass; the objections it took and rejected are in the last section.

### Language: Go

Go, standard library only: `net/http`, `html/template`, `embed`. One static binary with the page, the CSS and the fonts inside it, no dependencies to vendor, and `go test` for the tests. Rust would need crates for HTTP and templating; Go needs none.

The binary is built on first use: `athena dash on` runs `go build` into `~/.local/state/athena/bin/athena-dash-<hash>`, where the hash covers the sources in `dash/`, so an edit rebuilds and an unchanged tree starts at once. The repository keeps no binary.

### Files read

All under `ATHENA_STATE_DIR` (`~/.local/state/athena/`):

| File | Writer | Gives |
|---|---|---|
| `ledger.jsonl` | athena commands | every worker: name, repo, branch, ticket, title, spawned, retired, `goal_pending` |
| `workers/<name>.json` | the worker's hooks | state (working, idle, blocked, done, exited), summary, `report` and `last_report` (status, pr, head, concerns) |
| `watch.json` (new) | `athena watch` | the last snapshot: each live worker's merged state and PR (url, number, state, draft, checks, review) |
| `history.jsonl` (new) | `athena retire` | one line per retired worker with its PR as last known, so a merge is still known after retire |
| `needs-you.md` (new) | athena HQ | decisions that belong to no worker: merge asks, open questions |

The ledger and the worker files alone give all three groups: the ledger keeps retired workers with their ticket, title and retire time, and a worker's file outlives it with its last report, which carries the PR url. The new files only enrich: `watch.json` adds checks, review and draft for live PRs, and `history.jsonl` adds whether a retired worker's PR merged. Today's state has none of them, and the page still works on it.

`board.json`, `hq.json`, `standing-orders.md` and `notes.md` are not read: the page has no use for layout, and notes are athena's own.

### Python additions

- `watch.py`: after each tick, write the snapshot to `watch.json` (temp file, then rename), with a `ts`. It holds what `athena watch` already computes; nothing new is fetched.
- `retire.py`: before the `retire` ledger event, append to `history.jsonl` the worker's ticket, title, repo, branch, report status and PR. The PR comes from one `gh pr view` (`pr.summary`), else from `watch.json`, else from the report's `pr` url. HQ retires right after it merges, while `athena watch` polls every 20 s, so the snapshot alone would usually still say open.
- `dash.py` and `athena dash on|off|show|open`, below.
- `skills/athena/SKILL.md` section 6: HQ keeps `needs-you.md` current, one bullet per open ask, and removes a bullet once answered.

### Data model

The server folds the files into one view, every second when something changed:

- **Needs you**
  - a live worker whose hook state is `blocked` (a permission prompt, or a `BLOCKED` or `NEEDS_CONTEXT` report; the label says which, and the report's first concern is the detail);
  - a live worker with `goal_pending` (a startup prompt only the human may answer);
  - an open PR that is approved, or that is out of draft with its worker done and checks not failing (waiting for your review or merge);
  - each bullet of `needs-you.md`, in athena's voice.
- **In progress**: every other live worker, with its state, its last summary line, and its PR with checks and review.
- **Done**: retired workers from the last 14 days, newest first, at most 12, and live workers whose PR is merged. Each shows when, the ticket link, the PR link and whether it merged.

Links are built from ids: a ticket `ABC-101` links to `https://linear.app/<workspace>/issue/ABC-101` (`ATHENA_LINEAR_WORKSPACE`, default `sunsecurity`); a PR links to the url already in state.

### Update mechanism

- The server polls every second: it lists the state dir and `workers/` and compares each file's size and modification time with the last tick. Only a change triggers a read. A few `stat` calls a second keep idle CPU near zero, and polling needs no dependency and cannot miss an event the way a dropped fsnotify watch can.
- On a change it re-reads, re-renders the page's `<main>`, and if the HTML differs, pushes it to every open page over Server-Sent Events (`/events`). The page swaps `<main>` in place: a few lines of JS, no build step, no reload button.
- Every row has a stable id (its worker name or note). After a swap, only rows whose id was not there before enter, by a moving edge (a clip from left to right, `cubic-bezier(.16,1,.3,1)`, staggered 70 ms), and only when the reader allows motion. Times show as "4 min ago" and a small timer refreshes them every 30 s, without a server round trip.

### Lifecycle

- `athena dash on`: if the pid in `dash.pid` is alive and is our binary, print its URL and pid and stop (idempotent). Otherwise build if needed, start the binary in its own session with output to `logs/dash.log`, write `dash.pid`, and wait up to 5 s for `/healthz` to answer with that pid. A server for the same state dir that already answers on the port (a concurrent `on` won the race) is adopted rather than started twice.
- `athena dash show` prints the URL and pid, or `dash off`. `athena dash off` sends SIGTERM and waits for it to exit. `athena dash open` runs `on`, then opens the URL in the browser.
- HQ owns it. `athena dash on` finds the HQ Claude, the process whose command is `claude --name athena` (`ATHENA_HQ`) with `hq.md`, never just any `claude`, since a worker's own Claude is one too. It passes the pid of that Claude's parent, the HQ pane's shell, as `--hq-pid` (the Claude itself when its parent is herdr or init). The server checks that pid every 2 s and exits when it is gone. So closing the HQ space stops the dashboard, while a Claude that exits and restarts in the same space (`athena hq` restarts it there) keeps it running: it survives a restart of HQ only while HQ's space is open. `athena hq` runs `dash on` where it starts the board watcher, so a new HQ space starts a new dashboard. With no HQ running, `dash on` runs until `athena dash off`, and says so.
- The port is `2843` (`ATHENA_DASH_PORT` overrides). It binds `127.0.0.1` only, and answers only requests whose `Host` is `127.0.0.1:<port>` or `localhost:<port>`, so a web page cannot read it through DNS rebinding.

### Failure modes

| Failure | Behaviour |
|---|---|
| A state file is missing | That source is empty. A missing state dir shows an empty page with a quiet note. |
| A JSON file is half written or invalid | The server keeps that file's last good parse and shows a quiet note naming the file. Writers already replace files atomically, so this should be rare. |
| A ledger line is invalid (a partial last line) | That line is skipped, as `ledger.py` does. |
| The port is taken | The server exits with "port 2843 is in use"; `athena dash on` prints that and the `ATHENA_DASH_PORT` hint, and removes the pid file. |
| `go` is missing | `athena dash on` looks on `PATH`, then in `/opt/homebrew/bin` and `/usr/local/go/bin` (herdr's key bindings may run with a short `PATH`), then says so. `athena hq` carries on without the dashboard. |
| `watch.json` is stale (HQ not running `athena watch`) | PR checks and review show with "as of 12 min ago" once older than 2 minutes; the PR link still comes from the report. |
| HQ closes | The server exits within 2 s. Open pages show "dashboard stopped" and reconnect when it is back. |
| The server crashes | `athena dash show` reports it off (the pid is gone or is not ours); `athena hq` or `athena dash on` starts it again. |
| A panic or template error while rendering | The poll recovers and keeps the last good page on open tabs; the server keeps running. |

### Design

The page follows `docs/brand.md`:

- Linen ground and indigo ink in light; indigo night and lit linen in dark, from `prefers-color-scheme`. Muted labels in the muted pair.
- Only athena is red: the knot in the header, and the `needs-you.md` lines, which are athena speaking. Workers, states and PRs are ink. A state that needs you is a solid ink badge; routine states are muted outline marks, the same split as `watch.svg`.
- Instrument Serif for the three group titles and their counts; Instrument Sans at 400 to 600 for everything else, with tabular figures. No monospace: a sha or branch is set in Sans.
- Fonts are the official webfonts (woff2) from Instrument's repositories, embedded in the binary, with their OFL licences in `dash/static/`. Nothing loads from another host.
- Three columns on a wide desk screen, one below the other on a narrow one. Rows are dense: ticket and title on one line, state, PR and time on the next, the summary muted and cut to one line.
- Motion: arrivals by a moving edge, no fades, glows, pulses or spinners. Reduced motion shows the page still. Brand rule 7 (nothing moves to make room) cannot fully hold for a live list: a new row pushes the rows below it down. The lists are short and change rarely, so this is accepted.
- `?scheme=light` or `?scheme=dark` pins the scheme, for screenshots from headless Chrome, which has no flag for `prefers-color-scheme`.

### Tests

One fixture state dir, `dash/testdata/state/`, with fictional tickets (`ABC-101`), a worker file cut off mid-write and one missing file. `go test` uses it, and the README screenshots and the live-update demo run against a copy of it. Tests never point `athena watch` or `athena retire` at the real state dir.

- Go: the ledger fold, worker classification into the three groups, invalid and missing files keeping the last good state, the rendered HTML (links, escaping), the Host check, and the SSE path (a file change reaches a connected client).
- Python: `watch.json` written by a watch tick, `history.jsonl` appended by retire with the PR from `watch.json`, and `athena dash on|show|off` against a real build on a free port (skipped when `go` is missing).
- `make test` runs both.

### /refute

Seventeen constraints were extracted and checked; the full table is in the PR.

- Changed the plan: **retire asks `gh` once.** The first draft filled `history.jsonl` from `watch.json` alone. `athena watch` polls every 20 s and HQ retires right after merging, so the snapshot would usually record a merged PR as open.
- Changed the plan (before refute, from review): the ledger and worker files alone must give all three groups; the HQ process is matched on `--name athena`, never any `claude`; `<main>` is swapped, not `<body>`, with stable row ids.
- Rejected: **a Python server instead of Go** would drop the build step, but the brief asks for Go or Rust. Left to the human.
- Rejected: **fsnotify instead of polling.** A poll costs 0.085 ms of CPU (measured on the real state dir), about 0.01% at 1 Hz, and polling cannot drop a watch.
- Kept, though nobody asked: **the Host check.** It is the only way a remote page could read a localhost server, and it is five lines.
- Narrowed: the Linear workspace default comes from the brief and `ATHENA_LINEAR_WORKSPACE` overrides it; the fonts come from Instrument's repositories named in `docs/brand.md`, since this repository holds none.

## Measured

On an M-series Mac, against the fixture state:

| What | Result |
|---|---|
| Server start to first answer | 7 to 10 ms (`athena dash on` with a cached build: about 0.1 s; the first build about 1.5 s) |
| Load and render the page body | 0.47 ms (`go test -bench Render`) |
| `GET /`, whole page | 0.40 ms |
| A worker file written to the page updated, no reload | 345 ms (the poll is 1 s) |
| Idle CPU, one page connected | 30 ms of CPU in 60 s, about 0.05%; 13 MB resident |
| Binary | 13 MB, fonts included |
