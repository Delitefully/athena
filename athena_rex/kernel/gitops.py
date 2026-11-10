"""Git, for what guards a launch and a removal. Read commands take no optional locks, so looking never writes an index."""
import fcntl
import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from athena_rex.kernel import store

_ENV = {"GIT_OPTIONAL_LOCKS": "0"}
_NO_FETCH = {"GIT_NO_LAZY_FETCH": "1"}  # for a look: a worktree or a fast-forward in a partial clone has to fetch
_SAFE = ("-c", "core.fsmonitor=false")  # a repository's own config must not run a program because we looked at it
BACKUPS = "refs/athena-backup/"
CACHE_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt",
              ".turbo", ".cache", ".gradle"}
BUILD_DIRS = {"target", "dist", "build", "coverage"}
DISPOSABLE_FILES = ("*.pyc", "*.pyo", ".DS_Store")
CLAUDE_DIR_CACHE, CLAUDE_FILE_CACHE = ".claude/.cc-writes", ".claude/settings.local.json"  # only directly under a .claude folder
SECRET_SHAPED = (".env", ".env.*", "*.env", "*.env.local", ".envrc", ".npmrc", ".netrc", ".pypirc", "*.pem", "*.key", "*.p12", "*.pfx",
                 "id_rsa*", "id_ed25519*", "credentials*", "secrets*", "secret.*", "client_secret*.json", "*.tfstate", "*.tfstate.*",
                 "*.sqlite", "*.sqlite3", "*.db")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9._-]+")
STEM_LENGTH = 3
_CLEARS = "Repair it, or if it is gone for good move it away: a retire with no repo left has nothing to prune."


class GitError(Exception):
    pass


def git(cwd, *args, check=True, timeout=120, env=None, stdin=None, fetches=False) -> str:
    """Bytes, not text mode: Python's text mode turns the \\r of a name like Finder's `Icon\\r` into \\n."""
    sent = stdin.encode("utf-8", "surrogateescape") if stdin is not None else None
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *_SAFE, *args], capture_output=True, timeout=timeout, input=sent,
                              env={**os.environ, **_ENV, **({} if fetches else _NO_FETCH), **(env or {})})
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)}: timed out after {timeout}s")
    out = proc.stdout.decode("utf-8", "surrogateescape")
    if check and proc.returncode:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr.decode('utf-8', 'surrogateescape') or out).strip()}")
    return out.rstrip()


def ok(cwd, *args) -> bool:
    return subprocess.run(["git", "-C", str(cwd), *_SAFE, *args], capture_output=True,
                          env={**os.environ, **_ENV, **_NO_FETCH}).returncode == 0


def main_checkout(cwd) -> str:
    """The real path of the checkout a worktree belongs to: where Claude keeps a folder's trust."""
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(os.path.dirname(common)) if common.endswith("/.git") else os.path.realpath(common)


def default_branch(repo) -> str:
    head = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if head:
        return head.replace("refs/remotes/origin/", "", 1)
    known = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", check=False).splitlines()
    return next((b for b in ("main", "master") if f"origin/{b}" in known), "main")


def preflight(repo, fetch=True) -> list:
    """Why a launch can't start here; empty means clean, on the default branch and fast-forwarded."""
    if not ok(repo, "rev-parse", "--git-dir"):
        return [f"{repo} is not a git repository"]
    trouble = []
    if fetch:
        try:
            git(repo, "fetch", "--quiet", "origin", fetches=True)
        except GitError as exc:
            trouble.append(f"fetch failed: {exc}")
    wanted = default_branch(repo)
    current = git(repo, "branch", "--show-current", check=False)
    if current != wanted:
        trouble.append(f"the checkout is on {current or 'a detached HEAD'}, not {wanted}")
    changed = git(repo, "status", "--porcelain", "--untracked-files=no", check=False).splitlines()
    if changed:
        trouble.append(f"{len(changed)} tracked files have changes")
    spread = git(repo, "rev-list", "--left-right", "--count", f"HEAD...origin/{wanted}", check=False).split()
    mine, theirs = (int(n) for n in spread) if len(spread) == 2 else (0, 0)
    if mine and theirs:
        trouble.append(f"{wanted} has diverged: {mine} local, {theirs} on origin")
    if theirs and not trouble:
        try:
            git(repo, "merge", "--ff-only", "--quiet", f"origin/{wanted}", fetches=True)
        except GitError as exc:
            trouble.append(f"fast-forward failed: {exc}")
    return trouble


def add_worktree(repo, branch, path):
    """One at a time per repo: two `git worktree add` at once race on .git/config."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    with open(os.path.join(common, "athena-worktree.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            args = ["worktree", "add", str(path), branch]
        else:
            remote = ok(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
            base = f"origin/{branch}" if remote else f"origin/{default_branch(repo)}"
            if not ok(repo, "rev-parse", "--verify", "--quiet", base):
                base = "HEAD"
            args = ["worktree", "add", "-b", branch, str(path), base]
        git(repo, *args, fetches=True)


def unsaved(worktree) -> dict:
    """What only this worktree holds. `unknown` when git cannot say: a caller treats that as holding work."""
    try:
        dirty = status_paths(worktree, "--untracked-files=all")
        upstream = git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
        has_upstream = bool(upstream) and "@{u}" not in upstream
        ahead, pushed = 0, has_upstream
        if has_upstream:
            ahead = int(git(worktree, "rev-list", "--count", "@{u}..HEAD", check=False) or 0)
        else:
            base = f"origin/{default_branch(worktree)}"
            if ok(worktree, "rev-parse", "--verify", "--quiet", base):
                ahead = int(git(worktree, "rev-list", "--count", f"{base}..HEAD", check=False) or 0)
            remotes = git(worktree, "branch", "-r", "--contains", "HEAD", check=False)
            if remotes:
                ahead = 0
        return {"dirty": dirty, "ahead": ahead,
                "has_upstream": pushed, "branch": git(worktree, "branch", "--show-current", check=False),
                "ignored": ignored_work(worktree), "modules": modules_hold(worktree), "unknown": False}
    except (GitError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"dirty": [], "ahead": 0, "has_upstream": False, "branch": None, "ignored": [], "unknown": True,
                "error": str(exc)}


def holds_work(info) -> bool:
    return bool(info.get("unknown") or info.get("dirty") or info.get("ahead") or info.get("ignored") or info.get("modules"))


def _keep(repo, name, sha, suffix="") -> str:
    """A ref under the namespace the old athena also uses, so one tool finds both's backups."""
    stamp = int(time.time())
    ref = f"{BACKUPS}{name}/{stamp}{suffix}"
    while git(repo, "rev-parse", "--verify", "-q", ref, check=False) not in ("", sha):
        stamp += 1
        ref = f"{BACKUPS}{name}/{stamp}{suffix}"
    git(repo, "update-ref", ref, sha)
    return ref


def backup_head(repo, name, sha) -> str:
    return _keep(repo, name, sha)


def _links_in(listing) -> set:
    """The submodule paths in `ls-files --stage -z` or `ls-tree -r -z` output. Without -z git quotes a name like café, which
    then matches nothing and reads as an empty folder."""
    return {record.split("\t", 1)[1] for record in listing.split("\0") if record.startswith("160000 ")}


def needs_force(worktree) -> bool:
    """Git won't remove a worktree with submodules, or with any modules folder, even an empty one, unless forced."""
    return bool(_links_in(git(worktree, "ls-files", "--stage", "-z"))) or Path(_modules_folder(worktree)).is_dir()


def _modules_folder(worktree) -> str:
    return git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "modules")


def _only_here(where, *flags) -> int:
    """Commits on any branch, tag or stash, or that only a reflog remembers, that no remote has. Raises if git can't say."""
    return int(git(where, *flags, "rev-list", "--count", "--all", "--reflog", "--not", "--remotes") or 0)


def _gitdirs_hold(folder):
    """A linked worktree keeps its submodules' repositories in its admin folder, and removing or pruning the worktree deletes
    them. So each one counts, whether or not anything still names it: after a deinit, a `git rm` or a rename, its folder in the
    tree can be empty or gone. Module names can hold slashes, and modules can nest. Anything else in here counts as holding."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    for entry in sorted(folder.iterdir()):
        if not entry.is_dir():
            return f"{entry.name}, which git doesn't know"
        if (entry / "HEAD").exists():
            count = _only_here(entry, "--git-dir=.", "--work-tree=.")  # its core.worktree may be a folder that's gone
            held = f"{count} commits found only there" if count else _gitdirs_hold(entry / "modules") or _its_worktrees_hold(entry)
        else:
            held = _gitdirs_hold(entry)
        if held:
            return f"{entry.name}: {held}"
    return None


def _its_worktrees_hold(repository):
    """A submodule can have linked worktrees of its own, each with its own modules folder."""
    linked = repository / "worktrees"
    for admin in sorted(linked.iterdir()) if linked.is_dir() else ():
        held = _gitdirs_hold(admin / "modules")
        if held:
            return f"{admin.name}: {held}"
    return None


def modules_hold(worktree):
    """What this worktree's submodule repositories alone hold, or None. What git can't read counts as holding."""
    try:
        return _gitdirs_hold(_modules_folder(worktree))
    except (GitError, OSError, ValueError) as exc:
        return f"what git can't read ({exc})"


def repo_gone(repo) -> bool:
    """No repository there to ask, so no worktree records left to lose. Not asked of git, which would answer for any repository
    holding this folder."""
    place = Path(repo)
    return not place.is_dir() or not ((place / ".git").exists() or ((place / "HEAD").is_file() and (place / "objects").is_dir()))


def _admins(repo) -> dict:
    """Each linked worktree's admin folder by name, with the folder its `gitdir` file names, if it can be read and names one."""
    root = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "worktrees"
    found = {}
    for admin in sorted(root.iterdir()) if root.is_dir() else ():
        try:
            named = (admin / "gitdir").read_bytes().rstrip(b"\r\n")
        except OSError:
            named = b""
        found[admin.name] = (admin, (admin / os.fsdecode(named)).parent if named else None)
    return found


def _prunable(repo) -> list:
    """What `git worktree prune` would delete, from git itself: it also drops duplicates, and its rules move between versions.
    A line that isn't exactly one known admin folder raises."""
    admins = _admins(repo)
    proc = subprocess.run(["git", "-C", str(repo), *_SAFE, "worktree", "prune", "--dry-run", "--verbose"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding="utf-8", errors="surrogateescape", timeout=120,
                          env={**os.environ, **_ENV, **_NO_FETCH, "LC_ALL": "C"})
    if proc.returncode:
        raise GitError(f"git worktree prune --dry-run: {proc.stdout.strip()}")
    found = []
    for line in proc.stdout.splitlines():
        names = [name for name in admins if line.startswith(f"Removing worktrees/{name}: ")]
        if len(names) != 1:
            raise GitError(f"git said something else: {line}")
        found.append(admins[names[0]])
    return found


def _held_at(admin, where):
    """What this admin folder's submodule repositories hold, said so that a person can find it and keep it. `git worktree lock`
    can't name a record this broken, but the file it writes works on all of them, and git's prune and gc both leave it."""
    try:
        held = _gitdirs_hold(admin / "modules")
    except (GitError, OSError, ValueError) as exc:
        held = f"what git can't read ({exc})"
    return held and (f"a submodule repository of the worktree at {where or admin} holds {held}, and git would delete it with the "
                     f"worktree's record. To keep it there and go on: touch {shlex.quote(f'{admin}/locked')}")


def admin_modules_hold(repo, path):
    """The same for a worktree whose own link is broken, from the main checkout's side. A record that names no folder could be
    this one."""
    if repo_gone(repo):
        return None
    try:
        for admin, where in _admins(repo).values():
            if (where is None or os.path.realpath(where) == os.path.realpath(path)) and not (admin / "locked").exists():
                held = _held_at(admin, where)
                if held:
                    return held
    except (GitError, OSError, ValueError) as exc:
        return f"can't read git's worktree records in {repo} ({exc}). {_CLEARS}"
    return None


def prune_refused(repo):
    """Why `git worktree prune` must not run here, or None."""
    if repo_gone(repo):
        return None
    try:
        for admin, where in _prunable(repo):
            held = _held_at(admin, where)
            if held:
                return held
    except (GitError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"can't read what git would prune in {repo} ({exc}). {_CLEARS}"
    return None


def _detached_at(admin):
    """The commit a worktree record's detached HEAD holds, or None. Nothing else names that commit once the record goes."""
    try:
        head = (Path(admin) / "HEAD").read_text().strip()
    except OSError:
        return None
    return head if re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def keep_detached(repo, name, path):
    """A ref for the detached HEAD of this vanished or broken worktree, or None if it has none."""
    if repo_gone(repo):
        return None
    for admin, where in _admins(repo).values():
        if where and os.path.realpath(where) == os.path.realpath(path):
            sha = _detached_at(admin)
            return backup_head(repo, name, sha) if sha else None
    return None


def prune(repo):
    why = prune_refused(repo)
    if why:
        raise GitError(why)
    if repo_gone(repo):
        return
    for admin, _ in _prunable(repo):
        sha = _detached_at(admin)
        if sha:
            backup_head(repo, f"pruned-{admin.name}", sha)
    git(repo, "worktree", "prune", check=False)


def status_paths(cwd, *extra) -> list:
    """Each changed path, whole: -z keeps a space in a path from being quoted, and a rename counts once, by its new name."""
    tokens = git(cwd, "status", "--porcelain", "-z", *extra).split("\0")
    paths, moved = [], False
    for token in tokens:
        if moved:
            moved = False
        elif token:
            paths.append(token[3:])
            moved = "R" in token[:2] or "C" in token[:2]
    return paths


def _what_it_holds(repo):
    """What a repository inside a worktree alone holds, or None. Commits on any branch, stash or tag no remote has, changes,
    ignored work, and the same for any repository inside it. What git can't read counts as holding."""
    repo = Path(repo)
    if not repo.exists():
        return "a path git lists and that isn't there"
    if not (repo / ".git").exists():
        return "files, and no repository git can read" if repo.is_dir() and any(repo.iterdir()) else None
    try:
        only_here = _only_here(repo)
        if only_here:
            return f"{only_here} commits found only there"
        if status_paths(repo, "--untracked-files=all", "--ignore-submodules=none"):
            return "changes"
        if ignored_work(repo):
            return "git-ignored work"
        for nested in sorted(_links_in(git(repo, "ls-files", "--stage", "-z"))):
            held = _what_it_holds(repo / nested)
            if held:
                return f"{nested}: {held}"
    except (GitError, OSError, ValueError) as exc:
        return f"what git can't read ({exc})"
    return None


def refuse_nested_repos(worktree, head, env=None):
    """A repository inside the worktree goes into a commit as one pointer, so its files and commits would not be saved. A
    submodule already at HEAD is fine only if it holds nothing of its own, and that goes for every repository in the modules
    folder, named or not."""
    in_modules = modules_hold(worktree)
    if in_modules:
        raise GitError(f"a submodule repository of this worktree holds {in_modules}: removing the worktree would delete it. Push it or move it")
    at_head = _links_in(git(worktree, "ls-tree", "-r", "-z", head, check=False)) if head else set()
    changed = set(status_paths(worktree, "--ignore-submodules=none"))
    for path in sorted(_links_in(git(worktree, "ls-files", "--stage", "-z", env=env))):
        held = "not committed here" if path not in at_head else "changes" if path in changed else _what_it_holds(Path(worktree) / path)
        if held:
            raise GitError(f"{path} is a repository inside the worktree holding {held}: a ref can't save that. Move it out by hand")


def _config_names() -> tuple:
    """Names added by config rex.disposable, from the state dir's own file. A glob can't be proved safe by trying names on it, so
    only two forms are allowed: a plain name, or a literal stem of real length then one `*`. Neither may look like a secret,
    and is_secret is checked again on every match."""
    data = store.read_json(store.state_dir() / "config.json", {})
    named = (data.get("rex") or {}).get("disposable") if isinstance(data, dict) and isinstance(data.get("rex"), dict) else None
    if not isinstance(named, list):
        return ()
    allowed = []
    for name in named:
        stem = name[:-1] if isinstance(name, str) and name.endswith("*") else name
        if isinstance(stem, str) and _PLAIN_NAME.fullmatch(stem) and not is_secret(stem) and (stem == name or len(stem) >= STEM_LENGTH):
            allowed.append(name)
    return tuple(allowed)


def is_secret(name) -> bool:
    """By the file's name alone, in any case: this disk ignores case, and so do people naming .ENV or Prod.PEM."""
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(form, shape) for form in (lowered, lowered.rstrip(". ")) for shape in SECRET_SHAPED)


def _ignored_folders(worktree, folders) -> set:
    """Those of `folders` git ignores as folders, not merely ones whose files happen to be ignored."""
    if not folders:
        return set()
    answer = git(worktree, "check-ignore", "--stdin", "-z", check=False, stdin="\0".join(f"{f}/" for f in folders) + "\0")
    return {f.rstrip("/") for f in answer.split("\0") if f}


def ignored_work(worktree) -> list:
    """Git-ignored files only this worktree holds, listed file by file: git's --directory reports `services/` for a lone
    services/build/.env and `src/` for src/__pycache__/x.pyc, which hides one and holds the other.

    A repository is always work. A file is a cache's only inside a folder that has a cache's name and that git ignores as a
    folder, or by its own name. In a build folder a secret-shaped file is still work; its other files count as output, and
    that is the limit of this: a hand-made file kept in an ignored build/ goes with it.
    """
    listed = git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "--no-empty-directory", "-z")
    extra = _config_names()
    entries = [e for e in listed.split("\0") if e]
    named = {}
    for entry in entries:
        parts = entry.rstrip("/").split("/")[:-1]
        for depth, part in enumerate(parts, 1):
            folder = "/".join(parts[:depth])
            if f"/{folder}".endswith(f"/{CLAUDE_DIR_CACHE}"):
                named.setdefault(folder, CLAUDE_DIR_CACHE)
            elif part in CACHE_DIRS or part in BUILD_DIRS or (any(fnmatch.fnmatchcase(part, p) for p in extra) and not is_secret(part)):
                named.setdefault(folder, part)
    ignored = _ignored_folders(worktree, sorted(named))
    work = []
    for entry in entries:
        name = entry.rsplit("/", 1)[-1]
        if entry.endswith("/"):
            work.append(entry)
            continue
        folders = ["/".join(entry.split("/")[:n]) for n in range(1, entry.count("/") + 1)]
        tiers = [named[f] for f in folders if f in ignored]
        if any(t in CACHE_DIRS or t == CLAUDE_DIR_CACHE for t in tiers):
            continue
        secret = is_secret(name) or any(is_secret(part) for part in entry.split("/")[:-1])
        if tiers and not secret:
            continue
        if not tiers and not secret and any(fnmatch.fnmatchcase(name, p) for p in (*DISPOSABLE_FILES, *extra)):
            continue
        if f"/{entry}".endswith(f"/{CLAUDE_FILE_CACHE}"):
            continue
        work.append(entry)
    return work


def size_of(worktree, entries) -> int:
    """Bytes these entries hold, links counted as links."""
    total = 0
    for entry in entries:
        source = os.path.join(worktree, entry.rstrip("/"))
        walked = os.walk(source) if os.path.isdir(source) and not os.path.islink(source) else [(os.path.dirname(source), [], [os.path.basename(source)])]
        for folder, _, files in walked:
            for name in files:
                try:
                    total += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
    return total


def keep_ignored(worktree, entries, target) -> str:
    """A copy of ignored work, kept where the trash is, before its worktree goes. Links stay links."""
    for entry in entries:
        source, copy = Path(worktree) / entry.rstrip("/"), Path(target) / entry.rstrip("/")
        copy.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, copy, symlinks=True)
        else:
            shutil.copy2(source, copy, follow_symlinks=False)
    return str(target)


def backup_wip(worktree, repo, name) -> str:
    """Tracked changes and untracked files, kept as a commit under a ref. Ignored files are not work."""
    with tempfile.TemporaryDirectory(prefix="athena-wip-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        head = git(worktree, "rev-parse", "--verify", "-q", "HEAD", check=False)
        if head:
            git(worktree, "read-tree", head, env=env)
        git(worktree, "add", "-A", env=env, timeout=300)
        refuse_nested_repos(worktree, head, env)
        tree = git(worktree, "write-tree", env=env)
        identity = {"GIT_AUTHOR_NAME": "athena", "GIT_AUTHOR_EMAIL": "athena@localhost",
                    "GIT_COMMITTER_NAME": "athena", "GIT_COMMITTER_EMAIL": "athena@localhost"}
        commit = git(worktree, "commit-tree", tree, *(["-p", head] if head else []), "-m", f"athena wip of {name}",
                     env=identity)
    return _keep(repo, name, commit, "-wip")


def backups(repo, name=None) -> list:
    prefix = f"{BACKUPS}{name}/" if name else BACKUPS
    return git(repo, "for-each-ref", "--format=%(refname)", prefix, check=False).splitlines()


def remove_worktree(repo, path, force=False):
    git(repo, "worktree", "remove", *(["--force"] if force else []), str(path), fetches=True)


def slug(branch: str) -> str:
    """The folder for a branch: its owner prefix dropped, the rest in lowercase words."""
    _, owned, rest = branch.partition("/")
    return "-".join(re.findall(r"[a-z0-9]+", (rest if owned else branch).lower()))


def github_slug(repo):
    """owner/name when origin is on GitHub; None for anything else, so a local repo never reaches gh."""
    url = git(repo, "remote", "get-url", "origin", check=False)
    found = re.search(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url or "")
    return found.group(1) if found else None
