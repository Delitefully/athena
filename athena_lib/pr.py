"""Pull request state from `gh`, reduced to what the chief of staff acts on, and whose move each PR waits on."""
import json
import re
import subprocess

FIELDS = ("number,url,title,state,isDraft,reviewDecision,headRefOid,mergeable,statusCheckRollup,comments,reviews,"
          "reviewRequests,latestReviews,autoMergeRequest,mergeStateStatus,baseRefName")


def reduce_checks(rollup) -> str:
    states = []
    for check in rollup or []:
        value = (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        states.append(value)
    if not states:
        return "none"
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") for s in states):
        return "failure"
    if any(s in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED", "") for s in states):
        return "pending"
    return "success"


_QUEUE_CHECK = re.compile(r"^merge[ _-]?queue$", re.I)
_WAITING = ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED")


def _queued(data) -> bool:
    """In a merge queue: GitHub's own (`isInMergeQueue`, GraphQL only), or a bot queue's pending `merge-queue` check."""
    if data.get("isInMergeQueue"):
        return True
    for check in data.get("statusCheckRollup") or []:
        name = check.get("name") or check.get("context") or ""
        state = (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        if _QUEUE_CHECK.match(name) and state in _WAITING:
            return True
    return False


def _reviewer(r) -> str:
    """A requested reviewer's display name, else its login (a team's slug)."""
    return ((r or {}).get("name") or (r or {}).get("login") or (r or {}).get("slug") or "").strip()


def reduce(data: dict) -> dict:
    return {
        "number": data.get("number"), "url": data.get("url"), "title": data.get("title") or "",
        "state": data.get("state"), "draft": data.get("isDraft"), "review": data.get("reviewDecision") or "",
        "head": (data.get("headRefOid") or "")[:12], "mergeable": data.get("mergeable"),
        "base": data.get("baseRefName"), "checks": reduce_checks(data.get("statusCheckRollup")),
        "comments": len(data.get("comments") or []) + sum(1 for r in data.get("reviews") or [] if r.get("body")),
        "requested": [n for n in (_reviewer(r) for r in data.get("reviewRequests") or []) if n],
        "reviews": {r["author"]["login"]: r.get("state") for r in data.get("latestReviews") or []
                    if (r.get("author") or {}).get("login")},
        "auto_merge": bool(data.get("autoMergeRequest")), "queued": _queued(data),
        "merge_state": data.get("mergeStateStatus"),
    }


def _names(names, most=3) -> str:
    shown = ", ".join(names[:most])
    return shown + (f" +{len(names) - most}" if len(names) > most else "")


def move(p, live: bool, human_merge: bool):
    """Whose move an open PR waits on: {"label", "kind", "needs"}; `needs` puts its row in the human's Needs you.

    `no reviewer requested` only where a review is required (REVIEW_REQUIRED); a repo whose branch has no review
    rule (reviewDecision empty) waits on the merge alone. A merge is the human's only once checks are green (or the
    repo runs none); before that it is `checks running`. A live worker's PR is the human's to merge only once the
    worker reported DONE: the dashboard and the band check that, from the worker's state.

    `live`: a live worker owns the PR. `human_merge`: its repo is in `human_merge_repos`. None for a PR that is not
    open, or one with no review data (fetched before athena read reviewers): no guess at whose move it is.
    """
    if not p or p.get("state") != "OPEN" or "requested" not in p:
        return None

    def out(label, kind, needs):
        return {"label": label, "kind": kind, "needs": needs}

    requested = list(p.get("requested") or [])
    green = p.get("checks") in ("success", "none")  # nothing is the human's to merge while CI still runs
    if p.get("draft"):
        return out("draft", "draft", False)
    if p.get("checks") == "failure":
        return out("checks failing", "failing", not live)
    if p.get("review") == "CHANGES_REQUESTED":
        asked = [who for who, state in (p.get("reviews") or {}).items() if state == "CHANGES_REQUESTED"]
        if not (requested and asked and all(who in requested for who in asked)):
            return out("changes requested · worker fixing", "changes", False) if live else \
                out("changes requested", "changes", True)
    elif p.get("review") == "APPROVED":
        if p.get("queued"):
            return out("approved · in merge queue", "queued", False)
        if p.get("auto_merge"):
            return out("approved · auto-merge on", "queued", False)
        if not green:
            return out("checks running", "pending", False)
        return out("approved · yours to merge" if human_merge else "approved · your go to merge", "approved", True)
    if requested:
        return out("awaiting review · " + _names(requested), "awaiting", False)
    if p.get("review") == "REVIEW_REQUIRED":
        return out("no reviewer requested", "unrequested", True)
    # No review rule on its branch (reviewDecision empty): nothing to wait for but the merge.
    if p.get("queued"):
        return out("in merge queue", "queued", False)
    if p.get("auto_merge"):
        return out("auto-merge on", "queued", False)
    if not green:
        return out("checks running", "pending", False)
    return out("yours to merge" if human_merge else "your go to merge", "mergeable", True)


def summary(repo, branch):
    try:
        proc = subprocess.run(["gh", "pr", "view", branch, "--json", FIELDS], cwd=str(repo),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return reduce(json.loads(proc.stdout))
    except ValueError:
        return None


_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")  # whole string only: fullmatch


def is_slug(text) -> bool:
    """`owner/name` and nothing else: no host, no query, no trailing newline."""
    return isinstance(text, str) and _SLUG.fullmatch(text) is not None


_NODE = """number url title state isDraft baseRefName mergeable reviewDecision headRefOid isInMergeQueue
mergeStateStatus autoMergeRequest { enabledAt }
reviewRequests(first: 20) { nodes { requestedReviewer { __typename
  ... on User { login name } ... on Team { slug name } ... on Bot { login } ... on Mannequin { login } } } }
latestReviews(first: 20) { nodes { author { login } state } }
commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 50) { nodes { __typename
  ... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } } } }"""


def _flatten(node) -> dict:
    """A GraphQL pull request in the shape `gh pr view --json` gives, so `reduce` reads both."""
    data = dict(node)
    data["reviewRequests"] = [(n or {}).get("requestedReviewer") or {} for n in (node.get("reviewRequests") or {}).get("nodes") or []]
    data["latestReviews"] = (node.get("latestReviews") or {}).get("nodes") or []
    commits = (node.get("commits") or {}).get("nodes") or [{}]
    rollup = ((commits[-1] or {}).get("commit") or {}).get("statusCheckRollup") or {}
    data["statusCheckRollup"] = (rollup.get("contexts") or {}).get("nodes") or []
    return data


def batch(keys):
    """`owner/name#n` -> reduced PR for every (owner/name, number), in one GraphQL call. None when gh fails.

    Repos and numbers are validated and passed as variables or plain integers, so nothing a ledger or stack file
    holds can change the query. A PR GitHub does not know is left out.
    """
    repos = {}
    for slug, number in keys:
        if is_slug(slug) and isinstance(number, int) and not isinstance(number, bool) and number > 0:
            repos.setdefault(slug, [])
            if number not in repos[slug]:
                repos[slug].append(number)
    if not repos:
        return {}
    args, params, parts = [], [], []
    for i, (slug, numbers) in enumerate(repos.items()):
        owner, name = slug.split("/", 1)
        params += [f"$o{i}: String!", f"$n{i}: String!"]
        args += ["-f", f"o{i}={owner}", "-f", f"n{i}={name}"]
        prs = " ".join(f"p{j}: pullRequest(number: {n}) {{ ...F }}" for j, n in enumerate(numbers))
        parts.append(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ {prs} }}")
    query = f"query({', '.join(params)}) {{ {' '.join(parts)} }} fragment F on PullRequest {{ {_NODE} }}"
    try:
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}", *args],
                              capture_output=True, text=True, timeout=60)
        data = (json.loads(proc.stdout) or {}).get("data") if proc.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        data = None
    if not isinstance(data, dict):
        return None  # a PR GitHub cannot resolve still comes back as data, with an error beside it
    out = {}
    for i, (slug, numbers) in enumerate(repos.items()):
        repo = data.get(f"r{i}") or {}
        for j, n in enumerate(numbers):
            node = repo.get(f"p{j}")
            if isinstance(node, dict):
                out[f"{slug}#{n}"] = reduce(_flatten(node))
    return out


_TITLE_MAX = 120


def _title(text) -> str:
    """A PR title as one line of data: whitespace folded, control characters dropped, capped."""
    text = " ".join("".join(c for c in str(text or "") if c.isprintable() or c.isspace()).split())
    return text if len(text) <= _TITLE_MAX else text[:_TITLE_MAX - 1].rstrip() + "…"


def review_requested(slugs, cap=10, last=None) -> list:
    """Open PRs that ask the human (`@me`) for a review, one `gh search prs` per repo, at most `cap` in all.

    Each links by the repo searched and its number, never by the URL GitHub returns. A repo whose search fails keeps
    its rows from `last`, the previous list.
    """
    out = []
    for slug in slugs:
        if len(out) >= cap or not is_slug(slug):
            continue
        try:
            proc = subprocess.run(["gh", "search", "prs", "--review-requested=@me", "--state=open", "--repo", slug,
                                   "--json", "number,title,author", "--limit", str(cap)],
                                  capture_output=True, text=True, timeout=30)
            items = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            items = None
        if not isinstance(items, list):
            out += [r for r in last or [] if isinstance(r, dict) and r.get("repo") == slug][:cap - len(out)]
            continue
        for item in items:
            n = item.get("number") if isinstance(item, dict) else None
            if isinstance(n, int) and n > 0 and len(out) < cap:
                out.append({"repo": slug, "number": n, "title": _title(item.get("title")),
                            "author": ((item.get("author") or {}).get("login") or ""),
                            "url": f"https://github.com/{slug}/pull/{n}"})
    return out
