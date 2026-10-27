"""Getting ready PRs to reviewers: when a live worker's PR is ready, ask for it to be posted in the review channel.

Config-driven: with no `open_prs_channel` in config.json, nothing here runs and nothing is written. With one, athena
watch calls `run` every tick, and a PR is due once all of these hold:

- it is a live worker's, open, not a draft, neither approved nor with changes requested;
- its checks are green, and HQ's own review (`athena review`) passed at its current head;
- it is the bottom of its stack: based on its repo's default branch, and not after another PR in a stack.json chain;
- it was never posted (open-prs-posted.json, which `athena open-prs add` writes) nor asked for before.

A due PR is recorded in open-prs-auto.json first, then handed to the poster, so a crash or a failing poster can never
post it twice. The poster athena watch uses, `ring_hq`, posts nothing itself: it returns the line
`<worker> post for review #<n>`, which wakes HQ to post the PR through its Slack connector and record it with
`athena open-prs add` (the Slack token athena holds only reads). A request still not recorded after REMIND_AFTER rings
once more, then never again. That is the automatic lane's whole budget: one post per PR. A re-post after a
teammate's review is the human's call, never automatic.
"""
import json
import os
import time

from athena_lib import config, gitops, ledger, paths, pr, slack

STATE_FILE = "open-prs-auto.json"
REMIND_AFTER = 30 * 60  # a request HQ has not recorded as posted by then rings once more


def channel():
    """The configured review channel's id, or None (unset, or not a channel id)."""
    try:
        value = config.get("open_prs_channel")
    except config.ConfigError:
        return None
    return value if isinstance(value, str) and slack.CHANNEL_ID.match(value) else None


def _file():
    return paths.state_dir() / STATE_FILE


def _load() -> dict:
    try:
        data = json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _save(data):
    paths.ensure()
    target = _file()
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, target)


def _key(p):
    found = slack.pr_keys((p or {}).get("url"))
    return found[0] if found else None


def _passed(review, p) -> bool:
    review = review if isinstance(review, dict) else {}
    return review.get("verdict") == "pass" and pr.same_head(review.get("head"), p.get("head"))


def _ready(p, review) -> bool:
    return (p.get("state") == "OPEN" and not p.get("draft") and p.get("checks") == "success"
            and p.get("review") not in ("APPROVED", "CHANGES_REQUESTED") and _passed(review, p))


def _later_in_a_chain(key, states) -> bool:
    """Whether a stack.json chain lists an open PR before this one. `states`: the watch's stack snapshot, where a
    merged or closed PR reads "MERGED ..." or "CLOSED ..."; one not known yet counts as open."""
    from athena_lib import watch  # watch runs this module every tick
    done = {k.lower() for k, v in (states or {}).items() if str(v).startswith(("MERGED", "CLOSED"))}
    for chain in watch.stacks():
        numbers = [f"{chain['repo']}#{n}".lower() for n in chain["prs"]]
        if key in numbers and any(k not in done for k in numbers[:numbers.index(key)]):
            return True
    return False


def _bottom(p, repo, key, states=None) -> bool:
    base = p.get("base")
    if not base or _later_in_a_chain(key, states):
        return False
    default = gitops.default_branch(repo) if repo else None
    return base == (default or "main")


def due(snap) -> list:
    """(worker, key, pr) for each live worker's PR ready to post now, before checking what was posted."""
    reviews = {w["name"]: w.get("review") for w in ledger.live()}
    out = []
    for name, w in sorted((snap.get("workers") or {}).items()):
        p = w.get("pr") or {}
        key = _key(p)
        repo = (snap.get("repos") or {}).get(name)
        if key and name in reviews and _ready(p, reviews[name]) and _bottom(p, repo, key, snap.get("stacks")):
            out.append((name, key, p))
    return out


def ring_hq(worker, key, p, reminder):
    """The poster athena watch uses: the watch line that wakes HQ to post the PR. It posts nothing itself."""
    return f"{worker} post for review #{p['number']}"


def _ask(poster, worker, key, p, reminder):
    try:
        return poster(worker, key, p, reminder)
    except Exception:  # noqa: BLE001: a poster that fails leaves the record, so it never posts twice
        return f"{worker} post for review failed"


def run(snap, poster, now=None) -> list:
    """Ask `poster` to post each due PR once, and once more after REMIND_AFTER while it is not recorded as posted.
    Returns the poster's lines. No channel configured: [] with no file read or written."""
    if not channel():
        return []
    now = time.time() if now is None else now
    posted = slack.posted()
    asked = _load()
    ready = {key: (name, p) for name, key, p in due(snap)}
    lines = []

    def ask(name, key, p, reminder):
        _save(asked)  # recorded before the poster runs: at most once, whatever happens next
        line = _ask(poster, name, key, p, reminder)
        if line:
            lines.append(line)

    for key, record in asked.items():  # a reminder only while the PR is still ready, and only once
        if key in posted or key not in ready or record.get("rings", 1) >= 2:
            continue
        if now - float(record.get("asked") or now) >= REMIND_AFTER:
            record["rings"] = 2
            name, p = ready[key]
            ask(name, key, p, True)
    for key, (name, p) in ready.items():
        if key not in posted and key not in asked:
            asked[key] = {"worker": name, "asked": now, "rings": 1}
            ask(name, key, p, False)
    return lines
