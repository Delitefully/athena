"""Which of athena's panes a revive brings back, and why each other one is left as it is.

A pane is HQ's (hq.json) or a worker's (the ledger). Only one whose block survived the restart, sitting at an idle shell with no Claude
of its own running, comes back, and only if it is athena's to bring back: never a retired, retiring or closed-by-human worker. Each pane
gets one Decision: revive, or skip with a Reason. The tick and the dry run reach the same list through `decide`.
"""
import enum
import os
from collections import namedtuple

from athena.kernel import agents, ledger, store
from athena.kernel.backend import BackendError

from . import script

SHELLS = frozenset({"bash", "csh", "dash", "elvish", "fish", "ksh", "nu", "sh", "tcsh", "xonsh", "zsh"})


class Kind(enum.Enum):
    HQ = "hq"
    WORKER = "worker"


class Reason(enum.Enum):
    """Why a pane is left as it is. The value is what the record keeps; `words` is what a person reads."""
    RETIRED = "retired"
    NOT_LIVE = "not-live"
    RETIRING = "retiring"
    RETIRE_UNFINISHED = "retire-unfinished"
    CLOSED_BY_HUMAN = "closed-by-human"
    NO_PANE = "no-pane"
    PANE_GONE = "pane-gone"
    NOT_ITS_PANE = "not-its-pane"
    CLAUDE_RUNNING = "claude-running"
    NO_CONVERSATION = "no-conversation"
    NO_SCRIPT = "no-script"
    NOT_ITS_SCRIPT = "not-its-script"
    STARTING = "starting"
    BUSY = "busy"
    UNKNOWN = "unknown"

    @property
    def words(self) -> str:
        return WORDS[self]


DOUBT = frozenset({Reason.STARTING, Reason.UNKNOWN})  # may pass in a moment: waited for while Rex is new, then said as trouble
SAID = DOUBT | {Reason.RETIRE_UNFINISHED}  # skips a person should hear of: a pane that may have been one to bring back, or a retire
# the restart cut short
UNREADABLE = (BackendError, ValueError, AttributeError)  # what asking Rex raises: its own errors, or an answer that doesn't parse

WORDS = {
    Reason.RETIRED: "retired",
    Reason.NOT_LIVE: "not live",
    Reason.RETIRING: "retiring",
    Reason.RETIRE_UNFINISHED: "a retire began and never finished",
    Reason.CLOSED_BY_HUMAN: "closed by the human",
    Reason.NO_PANE: "no pane on record",
    Reason.PANE_GONE: "its pane is gone",
    Reason.NOT_ITS_PANE: "its pane is labelled for someone else",
    Reason.CLAUDE_RUNNING: "its Claude is running",
    Reason.NO_CONVERSATION: "no conversation on record",
    Reason.NO_SCRIPT: "its launch script can't be read",
    Reason.NOT_ITS_SCRIPT: "its launch script isn't its Claude's launch",
    Reason.STARTING: "its pane shows login: the shell hasn't started, or something runs under it",
    Reason.BUSY: "its pane runs something else",
    Reason.UNKNOWN: "Rex or ps couldn't say",
}

Process = namedtuple("Process", "name argv0")
Pane = namedtuple("Pane", "name kind record session")
# reason None: revive, on `session`; `detail` is the shell it was found at.
Decision = namedtuple("Decision", "name kind reason detail session", defaults=("", None))


def is_shell(process) -> bool:
    """A login shell shows as -zsh; whichever of argv0 and name is there."""
    return any(os.path.basename((word or "").lstrip("-")) in SHELLS for word in (process.argv0, process.name))


def at_shell(process) -> bool:
    """An idle shell at its prompt. `login` never is: a shell not started yet, or a program exec'd under it."""
    return process is not None and process.name != "login" and is_shell(process)


class Look:
    """What Rex and ps say, asked once per pane at most and kept for one pass. `rex` is the module's adapter (`Rex` in __init__)."""

    def __init__(self, ctx, rex):
        self.ctx, self.rex = ctx, rex
        self._spaces = None
        self._blocks = {}
        self._running = None

    def pane(self, place):
        """(present, label) of a block. BackendError when Rex can't say."""
        if self._spaces is None:
            self._spaces = {s["id"] for s in self.ctx.backend.spaces()}
        if place.space not in self._spaces:
            return False, None
        if place.space not in self._blocks:
            self._blocks[place.space] = self.rex.blocks(self.ctx, place.space)
        blocks = self._blocks[place.space]
        return place.block in blocks, blocks.get(place.block)

    def present(self, place) -> bool:
        try:
            return self.pane(place)[0]
        except UNREADABLE:
            return False

    def running(self, name, among) -> bool:
        """Whether `name`'s own Claude runs: one ps for all of `among`. agents.Unknown when ps can't say."""
        if self._running is None:
            self._running = agents.running(among)
        return name in self._running

    def foreground(self, place):
        return self.rex.foreground(self.ctx, place)


def panes(ctx, look) -> list:
    """HQ's, then each worker that is live on this backend or still has a pane, by name. A retired worker whose pane is gone isn't
    athena's pane any more, so it isn't listed."""
    out = []
    hq = ctx.cap("hq")
    record = hq.load() if hq else {}
    if record.get("space"):
        out.append(Pane(hq.name(), Kind.HQ, record, hq.resumable(record)))
    spawn = ctx.cap("spawn")
    for name, worker in sorted(ledger.workers().items()):
        if not ledger.managed(worker):
            continue
        if worker.get("state") == "live" or (worker.get("block") and look.present(agents.place_of(worker))):
            out.append(Pane(name, Kind.WORKER, worker, spawn.conversation(name, worker) if spawn else worker.get("session")))
    return out


def _skip(pane, reason, detail=""):
    return Decision(pane.name, pane.kind, reason, str(detail or ""))


def not_to_revive(worker):
    """(Reason, detail) when the ledger says this worker isn't athena's to bring back, else None. Asked as panes are judged, and
    again at each one's turn: by then HQ, back first, may have begun to retire it."""
    if worker.get("state") == "retired":
        return Reason.RETIRED, ""
    if worker.get("state") != "live":
        return Reason.NOT_LIVE, worker.get("state")
    if worker.get("retiring"):  # retire's mark, set before it stops the Claude and cleared only as it ends, whatever its age
        return (Reason.RETIRING if ledger.is_retiring(worker) else Reason.RETIRE_UNFINISHED), ""
    if worker.get("closed_by_human"):
        return Reason.CLOSED_BY_HUMAN, worker["closed_by_human"]
    return None


def launch_script(name):
    """Where agents.launcher wrote its last launch: <state>/run/<name>.sh."""
    return store.path("run", f"{name}.sh")


def decide(pane, look, among) -> Decision:
    """The checks in order, cheapest and surest first; the first that fails names the reason. `among`: every pane's name, for one ps."""
    skipped = not_to_revive(pane.record) if pane.kind is Kind.WORKER else None
    if skipped:
        return _skip(pane, *skipped)
    place = agents.place_of(pane.record)
    if not place.space or not place.block:
        return _skip(pane, Reason.NO_PANE)
    try:
        present, label = look.pane(place)
        if not present:
            return _skip(pane, Reason.PANE_GONE)
        if label and label != pane.name:
            return _skip(pane, Reason.NOT_ITS_PANE, label)
        if look.running(pane.name, among):
            return _skip(pane, Reason.CLAUDE_RUNNING)
    except UNREADABLE as exc:  # agents.Unknown too
        return _skip(pane, Reason.UNKNOWN, exc)
    if not pane.session:
        return _skip(pane, Reason.NO_CONVERSATION)
    try:
        script.resumed(launch_script(pane.name).read_text(), pane.name, pane.session)  # made again at its turn, from the file then
    except OSError as exc:
        return _skip(pane, Reason.NO_SCRIPT, exc.strerror or exc)
    except script.NotLaunch as exc:
        return _skip(pane, Reason.NOT_ITS_SCRIPT, exc)
    try:
        process = look.foreground(place)
    except UNREADABLE as exc:
        return _skip(pane, Reason.UNKNOWN, exc)
    if at_shell(process):
        return Decision(pane.name, pane.kind, None, process.argv0 or process.name, pane.session)
    if process is None or process.name == "login":
        return _skip(pane, Reason.STARTING)
    return _skip(pane, Reason.BUSY, process.argv0 or process.name)

