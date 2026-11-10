"""HQ's own review of a worker's PR, kept in the ledger at the head it reviewed: a push voids it."""
import sys
import time

from athena_rex.cli import UserError
from athena_rex.kernel import caps, gitops, ledger

SUMMARY = "record HQ's review of a worker's PR at a head"
FAILS_BEFORE_FORCE = 2
ROUNDS = 3
MAX_ROUNDS = 8


def current_head(worker):
    path = worker.get("path")
    return gitops.git(path, "rev-parse", "HEAD", check=False) if path else None


def _pr_head(worker):
    """`start` records the PR's head, as the old athena's did: the commit the reviewer will read on GitHub."""
    prs = caps.get("prs")
    found = prs.summary(worker.get("repo"), worker.get("branch")) if prs and worker.get("repo") and worker.get("branch") else None
    return (found or {}).get("head")


def record(name, verdict, head=None, force=False, now=None) -> dict:
    worker = ledger.get(name)
    if not worker or worker.get("state") != "live":
        raise UserError(f"no live worker named {name}")
    past = ledger.reviews(name)
    now = time.time() if now is None else now
    if verdict == "start":
        fails = sum(1 for r in past if r.get("verdict") == "fail")
        if fails >= FAILS_BEFORE_FORCE and not force:
            raise UserError(f"{name} failed {fails} review rounds. Review again with --force (up to {MAX_ROUNDS} rounds), "
                            f"or restart it fresh from the updated brief if the rounds stop converging")
        round_ = sum(1 for r in past if r.get("verdict") == "start") + 1
        if round_ > MAX_ROUNDS:
            raise UserError(f"{name} has had {MAX_ROUNDS} review rounds; bring the open findings to the human")
        head = head or _pr_head(worker) or current_head(worker)
        if not head:
            raise UserError(f"cannot read {name}'s head; pass --head")
        extra = {"round": round_}
        last = next((r for r in reversed(past) if r.get("verdict") in ("pass", "fail")), None)
        if last:
            extra["since"] = last.get("head")
        if round_ > ROUNDS:
            print(f"round {round_} for {name}: past {ROUNDS}, review only what changed since {extra.get('since') or 'the last verdict'}, "
                  "and bring open findings to the human", file=sys.stderr)
    else:
        last_start = next((r for r in reversed(past) if r.get("verdict") == "start"), None)
        if not head and not last_start:
            raise UserError(f"no review of {name} was started; run `review {name} start` first")
        head = head or last_start.get("head")
        extra = {"round": last_start.get("round")} if last_start else {}
    entry = {"verdict": verdict, "head": head, "at": now, **extra}
    ledger.append("review", name, review=entry)
    return entry


def _configure(p):
    p.add_argument("name")
    p.add_argument("verdict", choices=["start", "pass", "fail"])
    p.add_argument("--head")
    p.add_argument("--force", action="store_true")


def _run(args):
    entry = record(args.name, args.verdict, args.head, args.force)
    since = f", since {entry['since'][:7]}" if entry.get("since") else ""
    print(f"{args.name} review {entry['verdict']} at {entry['head'][:7]}, round {entry.get('round')}{since}")
    return 0


def commands():
    return {"review": caps.Command(SUMMARY, _configure, _run)}
