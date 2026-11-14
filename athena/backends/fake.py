"""An in-file backend for tests: spaces and blocks in a JSON file, no programs. Tests set a block's screen and status."""
import json
import os

from athena.kernel.backend import Backend as Base, BackendError, Place


def _file():
    return os.environ["ATHENA_FAKE_BACKEND"]


def _load():
    try:
        with open(_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"up": True, "n": 0, "spaces": {}, "typed": [], "pressed": []}


def _save(data):
    with open(_file(), "w") as f:
        json.dump(data, f)


def _said(data, space, *events):
    """What Rex's event stream says, in its shape and order: (type, payload) of a session's own events, or a whole event."""
    for event in events:
        if isinstance(event, dict):
            data.setdefault("events", []).append(event)
        else:
            kind, payload = event
            data.setdefault("events", []).append({"type": "session_event", "payload": {"session_id": space, "event": {
                "type": kind, "payload": payload}}})


def _window_went(data, space, window):
    """A block's window, when that block was its last, goes with it, as in Rex."""
    found = data["spaces"].get(space) or {}
    if window and window in (found.get("windows") or {}) and not any(b.get("window") == window for b in found["blocks"].values()):
        found["windows"].pop(window, None)
        _said(data, space, ("window_closed", {"window_id": window}))


class Backend(Base):
    name = "fake"

    def _check(self, data):
        if not data.get("up", True):
            raise BackendError("server_down", "fake server down")

    def up(self):
        return _load().get("up", True)

    def start_server(self):
        data = _load()
        data["up"] = True
        _save(data)

    def create(self, label, cwd, argv, block_label=None, window_label=None):
        data = _load()
        self._check(data)
        data["n"] += 1
        space, block, window = f"s{data['n']}", f"b{data['n']}", f"w{data['n']}"
        data["spaces"][space] = {"label": label, "windows": {window: window_label or "claude"},
                                 "blocks": {block: {"label": block_label or label, "cwd": str(cwd), "argv": argv, "screen": "",
                                                    "status": None, "window": window}}}
        _save(data)
        return Place(space, block, window)

    def new_window(self, space, cwd, argv, window_label, block_label=None, focus=False):
        data = _load()
        self._check(data)
        if space not in data["spaces"]:
            raise BackendError("not_found", space)
        data["n"] += 1
        block, window = f"b{data['n']}", f"w{data['n']}"
        data["spaces"][space].setdefault("windows", {})[window] = window_label
        data["spaces"][space]["blocks"][block] = {"label": block_label or window_label, "cwd": str(cwd), "argv": argv, "screen": "",
                                                  "status": None, "window": window}
        _save(data)
        return Place(space, block, window)

    def split(self, place, cwd, argv, label, ratio=50):
        return self.new_window(place.space, cwd, argv, label, label)

    def spaces(self):
        data = _load()
        self._check(data)
        return [{"id": k, "label": v["label"]} for k, v in data["spaces"].items()]

    def has_block(self, place, strict=False):
        data = _load()
        self._check(data)
        return place.block in (data["spaces"].get(place.space) or {}).get("blocks", {})

    def _b(self, data, place):
        self._check(data)
        try:
            return data["spaces"][place.space]["blocks"][place.block]
        except KeyError:
            raise BackendError("not_found", str(place))

    def block_label(self, place):
        return self._b(_load(), place).get("label")

    def program_status(self, place):
        return self._b(_load(), place).get("status")

    def screen(self, place, styled=False):
        return self._b(_load(), place).get("screen", "")

    def type(self, place, text):
        data = _load()
        self._b(data, place)
        data["typed"].append([place.block, text])
        _save(data)

    def press(self, place, *keys):
        data = _load()
        self._b(data, place)
        data["pressed"].append([place.block, *keys])
        _save(data)

    def close_block(self, place):
        """As Rex: a window whose last block closes goes with it."""
        data = _load()
        found = data["spaces"].get(place.space) or {}
        gone = (found.get("blocks") or {}).pop(place.block, None)
        if gone is not None:
            _said(data, place.space, ("block_closed", {"block_id": place.block}))
            _window_went(data, place.space, gone.get("window"))
        _save(data)

    def close_window(self, place):
        data = _load()
        self._check(data)
        found = data["spaces"].get(place.space)
        if not found or place.window not in (found.get("windows") or {}):
            raise BackendError("not_found", str(place))
        found["windows"].pop(place.window)
        _said(data, place.space, ("window_closed", {"window_id": place.window}),
              *[("block_closed", {"block_id": k}) for k, v in found["blocks"].items() if v.get("window") == place.window])
        found["blocks"] = {k: v for k, v in found["blocks"].items() if v.get("window") != place.window}
        _save(data)

    def windows(self, space):
        data = _load()
        self._check(data)
        if space not in data["spaces"]:
            raise BackendError("not_found", space)
        return [{"id": k, "label": v} for k, v in (data["spaces"][space].get("windows") or {}).items()]

    def label(self, place, session=None, window=None, block=None):
        data = _load()
        found = data["spaces"].get(place.space)
        if not found:
            raise BackendError("not_found", str(place))
        if session is not None:
            found["label"] = session
        if window is not None:
            target = place.window or next((b.get("window") for k, b in found["blocks"].items() if k == place.block), None)
            if target:
                found.setdefault("windows", {})[target] = window
        if block is not None and place.block in found["blocks"]:
            found["blocks"][place.block]["label"] = block
        _save(data)

    def close_space(self, space):
        """As Rex: its blocks close and the session goes; no window is said to close."""
        data = _load()
        gone = data["spaces"].pop(space, None)
        if gone is not None:
            _said(data, space, *[("block_closed", {"block_id": k}) for k in gone.get("blocks") or {}],
                  {"type": "session_destroyed", "payload": {"session_id": space}})
        _save(data)

    def rename(self, space, label):
        data = _load()
        data["spaces"][space]["label"] = label
        _save(data)

    def to_front(self, space):
        pass

    def focus(self, space, window=None):
        pass

    def events(self, stop=None):
        """What was said since the last read, then the stream ends, as a server going away would end it."""
        data = _load()
        said = data.get("events") or []
        new = said[int(data.get("events_read") or 0):]
        if new:
            data["events_read"] = len(said)
            _save(data)
        yield from new
