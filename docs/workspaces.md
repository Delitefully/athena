# Worktrees and scratch spaces

Worktrees, Claude's own agent worktrees, per-session scratchpads, leftovers in the state dir and backup refs all pile up over time: a retired worker's folder goes, but anything else stays (the human's own worktrees, `--keep-worktree`, half-removed ones, disposable output workers left in the state dir).

The common cause: **a space has no owner on record and no end.** So nothing can say whether it is safe to remove, and nobody dares.

## The design

**Nothing new to keep in step.** The ledger already knows each worker's path. A workspace's state is computed from facts on every read, never stored, so it cannot drift.

**Every space has an owner:**

| Kind | Where | Owner comes from |
|---|---|---|
| `worktree` | the worktree root | the ledger, by path |
| `claude-worktree` | `<repo>/.claude/worktrees` | none: always orphan or held |
| `scratch` | `<state>/scratch/<owner>/<name>` | a marker file, made by `athena-rex scratch new` |
| `scratchpad` | `/private/tmp/claude-<uid>/` | the ledger, matching the folder name to a worker's encoded path |
| `state` | a directory in the state dir that isn't one of ours | none, and reported only |

**Every space has a lifecycle, one pure function (`workspaces.classify`):**

```
owner live, Claude running    -> active
owner live, Claude gone       -> crashed    kept: resume or retire it
owner retired                 -> retired    removable after the grace
no owner                      -> orphan     removable after the grace
scratch past its ttl          -> expired    removable
anything holding unsaved work -> held       never removed
a running Claude has it open  -> active     never removed
```

Unknown keeps: a worktree git can't read is held, not assumed clean.

**One command lists them all:** `athena-rex workspaces [--json] [--kind K] [--no-size]` gives kind, state, size, owner and path.

Worktrees held only by git-ignored files are common, so expect `retire` to refuse more often, and add what is regenerable to `rex.disposable`.

**Cleanup can't lose work.** `athena-rex workspaces clean` only says what it would do; `--apply` does it.
- Git-ignored files count as work, since an `.env` or a notes folder exists nowhere else. A worktree holding any is held, and its row names them. Regenerable caches don't count. A file is a cache's only by its own name (`*.pyc`, `.DS_Store`), or inside a folder with a cache's name that git ignores as a folder: `node_modules`, `.venv`, `__pycache__`, `dist`, `build`, `target` and the like (`gitops.py`), plus names in `rex.disposable` in the state dir's own config. Those are plain names or a literal stem of at least 3 characters then one `*` (`generated`, `gen*`), and anything else is ignored. `.cc-writes` and `settings.local.json` count only directly under a `.claude` folder. So an ignored `.env` in a tracked `services/build/` counts. A repository under any such name is held. A secret-shaped file or folder, by its name in any case and with trailing dots and spaces ignored (`.env`, `x.pem.`, `secrets/`, `credentials*`, `*.tfstate` and more in `gitops.SECRET_SHAPED`), is held in a build folder or a folder config names, and no config name can exempt one. It is a list of names, so a secret with an ordinary name in a build folder still goes. Those folders' other contents do go with the worktree, unlisted: a hand-made file kept inside an ignored `build/` is the limit of this.
- A worktree goes through `git worktree remove` without `--force`, which refuses a dirty tree, and only after unsaved work is checked again at that moment. Its head goes to a backup ref first.
- Anything else moves to `<state>/trash/<date>/`. Nothing is deleted outright.
- The state dir's own leftovers are only ever reported: only the human removes those.
- The grace is 7 days (`--grace-days`).
- In the lab, `--apply` refuses anything outside a marked scratch copy or a marked isolated HOME (`.athena-scratch`, written by `scripts/scratch-state.py`), and a state dir under the real home is refused outright. A survey with explicit roots never applies.
- A scratchpad a running Claude has open stays, whoever owns it, and its age is its newest file, not its folder's date.

**Retire keeps the same promise.** Unsaved work refuses a retire, and the refusal names git-ignored files. `--force` first saves changes under `refs/athena-backup/<name>/<ts>-wip`, untracked files included (the old `git stash create` missed those), and copies git-ignored work to `<state>/trash/<date>/<name>-ignored-<ts>/`. If a save fails, the retire stops. A repository inside the worktree can't be saved under a ref, so one that holds anything of its own stops the retire, `--force` or not, until someone moves it. That means changes, ignored work, commits on any branch, stash or tag that no remote has, commits only a reflog remembers, or a nested repository holding any of these. It covers untracked repositories, tracked submodules, and every repository in the worktree's `modules` folder, where a real submodule lives and which `--force` and `git worktree prune` delete, even after `deinit`, `git rm` or a rename left nothing in the tree. A check that fails counts as holding. Retiring a vanished or broken worker runs that prune, and git itself lists what the prune would take, so the retire refuses while any of those holds work, and names the one that does. `git gc` prunes such a record on its own about three months after anyone last used it, so a refused worker needs a person. Locking the record clears the refusal and outlasts both; the refusal prints how, as `touch <admin>/locked`, since `git worktree lock` can't name a badly broken record. `--force` removes a submodule that holds nothing of its own; a plain retire stops at any submodule, since git won't remove one without `--force`. `--keep-worktree` removes nothing, so it refuses only when the worktree is already vanished or broken and its own record holds work, because nothing would track that record afterwards. A record's detached HEAD gets a backup ref before a prune takes it, under the worker's name or `pruned-<record>` for another worker's prune. When the main checkout is gone there are no records left to lose, so nothing refuses for them; one git can't read refuses and says to repair it or move it away.

**New disposable work has a home:** `athena-rex scratch new <name> --owner <w> [--ttl-days N]`. HQ's prompt says to use it.

## Not built yet
- Removing empty repo folders in the worktree root.
- Expiring `refs/athena-backup` (kept for now: cheap, and the safety net).
- Emptying `trash/` on a schedule.
- Disk-pressure triggers: named caches are listed and emptied by hand (`athena-rex disk`).

## Tests
`tests/test_lifecycle.py`, through `scripts/test.sh`, in an isolated HOME with real git repos: a crashed worker keeps its worktree and its work; a retired worker that held unsaved work is never cleaned; a clean retired one goes with its head kept; work that appears between the look and the removal keeps it; the lab refuses the real home.
