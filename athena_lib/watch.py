"""Print one line per meaningful change, for the chief of staff to run under Monitor.

Quiet when nothing changes. Also keeps athena workspace labels from being renamed by other plugins.
"""
import os
import sys
import time

from athena_lib import gitops, herdr, hq, ledger, spawn, status
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
    return lines


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
    return cur, lines + ([] if prev is None else diff(prev, cur))


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


def run(interval=20, fetch_every=120):
    prev = None
    last_fetch = 0.0
    while True:
        fetch = time.time() - last_fetch >= fetch_every
        if fetch:
            last_fetch = time.time()
        try:
            first = prev is None
            prev, lines = step(prev, fetch=fetch)
            if first:
                names = ", ".join(f"{n}={v['state']}" for n, v in prev["workers"].items()) or "no live workers"
                print(f"watching: {names}", flush=True)
            for line in lines:
                print(line, flush=True)
            keep_labels()
        except Exception as exc:
            print(f"watch error: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)
