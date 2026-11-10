"""The Slack doorbell: rings `slack-new <id> <n>` when the human's DMs or configured channels have new messages. It reads
each message's author and time, never its text, and the token only ever goes in an Authorization header of read methods.

State is in its own slack-rex.json: the old athena's slack.json has its own shape and stays untouched.
"""
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from athena_rex.kernel import caps, config, store, world

SUMMARY = "ring when the human's DMs or watched channels have something new"
POLL_EVERY = 60
MOST = 40
MOST_WAIT = 900
TOKEN_ERRORS = {"token_revoked", "token_expired", "invalid_auth", "account_inactive", "not_authed"}


class SlackError(Exception):
    pass


class RateLimited(SlackError):
    def __init__(self, method, retry_after):
        super().__init__(f"{method}: ratelimited")
        self.retry_after = retry_after


def read_token():
    """From the Keychain, through the door. No item, a locked Keychain or a shut door all mean no doorbell."""
    try:
        found = world.run("security", "find-generic-password", "-s", "athena-slack", "-w", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (found.stdout.strip() or None) if found.returncode == 0 else None


class Client:
    def __init__(self, token):
        self._token = token

    def call(self, method, **params) -> dict:
        world.require("Slack")
        url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(method, int(exc.headers.get("Retry-After") or POLL_EVERY))
            raise SlackError(f"{method}: HTTP {exc.code}")
        except (urllib.error.URLError, ValueError, OSError) as exc:
            raise SlackError(f"{method}: {type(exc).__name__}")
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error')}")
        return data


COUNTED = (None, "thread_broadcast", "file_share", "me_message")
PAGES = 20
CHANNEL_HOLD = 300
_PR = __import__("re").compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def _pages(client, method, key, **params) -> list:
    """Every page of a listing, as the old athena read them: a cut-off list of conversations misses DMs."""
    out, cursor = [], None
    for _ in range(PAGES):
        data = client.call(method, limit=200, **({"cursor": cursor} if cursor else {}), **params)
        out += [c for c in data.get(key) or [] if isinstance(c, dict)]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def poll(client, state: dict, channels, allow_bots=(), active_minutes=15, now=None, open_prs=None) -> list:
    """One pass, on any client with `call`: the conversations to ring, as (id, count, dm, active). First sight starts at now. At most MOST
    conversations a pass, taken in turn, so every DM is read within a few passes. A rate limit stops the pass (RateLimited). A DM the
    human is replying in rings `active` (held from HQ); once their window lapses, what came after their last message rings once."""
    now = time.time() if now is None else now
    if not state.get("me"):
        state["me"] = client.call("auth.test").get("user_id")
    me, seen, last_mine = state["me"], state.setdefault("seen", {}), state.setdefault("mine", {})
    held = state.setdefault("held", {})
    first_poll = not state.get("polled")
    conversations = [(c, False) for c in channels]
    if open_prs and open_prs not in channels:
        conversations.append((open_prs, False))
    conversations += [(c["id"], True) for c in _pages(client, "users.conversations", "channels", types="im,mpim", exclude_archived="true")]
    if first_poll:  # first sight starts at now, for every conversation listed, read this pass or not
        for conversation, _ in conversations:
            seen.setdefault(conversation, str(now))
        state["polled"] = now
        return []
    for conversation, _ in conversations:  # seen from the moment it is first listed, read this pass or later, as the old athena did
        seen.setdefault(conversation, str(state.get("polled")))
    start = int(state.get("next") or 0) % max(len(conversations), 1)
    turn = (conversations[start:] + conversations[:start])[:MOST]
    rings = []
    done = 0
    for conversation, dm in turn:
        oldest = seen.get(conversation) or str(state.get("polled"))  # one first listed later starts at the last completed poll, as athena
        try:
            history = client.call("conversations.history", channel=conversation, oldest=oldest, limit=100)
        except RateLimited as exc:
            state["rate_limited"] = exc.retry_after  # what was read so far still rings; the rest waits for the next pass
            break
        except SlackError:
            done += 1
            continue
        done += 1
        count = 0
        newest = oldest
        for message in sorted(history.get("messages") or [], key=lambda m: float(m.get("ts") or 0)):
            if float(message.get("ts") or 0) <= float(oldest):
                continue
            newest = max(newest, str(message.get("ts")), key=float)
            if message.get("user") == me:
                last_mine[conversation] = float(message.get("ts"))
                held.pop(conversation, None)
                if conversation == open_prs:
                    _record_posts(message)
                continue
            if message.get("subtype") not in COUNTED:
                continue
            if message.get("bot_id") and message.get("bot_id") not in allow_bots:
                continue
            if conversation == open_prs and conversation not in channels:
                continue
            count += 1
        seen[conversation] = newest
        if count:
            active = dm and active_minutes and now - float(last_mine.get(conversation) or 0) < active_minutes * 60
            if active:
                held[conversation] = int(held.get(conversation) or 0) + count
            rings.append((conversation, count, dm, bool(active)))
    state["next"] = start + done
    for conversation in list(held):
        if not active_minutes or now - float(last_mine.get(conversation) or 0) >= active_minutes * 60:
            rings.append((conversation, int(held.pop(conversation)), True, False))
    state["polled"] = now
    return rings


def _record_posts(message):
    """The human's own post in #open-prs: the PRs it links count as posted, as the old athena's #open-prs read kept them. Nothing of
    the message but a PR's key and its ts is kept."""
    keys = {f"{m.group(1)}#{m.group(2)}".lower() for m in _PR.finditer(str(message.get("text") or ""))}
    if not keys:
        return
    target = store.path("open-prs-posted.json")
    with store.locked("locks/open-prs.lock"):
        data = store.read_dict(target)
        for key in keys:
            if key not in data or float(data[key]) < float(message.get("ts") or 0):
                data[key] = str(message.get("ts"))
        store.write_json(target, data, indent=2)


def hold_channels(state, rings, now) -> list:
    """A channel wakes HQ at most once in CHANNEL_HOLD, its count summed meanwhile; a DM at once."""
    told, waiting = state.setdefault("channel_told", {}), state.setdefault("channel_held", {})
    out = []
    for conversation, count, dm, active in rings:
        if dm:
            out.append((conversation, count, dm, active))
            continue
        waiting[conversation] = int(waiting.get(conversation) or 0) + count
    for conversation in list(waiting):
        if now - float(told.get(conversation) or 0) >= CHANNEL_HOLD:
            out.append((conversation, int(waiting.pop(conversation)), False, False))
            told[conversation] = now
    return out


def _file():
    return store.path("slack-rex.json")


def tick(ctx, now):
    from athena_rex.caps.watch import Line
    state = store.read_dict(_file())
    if now - float(state.get("polled") or 0) < POLL_EVERY or now < float(state.get("retry_at") or 0):
        return []
    if not world.available("security"):
        return []
    token = read_token()
    if not token:
        store.write_json(_file(), {**state, "polled": now, "off": "no athena-slack keychain item, or the keychain is locked"})
        return []
    try:
        rings = poll(Client(token), state, config.get("slack_channels") or [], config.get("slack_allow_bots") or [],
                     config.get("slack_dm_active_minutes"), now, config.get("open_prs_channel") or None)
        rings = hold_channels(state, rings, now)
        limited = state.pop("rate_limited", None)
        if limited:
            state["retry_at"] = now + float(limited)
    except RateLimited as exc:
        store.write_json(_file(), {**state, "polled": now, "retry_at": now + exc.retry_after})
        return []
    except SlackError as exc:
        return _failed(state, str(exc), now, Line)
    for key in ("off", "fails", "told_off") + (() if limited else ("retry_at",)):
        state.pop(key, None)
    store.write_json(_file(), state)
    lines = []
    for conversation, count, dm, active in rings:
        suffix = " dm" if dm else ""
        lines.append(Line(f"slack-new {conversation} {count}{suffix}{' active' if active else ''}",
                          "quiet" if active else "wake"))
    return lines


def _failed(state, error, now, Line) -> list:
    """An outage backs off in silence; the doctor check shows it. A dead token needs a person, so it is told, once."""
    reason = error.rsplit(": ", 1)[-1]
    fails = int(state.get("fails") or 0) + 1
    state.update(polled=now, fails=fails, retry_at=now + min(POLL_EVERY * 2 ** fails, MOST_WAIT))
    lines = []
    if reason in TOKEN_ERRORS:
        state["off"] = f"Slack says {reason}: the token needs replacing"
        if state.get("told_off") != reason:
            state["told_off"] = reason
            lines.append(Line(f"slack doorbell off {reason}", "wake"))
    store.write_json(_file(), state)
    return lines


def checks(ctx):
    """A doorbell that is off says so, on the dash, instead of letting the human believe their DMs are watched."""
    if not world.available("security"):
        return [caps.Check("slack", True, "not run here")]
    state = store.read_dict(_file())
    if state.get("off"):
        return [caps.Check("slack", False, f"doorbell off: {state['off']}")]
    age = time.time() - float(state.get("polled") or 0)
    return [caps.Check("slack", age < 600 or not state.get("polled"), f"last read {int(age)}s ago")]


def _configure(p):
    p.add_argument("action", choices=["status"])


def _run(args):
    state = store.read_dict(_file())
    print(json.dumps({"polled": state.get("polled"), "conversations": len(state.get("seen") or {})}))
    return 0


def commands():
    return {"slack": caps.Command(SUMMARY, _configure, _run)}
