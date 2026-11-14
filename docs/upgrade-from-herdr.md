# Upgrading from the herdr athena

For an agent (or a person) moving a Mac from the herdr athena to athena on Rex. Follow the steps in order. Each command
can be run twice: a second run says `=` for what is done and changes nothing. Every step stops with what to do when it
can't go on, and nothing is changed past the step that stopped.

`scripts/upgrade-from-herdr.py run` does steps 3 to 9 in one go (with the old HQ's workspace in step 6). The steps below run them one at a time, so you can read
each result before going on. Use either.

## What changes

| The herdr athena's | Becomes |
|---|---|
| State in `~/.local/state/athena` | Renamed to `~/Library/Application Support/athena`, one rename on the same volume. The old path becomes a link to it. |
| launchd jobs `athena.doctor`, `athena.dash`, `athena.board` | Stopped. Their job files are kept in `~/Library/LaunchAgents.athena-old`. |
| launchd job `athena.herdr` | Left alone: it runs the herdr server, which may serve more than athena. |
| The old HQ's herdr workspace | Closed. herdr resumes the Claude in each pane it restores, so an old HQ pane left open comes back as a second HQ after any herdr restart. |
| `~/.local/bin/athena`, a link into its checkout | Moved aside to `~/.local/bin/athena-herdr`. |
| The `athena@athena` Claude plugin (hooks, skill, watch mod) | Turned off in `~/.claude/settings.json`. Nothing else in that file changes. |
| HQ's conversation (`hq.json`) | Kept. `athena hq` resumes it. |
| Open items, the ledger, briefs, notes | Kept as they are. Items with `auto` take their recommendation 12 hours after the new watch first looks. |

athena itself is installed in `~/.local/share/athena`, with its command at `~/.local/bin/athena` and its launchd jobs
`local.athena.doctor`, `.watch` and `.dash`.

## Steps

Run every command in a new, plain terminal: not inside a herdr pane, and not from a Claude session that athena started.

**1. Get athena.**

```sh
git clone https://github.com/Delitefully/athena.git ~/athena
cd ~/athena
```

**2. Retire the herdr athena's workers.** The upgrade takes the herdr athena away, so no worker may still run on it.
Let each finish, then `athena retire <name>`, with the herdr athena's own command. `athena status` lists them.

**3. Check.** Changes nothing.

```sh
scripts/upgrade-from-herdr.py check
```

Each `✗` says what clears it. The usual ones:
- *these still run on the herdr athena*: step 2.
- *exit the herdr athena's HQ first*: type `/exit` in HQ's pane, then close that pane, as herdr can restart what was in
  it. If the old watch is still named, kill that one pid, as the line says.
- *this shell sets ATHENA_STATE_DIR*: open a new terminal without it.

Run `check` again until it says `ready`.

**4. Dry run.** Says what every step would do, and changes nothing.

```sh
scripts/upgrade-from-herdr.py run --dry-run
```

**5. Snapshot.** A copy of the state (an APFS clone: instant, no extra room until files change), with a fingerprint of
every file, and copies of Claude's settings and the launchd jobs beside it, at `~/.local/state/athena.bak-upgrade-<time>`.
Keep it until you are sure.

```sh
scripts/upgrade-from-herdr.py snapshot
```

**6. Stop the old jobs, close the old HQ's herdr workspace, then move the state.**

```sh
scripts/upgrade-from-herdr.py stop-old
scripts/upgrade-from-herdr.py old-hq
scripts/upgrade-from-herdr.py move
```

`old-hq` closes the workspace the old HQ ran in (named in `hq.json`), so a herdr restart can't resume the old HQ beside
the new one. herdr reuses ids after a restart, so it closes that workspace only while herdr still labels it `athena`;
otherwise, or without `herdr` on PATH, it says what to close by hand. Run it outside herdr: it refuses inside a herdr pane.

`move` refuses while the old HQ or its watch runs, and while anything is at the new path already.

**7. Move the old command aside, then install athena.**

```sh
scripts/upgrade-from-herdr.py launcher
scripts/upgrade-from-herdr.py install
```

`install` runs `bin/athena install --services --real-world` from this checkout: preflight (Python, git, Rex, Claude, gh),
the release, the launcher, the permission profiles, and the launchd jobs. If it says `~/.local/bin is not on your PATH`,
add the line it prints to your shell's profile.

**8. Turn the old plugin off.**

```sh
scripts/upgrade-from-herdr.py plugin
```

**9. Verify.**

```sh
scripts/upgrade-from-herdr.py verify
```

Every row should be `✓`. The new jobs need a minute to come round the first time: if only *the doctor job has run* or
*the watch has come round* is `✗`, wait a minute and run `verify` again.

**10. Open HQ.**

```sh
athena hq
```

HQ resumes its conversation in its own Rex session, named `athena`. Tell it what to start.

## Going back

From any step:

```sh
scripts/upgrade-from-herdr.py rollback
```

It stops athena's jobs, its watch, its dash and its HQ, moves the state back to `~/.local/state/athena`, puts the herdr
athena's `hq.json` back on the newest conversation, takes `rex.real_world` out, puts the herdr athena's command and
plugin back, and loads the old jobs again. Workers started by the new athena stop it: retire them first (`athena retire
<name>`), or pass `--leave-workers`. Then start the old HQ with `athena hq`.

If the state itself is damaged, after a rollback:

```sh
scripts/upgrade-from-herdr.py restore-snapshot
```

`compare` tells what changed in the state since the snapshot: files only appended to, files running things change, and
anything unexpected.

## Later

- `~/.local/bin/athena-herdr` and `~/Library/LaunchAgents.athena-old` can go once you are sure.
- The herdr athena's checkout and its plugin's marketplace entry are yours to remove.
- `athena update` brings each new release; `athena doctor` says when one is out.
