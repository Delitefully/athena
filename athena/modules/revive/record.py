"""The record of the last Rex server instance handled, <state>/revive.json: explicit values, each a type, never inferred.

    no record, or unreadable --> ADOPT_NEXT: the next instance seen is adopted, never acted on
    turned on, or ADOPT_NEXT --> ADOPTED (instance): nothing done; a later instance is a restart
    a new instance           --> REVIVED (instance): each pane's Outcome, decided and written before anything is typed

A revived pane's State moves on tick by tick (`_advance` and `_follow` in __init__.py); a skipped pane has none:

    QUEUED --its turn--> SENDING --typed--> TYPED --its Claude up--> RUNNING, or UP --its prompt taken--> PROMPTED
                                  --Rex failed--> UNCONFIRMED (followed as TYPED)
           --by its turn no idle shell--> LEFT     --launch script not written--> FAILED
           --by its turn retired, retiring or closed--> STOOD_DOWN (quiet, as the same found when judged)
    TYPED, UNCONFIRMED, SENDING or UP past FOLLOW_LIMIT --> GAVE_UP;   Rex failing while it types the prompt --> PROMPT_UNSURE

The next QUEUED pane is typed once no pane is LAUNCHING within PACE_LIMIT of its typing. HQ is first in the record.
"""
import dataclasses
import enum
import time

from .plan import Kind, Reason


class How(enum.Enum):
    ADOPT_NEXT = "adopt-next"
    ADOPTED = "adopted"
    REVIVED = "revived"


class State(enum.Enum):
    QUEUED = "queued"              # on record, nothing typed yet: it waits its turn
    SENDING = "sending"            # its line is being typed: a tick that died here may or may not have typed it; never typed again
    FAILED = "failed"              # nothing typed: its launch script couldn't be written
    STOOD_DOWN = "stood-down"      # nothing typed: by its turn the ledger had it retired, retiring or closed by the human
    LEFT = "left"                  # nothing typed: by its turn its pane ran something else, or Rex couldn't say
    TYPED = "typed"                # its line went into the pane
    UNCONFIRMED = "unconfirmed"    # Rex failed while typing: the line may or may not have arrived
    UP = "up"                      # its Claude is up; its prompt waits until it is ready
    RUNNING = "running"            # its Claude is up, and it has no prompt to take
    PROMPTED = "prompted"          # its Claude took its prompt
    PROMPT_UNSURE = "prompt-unsure"  # Rex failed while typing its prompt: it may or may not have arrived
    GAVE_UP = "gave-up"            # its Claude not up, or its prompt not taken, within FOLLOW_LIMIT


LAUNCHING = frozenset({State.SENDING, State.TYPED, State.UNCONFIRMED})  # typed, its Claude not up yet: holds the queue
FOLLOWING = LAUNCHING | {State.UP}  # looked at each tick, until a final state
TROUBLE = frozenset({State.FAILED, State.LEFT, State.GAVE_UP, State.PROMPT_UNSURE})


@dataclasses.dataclass
class Outcome:
    name: str
    kind: Kind
    reason: Reason = None       # None: revived
    detail: str = ""
    state: State = None         # None: skipped
    session: str = None
    prompt: str = None          # what its Claude is told once it is ready: HQ's session start
    since: float = 0.0          # when `state` was entered

    @property
    def revived(self) -> bool:
        return self.reason is None

    def to_json(self) -> dict:
        return {"name": self.name, "kind": self.kind.value, "reason": self.reason and self.reason.value, "detail": self.detail,
                "state": self.state and self.state.value, "session": self.session, "prompt": self.prompt, "since": self.since}

    @classmethod
    def from_json(cls, data) -> "Outcome":
        reason = None if data["reason"] is None else Reason(data["reason"])
        state = None if data["state"] is None else State(data["state"])
        if (reason is None) == (state is None):
            raise ValueError(f"{data['name']}: a pane is revived with a state or skipped with a reason, never both or neither")
        return cls(_text(data["name"]), Kind(data["kind"]), reason, _text(data["detail"]), state, _optional(data["session"]),
                   _optional(data["prompt"]), float(data["since"]))


@dataclasses.dataclass
class Handled:
    how: How
    instance: str = None
    started_at: str = None
    at: float = 0.0
    panes: list = dataclasses.field(default_factory=list)

    @classmethod
    def adopt_next(cls) -> "Handled":
        return cls(How.ADOPT_NEXT)

    @classmethod
    def adopted(cls, instance, now=None) -> "Handled":
        return cls(How.ADOPTED, instance.id, instance.started_at, time.time() if now is None else now)

    def to_json(self) -> dict:
        return {"how": self.how.value, "instance": self.instance, "started_at": self.started_at, "at": self.at,
                "panes": [p.to_json() for p in self.panes]}

    @classmethod
    def from_json(cls, data) -> "Handled":
        how = How(data["how"])
        instance = _optional(data["instance"])
        if (how is How.ADOPT_NEXT) != (instance is None):
            raise ValueError("an instance is on record exactly when one was adopted or revived")
        if how is not How.REVIVED and data["panes"]:
            raise ValueError("only a revive has panes")
        return cls(how, instance, _optional(data["started_at"]), float(data["at"]), [Outcome.from_json(p) for p in data["panes"]])


def _text(value) -> str:
    if not isinstance(value, str):
        raise ValueError(f"not text: {value!r}")
    return value


def _optional(value):
    return None if value is None else _text(value)


def parse(data) -> Handled:
    """A record read from disk, or ADOPT_NEXT when there is none or it isn't one: a doubtful record never leads to typing."""
    try:
        return Handled.from_json(data)
    except (KeyError, TypeError, ValueError, AttributeError):
        return Handled.adopt_next()
