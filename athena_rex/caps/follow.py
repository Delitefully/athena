"""What the old athena's watch followed beyond the workers' own PRs: stacked chains, retired workers' open PRs, PRs asking the human
for a review, the CI doorbell on each repo's default branch, and main moving. Fetched every two minutes on its own thread, so a slow
GitHub never holds up the watch's loop; its lines go out on the next loop after the fetch ends.

Lines, as the old athena wrote them:
    stack <owner/repo>#<n>: <before> -> <after>         a stacked PR's state, base, draft and mergeability changed
    <repo> main ci failed <workflow> <run-id>[ (attempt <n>)]   one incident per workflow, told once (ci-failed.json)
    <repo> main ci recovered <workflow>                  routine: the incident closed
    <repo> main moved <old>..<new>                       told by config watch_main_moved: never, conflicts (default), active, always
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from athena_rex.kernel import caps, config, gitops, ledger, store

SUMMARY = "follow stacks, retired workers' PRs, review requests, CI on main and main moving"
FETCH_EVERY = 120
FOLLOW_RETIRED = 14 * 86400
CI_KEEP = 200
CI_FRESH = 86400
CI_FAILED = ("failure", "timed_out", "startup_failure")
CI_PASSED = ("success",)
MAIN_MOVED = ("never", "conflicts", "active", "always")
REVIEW_CAP = 10
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
                    "databaseId,attempt,event,workflowName,createdAt,conclusion")
    if not isinstance(rows, list):
        return None
    return [{"id": r["databaseId"], "attempt": r["attempt"] if isinstance(r.get("attempt"), int) else 1,
             "workflow": str(r.get("workflowName") or ""), "created": r.get("createdAt"), "conclusion": str(r.get("conclusion") or "").lower()}
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


def ci_failures(prs, targets, suppress) -> list:
    """One incident per failing workflow on each repo's default branch, as the old athena keeps them in ci-failed.json: the first
    failed run opens it and is told; more failures while it is open are counted, not told; a rerun of the opening run that fails again
    is told with its attempt; the newest run passing closes it (`recovered`). A repo seen for the first time records, tells nothing."""
    from athena_rex.caps.watch import safe_workflow
    seen = store.read_dict(_ci_file())
    lines, changed = [], False
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
        if first:
            seen[key] = {"runs": [f"{r['id']}.{r['attempt']}" for r in failed][:CI_KEEP],
                         "incidents": {wf: {"run": r["id"], "opened": r.get("created"), "runs": 1}
                                       for wf, r in newest.items() if r["conclusion"] in CI_FAILED}}
            changed = True
            continue
        for r in reversed(failed):
            run_key = f"{r['id']}.{r['attempt']}"
            if run_key in known_runs:
                continue
            known_runs.append(run_key)
            changed = True
            wf = safe_workflow(r["workflow"])
            line = f"{name} main ci failed {wf} {r['id']}"
            open_ = incidents.get(wf)
            if open_ is None:
                incidents[wf] = {"run": r["id"], "opened": r.get("created"), "runs": 1}
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
    from athena_rex.caps.watch import Line
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
    return {"ts": time.time(), "stacks": stacks, "prs": followed, "review_requests": asking, "mains": mains}, lines


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
    from athena_rex.caps import status
    from athena_rex.caps.watch import suppress
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
