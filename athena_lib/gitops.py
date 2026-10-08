"""Git checks that guard every launch and every retirement."""
import subprocess
import time
from pathlib import Path


class GitError(Exception):
    pass


def git(cwd, *args, check=True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def ok(cwd, *args) -> bool:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True).returncode == 0


def default_branch(repo) -> str:
    ref = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if ref.startswith("origin/"):
        return ref[len("origin/"):]
    for name in ("main", "master"):
        if ok(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
            return name
    return "main"


def _porcelain(cwd, *extra) -> list:
    proc = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain", *extra], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git status: {proc.stderr.strip()}")
    return [line[3:] for line in proc.stdout.splitlines() if line.strip()]


def dirty_tracked(cwd) -> list:
    return _porcelain(cwd, "--untracked-files=no")


def dirty_all(cwd) -> list:
    return _porcelain(cwd)


def preflight(repo, fetch=True, pull=True) -> dict:
    """A launch needs the main checkout clean, on the default branch, and fast-forwarded."""
    repo = Path(repo)
    result = {"ok": False, "repo": str(repo), "branch": None, "default_branch": None,
              "dirty": [], "behind": 0, "problems": []}
    if not ok(repo, "rev-parse", "--git-dir"):
        result["problems"].append(f"{repo} is not a git repository")
        return result
    if fetch:
        try:
            git(repo, "fetch", "--quiet", "origin")
        except GitError as exc:
            result["problems"].append(f"fetch failed: {exc}")
    default = default_branch(repo)
    branch = git(repo, "branch", "--show-current", check=False)
    result.update(default_branch=default, branch=branch, dirty=dirty_tracked(repo))
    if branch != default:
        result["problems"].append(f"on branch {branch or '(detached)'}, not {default}")
    if result["dirty"]:
        result["problems"].append(f"{len(result['dirty'])} modified tracked files")
    upstream = f"origin/{default}"
    if ok(repo, "rev-parse", "--verify", "--quiet", upstream):
        counts = git(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}", check=False).split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])
            result["behind"] = behind
            if ahead and behind:
                result["problems"].append(f"{default} has {ahead} local commits and is {behind} behind: cannot fast-forward")
    if result["problems"]:
        return result
    if pull and result["behind"]:
        try:
            git(repo, "merge", "--ff-only", "--quiet", upstream)
            result["behind"] = 0
        except GitError as exc:
            result["problems"].append(f"cannot fast-forward: {exc}")
            return result
    result["ok"] = True
    return result


def choose_base(repo, branch) -> list:
    if ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        return []
    if ok(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"):
        return ["--base", f"origin/{branch}"]
    return ["--base", f"origin/{default_branch(repo)}"]


def unpushed(worktree) -> dict:
    branch = git(worktree, "branch", "--show-current", check=False)
    upstream = git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    has_upstream = bool(upstream) and "@{u}" not in upstream
    if has_upstream:
        ahead = int(git(worktree, "rev-list", "--count", "@{u}..HEAD", check=False) or 0)
    else:
        repo_default = default_branch(worktree)
        base = f"origin/{repo_default}"
        ahead = int(git(worktree, "rev-list", "--count", f"{base}..HEAD", check=False) or 0) if ok(worktree, "rev-parse", "--verify", "--quiet", base) else 0
    return {"dirty": dirty_all(worktree), "ahead": ahead, "has_upstream": has_upstream, "branch": branch}


def backup_ref(repo, name, sha) -> str:
    ref = f"refs/athena-backup/{name}/{int(time.time())}"
    git(repo, "update-ref", ref, sha)
    return ref
