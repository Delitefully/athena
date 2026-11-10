"""PR following: what `gh` says, reduced to what HQ acts on, and whose move each PR waits on. A repo not on GitHub never reaches gh."""
import json
import re
import subprocess
import sys
import threading
import time

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, gitops, lab, ledger, store, world

SUMMARY = "follow PRs: checks, reviews, whose move, stacks and threads"
FIELDS = ("number,url,title,state,isDraft,reviewDecision,headRefOid,mergeable,statusCheckRollup,comments,reviews,"
          "reviewRequests,latestReviews,autoMergeRequest,mergeStateStatus,baseRefName,headRefName,author")
BOTS = {"github-actions", "github-advanced-security", "copilot", "linear-code"}
FETCH_EVERY = 120
RATE_BACKOFF = 900
_latest = {}
_fetcher = None
last_error = None


class GhError(Exception):
    pass


def gh(*args, timeout=30) -> str:
    try:
        proc = world.run("gh", *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhError(str(exc))
    if proc.returncode:
        raise GhError((proc.stderr or proc.stdout).strip()[:300])
    return proc.stdout


def is_bot(author) -> bool:
    author = author if isinstance(author, dict) else {}
    login = str(author.get("login") or "").lower()
    return (login in BOTS or login in (config.rex("bots") or []) or login.startswith("copilot-") or login.endswith("[bot]")
            or author.get("is_bot") is True)


_FAILED = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
_WAITING = ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED")
_QUEUE_CHECK = re.compile(r"^merge[ _-]?queue$", re.I)


def reduce_checks(rollup) -> str:
    """success, failure, pending or none, in the old athena's words: the watch's `checks` lines and watch.json carry them."""
    states = [str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in rollup or []]
    if not states:
        return "none"
    if any(s in _FAILED for s in states):
        return "failure"
    if any(s in _WAITING or s == "" for s in states):
        return "pending"
    return "success"


def _queued(data) -> bool:
    """In a merge queue: GitHub's own (`isInMergeQueue`), or a bot queue's pending `merge-queue` check."""
    if data.get("isInMergeQueue"):
        return True
    for check in data.get("statusCheckRollup") or []:
        state = str(check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        if _QUEUE_CHECK.match(check.get("name") or check.get("context") or "") and state in _WAITING:
            return True
    return False


def _teammate_reviewed_at(data) -> str:
    """When a teammate last reviewed the PR (ISO time), or "": never the author's own review, a bot's or an account with no association."""
    author = str((data.get("author") or {}).get("login") or "").lower()
    times = []
    for r in data.get("latestReviews") or []:
        who = (r or {}).get("author") or {}
        login = str(who.get("login") or "").lower()
        if not login or login == author or is_bot(who) or r.get("authorAssociation") == "NONE":
            continue
        if isinstance(r.get("submittedAt"), str) and r["submittedAt"]:
            times.append(r["submittedAt"])
    return max(times, default="")


def reduce(data: dict) -> dict:
    owner = str((data.get("author") or {}).get("login") or "").lower()
    humans = [c for c in list(data.get("comments") or []) + [r for r in data.get("reviews") or [] if r.get("body")]
              if not is_bot(c.get("author")) and str((c.get("author") or {}).get("login") or "").lower() != owner]
    return {"number": data.get("number"), "url": data.get("url"), "title": data.get("title"), "state": data.get("state"),
            "draft": bool(data.get("isDraft")), "review": data.get("reviewDecision") or "",
            "head": data.get("headRefOid"), "mergeable": data.get("mergeable"),
            "checks": reduce_checks(data.get("statusCheckRollup")), "comments": len(humans),
            "all_comments": len(data.get("comments") or []) + len(data.get("reviews") or []),
            "requested": [n for n in ((r.get("name") or r.get("login") or r.get("slug") or "").strip()
                                      for r in data.get("reviewRequests") or []) if n],
            "reviews": {r["author"]["login"]: r.get("state") for r in data.get("latestReviews") or []
                        if (r.get("author") or {}).get("login")},
            "teammate_reviewed_at": _teammate_reviewed_at(data), "queued": _queued(data), "author": owner,
            "auto_merge": bool(data.get("autoMergeRequest")), "base": data.get("baseRefName"),
            "branch": data.get("headRefName"), "merge_state": data.get("mergeStateStatus")}


def summary(repo, branch):
    """The PR of a branch, or None. Never raises: a missing or broken gh, or a non-GitHub repo, means no PR known."""
    try:
        slug = gitops.github_slug(repo)
        if not slug:
            return None
        found = json.loads(gh("pr", "view", branch, "--repo", slug, "--json", FIELDS) or "null")
        return {**reduce(found), "slug": slug} if found else None
    except GhError as exc:
        global last_error
        if "no pull requests found" not in str(exc).lower():
            last_error = str(exc)
        return None
    except (ValueError, gitops.GitError, lab.Refused):
        return None


def same_head(a, b) -> bool:
    """Two shas name one commit: one is a prefix of the other, at least 7 hex digits, any case."""
    a, b = str(a or "").lower(), str(b or "").lower()
    if min(len(a), len(b)) < 7 or not re.fullmatch(r"[0-9a-f]+", a + b):
        return False
    return a.startswith(b) or b.startswith(a)


SECTION = {"approved": "you", "mergeable": "you", "unrequested": "you", "awaiting": "awaiting"}
WHO = {"working": "worker", "draft": "worker", "failing": "ci", "pending": "ci", "changes": "worker", "queued": "nobody", "reviewing": "athena",
       "awaiting": "teammate", "unrequested": "you", "approved": "you", "mergeable": "you", "merged": "nobody", "closed": "nobody"}


def move(p: dict, live: bool, human_merge: bool, review=None, reported=False, ci=None, posted=None) -> dict:
    """Whose move a PR waits on, as the old athena's pr.move says it: {label, kind, needs, human_merge}, with who, what and section
    for this dashboard. A merge is the human's only once checks are green (or the repo runs no CI, `ci` False), and a live worker's PR
    only once it reported DONE and HQ's review passed at its head. `posted`: when it was posted in #open-prs; a PR with no reviewer
    requested waits on a teammate from there, until one reviews it after the post."""
    def out(label, kind, needs):
        section = "you" if needs else SECTION.get(kind, "flight") if kind in ("awaiting",) else "flight"
        if kind in ("merged", "closed"):
            section = "done"
        return {"label": label, "kind": kind, "needs": needs, "human_merge": human_merge, "who": "you" if needs else WHO.get(kind, "nobody"),
                "what": label, "section": section}
    if p.get("state") == "MERGED":
        return out("merged", "merged", False)
    if p.get("state") == "CLOSED":
        return out("closed", "closed", False)

    def starting():
        return out("checks starting" if p.get("checks") == "none" else "checks running", "pending", False)

    def athena_review():
        verdict = review if isinstance(review, dict) else {}
        current = same_head(verdict.get("head"), p.get("head"))
        if current and verdict.get("verdict") == "pass":
            return None
        if current and verdict.get("verdict") == "fail":
            return out("review findings · worker fixing", "changes", False)
        return out("in review · athena", "reviewing", False)

    def gate():
        """A live worker's PR shows its worker's own state until it reported DONE, then HQ's review until that passed."""
        if not live:
            return None
        if not reported:
            return out("working", "working", False)
        return athena_review()

    requested = list(p.get("requested") or [])
    green = p.get("checks") == "success" or p.get("checks") == "none" and ci is False
    if p.get("draft"):
        return out("draft", "draft", False)
    if p.get("checks") == "failure":
        return out("checks failing", "failing", not live)
    if p.get("review") == "CHANGES_REQUESTED":
        asked = [who for who, state in (p.get("reviews") or {}).items() if state == "CHANGES_REQUESTED"]
        if not (requested and asked and all(who in requested for who in asked)):
            return out("changes requested · worker fixing", "changes", False) if live else out("changes requested", "changes", True)
    elif p.get("review") == "APPROVED":
        if p.get("queued"):
            return out("approved · in merge queue", "queued", False)
        if p.get("auto_merge"):
            return out("approved · auto-merge on", "queued", False)
        if not green:
            return starting()
        return gate() or out("approved · yours to merge" if human_merge else "approved · your go to merge", "approved", True)
    if requested:
        shown = ", ".join(requested[:3]) + (f" +{len(requested) - 3}" if len(requested) > 3 else "")
        return out("awaiting review · " + shown, "awaiting", False)
    if p.get("review") == "REVIEW_REQUIRED":
        held = gate()
        if held:
            return held
        try:
            counts = posted is not None and _posted_counts(posted, p.get("teammate_reviewed_at"))
        except ValueError:
            counts = False
        if counts:
            return out("posted in #open-prs", "awaiting", False)
        return out("no reviewer requested", "unrequested", True)
    if p.get("queued"):
        return out("in merge queue", "queued", False)
    if p.get("auto_merge"):
        return out("auto-merge on", "queued", False)
    if not green:
        return starting()
    return gate() or out("yours to merge" if human_merge else "your go to merge", "mergeable", True)


def _posted_counts(posted, reviewed_at) -> bool:
    """A post in #open-prs counts until a teammate reviews the PR after it."""
    if not reviewed_at:
        return True
    from datetime import datetime
    return float(posted) > datetime.fromisoformat(str(reviewed_at).replace("Z", "+00:00")).timestamp()


def posted() -> dict:
    """open-prs-posted.json, `{"owner/name#n": "<ts>"}`, as the old athena and its dash read it; {} when unreadable."""
    data = store.read_dict(store.path("open-prs-posted.json"))
    out = {}
    for key, value in data.items():
        if isinstance(key, str) and re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", key) and isinstance(value, (str, int, float)) \
                and not isinstance(value, bool):
            try:
                out[key.lower()] = str(float(value) and value)
            except ValueError:
                continue
    return out


_CI = {}
CI_TTL = 24 * 3600


def has_ci(slug, wait=False):
    """Whether a repo runs GitHub Actions, asked at most once a day; None when GitHub can't say, which waits for checks. The ask runs on
    PR fetch's own thread (`wait`), so the watch's loop never waits on GitHub: there, the last answer (or None) stands."""
    known, asked = _CI.get(slug, (None, None))
    if asked is not None and time.time() - asked < CI_TTL:
        return known
    return _ask_ci(slug) if wait else known


def _ask_ci(slug):
    known, _ = _CI.get(slug, (None, None))
    try:
        gh("api", f"repos/{slug}/contents/.github/workflows", "--silent")
        found = True
    except GhError as exc:
        found = False if "404" in str(exc) or "Not Found" in str(exc) else None
    if found is None:  # a failed ask keeps the last answer for a day; never known, it asks again next time
        if slug in _CI:
            _CI[slug] = (known, time.time())
        return known
    _CI[slug] = (found, time.time())
    return found


def stacks() -> list:
    return store.read_dict(store.path("stack.json")).get("stacks") or []


def set_stack(repo, prs) -> list:
    data = [s for s in stacks() if s.get("repo") != repo]
    if prs:
        data.append({"repo": repo, "prs": [int(p) for p in prs]})
    store.write_json(store.path("stack.json"), {"stacks": data})
    return data


def threads(slug, number) -> list:
    owner, name = slug.split("/", 1)
    query = ("query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){reviewThreads(first:100)"
             "{nodes{id isResolved isOutdated path line comments(first:1){nodes{author{login} body url}}}}}}}")
    out = json.loads(gh("api", "graphql", "-f", f"query={query}", "-F", f"o={owner}", "-F", f"n={name}", "-F", f"p={number}"))
    rows = []
    for node in out["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        if node.get("isResolved"):
            continue
        first = (node.get("comments", {}).get("nodes") or [{}])[0]
        rows.append({"id": node["id"], "author": (first.get("author") or {}).get("login"),
                     "bot": is_bot(first.get("author")), "path": node.get("path"), "line": node.get("line"),
                     "outdated": node.get("isOutdated"), "body": (first.get("body") or "")[:300], "url": first.get("url")})
    return rows


def _pr_ref(text):
    found = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", text) or re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", text)
    return (found.group(1), int(found.group(2))) if found else None


def _kind_of(error):
    """What a failed `gh` means, since each one wants something else from a person."""
    text = str(error).lower()
    if "rate limit" in text:
        return "rate limit: backing off"
    if "auth login" in text or "http 401" in text or "not logged" in text:
        return "sign in with gh auth login: its session has expired"
    return f"cannot reach GitHub: {str(error)[:120]}"


def _fetch_all(rows):
    global last_error
    last_error = None
    for r in rows:
        if r.get("repo") and r.get("branch"):
            found = summary(r["repo"], r["branch"])
            if found and found.get("checks") == "none" and found.get("state") == "OPEN" and found.get("slug"):
                has_ci(found["slug"], wait=True)
            if found:
                _latest[r["name"]] = {**found, "fetched": time.time()}
    tick_file = store.path("run", "prs-tick.json")
    if last_error and "rate limit" in last_error.lower():
        store.write_json(tick_file, {"ts": time.time() + RATE_BACKOFF, "why": "rate limit"})
    elif float(store.read_dict(tick_file).get("ts") or 0) > time.time():
        store.write_json(tick_file, {"ts": time.time()})
    store.write_json(_source(), {"ts": time.time(), "trouble": _kind_of(last_error) if last_error else None})


def _source():
    return store.path("run", "github.json")


def checks(ctx):
    """GitHub's own state: slow or out of quota never reads as a dead watch, and each kind of trouble says what it wants."""
    known = store.read_dict(_source())
    trouble = known.get("trouble")
    return [caps.Check("github", not trouble, trouble or "ok")]


def refresh(rows, wait=False):
    """On its own thread, one at a time: a slow GitHub never holds up the watch's loop."""
    global _fetcher
    if _fetcher and _fetcher.is_alive():
        return
    _fetcher = threading.Thread(target=_fetch_all, args=(rows,), daemon=True)
    _fetcher.start()
    if wait:
        _fetcher.join()


def snapshot_workers(rows, fetch: bool, prev: dict, wait=False) -> dict:
    """The `pr` for each live worker row: the fetcher's newest, else last seen. Each carries when it was fetched."""
    if fetch:
        refresh(rows, wait)
    out = {}
    for r in rows:
        known = _latest.get(r["name"]) or (prev.get(r["name"]) or {}).get("pr")
        out[r["name"]] = None if known and known.get("branch") not in (None, r.get("branch")) else known  # a name is reused; a branch isn't this life's
        if out[r["name"]]:
            out[r["name"]]["move"] = move_of(out[r["name"]], True, r.get("repo") or "", r.get("athena_review"),
                                             str((r.get("report") or {}).get("status") or "").upper().startswith("DONE"))
    return out


def move_of(p, live, repo, review=None, reported=False) -> dict:
    """`move` with what it reads from config, GitHub and open-prs-posted.json filled in."""
    slug = p.get("slug") or (_pr_ref(p.get("url") or "") or ("",))[0]
    human = config.listed("human_merge_repos", repo) or bool(slug and config.listed("human_merge_repos", slug))
    ci = has_ci(slug) if p.get("checks") == "none" and slug and p.get("state") == "OPEN" else None
    key = f"{slug}#{p.get('number')}".lower() if slug else ""
    return move(p, live, human, review, reported, ci, posted().get(key))


def fetch_due(now) -> bool:
    state = store.read_dict(store.path("run", "prs-tick.json"))
    return now - float(state.get("ts") or 0) >= FETCH_EVERY


def _pr_cfg(p):
    p.add_argument("name")


def _pr(args):
    worker = ledger.get(args.name)
    if not worker:
        raise UserError(f"no worker named {args.name}")
    print(json.dumps(summary(worker.get("repo"), worker.get("branch")), indent=1))
    return 0


def _threads_cfg(p):
    p.add_argument("target", help="a worker, owner/repo#n or a PR URL")


def _threads(args):
    ref = _pr_ref(args.target)
    if not ref:
        worker = ledger.get(args.target) or {}
        found = summary(worker.get("repo"), worker.get("branch")) if worker else None
        if not found:
            print("GitHub cannot say")
            return 2
        ref = (found["slug"], found["number"])
    try:
        print(json.dumps(threads(*ref), indent=1))
    except (GhError, KeyError, ValueError) as exc:
        print(f"GitHub cannot say: {exc}")
        return 2
    return 0


def _stack_cfg(p):
    p.add_argument("action", nargs="?", choices=["set", "clear", "show"], default="show")
    p.add_argument("repo", nargs="?")
    p.add_argument("prs", nargs="*")


def _stack(args):
    if args.action in ("set", "clear"):
        if not args.repo:
            raise UserError("name the owner/repo")
        set_stack(args.repo, args.prs if args.action == "set" else [])
    print(json.dumps(stacks(), indent=1))
    return 0


def _openprs_cfg(p):
    p.add_argument("action", nargs="?", choices=["show", "add", "check"], default="show")
    p.add_argument("prs", nargs="*", help="owner/name#n or a PR's GitHub URL. add: posted just now. check: exit 0 when its post still "
                                          "counts (no teammate review since), 1 when a post is wanted, 2 when GitHub cannot say")


def _openprs_check(given) -> int:
    """0: the post still counts, so post nothing; 1: post it; 2: can't tell, so post nothing and tell the human. Every error is 2."""
    ref = _pr_ref(given[0]) if len(given) == 1 else None
    if not ref:
        print("athena-rex open-prs check <owner/name#n|PR URL>", file=sys.stderr)
        return 2
    key = f"{ref[0]}#{ref[1]}".lower()
    try:
        found = json.loads(gh("pr", "view", str(ref[1]), "--repo", ref[0], "--json", FIELDS) or "null")
        if not isinstance(found, dict):
            raise GhError("gh found no PR")
        reviewed, at = reduce(found).get("teammate_reviewed_at") or "", posted().get(key)
        counts = at is not None and _posted_counts(at, reviewed)
    except Exception as exc:
        print(f"athena-rex open-prs check: cannot tell whether {key}'s post still counts ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print(json.dumps({"pr": key, "posted": at, "teammate_reviewed_at": reviewed or None, "counts": counts}, indent=2))
    return 0 if counts else 1


def _openprs(args):
    if args.action == "check":
        return _openprs_check(args.prs)
    target = store.path("open-prs-posted.json")
    if args.action == "add":
        if not config.get("open_prs_channel"):
            print("athena-rex open-prs add: no open_prs_channel in config.json, so no post to record", file=sys.stderr)
            return 2
        keys = []
        for pr in args.prs:
            ref = _pr_ref(pr)
            if not ref:
                print(f"athena-rex open-prs add: not a PR: {pr!r} (owner/name#n or its GitHub URL)", file=sys.stderr)
                return 2
            keys.append(f"{ref[0]}#{ref[1]}".lower())
        if not keys:
            print("athena-rex open-prs add <owner/name#n|PR URL> ...", file=sys.stderr)
            return 2
        with store.locked("locks/open-prs.lock"):
            data = store.read_dict(target)
            for key in keys:
                data[key] = f"{time.time():.6f}"
            store.write_json(target, data, indent=2)
    print(json.dumps(posted(), indent=2))
    return 0


def commands():
    return {"pr": caps.Command("a worker's PR", _pr_cfg, _pr),
            "threads": caps.Command("a PR's unresolved review threads", _threads_cfg, _threads),
            "stack": caps.Command("the stacked-PR chains watch follows", _stack_cfg, _stack),
            "open-prs": caps.Command("the PRs posted for review", _openprs_cfg, _openprs)}
