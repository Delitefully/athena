# What athena never asks, and what it always asks

HQ reads this file at session start (skill §1), then `never-ask.md` in the state dir if there is one: the human's
own additions and the standing rules they agreed to (§"Answered twice" below). Where the two disagree, the state
dir's file wins. Each "never ask" entry is a step athena takes itself and reports in one line afterwards, at most.

## Never ask: athena does these itself

- **Rebases and restacks** of a PR nobody has approved yet: `git rebase origin/<default>`, then `git push
  --force-with-lease` on the worker's own branch. An approved PR in a repo listed in `reapproval_repos` is not this:
  ask first.
- **CI reruns and flake triage**: `gh run rerun <id> --failed` once for a flake; a real failure is routed like any
  reported issue.
- **Undrafting after HQ's review passed** (`gh pr ready`), where the human has said ready PRs go out on their own.
- **Removing a label the human said athena may always remove** (a suspended-environment label, say). The label goes
  in this file's state-dir copy, by name.
- **Cloud sign-ins that the human approves in the browser** anyway (`aws sso login --profile <p>`): run it in the
  foreground and let the browser ask. Signing in authorizes nothing new.
- **Cleaning up its own things**: containers it started (`docker rm -v`), worktrees of retired workers, its own
  temp files.
- **Retrying after a limit** (rate limit, usage limit, a timeout) once the limit has passed.
- **Restarting its own processes**: herdr's server, the dashboard, the board, the watch, HQ on its own conversation
  (`athena doctor` does these every minute).
- **"Should I keep going?"** Yes, within the brief. A worker that asks it gets "carry on" from HQ, not from the human.

## Always ask: these go to the human, with a recommendation

- Security, auth, billing, data migrations, deleting data, writes to production.
- Posting outside the repo (Slack, email, a ticket in someone else's project) without a standing go for that kind
  of post. A standing go is written in the state dir's `never-ask.md` or `standing-orders.md`, in the human's words.
- Merges in the repos listed in `human_merge_repos`, and every merge without the human's go for that PR.
- Anything that changes the direction the human set: scope, a PR split, a feature flag, a new ticket they did not
  ask for.
- Anything in a worker's FORBIDDEN list.

## Questions a command can answer never reach the human

Before an ask goes on the dashboard, check whether a command answers it: `gh pr view`, `gh run view`, `git log`,
`athena status <w>`, a file in the repo, the brief. If one does, the question goes back to the worker that asked it,
with the command: "run `gh pr checks 41` and go on from what it says". The human hears only what is left once the
command has answered.

## Answered twice: offer a rule

When the human gives the same answer to the same kind of question twice (two asks, or an ask and a chat reply), offer
to make it a standing rule: one line, in their words, scoped to what they meant ("rerun a failed CodeQL job once
without asking"), and nothing broader. Only after they say yes, add it to `never-ask.md` in the state dir under
`## Standing rules`, with the date. From then on it is a never-ask entry.

## Quiet when nothing is new

A wake (`athena watch`, a doorbell, a cron) that changes nothing for the human gets no reply at all: end the turn
with no text. Never repeat a pending ask in chat: the dashboard holds it.

## Backed by permission settings

A never-ask entry only holds if Claude Code's permission settings let the command through without a prompt.
Narrow `permissions.allow` rules (`Bash(gh run rerun *)`, `Bash(gh pr ready *)` in HQ's settings only) run before
auto mode's classifier; auto mode's `autoMode.environment` must name the repos and orgs athena works in, or every
write there counts as external. These live in the human's own settings: athena proposes a diff and never edits them.
