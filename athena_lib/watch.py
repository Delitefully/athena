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

from athena_lib import dashstate, gitops, herdr, hq, ledger, paths, spawn, status
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
    """The stacked-PR chains HQ watches: [{"repo": "owner/name", "prs": [5844, 5856]}], from stack.json."""
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


def stack_snapshot(prev=None) -> dict:
    """`<repo>#<n>` -> "STATE base=<branch> MERGEABLE" per watched PR. gh's UNKNOWN (still computing) keeps the last value."""
    prev = prev or {}
    out = {}
    for chain in stacks():
        for n in chain["prs"]:
            key = f"{chain['repo']}#{n}"
            try:
                proc = subprocess.run(["gh", "pr", "view", str(n), "--repo", chain["repo"], "--json",
                                       "state,baseRefName,mergeable"], capture_output=True, text=True, timeout=30)
                data = json.loads(proc.stdout) if proc.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                data = None
            if not data or data.get("mergeable") == "UNKNOWN" and data.get("state") == "OPEN":
                if key in prev:
                    out[key] = prev[key]
                continue
            out[key] = f"{data.get('state')} base={data.get('baseRefName')} {data.get('mergeable')}"
    return out


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
    re.compile(r"^\S+ goal (not sent|unconfirmed)"),
    re.compile(r"^\S+ new worker "),
    re.compile(r"^watch error"),
]


def classify(line: str, retired=frozenset()) -> str:
    """"wake" for a line HQ must act on, "status" for routine progress the status line shows.

    A line no rule knows wakes, so nothing new that athena watch says is lost.
    """
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


def _needs_you(w) -> bool:
    pr = w.get("pr") or {}
    if pr.get("state") != "OPEN":
        return False
    return pr.get("review") == "APPROVED" or (not pr.get("draft") and w.get("state") in ("done", "idle", "exited"))


def summary(snap) -> str:
    """The status line (Claude Code shows it as `athena: <this>`): each worker's last state, most urgent first,
    and how many PRs wait on the human."""
    workers = snap.get("workers", {})
    parts = [f"{n} {w.get('state')}" for n, w in sorted(workers.items(), key=lambda kv: (status.urgency(kv[1].get("state")), kv[0]))]
    need = sum(1 for w in workers.values() if _needs_you(w))
    need += sum(1 for v in (snap.get("stacks") or {}).values() if v.startswith("OPEN") and v.endswith(" CONFLICTING"))
    if need:
        parts.append(f"{need} PR needs you" if need == 1 else f"{need} PRs need you")
    return " · ".join(parts if workers else ["no live workers"] + parts)


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
    text = summary(snap)
    if text != last_status:
        print(json.dumps({"status": text}), flush=True)
    return text


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
        workers[w["name"]] = {"state": s["state"], "pr": pr_state, "report": s["report"], "detail": s["detail"]}
        repo = w.get("repo")
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
    cur["stacks"] = stack_snapshot((prev or {}).get("stacks")) if fetch else dict((prev or {}).get("stacks") or {})
    return cur, lines + ([] if prev is None else diff(prev, cur))


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


def run(interval=20, fetch_every=120, tagged=False):
    prev, shown = None, None
    last_fetch = 0.0
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
        time.sleep(interval)
