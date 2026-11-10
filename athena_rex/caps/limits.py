"""What Claude's own account is doing: a usage limit, expired sign-in, an API outage, or a switch of account. One sign for the
whole fleet, not one per worker, and the kernel keeps running through all of them.

Claude tells no hook about these. A sign-in and an API error it does report through Rex's program status (`kind: auth`,
`state: error`), so those come from there first. A usage limit is read from the screen, and so is anything the status missed. The rows
for 401, 429 and 529 were captured from a real Claude against a local API. The usage limit's line has never been seen on
a screen here: it comes from the installed binary's strings. A miss means only that a worker reads as idle, as it did before this existed.
"""
import calendar
import json
import re
import time
from datetime import datetime, timedelta

from athena_rex.cli import UserError
from athena_rex.kernel import agents, caps, ledger, store
from athena_rex.kernel.backend import BackendError

SUMMARY = "usage limits, sign-in and account changes, once for the fleet"
ACCOUNT_BOUND = ("connectors (Slack, Linear)", "artifacts it published", "the Chrome extension")
LIMIT = re.compile(r"^\W*You've hit your [\w -]*limit\s*·\s*resets\b.*$", re.M)  # a whole row of its own: a worker quoting it isn't one
RESET = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([\w/+-]+)\)", re.I)
AUTH = re.compile(r"^\W*(?:Please run /login\s*·\s*API Error: \d+"  # as a real Claude draws it: long, so it may wrap, and only its start is matched
                  r"|(?:API Error: \d+\b.*|[\w ]{0,40}[·.]\s*)?(?:Please run /login|OAuth token has expired|Invalid API key)(?:\W*|\W.{0,12})$)", re.M)  # rows of their own, as LIMIT is
API = re.compile(r"^\W*(?:API Error: (?:5\d\d|429)|overloaded_error)", re.M)
READ_EVERY = 60
API_BACKOFF = 300
UNKNOWN_RESET = 3600
HALF_DAY = timedelta(hours=12)
BOTTOM_ROWS = 12
CARRY_ON = "athena: the usage limit has reset. Carry on from where you were."
API_BACK = "athena: the API error looks over. Carry on from where you were."


def _park_file():
    return store.path("run", "limit.json")


def _account_file():
    return store.path("run", "account.json")


def read_reset(text, now):
    """When a limit's own words say it resets, in the zone they name, or None. The nearest such time to `now`, which can be past: the
    line stays on screen after its reset, and read as the next day's it would park everyone for a day."""
    found = RESET.search(text or "")
    if not found:
        return None
    hour = int(found.group(1)) % 12 + (12 if found.group(3).lower() == "pm" else 0)
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(found.group(4))
    except (ImportError, KeyError, ValueError, OSError):
        return None
    local = datetime.fromtimestamp(now, zone)
    try:
        at = local.replace(hour=hour, minute=int(found.group(2) or 0), second=0, microsecond=0)
    except ValueError:
        return None
    if at - local > HALF_DAY:
        at -= timedelta(days=1)
    elif local - at > HALF_DAY:
        at += timedelta(days=1)
    return calendar.timegm(at.utctimetuple())


def park(kind, until):
    store.write_json(_park_file(), {"kind": kind, "until": until})


def parked(now=None):
    """Until when the fleet is parked, or None. Any live worker's own mark counts, as a lost file must not start new work."""
    now = time.time() if now is None else now
    marks = [float(w.get("limit_until") or 0) for w in ledger.live()]
    marks.append(float(store.read_dict(_park_file()).get("until") or 0))
    latest = max(marks)
    return latest if latest > now else None


def why_not_rotate():
    """A fresh HQ can't use a limited account, or starts as someone else on another one."""
    until = parked()
    if until:
        return f"a usage limit holds until {time.strftime('%H:%M', time.localtime(until))}"
    return _account_changed()


def _read_account():
    """The account id Claude is signed in as, or None where it can't be read or may not be."""
    trust = caps.get("trust")
    if not trust:
        return None
    target = trust.config_path()
    if trust.why_not(target):
        return None
    data = store.read_dict(target)
    account = data.get("oauthAccount")
    return account.get("accountUuid") if isinstance(account, dict) else None


def _account_changed():
    now = _read_account()
    known = store.read_dict(_account_file()).get("account")
    if now and known and now != known:
        return "Claude's account has changed since it was last accepted: connectors and ownership move with it. `athena-rex limits accept` after looking"
    return None


def unavailable() -> list:
    """What moves with the account: while it differs from the accepted one, these act as someone else, or not at all."""
    return list(ACCOUNT_BOUND) if _account_changed() else []


def checks(ctx):
    now = _read_account()
    if not now:
        return [caps.Check("account", True, "not read here")]
    if not store.read_dict(_account_file()).get("account"):
        store.write_json(_account_file(), {"account": now, "ts": time.time()})
    changed = _account_changed()
    return [caps.Check("account", not changed, f"{changed}. Unavailable until then: {', '.join(ACCOUNT_BOUND)}" if changed else "unchanged")]


def _bottom(screen) -> str:
    """Only the last rows: a worker talking about limits has these words higher up its screen."""
    rows = [row for row in (screen or "").splitlines() if row.strip()]
    return "\n".join(rows[-BOTTOM_ROWS:])


def _carry_on(ctx, worker, name, text, **clear) -> bool:
    try:
        agents.send(ctx.backend, {**worker, "name": name}, text)
    except (agents.Refused, BackendError):
        return False
    ledger.append("update", name, **clear)
    return True


def _read_memory(name):
    return store.path("run", f"limits-{name}.json")


def _read_due(name, now) -> bool:
    return not 0 <= now - float(store.read_dict(_read_memory(name)).get("read") or 0) < READ_EVERY


def _reported(ctx, worker, name, now):
    """What Claude says of itself through Rex, or None when it can't be read now or it is busy: (is_busy, status)."""
    hook = store.read_dict(store.path("workers", f"{name}.json"))
    if hook.get("state") == "working" and now - float(hook.get("ts") or 0) < READ_EVERY:
        return True, None
    try:
        status = ctx.backend.program_status(agents.place_of(worker))
    except BackendError:
        return True, None
    return bool(status and status.get("state") == "working"), status or {}


def _sign_in_item():
    items = caps.get("items")
    if not items or any(i.get("name") == "claude-sign-in" for i in items.open_items()):
        return False
    items.add("Sign in to Claude: a session says its login has expired", name="claude-sign-in", kind="incident", next_="you")
    return True


def tick(ctx, now):
    from athena_rex.caps.watch import Line
    lines = []
    workers = [w for w in ledger.live() if ledger.managed(w) and w.get("block")]
    try:
        running = agents.running([w["name"] for w in workers])
    except agents.Unknown:
        return []
    told_limit = False
    for worker in workers:
        name = worker["name"]
        until = float(worker.get("limit_until") or 0)
        if until:
            if now >= until and name in running and _carry_on(ctx, worker, name, CARRY_ON, limit_until=None):
                lines.append(Line(f"{name} carries on after the usage limit", "routine", name))
            continue
        if name not in running or not _read_due(name, now):
            continue
        busy, status = _reported(ctx, worker, name, now)
        if busy:
            continue
        store.write_json(_read_memory(name), {"read": now})
        try:
            screen = _bottom(ctx.backend.screen(agents.place_of(worker)))
        except BackendError:
            continue
        limit = LIMIT.search(screen)
        if not limit and worker.get("limit_seen"):
            ledger.append("update", name, limit_seen=None)
        if limit:
            reset = read_reset(limit.group(0), now)
            seen = str(reset) if reset else limit.group(0)
            if worker.get("limit_seen") == seen:
                continue
            if reset and reset <= now:
                if _carry_on(ctx, worker, name, CARRY_ON, limit_seen=seen):
                    lines.append(Line(f"{name} carries on after the usage limit", "routine", name))
                continue
            until = reset or now + UNKNOWN_RESET
            ledger.append("update", name, limit_until=until, limit_seen=seen)
            if not told_limit:
                told_limit = True
                park("limit", until)
                lines.append(Line(f"usage limit hit: workers parked until {time.strftime('%H:%M', time.localtime(until))}", "quiet"))
        elif status.get("kind") == "auth" or AUTH.search(screen):
            if _sign_in_item():
                lines.append(Line("claude sign-in expired: one item filed", "quiet"))  # not told to HQ
        elif API.search(status.get("msg") or "" if status.get("state") == "error" else "") or API.search(screen):
            seen = float(worker.get("api_error_at") or 0)
            if not seen:
                ledger.append("update", name, api_error_at=now)
                lines.append(Line(f"{name} api error: waiting it out", "routine", name))
            elif now - seen >= API_BACKOFF and not worker.get("api_resumed"):
                if _carry_on(ctx, worker, name, API_BACK, api_resumed=True):
                    lines.append(Line(f"{name} told to carry on after an api error", "routine", name))
        elif worker.get("api_error_at"):
            ledger.append("update", name, api_error_at=None, api_resumed=None)
    return lines


def _configure(p):
    p.add_argument("action", nargs="?", choices=["show", "accept"], default="show")


def _run(args):
    if args.action == "accept":
        account = _read_account()
        if not account:
            raise UserError("Claude's account can't be read here")
        store.write_json(_account_file(), {"account": account, "ts": time.time()})
    until = parked()
    print(json.dumps({"parked_until": until, "account_changed": bool(_account_changed()), "unavailable": unavailable()}))
    return 0


def commands():
    return {"limits": caps.Command(SUMMARY, _configure, _run)}
