"""`athena report`: a worker files its report here, not in the chat. The Stop hook's gate takes it in when the turn ends."""
import json
import os
import re

from athena.cli import UserError
from athena.kernel import caps, gitops, ledger, store

SUMMARY = "a worker files its report (head and PR filled in); the Stop hook's gate takes it in when its turn ends"
STATUSES = ("DONE", "DONE_WITH_CONCERNS", "IN_PROGRESS", "BLOCKED", "NEEDS_CONTEXT")


def _worker() -> str:
    name = (os.environ.get("ATHENA_WORKER") or "").strip()
    if not store.valid_name(name):
        raise UserError("athena report is for a worker's own session: ATHENA_WORKER names no worker here")
    return name


def _worktree(name) -> str:
    """The worktree its gate checks: the one on record, else where the command runs."""
    found = ledger.get(name) or {}
    path = found.get("path")
    return path if path and os.path.isdir(path) else os.getcwd()


def _open_pr(path, branch):
    """(url or "", why it could not say or None). Only an open PR counts; a repo not on GitHub has none, and gh is never asked."""
    try:
        slug = gitops.github_slug(path)
    except gitops.GitError:
        return "", None
    prs = caps.get("prs")
    if not slug or not branch or not prs:
        return "", None
    try:
        found = json.loads(prs.gh("pr", "view", branch, "--repo", slug, "--json", "url,state", timeout=20) or "null")
    except prs.GhError as exc:
        return ("", None) if "no pull requests found" in str(exc).lower() else ("", f"gh could not say which PR is open: {exc}")
    except ValueError:
        return "", "gh's answer did not parse"
    if isinstance(found, dict) and str(found.get("state") or "").upper() == "OPEN":
        return str(found.get("url") or ""), None
    return "", None


def build(status, verify, decisions=(), concerns=(), path=None) -> tuple:
    """(report, a note on the PR lookup or None). The fields are today's, so every reader reads it as it read a typed line."""
    path = path or os.getcwd()
    head = gitops.git(path, "rev-parse", "HEAD", check=False) or ""
    head = head if re.fullmatch(r"[0-9a-f]{7,64}", head) else ""  # a repo with no commit prints "HEAD", not a sha
    branch = gitops.git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False) or ""
    pr, note = _open_pr(path, branch if branch != "HEAD" else "")
    return ({"status": status, "pr": pr, "head": head, "verify": verify, "decisions": list(decisions), "concerns": list(concerns)},
            note)


def _configure(p):
    p.add_argument("--status", required=True, type=str.upper, choices=STATUSES)
    p.add_argument("--verify", required=True, help='"<command> -> pass|fail"')
    p.add_argument("--decision", action="append", default=[], help="one short line; repeat for more")
    p.add_argument("--concern", action="append", default=[], help="one short line; repeat for more")
    p.add_argument("--summary", help="what you did, in a sentence or two; else your last chat message stands")


def _run(args):
    name = _worker()
    hooks = caps.get("hooks")
    if not hooks:
        raise UserError("the hooks capability is off, so no gate would read a report")
    path = _worktree(name)
    report, note = build(args.status, args.verify, args.decision, args.concern, path)
    why = hooks.refusal("", report, path)
    if why:
        print(f"not filed: {why}" + (f" ({note})" if note else ""))
        return 1
    hooks.file_report(name, report, args.summary)
    pr = report["pr"] or ("none: " + note if note else "none")
    print(f"report filed: {report['status']} @ {report['head'][:12] or 'no commit'}, pr {pr}, verify {report['verify']}. "
          "It stands when this turn ends; end it with a short plain summary.")
    return 0


def commands():
    return {"report": caps.Command(SUMMARY, _configure, _run)}
