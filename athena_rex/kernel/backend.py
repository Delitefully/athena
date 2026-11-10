"""The session backend: where programs run. Rex is the implementation that matters; `fake` is for tests.

A space is a top-level home (a Rex session): one per repo, holding a window per worker of that repo, and HQ's own. A place is a
space, a block (a Rex terminal) in it, and the window that holds the block. A worker from before repo sessions has a space of its
own and no window on record.
A backend starts programs and reads and types into terminals. What a Claude is, and how to treat one, lives in agents.py.
"""
import importlib
from collections import namedtuple

import time

from athena_rex.kernel import config, store

SESSIONS_FRESH_FOR = 120


class BackendError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


Place = namedtuple("Place", "space block window", defaults=(None,))


class Backend:
    name = "none"

    def up(self) -> bool:
        raise NotImplementedError

    def why_down(self) -> str:
        return "not answering"

    def block_label(self, place):
        """What this block was labelled when it was made, or None. A worker's block carries its name."""
        return None

    def start_server(self):
        raise NotImplementedError

    def create(self, label: str, cwd: str, argv: list, block_label: str = None, window_label: str = None) -> Place:
        """A new top-level space running argv directly, in the background, in one window. Its block stays after the program exits."""
        raise NotImplementedError

    def new_window(self, space: str, cwd: str, argv: list, window_label: str, block_label: str = None) -> Place:
        """A new window in an existing space, running argv, without taking focus."""
        raise NotImplementedError

    def replace(self, place: Place, cwd: str, argv: list, block_label: str = None, window_label: str = None) -> Place:
        """A new window in the same space, running argv; the old block, if any, closes. For a program that died."""
        raise NotImplementedError

    def split(self, place: Place, cwd: str, argv: list, label: str, ratio: int = 50) -> Place:
        raise NotImplementedError

    def spaces(self) -> list:
        """[{id, label}]"""
        raise NotImplementedError

    def describe(self, space: str) -> dict:
        """What a person sees of a space: {windows, blocks, clients}, where clients are the apps attached, not this command."""
        return {}

    def has_block(self, place: Place) -> bool:
        raise NotImplementedError

    def program_status(self, place: Place) -> dict:
        """What the program reports about itself: {state, kind, msg}, or None if it reports nothing."""
        raise NotImplementedError

    def screen(self, place: Place, styled: bool = False) -> str:
        """The visible rows, as text or with escape sequences."""
        raise NotImplementedError

    def type(self, place: Place, text: str):
        raise NotImplementedError

    def press(self, place: Place, *keys: str):
        raise NotImplementedError

    def close_block(self, place: Place):
        raise NotImplementedError

    def close_window(self, place: Place):
        raise NotImplementedError

    def windows(self, space: str) -> list:
        """[{id, label}] of the space's windows."""
        raise NotImplementedError

    def label(self, place: Place, session: str = None, window: str = None, block: str = None):
        """Sets whichever of the space's, the window's and the block's labels are given."""
        raise NotImplementedError

    def close_space(self, space: str):
        raise NotImplementedError

    def rename(self, space: str, label: str):
        raise NotImplementedError

    def to_front(self, space: str):
        raise NotImplementedError

    def focus(self, space: str, window: str = None):
        raise NotImplementedError


def last_look(now=None) -> dict:
    """The sessions the server last listed (run/sessions.json), or nothing if that is too old to speak for now. Kept by `sessions`, read
    here so that `status` stays a read of files and of its own module."""
    now = time.time() if now is None else now
    data = store.read_dict(store.path("run", "sessions.json"))
    return data if data.get("ts") and 0 <= now - float(data["ts"]) < SESSIONS_FRESH_FOR else {}


def session_of(record, seen) -> dict:
    """A worker's session as people know it: its label always, as the ledger has it, and `seen` False only when a fresh look missed it.
    A worker in its repo's session is found by its window, as the session outlives it; one from before, by its own session."""
    space, window = record.get("space"), record.get("window")
    out = {"id": space, "window": window, "label": record.get("label"), "seen": None}
    if seen and not seen.get("down") and space:
        found = next((s for s in seen.get("sessions") or [] if s["id"] == space), None)
        if window and found is not None and found.get("windows") is not None:
            out["seen"] = window in found["windows"]
        else:
            out["seen"] = found is not None
    return out


def load() -> Backend:
    name = config.rex("backend")
    return importlib.import_module(f"athena_rex.backends.{name}").Backend()
