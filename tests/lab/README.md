# Integration lab

`lab.sh` runs cos against an isolated herdr server (its own config and state under `/private/tmp/claude-501/cl`, session `cos-lab`) with a sandbox repo at `~/Developer/.cos-lab-sandbox` and real Claude workers. It never talks to the live herdr session.

```sh
tests/lab/lab.sh up && tests/lab/lab.sh trust
eval "$(tests/lab/lab.sh env)"; export COS_HQ=<your session name>   # workers report to you
bin/cos spawn --repo ~/Developer/.cos-lab-sandbox --branch gabriel/lab-1-x --name lab-1 \
  --brief <brief.md> --model haiku --effort low --goal "LAB-1 is done when ..."
bin/cos status; bin/cos board show
tests/lab/lab.sh down
```

## Result on 2026-10-07 (herdr 0.9.1, Claude Code 2.1.293)

- Two Haiku workers launched by `cos spawn` ran their `/goal` to "Goal achieved", committed and pushed, and reported by SendMessage to the session named in `COS_HQ`.
- Plugin hooks (loaded with `COS_PLUGIN_DIR`) wrote `done` with the parsed `COS-REPORT`; herdr still showed one of them as `working`.
- keepwarm stayed off in workers (no `kw` in their status line), so `--settings` `enabledPlugins` overrides user settings.
- `cos board sync` built HQ 70 + two 155-column attach columns at 380 columns; after `cos retire`, it rebuilt to one column.
- Limit: without an attached client, herdr does not resize pane terminals to their split size (an HQ pane laid out at 70 columns reported `stty size` 50 379), so a headless lab cannot show workers resizing to their column. The research lab, with a client attached, did.
