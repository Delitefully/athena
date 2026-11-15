"""Revive: after the Rex server restarts, HQ and every live worker come back in their own panes, on their own conversations, with
the exact flags they were launched with. A module shipped with athena and on by default (docs/modules.md); `athena modules unload
revive` turns it off.

Rex keeps each session, window and block, ids and labels included, across a restart, but drops the command a block ran and starts
a login shell there instead. The watch ticks this module: once per Rex server instance (record.py), each athena pane that is an idle
shell with no Claude of its own (plan.py) gets its launch script made to resume its conversation (script.py), and `exec` of that
script typed into it, so the pane runs what athena started there, as it did before. HQ is then told its session start once it is
ready. `athena revive` is the dry run: what a restart would bring back now, and why each other pane is left.
"""
import argparse
import datetime
import json
import re
import shlex
import time
from collections import namedtuple

from athena.kernel import agents, caps, ledger, store
from athena.kernel.backend import BackendError

from . import plan, script
from .plan import Kind, Reason
from .record import FOLLOWING, LAUNCHING, TROUBLE, Handled, How, Outcome, State, parse

SUMMARY = "after a Rex restart, bring HQ and its workers back on their conversations"
RECORD = "revive.json"
LOCK = "locks/revive.lock"
SETTLE_LIMIT = 120   # seconds after Rex starts in which a pane in doubt (plan.DOUBT) is waited for, not skipped
FOLLOW_LIMIT = 600   # seconds a revived pane has to show its Claude, and to take its prompt
PACE_LIMIT = 60      # seconds the queue waits for a relaunched Claude to come up before the next pane is typed anyway
CONTINUE = "Rex restarted while you were mid-turn, and athena brought you back on this conversation: carry on where you left off."

Instance = namedtuple("Instance", "id started_at")
RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$")


class Rex:
    """What this module asks of Rex beyond kernel/backend.py's interface, in one place: only the rex backend has these. A test
    replaces this whole object."""

    def _rex(self, ctx):
        if ctx.backend.name != "rex":
            raise BackendError("not_rex", f"the {ctx.backend.name} backend has no Rex server to ask")
        return ctx.backend

    def instance(self, ctx):
        """The running server's Instance, or None when there is none to read."""
        try:
            status = self._rex(ctx)._api("server.status")
        except (BackendError, ValueError):
            return None
        found = status.get("instance_id") if isinstance(status, dict) else None
        return Instance(found, status.get("started_at")) if isinstance(found, str) and found else None

    def blocks(self, ctx, space) -> dict:
        """{block id: label} of a session's blocks."""
        listed = _answer(lambda: json.loads(self._rex(ctx)._run("block", "ls", "-s", space, "--json") or "{}"))
        return {b["block_id"]: b.get("label") for b in listed.get("blocks") or [] if isinstance(b, dict) and b.get("block_id")}

    def foreground(self, ctx, place):
        """The process in the pane's foreground, as Rex's terminal names it, or None."""
        found = _answer(lambda: self._rex(ctx)._method(place, "process")).get("foreground")
        return plan.Process(found.get("name"), found.get("argv0")) if isinstance(found, dict) else None


def _answer(ask) -> dict:
    """Rex's answer as an object. One that isn't is Rex unable to say, as any BackendError is: the pane is recorded as unknown."""
    try:
        found = ask()
    except ValueError as exc:
        raise BackendError("unreadable", f"Rex's answer didn't parse: {exc}")
    if not isinstance(found, dict):
        raise BackendError("unreadable", f"Rex answered {type(found).__name__}, not an object")
    return found


REX = Rex()


def load() -> Handled:
    return parse(store.read_json(store.path(RECORD), None))


def save(handled: Handled):
    store.write_json(store.path(RECORD), handled.to_json(), indent=1)


def started(instance):
    """When Rex says the instance started, in epoch seconds, or None when it says nothing readable. Rex writes RFC 3339, with
    nanoseconds at times, which Python 3.9 won't read: the fraction is left out."""
    found = RFC3339.match(str(instance.started_at or ""))
    if not found:
        return None
    return datetime.datetime.strptime(found.group(1) + found.group(2), "%Y-%m-%dT%H:%M:%S%z").timestamp()


def decisions(ctx) -> list:
    """Each athena pane's Decision, as a restart now would make it. Reads only: the tick and the dry run share it."""
    look = plan.Look(ctx, REX)
    found = plan.panes(ctx, look)
    names = [p.name for p in found]
    return [plan.decide(p, look, names) for p in found]


def _settling(made, instance, now) -> bool:
    """Whether a pane is in doubt (still at login, or Rex or ps couldn't say) while Rex is new enough for that to pass: worth
    another tick before deciding. With Rex's start unreadable there is nothing to count from, so it decides at once."""
    began = started(instance)
    return began is not None and now - began < SETTLE_LIMIT and any(d.reason in plan.DOUBT for d in made)


def _prompt(decision):
    """What a revived Claude is told once it is ready: HQ its session start; a worker that was mid-turn when Rex went, to carry on,
    as the reply it was writing is gone. Read before its Claude comes back, whose SessionStart moves its state on."""
    if decision.kind is Kind.HQ:
        hq = caps.get("hq")
        return hq.ROUTINE if hq else None
    working = store.read_dict(store.path("workers", f"{decision.name}.json")).get("state") == "working"
    return CONTINUE if working else None


def _target(outcome) -> dict:
    """The record agents.py reads for this pane: its place and name."""
    if outcome.kind is Kind.HQ:
        return {**caps.get("hq").load(), "name": outcome.name}
    return {**(ledger.get(outcome.name) or {}), "name": outcome.name}


def _stood_down(outcome) -> str:
    """Why the ledger no longer has this worker to bring back, asked again at its turn; "" when it still does. Panes are judged in
    one pass and typed into one by one, minutes apart: meanwhile HQ, back first, may begin a retire."""
    skipped = plan.not_to_revive(ledger.get(outcome.name) or {}) if outcome.kind is Kind.WORKER else None
    return f"by its turn it was {skipped[0].words}" if skipped else ""


def _gone_since(ctx, target) -> str:
    """Why the pane is no longer at an idle shell, asked again at its turn; "" when it still is: a person may have started something
    there since it was judged."""
    try:
        process = REX.foreground(ctx, target)
    except plan.UNREADABLE as exc:
        return f"Rex couldn't say what it runs now: {exc}"
    if plan.at_shell(process):
        return ""
    return f"by its turn it ran {(process.argv0 or process.name) if process else 'nothing Rex could name'}"


def _move(outcome, state, detail, now):
    outcome.state, outcome.detail, outcome.since = state, detail, now


def _relaunch(ctx, handled, outcome, now) -> list:
    """Its turn: asked again that it is still one to bring back, at an idle shell; its launch script made, from the script on disk
    now, to resume its conversation; the stale pid gone, so only the relaunch's own is ever read; then `exec` of the script typed in,
    on record as SENDING first. HQ's record takes the conversation it came back on, as `hq open` would. Lines to tell back."""
    from athena.caps.watch import Line
    target = agents.place_of(_target(outcome))
    down = _stood_down(outcome)
    if down:
        _move(outcome, State.STOOD_DOWN, down, now)
        return []
    left = _gone_since(ctx, target)
    if left:
        _move(outcome, State.LEFT, left, now)
        return []
    path = plan.launch_script(outcome.name)
    try:
        store.write_text(path, script.resumed(path.read_text(), outcome.name, outcome.session), mode=0o700)
        agents.pid_file(outcome.name).unlink(missing_ok=True)
    except (OSError, script.NotLaunch) as exc:
        _move(outcome, State.FAILED, f"launch script not made: {exc}", now)
        return []
    _move(outcome, State.SENDING, "", now)
    save(handled)
    try:
        ctx.backend.press(target, "ctrl+c")  # whatever is pending goes, a continued line too, so it can't run joined to this
        ctx.backend.type(target, f"exec /bin/sh {shlex.quote(str(path))}")
        ctx.backend.press(target, "enter")
        _move(outcome, State.TYPED, "", now)
    except plan.UNREADABLE as exc:
        _move(outcome, State.UNCONFIRMED, f"Rex: {exc}", now)
    if outcome.kind is Kind.HQ:
        hq = caps.get("hq")
        record = hq.load()
        if record.get("session") != outcome.session:
            try:
                hq._save({**record, "session": outcome.session})
            except OSError as exc:  # its Claude is on its way all the same; resumable() reads hq-start.json first
                return [Line(f"revive: {outcome.name} hq.json not updated: {exc}", "wake", outcome.name)]
    return []


def _trouble(handled) -> list:
    """The panes a person should look at: one that didn't come back, or whose prompt may not have arrived, and one left in doubt."""
    return [o for o in handled.panes if o.state in TROUBLE or o.reason in plan.SAID]


def _summary(handled) -> str:
    revived = [o.name for o in handled.panes if o.revived]
    skipped = [f"{o.name} ({o.reason.words})" for o in handled.panes if not o.revived]
    return (f"reviving {', '.join(revived) + ', one at a time' if revived else 'none'}"
            + (f"; skipped {', '.join(skipped)}" if skipped else ""))


def revive(ctx, instance, made, now) -> Handled:
    """One restart handled: every decision on record before the first keystroke, each pane to bring back QUEUED in the record's
    order, HQ first. `_advance` types them, one at a time."""
    handled = Handled(How.REVIVED, instance.id, instance.started_at, now, [
        Outcome(d.name, d.kind, d.reason, d.detail if d.reason else "", None if d.reason else State.QUEUED, d.session,
                None if d.reason else _prompt(d), now) for d in made])
    save(handled)
    return handled


def _advance(ctx, handled, now) -> list:
    """The next QUEUED pane is typed once no pane is still coming up within PACE_LIMIT: Claudes started together have taken this Mac
    down. One that ends at once (LEFT, FAILED) holds nothing, so the next goes in the same tick."""
    from athena.caps.watch import Line
    lines = []
    while not any(o.state in LAUNCHING and now - o.since < PACE_LIMIT for o in handled.panes):
        turn = next((o for o in handled.panes if o.state is State.QUEUED), None)
        if turn is None:
            break
        lines += _relaunch(ctx, handled, turn, now)
        save(handled)
        if turn.state in TROUBLE:
            lines.append(Line(f"revive: {turn.name} {turn.detail}", "wake", turn.name))
    return lines


def _up(ctx, outcome) -> bool:
    """Its Claude runs and has drawn itself: it reports a state, or shows a startup prompt. Unreadable counts as not yet, and why
    is kept on record, so a give-up says it."""
    try:
        return agents.state(ctx.backend, _target(outcome))["state"] not in (None, "starting")
    except plan.UNREADABLE as exc:
        outcome.detail = f"its state couldn't be read: {exc}"
        return False


def _offer(ctx, outcome, now):
    """Its prompt, typed only once its Claude is ready and nobody's text is in its box. A refusal, or a state nobody could read,
    waits for the next tick; only Rex failing while it types leaves the prompt unsure."""
    target = _target(outcome)
    try:
        ready = agents.state(ctx.backend, target)["state"] in agents.READY
    except plan.UNREADABLE as exc:
        outcome.detail = f"prompt waits: its state couldn't be read: {exc}"
        return
    if not ready:
        return
    try:
        agents.send(ctx.backend, target, outcome.prompt)
        _move(outcome, State.PROMPTED, "", now)
    except agents.Refused as exc:
        outcome.detail = f"prompt waits: {exc}"
    except BackendError as exc:  # from here on, Rex may have taken some of it
        _move(outcome, State.PROMPT_UNSURE, f"Rex: {exc}", now)
    except (ValueError, AttributeError) as exc:  # an answer send's own looks couldn't parse, before it typed anything
        outcome.detail = f"prompt waits: Rex's answer didn't parse: {exc}"


def _follow(ctx, outcome, now) -> bool:
    """One look at a revived pane on its way. True when its state or what it waits on changed, so it is saved."""
    before = (outcome.state, outcome.detail)
    if outcome.state in LAUNCHING:
        if _up(ctx, outcome):
            _move(outcome, State.UP if outcome.prompt else State.RUNNING, "", now)
        elif now - outcome.since > FOLLOW_LIMIT:
            _move(outcome, State.GAVE_UP, "its Claude never came up" + (f" ({outcome.detail})" if outcome.detail else ""), now)
    if outcome.state is State.UP:
        _offer(ctx, outcome, now)
        if outcome.state is State.UP and now - outcome.since > FOLLOW_LIMIT:
            _move(outcome, State.GAVE_UP, "its Claude never took its prompt" + (f" ({outcome.detail})" if outcome.detail else ""), now)
    return (outcome.state, outcome.detail) != before


def _follow_all(ctx, handled, now) -> list:
    from athena.caps.watch import Line
    lines = []
    for outcome in handled.panes:
        if outcome.state in FOLLOWING and _follow(ctx, outcome, now):
            save(handled)
            if outcome.state in TROUBLE:
                lines.append(Line(f"revive: {outcome.name} {outcome.detail}", "wake", outcome.name))
    return lines


def step(ctx, now) -> list:
    """One tick: a new instance is handled once, an old one only followed. Watch Lines back."""
    from athena.caps.watch import Line
    instance = REX.instance(ctx)
    if instance is None:
        return []
    with store.locked(LOCK):
        handled = load()
        if handled.how is How.ADOPT_NEXT:
            save(Handled.adopted(instance, now))
            return []
        if handled.instance == instance.id:
            return _follow_all(ctx, handled, now) + _advance(ctx, handled, now)
        made = decisions(ctx)
        if _settling(made, instance, now):
            return []
        handled = revive(ctx, instance, made, now)
        said = Line(f"Rex restarted: {_summary(handled)}", "wake" if _trouble(handled) else "routine")
        return [said] + _advance(ctx, handled, now)


def tick(ctx, now):
    return step(ctx, now)


def start(ctx):
    """Turned on: the running instance is adopted, so only a restart seen from now on is acted on. Rex unreadable: the next seen is."""
    instance = REX.instance(ctx)
    with store.locked(LOCK):
        if instance is None or load().instance != instance.id:
            save(Handled.adopted(instance) if instance else Handled.adopt_next())


def _when(at) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def last_line(handled) -> str:
    if handled.how is How.ADOPT_NEXT:
        return "no Rex instance on record yet: the next one seen is adopted, not acted on"
    if handled.how is How.ADOPTED:
        return f"Rex {handled.instance[:8]} adopted {_when(handled.at)}, nothing revived; a restart from here on is acted on"
    states = [f"{o.name} {o.state.value}" for o in handled.panes if o.revived]
    skipped = [f"{o.name} ({o.reason.words})" for o in handled.panes if not o.revived]
    return (f"Rex restart {handled.instance[:8]} handled {_when(handled.at)}: revived {', '.join(states) or 'none'}"
            + (f"; skipped {', '.join(skipped)}" if skipped else ""))


def checks(ctx):
    handled = load()
    trouble = _trouble(handled)
    detail = last_line(handled) + "".join(f"; {o.name}: {o.detail or o.reason.words}" for o in trouble)
    return [caps.Check("revive", not trouble, detail, short="trouble" if trouble else "")]


def status(ctx) -> tuple:
    handled = load()
    trouble = [o.name for o in _trouble(handled)]
    if trouble:
        return False, f"didn't come back: {', '.join(trouble)}"
    if handled.how is How.REVIVED:
        return True, f"last restart {_when(handled.at)}: revived {sum(o.revived for o in handled.panes)}"
    return True, "waiting for a Rex restart"


def _row(decision) -> dict:
    out = {"name": decision.name, "kind": decision.kind.value, "revive": decision.reason is None}
    if decision.reason is None:
        told = _prompt(decision)
        then = None if told is None else "its session start" if decision.kind is Kind.HQ else "a nudge to carry on"
        worker = ledger.get(decision.name) if decision.kind is Kind.WORKER else None
        out.update(session=decision.session, at=decision.detail, then=then, agent=agents.agent_of(worker))
    else:
        out.update(reason=decision.reason.value, why=decision.reason.words, detail=decision.detail)
    return out


def dry_run(ctx) -> dict:
    instance = REX.instance(ctx)
    return {"instance": instance and instance._asdict(), "last": load().to_json(),
            "panes": [_row(d) for d in decisions(ctx)]}


def describe(out) -> list:
    now = out["instance"]
    lines = [f"Rex now: instance {now['id'][:8]}, started {now['started_at']}" if now else "Rex now: no server to read",
             "last handled: " + last_line(parse(out["last"])), "dry run, nothing typed: a revive now would"]
    for row in out["panes"]:
        if row["revive"]:
            how = f"pi --session-id {row['session']}" if row.get("agent") == "pi" else f"--resume {row['session']}"
            lines.append(f"  revive  {row['name']:<20} {row['kind']:<6} {how} into its idle {row['at']}"
                         + (f", then {row['then']}" if row["then"] else ""))
        else:
            lines.append(f"  skip    {row['name']:<20} {row['kind']:<6} {row['why']}" + (f": {row['detail']}" if row["detail"] else ""))
    if not out["panes"]:
        lines.append("  find no athena pane")
    return lines


def _configure(p: argparse.ArgumentParser):
    p.add_argument("--json", action="store_true")


def _run(args):
    out = dry_run(caps.CTX)
    print(json.dumps(out, indent=1) if args.json else "\n".join(describe(out)))
    return 0


def commands():
    return {"revive": caps.Command(SUMMARY + " (this verb is the dry run)", _configure, _run)}
