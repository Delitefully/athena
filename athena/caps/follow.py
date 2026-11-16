"""What the old athena's watch followed beyond the workers' own PRs: stacked chains, retired workers' open PRs, PRs asking the human
for a review, the CI doorbell on each repo's default branch, and main moving. Fetched every two minutes on its own thread, so a slow
GitHub never holds up the watch's loop; its lines go out on the next loop after the fetch ends.

Lines, as the old athena wrote them:
    stack <owner/repo>#<n>: <before> -> <after>         a stacked PR's state, base, draft and mergeability changed
    <repo> main ci failed <workflow> <run-id>[ (attempt <n>)]   one incident per workflow, told once (ci-failed.json), and only for
                                                         a run on our own commit (`is_ours`); any other goes to the suppressed log
    <repo> main ci recovered <workflow>                  routine: the incident closed
    <repo> main moved <old>..<new>                       told by config watch_main_moved: never, conflicts (default), active, always
    <w> bot threads +<n> #<pr>                           new unresolved review threads a bot started on a live worker's PR
    <owner/repo>#<pr> bot threads +<n>                   the same on a followed PR no live worker owns (stacked, a retired worker's)
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from athena.kernel import caps, config, gitops, ledger, store

SUMMARY = "follow stacks, retired workers' PRs, review requests, CI on main and main moving"
FETCH_EVERY = 120
FOLLOW_RETIRED = 14 * 86400
CI_KEEP = 200
CI_FRESH = 86400
CI_FAILED = ("failure", "timed_out", "startup_failure")
CI_PASSED = ("success",)
MAIN_MOVED = ("never", "conflicts", "active", "always")
REVIEW_CAP = 10
BOT_REMIND = 1800
OURS_KEEP = 2000
_SHA = re.compile(r"[0-9a-f]{7,40}")
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")

_lock = threading.Lock()
_fetcher = None
_ready = []  # lines a finished fetch left for the next loop
wait_for_fetch = False  # a one-pass watch waits for its fetch, so `watch --once` tells what it found


def _file():
    return store.path("run", "follow.json")


def _ci_file():
    return store.path("ci-failed.json")


def is_slug(text) -> bool:
    return isinstance(text, str) and _SLUG.fullmatch(text) is not None


def pr_key(url):
    found = _PR_URL.search(str(url or ""))
    return f"{found.group(1)}#{found.group(2)}" if found else None


def known() -> dict:
    """What the last fetch found: {stacks, prs, review_requests, mains, ts}; the dash and the watch.json snapshot read it."""
    return store.read_dict(_file())


def _gh_json(prs, *args):
    try:
        return json.loads(prs.gh(*args) or "null")
    except (prs.GhError, ValueError):
        return None


def fetch_pr(prs, slug, number):
    found = _gh_json(prs, "pr", "view", str(number), "--repo", slug, "--json", prs.FIELDS)
    return {**prs.reduce(found), "slug": slug} if isinstance(found, dict) else None


def stack_state(data) -> str:
    return f"{data.get('state')} base={data.get('base')}{' DRAFT' if data.get('draft') else ''} {data.get('mergeable')}"


def _stack_keys(prs) -> list:
    return [(chain.get("repo"), int(n)) for chain in prs.stacks() for n in chain.get("prs") or [] if is_slug(chain.get("repo"))]


def retired_open(now=None) -> dict:
    """`owner/name#n` -> (worker, its repo) for each worker retired in the last two weeks whose PR was open then."""
    now = time.time() if now is None else now
    last = {}
    for entry in store.read_lines(store.path("history.jsonl")):
        if entry.get("name"):
            last[entry["name"]] = entry
    out = {}
    for name, entry in last.items():
        pr = entry.get("pr") if isinstance(entry.get("pr"), dict) else {}
        key = pr_key(pr.get("url"))
        live = (ledger.get(name) or {}).get("state") == "live"
        if key and pr.get("state") == "OPEN" and now - float(entry.get("ts") or 0) <= FOLLOW_RETIRED and not live:
            out[key] = (name, entry.get("repo"))
    return out


def review_requested(prs, slugs, last=None) -> list:
    """Open, non-draft PRs asking the human by name for a review, one search per repo, at most REVIEW_CAP. A repo whose search fails
    keeps its rows from `last`."""
    out = []
    for slug in slugs:
        if len(out) >= REVIEW_CAP or not is_slug(slug):
            continue
        found = _gh_json(prs, "search", "prs", "user-review-requested:@me", "draft:false", "--state=open", "--repo", slug,
                         "--json", "number,title,author,isDraft", "--limit", str(REVIEW_CAP))
        if not isinstance(found, list):
            out += [r for r in last or [] if isinstance(r, dict) and r.get("repo") == slug][:REVIEW_CAP - len(out)]
            continue
        for item in found:
            n = item.get("number") if isinstance(item, dict) else None
            if isinstance(n, int) and n > 0 and not item.get("isDraft") and len(out) < REVIEW_CAP:
                title = " ".join(str(item.get("title") or "").split())[:120]
                out.append({"repo": slug, "number": n, "title": title, "author": (item.get("author") or {}).get("login") or "",
                            "url": f"https://github.com/{slug}/pull/{n}"})
    return out


def _age(created) -> float:
    try:
        when = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return time.time() - when.timestamp()


def completed_runs(prs, slug, branch):
    rows = _gh_json(prs, "run", "list", "-R", slug, "--branch", branch, "--status", "completed", "--limit", "100", "--json",
                    "databaseId,attempt,event,headSha,workflowName,createdAt,conclusion")
    if not isinstance(rows, list):
        return None
    return [{"id": r["databaseId"], "attempt": r["attempt"] if isinstance(r.get("attempt"), int) else 1,
             "workflow": str(r.get("workflowName") or ""), "created": r.get("createdAt"), "conclusion": str(r.get("conclusion") or "").lower(),
             "event": str(r.get("event") or ""), "sha": str(r.get("headSha") or "")}
            for r in rows if isinstance(r, dict) and isinstance(r.get("databaseId"), int)
            and not str(r.get("event") or "").startswith("pull_request")]


def ci_targets(repos) -> list:
    """The live workers' repos (paths), then config `ci_repos` (a path or owner/name), each once."""
    out, seen = [], set()
    for entry in list(repos) + [e for e in config.get("ci_repos") or [] if isinstance(e, str)]:
        if entry.startswith(("/", "~")):
            entry = os.path.expanduser(entry)
            slug = gitops.github_slug(entry) if os.path.isdir(entry) else None
        elif is_slug(entry):
            slug = entry
        else:
            continue
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            out.append((entry, slug))
    return out


_BRANCHES = {}


def _default_branch(prs, entry, slug):
    """A repo's default branch: its checkout's, or GitHub's, asked once a day; a failed ask keeps the last answer."""
    if not entry.startswith("/"):
        branch, asked = _BRANCHES.get(slug.lower(), (None, None))
        if asked is not None and time.time() - asked < 86400:
            return branch
        found = _ask_default_branch(prs, slug)
        if found:  # a failed ask is never kept: the next fetch asks again
            _BRANCHES[slug.lower()] = (found, time.time())
        return found or branch
    return _ask_default_branch(prs, slug, entry)


def _ask_default_branch(prs, slug, entry=""):
    if entry.startswith("/"):
        try:
            return gitops.default_branch(entry)
        except Exception:
            return None
    found = _gh_json(prs, "repo", "view", slug, "--json", "defaultBranchRef")
    return ((found or {}).get("defaultBranchRef") or {}).get("name") if isinstance(found, dict) else None


_ME = {}  # the gh user's login and verified emails, asked once; a failed ask is never kept
_OURS = {}  # (slug, sha) -> whether that commit is ours; only a decided answer is kept
_GONE = set()  # (slug, sha) GitHub has no such commit for: not ours, and said so in the log
_EMAILS = set()  # the human's own commit emails: gh's verified ones and git's user.email in the followed checkouts


def _gh_api(prs, *args, answers=()):
    """(what `gh api` answered, whether GitHub said there is no such thing): a 404 or 422 is an answer (and any of `answers` in the
    error), any other error is not."""
    try:
        return json.loads(prs.gh("api", *args) or "null"), False
    except prs.GhError as exc:
        return None, any(code in str(exc) for code in ("HTTP 404", "HTTP 422", "Not Found", "No commit found", *answers))
    except ValueError:
        return None, False


def gh_login(prs):
    if "login" not in _ME:
        found = _gh_json(prs, "api", "user", "--jq", "{login: .login}")
        login = found.get("login") if isinstance(found, dict) else None
        if not isinstance(login, str) or not login:
            return None
        _ME["login"] = login
    return _ME["login"]


def our_emails(prs, targets) -> set:
    """The emails the human commits as: the gh user's verified ones (asked once; a token without the scope has none) and git's
    user.email in each followed repo's checkout, lower-cased. A push under an email GitHub links to no login is still ours."""
    if "emails" not in _ME:
        found, missing = _gh_api(prs, "user/emails", "--jq", "[.[] | select(.verified) | .email]", answers=("scope",))
        if isinstance(found, list) or missing:  # a token without the user scope has no emails, and is not asked again
            _ME["emails"] = {e.lower() for e in found or [] if isinstance(e, str)}
    out = set(_ME.get("emails") or ())
    for entry, _ in targets:
        if entry.startswith("/") and os.path.isdir(entry):
            try:
                email = gitops.git(entry, "config", "--get", "user.email", check=False, timeout=10)
            except Exception:
                continue
            if "@" in email:
                out.add(email.strip().lower())
    return out


def worker_branches() -> set:
    """Every branch an athena worker has had: the ledger's, retired workers' included, and the retired workers' history."""
    found = {e.get("branch") for e in ledger.events()} | {e.get("branch") for e in store.read_lines(store.path("history.jsonl"))}
    return {b for b in found if isinstance(b, str) and b}


def is_ours(prs, slug, sha):
    """Whether a commit on a repo's default branch is ours: the gh user authored or committed it, under their login or one of the human's
    emails (`our_emails`), or it is the merge of a PR the gh user opened or an athena worker's branch. None when GitHub can't say, so
    the next fetch asks again; an answer is kept per sha, and a commit GitHub has none of (404) is not ours, kept as well."""
    if not _SHA.fullmatch(sha or ""):
        return False
    key = (slug.lower(), sha)
    if key in _OURS:
        return _OURS[key]
    me = gh_login(prs)
    if not me:
        return None
    commit, missing = _gh_api(prs, f"repos/{slug}/commits/{sha}", "--jq", "{author: .author.login, committer: .committer.login, "
                              "author_email: .commit.author.email, committer_email: .commit.committer.email}")
    if missing:
        _GONE.add(key)
        return _keep(key, False)
    if not isinstance(commit, dict):
        return None
    ours = (me.lower() in {str(commit.get(k) or "").lower() for k in ("author", "committer")}
            or bool(_EMAILS & {str(commit.get(k) or "").lower() for k in ("author_email", "committer_email")}))
    if not ours:
        pulls, missing = _gh_api(prs, f"repos/{slug}/commits/{sha}/pulls", "--jq",
                                 "[.[] | {user: .user.login, branch: .head.ref, merged: .merge_commit_sha}]")
        if missing:
            pulls = []
        if not isinstance(pulls, list):
            return None
        branches = worker_branches()
        ours = any(isinstance(p, dict) and p.get("merged") == sha and (str(p.get("user") or "").lower() == me.lower() or p.get("branch") in branches)
                   for p in pulls)  # only the PR this commit merged: a PR that merely contains it is not
    return _keep(key, ours)


def _keep(key, ours):
    if len(_OURS) >= OURS_KEEP:
        _OURS.clear()
        _GONE.clear()
    _OURS[key] = ours
    return ours


def run_is_ours(prs, slug, run):
    """(ours, why not) for a failed main-CI run, or None when GitHub can't say yet. A scheduled run is never ours, a dispatched one is
    when the gh user dispatched it, any other when its head commit is ours."""
    if run.get("event") == "schedule":
        return False, "not our commit: a scheduled run"
    if run.get("event") == "workflow_dispatch":
        me = gh_login(prs)
        found, missing = _gh_api(prs, f"repos/{slug}/actions/runs/{run['id']}", "--jq", "{actor: .triggering_actor.login}") if me else (None, False)
        if missing:
            return False, "not our commit: GitHub has no such run"
        if not isinstance(found, dict):
            return None
        ours = str(found.get("actor") or "").lower() == me.lower()
        return ours, "" if ours else "not our commit: dispatched by someone else"
    ours = is_ours(prs, slug, run.get("sha"))
    if ours is None:
        return None
    if (slug.lower(), run.get("sha")) in _GONE:
        return False, "not our commit: GitHub has no such commit"
    return ours, "" if ours else "not our commit"


def _opening_run(prs, slug, runs, run_id):
    """An incident's opening run, from this fetch's runs or, aged out of them, from GitHub."""
    for r in runs:
        if r["id"] == run_id:
            return r
    found, missing = _gh_api(prs, f"repos/{slug}/actions/runs/{run_id}", "--jq", "{event: .event, sha: .head_sha}")
    if missing:  # gone from GitHub: its commit can't be ours, so the incident closes instead of asking every fetch
        return {"id": run_id, "event": "", "sha": ""}
    return {"id": run_id, "event": str(found.get("event") or ""), "sha": str(found.get("sha") or "")} if isinstance(found, dict) else None


def ci_failures(prs, targets, suppress) -> list:
    """One incident per failing workflow on each repo's default branch, as the old athena keeps them in ci-failed.json: the first
    failed run opens it and is told; more failures while it is open are counted, not told; a rerun of the opening run that fails again
    is told with its attempt; the newest run passing closes it (`recovered`). A repo seen for the first time records, tells nothing.
    Only a run on our own commit counts (`run_is_ours`): any other is logged as suppressed, opens nothing, and an open incident that
    turns out not to be ours is closed without a word, so the lights, which read ci-failed.json, drop its red."""
    from athena.caps.watch import safe_workflow
    seen = store.read_dict(_ci_file())
    lines, changed = [], False
    _EMAILS.clear()
    _EMAILS.update(our_emails(prs, targets) if targets else ())
    for entry, slug in targets:
        branch = _default_branch(prs, entry, slug)
        runs = completed_runs(prs, slug, branch) if branch else None
        if runs is None:
            continue
        name, key = os.path.basename(entry.rstrip("/")), slug.lower()
        state = seen.get(key) if isinstance(seen.get(key), dict) else {}
        first = key not in seen
        known_runs = [str(k) for k in state.get("runs") or []]
        incidents = {k: v for k, v in (state.get("incidents") or {}).items() if isinstance(v, dict)}
        newest = {}
        for r in runs:
            newest.setdefault(safe_workflow(r["workflow"]), r)
        failed = [r for r in runs if r["conclusion"] in CI_FAILED]
        if first:  # told nothing; a run GitHub can't say about yet opens its incident unconfirmed, and the next fetch asks again
            opened = {}
            for wf, r in newest.items():
                verdict = run_is_ours(prs, slug, r) if r["conclusion"] in CI_FAILED else (False,)
                if verdict is None or verdict[0]:
                    opened[wf] = {"run": r["id"], "opened": r.get("created"), "runs": 1, **({"ours": True} if verdict else {})}
            seen[key] = {"runs": [f"{r['id']}.{r['attempt']}" for r in failed][:CI_KEEP], "incidents": opened}
            changed = True
            continue
        for wf, open_ in list(incidents.items()):
            if open_.get("ours"):
                continue
            opening = _opening_run(prs, slug, runs, open_.get("run"))
            verdict = run_is_ours(prs, slug, opening) if opening else None
            if verdict is None:
                continue
            changed = True
            if verdict[0]:
                open_["ours"] = True
            else:
                del incidents[wf]
                suppress(f"{name} main ci failed {wf} {open_.get('run')}", f"{verdict[1]}: its open incident closed")
        for r in reversed(failed):
            run_key = f"{r['id']}.{r['attempt']}"
            if run_key in known_runs:
                continue
            wf = safe_workflow(r["workflow"])
            line = f"{name} main ci failed {wf} {r['id']}"
            verdict = run_is_ours(prs, slug, r)
            if verdict is None:  # GitHub can't say yet: the next fetch asks again
                continue
            known_runs.append(run_key)
            changed = True
            if not verdict[0]:
                suppress(line, verdict[1])
                continue
            open_ = incidents.get(wf)
            if open_ is None:
                incidents[wf] = {"run": r["id"], "opened": r.get("created"), "runs": 1, "ours": True}
                if _age(r["created"]) <= CI_FRESH:
                    lines.append(line)
            elif r["id"] == open_.get("run") and r["attempt"] > 1:
                open_["runs"] = int(open_.get("runs") or 1) + 1
                if _age(r["created"]) <= CI_FRESH:
                    lines.append(f"{line} (attempt {r['attempt']})")
            else:
                open_["runs"] = int(open_.get("runs") or 1) + 1
                suppress(line, f"CI incident open since run {open_.get('run')}: {open_['runs']} failed runs")
        for wf in list(incidents):
            r = newest.get(wf)
            if r and r["conclusion"] in CI_PASSED:
                del incidents[wf]
                changed = True
                lines.append(f"{name} main ci recovered {wf}")
        seen[key] = {"runs": known_runs[-CI_KEEP:], "incidents": incidents}
    if changed:
        store.write_json(_ci_file(), seen, indent=1)
    return lines


def main_moved_mode() -> str:
    mode = config.get("watch_main_moved")
    return mode if mode in MAIN_MOVED else "conflicts"


def _repo_active(name, rows, pr_of) -> bool:
    """A live worker in repo `name` works or is blocked, or has an open PR not yet approved."""
    for r in rows:
        if os.path.basename(str(r.get("repo") or "")) != name:
            continue
        p = pr_of.get(r["name"]) or {}
        if r.get("state") in ("working", "blocked") or p.get("state") == "OPEN" and p.get("review") != "APPROVED":
            return True
    return False


def fetch(ctx, rows, pr_of, prev, suppress) -> tuple:
    """One fetch: (what it found, its lines with their kinds)."""
    prs = ctx.cap("prs")
    if not prs:
        return {}, []
    from athena.caps.watch import Line
    lines = []
    stacked = {f"{r}#{n}" for r, n in _stack_keys(prs)}
    retired = retired_open()
    owned = {pr_key((p or {}).get("url")) for p in pr_of.values()}
    fetched = {}
    for key in sorted(stacked | set(retired)):
        slug, _, n = key.rpartition("#")
        found = fetch_pr(prs, slug, int(n))
        if found:
            fetched[key] = found
    stacks = {}
    for key in sorted(stacked):
        data = fetched.get(key)
        before = (prev.get("stacks") or {}).get(key)
        if not data or data.get("mergeable") == "UNKNOWN" and data.get("state") == "OPEN":
            if before:
                stacks[key] = before
            continue
        stacks[key] = stack_state(data)
        if before and before != stacks[key]:
            merged_or_conflict = data.get("state") == "MERGED" or data.get("mergeable") == "CONFLICTING" and "CONFLICTING" not in before
            lines.append(Line(f"stack {key}: {before} -> {stacks[key]}", "wake" if merged_or_conflict else "routine"))
    followed = {}
    for key in sorted(stacked | set(retired)):
        if key in owned:
            continue
        data = fetched.get(key) or (prev.get("prs") or {}).get(key)
        if not data:
            continue
        owner, repo = retired.get(key, (None, None))
        if data.get("checks") == "none" and data.get("state") == "OPEN":
            prs.has_ci(key.rpartition("#")[0], wait=True)
        followed[key] = {**data, "owner": owner, "move": prs.move_of(data, False, repo or key.rpartition("#")[0])}
    slugs = [s for s in dict.fromkeys([(p or {}).get("slug") for p in pr_of.values()] + [_slug_of(w.get("repo")) for w in
                                       ledger.workers().values() if w.get("repo")] + [c.get("repo") for c in prs.stacks()]) if is_slug(s)]
    asking = [r for r in review_requested(prs, slugs, prev.get("review_requests")) if f"{r['repo']}#{r['number']}" not in owned | set(followed)]
    mains = dict(prev.get("mains") or {})
    repos = {r.get("repo") for r in rows if r.get("repo") and os.path.isdir(str(r.get("repo")))}
    mode = main_moved_mode()
    for repo in sorted(repos):
        try:
            gitops.git(repo, "fetch", "--quiet", "origin", check=False, timeout=60, fetches=True)
            sha = gitops.git(repo, "rev-parse", f"origin/{gitops.default_branch(repo)}", check=False)
        except Exception:
            continue
        old = mains.get(repo)
        if old and sha and old != sha:
            name = os.path.basename(repo.rstrip("/"))
            text = f"{name} main moved {old[:7]}..{sha[:7]}"
            if mode in ("never", "conflicts"):
                suppress(text, f"watch_main_moved is {mode}")
            elif mode == "active" and not _repo_active(name, rows, pr_of):
                suppress(text, f"no active worker or open PR in {name}")
            else:
                lines.append(Line(text, "wake"))
        if sha:
            mains[repo] = sha
    for text in ci_failures(prs, ci_targets(sorted(repos)), suppress):
        lines.append(Line(text, "routine" if " main ci recovered " in text else "wake"))
    bots, bot_lines = bot_threads(prs, _open_followed(pr_of, followed), prev.get("bot_threads") or {}, time.time())
    lines += bot_lines
    return {"ts": time.time(), "stacks": stacks, "prs": followed, "review_requests": asking, "mains": mains, "bot_threads": bots}, lines


def _open_followed(pr_of, followed) -> list:
    """(key, PR, live worker or None, whose review counts) for each open PR athena follows: the live workers' own, then the stacked
    and retired workers' ones no live worker owns."""
    out = []
    for name, p in sorted(pr_of.items()):
        key = pr_key((p or {}).get("url"))
        if key and p.get("state") == "OPEN":
            out.append((key, p, name, name))
    for key, data in sorted(followed.items()):
        if data.get("state") == "OPEN":
            out.append((key, data, None, data.get("owner")))
    return out


def bot_threads(prs, targets, last, now) -> tuple:
    """Each followed PR's unresolved review threads that a bot started, by id, as {key: {ids, reminded}}, and a wake for the new ones:
    `<w> bot threads +<n> #<pr>`, or `<owner/repo>#<pr> bot threads +<n>` with no live worker. One still open BOT_REMIND after HQ's
    review passed at the PR's head rings once more, with how many are open. A PR GitHub cannot say about keeps what it had; one no
    longer followed is forgotten. A person's thread never counts."""
    from athena.caps.watch import Line
    out, lines = {}, []
    for key, p, worker, reviewer in targets:
        slug, _, number = key.rpartition("#")
        before = last.get(key) if isinstance(last.get(key), dict) else {}
        try:
            ids = sorted(t["id"] for t in prs.threads(slug, int(number)) if t.get("bot"))
        except (prs.GhError, KeyError, TypeError, ValueError):
            if before:
                out[key] = before
            continue
        entry = {"ids": ids, "reminded": before.get("reminded")}
        new = [i for i in ids if i not in (before.get("ids") or [])]
        review = (ledger.get(reviewer) or {}).get("review") if reviewer else None
        review = review if isinstance(review, dict) else {}
        due = (ids and review.get("verdict") == "pass" and prs.same_head(review.get("head"), p.get("head"))
               and now - float(review.get("at") or now) >= BOT_REMIND and before.get("reminded") != review.get("at"))
        if due:
            entry["reminded"] = review.get("at")
        if new or due:
            n = len(new) if new else len(ids)
            text = f"{worker} bot threads +{n} #{number}" if worker else f"{key} bot threads +{n}"
            lines.append(Line(text, "wake", worker or "", ",".join(new) or f"remind {review.get('at')}"))
        out[key] = entry
    return out, lines


def _slug_of(repo):
    try:
        return gitops.github_slug(repo) if repo and os.path.isdir(str(repo)) else None
    except Exception:
        return None


def _run(ctx, rows, pr_of, suppress):
    try:
        found, lines = fetch(ctx, rows, pr_of, known(), suppress)
        if found:
            store.write_json(_file(), found)
        with _lock:
            _ready.extend(lines)
    except Exception as exc:
        suppress("follow: a fetch failed", f"{type(exc).__name__}: {exc}")


def tick(ctx, now):
    """Start a fetch when one is due and none runs; hand over what the last one found."""
    global _fetcher
    from athena.caps import status
    from athena.caps.watch import suppress
    if (_fetcher is None or not _fetcher.is_alive()) and now - float(known().get("ts") or 0) >= FETCH_EVERY:
        rows = status.collect()
        workers = store.read_dict(store.path("watch.json")).get("workers") or {}
        pr_of = {name: (w or {}).get("pr") for name, w in workers.items() if isinstance(w, dict)}
        _fetcher = threading.Thread(target=_run, args=(ctx, rows, pr_of, suppress), daemon=True)
        _fetcher.start()
        if wait_for_fetch:
            _fetcher.join(300)
    with _lock:
        out, _ready[:] = list(_ready), []
    return out


def commands():
    return {}
