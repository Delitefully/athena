"""Throwaway git repositories with a bare origin, for tests."""
import subprocess
from pathlib import Path


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp, name="proj"):
    """Returns (repo, origin). The repo is on main with one commit pushed to origin."""
    tmp = Path(tmp)
    origin = tmp / f"{name}-origin.git"
    repo = tmp / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return repo, origin


def push_from_clone(tmp, origin, filename, branch="main"):
    """Advance origin/<branch> from a second clone, so the first repo is behind."""
    clone = Path(tmp) / f"clone-{filename}"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    git(clone, "checkout", "-q", "-B", branch)
    (clone / filename).write_text("x\n")
    git(clone, "add", filename)
    git(clone, "commit", "-q", "-m", f"add {filename}")
    git(clone, "push", "-q", "origin", branch)
    return git(clone, "rev-parse", "HEAD")
