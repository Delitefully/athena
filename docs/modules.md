# Writing a module

A module is an optional feature: off unless config lists it, and once loaded a capability like those in
`athena_rex/caps/`, with an entry in the menu bar. Public modules live in `athena_rex/modules/<name>/` (clamshell). Private
ones live in your own folder, its own git repo, outside this one: `rex.modules_dir`, default `~/.config/athena-rex/modules/`.

```
<name>/manifest.json   {"name": "<name>", "title": "Shown in the menu"}
<name>/__init__.py     the hooks below; other files beside it import relatively (from . import card)
<name>/tests/          optional: scripts/test.sh runs a private folder's tests with the same harness
```

Names are lowercase letters, digits, `-` and `_`. A name found in both places is an error, never a quiet pick.

## Hooks, all optional

| Hook | What it gives |
|---|---|
| `SUMMARY`, `commands()` | `{verb: caps.Command}`: its CLI; a module never shadows a core verb |
| `checks(ctx)` | `[caps.Check]`: its doctor line |
| `tick(ctx, now)`, `on_event(ctx, e)` | watch lines; the watch is the long-running process modules run in |
| `services(ctx)` | launchd jobs, as for a capability |
| `start(ctx)`, `stop(ctx)` | on load or turn on, on unload or turn off. Quick and idempotent: they run in the short `modules` command, not in the watch |
| `status(ctx)` | `(ok, "one short line")`: the menu's state (`on`, or `error` when not ok) and detail |
| `actions(ctx)`, `action(ctx, id)` | `[{"id", "label"}]` and running one: menu items beyond `toggle`, which every module has |

Reach other capabilities through `ctx.cap(name)`, which is `None` when absent, and the outside world only through
`kernel/world.py`, so the lab and the tests keep it shut. Something long-running, such as a thread in the watch, checks
`caps.get(<name>) is None` before it acts, so `toggle` and `unload` from another process reach it.

## Turning it on

```
athena-rex modules load <name>         adds it to rex.modules in config.json, turns it on, calls start
athena-rex modules unload <name>       takes it out, calls stop
athena-rex modules action <name> <id>  toggle (off or on, kept in the list) or one of its own; prints the new object
athena-rex modules --json              the menu bar's list: one object per listed module, loaded or not
```

Each object is `{"name", "title", "public", "loaded", "state": "on|off|error", "detail", "actions"}`. `loaded` means its code
imported. `state` is `off` when toggled off (kept in the state dir's `modules.json`), and `error` when it won't load or its
status isn't ok. Doctor names a listed module that won't load. With `rex.modules: []`, the default, no module code is imported.

Next candidates to move out of the core into private modules: the #open-prs extras and HQ skip-perms.
