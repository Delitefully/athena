#!/usr/bin/python3 -I
"""A stand-in for the Rex app, for tests: connects to a private Rex server as an app and answers its prompts as told.

    scripts/rex-app.py ENDPOINT [ANSWER ...] [--for SECONDS]

Each prompt an action puts up (`rex.ui.pick`, `rex.ui.input`) takes the next ANSWER:

    id:ID        choose the item with that id
    text:WORDS   type WORDS (an input, or a pick's own text when it allows other)
    cancel       close the prompt
    unsupported  say this app can't show it
    silent       leave it unanswered, so it times out

An action's request for the app to do something itself (`rex.client.queue`) is answered as done. Every event is printed as one
JSON line, the prompt it answered included, so a test can check what was asked. A prompt past the last ANSWER is left
unanswered. It stops once --for seconds (default 30) have passed. Only a private server's socket under /private/tmp/claude-<uid>/
is used: the human's own Rex is never reached.
"""
import base64
import json
import os
import socket
import struct
import sys
import time
from urllib.parse import unquote, urlparse

API_REVISION = 18  # prompts need 17


def _endpoint(text) -> str:
    url = urlparse(text)
    path = unquote(url.path) if url.scheme == "unix" else ""
    scratch = f"/private/tmp/claude-{os.getuid()}/"
    if not path or not os.path.realpath(path).startswith(scratch):
        sys.exit(f"rex-app: only a private server's unix socket under {scratch} is used, not {text}")
    return path


class Connection:
    """The control API's WebSocket on a Unix socket: text frames, masked as a client's must be."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /api/control HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "Sec-WebSocket-Protocol: rex.control.v1\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                sys.exit("rex-app: the server closed the connection during the handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            sys.exit(f"rex-app: no WebSocket: {head.splitlines()[0].decode(errors='replace')}")

    def send(self, message):
        data, mask = json.dumps(message).encode(), os.urandom(4)
        n = len(data)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else \
            bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(bytes([0x81]) + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _take(self, n) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the server closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def receive(self):
        """The next text message, or None for a close. Pings are answered on the way."""
        while True:
            first, second = self._take(2)
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            data, op = self._take(n), first & 0x0F
            if op == 8:
                return None
            if op == 9:
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                continue
            if op == 1:
                return json.loads(data)


def reply_for(answer) -> dict:
    """What client.reply carries for one ANSWER to one prompt."""
    if answer == "cancel":
        return {"result": {"cancelled": True}}
    if answer == "unsupported":
        return {"error": {"code": "not_supported", "message": "this stand-in shows no prompts"}}
    if answer.startswith("id:"):
        return {"result": {"ids": [answer[3:]]}}
    if answer.startswith("text:"):
        return {"result": {"text": answer[5:]}}
    raise SystemExit(f"rex-app: not an answer: {answer!r}")


def main(argv) -> int:
    seconds = 30.0
    if "--for" in argv:
        at = argv.index("--for")
        seconds = float(argv[at + 1])
        argv = argv[:at] + argv[at + 2:]
    if not argv:
        sys.exit(__doc__)
    path, answers = _endpoint(argv[0]), list(argv[1:])
    for answer in answers:
        if answer != "silent":
            reply_for(answer)  # a mistake in the answers stops it before it connects
    conn = Connection(path)
    conn.send({"type": "hello", "api_revision": API_REVISION, "client": {"name": "rex-app stand-in", "version": "1", "kind": "app"}})
    deadline = time.monotonic() + seconds
    replies = 0
    while time.monotonic() < deadline:
        conn.sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            message = conn.receive()
        except (socket.timeout, EOFError):
            break
        if message is None:
            break
        print(json.dumps(message, sort_keys=True), flush=True)
        if message.get("type") != "event":
            continue
        event = message.get("payload") or {}
        kind, body = event.get("type"), event.get("payload") or {}
        if kind == "client_prompt_requested":
            answer = answers.pop(0) if answers else "silent"
            if answer != "silent":
                replies += 1
                conn.send({"type": "request", "id": f"r{replies}", "method": "client.reply",
                           "payload": {"call_id": body["call_id"], **reply_for(answer)}})
        elif kind == "client_perform_requested":
            replies += 1
            conn.send({"type": "request", "id": f"r{replies}", "method": "client.reply", "payload": {"call_id": body["call_id"], "result": None}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
