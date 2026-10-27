"""athena watch's Slack doorbell: `slack-new <channel-id> <n>` when new messages reach the human, never their text.

It reads the human's Slack user token from the macOS Keychain (`security find-generic-password -s athena-slack -w`),
once, and only ever calls read methods with it. About once a minute it asks for messages newer than the last one it
saw in each conversation: the human's DMs and group DMs, the channels in config `slack_channels`, and (config
`slack_ext_channels`) every `ext-` channel the human is a member of. It counts top-level messages, and replies in
threads the human started that it saw start (followed for two days); replies in a thread the human only joined are
not counted (HQ's fallback sweep reads those). The human's own messages, bots and apps (but those in config
`slack_allow_bots`) and housekeeping (joins, edits, deletions) do not count. A DM the human wrote in within config
`slack_dm_active_minutes` rings `slack-new <dm> <n> dm active`, which the watch logs instead of telling HQ; when that
window lapses, the messages after the human's last one ring once.

What it keeps, in slack.json in the state dir: the last-seen ts per conversation and per followed thread, the counts
it has not rung yet, and per DM the ts of the human's last message and how many counted ones came after it. Never a
message's text, its author or a link to it. The token goes in the Authorization
header and nowhere else: not a URL, an error, a log line or a file.

Review-channel posts (config `open_prs_channel`): open-prs-posted.json in the state dir records the PRs posted there
for review, `{"owner/name#n": "<ts>"}`, nothing else of the message. `athena open-prs add` writes it once HQ has
posted one (athena_lib/reviewpost.py says when a PR is due). This module never posts: its token only reads.
"""
import fcntl
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from http.client import HTTPException

from athena_lib import config, paths

API = "https://slack.com/api/"
KEYCHAIN_SERVICE = "athena-slack"
OFF = "slack doorbell off: no athena-slack keychain item"
READ_METHODS = frozenset({"auth.test", "conversations.list", "users.conversations", "conversations.history",
                          "conversations.replies"})

EVERY = 60  # seconds between polls
BACKOFF_MAX = 900  # the longest wait after errors in a row
LIST_EVERY = 600  # how often the ext- channel list is read again (the DM list, for its `latest`, every poll)
HISTORY_BUDGET = 40  # conversations.history calls a poll may make (Tier 3 allows 50 and more a minute)
CHANNEL_SHARE = 12  # of which channels get at most this many (more channels take turns), so DMs always get the rest
ACTIVE = 7 * 24 * 3600  # a DM with a message this recent is read every poll; quieter ones take turns
PROBE_FLOOR = 10  # first reads a poll makes at least while some DMs' last activity is unknown
KEEP_QUIET = 2  # reads a poll keeps for the quiet DMs' turns, however many DMs are active
PROBE_LIMIT = 20  # a DM's first read, which learns when it was last active, looks at this many of its messages
HISTORY_LIMIT = 200  # new messages a read counts at most: plenty for a minute, and a ring past it still rings
THREAD_MAX = 10  # threads followed at once, the newest
THREAD_FOLLOW = 2 * 24 * 3600
SKIP_FOR = 3600  # a channel Slack refuses (not_in_channel, channel_not_found) is left alone this long
TIMEOUT = 15
COUNT_MAX = 999999
POSTED_KEEP = 90 * 24 * 3600  # a recorded post this old is dropped

CHANNEL_ID = re.compile(r"^[A-Z][A-Z0-9]{5,20}$")
_ERROR_CODE = re.compile(r"[a-z_]{1,60}")
COUNTED_SUBTYPES = (None, "thread_broadcast", "file_share", "me_message")
# A GitHub PR link, bare or in Slack's <url> and <url|label> forms; the key is the dash's prKey, in lower case.
_PR_LINK = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/pull/"
                      r"([1-9][0-9]{0,9})(?![0-9])")
_PR_KEY = re.compile(r"^[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}#[1-9][0-9]{0,9}$")  # the dash's prKeyText


# Errors that say the token itself no longer reads (revoked, a missing scope): not one channel's refusal.
TOKEN_ERRORS = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive",
                          "missing_scope", "no_permission", "not_allowed_token_type", "ekm_access_denied"})


class SlackError(Exception):
    """A Slack call that failed. Its message is built from fixed words, the method and Slack's error code only."""

    def __init__(self, message, network=False, code=None):
        super().__init__(message)
        self.network = network
        self.code = code

    @property
    def token_wide(self) -> bool:
        return self.code in TOKEN_ERRORS


class RateLimited(SlackError):
    def __init__(self, retry_after):
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


def read_token():
    """The token from the Keychain, or None when there is no `athena-slack` item (or no `security`)."""
    try:
        proc = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    token = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    return token or None


def _retry_after(headers) -> int:
    try:
        return min(max(int((headers or {}).get("Retry-After")), 1), 3600)
    except (TypeError, ValueError):
        return EVERY


class Client:
    """Read-only Slack Web API calls with a user token."""

    def __init__(self, token, opener=None, timeout=TIMEOUT):
        self._token = token
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    def __repr__(self):
        return "<slack.Client>"

    def redact(self, text) -> str:
        text = str(text)
        return text.replace(self._token, "[token]") if self._token else text

    def call(self, method, **params) -> dict:
        if method not in READ_METHODS:
            raise SlackError(f"{method} is not a read method: the doorbell only reads")
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(API + method + (f"?{query}" if query else ""),
                                     headers={"Authorization": f"Bearer {self._token}"})
        fail, body = None, b""
        # Each failure is rebuilt outside the except block, so it carries none of the original's text or context.
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            fail = (RateLimited(_retry_after(exc.headers)) if status == 429
                    else SlackError(f"{method}: HTTP {status}", network=status >= 500,
                                    code="invalid_auth" if status in (401, 403) else None))
        except (urllib.error.URLError, OSError, HTTPException, ValueError) as exc:
            fail = SlackError(f"{method}: network error ({type(exc).__name__})", network=True)
        if fail:
            raise fail
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SlackError(f"{method}: an answer that is not a JSON object", network=True)
        if not data.get("ok"):
            found = _ERROR_CODE.match(str(data.get("error") or ""))
            code = found.group(0) if found else "unknown_error"
            if code == "ratelimited":
                raise RateLimited(EVERY)
            raise SlackError(f"{method}: {code}", code=code)
        return data


def _ts(value):
    try:
        found = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return found if found.is_finite() else None


def _fmt(seconds) -> str:
    return f"{seconds:.6f}"


def _latest(conversation):
    """A DM's last message ts when conversations.list says it, else None."""
    latest = conversation.get("latest")
    if isinstance(latest, dict):
        latest = latest.get("ts")
    return latest if _ts(latest) is not None else None


def _state_file():
    return paths.state_dir() / "slack.json"


def pr_key(given):
    """`owner/name#n` (any case) or a PR's GitHub URL as its key, else None."""
    if not isinstance(given, str):
        return None
    found = pr_keys(given)
    if found:
        return found[0] if len(found) == 1 else None
    return given.lower() if _PR_KEY.match(given.lower()) else None


def pr_keys(text) -> list:
    """The `owner/name#n` keys of the GitHub PR links in a message's text, in order, each once."""
    if not isinstance(text, str):
        return []
    keys = [f"{m.group(1)}/{m.group(2)}#{m.group(3)}".lower() for m in _PR_LINK.finditer(text)]
    return [k for k in dict.fromkeys(keys) if _PR_KEY.match(k)]


def posted_file():
    return paths.state_dir() / "open-prs-posted.json"


def _parse_posted(text) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    # As the dash reads it (dash/state.go parsePosted): a key in any case, a ts as a string or a number.
    return {k.lower(): str(v) for k, v in data.items() if isinstance(k, str) and _PR_KEY.match(k.lower())
            and isinstance(v, (str, int, float)) and not isinstance(v, bool) and _ts(v) is not None}


def posted() -> dict:
    """open-prs-posted.json: the PRs posted in the review channel, `{"owner/name#n": "<ts>"}`; {} when unreadable."""
    try:
        return _parse_posted(posted_file().read_text())
    except OSError:
        return {}


def record_posted(found, now=None) -> dict:
    """Add `found` ({key: ts}) to open-prs-posted.json, keeping each key's latest post (a re-post after a teammate's
    review counts from then) and dropping posts older than POSTED_KEEP. Writers hold open-prs-posted.lock; the file is
    created if missing and rewritten only when it changes."""
    now = time.time() if now is None else now
    paths.ensure()
    target = posted_file()
    with open(target.with_name("open-prs-posted.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            before = target.read_text()
        except OSError:
            before = None
        merged = {k: v for k, v in _parse_posted(before or "").items() if now - float(_ts(v)) < POSTED_KEEP}
        for key, ts in found.items():
            if _PR_KEY.match(key) and _ts(ts) is not None and now - float(_ts(ts)) < POSTED_KEEP and \
                    (key not in merged or _ts(ts) > _ts(merged[key])):
                merged[key] = str(ts)
        text = json.dumps(merged, indent=1, sort_keys=True) + "\n"
        if text != before:
            tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
            tmp.write_text(text)
            os.replace(tmp, target)
    return merged


def _settings() -> dict:
    try:
        loaded = config.load()
    except config.ConfigError:
        loaded = {}
    cfg = {key: loaded.get(key, config.DEFAULTS[key]) for key in ("slack_channels", "slack_ext_channels",
                                                                    "slack_allow_bots", "slack_dm_active_minutes")}
    channels = [c for c in cfg.get("slack_channels") or [] if isinstance(c, str) and CHANNEL_ID.match(c)]
    allow = {b for b in cfg.get("slack_allow_bots") or [] if isinstance(b, str) and b}
    minutes = cfg.get("slack_dm_active_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 <= minutes <= 24 * 60:
        minutes = config.DEFAULTS["slack_dm_active_minutes"]
    return {"channels": channels, "ext": bool(cfg.get("slack_ext_channels")), "allow": allow,
            "dm_active": minutes * 60}


class Doorbell:
    """Polls Slack about once a minute; `check` returns the `slack-new` lines of a poll that is due, else []."""

    def __init__(self, token_reader=None, now=time.time, every=EVERY, window=0, opener=None):
        self._token_reader = token_reader or read_token
        self._now = now
        self.every = every
        self.window = window  # a first sight reaches back this far (a smoke run); 0 starts at now
        self._opener = opener
        self._client = None
        self._call = None
        self.off = False
        self.error = None  # the last error told, so a repeat is told once
        self._next_at = 0.0
        self._failures = 0
        self._me = None
        self._dms, self._ext, self._listed_at = [], [], None
        self._dm_ids = set()
        self._cursor = 0
        self._channel_cursor = 0
        self._active_cursor = 0
        self.stats = {}
        self._skip = {}  # channel -> until when Slack's refusal is respected
        self._notes = []
        self._state = None

    def start(self, call=None):
        """Read the token (or take `call`, a stand-in for Client.call) and the saved state. Returns OFF when there
        is no token: the doorbell stays off and `check` returns nothing."""
        if call is None:
            token = self._token_reader()
            if not token:
                self.off = True
                return OFF
            self._client = Client(token, opener=self._opener) if self._opener else Client(token)
            call = self._client.call
        self._call = call
        self._state = self._load()
        return None

    def redact(self, text) -> str:
        return self._client.redact(text) if self._client else str(text)

    def summary(self) -> str:
        """Counts only, for a smoke run: what the last poll knew of the DMs, and how often an active one is read."""
        st = self.stats
        if not st:
            return f"{self.known()} conversations known"
        room = max(st["active_room"], 1)
        polls = -(-st["active"] // room) if st["active"] else 1
        return (f"{self.known()} conversations known; {st['dms']} DMs: {st['dated']} dated by Slack, "
                f"{st['active']} active this week, {st['unknown']} not read yet; {st['reads']} reads this poll; "
                f"an active DM is read every {polls} poll(s), about {polls * self.every:.0f}s at worst")

    def known(self) -> int:
        """How many conversations it has a last-seen ts for."""
        return len((self._state or {}).get("seen") or {})

    def notes(self) -> list:
        """Things worth a line on stderr (a channel Slack refused), each once."""
        out, self._notes = self._notes, []
        return out

    def check(self) -> list:
        if self._call is None and not self.off:
            self.start()
        if self.off:
            return []
        now = self._now()
        if now < self._next_at:
            return []
        self._next_at = now + self.every
        try:
            lines = self._poll(now)
        except RateLimited as exc:
            self._next_at = now + max(exc.retry_after, self.every)
            return []
        except SlackError:
            self._failures += 1
            self._next_at = now + min(self.every * 2 ** self._failures, BACKOFF_MAX)
            raise
        self._failures = 0
        return lines

    # The state file: seen ts per conversation, followed threads, counts not rung yet. Never message content.

    def _load(self) -> dict:
        try:
            data = json.loads(_state_file().read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        def part(key):
            return data.get(key) if isinstance(data.get(key), dict) else {}
        def stamps(key):
            return {ch: ts for ch, ts in part(key).items() if isinstance(ts, str) and _ts(ts) is not None}
        polled = data.get("polled")
        return {"polled": polled if isinstance(polled, str) and _ts(polled) is not None else None,
                "seen": stamps("seen"), "active": stamps("active"),
                "threads": {ch: {t: last for t, last in ts.items() if isinstance(last, str) and _ts(last) is not None}
                            for ch, ts in part("threads").items() if isinstance(ts, dict)},
                "unrung": {ch: n for ch, n in part("unrung").items() if isinstance(n, int) and n > 0},
                "mine": stamps("mine"),
                "after": {ch: n for ch, n in part("after").items() if isinstance(n, int) and n >= 0},
                "held": sorted({ch for ch in data.get("held") or [] if isinstance(ch, str) and CHANNEL_ID.match(ch)})}

    def _save(self):
        paths.ensure()
        target = _state_file()
        tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(self._state, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, target)

    def _poll(self, now) -> list:
        try:
            if self._me is None:
                self._me = self._call("auth.test").get("user_id") or ""
            cfg = _settings()
            self._refresh_lists(now, cfg)
            self._read_conversations(now, cfg)
            self._read_threads(now, cfg)
            unrung = self._state["unrung"]
            dms = self._dm_ids | set(self._state["held"])
            active = self._hold(now, cfg, dms)
            lines = [f"slack-new {ch} {min(int(n), COUNT_MAX)}{' dm' if ch in dms else ''}"
                     f"{' active' if ch in active else ''}"
                     for ch, n in sorted(unrung.items()) if CHANNEL_ID.match(ch) and isinstance(n, int) and n > 0]
            self._state["unrung"] = {}
            self._state["polled"] = _fmt(now)  # a conversation first seen next poll is read from here
            return lines
        finally:
            if self._state is not None:
                self._save()

    def _hold(self, now, cfg, dms) -> set:
        """The DMs the human wrote in within config `slack_dm_active_minutes`, which ring ` dm active` (the watch
        keeps those out of HQ); a DM whose window has lapsed rings, once, the messages that came after the human's
        last one."""
        unrung, mine, after = self._state["unrung"], self._state["mine"], self._state["after"]
        held, active = set(self._state["held"]), set()
        for ch in sorted(dms):
            if cfg["dm_active"] and ch in mine and now - float(_ts(mine[ch])) < cfg["dm_active"]:
                if unrung.get(ch):
                    active.add(ch)
                    held.add(ch)
                continue
            if ch in held:
                held.discard(ch)
                unrung.pop(ch, None)
                if after.get(ch):
                    unrung[ch] = after[ch]
            after.pop(ch, None)
        self._state["held"] = sorted(held)
        return active

    def _tally(self, ch, m, counted):
        """Keep, for a DM, when the human last wrote in it and how many counted messages came after that."""
        if ch not in self._dm_ids:
            return
        mine, after = self._state["mine"], self._state["after"]
        if m.get("user") == self._me and m.get("subtype") in COUNTED_SUBTYPES:
            if ch not in mine or _ts(m["ts"]) > _ts(mine[ch]):
                mine[ch] = str(_ts(m["ts"]))
            after[ch] = 0
        elif counted and ch in mine:
            after[ch] = after.get(ch, 0) + 1

    def _pages(self, method, key, **params) -> list:
        out, cursor = [], None
        for _ in range(20):
            data = self._call(method, cursor=cursor, limit=200, **params)
            out += [c for c in data.get(key) or [] if isinstance(c, dict)]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    def _refresh_lists(self, now, cfg):
        # Every poll: a DM's `latest`, where Slack gives one, is what spares reading it.
        dms = self._pages("conversations.list", "channels", types="im,mpim", exclude_archived="true")
        self._dms = [(c["id"], _latest(c)) for c in dms
                     if CHANNEL_ID.match(str(c.get("id"))) and not c.get("is_user_deleted") and not c.get("is_archived")]
        self._dm_ids = {d for d, _ in self._dms}
        if self._listed_at is not None and now - self._listed_at < LIST_EVERY:
            return
        self._ext = []
        if cfg["ext"]:
            mine = self._pages("users.conversations", "channels", types="public_channel,private_channel",
                               exclude_archived="true")
            self._ext = [c["id"] for c in mine
                         if CHANNEL_ID.match(str(c.get("id"))) and str(c.get("name") or "").startswith("ext-")]
        self._listed_at = now

    @staticmethod
    def _turn(items, start, room):
        """`room` items from `items` starting at `start`, wrapping; and where the next turn starts."""
        if not items or room <= 0:
            return [], start
        start %= len(items)
        turn = (items[start:] + items[:start])[:room]
        return turn, start + len(turn)

    def _targets(self, now, cfg) -> list:
        """(conversation, how) for each read this poll, within HISTORY_BUDGET: the channels (up to CHANNEL_SHARE,
        more take turns); then the DMs Slack dates with a newer message, every DM active in the last week, a first
        read ("probe") of each DM whose last activity is not known yet, and the quieter DMs in turn.

        A conversation seen for the first time is read from the last completed poll, so a new DM's first message
        counts; only the very first poll (no `polled` yet) starts everything at now, or the window, and reads none."""
        seen, active = self._state["seen"], self._state["active"]
        polled = self._state.get("polled")
        start = polled or _fmt(now - self.window)
        channels = list(dict.fromkeys(cfg["channels"] + self._ext))
        fresh = set()
        for ch in channels + [d for d, _ in self._dms]:
            if ch not in seen:
                seen[ch] = start
                fresh.add(ch)
        skip = fresh if polled is None and not self.window else set()
        skip |= {ch for ch, until in self._skip.items() if now < until}
        readable = [ch for ch in channels if ch not in skip]
        chans, self._channel_cursor = self._turn(readable, self._channel_cursor, CHANNEL_SHARE)
        order = [(ch, "read") for ch in chans]
        dms = [(d, latest) for d, latest in self._dms if d not in skip and d not in channels]
        for d, latest in dms:
            if latest is not None and (d not in active or _ts(latest) > _ts(active[d])):
                active[d] = latest  # Slack's own date for it
        week = max(_ts(_fmt(now - ACTIVE)), 0)  # "0": a DM with no message at all is never active
        dated = [d for d, latest in dms if latest is not None and _ts(latest) > _ts(seen[d])]
        undated = [d for d, latest in dms if latest is None]
        recent = sorted((d for d in undated if d in active and _ts(active[d]) > week), key=lambda d: (-_ts(active[d]), d))
        probes = [d for d in undated if d not in active]
        quiet = sorted(d for d in undated if d in active and _ts(active[d]) <= week)
        room = HISTORY_BUDGET - len(order)
        order += [(d, "read") for d in dated[:room]]
        # First reads (once per DM) take what the active DMs leave, at least PROBE_FLOOR; the active DMs take the
        # rest in turn; a couple of reads stay for the quiet DMs.
        room = HISTORY_BUDGET - len(order) - min(len(quiet), KEEP_QUIET)
        first = probes[:max(room - len(recent), min(PROBE_FLOOR, room), 0)]
        busy, self._active_cursor = self._turn(recent, self._active_cursor, room - len(first))
        order += [(d, "read") for d in busy] + [(d, "probe") for d in first]
        turn, self._cursor = self._turn(quiet, self._cursor, HISTORY_BUDGET - len(order))
        order += [(d, "read") for d in turn]
        self.stats = {"dms": len(self._dms), "dated": sum(1 for _, latest in self._dms if latest is not None),
                      "active": len(recent) + sum(1 for d, latest in dms if latest is not None and d in active
                                                  and _ts(active[d]) > week),
                      "unknown": len(probes), "active_room": room, "reads": len(order)}
        return order

    def _counts(self, m, cfg) -> bool:
        subtype = m.get("subtype")
        if m.get("bot_id") or m.get("app_id") or subtype == "bot_message":
            return bool({m.get("bot_id"), m.get("app_id"), m.get("user")} & cfg["allow"])
        if subtype not in COUNTED_SUBTYPES:
            return False
        user = m.get("user")
        return bool(user) and user != self._me

    def _refused(self, ch, exc, now):
        """One conversation Slack refuses is left alone for an hour; anything wider fails the poll."""
        if exc.network or exc.token_wide or isinstance(exc, RateLimited):
            raise exc
        self._skip[ch] = now + SKIP_FOR
        self._notes.append(f"slack doorbell: skipping {ch} for an hour ({exc})")

    def _read_conversations(self, now, cfg):
        seen, unrung, threads = self._state["seen"], self._state["unrung"], self._state["threads"]
        active = self._state["active"]
        for ch, how in self._targets(now, cfg):
            oldest = seen[ch]
            try:
                if how == "probe":  # its newest messages, whatever their age: when it was last active
                    data = self._call("conversations.history", channel=ch, limit=PROBE_LIMIT)
                else:
                    data = self._call("conversations.history", channel=ch, oldest=oldest, limit=HISTORY_LIMIT)
            except SlackError as exc:
                self._refused(ch, exc, now)
                continue
            newest, n = _ts(oldest), 0
            last = None
            dated = [(_ts(m.get("ts")), m) for m in data.get("messages") or [] if isinstance(m, dict)]
            for ts, m in sorted(((ts, m) for ts, m in dated if ts is not None), key=lambda x: x[0]):
                last = ts if last is None else max(last, ts)
                if ts <= _ts(oldest):
                    continue
                newest = max(newest, ts)
                counted = self._counts(m, cfg)
                n += counted
                self._tally(ch, m, counted)
                if m.get("user") == self._me and m.get("thread_ts", m.get("ts")) == m.get("ts"):
                    threads.setdefault(ch, {})[m["ts"]] = m["ts"]
            seen[ch] = str(newest)
            if last is not None and (ch not in active or last > _ts(active[ch])):
                active[ch] = str(last)
            elif how == "probe":
                active.setdefault(ch, "0")  # no message at all: quiet
            if n:
                unrung[ch] = unrung.get(ch, 0) + n

    def _read_threads(self, now, cfg):
        threads, unrung = self._state["threads"], self._state["unrung"]
        followed = [(ch, t) for ch, ts in threads.items() if isinstance(ts, dict) for t in ts
                    if _ts(t) is not None and now - float(_ts(t)) < THREAD_FOLLOW and CHANNEL_ID.match(ch)]
        followed = sorted(followed, key=lambda x: -_ts(x[1]))[:THREAD_MAX]
        kept = {}
        for ch, t in followed:
            last = threads[ch][t]
            kept.setdefault(ch, {})[t] = last
            if now < self._skip.get(ch, 0):
                continue
            try:
                data = self._call("conversations.replies", channel=ch, ts=t, oldest=last, limit=HISTORY_LIMIT)
            except SlackError as exc:
                self._refused(ch, exc, now)
                continue
            newest, n = _ts(last), 0
            for m in data.get("messages") or []:
                ts = _ts(m.get("ts")) if isinstance(m, dict) else None
                if ts is None or ts <= _ts(last) or m.get("ts") == t:
                    continue
                newest = max(newest, ts)
                # A reply also sent to the channel was counted with the channel's messages.
                counted = m.get("subtype") != "thread_broadcast" and self._counts(m, cfg)
                n += counted
                if m.get("subtype") != "thread_broadcast":
                    self._tally(ch, m, counted)
            kept[ch][t] = str(newest)
            if n:
                unrung[ch] = unrung.get(ch, 0) + n
        self._state["threads"] = kept


class Background:
    """Runs a started Doorbell's checks on a thread of its own, so a slow Slack never holds up the watch's loop. The
    loop calls `check` as it would the Doorbell's: it hands over one result the thread found (lines, or the error to
    raise), and the loop alone prints."""

    def __init__(self, doorbell, pause=1.0):
        self._bell = doorbell
        self._pause = pause
        self._found = queue.Queue()
        self._notes = []
        self.error = None
        self.off = doorbell.off
        threading.Thread(target=self._loop, name="slack-doorbell", daemon=True).start()

    def _loop(self):
        failing = False
        while True:
            try:
                lines, exc = self._bell.check(), None
            except Exception as caught:  # handed to the loop, which tells it once
                lines, exc = [], caught
            notes = self._bell.notes()
            if lines or notes or exc or failing:
                self._found.put((lines, notes, exc))
            failing = exc is not None
            time.sleep(self._pause)

    def redact(self, text) -> str:
        return self._bell.redact(text)

    def notes(self) -> list:
        out, self._notes = self._notes, []
        return out

    def check(self) -> list:
        try:
            lines, notes, exc = self._found.get_nowait()
        except queue.Empty:
            return []
        self._notes += notes
        if exc is not None:
            raise exc
        return lines
