# Flame: where athena's time and tokens go

A flamegraph for athena. The dash's Flame page shows where HQ, every worker, and their subagents and workflows spent time
and tokens, so a round of tuning starts from where the cost actually went.

## Use it

```
athena flame index      # read what is new in Claude's transcripts into the index (the first run reads them all)
athena flame status     # how big the index is, what it covers, when it last ran (--json)
```

Then open the dash and follow **Flame** in its header, or go to `/flame`. You needn't run the command first. When the
index is more than 5 minutes old, opening the page updates it in the background, one run at a time and niced. The page says
"Updating the index", then swaps the new data in, keeping your zoom. An update takes about a second when little is new;
a first index reads every transcript once, in about a minute. There is no launchd or doctor job for it.

On the page:

- **Click a frame to zoom** to it. The breadcrumb walks back up; Esc goes up one level. Zoom, metric, scope and tab are
  kept in the URL, so a link opens the same view.
- **Hover a frame** for its exact time, its tokens (input, output, cache read, cache write), est. $, model calls, its
  share of the root and of its parent, and model time against tool time. A worker's frame adds its ticket and wall span.
- **Metric:** Time, Tokens, Est. $, Output tokens. **Scope:** all athena, HQ, workers, one worker, one ticket, or
  everything including sessions that aren't athena's. **Dates:** today, 7 days, 30 days, all, or a from and to.
- **Top frames** ranks by tool, by Bash command, by subagent type and by skill. Hovering a row rings its frames in the
  chart.

## What a frame means

A Claude session is a loop of steps: a model call, then the action it chose. A frame is that action, and it carries the
call that chose it as well as the action's own run. So `Bash › gh › gh pr` holds the model calls that issued those
commands and the time they ran, and a polling loop shows what it really costs: each round trip reads the whole context
again.

- A call with several tool calls splits its time and tokens evenly between them. A call with none is `reply`.
- A shell loop is named by what it runs. `until gh pr checks 1; do sleep 30; done` is `Bash › loop › loop: gh pr`: the
  condition when it is a command, else the body's first command, so a polling loop shows what it polls.
- Parallel runs from one call share their overlap: on a stretch where k of them run, each gets 1/k.
- **Time is agent time, not wall time.** It sums parallel work (subagents, workflows, background runs), as CPU time adds up
  across threads. The page says so beside its legend and in every tooltip.
- A subagent's steps hang under `Agent › <type>`, and a workflow's under `Workflow › <name> › <phase>`. The parent's wait
  on them isn't counted again.
- Idle time isn't counted. A call runs from the record before it to its own last record, capped at 10 minutes. A tool
  run is capped at 20 minutes.
- Est. $ is at list API prices per model, with cache writes priced by their TTL. It is a measure of weight, not a bill.
- A line of a shape the reader doesn't know is skipped and counted, and a file that fails is undone and tried on the next
  run, so one bad line never stops an index. `--full` rebuilds in one transaction: the page shows the old index until the
  new one is whole.

## The index

`<state dir>/flame/index.sqlite`, standard library `sqlite3`. These tables:

- `files`: each transcript, with its byte offset and the state of whatever was still open when a run stopped.
- `sessions`: HQ, worker (with ticket and title) or other, from athena's own records.
- `frames`: a trie of frame names.
- `steps`: the spans, one row per model call share and per tool run, each with start, end, model and tokens.
- `turns`: the turns, with their times.
- `rollup`: steps summed per file, day, frame and skill. This is what the page reads.

What reaches the index, and nothing more:

- timestamps
- token counts
- model names
- tool names
- a command's first two words
- subagent types
- skill and MCP names

A name that doesn't look like one (only letters, digits and `._+:/@-`, at most 40) becomes `other`. No prompt, reply,
tool input or tool output is stored. A model, id or name that isn't a string is dropped where it is read, and any odd line or step is skipped and
counted, per transcript, as is a token count no real message has (at or past 2**53) and a line nested past what a JSON
parser will go. The page notes how many lines the index could not read and how many transcripts the last update could not
index, and `flame status` lists them.

Sessions are named, in this order:

1. Claude's `agent-name` record: a worker's name, or `athena` for HQ.
2. `workers/<name>.json` and `hq.json` session ids.
3. The session's folder against the ledger's spawn paths.

The code is `athena/caps/flame/`, in these files:

- `ingest.py`: the reader and the steps.
- `frames.py`: names and prices.
- `label.py`: who a session was.
- `query.py`: the tree and the tables.
- `page.py` and `static/flame.css`, `static/flame.js`: the page, its style and script, which the dash serves under `/static/`.
