# athena in Rex's command palette

Four athena actions in Rex's command palette, for things you would otherwise type into HQ or click on the dashboard.
Each one asks its questions in the palette (Rex's custom UI: `rex.ui.pick` and `rex.ui.input`), then hands the answer to
athena, which does the same thing its own command or the dashboard does.

| Action | In the palette | What it does |
|---|---|---|
| `athena_answer` | athena: Answer a Decision… | Pick a decision waiting for you, then one of its options, or type a reply. The answer joins the dashboard's answer queue, through the dashboard's own writer and checks, so HQ takes it up exactly as it does a click on the page. |
| `athena_message` | athena: Message a Worker… | Pick a live worker, then type a message. `athena send` delivers it: typed safely into a Claude worker, or posted to a Pi worker's inbox. |
| `athena_open` | athena: Open… | Pick a live worker, its pull request, or any open item. A worker's window comes to the front in the app you asked from. A pull request opens in your browser, and an item opens on its dashboard page. |
| `athena_retire` | athena: Retire a Worker… | Pick a live worker, then choose **Retire** (or **Keep**). `athena retire` saves its work, stops it, and removes its worktree and window. Unsaved work stops the retire, as always. |

Search the palette for "athena" to find them.

## Installing them

```
athena palette install     adds one line to Rex's config, once
rex config reload          Rex reads its config only when told
athena palette             says which file Rex's config is, whether it loads them, and the actions
```

`athena palette install` adds this line to Rex's config file: `REX_CONFIG` if it is set, else
`$XDG_CONFIG_HOME/rex/init.lua`, else `~/.config/rex/init.lua`, as Rex looks for it. The line is marked with a comment:

```lua
do local ok, err = pcall(function() dofile(".../athena/caps/palette/palette.lua"){ state_dir = "..." } end) if not ok then rex.log("warn", "athena palette: " .. tostring(err)) end end -- athena's palette actions (athena palette install)
```

- **Idempotent.** Running it again changes nothing. A marked line for another copy of athena or another state dir is
  replaced in place, and the rest of the file is never touched.
- **Always the current release.** The path goes through `~/.local/share/athena/current`, so `athena update` carries the
  actions with it.
- **A missing file only warns.** If the file is gone, the line writes a warning to Rex's log, and the rest of your config
  still loads.
- **One state dir, by name.** The line names the state dir athena was installed for. Every athena command the actions run
  uses it, whatever the Rex server's own environment says.
- **Your config is the real world's.** Without `--config`, the install writes your own Rex config only when the state dir's
  config opens athena's door to the real world (`rex.real_world`, as the live one does). `--config FILE` writes another
  file, such as a test's scratch config. A lab worker never writes one under your home.

To remove the actions, delete the marked line and run `rex config reload`.

## How each one behaves

- **Arguments skip questions.** Every question has an argument that answers it, so `rex do` can run the action with
  nobody there:

  ```
  rex do athena_answer item=a12 option=yes          rex do athena_answer item=a12 text="after the tests"
  rex do athena_message worker=rex-palette text="rebase on main, please"
  rex do athena_open target=worker:rex-palette      rex do athena_retire worker=rex-palette confirm=true
  ```

- **An unanswered question does nothing.** If a question is closed, times out, or can't be shown, the action stops before
  it acts, and says why: cancelled, nobody answered in time, no Rex app is connected to ask in, the Rex app can't show
  questions, or the app is showing another question. The message always ends "nothing was answered" (or sent, opened,
  retired). With `rex do`, it is the error, and the exit status is 1.
- **Nothing to choose is said, not shown.** With no decision waiting or no worker live, the action says so instead of
  showing an empty list.
- **What it offers is athena's.** Answer offers what the dashboard's Needs you offers as decisions. Permission prompts and
  the pull-request buttons (Re-check, Merge and the rest) stay on the dashboard. Open offers only what athena lists:
  live workers, the pull requests they reported, and open items. A list holds at most 2,000 entries, Rex's limit for one
  question; past that, the first 2,000 are shown (workers come before items).
- **A worker named in an argument must be live.** Otherwise the action says so before asking anything.
- **Retire asks twice.** Rex's `confirm` is only the label of a question's submit row, so the guard is a question of its own.
  The worker is retired only after **Retire** is chosen in that second question, or with `confirm=true` from `rex do`.
- **A slow send or retire finishes on its own.** Rex gives an action 30 seconds. A send or retire still running after 20
  seconds carries on in the background, and the action says so. It is never stopped halfway.
- **Questions wait 5 minutes** (Rex's default). The first Rex app to answer wins, on any of your devices.

## The athena side

`athena palette <verb>` is what the Lua asks. It prints JSON for the Lua, or exits 1 with `athena: <why>`. All the rules
are here, tested under `scripts/test.sh` (`tests/test_palette.py`):

| Verb | Prints |
|---|---|
| `decisions` | The decisions to pick from, and for each its options (recommended first and starred) and whether a reply is offered |
| `answer ITEM (--option ID \| --text TEXT)` | `{"said"}`, after the dashboard's own `take_action` queued it |
| `workers` | The live workers |
| `targets` | What Open offers: `worker:<name>`, `pr:<url>`, `item:<id>` |
| `open TARGET` | `{"said"}`, plus `focus` (the worker's Rex session and window) for the Lua to show |

`athena/caps/palette/palette.lua` asks the questions, and does nothing else. `scripts/smoke.sh` runs every action against
a private Rex server and its own scratch config. A stand-in Rex app, `scripts/rex-app.py`, answers each question as the
test tells it, or leaves it unanswered.
