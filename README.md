<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-lockup-dark.svg">
  <img src="assets/logo-lockup.svg" alt="athena" width="360">
</picture>

A chief of staff for Claude Code in [herdr](https://herdr.dev). You hand athena Linear tickets. For each one it
launches a visible worker Claude in its own git worktree and herdr space, shows the workers as columns beside
itself, and supervises them to merge-ready PRs. It asks you only when a decision is yours, and you can type
into any worker at any time.

![athena's red thread runs through three workers, brings you a question, ties off at the merge](assets/one-thread.svg)

## Requirements

- macOS or Linux, with Python 3.9 or later as `/usr/bin/python3`.
- [herdr](https://herdr.dev) 0.9.1 or later (`athena init` offers to install it).
- [Claude Code](https://claude.com/claude-code) as `claude`, 2.1.286 or later for the watch mod.
- [gh](https://cli.github.com), signed in (`gh auth login`), and a git identity: workers push branches and open PRs.
- Go, only for the dashboard (built on first use).

## Install

```sh
git clone https://github.com/Delitefully/athena.git
mkdir -p ~/.local/bin && ln -sf "$PWD/athena/bin/athena" ~/.local/bin/athena   # ~/.local/bin on your PATH
claude plugin marketplace add "$PWD/athena"
claude plugin install athena@athena
```

## Quickstart

```sh
athena init   # checks what athena needs, asks for your folders, merge rules and model
athena        # starts herdr if needed, opens the athena space, attaches
```

Then tell athena what to start: "start abc-101 and abc-102". Detach with `ctrl+b q`; everything keeps running,
and `athena` brings you back.

To keep athena running on its own, install it as login services:

```sh
athena service install --dry-run   # prints every file it would write and every command it would run
athena service install             # herdr, the dashboard, the board, and `athena doctor` every minute
```

herdr's server, the dashboard and the board then restart after a crash and come back after a reboot, and quitting
the terminal only detaches. `athena doctor` checks them every minute, resumes athena's own conversation if its
Claude died (never a fresh one), and shows what it could not fix on the dashboard's health dot. launchd on macOS,
systemd user units on Linux; `athena service uninstall` removes them.

## Using it day to day

- **Start work:** tell athena the tickets. It reads Linear, writes a brief, shows a launch card and starts a worker.
- **Answer questions:** when a worker needs a decision, athena asks you in its own pane and passes your answer on.
- **Step in:** each worker is a full Claude session; type into its column whenever you like.
- **Review and merge:** athena checks CI and has a fresh reviewer read each PR. Then you merge, or tell athena to.
- **`prefix+shift+c`** (or `athena hq`): open or focus athena's space.
- **`prefix+shift+b`** (or `athena board toggle`): show or hide the worker columns. To select text in a column, hold Shift while you drag (the column's `herdr terminal attach` takes the mouse; Ghostty and most terminals let Shift through to their own selection).
- **`prefix+shift+h`** (or `athena hq restart`): update Claude Code and the plugins (at most every 6 hours; `--update` forces it), then restart athena in place.
- **`athena hq rotate`**: replace a long HQ conversation with a fresh one that starts from a handoff written from athena's state, instead of compacting it again.
- **Every request is an item:** what you ask HQ for becomes a named item on the dashboard, so nothing is kept only in chat. `athena item` lists them; [Items](docs/items.md) says how they work.
- **`athena dash open`:** the dashboard in your browser: what needs you, what is ready for you to read, what is in flight, and what was done today, with a full view per item.
- **`athena doctor`:** one line per part athena needs and whether it runs; `--fix` repairs what it safely can.
- **Fewer questions:** athena does the routine steps in [`never-ask.md`](skills/athena/never-ask.md) itself (rebases, CI reruns, sign-ins you approve in the browser, restarting its own processes) and always asks about the rest. Add your own lines to `never-ask.md` in the state dir; when you give the same answer twice, athena offers to make it a rule there.
- **Remote Control:** athena's own session always connects to Remote Control, so you can reach it from your phone or claude.ai/code; workers never do, whatever `remoteControlAtStartup` says in `~/.claude/settings.json`. A worker that was already running keeps its connection until it next starts.

## Learn more

- [How it works](docs/how-it-works.md): the supervision loop, the watch mod, what it may say and what it keeps quiet (`logs/watch-suppressed.log`).
- [Commands](docs/commands.md): every `athena` command and flag.
- [Configuration](docs/configuration.md): `athena init`, `config.json`, environment variables, state files.
- [Items](docs/items.md): what an item is, how HQ files, names, resolves and snoozes them, and defaults that apply on their own.
- [Dashboard](docs/dashboard.md): what each row means, and how the page is built.
- [Troubleshooting](docs/troubleshooting.md): common errors and their fixes.
- [Development](docs/development.md): the code layout, tests, the lab, and updating after an edit.
- [Decisions](docs/decisions.md): the choices athena was built on.
