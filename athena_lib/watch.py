"""Print one line per meaningful change, for the chief of staff.

Quiet when nothing changes. Also keeps athena workspace labels from being renamed by other plugins, and watches the
stacked PRs listed in stack.json. Plain output is for Monitor; `--tagged` output is for the athena-watch mod in HQ,
which wakes HQ only for the lines `classify` calls actionable and shows the rest in its status line.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from athena_lib import asks, config, dash, dashstate, gitops, herdr, hq, ledger, paths, pr, slack, spawn, status
from athena_lib.retire import workspace_owned


def _short(sha):
    return (sha or "")[:7]


def diff(prev: dict, cur: dict) -> list:
    lines = []
    pw, cw = prev.get("workers", {}), cur.get("workers", {})
    for name, now in cw.items():
        before = pw.get(name)
        detail = f" ({now['detail']})" if now.get("detail") else ""
        if before is None:
            lines.append(f"{name} new worker ({now.get('state')}{', ' + now['detail'] if now.get('detail') else ''})")
            continue
        if before.get("state") != now.get("state"):
            lines.append(f"{name} state {before.get('state')}->{now.get('state')}{detail}")
        rep_now = (now.get("report") or {}).get("status")
        rep_before = (before.get("report") or {}).get("status")
        if rep_now and rep_now != rep_before:
            lines.append(f"{name} report {rep_now}")
        bp, np_ = before.get("pr") or {}, now.get("pr") or {}
        if np_ and not bp:
            lines.append(f"{name} pr opened {np_.get('url')}")
        if bp and np_:
            if bp.get("checks") != np_.get("checks"):
                lines.append(f"{name} checks {bp.get('checks')}->{np_.get('checks')}")
            if (np_.get("comments") or 0) > (bp.get("comments") or 0):
                lines.append(f"{name} review comments +{np_['comments'] - (bp.get('comments') or 0)}")
            if np_.get("review") and np_.get("review") != bp.get("review"):
                lines.append(f"{name} review {np_['review']}")
            if np_.get("state") != bp.get("state"):
                lines.append(f"{name} pr {np_.get('state', '').lower()}")
    for name in pw:
        if name not in cw:
            lines.append(f"{name} gone from the ledger")
    for repo, sha in cur.get("mains", {}).items():
        old = prev.get("mains", {}).get(repo)
        if old and sha and old != sha:
            lines.append(f"{os.path.basename(repo)} main moved {_short(old)}..{_short(sha)}")
    for key, now in cur.get("stacks", {}).items():
        old = prev.get("stacks", {}).get(key)
        if old and now != old:
            lines.append(f"stack {key}: {old} -> {now}")
    return lines


def _stack_file():
    return paths.state_dir() / "stack.json"


def stacks() -> list:
    """The stacked-PR chains HQ watches: [{"repo": "owner/name", "prs": [41, 42]}], from stack.json."""
    try:
        data = json.loads(_stack_file().read_text())
    except (OSError, ValueError):
        return []
    out = []
    for chain in data.get("stacks", []) if isinstance(data, dict) else []:
        if isinstance(chain, dict) and chain.get("repo") and isinstance(chain.get("prs"), list):
            out.append({"repo": chain["repo"], "prs": [int(n) for n in chain["prs"]]})
    return out


def set_stack(repo: str, prs) -> list:
    """Replace the chain watched for `repo`; no PRs drops it."""
    chains = [c for c in stacks() if c["repo"] != repo]
    if prs:
        mine = {"repo": repo, "prs": [int(n) for n in prs]}
        old = [c["repo"] for c in stacks()]
        chains.insert(old.index(repo) if repo in old else len(chains), mine)
    paths.ensure()
    target = _stack_file()
    tmp = target.with_suffix(".json.tmp%d" % os.getpid())
    tmp.write_text(json.dumps({"stacks": chains}, indent=2) + "\n")
    os.replace(tmp, target)  # a watch tick never reads it half-written
    return chains


def _stack_keys() -> list:
    return [(chain["repo"], n) for chain in stacks() for n in chain["prs"]]


def stack_snapshot(prev=None, fetched=None) -> dict:
    """`<repo>#<n>` -> "STATE base=<branch> [DRAFT ]MERGEABLE" per watched PR, from one `pr.batch` (or `fetched`,
    a batch already made). A PR gh cannot read, or whose mergeability is still computing, keeps the last value."""
    prev = prev or {}
    if fetched is None:
        fetched = pr.batch(_stack_keys())
    out = {}
    for repo, n in _stack_keys():
        key = f"{repo}#{n}"
        data = (fetched or {}).get(key)
        if not data or data.get("mergeable") == "UNKNOWN" and data.get("state") == "OPEN":
            if key in prev:
                out[key] = prev[key]
            continue
        draft = " DRAFT" if data.get("draft") else ""
        out[key] = f"{data.get('state')} base={data.get('base')}{draft} {data.get('mergeable')}"
    return out


_FOLLOW_RETIRED = 14 * 24 * 3600  # as long as the dashboard lists a retired worker under Done


def _human_merge(*repos) -> bool:
    """Whether the human merges a PR of these repos (its folder, its owner/name): either one listed is enough. An
    unreadable config.json counts as listed, so the dashboard never offers Merge on a guess."""
    try:
        return any(config.listed("human_merge_repos", r) for r in repos if r)
    except config.ConfigError:
        return True


_CI = {}  # owner/name -> (runs CI, when asked)
_CI_TTL = 24 * 3600


def has_ci(slug):
    """Whether `slug` runs CI, asked of GitHub at most once a day. A failed ask keeps the last answer; never known
    is None, which waits for checks like a repo with CI."""
    known, asked = _CI.get(slug, (None, None))
    if asked is not None and time.time() - asked < _CI_TTL:
        return known
    found = pr.has_ci(slug)
    if found is None:
        if slug in _CI:
            _CI[slug] = (known, time.time())  # keep it; ask again tomorrow
        return known
    _CI[slug] = (found, time.time())
    return found


def _move(p, live, repo, slug):
    """pr.move for a PR of `repo` (a path or owner/name, for human_merge_repos); whether `slug` runs CI is asked only
    for a PR with no checks yet."""
    ci = has_ci(slug) if p.get("checks") == "none" and is_slug(slug) else None
    return pr.move(p, live=live, human_merge=_human_merge(repo, slug), ci=ci)


def _retired_open() -> dict:
    """`owner/name#n` -> (worker, its repo) for each recently retired worker whose PR was open when it retired."""
    try:
        lines = (paths.state_dir() / "history.jsonl").read_text().splitlines()
    except OSError:
        return {}
    last = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("name"):
            last[entry["name"]] = entry
    out = {}
    for name, entry in last.items():
        p = entry.get("pr") if isinstance(entry.get("pr"), dict) else {}
        key = _pr_key(p.get("url"))
        if key and p.get("state") == "OPEN" and time.time() - (entry.get("ts") or 0) <= _FOLLOW_RETIRED:
            out[key] = (name, entry.get("repo"))
    return out


def _key_number(key):
    repo, _, n = key.rpartition("#")
    return repo, int(n) if n.isdigit() else 0


def follow(cur, prev=None) -> None:
    """On a fetch tick: one `pr.batch` for the stacked PRs and the retired workers' open ones, then one review
    search per repo athena knows. Sets cur's `stacks`, `prs` (each followed PR no live worker owns, with whose move
    it waits on) and `review_requests` (a teammate's non-draft PR asking the human by name, one row each)."""
    prev = prev or {}
    retired = _retired_open()
    stacked = {f"{r}#{n}" for r, n in _stack_keys()}
    keys = sorted(set(_stack_keys()) | {_key_number(k) for k in retired})
    fetched = pr.batch(keys)
    cur["stacks"] = stack_snapshot(prev.get("stacks"), fetched=fetched)
    owned = {_pr_key((w.get("pr") or {}).get("url")) for w in cur.get("workers", {}).values()}
    prs = {}
    for key in sorted(stacked | set(retired)):
        if key in owned:
            continue
        data = (fetched or {}).get(key) or (prev.get("prs") or {}).get(key)
        if not data:
            continue
        repo, _ = _key_number(key)
        owner, worker_repo = retired.get(key, (None, None))
        slug = repo if key in stacked else (repo_slug(worker_repo) if worker_repo else None)
        entry = dict(data)
        entry.update({"owner": owner, "slug": slug if slug and slug.lower() == repo.lower() else None,
                      "move": _move(data, False, worker_repo or repo, repo)})
        prs[key] = entry
    cur["prs"] = prs
    slugs = [w.get("slug") for w in cur.get("workers", {}).values()]
    slugs += [repo_slug(w["repo"]) for w in ledger.workers().values() if w.get("repo")]
    slugs += [chain["repo"] for chain in stacks()]
    known = owned | set(prs) | stacked
    found = pr.review_requested([s for s in dict.fromkeys(slugs) if is_slug(s)], last=prev.get("review_requests"))
    cur["review_requests"] = [r for r in found if f"{r['repo']}#{r['number']}" not in known]


def safe_workflow(name) -> str:
    """A CI workflow's name in the words a wake may carry (the mod's safeWorkflow): runs of anything but letters,
    digits, `.`, `_` and `-` fold to `-`, at most 32 characters."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).lstrip("-")[:32].rstrip("-")
    return safe or "workflow"


def _ci_file():
    return paths.state_dir() / "ci-failed.json"


_CI_KEEP = 200  # run ids remembered per repo
_CI_FRESH = 24 * 3600  # an unseen failure older than this (the watch was off) is recorded, not told


def _age(created) -> float:
    try:
        when = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return time.time() - when.timestamp()


_DEFAULT_BRANCHES = {}  # owner/name -> (branch, when asked), for a ci_repos slug with no local checkout


def _slug_default_branch(slug):
    branch, asked = _DEFAULT_BRANCHES.get(slug.lower(), (None, None))
    if asked is not None and time.time() - asked < _CI_TTL:
        return branch
    found = pr.default_branch(slug)
    if found or branch is None:
        _DEFAULT_BRANCHES[slug.lower()] = (found or branch, time.time())
    return found or branch


def ci_targets(repos) -> list:
    """The repos ci_failures checks: those of the live workers (`repos`, local paths), then config `ci_repos` (a
    path, `~/...` or `/...`, or an `owner/name`), each repo once."""
    try:
        extra = config.get("ci_repos") or []
    except config.ConfigError:
        extra = []
    out, slugs = [], set()
    for entry in list(repos) + [e for e in extra if isinstance(e, str)]:
        if entry.startswith(("/", "~")):
            entry = os.path.expanduser(entry)
            slug = repo_slug(entry)
        elif is_slug(entry):
            slug = entry
        else:
            continue
        key = (slug or entry).lower()
        if key not in slugs:
            slugs.add(key)
            out.append(entry)
    return out


def _run_key(run) -> str:
    """A failed run as ci-failed.json remembers it: a rerun keeps its id, so its attempt goes with it."""
    return f"{run['id']}.{run['attempt']}"


def ci_failures(repos) -> list:
    """`<repo> main ci failed <workflow> <run-id>` once per failed run on each repo's default branch (a local path, or
    an owner/name from ci_targets: `<repo>` is its last part), and again with
    ` (attempt <n>)` when a rerun fails too. The runs told live in ci-failed.json in the state dir, so a restarted
    watch tells none twice; a repo looked at for the first time records its failures without telling them."""
    try:
        seen = json.loads(_ci_file().read_text())
    except (OSError, ValueError):
        seen = {}
    seen = seen if isinstance(seen, dict) else {}
    lines, changed = [], False
    for repo in dict.fromkeys(repos):
        local = repo.startswith("/")
        slug = repo_slug(repo) if local else repo if is_slug(repo) else None
        branch = (gitops.default_branch(repo) if local else _slug_default_branch(slug)) if slug else None
        if not branch:
            continue
        runs = pr.failed_runs(slug, branch)
        if runs is None:
            continue
        key = slug.lower()
        known = seen.get(key) if isinstance(seen.get(key), list) else None
        if known is None:
            seen[key] = [_run_key(r) for r in runs][:_CI_KEEP]
            changed = True
            continue
        for r in reversed(runs):  # oldest first
            if _run_key(r) in known:
                continue
            known.append(_run_key(r))
            changed = True
            if _age(r["created"]) <= _CI_FRESH:
                again = f" (attempt {r['attempt']})" if r["attempt"] > 1 else ""
                lines.append(f"{os.path.basename(repo)} main ci failed {safe_workflow(r['workflow'])} {r['id']}{again}")
        seen[key] = known[-_CI_KEEP:]
    if changed:
        paths.ensure()
        tmp = _ci_file().with_suffix(".json.tmp%d" % os.getpid())
        tmp.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, _ci_file())
    return lines


_WAKE_REVIEWS = ("CHANGES_REQUESTED", "APPROVED")
_ROUTINE = [
    re.compile(r"^watching: "),
    re.compile(r"^\S+ state \S+->(working|idle|done|starting|unknown)( \(.*\))?$"),
    re.compile(r"^\S+ checks \S+->(pending|success|none)$"),
    re.compile(r"^\S+ goal sent$"),
    re.compile(r"^\S+ new worker \((working|idle|done|starting|unknown)[,)]"),
]
_WAKE = [
    re.compile(r"^\S+ report \S+$"),
    re.compile(r"^\S+ state \S+->"),
    re.compile(r"^\S+ pr \S+"),
    re.compile(r"^\S+ checks \S+->"),
    re.compile(r"^\S+ review comments \+\d+$"),
    re.compile(r"^\S+ main moved "),
    re.compile(r"^\S+ main ci failed "),
    re.compile(r"^slack-new "),
    re.compile(r"^\S+ goal (not sent|unconfirmed)"),
    re.compile(r"^\S+ new worker "),
    re.compile(r"^watch error"),
]


_DOORBELL = re.compile(r"^dash-queue \d{1,9}$")


def classify(line: str, retired=frozenset()) -> str:
    """"wake" for a line HQ must act on, "status" for routine progress the status line shows.

    A line no rule knows wakes, so nothing new that athena watch says is lost.
    """
    if _DOORBELL.match(line):
        return "wake"  # the human answered on the dashboard: HQ reads `athena ask queue`
    name = line.split(" ", 1)[0]
    if line.endswith(" gone from the ledger"):
        return "status" if name in retired else "wake"
    m = re.match(r"^\S+ review (\S+)$", line)
    if m and m.group(1) != "comments":
        return "wake" if m.group(1) in _WAKE_REVIEWS else "status"
    m = re.match(r"^stack \S+: (.*) -> (.*)$", line)
    if m:
        old, new = m.groups()
        merged = new.startswith("MERGED") and not old.startswith("MERGED")
        conflicting = new.endswith(" CONFLICTING") and not old.endswith(" CONFLICTING")
        return "wake" if merged or conflicting else "status"
    if any(r.search(line) for r in _ROUTINE):
        return "status"
    return "wake"


_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_DEFAULT_BASES = ("main", "master")


def _group(w) -> str:
    """Where a worker sits on the status line: `needs context`, `blocked` or `exited` need the human, `done` is
    done (its report says so even once its hook went idle), anything else is its state, shown as active."""
    report = ((w.get("report") or {}).get("status") or "").upper()
    state = w.get("state") or "unknown"
    if report == "NEEDS_CONTEXT":
        return "needs context"
    if state == "blocked" or report == "BLOCKED":
        return "blocked"
    if state == "exited":
        return "exited"
    if state == "done" or report.startswith("DONE"):
        return "done"
    return state


def _pr_key(url):
    m = _PR_URL.search(url or "")
    return f"{m.group(1)}#{m.group(2)}" if m else None


_OVER_WORKING = ("approved", "queued", "changes")  # moves a working worker's row shows; others show `working`
_MERGES = ("approved", "mergeable")  # the human's to merge: a live worker's PR only once the worker reported DONE


def _reported(w) -> bool:
    """The worker finished its goal: done, or idle or exited after a DONE report (dash/view.go `reported`)."""
    state = w.get("state")
    report = w.get("report") or w.get("last_report") or {}
    return state == "done" or state in ("idle", "exited") and (report.get("status") or "").upper().startswith("DONE")


def _prs_awaiting(snap) -> list:
    """`#n` per PR whose dashboard row is in Needs you, each once: a live worker's PR whose move is the human's
    (unless the worker itself is blocked, or still working on it), a followed PR whose move is the human's, and a
    teammate's PR asking the human for a review. needs-you.json adds none: it is for decisions."""
    keys = []
    for w in snap.get("workers", {}).values():
        p = w.get("pr") or {}
        m = p.get("move") or {}
        if not m.get("needs") or _group(w) in ("needs context", "blocked"):
            continue
        if w.get("state") == "working" and m.get("kind") not in _OVER_WORKING:
            continue
        if m.get("kind") in _MERGES and not _reported(w):
            continue
        keys.append(_pr_key(p.get("url")) or f"#{p.get('number')}")
    keys += [k for k, p in (snap.get("prs") or {}).items() if (p.get("move") or {}).get("needs")]
    keys += [f"{r.get('repo')}#{r.get('number')}" for r in snap.get("review_requests") or []]
    numbers = [k.rsplit("#", 1)[1] for k in dict.fromkeys(k.lower() for k in keys)]  # one per owner/repo#n
    return ["#" + n for n in sorted(numbers, key=lambda n: int(n) if n.isdigit() else 0)]


_NEEDS_ORDER = ("needs context", "blocked", "exited")
_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_SLUGS = {}


is_slug = pr.is_slug


def _worker_slug(w):
    """A worker's repo for linking its PR: its origin's `owner/name`, unless its PR lives in another repo (a fork,
    or a PR opened elsewhere); then nothing, so its #n draws plain. GitHub's URL only ever withholds a link."""
    slug = w.get("slug")
    if not is_slug(slug):
        return None
    m = _PR_URL.search((w.get("pr") or {}).get("url") or "")
    return slug if not m or m.group(1).lower() == slug.lower() else None


def repo_slug(repo):
    """`owner/name` of a repo's GitHub origin remote, or None (no repo, no origin, not GitHub). Cached per repo."""
    if repo not in _SLUGS:
        try:
            remote = gitops.git(repo, "remote", "get-url", "origin", check=False)
        except OSError:
            remote = ""
        m = _REMOTE.fullmatch((remote or "").strip())
        _SLUGS[repo] = m.group(1) if m and is_slug(m.group(1)) else None
    return _SLUGS[repo]


def _links(snap) -> dict:
    """PR number -> `owner/name`, for the mod to link `#n`: a live worker's PR by the worker's own repo (its origin
    remote, from the ledger), a stacked PR by stack.json's repo, a retired worker's by its origin, a review request
    by the repo searched. Never from a URL GitHub or a worker wrote, nor from needs-you.json; a number two repos
    share is left out, so it draws plain."""
    seen = {}
    for w in snap.get("workers", {}).values():
        n = (w.get("pr") or {}).get("number")
        slug = _worker_slug(w)
        if slug and isinstance(n, int):
            seen.setdefault(str(n), set()).add(slug)
    for key in snap.get("stacks") or {}:
        repo, _, n = key.rpartition("#")
        if n.isdigit() and is_slug(repo):
            seen.setdefault(n, set()).add(repo)
    for key, p in (snap.get("prs") or {}).items():
        n = key.rpartition("#")[2]
        if n.isdigit() and is_slug(p.get("slug")):
            seen.setdefault(n, set()).add(p["slug"])
    for r in snap.get("review_requests") or []:
        if isinstance(r.get("number"), int) and is_slug(r.get("repo")):
            seen.setdefault(str(r["number"]), set()).add(r["repo"])
    return {n: next(iter(r)) for n, r in seen.items() if len(r) == 1}


def board(snap) -> dict:
    """The status line's parts, most urgent first: what needs the human (each worker, then the PRs awaiting a
    review or merge), the active workers grouped by state, and the done ones."""
    workers = snap.get("workers", {})
    groups = {}
    for name in sorted(workers):
        groups.setdefault(_group(workers[name]), []).append(name)
    needs = [f"{n} {g}" for g in _NEEDS_ORDER for n in groups.get(g, [])]
    active = [[g, names] for g, names in sorted(groups.items(), key=lambda kv: (status.urgency(kv[0]) * -1, kv[0]))
              if g not in _NEEDS_ORDER and g != "done"]
    out = {"needs": needs, "prs": _prs_awaiting(snap),
           "active": active, "done": groups.get("done", []), "links": _links(snap),
           "repos": {n: _worker_slug(w) for n, w in sorted(workers.items()) if _worker_slug(w)}}
    if snap.get("dash"):
        out["dash"] = snap["dash"]
    return out


def summary(snap) -> str:
    """The status line as one plain line (the mod draws `board` itself): what needs the human, the PRs awaiting
    them, the active workers by state, then the done ones."""
    b = board(snap)
    parts = list(b["needs"])
    if b["prs"]:
        n = len(b["prs"])
        parts.append("1 PR needs you" if n == 1 else f"{n} PRs need you")
    parts += [f"{', '.join(names)} {state}" for state, names in b["active"]]
    if b["done"]:
        parts.append(f"{', '.join(b['done'])} done")
    if not snap.get("workers"):
        parts.append("no live workers")
    return " · ".join(parts)


def _retired() -> set:
    return {n for n, w in ledger.workers().items() if w.get("state") == "retired"}


def emit(lines, snap, tagged=False, last_status=None):
    """Print a tick's lines; tagged, as JSON with the status line after them when it changed. Returns the status."""
    if not tagged:
        for line in lines:
            print(line, flush=True)
        return last_status
    retired = _retired() if any(l.endswith(" gone from the ledger") for l in lines) else frozenset()
    for line in lines:
        print(json.dumps({"line": line, "wake": classify(line, retired) == "wake"}), flush=True)
    record = json.dumps({"status": summary(snap), "board": board(snap)})
    if record != last_status:
        print(record, flush=True)
    return record


def report_error(exc, tagged=False):
    if tagged:
        print(json.dumps({"line": f"watch error: {exc}", "wake": True}), flush=True)
    else:
        print(f"watch error: {exc}", file=sys.stderr, flush=True)


def snapshot(fetch=False, with_pr=True, prev=None) -> dict:
    workers, mains = {}, {}
    before = (prev or {}).get("workers", {})
    for w in ledger.live():
        s = status.collect(w, with_pr=with_pr)
        pr_state = s["pr"] if s["pr"] is not None else (before.get(w["name"]) or {}).get("pr")
        repo = w.get("repo")
        if pr_state and "requested" in pr_state:
            key = _pr_key(pr_state.get("url"))
            pr_state = {**pr_state, "move": _move(pr_state, True, repo, key.rpartition("#")[0] if key else None)}
        workers[w["name"]] = {"state": s["state"], "pr": pr_state, "report": s["report"], "detail": s["detail"],
                              "last_report": s.get("last_report"), "slug": repo_slug(repo) if repo else None}
        if repo and repo not in mains:
            if fetch:
                gitops.git(repo, "fetch", "--quiet", "origin", check=False)
            default = gitops.default_branch(repo)
            mains[repo] = gitops.git(repo, "rev-parse", f"origin/{default}", check=False)
    return {"workers": workers, "mains": mains}


def step(prev, fetch=False, with_pr=True):
    """One watch tick: send goals that were waiting for a ready Claude, then diff. Returns (snapshot, lines)."""
    lines, missing = [], []
    told = set((prev or {}).get("missing", []))
    for r in spawn.deliver_pending():
        if r["outcome"] == "sent":
            lines.append(f"{r['name']} goal sent")
        elif r["outcome"] == "unconfirmed":
            lines.append(f"{r['name']} goal unconfirmed ({r.get('error')}): check its pane, do not resend blindly")
        elif r["outcome"] == "missing":
            missing.append(r["name"])
            if r["name"] not in told:
                lines.append(f"{r['name']} goal not sent: {r['problem']}")
    cur = snapshot(fetch=fetch, with_pr=with_pr, prev=prev)
    cur["missing"] = missing
    cur["dash"] = _dash_url()
    if fetch:
        follow(cur, prev)
        lines += ci_failures(ci_targets(cur["mains"]))
    else:
        last = prev or {}
        cur["stacks"], cur["prs"] = dict(last.get("stacks") or {}), dict(last.get("prs") or {})
        cur["review_requests"] = list(last.get("review_requests") or [])
    return cur, lines + ([] if prev is None else diff(prev, cur))


def _dash_url():
    """The dashboard's URL while it runs, from dash.pid as `athena dash show` reads it; None when it is off."""
    try:
        return (dash.running() or {}).get("url")
    except OSError:
        return None


def tick(prev, fetch=False):
    """step, then save the snapshot for the dashboard."""
    cur, lines = step(prev, fetch=fetch)
    try:
        dashstate.save_watch(cur)
    except OSError as exc:
        print(f"watch.json not saved: {exc}", file=sys.stderr, flush=True)
    return cur, lines


def keep_labels():
    hq.keep_label()
    for w in ledger.live():
        ws = w.get("workspace")
        if not ws:
            continue
        want = spawn.space_label(w["name"], w.get("title"), w.get("linear"), w.get("branch"))
        if not workspace_owned(ws, w.get("path")):
            continue
        try:
            current = herdr.call("workspace", "get", ws, timeout=10).get("workspace", {}).get("label")
            if current and current != want:
                herdr.call("workspace", "rename", ws, want, timeout=10)
        except herdr.HerdrError:
            pass


def ring(bell, tagged=False):
    """Print the dashboard's doorbell when answers settled since it last rang: one line, never their text."""
    try:
        line = bell.check()
    except Exception as exc:  # a queue it cannot read must not stop the watch; told once until it reads again
        if str(exc) != bell.error:
            bell.error = str(exc)
            report_error(f"dash queue: {exc}", tagged=tagged)
        return
    bell.error = None
    if line:
        print(json.dumps({"line": line, "wake": True}) if tagged else line, flush=True)


def knock_start(doorbell):
    """Start the Slack doorbell; when it is off (no token), say so once on stderr, which wakes no one."""
    try:
        off = doorbell.start()
    except Exception as exc:  # the doorbell is extra: the watch carries on without it
        doorbell.off = True
        off = f"slack doorbell off: {doorbell.redact(exc)}"
    if off:
        print(off, file=sys.stderr, flush=True)


def knock(doorbell, tagged=False):
    """Print the Slack doorbell's `slack-new <channel> <n>` lines when a poll is due: ids and counts, never text. A
    network error backs off quietly on stderr; anything else Slack says is a watch error, told once until it reads
    again."""
    try:
        lines = doorbell.check()
    except Exception as exc:
        text = doorbell.redact(exc)
        if text != doorbell.error:
            doorbell.error = text
            if getattr(exc, "network", False):
                print(f"slack doorbell: {text}; backing off", file=sys.stderr, flush=True)
            else:
                report_error(f"slack doorbell: {text}", tagged=tagged)
        return
    doorbell.error = None
    for note in doorbell.notes():
        print(doorbell.redact(note), file=sys.stderr, flush=True)
    for line in lines:
        print(json.dumps({"line": line, "wake": True}) if tagged else line, flush=True)


def slack_once(window=0) -> int:
    """One Slack doorbell poll, for a smoke run: its `slack-new` lines on stdout, and on stderr how many
    conversations it knows. Use a scratch ATHENA_STATE_DIR: the poll writes slack.json there."""
    doorbell = slack.Doorbell(window=window)
    knock_start(doorbell)
    if doorbell.off:
        return 1
    knock(doorbell)
    if doorbell.error:
        return 1
    print(f"slack doorbell: {doorbell.summary()}", file=sys.stderr, flush=True)
    return 0


def run(interval=20, fetch_every=120, tagged=False):
    prev, shown = None, None
    last_fetch = 0.0
    bell = asks.Doorbell()
    slackbell = slack.Doorbell()
    knock_start(slackbell)
    if not slackbell.off:
        slackbell = slack.Background(slackbell)  # its HTTP calls never hold up the dash-queue ring or a tick
    try:
        asks.migrate()  # needs-you.md, once, into needs-you.json
    except (OSError, ValueError) as exc:
        report_error(f"needs-you.md not migrated: {exc}", tagged=tagged)
    while True:
        fetch = time.time() - last_fetch >= fetch_every
        if fetch:
            last_fetch = time.time()
        try:
            first = prev is None
            prev, lines = tick(prev, fetch=fetch)
            if first:
                names = ", ".join(f"{n}={v['state']}" for n, v in prev["workers"].items()) or "no live workers"
                lines = [f"watching: {names}"] + lines
            shown = emit(lines, prev, tagged=tagged, last_status=shown)
            keep_labels()
        except Exception as exc:
            report_error(exc, tagged=tagged)
        # Between ticks, look at the dashboard's queue every second: one stat while nothing changed.
        deadline = time.time() + interval
        while True:
            ring(bell, tagged=tagged)
            if not slackbell.off:
                knock(slackbell, tagged=tagged)
            left = deadline - time.time()
            if left <= 0:
                break
            time.sleep(min(1.0, left))
