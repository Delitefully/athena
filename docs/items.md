# Items

An **item** is anything you asked athena for or need to know about: a question, research, a plan, a build, a chore,
an incident, or a promise athena made. Every request you make becomes one, so nothing lives only in HQ's chat. Items
are today's asks grown up and stored the same way, in `needs-you.json` in the state dir; there is no other store.

## What an item holds

| Field | What it is |
|---|---|
| `name` | 2 to 4 words you would recognise: "⌘K palette", "Disk cleanup", "System One on clouds". Every command, the handoff and the dashboard lead with it, never with a bare id |
| `kind` | `decision` (your call), `request` (something you asked for), `result` (an answer or report for you to read), `promise` (something athena said it would do, often with a due date) or `incident` (something broke) |
| `next` | whose move it is: `you`, `athena`, `worker`, `reviewer`, `customer`, `colleague` or `ci`. A decision or a result is yours by default; anything else athena's |
| `text`, `why` | the title, one sentence of at most 120 characters: the call for a decision, where it stands for anything else; and one sentence under it |
| `body` | Markdown, up to 8000 characters: the decision brief (recommendation, options and what each costs, evidence), or the report or plan in full |
| `options`, `rec` | up to 2 options (4 with a body), and the recommended one: **Take rec** on the dashboard |
| `auto`, `stakes` | the recommended option applies on its own (below); `stakes` marks a call as `security`, `customer`, `money` or `one-way`, which never does |
| `due` | a date it is due, if it has one; an item due within a day sorts to the top of Needs you |
| `words` | your own words, quoted in its full view |
| `requests`, `workers`, `pr`, `links` | the request-log ids it files, the workers on it, its PR, and any reports, pages or screenshots |
| `created`, `updated`, `log` | its age, its last change, and its timeline |
| `resolved`, `resolved_note`, `snooze`, `applied` | how it ended; when a snooze ends; a default athena applied on its own |

Stages such as building, in review or landing are not stored: they are read from what athena already tracks. An item
with a live worker names that worker's row on the dashboard, and the row's label says the stage.

## How HQ uses them

HQ files each new request as an item before it starts working on it, and edits it as things move, so one CI glitch
is one item that changes, not three:

```sh
athena item add "Which clouds carry System One models?" --name "System One on clouds" --kind request \
  --words "is system one on vertex, foundry or bedrock?" --request r42
athena item edit a12 --title "None of Vertex, Foundry or Bedrock carry them" --kind result --body-file report.md
athena item done a12 --resolution "you replied in chat"
athena item snooze a7 --until 2026-10-29
```

- `athena item` lists the open items, name first; `athena item show <id>` one, in JSON. `athena ask` is the same command.
- `add` takes `--name`; HQ always gives one. Without it, a name is derived from the title, marked "name derived:
  rename it", and a hint says how. `edit` changes any field in place: the item keeps its id, its age and its place,
  and its timeline says what changed.
- `done` resolves an item, with a note on how. An item resolves **when you took the last step**: you clicked Merge,
  replied about it in chat, posted the message, ran the login, merged the PR yourself. HQ notices these and resolves
  it with a note. Only unseen results wait for your **Done** on the dashboard; tiny things (an FYI, a chore with a
  one-line result) are filed with `--fyi`, already resolved, a line in Done today.
- `snooze` hides an item until a time; it comes back unread, and any edit to it brings it back sooner.
- A resolved item stays a week for Done today and search, then moves to `items-archive.jsonl`. Its full view still
  opens, and search still finds it.

## Defaults that apply on their own

A reversible, low-stakes decision can carry a default: `--rec <option> --auto`. If you do not object, the dashboard
takes the rec for you **12 hours** after the item was filed, or **2 hours** once you are away (no prompt typed into HQ
and no answer on the dashboard for 2 hours). The row says when: "athena takes the rec at 09:00 unless you object".

- Never for a call marked `security`, `customer`, `money` or `one-way`: `athena item` refuses `--auto` on one, marking
  the stakes later takes the default off, and the dashboard skips one even if the file says otherwise.
  Stakes it cannot read (a string instead of a list, another case, an unknown value) block the default too: an
  unknown risk is never auto.
- Any answer of yours (an option, a reply, even Discuss) turns the default off for good, so a call you are still
  discussing never applies a day later.
- Applying it queues the recommended option for HQ, marked `auto`, exactly as your own answer would be. HQ acts on it,
  and the item moves to **Ready for you**, saying what athena took, with **Done** and **Undo**. Undo queues
  `undo-default` for HQ, which reverses it; the decision is then yours again, its default off.

## Don't ask me this again

An item's full view has a "Don't ask me this again" box beside its answers. Ticked, the answer you give carries
`rule: true` in `athena ask queue`, and HQ proposes a standing rule from it, in your words and scoped to what you meant.

## The request log and the weekly check

Every prompt you type into HQ is logged by a hook, outside the model, in `requests.jsonl` (private, mode 0600), each
with an id such as `r42`. Prompts athena submits itself (the watch mod's wakes, the start, restart and rotation
routines, cron prompts, slash commands, messages from other sessions) are left out, and so is anything that starts
with `<` (how Claude Code wraps those): a request pasted as HTML is not logged, and the weekly check cannot see it.

- `athena item requests [--days N]` lists the logged requests and the item that holds each.
- `athena item check [--days 7]` lists the requests with no item: what would have fallen through. HQ runs it weekly
  and files each one, or marks it chat with `athena item ignore <id>`.

## Migrating today's asks

The first `athena item` after an upgrade brings every ask in `needs-you.json` up to an item in place, losing nothing:
a question becomes a decision, a note a result (or a decision when it offers options), each gets a name derived from
its title, marked "name derived: rename it" in `athena item` until HQ names it, and the file from before is kept as
`needs-you.json.v1`. `athena item migrate --dry-run` counts what it would change.
