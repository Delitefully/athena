"""The Slack doorbell: rings `slack-new <id> <n>` when the human's DMs or configured channels have new messages, and
`slack-new <id> <n> [dm] thread <ts>` for new replies in a thread of a DM, or of a channel's thread the human started or replied
in. It reads each message's author and time; its text only to test, in memory, whether it addresses Claude or athena
(`_addressed`), keeping nothing of it but that yes or no. The token only ever goes in an Authorization header of read methods.

State is in its own slack-rex.json: the old athena's slack.json has its own shape and stays untouched.
"""
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from athena.kernel import caps, config, store, world

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
THREAD_LOOKBACK = 7 * 86400  # a thread whose parent is older, or not in the newest 100 messages read, is left to the sweep
PAGES = 20
PAGE = 100
CHANNEL_HOLD = 300
_PR = __import__("re").compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_ADDRESS = __import__("re").compile(r"\b(?:claude|athena)\b", __import__("re").IGNORECASE)


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


def poll(client, state: dict, channels, allow_bots=(), active_minutes=15, now=None, open_prs=None, claude_user=None) -> list:
    """One pass, on any client with `call`: the conversations to ring, as (id, count, dm, active), and a thread's new replies as
    (id, count, dm, active, thread_ts). First sight starts at now. At most MOST conversations a pass, taken in turn, so every DM is
    read within a few passes. A rate limit stops the pass (RateLimited). A DM the human is replying in, at the top or in a thread,
    rings `active` (held from HQ); once their window lapses, what came after their last message rings once. What the human posted
    through an app (HQ, through the Claude connector) is not them at the keyboard (`_by_app`). What they already read in Slack does
    not ring, unless it addresses Claude or athena (`_addressed`): a conversation with something new reads its `last_read`
    (`_last_read`), one call; without it, it rings as before. Each conversation is read newest first back to its cursor (`_read`);
    one more than a page behind catches up in one line, its count real."""
    now = time.time() if now is None else now
    if not state.get("me"):
        state["me"] = client.call("auth.test").get("user_id")
    me, seen, last_mine = state["me"], state.setdefault("seen", {}), state.setdefault("mine", {})
    held, held_threads = state.setdefault("held", {}), state.setdefault("held_threads", {})
    state.setdefault("threads_from", state.get("polled") or now)  # threads are followed from the pass that first followed them
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
            history, pages = _read(client, conversation, oldest)
        except RateLimited as exc:
            state["rate_limited"] = exc.retry_after  # what was read so far still rings; the rest waits for the next pass
            break
        except SlackError:
            done += 1
            continue
        done += 1
        counted, app_posts = [], []
        newest = oldest
        messages = sorted(history, key=lambda m: float(m.get("ts") or 0))
        for message in messages:
            if float(message.get("ts") or 0) <= float(oldest):
                continue
            newest = max(newest, str(message.get("ts")), key=float)
            if message.get("user") == me:
                if not _by_app(message):
                    last_mine[conversation] = float(message.get("ts"))
                    held.pop(conversation, None)
                else:
                    app_posts.append(float(message.get("ts")))
                if conversation == open_prs:
                    _record_posts(message)
                continue
            if message.get("subtype") not in COUNTED:
                continue
            if message.get("bot_id") and message.get("bot_id") not in allow_bots:
                continue
            if conversation == open_prs and conversation not in channels:
                continue
            counted.append((float(message.get("ts")), _addressed(message, claude_user)))
        seen[conversation] = newest
        threads, limited = [], False
        if not (conversation == open_prs and conversation not in channels):
            parents = [m for m in messages if float(m.get("ts") or 0) >= now - THREAD_LOOKBACK]
            threads, limited = _threads(client, state, conversation, dm, parents, oldest, me, allow_bots, now, app_posts, claude_user)
        stop = False
        if counted and not limited:
            read, stop = _last_read(client, state, conversation)
            if read is not None and any(read >= posted for posted in app_posts):
                read = None  # the human's app post this pass may have moved it past what they never saw: ring as before
            counted = [(ts, to_hq) for ts, to_hq in counted if read is None or ts > read or to_hq]  # addressed rings, read or not
        count = len(counted)
        if pages > 1 and threads:  # a catch-up of more than a page rings one line, its threads' replies in its count
            count += sum(replies for _, replies in threads)
            threads = []
        active = bool(dm and active_minutes and now - float(last_mine.get(conversation) or 0) < active_minutes * 60)
        if count:
            if active:
                held[conversation] = int(held.get(conversation) or 0) + count
            rings.append((conversation, count, dm, active))
        for thread, replies in threads:
            if active:
                waiting = held_threads.setdefault(conversation, {})
                waiting[thread] = int(waiting.get(thread) or 0) + replies
            rings.append((conversation, replies, dm, active, thread))
        if limited:  # first again next pass, for the threads it did not reach; what was counted keeps its cursors
            done -= 1
            break
        if stop:  # read whole, its read cursor rate-limited: it rang as before, and the rest waits for the next pass
            break
    state["next"] = start + done
    lapsed = lambda c: not active_minutes or now - float(last_mine.get(c) or 0) >= active_minutes * 60
    for conversation in list(held):
        if lapsed(conversation):
            rings.append((conversation, int(held.pop(conversation)), True, False))
    for conversation in list(held_threads):
        if lapsed(conversation):
            for thread, replies in sorted(held_threads.pop(conversation).items(), key=lambda t: float(t[0])):
                rings.append((conversation, int(replies), True, False, thread))
    state["polled"] = now
    return rings


def _read(client, conversation, oldest) -> tuple:
    """A conversation's messages, newest first, a page at a time back to the cursor `oldest`: (messages, pages read). The first page
    is always read whole, as its newest PAGE messages are where threads' parents are found. Never Slack's `oldest` parameter: given
    without `latest`, Slack pages from the oldest message after it, so a conversation with more than a page in the window never
    showed its newest messages. At most PAGES pages: a cursor further back than that counts what they held."""
    messages, cursor, pages = [], None, 0
    while pages < PAGES:
        data = client.call("conversations.history", channel=conversation, limit=PAGE, **({"cursor": cursor} if cursor else {}))
        pages += 1
        batch = [m for m in data.get("messages") or [] if isinstance(m, dict)]
        messages += batch
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor or not batch or min(float(m.get("ts") or 0) for m in batch) <= float(oldest):
            break
    return messages, pages


def _by_app(message) -> bool:
    """The human's account posting through an app, as HQ does through the Claude connector: Slack gives it an `app_id` (and no
    `client_msg_id`); what the human types in Slack never has one."""
    return bool(message.get("app_id"))


def _addressed(message, claude_user=None) -> bool:
    """Whether a message speaks to Claude or athena ("Claude, ...", "hi athena", a mention of the Claude app's user, config
    `slack_claude_user`, as <@U...> or <@U...|name>): those ring whether or not the human read them, as HQ must answer. The text is
    only matched here, in memory; nothing of it is kept or logged."""
    text = str(message.get("text") or "")
    return bool(_ADDRESS.search(text)) or bool(claude_user) and any(f"<@{claude_user}{end}" in text for end in ">|")


def _last_read(client, state, conversation) -> tuple:
    """How far the human has read the conversation in Slack, from conversations.info's `last_read`, and whether a rate limit stopped
    the call: (ts or None, limited). None rings as before; why goes in `read_cursor_off`, told once by `tick`. A rate limit is not a
    missing cursor."""
    try:
        channel = client.call("conversations.info", channel=conversation).get("channel")
    except RateLimited as exc:
        state["rate_limited"] = exc.retry_after
        return None, True
    except SlackError as exc:
        state["read_cursor_off"] = str(exc)
        return None, False
    read = _ts(channel.get("last_read") if isinstance(channel, dict) else None)
    if read is None:
        state["read_cursor_off"] = "conversations.info: no last_read"
    return read, False


def _ts(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _threads(client, state, conversation, dm, messages, oldest, me, allow_bots, now, app_posts=None, claude_user=None):
    """The new replies in one conversation's threads, as [(thread_ts, count)], and whether a rate limit stopped the read. A parent's
    latest_reply says which threads moved, so only those are read, with one conversations.replies call each. A DM's every thread
    counts; a channel's only when the human started it, is among its reply_users, or was seen replying in it (Slack lists five
    reply_users at most, so a longer list's thread is read whole to see whether the human is in it, and one that is not theirs is
    skipped in `not_mine` until its latest_reply moves). Each thread keeps its own cursor in `threads`, so a reply is counted once;
    one first seen starts at the conversation's cursor before this pass, never before the pass that first followed threads. Every
    thread to read has its start saved before the first read, so one a rate limit or a failure leaves unread keeps it. The human's
    own replies and bots are skipped, and the human's reply marks the DM active, unless posted through an app; those go in
    `app_posts`. A reply at or before the parent's `last_read` (the human's read cursor in that thread, on the parent as
    conversations.replies gives it, else as conversations.history did) is not counted, unless it addresses Claude or athena; a parent
    without one, or one at or past the human's app reply in this read, counts as before."""
    known = state.setdefault("threads", {}).setdefault(conversation, {})
    not_mine = state.setdefault("not_mine", {}).setdefault(conversation, {})
    unsure_from = state.setdefault("unsure_from", {}).setdefault(conversation, {})
    last_mine, held = state.setdefault("mine", {}), state.setdefault("held_threads", {})
    start = str(max(float(oldest), float(state.get("threads_from") or 0)))
    todo = []
    for parent in messages:
        thread = str(parent.get("ts") or "")
        if not thread or parent.get("thread_ts") != thread or not parent.get("latest_reply"):
            continue
        latest, users = str(parent["latest_reply"]), parent.get("reply_users") or []
        unsure = not dm and thread not in known and parent.get("user") != me and me not in users
        if unsure and (int(parent.get("reply_users_count") or 0) <= len(users) or float(latest) <= float(not_mine.get(thread) or 0)):
            continue
        cursor = known.get(thread) or unsure_from.get(thread) or max(start, str(not_mine.get(thread) or start), key=float)
        if float(latest) <= float(cursor):
            continue
        (unsure_from if unsure else known)[thread] = cursor  # saved before any read: a stopped pass keeps every start
        todo.append((thread, cursor, unsure, latest, _ts(parent.get("last_read"))))
    found = []
    for thread, cursor, unsure, latest, read in todo:
        try:
            replies = client.call("conversations.replies", channel=conversation, ts=thread, limit=200,
                                  **({} if unsure else {"oldest": cursor}))
        except RateLimited as exc:
            state["rate_limited"] = exc.retry_after  # the threads not read keep their saved start for the next pass
            return found, True
        except SlackError:
            continue
        thread_messages = replies.get("messages") or []
        head = next((m for m in thread_messages if str(m.get("ts")) == thread), None) or {}
        read = _ts(head.get("last_read")) if "last_read" in head else read
        by_app = [float(r.get("ts")) for r in thread_messages if r.get("user") == me and _by_app(r)
                  and str(r.get("ts")) != thread and float(r.get("ts") or 0) > float(cursor)]
        if read is not None and any(read >= posted for posted in by_app):
            read = None  # the human's app reply may have moved it past what they never saw: ring as before
        if app_posts is not None:
            app_posts += by_app
        unsure_from.pop(thread, None)
        if unsure and not any(reply.get("user") == me for reply in thread_messages):
            not_mine[thread] = latest  # not the human's thread: not read again until a newer reply
            continue
        not_mine.pop(thread, None)
        count, newest = 0, cursor
        for reply in sorted(thread_messages, key=lambda m: float(m.get("ts") or 0)):
            ts = str(reply.get("ts") or "0")
            if ts == thread or float(ts) <= float(cursor):
                continue
            newest = max(newest, ts, key=float)
            if reply.get("user") == me:
                if not _by_app(reply):
                    last_mine[conversation] = max(float(last_mine.get(conversation) or 0), float(ts))
                    held.get(conversation, {}).pop(thread, None)
                continue
            if read is not None and float(ts) <= read and not _addressed(reply, claude_user):  # the human read it, and it is not to HQ
                continue
            if reply.get("subtype") not in COUNTED or reply.get("subtype") == "thread_broadcast":  # a broadcast counts at the top
                continue
            if reply.get("bot_id") and reply.get("bot_id") not in allow_bots:
                continue
            count += 1
        known[thread] = newest
        if count:
            found.append((thread, count))
    for table in (known, not_mine, unsure_from):
        for thread in [t for t in table if float(t) < now - THREAD_LOOKBACK]:
            del table[thread]
    return found, False


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
            if key not in data or (_ts(data[key]) or 0.0) < (_ts(message.get("ts")) or 0.0):  # a damaged entry is written over
                data[key] = str(message.get("ts"))
        store.write_json(target, data, indent=2)


def hold_channels(state, rings, now) -> list:
    """A channel wakes HQ at most once in CHANNEL_HOLD, its count summed meanwhile; a DM at once. A channel's thread is held on its
    own, as `<channel> <thread_ts>`."""
    told, waiting = state.setdefault("channel_told", {}), state.setdefault("channel_held", {})
    out = []
    for ring in rings:
        conversation, count, dm = ring[:3]
        if dm:
            out.append(ring)
            continue
        key = " ".join((conversation, *ring[4:]))
        waiting[key] = int(waiting.get(key) or 0) + count
    for key in list(waiting):
        if now - float(told.get(key) or 0) >= CHANNEL_HOLD:
            conversation, *thread = key.split(" ")
            out.append((conversation, int(waiting.pop(key)), False, False, *thread))
            told[key] = now
    for key in [k for k in told if k not in waiting and now - float(told[k] or 0) >= CHANNEL_HOLD]:
        del told[key]  # a lapsed hold says nothing more than none, and thread keys would pile up
    return out


def ring_line(ring) -> str:
    """`slack-new <id> <n>`, then ` dm`, ` thread <thread_ts>` and ` active` as they apply."""
    conversation, count, dm, active = ring[:4]
    thread = f" thread {ring[4]}" if len(ring) > 4 else ""
    return f"slack-new {conversation} {count}{' dm' if dm else ''}{thread}{' active' if active else ''}"


def _file():
    return store.path("slack-rex.json")


def tick(ctx, now):
    from athena.caps.watch import Line
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
                     config.get("slack_dm_active_minutes"), now, config.get("open_prs_channel") or None,
                     config.get("slack_claude_user") or None)
        rings = hold_channels(state, rings, now)
        limited = state.pop("rate_limited", None)
        if limited:
            state["retry_at"] = now + float(limited)
    except RateLimited as exc:
        store.write_json(_file(), {**state, "polled": now, "retry_at": now + exc.retry_after})
        return []
    except SlackError as exc:
        return _failed(state, str(exc), now, Line)
    off = state.get("read_cursor_off")
    if off and state.get("read_cursor_told") != off:  # told once: the doorbell rings as before without it
        from athena.caps.watch import suppress
        suppress(f"slack read cursor unavailable: {off}", "what the human read in Slack still rings")
        state["read_cursor_told"] = off
    for key in ("off", "fails", "told_off") + (() if limited else ("retry_at",)):
        state.pop(key, None)
    store.write_json(_file(), state)
    return [Line(ring_line(ring), "quiet" if ring[3] else "wake") for ring in rings]


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
