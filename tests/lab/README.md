# Integration lab

`lab.sh` runs athena against an isolated herdr server (its own config and state under `/private/tmp/claude-501/cl`, session `athena-lab`) with a sandbox repo at `~/Developer/.athena-lab-sandbox` and real Claude workers. It never talks to the live herdr session.

```sh
tests/lab/lab.sh up && tests/lab/lab.sh trust
eval "$(tests/lab/lab.sh env)"; export ATHENA_HQ=<your session name>   # workers report to you
bin/athena spawn --repo ~/Developer/.athena-lab-sandbox --branch gabriel/lab-1-x --name lab-1 \
  --brief <brief.md> --model haiku --effort low --goal "LAB-1 is done when ..."
bin/athena status; bin/athena board show
tests/lab/lab.sh down
```

## The watch mod

`tests/lab/fake-watch` stands in for `athena watch --tagged`: it follows `$FAKE_WATCH_LINES` and prints each raw watch line appended there through the real classifier (`exit` makes it exit, to try the mod's restart). Give a throwaway Claude session `--plugin-dir <this repo>` and a `--settings` file whose `env` sets `ATHENA_ROLE=hq`, its own `ATHENA_STATE_DIR` and `ATHENA_HQ`, `ATHENA_WATCH_CMD=<repo>/tests/lab/fake-watch` and `FAKE_WATCH_LINES`, and `enabledPlugins` `{"athena@athena": false}` so the installed copy stays out. Use worker names that do not exist, and an `--append-system-prompt` that keeps the session from acting on real workers. `<w> pr opened <url>` gives a worker an open PR, `<w> repo <owner/name>` (not printed) stands in for its origin remote, and the band carries `open dash →` while `athena dash on` runs for the lab's state dir (set `ATHENA_DASH_PORT` so it never meets the real one). Add `FORCE_HYPERLINK=1` to the settings `env` for masked links in herdr; run the session from a clean environment (`env -i`, as `lab.sh` does), since one started inside a worker inherits `ATHENA_WORKER` and the mod then draws no band.

## Result on 2026-10-07 (herdr 0.9.1, Claude Code 2.1.293)

- Two Haiku workers launched by `athena spawn` ran their `/goal` to "Goal achieved", committed and pushed, and reported by SendMessage to the session named in `ATHENA_HQ`.
- Plugin hooks (loaded with `ATHENA_PLUGIN_DIR`) wrote `done` with the parsed `ATHENA-REPORT`; herdr still showed one of them as `working`.
- keepwarm stayed off in workers (no `kw` in their status line), so `--settings` `enabledPlugins` overrides user settings.
- `athena board sync` built HQ 70 + two 155-column attach columns at 380 columns; after `athena retire`, it rebuilt to one column.
- Limit: without an attached client, herdr does not resize pane terminals to their split size (an HQ pane laid out at 70 columns reported `stty size` 50 379), so a headless lab cannot show workers resizing to their column. The research lab, with a client attached, did.
