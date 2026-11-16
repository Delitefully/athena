# The watch

`athena watch` is the long-running loop that turns state changes into lines and wakes HQ for the ones that matter
(`athena/caps/watch.py`; HQ's side is the line table in `plugin/skills/athena/SKILL.md` §3). This page covers four things:
acting when the human closes a worker's window in Rex, the AI reviewers' threads on followed PRs, the CI doorbell on main, and how
the Slack doorbell reads a conversation.

## A worker's window closed in Rex

### What happens

Each worker lives in a window named for it in its repo's Rex session. When the human closes that window (or its only pane, which
closes the window with it), the watch acts within a few seconds, instead of HQ finding out later from a `<w> state ...->exited` or
`<w> gone from the ledger` line:

1. **It retires the worker the safe way**, through the ordinary `athena retire <w>`, never `--force`. Its head is backed up under
   `refs/athena-backup/<w>/`, its branch is kept, and a clean worktree is removed.
2. **Unsaved work is never dropped.** If the worktree holds uncommitted, unpushed or git-ignored work, retire refuses, as it always
   does, after it has backed up the head. Nothing is deleted: the worktree stays, the worker stays in the ledger marked
   `closed_by_human: unsaved work` (`athena status` shows `closed-by-human (unsaved work)` as its detail), and a later
   `athena retire <w>` works on it as usual once the work is saved.
3. **Its items turn to the human.** Each open `request` or `promise` item whose only owner is that worker gets the timeline note
   `worker window closed in Rex` and `next: you`. Nothing is resolved. Decision items, items shared with other workers, and anything
   linked to a PR stay as they are: PR rows are derived anyway.
4. **HQ gets one line**, as a wake:
   - `<w> closed in Rex · retired`
   - `<w> closed in Rex · unsaved work kept`
   - `<w> closed in Rex · retire failed`: retire refused for another reason; or the worker's Claude was still running a minute after
     its window closed, or Rex or `ps` could not say for a minute whether its block and Claude were gone. Then nothing is retired:
     a worker whose Claude may be alive is never stopped. Look with `athena status`.

   That worker's `->exited` and `gone from the ledger` lines that follow are not sent; they go to `logs/watch-suppressed.log`.

### What does nothing

- **Closing a whole session.** Rex says only that its blocks closed and the session went, not that a window closed. As a second
  guard, a window close whose repo session is gone by the time the watch looks, while athena still lists that session in
  `run/repo-sessions.json`, is taken as part of a session kill (a session athena closes itself, once its last window went, it
  forgets first). Its workers are left as they are, and the usual `->exited` lines follow.
- **HQ's own window**, and **any window that is not a live worker's** (a pane the human opened, an old worker's).
- **A worker's pane moved to another window**: its old window closes, but its block, and its Claude, live on.
- **Athena's own closes**: retire closing a window, a failed launch cleaning up. The worker is retiring or failed by then, or was
  while the window closed: a retire that closed the block and then failed takes its mark off, and the ledger still says when.

### How it hears

The watch follows Rex's event stream (`rex events --all --json`, never starting a server) on a thread of its own, and acts on
`window_closed` events only. A close is matched to a worker through the ledger: the live worker whose recorded session and window are
the closed one's, and whose block is gone. The loop wakes from its nap as soon as a close arrives, waits a second so a session kill
can show itself, waits for the worker's Claude to exit, and then retires it. While a close waits, that worker's snapshot stays as it
was, so if the close is dropped after all (a session kill) its exit is still told as `->exited`. Only Rex saying the block is not there
counts as gone; an error that says nothing holds the close, and a minute of them gives up with the line above.

Each close is kept in `run/closes.json` until its line is out: which worker it is, when its retire started, and what came of it. What it
decided (retire, or give up) is saved with the start. A watch killed before a retire finishes runs it again on restart (retire is made
to be run again), and only a retire: a give-up is never turned into one. One killed after tells the line then. The worker's ledger mark, `closed_by_human` (`retired`, `unsaved work` or `retire failed`), is written only once the outcome is
known, and its items turn then. What retire prints goes to the suppressed log, never onto the watch's own output. If the stream
drops (a Rex restart), the watch reconnects with a growing pause; a close missed while nobody listened is told the old way, as
`->exited`. Stopping the watch ends the stream's `rex events` process too.

## AI reviewers' threads on followed PRs

By default, every review thread an AI reviewer (a bot) starts on a PR athena follows is replied to and
resolved, by the owning worker or by HQ, without asking. A bot's inline thread changes neither `reviewDecision` nor the PR's comment
count, so the watch reads the threads themselves (`athena/caps/follow.py`, `bot_threads`).

Every two minutes, on the follow fetch's own thread, each open PR athena follows has its unresolved review threads read (GraphQL
`reviewThreads`, the query `athena threads` runs): a live worker's PR, a stacked PR, and a retired worker's PR while it is open
(for two weeks). A thread counts when its first comment's author is a bot: GitHub's `Bot` type, a `[bot]` login, the logins in `BOTS`
(`prs.py`) or config `rex.bots`. A person's thread never counts. The ids of each PR's open bot threads are kept in `run/follow.json`
(`bot_threads`), so a thread rings once; a PR no longer followed is forgotten, and a read GitHub fails keeps what was known.

Lines, as wakes:

- `<w> bot threads +N #<n>`: N new unresolved bot threads on live worker `<w>`'s PR `#<n>`.
- `<owner/repo>#<n> bot threads +N`: the same on a followed PR no live worker owns (stacked, or a retired worker's).
- The same line once more, N then the count still open, when a bot thread is still unresolved 30 minutes (`BOT_REMIND`) after HQ's
  review passed at the PR's head (`athena review <w> pass`).

Each line carries the new thread ids as its key, so two new threads within ten minutes both ring. A bot's top-level comment or review
body alone is still not sent: the quiet line `<w> bot comments #<n>` goes to `logs/watch-suppressed.log` with its reason.

## The CI doorbell on main: only our own commits

`<repo> main ci failed <workflow> <run-id>` covers the repos with a live worker plus config `ci_repos`
(`athena/caps/follow.py`), and it counts a failed run on a repo's default branch only when the run is ours (`is_ours`). That means
the run's head commit was authored or committed by the human's GitHub account (`gh api user`), or it is the merge of a PR that
account opened or that came from an athena worker's branch (any branch in the ledger or in retired workers' history). A scheduled
run is never ours, and a dispatched run is ours only when that account dispatched it. Everything else, other people's merges and
pushes included, opens no incident in `ci-failed.json`, does not turn the lights red (they read those incidents), and goes to
`logs/watch-suppressed.log` with the reason `not our commit`. An incident already open for a run that is not ours is closed on the
next fetch, without a `recovered` line. Each commit is asked about once, and the answer is kept for that sha. When GitHub can't
say, nothing is recorded and the next fetch asks again. There is no setting to watch every failure on main again: that was the old
default, removed as a bad one.

## How the Slack doorbell reads a conversation

Each pass reads up to 40 conversations in turn (`athena/caps/slack.py`). One conversation is read newest first, 100 messages a page,
back to its cursor (`seen` in `slack-rex.json`): one call when fewer than 100 messages came since, at most 20 pages. It never passes
Slack's `oldest`: given without `latest`, Slack answers with the oldest page after it, so a conversation with more than 100 messages
in seven days never showed its newest ones and its cursor stuck. The first page, at least the newest 100
messages, is where threads' parents within seven days are found. A conversation more than a page behind its cursor catches up in one
line, `slack-new <id> <n>`, `n` its real count with its threads' new replies in it, rather than a line per thread.

What the human sent through an app is not the human: a message from their account that carries an `app_id`, as HQ's sends through
the Claude connector do (a typed one never has one), does not mark the DM active, so a reply to it rings at once.

What the human already read in Slack does not ring: a conversation with something new costs one `conversations.info` call for its
`last_read`, and a thread's replies are checked against its parent's `last_read` in the `conversations.replies` already read (else in
`conversations.history`); a message or reply at or before it is not counted. Without a cursor (a failed call, or no field) it rings
as before, and says why once in `logs/watch-suppressed.log`; a cursor at or past an app post of the human's in the same read (HQ's
send may have moved it) is not trusted either, and rings as before. A message or reply whose text addresses Claude or athena (the word
`claude` or `athena`, any case, or a mention of the Claude app's user, config `slack_claude_user`) rings whether the human read it or not, as HQ must
answer it; the text is only matched in memory, and nothing of it is kept or logged.
