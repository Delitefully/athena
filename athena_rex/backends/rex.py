"""Rex: a session per repo with a window per worker, and HQ's own; each started directly with no shell to type into."""
import json
import os
import socket
import subprocess
import time

from athena_rex.kernel import config, world
from athena_rex.kernel.backend import Backend as Base, BackendError, Place

TERMINAL = "com.superlogical.terminal.shell"
SOCKET_PATH_LIMIT = 104
TYPE_PIECE = 200
TYPE_PAUSE = 0.05


def _size():
    columns, rows = str(config.rex("size")).split("x")
    return {"columns": int(columns), "rows": int(rows)}


def _block(label, cwd, argv):
    return {"block": {"flavor": TERMINAL, "label": label, "options": {
        "command": list(argv), "interactive": True, "cwd": str(cwd), "size": _size(), "exit": {"on_completion": False}}}}


class Backend(Base):
    name = "rex"

    def _base(self):
        server = config.rex("server")
        if not world.private_server(server):
            world.require(f"Rex server {server or '(the default)'}: only a private endpoint is open with the door shut;")
        return [str(config.rex("binary"))] + (["-S", server, "--autostart=false"] if server else [])

    def _run(self, *args, stdin=None, timeout=20) -> str:
        """Rex's own exit codes say which failure: 3 not found, 4 no server. Anything else keeps its number."""
        argv = self._base() + [str(a) for a in args]
        try:
            done = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BackendError("timeout", f"no answer to rex {args[0]} in {timeout}s")
        except OSError as exc:
            raise BackendError("no_binary", str(exc))
        if done.returncode == 0:
            return done.stdout
        named = {3: "not_found", 4: "server_down"}
        raise BackendError(named.get(done.returncode, f"exit_{done.returncode}"), (done.stderr or done.stdout).strip()[:300])

    def _api(self, method, args=None, session=None):
        scope = ["-s", session] if session else []
        return json.loads(self._run("api", "call", *scope, method, json.dumps(args or {})) or "{}")

    def _method(self, place, method, args=None):
        extra = [json.dumps(args)] if args else []
        return json.loads(self._run("block", "call", "-s", place.space, "-b", place.block, method, *extra) or "{}")

    def up(self) -> bool:
        """False only when it is really not there. A timeout says nothing about that, so it raises: slow is not dead."""
        try:
            self._run("whoami", "--json", timeout=5)
            return True
        except BackendError as exc:
            if exc.code == "timeout":
                raise
            return False

    def why_down(self) -> str:
        """Rex's answer is the same `no server` for all of these, and each wants something else done."""
        server = str(config.rex("server") or "")
        if not server.startswith("unix://"):
            return "not answering"
        path = server[len("unix://"):]
        if len(path.encode()) >= SOCKET_PATH_LIMIT:
            return f"unusable: its socket path is {len(path.encode())} bytes, and macOS allows {SOCKET_PATH_LIMIT}"
        if not os.path.exists(path):
            return "not running: its socket is gone"
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(2)
        try:
            probe.connect(path)
            return "not answering, though its socket takes connections"
        except OSError:
            return "dead: its socket is there and refuses connections"
        finally:
            probe.close()

    def start_server(self):
        world.require("starting a Rex server by repair")
        self._run("server", "start", timeout=30)

    def create(self, label, cwd, argv, block_label=None, window_label=None) -> Place:
        made = self._api("session.create", {"label": label[:128], "initial_windows": [
            {"window_label": (window_label or "claude")[:128], "layout": _block(block_label or label, cwd, argv)}]})
        window = made["initial_windows"][0]
        return Place(made["session_id"], window["block_ids"][0], window.get("window_id"))

    def new_window(self, space, cwd, argv, window_label, block_label=None) -> Place:
        made = self._api("session.new_window", {"session_id": space, "window_label": window_label[:128], "focus": False,
                                                "layout": _block(block_label or window_label, cwd, argv)}, space)
        return Place(space, made["block_ids"][0], made.get("window_id"))

    def replace(self, place, cwd, argv, block_label=None, window_label=None) -> Place:
        made = self._api("session.new_window", {"session_id": place.space, "window_label": (window_label or "claude")[:128],
                                                "focus": True, "layout": _block(block_label or "claude", cwd, argv)}, place.space)
        fresh = Place(place.space, made["block_ids"][0], made.get("window_id"))
        try:
            if place.block:
                self.close_block(place)
        except BackendError:
            pass
        return fresh

    def split(self, place, cwd, argv, label, ratio=50) -> Place:
        out = json.loads(self._run("split", "-s", place.space, "-b", place.block, "--split=right", "--ratio", ratio,
                                   "--label", label, "--focus=false", "-c", cwd, "--json", "--", *argv))
        return Place(place.space, out["block_ids"][0])

    def spaces(self) -> list:
        return [{"id": s["session_id"], "label": s.get("label")}
                for s in json.loads(self._run("ls", "--json")).get("sessions") or []]

    def windows(self, space) -> list:
        seen = json.loads(self._run("session", "inspect", space, "--json"))
        return [{"id": w.get("window_id"), "label": w.get("label")} for w in seen.get("windows") or []]

    def describe(self, space) -> dict:
        seen = json.loads(self._run("session", "inspect", space, "--json"))
        windows = seen.get("windows") or []
        blocks = sum(len(layer.get("blocks") or []) for window in windows for layer in window.get("layers") or [])
        clients = [c for c in seen.get("clients") or [] if (c.get("info") or {}).get("kind") != "cli"]
        return {"windows": len(windows), "blocks": blocks, "clients": len(clients)}

    def has_block(self, place) -> bool:
        try:
            blocks = json.loads(self._run("block", "ls", "-s", place.space, "--json")).get("blocks") or []
        except BackendError as exc:
            if exc.code == "server_down":
                raise
            return False
        return any(b.get("block_id") == place.block for b in blocks)

    def block_label(self, place):
        blocks = json.loads(self._run("block", "ls", "-s", place.space, "--json")).get("blocks") or []
        return next((b.get("label") for b in blocks if b.get("block_id") == place.block), None)

    def program_status(self, place):
        records = self._method(place, "program_status").get("records") or []
        root = next((r for r in records if not r.get("id")), None)
        return {"state": root.get("state"), "kind": root.get("kind"), "msg": root.get("msg")} if root else None

    def screen(self, place, styled=False) -> str:
        text = self._run("capture", "-s", place.space, "-b", place.block, "--format", "vt" if styled else "text")
        rows = self._method(place, "size").get("rows") or 24
        return "\n".join(text.splitlines()[-int(rows):]) + "\n"

    def type(self, place, text):
        """In pieces. Over about 800 characters at once Claude takes it for a paste: it arrives wrapped as data, and a /goal never
        runs. Measured on a real Claude: 3990 characters in pieces of 200 arrive whole and unwrapped."""
        for at in range(0, len(text), TYPE_PIECE):
            if at:
                time.sleep(TYPE_PAUSE)
            self._run("send", "-s", place.space, "-b", place.block, stdin=text[at:at + TYPE_PIECE])

    def press(self, place, *keys):
        self._run("send-key", "-s", place.space, "-b", place.block, *keys)

    def close_block(self, place):
        self._run("block", "close", "-s", place.space, place.block)

    def close_window(self, place):
        self._api("session.close_window", {"session_id": place.space, "window_id": place.window}, place.space)

    def label(self, place, session=None, window=None, block=None):
        if session is not None:
            self._api("session.set_label", {"session_id": place.space, "label": session[:128]}, place.space)
        if window is not None:
            target = place.window or next((b.get("window_id") for b in json.loads(self._run(
                "block", "ls", "-s", place.space, "--json")).get("blocks") or [] if b.get("block_id") == place.block), None)
            if target:
                self._api("session.set_window_label", {"session_id": place.space, "window_id": target, "label": window[:128]}, place.space)
        if block is not None and place.block:
            self._api("session.set_block_label", {"session_id": place.space, "block_id": place.block, "label": block[:128]}, place.space)

    def close_space(self, space):
        self._run("kill", space)

    def rename(self, space, label):
        self._run("session", "rename", space, label[:128])

    def to_front(self, space):
        self._run("session", "move", space, "--index", "1")

    def focus(self, space, window=None):
        self._run("do", "session.select", f"session_id={space}")
        if window:
            self._api("session.focus_window", {"session_id": space, "window_id": window}, space)
