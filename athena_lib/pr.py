"""Pull request state from `gh`, reduced to what the chief of staff acts on, and whose move each PR waits on."""
import json
import re
import subprocess

FIELDS = ("number,url,title,state,isDraft,reviewDecision,headRefOid,mergeable,statusCheckRollup,comments,reviews,"
          "reviewRequests,latestReviews,autoMergeRequest,mergeStateStatus,baseRefName,headRefName,author")

# Bot accounts (is_bot): their comments never count as review comments, nor their reviews as a teammate's. gh names
# them without the `[bot]` suffix REST uses. The last two are platform bots that post as ordinary accounts.
BOTS = {"github-actions", "github-advanced-security", "copilot", "linear-code", "sun-merge-queue"}


def is_bot(author, association=None) -> bool:
    """Whether a comment's author is a bot: a known bot login (Copilot's reviewer and agent logins too), any login
    ending in `[bot]`, or an account gh or GitHub types as a Bot. An association of NONE is an outside human, too."""
    author = author if isinstance(author, dict) else {}
    login = str(author.get("login") or "").lower()
    if login in BOTS or login.startswith("copilot-") or login.endswith("[bot]"):
        return True
    if author.get("is_bot") is True or "Bot" in (author.get("type"), author.get("__typename")):
        return True
    return str(association or "").upper() == "BOT"


def _human_comments(data) -> int:
    """Comments and review bodies by humans other than the PR's author: what HQ reads review comments for."""
    owner = str(((data.get("author") or {}).get("login")) or "").lower()
    notes = list(data.get("comments") or []) + [r for r in data.get("reviews") or [] if r.get("body")]
    count = 0
    for note in notes:
        who = note.get("author") or {}
        if is_bot(who, note.get("authorAssociation")) or owner and str(who.get("login") or "").lower() == owner:
            continue
        count += 1
    return count


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


def _teammate_reviewed_at(data) -> str:
    """When a teammate last reviewed the PR (ISO time), or "": a person's review, not the PR author's own, never a
    bot's (is_bot) nor an account with no association to the repo."""
    author = ((data.get("author") or {}).get("login") or "").lower()
    times = []
    for r in data.get("latestReviews") or []:
        who = (r or {}).get("author") or {}
        login = (who.get("login") or "").lower()
        association = r.get("authorAssociation")
        if not login or login == author or is_bot(who, association) or association == "NONE":
            continue
        if isinstance(r.get("submittedAt"), str) and r["submittedAt"]:
            times.append(r["submittedAt"])
    return max(times, default="")  # ISO 8601 in UTC sorts as time


def reduce(data: dict) -> dict:
    return {
        "number": data.get("number"), "url": data.get("url"), "title": data.get("title") or "",
        "state": data.get("state"), "draft": data.get("isDraft"), "review": data.get("reviewDecision") or "",
        "head": (data.get("headRefOid") or "")[:12], "mergeable": data.get("mergeable"),
        "base": data.get("baseRefName"), "branch": data.get("headRefName"), "checks": reduce_checks(data.get("statusCheckRollup")),
        "author": (data.get("author") or {}).get("login"),
        "comments": _human_comments(data),
        "comments_all": len(data.get("comments") or []) + sum(1 for r in data.get("reviews") or [] if r.get("body")),
        "requested": [n for n in (_reviewer(r) for r in data.get("reviewRequests") or []) if n],
        "reviews": {r["author"]["login"]: r.get("state") for r in data.get("latestReviews") or []
                    if (r.get("author") or {}).get("login")},
        "teammate_reviewed_at": _teammate_reviewed_at(data),
        "auto_merge": bool(data.get("autoMergeRequest")), "queued": _queued(data),
        "merge_state": data.get("mergeStateStatus"),
    }


def _names(names, most=3) -> str:
    shown = ", ".join(names[:most])
    return shown + (f" +{len(names) - most}" if len(names) > most else "")


VERDICTS = ("start", "pass", "fail")  # `athena review`: HQ's own review of a live worker's PR
_SHA_MIN = 7


def same_head(a, b) -> bool:
    """Whether two shas name one commit: one is a prefix of the other, at least 7 hex digits, any case."""
    a, b = str(a or "").lower(), str(b or "").lower()
    if min(len(a), len(b)) < _SHA_MIN or not re.fullmatch(r"[0-9a-f]+", a + b):
        return False
    return a.startswith(b) or b.startswith(a)


def move(p, live: bool, human_merge: bool, ci=True, review=None):
    """Whose move an open PR waits on: {"label", "kind", "needs", "human_merge"}; `needs` puts its row in the human's
    Needs you, and `human_merge` (its repo is in `human_merge_repos`) keeps the dashboard's Merge, a go for athena, off it.

    `no reviewer requested` only where a review is required (REVIEW_REQUIRED); a repo whose branch has no review
    rule (reviewDecision empty) waits on the merge alone. A merge is the human's only once checks are green (or the
    repo runs no CI); before that it is `checks running`, or `checks starting` while none have registered. A live worker's PR is the human's to merge only once the
    worker reported DONE: the dashboard and the band check that, from the worker's state.

    A live worker's PR is the human's to merge, or to request a reviewer for, only once HQ's review passed at the PR's current head (`review`, the
    worker's ledger verdict from `athena review`): before that it is `in review · athena`, and after a fail at that head
    `review findings · worker fixing`, both In progress. A verdict at an older head is void: in review again.

    `live`: a live worker owns the PR. `human_merge`: its repo is in `human_merge_repos`. `ci`: False when its repo
    runs no CI (`has_ci`), so a PR with no checks is green there; True or None (not known) waits for checks. None for a PR that is not
    open, or one with no review data (fetched before athena read reviewers): no guess at whose move it is.
    """
    if not p or p.get("state") != "OPEN" or "requested" not in p:
        return None

    def out(label, kind, needs):
        return {"label": label, "kind": kind, "needs": needs, "human_merge": human_merge}

    def starting():
        return out("checks starting" if p.get("checks") == "none" else "checks running", "pending", False)

    def athena_review():
        """In review, or the worker fixing findings, until HQ's review passed at this head; None once it did."""
        verdict = review if isinstance(review, dict) else {}
        current = same_head(verdict.get("head"), p.get("head"))
        if current and verdict.get("verdict") == "pass":
            return None
        if current and verdict.get("verdict") == "fail":
            return out("review findings · worker fixing", "changes", False)
        return out("in review · athena", "reviewing", False)

    requested = list(p.get("requested") or [])
    # Nothing is the human's to merge while CI still runs. No checks at all is green only in a repo known to run no CI
    # (`ci` False); elsewhere the workflows have not registered yet after a push.
    green = p.get("checks") == "success" or p.get("checks") == "none" and ci is False
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
            return starting()
        if live and (gate := athena_review()):
            return gate
        return out("approved · yours to merge" if human_merge else "approved · your go to merge", "approved", True)
    if requested:
        return out("awaiting review · " + _names(requested), "awaiting", False)
    if p.get("review") == "REVIEW_REQUIRED":
        if live and (gate := athena_review()):
            return gate  # HQ reviews before the human is asked to request a reviewer
        return out("no reviewer requested", "unrequested", True)
    # No review rule on its branch (reviewDecision empty): nothing to wait for but the merge.
    if p.get("queued"):
        return out("in merge queue", "queued", False)
    if p.get("auto_merge"):
        return out("auto-merge on", "queued", False)
    if not green:
        return starting()
    if live and (gate := athena_review()):
        return gate
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


_NODE = """number url title state isDraft author { login } baseRefName headRefName mergeable reviewDecision headRefOid isInMergeQueue
mergeStateStatus autoMergeRequest { enabledAt }
reviewRequests(first: 20) { nodes { requestedReviewer { __typename
  ... on User { login name } ... on Team { slug name } ... on Bot { login } ... on Mannequin { login } } } }
latestReviews(first: 20) { nodes { author { __typename login } state submittedAt authorAssociation } }
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


THREAD_BODY = 300  # characters of a thread's first comment kept
_THREADS = """query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) {
pullRequest(number: $number) { reviewThreads(first: 100) { nodes { id isResolved isOutdated path line
comments(first: 1) { nodes { author { __typename login } body url } } } } } } }"""


def threads(slug, number):
    """A PR's unresolved review threads, oldest first: [{"id", "author", "bot", "path", "line", "outdated", "body",
    "url"}], the first comment's body cut to THREAD_BODY. None when gh cannot say, never [] for a failure."""
    if not is_slug(slug) or not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    owner, name = slug.split("/", 1)
    try:
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={_THREADS}", "-f", f"owner={owner}",
                               "-f", f"name={name}", "-F", f"number={number}"], capture_output=True, text=True,
                              timeout=60)
        nodes = json.loads(proc.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if proc.returncode != 0 or not isinstance(nodes, list):
        return None
    out = []
    for t in nodes:
        if not isinstance(t, dict) or t.get("isResolved"):
            continue
        first = (((t.get("comments") or {}).get("nodes") or [{}])[0]) or {}
        author = first.get("author") or {}
        body = " ".join(str(first.get("body") or "").split())
        out.append({"id": t.get("id"), "author": author.get("login") or "", "bot": is_bot(author),
                    "path": t.get("path"), "line": t.get("line"), "outdated": bool(t.get("isOutdated")),
                    "body": body[:THREAD_BODY] + ("…" if len(body) > THREAD_BODY else ""), "url": first.get("url")})
    return out


def has_ci(slug):
    """Whether a repo runs GitHub Actions: a `.github/workflows` directory on its default branch. True, False (404),
    or None when gh cannot say (rate limit, network)."""
    if not is_slug(slug):
        return None
    try:
        proc = subprocess.run(["gh", "api", f"repos/{slug}/contents/.github/workflows", "--silent"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    return False if "404" in proc.stderr or "Not Found" in proc.stderr else None


def default_branch(slug):
    """A GitHub repo's default branch, or None when gh cannot say."""
    if not is_slug(slug):
        return None
    try:
        proc = subprocess.run(["gh", "api", f"repos/{slug}", "--jq", ".default_branch"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = proc.stdout.strip() if proc.returncode == 0 else ""
    return branch if re.fullmatch(r"[\w./-]{1,100}", branch) else None


def completed_runs(slug, branch, limit=100):
    """The latest completed workflow runs on `branch`, newest first: [{"id", "attempt", "workflow", "created",
    "conclusion"}], or None when gh cannot say (rate limit, network). A pull request's run is left out: its head
    branch can be a fork's `main`."""
    if not is_slug(slug):
        return None
    try:
        proc = subprocess.run(["gh", "run", "list", "-R", slug, "--branch", branch, "--status", "completed",
                               "--limit", str(limit), "--json",
                               "databaseId,attempt,event,workflowName,createdAt,conclusion"],
                              capture_output=True, text=True, timeout=30)
        rows = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return [{"id": r["databaseId"], "attempt": r["attempt"] if isinstance(r.get("attempt"), int) else 1,
             "workflow": str(r.get("workflowName") or ""), "created": r.get("createdAt"),
             "conclusion": str(r.get("conclusion") or "").lower()}
            for r in rows if isinstance(r, dict) and isinstance(r.get("databaseId"), int)
            and not str(r.get("event") or "").startswith("pull_request")]


_TITLE_MAX = 120


def _title(text) -> str:
    """A PR title as one line of data: whitespace folded, control characters dropped, capped."""
    text = " ".join("".join(c for c in str(text or "") if c.isprintable() or c.isspace()).split())
    return text if len(text) <= _TITLE_MAX else text[:_TITLE_MAX - 1].rstrip() + "…"


def review_requested(slugs, cap=10, last=None) -> list:
    """Open, non-draft PRs that ask the human (`@me`) by name for a review, one `gh search prs` per repo, at most `cap`
    in all. A PR that requests only one of the human's teams is not theirs to review, so it is left out.

    The qualifiers go to gh as separate words: one quoted string becomes `user-review-requested:"@me draft:false"`.
    Each links by the repo searched and its number, never by the URL GitHub returns. A repo whose search fails keeps
    its rows from `last`, the previous list.
    """
    out = []
    for slug in slugs:
        if len(out) >= cap or not is_slug(slug):
            continue
        try:
            proc = subprocess.run(["gh", "search", "prs", "user-review-requested:@me", "draft:false", "--state=open",
                                   "--repo", slug, "--json", "number,title,author,isDraft", "--limit", str(cap)],
                                  capture_output=True, text=True, timeout=30)
            items = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            items = None
        if not isinstance(items, list):
            out += [r for r in last or [] if isinstance(r, dict) and r.get("repo") == slug][:cap - len(out)]
            continue
        for item in items:
            n = item.get("number") if isinstance(item, dict) else None
            if isinstance(n, int) and n > 0 and not item.get("isDraft") and len(out) < cap:
                out.append({"repo": slug, "number": n, "title": _title(item.get("title")),
                            "author": ((item.get("author") or {}).get("login") or ""),
                            "url": f"https://github.com/{slug}/pull/{n}"})
    return out
